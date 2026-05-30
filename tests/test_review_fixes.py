"""Regression tests for the v0.4.0 deep-review fixes.

These pin the behaviors changed/added during the review:

  * refresh_project's version-match decision (bootstrap / no_change / rewind /
    dropbox-echo / mixed / all_covered full-pull) AND the new invariant that a
    PROTECTED file holds the cached version back so the pending web edit is
    re-offered instead of being stranded.
  * _extract_files zip-slip containment guard and the protect-recent-mtime
    window (the core anti-clobber mechanism).
  * cmd_hook's exit-code contract — exit 2 (block) ONLY on AuthExpired, exit 0
    (pass through) on everything else, and mark_synced NOT called when blocking.
  * get_session raising AuthExpired (not a plain RuntimeError) on missing/expired
    cookies so the hook blocks, while a sandbox block stays a plain RuntimeError
    so it passes through. Plus the bounded GET retry adapter being mounted.
  * find_linked_folder parent-walk, shared-level skip, and Apps/Overleaf
    auto-link dispatch.
  * cmd_save_cookie value normalization.
  * the data dir being hardened to 0o700 on POSIX.
  * the single-sourced version not drifting across __init__/pyproject/CITATION.

No network is touched: every HTTP-facing dependency is monkeypatched.

Run with: python -m unittest discover tests
"""
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import overleaf_sync_now  # noqa: E402
from overleaf_sync_now import cli  # noqa: E402


# ---------------------------------------------------------------------------
# refresh_project: version-match decision + the protected-file version hold
# ---------------------------------------------------------------------------
class RefreshDecisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        self.pid = "a" * 24
        self._orig = {
            "fetch_updates": cli.fetch_updates,
            "download_zip": cli.download_zip,
            "_extract_files": cli._extract_files,
            "_fingerprint_sanity_warn": cli._fingerprint_sanity_warn,
            "VERSIONS_FILE": cli.VERSIONS_FILE,
        }
        cli.VERSIONS_FILE = self.folder / "versions.json"
        cli._fingerprint_sanity_warn = lambda *a, **k: None  # silence stderr noise
        self.dl_calls = []
        self.extract_calls = []
        self.extract_ret = (1, 0, 0)  # (written, unchanged, protected)

        def fake_download(pid):
            self.dl_calls.append(pid)
            return b"ZIPBYTES"

        def fake_extract(zip_bytes, folder, paths, protect_recent_seconds=30):
            self.extract_calls.append({"paths": paths, "protect": protect_recent_seconds})
            return self.extract_ret

        cli.download_zip = fake_download
        cli._extract_files = fake_extract

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def _updates(self, updates):
        cli.fetch_updates = lambda pid: {"updates": updates}

    def _seed(self, toV):
        cli._save_versions({self.pid: toV})

    def _cached(self):
        return cli._load_versions().get(self.pid)

    def test_empty_history(self):
        self._updates([])
        self.assertEqual(cli.refresh_project(self.pid, self.folder), "empty_history")
        self.assertEqual(self.dl_calls, [])

    def test_missing_toV_on_latest_raises(self):
        self._updates([{"no_toV": 1}])
        with self.assertRaises(RuntimeError):
            cli.refresh_project(self.pid, self.folder)

    def test_bootstrap_pulls_all(self):
        self._updates([{"toV": 5}])
        status = cli.refresh_project(self.pid, self.folder)
        self.assertTrue(status.startswith("bootstrap"))
        self.assertEqual(len(self.dl_calls), 1)
        self.assertIsNone(self.extract_calls[0]["paths"])  # extract everything
        self.assertEqual(self._cached(), 5)

    def test_no_change_skips_download(self):
        self._seed(5)
        self._updates([{"toV": 5}])
        self.assertEqual(cli.refresh_project(self.pid, self.folder), "no_change")
        self.assertEqual(self.dl_calls, [])

    def test_rewind_rebootstraps(self):
        # Overleaf history rewound (or cache seeded from another machine):
        # latest < cached must re-anchor via bootstrap.
        self._seed(10)
        self._updates([{"toV": 5}])
        status = cli.refresh_project(self.pid, self.folder)
        self.assertTrue(status.startswith("bootstrap"))
        self.assertEqual(len(self.dl_calls), 1)
        self.assertEqual(self._cached(), 5)

    def test_dropbox_echo_bumps_version_without_download(self):
        self._seed(5)
        self._updates([
            {"toV": 7, "meta": {"origin": {"kind": "dropbox"}}, "pathnames": ["main.tex"]},
            {"toV": 6, "meta": {"origin": {"kind": "dropbox"}}, "pathnames": ["a.tex"]},
            {"toV": 5},
        ])
        status = cli.refresh_project(self.pid, self.folder)
        self.assertIn("dropbox_echo", status)
        self.assertEqual(self.dl_calls, [])          # NO zip download
        self.assertEqual(self._cached(), 7)          # but version advanced

    def test_mixed_extracts_only_web_paths(self):
        self._seed(5)
        self._updates([
            {"toV": 7, "meta": {"origin": {"kind": "web"}}, "pathnames": ["intro.tex"]},
            {"toV": 6, "meta": {"origin": {"kind": "dropbox"}}, "pathnames": ["main.tex"]},
            {"toV": 5},
        ])
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual(len(self.dl_calls), 1)
        self.assertEqual(self.extract_calls[0]["paths"], {"intro.tex"})  # dropbox path excluded
        self.assertEqual(self._cached(), 7)

    def test_cached_older_than_page_forces_full_pull(self):
        # cached_toV predates the oldest update on the first page -> can't be
        # sure we saw every web-origin edit -> full pull (need_paths=None).
        self._seed(1)
        self._updates([
            {"toV": 7, "meta": {"origin": {"kind": "web"}}, "pathnames": ["a.tex"]},
            {"toV": 6, "meta": {"origin": {"kind": "web"}}, "pathnames": ["b.tex"]},
            {"toV": 5, "meta": {"origin": {"kind": "web"}}, "pathnames": ["c.tex"]},
        ])
        cli.refresh_project(self.pid, self.folder)
        self.assertEqual(len(self.dl_calls), 1)
        self.assertIsNone(self.extract_calls[0]["paths"])
        self.assertEqual(self._cached(), 7)

    def test_force_passes_protect_zero_and_advances(self):
        self._seed(5)
        self._updates([{"toV": 5}])
        cli.refresh_project(self.pid, self.folder, force=True)
        self.assertEqual(len(self.dl_calls), 1)            # force bypasses no_change
        self.assertEqual(self.extract_calls[0]["protect"], 0)
        self.assertEqual(self._cached(), 5)

    # ---- the headline fix ----
    def test_protected_file_holds_version_back(self):
        # A web-origin edit is in the zip, but the local file was protected
        # (recent mtime). The version MUST NOT advance, or the next sync would
        # return no_change forever and strand the web edit.
        self._seed(5)
        self._updates([
            {"toV": 7, "meta": {"origin": {"kind": "web"}}, "pathnames": ["main.tex"]},
            {"toV": 5},
        ])
        self.extract_ret = (0, 0, 1)  # one protected, nothing written
        status = cli.refresh_project(self.pid, self.folder)
        self.assertEqual(len(self.dl_calls), 1)  # we did download + try
        self.assertEqual(self._cached(), 5)      # but version held at cached
        self.assertIn("protected", status)
        self.assertIn("held at 5", status)

    def test_protected_on_bootstrap_does_not_record_version(self):
        self._updates([{"toV": 7, "meta": {"origin": {"kind": "web"}}, "pathnames": ["m.tex"]}])
        self.extract_ret = (0, 0, 1)
        cli.refresh_project(self.pid, self.folder)
        self.assertIsNone(self._cached())  # nothing recorded -> next sync retries


# ---------------------------------------------------------------------------
# _extract_files: zip-slip containment + protect-recent-mtime window
# ---------------------------------------------------------------------------
class ExtractFilesSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "proj"
        self.folder.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _zip(self, files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    def test_zip_slip_entries_cannot_escape_folder(self):
        zb = self._zip({
            "../escape.tex": "evil",
            "sub/../../escape2.tex": "evil",
            "main.tex": "legit",
        })
        # Must not raise, must not write outside the folder, must still extract
        # the legitimate sibling.
        cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=0)
        self.assertFalse((self.folder.parent / "escape.tex").exists())
        self.assertFalse((self.folder.parent / "escape2.tex").exists())
        self.assertEqual((self.folder / "main.tex").read_text(), "legit")

    def test_absolute_entry_is_contained_not_escaped(self):
        # A leading-slash entry is stripped to a relative path and lands INSIDE
        # the folder (not an escape) — verify it stays contained.
        zb = self._zip({"/abs/escape.tex": "x"})
        cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=0)
        self.assertTrue((self.folder / "abs" / "escape.tex").exists())
        self.assertFalse((pathlib.Path("/abs") / "escape.tex").exists())

    def test_recent_mtime_is_protected(self):
        local = self.folder / "main.tex"
        local.write_text("LOCAL")  # mtime ~ now
        zb = self._zip({"main.tex": "FROM_WEB"})
        w, u, p = cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=30)
        self.assertEqual((w, p), (0, 1))
        self.assertEqual(local.read_text(), "LOCAL")  # not clobbered

    def test_old_mtime_is_overwritten(self):
        local = self.folder / "main.tex"
        local.write_text("LOCAL")
        old = time.time() - 120
        os.utime(local, (old, old))
        zb = self._zip({"main.tex": "FROM_WEB"})
        w, u, p = cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=30)
        self.assertEqual((w, p), (1, 0))
        self.assertEqual(local.read_text(), "FROM_WEB")

    def test_force_zero_window_overwrites_recent(self):
        local = self.folder / "main.tex"
        local.write_text("LOCAL")
        zb = self._zip({"main.tex": "FROM_WEB"})
        w, u, p = cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=0)
        self.assertEqual((w, p), (1, 0))
        self.assertEqual(local.read_text(), "FROM_WEB")

    def test_identical_content_counts_unchanged(self):
        local = self.folder / "main.tex"
        local.write_text("SAME")
        zb = self._zip({"main.tex": "SAME"})
        w, u, p = cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=30)
        self.assertEqual((w, u, p), (0, 1, 0))


# ---------------------------------------------------------------------------
# cmd_hook: exit-code contract
# ---------------------------------------------------------------------------
class HookExitCodeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        self.pid = "b" * 24
        self._orig = {
            "find_linked_folder": cli.find_linked_folder,
            "refresh_project": cli.refresh_project,
            "is_debounced": cli.is_debounced,
            "mark_synced": cli.mark_synced,
        }
        self.link = (self.folder, self.pid)
        self.debounced = False
        self.refresh_outcome = lambda pid, folder: "ok"
        self.marked = []
        cli.find_linked_folder = lambda fp, verbose=True, **kw: self.link
        cli.is_debounced = lambda pid: self.debounced
        cli.mark_synced = lambda pid: self.marked.append(pid)
        cli.refresh_project = lambda pid, folder, **kw: self.refresh_outcome(pid, folder)
        self._stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self._stdin
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def _run(self, payload, raw=None):
        sys.stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_hook([])
        code = cm.exception.code
        return 0 if code is None else code

    def test_non_allowlisted_tool_passes(self):
        self.assertEqual(self._run({"tool_name": "Bash", "tool_input": {"file_path": "a.tex"}}), 0)

    def test_non_tex_extension_passes(self):
        self.assertEqual(self._run({"tool_name": "Edit", "tool_input": {"file_path": "a.png"}}), 0)

    def test_malformed_stdin_passes(self):
        self.assertEqual(self._run(None, raw="{not json"), 0)

    def test_no_link_passes(self):
        self.link = (None, None)
        self.assertEqual(self._run({"tool_name": "Edit", "tool_input": {"file_path": "a.tex"}}), 0)

    def test_debounced_passes_without_refresh(self):
        self.debounced = True
        self.assertEqual(self._run({"tool_name": "Edit", "tool_input": {"file_path": "a.tex"}}), 0)

    def test_success_passes_and_marks(self):
        self.assertEqual(self._run({"tool_name": "Edit", "tool_input": {"file_path": "a.tex"}}), 0)
        self.assertEqual(self.marked, [self.pid])

    def test_authexpired_blocks_and_does_not_mark(self):
        def boom(pid, folder):
            raise cli.AuthExpired("no cookies")
        self.refresh_outcome = boom
        self.assertEqual(self._run({"tool_name": "Edit", "tool_input": {"file_path": "a.tex"}}), 2)
        self.assertEqual(self.marked, [])  # must NOT debounce a blocking failure

    def test_ratelimited_passes_and_marks(self):
        def boom(pid, folder):
            raise cli.RateLimited(5)
        self.refresh_outcome = boom
        self.assertEqual(self._run({"tool_name": "Read", "tool_input": {"file_path": "a.bib"}}), 0)
        self.assertEqual(self.marked, [self.pid])

    def test_generic_error_passes_and_marks(self):
        def boom(pid, folder):
            raise RuntimeError("network blip")
        self.refresh_outcome = boom
        self.assertEqual(self._run({"tool_name": "Write", "tool_input": {"file_path": "a.sty"}}), 0)
        self.assertEqual(self.marked, [self.pid])

    def test_extension_regex_case_insensitive(self):
        self.assertTrue(re.search(r"\.(tex|bib|cls|sty|bst)$", "Paper.TEX", re.IGNORECASE))
        self.assertFalse(re.search(r"\.(tex|bib|cls|sty|bst)$", "a.texx", re.IGNORECASE))
        self.assertFalse(re.search(r"\.(tex|bib|cls|sty|bst)$", "a.tex.bak", re.IGNORECASE))


# ---------------------------------------------------------------------------
# get_session: AuthExpired vs RuntimeError, and the retry adapter
# ---------------------------------------------------------------------------
class GetSessionAuthTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "_load_cache": cli._load_cache,
            "_resolve_cookies": cli._resolve_cookies,
            "_validate_cookies": cli._validate_cookies,
        }

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cli, k, v)

    def test_no_cookies_raises_authexpired(self):
        # The headline fix: missing/expired cookies must raise AuthExpired (so
        # the hook BLOCKS the edit), not a plain RuntimeError (which the hook's
        # generic handler would pass through, editing a stale copy).
        cli._load_cache = lambda: None
        cli._resolve_cookies = lambda interactive=False: None
        with self.assertRaises(cli.AuthExpired):
            cli.get_session()

    def test_sandbox_block_stays_plain_runtimeerror(self):
        # A blocked outbound socket must NOT become AuthExpired — auth is
        # probably fine and the hook should pass the edit through, not block.
        import requests
        cli._load_cache = lambda: {cli.SESSION_COOKIE: "v"}
        cli._validate_cookies = lambda c, use_cache=True: False
        cli._resolve_cookies = lambda interactive=False: None
        orig_get = requests.get

        def fake_get(*a, **k):
            raise requests.exceptions.ConnectionError("[Errno 13] Permission denied")

        requests.get = fake_get
        try:
            with self.assertRaises(RuntimeError) as cm:
                cli.get_session()
        finally:
            requests.get = orig_get
        self.assertNotIsInstance(cm.exception, cli.AuthExpired)
        self.assertEqual(str(cm.exception), cli._SANDBOX_HINT)

    def test_retry_adapter_is_mounted(self):
        cli._load_cache = lambda: {cli.SESSION_COOKIE: "v"}
        cli._validate_cookies = lambda c, use_cache=True: True
        s = cli.get_session()
        adapter = s.get_adapter("https://www.overleaf.com")
        self.assertEqual(adapter.max_retries.total, 3)
        for code in (502, 503, 504):
            self.assertIn(code, adapter.max_retries.status_forcelist)
        # GET-only is the load-bearing safety property: a transient 5xx must
        # never replay a non-idempotent request if one is ever added.
        self.assertEqual(adapter.max_retries.allowed_methods, frozenset({"GET"}))


# ---------------------------------------------------------------------------
# find_linked_folder: parent walk, shared-level skip, auto-link dispatch
# ---------------------------------------------------------------------------
class FindLinkedFolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self._orig_autolink = cli._autolink_resolve

    def tearDown(self):
        cli._autolink_resolve = self._orig_autolink
        self._tmp.cleanup()

    def _marker(self, folder, pid):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / cli.PROJECT_MARKER).write_text(json.dumps({"project_id": pid}))

    def test_parent_walk_finds_marker_above(self):
        proj = self.root / "MyProj"
        self._marker(proj, "c" * 24)
        nested = proj / "chapters" / "deep"
        nested.mkdir(parents=True)
        f = nested / "intro.tex"
        f.write_text("x")
        linked, pid = cli.find_linked_folder(f, verbose=False)
        self.assertEqual(linked.resolve(), proj.resolve())  # .resolve() for /tmp symlink on macOS
        self.assertEqual(pid, "c" * 24)

    def test_shared_level_marker_is_skipped_then_autolinks(self):
        # A marker at .../Apps/Overleaf/ must NOT shadow every project: the walk
        # must fall through to per-project auto-link.
        shared = self.root / "Apps" / "Overleaf"
        self._marker(shared, "shadowshadowshadowshadow")
        projfile = shared / "MyProj" / "main.tex"
        projfile.parent.mkdir(parents=True)
        projfile.write_text("x")
        seen = {}

        def fake_autolink(name, folder, **kw):
            seen["name"] = name
            return "sentinelpidsentinelpid00"

        cli._autolink_resolve = fake_autolink
        linked, pid = cli.find_linked_folder(projfile, verbose=False)
        self.assertEqual(pid, "sentinelpidsentinelpid00")  # NOT the shadow marker
        self.assertEqual(seen["name"], "MyProj")

    def test_apps_overleaf_triggers_autolink(self):
        projfile = self.root / "Apps" / "Overleaf" / "Thesis" / "sub" / "f.tex"
        projfile.parent.mkdir(parents=True)
        projfile.write_text("x")
        seen = {}

        def fake_autolink(name, folder, **kw):
            seen["name"] = name
            return "p" * 24

        cli._autolink_resolve = fake_autolink
        linked, pid = cli.find_linked_folder(projfile, verbose=False)
        self.assertEqual(seen["name"], "Thesis")
        self.assertEqual(pid, "p" * 24)
        self.assertEqual(
            linked.resolve(), (self.root / "Apps" / "Overleaf" / "Thesis").resolve()
        )


# ---------------------------------------------------------------------------
# cmd_save_cookie: value normalization
# ---------------------------------------------------------------------------
class SaveCookieNormalizationTests(unittest.TestCase):
    def setUp(self):
        self._orig = {"_validate_cookies": cli._validate_cookies, "_save_cache": cli._save_cache}
        self.saved = {}
        cli._validate_cookies = lambda c, use_cache=True: True
        cli._save_cache = lambda c: self.saved.update(c)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cli, k, v)

    def test_strips_surrounding_quotes(self):
        cli.cmd_save_cookie(['"abc123"'])
        self.assertEqual(self.saved[cli.SESSION_COOKIE], "abc123")

    def test_name_equals_value_recovers_value(self):
        cli.cmd_save_cookie(["overleaf_session2=longvalue"])
        self.assertEqual(self.saved[cli.SESSION_COOKIE], "longvalue")

    def test_bare_name_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_save_cookie(["overleaf_session2"])
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.saved, {})

    def test_no_args_usage(self):
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_save_cookie([])
        self.assertEqual(cm.exception.code, 2)


# ---------------------------------------------------------------------------
# data dir hardened to 0o700 on POSIX
# ---------------------------------------------------------------------------
@unittest.skipIf(os.name != "posix", "POSIX-only directory permission test")
class DataDirHardeningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = cli.CACHE_DIR
        cli.CACHE_DIR = pathlib.Path(self._tmp.name) / "data"

    def tearDown(self):
        cli.CACHE_DIR = self._orig
        self._tmp.cleanup()

    def test_writing_under_data_dir_hardens_it(self):
        cli._atomic_write_text(cli.CACHE_DIR / "cookies.json", "{}")
        mode = cli.CACHE_DIR.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700, oct(mode))


# ---------------------------------------------------------------------------
# version single-source: no drift across __init__ / pyproject / CITATION
# ---------------------------------------------------------------------------
class VersionConsistencyTests(unittest.TestCase):
    def test_init_matches_citation_and_pyproject(self):
        v = overleaf_sync_now.__version__
        cff = (ROOT / "CITATION.cff").read_text()
        m = re.search(r'^version:\s*"([^"]+)"', cff, re.M)
        self.assertIsNotNone(m, "CITATION.cff has no version line")
        self.assertEqual(m.group(1), v, "CITATION.cff version drifted from __init__")

        pyproject = (ROOT / "pyproject.toml").read_text()
        if re.search(r'dynamic\s*=\s*\[[^\]]*["\']version["\']', pyproject):
            # Dynamic: pyproject derives the version from __init__, so it cannot
            # drift — just assert the wiring is present and points at us.
            self.assertRegex(pyproject, r"\[tool\.setuptools\.dynamic\]")
            self.assertRegex(
                pyproject,
                r'version\s*=\s*\{\s*attr\s*=\s*["\']overleaf_sync_now\.__version__["\']',
            )
        else:
            m2 = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
            self.assertIsNotNone(m2, "pyproject has neither dynamic nor literal version")
            self.assertEqual(m2.group(1), v, "pyproject version drifted from __init__")


# ---------------------------------------------------------------------------
# install/uninstall: ~/.claude/settings.json hook surgery (corruption-critical)
# ---------------------------------------------------------------------------
class IsOurHookTests(unittest.TestCase):
    def test_matches_our_forms(self):
        self.assertTrue(cli._is_our_hook("overleaf-sync-now hook"))
        self.assertTrue(cli._is_our_hook('"/opt/bin/overleaf-sync-now" hook'))
        self.assertTrue(cli._is_our_hook("python /x/overleaf_sync.py hook"))  # legacy form
        self.assertTrue(cli._is_our_hook("overleaf-sync-now hook --quiet"))   # trailing flags

    def test_rejects_other_commands(self):
        self.assertFalse(cli._is_our_hook("echo hello"))
        self.assertFalse(cli._is_our_hook(""))
        self.assertFalse(cli._is_our_hook("overleaf-sync-now sync"))  # not the hook subcommand


class HookInstallTests(unittest.TestCase):
    """cmd_install/cmd_uninstall read-modify-write the user's real
    ~/.claude/settings.json; a regression there breaks Claude Code startup.
    These pin the dedupe/idempotency loop and the user-hook preservation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = pathlib.Path(self._tmp.name) / "settings.json"
        self._orig = {
            "CLAUDE_SETTINGS": cli.CLAUDE_SETTINGS,
            "SKILL_TARGETS": cli.SKILL_TARGETS,
            "cmd_setup": cli.cmd_setup,
            "_validate_cookies": cli._validate_cookies,
            "_load_cache": cli._load_cache,
        }
        cli.CLAUDE_SETTINGS = self.settings
        cli.SKILL_TARGETS = {}                              # skip the SKILL.md copy loop
        cli.cmd_setup = lambda *a, **k: None                # skip the auth wizard
        cli._validate_cookies = lambda *a, **k: True        # skip the final auth probe/network
        cli._load_cache = lambda: {cli.SESSION_COOKIE: "x"}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def _entries(self):
        return json.loads(self.settings.read_text())["hooks"]["PreToolUse"]

    def _commands(self):
        return [h.get("command", "") for e in self._entries() for h in e.get("hooks", [])]

    def _our_count(self):
        return sum(1 for c in self._commands() if cli._is_our_hook(c))

    def test_install_adds_one_hook_and_preserves_user_hooks(self):
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
        ]}}))
        cli.cmd_install(["--no-interactive"])
        self.assertEqual(self._our_count(), 1)
        self.assertIn("echo hi", self._commands())  # unrelated user hook untouched

    def test_install_is_idempotent(self):
        cli.cmd_install(["--no-interactive"])
        cli.cmd_install(["--no-interactive"])
        cli.cmd_install(["--no-interactive"])
        self.assertEqual(self._our_count(), 1)  # no duplicate hooks accumulate

    def test_uninstall_removes_only_our_hook(self):
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
        ]}}))
        cli.cmd_install(["--no-interactive"])
        self.assertEqual(self._our_count(), 1)
        cli.cmd_uninstall([])
        self.assertEqual(self._our_count(), 0)
        self.assertIn("echo hi", self._commands())  # user hook survives uninstall

    def test_broken_settings_is_backed_up_not_overwritten(self):
        self.settings.write_text("{not valid json")
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_install(["--no-interactive"])
        self.assertEqual(cm.exception.code, 1)
        backups = list(pathlib.Path(self._tmp.name).glob("settings.json.broken-*"))
        self.assertTrue(backups, "broken settings.json should be backed up before bailing")
        # original broken content preserved (not silently overwritten)
        self.assertEqual(self.settings.read_text(), "{not valid json")


if __name__ == "__main__":
    unittest.main()
