"""How the client reads Overleaf's replies (0.5.1).

The tool depends on overleaf.com endpoints that are not a public API:
/project/<id>/updates, /project/<id>/download/zip and the dashboard's
ol-prefetchedProjectsBlob. These tests pin how each reply is mapped today
(status codes to exception classes, malformed bodies, the project-index
parser and its fallbacks), so a change in Overleaf's behaviour or in this
code shows up as a test failure, not as a silent pass-through.

Every fixture is synthetic: no real names, ids or cookies. get_session is
replaced by a fake whose .get returns canned responses; nothing touches the
network.

Run with: python -m unittest discover tests
"""
import html
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402

PID = "0123456789abcdef01234567"
PID2 = "89abcdef0123456789abcdef"


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class _Env(unittest.TestCase):
    NAMES = ("CACHE_DIR", "CACHE_FILE", "STATE_FILE", "VERSIONS_FILE", "INDEX_FILE",
             "WEB_REMOVED_FILE", "_VALIDATION_FILE", "get_session",
             "_fingerprint_sanity_warn")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name).resolve()
        self._orig = {n: getattr(cli, n) for n in self.NAMES}
        data = self.base / "data"
        data.mkdir()
        cli.CACHE_DIR = data
        cli.CACHE_FILE = data / "cookies.json"
        cli.STATE_FILE = data / "state.json"
        cli.VERSIONS_FILE = data / "versions.json"
        cli.INDEX_FILE = data / "projects.json"
        cli.WEB_REMOVED_FILE = data / "web-removed.json"
        cli._VALIDATION_FILE = data / ".validated-at"
        cli._fingerprint_sanity_warn = lambda *a, **k: None
        self._stderr = sys.stderr
        sys.stderr = io.StringIO()

    def tearDown(self):
        sys.stderr = self._stderr
        for n, v in self._orig.items():
            setattr(cli, n, v)
        self._tmp.cleanup()

    def serve(self, *responses):
        self.session = FakeSession(responses)
        cli.get_session = lambda force_refresh=False: self.session
        return self.session


LOGIN_PAGE = ("<!DOCTYPE html><html><head><title>Log in to Overleaf</title></head>"
              "<body><form action=\"/login\"></form></body></html>")


class UpdatesReplyTests(_Env):
    def test_401_and_403_raise_auth_expired(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.serve(FakeResponse(status))
                with self.assertRaises(cli.AuthExpired) as cm:
                    cli.fetch_updates(PID)
                self.assertIn(f"HTTP {status}", str(cm.exception))
                self.assertIn("overleaf-sync-now login", str(cm.exception))

    def test_429_raises_rate_limited_with_a_default_wait(self):
        for headers in ({}, {"Retry-After": "soon"}, {"Retry-After": ""}):
            with self.subTest(headers=headers):
                self.serve(FakeResponse(429, headers=headers))
                with self.assertRaises(cli.RateLimited) as cm:
                    cli.fetch_updates(PID)
                self.assertEqual(cm.exception.retry_after, 60)
        self.serve(FakeResponse(429, headers={"Retry-After": "17"}))
        with self.assertRaises(cli.RateLimited) as cm:
            cli.fetch_updates(PID)
        self.assertEqual(cm.exception.retry_after, 17)

    def test_other_statuses_raise_plain_runtime_error(self):
        for status in (404, 500):
            with self.subTest(status=status):
                self.serve(FakeResponse(status))
                with self.assertRaises(RuntimeError) as cm:
                    cli.fetch_updates(PID)
                self.assertIs(type(cm.exception), RuntimeError)
                self.assertIn(f"HTTP {status}", str(cm.exception))

    def test_html_login_page_with_200_is_invalid_json(self):
        self.serve(FakeResponse(200, LOGIN_PAGE, {"Content-Type": "text/html"}))
        with self.assertRaises(RuntimeError) as cm:
            cli.fetch_updates(PID)
        self.assertIs(type(cm.exception), RuntimeError)
        self.assertIn("invalid JSON", str(cm.exception))

    def test_requests_the_project_updates_url(self):
        s = self.serve(FakeResponse(200, json.dumps({"updates": []})))
        self.assertEqual(cli.fetch_updates(PID), {"updates": []})
        self.assertEqual(s.urls, [f"{cli.BASE}/project/{PID}/updates"])

    def test_latest_update_without_toV_raises(self):
        self.serve(FakeResponse(200, json.dumps({"updates": [
            {"fromV": 3, "pathnames": ["main.tex"], "meta": {"origin": {"kind": "web"}}},
        ]})))
        folder = self.base / "proj"
        folder.mkdir()
        with self.assertRaises(RuntimeError) as cm:
            cli.refresh_project(PID, folder)
        self.assertIn("missing toV", str(cm.exception))

    def test_empty_history_is_reported_not_raised(self):
        self.serve(FakeResponse(200, json.dumps({"updates": []})))
        folder = self.base / "proj"
        folder.mkdir()
        self.assertEqual(cli.refresh_project(PID, folder), "empty_history")


class ZipReplyTests(_Env):
    def test_status_mapping(self):
        for status, exc in ((401, cli.AuthExpired), (403, cli.AuthExpired),
                            (429, cli.RateLimited), (404, RuntimeError)):
            with self.subTest(status=status):
                self.serve(FakeResponse(status))
                with self.assertRaises(exc) as cm:
                    cli.download_zip(PID)
                if status == 404:
                    self.assertIs(type(cm.exception), RuntimeError)
                    self.assertIn("HTTP 404", str(cm.exception))

    def test_200_body_that_is_not_a_zip(self):
        # download_zip returns the bytes as they came; reading them as a zip
        # raises zipfile.BadZipFile. The hook reports that as "refresh
        # failed" and lets the call through (docs/architecture.md).
        self.serve(FakeResponse(200, LOGIN_PAGE))
        self.assertEqual(cli.download_zip(PID), LOGIN_PAGE.encode("utf-8"))
        folder = self.base / "proj"
        folder.mkdir()
        cli._save_versions({PID: 4})
        updates = FakeResponse(200, json.dumps({"updates": [
            {"fromV": 4, "toV": 5, "pathnames": ["main.tex"],
             "meta": {"origin": {"kind": "web"}}},
            {"toV": 4},
        ]}))
        self.serve(updates, FakeResponse(200, LOGIN_PAGE))
        with self.assertRaises(zipfile.BadZipFile):
            cli.refresh_project(PID, folder)
        self.assertEqual(cli._load_versions()[PID], 4)  # not advanced


def _dashboard(projects, *, total=None, meta=True, anchors=()):
    blob = {"projects": projects}
    if total is not None:
        blob["totalSize"] = total
    head = ""
    if meta:
        head = (f'<meta name="ol-prefetchedProjectsBlob" data-type="json" '
                f'content="{html.escape(json.dumps(blob), quote=True)}">')
    links = "".join(f'<a href="/project/{pid}">{name}</a>' for pid, name in anchors)
    return f"<!DOCTYPE html><html><head>{head}</head><body>{links}</body></html>"


def _proj(pid, name, **kw):
    rec = {"id": pid, "name": name, "lastUpdated": "2026-01-02T03:04:05.000Z",
           "owner": {"id": "f" * 24}, "trashed": False, "archived": False}
    rec.update(kw)
    return rec


class ProjectIndexTests(_Env):
    def test_prefetched_blob_is_parsed(self):
        page = _dashboard([_proj(PID, "Paper & Notes \"draft\""),
                           _proj(PID2, "Old", trashed=True, archived=True)], total=2)
        self.serve(FakeResponse(200, page))
        records = cli._refresh_projects_records()
        self.assertEqual([r["id"] for r in records], [PID, PID2])
        self.assertEqual(records[0]["name"], "Paper & Notes \"draft\"")
        self.assertEqual(records[0]["ownerId"], "f" * 24)
        self.assertEqual(records[0]["lastUpdated"], "2026-01-02T03:04:05.000Z")
        self.assertEqual((records[1]["trashed"], records[1]["archived"]), (True, True))
        index = json.loads(cli.INDEX_FILE.read_text())
        self.assertEqual(index["version"], cli.INDEX_FORMAT_VERSION)
        self.assertEqual(index["projects"], records)
        self.assertNotIn("truncated", index)
        self.assertIsNone(cli._index_truncation())

    def test_anchor_fallback_when_the_meta_tag_is_missing(self):
        page = _dashboard([], meta=False,
                          anchors=[(PID, "Paper"), (PID2, "Thesis")])
        page = page.replace("</body>", f'<a href="/project/{PID}/download/zip">Download</a></body>')
        self.serve(FakeResponse(200, page))
        records = cli._refresh_projects_records()
        self.assertEqual([(r["id"], r["name"]) for r in records], [(PID, "Paper"), (PID2, "Thesis")])
        self.assertFalse(records[0]["trashed"])

    def test_non_200_keeps_the_cached_index_byte_for_byte(self):
        self.serve(FakeResponse(200, _dashboard([_proj(PID, "Paper")])))
        cli._refresh_projects_records()
        before = cli.INDEX_FILE.read_bytes()
        s = self.serve(FakeResponse(500, "oops"))
        records = cli._refresh_projects_records()
        self.assertEqual([r["id"] for r in records], [PID])
        self.assertEqual(cli.INDEX_FILE.read_bytes(), before)
        self.assertEqual(len(s.urls), 2)  # retried once with a fresh session

    def test_empty_parse_keeps_the_cached_index(self):
        self.serve(FakeResponse(200, _dashboard([_proj(PID, "Paper")])))
        cli._refresh_projects_records()
        before = cli.INDEX_FILE.read_bytes()
        self.serve(FakeResponse(200, LOGIN_PAGE))
        records = cli._refresh_projects_records()
        self.assertEqual([r["id"] for r in records], [PID])
        self.assertEqual(cli.INDEX_FILE.read_bytes(), before)


class TruncatedIndexTests(_Env):
    """If Overleaf ever pages the dashboard list (page size 20 upstream), a
    folder whose project is on a later page must not be reported as 'no
    project named X'. Writes stay blocked; only the message changes."""

    def setUp(self):
        super().setUp()
        self.folder = self.base / "Dropbox" / "Apps" / "Overleaf" / "Unlisted Paper"
        self.folder.mkdir(parents=True)
        (self.folder / "main.tex").write_text("x")

    def _hook_write(self):
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps({
            "tool_name": "Edit", "tool_input": {"file_path": str(self.folder / "main.tex")}}))
        sys.stderr = err = io.StringIO()
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_hook([])
        finally:
            sys.stdin, sys.stdout = stdin, stdout
            sys.stderr = io.StringIO()
        return cm.exception.code, err.getvalue()

    def test_blob_with_more_projects_than_listed_records_it(self):
        self.serve(FakeResponse(200, _dashboard([_proj(PID, "Paper")], total=45)))
        cli._refresh_projects_records()
        index = json.loads(cli.INDEX_FILE.read_text())
        self.assertEqual(index["truncated"], {"listed": 1, "total": 45})
        self.assertEqual(cli._index_truncation(), {"listed": 1, "total": 45})
        self.assertEqual(cli._load_cached_projects_records()[0]["id"], PID)

    def test_hook_write_to_unmatched_folder_says_the_list_was_cut_short(self):
        self.serve(FakeResponse(200, _dashboard([_proj(PID, "Paper")], total=45)))
        cli._refresh_projects_records()
        code, err = self._hook_write()
        self.assertEqual(code, 2)
        self.assertIn("Overleaf listed only 1 of your 45 projects", err)
        self.assertIn(f'overleaf-sync-now link <project_id> "{self.folder}"', err)
        self.assertNotIn("projects --refresh", err)
        self.assertFalse((self.folder / cli.PROJECT_MARKER).exists())

    def test_full_list_keeps_the_no_project_message(self):
        self.serve(FakeResponse(200, _dashboard([_proj(PID, "Paper")], total=1)))
        cli._refresh_projects_records()
        self.assertNotIn("truncated", json.loads(cli.INDEX_FILE.read_text()))
        code, err = self._hook_write()
        self.assertEqual(code, 2)
        self.assertIn("no living (non-trashed, non-archived) Overleaf project is named", err)
        self.assertNotIn("listed only", err)

    def test_projects_command_prints_a_note(self):
        cli.INDEX_FILE.write_text(json.dumps({
            "version": cli.INDEX_FORMAT_VERSION, "ts": time.time(),
            "projects": [{"id": PID, "name": "Paper"}],
            "truncated": {"listed": 1, "total": 45},
        }))
        stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            cli.cmd_projects([])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = stdout
        self.assertIn("Note: Overleaf listed only 1 of 45 projects.", out)


if __name__ == "__main__":
    unittest.main()
