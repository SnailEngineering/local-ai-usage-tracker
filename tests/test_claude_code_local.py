from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiusage import db
from aiusage.sources import claude_code_local as cc


def _assistant(i: int, tokens: int, ts: str | None = None) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts or f"2026-08-02T12:00:{i:02d}Z",
        "requestId": f"r{i}",
        "cwd": "/work/project",
        "message": {
            "id": f"m{i}",
            "model": "claude-opus-5",
            "usage": {"input_tokens": tokens, "output_tokens": 1},
        },
    }


class ClaudeIngestTests(unittest.TestCase):
    def _ingest(self, blob: bytes):
        """Write `blob`, ingest, and hand back the connection plus the archive."""
        root = Path(tempfile.mkdtemp())
        archive = root / "archive"
        archive.mkdir()
        (archive / "s.jsonl").write_bytes(blob)
        conn = db.connect(root / "t.db")
        self.addCleanup(conn.close)
        return conn, archive

    def test_one_malformed_record_does_not_stall_the_file(self) -> None:
        """A structurally valid record with an unparseable timestamp must be
        counted and stepped over. Raising would roll the whole source back and
        leave the offset unsaved, so every later run would die on the same byte
        and never reach the records after it."""
        bad = _assistant(2, 10, ts="not-a-timestamp")
        blob = b"".join(json.dumps(r).encode() + b"\n"
                        for r in (_assistant(1, 5), bad, _assistant(3, 7)))
        conn, archive = self._ingest(blob)

        stats = cc.ingest(conn, archive, "now")

        self.assertEqual(stats["bad_records"], 1)
        # The good record *after* the bad one still has to land.
        self.assertEqual(
            [r["input_tokens"] for r in
             conn.execute("SELECT input_tokens FROM usage_event ORDER BY ts")],
            [5, 7],
        )
        # ...and the offset must have advanced past the whole file.
        offset = json.loads(
            conn.execute("SELECT value FROM ingest_state").fetchone()["value"])["offset"]
        self.assertEqual(offset, (archive / "s.jsonl").stat().st_size)


if __name__ == "__main__":
    unittest.main()
