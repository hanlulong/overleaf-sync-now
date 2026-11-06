# Operations

Day-to-day notes for keeping `overleaf-sync-now` running smoothly.

## Cookie maintenance

Overleaf session cookies last several weeks. The tool re-validates the cached cookie at most once a minute (a successful check is cached for 60 s). If it's been invalidated, the auth chain re-runs automatically and refreshes the cache — the `login` browser profile is checked first, so after one `overleaf-sync-now login` the system usually self-heals. (Being logged into overleaf.com in your everyday browser only helps where cookies can still be read from it; Chrome 130+ app-bound encryption blocks that.)

If the entire auth chain fails, the PreToolUse hook **blocks AI edits** with `exit 2` and a "run `overleaf-sync-now login`" message — your AI agent surfaces this to you instead of silently writing over a stale file. Reads still go through, with a warning.

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
overleaf-sync-now uninstall          # removes skill copies + Claude/Codex hooks (keeps cookies/cache)
uv tool uninstall overleaf-sync-now  # removes the binary
rm -r ~/.overleaf-sync               # optional: also remove cached cookies
                                     # (legacy installs: ~/.claude/overleaf-data)
```

## Multiple Overleaf accounts

The cache holds one session at a time. To switch accounts:

```bash
rm ~/.overleaf-sync/cookies.json     # legacy: ~/.claude/overleaf-data/cookies.json
overleaf-sync-now setup              # or `login` on Chrome 130+ Windows
```

Run `overleaf-sync-now status` to confirm which data dir your install uses (resolved from `$OVERLEAF_SYNC_DATA_DIR` → `~/.claude/overleaf-data/` → `~/.overleaf-sync/`, in that order).

## Self-hosted Overleaf / Server Pro

Not supported out of the box. The tool hardcodes `https://www.overleaf.com`. PRs welcome to add a `--base-url` option.
