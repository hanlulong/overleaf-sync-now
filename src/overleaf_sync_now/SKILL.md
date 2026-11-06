---
name: overleaf
description: "Pull the latest Overleaf web edits into a local Dropbox-synced Overleaf folder before an AI agent reads or edits it, so the agent never works on a stale .tex/.bib copy (Overleaf's Dropbox sync lags 10-20 minutes). Use when reading or editing .tex/.bib/.cls/.sty/.bst files under Apps/Overleaf/PROJECT/, when the user asks to pull latest from Overleaf or sync Overleaf, mentions a stale or out-of-date Overleaf or Dropbox copy, or wants to set up Overleaf Dropbox sync for an AI agent. Claude Code and Codex CLI run the refresh automatically through a PreToolUse hook; run `overleaf-sync-now sync` by hand when no hook is active. When auth fails, the user runs `overleaf-sync-now login` in their own terminal; never ask them to paste the Overleaf session cookie into the chat."
argument-hint: "sync [folder] [--force] | status [folder] | link <project_id> [folder] | install | login | setup | doctor"
user-invocable: true
---

# overleaf-sync-now

## ⚠ STOP — read this before suggesting "log in to Overleaf"

If the user is on Windows with Chrome 130 or later, **logging into Overleaf in their daily browser will NOT fix auth failures.** Chrome 130 added App-Bound Encryption: the cookie database AES key is wrapped a second time with a key bound to `Chrome.exe` itself, brokered through Windows COM. Any process that isn't Chrome.exe (and isn't running as admin) can read the encrypted bytes but can't decrypt them. So `browser_cookie3` and `rookiepy` fail regardless of how recently the user logged in.

**The proper fix is `overleaf-sync-now login`.** It launches a controlled browser (patchright-managed Chromium since 0.3.2; vanilla Playwright fallback), the user logs in there once, and we read the cookie via Chrome DevTools Protocol — which returns plaintext because the request comes from inside the browser. The login persists in our profile for weeks.

## The problem

Overleaf's Dropbox bridge polls in one direction (Overleaf → Dropbox) every 10–20 minutes, so a local Dropbox-mirrored `.tex` file can be stale by that long. This skill fixes it by probing Overleaf's version history (`GET /project/{id}/updates`) and, on an actual web-origin change, downloading the project zip (`GET /project/{id}/download/zip`) and extracting only the files that changed — without changing the user's existing Dropbox-bridge setup, so cross-device Dropbox sync still works as before.

## How project lookup works

- **Auto-link** (zero-config, preferred): if a file lives under any `…/Apps/Overleaf/<project-name>/`, the skill looks up `<project-name>` in the user's Overleaf project list (cached 24 h, projects.json v2 since 0.3.0). The cached list keeps `trashed`/`archived`/`lastUpdated`/`ownerId`, so the resolver filters out trashed and archived projects by default and refuses to guess when two living projects share a name.
- **Marker auto-write** (since 0.3.0): on a successful auto-link the resolver writes a tiny `.overleaf-project` JSON marker into the project folder so future resolution skips the index lookup entirely. The marker carries `project_id` + provenance (`source`, `linked_at`, `name_at_link_time`).
- **Manual link** (override): if the user has two living projects with the same name (or the local folder name doesn't match), the resolver refuses to guess and prints all candidates with the exact `overleaf-sync-now link <id> <folder>` command to copy-paste. Run that command to write the marker explicitly. Old marker files containing only `{"project_id": "..."}` keep working. A marker whose `project_id` is not 24 hex characters is ignored (since 0.5.0).
- **Fail closed** (since 0.5.0): when a folder under `Apps/Overleaf/<name>/` can't be resolved (not logged in, no project with that name, or several), the hook **blocks** writes (exit 2) and only warns on reads. See "When the hook blocks" below.

### When `sync` warns or refuses

- **`auto-link: N non-trashed Overleaf projects named <name>. Refusing to guess.`** — Two or more live projects share the folder name. The skill lists candidates with IDs, last-updated timestamps, and any archived flag. Tell the user to pick one and run the printed `link` command. *Do NOT pick a project ID for them.*
- **`WARN: marker at <folder> -> <id> is trashed/archived on Overleaf`** — User explicitly linked a stale project. Sync proceeds but the user probably wants `overleaf-sync-now link <other_id> <folder>` to relink to the active project.
- **`WARN: project <id> has recent Dropbox-origin updates touching files not present in <folder>`** — Strong signal the link points at the wrong project entirely (a typo'd manual link, a fork, or a same-name duplicate that earlier defenses didn't catch). Have the user run `overleaf-sync-now status` and `overleaf-sync-now projects` to verify, then `link` to fix.

## Auth — DO NOT ask the user which method to use

The script handles auth itself. It tries (in this exact order, automatically): cached cookies → persistent `login` browser profile → `rookiepy` (Chrome 127+ friendly) → `browser_cookie3` → Claude Code's Playwright MCP profile → (interactive `setup` only) a hidden terminal prompt the user types into.

**Just run `overleaf-sync-now setup` (or `install`).** It reports which source worked. Do not ask the user which method to use — the script picks the best available source.

## When auth fails (the recovery flow you should use)

When `setup` reports "AUTO-DETECT FAILED" or `sync` returns a "No valid Overleaf cookies" error, **the proper recovery is `overleaf-sync-now login`** (browser-assisted, works on every platform including Chrome 130+ Windows). Don't suggest the user "log in" in their daily browser — on Chrome 130+ Windows that doesn't help.

**Recovery flow:**

1. Run `overleaf-sync-now doctor` to confirm which automatic sources failed.
2. Tell the user: *"Run `overleaf-sync-now login` in your terminal. A browser window will open. Log into Overleaf there. We'll capture the cookie via the browser's API."*
3. (On first run, this auto-installs Playwright + Chromium, ~150MB, one-time.) The browser opens to overleaf.com, the user logs in, and the script captures the cookie via Chrome DevTools Protocol (no on-disk decryption needed). The login persists in the profile.
4. After they confirm login finished, run `overleaf-sync-now status` to verify, then retry the original sync.

**Fallback only where `login` can't open a browser** (server, SSH, no display): tell the user to run `overleaf-sync-now save-cookie` **in their own terminal**. It prompts for the `overleaf_session2` value with hidden input (or reads it from stdin with `save-cookie -`). Don't run it for them, and don't use it when `login` is available.

**Never ask the user to paste the session cookie into the chat, and never put it on a command line.** The cookie grants full access to their Overleaf account; pasted into a chat it lands in the model provider's transcript and in local session logs, and as an argument it shows up in the process list and shell history. If the user pastes a cookie into the chat anyway, do not repeat it or use it. Suggest they sign out of Overleaf in the browser it came from (that ends the session on Overleaf's side) and run `overleaf-sync-now login`.

### If Google blocks the `login` browser ("This browser or app may not be secure")

If the user signs into Overleaf via **"Sign in with Google"** and the managed `login` browser hits Google's *"This browser or app may not be secure"* page, that's Google's anti-automation gate (`disallowed_useragent`). The `login` command since 0.3.2 detects this and prints a tailored recovery message. The supported escape hatch is to give the user's Overleaf account an email+password alternate login:

1. Open https://www.overleaf.com/user/password/reset in any normal browser.
2. Enter the Overleaf account email; Overleaf emails a password-set link.
3. Set a password (their Google sign-in still works elsewhere; this just adds email+password).
4. Re-run `overleaf-sync-now login` and use **email+password** in the Overleaf form. Google never sees this flow.

Or, on a machine with no browser, the user runs `overleaf-sync-now save-cookie` in their own terminal. **Do not** suggest fighting Google's detection further — patchright already does the best-available stealth, and Google iterates against bypasses.

## Subcommands

The CLI is `overleaf-sync-now` (installed globally via `uv tool install`).

### `setup`
Auth wizard. Walks the same auto-detect chain as `sync` and says which source worked; in an interactive terminal it can also prompt (hidden input) for a cookie the user types. Run once; cached for weeks afterward.

### `login`
Browser-assisted login (Playwright + Chromium). The proper fix when `setup` can't auto-detect — including the Chrome 130+ app-bound encryption case on Windows. See the recovery flow above.

### `save-cookie [-]`
Last resort, run by the **user** in their own terminal: saves an `overleaf_session2` value copied from the browser's F12 → Application → Cookies pane, read from a hidden prompt (no argument) or stdin (`-`). Use only when `login` can't run (no display / CI / server). A value passed as an argument still works but prints a warning.

### `sync [folder] [--force]`
Refresh the project that owns `folder` (or current dir) against Overleaf.

- Probes `/project/<id>/updates`; skips if no change or if all new updates are Dropbox-origin round-trips of our own saves; otherwise downloads the zip and extracts only the files whose web-origin updates aren't yet on local.
- `--force`: always download the zip and re-extract. Hash-compare still skips files whose bytes already match local. Also disables the 30-second recent-mtime guard (see below).

Data-safety: by default, `sync` refuses to overwrite a local file modified within the last 30 seconds, and prints `SKIP <path>` to stderr. This protects an in-progress local save that hasn't yet propagated Dropbox → Overleaf. When a protected file *also* had a web-origin change in the pulled zip, `sync` does **not** advance the cached version — it reports `version held at <toV>` and re-offers that web change on the next sync (once the local file ages out of the 30 s window), so the web edit is never silently dropped. Pass `--force` to overwrite local with the Overleaf copy immediately.

When to invoke: when the user says "pull latest from Overleaf", or before editing when no hook is active (see "When to invoke this skill"). Web-side file adds and renames are pulled too; a file deleted or renamed on the web is never deleted locally, but edits to the old path are blocked (deleting it is allowed).

### `status [folder]`
Reports data dir, cookie validity, linked project, last-sync time, and cached Overleaf version (`toV`). Distinguishes sandbox-blocked network from real auth failures.

### `projects [--refresh]`
List the user's Overleaf projects (name + ID). `--refresh` forces re-fetch of the index.

### `doctor [folder]`
Diagnostic dump: cookie cache state, per-browser cookie extraction, Playwright profile, auth-chain resolution, and a live `/updates` probe against the given folder's linked project.

### `link <project_id> [folder]`
Writes a `.overleaf-project` marker. Only needed for non-standard folder layouts (i.e., not `Apps/Overleaf/<name>/`).

### `hook`
PreToolUse hook entrypoint for Claude Code and Codex CLI. Reads the tool-call JSON from stdin. **Not for manual use.**

### `install`
Idempotent post-install setup. Copies `SKILL.md` into `~/.claude/skills/overleaf/` and `~/.agents/skills/overleaf/` (the Codex CLI user skills folder) and removes an old copy of ours from `~/.codex/skills/overleaf/` (where 0.4.x put it; a second copy makes Codex list the skill twice). It never deletes a folder holding a different skill. It writes the Claude Code hook (exec form, matcher `Read|Edit|Write|Bash|PowerShell`, timeout 180 s) and a Codex CLI hook in `~/.codex/hooks.json` (matcher `apply_patch|Bash`), then runs the auth chain. Codex only runs a new or changed hook after the user trusts it with `/hooks`. Re-run after upgrading.

### `uninstall`
Removes our skill copies and both hooks. Cookies are preserved.

## When the hook blocks (exit 2) and what to do

The hook runs before Read / Edit / Write / Bash / PowerShell (Claude Code) and apply_patch / Bash (Codex) calls that touch `.tex` / `.bib` / `.cls` / `.sty` / `.bst` files under an Overleaf folder. Reads are never blocked; they only get a warning. Writes are blocked in these cases:

| Message starts with | What to do |
|---|---|
| `No valid Overleaf cookies` / `not logged in to Overleaf` | Ask the user to run `overleaf-sync-now login` in their own terminal, then retry. Never ask for the cookie in chat. |
| `... N living Overleaf projects are named ...` | Show the user the candidate ids and ask which one this folder is. Then run the printed `overleaf-sync-now link <project_id> "<folder>"`. Do not pick one yourself. |
| `... no living ... Overleaf project is named ...` | The folder name doesn't match a project (renamed, trashed, or a different account). Ask the user for the project id from the Overleaf URL, then run `link`. |
| `Refreshed from Overleaf: newer web edits were just pulled into ...` | The file changed on the web since you last saw it. Re-read it, redo the edit on the new content, and retry. |
| `Conflict: ... changed on Overleaf while a local save from the last 30 s ...` | Both sides changed the file within seconds. Wait about 30 seconds, re-read it, check that both the web edit and the recent local change are there, then redo the edit. Tell the user if it repeats. |
| `... was renamed on Overleaf to NEW` | Edit `NEW` instead (it was pulled locally). You may delete the old local copy. |
| `... was deleted on Overleaf` | Ask the user. If it should stay deleted, delete the local copy (allowed). If it should come back, the user re-creates it on Overleaf first. |

Network or sandbox failures and HTTP 429 never block; the call proceeds with a warning.

## Sandbox notes (Codex CLI, restricted shells)

If a network-using subcommand (`sync`, `status`, `login`, `doctor`, `setup`) fails with a socket-permission error — **Windows** `WinError 10013` / `forbidden by its access permissions`, **POSIX** `EACCES` / `EPERM` / "Permission denied" on the connection — the host shell is blocking outbound HTTPS. **This is not an auth problem.** The tool prints `Outbound HTTPS to Overleaf was blocked by the host environment ...` and `status` prints `Cookie auth: UNKNOWN — outbound HTTPS is blocked ...`.

When you see that:
- Do **not** run `setup` / `login` / `doctor` — each hits the same blocked socket.
- Tell the user to approve the `overleaf-sync-now` command prefix in their sandbox (Codex CLI's always-approve list), or re-run from an unsandboxed shell.

## When to invoke this skill

- **In Claude Code**: rarely — the hook handles reads and edits automatically. Invoke `sync` only on explicit user request, or `setup`/`link` for first-time configuration.
- **In Codex CLI**: the hook covers `apply_patch` and `Bash` once `overleaf-sync-now install` has run and the user has trusted the hook with `/hooks`. If you are not sure that happened (or you are on Windows, where the Codex hook path is untested), run `overleaf-sync-now sync <folder>` before editing a `.tex`/`.bib`/`.cls`/`.sty`/`.bst` file under `Apps/Overleaf/`. It costs about a second when nothing changed.
- **Shell edits**: the hook finds LaTeX paths written literally in a command (`sed -i ... main.tex`, `"…/My Paper/intro.tex"`, `*.tex` in the project folder). Paths built at run time (variables, `find -exec`, scripts that open files themselves) are invisible to it, so run `sync` first in those cases.
- **User mentions stale Overleaf content**: invoke `sync`.
- **User asks to set up Overleaf sync**: invoke `setup` (or full `install` if not yet linked into Claude/Codex skill dirs).

## Failure modes & recovery

- **`No valid Overleaf cookies (cache: <path>)`**: cookies expired or never set. **The hook BLOCKS writes (exit 2)** so the AI re-auths instead of editing a possibly-stale copy; reads get a warning. Recovery: the user runs `overleaf-sync-now login` in their own terminal (browser-assisted; works on Chrome 130+ Windows). Headless fallback: the user runs `overleaf-sync-now save-cookie` in their terminal. Do **not** rely on `setup` here — in non-interactive/agent context it only retries auto-detect and returns `AUTO-DETECT FAILED` without capturing a cookie.
- **`setup` can't find browser cookies**: use `overleaf-sync-now login` (the reliable path).
- **Auto-link can't find the project**: folder name doesn't match Overleaf project name. Use `link <project_id> .` inside the folder.

## Security note

Cached cookies grant full access to the user's Overleaf account. Two sensitive stores live under the data dir (`~/.overleaf-sync/` by default; legacy `~/.claude/overleaf-data/`):

- `cookies.json` — the cached session, written `0600` on POSIX (user-only).
- `browser-profile/` — created by `overleaf-sync-now login`; holds a **live, weeks-long Overleaf session** that grants the same full account access.

Since 0.4.0 the data dir itself is `0700` on POSIX so neither is reachable by other local users. To scrub credentials (sharing a home dir, decommissioning a machine, after a suspected compromise), delete the **whole** data dir, not just `cookies.json`. Don't commit, don't share. (On Windows, ACL inheritance from the parent directory is the relevant control.)

Agent transcripts are a third place a cookie could leak: anything typed into the chat or passed on a command line the agent runs is stored by the model provider and in local session logs. That is why the cookie only ever goes through `login` or the user's own `save-cookie` prompt.
