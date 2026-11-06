"""overleaf-sync-now CLI.

Subcommands:
  install                       Link skill into Claude Code/Codex + add hook + run setup
  login                         Open a controlled browser to log into Overleaf and capture
                                the cookie via DevTools Protocol (the proper fix for Chrome
                                127+ app-bound encryption). One-time, persists for weeks.
  setup                         Auth setup (auto-detect from existing browsers/profiles)
  save-cookie [-]               Save an overleaf_session2 cookie typed at a hidden prompt
                                (or piped on stdin with `-`). Headless fallback only; the
                                user runs it in their own terminal, never via an agent.
  link <project_id> [folder]    Mark a folder as belonging to an Overleaf project (override)
  sync [folder] [--force] [--wait]
                                Refresh the linked folder against Overleaf. Probes
                                /project/<id>/updates and downloads the zip only when
                                a web-origin change has actually happened. --force always
                                re-extracts. --wait waits (up to 2 min) for files saved
                                locally in the last 30 s to age out, then pulls them.
  status [folder]               Show data dir, cookie validity, and link/sync state
  projects [--refresh]          List your Overleaf projects (name + ID)
  doctor [folder]               Verbose diagnostic dump of the auth chain
  hook                          PreToolUse hook entrypoint for Claude Code and Codex CLI
                                (reads the tool-call JSON from stdin)
  uninstall [--remove-markers] [FOLDER...]
                                Remove skill copies and hooks, then list what is left
                                (markers, cached session, backups, browser download)
                                and how to remove it

Auth chain (first hit wins): cached cookies -> persistent `login` browser
profile (the proper Chrome 127+ Windows fix) -> rookiepy (Rust cookie reader,
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
# Opt-out for a folder under Apps/Overleaf/ that is not an Overleaf project:
# an (empty) file with this name in it or in any parent, or the folder listed
# in $OVERLEAF_SYNC_IGNORE (paths separated by os.pathsep). The hook and sync
# then leave everything below it alone.
IGNORE_MARKER = ".overleaf-sync-ignore"
IGNORE_ENV = "OVERLEAF_SYNC_IGNORE"
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
# Browsers the libraries' all-browsers `load()` adds; tried once each instead.
EXTRA_COOKIE_FNS = ("opera_gx", "arc", "safari")
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


def _atomic_write_text(path, text, *, mode=None):
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
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
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


def _check_cookies(cookies, *, use_cache=True):
    """Check cookies against Overleaf. Returns (state, exc):

      "ok"           Overleaf accepted the session
      "missing"      no overleaf_session2 cookie to check (no request made)
      "rejected"     Overleaf answered and refused it (401/403, or a redirect
                     to the login page)
      "unreachable"  no usable answer: offline, DNS or proxy failure, a
                     sandbox that blocks the network, a timeout, or an
                     Overleaf outage (429/5xx). Says nothing about the cookie.

    `exc` is the exception (or a RuntimeError naming the HTTP status) for
    "unreachable", else None. Set use_cache=False to force a fresh network
    probe (doctor, status, save-cookie)."""
    if not cookies or SESSION_COOKIE not in cookies:
        return "missing", None
    # Hot path: cookie shape is right and we just validated successfully —
    # skip the network round-trip. Keeps the hook fast on every AI edit.
    if use_cache and _cookies_recently_validated():
        return "ok", None
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".overleaf.com")
    try:
        r = s.get(f"{BASE}/project", allow_redirects=False, timeout=10)
    except Exception as e:  # ConnectionError / Timeout / ProxyError / SSLError ...
        return "unreachable", e
    if r.status_code == 200:
        _mark_cookies_validated()
        return "ok", None
    if r.status_code == 429 or r.status_code >= 500:
        return "unreachable", RuntimeError(f"Overleaf answered HTTP {r.status_code}")
    return "rejected", None


def _validate_cookies(cookies, *, use_cache=True):
    """True only when Overleaf accepted the cookies (see _check_cookies)."""
    return _check_cookies(cookies, use_cache=use_cache)[0] == "ok"


def _try_rookiepy():
    """rookiepy is a Rust-backed cookie reader that handles Chrome 127+
    app-bound encryption better than browser_cookie3 on Windows."""
    try:
        import rookiepy
    except ImportError:
        return None
    for fn_name in (*CHROMIUM_COOKIE_FNS, *EXTRA_COOKIE_FNS):
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
    # One pass per browser. browser_cookie3.load() would repeat every browser
    # above, and on macOS each Chromium-family lookup asks the Keychain for
    # "<Browser> Safe Storage" (a password dialog per installed browser).
    for fn_name in (*CHROMIUM_COOKIE_FNS, *EXTRA_COOKIE_FNS):
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


# Reading cookies from installed browsers' on-disk stores (rookiepy,
# browser_cookie3, the Claude Code Playwright profile) asks the macOS Keychain
# for each Chromium browser's "Safe Storage" key, which can raise a password
# dialog per browser. That is acceptable when the user runs setup / install /
# status / doctor, but not from the PreToolUse hook (it would fire on every
# unauthenticated AI read or edit), so the hook turns it off. Other callers
# (sync, run by agents too) back off for an hour after the chain failed.
_BROWSER_EXTRACTION = {"allowed": True}
_BROWSER_COOLDOWN_SECONDS = 3600


def _browser_cooldown_file():
    return pathlib.Path(CACHE_DIR) / ".browser-cookies-failed-at"


def _browser_extraction_in_cooldown():
    try:
        with open(_browser_cooldown_file()) as f:
            return (time.time() - float(f.read().strip())) < _BROWSER_COOLDOWN_SECONDS
    except Exception:
        return False


def _mark_browser_extraction_failed():
    try:
        _atomic_write_text(_browser_cooldown_file(), str(time.time()))
    except OSError:
        pass


def _resolve_cookies_with_source(interactive=False, *, browsers=None):
    """Walk the auth chain. Returns (cookies, source_label) or (None, None).

    browsers: True tries the installed-browser extractors (setup, status,
    doctor), False skips them (the hook), None decides automatically: on
    unless the hook is running or they failed within the last hour.

    Raises NetworkUnavailable when Overleaf cannot be reached: a cookie that
    can't be checked is not an expired cookie, and walking on to the browser
    extractors would only add OS keychain prompts for nothing.

    `setup` reports the label so the user knows which source won; every
    other caller goes through `_resolve_cookies`."""
    cached = _load_cache()
    state, exc = _check_cookies(cached)
    if state == "ok":
        return cached, "cached cookie"
    if state == "unreachable":
        raise NetworkUnavailable(_network_unavailable_message(exc))
    # Persistent browser profile from `login` command (the proper fix for
    # Chrome 127+ app-bound encryption). Browser API can read HttpOnly cookies
    # that no on-disk extractor can decrypt without admin.
    if browsers is None:
        browsers = _BROWSER_EXTRACTION["allowed"] and not _browser_extraction_in_cooldown()
    chain = [("`login` browser profile", _try_login_profile)]
    if browsers:
        chain += [
            # rookiepy: Chrome 127+ friendly via Rust
            ("rookiepy (installed browsers)", _try_rookiepy),
            ("browser_cookie3 (installed browsers)", _try_browser_cookie3),
            ("Claude Code Playwright profile", _try_playwright_profile),
        ]
    for label, fn in chain:
        found = fn()
        state, exc = _check_cookies(found)
        if state == "ok":
            _save_cache(found)
            return found, label
        if state == "unreachable":
            raise NetworkUnavailable(_network_unavailable_message(exc))
    if interactive:
        manual = _prompt_manual_cookie()
        state, exc = _check_cookies(manual)
        if state == "ok":
            _save_cache(manual)
            return manual, "hidden terminal prompt"
        if state == "unreachable":
            raise NetworkUnavailable(_network_unavailable_message(exc))
    if browsers:
        _mark_browser_extraction_failed()
    return None, None


def _resolve_cookies(interactive=False, **kw):
    return _resolve_cookies_with_source(interactive=interactive, **kw)[0]


# Where to find the cookie in a browser, for the user (not the agent). macOS
# has no F12 key by default, Safari hides its developer tools, and Firefox
# and Safari call the pane Storage rather than Application.
_DEVTOOLS_STEPS = (
    "open https://www.overleaf.com (logged in), then the browser's developer tools:\n"
    "       Chrome / Edge / Firefox: F12 or Ctrl+Shift+I (Windows, Linux), Cmd+Option+I (macOS)\n"
    "       Safari: Settings > Advanced > \"Show features for web developers\", then Cmd+Option+I\n"
    "     Go to Application > Cookies (Chrome, Edge) or Storage > Cookies (Firefox, Safari)\n"
    "     > https://www.overleaf.com, find the row named 'overleaf_session2' and copy its Value."
)


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
    print(f"  1. In any browser, {_DEVTOOLS_STEPS}")
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
    state, exc = _check_cookies(cookies)
    if state == "unreachable":
        # Offline, or a sandbox that blocks the network. The cookie may be
        # fine, so this must never become AuthExpired (which makes the hook
        # block and steers the agent toward re-auth), and the browser
        # extractors are skipped (they would only raise keychain prompts).
        raise NetworkUnavailable(_network_unavailable_message(exc))
    if state != "ok":
        # May itself raise NetworkUnavailable if a candidate can't be checked.
        cookies = _resolve_cookies(interactive=False)
    if not cookies:
        # Genuine missing/expired-cookie case. Raise AuthExpired, NOT a plain
        # RuntimeError: the PreToolUse hook blocks the edit (exit 2) on
        # AuthExpired so the AI re-auths instead of silently editing a
        # possibly-stale local copy — the precise data-loss this tool exists
        # to prevent.
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
    "     A browser window opens; they log into Overleaf there (works on Chrome 127+).\n"
    "     Do not suggest logging into Overleaf in their everyday browser instead:\n"
    "     Chrome 127+ app-bound encryption blocks reading cookies from it.\n"
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


class NetworkUnavailable(RuntimeError):
    """Overleaf could not be reached: offline, DNS or proxy failure, an
    Overleaf outage, or a sandbox that blocks the network (Codex CLI's
    default). Nothing is known about the cookie. The hook lets the call
    through with a note; it never blocks or asks for re-auth on this."""


def _exc_chain(exc):
    """`exc` and everything it was raised from (__cause__ / __context__, and
    urllib3's MaxRetryError.reason), outermost first."""
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        yield e
        nxt = e.__cause__ or e.__context__
        if nxt is None:
            reason = getattr(e, "reason", None)
            nxt = reason if isinstance(reason, BaseException) else None
        e = nxt


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
    for e in _exc_chain(exc):
        if isinstance(e, NetworkUnavailable):
            return True
        if req_exc is not None and isinstance(e, req_exc):
            return True
        mod = type(e).__module__ or ""
        if mod.startswith(("urllib3", "requests", "socket")):
            return True
    return False


def _is_name_resolution_error(exc):
    import socket
    return any(
        isinstance(e, socket.gaierror) or type(e).__name__ == "NameResolutionError"
        for e in _exc_chain(exc)
    )


def _is_sandbox_block(exc):
    """True if `exc` looks like the host blocking outbound connections (Codex
    CLI's sandbox, a firewall policy) rather than a plain network outage.
    Retrying doesn't help there, and setup/login/doctor hit the same block.

    Codex CLI sets CODEX_SANDBOX_NETWORK_DISABLED=1 inside a sandbox without
    network access. In its macOS Seatbelt sandbox, DNS fails
    (NameResolutionError, [Errno 8]) and a connect to a literal IP fails with
    [Errno 1] Operation not permitted (EPERM); on Windows a blocked socket is
    WinError 10013.

    Only network exceptions qualify: a local PermissionError (read-only
    Dropbox folder, file locked by another process) shares the "permission
    denied" wording but is not a sandbox block."""
    if not _is_network_exception(exc):
        return False
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        return True
    import errno as _errno
    msgs = []
    for e in _exc_chain(exc):
        if isinstance(e, OSError) and (
            e.errno in (_errno.EPERM, _errno.EACCES) or getattr(e, "winerror", None) == 10013
        ):
            return True
        msgs.append(str(e).lower())
    msg = " ".join(msgs)
    markers = (
        "winerror 10013",                          # Windows WSAEACCES
        "forbidden by its access permissions",     # Windows human-readable form
        "eacces",                                  # POSIX symbolic
        "eperm",
        "[errno 1]",                               # POSIX numeric EPERM (macOS Seatbelt)
        "operation not permitted",
        "[errno 13]",                              # POSIX numeric EACCES
        "permission denied",
    )
    if any(m in msg for m in markers):
        return True
    # DNS is the first thing the Seatbelt sandbox breaks: inside a Codex
    # sandbox, a name-resolution failure is the sandbox, not the network.
    return bool(os.environ.get("CODEX_SANDBOX")) and _is_name_resolution_error(exc)


_SANDBOX_HINT = (
    "Could not reach Overleaf: this command runs in a sandbox that blocks network "
    "access (Codex CLI's default sandbox does). This is not a login problem: the "
    "cookie was not checked, and setup/login/doctor would fail the same way. Approve "
    "running `overleaf-sync-now` with network access (outside the sandbox), or run it "
    "from a normal terminal."
)

_OFFLINE_HINT = (
    "Could not reach Overleaf ({detail}). This looks like no network connection "
    "(offline, DNS or proxy failure) or an Overleaf outage, not a login problem: the "
    "cookie was not checked, so do not run login or setup for this."
)
# Appended only where a refresh was attempted (sync), not by status/doctor/save-cookie.
_NOT_REFRESHED_NOTE = (
    " The local copy was not refreshed and may be stale; retry when the connection is back."
)


def _network_detail(exc):
    """The most specific (innermost) message in the exception chain, short."""
    detail = ""
    for e in _exc_chain(exc):
        text = str(e).strip()
        if text:
            detail = text
    detail = detail or type(exc).__name__
    return detail if len(detail) <= 160 else detail[:157] + "..."


def _network_unavailable_message(exc):
    if exc is not None and _is_sandbox_block(exc):
        return _SANDBOX_HINT
    if exc is None and os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        return _SANDBOX_HINT
    return _OFFLINE_HINT.format(detail=_network_detail(exc) if exc is not None else "no response")


def _wrap_network_error(e, context):
    """Convert a requests exception into NetworkUnavailable, with the sandbox
    message when the host blocked the connection."""
    if _is_sandbox_block(e):
        return NetworkUnavailable(_SANDBOX_HINT)
    return NetworkUnavailable(_OFFLINE_HINT.format(detail=f"{context}: {_network_detail(e)}"))


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


# -------- keep-both conflicts and our own writes (persisted) --------
#
# conflicts-pending.json  {project_id: {path: conflict_copy}}: files a
#   bootstrap / full pull KEPT (local newer than Overleaf's last change).
#   The next agent write to such a path is blocked once, even when a Read
#   did the pull, so the agent shows the user both versions first. An entry
#   is dropped after that block, or when a later pull writes the file.
# extracted.json  {project_id: {path: [mtime_ns, size]}}: files this tool
#   itself wrote. A file still carrying that stamp was not saved locally
#   since, so it never counts as a "local save" for the 30 s protect window
#   or for keep-both.


def _pending_conflicts_file():
    return pathlib.Path(CACHE_DIR) / "conflicts-pending.json"


def _extracted_file():
    return pathlib.Path(CACHE_DIR) / "extracted.json"


def _load_json_map(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[overleaf-sync-now] WARNING: {path} is unreadable ({e}); ignoring it.", file=sys.stderr)
        return {}


def _save_json_map(path, data):
    try:
        _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
    except OSError as e:
        print(f"[overleaf-sync-now] WARNING: could not save {path}: {e}", file=sys.stderr)


def pending_conflicts(project_id):
    entry = _load_json_map(_pending_conflicts_file()).get(project_id)
    return dict(entry) if isinstance(entry, dict) else {}


def _update_pending_conflicts(project_id, add=None, drop=()):
    """Add {path: copy} entries and drop paths for one project."""
    if not project_id or (not add and not drop):
        return
    path = _pending_conflicts_file()
    data = _load_json_map(path)
    entry = dict(data.get(project_id) or {})
    before = dict(entry)
    for rel in drop:
        entry.pop(rel, None)
    entry.update(add or {})
    if entry == before:
        return
    if entry:
        data[project_id] = entry
    else:
        data.pop(project_id, None)
    _save_json_map(path, data)


def _file_stamp(path):
    try:
        st = os.stat(str(path))
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


EXTRACTED_KEEP_PER_PROJECT = 2000


def _record_extracted(project_id, stamps):
    if not project_id or not stamps:
        return
    path = _extracted_file()
    data = _load_json_map(path)
    entry = dict(data.get(project_id) or {})
    for rel, stamp in stamps.items():
        entry.pop(rel, None)
        if stamp:
            entry[rel] = stamp
    if len(entry) > EXTRACTED_KEEP_PER_PROJECT:
        entry = dict(list(entry.items())[-EXTRACTED_KEEP_PER_PROJECT:])
    data[project_id] = entry
    _save_json_map(path, data)


def refresh_project(project_id, folder, *, force=False, report=None):
    """Version-match refresh. Probes /updates, decides whether a zip download
    is actually needed, and if so extracts only the changed files into folder.

    Returns a short human-readable status string. Raises AuthExpired,
    RateLimited, NetworkUnavailable or RuntimeError for the caller to handle.
    `report` (a dict), when given, receives what _extract_files did
    ("written", "conflicts", "protected", "backup_dir").
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
    # A bootstrap or full pull compares every file, including ones Overleaf
    # has not changed in a long time. A local file changed after Overleaf's
    # last change to it holds edits Overleaf never received (Dropbox paused,
    # offline edits, an Overleaf Dropbox outage): keep it and save Overleaf's
    # copy aside, instead of reverting it. --force always takes Overleaf's.
    change_times = _overleaf_change_times(updates) if (need_paths is None and not force) else None
    if report is None:
        report = {}
    n_written, n_unchanged, n_protected = _extract_files(
        zip_bytes, folder, need_paths, protect_recent_seconds=protect,
        project_id=project_id, change_times=change_times, report=report,
    )
    n_conflicts = len(report.get("conflicts") or {})
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
    if n_conflicts:
        parts.append(
            f"kept {n_conflicts} newer local file(s); Overleaf's copies saved under "
            f"{pathlib.Path(CACHE_DIR) / 'conflicts' / project_id}"
        )
    if report.get("backup_dir"):
        parts.append(f"previous local versions backed up to {report['backup_dir']}")
    return f"{label} (" + ", ".join(parts) + ")"


BACKUP_KEEP_DAYS = 30


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _save_aside(kind, project_id, stamp, rel, content):
    """Write `content` to <DATA_DIR>/<kind>/<project>/<stamp>/<rel> (kind is
    "backups" or "conflicts") and return the path. Never overwrites an
    earlier copy: a second copy in the same second gets a .1, .2, ... suffix.
    Files are user-only (0o600) on POSIX, like everything under DATA_DIR."""
    root = pathlib.Path(CACHE_DIR) / kind / (project_id or "unlinked") / stamp
    dest = root.joinpath(*[p for p in rel.split("/") if p not in ("", ".", "..")])
    n = 0
    candidate = dest
    while candidate.exists():
        n += 1
        candidate = dest.with_name(f"{dest.name}.{n}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(candidate)
    try:
        with open(tmp, "wb") as f:
            f.write(content)
        if os.name == "posix":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        _replace_with_retry(tmp, candidate)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _harden_dir(CACHE_DIR)
    return candidate


def _prune_backups(project_id, keep_days=BACKUP_KEEP_DAYS):
    """Delete this project's backup snapshots older than `keep_days`.
    Conflict copies are never pruned automatically."""
    root = pathlib.Path(CACHE_DIR) / "backups" / (project_id or "unlinked")
    cutoff = time.time() - keep_days * 86400
    try:
        for d in root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _overleaf_change_times(updates):
    """When did Overleaf last change each file, per the /updates page?

    Returns {"paths": {path: epoch_seconds}, "default": epoch_seconds} or None
    when the page carries no timestamps. A path that is not in the page last
    changed before the page's oldest entry, so "default" (that oldest time)
    is an upper bound for it."""
    last, oldest = {}, None
    for u in updates or []:
        ts = (u.get("meta") or {}).get("end_ts")
        if not isinstance(ts, (int, float)):
            continue
        ts = ts / 1000.0
        oldest = ts if oldest is None else min(oldest, ts)
        paths = list(u.get("pathnames") or [])
        for op in u.get("project_ops") or []:
            if not isinstance(op, dict):
                continue
            for key in ("add", "rename", "remove"):
                body = op.get(key)
                if isinstance(body, dict):
                    paths += [body.get("pathname"), body.get("newPathname")]
        for p in paths:
            p = _norm_overleaf_path(p)
            if p:
                last[p] = max(last.get(p, 0.0), ts)
    if oldest is None:
        return None
    return {"paths": last, "default": oldest}


def _extract_files(zip_bytes, folder, paths, protect_recent_seconds=30, *,
                   project_id=None, change_times=None, report=None):
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

    Before any overwrite the previous local bytes are copied to
    <DATA_DIR>/backups/<project>/<timestamp>/<path>.

    `change_times` (from _overleaf_change_times; passed on a bootstrap or
    full pull) turns on keep-both: a differing local file whose mtime is
    newer than Overleaf's last change to it holds local edits Overleaf never
    received (Dropbox paused or offline, Overleaf's Dropbox sync down). It is
    kept, and Overleaf's copy goes to <DATA_DIR>/conflicts/<project>/<ts>/.

    `report` (a dict) receives "written", "conflicts" ({path: copy}),
    "protected" (paths) and "backup_dir".

    Returns (n_written, n_unchanged, n_protected).
    """
    folder = pathlib.Path(folder).resolve()
    n_written = 0
    n_unchanged = 0
    n_protected = 0
    now = time.time()
    stamp = _stamp()
    if report is None:
        report = {}
    report.setdefault("written", [])
    report.setdefault("conflicts", {})
    report.setdefault("protected", [])
    report.setdefault("backup_dir", None)
    own = (_load_json_map(_extracted_file()).get(project_id) or {}) if project_id else {}
    stamps = {}

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
            # The opt-out file is per machine too: a copy that reached
            # Overleaf (uploaded from someone's Dropbox) must not switch the
            # hook off in every collaborator's folder.
            if pathlib.PurePosixPath(rel).name in (PROJECT_MARKER, IGNORE_MARKER):
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

            old_content = None
            if target.exists():
                try:
                    old_content = target.read_bytes()
                except OSError as e:
                    # Can't back it up, so don't overwrite it. Counted as
                    # protected: the version is held and the next sync retries.
                    n_protected += 1
                    report["protected"].append(rel)
                    print(
                        f"[overleaf-sync-now] SKIP {rel}: the local file can't be read ({e}), "
                        f"so it can't be backed up; not overwriting it. Fix its permissions "
                        f"and sync again.",
                        file=sys.stderr,
                    )
                    continue
                if old_content == new_content:
                    n_unchanged += 1
                    continue
                try:
                    mtime = target.stat().st_mtime
                except OSError:
                    mtime = None
                # A file that still carries the stamp of our own last write
                # was not saved locally since: no protect window, no keep-both.
                if mtime is not None and own.get(rel) and own.get(rel) == _file_stamp(target):
                    mtime = None
                # Content differs. If the local file was modified very
                # recently, the user is probably mid-save and our write
                # would clobber a local edit Dropbox hasn't yet pushed
                # up to Overleaf. Leave it alone and warn.
                if protect_recent_seconds > 0 and mtime is not None:
                    age = now - mtime
                    if age < protect_recent_seconds:
                        n_protected += 1
                        report["protected"].append(rel)
                        print(
                            f"[overleaf-sync-now] SKIP {rel}: local modified {age:.0f}s ago "
                            f"(< {protect_recent_seconds}s protection window). The next sync "
                            f"pulls it once the local save is older than that "
                            f"(`sync --force` overwrites now; the local copy is backed up first).",
                            file=sys.stderr,
                        )
                        continue
                # Bootstrap / full pull: a local file changed after Overleaf
                # last changed it holds edits Overleaf never got. Keep both.
                if change_times is not None and mtime is not None:
                    last = change_times["paths"].get(rel, change_times["default"])
                    if last is not None and mtime > last:
                        copy = _save_aside("conflicts", project_id, stamp, rel, new_content)
                        report["conflicts"][rel] = str(copy)
                        print(
                            f"[overleaf-sync-now] KEPT local {rel}: it differs from Overleaf and "
                            f"was changed locally after Overleaf's last change to it (edits "
                            f"Overleaf never received?). Overleaf's version was saved to {copy}. "
                            f"Compare the two before editing.",
                            file=sys.stderr,
                        )
                        continue
                if old_content is not None:
                    _save_aside("backups", project_id, stamp, rel, old_content)
                    report["backup_dir"] = str(
                        pathlib.Path(CACHE_DIR) / "backups" / (project_id or "unlinked") / stamp)

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
            report["written"].append(rel)
            stamps[rel] = _file_stamp(target)

    if project_id:
        _record_extracted(project_id, stamps)
        # Kept files wait for the user's decision (the hook blocks the next
        # write to them once); a file we just wrote is no longer in conflict.
        _update_pending_conflicts(project_id, add=report["conflicts"], drop=report["written"])
    if report["backup_dir"]:
        _prune_backups(project_id)
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
                   "ignored"    opted out (.overleaf-sync-ignore / $OVERLEAF_SYNC_IGNORE)
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
    for entry in (os.environ.get(IGNORE_ENV) or "").split(os.pathsep):
        if not entry.strip():
            continue
        try:
            ignored = pathlib.Path(entry.strip()).expanduser().resolve()
        except OSError:
            continue
        if p == ignored or p.is_relative_to(ignored):
            res.update(folder=ignored, reason="ignored", detail=f"listed in ${IGNORE_ENV}")
            return res
    # The opt-out file is for folders that are NOT Overleaf projects. It sits
    # in the Dropbox folder, so it can also reach Overleaf and every
    # collaborator; it therefore never beats a valid marker, and it only
    # applies when the folder name does not resolve to a project (below).
    ignore_at = None
    cur = p
    while True:
        if ignore_at is None and (cur / IGNORE_MARKER).exists():
            ignore_at = cur
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
                    if ignore_at is not None and verbose:
                        print(
                            f"[overleaf-sync-now] WARN: ignoring {ignore_at / IGNORE_MARKER}: "
                            f"{cur} is linked to Overleaf project {pid}. To opt a real project "
                            f"out, list its folder in ${IGNORE_ENV} instead (the file would sync "
                            f"to Overleaf and to collaborators).",
                            file=sys.stderr,
                        )
                    res.update(folder=cur, project_id=pid, reason="ok")
                    return res
        if cur.parent == cur:
            break
        cur = cur.parent

    def _ignored():
        res.update(folder=ignore_at, project_id=None, reason="ignored",
                   detail=f"{ignore_at / IGNORE_MARKER} exists")
        return res

    folder = _apps_overleaf_folder(p)
    if folder is not None and not folder.is_dir():
        # A project folder that does not exist yet (a new project being
        # created, or a mis-parsed path): nothing local can be stale, and
        # auto-link must not create directories by writing a marker.
        return _ignored() if ignore_at is not None else res
    if folder is not None:
        name = folder.name
        outcome = {}
        pid = _autolink_resolve(
            name, folder, verbose=verbose,
            allow_disambig_network=allow_disambig_network,
            write_marker=not read_only and ignore_at is None, outcome=outcome,
        )
        res.update(folder=folder, name=name)
        if pid:
            if ignore_at is not None and verbose:
                print(
                    f"[overleaf-sync-now] WARN: ignoring {ignore_at / IGNORE_MARKER}: "
                    f"{folder.name!r} is the name of an Overleaf project ({pid}). To opt it "
                    f"out anyway, list the folder in ${IGNORE_ENV}.",
                    file=sys.stderr,
                )
            res.update(project_id=pid, reason="ok")
        elif ignore_at is not None:
            return _ignored()
        else:
            res.update(
                reason=outcome.get("reason") or "none",
                candidates=outcome.get("candidates") or [],
                detail=outcome.get("detail"),
                numbered_base=outcome.get("numbered_base"),
            )
        return res
    if ignore_at is not None:
        return _ignored()
    return res


def _apps_overleaf_folder(path):
    """The .../Apps/Overleaf/<name> folder containing `path` (a resolved,
    absolute folder path), or None."""
    parts = pathlib.Path(path).parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "overleaf" and i > 0 and parts[i - 1].lower() == "apps":
            if i + 1 < len(parts):
                return pathlib.Path(*parts[: i + 2])
    return None


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
    if status_outcome[0] in ("none", "numbered") and _index_age_seconds() > 60:
        # The cached index can be up to 24 h old, so a project created since
        # then is missing from it ("Problem Set 5" next to an older "Problem
        # Set"). Re-fetch once before declaring "none" or "numbered".
        try:
            records = _load_projects_records(force_refresh=True, strict=True, require_fresh=True)
            status_outcome = _resolve_by_name(name, records)
        except AuthExpired as e:
            outcome.update(reason="auth", detail=str(e))
            return None
        except (RuntimeError, requests.exceptions.RequestException) as e:
            # Offline / sandboxed: the stale list proves nothing about this
            # name, so report "could not reach Overleaf", never "no project".
            outcome.update(reason="network", detail=str(e))
            if verbose:
                print(f"[overleaf-sync-now] auto-link: could not refresh the project list: {e}",
                      file=sys.stderr)
            return None
    status = status_outcome[0]
    if status == "numbered":
        # A numbered duplicate folder. With several same-name projects the
        # fingerprint check may pick one; with a single one, the number is
        # no evidence ("Chapter 2" is not "Chapter"), so the user decides.
        outcome["numbered_base"] = status_outcome[1][0].get("name")
        if len(status_outcome[1]) >= 2:
            status = "ambiguous"
        else:
            outcome.update(reason="ambiguous", candidates=status_outcome[1])
            if verbose:
                print(
                    f"[overleaf-sync-now] auto-link: no project is named {name!r}; it looks like a "
                    f"numbered copy of {outcome['numbered_base']!r} "
                    f"({status_outcome[1][0].get('id')}). Not guessing. If that is the project, run:\n"
                    f"    overleaf-sync-now link {status_outcome[1][0].get('id')} {folder}",
                    file=sys.stderr,
                )
            return None
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


_NUMBERED_SUFFIX_RE = re.compile(r"^(.*\S)\s+\(?(\d+)\)?$")


def _nfc(text):
    import unicodedata
    return unicodedata.normalize("NFC", text or "")


def _resolve_by_name(name, records):
    """Policy-driven name resolver. Returns one of:
      ("ok", project_id)
      ("none",)
      ("ambiguous", [candidate_records])
      ("numbered", [candidate_records])  folder "Paper (1)" / "Paper 2" with
                                         no exact match, projects named "Paper"

    Filters trashed and archived projects out by default. Tries case-sensitive
    exact match first; only if that yields zero results does it fall back to
    case-insensitive (covers macOS/Windows case-insensitive filesystems where
    the local folder name may differ in case from Overleaf's project name).
    """
    candidates = [r for r in records
                  if not r.get("trashed") and not r.get("archived")
                  and r.get("name") and r.get("id")]
    # Compare in Unicode NFC: a folder name can reach us decomposed (NFD,
    # e.g. "E" + combining accent) while Overleaf stores the composed form.
    name = _nfc(name)
    exact = [r for r in candidates if _nfc(r["name"]) == name]
    if not exact:
        exact = [r for r in candidates if _nfc(r["name"]).casefold() == name.casefold()]
    if not exact:
        # Overleaf's Dropbox sync may append an incremented number to the
        # folders of projects that share a name ("Paper (1)", "Paper 2").
        # Never resolve such a folder on the name alone: return the projects
        # with the base name for the fingerprint check / the user to pick.
        m = _NUMBERED_SUFFIX_RE.match(name)
        if m:
            base = m.group(1).casefold()
            dup = [r for r in candidates if _nfc(r["name"]).casefold() == base]
            if dup:
                dup.sort(key=lambda r: r.get("lastUpdated") or "", reverse=True)
                return ("numbered", dup)
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


def _load_projects_records(*, force_refresh=False, strict=False, require_fresh=False):
    """Return the list of project records, refreshing from Overleaf when
    the cache is missing/expired/wrong-version. May make a network call;
    use `_load_cached_projects_records` for the no-network variant.

    strict=True raises RuntimeError when the fetch fails and there is no
    cached index to fall back on (instead of returning []), so a resolver
    can tell "project list unavailable" from "no such project".
    require_fresh=True (with force_refresh) raises when the fetch fails even
    if a cached index exists, so a re-fetch failure is never mistaken for
    "the project is not in the list"."""
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
    return _refresh_projects_records(strict=strict, require_fresh=require_fresh)


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


def _refresh_projects_records(strict=False, require_fresh=False):
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
            raise NetworkUnavailable(_SANDBOX_HINT) from e
        cached = _load_cached_projects_records()
        if (strict and not cached) or require_fresh:
            raise _wrap_network_error(e, "fetching the project list") from e
        return cached
    if r.status_code != 200:
        cached = _load_cached_projects_records()
        if (strict and not cached) or require_fresh:
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


def clear_synced(project_ids):
    state = _state()
    if any(pid in state for pid in project_ids):
        for pid in project_ids:
            state.pop(pid, None)
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
    if sys.platform == "darwin" and _check_cookies(_load_cache())[0] != "ok":
        print("  - Looking for an existing Overleaf login in your browsers. macOS may ask for")
        print("    your login keychain password to let `security` read \"<Browser> Safe Storage\".")
        print("    Allow uses that login; Deny is fine too (then run `overleaf-sync-now login`).")
        print("    Avoid Always Allow. The PreToolUse hook never asks for this.")
    # Same chain `sync` and the hook use (cached -> login profile -> rookiepy
    # -> browser_cookie3 -> Playwright profile), so setup never reports
    # failure for a session that sync would find.
    try:
        cookies, source = _resolve_cookies_with_source(interactive=interactive, browsers=True)
    except NetworkUnavailable as e:
        print(f"  - Could not reach Overleaf, so auth was not checked.\n    {e}")
        print("  - Re-run `overleaf-sync-now setup` (or `status`) once Overleaf is reachable.")
        return
    if cookies:
        print(f"  - Valid Overleaf session found via {source} ({SESSION_COOKIE}: present).")
        print(f"  - Cached at {CACHE_FILE}")
        return
    if not interactive:
        print()
        print("AUTO-DETECT FAILED. Next step (the user runs this in their own terminal):")
        print("    overleaf-sync-now login")
        print("  A browser window opens; log into Overleaf there. Works on Chrome 127+,")
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


SYNC_WAIT_MAX_SECONDS = 120


def cmd_sync(args):
    force = "--force" in args
    wait = "--wait" in args
    args = [a for a in args if not a.startswith("--")]
    folder = pathlib.Path(args[0] if args else ".")
    res = resolve_project(folder)
    linked, project_id = res["folder"], res["project_id"]
    if res["reason"] == "ignored":
        print(
            f"{folder.resolve()} is opted out of overleaf-sync-now ({res.get('detail')}); "
            f"nothing to sync. Remove the opt-out to sync it.",
            file=sys.stderr,
        )
        sys.exit(1)
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

    print(f"Refreshing project {project_id} (folder: {linked})", flush=True)
    t0 = time.time()
    deadline = t0 + SYNC_WAIT_MAX_SECONDS
    while True:
        report = {}
        try:
            status = refresh_project(project_id, linked, force=force, report=report)
        except RateLimited as e:
            print(f"ERROR: rate-limited ({e}). Try again in ~{e.retry_after}s.", file=sys.stderr)
            sys.exit(1)
        except AuthExpired as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        except RuntimeError as e:
            # Network failure, sandbox block, malformed response, etc. Print
            # clean message (no traceback) so the actionable hint is visible.
            note = _NOT_REFRESHED_NOTE if isinstance(e, NetworkUnavailable) else ""
            print(f"ERROR: {e}{note}", file=sys.stderr)
            sys.exit(1)
        delay = _protect_wait_seconds(linked, report.get("protected") or []) if wait else 0
        if not delay:
            break
        if time.time() + delay > deadline:
            print(
                f"Still saved locally in the last 30 s after waiting: "
                f"{', '.join(sorted(report['protected'])[:5])}. Someone (or something) keeps "
                f"saving it; stop and ask the user before editing it.",
                file=sys.stderr,
            )
            break
        print(f"Waiting {delay:.0f} s until {', '.join(sorted(report['protected'])[:5])} "
              f"is older than 30 s, then pulling again...", flush=True)
        time.sleep(delay)
    mark_synced(project_id)
    print(f"{status} ({time.time() - t0:.2f}s)")
    if report.get("protected") and wait:
        sys.exit(1)


def _protect_wait_seconds(folder, protected, window=30):
    """Seconds until every file in `protected` (paths relative to folder) is
    older than the protect window, or 0 when none is waiting on it."""
    now, wait_for = time.time(), 0.0
    for rel in protected:
        try:
            age = now - (pathlib.Path(folder) / rel).stat().st_mtime
        except OSError:
            continue
        if age < window:
            wait_for = max(wait_for, window - age + 1)
    return wait_for


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
    res = resolve_project(folder, read_only=True)
    linked, project_id = (res["folder"], res["project_id"]) if res["reason"] == "ok" else (None, None)
    print(f"Data dir:    {CACHE_DIR}")
    print(f"Cookie file: {CACHE_FILE} ({'present' if CACHE_FILE.exists() else 'MISSING'})")
    cached = _load_cache()
    # Force a real network probe — status is a diagnostic, not a hot path.
    state, exc = _check_cookies(cached, use_cache=False)
    if state == "ok":
        print("Cookie auth: OK (cache valid, sync would succeed)")
    elif state == "unreachable":
        # Offline or sandboxed: say so instead of a misleading INVALID.
        print(f"Cookie auth: UNKNOWN — could not reach Overleaf, so the cookie was not checked.\n"
              f"  {_network_unavailable_message(exc)}")
    else:
        if quick:
            print("Cookie auth: cache INVALID (chain not checked; pass without --quick for full check, or run `doctor`).")
        else:
            # Cache failed; walk the full chain to predict sync behavior.
            try:
                resolved = _resolve_cookies(interactive=False, browsers=True)
                unreachable = None
            except NetworkUnavailable as e:
                resolved, unreachable = None, e
            if unreachable is not None:
                print(f"Cookie auth: UNKNOWN — could not reach Overleaf, so the cookie was not checked.\n"
                      f"  {unreachable}")
            elif resolved:
                print("Cookie auth: OK (cache stale, but chain resolved; sync would succeed and refresh cache)")
            else:
                print("Cookie auth: INVALID — sync would FAIL and the hook blocks AI edits. "
                      "Fix: the user runs `overleaf-sync-now login` in their own terminal "
                      "(headless: `overleaf-sync-now save-cookie`, hidden prompt).")
    _print_install_health()
    print()
    if not project_id:
        print(f"Folder: {folder.resolve()}")
        if res["reason"] == "ignored":
            print(f"Project: opted out ({res.get('detail')}); the hook and sync leave it alone")
        else:
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
    r"""(?:^|[;&|\n]|&&)\s*(?:cd|pushd|Set-Location|sl)\s+("[^"\n]+"|'[^'\n]+'|(?:\\.|[^\s;&|])+)""",
    re.IGNORECASE,
)
_PS_ENV_RE = re.compile(r"\$\{?env:([A-Za-z_][A-Za-z0-9_]*)\}?", re.IGNORECASE)
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
# sed's `w FILE` command / s///w flag writes FILE.
_SED_WRITE_RE = re.compile(
    r"(?:^|[\s;/}'\"])[wW]\s*[^\s;'\"|&]+\.(?:tex|bib|cls|sty|bst)(?![\w])", re.IGNORECASE
)
# Output redirections: >, >>, 1>, 2>, &>, &>>, >|, and >&file. Only fd
# duplication (2>&1, >&2, >&-) is not a write: the target group can't start
# with '&', and the optional '&' after '>' is taken only before a filename.
_REDIRECT_RE = re.compile(
    r"(?:\d+|&)?>>?\|?(?:&(?![\d-]))?\s*(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)


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
        if _is_latex_path(t) or any(ch in t for ch in "*?[$`"):
            return False  # a LaTeX file, a glob, or a target built at run time ("$F")
    if re.search(r"\bprintf?\b[^;}'\"]*>", cmd):
        return False  # awk '{print > FILENAME}' / print $0 > "out.tex"
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
        if prog == "sed" and _SED_WRITE_RE.search(cmd):
            return False  # sed 's/a/b/w main.tex' (the script is masked in `seg`)
        if prog == "find" and any(
            w in ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0",
                  "-fprintf", "-fls") for w in rest
        ):
            return False
        if prog == "git" and (
            not rest or rest[0] not in _READ_ONLY_GIT
            or any(w.startswith("--output") for w in rest)  # git diff --output=x
        ):
            return False
        if prog == "sort" and any(
            w.startswith("--output")
            or (w.startswith("-") and not w.startswith("--") and "o" in w[1:])
            for w in rest
        ):
            return False  # sort -o FILE
        if prog == "uniq":
            operands, skip = [], False
            for w in rest:
                if skip:
                    skip = False
                elif re.match(r"(?:\d+|&)?[<>]", w):
                    # a redirection (`2>/dev/null`, or `>` followed by its target)
                    skip = bool(re.fullmatch(r"(?:\d+|&)?[<>]{1,2}[|&]?", w))
                elif w in ("-f", "-s", "-w"):
                    skip = True  # option taking a separate value
                elif not w.startswith("-") or w == "-":
                    operands.append(w)
            if len(operands) >= 2:
                return False  # uniq INPUT OUTPUT
    return True


def _expand_shell_vars(text):
    """Expand $NAME / ${NAME} (and PowerShell $env:NAME) from this process's
    environment, which the hook shares with the agent's shell. Unknown
    variables are left as they are."""
    if "$" not in text:
        return text
    text = _PS_ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), text)
    return os.path.expandvars(text)


def _shell_candidate_paths(cmd, cwd, *, posix_escapes=True, cd_text=None):
    """Candidate LaTeX paths mentioned in a shell command, as absolute Paths.
    Globs are expanded (bounded). Paths need not exist. Relative paths are
    tried against `cwd` and every `cd <dir>` in `cd_text` (default: `cmd`)."""
    import glob as _glob
    tokens = []
    for m in _SHELL_PATH_RE.finditer(cmd):
        tok = m.group(1) or m.group(2) or m.group(3)
        if not tok:
            continue
        if "$" in tok and not m.group(2):  # no expansion inside '...'
            tok = _expand_shell_vars(tok)
        tokens.append(tok)
        if posix_escapes and m.group(3) and "\\" in tok:
            tokens.append(re.sub(r"\\(.)", r"\1", tok))
    if not tokens:
        return []
    cwd = pathlib.Path(cwd)
    bases = [cwd]
    for m in _SHELL_CD_RE.finditer(cmd if cd_text is None else cd_text):
        raw = m.group(1)
        if raw[:1] == "'":
            d = raw.strip("'")
        else:
            d = raw[1:-1] if raw[:1] == '"' else raw
            if posix_escapes and raw[:1] != '"':
                d = re.sub(r"\\(.)", r"\1", d)  # cd ~/A\ B
            d = _expand_shell_vars(d)
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


_PATCH_HEADER_RE = re.compile(
    r"^\*\*\* ((?:Update|Add|Delete) File|Move to):\s*(.+?)\s*$", re.MULTILINE
)
# Programs (and PowerShell aliases) whose operands are only deleted.
_DELETE_PROGRAMS = {"rm", "unlink", "del", "erase", "remove-item", "ri"}


def _path_key(p):
    return os.path.normcase(str(_real_path(p)))


def _pure_delete_targets(data):
    """Keys (_path_key) of the LaTeX files this tool call only deletes: Codex
    `*** Delete File:` lines, or shell segments that run rm / git rm /
    unlink / Remove-Item / del / erase on them. A path the call also names
    anywhere else (an Update/Move, a redirect, cp/mv, another program) is not
    included, so `rm a.tex && echo x > a.tex` still counts as editing a.tex.
    Used to let the agent delete the outdated local copy of a file that was
    deleted or renamed on Overleaf (deleting it cannot re-create it)."""
    import shlex
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return set()
    cwd = pathlib.Path(data.get("cwd") or os.getcwd())
    text = _command_text(tool_input)
    deleted, other = set(), set()
    if tool == "apply_patch" or (tool in _SHELL_TOOLS and "*** Begin Patch" in text):
        for m in _PATCH_HEADER_RE.finditer(text):
            fp = pathlib.Path(m.group(2).strip().strip("\"'")).expanduser()
            key = _path_key(fp if fp.is_absolute() else cwd / fp)
            (deleted if m.group(1) == "Delete File" else other).add(key)
        if tool == "apply_patch":
            return deleted - other
        text = re.sub(r"\*\*\* Begin Patch.*?(?:\*\*\* End Patch|\Z)", " ", text, flags=re.S)
    if tool not in _SHELL_TOOLS:
        return set()
    posix = tool == "Bash"
    try:
        lex = shlex.shlex(text.replace("\n", " ; "), posix=posix, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return set()  # unbalanced quotes: no exemption
    segments, cur = [], []
    for tok in tokens:
        if tok and set(tok) <= set(";&|"):
            segments.append(cur)
            cur = []
        else:
            cur.append(tok if posix else tok.strip("\"'"))
    segments.append(cur)

    def paths_of(words):
        quoted = " ".join('"' + w.replace('"', "") + '"' for w in words)
        return {_path_key(p) for p in _shell_candidate_paths(
            quoted, cwd, posix_escapes=False, cd_text=text)}

    for words in segments:
        words = list(words)
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
            words.pop(0)
        if not words:
            continue
        prog = os.path.basename(words[0]).lower()
        if prog.endswith(".exe"):
            prog = prog[:-4]
        rest = words[1:]
        if prog == "git" and rest[:1] == ["rm"]:
            prog, rest = "git rm", rest[1:]
        # An output redirection makes the segment a write, except fd
        # duplication (`2>&1`, `>&2`, `>&-`).
        redirect = False
        for i, w in enumerate(rest):
            if ">" in w and not (
                re.fullmatch(r"\d*>&\d*-?", w)
                and (w[-1] != "&" or (i + 1 < len(rest) and re.fullmatch(r"\d+|-", rest[i + 1])))
            ):
                redirect = True
        if (prog in _DELETE_PROGRAMS or prog == "git rm") and not redirect:
            deleted |= paths_of([w for w in rest if not w.startswith("-")])
        else:
            other |= paths_of(words)
    return deleted - other


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


def _real_path(path):
    """Absolute path with symlinks resolved; the file need not exist.
    resolve_project() resolves the folder it returns, so a target reached
    through a symlink (e.g. ~/Dropbox -> ~/Library/CloudStorage/Dropbox) has
    to be resolved too before it is compared with that folder."""
    try:
        return pathlib.Path(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return pathlib.Path(os.path.abspath(str(path)))


def _rel_posix(path, folder):
    try:
        return _real_path(path).relative_to(_real_path(folder)).as_posix()
    except ValueError:
        return None


def _refresh_summary(folder, report):
    """One line saying what a hook-driven refresh wrote or skipped (None when
    it touched nothing), so a pull into the user's files is never silent."""
    def names(items):
        items = sorted(items)
        shown = ", ".join(items[:5])
        return shown + (f" and {len(items) - 5} more" if len(items) > 5 else "")

    parts = []
    if report.get("written"):
        parts.append(f"pulled newer Overleaf content into {names(report['written'])}")
    if report.get("conflicts"):
        parts.append(f"kept the newer local {names(report['conflicts'])} (Overleaf's copy "
                     f"saved under {pathlib.Path(CACHE_DIR) / 'conflicts'})")
    if report.get("protected"):
        parts.append(f"skipped {names(report['protected'])} (saved locally in the last 30 s)")
    if not parts:
        return None
    line = f"[overleaf-sync-now] {pathlib.Path(folder).name}: " + "; ".join(parts) + "."
    if report.get("backup_dir"):
        line += f" Previous local versions: {report['backup_dir']}"
    return line


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
        n = len(res.get("candidates") or [])
        if res.get("numbered_base"):
            lines = [f"{head}: no project is named {res.get('name')!r}; the folder looks like a "
                     f"numbered duplicate of {res['numbered_base']!r} ({n} living project(s) with "
                     f"that name). Refusing to guess. Candidates (most recent first):"]
        else:
            lines = [f"{head}: {n} living Overleaf projects are named "
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
        f"  `overleaf-sync-now projects --refresh` lists the user's projects.\n"
        f"  If the user says this folder is not an Overleaf project at all, they can opt it\n"
        f"  out with an empty file named {IGNORE_MARKER} in it (only with their OK)."
    )


def _hook_output_json(text, event="PreToolUse"):
    """Exit-0 hook output both clients show: `systemMessage` is shown to the
    user, `hookSpecificOutput.additionalContext` is added to the model's
    context. (Claude Code never shows exit-0 stderr to the model, and shows
    it to the user only in debug logs; Codex ignores plain stdout.) No
    permissionDecision: the normal permission flow still applies."""
    if len(text) > 4000:
        text = text[:3990] + "\n[...]"
    return json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
    }, ensure_ascii=False)


def cmd_hook(args):
    # No keychain / browser-store access from the hook (see _BROWSER_EXTRACTION).
    previous = _BROWSER_EXTRACTION["allowed"]
    _BROWSER_EXTRACTION["allowed"] = False
    # stdout is reserved for the one JSON object the hook may print; anything
    # else printed while refreshing goes to stderr.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        _cmd_hook(args, real_stdout)
    finally:
        sys.stdout = real_stdout
        _BROWSER_EXTRACTION["allowed"] = previous


def _cmd_hook(args, out=None):
    out = out or sys.stdout
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
    # Resolve symlinks once, so the write-guard snapshot keys and the
    # web-removed lookup compare against the (resolved) project folder.
    probe_points = [(_real_path(p), True) for p in paths]
    if cwd_hint is not None:
        probe_points.append((cwd_hint, False))
    for point, is_file_target in probe_points:
        try:
            res = resolve_project(point, verbose=False)
        except Exception as e:
            apps = _apps_overleaf_folder(point if not is_file_target else point.parent)
            if is_write and apps is not None:
                # Fail closed under Apps/Overleaf/<name>/, as for any other
                # folder whose project cannot be determined.
                blocks.append(
                    f"[overleaf-sync-now] Could not tell which Overleaf project {apps} belongs "
                    f"to ({type(e).__name__}: {e}). {tool} blocked so a possibly-stale local "
                    f"copy is not edited. Run `overleaf-sync-now status \"{apps}\"` to see the "
                    f"error, then retry."
                )
            else:
                warnings.append(f"[overleaf-sync-now] hook: could not resolve project for {point}: {e}; skipping.")
            continue
        reason = res["reason"]
        if reason == "ok":
            entry = projects.setdefault(res["project_id"], {"folder": res["folder"], "paths": []})
            if is_file_target:
                entry["paths"].append(point)
            continue
        if reason in ("unmanaged", "ignored"):
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

    deletes = None  # computed on demand by write guard 2
    blocked_pids = []
    for pid, entry in projects.items():
        n_blocks = len(blocks)
        folder = entry["folder"]
        targets = entry["paths"]
        before = _latex_snapshot(folder, targets) if is_write else None
        window = WRITE_DEBOUNCE_SECONDS if is_write else DEBOUNCE_SECONDS
        refreshed = False
        report = {}
        if not is_debounced(pid, window=window):
            try:
                refresh_project(pid, folder, report=report)
                mark_synced(pid)
                refreshed = True
                summary = _refresh_summary(folder, report)
                if summary:
                    warnings.append(summary)
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
                if isinstance(e, NetworkUnavailable) or _is_network_exception(e):
                    # Offline or sandboxed: nothing is known about auth, so
                    # never block or send the agent to re-auth (documented
                    # policy: transient errors pass through with a note).
                    text = str(e) if isinstance(e, NetworkUnavailable) else _network_unavailable_message(e)
                    warnings.append(
                        f"[overleaf-sync-now] {text}\n[overleaf-sync-now] {tool} allowed "
                        f"without a refresh; the local copy may be stale."
                    )
                elif isinstance(e, OSError):
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
        protected = list(report.get("protected") or []) if refreshed else []
        if protected:
            if targets:
                rels = {_rel_posix(p, folder) for p in targets}
                recent = [r for r in protected if r in rels]
            else:
                recent = protected
            if recent:
                names = ", ".join(recent[:5])
                blocks.append(
                    f"[overleaf-sync-now] Conflict: {names} changed on Overleaf, and was also "
                    f"saved locally in the last 30 s (that save may not have reached Overleaf "
                    f"yet), so the web version was not pulled. {tool} blocked so it does not "
                    f"overwrite the web edit. Do exactly this:\n"
                    f"  1. Run `overleaf-sync-now sync --wait \"{folder}\"`. It waits until the "
                    f"local save is 30 s old (well under a minute), then pulls the web version "
                    f"(the local copy is backed up first under "
                    f"{pathlib.Path(CACHE_DIR) / 'backups'}). Do not retry the edit before that.\n"
                    f"  2. Read {names} again and check that both your recent change and the "
                    f"web edit are there; redo whatever is missing.\n"
                    f"  3. Then retry the edit. If this message comes back for the same file, "
                    f"stop and tell the user: someone is editing it on overleaf.com right now."
                )
        # Write guard 1c: a bootstrap / full pull found that a target was
        # changed locally after Overleaf last changed it (edits Overleaf never
        # received) and kept it, saving Overleaf's version aside. Persisted,
        # so the first write after the pull is blocked once even when a Read
        # (or another call) did the pull.
        conflicts = pending_conflicts(pid)
        if conflicts:
            if targets:
                hit = {}
                for p in targets:
                    rel = _rel_posix(p, folder)
                    if rel in conflicts:
                        hit[rel] = conflicts[rel]
            else:
                hit = dict(conflicts)
            for rel, copy in sorted(hit.items())[:5]:
                blocks.append(
                    f"[overleaf-sync-now] Conflict: {rel} differs from Overleaf's copy, and the "
                    f"local file was changed after Overleaf last changed it, so it may hold edits "
                    f"Overleaf never received. The local file was kept; Overleaf's version was "
                    f"saved to {copy}. {tool} blocked: show the user both versions (for example "
                    f"`diff \"{folder / rel}\" \"{copy}\"`) and ask which to keep before editing. "
                    f"Then retry the edit; this check does not repeat for this file."
                )
            if hit:
                _update_pending_conflicts(pid, drop=list(hit))
        # Write guard 2: the target was deleted or renamed on the web.
        # Deleting the outdated local copy is fine (it cannot re-create the
        # file on Overleaf); anything else that touches the old path is not.
        gone = web_removed_paths(pid, folder)
        if gone and deletes is None:
            deletes = _pure_delete_targets(data)
        for p in targets:
            rel = _rel_posix(p, folder)
            if rel and rel in gone:
                if _path_key(p) in deletes:
                    continue
                new = gone[rel]
                if new:
                    blocks.append(
                        f"[overleaf-sync-now] {rel} was renamed on Overleaf to {new} (refreshed "
                        f"locally). {tool} blocked: editing the old path would re-create it on "
                        f"Overleaf through Dropbox. Edit {new} instead. Deleting the outdated "
                        f"local {rel} is allowed."
                    )
                else:
                    blocks.append(
                        f"[overleaf-sync-now] {rel} was deleted on Overleaf. {tool} blocked: "
                        f"editing it would re-create it on Overleaf through Dropbox. Ask the "
                        f"user. If it should stay deleted, delete the outdated local copy "
                        f"(deleting it is allowed). If it should come back, the user re-creates "
                        f"it on Overleaf first; edits are allowed again after the next refresh."
                    )
        if len(blocks) > n_blocks:
            blocked_pids.append(pid)

    for w in warnings:
        print(w, file=sys.stderr)
    if blocks:
        # A blocked write must not leave the 3 s write debounce set: the
        # retry (or the next Edit of the same turn) has to probe again and
        # meet the same guards, not slip through unchecked.
        if blocked_pids:
            clear_synced(blocked_pids)
        print("\n".join(blocks), file=sys.stderr)
        sys.exit(2)
    if warnings:
        event = data.get("hook_event_name") or "PreToolUse"
        print(_hook_output_json("\n".join(warnings), event), file=out)
    sys.exit(0)


# -------- install / uninstall --------

HOME = pathlib.Path.home()
# Claude Code reads its configuration from $CLAUDE_CONFIG_DIR when set (one
# directory per account), else ~/.claude. Codex CLI uses $CODEX_HOME, else
# ~/.codex. ~/.agents/skills is the Codex user skills folder either way.
CLAUDE_DIR = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude")).expanduser()
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME") or (HOME / ".codex")).expanduser()
SKILL_TARGETS = {
    "Claude Code": CLAUDE_DIR / "skills" / "overleaf",
    # Codex CLI (and other Agent Skills clients) read user skills from
    # ~/.agents/skills (the documented user location); <= 0.4.x used
    # ~/.codex/skills, and two copies make Codex list the skill twice.
    "Codex CLI": HOME / ".agents" / "skills" / "overleaf",
}
# Copies written by <= 0.4.x. Removed on install (when ours) so Codex does not
# list the skill twice, and on uninstall.
LEGACY_SKILL_TARGETS = {
    "Codex CLI (legacy ~/.codex/skills)": CODEX_HOME / "skills" / "overleaf",
}
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
CODEX_HOOKS = CODEX_HOME / "hooks.json"
SETTINGS_BACKUP_SUFFIX = ".bak-overleaf-sync-now"


def _env_dir_is(var, path):
    """True when $var names `path` (the user pointed the tool at it, so it
    counts as present even before the client has created it)."""
    value = os.environ.get(var)
    if not value:
        return False
    try:
        return pathlib.Path(value).expanduser().resolve() == pathlib.Path(path).resolve()
    except OSError:
        return False


def _claude_present():
    return CLAUDE_SETTINGS.parent.exists() or _env_dir_is("CLAUDE_CONFIG_DIR", CLAUDE_SETTINGS.parent)


def _codex_present():
    return CODEX_HOME.exists() or _env_dir_is("CODEX_HOME", CODEX_HOME)


def _is_ephemeral_install(path):
    """A uvx / `uv run --with` environment lives in uv's cache and can be
    garbage-collected at any time, so a hook pointing into it breaks later."""
    parts = [x.lower() for x in pathlib.PurePath(str(path)).parts]
    if any(x.startswith("archive-v") or x.startswith("builds-v") for x in parts):
        return True
    cache = os.environ.get("UV_CACHE_DIR")
    if cache:
        try:
            return pathlib.Path(path).resolve().is_relative_to(pathlib.Path(cache).resolve())
        except OSError:
            return False
    return False


def _cli_path():
    """Absolute path of the overleaf-sync-now executable, or None.

    The hooks must name it absolutely: Claude Code runs hook commands with
    its own PATH, and a hook that cannot start (exit 127) does not block the
    tool call. ~/.local/bin is often not on PATH (e.g. uv from Homebrew), so
    besides PATH this also looks at the running entry point, the scripts
    directory of this Python (the uv tool environment), and `uv tool dir
    --bin`."""
    def usable(c):
        return bool(c) and os.path.isfile(c) and os.access(c, os.X_OK)

    found = shutil.which("overleaf-sync-now")
    if usable(found):
        return os.path.abspath(found)
    argv0 = sys.argv[0] if sys.argv else ""
    candidates = []
    if argv0 and os.path.basename(argv0).lower().startswith("overleaf-sync-now"):
        candidates.append(argv0)
    exe_dir = pathlib.Path(sys.executable).parent
    candidates += [str(exe_dir / "overleaf-sync-now"), str(exe_dir / "overleaf-sync-now.exe")]
    for c in candidates:
        if usable(c):
            return os.path.abspath(c)
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True,
                             timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    for name in ("overleaf-sync-now", "overleaf-sync-now.exe"):
        c = str(pathlib.Path(out) / name) if out else ""
        if usable(c):
            return os.path.abspath(c)
    return None


def _claude_hook_handler(existing=None):
    """Exec-form Claude Code hook handler: `command` is spawned directly with
    `args`, no shell. Shell form (`"<path>" hook`) is a parse error under
    PowerShell, which Claude Code uses on Windows without Git Bash, and a hook
    that fails to start does not block the tool call. Exec form also resolves
    a bare `overleaf-sync-now` on PATH when the absolute path is unknown."""
    handler = {
        "type": "command",
        "command": _cli_path() or "overleaf-sync-now",  # install refuses to run without a path
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
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level is not a JSON object")
        return data, True
    except (json.JSONDecodeError, ValueError) as e:  # UnicodeDecodeError is a ValueError
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


def _write_json_config(path, data, *, backup="once"):
    """Write a client config file without breaking how the user keeps it.

    Writes through a symlink (dotfiles setups link settings.json elsewhere;
    replacing the link would silently fork the config), keeps non-ASCII text
    as is, keeps the file mode, and saves the previous content first: once as
    <name>.bak-overleaf-sync-now (backup="once"), or as a timestamped copy
    (backup="timestamped"). The backup sits next to `path` (e.g. in
    ~/.claude/), not next to a symlink's target, so it never lands in a
    dotfiles repository. Returns the backup path written, if any."""
    path = pathlib.Path(path)
    real = pathlib.Path(os.path.realpath(str(path)))
    made = None
    mode = None
    if real.exists():
        try:
            mode = real.stat().st_mode & 0o777
        except OSError:
            mode = None
        if backup == "timestamped":
            made = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        else:
            made = path.with_name(path.name + SETTINGS_BACKUP_SUFFIX)
            if made.exists():
                made = None
        if made is not None:
            shutil.copyfile(str(real), str(made))
    _atomic_write_text(real, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)
    return made


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
    backup = _write_json_config(CODEX_HOOKS, data, backup="timestamped")
    if backup:
        print(f"  - Codex CLI: previous hooks.json backed up to {backup}")
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
    _write_json_config(CODEX_HOOKS, data, backup="timestamped")
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
                    problems.append(
                        f"{tool}: {skill} is a different skill named `overleaf`; install will not "
                        f"overwrite it (move or rename it to install ours there)"
                    )
        except OSError as e:
            problems.append(f"{tool}: cannot read {skill}: {e}")
    if not any_copy:
        problems.append("no SKILL.md copy in " + " or ".join(str(t) for t in SKILL_TARGETS.values()))
    for tool, target in LEGACY_SKILL_TARGETS.items():
        if (target / "SKILL.md").exists() and _is_our_skill_dir(target):
            problems.append(f"{tool}: old copy at {target} (Codex lists the skill twice)")
    if CLAUDE_SETTINGS.parent.exists():
        try:
            settings = (json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
                        if CLAUDE_SETTINGS.exists() else {})
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
                problems.append(f"Claude Code: no overleaf-sync-now PreToolUse hook in {CLAUDE_SETTINGS}")
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
                else:
                    problem = _hook_command_problem(h.get("command"))
                    if problem:
                        problems.append(f"Claude Code: {problem}")
    if CODEX_HOME.exists():
        try:
            codex = json.loads(CODEX_HOOKS.read_text(encoding="utf-8")) if CODEX_HOOKS.exists() else {}
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
            for g in groups:
                for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                    if isinstance(h, dict) and _is_our_hook(h) and os.name != "nt":
                        import shlex
                        try:
                            first = shlex.split(str(h.get("command") or ""))[:1]
                        except ValueError:
                            first = []
                        problem = _hook_command_problem(first[0] if first else "")
                        if problem:
                            problems.append(f"Codex CLI: {problem}")
    return problems


def _hook_command_problem(cmd):
    """Why a hook's executable may fail to start (exit 127, which lets the
    tool call through), or None."""
    if not cmd:
        return "hook has no command"
    if not os.path.isabs(str(cmd)):
        return (f"hook runs the bare name {cmd!r}, which fails wherever it is not on PATH; "
                f"install writes the full path")
    if not os.path.isfile(str(cmd)):
        return f"hook command {cmd} does not exist (CLI moved or uninstalled); re-run install"
    return None


def _print_install_health(indent=""):
    problems = _install_health()
    if not problems:
        print(f"{indent}Install:   OK (skill copies match this version; hooks look current)")
        return
    print(f"{indent}Install:   NEEDS ATTENTION (re-running `overleaf-sync-now install` fixes most of this):")
    for prob in problems:
        print(f"{indent}  - {prob}")
    if CODEX_HOME.exists():
        print(f"{indent}  (In Codex, trust a new or changed hook with /hooks afterwards.)")


def cmd_install(args):
    """Set up the skill in available AI tools and add the PreToolUse hooks.

    Everything written here is USER-GLOBAL (~/.claude/ or $CLAUDE_CONFIG_DIR,
    ~/.agents/, ~/.codex/ or $CODEX_HOME). The hooks + skill apply to every
    project the user works in, in any directory. The only per-project file the
    tool ever writes is the `.overleaf-project` marker that auto-link drops
    into an Overleaf folder.
    """
    print("=== Installing overleaf-sync-now (USER-GLOBAL, applies to all projects) ===\n")

    if not SKILL_MD_SRC.exists():
        print(f"ERROR: SKILL.md not found at {SKILL_MD_SRC}", file=sys.stderr)
        sys.exit(1)
    # The hooks name the CLI by absolute path. Without one, a hook would be a
    # bare `overleaf-sync-now` that fails to start (exit 127) wherever it is
    # not on PATH, and a hook that cannot start lets every edit through.
    cli = _cli_path()
    if not cli:
        print(
            "ERROR: could not find the overleaf-sync-now executable (not on PATH, not next to\n"
            f"  this Python ({sys.executable}), and `uv tool dir --bin` did not list it).\n"
            "  Nothing was installed. Install the CLI with `uv tool install` (see README), then run\n"
            "  the installed `overleaf-sync-now install`.",
            file=sys.stderr,
        )
        sys.exit(1)
    if _is_ephemeral_install(cli):
        print(
            f"ERROR: {cli} is in uv's cache (a uvx / `uv run` environment that uv may delete),\n"
            "  so hooks pointing at it would break later. Nothing was installed. Run\n"
            "    uv tool install --from git+https://github.com/hanlulong/overleaf-sync-now"
            f"@v{_pkg_version()} overleaf-sync-now\n"
            "  and then `overleaf-sync-now install`.",
            file=sys.stderr,
        )
        sys.exit(1)

    installed = []
    failed = []
    claude_present = _claude_present()
    codex_present = _codex_present()

    # 1. Copy SKILL.md into available skills directories.
    for tool, target in SKILL_TARGETS.items():
        present = claude_present if tool == "Claude Code" else (
            target.parent.parent.exists() or codex_present)
        if not present:
            print(f"  - {tool}: skipped ({target.parent.parent} not found)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _remove_existing_target(target):
            print(f"  - {tool}: skipped (see warning above)")
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(SKILL_MD_SRC), str(target / "SKILL.md"))
        installed.append(f"{tool} skill: {target / 'SKILL.md'}")
        print(f"  - {tool}: SKILL.md installed at {target / 'SKILL.md'}  [user-global]")
    for tool, target in LEGACY_SKILL_TARGETS.items():
        if (target.exists() or target.is_symlink()) and _is_our_skill_dir(target):
            if _remove_existing_target(target):
                print(f"  - {tool}: removed old copy at {target} (Codex now reads ~/.agents/skills)")

    # 2. Add Claude Code PreToolUse hook (atomically — settings.json
    # corruption breaks Claude Code start, so don't half-write it).
    if claude_present:
        settings, ok = _read_json_config(CLAUDE_SETTINGS, str(CLAUDE_SETTINGS))
        if not ok:
            sys.exit(1)
        hooks_root = settings.setdefault("hooks", {})
        cleaned, removed = _strip_our_hooks(hooks_root.get("PreToolUse", []))
        handler = _claude_hook_handler(removed[-1] if removed else None)
        cleaned.append({"matcher": CLAUDE_HOOK_MATCHER, "hooks": [handler]})
        hooks_root["PreToolUse"] = cleaned
        try:
            backup = _write_json_config(CLAUDE_SETTINGS, settings)
        except OSError as e:
            # E.g. settings.json links into a read-only store (nix /
            # home-manager). Say what to add by hand and carry on with Codex.
            failed.append(f"Claude Code hook: {CLAUDE_SETTINGS}")
            print(
                f"  - Claude Code hook: NOT installed, could not write "
                f"{os.path.realpath(str(CLAUDE_SETTINGS))} ({e}).\n"
                f"    Add this entry to hooks.PreToolUse in your settings.json source by hand:\n"
                + "\n".join("      " + line for line in json.dumps(
                    {"matcher": CLAUDE_HOOK_MATCHER, "hooks": [handler]}, indent=2).splitlines()),
                file=sys.stderr,
            )
        else:
            if backup:
                print(f"  - Claude Code: previous settings.json saved as {backup} (one-time backup)")
            installed.append(f"Claude Code hook: {CLAUDE_SETTINGS}")
            print(
                f"  - Claude Code hook: {handler['command']} {' '.join(handler['args'])} "
                f"(matcher {CLAUDE_HOOK_MATCHER}, timeout {handler['timeout']}s) in "
                f"{CLAUDE_SETTINGS}  [user-global]"
            )
    else:
        print(f"  - Claude Code: skipped ({CLAUDE_SETTINGS.parent} not found; "
              f"set CLAUDE_CONFIG_DIR if you keep it elsewhere)")

    # 3. Codex CLI PreToolUse hook ($CODEX_HOME/hooks.json).
    codex_hook = False
    if codex_present:
        try:
            codex_hook = _install_codex_hook()
        except OSError as e:
            failed.append(f"Codex CLI hook: {CODEX_HOOKS}")
            print(f"  - Codex CLI hook: NOT installed, could not write "
                  f"{os.path.realpath(str(CODEX_HOOKS))} ({e}).", file=sys.stderr)
        if codex_hook:
            installed.append(f"Codex CLI hook: {CODEX_HOOKS}")
            print(
                f"  - Codex CLI hook: {_hook_command()} (matcher {CODEX_HOOK_MATCHER}) "
                f"in {CODEX_HOOKS}  [user-global]"
            )
    else:
        print(f"  - Codex CLI: hook skipped ({CODEX_HOME} not found; set CODEX_HOME if you keep it elsewhere)")

    if not installed:
        print(
            "\nERROR: nothing was installed. No Claude Code configuration directory "
            f"({CLAUDE_SETTINGS.parent}) and no Codex CLI home ({CODEX_HOME}) was found.\n"
            "  Start Claude Code or Codex once so it creates that directory (or set\n"
            "  CLAUDE_CONFIG_DIR / CODEX_HOME to where you keep it), then re-run install.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 4. Run setup. Always non-interactive in install context: install is
    # the path agents run, and an unexpected prompt would block them mid-flow.
    print()
    cmd_setup([], force_noninteractive=True)

    print("\n=== Install complete (USER-GLOBAL) ===")
    print("Scope: applies to ALL projects, in any directory, for this user.")
    print("Written:")
    for item in installed:
        print(f"  - {item}")
    print(f"Hooks run: {cli}")
    if not shutil.which("overleaf-sync-now"):
        print(
            f"WARN: {pathlib.Path(cli).parent} is not on your PATH. The hooks use the full path,\n"
            "  so they work, but to type `overleaf-sync-now login` (and the other commands) by\n"
            "  name, run `uv tool update-shell` and open a new terminal."
        )
    print()
    # Check if auth resolved during the setup that just ran. If not, surface
    # the fix up front so the user doesn't have to wait for the first sync.
    auth_state, _ = _check_cookies(_load_cache())
    if auth_state == "unreachable":
        print("AUTH NOT CHECKED: Overleaf could not be reached from here (offline or a")
        print("  sandbox without network). Run `overleaf-sync-now status` in a normal terminal")
        print("  to check it; run `overleaf-sync-now login` there if it reports INVALID.")
        print()
    elif auth_state != "ok":
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
    if failed:
        print("\nERROR: not installed (see above): " + "; ".join(failed), file=sys.stderr)
        sys.exit(1)


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


_PROFILE_CHANNEL_FILE = ".overleaf-sync-channel"


def _launch_profile(p, kind, profile_dir, *, headless, channels):
    """Open the persistent `login` profile with the first browser channel
    that starts ("chrome", "msedge", or None = Playwright's Chromium).
    Returns (context, channel) or (None, None)."""
    for channel in channels:
        try:
            kwargs = _build_launch_kwargs(kind, profile_dir, headless=headless, channel=channel)
            return p.chromium.launch_persistent_context(**kwargs), channel
        except Exception:
            continue
    return None, None


def _record_profile_channel(profile_dir, channel):
    try:
        (pathlib.Path(profile_dir) / _PROFILE_CHANNEL_FILE).write_text(channel or "chromium")
    except OSError:
        pass


def _profile_channels(profile_dir):
    """Channel order for reopening the profile: the browser `login` used
    first (a profile is best read by the browser that wrote it), then the
    rest, so no Chromium download is needed when Chrome or Edge exists."""
    order = ["chrome", "msedge", None]
    try:
        used = (pathlib.Path(profile_dir) / _PROFILE_CHANNEL_FILE).read_text().strip()
    except OSError:
        used = ""
    if used in ("chrome", "msedge", "chromium"):
        first = None if used == "chromium" else used
        order.remove(first)
        order.insert(0, first)
    return order


def _ensure_playwright_browser():
    """Download Playwright's Chromium for whichever backend the adapter
    prefers. Idempotent. Only called when neither Chrome nor Edge is
    installed: about 300 MB (Chromium plus its headless shell), stored in the
    Playwright cache (see _playwright_cache_dir).

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
        f"[overleaf-sync-now] Chrome and Edge could not be started, so downloading Chromium via "
        f"{kind} (one-time, about 300 MB, into {_playwright_cache_dir()})...",
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
    automatic on-disk cookie extraction fails (Chrome 127+ app-bound encryption,
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
        # Prefer the installed Chrome or Edge (no download); fetch Playwright's
        # Chromium only when neither starts.
        ctx, channel = _launch_profile(p, kind, profile_dir, headless=False, channels=("chrome", "msedge"))
        if not ctx:
            _ensure_playwright_browser()
            ctx, channel = _launch_profile(p, kind, profile_dir, headless=False, channels=(None,))
        if not ctx:
            print("ERROR: Could not launch any browser. Make sure Chrome, Edge, or Chromium is installed.", file=sys.stderr)
            sys.exit(1)
        _record_profile_channel(profile_dir, channel)

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
        print("terminal and paste your overleaf_session2 value (browser developer tools > Cookies)", file=sys.stderr)
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
            ctx, _ = _launch_profile(p, kind, profile_dir, headless=True,
                                     channels=_profile_channels(profile_dir))
            if ctx is None:
                raise RuntimeError("no browser could open the login profile")
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
    f"Get the value from your browser: {_DEVTOOLS_STEPS}\n"
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
    state, exc = _check_cookies(cookies, use_cache=False)
    if state == "unreachable":
        print(
            f"ERROR: could not reach Overleaf to check this cookie, so nothing was saved.\n"
            f"  {_network_unavailable_message(exc)}\n"
            f"  Run `overleaf-sync-now save-cookie` again once Overleaf is reachable.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state != "ok":
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
    net_down = None  # set when [1] could not reach Overleaf
    if cached:
        names = sorted(cached.keys())
        has_session = SESSION_COOKIE in cached
        print(f"[1] Cached cookies: {len(names)} cookie(s); {SESSION_COOKIE}: {'yes' if has_session else 'no'}")
        if has_session:
            print(f"    -> Validating against Overleaf (forced fresh probe)...")
            # Doctor MUST do a real network check, not trust the cached
            # 'validated-at' timestamp. Otherwise it would just print 'OK'
            # for any cookie we touched in the last minute.
            state, exc = _check_cookies(cached, use_cache=False)
            if state == "ok":
                print("    -> Result: OK")
            elif state == "unreachable":
                net_down = NetworkUnavailable(_network_unavailable_message(exc))
                print(f"    -> Result: UNKNOWN (could not reach Overleaf: {_network_detail(exc)})")
                print(f"       {_network_unavailable_message(exc)}")
            else:
                print("    -> Result: INVALID (rejected by /project)")
    else:
        print("[1] Cached cookies: NONE")
    print()

    # Each browser's cookie store is read once per reader here ([2a], [2b]);
    # [4] reuses what they found instead of reading the stores again, so on
    # macOS a browser asks for keychain access at most once per reader.
    found = []
    if net_down is not None:
        print("[2] Browser cookie stores: skipped (Overleaf could not be reached in [1], so a")
        print("    cookie found there could not be checked; on macOS reading them can ask for")
        print("    keychain access). Re-run doctor when the connection is back.")
        print()
    rookie_mod = bc3_mod = None
    if net_down is None:
        try:
            import rookiepy as rookie_mod
        except ImportError:
            print("[2a] rookiepy: not installed (recommended on Windows for Chrome 127+; `pip install rookiepy`)")
            print()
        try:
            import browser_cookie3 as bc3_mod
        except ImportError:
            print("[2b] browser_cookie3: NOT INSTALLED — run `pip install browser-cookie3` or reinstall")
            print()

    # Rookie (Chrome 127+ friendly)
    if rookie_mod is not None:
        rookiepy = rookie_mod
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
                    found.append((f"rookiepy {fn_name}", cookies))
                else:
                    print(f"     {fn_name:10s}: no overleaf.com cookies (not logged in here?)")
            except Exception as e:
                print(f"     {fn_name:10s}: error - {type(e).__name__}: {e}")
        print()

    if bc3_mod is not None:
        browser_cookie3 = bc3_mod
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
                    found.append((f"browser_cookie3 {fn_name}", cookies))
                else:
                    print(f"    {fn_name:10s}: no overleaf.com cookies (not logged in here?)")
            except Exception as e:
                print(f"    {fn_name:10s}: error - {type(e).__name__}: {e}")
        print()

    pw = _try_playwright_profile()
    if pw and SESSION_COOKIE in pw:
        found.append(("Claude Code Playwright profile", pw))
        print(f"[3] Playwright profile: {SESSION_COOKIE} found")
    elif PLAYWRIGHT_PROFILE.exists():
        print("[3] Playwright profile: present but no usable overleaf.com cookies")
    else:
        print("[3] Playwright profile: not installed (this is fine; only needed if browser_cookie3 fails)")
    print()

    # Best-available cookies: cached cookie and `login` profile, then what
    # [2a]/[2b]/[3] found (no second read of the browser stores).
    best, unreachable = None, net_down
    if unreachable is None:
        try:
            best = _resolve_cookies(interactive=False, browsers=False)
        except NetworkUnavailable as e:
            unreachable = e
    if best is None and unreachable is None:
        for _label, cookies in found:
            state, exc = _check_cookies(cookies, use_cache=False)
            if state == "ok":
                _save_cache(cookies)
                best = cookies
                break
            if state == "unreachable":
                unreachable = NetworkUnavailable(_network_unavailable_message(exc))
                break
    if unreachable is not None:
        print("[4] Auth resolution: UNKNOWN (could not reach Overleaf; this is not an auth failure).")
        print(f"    {unreachable}")
    elif best:
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
    elif unreachable is not None:
        print("    Skipped (Overleaf could not be reached; see [4])")
    else:
        print(f"    Skipped (auth resolution failed above; fix auth first)")
    print()
    print("[6] Install health:")
    _print_install_health(indent="    ")


def _playwright_cache_dir():
    """Where `login` downloaded its browser (shared with other Playwright
    tools): $PLAYWRIGHT_BROWSERS_PATH, else the per-OS default."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        return pathlib.Path(env).expanduser()
    if sys.platform == "darwin":
        return HOME / "Library" / "Caches" / "ms-playwright"
    if os.name == "nt":
        return pathlib.Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local")) / "ms-playwright"
    return pathlib.Path(os.environ.get("XDG_CACHE_HOME") or (HOME / ".cache")) / "ms-playwright"


def _overleaf_roots(extra=()):
    """Apps/Overleaf folders to look for markers in: the usual Dropbox
    locations plus any folders given on the command line."""
    bases = [HOME / "Dropbox"]
    for pattern in ("Dropbox (*)", "Library/CloudStorage/Dropbox*"):
        try:
            bases += sorted(HOME.glob(pattern))
        except OSError:
            pass
    roots = [b / "Apps" / "Overleaf" for b in bases]
    roots += [pathlib.Path(x).expanduser() for x in extra]
    seen, out = set(), []
    for r in roots:
        try:
            key = r.resolve()
        except OSError:
            continue
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def _find_markers(roots):
    """`.overleaf-project` files in each root and its direct subfolders (auto-
    link writes them at Apps/Overleaf/<project>/)."""
    found = []
    for r in roots:
        candidates = [r / PROJECT_MARKER]
        try:
            candidates += [c / PROJECT_MARKER for c in sorted(r.iterdir()) if c.is_dir()]
        except OSError:
            pass
        for m in candidates:
            try:
                if m.is_file() and "project_id" in m.read_text(encoding="utf-8", errors="replace"):
                    found.append(m)
            except OSError:
                pass
    return found


def cmd_uninstall(args):
    """uninstall [--remove-markers] [FOLDER ...]: remove our skill copies and
    hooks, then list what stays behind (project markers, the data dir with the
    cached session, the browser download) and how to remove it. FOLDERs are
    extra Apps/Overleaf (or project) folders to search for markers."""
    remove_markers = "--remove-markers" in args
    extra = [a for a in args if not a.startswith("--")]
    print("=== Uninstalling overleaf-sync-now skill and hooks ===")
    for tool, target in list(SKILL_TARGETS.items()) + list(LEGACY_SKILL_TARGETS.items()):
        if target.exists() or target.is_symlink():
            if _remove_existing_target(target):
                print(f"  - {tool}: removed {target}")
    if CLAUDE_SETTINGS.exists():
        settings, ok = _read_json_config(CLAUDE_SETTINGS, str(CLAUDE_SETTINGS))
        if not ok:
            print("  WARN: skipping Claude Code hook removal.", file=sys.stderr)
        else:
            hooks_root = settings.get("hooks")
            if isinstance(hooks_root, dict) and "PreToolUse" in hooks_root:
                cleaned, removed = _strip_our_hooks(hooks_root.get("PreToolUse", []))
                if removed:
                    if cleaned:
                        hooks_root["PreToolUse"] = cleaned
                    else:
                        hooks_root.pop("PreToolUse", None)
                    _write_json_config(CLAUDE_SETTINGS, settings)
                    print(f"  - Claude Code hook removed from {CLAUDE_SETTINGS}")
    if _uninstall_codex_hook():
        print(f"  - Codex CLI hook removed from {CODEX_HOOKS}")

    markers = _find_markers(_overleaf_roots(extra))
    if remove_markers:
        for m in markers:
            try:
                m.unlink()
                print(f"  - removed marker {m}")
            except OSError as e:
                print(f"  WARN: could not remove {m}: {e}", file=sys.stderr)
        markers = [m for m in markers if m.exists()]

    print()
    print("Left in place (remove what you no longer want):")
    if markers:
        print(f"  - {len(markers)} project marker(s). Each syncs into its Overleaf project through")
        print("    Dropbox, so collaborators see it too:")
        for m in markers[:20]:
            print(f"      {m}")
        if len(markers) > 20:
            print(f"      ... and {len(markers) - 20} more")
        print("    Remove them with `overleaf-sync-now uninstall --remove-markers` (or delete the files).")
    else:
        print("  - No .overleaf-project markers found in the usual Dropbox locations. If your")
        print("    Dropbox folder is elsewhere, run `overleaf-sync-now uninstall <Apps/Overleaf folder>`.")
    if CACHE_DIR.exists():
        print(f"  - Data dir {CACHE_DIR}: the cached Overleaf session (cookies.json), the `login`")
        print("    browser profile, backups/ and conflicts/ copies of your files, and sync state.")
        print(f"    Remove it: rm -r \"{CACHE_DIR}\"   (Windows: Remove-Item -Recurse \"{CACHE_DIR}\")")
    backups = [CLAUDE_SETTINGS.with_name(CLAUDE_SETTINGS.name + SETTINGS_BACKUP_SUFFIX)]
    try:
        backups += sorted(CODEX_HOOKS.parent.glob(CODEX_HOOKS.name + ".bak-*"))
    except OSError:
        pass
    backups = [b for b in backups if b.exists()]
    if backups:
        print("  - Config backups made by install/uninstall. They still contain the old hook")
        print("    entry, so restoring one brings the hook back. Remove them with:")
        for b in backups:
            print(f"      rm \"{b}\"")
    pw = _playwright_cache_dir()
    if pw.exists():
        print(f"  - Browser downloaded by `login`: {pw} (shared with any other Playwright tool;")
        print("    remove it only if nothing else uses it).")
    print("  - The Overleaf session itself stays valid on Overleaf's servers until it expires;")
    print("    deleting files does not end it. To end it: on overleaf.com open Account >")
    print("    Account Settings > Sessions > Manage your sessions, then Clear Sessions (it ends")
    print("    every session except the one in the browser you click it in).")
    print("  - The CLI: uv tool uninstall overleaf-sync-now")


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
