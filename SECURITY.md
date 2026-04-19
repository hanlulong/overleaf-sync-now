# Security

## Reporting

Email security issues to `hanlulong@gmail.com` with subject `overleaf-sync-now security`. Please don't open public issues for vulnerabilities.

## What this tool stores

- **Overleaf session cookie** (`overleaf_session2`) at `~/.claude/overleaf-data/cookies.json` (or `~/.overleaf-sync/cookies.json` on fresh installs). On POSIX systems the file is chmod 0600.
- **Persistent browser profile** (after running `overleaf-sync-now login`) at `~/.claude/overleaf-data/browser-profile/`. This includes its own cookie database.
- **Project list cache** at `~/.claude/overleaf-data/projects.json`. Names + IDs only, no content.

A leak of the session cookie grants full access to the user's Overleaf account until the session expires (typically several weeks). Treat the cache file as a credential.

## What this tool does NOT do

- It does not transmit cookies or any other data to anyone except `https://www.overleaf.com`.
- It does not log to disk by default.
- It does not auto-update.
- It does not run code from Overleaf — only consumes a documented (well, reverse-engineered) JSON API.

## Threat model

The tool is intended for use on a single user's workstation. It is not hardened for shared / multi-tenant servers. If you share a machine, ensure file permissions on `~/.claude/overleaf-data/` are user-only.
