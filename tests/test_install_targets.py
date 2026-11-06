"""install / uninstall against a fake HOME (0.5.0).

Pins:
  * the skill goes to ~/.claude/skills/overleaf and ~/.agents/skills/overleaf
    (the Codex / Agent Skills user location); an old copy of ours in the
    older ~/.codex/skills/overleaf location is removed so Codex lists it once.
  * nothing that is not ours is ever deleted (another skill named `overleaf`).
  * the Claude Code hook is exec form (`command` + `args`), matcher
    Read|Edit|Write|Bash|PowerShell, explicit timeout; an old v0.0.x entry
    (matcher Edit|Write|MultiEdit, timeout 8, shell form) is replaced.
  * ~/.codex/hooks.json gets one idempotent PreToolUse entry (apply_patch|Bash,
    command + commandWindows), the previous file is backed up when it
    changes, and uninstall removes only our entry.

Run with: python -m unittest discover tests
"""
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402

FAKE_BIN = "/opt/fake bin/overleaf-sync-now"


class InstallTargetsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        for d in (".claude", ".codex", ".agents"):
            (self.home / d).mkdir()
        self._orig = {name: getattr(cli, name) for name in (
            "SKILL_TARGETS", "LEGACY_SKILL_TARGETS", "CLAUDE_SETTINGS", "CODEX_HOME",
            "CODEX_HOOKS", "cmd_setup", "_check_cookies", "_load_cache", "_cli_path",
        )}
        cli.SKILL_TARGETS = {
            "Claude Code": self.home / ".claude" / "skills" / "overleaf",
            "Codex CLI": self.home / ".agents" / "skills" / "overleaf",
        }
        cli.LEGACY_SKILL_TARGETS = {
            "Codex CLI (legacy ~/.codex/skills)": self.home / ".codex" / "skills" / "overleaf",
        }
        cli.CLAUDE_SETTINGS = self.home / ".claude" / "settings.json"
        cli.CODEX_HOME = self.home / ".codex"
        cli.CODEX_HOOKS = cli.CODEX_HOME / "hooks.json"
        cli.cmd_setup = lambda *a, **k: None
        cli._check_cookies = lambda *a, **k: ("ok", None)
        cli._load_cache = lambda: {cli.SESSION_COOKIE: "x"}
        cli._cli_path = lambda: FAKE_BIN
        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout = self.out = io.StringIO()
        sys.stderr = self.err = io.StringIO()

    def tearDown(self):
        sys.stdout, sys.stderr = self._stdout, self._stderr
        for k, v in self._orig.items():
            setattr(cli, k, v)
        self._tmp.cleanup()

    def install(self):
        cli.cmd_install(["--no-interactive"])

    def claude_handlers(self):
        data = json.loads(cli.CLAUDE_SETTINGS.read_text())
        return [(e.get("matcher"), h) for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]

    def codex_groups(self):
        return json.loads(cli.CODEX_HOOKS.read_text())["hooks"]["PreToolUse"]

    # ---- skill copies ----
    def test_skill_goes_to_claude_and_agents(self):
        self.install()
        for t in cli.SKILL_TARGETS.values():
            self.assertEqual((t / "SKILL.md").read_bytes(), cli.SKILL_MD_SRC.read_bytes())

    def test_agents_dir_created_when_only_codex_exists(self):
        (self.home / ".agents").rmdir()
        self.install()
        self.assertTrue((self.home / ".agents" / "skills" / "overleaf" / "SKILL.md").exists())

    def test_legacy_codex_copy_of_ours_is_removed(self):
        legacy = self.home / ".codex" / "skills" / "overleaf"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("---\nname: overleaf\n---\nRun overleaf-sync-now sync.\n")
        self.install()
        self.assertFalse(legacy.exists())

    def test_foreign_skill_is_never_deleted(self):
        foreign = self.home / ".claude" / "skills" / "overleaf"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("---\nname: overleaf\n---\nUse olcli to sync.\n")
        (foreign / "helper.js").write_text("// theirs")
        legacy = self.home / ".codex" / "skills" / "overleaf"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("someone else's overleaf skill")
        self.install()
        self.assertIn("olcli", (foreign / "SKILL.md").read_text())
        self.assertTrue((foreign / "helper.js").exists())
        self.assertTrue(legacy.exists())
        self.assertIn("different skill", self.err.getvalue())
        cli.cmd_uninstall([])
        self.assertTrue((foreign / "helper.js").exists())
        self.assertTrue(legacy.exists())
        self.assertFalse((self.home / ".agents" / "skills" / "overleaf").exists())

    # ---- Claude Code hook ----
    def test_claude_hook_is_exec_form_with_timeout(self):
        self.install()
        ours = [(m, h) for m, h in self.claude_handlers() if cli._is_our_hook(h)]
        self.assertEqual(len(ours), 1)
        matcher, h = ours[0]
        self.assertEqual(matcher, "Read|Edit|Write|Bash|PowerShell")
        self.assertEqual(h["command"], FAKE_BIN)
        self.assertEqual(h["args"], ["hook"])
        self.assertEqual(h["timeout"], cli.HOOK_TIMEOUT_SECONDS)
        self.assertNotIn("MultiEdit", matcher)

    def test_old_v00_entry_is_replaced(self):
        cli.CLAUDE_SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Edit|Write|MultiEdit",
             "hooks": [{"type": "command", "command": '"/old/overleaf-sync-now" hook', "timeout": 8}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]},
        ]}}))
        self.install()
        handlers = self.claude_handlers()
        ours = [h for _, h in handlers if cli._is_our_hook(h)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["timeout"], cli.HOOK_TIMEOUT_SECONDS)  # 8 s not kept
        self.assertIn("echo mine", [h.get("command") for _, h in handlers])

    def test_user_longer_timeout_and_onfailure_kept(self):
        cli.CLAUDE_SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Read|Edit|Write", "hooks": [{
                "type": "command", "command": FAKE_BIN, "args": ["hook"],
                "timeout": 400, "onFailure": "block"}]},
        ]}}))
        self.install()
        ours = [h for _, h in self.claude_handlers() if cli._is_our_hook(h)]
        self.assertEqual((ours[0]["timeout"], ours[0]["onFailure"]), (400, "block"))

    def test_is_our_hook_exec_form(self):
        self.assertTrue(cli._is_our_hook({"command": FAKE_BIN, "args": ["hook"]}))
        self.assertTrue(cli._is_our_hook("overleaf-sync-now", ["hook"]))
        self.assertFalse(cli._is_our_hook({"command": FAKE_BIN, "args": ["sync"]}))
        self.assertFalse(cli._is_our_hook({"command": "node", "args": ["hook.js"]}))

    # ---- Codex hook ----
    def test_codex_hook_written(self):
        self.install()
        groups = self.codex_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["matcher"], "apply_patch|Bash")
        h = groups[0]["hooks"][0]
        self.assertEqual(h["type"], "command")
        self.assertEqual(h["command"], "'/opt/fake bin/overleaf-sync-now' hook")
        self.assertIn("commandWindows", h)
        self.assertTrue(cli._is_our_hook(h["commandWindows"]))
        self.assertGreaterEqual(h["timeout"], 120)
        self.assertIn("/hooks", self.out.getvalue())  # trust instruction printed

    def test_codex_hook_idempotent_and_backed_up_once(self):
        cli.CODEX_HOOKS.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Read|Edit|Write|MultiEdit",
             "hooks": [{"type": "command", "command": "overleaf-sync-now hook"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.codex/check.py"}]},
        ], "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))
        self.install()
        self.install()
        self.install()
        groups = self.codex_groups()
        ours = [h for g in groups for h in g["hooks"] if cli._is_our_hook(h)]
        self.assertEqual(len(ours), 1)
        self.assertIn("python3 ~/.codex/check.py", [h["command"] for g in groups for h in g["hooks"]])
        data = json.loads(cli.CODEX_HOOKS.read_text())
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "say done")
        backups = list(cli.CODEX_HOME.glob("hooks.json.bak-*"))
        self.assertEqual(len(backups), 1)  # only the first (changing) install backs up
        self.assertIn("MultiEdit", backups[0].read_text())

    def test_codex_skipped_without_codex_home(self):
        import shutil
        shutil.rmtree(self.home / ".codex")
        cli.CODEX_HOME = self.home / ".codex"
        self.install()
        self.assertFalse(cli.CODEX_HOOKS.exists())

    def test_broken_codex_hooks_json_left_alone(self):
        cli.CODEX_HOOKS.write_text("{broken")
        self.install()
        self.assertEqual(cli.CODEX_HOOKS.read_text(), "{broken")
        self.assertTrue(list(cli.CODEX_HOME.glob("hooks.json.broken-*")))
        # Claude side still installed.
        self.assertTrue(any(cli._is_our_hook(h) for _, h in self.claude_handlers()))

    # ---- uninstall ----
    def test_uninstall_removes_everything_of_ours_only(self):
        cli.CODEX_HOOKS.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 check.py"}]},
        ]}}))
        cli.CLAUDE_SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]},
        ]}}))
        self.install()
        legacy = self.home / ".codex" / "skills" / "overleaf"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("overleaf-sync-now old copy")
        cli.cmd_uninstall([])
        for t in list(cli.SKILL_TARGETS.values()) + [legacy]:
            self.assertFalse(t.exists(), t)
        self.assertFalse(any(cli._is_our_hook(h) for _, h in self.claude_handlers()))
        groups = self.codex_groups()
        self.assertEqual([h["command"] for g in groups for h in g["hooks"]], ["python3 check.py"])


    # ---- CLI path resolution (the hook must name an absolute path) ----
    def test_cli_found_from_entry_point_when_not_on_path(self):
        import os
        import shutil
        import stat
        from unittest import mock
        cli._cli_path = self._orig["_cli_path"]
        bindir = self.home / "uv-bin"
        bindir.mkdir()
        exe = bindir / "overleaf-sync-now"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        with mock.patch.object(shutil, "which", lambda name: None), \
                mock.patch.object(sys, "argv", [str(exe), "install"]), \
                mock.patch.object(sys, "executable", str(self.home / "nopy" / "python")), \
                mock.patch.object(cli.subprocess, "run", side_effect=OSError("no uv")):
            self.install()
        (matcher, handler), = self.claude_handlers()
        self.assertEqual(handler["command"], os.path.abspath(str(exe)))
        self.assertIn("is not on your PATH", self.out.getvalue())
        self.assertIn("uv tool update-shell", self.out.getvalue())

    def test_install_fails_loudly_without_a_cli_path(self):
        cli._cli_path = lambda: None
        with self.assertRaises(SystemExit) as cm:
            self.install()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("could not find the overleaf-sync-now executable", self.err.getvalue())
        self.assertFalse(cli.CLAUDE_SETTINGS.exists())
        self.assertFalse(cli.CODEX_HOOKS.exists())

    def test_install_refuses_a_uvx_cache_path(self):
        cli._cli_path = lambda: str(self.home / ".cache" / "uv" / "archive-v0" / "x" / "bin" / "overleaf-sync-now")
        with self.assertRaises(SystemExit) as cm:
            self.install()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("uv's cache", self.err.getvalue())
        self.assertFalse(cli.CLAUDE_SETTINGS.exists())

    # ---- CLAUDE_CONFIG_DIR / CODEX_HOME, and "nothing installed" ----
    def test_config_dirs_follow_environment(self):
        import os
        import subprocess
        env = dict(os.environ, HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.home / "cfg-claude"),
                   CODEX_HOME=str(self.home / "cfg-codex"), PYTHONPATH=str(ROOT / "src"))
        out = subprocess.run([sys.executable, "-c", (
            "from overleaf_sync_now import cli;"
            "print(cli.CLAUDE_SETTINGS); print(cli.SKILL_TARGETS['Claude Code']);"
            "print(cli.CODEX_HOOKS); print(cli.LEGACY_SKILL_TARGETS[next(iter(cli.LEGACY_SKILL_TARGETS))])")],
            env=env, capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual(out[0], str(self.home / "cfg-claude" / "settings.json"))
        self.assertEqual(out[1], str(self.home / "cfg-claude" / "skills" / "overleaf"))
        self.assertEqual(out[2], str(self.home / "cfg-codex" / "hooks.json"))
        self.assertEqual(out[3], str(self.home / "cfg-codex" / "skills" / "overleaf"))

    def test_claude_config_dir_from_env_counts_as_present(self):
        import os
        from unittest import mock
        cfg = self.home / "cfg-claude"
        cli.CLAUDE_SETTINGS = cfg / "settings.json"
        cli.SKILL_TARGETS["Claude Code"] = cfg / "skills" / "overleaf"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cfg)}):
            self.install()
        self.assertTrue(any(cli._is_our_hook(h) for _, h in self.claude_handlers()))
        self.assertTrue((cfg / "skills" / "overleaf" / "SKILL.md").exists())

    def test_nothing_installed_exits_nonzero(self):
        import shutil
        for d in (".claude", ".codex", ".agents"):
            shutil.rmtree(self.home / d)
        with self.assertRaises(SystemExit) as cm:
            self.install()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("nothing was installed", self.err.getvalue())
        self.assertNotIn("Install complete", self.out.getvalue())

    # ---- settings.json: symlinks, non-ASCII, backup, mode ----
    def test_symlinked_settings_written_through_with_backup(self):
        import os
        dotfiles = self.home / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "claude-settings.json"
        original = '{"statusLine": {"type": "command", "command": "echo \u8bba\u6587 \u2713"}}'
        real.write_text(original, encoding="utf-8")
        os.chmod(real, 0o600)
        try:
            cli.CLAUDE_SETTINGS.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.install()
        self.assertTrue(cli.CLAUDE_SETTINGS.is_symlink())
        text = real.read_text(encoding="utf-8")
        self.assertIn("echo \u8bba\u6587 \u2713", text)
        self.assertNotIn("\\u8bba", text)
        self.assertTrue(any(cli._is_our_hook(h) for _, h in self.claude_handlers()))
        backup = dotfiles / ("claude-settings.json" + cli.SETTINGS_BACKUP_SUFFIX)
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        if os.name == "posix":
            self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        # The one-time backup is not replaced on the next install.
        self.install()
        self.assertEqual(backup.read_text(encoding="utf-8"), original)


    def test_uninstall_lists_markers_data_dir_and_session(self):
        from unittest import mock
        proj = self.home / "Dropbox" / "Apps" / "Overleaf" / "My JMP"
        proj.mkdir(parents=True)
        marker = proj / cli.PROJECT_MARKER
        marker.write_text(json.dumps({"project_id": "0123456789abcdef01234567"}))
        data = self.home / "data"
        data.mkdir()
        self.install()
        with mock.patch.object(cli, "HOME", self.home), mock.patch.object(cli, "CACHE_DIR", data):
            cli.cmd_uninstall([])
            out = self.out.getvalue()
            self.assertIn(str(marker), out)
            self.assertIn("--remove-markers", out)
            self.assertIn(str(data), out)
            self.assertIn("Clear Sessions", out)
            self.assertTrue(marker.exists())
            cli.cmd_uninstall(["--remove-markers"])
        self.assertFalse(marker.exists())

    # ---- install health (status / doctor) ----
    def test_health_ok_after_install(self):
        self.install()
        self.assertEqual(cli._install_health(), [])

    def test_health_flags_stale_v00_install(self):
        # The owner's real state before 0.5.0: old skill copy, Edit|Write|
        # MultiEdit matcher without Read, timeout 8, shell form, no Codex hook,
        # duplicate ~/.codex/skills copy.
        for t in list(cli.SKILL_TARGETS.values()) + list(cli.LEGACY_SKILL_TARGETS.values()):
            t.mkdir(parents=True)
            (t / "SKILL.md").write_text("old overleaf-sync-now skill")
        cli.CLAUDE_SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Edit|Write|MultiEdit",
             "hooks": [{"type": "command", "command": '"/x/overleaf-sync-now" hook', "timeout": 8}]},
        ]}}))
        problems = "\n".join(cli._install_health())
        for needle in ("stale copy", "old copy", "misses Read", "timeout 8", "shell form",
                       "no overleaf-sync-now PreToolUse hook in"):
            self.assertIn(needle, problems)
        self.install()
        self.assertEqual(cli._install_health(), [])


if __name__ == "__main__":
    unittest.main()
