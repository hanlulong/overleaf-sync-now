"""Local edits Overleaf never received are not reverted (0.5.0).

Pins:
  * before ANY overwrite, the previous local bytes are copied to
    <DATA_DIR>/backups/<project>/<timestamp>/<path>;
  * on a bootstrap (no cached version, or a history rewind) or a full pull
    (cached version older than the first /updates page), a local file that
    differs from Overleaf and was changed after Overleaf's last change to it
    is KEPT, and Overleaf's copy goes to <DATA_DIR>/conflicts/<project>/<ts>/;
  * a targeted pull and `--force` still take Overleaf's version (backed up);
  * the hook prints a one-line summary when a refresh writes or skips files,
    and blocks a write whose target was kept that way (exit 2).

The scenario is the critic's repro: Dropbox paused (or Overleaf's Dropbox
sync down), the user edits main.tex locally, then the first sync on this
machine would have replaced it with Overleaf's older copy.

No network: fetch_updates / download_zip are monkeypatched and a real zip is
extracted by the real _extract_files.

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
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402

PID = "c" * 24
NOW = time.time()


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def upd(fromV, toV, paths, minutes_ago, kind="web"):
    return {"fromV": fromV, "toV": toV, "pathnames": list(paths),
            "meta": {"origin": {"kind": kind}, "end_ts": int((NOW - minutes_ago * 60) * 1000)}}


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name).resolve()
        self.data = base / "data"
        self.folder = base / "Dropbox" / "Apps" / "Overleaf" / "My JMP"
        self.folder.mkdir(parents=True)
        self._orig = {name: getattr(cli, name) for name in (
            "CACHE_DIR", "CACHE_FILE", "STATE_FILE", "VERSIONS_FILE", "INDEX_FILE",
            "WEB_REMOVED_FILE", "_VALIDATION_FILE", "fetch_updates", "download_zip",
            "_fingerprint_sanity_warn",
        )}
        cli.CACHE_DIR = self.data
        cli.CACHE_FILE = self.data / "cookies.json"
        cli.STATE_FILE = self.data / "state.json"
        cli.VERSIONS_FILE = self.data / "versions.json"
        cli.INDEX_FILE = self.data / "projects.json"
        cli.WEB_REMOVED_FILE = self.data / "web-removed.json"
        cli._VALIDATION_FILE = self.data / ".validated-at"
        cli._fingerprint_sanity_warn = lambda *a, **k: None
        self.zip_files = {}
        cli.download_zip = lambda pid: _zip(self.zip_files)
        self._streams = (sys.stdin, sys.stderr)
        sys.stderr = self.err = io.StringIO()

    def tearDown(self):
        sys.stdin, sys.stderr = self._streams
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def updates(self, updates):
        cli.fetch_updates = lambda pid: {"updates": updates}

    def local(self, rel, text, minutes_ago):
        p = self.folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # Bytes as given: on Windows write_text turns "\n" into "\r\n", and
        # the file would then differ from the zip's copy.
        p.write_bytes(text.encode("utf-8"))
        t = NOW - minutes_ago * 60
        os.utime(p, (t, t))
        return p

    def copies(self, kind):
        root = self.data / kind / PID
        return {str(p.relative_to(root).as_posix()).split("/", 1)[1]: p.read_text()
                for p in root.rglob("*") if p.is_file()} if root.exists() else {}

    def toV(self):
        return cli._load_versions().get(PID)


class KeepBothTests(_Env):
    def test_bootstrap_keeps_local_newer_than_overleaf(self):
        # Overleaf last changed main.tex 60 min ago; the local copy was edited
        # 10 min ago (Dropbox paused). First sync on this machine.
        main = self.local("main.tex", "OLD\nNEW LOCAL PARAGRAPH\n", minutes_ago=10)
        self.zip_files = {"main.tex": "OLD\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=60)])
        report = {}
        status = cli.refresh_project(PID, self.folder, report=report)
        self.assertEqual(main.read_text(), "OLD\nNEW LOCAL PARAGRAPH\n")
        self.assertEqual(self.copies("conflicts"), {"main.tex": "OLD\n"})
        self.assertIn("main.tex", report["conflicts"])
        self.assertIn("kept 1 newer local", status)
        self.assertIn("KEPT local main.tex", self.err.getvalue())
        self.assertEqual(self.toV(), 10)

    def test_bootstrap_overwrites_stale_local_and_backs_it_up(self):
        # The case the tool exists for: a web edit 5 min ago, local is older.
        main = self.local("main.tex", "STALE\n", minutes_ago=60)
        self.zip_files = {"main.tex": "WEB EDIT\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        report = {}
        cli.refresh_project(PID, self.folder, report=report)
        self.assertEqual(main.read_text(), "WEB EDIT\n")
        self.assertEqual(self.copies("backups"), {"main.tex": "STALE\n"})
        self.assertEqual(report["written"], ["main.tex"])
        self.assertEqual(self.copies("conflicts"), {})

    def test_full_pull_keeps_untouched_local_edit(self):
        # Cached toV 5; the first page only reaches back to toV 40, so the
        # whole zip is compared. Web edits only touched other.tex. intro.tex
        # was edited locally 10 min ago and is not in the page at all.
        cli._save_versions({PID: 5})
        intro = self.local("intro.tex", "INTRO + LOCAL EDIT\n", minutes_ago=10)
        other = self.local("other.tex", "OTHER OLD\n", minutes_ago=600)
        self.zip_files = {"intro.tex": "INTRO\n", "other.tex": "OTHER WEB\n"}
        self.updates([upd(49, 50, ["other.tex"], minutes_ago=3),
                      upd(40, 41, ["other.tex"], minutes_ago=120)])
        cli.refresh_project(PID, self.folder)
        self.assertEqual(intro.read_text(), "INTRO + LOCAL EDIT\n")
        self.assertEqual(other.read_text(), "OTHER WEB\n")
        self.assertEqual(self.copies("conflicts"), {"intro.tex": "INTRO\n"})
        self.assertEqual(self.copies("backups"), {"other.tex": "OTHER OLD\n"})
        self.assertEqual(self.toV(), 50)

    def test_history_rewind_uses_the_same_rule(self):
        cli._save_versions({PID: 100})  # newer than anything Overleaf reports
        main = self.local("main.tex", "LOCAL NEWER\n", minutes_ago=2)
        self.zip_files = {"main.tex": "OVERLEAF\n"}
        self.updates([upd(49, 50, ["main.tex"], minutes_ago=30)])
        cli.refresh_project(PID, self.folder)
        self.assertEqual(main.read_text(), "LOCAL NEWER\n")
        self.assertEqual(self.copies("conflicts"), {"main.tex": "OVERLEAF\n"})
        self.assertEqual(self.toV(), 50)

    def test_targeted_pull_takes_overleaf_and_backs_up(self):
        cli._save_versions({PID: 10})
        main = self.local("main.tex", "LOCAL\n", minutes_ago=10)
        self.zip_files = {"main.tex": "WEB\n"}
        self.updates([upd(10, 11, ["main.tex"], minutes_ago=20), upd(9, 10, [], minutes_ago=90)])
        cli.refresh_project(PID, self.folder)
        self.assertEqual(main.read_text(), "WEB\n")
        self.assertEqual(self.copies("backups"), {"main.tex": "LOCAL\n"})

    def test_force_takes_overleaf_and_backs_up(self):
        main = self.local("main.tex", "LOCAL NEWER\n", minutes_ago=2)
        self.zip_files = {"main.tex": "OVERLEAF\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=30)])
        cli.refresh_project(PID, self.folder, force=True)
        self.assertEqual(main.read_text(), "OVERLEAF\n")
        self.assertEqual(self.copies("backups"), {"main.tex": "LOCAL NEWER\n"})

    def test_backup_never_overwrites_an_earlier_backup(self):
        p = self.folder / "a.tex"
        p.write_text("v1")
        os.utime(p, (NOW - 3600, NOW - 3600))
        cli._extract_files(_zip({"a.tex": "v2"}), self.folder, None, project_id=PID)
        os.utime(p, (NOW - 3600, NOW - 3600))
        cli._extract_files(_zip({"a.tex": "v3"}), self.folder, None, project_id=PID)
        contents = sorted(self.copies("backups").values())
        self.assertEqual(contents, ["v1", "v2"])

    def test_old_backups_are_pruned(self):
        old = self.data / "backups" / PID / "20200101-000000"
        old.mkdir(parents=True)
        (old / "x.tex").write_text("ancient")
        os.utime(old, (NOW - 40 * 86400, NOW - 40 * 86400))
        self.local("main.tex", "STALE\n", minutes_ago=60)
        self.zip_files = {"main.tex": "WEB\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        cli.refresh_project(PID, self.folder)
        self.assertFalse(old.exists())


class ExtractSafetyTests(_Env):
    def test_own_extraction_is_not_a_local_save(self):
        # The tool's own write must not trigger the 30 s protect window: a
        # web edit right after a pull would otherwise be held as a "local
        # save" conflict nobody made.
        main = self.local("main.tex", "V1\n", minutes_ago=60)
        cli._extract_files(_zip({"main.tex": "V2\n"}), self.folder, None, project_id=PID)
        self.assertEqual(main.read_text(), "V2\n")
        n_written, _, n_protected = cli._extract_files(
            _zip({"main.tex": "V3\n"}), self.folder, {"main.tex"}, project_id=PID)
        self.assertEqual((n_written, n_protected, main.read_text()), (1, 0, "V3\n"))
        # A real local save after our write is protected again.
        main.write_text("USER\n")
        _, _, n_protected = cli._extract_files(
            _zip({"main.tex": "V4\n"}), self.folder, {"main.tex"}, project_id=PID)
        self.assertEqual((n_protected, main.read_text()), (1, "USER\n"))

    def test_unreadable_local_file_is_not_overwritten(self):
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions as a normal user")
        main = self.local("main.tex", "LOCAL\n", minutes_ago=60)
        os.chmod(main, 0)
        try:
            report = {}
            n_written, _, n_protected = cli._extract_files(
                _zip({"main.tex": "WEB\n"}), self.folder, {"main.tex"}, project_id=PID,
                report=report)
        finally:
            os.chmod(main, 0o644)
        self.assertEqual((n_written, n_protected), (0, 1))
        self.assertEqual(main.read_text(), "LOCAL\n")
        self.assertIn("can't be backed up", self.err.getvalue())

    def test_opt_out_file_from_overleaf_is_never_extracted(self):
        cli._extract_files(_zip({cli.IGNORE_MARKER: "", "main.tex": "x"}), self.folder, None,
                           project_id=PID)
        self.assertFalse((self.folder / cli.IGNORE_MARKER).exists())
        self.assertTrue((self.folder / "main.tex").exists())


class SyncWaitTests(_Env):
    """`sync --wait`: the protect-window block's first step (Claude Code's
    Bash tool refuses a foreground `sleep 30`)."""

    def setUp(self):
        super().setUp()
        (self.folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": PID}))
        cli._save_versions({PID: 10})
        self.main = self.local("main.tex", "LOCAL SAVE\n", minutes_ago=0.1)  # 6 s ago
        self.zip_files = {"main.tex": "WEB EDIT\n"}
        self.updates([upd(10, 11, ["main.tex"], minutes_ago=0.05), upd(9, 10, [], minutes_ago=90)])
        self.slept = []

    def sync(self, *args):
        from unittest import mock

        def fake_sleep(sec):
            self.slept.append(sec)
            old = time.time() - 60  # the local save ages out of the window
            os.utime(self.main, (old, old))
        out = io.StringIO()
        with mock.patch.object(cli.time, "sleep", fake_sleep), mock.patch.object(sys, "stdout", out):
            try:
                cli.cmd_sync([*args, str(self.folder)])
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def test_wait_holds_then_pulls(self):
        code, out = self.sync("--wait")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.slept), 1)
        self.assertTrue(20 <= self.slept[0] <= 31, self.slept)
        self.assertIn("Waiting", out)
        self.assertEqual(self.main.read_text(), "WEB EDIT\n")
        self.assertEqual(self.copies("backups"), {"main.tex": "LOCAL SAVE\n"})
        self.assertEqual(self.toV(), 11)

    def test_without_wait_the_file_stays_protected(self):
        code, out = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.slept, [])
        self.assertEqual(self.main.read_text(), "LOCAL SAVE\n")
        self.assertEqual(self.toV(), 10)


class HookSummaryTests(_Env):
    def setUp(self):
        super().setUp()
        (self.folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": PID}))
        cli.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def run_hook(self, tool, path):
        sys.stdin = io.StringIO(json.dumps({"tool_name": tool, "hook_event_name": "PreToolUse",
                                            "tool_input": {"file_path": str(path)}}))
        sys.stderr = err = io.StringIO()
        stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_hook([])
            self.stdout = sys.stdout.getvalue()
        finally:
            sys.stdout = stdout
            sys.stderr = self.err
        return cm.exception.code or 0, err.getvalue()

    def hook_json(self):
        """The one JSON object an exit-0 hook prints on stdout (or None)."""
        return json.loads(self.stdout) if self.stdout.strip() else None

    def test_read_prints_one_line_summary(self):
        main = self.local("main.tex", "STALE\n", minutes_ago=60)
        self.zip_files = {"main.tex": "WEB\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        code, err = self.run_hook("Read", main)
        self.assertEqual(code, 0)
        self.assertIn("My JMP: pulled newer Overleaf content into main.tex.", err)
        self.assertIn("Previous local versions:", err)

    def test_summary_reaches_user_and_model_as_json(self):
        # Claude Code never shows exit-0 stderr to the model (and the user
        # sees it only in debug logs); Codex ignores plain stdout. The note
        # goes out as systemMessage (user) + additionalContext (model).
        main = self.local("main.tex", "STALE\n", minutes_ago=60)
        self.zip_files = {"main.tex": "WEB\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        code, _ = self.run_hook("Read", main)
        self.assertEqual(code, 0)
        out = self.hook_json()
        self.assertIn("pulled newer Overleaf content into main.tex", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["systemMessage"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertNotIn("continue", out)  # Codex rejects it on PreToolUse
        self.stdout.encode("ascii")  # any console code page can print it

    def test_nothing_to_report_prints_nothing(self):
        main = self.local("main.tex", "SAME\n", minutes_ago=60)
        self.zip_files = {"main.tex": "SAME\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        code, err = self.run_hook("Read", main)
        self.assertEqual((code, self.stdout), (0, ""))

    def test_read_then_edit_of_kept_file_is_blocked_once(self):
        # The usual order: the agent Reads first, so the Read does the
        # bootstrap that keeps the local file. The Edit after it (no new
        # refresh) must still be blocked once, and the Read must tell both
        # the user and the model about the kept file.
        main = self.local("main.tex", "LOCAL NEWER\n", minutes_ago=10)
        self.zip_files = {"main.tex": "OVERLEAF\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=60)])
        code, err = self.run_hook("Read", main)
        self.assertEqual(code, 0, err)
        self.assertIn("kept the newer local main.tex", self.hook_json()["systemMessage"])
        self.assertEqual(self.toV(), 10)
        code, err = self.run_hook("Edit", main)  # within the debounce: no refresh
        self.assertEqual(code, 2, err)
        self.assertIn("Conflict: main.tex differs from Overleaf's copy", err)
        self.assertIn(str(self.data / "conflicts" / PID), err)
        code, err = self.run_hook("Edit", main)
        self.assertEqual(code, 0, err)
        self.assertEqual(main.read_text(), "LOCAL NEWER\n")

    def test_pull_that_writes_the_file_clears_the_pending_conflict(self):
        main = self.local("main.tex", "LOCAL NEWER\n", minutes_ago=10)
        self.zip_files = {"main.tex": "OVERLEAF\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=60)])
        cli.refresh_project(PID, self.folder)
        self.assertIn("main.tex", cli.pending_conflicts(PID))
        cli.refresh_project(PID, self.folder, force=True)  # user chose Overleaf's
        self.assertEqual(main.read_text(), "OVERLEAF\n")
        self.assertEqual(cli.pending_conflicts(PID), {})

    def test_write_to_kept_local_file_is_blocked_once(self):
        main = self.local("main.tex", "LOCAL NEWER\n", minutes_ago=10)
        self.zip_files = {"main.tex": "OVERLEAF\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=60)])
        code, err = self.run_hook("Edit", main)
        self.assertEqual(code, 2, err)
        self.assertIn("Conflict: main.tex differs from Overleaf's copy", err)
        self.assertIn(str(self.data / "conflicts" / PID), err)
        self.assertEqual(main.read_text(), "LOCAL NEWER\n")
        # Version advanced: after the user decides, the retry goes through.
        cli._save_state({})
        code, err = self.run_hook("Edit", main)
        self.assertEqual(code, 0, err)

    def test_write_after_pull_blocks_with_reread_and_summary(self):
        main = self.local("main.tex", "STALE\n", minutes_ago=60)
        self.zip_files = {"main.tex": "WEB\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        code, err = self.run_hook("Write", main)
        self.assertEqual(code, 2)
        self.assertIn("Re-read the file(s) before editing", err)
        self.assertIn("pulled newer Overleaf content into main.tex", err)


if __name__ == "__main__":
    unittest.main()
