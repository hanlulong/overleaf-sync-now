---
name: overleaf
description: "Refresh local .tex/.bib files against Overleaf before AI edits, so the agent never edits a stale copy (default Overleaf-to-Dropbox sync lags 10-20 min). Since 0.1.0 the refresh path is: probe `/project/<id>/updates` (read-only), skip if nothing changed, skip if all changes were our own Dropbox round-trips, else download-zip and extract only the web-origin changed files. WHEN: (1) before editing .tex/.bib/.cls/.sty/.bst under Apps/Overleaf/<project>/ run `overleaf-sync-now sync`; (2) on user request to refresh; (3) for first-time auth run `overleaf-sync-now login`. Claude Code: a PreToolUse hook auto-runs sync; manual invocation rarely needed. Codex CLI: invoke sync explicitly. AUTH RECOVERY when sync/setup fails: run `overleaf-sync-now login` (browser-assisted, works on Chrome 130+). Do NOT tell the user to 'log into Overleaf' in their daily browser — on Chrome 130+ app-bound encryption blocks on-disk cookie extraction regardless of login state. See body for full recovery flow."
argument-hint: "setup | sync [folder] [--force] [--legacy] | status [folder] | link <project_id> [folder] | install | save-cookie <value> | doctor"
user-invocable: true
---

# overleaf-sync-now

## ⚠ STOP — read this before suggesting "log in to Overleaf"

If the user is on Windows with Chrome 130 or later, **logging into Overleaf in their daily browser will NOT fix auth failures.** Chrome 130 added App-Bound Encryption: the cookie database AES key is wrapped a second time with a key bound to `Chrome.exe` itself, brokered through Windows COM. Any process that isn't Chrome.exe (and isn't running as admin) can read the encrypted bytes but can't decrypt them. So `browser_cookie3` and `rookiepy` fail regardless of how recently the user logged in.

**The proper fix is `overleaf-sync-now login`.** It launches a controlled browser (Playwright-managed Chromium), the user logs in there once, and we read the cookie via Chrome DevTools Protocol — which returns plaintext because the request comes from inside the browser. The login persists in our profile for weeks.

## The problem

Overleaf's Dropbox bridge polls in one direction (Overleaf → Dropbox) every 10–20 minutes, so a local Dropbox-mirrored `.tex` file can be stale by that long. This skill fixes it by probing Overleaf's version history (`GET /project/{id}/updates`) and, on an actual web-origin change, downloading the project zip (`GET /project/{id}/download/zip`) and extracting only the files that changed — without changing the user's existing Dropbox-bridge setup, so cross-device Dropbox sync still works as before.

Before 0.1.0 the skill used `POST /project/{id}/dropbox/sync-now`, but that enqueued a heavy per-user "poll Dropbox" job in Overleaf's serialized tpdsworker queue, starving local→Overleaf propagation. The new version-match path never touches that queue.

## How project lookup works

- **Auto-link** (zero-config, preferred): if a file lives under any `…/Apps/Overleaf/<project-name>/`, the skill looks up `<project-name>` in the user's Overleaf project list (cached 24h) and uses its ID. No marker file needed.
- **Manual link** (override): a `.overleaf-project` JSON file containing `{"project_id": "..."}` in any folder takes priority. Use this when the local folder name doesn't match the Overleaf project name.

## Auth — DO NOT ask the user which method to use

The script handles auth itself. It tries (in this exact order, automatically): cached cookies → `rookiepy` (Chrome 127+ friendly) → `browser_cookie3` → Claude Code's Playwright profile → manual paste prompt.

**Just run `overleaf-sync-now setup` (or `install`).** Do not ask the user "do you want auto-detect or manual paste?" — the script picks the best available source.

## When auth fails (the recovery flow you should use)

When `setup` reports "AUTO-DETECT FAILED" or `sync` returns a "No valid Overleaf cookies" error, **the proper recovery is `overleaf-sync-now login`** (browser-assisted, works on every platform including Chrome 130+ Windows). Don't suggest the user "log in" in their daily browser — on Chrome 130+ Windows that doesn't help.

**Recovery flow:**

1. Run `overleaf-sync-now doctor` to confirm which automatic sources failed.
2. Tell the user: *"Run `overleaf-sync-now login` in your terminal. A browser window will open. Log into Overleaf there. We'll capture the cookie via the browser's API."*
3. (On first run, this auto-installs Playwright + Chromium, ~150MB, one-time.) The browser opens to overleaf.com, the user logs in, and the script captures the cookie via Chrome DevTools Protocol (no on-disk decryption needed). The login persists in the profile.
4. After they confirm login finished, run `overleaf-sync-now status` to verify, then retry the original sync.

**Fallback for environments where `login` can't run** (no display, server, etc.): `save-cookie <value>`. Ask the user to copy their `overleaf_session2` value from F12 → Application → Cookies → overleaf.com, then run `overleaf-sync-now save-cookie "<value>"`. Don't rely on this when `login` is available — `login` is more reliable.

## Subcommands

The CLI is `overleaf-sync-now` (installed globally via `uv tool install`).

### `setup`
Auth wizard. Walks the auto-detect chain; in interactive mode also prompts for manual paste. Run once; cached for weeks afterward.

### `login`
Browser-assisted login (Playwright + Chromium). The proper fix when `setup` can't auto-detect — including the Chrome 130+ app-bound encryption case on Windows. See the recovery flow above.

### `save-cookie <value>`
Last-resort: persist an `overleaf_session2` cookie value pasted from the browser's F12 → Application → Cookies pane. Use only when `login` can't run (no display / CI / server).

### `sync [folder] [--force] [--legacy]`
Refresh the project that owns `folder` (or current dir) against Overleaf.

- **Default path** (0.1.0+): probe `/project/<id>/updates`; skip if no change or if all new updates are Dropbox-origin round-trips of our own saves; otherwise download the zip and extract only the files whose web-origin updates aren't yet on local. No Dropbox-queue load, no 10-second wait.
- `--force`: always download the zip and re-extract. Hash-compare still skips files whose bytes already match local. Also disables the 30-second recent-mtime guard (see below).
- `--legacy`: fall back to `POST /dropbox/sync-now` + 10-second Dropbox settle. Kept as an escape hatch; prefer the default — the legacy path pollutes Overleaf's per-user `tpdsworker` queue, which can slow down *local → Overleaf* propagation.

Data-safety: by default, `sync` refuses to overwrite a local file modified within the last 30 seconds, and prints `SKIP <path>` to stderr. This protects an in-progress local save that hasn't yet propagated Dropbox → Overleaf. Pass `--force` to override.

When to invoke: manually when the user says "pull latest from Overleaf," or automatically before editing in Codex CLI (PreToolUse hooks aren't reliable on Windows in Codex).

### `status [folder]`
Reports data dir, cookie validity, linked project, last-sync time, and cached Overleaf version (`toV`). Distinguishes sandbox-blocked network from real auth failures.

### `projects [--refresh]`
List the user's Overleaf projects (name + ID). `--refresh` forces re-fetch of the index.

### `doctor [folder]`
Diagnostic dump: cookie cache state, per-browser cookie extraction, Playwright profile, auth-chain resolution, and a live `/updates` probe against the given folder's linked project.

### `link <project_id> [folder]`
Writes a `.overleaf-project` marker. Only needed for non-standard folder layouts (i.e., not `Apps/Overleaf/<name>/`).

### `hook`
PreToolUse hook entrypoint for Claude Code. Reads JSON from stdin. **Not for manual use.**

### `install`
Idempotent post-install setup: copies `SKILL.md` into `~/.claude/skills/overleaf/` and `~/.codex/skills/overleaf/` (whichever exists), installs / updates the Claude Code PreToolUse hook (matcher `Read|Edit|Write|MultiEdit`), runs the auth chain. Re-run after upgrading to 0.1.0 so the expanded matcher takes effect.

### `uninstall`
Removes skill installs and the hook. Cookies are preserved.

## Sandbox notes (Codex CLI, restricted shells)

If any network-using subcommand (`sync`, `status`, `login`, `doctor`, `setup`) fails with a socket-permission error — **Windows** `WinError 10013` / `forbidden by its access permissions`, **POSIX** `EACCES` / `EPERM` / "Permission denied" — the host shell is blocking the outbound HTTPS call. **This is not an auth problem.**

0.1.1+ detects this case specifically and prints:

> Outbound HTTPS to Overleaf was blocked by the host environment (likely a sandboxed shell — Codex CLI, some CI runners). Auth is probably fine; running setup/login/doctor will fail the same way. Approve the `overleaf-sync-now` command prefix in your sandbox policy, or re-run outside the sandbox.

When you see that message (or any of the above errno markers):
- Do **not** run `setup` / `login` / `doctor` — each hits the same blocked socket.
- Tell the user to approve the `overleaf-sync-now` command prefix in their sandbox (Codex CLI's always-approve list), or re-run from an unsandboxed shell.
- `status` will print `Cookie auth: UNKNOWN — outbound HTTPS is blocked ...` rather than `INVALID` in this case, so you don't have to guess.

## When to invoke this skill

- **In Claude Code**: rarely — the hook handles editing automatically. Invoke `sync` only on explicit user request, or `setup`/`link` for first-time configuration.
- **In Codex CLI on Windows**: every time you're about to Edit/Write a `.tex`/`.bib`/`.cls`/`.sty`/`.bst` file under `Apps/Overleaf/`, invoke `sync` first. (Codex hooks don't reliably fire pre-Edit on Windows.)
- **User mentions stale Overleaf content**: invoke `sync`.
- **User asks to set up Overleaf sync**: invoke `setup` (or full `install` if not yet linked into Claude/Codex skill dirs).

## Failure modes & recovery

- **"No valid Overleaf cookies found. Run setup"**: cookies expired or never set. Run `overleaf-sync-now setup`. The chain will try cached → browsers → Playwright; falls through to manual paste in interactive mode.
- **`setup` can't find browser cookies**: user may not be logged into overleaf.com in Chrome/Edge/Firefox. Either log in there, or paste cookie manually when prompted.
- **`sync` returns "Project page returned HTTP 302"**: cookies invalidated. Run `setup` again.
- **Auto-link can't find the project**: folder name doesn't match Overleaf project name. Use `link <project_id> .` inside the folder.

## Security note

Cached cookies grant full access to the user's Overleaf account. Stored at `~/.overleaf-sync/cookies.json` with default user permissions. Don't commit, don't share.
