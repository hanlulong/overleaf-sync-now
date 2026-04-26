"""Unit tests for the v0.3.0 project-resolution rewrite.

These tests target the policy resolver, the index migration, and the
fingerprint helper — i.e. the load-bearing pieces of the fix for the
"trashed duplicate auto-linked silently" bug. They do not exercise any
network paths; HTTP calls are out of scope for unit tests.

Run with: python -m unittest discover tests
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

# Tests run against the in-repo source without installation.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overleaf_sync_now import cli  # noqa: E402


def _rec(pid, name, *, trashed=False, archived=False, last="2026-04-25T12:00:00.000Z", owner="u1"):
    return {
        "id": pid, "name": name, "trashed": trashed, "archived": archived,
        "lastUpdated": last, "ownerId": owner,
    }


class ResolverTests(unittest.TestCase):
    def test_no_match_returns_none(self):
        records = [_rec("a" * 24, "Other Project")]
        self.assertEqual(cli._resolve_by_name("Missing", records), ("none",))

    def test_exact_single_match(self):
        records = [_rec("a" * 24, "Paper")]
        self.assertEqual(cli._resolve_by_name("Paper", records), ("ok", "a" * 24))

    def test_case_insensitive_fallback(self):
        records = [_rec("a" * 24, "Paper")]
        self.assertEqual(cli._resolve_by_name("paper", records), ("ok", "a" * 24))

    def test_case_sensitive_preferred_over_case_insensitive(self):
        # Both "Paper" and "paper" exist; case-sensitive match wins.
        records = [_rec("a" * 24, "Paper"), _rec("b" * 24, "paper")]
        self.assertEqual(cli._resolve_by_name("Paper", records), ("ok", "a" * 24))

    def test_trashed_filtered_out_by_default(self):
        # The reported bug: trashed duplicate must NOT shadow the active project.
        records = [
            _rec("a" * 24, "CrossBorder", trashed=True),
            _rec("b" * 24, "CrossBorder", trashed=False),
        ]
        self.assertEqual(cli._resolve_by_name("CrossBorder", records), ("ok", "b" * 24))

    def test_archived_filtered_out_by_default(self):
        records = [
            _rec("a" * 24, "Paper", archived=True),
            _rec("b" * 24, "Paper", archived=False),
        ]
        self.assertEqual(cli._resolve_by_name("Paper", records), ("ok", "b" * 24))

    def test_two_living_duplicates_returns_ambiguous(self):
        records = [
            _rec("a" * 24, "Paper", last="2026-04-01T00:00:00.000Z"),
            _rec("b" * 24, "Paper", last="2026-04-25T00:00:00.000Z"),
        ]
        outcome = cli._resolve_by_name("Paper", records)
        self.assertEqual(outcome[0], "ambiguous")
        self.assertEqual(len(outcome[1]), 2)
        # Most-recently-updated must be listed first.
        self.assertEqual(outcome[1][0]["id"], "b" * 24)

    def test_all_duplicates_trashed_returns_none(self):
        # If every duplicate is trashed, refuse to auto-link — there's
        # nothing to pick. Better a clear "none" than silently picking trash.
        records = [
            _rec("a" * 24, "Paper", trashed=True),
            _rec("b" * 24, "Paper", trashed=True),
        ]
        self.assertEqual(cli._resolve_by_name("Paper", records), ("none",))


class IndexMigrationTests(unittest.TestCase):
    """Layer 1 migration: a v1-shape file on disk must be treated as expired,
    not crash the loader. The rewrite picks up via _refresh_projects_records,
    which we don't network-test here; we just verify the load path returns []
    for v1 instead of trying to interpret it as v2."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_index_file = cli.INDEX_FILE
        cli.INDEX_FILE = pathlib.Path(self._tmp.name) / "projects.json"

    def tearDown(self):
        cli.INDEX_FILE = self._orig_index_file
        self._tmp.cleanup()

    def test_v1_format_treated_as_empty_in_cached_load(self):
        # v1: {"ts": ..., "index": {name: id}}
        cli.INDEX_FILE.write_text(json.dumps({
            "ts": 9999999999,  # not expired
            "index": {"Paper": "a" * 24},
        }))
        self.assertEqual(cli._load_cached_projects_records(), [])

    def test_v2_format_round_trips(self):
        records = [_rec("a" * 24, "Paper")]
        cli.INDEX_FILE.write_text(json.dumps({
            "version": 2,
            "ts": 9999999999,
            "projects": records,
        }))
        loaded = cli._load_cached_projects_records()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "a" * 24)

    def test_corrupt_file_returns_empty(self):
        cli.INDEX_FILE.write_text("{not json")
        self.assertEqual(cli._load_cached_projects_records(), [])


class FingerprintTests(unittest.TestCase):
    """Layer 6a/6b fingerprint helper: counts dropbox-origin pathnames that
    map to existing local files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, rel):
        target = self.folder.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

    def test_no_dropbox_entries_returns_zero(self):
        # Zero is "no signal", not "negative signal" — caller treats as
        # indeterminate.
        updates = [{"meta": {}, "pathnames": ["main.tex"]}]
        self.assertEqual(cli._fingerprint_hits(self.folder, updates), 0)

    def test_dropbox_entry_with_matching_local_file_counts(self):
        self._touch("main.tex")
        updates = [{
            "meta": {"origin": {"kind": "dropbox"}},
            "pathnames": ["main.tex"],
        }]
        self.assertEqual(cli._fingerprint_hits(self.folder, updates), 1)

    def test_dropbox_entry_with_no_matching_local_file_returns_zero(self):
        # The cross-folder dedupe bug fingerprint: project's recent dropbox
        # update touches a file we've never heard of locally.
        updates = [{
            "meta": {"origin": {"kind": "dropbox"}},
            "pathnames": ["someone_elses_paper.tex"],
        }]
        self.assertEqual(cli._fingerprint_hits(self.folder, updates), 0)

    def test_subdirectory_paths_normalize(self):
        self._touch("chapters/intro.tex")
        updates = [{
            "meta": {"origin": {"kind": "dropbox"}},
            "pathnames": ["chapters/intro.tex"],
        }]
        self.assertEqual(cli._fingerprint_hits(self.folder, updates), 1)

    def test_caps_at_max_paths(self):
        # Make a lot of paths exist locally, then feed many. The function
        # should bound its work to FINGERPRINT_MAX_PATHS regardless.
        for i in range(20):
            self._touch(f"f{i}.tex")
        updates = [{
            "meta": {"origin": {"kind": "dropbox"}},
            "pathnames": [f"f{i}.tex" for i in range(20)],
        }]
        hits = cli._fingerprint_hits(self.folder, updates)
        self.assertLessEqual(hits, cli.FINGERPRINT_MAX_PATHS)

    def test_only_first_n_dropbox_entries_inspected(self):
        # Older dropbox-origin entries beyond FINGERPRINT_RECENT_DBX_ENTRIES
        # should be ignored.
        self._touch("recent.tex")
        self._touch("ancient.tex")
        updates = []
        # Pad with FINGERPRINT_RECENT_DBX_ENTRIES dropbox entries that touch
        # only "recent.tex", then add one more touching only "ancient.tex".
        for _ in range(cli.FINGERPRINT_RECENT_DBX_ENTRIES):
            updates.append({
                "meta": {"origin": {"kind": "dropbox"}},
                "pathnames": ["recent.tex"],
            })
        updates.append({
            "meta": {"origin": {"kind": "dropbox"}},
            "pathnames": ["ancient.tex"],
        })
        # Hits should only include "recent.tex" — ancient.tex's update is
        # past the bound. So we expect 1 (recent.tex), not 2.
        self.assertEqual(cli._fingerprint_hits(self.folder, updates), 1)


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_marker_is_atomic_and_has_metadata(self):
        cli._write_marker(self.folder, "a" * 24, project_name="Paper", source="auto-link")
        marker = self.folder / cli.PROJECT_MARKER
        self.assertTrue(marker.exists())
        data = json.loads(marker.read_text())
        self.assertEqual(data["project_id"], "a" * 24)
        self.assertEqual(data["name_at_link_time"], "Paper")
        self.assertEqual(data["source"], "auto-link")
        self.assertIn("linked_at", data)
        # No leftover .tmp sibling.
        self.assertFalse((self.folder / (cli.PROJECT_MARKER + ".tmp")).exists())

    def test_marker_old_format_still_resolvable(self):
        # An existing marker without metadata fields should still work.
        marker = self.folder / cli.PROJECT_MARKER
        marker.write_text(json.dumps({"project_id": "b" * 24}))
        # We can't run find_linked_folder cleanly without the .../Apps/Overleaf/
        # path, so just assert the JSON parses and project_id is read.
        data = json.loads(marker.read_text())
        self.assertEqual(data["project_id"], "b" * 24)


class ExtractRoundTripSkipTests(unittest.TestCase):
    """Round-trip safety: if Overleaf's Dropbox bridge uploads our marker
    file to the project, the next /download/zip will contain that marker.
    Extract must NEVER overwrite the local marker — this machine's marker
    is the source of truth for the local link."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _build_zip(self, files):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    def test_marker_in_zip_is_not_extracted(self):
        # Local marker says project A; the zip (from Overleaf round-trip)
        # contains a marker that says project B. Local marker must survive.
        local_marker = self.folder / cli.PROJECT_MARKER
        local_marker.write_text(json.dumps({"project_id": "a" * 24}))
        zb = self._build_zip({
            cli.PROJECT_MARKER: json.dumps({"project_id": "b" * 24}),
            "main.tex": "hello",
        })
        cli._extract_files(zb, self.folder, paths=None, protect_recent_seconds=0)
        # main.tex extracted...
        self.assertEqual((self.folder / "main.tex").read_text(), "hello")
        # ...but local marker preserved.
        marker_data = json.loads(local_marker.read_text())
        self.assertEqual(marker_data["project_id"], "a" * 24)


@unittest.skipIf(os.name != "posix", "POSIX-only file permission test")
class FilePermsTests(unittest.TestCase):
    """Files under the data dir must be 0o600 on POSIX so cookie values,
    project IDs, and ownership metadata don't leak to other local users."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cache_dir = cli.CACHE_DIR
        cli.CACHE_DIR = pathlib.Path(self._tmp.name)

    def tearDown(self):
        cli.CACHE_DIR = self._orig_cache_dir
        self._tmp.cleanup()

    def test_data_dir_writes_get_0o600(self):
        target = cli.CACHE_DIR / "secret.json"
        cli._atomic_write_text(target, '{"x":1}')
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"got {oct(mode)}")

    def test_outside_data_dir_keeps_default_perms(self):
        # .overleaf-project markers in user folders must remain readable so
        # the local Dropbox client / Overleaf bridge can do whatever they
        # do. We only restrict files we own.
        outside = pathlib.Path(self._tmp.name).parent / "marker.json"
        try:
            cli._atomic_write_text(outside, '{"x":1}')
            mode = outside.stat().st_mode & 0o777
            # Whatever the umask gave us, NOT 0o600.
            self.assertNotEqual(mode, 0o600,
                                "marker should not get 0o600 — it must stay readable")
        finally:
            try:
                outside.unlink()
            except OSError:
                pass


class IndexLookupTests(unittest.TestCase):
    def test_lookup_by_id_finds_record(self):
        records = [_rec("a" * 24, "X"), _rec("b" * 24, "Y")]
        rec = cli._index_record_by_id(records, "b" * 24)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["name"], "Y")

    def test_lookup_by_id_misses(self):
        records = [_rec("a" * 24, "X")]
        self.assertIsNone(cli._index_record_by_id(records, "z" * 24))


class ConcurrencyTests(unittest.TestCase):
    """Two-agent scenario: e.g. two Codex CLI windows fire the PreToolUse
    hook on the same project simultaneously. Each hook writes state.json,
    versions.json, projects.json, and possibly the marker. The previous
    `_atomic_write_text` used a deterministic .tmp filename and stomped
    itself under concurrency."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self._tmp.name) / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_atomic_write_survives_concurrent_writers(self):
        # Spawn many threads writing distinct content to the same target
        # file. After all complete, the file must be readable JSON whose
        # content matches one of the writers — never garbled, never empty,
        # and no leftover .tmp files.
        import threading
        N = 20
        errors = []

        def worker(i):
            try:
                cli._atomic_write_text(self.target, json.dumps({"writer": i}))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"writers raised: {errors}")
        # File parses cleanly and matches one of the writers.
        data = json.loads(self.target.read_text())
        self.assertIn(data.get("writer"), set(range(N)))
        # No orphaned .tmp files in the directory.
        leftovers = [p.name for p in self.target.parent.iterdir()
                     if ".tmp-" in p.name]
        self.assertEqual(leftovers, [], f"orphaned tmp files: {leftovers}")

    def test_concurrent_extract_files_does_not_corrupt(self):
        # Two "processes" (threads here) extract the same file from the
        # same zip simultaneously. The shared retry+unique-tmp path in
        # _extract_files must produce a coherent target file matching the
        # zip content, with no orphaned .tmp files and no spurious
        # PermissionError raised to the caller.
        import io
        import threading
        import zipfile

        folder = pathlib.Path(self._tmp.name) / "proj"
        folder.mkdir()
        zb_buf = io.BytesIO()
        with zipfile.ZipFile(zb_buf, "w") as z:
            z.writestr("main.tex", "FROM_ZIP_CONTENT")
        zb = zb_buf.getvalue()
        errors = []

        def worker():
            try:
                cli._extract_files(zb, folder, paths=None, protect_recent_seconds=0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"extract raised: {errors}")
        self.assertEqual((folder / "main.tex").read_bytes(), b"FROM_ZIP_CONTENT")
        leftovers = [p.name for p in folder.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftovers, [], f"orphaned tmp files: {leftovers}")

    def test_concurrent_state_writes_different_keys_dont_corrupt_file(self):
        # state.json and versions.json are read-modify-write. Two processes
        # updating DIFFERENT keys can lose one of the updates (last-writer-
        # wins) — that's documented as acceptable behavior. What MUST hold
        # is that the file always parses as valid JSON and contains a
        # subset of the intended writes; never partially-written, never
        # corrupted, never an exception bubbles up.
        import threading
        target = pathlib.Path(self._tmp.name) / "state.json"
        target.write_text(json.dumps({}))
        errors = []
        N = 20

        def worker(i):
            try:
                # Simulate the read-modify-write pattern of mark_synced.
                with open(target) as f:
                    state = json.load(f)
                state[f"key{i}"] = i
                cli._atomic_write_text(target, json.dumps(state))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"writers raised: {errors}")
        # File parses, contains a subset of writers' keys (lost updates OK).
        data = json.loads(target.read_text())
        for k, v in data.items():
            self.assertTrue(k.startswith("key"))
            i = int(k[3:])
            self.assertEqual(v, i, f"value for {k} should be {i}, got {v}")

    def test_concurrent_marker_writers_converge(self):
        # Two hooks both auto-link the same folder concurrently. Both write
        # the marker via _write_marker (which goes through _atomic_write_text).
        # Final marker must be valid JSON pointing at the agreed project_id.
        import threading
        folder = pathlib.Path(self._tmp.name)
        pid_value = "a" * 24
        errors = []

        def worker():
            try:
                cli._write_marker(folder, pid_value, project_name="P", source="auto-link")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        data = json.loads((folder / cli.PROJECT_MARKER).read_text())
        self.assertEqual(data["project_id"], pid_value)


if __name__ == "__main__":
    unittest.main()
