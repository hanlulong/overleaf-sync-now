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
| `~/.claude/overleaf-data/` *(or `~/.overleaf-sync/`)* | Cookie cache, sync state, project index |

`install` is idempotent — re-running it refreshes the skill files and hook in place. The auto-link logic walks each edited file's path upward to find `…/Apps/Overleaf/<name>/`, so the same install applies to every Overleaf project under your Dropbox.

## The PreToolUse hook

The hook intercepts every `Edit` / `Write` / `MultiEdit` of `.tex`, `.bib`, `.cls`, `.sty`, and `.bst` files. Other tools and other file types pass through untouched.

| Step | What happens |
|---|---|
| **1. Path filter** | Non-LaTeX paths exit 0 immediately. |
| **2. Debounce** | Any sync within the last 30 s for the same project is treated as fresh. The hook exits 0 without contacting Overleaf. |
| **3. Auto-link** | Walk the path upward to find `…/Apps/Overleaf/<name>/`. Map `<name>` → project ID via the cached project index (refreshed every 24 h). A `.overleaf-project` marker file in the folder overrides the auto-link. |
| **4. Cookie resolve** | Use the cached cookie if valid (60 s validation cache). Otherwise walk the [auth chain](authentication.md). |
| **5. POST sync** | `POST /project/<id>/dropbox/sync-now` with `X-Csrf-Token`. |
| **6. Settle** | Wait 3 s for Dropbox client to pull. The manual `sync` command waits 10 s for thoroughness. |

## Exit codes

| Code | Meaning | Effect on the AI agent |
|---|---|---|
| `0` | Sync triggered, debounced, skipped (non-LaTeX), or transient error | Edit proceeds. |
| `2` | Auth chain exhausted; can't sync | Edit is **blocked**. The agent surfaces "re-auth required" to the user. |

The choice to exit 0 on transient errors (network blip, 429, 500) is deliberate: blocking your AI session over a momentary server hiccup is worse than risking one stale read. Exit 2 is reserved for auth failures, where blocking is the correct answer.

## Auto-link

The mapping `…/Apps/Overleaf/<name>/` → Overleaf project ID uses a **24-hour-cached project index** fetched once per day from `https://www.overleaf.com/project`. The index lives at `~/.claude/overleaf-data/projects.json`.

If the auto-link is wrong (folder name differs from the Overleaf project name), drop a marker file:

```bash
overleaf-sync-now link <project_id> .
```

This writes a `.overleaf-project` file in the folder. The marker takes priority over the auto-link.

## How the endpoint was reverse-engineered

`POST /project/{id}/dropbox/sync-now` isn't documented anywhere. Found by opening Overleaf in Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp)-controlled browser, clicking the "Sync this project now" button in the project's Integrations panel, and capturing the resulting POST in the Network tab. The CSRF token comes from `<meta name="ol-csrfToken">` on any project page; auth is the standard `overleaf_session2` cookie.

## End-to-end latency budget

A fresh hook-triggered sync takes **5–10 seconds**:

| Phase | Duration |
|---|---|
| `POST /sync-now` round trip | ~0.3–1 s |
| Overleaf pushes to Dropbox cloud | ~1–3 s |
| Dropbox client pulls locally | ~1–3 s |
| Hook settle wait | 3 s |

Repeat edits within the 30-second debounce window cost ~0.3 s (debounce check + exit). The manual `sync` command uses a 10-second settle wait for thoroughness.
