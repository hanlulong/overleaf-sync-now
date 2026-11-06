# Architecture

How `overleaf-sync-now` is wired together, and where it puts things.

## Files written by `install`

Everything `install` writes is **user-global**. The only file the tool ever writes into a project folder is the `.overleaf-project` marker that auto-link drops there (see [Auto-link](#auto-link)); because it sits in the Dropbox folder, it also syncs into the Overleaf project.

| Path | Purpose |
|---|---|
| `~/.local/bin/overleaf-sync-now` | CLI binary (on PATH) |
| `~/.claude/skills/overleaf/SKILL.md` | Skill description for Claude Code |
| `~/.agents/skills/overleaf/SKILL.md` | Skill description for Codex CLI (user skills folder; since 0.5.0) |
| `~/.claude/settings.json` | Claude Code PreToolUse hook entry (written through a symlink; the previous content is kept once as `settings.json.bak-overleaf-sync-now`). `$CLAUDE_CONFIG_DIR` replaces `~/.claude` when set. |
| `~/.codex/hooks.json` | Codex CLI PreToolUse hook entry (since 0.5.0; the previous file is backed up as `hooks.json.bak-<timestamp>` when install changes it). `$CODEX_HOME` replaces `~/.codex` when set. |
| `<DATA_DIR>/cookies.json` | Cached `overleaf_session2` cookie (POSIX `0o600`) |
| `<DATA_DIR>/state.json` | Per-project debounce timestamps (POSIX `0o600`) |
| `<DATA_DIR>/versions.json` | Per-project last-synced Overleaf `toV` (since 0.1.0; POSIX `0o600`) |
| `<DATA_DIR>/projects.json` | 24-hour-cached project records (id, name, trashed, archived, lastUpdated, ownerId — since 0.3.0; POSIX `0o600`) |
| `<DATA_DIR>/web-removed.json` | Paths deleted or renamed on overleaf.com whose local copy still exists (since 0.5.0; POSIX `0o600`) |
| `<DATA_DIR>/browser-profile/` | Persistent browser profile created by `login` (holds the Overleaf session; opened with the installed Chrome or Edge when available) |
| `<DATA_DIR>/backups/<project>/<timestamp>/` | Previous local version of every file a refresh overwrote (since 0.5.0; pruned after 30 days) |
| `<DATA_DIR>/conflicts/<project>/<timestamp>/` | Overleaf's version of a file kept locally because it had newer local edits (since 0.5.0; never pruned) |
| `<DATA_DIR>/.browser-cookies-failed-at` | One-hour back-off after the installed-browser cookie readers failed (since 0.5.0) |

Versions up to 0.4.x copied the skill to `~/.codex/skills/overleaf/`. Codex documents `~/.agents/skills` as the user skills location, and a second copy makes it list the skill twice, so `install` removes that copy (only if it is ours). `install` and `uninstall` never delete a skill folder whose `SKILL.md` doesn't mention `overleaf-sync-now`.

`<DATA_DIR>` resolution order: (1) `$OVERLEAF_SYNC_DATA_DIR` if set; (2) `~/.claude/overleaf-data/` if it exists (legacy installs); (3) `~/.overleaf-sync/` (default for new installs).

`install` is idempotent — re-running it refreshes the skill files and hook in place. The auto-link logic walks each edited file's path upward to find `…/Apps/Overleaf/<name>/`, so the same install applies to every Overleaf project under your Dropbox.

## The PreToolUse hook

The hook runs before tool calls that touch `.tex`, `.bib`, `.cls`, `.sty`, or `.bst` files. Other tools and other file types pass through untouched.

| Client | Tools (matcher) | Where the paths come from |
|---|---|---|
| Claude Code (`~/.claude/settings.json`) | `Read\|Edit\|Write\|Bash\|PowerShell` | `tool_input.file_path`; for Bash/PowerShell, LaTeX paths found in `tool_input.command` |
| Codex CLI (`~/.codex/hooks.json`) | `apply_patch\|Bash` | the `*** Update File:` / `*** Add File:` / `*** Delete File:` / `*** Move to:` lines of the patch in `tool_input.command`; for Bash, LaTeX paths in the command |

The Claude Code entry is written in exec form (`"command": "<abs path>", "args": ["hook"]`), so no shell parses it; shell form (`"<path>" hook`) is a parse error under PowerShell, which Claude Code uses on Windows without Git Bash. It sets `"timeout": 180` so a first-use bootstrap pull isn't killed (a longer timeout you set yourself is kept). The Codex entry is a shell command string (`command`, plus a `commandWindows` variant), which is the only form Codex supports; Codex runs it only after you trust it with `/hooks`.

Shell commands are classified as reads when every command in them is a known read-only program (`cat`, `grep`, `sed -n`, `git diff`, `latexmk`, …) and nothing is redirected into a LaTeX file. Anything else counts as a write. Relative paths resolve against the payload `cwd` and any `cd <dir>` in the command; `*.tex`-style globs are expanded; and a command run inside a project folder that mentions a LaTeX extension counts as touching that project.

| Step | What happens |
|---|---|
| **1. Tool + path filter** | Not one of the tools above, or no LaTeX path → exit 0. |
| **2. Resolve** | Walk each path upward to a `.overleaf-project` marker or `…/Apps/Overleaf/<name>/` (a `.overleaf-sync-ignore` file, or a folder listed in `$OVERLEAF_SYNC_IGNORE`, opts a folder out). Map `<name>` → project ID via the cached project index (refreshed every 24 h, and once more before concluding that no project has that name). |
| **3. Fail closed** | Under `Apps/Overleaf/<name>/` but unresolved (not logged in, no living project with that name, several) → **writes exit 2** with the reason and the exact fix; reads get a warning and proceed. Network/sandbox failures pass through. |
| **4. Debounce** | Reads: skip the probe if this project was probed in the last 30 s. Writes: skip only within 3 s. |
| **5. Cookie resolve** | Use the cached cookie if it was validated in the last 60 s; otherwise re-validate. Overleaf refused it → try the `login` profile (the hook never reads installed browsers or the keychain). No answer from Overleaf (offline, sandbox) → `NetworkUnavailable`: the call proceeds with a note, never an auth block. |
| **6. Version-match probe** | `GET /project/<id>/updates` (~30 KB, ~0.3 s). Compare latest `toV` to the cached `toV` in `versions.json`. |
| **7. Decide** | • Latest `toV` matches cache → no-op. • All new updates are `origin.kind == "dropbox"` (echoes of our own local saves round-tripping) → no-op, advance cache. • A web-origin update exists (text edits, or file adds/renames/deletes in `project_ops`) → continue. |
| **8. Conditional zip pull** | `GET /project/<id>/download/zip`, hash-compare each entry against local, write only the files whose contents actually differ (including files added or renamed on the web), after copying the previous local bytes to `backups/`. On a bootstrap or full pull, a local file changed after Overleaf's last change to it (per `/updates` `meta.end_ts`) is kept and Overleaf's copy goes to `conflicts/`. Web-side deletes and rename sources go to `web-removed.json`; local files are never deleted. A one-line summary of what was written or skipped goes to stderr. |
| **9. Recent-mtime guard** | Files modified locally in the last 30 s are skipped (with a `SKIP <path>` warning to stderr) — protects an in-progress local save not yet propagated Dropbox → Overleaf. `--force` disables. |
| **10. Write guards** | For writes only: if step 8 just rewrote a file this call is about to change → exit 2, "re-read first". If step 9 had to skip a target that also changed on the web → exit 2, "conflict" with the exact steps. If step 8 kept a newer local target → exit 2 once, "compare with Overleaf's copy and ask the user". If the target was deleted or renamed on the web and the local copy still exists → exit 2, unless the call only deletes it (`rm`, `git rm`, `Remove-Item`, `del`, `*** Delete File:`). |

## Exit codes (hook)

| Code | Meaning | Effect on the AI agent |
|---|---|---|
| `0` | Refresh succeeded, nothing changed, debounced, a read, or Overleaf could not be reached (offline, 429, 5xx, a sandbox without network access) | Tool call proceeds. Warnings and the refresh summary go to stderr. |
| `2` | A write that must not proceed: auth missing, folder unresolved, target refreshed under the agent, or target deleted/renamed on the web | Tool call is **blocked**; Claude Code and Codex show the stderr reason to the model, which can fix it (re-read, `link`, ask the user to `login`). |

The choice to exit 0 on transient errors is deliberate: blocking your AI session over a momentary problem is worse than risking one stale read. A hook that fails to start or times out also lets the call through by default; Claude Code 2.1.295 and later accept `"onFailure": "block"` on the hook entry if you prefer the opposite.

## Auto-link

The mapping `…/Apps/Overleaf/<name>/` → Overleaf project ID uses a **24-hour-cached project index** fetched once per day from `https://www.overleaf.com/project`. The index lives at `<DATA_DIR>/projects.json` and stores the full record per project (id, name, trashed, archived, lastUpdated, ownerId) so the resolver can apply policy (filter trashed/archived; refuse to guess on duplicate names; tie-break ambiguity by fingerprinting which project's recent Dropbox-origin files exist locally).

On a successful auto-link, the resolver writes a `.overleaf-project` marker into the project folder so subsequent syncs skip the index lookup entirely. The marker carries `project_id`, `linked_at`, `name_at_link_time`, and `source` (`auto-link` / `auto-link-fingerprint` / `link`); only `project_id` is load-bearing for resolution.

If the auto-link is wrong (folder name differs from the Overleaf project name, or two projects share a name) drop the marker explicitly:

```bash
overleaf-sync-now link <project_id> .
```

The marker takes priority over the auto-link. `_extract_files` skips any zip entry named `.overleaf-project` so a marker that round-trips through Overleaf's Dropbox bridge can never overwrite the local marker.

## How the endpoints were found

Overleaf's `/project/{id}/...` endpoints aren't publicly documented. The two used by this tool — `GET /project/{id}/updates` and `GET /project/{id}/download/zip` — were identified by reading Overleaf's open-source web service. `/updates` is the read-only history endpoint the editor uses for the changes panel; it returns per-update `{fromV, toV, pathnames, meta.origin}`, and the `meta.origin.kind == "dropbox"` field is what lets us cleanly tell apart our own Dropbox-bridge round-trips from genuine web edits. `/download/zip` is the project-export endpoint surfaced in the editor's Menu.

`/download/zip` does **not** support HTTP `Range` requests (tested: returns `200` with the full body regardless of the `Range` header; `Transfer-Encoding: chunked`, no `Accept-Ranges`). So the zip is always full-project. The version-match probe is what stops us from downloading unless something genuinely changed.

## End-to-end latency budget

| Path | Cost | When it's hit |
|---|---|---|
| Hot path: probe-only | ~0.3–1 s | Most hook fires. `toV` matches or every change was a Dropbox-origin echo. |
| Walk + zip + extract (typical paper) | ~5–10 s | A real web-origin edit happened. Bandwidth dominates. |
| Bootstrap on first edit per project | ~10–30 s | No cached `toV` yet. Full zip + hash-compare against local. One-time. |

Repeat reads within 30 s (writes within 3 s) of the last probe cost nothing (debounce check + exit 0). Manual `sync --force` always pulls the zip and re-extracts.
