# Operations

Day-to-day notes for keeping `overleaf-sync-now` running smoothly.

## Cookie maintenance

Overleaf session cookies last several weeks. The tool re-validates the cached cookie before every sync. If it's been invalidated, the auth chain re-runs automatically and refreshes the cache. **Stay logged into overleaf.com in your daily browser** (or in the `login` profile, if you used that path) and the system self-heals.

If the entire auth chain fails, the PreToolUse hook **blocks the edit** with `exit 2` and a clear "re-auth required" message — your AI agent surfaces this to you instead of silently writing over a stale file.

## Rate limiting

Overleaf rate-limits its project endpoints per account (HTTP 429). In the default 0.1.0+ refresh path, the version-match probe (`GET /project/<id>/updates`) is read-only and cheap, so it rarely triggers a limit; the heavier `GET /project/<id>/download/zip` only fires when something actually changed. The 30-second debounce keeps frequency low in normal use.

- The **PreToolUse hook** handles 429 silently — no point blocking edits over a transient server limit; the next non-debounced sync will succeed.
- The **manual `sync`** command honors the `Retry-After` header and retries once after waiting.
- If you keep hitting 429, slow down. Wait a minute or two between manual syncs.

The `--legacy` path (`POST /sync-now`) is more rate-limit-prone because it enqueues a heavy job in Overleaf's per-user `tpdsworker` queue. Prefer the default.

## Upgrading

```bash
uv tool upgrade overleaf-sync-now && overleaf-sync-now install
```

If `uv tool upgrade` reports *"Nothing to upgrade"* (the published version hasn't bumped since your install), force a refresh from git:

```bash
uv tool install --reinstall --force --refresh \
  --from git+https://github.com/hanlulong/overleaf-sync-now overleaf-sync-now
```

## Uninstalling

```bash
overleaf-sync-now uninstall          # removes skill + hook (keeps cookies/cache)
uv tool uninstall overleaf-sync-now  # removes the binary
rm -r ~/.claude/overleaf-data        # optional: also remove cached cookies
```

## Multiple Overleaf accounts

The cache holds one session at a time. To switch accounts:

```bash
rm ~/.claude/overleaf-data/cookies.json
overleaf-sync-now setup     # or `login` on Chrome 130+ Windows
```

## Self-hosted Overleaf / Server Pro

Not supported out of the box. The tool hardcodes `https://www.overleaf.com`. PRs welcome to add a `--base-url` option.
