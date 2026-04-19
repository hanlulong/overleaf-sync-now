---
name: overleaf
description: "Trigger Overleaf's server-side Dropbox sync on demand so local .tex/.bib files are fresh before AI edits (default Overleaf-to-Dropbox sync lags 10-20 min). WHEN: (1) before editing .tex/.bib/.cls/.sty/.bst under Apps/Overleaf/<project>/ run `overleaf-sync-now sync`; (2) on user request to refresh; (3) for first-time auth run `overleaf-sync-now login`. Claude Code: a PreToolUse hook auto-runs sync; manual invocation rarely needed. Codex CLI: invoke sync explicitly. AUTH RECOVERY when sync/setup fails: run `overleaf-sync-now login` (browser-assisted, works on Chrome 130+). Do NOT tell the user to 'log into Overleaf' in their daily browser — on Chrome 130+ app-bound encryption blocks on-disk cookie extraction regardless of login state. See body for full recovery flow."
argument-hint: "setup | sync [folder] | status [folder] | link <project_id> [folder] | install | save-cookie <value> | doctor"
user-invocable: true
---

# overleaf-sync-now

## ⚠ STOP — read this before suggesting "log in to Overleaf"

If the user is on Windows with Chrome 130 or later, **logging into Overleaf in their daily browser will NOT fix auth failures.** Chrome 130 added App-Bound Encryption: the cookie database AES key is wrapped a second time with a key bound to `Chrome.exe` itself, brokered through Windows COM. Any process that isn't Chrome.exe (and isn't running as admin) can read the encrypted bytes but can't decrypt them. So `browser_cookie3` and `rookiepy` fail regardless of how recently the user logged in.

**The proper fix is `overleaf-sync-now login`.** It launches a controlled browser (Playwright-managed Chromium), the user logs in there once, and we read the cookie via Chrome DevTools Protocol — which returns plaintext because the request comes from inside the browser. The login persists in our profile for weeks.

## The problem

Overleaf's Dropbox bridge polls in one direction (Overleaf → Dropbox) every 10–20 minutes, so a local Dropbox-mirrored `.tex` file can be stale by that long. This skill calls Overleaf's internal "Sync this project now" endpoint (`POST /project/{id}/dropbox/sync-now`) so the local file is fresh within seconds — without changing the user's existing Dropbox-bridge setup, so cross-device Dropbox sync still works as before.

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

The CLI is `overleaf-sync-now` (installed globally via `uv tool install` or `pip install`).

### `setup`
Runs the auth wizard. Walks the auto-detect chain; in interactive mode also prompts for manual paste. Run once; cached for weeks afterward.

### `sync [folder]`
Triggers Overleaf-side sync for the project that owns `folder` (or current dir). Waits ~10s for Dropbox to settle. Use this:
- Manually when the user says "I edited on Overleaf, pull please".
- Automatically before editing in Codex CLI (where PreToolUse hooks don't apply).

### `status [folder]`
Reports the linked project ID and time since last triggered sync.

### `link <project_id> [folder]`
Writes a `.overleaf-project` marker. Only needed for non-standard folder layouts (i.e., not `Apps/Overleaf/<name>`).

### `hook`
PreToolUse hook entrypoint for Claude Code. Reads JSON from stdin. **Not for manual use.**

### `install`
Idempotent post-install setup: copies SKILL.md into `~/.claude/skills/overleaf/` and `~/.codex/skills/overleaf/` (whichever exists), adds the Claude Code PreToolUse hook, runs the auth chain. Run after `uv tool install`.

### `uninstall`
Removes skill installs and the hook. Cookies are preserved.

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
