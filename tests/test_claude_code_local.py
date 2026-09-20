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

    def test_flat_cache_creation_total_falls_back_to_the_5m_tier(self) -> None:
        """Newer records split cache writes by TTL; older ones only carry the
        flat total. The fallback has to attribute it, or a 1.25x-priced chunk
        of every old record silently disappears from the cost."""
        nested = _assistant(1, 10)
        nested["message"]["usage"].update({
            "cache_creation_input_tokens": 900,
            "cache_creation": {"ephemeral_5m_input_tokens": 300,
                               "ephemeral_1h_input_tokens": 600},
        })
        flat = _assistant(2, 10)
        flat["message"]["usage"]["cache_creation_input_tokens"] = 900

        conn, archive = self._ingest(
            b"".join(json.dumps(r).encode() + b"\n" for r in (nested, flat)))
        cc.ingest(conn, archive, "now")

        rows = conn.execute(
            "SELECT cache_write_5m_tokens w5, cache_write_1h_tokens w1h "
            "FROM usage_event ORDER BY ts").fetchall()
        self.assertEqual([(r["w5"], r["w1h"]) for r in rows],
                         [(300, 600), (900, 0)])

    def test_synthetic_and_usageless_records_are_skipped(self) -> None:
        """Claude Code writes placeholder assistant turns for local no-ops.
        Counting them would inflate the message count with turns that never
        reached the API."""
        synthetic = _assistant(1, 10)
        synthetic["message"]["model"] = "<synthetic>"
        usageless = _assistant(2, 10)
        del usageless["message"]["usage"]
        user_turn = {"type": "user", "timestamp": "2026-08-02T12:00:03Z"}

        conn, archive = self._ingest(
            b"".join(json.dumps(r).encode() + b"\n"
                     for r in (synthetic, usageless, user_turn, _assistant(4, 7))))
        cc.ingest(conn, archive, "now")

        rows = conn.execute("SELECT input_tokens FROM usage_event").fetchall()
        self.assertEqual([r["input_tokens"] for r in rows], [7])

    def test_a_legacy_integer_offset_is_still_accepted(self) -> None:
        """Databases written before the state became JSON hold a bare integer.
        Rejecting it would silently re-ingest every archived file from zero."""
        conn, archive = self._ingest(
            json.dumps(_assistant(1, 5)).encode() + b"\n")
        db.set_state(conn, "cc_offset:s.jsonl", "12", "now")
        self.assertEqual(cc._read_state(conn, "cc_offset:s.jsonl"), (12, None, None, 0))

        db.set_state(conn, "cc_offset:s.jsonl", "not-a-number", "now")
        self.assertEqual(cc._read_state(conn, "cc_offset:s.jsonl"), (0, None, None, 0))

    def test_a_rewrite_that_grows_the_file_is_reread_from_the_top(self) -> None:
        """A rewrite that ends up longer than before looks exactly like an
        append to size and mtime, so ingest resumed mid-file and never saw the
        changed prefix. The hash of the file's head is what gives it away."""
        def blob(*events: dict) -> bytes:
            return b"".join(json.dumps(e).encode() + b"\n" for e in events)

        conn, archive = self._ingest(blob(_assistant(1, 5), _assistant(2, 5)))
        cc.ingest(conn, archive, "now")

        # A plain append leaves the prefix alone: not a rewrite.
        (archive / "s.jsonl").write_bytes(
            blob(_assistant(1, 5), _assistant(2, 5), _assistant(3, 5)))
        self.assertEqual(cc.ingest(conn, archive, "now")["rewritten"], 0)

        # Same file, first message changed, and longer overall.
        (archive / "s.jsonl").write_bytes(
            blob(_assistant(1, 9000), _assistant(2, 5), _assistant(3, 5), _assistant(4, 5)))
        stats = cc.ingest(conn, archive, "now")

        self.assertEqual(stats["rewritten"], 1)
        rows = {r["id"]: r["input_tokens"]
                for r in conn.execute("SELECT id, input_tokens FROM usage_event")}
        self.assertEqual(sorted(rows.values()), [5, 5, 5, 9000])

    def test_legacy_state_is_replayed_to_establish_a_complete_fingerprint(self) -> None:
        """Legacy offsets cannot verify the entire prefix and need one replay."""
        conn, archive = self._ingest(
            json.dumps(_assistant(1, 5)).encode() + b"\n" + json.dumps(_assistant(2, 5)).encode() + b"\n")
        size = (archive / "s.jsonl").stat().st_size
        db.set_state(conn, "cc_offset:s.jsonl", json.dumps({"offset": size // 2}), "now")
        (archive / "s.jsonl").write_bytes((archive / "s.jsonl").read_bytes())
        self.assertEqual(cc.ingest(conn, archive, "now")["rewritten"], 1)
        self.assertEqual(cc.ingest(conn, archive, "now")["files_read"], 0)

    def test_the_full_working_directory_is_stored_beside_its_basename(self) -> None:
        conn, archive = self._ingest(json.dumps(_assistant(1, 5)).encode() + b"\n")
        cc.ingest(conn, archive, "now")
        row = conn.execute("SELECT project, project_path FROM usage_event").fetchone()
        self.assertEqual((row["project"], row["project_path"]), ("project", "/work/project"))

    def test_the_schema_migration_backfills_paths_from_the_archive(self) -> None:
        """Rows ingested before project_path existed have NULL there. The
        migration's forgotten offsets must make the next ingest rewrite them."""
        conn, archive = self._ingest(json.dumps(_assistant(1, 5)).encode() + b"\n")
        cc.ingest(conn, archive, "now")
        conn.execute("UPDATE usage_event SET project_path = NULL")

        cc.ingest(conn, archive, "now")   # nothing new: offsets say it is done
        self.assertIsNone(conn.execute("SELECT project_path FROM usage_event").fetchone()[0])

        db._backfill_project_path(conn)
        cc.ingest(conn, archive, "now")
        rows = conn.execute("SELECT project_path FROM usage_event").fetchall()
        self.assertEqual([r[0] for r in rows], ["/work/project"])   # one row, not two


if __name__ == "__main__":
    unittest.main()
