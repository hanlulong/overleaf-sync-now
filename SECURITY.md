# Security

## Reporting

Email security issues to `hanlulong@gmail.com` with subject `overleaf-sync-now security`. Please don't open public issues for vulnerabilities.

## What this tool stores

All under `<DATA_DIR>` (resolved as `$OVERLEAF_SYNC_DATA_DIR` → `~/.claude/overleaf-data/` if it exists (legacy installs) → `~/.overleaf-sync/`). On POSIX, every file the tool writes under `<DATA_DIR>` is chmod `0600` (since v0.3.0) and `<DATA_DIR>` itself is `0700` (since v0.4.0). On Windows, ACL inheritance from the parent directory is the relevant control.

- **Overleaf session cookie** (`overleaf_session2`) at `<DATA_DIR>/cookies.json`. A leak grants full access to the user's Overleaf account until the session expires (typically several weeks). Treat as a credential.
- **Persistent browser profile** (after running `overleaf-sync-now login`) at `<DATA_DIR>/browser-profile/`. Holds an equally sensitive, weeks-long Overleaf session in Chromium's own cookie database; it is protected by the `0700` data dir. If you sign in through Google, ORCID or institutional SSO in that window, that provider's session is kept in the profile too; sign out of the provider there, or delete the profile, if you do not want that.
- **Project list cache** at `<DATA_DIR>/projects.json`. Project IDs, names, trashed/archived flags, lastUpdated timestamps, and ownerIds. No project content.
- **Per-project sync state** at `<DATA_DIR>/state.json`, `<DATA_DIR>/versions.json`, `<DATA_DIR>/web-removed.json`, `<DATA_DIR>/conflicts-pending.json` and `<DATA_DIR>/extracted.json`. Project IDs, timestamps, version numbers, the paths of files deleted or renamed on overleaf.com, the paths of kept files and of Overleaf's saved copy of each (`conflicts-pending.json`), and the paths, mtimes and sizes of the files the tool wrote (`extracted.json`). A `state.json` that cannot be parsed is renamed to `<DATA_DIR>/state.json.bad`, which the tool keeps and never reads again. No content.
- **Backups and conflict copies** at `<DATA_DIR>/backups/` and `<DATA_DIR>/conflicts/`. Copies of your project files (the previous local version of every file a refresh overwrote, and Overleaf's version of files kept locally). Same `0600` / `0700` protection; backups are deleted after 30 days, conflict copies only by you.
- **Cookie-validation timestamp** at `<DATA_DIR>/.validated-at`. A single epoch float used as a 60-second cache to skip re-probing Overleaf on every hook fire.
- **Back-off timestamps** at `<DATA_DIR>/.browser-cookies-failed-at` and `<DATA_DIR>/.login-profile-failed-at`. Each holds a single epoch float, the time of the last failed attempt. No credentials. `.browser-cookies-failed-at` (since v0.5.0) is written when a cookie lookup that read your installed browsers' cookie stores found no valid Overleaf session. For the next hour, `sync` and the other lookups that would read those stores on their own skip them, so they raise no keychain prompts; `setup`, `install`, a full `status` and `doctor` still read them. `.login-profile-failed-at` is written when reading the `login` profile fails: a headless browser could not open it (for example, while the `login` window still has it open), or it held no `overleaf_session2` cookie. For the next 5 minutes, every cookie lookup, the hook included, skips the profile instead of launching a browser again. The tool never deletes either file: each stops applying once its window has passed, and a later failure overwrites it. Delete one to end its back-off early.
- **`.overleaf-project` markers** (per-project) inside `<Dropbox>/Apps/Overleaf/<name>/`. JSON containing `project_id` plus debug metadata (`source`, `linked_at`, `name_at_link_time`). These are deliberately **not** restricted to `0600` — the local Dropbox client and other AI agents on the same machine need to read them. They contain no credentials, but they sync into the Overleaf project, so collaborators can read and rewrite them. The tool ignores a marker whose `project_id` is not a 24-character hex id.

`install` also writes hook entries into `~/.claude/settings.json` and `~/.codex/hooks.json` (backing up the latter when it changes). Neither contains credentials.

## Browser cookie stores and the macOS keychain

`setup`, `install`, `status`, `doctor` and `sync` may read your installed browsers' cookie stores to find an existing Overleaf login (only the `overleaf.com` cookies are used). On macOS, Chromium-based browsers protect that store with a "<Browser> Safe Storage" key in the login keychain, so macOS asks before `security` hands it out. The tool has two cookie readers, so one browser can ask twice. Choose Allow or Deny; avoid Always Allow, which adds `security` to that key's access list so any program that runs it can read the key without a prompt. The PreToolUse hook never reads browser stores or the keychain, and `sync` stops trying for an hour after a failed attempt. `overleaf-sync-now login` avoids all of this by using its own browser profile.

## Agent transcripts

The cookie must never pass through an AI agent. Anything typed into an agent chat, and any command-line argument an agent runs, ends up in the model provider's transcript and in local session logs; an argument is also visible in the process list and shell history. So:

- Every auth-failure message the tool shows an agent is login-first and tells the agent never to ask for the cookie in chat.
- `overleaf-sync-now login` captures the cookie inside a browser the user controls; it is never printed.
- `overleaf-sync-now save-cookie` (headless fallback) is run by the user in their own terminal and reads the value from a hidden prompt or stdin. Passing it as an argument still works for old scripts but prints a warning.
- `setup`, `status` and `doctor` report a cookie as present or absent; they never print any part of it.

If a cookie has been pasted somewhere it shouldn't be, end that session. If it came from the browser you are using, sign out of Overleaf there: Clear Sessions (on overleaf.com, Account → Account Settings → Sessions → Manage your sessions) ends every session except the one in the browser you click it in. For a cookie from another browser or from the `login` profile, Clear Sessions is enough. Then run `overleaf-sync-now login` again.

## What this tool does NOT do

- It does not transmit cookies or any other data to anyone except `https://www.overleaf.com`. (Content an agent reads from your project goes wherever that agent sends it; that is the agent's data flow, not this tool's.)
- It does not log to disk by default.
- It does not auto-update.
- It does not run code from Overleaf — only consumes a documented (well, reverse-engineered) JSON API.
- It never deletes local project files, and it copies a file's previous version to `<DATA_DIR>/backups/` before overwriting it.

## Threat model

The tool is intended for use on a single user's workstation. It is not hardened for shared / multi-tenant servers. If you share a machine, check that `<DATA_DIR>` (run `overleaf-sync-now status` to see it) is user-only.
