# Architecture

How `overleaf-sync-now` is wired together, and where it puts things.

## Files written by `install`

Everything is **user-global**. Nothing is written into your project directory.

| Path | Purpose |
|---|---|
| `~/.local/bin/overleaf-sync-now` | CLI binary (on PATH) |
| `~/.claude/skills/overleaf/SKILL.md` | Skill description for Claude Code |
| `~/.codex/skills/overleaf/SKILL.md` | Skill description for Codex CLI |
| `~/.claude/settings.json` | PreToolUse hook entry (Claude Code only) |
| `<DATA_DIR>/cookies.json` | Cached `overleaf_session2` cookie (POSIX `0o600`) |
| `<DATA_DIR>/state.json` | Per-project debounce timestamps (POSIX `0o600`) |
| `<DATA_DIR>/versions.json` | Per-project last-synced Overleaf `toV` (since 0.1.0; POSIX `0o600`) |
| `<DATA_DIR>/projects.json` | 24-hour-cached project records (id, name, trashed, archived, lastUpdated, ownerId — since 0.3.0; POSIX `0o600`) |
| `<DATA_DIR>/browser-profile/` | Persistent Playwright profile created by `login` (Chromium with the Overleaf session) |

`<DATA_DIR>` resolution order: (1) `$OVERLEAF_SYNC_DATA_DIR` if set; (2) `~/.claude/overleaf-data/` if it exists (legacy installs); (3) `~/.overleaf-sync/` (default for new installs).

`install` is idempotent — re-running it refreshes the skill files and hook in place. The auto-link logic walks each edited file's path upward to find `…/Apps/Overleaf/<name>/`, so the same install applies to every Overleaf project under your Dropbox.

## The PreToolUse hook

The hook intercepts every `Read` / `Edit` / `Write` / `MultiEdit` of `.tex`, `.bib`, `.cls`, `.sty`, and `.bst` files. Other tools and other file types pass through untouched. (Read was added in 0.1.0 — the cheap `/updates` probe makes refresh-on-read affordable, and it keeps Claude's reasoning grounded in current content.)

| Step | What happens |
|---|---|
| **1. Tool + path filter** | Reject if `tool_name` isn't `Read`/`Edit`/`Write`/`MultiEdit`, or path doesn't end in a LaTeX extension. Exit 0. |
| **2. Auto-link** | Walk the path upward to find `…/Apps/Overleaf/<name>/`. Map `<name>` → project ID via the cached project index (refreshed every 24 h). A `.overleaf-project` marker file in any folder overrides the auto-link. |
| **3. Debounce** | If we've synced this project within the last 30 s, exit 0 without contacting Overleaf. |
| **4. Cookie resolve** | Use the cached cookie if valid. Otherwise walk the [auth chain](authentication.md). |
| **5. Version-match probe** | `GET /project/<id>/updates` (~30 KB, ~0.3 s). Compare latest `toV` to the cached `toV` in `versions.json`. |
| **6. Decide** | • Latest `toV` matches cache → no-op. • All new updates are `origin.kind == "dropbox"` (echoes of our own local saves round-tripping) → no-op, advance cache. • A web-origin update exists → continue. |
| **7. Conditional zip pull** | `GET /project/<id>/download/zip`, hash-compare each entry against local, write only the files whose contents actually differ. |
| **8. Recent-mtime guard** | Files modified locally in the last 30 s are skipped (with a `SKIP <path>` warning to stderr) — protects an in-progress local save not yet propagated Dropbox → Overleaf. `--force` disables. |

## Exit codes (hook)

| Code | Meaning | Effect on the AI agent |
|---|---|---|
| `0` | Refresh succeeded, nothing changed, debounced, or transient error | Tool call proceeds. |
| `2` | Auth chain exhausted | Tool call is **blocked**. The agent surfaces "re-auth required" to the user. |

The choice to exit 0 on transient errors (network blip, 429, 500, sandbox-blocked socket) is deliberate: blocking your AI session over a momentary problem is worse than risking one stale read. Exit 2 is reserved for real auth failures, where blocking is the correct answer.

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

Repeat fires within the 30-second debounce window cost nothing (debounce check + exit 0). Manual `sync --force` always pulls the zip and re-extracts.
