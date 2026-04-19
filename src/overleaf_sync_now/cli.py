"""overleaf-sync-now CLI.

Subcommands:
  install                       Link skill into Claude Code/Codex + add hook + run setup
  login                         Open a controlled browser to log into Overleaf and capture
                                the cookie via DevTools Protocol (the proper fix for Chrome
                                130+ app-bound encryption). One-time, persists for weeks.
  setup                         Auth setup (auto-detect from existing browsers/profiles)
  save-cookie <value>           Persist a manually-pasted overleaf_session2 cookie value
  link <project_id> [folder]    Mark a folder as belonging to an Overleaf project (override)
  sync [folder]                 Trigger sync for the linked folder (or current dir)
  status [folder]               Show data dir, cookie validity, and link/sync state
  projects [--refresh]          List your Overleaf projects (name + ID)
  doctor                        Verbose diagnostic dump of the auth chain
  hook                          PreToolUse hook entrypoint (reads JSON from stdin)
  uninstall                     Remove skill links and hook (cookies preserved)

Auth chain (first hit wins): cached cookies, regular browser via browser_cookie3,
Claude Code Playwright profile, interactive paste prompt.

Project-folder mapping: auto-discovered when a file lives under
  <anywhere>/Apps/Overleaf/<project>/.
Override: drop a `.overleaf-project` JSON file with the project ID in any folder.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time


def _data_dir():
    """Universal location for runtime data; legacy path honored for upgraders."""
    env = os.environ.get("OVERLEAF_SYNC_DATA_DIR")
    if env:
        return pathlib.Path(env)
    legacy = pathlib.Path.home() / ".claude" / "overleaf-data"
    if legacy.exists():
        return legacy
    return pathlib.Path.home() / ".overleaf-sync"


CACHE_DIR = _data_dir()
CACHE_FILE = CACHE_DIR / "cookies.json"
STATE_FILE = CACHE_DIR / "state.json"
INDEX_FILE = CACHE_DIR / "projects.json"
INDEX_TTL = 86400
PLAYWRIGHT_PROFILE = pathlib.Path.home() / ".claude" / "playwright-profile"
PROJECT_MARKER = ".overleaf-project"
DEBOUNCE_SECONDS = 30
HOOK_WAIT_SECONDS = 3
MANUAL_WAIT_SECONDS = 10
BASE = "https://www.overleaf.com"
SESSION_COOKIE = "overleaf_session2"

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
SKILL_MD_SRC = PACKAGE_DIR / "SKILL.md"


# -------- auth chain --------

def _atomic_write_text(path, text):
    """Write `text` to `path` atomically: write to a sibling tempfile, then
    os.replace. Avoids leaving the destination in a half-written state if the
    process is killed (which would corrupt e.g. ~/.claude/settings.json)."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(str(tmp), str(path))


def _save_cache(cookies):
    _atomic_write_text(CACHE_FILE, json.dumps(cookies))
    if os.name == "posix":
        try:
            os.chmod(CACHE_FILE, 0o600)
        except OSError:
            pass
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


def fetch_csrf(s, project_id):
    r = s.get(f"{BASE}/project/{project_id}", allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(f"Project page returned HTTP {r.status_code}")
    # Allow other attributes (data-type, etc.) between `name` and `content`.
    m = re.search(r'name="ol-csrfToken"[^>]*\scontent="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("CSRF token not found in project page")
    return m.group(1)


class AuthExpired(RuntimeError):
    """Cookies invalid or expired; user needs to re-auth."""


class RateLimited(RuntimeError):
    """Overleaf returned 429. Pass `retry_after` (seconds) when known."""
    def __init__(self, retry_after=60):
        self.retry_after = retry_after
        super().__init__(f"Overleaf rate-limited; wait ~{retry_after}s and retry.")


def trigger_sync(project_id):
    import requests
    s = get_session()
    try:
        csrf = fetch_csrf(s, project_id)
    except requests.exceptions.RequestException as e:
        # Network-layer failure (DNS, timeout, connection reset). Distinct from
        # auth — caller may want to back off and retry rather than re-auth.
        raise RuntimeError(f"Network error reaching Overleaf: {e}") from e
    except RuntimeError:
        s = get_session(force_refresh=True)
        try:
            csrf = fetch_csrf(s, project_id)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error reaching Overleaf: {e}") from e
        except RuntimeError as e:
            raise AuthExpired(
                "Overleaf cookies invalid. Re-auth by opening overleaf.com in your "
                "browser (auto-detect picks it up next time), or run "
                "`overleaf-sync-now login` for the browser-assisted flow."
            ) from e
    try:
        r = s.post(
            f"{BASE}/project/{project_id}/dropbox/sync-now",
            headers={"x-csrf-token": csrf, "Content-Type": "application/json"},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error during sync-now POST: {e}") from e
    if r.status_code == 429:
        # Retry-After can be either delta-seconds OR an HTTP-date per RFC 7231.
        # We only handle the integer form; fall back to 60s otherwise.
        try:
            retry_after = int(r.headers.get("Retry-After", "60") or "60")
        except (TypeError, ValueError):
            retry_after = 60
        raise RateLimited(retry_after)
    if r.status_code in (401, 403):
        raise AuthExpired(
            f"Overleaf rejected the request (HTTP {r.status_code}). "
            "Cookies likely expired; re-auth via your browser or `overleaf-sync-now login`."
        )
    if r.status_code == 404:
        raise RuntimeError(
            f"Project {project_id} not found (HTTP 404). It may have been deleted, "
            f"or the auto-link found the wrong project. Run `overleaf-sync-now status` "
            f"to verify, or re-link with the correct ID."
        )
    if r.status_code >= 500:
        raise RuntimeError(
            f"Overleaf server error (HTTP {r.status_code}). Try again in a minute."
        )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"sync-now returned HTTP {r.status_code}: {r.text!r}")


# -------- project lookup --------

def find_linked_folder(start):
    p = pathlib.Path(start).resolve()
    if p.is_file():
        p = p.parent
    cur = p
    while True:
        marker = cur / PROJECT_MARKER
        if marker.exists():
            try:
                with open(marker) as f:
                    return cur, json.load(f).get("project_id")
            except Exception:
                pass
        if cur.parent == cur:
            break
        cur = cur.parent
    parts = p.parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "overleaf" and i > 0 and parts[i - 1].lower() == "apps":
            if i + 1 < len(parts):
                name = parts[i + 1]
                folder = pathlib.Path(*parts[: i + 2])
                pid = lookup_project_id(name)
                if pid:
                    return folder, pid
    return None, None


def lookup_project_id(name):
    index = _load_index()
    if name in index:
        return index[name]
    index = _refresh_index()
    return index.get(name)


def _load_index():
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE) as f:
                cached = json.load(f)
            if time.time() - cached.get("ts", 0) < INDEX_TTL:
                return cached.get("index", {})
        except Exception:
            pass
    return _refresh_index()


def _refresh_index():
    s = get_session()
    r = s.get(f"{BASE}/project")
    if r.status_code != 200:
        s = get_session(force_refresh=True)
        r = s.get(f"{BASE}/project")
    # Don't poison the cache with an empty/garbage index when Overleaf is
    # unhappy. Return whatever we have without writing.
    if r.status_code != 200:
        return _load_index_raw()
    m = re.search(r'name="ol-prefetchedProjectsBlob"[^>]*\scontent="([^"]+)"', r.text)
    index = {}
    if m:
        import html
        try:
            blob = json.loads(html.unescape(m.group(1)))
            for proj in blob.get("projects", []):
                if proj.get("id") and proj.get("name"):
                    index[proj["name"]] = proj["id"]
        except (ValueError, KeyError, TypeError):
            pass
    if not index:
        # Fallback: only match anchors whose href is the project root, not
        # /project/<id>/clone, /project/<id>/download, etc., which would map
        # the wrong link text (e.g. "Download") to the project ID.
        for pid, name in re.findall(
            r'/project/([0-9a-f]{24})"[^>]*>\s*([^<\n][^<]*?)\s*<', r.text
        ):
            index.setdefault(name.strip(), pid)
    if not index:
        # Don't overwrite a previously-good cached index with an empty one.
        return _load_index_raw()
    _atomic_write_text(INDEX_FILE, json.dumps({"ts": time.time(), "index": index}, indent=2))
    return index


def _load_index_raw():
    """Return the cached index dict (possibly empty/stale). Used as a fallback
    when refresh fails so we don't lose a previously-good index."""
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE) as f:
                return json.load(f).get("index", {}) or {}
        except Exception:
            return {}
    return {}


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
    with open(folder / PROJECT_MARKER, "w") as f:
        json.dump({"project_id": project_id}, f, indent=2)
    print(f"Linked {folder} -> Overleaf project {project_id}")


def cmd_sync(args):
    no_wait = "--no-wait" in args
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
    print(f"Triggering Overleaf sync for project {project_id} (folder: {linked})")
    t0 = time.time()
    try:
        trigger_sync(project_id)
    except RateLimited as e:
        wait = min(e.retry_after, 120)  # cap so we don't sleep forever
        print(f"Rate-limited by Overleaf. Waiting {wait}s and retrying once...")
        time.sleep(wait)
        try:
            trigger_sync(project_id)
        except RateLimited as e2:
            print(f"ERROR: still rate-limited after retry. Wait ~{e2.retry_after}s and try again.", file=sys.stderr)
            sys.exit(1)
    except AuthExpired as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if no_wait:
        print(f"Sync triggered ({time.time() - t0:.2f}s). Skipping settle wait (--no-wait).")
    else:
        print(f"Sync triggered ({time.time() - t0:.2f}s). Waiting {MANUAL_WAIT_SECONDS}s for Dropbox to settle "
              f"(use --no-wait to skip)...")
        time.sleep(MANUAL_WAIT_SECONDS)
    mark_synced(project_id)
    print("Done.")


def cmd_projects(args):
    """List the user's Overleaf projects (name, ID, last folder match if any).

    Useful when:
      - You want to know your project IDs without opening the web UI.
      - Auto-link is failing and you need to confirm which Overleaf project
        name corresponds to a local folder.
    """
    refresh = "--refresh" in args
    if refresh and INDEX_FILE.exists():
        try:
            INDEX_FILE.unlink()
        except OSError:
            pass
    try:
        index = _load_index()
    except RuntimeError as e:
        print(f"ERROR: could not fetch project list: {e}", file=sys.stderr)
        sys.exit(1)
    if not index:
        print("(No projects found. Run with --refresh to force a re-fetch from Overleaf.)")
        return
    name_w = max(len(n) for n in index)
    print(f"{'NAME'.ljust(name_w)}  PROJECT_ID")
    print(f"{'-' * name_w}  {'-' * 24}")
    for name in sorted(index):
        print(f"{name.ljust(name_w)}  {index[name]}")
    print(f"\n{len(index)} project(s). Cached at {INDEX_FILE}.")


def cmd_status(args):
    """Status reports whether sync would actually succeed right now, by walking
    the same auth chain sync itself uses. Use --quick to skip the slow chain
    fallback and only check the cached cookie."""
    quick = "--quick" in args
    args = [a for a in args if not a.startswith("--")]
    folder = pathlib.Path(args[0] if args else ".")
    linked, project_id = find_linked_folder(folder)
    print(f"Data dir:    {CACHE_DIR}")
    print(f"Cookie file: {CACHE_FILE} ({'present' if CACHE_FILE.exists() else 'MISSING'})")
    cached = _load_cache()
    # Force a real network probe — status is a diagnostic, not a hot path.
    cache_ok = _validate_cookies(cached, use_cache=False)
    if cache_ok:
        print("Cookie auth: OK (cache valid, sync would succeed)")
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
    print(f"Project:   {project_id}")
    last = _state().get(project_id)
    if last:
        ago = time.time() - last
        print(f"Last sync: {ago:.0f}s ago (debounce: {DEBOUNCE_SECONDS}s)")
    else:
        print("Last sync: never")


def cmd_hook(args):
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        # Don't block the edit, but surface the schema mismatch so future
        # Claude Code hook-payload changes don't make us silently no-op.
        print(f"[overleaf-sync-now] hook stdin not valid JSON ({e}); skipping.", file=sys.stderr)
        sys.exit(0)
    # Only the file-mutating tools need a sync. (Read used to be in this list
    # but adding it would mean a network round-trip on every file Claude Code
    # peeks at — far too aggressive.)
    if data.get("tool_name", "") not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)
    fp = data.get("tool_input", {}).get("file_path", "")
    if not fp or not re.search(r"\.(tex|bib|cls|sty|bst)$", fp, re.IGNORECASE):
        sys.exit(0)
    # find_linked_folder can in principle raise (PermissionError on a network
    # share, OSError reading a marker, etc.). Don't let those crash the hook
    # and block the user's edit.
    try:
        linked, project_id = find_linked_folder(fp)
    except Exception as e:
        print(f"[overleaf-sync-now] hook: could not resolve project for {fp}: {e}; skipping.", file=sys.stderr)
        sys.exit(0)
    if not project_id or is_debounced(project_id):
        sys.exit(0)
    try:
        trigger_sync(project_id)
        time.sleep(HOOK_WAIT_SECONDS)
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
        # Unknown error (network, etc.): don't block the edit, warn loudly,
        # and mark synced so the NEXT edit doesn't immediately retry the same
        # failing call within the debounce window. User can rerun manually
        # via `overleaf-sync-now sync` for an explicit retry.
        mark_synced(project_id)
        print(f"[overleaf-sync-now] sync failed: {e}; will retry after debounce.", file=sys.stderr)
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
    """Match the hooks we own (CLI form + legacy path form), nothing else.
    Tolerates trailing flags like `... hook --quiet` so future user
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
            "matcher": "Edit|Write|MultiEdit",
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
