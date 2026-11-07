# Operations

Day-to-day notes for keeping `overleaf-sync-now` running smoothly.

## Cookie maintenance

Overleaf session cookies last several weeks. The tool re-validates the cached cookie at most once a minute (a successful check is cached for 60 s). If it's been invalidated, the auth chain re-runs automatically and refreshes the cache — the `login` browser profile is checked first, so after one `overleaf-sync-now login` the system usually self-heals. (Being logged into overleaf.com in your everyday browser only helps where cookies can still be read from it; Chrome 127+ app-bound encryption blocks that.)

If the entire auth chain fails, the PreToolUse hook **blocks AI edits** with `exit 2` and a "run `overleaf-sync-now login`" message — your AI agent surfaces this to you instead of silently writing over a stale file. Reads still go through, with a warning.

## Backups and conflict copies

A refresh never overwrites a local file without keeping the old bytes:

- `<DATA_DIR>/backups/<project_id>/<timestamp>/<path>` holds the previous local version of every file a refresh rewrote. Snapshots older than 30 days are deleted on the next backup for that project.
- `<DATA_DIR>/conflicts/<project_id>/<timestamp>/<path>` holds Overleaf's version of a file the tool **kept** locally. That happens on a first sync, a history rewind, or after a long gap (when the whole project is compared): a local file that differs from Overleaf and was changed after Overleaf last changed it has edits Overleaf never received (Dropbox paused, offline edits, an Overleaf Dropbox outage), so it is not reverted. Compare the two and keep what you want; conflict copies are never deleted automatically.

To restore a backup, copy the file back into the project folder; Dropbox uploads it to Overleaf as usual. Overleaf's own History panel also keeps every earlier version.

## Rate limiting

Overleaf rate-limits its project endpoints per account (HTTP 429). The version-match probe (`GET /project/<id>/updates`) is read-only and cheap, so it rarely triggers a limit; the heavier `GET /project/<id>/download/zip` only fires when something actually changed. The debounce (one probe per project per 30 s for reads, 3 s for writes) keeps frequency low in normal use.

- The **PreToolUse hook** lets the call through on 429 with a warning — no point blocking edits over a transient server limit. A rate-limited write is not recorded as fresh, so the next write probes again.
- The **manual `sync`** command exits 1 and prints how long to wait (from the `Retry-After` header, default 60 s). It does not retry by itself.
- If you keep hitting 429, slow down. Wait a minute or two between manual syncs.

## Upgrading

Install the release you want by tag, then re-run `install` so the skill copies and both hooks are rewritten:

```bash
uv tool install --reinstall --force --refresh \
  --from git+https://github.com/hanlulong/overleaf-sync-now@v0.5.0 overleaf-sync-now
overleaf-sync-now install
```

To move to a later release, run the same command with the new tag. If `install` changed the Codex hook entry, Codex asks you to trust it again: run `/hooks` in Codex.

## Uninstalling

```bash
overleaf-sync-now uninstall          # removes skill copies + Claude/Codex hooks, then lists what is left
overleaf-sync-now uninstall --remove-markers   # also deletes the .overleaf-project markers it found
uv tool uninstall overleaf-sync-now  # removes the binary
rm -r ~/.overleaf-sync               # optional: cached session, login profile, backups, conflicts
                                     # (legacy installs: ~/.claude/overleaf-data)
```

`uninstall` lists what it leaves in place:

- **`.overleaf-project` markers** in the usual Dropbox locations (`~/Dropbox`, `~/Dropbox (…)`, `~/Library/CloudStorage/Dropbox*`); pass your `Apps/Overleaf` folder as an argument if Dropbox lives elsewhere. Each marker syncs into its Overleaf project, so collaborators see it until it is deleted.
- **The data dir**: the cached session, the `login` browser profile, `backups/` and `conflicts/`.
- **Config backups**: the one-time `settings.json.bak-overleaf-sync-now` beside Claude Code's `settings.json`, and `hooks.json.bak-<timestamp>` copies beside Codex's `hooks.json`. They still hold the old hook entry, so restoring one brings the hook back; `uninstall` prints an `rm` command for each.
- **The browser `login` downloaded**, if it had to (the Playwright cache, shared with other Playwright tools).
- **The Overleaf session itself**, which stays valid on Overleaf's servers until it expires; deleting the files does not end it. On overleaf.com open Account → Account Settings → Sessions → Manage your sessions and click Clear Sessions (it ends every session except the one in the browser you click it in, which includes the tool's `login` session).

## Claude Code or Codex configuration in another directory

`install`, `uninstall`, `status` and `doctor` follow `CLAUDE_CONFIG_DIR` (Claude Code's configuration directory, default `~/.claude`) and `CODEX_HOME` (default `~/.codex`). If you run several accounts side by side, run `install` once per directory with the variable set. `install` exits with an error, and writes nothing, when it finds neither directory.

## Multiple Overleaf accounts

The cache holds one session at a time. To switch accounts:

```bash
rm ~/.overleaf-sync/cookies.json     # legacy: ~/.claude/overleaf-data/cookies.json
overleaf-sync-now login              # log into the other account in the window that opens
```

Run `overleaf-sync-now status` to confirm which data dir your install uses (resolved from `$OVERLEAF_SYNC_DATA_DIR` → `~/.claude/overleaf-data/` → `~/.overleaf-sync/`, in that order).

## Self-hosted Overleaf / Server Pro

Not supported out of the box. The tool hardcodes `https://www.overleaf.com`. PRs welcome to add a `--base-url` option.
