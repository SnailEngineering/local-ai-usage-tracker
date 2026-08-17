from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from aiusage import db, prune


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
        db.set_state(self.conn, key, json.dumps({"offset": offset}), "now")
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

    def test_a_legacy_bare_integer_offset_still_counts_as_ingested(self) -> None:
        path = self._file(self.claude, "old.jsonl", "x" * 100, 90)
        db.set_state(self.conn, "cc_offset:old.jsonl", "100", "now")
        self.conn.commit()
        by_path = {c.path: c for c in prune.survey(self.conn, self.archives, 30)}
        self.assertTrue(by_path[path].prunable)


if __name__ == "__main__":
    unittest.main()
