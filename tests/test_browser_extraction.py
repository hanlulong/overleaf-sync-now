"""Installed-browser cookie extraction stays out of the hook (0.5.0).

On macOS, rookiepy and browser_cookie3 ask the Keychain for every Chromium
browser's "Safe Storage" key, which can open a password dialog per browser.
Pins:
  * the PreToolUse hook never runs the installed-browser extractors (only
    the cached cookie and the `login` profile);
  * other automatic callers (sync) try them, then back off for an hour;
  * setup / status / doctor always try them;
  * each library is asked once per browser (no second all-browsers `load()`).

Run with: python -m unittest discover tests
"""
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402

PID = "b" * 24


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name).resolve()
        self.data = self.base / "data"
        self.data.mkdir()
        self._orig = {name: getattr(cli, name) for name in (
            "CACHE_DIR", "CACHE_FILE", "STATE_FILE", "VERSIONS_FILE", "INDEX_FILE",
            "WEB_REMOVED_FILE", "_VALIDATION_FILE", "_try_login_profile", "_try_rookiepy",
            "_try_browser_cookie3", "_try_playwright_profile",
        )}
        cli.CACHE_DIR = self.data
        cli.CACHE_FILE = self.data / "cookies.json"
        cli.STATE_FILE = self.data / "state.json"
        cli.VERSIONS_FILE = self.data / "versions.json"
        cli.INDEX_FILE = self.data / "projects.json"
        cli.WEB_REMOVED_FILE = self.data / "web-removed.json"
        cli._VALIDATION_FILE = self.data / ".validated-at"
        self.calls = []
        cli._try_login_profile = lambda: self.calls.append("login") or None
        cli._try_rookiepy = lambda: self.calls.append("rookiepy") or None
        cli._try_browser_cookie3 = lambda: self.calls.append("browser_cookie3") or None
        cli._try_playwright_profile = lambda: self.calls.append("playwright") or None
        self._streams = (sys.stdin, sys.stdout, sys.stderr)
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

    def tearDown(self):
        sys.stdin, sys.stdout, sys.stderr = self._streams
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def browsers_tried(self):
        return [c for c in self.calls if c != "login"]


class HookPathTests(_Env):
    def test_hook_never_runs_browser_extractors(self):
        folder = self.base / "Dropbox" / "Apps" / "Overleaf" / "Paper"
        folder.mkdir(parents=True)
        (folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": PID}))
        (folder / "main.tex").write_text("x")
        for tool in ("Read", "Edit"):
            cli._save_state({})
            sys.stdin = io.StringIO(json.dumps(
                {"tool_name": tool, "tool_input": {"file_path": str(folder / "main.tex")}}))
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_hook([])
            self.assertEqual(cm.exception.code or 0, 2 if tool == "Edit" else 0)
        self.assertEqual(self.browsers_tried(), [])
        self.assertIn("login", self.calls)
        self.assertTrue(cli._BROWSER_EXTRACTION["allowed"], "flag restored after the hook")

    def test_sync_path_tries_browsers_then_backs_off(self):
        with self.assertRaises(cli.AuthExpired):
            cli.get_session()
        self.assertEqual(self.browsers_tried(), ["rookiepy", "browser_cookie3", "playwright"])
        self.calls.clear()
        with self.assertRaises(cli.AuthExpired):
            cli.get_session()
        self.assertEqual(self.browsers_tried(), [], "cooldown after a failed chain")

    def test_setup_always_tries_browsers(self):
        cli._mark_browser_extraction_failed()
        cli.cmd_setup([], force_noninteractive=True)
        self.assertEqual(self.browsers_tried(), ["rookiepy", "browser_cookie3", "playwright"])


class OnePassPerBrowserTests(unittest.TestCase):
    def test_browser_cookie3_is_not_asked_twice(self):
        calls = []

        def make(name):
            def fn(domain_name=""):
                calls.append(name)
                raise RuntimeError("no cookies")
            return fn

        fake = types.ModuleType("browser_cookie3")
        for name in (*cli.CHROMIUM_COOKIE_FNS, *cli.EXTRA_COOKIE_FNS, "load"):
            setattr(fake, name, make(name))
        with mock.patch.dict(sys.modules, {"browser_cookie3": fake}):
            self.assertIsNone(cli._try_browser_cookie3())
        self.assertNotIn("load", calls)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertIn("safari", calls)


if __name__ == "__main__":
    unittest.main()
