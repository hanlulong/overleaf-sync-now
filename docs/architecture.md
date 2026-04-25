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
| `~/.claude/overleaf-data/cookies.json` *(or `~/.overleaf-sync/cookies.json`)* | Cached `overleaf_session2` cookie |
| `~/.claude/overleaf-data/state.json` | Per-project debounce timestamps |
| `~/.claude/overleaf-data/versions.json` | Per-project last-synced Overleaf `toV` (since 0.1.0) |
| `~/.claude/overleaf-data/projects.json` | 24-hour-cached `<name>` → project-ID index for auto-link |
| `~/.claude/overleaf-data/browser-profile/` | Persistent Playwright profile created by `login` (Chromium with the Overleaf session) |

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

The pre-0.1.0 `POST /project/<id>/dropbox/sync-now` path is still in the codebase as `sync --legacy`. It enqueues a heavy "poll Dropbox for user X" job in Overleaf's serialized `tpdsworker` queue, which competes with webhook-triggered local→Overleaf updates and was making local saves *slower* to appear on overleaf.com. The version-match path doesn't touch that queue. Avoid `--legacy` unless you specifically need it.

## Exit codes (hook)

| Code | Meaning | Effect on the AI agent |
|---|---|---|
| `0` | Refresh succeeded, nothing changed, debounced, or transient error | Tool call proceeds. |
| `2` | Auth chain exhausted | Tool call is **blocked**. The agent surfaces "re-auth required" to the user. |

The choice to exit 0 on transient errors (network blip, 429, 500, sandbox-blocked socket) is deliberate: blocking your AI session over a momentary problem is worse than risking one stale read. Exit 2 is reserved for real auth failures, where blocking is the correct answer.

## Auto-link

The mapping `…/Apps/Overleaf/<name>/` → Overleaf project ID uses a **24-hour-cached project index** fetched once per day from `https://www.overleaf.com/project`. The index lives at `~/.claude/overleaf-data/projects.json`.

If the auto-link is wrong (folder name differs from the Overleaf project name), drop a marker file:

```bash
overleaf-sync-now link <project_id> .
```

This writes a `.overleaf-project` file in the folder. The marker takes priority over the auto-link.

## Reverse-engineering history

Overleaf's `/project/{id}/...` endpoints aren't publicly documented. Discovery came in two rounds:

**0.0.1 — `POST /project/{id}/dropbox/sync-now`** (now legacy). Found by opening Overleaf in Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp)-controlled browser, clicking the "Sync this project now" button in the project's Integrations panel, and capturing the resulting POST in the Network tab. The CSRF token comes from `<meta name="ol-csrfToken">` on any project page; auth is the standard `overleaf_session2` cookie.

**0.1.0 — `GET /project/{id}/updates` + `GET /project/{id}/download/zip`** (current default). Found by reading Overleaf's open-source web service, specifically `services/web/app/src/Features/ThirdPartyDataStore/TpdsUpdateSender.mjs`, where a comment makes the queue serialization explicit:

> Queue poll requests in the user queue along with file updates, in order to avoid race conditions between polling and updates.

That confirmed the serial-queue starvation hypothesis behind the slowness reports, and surfaced `/updates` as a read-only probe that doesn't touch the queue at all. `/download/zip` was already known but had been avoided as too coarse — the version-match probe makes it conditional and selective.

`/download/zip` does **not** support HTTP `Range` requests (tested: returns `200` with the full body regardless of the `Range` header; `Transfer-Encoding: chunked`, no `Accept-Ranges`). So the zip is always full-project. The version-match probe is what stops us from downloading unless something genuinely changed.

## End-to-end latency budget

| Path | Cost | When it's hit |
|---|---|---|
| Hot path: probe-only | ~0.3–1 s | Most hook fires. `toV` matches or every change was a Dropbox-origin echo. |
| Walk + zip + extract (typical paper) | ~5–10 s | A real web-origin edit happened. Bandwidth dominates. |
| Bootstrap on first edit per project | ~10–30 s | No cached `toV` yet. Full zip + hash-compare against local. One-time. |

Repeat fires within the 30-second debounce window cost nothing (debounce check + exit 0). Manual `sync --force` always pulls the zip; manual `sync --legacy` enqueues `POST /sync-now` and waits ~10 s for Dropbox to settle (the 0.0.x default).
