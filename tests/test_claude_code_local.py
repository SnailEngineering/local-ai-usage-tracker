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

    def test_duplicate_content_blocks_collapse_to_one_event(self) -> None:
        """Claude Code writes one record per content block, all sharing
        (message id, requestId), and the later ones carry the *complete*
        output_tokens while the first carry a partial count. They must collapse
        to a single row holding the final number, not sum into a phantom spend.
        """
        rec = _assistant(1, 100)
        blocks = []
        for out in (1, 1, 317):  # what a real streamed message looks like
            r = json.loads(json.dumps(rec))
            r["message"]["usage"]["output_tokens"] = out
            blocks.append(r)
        conn, archive = self._ingest(
            b"".join(json.dumps(r).encode() + b"\n" for r in blocks))

        cc.ingest(conn, archive, "now")
        rows = conn.execute("SELECT id, output_tokens FROM usage_event").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["output_tokens"], 317)

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

    def test_offset_tracks_bytes_not_decoded_characters(self) -> None:
        """`offset` is a byte position. Reading as text would collapse CRLF and
        expand undecodable bytes into U+FFFD, so the stored offset would drift
        from the real one and every later resumed read would start mid-line."""
        for label, blob in (
            ("crlf", b"".join(json.dumps(_assistant(i, 10)).encode() + b"\r\n"
                              for i in (1, 2, 3))),
            ("undecodable", json.dumps(_assistant(1, 10)).encode() + b"\n"
             + b'{"type":"assistant","timestamp":"2026-08-02T12:00:02Z","requestId":"r2"'
               b',"cwd":"/w/\xff\xfe","message":{"id":"m2","model":"claude-opus-5"'
               b',"usage":{"input_tokens":10,"output_tokens":1}}}\n'
             + json.dumps(_assistant(3, 10)).encode() + b"\n"),
        ):
            with self.subTest(label):
                lines = blob.splitlines(keepends=True)
                conn, archive = self._ingest(lines[0])
                path = archive / "s.jsonl"

                cc.ingest(conn, archive, "now")          # first pass
                with path.open("ab") as fh:              # ...then a resumed one
                    fh.write(b"".join(lines[1:]))
                cc.ingest(conn, archive, "now")

                self.assertEqual(
                    conn.execute("SELECT COUNT(*) c FROM usage_event").fetchone()["c"], 3)
                offset = json.loads(
                    conn.execute(
                        "SELECT value FROM ingest_state").fetchone()["value"])["offset"]
                self.assertEqual(offset, path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
