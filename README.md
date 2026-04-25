# overleaf-sync-now

> **Overleaf sync skill for Claude Code & Codex CLI** — keep local `.tex` / `.bib` files fresh before every AI edit.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![Install: uv](https://img.shields.io/badge/install-uv-orange.svg)](https://github.com/astral-sh/uv)

An **agent skill** (plus a CLI and a Claude Code PreToolUse hook) that fixes a specific failure mode: when you edit a paper on overleaf.com and then ask Claude Code or Codex CLI to keep working, the AI agent reads the *stale* local Dropbox copy and silently overwrites your fresh web edits. This package keeps the local `.tex` / `.bib` / `.cls` / `.sty` / `.bst` files in sync with Overleaf, on demand, before every AI Read / Edit / Write — without changing your existing Overleaf-Dropbox setup.

### Is this for you?

- ✅ You write papers in **Overleaf Premium** with Dropbox sync enabled (`Dropbox/Apps/Overleaf/<project>/`)
- ✅ You edit `.tex` files locally with **Claude Code** or **Codex CLI**
- ✅ You also edit on **overleaf.com** sometimes — from another device, in a browser, or with collaborators

If you only ever edit locally *or* only ever edit on the web, your local copy is never stale and you don't need this. Same if you don't use Dropbox or AI agents.

---

## Install

In Claude Code or Codex CLI, paste this prompt:

```text
Install overleaf-sync-now from https://github.com/hanlulong/overleaf-sync-now using uv tool install, then run `overleaf-sync-now install`.
```

The agent installs everything and runs setup. **Restart the agent afterward** (`/exit` then `claude` in Claude Code; equivalent in Codex).

*On Windows with Chrome 130 or later, also run `overleaf-sync-now login` once — Chrome's new App-Bound Encryption blocks the silent cookie path. [Details](docs/authentication.md#why-chrome-130-needs-the-login-path).*

<details>
<summary><b>Manual install</b> (no AI agent)</summary>

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && \
  export PATH="$HOME/.local/bin:$PATH" && \
  uv tool install --from git+https://github.com/hanlulong/overleaf-sync-now overleaf-sync-now && \
  overleaf-sync-now install
```

**Windows (PowerShell):**
```powershell
irm https://astral.sh/uv/install.ps1 | iex
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
uv tool install --from git+https://github.com/hanlulong/overleaf-sync-now overleaf-sync-now
overleaf-sync-now install
```

</details>

### Verify

After restart, edit any `.tex` file under `Dropbox/Apps/Overleaf/<project>/` — the hook fires automatically. Or check manually:

```bash
overleaf-sync-now status   # cookie + linked project
overleaf-sync-now sync     # trigger a sync, print latency
```

---

## Why

You use Dropbox with Overleaf because it gives you **free multi-device sync** — an edit on your laptop is instantly readable on your desktop, your iPad, and your collaborator's machine. AI agents like Claude Code and Codex CLI plug into that setup naturally: they just read and write the local `.tex` file like you do.

The problem: Overleaf's Dropbox sync polls the **Overleaf-web → local** direction every 10–20 minutes. So if you've edited the paper on overleaf.com and then ask the AI to keep working, it reads a stale local file and silently overwrites the changes you just made — restoring deleted paragraphs, undoing fresh edits, all while reporting a successful tool call.

`overleaf-sync-now` checks Overleaf for fresh content immediately before each AI edit and pulls only what changed. Dropbox keeps doing its multi-device job; the AI loop stops clobbering your work. Nothing else in your setup changes.

| Sync direction | Stock Overleaf + Dropbox | With `overleaf-sync-now` |
|---|---|---|
| Local edit → Overleaf | a few seconds | a few seconds *(unchanged)* |
| **Overleaf web edit → local Dropbox** | **10–20 minutes** (next poll) | **~1 s probe; ~5–10 s if a real change must be pulled** |
| AI agent reads a stale local file | yes, often | **no** |

When nothing changed on the Overleaf side, the hot path is ~0.3–1 s (a single read-only probe, no zip download). When something changed, the tool downloads the project zip once and writes only the files whose content differs from local — typically 5–10 s for a normal paper.

---

## How it works

<p align="center">
  <img src="docs/workflow.svg" alt="Workflow: AI agent edit triggers PreToolUse hook, which probes Overleaf's version history (/updates), and only when web-side edits exist does it download the zip and extract changed files. Then the AI edits the fresh local file." width="100%">
</p>

1. The **PreToolUse hook** intercepts every `Read` / `Edit` / `Write` / `MultiEdit` of `.tex` / `.bib` / `.cls` / `.sty` / `.bst` files. Other tools and other file types pass through.
2. **Auto-link** maps `…/Apps/Overleaf/<name>/` to its Overleaf project ID via a 24-hour-cached project list. No per-project setup; override with `overleaf-sync-now link <id>` when names differ.
3. **Version-match probe**: `GET /project/<id>/updates` returns Overleaf's history (`fromV` / `toV` / changed pathnames / origin). The tool caches the latest `toV` per project.
   - Latest `toV` matches cached → skip, no download.
   - Updates exist but are all Dropbox-origin (echoes of our own local saves round-tripping through Dropbox) → skip, no download.
   - A web-origin update exists → `GET /project/<id>/download/zip` and extract only the changed pathnames, hash-compared so unchanged files aren't rewritten.
4. **Data-safety guard**: extraction never overwrites a local file modified in the last 30 seconds — protects an in-progress local save that hasn't yet propagated Dropbox → Overleaf. Override with `sync --force`.
5. **Debounce**: 30 s per project. A flurry of AI edits share one probe.

For deeper details, see [`docs/architecture.md`](docs/architecture.md).

---

## Subcommands

```
overleaf-sync-now --version                              # print package version
overleaf-sync-now install                                # one-shot setup (idempotent)
overleaf-sync-now login                                  # browser-assisted login (Chrome 130+ Windows)
overleaf-sync-now setup                                  # auth wizard (auto-detect from existing browsers)
overleaf-sync-now save-cookie <value>                    # paste a cookie value directly (last-resort)
overleaf-sync-now sync [folder] [--force]                # refresh against Overleaf; --force re-extracts
overleaf-sync-now status [folder] [--quick]              # cookie validity + linked project + cached toV
overleaf-sync-now projects [--refresh]                   # list Overleaf projects (name + ID)
overleaf-sync-now doctor [folder]                        # full diagnostic, including a /updates probe
overleaf-sync-now link <id> .                            # override auto-link with a marker file
overleaf-sync-now uninstall                              # remove skill + hook (keeps cookies)
```

`sync` and `status` default to the current directory. Run `overleaf-sync-now --help` for full usage.

---

## Documentation

| Topic | Where |
|---|---|
| **Authentication** — auth chain, Chrome 130+ ABE, manual paste fallback | [`docs/authentication.md`](docs/authentication.md) |
| **Architecture** — files written, hook internals, exit codes, latency budget, reverse-engineering | [`docs/architecture.md`](docs/architecture.md) |
| **Operations** — cookie maintenance, rate limits, upgrading, uninstalling, multi-account | [`docs/operations.md`](docs/operations.md) |
| **Troubleshooting** — common errors and fixes, diagnostic commands | [`docs/troubleshooting.md`](docs/troubleshooting.md) |
| **Contributing** — local setup, style, areas needing help | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

---

## Related projects

Each of these solves part of the same problem, but trades away either **multi-device Dropbox sync** or **AI-native automation** (or both):

- **[Overleaf Git integration](https://www.overleaf.com/learn/how-to/Using_Git_and_GitHub)** (built into Overleaf Premium) — clone the project locally, then `git pull` / `git push` by hand. Replaces Dropbox with a one-machine clone, and every sync is manual.
- **[overleaf-sync (olsync)](https://github.com/moritzgloeckl/overleaf-sync)** — Python script that mirrors an Overleaf project to a local folder. Replaces Dropbox; you run `olsync download` before each AI session yourself.
- **[Overleaf Workshop (VS Code)](https://github.com/overleaf-workshop/Overleaf-Workshop)** — true real-time WebSocket editing inside VS Code. Beautiful for solo human use, but takes you out of your AI agent's editor and gives up the multi-device Dropbox story.
- **[pyoverleaf](https://github.com/jkulhanek/pyoverleaf)** — general Python API for Overleaf. A library, not a fix; useful if you want to script your own integration.

`overleaf-sync-now` is the only tool that **keeps Dropbox** (so all your devices and collaborators stay in sync the way they already do) **and fires automatically** (so the AI agent loop never reads a stale file). Smaller, narrower, invisible.

---

## License

[MIT](LICENSE)

## Credits

Created by **[Lu Han](https://luhan.io/)**. Published by **[OpenEcon.ai](https://openecon.ai/)**.

If this saved you time, a ⭐ on the repo helps other researchers find it.
