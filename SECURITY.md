# Security

## Reporting

Email security issues to `hanlulong@gmail.com` with subject `overleaf-sync-now security`. Please don't open public issues for vulnerabilities.

## What this tool stores

All under `<DATA_DIR>` (resolved as `$OVERLEAF_SYNC_DATA_DIR` → `~/.claude/overleaf-data/` if it exists (legacy installs) → `~/.overleaf-sync/`). On POSIX, every file the tool writes under `<DATA_DIR>` is chmod `0600` (since v0.3.0) and `<DATA_DIR>` itself is `0700` (since v0.4.0). On Windows, ACL inheritance from the parent directory is the relevant control.

- **Overleaf session cookie** (`overleaf_session2`) at `<DATA_DIR>/cookies.json`. A leak grants full access to the user's Overleaf account until the session expires (typically several weeks). Treat as a credential.
- **Persistent browser profile** (after running `overleaf-sync-now login`) at `<DATA_DIR>/browser-profile/`. Holds an equally sensitive, weeks-long Overleaf session in Chromium's own cookie database; it is protected by the `0700` data dir.
- **Project list cache** at `<DATA_DIR>/projects.json`. Project IDs, names, trashed/archived flags, lastUpdated timestamps, and ownerIds. No project content.
- **Per-project sync state** at `<DATA_DIR>/state.json`, `<DATA_DIR>/versions.json` and `<DATA_DIR>/web-removed.json`. Project IDs, timestamps, version numbers, and the paths of files deleted or renamed on overleaf.com. No content.
- **Cookie-validation timestamp** at `<DATA_DIR>/.validated-at`. A single epoch float used as a 60-second cache to skip re-probing Overleaf on every hook fire.
- **`.overleaf-project` markers** (per-project) inside `<Dropbox>/Apps/Overleaf/<name>/`. JSON containing `project_id` plus debug metadata (`source`, `linked_at`, `name_at_link_time`). These are deliberately **not** restricted to `0600` — the local Dropbox client and other AI agents on the same machine need to read them. They contain no credentials, but they sync into the Overleaf project, so collaborators can read and rewrite them. The tool ignores a marker whose `project_id` is not a 24-character hex id.

`install` also writes hook entries into `~/.claude/settings.json` and `~/.codex/hooks.json` (backing up the latter when it changes). Neither contains credentials.

## Agent transcripts

The cookie must never pass through an AI agent. Anything typed into an agent chat, and any command-line argument an agent runs, ends up in the model provider's transcript and in local session logs; an argument is also visible in the process list and shell history. So:

- Every auth-failure message the tool shows an agent is login-first and tells the agent never to ask for the cookie in chat.
- `overleaf-sync-now login` captures the cookie inside a browser the user controls; it is never printed.
- `overleaf-sync-now save-cookie` (headless fallback) is run by the user in their own terminal and reads the value from a hidden prompt or stdin. Passing it as an argument still works for old scripts but prints a warning.
- `setup`, `status` and `doctor` report a cookie as present or absent; they never print any part of it.

If a cookie has been pasted somewhere it shouldn't be, sign out of Overleaf in the browser it came from (Overleaf ends that session on logout) and run `overleaf-sync-now login` again.

## What this tool does NOT do

- It does not transmit cookies or any other data to anyone except `https://www.overleaf.com`. (Content an agent reads from your project goes wherever that agent sends it; that is the agent's data flow, not this tool's.)
- It does not log to disk by default.
- It does not auto-update.
- It does not run code from Overleaf — only consumes a documented (well, reverse-engineered) JSON API.
- It never deletes local project files.

## Threat model

The tool is intended for use on a single user's workstation. It is not hardened for shared / multi-tenant servers. If you share a machine, check that `<DATA_DIR>` (run `overleaf-sync-now status` to see it) is user-only.
