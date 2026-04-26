# Troubleshooting

| Symptom | Fix |
|---|---|
| `No valid Overleaf cookies` | Run `overleaf-sync-now doctor` to see which auth source failed. On Chrome 130+ Windows, run `overleaf-sync-now login`. Otherwise, log into overleaf.com in any [supported browser](authentication.md#auth-chain). |
| `[overleaf-sync-now] Overleaf cookies invalid. Re-auth ...` (Claude blocked the edit) | Same as above. After re-auth, retry the edit. |
| `Outbound HTTPS to Overleaf was blocked by the host environment ...` | The shell is sandboxed (Codex CLI, some CI runners) and blocked the socket. Auth is fine. Approve the `overleaf-sync-now` command prefix in your sandbox policy, or re-run from an unsandboxed shell. **Don't** run `setup`/`login`/`doctor` — they'll hit the same block. |
| `status` reports `Cookie auth: UNKNOWN — outbound HTTPS is blocked` | Same as above — sandbox / firewall is blocking. |
| `[overleaf-sync-now] SKIP <file>: local modified Ns ago (< 30s protection window)` | Working as intended. The recent-mtime guard refuses to overwrite a local file you may still be editing. Either wait until the file is older than 30 s and rerun `sync`, or pass `sync --force` to override. |
| Hook not firing in Claude Code | Restart Claude Code. Verify `~/.claude/settings.json` contains the hook entry with matcher `Read\|Edit\|Write\|MultiEdit`, and `which overleaf-sync-now` returns a path. After upgrading from 0.0.x, re-run `overleaf-sync-now install` so the matcher is rewritten to include `Read`. |
| Auto-link failed | Folder name doesn't match the Overleaf project name. Run `overleaf-sync-now link <project_id>` inside the folder to write a marker file. |
| `auto-link: N non-trashed Overleaf projects named '<name>'. Refusing to guess.` | Two or more **active** projects in your Overleaf account share the folder name. The tool lists candidates with IDs, `lastUpdated`, and any archived flag. Pick one and run `overleaf-sync-now link <project_id> <folder>` to write the marker explicitly. *(New in 0.3.0; prevents silent wrong-project sync.)* |
| `WARN: marker at <folder> -> <id> is trashed/archived on Overleaf` | The `.overleaf-project` marker points at a project that has since been trashed or archived on Overleaf. Sync still proceeds (you explicitly linked it), but you probably want `overleaf-sync-now link <active_id> <folder>` to relink. |
| `WARN: marker at <folder> -> <id> is not in your Overleaf account` | The cached projects index doesn't contain that project ID. Likely causes: the project was deleted on Overleaf; the cookies belong to a different Overleaf account than when you linked. Run `overleaf-sync-now projects --refresh` to confirm, then re-link or re-auth. |
| `WARN: project <id> has recent Dropbox-origin updates touching files not present in <folder>` | Strong signal the link points at the wrong project entirely (typo'd manual `link`, a fork, etc.). Run `overleaf-sync-now status` and `overleaf-sync-now projects` to verify, then `overleaf-sync-now link <correct_id> <folder>` to fix. *(New in 0.3.0; the wrong-project smoke detector.)* |
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
| `overleaf-sync-now status [folder]` | Data dir, cookie validity, linked project, last-sync time, **cached `toV`**. Distinguishes sandbox-blocked network from real auth failures. |
| `overleaf-sync-now status --quick` | Skips the chain-fallback step (only checks the cached cookie's network probe). Faster, but can't predict whether sync would still succeed via a stale-cache-then-refresh path. |
| `overleaf-sync-now doctor [folder]` | Every auth source it tried plus the exact error from each, plus a live `/updates` probe of the linked project. |
| `overleaf-sync-now projects` | Lists every Overleaf project the current session can see (name + ID). |
