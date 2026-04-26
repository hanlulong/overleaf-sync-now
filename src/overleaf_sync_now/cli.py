"""overleaf-sync-now CLI.

Subcommands:
  install                       Link skill into Claude Code/Codex + add hook + run setup
  login                         Open a controlled browser to log into Overleaf and capture
                                the cookie via DevTools Protocol (the proper fix for Chrome
                                130+ app-bound encryption). One-time, persists for weeks.
  setup                         Auth setup (auto-detect from existing browsers/profiles)
  save-cookie <value>           Persist a manually-pasted overleaf_session2 cookie value
  link <project_id> [folder]    Mark a folder as belonging to an Overleaf project (override)
  sync [folder] [--force]       Refresh the linked folder against Overleaf. Probes
                                /project/<id>/updates and downloads the zip only when
                                a web-origin change has actually happened. --force always
                                re-extracts.
  status [folder]               Show data dir, cookie validity, and link/sync state
  projects [--refresh]          List your Overleaf projects (name + ID)
  doctor                        Verbose diagnostic dump of the auth chain
  hook                          PreToolUse hook entrypoint (reads JSON from stdin)
  uninstall                     Remove skill links and hook (cookies preserved)

Auth chain (first hit wins): cached cookies -> persistent `login` browser
profile (the proper Chrome 130+ Windows fix) -> rookiepy (Rust cookie reader,
Chrome 127+ friendly) -> browser_cookie3 (legacy DPAPI) -> Claude Code's
Playwright profile -> interactive paste prompt (setup/login only).

Project-folder mapping (since 0.3.0):
  1. `.overleaf-project` marker file in the folder, or any parent up to the
     filesystem root, takes priority. Validated against the cached projects
     index (warns on trashed/archived/missing-from-account).
  2. Else, if the file lives under <anywhere>/Apps/Overleaf/<project>/, the
     resolver looks up <project> in the cached project records (filtering
     trashed/archived; refusing to guess when two living projects share a
     name; auto-resolving via fingerprint match against /updates Dropbox-
     origin pathnames when ambiguous; auto-writing the marker on success).
  3. Else, no link — the hook exits 0 (other tools pass through).

Refresh flow:
  1. GET /project/<id>/updates -- cheap probe (~0.3s, ~30 KB) that returns the
     version history with per-update pathnames and origin.
  2. If latest toV == cached toV for this project: no-op.
  3. Else walk updates back to cached toV; skip updates with origin.kind="dropbox"
     (those are the local->Dropbox->Overleaf round-trips of our own saves).
  4. If anything web-origin remains, GET /project/<id>/download/zip and extract
     only the affected pathnames, writing atomically and only when content
     actually differs from local (so Dropbox upload pressure stays at zero
     when local already matches).
"""
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile


def _data_dir():
    """Universal location for runtime data. The earlier `~/.claude/overleaf-data`
    path is honored when present so existing installs don't lose state."""
    env = os.environ.get("OVERLEAF_SYNC_DATA_DIR")
    if env:
        return pathlib.Path(env)
    claude_dir = pathlib.Path.home() / ".claude" / "overleaf-data"
    if claude_dir.exists():
        return claude_dir
    return pathlib.Path.home() / ".overleaf-sync"


CACHE_DIR = _data_dir()
CACHE_FILE = CACHE_DIR / "cookies.json"
STATE_FILE = CACHE_DIR / "state.json"
VERSIONS_FILE = CACHE_DIR / "versions.json"
INDEX_FILE = CACHE_DIR / "projects.json"
INDEX_TTL = 86400
INDEX_FORMAT_VERSION = 2
PLAYWRIGHT_PROFILE = pathlib.Path.home() / ".claude" / "playwright-profile"
PROJECT_MARKER = ".overleaf-project"
DEBOUNCE_SECONDS = 30
BASE = "https://www.overleaf.com"
SESSION_COOKIE = "overleaf_session2"
# Layer 6b: how many recent dropbox-origin update entries to use as a
# fingerprint of "this project is the local folder". Bounded to keep the
# extra stat() calls negligible on a Dropbox folder (which can be slow).
FINGERPRINT_RECENT_DBX_ENTRIES = 5
FINGERPRINT_MAX_PATHS = 10

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
SKILL_MD_SRC = PACKAGE_DIR / "SKILL.md"


# -------- auth chain --------

def _unique_tmp_path(target):
    """Build a sibling tempfile name unique across processes AND threads.

    Two writers (e.g. PreToolUse hooks fired by two Codex CLI windows on the
    same project at once) must not race on the same tmp filename — each
    `open(tmp, "w")` would truncate the other's content. PID + 8 random hex
    chars makes collisions astronomically unlikely even across rapid PID
    reuse or in-process threading."""
    target = pathlib.Path(target)
    return target.with_name(f"{target.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")


def _replace_with_retry(src, dst):
    """os.replace, retrying transient Windows ACCESS_DENIED.

    On Windows, os.replace is implemented via MoveFileEx and can briefly
    fail with PermissionError if another process/thread is in the middle of
    its own ReplaceFile call against the same target — or if a virus
    scanner / Dropbox / OneDrive client momentarily has the target open.
    Exponential backoff with jitter breaks thread-herding under contention.
    Cross-platform: a no-op extra wrapper on POSIX where replace doesn't
    exhibit this behavior."""
    import random
    last_err = None
    for attempt in range(15):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError as e:
            last_err = e
            # Cap per-sleep ~50ms; total worst-case ~700ms across all
            # retries. Jitter avoids two contending threads waking up in
            # lockstep on every retry.
            delay = min(0.05, 0.001 * (2 ** attempt)) * (0.5 + random.random())
            time.sleep(delay)
    raise last_err


def _atomic_write_text(path, text):
    """Write `text` to `path` atomically: write to a unique sibling tempfile,
    then os.replace. Avoids leaving the destination in a half-written state
    if the process is killed (which would corrupt e.g. ~/.claude/settings.json).
    Concurrency-safe across processes and threads (see _unique_tmp_path and
    _replace_with_retry).

    On POSIX, files written under our data dir get 0o600 (user-only read+write)
    so cookie values, project IDs, and ownership metadata don't leak to other
    local users. Files outside the data dir (e.g. ~/.claude/settings.json,
    `.overleaf-project` markers in user folders) keep system defaults — those
    are deliberately readable by whatever process consumes them."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(path)
    try:
        with open(tmp, "w") as f:
            f.write(text)
        # Restrict perms BEFORE the atomic replace so the target file never
        # exists with permissive perms — even briefly. POSIX-only; on Windows
        # ACL inheritance from the parent dir is the relevant control.
        if os.name == "posix":
            try:
                if _is_under_data_dir(path):
                    os.chmod(tmp, 0o600)
            except OSError:
                pass
        _replace_with_retry(tmp, path)
    except Exception:
        # Best-effort cleanup of the tmp file if anything between open and
        # replace failed. The exception is re-raised so the caller still sees
        # the failure; we just don't want orphaned .tmp-PID-XXXX files
        # accumulating in the data dir.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _is_under_data_dir(path):
    """True if `path` is inside our data dir. Used to gate POSIX 0o600 perms
    in _atomic_write_text — we restrict perms on files we own (cookies,
    state, versions, projects, validated-at) but not on user-facing files
    like the .overleaf-project marker (which Dropbox needs to read)."""
    try:
        path = pathlib.Path(path).resolve()
        data = pathlib.Path(CACHE_DIR).resolve()
        # Path.is_relative_to is 3.9+; emulate for 3.8 compat.
        try:
            return path.is_relative_to(data)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                path.relative_to(data)
                return True
            except ValueError:
                return False
    except OSError:
        return False


def _save_cache(cookies):
    # POSIX 0o600 is applied by _atomic_write_text (CACHE_FILE is under
    # CACHE_DIR; see _is_under_data_dir).
    _atomic_write_text(CACHE_FILE, json.dumps(cookies))
    # Successful save => fresh cookies. Skip re-validation for the next minute.
    _mark_cookies_validated()


VALIDATION_TTL_SECONDS = 60
_VALIDATION_FILE = CACHE_DIR / ".validated-at"


def _mark_cookies_validated():
    try:
        _atomic_write_text(_VALIDATION_FILE, str(time.time()))
    except OSError:
        pass


def _cookies_recently_validated():
    """Returns True if we validated successfully within the last VALIDATION_TTL_SECONDS.
    Skips a redundant network call to /project on every hook fire."""
    try:
        with open(_VALIDATION_FILE) as f:
            ts = float(f.read().strip())
        return (time.time() - ts) < VALIDATION_TTL_SECONDS
    except Exception:
        return False


def _load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception as e:
            # Corrupt cache file shouldn't be silent — user has no other
            # signal something is wrong.
            print(
                f"[overleaf-sync-now] WARNING: cache file is corrupt ({CACHE_FILE}): {e}\n"
                f"  Auth will fall back through the chain. To clear, delete the file.",
                file=sys.stderr,
            )
            return None
    return None


def _validate_cookies(cookies, *, use_cache=True):
    """Validate cookies against Overleaf. Set use_cache=False to force a fresh
    network probe (used by `doctor` and the periodic hook re-validation)."""
    if not cookies or SESSION_COOKIE not in cookies:
        return False
    # Hot path: cookie shape is right and we just validated successfully —
    # skip the network round-trip. Keeps the hook fast on every AI edit.
    if use_cache and _cookies_recently_validated():
        return True
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".overleaf.com")
    try:
        r = s.get(f"{BASE}/project", allow_redirects=False, timeout=10)
        ok = r.status_code == 200
    except Exception:
        ok = False
    if ok:
        _mark_cookies_validated()
    return ok


def _try_rookiepy():
    """rookiepy is a Rust-backed cookie reader that handles Chrome 127+
    app-bound encryption better than browser_cookie3 on Windows."""
    try:
        import rookiepy
    except ImportError:
        return None
    for fn_name in ("chrome", "edge", "brave", "vivaldi", "chromium", "opera", "firefox", "librewolf", "load"):
        try:
            fn = getattr(rookiepy, fn_name, None)
            if not fn:
                continue
            raw = fn(["overleaf.com", ".overleaf.com", "www.overleaf.com"])
            cookies = {c["name"]: c["value"] for c in raw}
            if SESSION_COOKIE in cookies:
                return cookies
        except Exception:
            continue
    return None


def _try_browser_cookie3():
    try:
        import browser_cookie3
    except ImportError:
        return None
    for fn_name in ("chrome", "edge", "brave", "vivaldi", "chromium", "opera", "firefox", "librewolf"):
        try:
            fn = getattr(browser_cookie3, fn_name, None)
            if not fn:
                continue
            cj = fn(domain_name="overleaf.com")
            cookies = {c.name: c.value for c in cj}
            if SESSION_COOKIE in cookies:
                return cookies
        except Exception:
            continue
    try:
        cj = browser_cookie3.load(domain_name="overleaf.com")
        cookies = {c.name: c.value for c in cj}
        if SESSION_COOKIE in cookies:
            return cookies
    except Exception:
        pass
    return None


def _try_playwright_profile():
    cookies_db = PLAYWRIGHT_PROFILE / "Default" / "Network" / "Cookies"
    key_file = PLAYWRIGHT_PROFILE / "Local State"
    if not cookies_db.exists() or not key_file.exists():
        return None
    try:
        import browser_cookie3
    except ImportError:
        return None
    for attempt in ("direct", "copy"):
        try:
            if attempt == "direct":
                cj = browser_cookie3.chromium(
                    cookie_file=str(cookies_db), key_file=str(key_file), domain_name="overleaf.com"
                )
            else:
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    shutil.copyfile(str(cookies_db), tmp_path)
                    cj = browser_cookie3.chromium(
                        cookie_file=tmp_path, key_file=str(key_file), domain_name="overleaf.com"
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            cookies = {c.name: c.value for c in cj}
            if SESSION_COOKIE in cookies:
                return cookies
        except Exception:
            continue
    return None


def _resolve_cookies(interactive=False):
    cached = _load_cache()
    if _validate_cookies(cached):
        return cached
    # Persistent browser profile from `login` command (the proper fix for
    # Chrome 130+ app-bound encryption). Browser API can read HttpOnly cookies
    # that no on-disk extractor can decrypt without admin.
    via_login = _try_login_profile()
    if _validate_cookies(via_login):
        _save_cache(via_login)
        return via_login
    # rookiepy: Chrome 127+ friendly via Rust
    via_rookie = _try_rookiepy()
    if _validate_cookies(via_rookie):
        _save_cache(via_rookie)
        return via_rookie
    via_browser = _try_browser_cookie3()
    if _validate_cookies(via_browser):
        _save_cache(via_browser)
        return via_browser
    via_playwright = _try_playwright_profile()
    if _validate_cookies(via_playwright):
        _save_cache(via_playwright)
        return via_playwright
    if interactive:
        manual = _prompt_manual_cookie()
        if _validate_cookies(manual):
            _save_cache(manual)
            return manual
    return None


def _prompt_manual_cookie():
    print()
    print("Could not auto-detect a logged-in Overleaf session.")
    print()
    print("Manual setup:")
    print("  1. Open https://www.overleaf.com in any browser and log in.")
    print("  2. Press F12 -> Application tab -> Storage -> Cookies -> https://www.overleaf.com")
    print(f"  3. Find the row named '{SESSION_COOKIE}' and copy the entire 'Value' field.")
    print()
    try:
        value = input(f"Paste the {SESSION_COOKIE} cookie value here: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not value:
        return None
    return {SESSION_COOKIE: value}


# -------- network --------

def get_session(force_refresh=False):
    import requests
    cookies = None if force_refresh else _load_cache()
    if not _validate_cookies(cookies):
        cookies = _resolve_cookies(interactive=False)
    if not cookies:
        # Before declaring "cookies bad" (which steers agents toward re-auth),
        # probe connectivity. If the outbound socket is blocked by the host
        # shell, auth is probably fine and re-auth would fail identically.
        cached = _load_cache()
        if cached and SESSION_COOKIE in cached:
            try:
                requests.get(f"{BASE}/", timeout=5, allow_redirects=False)
            except requests.exceptions.RequestException as probe_e:
                if _is_sandbox_block(probe_e):
                    raise RuntimeError(_SANDBOX_HINT) from probe_e
        raise RuntimeError(
            f"No valid Overleaf cookies (cache: {CACHE_FILE}).\n"
            f"\n"
            f"AGENT RECOVERY (do this — do NOT just tell the user 'log in'; on Windows + Chrome 130+ the browser has the cookie but app-bound encryption blocks our extractor):\n"
            f"  1. Run: overleaf-sync-now doctor\n"
            f"  2. Ask the user: 'Paste your overleaf_session2 cookie value. Open https://www.overleaf.com (logged in), F12 -> Application -> Cookies -> https://www.overleaf.com -> overleaf_session2 -> copy the Value field (long string starting with s%3A...).'\n"
            f"  3. Run: overleaf-sync-now save-cookie \"<pasted-value>\"\n"
            f"  4. Verify: overleaf-sync-now status\n"
            f"  5. Retry the original command."
        )
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".overleaf.com")
    return s


class AuthExpired(RuntimeError):
    """Cookies invalid or expired; user needs to re-auth."""


class RateLimited(RuntimeError):
    """Overleaf returned 429. Pass `retry_after` (seconds) when known."""
    def __init__(self, retry_after=60):
        self.retry_after = retry_after
        super().__init__(f"Overleaf rate-limited; wait ~{retry_after}s and retry.")


def _is_sandbox_block(exc):
    """True if `exc` looks like the host shell blocking an outbound socket
    (sandboxed shell, firewall policy) rather than a generic network failure.
    Distinguishing these matters: retrying doesn't help here, and the tool
    should stop reaching for auth-recovery commands (setup/login/doctor) that
    would hit the same block."""
    msg = str(exc).lower()
    markers = (
        "winerror 10013",                          # Windows WSAEACCES
        "forbidden by its access permissions",     # Windows human-readable form
        "eacces",                                  # POSIX symbolic
        "eperm",
        "[errno 13]",                              # POSIX numeric EACCES
        "permission denied",
    )
    return any(m in msg for m in markers)


_SANDBOX_HINT = (
    "Outbound HTTPS to Overleaf was blocked by the host environment "
    "(likely a sandboxed shell -- Codex CLI, some CI runners). Auth is "
    "probably fine; running setup/login/doctor will fail the same way. "
    "Approve the `overleaf-sync-now` command prefix in your sandbox "
    "policy, or re-run outside the sandbox."
)


def _wrap_network_error(e, context):
    """Convert a requests exception into a RuntimeError with a specific
    sandbox-block message when applicable, or a generic one otherwise."""
    if _is_sandbox_block(e):
        return RuntimeError(_SANDBOX_HINT)
    return RuntimeError(f"Network error {context}: {e}")


# -------- version-match refresh (/updates + /download/zip) --------


def fetch_updates(project_id):
    """GET /project/<id>/updates. Returns the parsed JSON history."""
    import requests
    s = get_session()
    try:
        r = s.get(f"{BASE}/project/{project_id}/updates", timeout=15)
    except requests.exceptions.RequestException as e:
        raise _wrap_network_error(e, "on /updates") from e
    if r.status_code in (401, 403):
        raise AuthExpired(
            f"Overleaf rejected /updates (HTTP {r.status_code}). "
            "Cookies likely expired; re-auth via `overleaf-sync-now login`."
        )
    if r.status_code == 429:
        try:
            retry_after = int(r.headers.get("Retry-After", "60") or "60")
        except (TypeError, ValueError):
            retry_after = 60
        raise RateLimited(retry_after)
    if r.status_code != 200:
        raise RuntimeError(f"/updates returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"/updates returned invalid JSON: {e}") from e


def download_zip(project_id):
    """GET /project/<id>/download/zip. Returns raw zip bytes."""
    import requests
    s = get_session()
    try:
        r = s.get(
            f"{BASE}/project/{project_id}/download/zip", timeout=120, stream=False
        )
    except requests.exceptions.RequestException as e:
        raise _wrap_network_error(e, "on /download/zip") from e
    if r.status_code in (401, 403):
        raise AuthExpired(f"Overleaf rejected /download/zip (HTTP {r.status_code}).")
    if r.status_code == 429:
        try:
            retry_after = int(r.headers.get("Retry-After", "60") or "60")
        except (TypeError, ValueError):
            retry_after = 60
        raise RateLimited(retry_after)
    if r.status_code != 200:
        raise RuntimeError(f"/download/zip returned HTTP {r.status_code}")
    return r.content


def _load_versions():
    if VERSIONS_FILE.exists():
        try:
            with open(VERSIONS_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(
                f"[overleaf-sync-now] WARNING: versions file corrupt ({VERSIONS_FILE}): {e}",
                file=sys.stderr,
            )
    return {}


def _save_versions(versions):
    _atomic_write_text(VERSIONS_FILE, json.dumps(versions, indent=2))


def refresh_project(project_id, folder, *, force=False):
    """Version-match refresh. Probes /updates, decides whether a zip download
    is actually needed, and if so extracts only the changed files into folder.

    Returns a short human-readable status string. Raises AuthExpired,
    RateLimited, or RuntimeError for the caller to handle.
    """
    data = fetch_updates(project_id)
    updates = data.get("updates", [])
    if not updates:
        return "empty_history"

    latest_toV = updates[0].get("toV")
    if latest_toV is None:
        raise RuntimeError("/updates response missing toV on latest entry")

    versions = _load_versions()
    cached_toV = versions.get(project_id)
    bootstrap = cached_toV is None

    # Defensive: if our cache claims we've synced to a future version, it's
    # either corrupted, seeded from another machine, or Overleaf's history
    # got rewound. Either way, treat it as a first-run bootstrap so we
    # re-anchor against what Overleaf actually has now.
    if not bootstrap and latest_toV < cached_toV:
        bootstrap = True
        cached_toV = None

    # Layer 6b: fingerprint sanity gate. If the recent dropbox-origin updates
    # touch pathnames that don't exist locally at all, we're almost certainly
    # syncing the wrong project (right account, valid project, just not the
    # one matching this folder — e.g. a typo'd manual link, a fork, or a
    # name-collision survivor that earlier defenses didn't catch). Warn but
    # proceed. Indeterminate outcomes (no dropbox-origin entries, no
    # pathnames) stay silent.
    _fingerprint_sanity_warn(folder, updates, project_id)

    if not force and not bootstrap and latest_toV == cached_toV:
        return "no_change"

    # Decide what to extract. On bootstrap or force, pull everything (hash
    # compare will skip any file that already matches). Otherwise walk the
    # update history back to cached_toV and collect pathnames from web-origin
    # edits only — dropbox-origin updates are local's own round-trip.
    if force or bootstrap:
        need_paths = None  # sentinel: extract all
    else:
        need_paths = set()
        all_covered = False
        for u in updates:
            toV = u.get("toV")
            if toV is not None and toV <= cached_toV:
                all_covered = True
                break
            origin_kind = (u.get("meta", {}).get("origin") or {}).get("kind")
            if origin_kind == "dropbox":
                continue
            for p in u.get("pathnames", []):
                need_paths.add(p)
        if not all_covered:
            # Cached version is older than the oldest update we received in
            # the first page. Rather than paginate and risk missing a
            # web-origin edit, just pull the full zip and hash-diff.
            need_paths = None

    if need_paths is not None and not need_paths:
        # All changes were dropbox-origin. Local already has this content
        # (or Dropbox will deliver it within seconds via multi-device sync).
        versions[project_id] = latest_toV
        _save_versions(versions)
        return f"dropbox_echo (toV {cached_toV}->{latest_toV})"

    zip_bytes = download_zip(project_id)
    # --force implies "user explicitly asked to overwrite" — disable the
    # recent-mtime guard. Otherwise protect files the user just saved from
    # being clobbered before their save has propagated Dropbox -> Overleaf.
    protect = 0 if force else 30
    n_written, n_unchanged, n_protected = _extract_files(
        zip_bytes, folder, need_paths, protect_recent_seconds=protect
    )
    versions[project_id] = latest_toV
    _save_versions(versions)
    label = "bootstrap" if bootstrap else ("forced" if force else "refreshed")
    parts = [f"toV -> {latest_toV}", f"wrote {n_written}", f"unchanged {n_unchanged}"]
    if n_protected:
        parts.append(f"protected {n_protected} (recent local edits; pass --force to override)")
    return f"{label} (" + ", ".join(parts) + ")"


def _extract_files(zip_bytes, folder, paths, protect_recent_seconds=30):
    """Extract files from zip into folder. `paths` is either a set of relative
    pathnames (no leading slash) or None to extract every entry.

    Writes only when file contents differ from what's on disk — keeps Dropbox
    upload pressure (and Overleaf's resulting merge-queue load) at zero when
    the zip content matches local.

    Also skips files whose local mtime is within `protect_recent_seconds`
    (0 to disable). This is the guard against clobbering a user's in-progress
    local save that hasn't yet propagated Dropbox -> Overleaf, so the zip
    still has old content while local has fresh content. Pass 0 when the
    caller explicitly wants to overwrite (e.g. --force).

    Returns (n_written, n_unchanged, n_protected).
    """
    folder = pathlib.Path(folder).resolve()
    n_written = 0
    n_unchanged = 0
    n_protected = 0
    now = time.time()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = info.filename.lstrip("/")
            if paths is not None and rel not in paths:
                continue
            # Never let a round-tripped .overleaf-project marker (uploaded
            # to Overleaf via Dropbox sync, then re-downloaded in the zip)
            # overwrite the local marker. The local marker is the source of
            # truth for this machine's link, and Overleaf's copy may be from
            # a different machine that picked a different project.
            if pathlib.PurePosixPath(rel).name == PROJECT_MARKER:
                continue
            target = (folder / rel).resolve()
            try:
                target.relative_to(folder)
            except ValueError:
                print(
                    f"[overleaf-sync-now] WARNING: skipping zip entry outside folder: {info.filename}",
                    file=sys.stderr,
                )
                continue

            new_content = z.read(info)

            if target.exists():
                try:
                    if target.read_bytes() == new_content:
                        n_unchanged += 1
                        continue
                except OSError:
                    pass
                # Content differs. If the local file was modified very
                # recently, the user is probably mid-save and our write
                # would clobber a local edit Dropbox hasn't yet pushed
                # up to Overleaf. Leave it alone and warn.
                if protect_recent_seconds > 0:
                    try:
                        age = now - target.stat().st_mtime
                    except OSError:
                        age = None
                    if age is not None and age < protect_recent_seconds:
                        n_protected += 1
                        print(
                            f"[overleaf-sync-now] SKIP {rel}: local modified {age:.0f}s ago "
                            f"(< {protect_recent_seconds}s protection window). "
                            f"Pass --force to overwrite.",
                            file=sys.stderr,
                        )
                        continue

            target.parent.mkdir(parents=True, exist_ok=True)
            # Same concurrency contract as _atomic_write_text: per-process,
            # per-thread unique tmp name plus retry on Windows transient
            # ACCESS_DENIED. Two agents extracting the same file at once
            # (rare, but possible when neither has reached `mark_synced`
            # yet) must not stomp each other or fail spuriously.
            tmp = _unique_tmp_path(target)
            try:
                with open(tmp, "wb") as f:
                    f.write(new_content)
                _replace_with_retry(tmp, target)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            n_written += 1

    return n_written, n_unchanged, n_protected


# -------- project lookup --------
#
# Project resolution flow:
#   1. Walk parents looking for a `.overleaf-project` marker file. If found,
#      return its project_id (and validate against the cached index — see
#      Layer 4 in _validate_marker_id_against_index).
#   2. If the path is under .../Apps/Overleaf/<name>/, resolve <name> against
#      the cached projects index using the policy resolver (filter trashed/
#      archived; refuse to guess on ambiguity). On a unique match, write a
#      marker so subsequent calls take the fast path above.
#
# The previous implementation used a `{name: id}` dict that silently overwrote
# duplicates and didn't filter trashed projects. That caused trashed projects
# to shadow the active one when both shared a name (the cross-folder dedupe
# bug). The new path keeps the full project record list and decides via
# explicit policy.


def find_linked_folder(start, *, verbose=True, allow_disambig_network=True, read_only=False):
    """Resolve `start` (a file or folder path) to (linked_folder, project_id).

    Returns (None, None) when nothing resolves. `verbose` controls whether
    diagnostic messages are printed to stderr — the hook passes False so
    repeated keystrokes don't spam.

    `allow_disambig_network` controls whether ambiguity may be resolved by
    fingerprinting candidates' /updates against local files (Layer 6a, costs
    one HTTP per candidate). Hook callers should leave this True since
    auto-link runs at most once per folder before a marker is written.

    `read_only` suppresses the side-effect marker write on a successful
    auto-link. Used by `status` so a diagnostic command doesn't surprise
    the user by creating files.
    """
    p = pathlib.Path(start).resolve()
    if p.is_file():
        p = p.parent
    cur = p
    while True:
        marker = cur / PROJECT_MARKER
        if marker.exists():
            try:
                with open(marker) as f:
                    pid = json.load(f).get("project_id")
            except Exception:
                pid = None
            if pid:
                # Reject markers placed at the shared `Apps/Overleaf/` level.
                # A marker there would silently shadow every project under it
                # (every project would resolve to the same ID). Easy footgun
                # if a user ran `link <id>` from the wrong cwd. Warn + skip
                # so the parent walk falls through to auto-link, which will
                # find the correct per-project mapping.
                if _is_marker_at_shared_level(cur):
                    if verbose:
                        print(
                            f"[overleaf-sync-now] WARN: ignoring {marker} — a marker "
                            f"in the shared Apps/Overleaf/ folder would shadow every "
                            f"project under it. Move it into a specific project "
                            f"subfolder (e.g. {cur / 'YourProject' / PROJECT_MARKER}), "
                            f"or delete it and let auto-link handle each project.",
                            file=sys.stderr,
                        )
                else:
                    _validate_marker_id_against_index(pid, cur, verbose=verbose)
                    return cur, pid
        if cur.parent == cur:
            break
        cur = cur.parent
    parts = p.parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "overleaf" and i > 0 and parts[i - 1].lower() == "apps":
            if i + 1 < len(parts):
                name = parts[i + 1]
                folder = pathlib.Path(*parts[: i + 2])
                pid = _autolink_resolve(
                    name, folder, verbose=verbose,
                    allow_disambig_network=allow_disambig_network,
                    write_marker=not read_only,
                )
                if pid:
                    return folder, pid
                return None, None
    return None, None


def _autolink_resolve(name, folder, *, verbose=True, allow_disambig_network=True, write_marker=True):
    """Auto-link `folder` (an .../Apps/Overleaf/<name>/ directory) to a
    project ID. Writes a `.overleaf-project` marker on success so future
    resolution skips the index lookup entirely (unless write_marker=False).

    Returns the project_id on success, None on no-match or unresolved
    ambiguity. When verbose, prints a clear error to stderr listing the
    candidates and the exact `link` command to run.
    """
    # Broad except: get_session can raise RuntimeError for auth/network/sandbox
    # issues, and requests can raise its own exceptions on top. We never want
    # an auto-link probe to crash the calling command (esp. the hook) — at
    # worst it should fail closed and let the user run `setup`/`login`.
    try:
        records = _load_projects_records()
    except Exception as e:
        if verbose:
            print(f"[overleaf-sync-now] auto-link: could not fetch project list: {e}", file=sys.stderr)
        return None
    outcome = _resolve_by_name(name, records)
    status = outcome[0]
    if status == "ok":
        pid = outcome[1]
        if write_marker:
            try:
                _write_marker(folder, pid, project_name=name, source="auto-link")
                if verbose:
                    print(
                        f"[overleaf-sync-now] auto-linked {folder} -> {pid} ({PROJECT_MARKER} written)",
                        file=sys.stderr,
                    )
            except OSError as e:
                # Don't fail resolution just because we can't drop the marker
                # (read-only filesystem, permission issue, etc.). Auto-link will
                # just have to re-run on the next call.
                if verbose:
                    print(f"[overleaf-sync-now] auto-link: marker write failed ({e}); proceeding without marker", file=sys.stderr)
        return pid
    if status == "none":
        if verbose:
            print(
                f"[overleaf-sync-now] auto-link: no Overleaf project named {name!r}.\n"
                f"  Run `overleaf-sync-now projects --refresh` to re-fetch the list, "
                f"or `overleaf-sync-now link <project_id> {folder}` to link explicitly.",
                file=sys.stderr,
            )
        return None
    if status == "ambiguous":
        candidates = outcome[1]
        # Layer 6a: probe /updates for each candidate and pick the unique
        # one whose recent dropbox-origin pathnames map to local files.
        if allow_disambig_network and len(candidates) <= 5:
            picked = _disambiguate_by_fingerprint(candidates, folder, verbose=verbose)
            if picked:
                pid = picked["id"]
                if write_marker:
                    try:
                        _write_marker(folder, pid, project_name=name, source="auto-link-fingerprint")
                        if verbose:
                            print(
                                f"[overleaf-sync-now] auto-linked {folder} -> {pid} via fingerprint match "
                                f"({PROJECT_MARKER} written)",
                                file=sys.stderr,
                            )
                    except OSError as e:
                        if verbose:
                            print(f"[overleaf-sync-now] auto-link: marker write failed ({e}); proceeding without marker", file=sys.stderr)
                return pid
        if verbose:
            print(
                f"[overleaf-sync-now] auto-link: {len(candidates)} non-trashed Overleaf projects "
                f"named {name!r}. Refusing to guess. Candidates:",
                file=sys.stderr,
            )
            for c in candidates:
                last = c.get("lastUpdated") or "?"
                tags = []
                if c.get("archived"):
                    tags.append("archived")
                tag_s = f"  [{', '.join(tags)}]" if tags else ""
                print(f"    {c['id']}  lastUpdated={last}{tag_s}", file=sys.stderr)
            print(
                f"  Pick one and run:\n"
                f"    overleaf-sync-now link <project_id> {folder}",
                file=sys.stderr,
            )
        return None
    return None  # unknown status


def _is_marker_at_shared_level(folder):
    """True if `folder` is the shared Apps/Overleaf/ directory itself (the
    parent of all per-project folders). A marker placed there would shadow
    every project; we refuse such markers so the parent walk falls through
    to per-project auto-link.

    Case-insensitive on both components to match macOS/Windows folder casing
    quirks. Anything else (including a project subfolder named `overleaf`
    that just happens to live under an `apps` parent) is fine."""
    folder = pathlib.Path(folder)
    parts = folder.parts
    if len(parts) < 2:
        return False
    return parts[-1].lower() == "overleaf" and parts[-2].lower() == "apps"


def _validate_marker_id_against_index(project_id, folder, *, verbose=True):
    """Layer 4: cheap sanity check that a marker's project_id is still a
    sensible target. Looks up the ID in the cached projects index (no extra
    network call when the cache is warm).

    Warns but never refuses — the user explicitly wrote the marker, so we
    proceed with sync regardless. The point is to stop being silent when
    the marker points at a stale, trashed, or wrong-account project.
    """
    if not verbose:
        return
    records = _load_cached_projects_records()
    if not records:
        return
    rec = _index_record_by_id(records, project_id)
    if rec is None:
        print(
            f"[overleaf-sync-now] WARN: marker at {folder} -> {project_id} is not in your "
            f"Overleaf account (trashed long ago, deleted, or cookies are from a different "
            f"account). Sync will likely fail. Run `overleaf-sync-now projects --refresh` "
            f"and verify with `overleaf-sync-now status`.",
            file=sys.stderr,
        )
        return
    flags = []
    if rec.get("trashed"):
        flags.append("trashed")
    if rec.get("archived"):
        flags.append("archived")
    if flags:
        print(
            f"[overleaf-sync-now] WARN: marker at {folder} -> {project_id} ({rec.get('name','?')!r}) "
            f"is {'/'.join(flags)} on Overleaf. Syncing anyway since you explicitly linked it. "
            f"To switch, edit {folder/PROJECT_MARKER} or run "
            f"`overleaf-sync-now link <other_project_id> {folder}`.",
            file=sys.stderr,
        )


def _resolve_by_name(name, records):
    """Policy-driven name resolver. Returns one of:
      ("ok", project_id)
      ("none",)
      ("ambiguous", [candidate_records])

    Filters trashed and archived projects out by default. Tries case-sensitive
    exact match first; only if that yields zero results does it fall back to
    case-insensitive (covers macOS/Windows case-insensitive filesystems where
    the local folder name may differ in case from Overleaf's project name).
    """
    candidates = [r for r in records
                  if not r.get("trashed") and not r.get("archived")
                  and r.get("name") and r.get("id")]
    exact = [r for r in candidates if r["name"] == name]
    if not exact:
        exact = [r for r in candidates if r["name"].lower() == name.lower()]
    if not exact:
        return ("none",)
    if len(exact) == 1:
        return ("ok", exact[0]["id"])
    # Sort ambiguous candidates by lastUpdated desc so the user-facing
    # error lists the most-recently-touched first.
    exact.sort(key=lambda r: r.get("lastUpdated") or "", reverse=True)
    return ("ambiguous", exact)


def _disambiguate_by_fingerprint(candidates, folder, *, verbose=True):
    """Layer 6a: fetch each candidate's /updates top page and count how many
    of its recent dropbox-origin pathnames exist as local files in `folder`.
    If exactly one candidate has any matches and the others have zero,
    return that candidate; otherwise return None (caller falls back to
    refusal).

    Cost: one HTTPS request per candidate (typical ambiguity = 2-3). Only
    runs when the resolver is ambiguous, which is rare.
    """
    folder = pathlib.Path(folder)
    matchers = []  # list of (candidate, hits)
    for c in candidates:
        try:
            data = fetch_updates(c["id"])
        except Exception as e:
            if verbose:
                print(
                    f"[overleaf-sync-now] auto-link: fingerprint probe failed for {c['id']} "
                    f"({type(e).__name__}); skipping that candidate",
                    file=sys.stderr,
                )
            matchers.append((c, 0))
            continue
        hits = _fingerprint_hits(folder, data.get("updates", []))
        matchers.append((c, hits))
    matched = [(c, h) for c, h in matchers if h > 0]
    if len(matched) == 1:
        if verbose:
            picked, hits = matched[0]
            print(
                f"[overleaf-sync-now] auto-link: fingerprint resolved ambiguity "
                f"({hits} recent Dropbox-origin file(s) of {picked['id']} exist locally; "
                f"the other {len(candidates)-1} candidate(s) have zero local matches)",
                file=sys.stderr,
            )
        return matched[0][0]
    return None


def _collect_recent_dbx_pathnames(updates):
    """Return the deduplicated list of pathnames from the most recent
    dropbox-origin entries in `updates`, bounded to FINGERPRINT_MAX_PATHS
    to keep downstream stat() calls cheap on slow Dropbox folders.

    An empty list means "no signal" — either no dropbox-origin entries in
    the window, or those entries had no pathnames. Callers must treat
    that as indeterminate, never as "wrong project."
    """
    seen = []
    dbx_count = 0
    for u in updates:
        if dbx_count >= FINGERPRINT_RECENT_DBX_ENTRIES:
            break
        origin_kind = (u.get("meta", {}).get("origin") or {}).get("kind")
        if origin_kind != "dropbox":
            continue
        dbx_count += 1
        for p in u.get("pathnames", []):
            if p and p not in seen:
                seen.append(p)
            if len(seen) >= FINGERPRINT_MAX_PATHS:
                break
    return seen


def _path_exists_under(folder, rel):
    """Best-effort check that the Overleaf-style relative path `rel` exists
    as a file under `folder`. Forward-slashes normalized to native separators.
    Refuses anything that would resolve outside `folder` (absolute paths,
    `..` components, current/parent-dir aliases) — these never come from
    real Overleaf payloads but the cost of guarding is trivial."""
    rel = (rel or "").lstrip("/")
    if not rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    try:
        return folder.joinpath(*parts).exists()
    except (OSError, ValueError):
        return False


def _collect_local_basenames(folder, cap=5000):
    """Walk `folder` collecting file basenames into a set, bounded by `cap`
    to keep cost trivial on huge projects (1000-file paper folder is ~50 ms;
    we stop early once we have plenty of signal). Skips dotdirs to avoid
    wandering into `.git`, IDE caches, etc.

    Used by the fingerprint check to handle the file-rename/move case: a
    historic dropbox-origin /updates entry references `Paper-New.tex`, but
    the file now lives at `Archive/Paper-New.tex`. Basename match still
    confirms the project is the right one even though the exact path drifted.
    """
    folder = pathlib.Path(folder)
    seen = set()
    try:
        for root, dirs, files in os.walk(folder):
            for f in files:
                seen.add(f)
                if len(seen) >= cap:
                    return seen
            dirs[:] = [d for d in dirs if not d.startswith(".")]
    except OSError:
        pass
    return seen


def _count_pathname_hits(folder, pathnames):
    """How many of `pathnames` (Overleaf-style forward-slash relative paths)
    map to local files under `folder`. A path matches if either:
      (a) the exact path exists under `folder` (strong signal: same path), or
      (b) its basename appears anywhere under `folder` (rename/move tolerance).

    The basename index is built lazily — only paid when at least one direct
    match failed, so the common all-paths-match case stays free.
    """
    folder = pathlib.Path(folder)
    hits = 0
    basenames = None  # built lazily on first miss
    for rel in pathnames:
        if _path_exists_under(folder, rel):
            hits += 1
            continue
        if basenames is None:
            basenames = _collect_local_basenames(folder)
        base = (rel or "").rstrip("/").rsplit("/", 1)[-1]
        if base and base in basenames:
            hits += 1
    return hits


def _fingerprint_hits(folder, updates):
    """How many of the recent dropbox-origin pathnames map to local files.

    Returns 0 when there are no dropbox-origin entries or no pathnames —
    a "no signal" outcome, distinct from "negative signal." Callers should
    treat 0 as indeterminate, not as "wrong project."
    """
    seen = _collect_recent_dbx_pathnames(updates)
    if not seen:
        return 0
    return _count_pathname_hits(folder, seen)


def _fingerprint_sanity_warn(folder, updates, project_id):
    """Layer 6b: print a one-line WARN to stderr when recent dropbox-origin
    updates name pathnames whose exact paths AND basenames are absent from
    the local folder. This catches the only failure class the earlier
    layers can't: right account, valid non-trashed project, but the wrong
    project (typo'd manual link, fork, etc.).

    Silent on indeterminate signals (no dropbox-origin entries, no pathnames
    on the entries, file lookups fail). Silent on file rename/move (basename
    fallback in _count_pathname_hits). Never raises.
    """
    try:
        seen = _collect_recent_dbx_pathnames(updates)
        if not seen:
            return  # no signal — never warn
        if _count_pathname_hits(folder, seen) > 0:
            return  # at least one path or basename matched — looks like the right project
        sample = ", ".join(seen[:3])
        print(
            f"[overleaf-sync-now] WARN: project {project_id} has recent Dropbox-origin "
            f"updates touching files not present in {folder} (e.g. {sample}). The link "
            f"may point to the wrong project (a duplicate, a fork, or a typo). Run "
            f"`overleaf-sync-now status` and `overleaf-sync-now projects` to verify, then "
            f"`overleaf-sync-now link <correct_id> {folder}` to fix.",
            file=sys.stderr,
        )
    except Exception:
        # The sanity gate must never crash a sync. Swallow anything unexpected.
        return


def _write_marker(folder, project_id, *, project_name=None, source="auto-link"):
    """Layer 3: write the .overleaf-project marker with provenance metadata.
    Only `project_id` is load-bearing for resolution; the rest is for
    debuggability when the user (or future-us) inspects a marker file.
    """
    folder = pathlib.Path(folder)
    payload = {"project_id": project_id}
    if project_name:
        payload["name_at_link_time"] = project_name
    payload["source"] = source
    payload["linked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_text(folder / PROJECT_MARKER, json.dumps(payload, indent=2))


def _index_record_by_id(records, project_id):
    for r in records:
        if r.get("id") == project_id:
            return r
    return None


def _load_projects_records(*, force_refresh=False):
    """Return the list of project records, refreshing from Overleaf when
    the cache is missing/expired/wrong-version. May make a network call;
    use `_load_cached_projects_records` for the no-network variant."""
    if not force_refresh and INDEX_FILE.exists():
        try:
            with open(INDEX_FILE) as f:
                cached = json.load(f)
            if (
                cached.get("version") == INDEX_FORMAT_VERSION
                and time.time() - cached.get("ts", 0) < INDEX_TTL
            ):
                return cached.get("projects", []) or []
        except Exception:
            pass
    return _refresh_projects_records()


def _load_cached_projects_records():
    """Return the cached projects records without network. Returns [] on
    missing/corrupt/v1 file. Used by validation paths that must stay cheap
    (every find_linked_folder call hits this)."""
    if not INDEX_FILE.exists():
        return []
    try:
        with open(INDEX_FILE) as f:
            cached = json.load(f)
    except Exception:
        return []
    if cached.get("version") != INDEX_FORMAT_VERSION:
        return []
    return cached.get("projects", []) or []


def _refresh_projects_records():
    """Fetch /project, parse ol-prefetchedProjectsBlob, and persist a v2
    record list with the disambiguating fields (id, name, trashed, archived,
    lastUpdated, ownerId). Falls back to whatever's on disk when Overleaf
    is unhappy so we don't lose a previously-good index.
    """
    import requests
    s = get_session()
    try:
        r = s.get(f"{BASE}/project", timeout=15)
        if r.status_code != 200:
            s = get_session(force_refresh=True)
            r = s.get(f"{BASE}/project", timeout=15)
    except requests.exceptions.RequestException as e:
        # Network failure / sandbox block during the project-list fetch.
        # Don't poison the cache; fall back to whatever we have.
        if _is_sandbox_block(e):
            raise RuntimeError(_SANDBOX_HINT) from e
        return _load_cached_projects_records()
    if r.status_code != 200:
        return _load_cached_projects_records()
    m = re.search(r'name="ol-prefetchedProjectsBlob"[^>]*\scontent="([^"]+)"', r.text)
    records = []
    if m:
        import html
        try:
            blob = json.loads(html.unescape(m.group(1)))
            for proj in blob.get("projects", []):
                pid = proj.get("id")
                name = proj.get("name")
                if not pid or not name:
                    continue
                owner = proj.get("owner") or {}
                records.append({
                    "id": pid,
                    "name": name,
                    "trashed": bool(proj.get("trashed")),
                    "archived": bool(proj.get("archived")),
                    "lastUpdated": proj.get("lastUpdated"),
                    "ownerId": owner.get("id") if isinstance(owner, dict) else None,
                })
        except (ValueError, KeyError, TypeError):
            pass
    if not records:
        # HTML fallback: only match anchors whose href is the project root,
        # not /project/<id>/clone, /project/<id>/download, etc., which would
        # map the wrong link text (e.g. "Download") to the project ID.
        # This path can't recover trashed/archived/lastUpdated; the resolver
        # will treat them as non-trashed/non-archived, which is the safest
        # default when we lack signal.
        seen = set()
        for pid, name in re.findall(
            r'/project/([0-9a-f]{24})"[^>]*>\s*([^<\n][^<]*?)\s*<', r.text
        ):
            n = name.strip()
            if pid in seen:
                continue
            seen.add(pid)
            records.append({"id": pid, "name": n, "trashed": False,
                            "archived": False, "lastUpdated": None, "ownerId": None})
    if not records:
        # Don't overwrite a previously-good cached index with an empty one.
        return _load_cached_projects_records()
    _atomic_write_text(INDEX_FILE, json.dumps(
        {"version": INDEX_FORMAT_VERSION, "ts": time.time(), "projects": records},
        indent=2,
    ))
    return records


# Back-compat helper kept for any external callers / tests that imported the
# old name. New code should call _resolve_by_name on _load_projects_records().
def lookup_project_id(name):
    records = _load_projects_records()
    outcome = _resolve_by_name(name, records)
    return outcome[1] if outcome[0] == "ok" else None


# -------- state --------

def _state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            # Don't silently treat a corrupt state file as 'never synced'
            # forever — that would defeat the debounce. Move it aside so
            # subsequent saves don't compound the corruption.
            print(
                f"[overleaf-sync-now] WARNING: state file corrupt ({STATE_FILE}): {e}; renaming to .bad",
                file=sys.stderr,
            )
            try:
                STATE_FILE.rename(STATE_FILE.with_suffix(".json.bad"))
            except OSError:
                pass
            return {}
    return {}


def _save_state(state):
    _atomic_write_text(STATE_FILE, json.dumps(state))


def mark_synced(project_id):
    state = _state()
    state[project_id] = time.time()
    _save_state(state)


def is_debounced(project_id):
    return (time.time() - _state().get(project_id, 0)) < DEBOUNCE_SECONDS


# -------- subcommands --------

def cmd_setup(args, *, force_noninteractive=False):
    interactive = (
        not force_noninteractive
        and sys.stdin.isatty()
        and not os.environ.get("OVERLEAF_SYNC_NONINTERACTIVE")
    )
    print("Setting up Overleaf sync...")
    cached = _load_cache()
    if _validate_cookies(cached):
        print(f"  - Cached cookies still valid ({SESSION_COOKIE}={cached[SESSION_COOKIE][:12]}...).")
        return
    via_browser = _try_browser_cookie3()
    if _validate_cookies(via_browser):
        _save_cache(via_browser)
        print(f"  - Found valid cookies in your regular browser.")
        print(f"  - Saved to {CACHE_FILE}")
        return
    via_pw = _try_playwright_profile()
    if _validate_cookies(via_pw):
        _save_cache(via_pw)
        print("  - Found valid cookies in Claude Code Playwright profile.")
        print(f"  - Saved to {CACHE_FILE}")
        return
    if not interactive:
        print()
        print("AUTO-DETECT FAILED. Next steps:")
        print("  1. Open https://www.overleaf.com in Chrome/Edge/Firefox/Brave/etc. and log in.")
        print("  2. Re-run `overleaf-sync-now setup`.")
        print("OR (for the manual paste fallback) run `overleaf-sync-now setup` from an interactive terminal.")
        print()
        print("Sync will not work until cookies are obtained.")
        return
    manual = _prompt_manual_cookie()
    if _validate_cookies(manual):
        _save_cache(manual)
        print(f"  - Saved to {CACHE_FILE}")
        return
    print("ERROR: Could not authenticate.", file=sys.stderr)
    sys.exit(1)


def cmd_link(args):
    if not args:
        print("Usage: link <project_id> [folder]", file=sys.stderr)
        print(
            f"  project_id is the 24-char hex from your Overleaf URL:\n"
            f"  https://www.overleaf.com/project/<project_id>",
            file=sys.stderr,
        )
        sys.exit(2)
    project_id = args[0].strip()
    # Strip surrounding quotes / common URL paste forms.
    if project_id.startswith("http"):
        m = re.search(r"/project/([0-9a-f]{24})", project_id)
        if m:
            project_id = m.group(1)
    if not re.fullmatch(r"[0-9a-f]{24}", project_id):
        print(
            f"ERROR: '{project_id}' is not a valid Overleaf project ID.\n"
            f"  Expected: 24 lowercase hex characters (e.g. 69cd66411a29169cb64109e0)\n"
            f"  Found in the URL: https://www.overleaf.com/project/<project_id>",
            file=sys.stderr,
        )
        sys.exit(1)
    folder = pathlib.Path(args[1] if len(args) > 1 else ".").resolve()
    if not folder.is_dir():
        print(f"ERROR: {folder} is not a directory", file=sys.stderr)
        sys.exit(1)
    # Look up the project name from the cached index for the marker's
    # name_at_link_time field (debug aid; not load-bearing). Also surface
    # trashed/archived/missing-from-account state up front so the user can
    # cancel before the wrong link gets written.
    rec = _index_record_by_id(_load_cached_projects_records(), project_id)
    if rec is None:
        print(
            f"WARN: project {project_id} not found in cached projects index. "
            f"Linking anyway, but verify with `overleaf-sync-now projects --refresh`.",
            file=sys.stderr,
        )
    else:
        flags = []
        if rec.get("trashed"):
            flags.append("trashed")
        if rec.get("archived"):
            flags.append("archived")
        if flags:
            print(
                f"WARN: project {project_id} ({rec.get('name','?')!r}) is "
                f"{'/'.join(flags)} on Overleaf. Linking anyway since you asked.",
                file=sys.stderr,
            )
    _write_marker(
        folder, project_id,
        project_name=(rec or {}).get("name"),
        source="link",
    )
    print(f"Linked {folder} -> Overleaf project {project_id}")


def cmd_sync(args):
    force = "--force" in args
    args = [a for a in args if not a.startswith("--")]
    folder = pathlib.Path(args[0] if args else ".")
    linked, project_id = find_linked_folder(folder)
    if not project_id:
        print(
            f"ERROR: {folder.resolve()} is not under any Overleaf project.\n"
            f"  Auto-link only works under '<anywhere>/Apps/Overleaf/<project-name>/'.\n"
            f"  Override: `overleaf-sync-now link <id> {folder}` or drop a {PROJECT_MARKER} file.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Refreshing project {project_id} (folder: {linked})")
    t0 = time.time()
    try:
        status = refresh_project(project_id, linked, force=force)
    except RateLimited as e:
        print(f"ERROR: rate-limited ({e}). Try again in ~{e.retry_after}s.", file=sys.stderr)
        sys.exit(1)
    except AuthExpired as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        # Network failure, sandbox block, malformed response, etc. Print
        # clean message (no traceback) so the actionable hint is visible.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    mark_synced(project_id)
    print(f"{status} ({time.time() - t0:.2f}s)")


def cmd_projects(args):
    """List the user's Overleaf projects with disambiguating metadata.

    Useful when:
      - You want to know your project IDs without opening the web UI.
      - Auto-link is failing and you need to confirm which Overleaf project
        name corresponds to a local folder.
      - Two projects share a name and you need to see trashed/archived flags
        and last-updated timestamps to pick the right one.
    """
    refresh = "--refresh" in args
    try:
        records = _load_projects_records(force_refresh=refresh)
    except RuntimeError as e:
        print(f"ERROR: could not fetch project list: {e}", file=sys.stderr)
        sys.exit(1)
    if not records:
        print("(No projects found. Run with --refresh to force a re-fetch from Overleaf.)")
        return
    # Sort by lastUpdated desc so the most-recently-touched projects appear
    # first — matches the Overleaf dashboard's default ordering and helps
    # users debugging same-name ambiguity see the active duplicate at the top.
    records = sorted(records, key=lambda r: r.get("lastUpdated") or "", reverse=True)
    name_w = max(len(r.get("name", "")) for r in records)
    name_w = max(name_w, len("NAME"))
    print(f"{'NAME'.ljust(name_w)}  PROJECT_ID                FLAGS  LAST_UPDATED")
    print(f"{'-' * name_w}  {'-' * 24}  -----  ------------")
    # Surface name collisions inline so users debugging the cross-folder dedupe
    # bug can see at a glance which entries they need to disambiguate with link.
    name_counts = {}
    for r in records:
        name_counts[r.get("name", "")] = name_counts.get(r.get("name", ""), 0) + 1
    for r in records:
        flags = []
        if r.get("trashed"):
            flags.append("T")
        if r.get("archived"):
            flags.append("A")
        if name_counts.get(r.get("name", ""), 0) > 1:
            flags.append("DUP")
        flag_s = ",".join(flags) if flags else "-"
        last = (r.get("lastUpdated") or "?")[:19]  # trim ms / timezone
        print(f"{r.get('name','').ljust(name_w)}  {r.get('id','?'):24s}  {flag_s:5s}  {last}")
    n_trashed = sum(1 for r in records if r.get("trashed"))
    n_archived = sum(1 for r in records if r.get("archived"))
    print(
        f"\n{len(records)} project(s) "
        f"({n_trashed} trashed, {n_archived} archived). Cached at {INDEX_FILE}."
    )
    print("Flags: T=trashed, A=archived, DUP=name shared with another project.")


def cmd_status(args):
    """Status reports whether sync would actually succeed right now, by walking
    the same auth chain sync itself uses. Use --quick to skip the slow chain
    fallback and only check the cached cookie."""
    quick = "--quick" in args
    args = [a for a in args if not a.startswith("--")]
    folder = pathlib.Path(args[0] if args else ".")
    # read_only=True so a diagnostic command never writes a marker as a
    # side effect. The next sync (or hook fire) will write the marker.
    linked, project_id = find_linked_folder(folder, read_only=True)
    print(f"Data dir:    {CACHE_DIR}")
    print(f"Cookie file: {CACHE_FILE} ({'present' if CACHE_FILE.exists() else 'MISSING'})")
    cached = _load_cache()
    # Force a real network probe — status is a diagnostic, not a hot path.
    cache_ok = _validate_cookies(cached, use_cache=False)
    if cache_ok:
        print("Cookie auth: OK (cache valid, sync would succeed)")
    else:
        # Differentiate "cookies bad" from "network blocked" so a sandboxed
        # shell doesn't make status print a misleading INVALID verdict.
        sandboxed = False
        if cached and SESSION_COOKIE in cached:
            import requests
            try:
                requests.get(f"{BASE}/", timeout=5, allow_redirects=False)
            except requests.exceptions.RequestException as e:
                if _is_sandbox_block(e):
                    sandboxed = True
        if sandboxed:
            print("Cookie auth: UNKNOWN — outbound HTTPS is blocked by the host "
                  "shell (sandbox / firewall). Auth is probably fine. Approve "
                  "the `overleaf-sync-now` command prefix or run outside the sandbox.")
        elif quick:
            print("Cookie auth: cache INVALID (chain not checked; pass without --quick for full check, or run `doctor`).")
        else:
            # Cache failed; walk the full chain to predict sync behavior.
            resolved = _resolve_cookies(interactive=False)
            if resolved:
                print("Cookie auth: OK (cache stale, but chain resolved; sync would succeed and refresh cache)")
            else:
                print("Cookie auth: INVALID — sync would FAIL. Run `doctor` for details, "
                      "or run `overleaf-sync-now login` (or `save-cookie <value>`).")
    print()
    if not project_id:
        print(f"Folder: {folder.resolve()}")
        print("Project: not under any Overleaf project (auto-link only works under .../Apps/Overleaf/<name>/)")
        return
    print(f"Folder:    {linked}")
    # Resolution provenance: was the project ID supplied by an explicit
    # marker, or guessed by auto-link from the folder name? Helps debug the
    # "I thought I linked X but sync hits Y" class of confusion.
    marker = linked / PROJECT_MARKER
    if marker.exists():
        try:
            with open(marker) as f:
                meta = json.load(f)
            src = meta.get("source", "marker")
            linked_at = meta.get("linked_at")
            extra = f", linked_at={linked_at}" if linked_at else ""
            print(f"Source:    {marker} ({src}{extra})")
        except Exception:
            print(f"Source:    {marker}")
    else:
        print("Source:    auto-link (no marker file; resolved via folder name)")
    print(f"Project:   {project_id}")
    # Cross-reference against the cached projects index — surfaces the
    # bug-class this fix is for: marker pointing at trashed/archived/missing
    # project, or a name collision with a still-living duplicate.
    rec = _index_record_by_id(_load_cached_projects_records(), project_id)
    if rec is not None:
        flags = []
        if rec.get("trashed"):
            flags.append("trashed")
        if rec.get("archived"):
            flags.append("archived")
        flag_s = f"  [{'/'.join(flags)}]" if flags else ""
        last_up = rec.get("lastUpdated") or "?"
        print(f"Name:      {rec.get('name','?')}{flag_s}")
        print(f"Updated:   {last_up} (Overleaf-side)")
    else:
        print(
            "Name:      (project ID not in cached index — run `projects --refresh` "
            "to verify it still exists in your account)"
        )
    last = _state().get(project_id)
    if last:
        ago = time.time() - last
        print(f"Last sync: {ago:.0f}s ago (debounce: {DEBOUNCE_SECONDS}s)")
    else:
        print("Last sync: never")
    cached_toV = _load_versions().get(project_id)
    if cached_toV is not None:
        print(f"Cached toV: {cached_toV} (bootstrap will re-run if Overleaf reports an older version)")
    else:
        print("Cached toV: none (next sync will bootstrap)")


def cmd_hook(args):
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        # Don't block the edit, but surface the schema mismatch so future
        # Claude Code hook-payload changes don't make us silently no-op.
        print(f"[overleaf-sync-now] hook stdin not valid JSON ({e}); skipping.", file=sys.stderr)
        sys.exit(0)
    # Read is included: the /updates probe is cheap (~0.3s) and debounced
    # (30s per project), so refreshing on Read keeps Claude's downstream
    # reasoning grounded in current content for negligible cost.
    if data.get("tool_name", "") not in ("Read", "Edit", "Write", "MultiEdit"):
        sys.exit(0)
    fp = data.get("tool_input", {}).get("file_path", "")
    if not fp or not re.search(r"\.(tex|bib|cls|sty|bst)$", fp, re.IGNORECASE):
        sys.exit(0)
    # find_linked_folder can in principle raise (PermissionError on a network
    # share, OSError reading a marker, etc.). Don't let those crash the hook
    # and block the user's edit.
    #
    # verbose=False so repeated keystrokes don't spam stderr with the same
    # auto-link warning. The first auto-link writes a marker; subsequent
    # calls hit the marker fast path. Validation warnings (Layer 4) are also
    # suppressed in the hook — the user sees them via interactive `status`
    # / `sync`. Marker-write notice happens once per folder; that's the
    # only thing the hook would have surfaced anyway.
    try:
        linked, project_id = find_linked_folder(fp, verbose=False)
    except Exception as e:
        print(f"[overleaf-sync-now] hook: could not resolve project for {fp}: {e}; skipping.", file=sys.stderr)
        sys.exit(0)
    if not project_id or is_debounced(project_id):
        sys.exit(0)
    try:
        refresh_project(project_id, linked)
        mark_synced(project_id)
        sys.exit(0)
    except RateLimited as e:
        # Transient. Don't block the edit, but mark state so we don't retry
        # immediately on every subsequent edit and dig the rate limit deeper.
        mark_synced(project_id)
        print(f"[overleaf-sync-now] {e} Local file may be slightly stale.", file=sys.stderr)
        sys.exit(0)
    except AuthExpired as e:
        # Recoverable user action. Tell the AI by exiting 2 (Claude Code
        # surfaces stderr to the model as a blocking hook error so the
        # AI can prompt the user to re-auth instead of editing stale).
        print(f"[overleaf-sync-now] {e}", file=sys.stderr)
        print(
            "[overleaf-sync-now] Edit blocked to prevent writing over a stale local copy. "
            "After re-auth, retry the edit.",
            file=sys.stderr,
        )
        sys.exit(2)
    except Exception as e:
        # Unknown error (network, zip parse, sandbox block, etc.): don't
        # block the edit; mark synced so the next edit doesn't retry
        # inside the debounce window.
        mark_synced(project_id)
        msg = str(e)
        if _is_sandbox_block(e) or "Outbound HTTPS to Overleaf was blocked" in msg:
            # Permanent until the user/shell config changes. "Retry after
            # debounce" would be misleading.
            print(f"[overleaf-sync-now] {e}", file=sys.stderr)
        else:
            print(
                f"[overleaf-sync-now] refresh failed: {e}; will retry after debounce.",
                file=sys.stderr,
            )
        sys.exit(0)


# -------- install / uninstall --------

HOME = pathlib.Path.home()
SKILL_TARGETS = {
    "Claude Code": HOME / ".claude" / "skills" / "overleaf",
    "Codex CLI":   HOME / ".codex"  / "skills" / "overleaf",
}
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"


def _hook_command():
    """Build the hook command. Prefer the absolute path so Claude Code's
    hook subprocess (which may have a stripped PATH) can find the binary."""
    found = shutil.which("overleaf-sync-now")
    if found:
        # Quote the path in case it contains spaces (e.g. C:\Program Files\...).
        return f'"{found}" hook'
    return "overleaf-sync-now hook"


def _is_our_hook(cmd):
    """Match the hooks we own (CLI form + earlier python-script form), nothing
    else. Tolerates trailing flags like `... hook --quiet` so future user
    customizations don't accumulate duplicates on every reinstall."""
    if not cmd:
        return False
    s = cmd.strip().lower()
    if "overleaf-sync-now" not in s and "overleaf_sync.py" not in s:
        return False
    # Either ends in `hook` or has ` hook ` as a token somewhere.
    return bool(re.search(r"(^|\s)hook(\s|$)", s))


def _is_junction(p):
    try:
        return os.path.isdir(p) and (p.lstat().st_file_attributes & 0x400) != 0
    except (AttributeError, OSError):
        return False


def _remove_existing_target(target):
    """Remove a junction/symlink/dir at target. Junctions removed without following."""
    if not (target.exists() or target.is_symlink()):
        return
    try:
        if target.is_symlink():
            target.unlink()
        elif _is_junction(target):
            os.rmdir(str(target))  # NTFS junction removal does not touch target
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as e:
        print(f"  WARN: could not remove {target}: {e}", file=sys.stderr)


def cmd_install(args):
    """Set up the skill in available AI tools and add the Claude Code hook.

    Everything written here is USER-GLOBAL (~/.claude/, ~/.codex/, ~/.local/bin/).
    Nothing project-specific. The hook + skill apply to every project the user
    works in, in any directory.
    """
    interactive = sys.stdin.isatty() and "--no-interactive" not in args
    print("=== Installing overleaf-sync-now (USER-GLOBAL, applies to all projects) ===\n")

    # 1. Copy SKILL.md into available skills directories.
    if not SKILL_MD_SRC.exists():
        print(f"ERROR: SKILL.md not found at {SKILL_MD_SRC}", file=sys.stderr)
        sys.exit(1)
    for tool, target in SKILL_TARGETS.items():
        if not target.parent.parent.exists():
            print(f"  - {tool}: skipped (parent dir not found)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _remove_existing_target(target)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(SKILL_MD_SRC), str(target / "SKILL.md"))
        print(f"  - {tool}: SKILL.md installed at {target / 'SKILL.md'}  [user-global]")

    # 2. Add Claude Code PreToolUse hook (atomically — settings.json
    # corruption breaks Claude Code start, so don't half-write it).
    if CLAUDE_SETTINGS.parent.exists():
        settings = {}
        if CLAUDE_SETTINGS.exists():
            try:
                with open(CLAUDE_SETTINGS) as f:
                    settings = json.load(f)
            except json.JSONDecodeError as e:
                # User has hand-edited a broken settings.json. Don't overwrite
                # silently — they'd lose their other config. Back up + fail loud.
                backup = CLAUDE_SETTINGS.with_suffix(f".json.broken-{int(time.time())}")
                shutil.copyfile(str(CLAUDE_SETTINGS), str(backup))
                print(
                    f"  ERROR: ~/.claude/settings.json is not valid JSON ({e}).\n"
                    f"  Backed up to {backup}. Fix the file (or restore the backup) and re-run install.",
                    file=sys.stderr,
                )
                sys.exit(1)
        hooks = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
        cleaned = []
        for entry in hooks:
            entry["hooks"] = [
                h for h in entry.get("hooks", [])
                if not _is_our_hook(h.get("command", ""))
            ]
            if entry["hooks"]:
                cleaned.append(entry)
        hook_cmd = _hook_command()
        cleaned.append({
            "matcher": "Read|Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": hook_cmd}],
        })
        settings["hooks"]["PreToolUse"] = cleaned
        _atomic_write_text(CLAUDE_SETTINGS, json.dumps(settings, indent=2))
        print(f"  - Claude Code hook updated to: {hook_cmd}  [user-global]")
    else:
        print("  - Claude Code: skipped (~/.claude/ not found)")

    # 3. Run setup wizard. Always non-interactive in install context: install
    # is the path agents run, and an unexpected paste prompt would block them
    # mid-flow. Users can always run `overleaf-sync-now setup` separately for
    # the manual paste fallback.
    print()
    cmd_setup([], force_noninteractive=True)

    print("\n=== Install complete (USER-GLOBAL) ===")
    print("Scope: applies to ALL projects, in any directory, for this user.")
    print("Nothing project-specific was written.")
    cli_path = shutil.which("overleaf-sync-now") or "(not on PATH yet - open a new shell)"
    print(f"CLI on PATH at: {cli_path}")
    print()
    # Check if auth resolved during the setup that just ran. If not, surface
    # the most likely fix (login on Windows, browser login elsewhere) up front
    # so the user doesn't have to wait for the first sync to fail.
    if not _validate_cookies(_load_cache()):
        print("AUTH NOT YET CAPTURED. To finish setup:")
        if os.name == "nt":
            print("  Run:  overleaf-sync-now login")
            print("  (Browser opens - log into Overleaf there. One-time. Required on Windows + Chrome 130+.)")
        else:
            print("  - If logged into overleaf.com in Chrome/Firefox/etc., setup will pick that up next time it's run.")
            print("  - Otherwise run:  overleaf-sync-now login   (opens a browser for you to log in)")
            print("  - Or paste a cookie:  overleaf-sync-now save-cookie \"<value>\"")
        print()
    print("Restart Claude Code (or Codex) for the skill and hook to load.")
    print("After restart, edit any .tex file under <Dropbox>/Apps/Overleaf/<project>/")
    print("and sync runs automatically before each AI edit, in every project.")
    print()
    print("Manual sync any time, from any directory:  overleaf-sync-now sync .")


def _ensure_playwright_browser():
    """Ensure Chromium is downloaded. Idempotent. ~150MB on first run.

    Playwright the Python package is a regular pyproject.toml dep so it's
    always available; only the browser binary is lazy-downloaded.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        # This can only happen if user did `uv tool install --no-deps` or similar.
        print(
            "[overleaf-sync-now] Playwright Python package missing. Reinstall the tool:\n"
            "  uv tool install --reinstall --from git+https://github.com/hanlulong/overleaf-sync-now overleaf-sync-now",
            file=sys.stderr,
        )
        sys.exit(1)
    print("[overleaf-sync-now] Ensuring Chromium browser is available (one-time, ~150MB on first run)...", file=sys.stderr)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False, stdout=sys.stderr, stderr=sys.stderr,
        )
    except Exception as e:
        print(f"[overleaf-sync-now] Browser install warning: {e}", file=sys.stderr)


def cmd_login(args):
    """Launch a browser for the user to log into Overleaf, then capture the
    session cookie via the browser's own API. This is the PROPER fix when
    automatic on-disk cookie extraction fails (Chrome 130+ app-bound encryption,
    Edge same, Firefox profile not present, etc.)."""

    if not sys.stdin.isatty():
        print(
            "ERROR: `login` requires an interactive terminal because a browser will open\n"
            "and you need to physically log in. Run this command from a real shell\n"
            "(not via an AI agent's automated tool call). Once done, the captured cookie\n"
            "is reused for weeks - the agent can run sync after you log in once.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Early exit: if a valid cookie already exists, no need to open a browser.
    cached = _load_cache()
    if _validate_cookies(cached):
        print(f"Already logged in. Cached cookie is valid (cache: {CACHE_FILE}).")
        print("Nothing to do. If you want to force a fresh login anyway, delete the cache file and re-run.")
        return

    _ensure_playwright_browser()
    from playwright.sync_api import sync_playwright

    profile_dir = CACHE_DIR / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("Opening a browser window. Log into https://www.overleaf.com when it appears.")
    print(f"(Login persists in {profile_dir}; you only do this once per several weeks.)")
    print()

    with sync_playwright() as p:
        ctx = None
        # Prefer system Chrome (no download); fall back to bundled Chromium.
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = {"user_data_dir": str(profile_dir), "headless": False}
                if channel:
                    kwargs["channel"] = channel
                ctx = p.chromium.launch_persistent_context(**kwargs)
                break
            except Exception:
                continue
        if not ctx:
            print("ERROR: Could not launch any browser. Make sure Chrome, Edge, or Chromium is installed.", file=sys.stderr)
            sys.exit(1)

        # Clear stale overleaf.com cookies so we don't accidentally capture an
        # expired session left over from a previous run of `login`.
        try:
            ctx.clear_cookies(domain="www.overleaf.com")
            ctx.clear_cookies(domain=".overleaf.com")
        except Exception:
            pass

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.overleaf.com/login")

        print("Waiting up to 5 minutes for you to log in. Will detect automatically; close the browser to abort.")
        deadline = time.time() + 300
        captured = None
        last_status = 0
        while time.time() < deadline:
            try:
                cookies = ctx.cookies("https://www.overleaf.com")
                target = {c["name"]: c["value"] for c in cookies if "overleaf" in c.get("domain", "")}
                if SESSION_COOKIE in target and _validate_cookies(target):
                    captured = target
                    break
            except Exception:
                # Browser closed by user
                break
            # Progress indicator every 30s
            if time.time() - last_status > 30:
                remaining = int(deadline - time.time())
                print(f"  ...still waiting ({remaining}s remaining; press Ctrl+C to cancel)")
                last_status = time.time()
            time.sleep(2)
        try:
            ctx.close()
        except Exception:
            pass

    if not captured:
        print("\nERROR: did not capture a valid Overleaf session cookie within 5 minutes.", file=sys.stderr)
        print("If you did log in but it wasn't detected, try `overleaf-sync-now save-cookie <value>`", file=sys.stderr)
        print("with your overleaf_session2 cookie value (DevTools -> Application -> Cookies).", file=sys.stderr)
        sys.exit(1)

    _save_cache(captured)
    print(f"\nLogged in. Cookie saved to {CACHE_FILE}.")
    print("Future syncs will use the cached cookie. Re-run `login` if it ever expires.")


_LOGIN_PROFILE_FAILURE_FILE = CACHE_DIR / ".login-profile-failed-at"
_LOGIN_PROFILE_COOLDOWN_SECONDS = 300  # 5 min


def _login_profile_in_cooldown():
    try:
        with open(_LOGIN_PROFILE_FAILURE_FILE) as f:
            return (time.time() - float(f.read().strip())) < _LOGIN_PROFILE_COOLDOWN_SECONDS
    except Exception:
        return False


def _mark_login_profile_failed():
    try:
        _atomic_write_text(_LOGIN_PROFILE_FAILURE_FILE, str(time.time()))
    except OSError:
        pass


def _try_login_profile():
    """Read cookies from the persistent browser profile created by `login`.
    Cheap file-existence check first to avoid the ~3-second Playwright launch
    cost on every sync when no profile exists. After a failure (lock, no
    cookies, network), back off for 5 minutes so the hot hook path doesn't
    keep launching headless Chromium on every AI edit."""
    profile_dir = CACHE_DIR / "browser-profile"
    cookies_db = profile_dir / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        return None
    if _login_profile_in_cooldown():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), headless=True
            )
            try:
                cookies = ctx.cookies("https://www.overleaf.com")
                target = {c["name"]: c["value"] for c in cookies if "overleaf" in c.get("domain", "")}
                if SESSION_COOKIE in target:
                    return target
            finally:
                ctx.close()
    except Exception:
        pass
    # Either no usable cookie or launch failed (likely profile-locked because
    # the user's `login` browser is still open). Mark cooldown so the next
    # ~5 min of hooks skip this expensive path entirely.
    _mark_login_profile_failed()
    return None


def cmd_save_cookie(args):
    """Save an overleaf_session2 cookie value (passed as an arg) to the cache.

    For AI-agent use: when the auth chain fails (e.g., Chrome 127+ app-bound
    encryption on Windows defeats browser_cookie3), the agent asks the user
    to paste the cookie value, then calls this command to persist it.
    """
    if not args:
        print(
            "Usage: save-cookie <session-cookie-value>\n"
            f"Get the value from your browser: open https://www.overleaf.com "
            f"(logged in), press F12 -> Application -> Cookies -> "
            f"https://www.overleaf.com -> find '{SESSION_COOKIE}' -> copy Value.",
            file=sys.stderr,
        )
        sys.exit(2)
    value = args[0].strip()
    # Strip surrounding quotes (common copy/paste artifact from terminals
    # that auto-quote pasted text, or users hand-quoting).
    while value and value[0] in ('"', "'") and value[-1] == value[0]:
        value = value[1:-1].strip()
    if not value:
        print("ERROR: empty cookie value.", file=sys.stderr)
        sys.exit(1)
    if value.lower() == SESSION_COOKIE.lower() or "=" in value:
        # User pasted "name=value" or just the name. Try to recover.
        if "=" in value:
            value = value.split("=", 1)[1].strip()
        else:
            print(
                f"ERROR: looks like you pasted the cookie *name* ({SESSION_COOKIE}). "
                f"Need the *value* - the long string after the '=' sign in the cookie row.",
                file=sys.stderr,
            )
            sys.exit(1)
    cookies = {SESSION_COOKIE: value}
    # Force a fresh probe: a stale validation timestamp from a previous cookie
    # would falsely accept a brand-new value without checking it.
    if not _validate_cookies(cookies, use_cache=False):
        print(
            f"ERROR: Overleaf rejected this cookie. Common causes:\n"
            f"  - Copied only part of the value (must be the entire long string)\n"
            f"  - Logged out before copying\n"
            f"  - Copied the wrong cookie (we need '{SESSION_COOKIE}', not csrf or others)",
            file=sys.stderr,
        )
        sys.exit(1)
    _save_cache(cookies)
    print(f"OK. Cookie saved to {CACHE_FILE} and validated against Overleaf.")


def cmd_doctor(args):
    """Diagnostic dump: show every check the auth chain runs and its result."""
    print(f"=== overleaf-sync-now doctor ===\n")
    print(f"Data dir:           {CACHE_DIR}")
    print(f"Cookie cache:       {CACHE_FILE} ({'present' if CACHE_FILE.exists() else 'MISSING'})")
    print(f"State file:         {STATE_FILE} ({'present' if STATE_FILE.exists() else 'MISSING'})")
    print(f"Project index:      {INDEX_FILE} ({'present' if INDEX_FILE.exists() else 'MISSING'})")
    print(f"Playwright profile: {PLAYWRIGHT_PROFILE} ({'present' if PLAYWRIGHT_PROFILE.exists() else 'MISSING'})")
    print()

    cached = _load_cache()
    if cached:
        names = sorted(cached.keys())
        has_session = SESSION_COOKIE in cached
        print(f"[1] Cached cookies: {len(names)} cookie(s); {SESSION_COOKIE}: {'yes' if has_session else 'no'}")
        if has_session:
            print(f"    -> Validating against Overleaf (forced fresh probe)...")
            # Doctor MUST do a real network check, not trust the cached
            # 'validated-at' timestamp. Otherwise it would just print 'OK'
            # for any cookie we touched in the last minute.
            valid = _validate_cookies(cached, use_cache=False)
            print(f"    -> Result: {'OK' if valid else 'INVALID (rejected by /project)'}")
    else:
        print("[1] Cached cookies: NONE")
    print()

    # Rookie (Chrome 127+ friendly)
    try:
        import rookiepy
        print(f"[2a] rookiepy: installed")
        for fn_name in ("chrome", "edge", "brave", "vivaldi", "chromium", "firefox"):
            fn = getattr(rookiepy, fn_name, None)
            if not fn:
                continue
            try:
                raw = fn(["overleaf.com", ".overleaf.com", "www.overleaf.com"])
                cookies = {c["name"]: c["value"] for c in raw}
                if SESSION_COOKIE in cookies:
                    print(f"     {fn_name:10s}: {SESSION_COOKIE} found ({cookies[SESSION_COOKIE][:10]}...)")
                else:
                    print(f"     {fn_name:10s}: no overleaf.com cookies (not logged in here?)")
            except Exception as e:
                print(f"     {fn_name:10s}: error - {type(e).__name__}: {e}")
    except ImportError:
        print("[2a] rookiepy: not installed (recommended on Windows for Chrome 127+; `pip install rookiepy`)")
    print()

    try:
        import browser_cookie3
        print(f"[2b] browser_cookie3: installed (v{getattr(browser_cookie3, '__version__', '?')})")
        for fn_name in ("chrome", "edge", "brave", "vivaldi", "chromium", "opera", "firefox", "librewolf"):
            fn = getattr(browser_cookie3, fn_name, None)
            if not fn:
                print(f"    {fn_name:10s}: function not exported")
                continue
            try:
                cj = fn(domain_name="overleaf.com")
                cookies = {c.name: c.value for c in cj}
                if SESSION_COOKIE in cookies:
                    print(f"    {fn_name:10s}: {SESSION_COOKIE} found ({cookies[SESSION_COOKIE][:10]}...)")
                else:
                    print(f"    {fn_name:10s}: no overleaf.com cookies (not logged in here?)")
            except Exception as e:
                print(f"    {fn_name:10s}: error - {type(e).__name__}: {e}")
    except ImportError:
        print("[2b] browser_cookie3: NOT INSTALLED — run `pip install browser-cookie3` or reinstall")
    print()

    pw = _try_playwright_profile()
    if pw and SESSION_COOKIE in pw:
        print(f"[3] Playwright profile: {SESSION_COOKIE} found")
    elif PLAYWRIGHT_PROFILE.exists():
        print("[3] Playwright profile: present but no usable overleaf.com cookies")
    else:
        print("[3] Playwright profile: not installed (this is fine; only needed if browser_cookie3 fails)")
    print()

    # Best-available cookies
    best = _resolve_cookies(interactive=False)
    if best:
        print(f"[4] Auth resolution: WOULD SUCCEED via the chain above")
    else:
        print("[4] Auth resolution: WOULD FAIL.")
        print()
        print("AGENT RECOVERY (do not just tell the user to 'log in' - on Windows + Chrome 130+")
        print("the browser already has the cookie but app-bound encryption blocks every automatic")
        print("extractor; only manual paste works):")
        print()
        print("  1. Ask the user: 'Open https://www.overleaf.com in your browser (already logged in).")
        print("     Press F12 -> Application tab -> Cookies -> https://www.overleaf.com -> find the")
        print("     row named overleaf_session2 -> copy its Value (long string starting with s%3A).")
        print("     Paste it.'")
        print("  2. Take their pasted value as <COOKIE>.")
        print("  3. Run: overleaf-sync-now save-cookie \"<COOKIE>\"")
        print("  4. Verify: overleaf-sync-now status")
    print()

    # [5] Version-match endpoint health (the new default refresh path).
    # Only probe if we have working auth — otherwise it's guaranteed to fail.
    print(f"[5] Version-match endpoint (/updates):")
    print(f"    Versions cache: {VERSIONS_FILE} ({'present' if VERSIONS_FILE.exists() else 'MISSING'})")
    if best:
        folder = pathlib.Path(args[0] if args else ".").resolve() if args else pathlib.Path(".").resolve()
        # Doctor must be the most robust command — never crash on a bad cwd
        # or auth blip. read_only=True avoids surprising the user with a
        # marker file written by what's supposed to be a diagnostic.
        try:
            linked, project_id = find_linked_folder(folder, read_only=True)
        except Exception as e:
            print(f"    Folder resolve: FAILED ({type(e).__name__}: {e})")
            linked, project_id = None, None
        if project_id:
            try:
                t = time.time()
                data = fetch_updates(project_id)
                updates = data.get("updates") or []
                latest = updates[0].get("toV") if updates else None
                cached = _load_versions().get(project_id)
                print(f"    Project: {project_id}  ({linked})")
                print(f"    Probe:   OK  ({time.time()-t:.2f}s, {len(updates)} updates in first page)")
                print(f"    Latest Overleaf toV: {latest}")
                print(f"    Cached   local  toV: {cached if cached is not None else '(none - next sync bootstraps)'}")
            except Exception as e:
                print(f"    Probe:   FAILED ({type(e).__name__}: {e})")
        else:
            print(f"    Folder {folder} is not under any Overleaf project; can't probe /updates without a project ID.")
    else:
        print(f"    Skipped (auth resolution failed above; fix auth first)")


def cmd_uninstall(args):
    print("=== Uninstalling overleaf-sync-now skill ===")
    for tool, target in SKILL_TARGETS.items():
        if target.exists() or target.is_symlink():
            _remove_existing_target(target)
            print(f"  - {tool}: removed {target}")
    if CLAUDE_SETTINGS.exists():
        try:
            with open(CLAUDE_SETTINGS) as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            print(
                f"  WARN: ~/.claude/settings.json is not valid JSON ({e}); skipping hook removal.",
                file=sys.stderr,
            )
        else:
            hooks = settings.get("hooks", {}).get("PreToolUse", [])
            cleaned = []
            for entry in hooks:
                entry["hooks"] = [
                    h for h in entry.get("hooks", [])
                    if not _is_our_hook(h.get("command", ""))
                ]
                if entry["hooks"]:
                    cleaned.append(entry)
            if "hooks" in settings:
                settings["hooks"]["PreToolUse"] = cleaned
            _atomic_write_text(CLAUDE_SETTINGS, json.dumps(settings, indent=2))
            print("  - Claude Code hook removed")
    print(f"  - Cookies and state preserved at {CACHE_DIR} (delete manually if desired)")


# -------- entry --------

COMMANDS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "setup": cmd_setup,
    "login": cmd_login,
    "save-cookie": cmd_save_cookie,
    "link": cmd_link,
    "sync": cmd_sync,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "projects": cmd_projects,
    "hook": cmd_hook,
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    arg = sys.argv[1]
    if arg in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    if arg in ("-V", "--version", "version"):
        from . import __version__
        print(f"overleaf-sync-now {__version__}")
        sys.exit(0)
    if arg not in COMMANDS:
        print(f"Unknown subcommand: {arg!r}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(2)
    COMMANDS[arg](sys.argv[2:])


if __name__ == "__main__":
    main()
