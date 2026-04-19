# Contributing

Thanks for considering a contribution.

## Setup

```bash
git clone https://github.com/hanlulong/overleaf-sync-now
cd overleaf-sync-now
python -m venv .venv && source .venv/bin/activate   # or `.venv\Scripts\activate` on Windows
pip install -e .
overleaf-sync-now --version
```

## Testing your changes locally

After editing `src/overleaf_sync_now/cli.py`, the editable install picks up changes immediately — just rerun the CLI:

```bash
overleaf-sync-now status
overleaf-sync-now doctor
```

To install the changes back into your everyday `uv tool` install (so the Claude Code hook picks them up):

```bash
uv tool install --reinstall --from . overleaf-sync-now
```

## Style

- Python 3.8+ compatible. Avoid 3.10+ syntax (`match`, `|` type unions, etc.).
- Standard library where possible. New runtime deps need a justification in the PR.
- Errors raised by `trigger_sync` and friends should be one of: `AuthExpired`, `RateLimited`, or a `RuntimeError` with a user-actionable message including the path / URL / next step.
- Anything written to disk should go through `_atomic_write_text` (writes to `.tmp` + `os.replace`) — particularly `~/.claude/settings.json`.
- Anything that reads cookies / makes HTTP calls should be skippable (try/except around imports of optional deps; cheap-existence checks before expensive operations).
- Hot paths (the hook, anything called from it) must avoid network calls when avoidable. Use the validation cache (`_cookies_recently_validated`) rather than calling `_validate_cookies` with `use_cache=False`.

## Areas where help is especially welcome

| Area | Why |
|---|---|
| macOS / Linux end-to-end testing | Developed on Windows. CI verifies install + smoke tests but no real Overleaf round-trip. |
| Self-hosted Overleaf / Server Pro | The `BASE` URL is hardcoded to `https://www.overleaf.com`. Needs a `--base-url` (or env var) and a way to flow it through every HTTP call. |
| Background poller | A daemon mode that polls Overleaf periodically and triggers Dropbox sync, for users who want fresh files without an active AI session. |
| PyPI publishing | Currently install is git-only. PyPI makes `pip install overleaf-sync-now` work for users who don't want `uv`. |
| Standalone `.exe` build (PyInstaller) | For users who don't want any Python tooling. Multi-platform build complexity. |
| Multi-account support | Cache currently holds one session. Switching accounts means clearing and re-auth. |
| Bash / zsh / PowerShell completion | Subcommand + flag completion. |

## Pull requests

Small focused PRs are easier to review than large ones. If you're touching the auth chain or the install logic, please:

1. Run `overleaf-sync-now doctor` before and after your change and paste both outputs in the PR.
2. Bump the version in `pyproject.toml` and `src/overleaf_sync_now/__init__.py`.
3. Add a CHANGELOG.md entry under a new version header.

## Releasing (maintainers)

1. Update version in `pyproject.toml` + `src/overleaf_sync_now/__init__.py`.
2. Update CHANGELOG.md with the date.
3. `git tag v<version> && git push --tags`.
4. CI runs the matrix; users get the new version with `uv tool install --reinstall --refresh`.
