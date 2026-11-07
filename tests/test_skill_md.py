"""SKILL.md stays loadable and small (0.5.0).

A dependency-free stand-in for the skill validators: the frontmatter keys,
the name, the description limits both clients enforce (non-empty, at most
1,024 characters, no angle brackets), and a size budget so the skill stays
well inside the post-compaction re-attach budget (about 5k tokens).

`argument-hint` and `user-invocable` are Claude Code keys the owner chose to
keep; skills-ref reports them as unexpected, which is why this test exists
instead of a skills-ref CI job.

Run with: python -m unittest discover tests
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "src" / "overleaf_sync_now" / "SKILL.md"
ALLOWED_KEYS = {"name", "description", "argument-hint", "user-invocable", "license", "metadata"}


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with a --- frontmatter block"
    fields = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        fields[key.strip()] = value
    return fields, text[m.end():]


class SkillMdTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")
        self.fields, self.body = frontmatter(self.text)

    def test_keys_and_name(self):
        self.assertLessEqual(set(self.fields), ALLOWED_KEYS)
        self.assertEqual(self.fields.get("name"), "overleaf")
        self.assertRegex(self.fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_description_limits(self):
        desc = self.fields.get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertNotIn("<", desc)
        self.assertNotIn(">", desc)

    def test_size_budget(self):
        # About 2.3k tokens today; fail well before the ~5k re-attach budget.
        self.assertLess(len(self.text.encode("utf-8")), 14000)

    def test_ownership_marker_for_install(self):
        # install/uninstall only replace a skill folder whose SKILL.md says this.
        self.assertIn("overleaf-sync-now", self.body)


if __name__ == "__main__":
    unittest.main()
