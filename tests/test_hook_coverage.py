"""PreToolUse hook coverage (0.5.0).

Pins:
  * fail-closed resolution: a path under Apps/Overleaf/<name>/ whose project
    cannot be resolved (no auth, no match, ambiguous name) BLOCKS writes
    (exit 2) and only warns on reads. These run the REAL resolve_project /
    find_linked_folder against fixtures, not a stub.
  * Bash / PowerShell commands are scanned for LaTeX paths; Codex apply_patch
    payloads are parsed for `*** Update/Add/Delete File:` / `*** Move to:`.
  * reads debounce for 30 s, writes for only 3 s (Read then Write probes twice).
  * the write guard: when the refresh just rewrote the target, the write is
    blocked with a "re-read" message.
  * web-side deletes / renames block edits of the old path.
  * credential hygiene in the auth-recovery text.

No network: refresh_project / get_session / fetch_updates are monkeypatched.

Run with: python -m unittest discover tests
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402

PID = "e" * 24
PID2 = "f" * 24


class _HookHarness(unittest.TestCase):
    """Temp data dir + temp Dropbox tree; refresh_project recorded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name).resolve()
        self.data = self.base / "data"
        self.data.mkdir()
        self.overleaf = self.base / "Dropbox" / "Apps" / "Overleaf"
        self.overleaf.mkdir(parents=True)
        self._orig = {name: getattr(cli, name) for name in (
            "CACHE_DIR", "CACHE_FILE", "STATE_FILE", "VERSIONS_FILE", "INDEX_FILE",
            "WEB_REMOVED_FILE", "_VALIDATION_FILE", "refresh_project", "get_session",
            "fetch_updates", "_resolve_cookies",
        )}
        cli.CACHE_DIR = self.data
        cli.CACHE_FILE = self.data / "cookies.json"
        cli.STATE_FILE = self.data / "state.json"
        cli.VERSIONS_FILE = self.data / "versions.json"
        cli.INDEX_FILE = self.data / "projects.json"
        cli.WEB_REMOVED_FILE = self.data / "web-removed.json"
        cli._VALIDATION_FILE = self.data / ".validated-at"
        self.refresh_calls = []
        self.on_refresh = None

        def fake_refresh(pid, folder, **kw):
            self.refresh_calls.append((pid, pathlib.Path(folder)))
            if self.on_refresh:
                return self.on_refresh(pid, pathlib.Path(folder))
            return "no_change"

        cli.refresh_project = fake_refresh
        self._stdin, self._stderr = sys.stdin, sys.stderr

    def tearDown(self):
        sys.stdin, sys.stderr = self._stdin, self._stderr
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    # -- fixtures --
    def project(self, name, pid=PID, files=("main.tex",)):
        folder = self.overleaf / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": pid}))
        for f in files:
            p = folder / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("ORIGINAL " + f)
        return folder

    def index(self, records, ts=None):
        cli.INDEX_FILE.write_text(json.dumps({
            "version": cli.INDEX_FORMAT_VERSION,
            "ts": time.time() if ts is None else ts,
            "projects": records,
        }))

    def run_hook(self, payload):
        sys.stdin = io.StringIO(json.dumps(payload))
        sys.stderr = err = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_hook([])
        finally:
            sys.stderr = self._stderr
        code = cm.exception.code
        return (0 if code is None else code), err.getvalue()


# ---------------------------------------------------------------------------
# fail closed on unresolved Apps/Overleaf/<name>/ folders (OSN-02 / OSN-M3)
# ---------------------------------------------------------------------------
class FailClosedResolutionTests(_HookHarness):
    def _no_auth(self):
        def boom(force_refresh=False):
            raise cli.AuthExpired("No valid Overleaf cookies.\n\n" + cli._AUTH_RECOVERY)
        cli.get_session = boom

    def _target(self, name="My Paper"):
        folder = self.overleaf / name
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / "main.tex"
        f.write_text("x")
        return f

    def test_no_auth_no_marker_blocks_write(self):
        # The first-use case: install ran, auth not captured yet, no marker.
        self._no_auth()
        f = self._target()
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
        self.assertEqual(code, 2)
        self.assertIn("overleaf-sync-now login", err)
        self.assertIn("NEVER ask the user to paste the cookie", err)
        self.assertEqual(self.refresh_calls, [])
        self.assertFalse((f.parent / cli.PROJECT_MARKER).exists())

    def test_no_auth_fd_redirect_into_tex_blocks(self):
        # `1>` / `&>` are writes too; they used to be classified as reads.
        self._no_auth()
        f = self._target()
        for redirect in ("1>", "&>"):
            code, err = self.run_hook({
                "tool_name": "Bash", "cwd": str(self.base),
                "tool_input": {"command": f"echo x {redirect}'{f}'"},
            })
            self.assertEqual(code, 2, (redirect, err))
            self.assertIn("Bash blocked", err)

    def test_no_auth_no_marker_read_warns_only(self):
        self._no_auth()
        f = self._target()
        code, err = self.run_hook({"tool_name": "Read", "tool_input": {"file_path": str(f)}})
        self.assertEqual(code, 0)
        self.assertIn("Read allowed", err)

    def test_ambiguous_name_blocks_with_candidates(self):
        self.index([
            {"id": PID, "name": "Paper", "trashed": False, "archived": False, "lastUpdated": "2026-10-01"},
            {"id": PID2, "name": "Paper", "trashed": False, "archived": False, "lastUpdated": "2026-11-01"},
        ])

        def no_fp(pid):
            raise cli.AuthExpired("x")
        cli.fetch_updates = no_fp  # fingerprint tie-break unavailable
        f = self._target("Paper")
        code, err = self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(f)}})
        self.assertEqual(code, 2)
        self.assertIn(PID, err)
        self.assertIn(PID2, err)
        self.assertIn("overleaf-sync-now link <project_id>", err)
        self.assertLess(err.index(PID2), err.index(PID))  # most recent first

    def test_no_matching_project_blocks_write(self):
        self.index([{"id": PID, "name": "Something Else", "trashed": False, "archived": False}])
        f = self._target("Unknown Paper")
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
        self.assertEqual(code, 2)
        self.assertIn("no living", err)
        self.assertIn("link <project_id>", err)

    def test_stale_index_is_refetched_before_declaring_none(self):
        # A project created after the 24 h index was cached must auto-link.
        self.index([{"id": PID, "name": "Old", "trashed": False, "archived": False}],
                   ts=time.time() - 3600)
        calls = []

        def fake_refresh_records(strict=False):
            calls.append(strict)
            return [{"id": PID2, "name": "Brand New", "trashed": False, "archived": False}]
        orig = cli._refresh_projects_records
        cli._refresh_projects_records = fake_refresh_records
        try:
            f = self._target("Brand New")
            code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
        finally:
            cli._refresh_projects_records = orig
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [True])
        self.assertEqual(self.refresh_calls[0][0], PID2)

    def test_network_failure_passes_through(self):
        def blocked(force_refresh=False):
            raise RuntimeError(cli._SANDBOX_HINT)
        cli.get_session = blocked
        f = self._target()
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
        self.assertEqual(code, 0)
        self.assertIn("could not reach Overleaf", err)

    def test_outside_apps_overleaf_is_untouched(self):
        self._no_auth()
        other = self.base / "repo" / "paper.tex"
        other.parent.mkdir()
        other.write_text("x")
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(other)}})
        self.assertEqual((code, err), (0, ""))

    def test_invalid_marker_id_is_ignored(self):
        # A marker rewritten (it round-trips through Overleaf) to a path
        # fragment must never reach a request URL.
        folder = self.overleaf / "Paper"
        folder.mkdir()
        (folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": "../../user/settings"}))
        res = cli.resolve_project(folder / "main.tex", verbose=False, read_only=True)
        self.assertNotEqual(res["project_id"], "../../user/settings")
        self.index([{"id": PID, "name": "Paper", "trashed": False, "archived": False}])
        res = cli.resolve_project(folder / "main.tex", verbose=False, read_only=True)
        self.assertEqual((res["reason"], res["project_id"]), ("ok", PID))

    def test_sync_reports_auth_not_unmanaged(self):
        self._no_auth()
        f = self._target()
        sys.stderr = err = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_sync([str(f.parent)])
        finally:
            sys.stderr = self._stderr
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("overleaf-sync-now login", err.getvalue())
        self.assertNotIn("is not under any Overleaf project", err.getvalue())


# ---------------------------------------------------------------------------
# Bash / PowerShell / apply_patch coverage (OSN-05a, OSN-06)
# ---------------------------------------------------------------------------
class ShellAndPatchCoverageTests(_HookHarness):
    def test_bash_sed_in_place_triggers_refresh(self):
        folder = self.project("Paper")
        code, _ = self.run_hook({
            "tool_name": "Bash", "cwd": str(folder),
            "tool_input": {"command": "sed -i 's/foo/bar/' main.tex"},
        })
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.refresh_calls], [PID])

    def test_bash_quoted_absolute_path_with_spaces(self):
        folder = self.project("My Paper")
        code, _ = self.run_hook({
            "tool_name": "Bash", "cwd": str(self.base),
            "tool_input": {"command": f'perl -pi -e "s/a/b/" "{folder}/main.tex"'},
        })
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.refresh_calls], [PID])

    def test_bash_cd_then_relative_path(self):
        self.project("Paper")
        code, _ = self.run_hook({
            "tool_name": "Bash", "cwd": str(self.base),
            "tool_input": {"command": "cd Dropbox/Apps/Overleaf/Paper && python fix.py main.tex"},
        })
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.refresh_calls], [PID])

    def test_bash_glob_in_project_cwd(self):
        folder = self.project("Paper", files=("main.tex", "intro.tex"))
        code, _ = self.run_hook({
            "tool_name": "Bash", "cwd": str(folder),
            "tool_input": {"command": "sed -i 's/a/b/' *.tex"},
        })
        self.assertEqual(code, 0)
        self.assertEqual(len(self.refresh_calls), 1)

    def test_bash_two_projects_refreshes_both(self):
        a = self.project("A", pid=PID)
        b = self.project("B", pid=PID2)
        self.run_hook({
            "tool_name": "Bash", "cwd": str(self.base),
            "tool_input": {"command": f"cp {a}/main.tex {b}/main.tex"},
        })
        self.assertEqual(sorted(c[0] for c in self.refresh_calls), sorted([PID, PID2]))

    def test_bash_without_latex_mention_is_ignored(self):
        folder = self.project("Paper")
        code, err = self.run_hook({
            "tool_name": "Bash", "cwd": str(folder), "tool_input": {"command": "ls -la"},
        })
        self.assertEqual((code, err, self.refresh_calls), (0, "", []))

    def test_powershell_set_content(self):
        folder = self.project("Paper")
        code, _ = self.run_hook({
            "tool_name": "PowerShell", "cwd": str(folder),
            "tool_input": {"command": f'Set-Content -Path "{folder}/main.tex" -Value "x"'},
        })
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.refresh_calls], [PID])

    def test_codex_apply_patch_update_add_move(self):
        folder = self.project("Paper", files=("main.tex", "old.tex"))
        patch = (
            "*** Begin Patch\n"
            "*** Update File: main.tex\n@@\n-a\n+b\n"
            "*** Add File: sections/new.tex\n+hello\n"
            "*** Update File: old.tex\n*** Move to: renamed.tex\n@@\n-x\n+y\n"
            "*** End Patch\n"
        )
        targets = cli._patch_paths(patch, folder)
        self.assertEqual(
            [p.relative_to(folder).as_posix() for p in targets],
            ["main.tex", "sections/new.tex", "old.tex", "renamed.tex"],
        )
        code, _ = self.run_hook({
            "tool_name": "apply_patch", "cwd": str(folder), "tool_input": {"command": patch},
        })
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.refresh_calls], [PID])

    def test_codex_apply_patch_non_latex_ignored(self):
        folder = self.project("Paper")
        patch = "*** Begin Patch\n*** Update File: notes.md\n@@\n-a\n+b\n*** End Patch\n"
        code, err = self.run_hook({
            "tool_name": "apply_patch", "cwd": str(folder), "tool_input": {"command": patch},
        })
        self.assertEqual((code, self.refresh_calls), (0, []))

    def test_codex_bash_heredoc_apply_patch(self):
        folder = self.project("Paper")
        cmd = "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: main.tex\n@@\n-a\n+b\n*** End Patch\nEOF"
        code, _ = self.run_hook({"tool_name": "Bash", "cwd": str(folder), "tool_input": {"command": cmd}})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.refresh_calls), 1)


class ReadOnlyClassifierTests(unittest.TestCase):
    def test_read_only(self):
        for cmd in (
            "cat main.tex", "grep -n 'a|b' main.tex 2>&1 | head -20", "sed -n '1,5p' main.tex",
            "wc -l *.tex", "rg cite refs.bib", "git diff main.tex", "latexmk -pdf main.tex",
            "head main.tex > /dev/null", "Get-Content main.tex",
        ):
            self.assertTrue(cli._is_read_only_command(cmd), cmd)

    def test_writes(self):
        for cmd in (
            "sed -i 's/a/b/' main.tex", "sed -i '' 's/a/b/' main.tex", "perl -pi -e 's/a/b/' main.tex",
            "cat new.tex > main.tex", "echo x >> refs.bib", "python fix.py main.tex",
            "cp a.tex b.tex", "mv a.tex b.tex", "tee main.tex", "awk -i inplace '{print}' main.tex",
            "find . -name '*.tex' -exec sed -i s/a/b/ {} +", "git checkout main.tex",
            "Set-Content main.tex x",
        ):
            self.assertFalse(cli._is_read_only_command(cmd), cmd)

    def test_fd_numbered_and_combined_redirects_are_writes(self):
        for cmd in (
            "echo x 1>main.tex", "echo x 2>main.tex", "echo x &>main.tex", "echo x &>>main.tex",
            "echo x >&main.tex", "echo x >|main.tex", "echo x 1>'/a b/main.tex'",
        ):
            self.assertFalse(cli._is_read_only_command(cmd), cmd)

    def test_fd_duplication_stays_a_read(self):
        for cmd in (
            "cat main.tex 2>&1", "cat main.tex >&2", "cat main.tex 1>&2", "cat main.tex >&-",
            "grep x main.tex 2>/dev/null",
        ):
            self.assertTrue(cli._is_read_only_command(cmd), cmd)

    def test_output_options_of_read_only_programs_are_writes(self):
        for cmd in (
            "sort -o main.tex main.tex", "sort -uo main.tex a.tex", "sort --output=main.tex a.tex",
            "uniq a.tex main.tex", "git diff --output=main.tex", "git diff --output main.tex",
            "sed 's/a/b/w main.tex' a.tex", "sed -n '/x/w out.tex' main.tex",
        ):
            self.assertFalse(cli._is_read_only_command(cmd), cmd)
        for cmd in (
            "sort -u -k2 refs.bib", "uniq -c main.tex", "uniq -f 2 main.tex",
            "uniq main.tex 2>/dev/null", "uniq main.tex > /tmp/out.txt",
            "sed -n '/draw/p' main.tex", "sed -n 's/a/b/p' main.tex",
        ):
            self.assertTrue(cli._is_read_only_command(cmd), cmd)


# ---------------------------------------------------------------------------
# debounce: reads 30 s, writes 3 s (OSN-M2)
# ---------------------------------------------------------------------------
class DebounceTests(_HookHarness):
    def test_read_then_write_inside_30s_probes_twice(self):
        folder = self.project("Paper")
        f = str(folder / "main.tex")
        self.run_hook({"tool_name": "Read", "tool_input": {"file_path": f}})
        # Pretend the Read probe happened 20 s ago (a web edit could have
        # landed since). The write must probe again.
        state = json.loads(cli.STATE_FILE.read_text())
        state[PID] -= 20
        cli.STATE_FILE.write_text(json.dumps(state))
        self.run_hook({"tool_name": "Write", "tool_input": {"file_path": f}})
        self.assertEqual(len(self.refresh_calls), 2)

    def test_reads_share_the_30s_debounce(self):
        folder = self.project("Paper")
        f = str(folder / "main.tex")
        self.run_hook({"tool_name": "Read", "tool_input": {"file_path": f}})
        self.run_hook({"tool_name": "Read", "tool_input": {"file_path": f}})
        self.run_hook({"tool_name": "Bash", "cwd": str(folder), "tool_input": {"command": "cat main.tex"}})
        self.assertEqual(len(self.refresh_calls), 1)


# ---------------------------------------------------------------------------
# write guard + web-removed guard (OSN-05b, OSN-01)
# ---------------------------------------------------------------------------
class WriteGuardTests(_HookHarness):
    def _web_edit(self, rel):
        def refresh(pid, folder):
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("NEWER WEB CONTENT")
            return "refreshed (wrote 1)"
        self.on_refresh = refresh

    def test_write_blocked_when_refresh_changed_target(self):
        folder = self.project("Paper")
        self._web_edit("main.tex")
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.assertEqual(code, 2)
        self.assertIn("Re-read", err)
        self.assertIn("main.tex", err)

    def test_retry_after_reread_passes(self):
        folder = self.project("Paper")
        self._web_edit("main.tex")
        self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.on_refresh = None  # nothing new on the web this time
        state = json.loads(cli.STATE_FILE.read_text())
        state[PID] -= 10
        cli.STATE_FILE.write_text(json.dumps(state))
        code, err = self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.assertEqual(code, 0, err)

    def test_read_is_not_blocked_by_refresh(self):
        folder = self.project("Paper")
        self._web_edit("main.tex")
        code, _ = self.run_hook({"tool_name": "Read", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.assertEqual(code, 0)

    def test_change_to_other_file_does_not_block(self):
        folder = self.project("Paper", files=("main.tex", "intro.tex"))
        self._web_edit("intro.tex")
        code, _ = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.assertEqual(code, 0)

    def test_apply_patch_add_of_file_created_by_refresh_blocks(self):
        folder = self.project("Paper")
        self._web_edit("sections/new.tex")
        patch = "*** Begin Patch\n*** Add File: sections/new.tex\n+hi\n*** End Patch\n"
        code, err = self.run_hook({"tool_name": "apply_patch", "cwd": str(folder), "tool_input": {"command": patch}})
        self.assertEqual(code, 2)
        self.assertIn("sections/new.tex", err)

    def test_bash_glob_write_blocked_when_any_file_changed(self):
        folder = self.project("Paper", files=("main.tex", "intro.tex"))
        self._web_edit("intro.tex")
        code, err = self.run_hook({
            "tool_name": "Bash", "cwd": str(folder),
            "tool_input": {"command": "sed -i 's/a/b/' *.tex"},
        })
        self.assertEqual(code, 2)
        self.assertIn("intro.tex", err)

    def test_write_blocked_when_target_was_protected(self):
        # Local save seconds ago + a pending web change: the refresh leaves
        # the file alone (protect window), so the write must not proceed.
        folder = self.project("Paper")
        self.on_refresh = lambda pid, f: ("refreshed (wrote 0, unchanged 0, protected 1 (recent "
                                          "local edits; version held at 5 ...))")
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.assertEqual(code, 2)
        self.assertIn("Conflict", err)

    def test_protected_other_file_does_not_block(self):
        folder = self.project("Paper", files=("main.tex", "intro.tex"))
        old = time.time() - 120
        os.utime(folder / "main.tex", (old, old))
        self.on_refresh = lambda pid, f: "refreshed (wrote 0, unchanged 0, protected 1 (...))"
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "main.tex")}})
        self.assertEqual(code, 0, err)

    def test_edit_of_web_renamed_path_blocks(self):
        folder = self.project("Paper", files=("intro.tex",))
        cli._atomic_write_text(cli.WEB_REMOVED_FILE, json.dumps({PID: {"intro.tex": "sections/intro.tex"}}))
        code, err = self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(folder / "intro.tex")}})
        self.assertEqual(code, 2)
        self.assertIn("renamed on Overleaf to sections/intro.tex", err)
        # Reads are allowed, with a note.
        code, err = self.run_hook({"tool_name": "Read", "tool_input": {"file_path": str(folder / "intro.tex")}})
        self.assertEqual(code, 0)
        self.assertIn("renamed on Overleaf", err)

    def test_edit_of_web_deleted_path_blocks_until_local_copy_gone(self):
        folder = self.project("Paper", files=("old.tex",))
        cli._atomic_write_text(cli.WEB_REMOVED_FILE, json.dumps({PID: {"old.tex": None}}))
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "old.tex")}})
        self.assertEqual(code, 2)
        self.assertIn("deleted on Overleaf", err)
        (folder / "old.tex").unlink()
        state = json.loads(cli.STATE_FILE.read_text())
        state[PID] -= 10
        cli.STATE_FILE.write_text(json.dumps(state))
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "old.tex")}})
        self.assertEqual(code, 0, err)

    def test_deleting_outdated_copy_of_web_removed_path_is_allowed(self):
        # The block message tells the agent to delete the outdated local
        # copy; a pure delete cannot re-create the file on Overleaf.
        folder = self.project("Paper", files=("old.tex", "gone.tex"))
        cli._atomic_write_text(cli.WEB_REMOVED_FILE, json.dumps(
            {PID: {"old.tex": None, "gone.tex": "sections/gone.tex"}}))
        allowed = [
            ("Bash", "rm old.tex"), ("Bash", "rm -f old.tex gone.tex 2>&1"),
            ("Bash", "git rm -q old.tex"), ("PowerShell", "Remove-Item old.tex -Force"),
            ("PowerShell", "del gone.tex"),
            ("apply_patch", "*** Begin Patch\n*** Delete File: old.tex\n*** End Patch\n"),
        ]
        for tool, cmd in allowed:
            code, err = self.run_hook({"tool_name": tool, "cwd": str(folder), "tool_input": {"command": cmd}})
            self.assertEqual(code, 0, (tool, cmd, err))
        blocked = [
            ("Bash", "mv old.tex other.tex"), ("Bash", "rm old.tex && echo x > old.tex"),
            ("Bash", "sed -i 's/a/b/' gone.tex; rm gone.tex"), ("PowerShell", "Set-Content old.tex x"),
            ("apply_patch", "*** Begin Patch\n*** Update File: old.tex\n*** Move to: new.tex\n@@\n-a\n+b\n*** End Patch\n"),
        ]
        for tool, cmd in blocked:
            code, err = self.run_hook({"tool_name": tool, "cwd": str(folder), "tool_input": {"command": cmd}})
            self.assertEqual(code, 2, (tool, cmd, err))
        _, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "old.tex")}})
        self.assertIn("deleting it is allowed", err)
        _, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(folder / "gone.tex")}})
        self.assertIn("Deleting the outdated local gone.tex is allowed", err)

    # -- the same guards when the Dropbox root is reached through a symlink
    #    (~/Dropbox -> ~/Library/CloudStorage/Dropbox on macOS). The resolver
    #    returns the resolved folder, so the targets must be resolved too.
    def _via_symlink(self, folder):
        link = self.base / "link"
        if not link.exists():
            try:
                link.symlink_to(self.base / "Dropbox", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available here")
        return link / folder.relative_to(self.base / "Dropbox")

    def test_web_renamed_path_blocks_through_symlinked_dropbox(self):
        folder = self.project("Paper", files=("old.tex",))
        cli._atomic_write_text(cli.WEB_REMOVED_FILE, json.dumps({PID: {"old.tex": "new.tex"}}))
        linked = self._via_symlink(folder)
        code, err = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(linked / "old.tex")}})
        self.assertEqual(code, 2, err)
        self.assertIn("renamed on Overleaf to new.tex", err)
        code, err = self.run_hook({
            "tool_name": "Bash", "cwd": str(linked),
            "tool_input": {"command": "sed -i 's/a/b/' old.tex"},
        })
        self.assertEqual(code, 2, err)
        self.assertIn("renamed on Overleaf to new.tex", err)
        code, err = self.run_hook({"tool_name": "Read", "tool_input": {"file_path": str(linked / "old.tex")}})
        self.assertEqual(code, 0)
        self.assertIn("renamed on Overleaf to new.tex", err)

    def test_write_guard_through_symlinked_dropbox(self):
        folder = self.project("Paper")
        self._web_edit("main.tex")
        linked = self._via_symlink(folder)
        code, err = self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(linked / "main.tex")}})
        self.assertEqual(code, 2, err)
        self.assertIn("Re-read", err)
        self.assertIn("main.tex", err)


# ---------------------------------------------------------------------------
# credential hygiene + sandbox classification (OSN-03, OSN-16)
# ---------------------------------------------------------------------------
class CredentialHygieneTests(unittest.TestCase):
    def test_recovery_text_is_login_first_and_never_chat(self):
        t = cli._AUTH_RECOVERY
        self.assertLess(t.index("overleaf-sync-now login"), t.index("save-cookie"))
        self.assertIn("NEVER ask the user to paste the cookie into this chat", t)
        self.assertNotIn("Paste your", t)
        self.assertNotIn('save-cookie "<', t)

    def test_get_session_error_uses_recovery_text(self):
        orig = (cli._load_cache, cli._resolve_cookies)
        cli._load_cache = lambda: None
        cli._resolve_cookies = lambda interactive=False: None
        try:
            with self.assertRaises(cli.AuthExpired) as cm:
                cli.get_session()
        finally:
            cli._load_cache, cli._resolve_cookies = orig
        self.assertIn(cli._AUTH_RECOVERY, str(cm.exception))

    def test_cli_source_has_no_agent_paste_instructions(self):
        src = (ROOT / "src" / "overleaf_sync_now" / "cli.py").read_text()
        self.assertNotIn("Ask the user: 'Paste", src)
        self.assertNotIn('save-cookie \\"<', src)
        skill = (ROOT / "src" / "overleaf_sync_now" / "SKILL.md").read_text()
        self.assertNotIn('save-cookie "<value>"', skill)

    def test_local_permission_error_is_not_a_sandbox_block(self):
        self.assertFalse(cli._is_sandbox_block(PermissionError(13, "Permission denied", "/x/main.tex")))

    def test_network_permission_error_is_a_sandbox_block(self):
        import requests
        self.assertTrue(cli._is_sandbox_block(
            requests.exceptions.ConnectionError("[Errno 13] Permission denied")))
        try:
            try:
                raise PermissionError(13, "Permission denied")
            except PermissionError as inner:
                raise requests.exceptions.ConnectionError("connect failed") from inner
        except requests.exceptions.ConnectionError as outer:
            self.assertTrue(cli._is_network_exception(outer))


class SaveCookieInputModesTests(unittest.TestCase):
    def setUp(self):
        self._orig = {"_validate_cookies": cli._validate_cookies, "_save_cache": cli._save_cache}
        self.saved = {}
        cli._validate_cookies = lambda c, use_cache=True: True
        cli._save_cache = lambda c: self.saved.update(c)
        self._stdin, self._stdout, self._stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = self.err = io.StringIO()

    def tearDown(self):
        sys.stdin, sys.stdout, sys.stderr = self._stdin, self._stdout, self._stderr
        for k, v in self._orig.items():
            setattr(cli, k, v)

    def test_dash_reads_stdin(self):
        sys.stdin = io.StringIO("s%3Afromstdin\n")
        cli.cmd_save_cookie(["-"])
        self.assertEqual(self.saved[cli.SESSION_COOKIE], "s%3Afromstdin")
        self.assertNotIn("WARNING", self.err.getvalue())

    def test_no_arg_with_piped_stdin(self):
        sys.stdin = io.StringIO("piped-value")
        cli.cmd_save_cookie([])
        self.assertEqual(self.saved[cli.SESSION_COOKIE], "piped-value")

    def test_argv_still_works_but_warns(self):
        cli.cmd_save_cookie(["argvvalue"])
        self.assertEqual(self.saved[cli.SESSION_COOKIE], "argvvalue")
        self.assertIn("process list", self.err.getvalue())

    def test_output_never_echoes_the_value(self):
        sys.stdin = io.StringIO("SECRETVALUE123")
        cli.cmd_save_cookie(["-"])
        self.assertNotIn("SECRET", sys.stdout.getvalue() + self.err.getvalue())


class SetupNoPrefixTests(unittest.TestCase):
    def test_setup_reports_source_without_cookie_prefix(self):
        orig = cli._resolve_cookies_with_source
        cli._resolve_cookies_with_source = lambda interactive=False: (
            {cli.SESSION_COOKIE: "s%3ATOPSECRETVALUE"}, "cached cookie")
        out = io.StringIO()
        orig_out = sys.stdout
        sys.stdout = out
        try:
            cli.cmd_setup([], force_noninteractive=True)
        finally:
            sys.stdout = orig_out
            cli._resolve_cookies_with_source = orig
        self.assertIn("cached cookie", out.getvalue())
        self.assertNotIn("TOPSECRET", out.getvalue())
        self.assertNotIn("s%3A", out.getvalue())

    def test_setup_failure_recommends_login_not_browser(self):
        orig = cli._resolve_cookies_with_source
        cli._resolve_cookies_with_source = lambda interactive=False: (None, None)
        out = io.StringIO()
        orig_out = sys.stdout
        sys.stdout = out
        try:
            cli.cmd_setup([], force_noninteractive=True)
        finally:
            sys.stdout = orig_out
            cli._resolve_cookies_with_source = orig
        self.assertIn("overleaf-sync-now login", out.getvalue())
        self.assertNotIn("Open https://www.overleaf.com in Chrome", out.getvalue())


if __name__ == "__main__":
    unittest.main()
