"""overleaf-sync-now CLI.

Subcommands:
  install                       Link skill into Claude Code/Codex + add hook + run setup
  login                         Open a controlled browser to log into Overleaf and capture
                                the cookie via DevTools Protocol (the proper fix for Chrome
                                130+ app-bound encryption). One-time, persists for weeks.
  setup                         Auth setup (auto-detect from existing browsers/profiles)
  save-cookie [-]               Save an overleaf_session2 cookie typed at a hidden prompt
                                (or piped on stdin with `-`). Headless fallback only; the
                                user runs it in their own terminal, never via an agent.
  link <project_id> [folder]    Mark a folder as belonging to an Overleaf project (override)
  sync [folder] [--force]       Refresh the linked folder against Overleaf. Probes
                                /project/<id>/updates and downloads the zip only when
                                a web-origin change has actually happened. --force always
                                re-extracts.
  status [folder]               Show data dir, cookie validity, and link/sync state
  projects [--refresh]          List your Overleaf projects (name + ID)
  doctor                        Verbose diagnostic dump of the auth chain
  hook                          PreToolUse hook entrypoint for Claude Code and Codex CLI
                                (reads the tool-call JSON from stdin)
  uninstall                     Remove skill copies and hooks (cookies preserved)

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
  A folder under Apps/Overleaf/<name>/ that cannot be resolved (no auth,
  no matching project, ambiguous name) makes the hook BLOCK writes (exit 2)
  and warn on reads, instead of silently passing the edit through.

Refresh flow:
  1. GET /project/<id>/updates -- cheap probe (~0.3s, ~30 KB) that returns the
     version history with per-update pathnames and origin.
  2. If latest toV == cached toV for this project: no-op.
  3. Else walk updates back to cached toV; skip updates with origin.kind="dropbox"
     (those are the local->Dropbox->Overleaf round-trips of our own saves).
     Web-side file adds/renames (`project_ops`) are pulled too; web-side
     deletes/renames are recorded so the hook can block edits to the old path
     (local files are never deleted).
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
WEB_REMOVED_FILE = CACHE_DIR / "web-removed.json"
PROJECT_MARKER = ".overleaf-project"
# Read-type hook calls share one probe per project per 30 s. Write-type calls
# (Edit/Write/apply_patch/non-read-only shell commands) use a much shorter
# window so a web edit that lands between a Read and the following Edit is
# still caught (the mid-turn window described in issue #2).
DEBOUNCE_SECONDS = 30
WRITE_DEBOUNCE_SECONDS = 3
LATEX_EXTS = ("tex", "bib", "cls", "sty", "bst")
_LATEX_PATH_RE = re.compile(r"\.(?:tex|bib|cls|sty|bst)$", re.IGNORECASE)
_PROJECT_ID_RE = re.compile(r"[0-9a-f]{24}")
# Claude Code's default command-hook timeout is 600 s; we write an explicit
# value that comfortably covers a first-use bootstrap (full zip, 10-30 s) so
# an old short timeout (v0.0.1 wrote 8 s) never kills the refresh mid-pull.
HOOK_TIMEOUT_SECONDS = 180
CLAUDE_HOOK_MATCHER = "Read|Edit|Write|Bash|PowerShell"
CODEX_HOOK_MATCHER = "apply_patch|Bash"
BASE = "https://www.overleaf.com"
SESSION_COOKIE = "overleaf_session2"
# Chromium-family + Firefox cookie backends to probe, in priority order, for
# both rookiepy and browser_cookie3. Single source of truth so the live auth
# chain and the `doctor` diagnostics probe the SAME set — they had drifted
# (doctor's rookiepy block silently omitted opera/librewolf).
CHROMIUM_COOKIE_FNS = (
    "chrome", "edge", "brave", "vivaldi", "chromium", "opera", "firefox", "librewolf",
)
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
        if os.name == "posix" and _is_under_data_dir(path):
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            # Tighten the data dir itself so the persistent login browser
            # profile (Chromium-written, never chmod'd by us) and every other
            # secret under it are unreachable by other local users. Idempotent
            # and cheap; covers fresh installs where this write created the dir.
            _harden_dir(CACHE_DIR)
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
        return path.is_relative_to(data)
    except OSError:
        return False


def _harden_dir(path):
    """Best-effort: make a directory user-private (0o700) on POSIX.

    The 0o600 on cookies.json only protects files this module writes. The
    persistent `login` browser profile under CACHE_DIR/browser-profile/ holds
    an equally-sensitive, weeks-long Overleaf session but is written by
    Chromium and never chmod'd by us. Tightening the data dir itself to 0o700
    stops another local user (shared workstation, lab box, CI runner) from
    traversing a world-readable parent to reach it. No-op on Windows, where
    ACL inheritance from the parent is the relevant control."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


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
    for fn_name in (*CHROMIUM_COOKIE_FNS, "load"):
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
    for fn_name in CHROMIUM_COOKIE_FNS:
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


def _resolve_cookies_with_source(interactive=False):
    """Walk the auth chain. Returns (cookies, source_label) or (None, None).

    `setup` reports the label so the user knows which source won; every
    other caller goes through `_resolve_cookies`."""
    cached = _load_cache()
    if _validate_cookies(cached):
        return cached, "cached cookie"
    # Persistent browser profile from `login` command (the proper fix for
    # Chrome 130+ app-bound encryption). Browser API can read HttpOnly cookies
    # that no on-disk extractor can decrypt without admin.
    chain = (
        ("`login` browser profile", _try_login_profile),
        # rookiepy: Chrome 127+ friendly via Rust
        ("rookiepy (installed browsers)", _try_rookiepy),
        ("browser_cookie3 (installed browsers)", _try_browser_cookie3),
        ("Claude Code Playwright profile", _try_playwright_profile),
    )
    for label, fn in chain:
        found = fn()
        if _validate_cookies(found):
            _save_cache(found)
            return found, label
    if interactive:
        manual = _prompt_manual_cookie()
        if _validate_cookies(manual):
            _save_cache(manual)
            return manual, "hidden terminal prompt"
    return None, None


def _resolve_cookies(interactive=False):
    return _resolve_cookies_with_source(interactive=interactive)[0]


def _prompt_manual_cookie():
    """Interactive, user-only fallback. Reads the value with getpass so it is
    not echoed to the terminal (or captured by a screen recording / terminal
    scrollback)."""
    import getpass
    print()
    print("Could not auto-detect a logged-in Overleaf session.")
    print("Preferred fix: Ctrl+C and run `overleaf-sync-now login` (opens a browser).")
    print()
    print("Manual fallback (type it here yourself; never paste it into an AI chat):")
    print("  1. Open https://www.overleaf.com in any browser and log in.")
    print("  2. Press F12 -> Application tab -> Storage -> Cookies -> https://www.overleaf.com")
    print(f"  3. Find the row named '{SESSION_COOKIE}' and copy the entire 'Value' field.")
    print()
    try:
        value = getpass.getpass(f"Paste the {SESSION_COOKIE} value (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not value:
        return None
    return {SESSION_COOKIE: value}


# -------- network --------

def _mount_retries(session):
    """Mount a bounded retry for idempotent GETs so a transient upstream 5xx
    (502/503/504 — common from Overleaf's CDN/LB during deploys or under
    zip-build load) or a connect/read blip doesn't fail the whole refresh.
    Without this, a single momentary 5xx in the PreToolUse hook is swallowed
    and the AI edits a stale local copy — the exact failure this tool prevents.

    Only GET is retried (all our Overleaf calls are reads). raise_on_status is
    False so that after retries are exhausted the response flows back to our
    own status-code handling (which produces clean, actionable messages)
    rather than urllib3's MaxRetryError. Best-effort: if the urllib3/requests
    internals ever move, we degrade to no-retry rather than crash."""
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except Exception:
        return
    try:
        retry = Retry(
            total=3, connect=3, read=2, backoff_factor=0.5,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
    except TypeError:
        # Very old urllib3 used method_whitelist instead of allowed_methods.
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


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
        # Genuine missing/expired-cookie case (a sandbox block already
        # returned above). Raise AuthExpired, NOT a plain RuntimeError: the
        # PreToolUse hook blocks the edit (exit 2) on AuthExpired so the AI
        # re-auths instead of silently editing a possibly-stale local copy —
        # the precise data-loss this tool exists to prevent. A plain
        # RuntimeError here would fall through to the hook's generic handler,
        # which marks-synced and exits 0, passing the stale edit through.
        raise AuthExpired(
            f"No valid Overleaf cookies (cache: {CACHE_FILE}).\n\n{_AUTH_RECOVERY}"
        )
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    _mount_retries(s)
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".overleaf.com")
    return s


class AuthExpired(RuntimeError):
    """Cookies invalid or expired; user needs to re-auth."""


# Shown to the model on every auth failure (hook exit 2, sync, doctor). It is
# deliberately login-first and never routes the session cookie through the
# agent: a cookie pasted into a chat lands in the model provider's transcript
# and in local session logs, and as a command-line argument it is visible in
# the process list and shell history.
_AUTH_RECOVERY = (
    "HOW TO FIX (tell the user; the cookie must never pass through this chat):\n"
    "  1. The user runs, in their own terminal:  overleaf-sync-now login\n"
    "     A browser window opens; they log into Overleaf there (works on Chrome 130+).\n"
    "     Do not suggest logging into Overleaf in their everyday browser instead:\n"
    "     Chrome 130+ app-bound encryption blocks reading cookies from it.\n"
    "  2. Only on a machine with no display for `login`: the user runs\n"
    "       overleaf-sync-now save-cookie\n"
    "     in their own terminal and pastes the overleaf_session2 value at the hidden prompt.\n"
    "  3. NEVER ask the user to paste the cookie into this chat, and never pass it as a\n"
    "     command-line argument. It grants full access to their Overleaf account.\n"
    "  4. Afterwards, run `overleaf-sync-now status` to verify, then retry."
)


class RateLimited(RuntimeError):
    """Overleaf returned 429. Pass `retry_after` (seconds) when known."""
    def __init__(self, retry_after=60):
        self.retry_after = retry_after
        super().__init__(f"Overleaf rate-limited; wait ~{retry_after}s and retry.")


def _is_network_exception(exc):
    """True if `exc`, or anything in its __cause__/__context__ chain, came from
    the HTTP stack (requests / urllib3 / socket). A plain OSError raised while
    writing a local file is NOT a network exception, even though its message
    may also say "Permission denied"."""
    try:
        import requests
        req_exc = requests.exceptions.RequestException
    except Exception:  # pragma: no cover - requests is a hard dependency
        req_exc = None
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if req_exc is not None and isinstance(e, req_exc):
            return True
        mod = type(e).__module__ or ""
        if mod.startswith(("urllib3", "requests", "socket")):
            return True
        e = e.__cause__ or e.__context__
    return False


def _is_sandbox_block(exc):
    """True if `exc` looks like the host shell blocking an outbound socket
    (sandboxed shell, firewall policy) rather than a generic network failure.
    Distinguishing these matters: retrying doesn't help here, and the tool
    should stop reaching for auth-recovery commands (setup/login/doctor) that
    would hit the same block.

    Only network exceptions qualify: a local PermissionError (read-only
    Dropbox folder, file locked by another process) used to be misreported as
    a sandbox block because it shares the "permission denied" wording."""
    if not _is_network_exception(exc):
        return False
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
            f"Cookies likely expired or were revoked.\n\n{_AUTH_RECOVERY}"
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
        raise AuthExpired(
            f"Overleaf rejected /download/zip (HTTP {r.status_code}).\n\n{_AUTH_RECOVERY}"
        )
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


# -------- web-side file deletes / renames (project_ops) --------
#
# Overleaf's /updates reports file-level operations separately from text
# edits: each update carries `project_ops`, a list of
#   {"add": {"pathname": P}, "atV": V}
#   {"rename": {"pathname": OLD, "newPathname": NEW}, "atV": V}
#   {"remove": {"pathname": P}, "atV": V}
# (services/project-history ChunkTranslator.js). The pathname of an added
# file is NOT repeated in `pathnames`, so a walk that only reads `pathnames`
# misses web-side adds and renames entirely.
#
# Adds and rename targets are pulled like any other web-origin change.
# Removes and rename sources are recorded in WEB_REMOVED_FILE so the hook can
# block an AI edit that would resurrect the old path through Dropbox. Local
# files are never deleted by this tool; an entry is dropped once the local
# file is gone (Dropbox caught up) or the path is re-created on the web.


def _norm_overleaf_path(p):
    return (p or "").lstrip("/")


def _collect_web_file_ops(web_updates):
    """Replay the project_ops of web-origin `updates` (newest-first, as
    /updates returns them) in chronological order.

    Returns (present, removed):
      present: set of paths whose final state is "exists on Overleaf" because
               a web add/rename created them (need pulling; clear any stale
               removed-entry for them).
      removed: {old_path: new_path_or_None} for paths whose final state is
               "gone from Overleaf" (deleted, or renamed to new_path).
    """
    ops = []
    for u_idx, u in enumerate(reversed(web_updates)):
        for o_idx, op in enumerate(u.get("project_ops") or []):
            if not isinstance(op, dict):
                continue
            at_v = op.get("atV")
            if not isinstance(at_v, (int, float)):
                at_v = u.get("fromV") if isinstance(u.get("fromV"), (int, float)) else 0
            ops.append((at_v, u_idx, o_idx, op))
    ops.sort(key=lambda t: (t[0], t[1], t[2]))
    state = {}  # path -> ("present", None) | ("removed", new_path_or_None)
    for _, _, _, op in ops:
        if isinstance(op.get("add"), dict):
            path = _norm_overleaf_path(op["add"].get("pathname"))
            if path:
                state[path] = ("present", None)
        elif isinstance(op.get("rename"), dict):
            old = _norm_overleaf_path(op["rename"].get("pathname"))
            new = _norm_overleaf_path(op["rename"].get("newPathname"))
            if old and new:
                state[old] = ("removed", new)
                state[new] = ("present", None)
            elif old:
                state[old] = ("removed", None)
        elif isinstance(op.get("remove"), dict):
            path = _norm_overleaf_path(op["remove"].get("pathname"))
            if path:
                state[path] = ("removed", None)
    present = {p for p, (kind, _) in state.items() if kind == "present"}
    removed = {p: new for p, (kind, new) in state.items() if kind == "removed"}
    return present, removed


def _load_web_removed():
    if WEB_REMOVED_FILE.exists():
        try:
            with open(WEB_REMOVED_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(
                f"[overleaf-sync-now] WARNING: {WEB_REMOVED_FILE} is corrupt ({e}); ignoring it.",
                file=sys.stderr,
            )
    return {}


def _record_web_file_ops(project_id, folder, present, removed):
    """Merge newly observed web-side removes into the persisted map, clear
    entries the web re-created, and prune entries whose local file is gone.
    Prints a WARN for each newly recorded path. Never deletes local files.
    Returns the project's current {old_path: new_path_or_None} map."""
    folder = pathlib.Path(folder)
    data = _load_web_removed()
    before = dict(data.get(project_id) or {})
    entry = dict(before)
    for path in present:
        entry.pop(path, None)
    new_paths = {}
    for path, new in removed.items():
        if entry.get(path, "\0") != new:
            new_paths[path] = new
        entry[path] = new
    entry = {path: new for path, new in entry.items() if _path_exists_under(folder, path)}
    if entry != before:
        if entry:
            data[project_id] = entry
        else:
            data.pop(project_id, None)
        try:
            _atomic_write_text(WEB_REMOVED_FILE, json.dumps(data, indent=2))
        except OSError as e:
            print(f"[overleaf-sync-now] WARNING: could not save {WEB_REMOVED_FILE}: {e}", file=sys.stderr)
    for path, new in sorted(new_paths.items()):
        if path not in entry:
            continue  # local copy already gone; nothing to warn about
        what = f"renamed on Overleaf to {new}" if new else "deleted on Overleaf"
        print(
            f"[overleaf-sync-now] WARN: {path} was {what}, but the local copy still exists "
            f"in {folder}. It was NOT deleted locally. AI edits to it are blocked until "
            f"Dropbox removes it (or you delete it yourself).",
            file=sys.stderr,
        )
    return entry


def web_removed_paths(project_id, folder):
    """The project's {old_path: new_path_or_None} map, pruned of entries whose
    local file no longer exists (read-only: does not rewrite the file)."""
    folder = pathlib.Path(folder)
    entry = _load_web_removed().get(project_id) or {}
    return {p: n for p, n in entry.items() if _path_exists_under(folder, p)}


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
    web_present, web_removed = set(), {}
    if force or bootstrap:
        need_paths = None  # sentinel: extract all
    else:
        need_paths = set()
        all_covered = False
        web_updates = []
        for u in updates:
            toV = u.get("toV")
            if toV is not None and toV <= cached_toV:
                all_covered = True
                break
            origin_kind = (u.get("meta", {}).get("origin") or {}).get("kind")
            if origin_kind == "dropbox":
                continue
            web_updates.append(u)
            for p in u.get("pathnames", []):
                need_paths.add(p)
        # File-level web ops (add / rename / remove). Adds and rename
        # targets must be pulled; removes are recorded below.
        web_present, web_removed = _collect_web_file_ops(web_updates)
        need_paths |= web_present
        has_file_ops = any(u.get("project_ops") for u in web_updates)
        if not all_covered:
            # Cached version is older than the oldest update we received in
            # the first page. Rather than paginate and risk missing a
            # web-origin edit, just pull the full zip and hash-diff.
            need_paths = None

        if need_paths is not None and not need_paths:
            if has_file_ops:
                # Only web-side deletes (no content to pull). Never report
                # this as a dropbox echo: it is a real web change.
                _record_web_file_ops(project_id, folder, web_present, web_removed)
                versions[project_id] = latest_toV
                _save_versions(versions)
                return (
                    f"web_file_ops (toV {cached_toV}->{latest_toV}; "
                    f"{len(web_removed)} path(s) deleted or renamed on Overleaf)"
                )
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
    if web_present or web_removed:
        _record_web_file_ops(project_id, folder, web_present, web_removed)
    # Only advance the cached version when EVERY web-origin change actually
    # landed locally. If a file was protected (skipped because the local copy
    # was modified within the protect window), do NOT claim we're synced to
    # latest_toV: the cheap-probe gate above (latest_toV == cached_toV ->
    # "no_change") would then short-circuit every future sync and the pending
    # web edit would be stranded permanently — the exact silent-staleness this
    # tool exists to prevent. Holding cached_toV makes the next sync re-pull it
    # once the local mtime ages out of the protect window (or the user runs
    # --force). force never protect-skips (protect=0), so it always advances;
    # an already-written file hits the byte-equal short-circuit on re-pull and
    # is counted unchanged, so re-fetching the whole batch is cheap and safe.
    if n_protected == 0:
        versions[project_id] = latest_toV
        _save_versions(versions)
    label = "bootstrap" if bootstrap else ("forced" if force else "refreshed")
    if n_protected == 0:
        parts = [f"toV -> {latest_toV}", f"wrote {n_written}", f"unchanged {n_unchanged}"]
    else:
        held = cached_toV if cached_toV is not None else "unsynced"
        parts = [
            f"wrote {n_written}", f"unchanged {n_unchanged}",
            f"protected {n_protected} (recent local edits; version held at {held} so the "
            f"pending web change is re-offered on the next sync — pass --force to pull it now)",
        ]
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

    Returns (None, None) when nothing resolves; `resolve_project` returns the
    same lookup with the reason attached (the hook needs it to fail closed).
    """
    res = resolve_project(
        start, verbose=verbose, allow_disambig_network=allow_disambig_network,
        read_only=read_only,
    )
    if res["reason"] == "ok":
        return res["folder"], res["project_id"]
    return None, None


def resolve_project(start, *, verbose=True, allow_disambig_network=True, read_only=False):
    """Resolve `start` (a file or folder path; it need not exist yet) to its
    Overleaf project. Returns a dict:

      folder       linked folder (Path) or, when unresolved under
                   Apps/Overleaf/<name>/, that <name> folder; else None
      project_id   24-hex id on success, else None
      reason       "ok"         resolved (marker or auto-link)
                   "unmanaged"  not under a marker or Apps/Overleaf/<name>/
                   "auth"       under Apps/Overleaf/<name>/, no valid cookies
                   "none"       ... no living Overleaf project has that name
                   "ambiguous"  ... several living projects share that name
                   "network"    ... project list unavailable (network/sandbox)
      name         the <name> folder component, when applicable
      candidates   ambiguous candidate records
      detail       error text for auth/network

    `verbose` controls whether diagnostic messages are printed to stderr — the
    hook passes False so repeated keystrokes don't spam.

    `allow_disambig_network` controls whether ambiguity may be resolved by
    fingerprinting candidates' /updates against local files (Layer 6a, costs
    one HTTP per candidate). Hook callers should leave this True since
    auto-link runs at most once per folder before a marker is written.

    `read_only` suppresses the side-effect marker write on a successful
    auto-link. Used by `status` so a diagnostic command doesn't surprise
    the user by creating files.
    """
    res = {"folder": None, "project_id": None, "reason": "unmanaged",
           "name": None, "candidates": [], "detail": None}
    p = pathlib.Path(start).expanduser().resolve()
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
            if pid and not (isinstance(pid, str) and _PROJECT_ID_RE.fullmatch(pid)):
                # Markers round-trip through the Dropbox bridge, so anyone
                # with access to the Overleaf project can rewrite them. Only a
                # real 24-hex project id may ever reach a request URL.
                if verbose:
                    print(
                        f"[overleaf-sync-now] WARN: ignoring {marker}: project_id {pid!r} is "
                        f"not a 24-character hex Overleaf id. Fix it with "
                        f"`overleaf-sync-now link <project_id> {cur}`.",
                        file=sys.stderr,
                    )
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
                    res.update(folder=cur, project_id=pid, reason="ok")
                    return res
        if cur.parent == cur:
            break
        cur = cur.parent
    parts = p.parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "overleaf" and i > 0 and parts[i - 1].lower() == "apps":
            if i + 1 < len(parts):
                name = parts[i + 1]
                folder = pathlib.Path(*parts[: i + 2])
                outcome = {}
                pid = _autolink_resolve(
                    name, folder, verbose=verbose,
                    allow_disambig_network=allow_disambig_network,
                    write_marker=not read_only, outcome=outcome,
                )
                res.update(folder=folder, name=name)
                if pid:
                    res.update(project_id=pid, reason="ok")
                else:
                    res.update(
                        reason=outcome.get("reason") or "none",
                        candidates=outcome.get("candidates") or [],
                        detail=outcome.get("detail"),
                    )
                return res
    return res


def _autolink_resolve(name, folder, *, verbose=True, allow_disambig_network=True,
                      write_marker=True, outcome=None):
    """Auto-link `folder` (an .../Apps/Overleaf/<name>/ directory) to a
    project ID. Writes a `.overleaf-project` marker on success so future
    resolution skips the index lookup entirely (unless write_marker=False).

    Returns the project_id on success, None otherwise. When `outcome` (a dict)
    is passed, it is filled with "reason" ("auth" / "network" / "none" /
    "ambiguous"), "candidates" and "detail" so callers can fail closed.
    When verbose, prints a clear error to stderr listing the candidates and
    the exact `link` command to run.
    """
    if outcome is None:
        outcome = {}
    import requests
    # Auth failures propagate as a reason, never as a silent None: the hook
    # must block writes when it cannot tell which project a folder is.
    try:
        records = _load_projects_records(strict=True)
    except AuthExpired as e:
        outcome.update(reason="auth", detail=str(e))
        if verbose:
            print("[overleaf-sync-now] auto-link: not logged in to Overleaf; cannot look up the project list.", file=sys.stderr)
        return None
    except (RuntimeError, requests.exceptions.RequestException) as e:
        outcome.update(reason="network", detail=str(e))
        if verbose:
            print(f"[overleaf-sync-now] auto-link: could not fetch project list: {e}", file=sys.stderr)
        return None
    status_outcome = _resolve_by_name(name, records)
    if status_outcome[0] == "none" and _index_age_seconds() > 60:
        # The cached index can be up to 24 h old, so a project created since
        # then is missing from it. Re-fetch once before declaring "none".
        try:
            records = _load_projects_records(force_refresh=True, strict=True)
            status_outcome = _resolve_by_name(name, records)
        except AuthExpired as e:
            outcome.update(reason="auth", detail=str(e))
            return None
        except (RuntimeError, requests.exceptions.RequestException):
            pass
    status = status_outcome[0]
    outcome["reason"] = status if status != "ok" else None
    if status == "ok":
        pid = status_outcome[1]
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
        candidates = status_outcome[1]
        outcome["candidates"] = candidates
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
                outcome["reason"] = None
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
    outcome["reason"] = "none"
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


def _load_projects_records(*, force_refresh=False, strict=False):
    """Return the list of project records, refreshing from Overleaf when
    the cache is missing/expired/wrong-version. May make a network call;
    use `_load_cached_projects_records` for the no-network variant.

    strict=True raises RuntimeError when the fetch fails and there is no
    cached index to fall back on (instead of returning []), so a resolver
    can tell "project list unavailable" from "no such project"."""
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
    return _refresh_projects_records(strict=strict)


def _index_age_seconds():
    """Seconds since the cached project index was written (inf if none)."""
    try:
        with open(INDEX_FILE) as f:
            return time.time() - float(json.load(f).get("ts", 0))
    except Exception:
        return float("inf")


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


def _refresh_projects_records(strict=False):
    """Fetch /project, parse ol-prefetchedProjectsBlob, and persist a v2
    record list with the disambiguating fields (id, name, trashed, archived,
    lastUpdated, ownerId). Falls back to whatever's on disk when Overleaf
    is unhappy so we don't lose a previously-good index. With strict=True,
    a failed fetch with nothing on disk raises RuntimeError instead.
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
        cached = _load_cached_projects_records()
        if strict and not cached:
            raise RuntimeError(f"Network error fetching the Overleaf project list: {e}") from e
        return cached
    if r.status_code != 200:
        cached = _load_cached_projects_records()
        if strict and not cached:
            raise RuntimeError(f"Overleaf project list returned HTTP {r.status_code}")
        return cached
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


def is_debounced(project_id, window=DEBOUNCE_SECONDS):
    return (time.time() - _state().get(project_id, 0)) < window


# -------- subcommands --------

def cmd_setup(args, *, force_noninteractive=False):
    interactive = (
        not force_noninteractive
        and sys.stdin.isatty()
        and not os.environ.get("OVERLEAF_SYNC_NONINTERACTIVE")
    )
    print("Setting up Overleaf sync...")
    # Same chain `sync` and the hook use (cached -> login profile -> rookiepy
    # -> browser_cookie3 -> Playwright profile), so setup never reports
    # failure for a session that sync would find.
    cookies, source = _resolve_cookies_with_source(interactive=interactive)
    if cookies:
        print(f"  - Valid Overleaf session found via {source} ({SESSION_COOKIE}: present).")
        print(f"  - Cached at {CACHE_FILE}")
        return
    if not interactive:
        print()
        print("AUTO-DETECT FAILED. Next step (the user runs this in their own terminal):")
        print("    overleaf-sync-now login")
        print("  A browser window opens; log into Overleaf there. Works on Chrome 130+,")
        print("  where logging into your everyday browser does NOT help (app-bound")
        print("  encryption blocks reading its cookies).")
        print("  No display (server/SSH)? Run `overleaf-sync-now save-cookie` in a terminal")
        print("  and paste the cookie at its hidden prompt. Never paste it into an AI chat.")
        print()
        print("Sync will not work until cookies are obtained.")
        return
    print("ERROR: Could not authenticate. Run `overleaf-sync-now login`.", file=sys.stderr)
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
            f"  Expected: 24 lowercase hex characters (e.g. 0123456789abcdef01234567)\n"
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
    res = resolve_project(folder)
    linked, project_id = res["folder"], res["project_id"]
    if res["reason"] == "auth":
        print(f"ERROR: {res.get('detail')}", file=sys.stderr)
        sys.exit(1)
    if res["reason"] == "network":
        print(f"ERROR: could not fetch the Overleaf project list: {res.get('detail')}", file=sys.stderr)
        sys.exit(1)
    if res["reason"] in ("none", "ambiguous"):
        # resolve_project (verbose) already printed the candidates / hints.
        print(
            f"ERROR: could not auto-link {linked} (reason: {res['reason']}). "
            f"Link it explicitly: `overleaf-sync-now link <project_id> \"{linked}\"`.",
            file=sys.stderr,
        )
        sys.exit(1)
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
                print("Cookie auth: INVALID — sync would FAIL and the hook blocks AI edits. "
                      "Fix: the user runs `overleaf-sync-now login` in their own terminal "
                      "(headless: `overleaf-sync-now save-cookie`, hidden prompt).")
    _print_install_health()
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


# -------- hook: which files does this tool call touch? --------
#
# Claude Code   Read / Edit / Write        tool_input.file_path
#               Bash / PowerShell          tool_input.command (scanned)
# Codex CLI     apply_patch                tool_input.command (the patch text)
#               Bash                       tool_input.command (scanned)
#
# Shell commands are scanned heuristically: any token ending in a LaTeX
# extension is resolved against the payload `cwd` (and any `cd <dir>` in the
# command). Paths assembled at run time (variables, `find -exec`, scripts that
# open files themselves) cannot be seen; see README "Known limitations".

_READ_TOOLS = ("Read",)
_SHELL_TOOLS = ("Bash", "PowerShell")

_LATEX_MENTION_RE = re.compile(r"\.(?:tex|bib|cls|sty|bst)(?![\w])", re.IGNORECASE)
_SHELL_PATH_RE = re.compile(
    r'"([^"\n]*?\.(?:tex|bib|cls|sty|bst))"'
    r"|'([^'\n]*?\.(?:tex|bib|cls|sty|bst))'"
    r"|((?:\\.|[^\s'\"<>|;&()`=,])+?\.(?:tex|bib|cls|sty|bst))(?![\w.])",
    re.IGNORECASE,
)
_SHELL_CD_RE = re.compile(
    r"""(?:^|[;&|\n]|&&)\s*(?:cd|pushd|Set-Location|sl)\s+("[^"\n]+"|'[^'\n]+'|[^\s;&|]+)""",
    re.IGNORECASE,
)
_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to):\s*(.+?)\s*$", re.MULTILINE
)
_SHELL_SPLIT_RE = re.compile(r"\|\||&&|[|;&\n]")
# First words of shell segments that only read LaTeX sources. Anything else
# (python, perl, cp, mv, editors, unknown tools) counts as a possible write.
_READ_ONLY_PROGRAMS = {
    "cat", "head", "tail", "less", "more", "bat", "nl", "grep", "egrep", "fgrep",
    "rg", "ag", "ack", "wc", "ls", "diff", "cmp", "file", "stat", "du", "sort",
    "uniq", "cut", "tr", "awk", "gawk", "sed", "find", "echo", "printf", "pwd",
    "which", "type", "test", "[", "true", "cd", "pushd", "popd", "texcount",
    "chktex", "lacheck", "detex", "kpsewhich", "latexmk", "pdflatex", "xelatex",
    "lualatex", "latex", "bibtex", "biber", "makeindex", "synctex", "git",
    # PowerShell
    "get-content", "gc", "select-string", "sls", "get-childitem", "gci", "dir",
    "measure-object", "get-item", "test-path", "set-location", "sl",
    "compare-object", "format-hex", "write-output", "write-host",
}
_READ_ONLY_GIT = {"diff", "log", "show", "status", "blame", "grep", "ls-files"}
_REDIRECT_RE = re.compile(r"(?<![0-9&])>>?\s*(\"[^\"]*\"|'[^']*'|[^\s;&|]+)")


def _command_text(tool_input):
    cmd = tool_input.get("command", "")
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else ""


def _is_latex_path(p):
    return bool(p) and bool(_LATEX_PATH_RE.search(str(p)))


def _is_read_only_command(cmd):
    """Heuristic: True when every segment of a shell command starts with a
    known read-only program and nothing redirects into a LaTeX file. Unknown
    programs count as writes (fail closed)."""
    for target in _REDIRECT_RE.findall(cmd):
        t = target.strip("\"'")
        if _is_latex_path(t) or any(ch in t for ch in "*?["):
            return False
    # Quoted strings may contain | ; & (regexes, sed scripts); fd
    # duplications like 2>&1 contain &. Neither separates commands.
    bare = re.sub(r'"(?:\\.|[^"\\])*"|\'[^\']*\'', " Q ", cmd)
    bare = re.sub(r"\d*>&\d*-?|&>>?", " ", bare)
    for seg in _SHELL_SPLIT_RE.split(bare):
        words = seg.strip().split()
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
            words.pop(0)  # leading VAR=value assignments
        if not words:
            continue
        prog = os.path.basename(words[0].strip("\"'(")).lower()
        if prog.endswith(".exe"):
            prog = prog[:-4]
        if prog not in _READ_ONLY_PROGRAMS:
            return False
        rest = words[1:]
        if prog in ("sed", "awk", "gawk") and any(
            w.startswith("--in-place") or (w.startswith("-") and not w.startswith("--") and "i" in w)
            for w in rest
        ):
            return False
        if prog == "find" and any(
            w in ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint") for w in rest
        ):
            return False
        if prog == "git" and (not rest or rest[0] not in _READ_ONLY_GIT):
            return False
    return True


def _shell_candidate_paths(cmd, cwd, *, posix_escapes=True):
    """Candidate LaTeX paths mentioned in a shell command, as absolute Paths.
    Globs are expanded (bounded). Paths need not exist."""
    import glob as _glob
    tokens = []
    for m in _SHELL_PATH_RE.finditer(cmd):
        tok = m.group(1) or m.group(2) or m.group(3)
        if not tok:
            continue
        if "$" in tok:
            tok = os.path.expandvars(tok)
        tokens.append(tok)
        if posix_escapes and m.group(3) and "\\" in tok:
            tokens.append(re.sub(r"\\(.)", r"\1", tok))
    if not tokens:
        return []
    cwd = pathlib.Path(cwd)
    bases = [cwd]
    for m in _SHELL_CD_RE.finditer(cmd):
        d = m.group(1).strip("\"'")
        if d and d != "-":
            bases.append(cwd / pathlib.Path(d).expanduser())
    out, seen = [], set()
    for tok in tokens:
        tp = pathlib.Path(tok).expanduser()
        cands = [tp] if tp.is_absolute() else [b / tp for b in bases]
        for c in cands:
            if any(ch in str(c) for ch in "*?["):
                expanded = sorted(_glob.glob(str(c)))[:200]
            else:
                expanded = [str(c)]
            for e in expanded:
                if e not in seen and _is_latex_path(e):
                    seen.add(e)
                    out.append(pathlib.Path(e))
    return out


def _patch_paths(text, cwd):
    """Paths named by `*** Update File:` / `*** Add File:` / `*** Delete File:`
    / `*** Move to:` lines of a Codex apply_patch payload."""
    cwd = pathlib.Path(cwd)
    out = []
    for m in _PATCH_FILE_RE.finditer(text or ""):
        p = pathlib.Path(m.group(1).strip().strip("\"'")).expanduser()
        out.append(p if p.is_absolute() else cwd / p)
    return out


def _hook_targets(data):
    """Classify a hook payload. Returns (kind, paths, cwd_hint) where kind is
    "read", "write" or None (not ours), paths are the LaTeX files touched, and
    cwd_hint is the cwd when a shell command mentions a LaTeX extension
    without a resolvable path (e.g. `sed -i ... *.tex` in a project folder)."""
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None, [], None
    cwd = data.get("cwd") or os.getcwd()
    if tool in _READ_TOOLS or tool in ("Edit", "Write"):
        fp = tool_input.get("file_path") or ""
        if not _is_latex_path(fp):
            return None, [], None
        p = pathlib.Path(fp).expanduser()
        if not p.is_absolute():
            p = pathlib.Path(cwd) / p
        return ("read" if tool in _READ_TOOLS else "write"), [p], None
    if tool == "apply_patch":
        paths = [p for p in _patch_paths(_command_text(tool_input), cwd) if _is_latex_path(p)]
        return ("write", paths, None) if paths else (None, [], None)
    if tool in _SHELL_TOOLS:
        cmd = _command_text(tool_input)
        if not _LATEX_MENTION_RE.search(cmd):
            return None, [], None
        paths = _shell_candidate_paths(cmd, cwd, posix_escapes=(tool == "Bash"))
        paths += [p for p in _patch_paths(cmd, cwd) if _is_latex_path(p)]
        kind = "read" if (_is_read_only_command(cmd) and "*** Begin Patch" not in cmd) else "write"
        return kind, paths, pathlib.Path(cwd)
    return None, [], None


def _latex_snapshot(folder, targets=()):
    """{path_key: (mtime_ns, size) or None} used to see what a refresh
    rewrote. With explicit `targets`, only those are stat'ed (cheap). Without
    (a shell command that only mentions `*.tex` in the project folder), the
    LaTeX files under `folder` are walked, bounded and skipping dotdirs."""
    snap = {}
    if targets:
        for p in targets:
            key = os.path.normcase(os.path.abspath(str(p)))
            try:
                st = os.stat(key)
                snap[key] = (st.st_mtime_ns, st.st_size)
            except OSError:
                snap[key] = None
        return snap
    visited = 0
    try:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                visited += 1
                if visited > 20000:
                    return snap
                if _is_latex_path(f):
                    path = os.path.normcase(os.path.abspath(os.path.join(root, f)))
                    try:
                        st = os.stat(path)
                        snap[path] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        pass
    except OSError:
        pass
    return snap


def _rel_posix(path, folder):
    try:
        return pathlib.Path(os.path.abspath(str(path))).relative_to(
            pathlib.Path(os.path.abspath(str(folder)))
        ).as_posix()
    except ValueError:
        return None


def _hook_unresolved_message(res, tool):
    folder = res.get("folder")
    reason = res.get("reason")
    head = (
        f"[overleaf-sync-now] Could not tell which Overleaf project {folder} belongs to"
    )
    if reason == "auth":
        return (
            f"{head}: not logged in to Overleaf.\n{_AUTH_RECOVERY}"
        )
    if reason == "ambiguous":
        lines = [f"{head}: {len(res.get('candidates') or [])} living Overleaf projects are named "
                 f"{res.get('name')!r}. Refusing to guess. Candidates (most recent first):"]
        for c in res.get("candidates") or []:
            lines.append(f"    {c.get('id')}  lastUpdated={c.get('lastUpdated') or '?'}")
        lines.append("  Ask the user which one this folder is, then run:")
        lines.append(f"    overleaf-sync-now link <project_id> \"{folder}\"")
        lines.append("  Do not pick a project id on the user's behalf.")
        return "\n".join(lines)
    return (
        f"{head}: no living (non-trashed, non-archived) Overleaf project is named "
        f"{res.get('name')!r}.\n"
        f"  If the project was renamed on Overleaf, ask the user for the project id\n"
        f"  (the 24-character hex in https://www.overleaf.com/project/<id>) and run:\n"
        f"    overleaf-sync-now link <project_id> \"{folder}\"\n"
        f"  `overleaf-sync-now projects --refresh` lists the user's projects."
    )


def cmd_hook(args):
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        # Don't block the edit, but surface the schema mismatch so future
        # Claude Code hook-payload changes don't make us silently no-op.
        print(f"[overleaf-sync-now] hook stdin not valid JSON ({e}); skipping.", file=sys.stderr)
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)
    tool = data.get("tool_name") or "tool"
    kind, paths, cwd_hint = _hook_targets(data)
    if kind is None:
        sys.exit(0)
    is_write = kind == "write"

    # Group the touched files by project. find_linked_folder / resolve_project
    # can in principle raise (PermissionError on a network share, OSError
    # reading a marker, etc.); don't let those crash the hook.
    #
    # verbose=False so repeated tool calls don't spam stderr with the same
    # auto-link warning; the reasons come back in the result instead.
    projects = {}   # project_id -> {"folder": Path, "paths": [Path]}
    blocks = []     # messages that make the hook exit 2
    warnings = []
    unresolved_seen = set()
    probe_points = [(p, True) for p in paths]
    if cwd_hint is not None:
        probe_points.append((cwd_hint, False))
    for point, is_file_target in probe_points:
        try:
            res = resolve_project(point, verbose=False)
        except Exception as e:
            warnings.append(f"[overleaf-sync-now] hook: could not resolve project for {point}: {e}; skipping.")
            continue
        reason = res["reason"]
        if reason == "ok":
            entry = projects.setdefault(res["project_id"], {"folder": res["folder"], "paths": []})
            if is_file_target:
                entry["paths"].append(point)
            continue
        if reason == "unmanaged":
            continue
        key = (str(res.get("folder")), reason)
        if key in unresolved_seen:
            continue
        unresolved_seen.add(key)
        if reason == "network":
            # Unchanged from 0.4: a network/sandbox failure passes through.
            warnings.append(
                f"[overleaf-sync-now] could not reach Overleaf to resolve {res.get('folder')} "
                f"({res.get('detail')}); local copy may be stale."
            )
            continue
        msg = _hook_unresolved_message(res, tool)
        if is_write:
            blocks.append(msg + f"\n[overleaf-sync-now] {tool} blocked so a possibly-stale "
                                f"local copy is not edited. Retry after fixing the above.")
        else:
            warnings.append(msg + f"\n[overleaf-sync-now] {tool} allowed, but the local copy "
                                  f"may be stale; edits will be blocked until this is fixed.")

    for pid, entry in projects.items():
        folder = entry["folder"]
        targets = entry["paths"]
        before = _latex_snapshot(folder, targets) if is_write else None
        window = WRITE_DEBOUNCE_SECONDS if is_write else DEBOUNCE_SECONDS
        refreshed = False
        status = ""
        if not is_debounced(pid, window=window):
            try:
                status = refresh_project(pid, folder) or ""
                mark_synced(pid)
                refreshed = True
            except RateLimited as e:
                # Transient. Don't block, and don't claim the copy is fresh
                # for writes (a later write must probe again). Reads still
                # share the debounce so a read burst doesn't dig deeper.
                if not is_write:
                    mark_synced(pid)
                warnings.append(f"[overleaf-sync-now] {e} Local file may be slightly stale.")
            except AuthExpired as e:
                # Recoverable user action. Block writes (exit 2: Claude Code
                # and Codex feed stderr to the model so it can ask the user
                # to re-auth instead of editing stale); warn on reads.
                if is_write:
                    blocks.append(
                        f"[overleaf-sync-now] {e}\n[overleaf-sync-now] {tool} blocked to prevent "
                        f"writing over a possibly-stale local copy. After re-auth, retry."
                    )
                else:
                    warnings.append(
                        f"[overleaf-sync-now] {e}\n[overleaf-sync-now] {tool} allowed, but the "
                        f"local copy may be stale; edits stay blocked until re-auth."
                    )
            except Exception as e:
                # Unknown error (network, zip parse, sandbox block, local
                # write failure): don't block; debounce so the next call
                # doesn't retry immediately.
                mark_synced(pid)
                msg = str(e)
                if _is_sandbox_block(e) or "Outbound HTTPS to Overleaf was blocked" in msg:
                    # Permanent until the user/shell config changes. "Retry
                    # after debounce" would be misleading.
                    warnings.append(f"[overleaf-sync-now] {e}")
                elif isinstance(e, OSError) and not _is_network_exception(e):
                    warnings.append(
                        f"[overleaf-sync-now] refresh could not write local files in {folder}: {e}"
                    )
                else:
                    warnings.append(f"[overleaf-sync-now] refresh failed: {e}; will retry after debounce.")
        if not is_write:
            for p in targets:
                rel = _rel_posix(p, folder)
                gone = web_removed_paths(pid, folder) if rel else {}
                if rel in gone:
                    new = gone[rel]
                    warnings.append(
                        f"[overleaf-sync-now] NOTE: {rel} was "
                        f"{'renamed on Overleaf to ' + new if new else 'deleted on Overleaf'}; "
                        f"this local copy is outdated."
                    )
            continue
        # Write guard 1: the refresh just rewrote a file this call is about
        # to modify. The agent's edit was planned against the old bytes, so
        # make it re-read first (a whole-file Write would otherwise discard
        # the web edit the refresh just pulled in).
        if refreshed and before is not None:
            after = _latex_snapshot(folder, targets)
            changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            if targets:
                keys = {os.path.normcase(os.path.abspath(str(p))) for p in targets}
                hit = sorted(changed & keys)
            else:
                hit = sorted(changed)
            if hit:
                shown = ", ".join(_rel_posix(h, folder) or h for h in hit[:5])
                blocks.append(
                    f"[overleaf-sync-now] Refreshed from Overleaf: newer web edits were just "
                    f"pulled into {shown}. Re-read the file(s) before editing; this {tool} "
                    f"was planned against the old content and was blocked so it does not "
                    f"overwrite them."
                )
        # Write guard 1b: a web change is pending for a file the refresh had
        # to leave alone because it was saved locally in the last 30 s (the
        # protect window; the cached version is held). Both sides changed
        # the file, so a write now would overwrite the web edit.
        if refreshed and "protected" in str(status):
            now = time.time()
            if targets:
                recent = []
                for p in targets:
                    try:
                        if now - os.stat(str(p)).st_mtime < 30:
                            recent.append(_rel_posix(p, folder) or str(p))
                    except OSError:
                        pass
            else:
                recent = ["files in this project"]
            if recent:
                blocks.append(
                    f"[overleaf-sync-now] Conflict: {', '.join(recent[:5])} changed on Overleaf "
                    f"while a local save from the last 30 s may not have reached Overleaf yet, "
                    f"so the web version was not pulled. {tool} blocked so it does not overwrite "
                    f"the web edit. Wait about 30 seconds, re-read the file, check that both the "
                    f"web edit and the recent local change are present, then redo the edit. Tell "
                    f"the user if this repeats."
                )
        # Write guard 2: the target was deleted or renamed on the web.
        gone = web_removed_paths(pid, folder)
        for p in targets:
            rel = _rel_posix(p, folder)
            if rel and rel in gone:
                new = gone[rel]
                if new:
                    blocks.append(
                        f"[overleaf-sync-now] {rel} was renamed on Overleaf to {new} (refreshed "
                        f"locally). {tool} blocked: editing the old path would re-create it on "
                        f"Overleaf through Dropbox. Edit {new} instead."
                    )
                else:
                    blocks.append(
                        f"[overleaf-sync-now] {rel} was deleted on Overleaf. {tool} blocked: "
                        f"editing it would re-create it on Overleaf through Dropbox. Ask the "
                        f"user whether to restore it; if so, they can delete the outdated local "
                        f"copy or re-create the file on Overleaf first."
                    )

    for w in warnings:
        print(w, file=sys.stderr)
    if blocks:
        print("\n".join(blocks), file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


# -------- install / uninstall --------

HOME = pathlib.Path.home()
SKILL_TARGETS = {
    "Claude Code": HOME / ".claude" / "skills" / "overleaf",
    # Codex CLI (and other Agent Skills clients) read user skills from
    # ~/.agents/skills. ~/.codex/skills is the deprecated location.
    "Codex CLI": HOME / ".agents" / "skills" / "overleaf",
}
# Copies written by <= 0.4.x. Removed on install (when ours) so Codex does not
# list the skill twice, and on uninstall.
LEGACY_SKILL_TARGETS = {
    "Codex CLI (legacy ~/.codex/skills)": HOME / ".codex" / "skills" / "overleaf",
}
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
CODEX_HOOKS = CODEX_HOME / "hooks.json"


def _cli_path():
    return shutil.which("overleaf-sync-now")


def _claude_hook_handler(existing=None):
    """Exec-form Claude Code hook handler: `command` is spawned directly with
    `args`, no shell. Shell form (`"<path>" hook`) is a parse error under
    PowerShell, which Claude Code uses on Windows without Git Bash, and a hook
    that fails to start does not block the tool call. Exec form also resolves
    a bare `overleaf-sync-now` on PATH when the absolute path is unknown."""
    handler = {
        "type": "command",
        "command": _cli_path() or "overleaf-sync-now",
        "args": ["hook"],
        "timeout": HOOK_TIMEOUT_SECONDS,
    }
    existing = existing or {}
    # Keep a user's deliberately longer timeout and their onFailure choice.
    try:
        if float(existing.get("timeout") or 0) > HOOK_TIMEOUT_SECONDS:
            handler["timeout"] = existing["timeout"]
    except (TypeError, ValueError):
        pass
    if existing.get("onFailure") in ("block", "continue"):
        handler["onFailure"] = existing["onFailure"]
    return handler


def _hook_command():
    """Shell-form command string (Codex `command`; diagnostics)."""
    found = _cli_path()
    if found:
        import shlex
        return f"{shlex.quote(found)} hook"
    return "overleaf-sync-now hook"


def _hook_command_windows():
    """Codex `commandWindows`. Codex runs hook commands through the session
    shell (PowerShell by default on Windows, else cmd.exe /C). A bare,
    unquoted path parses in both; a path with spaces needs PowerShell's call
    operator."""
    found = _cli_path() if os.name == "nt" else None
    if not found:
        return "overleaf-sync-now hook"
    if any(ch.isspace() for ch in found):
        return f'& "{found}" hook'
    return f"{found} hook"


def _is_our_hook(cmd, args=None):
    """Match the hooks we own (CLI form, exec form, earlier python-script
    form), nothing else. Tolerates trailing flags like `... hook --quiet` so
    future user customizations don't accumulate duplicates on every
    reinstall. `cmd` may also be a whole handler dict."""
    if isinstance(cmd, dict):
        handler = cmd
        cmd = handler.get("command") or handler.get("commandWindows") or ""
        args = handler.get("args")
    if not cmd:
        return False
    s = str(cmd)
    if isinstance(args, (list, tuple)):
        s = s + " " + " ".join(str(a) for a in args)
    s = s.strip().lower()
    if "overleaf-sync-now" not in s and "overleaf_sync.py" not in s:
        return False
    # Either ends in `hook` or has ` hook ` as a token somewhere.
    return bool(re.search(r"(^|\s)hook(\s|$)", s))


def _strip_our_hooks(groups):
    """Remove our handlers from a list of PreToolUse matcher groups, dropping
    groups left empty. Returns (cleaned_groups, removed_handlers)."""
    cleaned, removed = [], []
    for entry in groups or []:
        if not isinstance(entry, dict):
            cleaned.append(entry)
            continue
        keep = []
        for h in entry.get("hooks", []) or []:
            if isinstance(h, dict) and _is_our_hook(h):
                removed.append(h)
            else:
                keep.append(h)
        if keep:
            entry = dict(entry)
            entry["hooks"] = keep
            cleaned.append(entry)
    return cleaned, removed


def _is_junction(p):
    try:
        return os.path.isdir(p) and (p.lstat().st_file_attributes & 0x400) != 0
    except (AttributeError, OSError):
        return False


def _is_our_skill_dir(target):
    """True if `target` holds our skill (its SKILL.md mentions
    overleaf-sync-now) or is an empty directory. Another tool's skill that
    happens to use the name `overleaf` (e.g. aloth/overleaf-skill) is not."""
    skill = target / "SKILL.md"
    try:
        if skill.exists():
            return "overleaf-sync-now" in skill.read_text(encoding="utf-8", errors="replace")
        if target.is_dir() and not target.is_symlink():
            return not any(target.iterdir())
    except OSError:
        return False
    return False


def _remove_existing_target(target):
    """Remove our junction/symlink/dir at target. Junctions removed without
    following. Returns True if the path is now free, False if something that
    is not ours was left in place."""
    if not (target.exists() or target.is_symlink()):
        return True
    if not _is_our_skill_dir(target):
        print(
            f"  WARN: {target} holds a different skill (its SKILL.md does not mention "
            f"overleaf-sync-now). Left untouched; remove or rename it yourself if you "
            f"want overleaf-sync-now installed there.",
            file=sys.stderr,
        )
        return False
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
        return False
    return True


def _read_json_config(path, label):
    """Read a JSON config file. Returns (data, ok). On invalid JSON, backs the
    file up and returns (None, False) so the caller never overwrites it."""
    if not path.exists():
        return {}, True
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level is not a JSON object")
        return data, True
    except (json.JSONDecodeError, ValueError) as e:
        backup = path.with_name(f"{path.name}.broken-{int(time.time())}")
        try:
            shutil.copyfile(str(path), str(backup))
        except OSError:
            backup = "(backup failed)"
        print(
            f"  ERROR: {label} is not valid JSON ({e}).\n"
            f"  Backed up to {backup}. Fix the file (or restore the backup) and re-run install.",
            file=sys.stderr,
        )
        return None, False


def _install_codex_hook():
    """Add (or refresh) our PreToolUse entry in ~/.codex/hooks.json.
    Idempotent; backs up the previous file whenever it changes. Returns True
    when the file now holds our hook."""
    data, ok = _read_json_config(CODEX_HOOKS, str(CODEX_HOOKS))
    if not ok:
        return False
    original = json.dumps(data, indent=2, sort_keys=True)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    groups, removed = _strip_our_hooks(hooks.get("PreToolUse"))
    handler = {
        "type": "command",
        "command": _hook_command(),
        "commandWindows": _hook_command_windows(),
        "timeout": HOOK_TIMEOUT_SECONDS,
        "statusMessage": "overleaf-sync-now: checking Overleaf for newer edits",
    }
    for old in removed:
        try:
            if float(old.get("timeout") or 0) > HOOK_TIMEOUT_SECONDS:
                handler["timeout"] = old["timeout"]
        except (TypeError, ValueError):
            pass
    groups.append({"matcher": CODEX_HOOK_MATCHER, "hooks": [handler]})
    hooks["PreToolUse"] = groups
    data["hooks"] = hooks
    if json.dumps(data, indent=2, sort_keys=True) == original:
        return True
    if CODEX_HOOKS.exists():
        backup = CODEX_HOOKS.with_name(f"hooks.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(str(CODEX_HOOKS), str(backup))
        print(f"  - Codex CLI: previous hooks.json backed up to {backup}")
    _atomic_write_text(CODEX_HOOKS, json.dumps(data, indent=2) + "\n")
    return True


def _uninstall_codex_hook():
    if not CODEX_HOOKS.exists():
        return False
    data, ok = _read_json_config(CODEX_HOOKS, str(CODEX_HOOKS))
    if not ok:
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or "PreToolUse" not in hooks:
        return False
    groups, removed = _strip_our_hooks(hooks.get("PreToolUse"))
    if not removed:
        return False
    if groups:
        hooks["PreToolUse"] = groups
    else:
        hooks.pop("PreToolUse", None)
    _atomic_write_text(CODEX_HOOKS, json.dumps(data, indent=2) + "\n")
    return True


def _install_health():
    """Problems with the installed skill copies and hooks, as short strings
    (empty list = healthy). Catches the stale-install case: `uv tool upgrade`
    replaces the CLI but not the SKILL.md copies or hook entries that an
    older `install` wrote, so they must be refreshed with `install`."""
    problems = []
    try:
        packaged = SKILL_MD_SRC.read_bytes()
    except OSError:
        packaged = None
    any_copy = False
    for tool, target in SKILL_TARGETS.items():
        skill = target / "SKILL.md"
        if not skill.exists():
            continue
        any_copy = True
        try:
            if packaged is not None and skill.read_bytes() != packaged:
                if _is_our_skill_dir(target):
                    problems.append(f"{tool}: {skill} differs from this version's SKILL.md (stale copy)")
                else:
                    problems.append(f"{tool}: {skill} is a different skill named `overleaf`")
        except OSError as e:
            problems.append(f"{tool}: cannot read {skill}: {e}")
    if not any_copy:
        problems.append("no SKILL.md copy in ~/.claude/skills/overleaf or ~/.agents/skills/overleaf")
    for tool, target in LEGACY_SKILL_TARGETS.items():
        if (target / "SKILL.md").exists() and _is_our_skill_dir(target):
            problems.append(f"{tool}: old copy at {target} (Codex lists the skill twice)")
    if CLAUDE_SETTINGS.parent.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text()) if CLAUDE_SETTINGS.exists() else {}
        except (OSError, ValueError):
            settings = None
            problems.append(f"Claude Code: {CLAUDE_SETTINGS} is not valid JSON")
        if settings is not None:
            ours = [
                (g.get("matcher") or "", h)
                for g in ((settings.get("hooks") or {}).get("PreToolUse") or [])
                if isinstance(g, dict)
                for h in (g.get("hooks") or [])
                if isinstance(h, dict) and _is_our_hook(h)
            ]
            if not ours:
                problems.append("Claude Code: no overleaf-sync-now PreToolUse hook in ~/.claude/settings.json")
            for matcher, h in ours:
                tools = set(matcher.split("|"))
                missing = [t for t in ("Read", "Edit", "Write", "Bash") if t not in tools]
                if missing:
                    problems.append(f"Claude Code: hook matcher {matcher!r} misses {', '.join(missing)}")
                try:
                    if h.get("timeout") is not None and float(h["timeout"]) < 60:
                        problems.append(
                            f"Claude Code: hook timeout {h['timeout']}s is too short for a first "
                            f"pull (10-30 s); install writes {HOOK_TIMEOUT_SECONDS}s"
                        )
                except (TypeError, ValueError):
                    pass
                if "args" not in h:
                    problems.append("Claude Code: hook is in shell form (fails under PowerShell); install writes exec form")
    if CODEX_HOME.exists():
        try:
            codex = json.loads(CODEX_HOOKS.read_text()) if CODEX_HOOKS.exists() else {}
        except (OSError, ValueError):
            codex = None
            problems.append(f"Codex CLI: {CODEX_HOOKS} is not valid JSON")
        if codex is not None:
            groups = (codex.get("hooks") or {}).get("PreToolUse") or []
            ours = [g.get("matcher") or "" for g in groups if isinstance(g, dict)
                    for h in (g.get("hooks") or []) if isinstance(h, dict) and _is_our_hook(h)]
            if not ours:
                problems.append(f"Codex CLI: no overleaf-sync-now PreToolUse hook in {CODEX_HOOKS}")
            elif not any("apply_patch" in m for m in ours):
                problems.append(f"Codex CLI: hook matcher {ours[0]!r} does not cover apply_patch")
    return problems


def _print_install_health(indent=""):
    problems = _install_health()
    if not problems:
        print(f"{indent}Install:   OK (skill copies match this version; hooks look current)")
        return
    print(f"{indent}Install:   NEEDS ATTENTION — re-run `overleaf-sync-now install`:")
    for prob in problems:
        print(f"{indent}  - {prob}")
    if CODEX_HOME.exists():
        print(f"{indent}  (In Codex, trust a new or changed hook with /hooks afterwards.)")


def cmd_install(args):
    """Set up the skill in available AI tools and add the PreToolUse hooks.

    Everything written here is USER-GLOBAL (~/.claude/, ~/.agents/, ~/.codex/).
    The hooks + skill apply to every project the user works in, in any
    directory. The only per-project file the tool ever writes is the
    `.overleaf-project` marker that auto-link drops into an Overleaf folder.
    """
    print("=== Installing overleaf-sync-now (USER-GLOBAL, applies to all projects) ===\n")

    # 1. Copy SKILL.md into available skills directories.
    if not SKILL_MD_SRC.exists():
        print(f"ERROR: SKILL.md not found at {SKILL_MD_SRC}", file=sys.stderr)
        sys.exit(1)
    codex_present = CODEX_HOME.exists()
    for tool, target in SKILL_TARGETS.items():
        root = target.parent.parent  # ~/.claude or ~/.agents
        if not root.exists() and not (tool == "Codex CLI" and codex_present):
            print(f"  - {tool}: skipped ({root} not found)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _remove_existing_target(target):
            print(f"  - {tool}: skipped (see warning above)")
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(SKILL_MD_SRC), str(target / "SKILL.md"))
        print(f"  - {tool}: SKILL.md installed at {target / 'SKILL.md'}  [user-global]")
    for tool, target in LEGACY_SKILL_TARGETS.items():
        if (target.exists() or target.is_symlink()) and _is_our_skill_dir(target):
            if _remove_existing_target(target):
                print(f"  - {tool}: removed old copy at {target} (Codex now reads ~/.agents/skills)")

    # 2. Add Claude Code PreToolUse hook (atomically — settings.json
    # corruption breaks Claude Code start, so don't half-write it).
    if CLAUDE_SETTINGS.parent.exists():
        settings, ok = _read_json_config(CLAUDE_SETTINGS, "~/.claude/settings.json")
        if not ok:
            sys.exit(1)
        hooks_root = settings.setdefault("hooks", {})
        cleaned, removed = _strip_our_hooks(hooks_root.get("PreToolUse", []))
        handler = _claude_hook_handler(removed[-1] if removed else None)
        cleaned.append({"matcher": CLAUDE_HOOK_MATCHER, "hooks": [handler]})
        hooks_root["PreToolUse"] = cleaned
        _atomic_write_text(CLAUDE_SETTINGS, json.dumps(settings, indent=2))
        print(
            f"  - Claude Code hook: {handler['command']} {' '.join(handler['args'])} "
            f"(matcher {CLAUDE_HOOK_MATCHER}, timeout {handler['timeout']}s)  [user-global]"
        )
    else:
        print("  - Claude Code: skipped (~/.claude/ not found)")

    # 3. Codex CLI PreToolUse hook (~/.codex/hooks.json).
    codex_hook = False
    if codex_present:
        codex_hook = _install_codex_hook()
        if codex_hook:
            print(
                f"  - Codex CLI hook: {_hook_command()} (matcher {CODEX_HOOK_MATCHER}) "
                f"in {CODEX_HOOKS}  [user-global]"
            )
    else:
        print(f"  - Codex CLI: hook skipped ({CODEX_HOME} not found)")

    # 4. Run setup. Always non-interactive in install context: install is
    # the path agents run, and an unexpected prompt would block them mid-flow.
    print()
    cmd_setup([], force_noninteractive=True)

    print("\n=== Install complete (USER-GLOBAL) ===")
    print("Scope: applies to ALL projects, in any directory, for this user.")
    cli_path = _cli_path() or "(not on PATH yet - open a new shell)"
    print(f"CLI on PATH at: {cli_path}")
    print()
    # Check if auth resolved during the setup that just ran. If not, surface
    # the fix up front so the user doesn't have to wait for the first sync.
    if not _validate_cookies(_load_cache()):
        print("AUTH NOT YET CAPTURED. To finish setup, run this in your own terminal:")
        print("    overleaf-sync-now login")
        print("  (A browser opens - log into Overleaf there. One-time; lasts weeks.)")
        print("  Until then the hook blocks AI edits of Overleaf files, so nothing is")
        print("  written over a possibly-stale copy.")
        print()
    print("Restart Claude Code (or Codex) for the skill and hooks to load.")
    if codex_hook:
        print("Codex CLI: new or changed hooks only run after you trust them. In Codex,")
        print("  run /hooks, review the overleaf-sync-now PreToolUse hook and trust it.")
    print("After restart, AI reads/edits of .tex/.bib/.cls/.sty/.bst files under")
    print("<Dropbox>/Apps/Overleaf/<project>/ refresh from Overleaf first, in every project.")
    print()
    print("Manual sync any time, from any directory:  overleaf-sync-now sync .")


def _import_sync_playwright():
    """Return (sync_playwright_callable, kind). Prefers patchright when installed.

    patchright is a maintained Playwright fork that patches the CDP
    Runtime.Enable leak and command-flag fingerprints Google's anti-automation
    gate detects (the "This browser or app may not be secure" page). It exposes
    the same `sync_playwright` API, so it's a drop-in. Vanilla playwright is
    the fallback for environments where the patchright wheel isn't available.
    """
    try:
        from patchright.sync_api import sync_playwright as patchright_sync  # type: ignore
        return patchright_sync, "patchright"
    except ImportError:
        pass
    try:
        from playwright.sync_api import sync_playwright as playwright_sync  # type: ignore
        return playwright_sync, "playwright"
    except ImportError:
        return None, None


def _build_launch_kwargs(kind, profile_dir, *, headless, channel):
    """kwargs for `chromium.launch_persistent_context`, tuned per backend.

    patchright applies CDP-level stealth patches internally; passing our own
    ignore_default_args would interfere. For vanilla playwright we strip
    `--enable-automation` and disable AutomationControlled as a best-effort
    baseline — it won't defeat every Google detector (the Runtime.Enable leak
    remains) but it reduces the obvious signals.
    """
    kwargs = {"user_data_dir": str(profile_dir), "headless": headless}
    if channel:
        kwargs["channel"] = channel
    if kind == "playwright":
        kwargs["ignore_default_args"] = ["--enable-automation"]
        kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
    return kwargs


def _pkg_version():
    from . import __version__
    return __version__


def _ensure_playwright_browser():
    """Ensure Chromium is downloaded for whichever backend the adapter prefers.
    Idempotent. ~150MB on first run.

    Both patchright and playwright are pyproject.toml deps, so import is
    guaranteed; only the browser binary is lazy-downloaded. We run the
    `install chromium` step for the preferred backend only — no need to
    download two Chromiums.
    """
    _, kind = _import_sync_playwright()
    if not kind:
        # Can only happen if user did `uv tool install --no-deps` or similar.
        print(
            "[overleaf-sync-now] Neither patchright nor playwright is installed.\n"
            "  Reinstall the tool:\n"
            f"  uv tool install --reinstall --from git+https://github.com/hanlulong/overleaf-sync-now@v{_pkg_version()} overleaf-sync-now",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"[overleaf-sync-now] Ensuring Chromium is available via {kind} (one-time, ~150MB on first run)...",
        file=sys.stderr,
    )
    try:
        subprocess.run(
            [sys.executable, "-m", kind, "install", "chromium"],
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
    sync_playwright, kind = _import_sync_playwright()
    if sync_playwright is None:
        print(
            "ERROR: Neither patchright nor playwright Python package is importable.\n"
            "Reinstall the tool.",
            file=sys.stderr,
        )
        sys.exit(1)

    profile_dir = CACHE_DIR / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # This profile will hold a live, weeks-long Overleaf session. Make both the
    # data dir and the profile dir user-private so it isn't reachable by other
    # local users (Chromium won't do this for us).
    _harden_dir(CACHE_DIR)
    _harden_dir(profile_dir)

    print()
    print(f"Opening a browser window (backend: {kind}). Log into https://www.overleaf.com when it appears.")
    print(f"(Login persists in {profile_dir}; you only do this once per several weeks.)")
    print()
    print("Tip: if your Overleaf account uses 'Sign in with Google' and Google blocks")
    print("the sign-in here, set an Overleaf-specific password at")
    print("  https://www.overleaf.com/user/password/reset")
    print("and use email+password in this window. Google never sees that flow.")
    print()

    with sync_playwright() as p:
        ctx = None
        # Prefer system Chrome (no download); fall back to bundled Chromium.
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = _build_launch_kwargs(kind, profile_dir, headless=False, channel=channel)
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
        google_blocked = False
        last_status = 0
        while time.time() < deadline:
            try:
                # Detect Google's `disallowed_useragent` block page so we can
                # surface a targeted recovery message instead of timing out
                # silently. URLs to watch: `/signin/rejected`,
                # `/disallowed_useragent`.
                try:
                    current_url = (page.url or "").lower()
                    if "signin/rejected" in current_url or "disallowed_useragent" in current_url:
                        google_blocked = True
                        break
                except Exception:
                    pass

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

    if google_blocked:
        print()
        print("ERROR: Google blocked the sign-in with 'This browser or app may not be secure.'", file=sys.stderr)
        print(file=sys.stderr)
        print("This is Google's anti-automation gate, not an Overleaf issue. It fires against any", file=sys.stderr)
        print("browser launched under remote-control protocols. The fix is to bypass Google entirely.", file=sys.stderr)
        print(file=sys.stderr)
        print("If your Overleaf account uses 'Sign in with Google':", file=sys.stderr)
        print(file=sys.stderr)
        print("  1. Visit  https://www.overleaf.com/user/password/reset", file=sys.stderr)
        print("  2. Enter your account email. Overleaf will email you a password-set link.", file=sys.stderr)
        print("  3. Set an Overleaf password. (Your Google sign-in still works in your normal", file=sys.stderr)
        print("     browser; this just adds an alternate email+password login path.)", file=sys.stderr)
        print("  4. Re-run `overleaf-sync-now login` and use email+password on the Overleaf form.", file=sys.stderr)
        print("     Google never sees this flow and never blocks it.", file=sys.stderr)
        print(file=sys.stderr)
        print("Fallback if you can't change the account: copy the `overleaf_session2` cookie", file=sys.stderr)
        print("from your normal browser's DevTools, then run this in your terminal and paste it", file=sys.stderr)
        print("at the hidden prompt (never into an AI chat):", file=sys.stderr)
        print("    overleaf-sync-now save-cookie", file=sys.stderr)
        sys.exit(1)

    if not captured:
        print("\nERROR: did not capture a valid Overleaf session cookie within 5 minutes.", file=sys.stderr)
        print("If you did log in but it wasn't detected, run `overleaf-sync-now save-cookie` in this", file=sys.stderr)
        print("terminal and paste your overleaf_session2 value (DevTools -> Application -> Cookies)", file=sys.stderr)
        print("at the hidden prompt. Never paste it into an AI chat.", file=sys.stderr)
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
    sync_playwright, kind = _import_sync_playwright()
    if sync_playwright is None:
        return None
    try:
        with sync_playwright() as p:
            kwargs = _build_launch_kwargs(kind, profile_dir, headless=True, channel=None)
            ctx = p.chromium.launch_persistent_context(**kwargs)
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


_SAVE_COOKIE_USAGE = (
    "Usage (run it yourself in a terminal; never via an AI agent or chat):\n"
    "  overleaf-sync-now save-cookie        prompt for the value (input hidden)\n"
    "  overleaf-sync-now save-cookie -      read the value from stdin\n"
    f"Get the value from your browser: open https://www.overleaf.com (logged in), "
    f"press F12 -> Application -> Cookies -> https://www.overleaf.com -> find "
    f"'{SESSION_COOKIE}' -> copy Value.\n"
    "Prefer `overleaf-sync-now login` whenever a browser can open on this machine."
)


def cmd_save_cookie(args):
    """Save an overleaf_session2 cookie value to the cache. Headless fallback
    for machines where `login` cannot open a browser.

    The user runs this in their own terminal. The value is read with getpass
    (no argument, interactive terminal) or from stdin (`-`, or no argument
    with piped input) so it never lands in shell history, the process list,
    or an agent transcript. A value passed as an argument still works for
    backward compatibility, with a warning.
    """
    if not args or args[0] == "-":
        from_stdin = bool(args) or not sys.stdin.isatty()
        try:
            if from_stdin:
                value = sys.stdin.read()
            else:
                import getpass
                value = getpass.getpass(f"Paste the {SESSION_COOKIE} value (input hidden): ")
        except (EOFError, KeyboardInterrupt):
            value = ""
        value = (value or "").strip()
        if not value:
            print(_SAVE_COOKIE_USAGE, file=sys.stderr)
            sys.exit(2)
    else:
        print(
            "WARNING: a cookie passed as a command-line argument is visible in the process "
            "list and your shell history (and in the transcript, if an AI agent ran this). "
            "Next time run `overleaf-sync-now save-cookie` with no argument instead.",
            file=sys.stderr,
        )
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
        for fn_name in CHROMIUM_COOKIE_FNS:
            fn = getattr(rookiepy, fn_name, None)
            if not fn:
                continue
            try:
                raw = fn(["overleaf.com", ".overleaf.com", "www.overleaf.com"])
                cookies = {c["name"]: c["value"] for c in raw}
                if SESSION_COOKIE in cookies:
                    print(f"     {fn_name:10s}: {SESSION_COOKIE} present")
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
        for fn_name in CHROMIUM_COOKIE_FNS:
            fn = getattr(browser_cookie3, fn_name, None)
            if not fn:
                print(f"    {fn_name:10s}: function not exported")
                continue
            try:
                cj = fn(domain_name="overleaf.com")
                cookies = {c.name: c.value for c in cj}
                if SESSION_COOKIE in cookies:
                    print(f"    {fn_name:10s}: {SESSION_COOKIE} present")
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
        print(_AUTH_RECOVERY)
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
    print()
    print("[6] Install health:")
    _print_install_health(indent="    ")


def cmd_uninstall(args):
    print("=== Uninstalling overleaf-sync-now skill and hooks ===")
    for tool, target in list(SKILL_TARGETS.items()) + list(LEGACY_SKILL_TARGETS.items()):
        if target.exists() or target.is_symlink():
            if _remove_existing_target(target):
                print(f"  - {tool}: removed {target}")
    if CLAUDE_SETTINGS.exists():
        settings, ok = _read_json_config(CLAUDE_SETTINGS, "~/.claude/settings.json")
        if not ok:
            print("  WARN: skipping Claude Code hook removal.", file=sys.stderr)
        else:
            hooks_root = settings.get("hooks")
            if isinstance(hooks_root, dict) and "PreToolUse" in hooks_root:
                cleaned, removed = _strip_our_hooks(hooks_root.get("PreToolUse", []))
                hooks_root["PreToolUse"] = cleaned
                _atomic_write_text(CLAUDE_SETTINGS, json.dumps(settings, indent=2))
                if removed:
                    print("  - Claude Code hook removed")
    if _uninstall_codex_hook():
        print(f"  - Codex CLI hook removed from {CODEX_HOOKS}")
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
