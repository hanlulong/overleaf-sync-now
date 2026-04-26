# Changelog

All notable changes to `overleaf-sync-now`. Versions follow [SemVer](https://semver.org/).

## 0.3.0 — 2026-04-26

Robust project resolution. Fixes a class of silent-wrong-project bugs whose
canonical instance was reported as: an Overleaf account with two projects of
the same name (one trashed) silently auto-linked to the trashed one, so
`sync` returned `no_change` while the active project had fresh edits.

The single-line dict overwrite in `_refresh_index()` was the proximate cause,
but the resolver was the architectural problem: it threw away every
disambiguating field (`trashed`, `archived`, `lastUpdated`, `ownerId`) and
had no read-side validation gate. This release rebuilds resolution as seven
independent layers so that no single failure mode produces a wrong-project
sync.

**The seven layers:**

1. **Richer project index format (v2).** `~/.overleaf-sync/projects.json` now
   stores the full record per project (`id`, `name`, `trashed`, `archived`,
   `lastUpdated`, `ownerId`) instead of a `{name: id}` dict. v1 files on
   disk are treated as expired and re-fetched on first use.
2. **Policy resolver, refuse on ambiguity.** Auto-link filters out trashed
   and archived projects by default and tries case-sensitive exact match
   before falling back to case-insensitive. When two non-trashed projects
   share a name, the resolver refuses to guess and prints all candidates
   (with IDs and `lastUpdated`) plus the exact `link <id> <folder>` command
   to copy-paste. The previous behavior — silently picking whichever the
   dict's last write wrote — was the proximate cause of the reported bug.
3. **Auto-write `.overleaf-project` marker on successful auto-link.** Once
   resolution is unambiguous, the marker is persisted in the folder so
   subsequent syncs are immune to later renames, newly-trashed duplicates,
   or anything else that mutates the projects list. Marker now carries
   `linked_at`, `name_at_link_time`, and `source` (`auto-link` /
   `auto-link-fingerprint` / `link`) for debuggability. Old markers
   containing just `{"project_id": "..."}` still resolve identically.
4. **Validate marker against the cached index on every resolution.** No
   extra network call. Surfaces the cases the earlier layers can't reach
   on their own: marker pointing at a project that's now trashed, archived,
   deleted, or owned by a different account (e.g. cookies switched). Warns
   to stderr but proceeds — the user explicitly wrote the marker, so we
   never refuse to sync, we just stop being silent.
5. **Better `status` and `projects` output.** `projects` now shows columns
   `NAME | PROJECT_ID | FLAGS | LAST_UPDATED`, sorted by `lastUpdated`
   desc, with inline flags `T` (trashed), `A` (archived), `DUP` (name
   shared with another project). `status` shows resolution provenance
   (marker file vs auto-link), the project's current name from the index,
   `lastUpdated`, and any trashed/archived flag.
6. **(a) Fingerprint-based ambiguity disambiguation.** When the policy
   resolver returns "ambiguous," probes each candidate's `/updates` top
   page (one HTTP request per candidate, only on ambiguity) and counts how
   many recent dropbox-origin pathnames exist as local files. If exactly
   one candidate has matches and the rest have zero, picks it
   automatically — converting the reported bug into auto-resolved-correctly
   even before the trashed filter would refuse. Falls back to the layer-2
   refusal when fingerprints don't disambiguate.
7. **(b) Fingerprint sanity gate on every sync.** Inside `refresh_project`,
   before returning `no_change`, walks the top dropbox-origin entries from
   `/updates` and warns to stderr if none of their pathnames map to local
   files. Catches the only failure class the earlier layers can't: right
   account, valid non-trashed project, but the wrong project (typo'd
   manual `link`, fork, etc.). Cost: ~10 `Path.exists()` calls; no extra
   network.

**Other 0.3.0 changes:**

- **Concurrency safety for atomic writes.** Both `_atomic_write_text`
  (used for state, versions, projects index, cookie cache, marker, and
  `~/.claude/settings.json`) and `_extract_files` (used to write zip
  contents into the project folder) had concurrency bugs that triggered
  when two PreToolUse hooks fired on the same project — e.g. two Codex
  CLI windows editing the same paper:
  - **Tempfile name collision.** The previous names were deterministic
    (`<path>.tmp`) and PID-only (`<file>.tmp-<pid>`). Two writers in the
    same process or across rapid PID-reuse could open the same tempfile
    in `w` mode and stomp each other's writes.
  - **Windows `ACCESS_DENIED` on contending `os.replace`.** When two
    processes call `MoveFileEx` against the same target near-simultaneously,
    one transiently fails. The original code would propagate this as a
    "refresh failed" hook error even though the target was correctly
    written by the other process.
  Both helpers now share `_unique_tmp_path` (PID + 8 random hex chars)
  and `_replace_with_retry` (jittered exponential backoff up to ~700ms).
  New `ConcurrencyTests` exercise 20-thread contention on `_atomic_write_text`,
  10-thread contention on `_extract_files`, and 10-thread contention on
  marker writes — all stable across stress runs.
- **Documented lost-update semantics.** `state.json` and `versions.json`
  are read-modify-write — two processes updating *different* keys at
  exactly the same moment can lose one update (last-writer-wins on the
  whole file). Effect on `state.json` is debounce-timestamp drift
  (harmless). Effect on `versions.json` is that the lost project's
  cached `toV` reverts to absent, triggering a bootstrap on next sync —
  bandwidth-wasteful (full zip download) but not destructive (the
  hash-diff + `protect_recent_seconds` guards keep local edits safe).
  Per-key locking would eliminate this but adds cross-platform
  complexity for a benign loss; not implemented.
- `_extract_files` now skips any zip entry whose basename is
  `.overleaf-project`. Defense in depth: if Overleaf's Dropbox bridge
  uploads a marker to the project, it can never overwrite a different
  machine's local marker on the next zip download.
- **POSIX `0o600` for every file under the data dir.** Previously only
  `cookies.json` was permission-restricted; `versions.json`, `state.json`,
  `projects.json`, and `.validated-at` all leaked project IDs, names,
  and ownership metadata to other local users on shared POSIX boxes.
  The atomic-write path now applies `0o600` to anything written under
  `<DATA_DIR>` (per `_is_under_data_dir`), but leaves `.overleaf-project`
  markers in user folders at default perms so Dropbox can still read
  them. New POSIX-only `FilePermsTests` cover both halves.
- **Hang-proof `/project` fetch.** `_refresh_projects_records` now passes
  `timeout=15` (matching `fetch_updates`) and converts a sandbox-block
  socket error into the existing `_SANDBOX_HINT` instead of letting the
  `requests` exception propagate. Previously a network stall during the
  daily project-list refresh could hang the hook indefinitely.
- **`doctor` is crash-proof.** Wrapped `find_linked_folder` in `cmd_doctor`
  with try/except so a malformed cwd or transient resolver error
  produces a clean diagnostic line rather than a Python traceback.
  Doctor must be the most robust command — it's what users run when
  things are already broken.
- Docs: `architecture.md` and `operations.md` now describe the data-dir
  resolution order (`$OVERLEAF_SYNC_DATA_DIR` → `~/.claude/overleaf-data/`
  → `~/.overleaf-sync/`) instead of hard-coding the legacy path.
  Architecture page also documents the new auto-link / marker /
  fingerprint flow.
- `cmd_link` writes the marker via the new `_write_marker` helper (atomic
  write, with metadata) and warns up front if the supplied project ID is
  trashed, archived, or missing from the cached index.
- The hook (which fires on every keystroke) suppresses Layer 4 marker
  validation warnings to avoid spam. Layer 6b's sanity warning still
  fires through the hook path. Interactive `status` and `sync` print
  full diagnostics.
- New unit tests under `tests/` cover the policy resolver, index
  migration, fingerprint matcher, and marker round-trip behavior. Run
  with `python -m unittest discover tests`.

**Migration:** existing `~/.overleaf-sync/projects.json` files are detected
as v1 and refreshed automatically on first use. Existing
`.overleaf-project` markers (containing only `project_id`) keep working
unchanged. No user action required.

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
