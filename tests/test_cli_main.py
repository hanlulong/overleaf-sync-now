"""Argument handling in main(): per-subcommand -h/--help and unknown options.

Before 0.5.1 only argv[1] was checked for help and the subcommands dropped
every '--' token, so `uninstall --help` removed the hooks and
`sync --dry-run` did a real pull.

Run with: python -m unittest discover tests
"""
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402


class MainArgTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def sentinel(name):
            def run(args):
                self.calls.append((name, list(args)))
            return run

        self.sentinels = {name: sentinel(name) for name in cli.COMMANDS}

    def _main(self, *argv):
        """Run main() with argv; return (exit code or None, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.dict(cli.COMMANDS, self.sentinels), \
                mock.patch.object(sys, "argv", ["overleaf-sync-now", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_help_flags_print_usage_and_run_nothing(self):
        for name in cli.COMMANDS:
            if name == "hook":
                continue
            for flag in ("--help", "-h"):
                with self.subTest(cmd=name, flag=flag):
                    self.calls.clear()
                    code, out, _ = self._main(name, flag)
                    self.assertEqual(code, 0)
                    self.assertEqual(self.calls, [])
                    self.assertIn(name, out)
                    self.assertIn("usage: overleaf-sync-now " + name, out)

    def test_help_after_other_args_still_wins(self):
        code, out, _ = self._main("uninstall", "--remove-markers", "some/dir", "--help")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("--remove-markers", out)

    def test_unknown_options_exit_2_and_run_nothing(self):
        cases = [
            ("sync", "--dry-run"),
            ("sync", "--forse"),
            ("uninstall", "--remove-marker"),
            ("status", "--verbose"),
            ("sync", "--no-wait"),
            ("sync", "--legacy"),
            ("link", "abc", "-x"),
            ("sync", "-"),
            ("doctor", "--"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.calls.clear()
                code, out, err = self._main(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(self.calls, [])
                bad = [a for a in argv[1:] if a.startswith("-")][0]
                self.assertIn(f"unknown option {bad!r}", err)
                self.assertIn("usage: overleaf-sync-now " + argv[0], err)

    def test_known_options_dispatch_unchanged(self):
        cases = [
            ("status", "--quick"),
            ("sync", "--force", "--wait"),
            ("projects", "--refresh"),
            ("uninstall", "--remove-markers", "some/dir"),
            ("save-cookie", "-"),
            ("sync", "./-odd-name"),
            ("link", "a" * 24, "some/dir"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.calls.clear()
                code, _, _ = self._main(*argv)
                self.assertIsNone(code)
                self.assertEqual(self.calls, [(argv[0], list(argv[1:]))])

    def test_hook_ignores_its_arguments(self):
        for extra in ([], ["--help"], ["--anything"], ["--quiet"], ["-h"]):
            with self.subTest(extra=extra):
                self.calls.clear()
                code, _, _ = self._main("hook", *extra)
                self.assertIsNone(code)
                self.assertEqual(self.calls, [("hook", extra)])

    def test_bare_help_is_a_positional_for_subcommands(self):
        # A folder can be named `help`; only -h/--help ask for help.
        code, _, _ = self._main("sync", "help")
        self.assertIsNone(code)
        self.assertEqual(self.calls, [("sync", ["help"])])

    def test_top_level_help_unchanged(self):
        for flag in ("-h", "--help", "help"):
            with self.subTest(flag=flag):
                code, out, _ = self._main(flag)
                self.assertEqual(code, 0)
                self.assertIn("Subcommands:", out)
                self.assertEqual(self.calls, [])

    def test_unknown_subcommand_still_exits_2(self):
        code, _, err = self._main("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("Unknown subcommand", err)


class UsageDocDriftTests(unittest.TestCase):
    def test_every_subcommand_has_a_usage_block(self):
        for name in cli.COMMANDS:
            with self.subTest(cmd=name):
                block = cli._subcommand_usage(name)
                self.assertNotEqual(block, cli.__doc__)
                self.assertEqual(block.splitlines()[0].split()[0], name)

    def test_usage_block_includes_continuation_lines_only(self):
        block = cli._subcommand_usage("sync")
        self.assertIn("--wait waits", block)
        self.assertNotIn("status [folder]", block)
        self.assertEqual(cli._subcommand_usage("no-such-command"), cli.__doc__)

    def test_allowed_flags_match_the_docstring(self):
        self.assertEqual(set(cli.ALLOWED_FLAGS), set(cli.COMMANDS) - {"hook"})
        for name in cli.COMMANDS:
            if name == "hook":
                continue
            with self.subTest(cmd=name):
                first = cli._subcommand_usage(name).splitlines()[0]
                documented = set(re.findall(r"\[(--[a-z][a-z-]*)\]", first))
                self.assertEqual(documented, cli.ALLOWED_FLAGS[name])


class UninstallHelpEndToEndTests(unittest.TestCase):
    """`uninstall --help` in a real process must leave the hook configs alone."""

    def test_uninstall_help_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            claude = home / ".claude"
            codex = home / ".codex"
            (claude / "skills" / "overleaf").mkdir(parents=True)
            (codex).mkdir()
            (claude / "skills" / "overleaf" / "SKILL.md").write_text("skill copy\n")
            settings = claude / "settings.json"
            hooks = codex / "hooks.json"
            settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
                "matcher": cli.CLAUDE_HOOK_MATCHER,
                "hooks": [{"type": "command", "command": "/opt/bin/overleaf-sync-now",
                           "args": ["hook"], "timeout": 180}],
            }]}}, indent=2))
            hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{
                "matcher": cli.CODEX_HOOK_MATCHER,
                "hooks": [{"type": "command", "command": "/opt/bin/overleaf-sync-now hook"}],
            }]}}, indent=2))
            before = (settings.read_bytes(), hooks.read_bytes())
            env = dict(os.environ)
            env.update({
                "HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex),
                "OVERLEAF_SYNC_DATA_DIR": str(home / "data"),
                "PYTHONPATH": str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", ""),
            })
            code = ("import sys; from overleaf_sync_now import cli; "
                    "sys.argv = ['overleaf-sync-now', 'uninstall', '--help']; cli.main()")
            proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp,
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage: overleaf-sync-now uninstall", proc.stdout)
            self.assertNotIn("removed", proc.stdout)
            self.assertEqual((settings.read_bytes(), hooks.read_bytes()), before)
            self.assertTrue((claude / "skills" / "overleaf" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
