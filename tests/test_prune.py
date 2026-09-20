from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from aiusage import db, prune
from aiusage.sources.fingerprint import prefix_hash


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.claude = self.root / "archive"
        self.codex = self.root / "archive-codex"
        self.conn = db.connect(self.root / "usage.db")
        self.addCleanup(self.conn.close)
        self.archives = {"claude_code_local": self.claude, "codex_local": self.codex}

    def _file(self, directory: Path, name: str, body: str, age_days: float) -> Path:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def _state(self, key: str, offset: int) -> None:
        prefix, rel = key.split(":", 1)
        path = (self.claude if prefix == "cc_offset" else self.codex) / rel
        digest, length = prefix_hash(path, offset)
        db.set_state(self.conn, key, json.dumps({
            "offset": offset, "mtime_ns": path.stat().st_mtime_ns,
            "prefix_hash": digest, "prefix_len": length,
        }), "now")
        self.conn.commit()

    def test_only_old_and_fully_ingested_files_are_prunable(self) -> None:
        old_done = self._file(self.claude, "old-done.jsonl", "x" * 100, 90)
        self._state("cc_offset:old-done.jsonl", 100)

        old_partial = self._file(self.claude, "old-partial.jsonl", "x" * 100, 90)
        self._state("cc_offset:old-partial.jsonl", 60)   # trailing bytes unread

        old_unknown = self._file(self.claude, "old-unknown.jsonl", "x" * 100, 90)
        # deliberately no ingest_state row

        recent = self._file(self.claude, "recent.jsonl", "x" * 100, 1)
        self._state("cc_offset:recent.jsonl", 100)

        by_path = {c.path: c for c in prune.survey(self.conn, self.archives, 30)}

        self.assertTrue(by_path[old_done].prunable)
        self.assertIn("60", by_path[old_partial].reason)
        self.assertEqual(by_path[old_unknown].reason, "never ingested")
        self.assertEqual(by_path[recent].reason, "newer than the cutoff")

    def test_prune_removes_the_file_and_its_state_only(self) -> None:
        doomed = self._file(self.claude, "old.jsonl", "x" * 100, 90)
        self._state("cc_offset:old.jsonl", 100)
        kept = self._file(self.claude, "new.jsonl", "x" * 100, 1)
        self._state("cc_offset:new.jsonl", 100)

        removed, reclaimed = prune.prune(
            self.conn, prune.survey(self.conn, self.archives, 30))

        self.assertEqual((removed, reclaimed), (1, 100))
        self.assertFalse(doomed.exists())
        self.assertTrue(kept.exists())
        keys = {r["key"] for r in self.conn.execute("SELECT key FROM ingest_state")}
        self.assertEqual(keys, {"cc_offset:new.jsonl"})

    def test_pruning_never_touches_recorded_usage(self) -> None:
        """The whole premise: the events are already in the database, so
        deleting the transcript must not change a single number."""
        self._file(self.claude, "old.jsonl", "x" * 100, 90)
        self._state("cc_offset:old.jsonl", 100)
        db.upsert_usage(self.conn, [{
            "id": "cc:m1:r1", "source": "claude_code_local", "provider": "anthropic",
            "ts": "2026-06-01T10:00:00Z", "day": "2026-06-01", "model": "claude-opus-5",
            "input_tokens": 10, "output_tokens": 5, "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0, "cache_read_tokens": 0, "reasoning_tokens": 0,
            "requests": 1, "project": "p", "git_branch": None, "session_id": "s",
            "service_tier": None, "ingested_at": "now",
        }])
        self.conn.commit()
        before = self.conn.execute(
            "SELECT COUNT(*) c, SUM(input_tokens) t FROM usage_event").fetchone()

        prune.prune(self.conn, prune.survey(self.conn, self.archives, 30))

        after = self.conn.execute(
            "SELECT COUNT(*) c, SUM(input_tokens) t FROM usage_event").fetchone()
        self.assertEqual((before["c"], before["t"]), (after["c"], after["t"]))

    def test_emptied_codex_date_directories_are_removed(self) -> None:
        """Codex nests under YYYY/MM/DD, so pruning a day leaves empty dirs."""
        self._file(self.codex, "2026/06/01/a.jsonl", "x" * 10, 90)
        self._state("codex_offset:2026/06/01/a.jsonl", 10)
        self._file(self.codex, "2026/06/02/b.jsonl", "x" * 10, 1)
        self._state("codex_offset:2026/06/02/b.jsonl", 10)

        prune.prune(self.conn, prune.survey(self.conn, self.archives, 30))

        self.assertFalse((self.codex / "2026" / "06" / "01").exists())
        self.assertTrue((self.codex / "2026" / "06" / "02").exists())

    def test_a_legacy_bare_integer_offset_needs_a_collection_before_pruning(self) -> None:
        path = self._file(self.claude, "old.jsonl", "x" * 100, 90)
        db.set_state(self.conn, "cc_offset:old.jsonl", "100", "now")
        self.conn.commit()
        by_path = {c.path: c for c in prune.survey(self.conn, self.archives, 30)}
        self.assertFalse(by_path[path].prunable)
        self.assertIn("collect again", by_path[path].reason)


    def test_changed_files_and_state_are_revalidated_before_deletion(self) -> None:
        for change in ("append", "replace", "rewrite", "state", "missing"):
            with self.subTest(change):
                path = self._file(self.claude, "changing.jsonl", "x" * 100, 90)
                key = "cc_offset:changing.jsonl"
                self._state(key, 100)
                candidates = prune.survey(self.conn, self.archives, 30)
                self.assertTrue(next(c for c in candidates if c.path == path).prunable)
                stamp = path.stat().st_mtime_ns
                if change == "append":
                    with path.open("a") as fh:
                        fh.write("unread")
                elif change == "replace":
                    replacement = path.with_suffix(".tmp")
                    replacement.write_text("y" * 100)
                    os.utime(replacement, ns=(stamp, stamp))
                    replacement.replace(path)
                elif change == "rewrite":
                    path.write_text("y" * 100)
                    os.utime(path, ns=(stamp, stamp))
                elif change == "state":
                    self._state(key, 50)
                else:
                    path.unlink()
                self.assertEqual(prune.prune(self.conn, candidates), (0, 0))
                self.assertIsNotNone(db.get_state(self.conn, key))
                if change != "missing":
                    self.assertTrue(path.exists())

    def test_same_size_rewrite_before_survey_is_not_fully_ingested(self) -> None:
        path = self._file(self.claude, "rewritten.jsonl", "x" * 100, 90)
        self._state("cc_offset:rewritten.jsonl", 100)
        stamp = path.stat().st_mtime_ns
        path.write_text("y" * 100)
        os.utime(path, ns=(stamp, stamp))
        candidate = prune.survey(self.conn, self.archives, 30)[0]
        self.assertFalse(candidate.prunable)
        self.assertEqual(candidate.reason, "changed since ingestion")

    def test_cleanup_keeps_archive_root_and_ancestors(self) -> None:
        self._file(self.codex, "2026/06/01/a.jsonl", "x" * 10, 90)
        self._state("codex_offset:2026/06/01/a.jsonl", 10)
        self.assertEqual(prune.prune(self.conn, prune.survey(self.conn, self.archives, 30)), (1, 10))
        self.assertTrue(self.codex.is_dir())
        self.assertTrue((self.codex / ".aiusage.lock").is_file())
        self.assertFalse((self.codex / "2026").exists())


if __name__ == "__main__":
    unittest.main()
