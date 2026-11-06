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
        p.write_text(text)
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


class HookSummaryTests(_Env):
    def setUp(self):
        super().setUp()
        (self.folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": PID}))
        cli.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def run_hook(self, tool, path):
        sys.stdin = io.StringIO(json.dumps({"tool_name": tool, "tool_input": {"file_path": str(path)}}))
        sys.stderr = err = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_hook([])
        sys.stderr = self.err
        return cm.exception.code or 0, err.getvalue()

    def test_read_prints_one_line_summary(self):
        main = self.local("main.tex", "STALE\n", minutes_ago=60)
        self.zip_files = {"main.tex": "WEB\n"}
        self.updates([upd(9, 10, ["main.tex"], minutes_ago=5)])
        code, err = self.run_hook("Read", main)
        self.assertEqual(code, 0)
        self.assertIn("My JMP: pulled newer Overleaf content into main.tex.", err)
        self.assertIn("Previous local versions:", err)

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
