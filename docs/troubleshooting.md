# Troubleshooting

| Symptom | Fix |
|---|---|
| `No valid Overleaf cookies` | Run `overleaf-sync-now doctor` to see which auth source failed. On Chrome 130+ Windows, run `overleaf-sync-now login`. Otherwise, log into overleaf.com in any [supported browser](authentication.md#auth-chain). |
| `[overleaf-sync-now] Overleaf cookies invalid. Re-auth ...` (Claude blocked the edit) | Same as above. After re-auth, retry the edit. |
| Hook not firing in Claude Code | Restart Claude Code. Verify `~/.claude/settings.json` contains the hook entry, and `which overleaf-sync-now` returns a path. |
| Auto-link failed | Folder name doesn't match the Overleaf project name. Run `overleaf-sync-now link <project_id>` inside the folder to write a marker file. |
| `HTTP 429 (rate limited)` | Wait ~60 s. Manual `sync` will auto-retry once with `Retry-After`. |
| `uv tool upgrade` says *"Nothing to upgrade"* but you want the latest commit | See [operations → upgrading](operations.md#upgrading) — use the `--reinstall --refresh` form. |
| Codex CLI on Windows | Codex hooks are limited / disabled on Windows. The skill instructs Codex's model to invoke `sync` explicitly before editing. |
| HTTP 404 from sync | Project was deleted, archived, or auto-link picked the wrong project. Run `overleaf-sync-now status` to confirm project ID; re-link if wrong (`overleaf-sync-now link <id> .`). |
| Network error / timeout | Transient. Sync exits with the underlying error. Hook logs and proceeds (doesn't block edit). Try again. |
| Self-hosted Overleaf / Server Pro | Not supported. The tool hardcodes `https://www.overleaf.com`. PRs welcome to add a `--base-url` option. |
| Multiple Overleaf accounts | The cache holds one session at a time. See [operations → multiple accounts](operations.md#multiple-overleaf-accounts). |
| WSL + Claude Code on Windows | The CLI only sees its own filesystem. Claude Code on Windows (Win paths) won't find a tool installed inside WSL (Linux paths) and vice versa. Install in whichever environment runs your AI agent. |
| Cookies cache file is corrupt | Tool prints a warning and falls through the auth chain. Delete `~/.claude/overleaf-data/cookies.json` to reset. |

## Diagnostic commands

| Command | What it shows |
|---|---|
| `overleaf-sync-now status` | Data dir, cookie validity, linked project. Walks the full auth chain. |
| `overleaf-sync-now status --quick` | Cookie-cache-only check. Skips Playwright. Fast. |
| `overleaf-sync-now doctor` | Every auth source it tried plus the exact error from each. |
| `overleaf-sync-now projects` | Lists every Overleaf project the current session can see (name + ID). |
