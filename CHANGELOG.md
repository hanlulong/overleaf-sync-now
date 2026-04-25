# Changelog

All notable changes to `overleaf-sync-now`. Versions follow [SemVer](https://semver.org/).

## 0.2.2 — 2026-04-25

Positioning. Lead with the failure mode this tool actually solves, instead of with a category label ("agent skill") that the search results show is now table stakes.

- **README opener**: subtitle is now a single literal sentence describing the failure — *"Stops Claude Code and Codex CLI from silently overwriting your Overleaf web edits with a stale local Dropbox copy."* New "The failure mode" section above the install block walks through the bug concretely (10–20 min Dropbox poll, AI reads stale file, edits round-trip, web edits vanish, success reported).
- **`pyproject.toml description`** rewritten to lead with the same failure-mode sentence. PyPI search and AI assistants weight this field heavily.
- **`CITATION.cff title`** reframed: *"stop AI coding agents from overwriting Overleaf web edits with a stale local Dropbox copy"* — replaces the older "instant Overleaf-to-Dropbox sync" wording.
- **GitHub repo description** updated via API to match.
- **README's "Related projects"** section is now a comparison table including `aloth/overleaf-skill` (the namesake collision on npm/Homebrew), the major Overleaf MCP servers, and olsync. Lays out who keeps Dropbox vs. replaces it, and who fires automatically vs. manually. We're the only row with ✅ on both.

No code change.

## 0.2.1 — 2026-04-25

Discoverability metadata. No functional change.

- `pyproject.toml` description reframed as "Overleaf sync skill for Claude Code and Codex CLI" (previously led with the implementation detail). Lifts "skill" / "Claude Code" / "Codex CLI" into the first line — these are the high-intent search terms users actually type, and PyPI / Google index that field heavily.
- `pyproject.toml` keywords reorganized and expanded: added `overleaf-skill`, `claude-skill`, `claude-code-skill`, `codex-skill`, `ai-skill`. Existing keywords preserved.
- `README.md` opening: H1 subtitle and lede paragraph now lead with "Overleaf sync skill for Claude Code & Codex CLI" and explicitly name the failure mode it fixes ("AI agent reads stale local Dropbox copy and silently overwrites fresh web edits"). Front-loads the search terms users actually type.
- GitHub topics expanded to include `overleaf-skill`, `claude-skill`, `claude-code-skill`, `codex-skill` (set via API, not in the repo).
- GitHub repo description updated to match the new positioning.

## 0.2.0 — 2026-04-25

Cleanup release. The codebase, CLI surface, and docs are now exclusively the version-match refresh path.

- Removed `sync --legacy` (and the supporting `trigger_sync` / `fetch_csrf` functions), `sync --no-wait`, and the `MANUAL_WAIT_SECONDS` constant. The CLI surface is now just `sync [folder] [--force]`.
- `package.description`, the module docstring, `SKILL.md`, `README.md`, `CITATION.cff`, and `docs/*` rewritten to describe only the current refresh path. No conditional or "kept as fallback" framing remains.
- Internal cleanup: `_data_dir()` variable rename, comment polish, removal of the `--legacy` hint in `doctor`'s probe-failed branch.

No behavioral change for any user who wasn't passing `--legacy` / `--no-wait`.

## 0.1.1 — 2026-04-24

Diagnostic polish for sandboxed shells (Codex CLI, some CI runners).

- **Sandbox-block detection** in every network-using subcommand. `WinError 10013` / `forbidden by its access permissions` / `EACCES` / `EPERM` / "Permission denied" at the socket layer raise a specific hint ("Outbound HTTPS to Overleaf was blocked by the host environment ... approve the command in your sandbox policy") instead of a generic network error.
- `get_session` probes connectivity before raising "No valid Overleaf cookies" so a sandbox block doesn't steer the AI agent into a pointless `setup` / `login` / `doctor` loop (they all hit the same blocked socket).
- `status` prints `Cookie auth: UNKNOWN — outbound HTTPS is blocked ...` instead of a misleading `INVALID` verdict when the probe can't run.
- `sync` catches `RuntimeError` cleanly — the sandbox hint reaches the user as a one-line `ERROR:` message, not a Python traceback.
- The PreToolUse hook distinguishes sandbox errors from transient failures: no more "will retry after debounce" for errors that won't self-heal.
- New **Sandbox notes** section in `SKILL.md` telling the AI playbook-reader not to reach for auth-recovery commands when the failure shape is an outbound-socket permission problem.

## 0.1.0 — 2026-04-24

Switched the default refresh path from `POST /project/<id>/dropbox/sync-now` to a cheap version-match probe + selective zip extract. The old endpoint enqueued a per-user "poll Dropbox" job in Overleaf's `tpdsworker` queue, which is serialized with webhook-triggered per-file updates coming the other direction; frequent AI-edit hooks therefore starved local→Overleaf propagation of queue slots, making users' own local saves feel slower *after* installing the tool than before. See `services/web/app/src/Features/ThirdPartyDataStore/TpdsUpdateSender.mjs` in the open-source Overleaf repo for the comment that confirms this.

**The new path:**

- `GET /project/<id>/updates` is the probe (~0.3 s, ~30 KB). Returns the version history with per-update pathnames and an `origin.kind` field.
- Cached `toV` per project in `versions.json`; if latest `toV` matches, exit.
- Walk updates from latest back to cached `toV`; skip updates whose `origin.kind === "dropbox"` — those are the local→Dropbox→Overleaf round-trips of our own saves, and local already has the content.
- Only when something web-origin remains do we `GET /download/zip` and extract just the changed pathnames, writing atomically and skipping any file whose bytes already match local.
- Web-origin edits show up in `/updates` within ~0.5 s of the edit committing on Overleaf — measured, not estimated.

**Other 0.1.0 changes:**

- New `sync --force` flag: always download the zip and re-extract, bypassing version-match.
- **Bootstrap**: on first run with no cached `toV` for a project, the tool downloads the zip once to guarantee local is in sync (hash-compare keeps Dropbox upload pressure at zero when local already matches).
- **Data-safety guard**: the extraction step never overwrites a local file modified in the last 30 seconds. Closes a race where Dropbox hasn't yet pushed the user's in-progress local save up to Overleaf, so the zip would have stale content. Pass `--force` to disable.
- **Hook matcher extended to Read**: `Read|Edit|Write|MultiEdit` (was `Edit|Write|MultiEdit`). The cheap `/updates` probe makes refresh-on-read affordable, and it keeps Claude's reasoning grounded in current content rather than stale reads. Existing installs need `overleaf-sync-now install` re-run once to pick up the new matcher in `~/.claude/settings.json`.
- **Defensive cache-ahead branch**: if local cache claims we've synced to a higher `toV` than Overleaf currently reports (corruption, cross-machine drift, history rewind), the refresh re-bootstraps instead of silently reporting a backwards version delta.
- **Diagnostics**: `status` reports `Cached toV`; `doctor` adds a `[5]` section probing `/updates` end-to-end.

## 0.0.1 — 2026-04-19

First public release. See the [v0.0.1 release notes](https://github.com/hanlulong/overleaf-sync-now/releases/tag/v0.0.1) for the full feature list.
