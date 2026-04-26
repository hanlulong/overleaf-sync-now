# Security

## Reporting

Email security issues to `hanlulong@gmail.com` with subject `overleaf-sync-now security`. Please don't open public issues for vulnerabilities.

## What this tool stores

All under `<DATA_DIR>` (resolved as `$OVERLEAF_SYNC_DATA_DIR` → `~/.claude/overleaf-data/` → `~/.overleaf-sync/`, in that order). On POSIX, every file the tool writes under `<DATA_DIR>` is chmod `0600` (since v0.3.0; previously only `cookies.json` was restricted). On Windows, ACL inheritance from the parent directory is the relevant control.

- **Overleaf session cookie** (`overleaf_session2`) at `<DATA_DIR>/cookies.json`. A leak grants full access to the user's Overleaf account until the session expires (typically several weeks). Treat as a credential.
- **Persistent browser profile** (after running `overleaf-sync-now login`) at `<DATA_DIR>/browser-profile/`. Includes its own cookie database; Chromium manages those perms.
- **Project list cache** at `<DATA_DIR>/projects.json`. Project IDs, names, trashed/archived flags, lastUpdated timestamps, and ownerIds. No project content.
- **Per-project sync state** at `<DATA_DIR>/state.json` and `<DATA_DIR>/versions.json`. Project IDs and timestamps / version numbers. No content.
- **Cookie-validation timestamp** at `<DATA_DIR>/.validated-at`. A single epoch float used as a 60-second cache to skip re-probing Overleaf on every hook fire.
- **`.overleaf-project` markers** (per-project) inside `<Dropbox>/Apps/Overleaf/<name>/`. JSON containing `project_id` plus debug metadata (`source`, `linked_at`, `name_at_link_time`). These are deliberately **not** restricted to `0600` — they need to be readable by the local Dropbox client and other AI agents on the same machine. They contain no credentials.

## What this tool does NOT do

- It does not transmit cookies or any other data to anyone except `https://www.overleaf.com`.
- It does not log to disk by default.
- It does not auto-update.
- It does not run code from Overleaf — only consumes a documented (well, reverse-engineered) JSON API.

## Threat model

The tool is intended for use on a single user's workstation. It is not hardened for shared / multi-tenant servers. If you share a machine, ensure file permissions on `~/.claude/overleaf-data/` are user-only.
