"""No network is not expired cookies (0.5.0).

Pins: when Overleaf can't be reached (offline laptop, DNS or proxy failure,
or Codex CLI's network-disabled sandbox), cookie validation reports
"unreachable", get_session raises NetworkUnavailable (never AuthExpired) and
skips the browser extractors, the hook lets the call through with a note,
and status / doctor / save-cookie say "could not reach Overleaf" instead of
INVALID / rejected.

The exceptions are built the way requests 2.x / urllib3 2.x raise them. The
shapes were captured inside `codex sandbox -P :workspace` (Codex CLI 0.164,
macOS Seatbelt): DNS fails with NameResolutionError ([Errno 8]); a connect to
a literal IP fails with NewConnectionError caused by PermissionError
([Errno 1] Operation not permitted). No real network is used.

Run with: python -m unittest discover tests
"""
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import requests  # noqa: E402
from urllib3.connection import HTTPSConnection  # noqa: E402
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError  # noqa: E402

from overleaf_sync_now import cli  # noqa: E402

PID = "a" * 24
_SANDBOX_ENV = ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def _requests_error(inner_factory):
    """Raise and catch the chain requests produces: ConnectionError <-
    MaxRetryError <- urllib3 error <- OSError. Returns the outer exception."""
    conn = HTTPSConnection("www.overleaf.com", 443)
    try:
        try:
            try:
                inner_factory(conn)
            except (NameResolutionError, NewConnectionError) as u3:
                raise MaxRetryError(None, "/project", u3) from u3
        except MaxRetryError as mre:
            raise requests.exceptions.ConnectionError(mre)
    except requests.exceptions.ConnectionError as outer:
        return outer
    raise AssertionError("no exception raised")


def dns_failure():
    def inner(conn):
        try:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        except socket.gaierror as e:
            raise NameResolutionError("www.overleaf.com", conn, e) from e
    return _requests_error(inner)


def eperm_failure():
    def inner(conn):
        try:
            raise PermissionError(1, "Operation not permitted")
        except PermissionError as e:
            raise NewConnectionError(conn, f"Failed to establish a new connection: {e}") from e
    return _requests_error(inner)


def refused_failure():
    def inner(conn):
        try:
            raise ConnectionRefusedError(61, "Connection refused")
        except ConnectionRefusedError as e:
            raise NewConnectionError(conn, f"Failed to establish a new connection: {e}") from e
    return _requests_error(inner)


class _Env(unittest.TestCase):
    """Temp data dir; requests.Session.get raises `self.net_error` (or
    returns `self.status`). Codex sandbox env vars are cleared."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name).resolve()
        self.data = self.base / "data"
        self.data.mkdir()
        self._orig = {name: getattr(cli, name) for name in (
            "CACHE_DIR", "CACHE_FILE", "STATE_FILE", "VERSIONS_FILE", "INDEX_FILE",
            "WEB_REMOVED_FILE", "_VALIDATION_FILE", "_resolve_cookies",
        )}
        cli.CACHE_DIR = self.data
        cli.CACHE_FILE = self.data / "cookies.json"
        cli.STATE_FILE = self.data / "state.json"
        cli.VERSIONS_FILE = self.data / "versions.json"
        cli.INDEX_FILE = self.data / "projects.json"
        cli.WEB_REMOVED_FILE = self.data / "web-removed.json"
        cli._VALIDATION_FILE = self.data / ".validated-at"
        self.chain_calls = []
        cli._resolve_cookies = lambda interactive=False, **kw: self.chain_calls.append(1)
        self.net_error = dns_failure()
        self.status = None
        test = self

        def fake_get(session, url, *a, **k):
            if test.status is not None:
                r = requests.Response()
                r.status_code = test.status
                return r
            raise test.net_error

        self._patch = mock.patch.object(requests.Session, "get", fake_get)
        self._patch.start()
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        for k in _SANDBOX_ENV:
            os.environ.pop(k, None)
        self._streams = (sys.stdin, sys.stdout, sys.stderr)

    def tearDown(self):
        sys.stdin, sys.stdout, sys.stderr = self._streams
        self._env.stop()
        self._patch.stop()
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def cookie(self):
        cli.CACHE_FILE.write_text(json.dumps({cli.SESSION_COOKIE: "v"}))


class ClassificationTests(_Env):
    def test_check_cookies_is_three_way(self):
        self.assertEqual(cli._check_cookies(None)[0], "missing")
        state, exc = cli._check_cookies({cli.SESSION_COOKIE: "v"})
        self.assertEqual(state, "unreachable")
        self.assertIsInstance(exc, requests.exceptions.ConnectionError)
        self.status = 302
        self.assertEqual(cli._check_cookies({cli.SESSION_COOKIE: "v"})[0], "rejected")
        self.status = 403
        self.assertEqual(cli._check_cookies({cli.SESSION_COOKIE: "v"})[0], "rejected")
        self.status = 503
        self.assertEqual(cli._check_cookies({cli.SESSION_COOKIE: "v"})[0], "unreachable")
        self.status = 200
        self.assertEqual(cli._check_cookies({cli.SESSION_COOKIE: "v"})[0], "ok")

    def test_macos_seatbelt_eperm_is_a_sandbox_block(self):
        self.assertTrue(cli._is_sandbox_block(eperm_failure()))

    def test_dns_failure_inside_codex_sandbox_is_a_sandbox_block(self):
        self.assertFalse(cli._is_sandbox_block(dns_failure()))
        os.environ["CODEX_SANDBOX"] = "seatbelt"
        self.assertTrue(cli._is_sandbox_block(dns_failure()))

    def test_network_disabled_flag_is_a_sandbox_block(self):
        os.environ["CODEX_SANDBOX_NETWORK_DISABLED"] = "1"
        self.assertTrue(cli._is_sandbox_block(refused_failure()))

    def test_plain_offline_is_not_a_sandbox_block(self):
        for err in (dns_failure(), refused_failure()):
            self.assertFalse(cli._is_sandbox_block(err))
            msg = cli._network_unavailable_message(err)
            self.assertIn("Could not reach Overleaf", msg)
            self.assertIn("not a login problem", msg)
            self.assertNotIn("sandbox", msg)

    def test_local_file_permission_error_is_still_not_network(self):
        self.assertFalse(cli._is_sandbox_block(PermissionError(1, "Operation not permitted", "/x.tex")))


class GetSessionTests(_Env):
    def test_offline_with_cached_cookie_raises_network_unavailable(self):
        self.cookie()
        with self.assertRaises(cli.NetworkUnavailable) as cm:
            cli.get_session()
        self.assertNotIsInstance(cm.exception, cli.AuthExpired)
        self.assertIn("Could not reach Overleaf", str(cm.exception))
        self.assertEqual(self.chain_calls, [], "browser extraction must be skipped")

    def test_codex_sandbox_gets_sandbox_hint(self):
        self.cookie()
        self.net_error = eperm_failure()
        with self.assertRaises(cli.NetworkUnavailable) as cm:
            cli.get_session()
        self.assertEqual(str(cm.exception), cli._SANDBOX_HINT)

    def test_rejected_cookie_still_raises_authexpired(self):
        self.cookie()
        self.status = 302
        cli._resolve_cookies = lambda interactive=False, **kw: None
        with self.assertRaises(cli.AuthExpired):
            cli.get_session()


class HookOfflineTests(_Env):
    def setUp(self):
        super().setUp()
        self.folder = self.base / "Dropbox" / "Apps" / "Overleaf" / "My JMP"
        self.folder.mkdir(parents=True)
        (self.folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": PID}))
        self.tex = self.folder / "main.tex"
        self.tex.write_text("x")

    def run_hook(self, tool):
        key = "file_path"
        sys.stdin = io.StringIO(json.dumps({"tool_name": tool, "tool_input": {key: str(self.tex)}}))
        sys.stderr = err = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_hook([])
        sys.stderr = self._streams[2]
        return cm.exception.code or 0, err.getvalue()

    def test_offline_edit_passes_with_note_not_auth_block(self):
        # Validation cache expired (none written), cookie cached, no network.
        self.cookie()
        for tool in ("Read", "Edit"):
            cli._save_state({})
            code, err = self.run_hook(tool)
            self.assertEqual(code, 0, err)
            self.assertIn("Could not reach Overleaf", err)
            self.assertIn(f"{tool} allowed", err)
            self.assertNotIn("No valid Overleaf cookies", err)
            self.assertNotIn("overleaf-sync-now login", err)
        self.assertEqual(self.chain_calls, [])

    def test_codex_sandbox_edit_passes_with_sandbox_hint(self):
        self.cookie()
        os.environ.update(CODEX_SANDBOX="seatbelt", CODEX_SANDBOX_NETWORK_DISABLED="1")
        code, err = self.run_hook("Edit")
        self.assertEqual(code, 0, err)
        self.assertIn("sandbox that blocks network access", err)

    def test_offline_unresolved_folder_passes(self):
        # No marker and no cached project index: the project list can't be
        # fetched, which is a network problem, not "not logged in".
        (self.folder / cli.PROJECT_MARKER).unlink()
        self.cookie()
        code, err = self.run_hook("Write")
        self.assertEqual(code, 0, err)
        self.assertIn("could not reach overleaf", err.lower())
        self.assertNotIn("not logged in", err)


class CommandMessagesTests(_Env):
    def capture(self, fn, *args):
        sys.stdout = out = io.StringIO()
        sys.stderr = err = io.StringIO()
        code = 0
        try:
            fn(*args)
        except SystemExit as e:
            code = e.code or 0
        finally:
            sys.stdout, sys.stderr = self._streams[1], self._streams[2]
        return code, out.getvalue() + err.getvalue()

    def test_status_says_could_not_reach(self):
        self.cookie()
        code, out = self.capture(cli.cmd_status, [str(self.base)])
        self.assertIn("could not reach Overleaf", out)
        self.assertNotIn("INVALID", out)

    def test_doctor_says_unknown_not_invalid(self):
        self.cookie()
        cli._resolve_cookies = self._orig["_resolve_cookies"]  # the real chain
        # Keep the per-browser diagnostics away from any real browser store.
        with mock.patch.dict(sys.modules, {"rookiepy": None, "browser_cookie3": None}):
            code, out = self.capture(cli.cmd_doctor, [str(self.base)])
        self.assertIn("could not reach Overleaf", out)
        self.assertNotIn("INVALID (rejected", out)
        self.assertNotIn("WOULD FAIL", out)

    def test_save_cookie_says_could_not_reach_and_saves_nothing(self):
        sys.stdin = io.StringIO("abc123\n")
        code, out = self.capture(cli.cmd_save_cookie, ["-"])
        self.assertEqual(code, 1)
        self.assertIn("could not reach Overleaf", out)
        self.assertNotIn("rejected", out)
        self.assertFalse(cli.CACHE_FILE.exists())


if __name__ == "__main__":
    unittest.main()
