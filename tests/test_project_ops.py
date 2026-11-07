"""Web-side file operations (`project_ops`) in the version-match refresh.

Overleaf's /updates reports file adds, renames and deletes in `project_ops`
(not in `pathnames`). Before 0.5.0 they were ignored: an update carrying only
a web-side add was classified as a dropbox echo, the cached version advanced,
and the new file was never pulled. A web-side rename left the old local file
in place for the AI to edit, which Dropbox would then upload again.

No network: fetch_updates / download_zip are monkeypatched, and a real zip is
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

WEB = {"origin": {"kind": "web"}}
DBX = {"origin": {"kind": "dropbox"}}


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


class ProjectOpsRefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)
        self.folder = base / "proj"
        self.folder.mkdir()
        self.pid = "d" * 24
        self._orig = {
            "fetch_updates": cli.fetch_updates,
            "download_zip": cli.download_zip,
            "_fingerprint_sanity_warn": cli._fingerprint_sanity_warn,
            "VERSIONS_FILE": cli.VERSIONS_FILE,
            "WEB_REMOVED_FILE": cli.WEB_REMOVED_FILE,
            "CACHE_DIR": cli.CACHE_DIR,
        }
        cli.CACHE_DIR = base / "data"  # backups / conflicts land here
        cli.VERSIONS_FILE = base / "versions.json"
        cli.WEB_REMOVED_FILE = base / "web-removed.json"
        cli._fingerprint_sanity_warn = lambda *a, **k: None
        self.zip_files = {}
        self.dl_calls = 0

        def fake_download(pid):
            self.dl_calls += 1
            return _zip(self.zip_files)

        cli.download_zip = fake_download
        self._stderr = sys.stderr
        sys.stderr = self.err = io.StringIO()

    def tearDown(self):
        sys.stderr = self._stderr
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def _updates(self, updates):
        cli.fetch_updates = lambda pid: {"updates": updates}

    def _seed(self, toV):
        cli._save_versions({self.pid: toV})

    def _cached(self):
        return cli._load_versions().get(self.pid)

    def _old_file(self, rel, content="OLD"):
        p = self.folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        old = time.time() - 600
        os.utime(p, (old, old))  # outside the 30 s protect window
        return p

    # ---- add ----
    def test_web_add_is_pulled_not_dropbox_echo(self):
        # The audit repro: pathnames empty, one web add -> used to report
        # "dropbox_echo (toV 10->12)" with zero downloads.
        self._seed(10)
        self._updates([
            {"fromV": 10, "toV": 12, "meta": WEB, "pathnames": [],
             "project_ops": [{"add": {"pathname": "sections/new.tex"}, "atV": 11}]},
            {"toV": 10},
        ])
        self.zip_files = {"sections/new.tex": "NEW FILE", "main.tex": "M"}
        status = cli.refresh_project(self.pid, self.folder)
        self.assertNotIn("dropbox_echo", status)
        self.assertEqual(self.dl_calls, 1)
        self.assertEqual((self.folder / "sections" / "new.tex").read_text(), "NEW FILE")
        self.assertFalse((self.folder / "main.tex").exists())  # only the changed path
        self.assertEqual(self._cached(), 12)

    def test_leading_slash_pathnames_are_normalized(self):
        self._seed(10)
        self._updates([
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"add": {"pathname": "/fig.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        self.zip_files = {"fig.tex": "F"}
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual((self.folder / "fig.tex").read_text(), "F")

    # ---- rename ----
    def test_web_rename_pulls_new_path_and_records_old(self):
        self._seed(10)
        self._old_file("intro.tex", "OLD INTRO")
        self._updates([
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"rename": {"pathname": "intro.tex",
                                         "newPathname": "sections/intro.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        self.zip_files = {"sections/intro.tex": "OLD INTRO"}
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual((self.folder / "sections" / "intro.tex").read_text(), "OLD INTRO")
        # Local old copy is NEVER deleted by the tool...
        self.assertTrue((self.folder / "intro.tex").exists())
        # ...but it is recorded so the hook can block edits to it.
        gone = cli.web_removed_paths(self.pid, self.folder)
        self.assertEqual(gone, {"intro.tex": "sections/intro.tex"})
        self.assertIn("renamed on Overleaf to sections/intro.tex", self.err.getvalue())
        self.assertEqual(self._cached(), 11)

    # ---- remove ----
    def test_web_remove_only_is_not_dropbox_echo(self):
        self._seed(10)
        self._old_file("old.tex")
        self._updates([
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"remove": {"pathname": "old.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        status = cli.refresh_project(self.pid, self.folder)
        self.assertTrue(status.startswith("web_file_ops"), status)
        self.assertEqual(self.dl_calls, 0)            # nothing to pull
        self.assertTrue((self.folder / "old.tex").exists())  # not deleted locally
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {"old.tex": None})
        self.assertEqual(self._cached(), 11)
        self.assertIn("deleted on Overleaf", self.err.getvalue())

    def test_remove_of_file_already_gone_locally_is_not_recorded(self):
        self._seed(10)
        self._updates([
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"remove": {"pathname": "gone.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {})
        self.assertFalse(cli.WEB_REMOVED_FILE.exists() and
                         json.loads(cli.WEB_REMOVED_FILE.read_text()).get(self.pid))

    def test_entry_pruned_once_local_file_disappears(self):
        self._seed(10)
        f = self._old_file("old.tex")
        self._updates([
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"remove": {"pathname": "old.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        cli.refresh_project(self.pid, self.folder)
        self.assertIn("old.tex", cli.web_removed_paths(self.pid, self.folder))
        f.unlink()  # Dropbox caught up
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {})

    # ---- dropbox-origin ops are our own round-trips ----
    def test_dropbox_origin_ops_are_skipped(self):
        self._seed(10)
        self._old_file("a.tex")
        self._updates([
            {"toV": 12, "meta": DBX, "pathnames": [],
             "project_ops": [{"add": {"pathname": "b.tex"}, "atV": 11},
                             {"remove": {"pathname": "a.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        status = cli.refresh_project(self.pid, self.folder)
        self.assertIn("dropbox_echo", status)
        self.assertEqual(self.dl_calls, 0)
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {})

    # ---- ordering across updates ----
    def test_remove_then_readd_ends_present(self):
        # Newest-first page: the re-add (toV 13) is newer than the remove.
        self._seed(10)
        self._old_file("x.tex", "LOCAL")
        self._updates([
            {"toV": 13, "meta": WEB, "pathnames": [],
             "project_ops": [{"add": {"pathname": "x.tex"}, "atV": 12}]},
            {"toV": 12, "meta": DBX, "pathnames": ["other.tex"]},
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"remove": {"pathname": "x.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        self.zip_files = {"x.tex": "WEB VERSION"}
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual((self.folder / "x.tex").read_text(), "WEB VERSION")
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {})

    def test_readd_clears_previous_entry(self):
        self._seed(10)
        self._old_file("x.tex")
        self._updates([
            {"toV": 11, "meta": WEB, "pathnames": [],
             "project_ops": [{"remove": {"pathname": "x.tex"}, "atV": 10}]},
            {"toV": 10},
        ])
        cli.refresh_project(self.pid, self.folder)
        self.assertIn("x.tex", cli.web_removed_paths(self.pid, self.folder))
        self._updates([
            {"toV": 12, "meta": WEB, "pathnames": [],
             "project_ops": [{"add": {"pathname": "x.tex"}, "atV": 11}]},
            {"toV": 11},
        ])
        self.zip_files = {"x.tex": "BACK"}
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {})

    def test_mixed_text_and_file_ops(self):
        self._seed(10)
        self._updates([
            {"toV": 12, "meta": WEB, "pathnames": ["main.tex"],
             "project_ops": [{"add": {"pathname": "app.tex"}, "atV": 11}]},
            {"toV": 11, "meta": DBX, "pathnames": ["local.tex"]},
            {"toV": 10},
        ])
        self.zip_files = {"main.tex": "M2", "app.tex": "A", "local.tex": "L"}
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual((self.folder / "main.tex").read_text(), "M2")
        self.assertEqual((self.folder / "app.tex").read_text(), "A")
        self.assertFalse((self.folder / "local.tex").exists())  # dropbox-origin path

    def test_history_resync_still_records_removes(self):
        # cached_toV older than the first page -> full pull, but removes seen
        # on the page are still recorded.
        self._seed(1)
        self._old_file("old.tex")
        self._updates([
            {"toV": 7, "meta": WEB, "pathnames": [],
             "project_ops": [{"remove": {"pathname": "old.tex"}, "atV": 6}]},
            {"toV": 6, "meta": WEB, "pathnames": ["main.tex"]},
        ])
        self.zip_files = {"main.tex": "M"}
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual(self.dl_calls, 1)
        self.assertEqual((self.folder / "main.tex").read_text(), "M")
        self.assertEqual(cli.web_removed_paths(self.pid, self.folder), {"old.tex": None})


class CollectWebFileOpsTests(unittest.TestCase):
    def test_rename_chain(self):
        # a -> b, then b -> c: a and b are gone, c is present.
        updates = [
            {"toV": 3, "project_ops": [{"rename": {"pathname": "b.tex", "newPathname": "c.tex"}, "atV": 2}]},
            {"toV": 2, "project_ops": [{"rename": {"pathname": "a.tex", "newPathname": "b.tex"}, "atV": 1}]},
        ]
        present, removed = cli._collect_web_file_ops(updates)
        self.assertEqual(present, {"c.tex"})
        self.assertEqual(removed, {"a.tex": "b.tex", "b.tex": "c.tex"})

    def test_malformed_ops_ignored(self):
        present, removed = cli._collect_web_file_ops([
            {"toV": 2, "project_ops": ["junk", {"add": {}}, {"rename": {"pathname": ""}}, {}]},
        ])
        self.assertEqual((present, removed), (set(), {}))


if __name__ == "__main__":
    unittest.main()
