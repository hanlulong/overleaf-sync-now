# Changelog

All notable changes to `overleaf-sync-now`. Versions follow [SemVer](https://semver.org/).

## 0.1.1 — 2026-04-24

Diagnostic polish. Surfaces the right error when the host shell blocks the
outbound socket (sandboxed Codex CLI, some CI runners), instead of letting
the failure masquerade as an auth problem.

- **Sandbox-block detection**: `fetch_updates`, `download_zip`, and
  `trigger_sync` now recognize `WinError 10013` / `forbidden by its access
  permissions` / `EACCES` / `EPERM` / "Permission denied" at the socket
  layer and raise a specific hint ("Outbound HTTPS to Overleaf was blocked
  by the host environment ... approve the command in your sandbox policy")
  instead of a generic network error.
- `get_session` probes connectivity before raising "No valid Overleaf
  cookies" so a sandbox block doesn't steer the AI agent into a pointless
  `setup` / `login` / `doctor` loop (they all hit the same blocked socket).
- `status` now prints `Cookie auth: UNKNOWN — outbound HTTPS is blocked ...`
  instead of a misleading `INVALID` verdict when the probe can't run.
- `sync` (both default and `--legacy`) catches `RuntimeError` cleanly — the
  sandbox hint reaches the user as a one-line `ERROR:` message, not a
  Python traceback.
- The PreToolUse hook distinguishes sandbox errors from transient failures:
  it no longer says "will retry after debounce" for errors that won't
  self-heal.

**SKILL.md refreshed**: the subcommand descriptions had drifted — `sync`
still said "waits 10s for Dropbox to settle" (the pre-0.1.0 `/sync-now`
behavior), and `login`/`save-cookie`/`doctor`/`projects` weren't listed.
Rewrote the subcommand block to match 0.1.0+ behavior, documented
`--force`/`--legacy`, and added a new **Sandbox notes** section explicitly
telling the AI playbook-reader not to reach for auth-recovery commands
when the error is an outbound-socket permission problem.

## 0.1.0 — 2026-04-24

Switch default refresh from `POST /dropbox/sync-now` to a version-match path.

**Why:** The old path enqueued a per-user "poll Dropbox" job in Overleaf's
`tpdsworker` queue, which is serialized with webhook-triggered per-file
updates coming the other direction. Frequent AI-edit hooks therefore starved
local→Overleaf propagation of queue slots, making users' own local saves feel
slower *after* installing the tool than before. See
`services/web/app/src/Features/ThirdPartyDataStore/TpdsUpdateSender.mjs` in
the open-source Overleaf repo for the comment that confirms this.

**What the new path does:**
- `GET /project/<id>/updates` is the probe (~0.3 s, ~30 KB). Returns the
  version history with per-update pathnames and an `origin.kind` field.
- Cached `toV` per project in `versions.json`; if latest `toV` matches, exit.
- Walk updates from latest back to cached `toV`; skip updates whose
  `origin.kind === "dropbox"` — those are the local→Dropbox→Overleaf
  round-trips of our own saves, and local already has the content.
- Only when something web-origin remains do we `GET /download/zip` and
  extract just the changed pathnames, writing atomically and skipping any
  file whose bytes already match local.
- Web-origin edits show up in `/updates` within ~0.5 s of the edit committing
  on Overleaf — measured, not estimated.

**New flags on `overleaf-sync-now sync`:**
- `--legacy`: fall back to the old `/sync-now` path (kept as escape hatch).
- `--force`: always download the zip and re-extract, bypassing version-match.

**Bootstrap:** on first run with no cached `toV` for a project, the tool
downloads the zip once to guarantee local is in sync (hash-compare keeps
Dropbox upload pressure at zero when local already matches).

**Data-loss guard:** the extraction step never overwrites a local file that
was modified in the last 30 seconds. This closes a race: if the user saved
locally a few seconds ago and Dropbox hasn't yet pushed the save up to
Overleaf, the zip still contains the *old* version and a naive overwrite
would clobber the in-progress local edit. Pass `--force` to disable the
guard.

**Hook matcher extended to Read:** `Read|Edit|Write|MultiEdit` (was
`Edit|Write|MultiEdit`). The cheap `/updates` probe makes refresh-on-read
affordable, and it keeps Claude's reasoning grounded in current content
rather than stale reads. Existing installs need `overleaf-sync-now install`
re-run once to pick up the new matcher in `~/.claude/settings.json`.

**Defensive cache-ahead branch:** if local cache claims we've synced to a
higher `toV` than Overleaf currently reports (corruption, cross-machine
drift, or Overleaf history rewind), `refresh_project` re-bootstraps instead
of silently reporting a dropbox_echo with a backwards version delta.

**Diagnostics:** `status` now reports `Cached toV`; `doctor` adds a `[5]`
section probing `/updates` end-to-end.

## 0.0.1 — 2026-04-19

First public release. See the [v0.0.1 release notes](https://github.com/hanlulong/overleaf-sync-now/releases/tag/v0.0.1) for the full feature list.
