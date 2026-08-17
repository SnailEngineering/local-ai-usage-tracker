from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from aiusage import db
from aiusage.sources import codex_local


class CodexLocalIngestTests(unittest.TestCase):
    def test_resumed_ingest_keeps_prior_context_and_adds_new_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive"
            session = archive / "2026" / "08" / "02" / "session.jsonl"
            session.parent.mkdir(parents=True)

            records = [
                {
                    "timestamp": "2026-08-02T12:00:00Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/work/project"},
                },
                {
                    "timestamp": "2026-08-02T12:00:01Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-terra"},
                },
                {
                    "timestamp": "2026-08-02T12:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"total_tokens": 3, "input_tokens": 3}},
                    },
                },
            ]
            session.write_text("".join(json.dumps(record) + "\n" for record in records))
            conn = db.connect(root / "usage.db")
            self.addCleanup(conn.close)

            self.assertEqual(codex_local.ingest(conn, archive, "now")["events"], 1)
            conn.commit()

            with session.open("a") as fh:
                fh.write(json.dumps({
                    "timestamp": "2026-08-02T12:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"total_tokens": 5, "input_tokens": 5}},
                    },
                }) + "\n")

            self.assertEqual(codex_local.ingest(conn, archive, "now")["events"], 1)
            rows = conn.execute(
                "SELECT model, project, input_tokens FROM usage_event ORDER BY ts"
            ).fetchall()
            self.assertEqual(
                [(row["model"], row["project"], row["input_tokens"]) for row in rows],
                [("gpt-5.6-terra", "project", 3), ("gpt-5.6-terra", "project", 5)],
            )

    def test_same_size_rewrite_is_reingested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive"
            session = archive / "session.jsonl"
            session.parent.mkdir(parents=True)

            def write_usage(tokens: int) -> None:
                session.write_text("".join(json.dumps(record) + "\n" for record in [
                    {
                        "timestamp": "2026-08-02T12:00:00Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-terra"},
                    },
                    {
                        "timestamp": "2026-08-02T12:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"last_token_usage": {
                                "total_tokens": tokens, "input_tokens": tokens,
                            }},
                        },
                    },
                ]))

            write_usage(3)
            conn = db.connect(root / "usage.db")
            self.addCleanup(conn.close)
            self.assertEqual(codex_local.ingest(conn, archive, "now")["events"], 1)
            original_mtime = session.stat().st_mtime_ns

            write_usage(7)  # Same JSONL length, different token count.
            os.utime(session, ns=(original_mtime, original_mtime + 1_000_000))
            self.assertEqual(codex_local.ingest(conn, archive, "now")["events"], 1)
            row = conn.execute("SELECT input_tokens FROM usage_event").fetchone()
            self.assertEqual(row["input_tokens"], 7)

    def _write(self, path: Path, n_events: int, tokens: int) -> None:
        recs = [{"timestamp": "2026-08-02T12:00:00Z", "type": "turn_context",
                 "payload": {"model": "gpt-5.6-terra"}}]
        recs += [{"timestamp": f"2026-08-02T12:00:{i + 1:02d}Z", "type": "event_msg",
                  "payload": {"type": "token_count", "info": {"last_token_usage": {
                      "total_tokens": tokens, "input_tokens": tokens}}}}
                 for i in range(n_events)]
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def _totals(self, conn) -> tuple[int, int]:
        r = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(input_tokens), 0) t FROM usage_event"
        ).fetchone()
        return (r["n"], r["t"])

    def test_rewritten_session_does_not_leave_orphan_events(self) -> None:
        """Codex ids are positional (`codex:<session>:<seq>`), so replaying a
        rewritten file through INSERT OR REPLACE is not enough on its own: any
        event that vanished from the file keeps its row and invents usage. A
        rewrite that *grows* the file is the nastier half -- it is byte-identical
        in shape to an append, so only the head hash catches it.
        """
        cases = [
            # (label, first, second, expected rows, expected tokens)
            ("shrink", (2, 5), (1, 5), 1, 5),
            ("grow-rewrite", (2, 5), (3, 9), 3, 27),
            ("plain append", (2, 5), (3, 5), 3, 15),
        ]
        for label, first, second, want_rows, want_tokens in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "archive"
                archive.mkdir()
                session = archive / "sess.jsonl"

                self._write(session, *first)
                conn = db.connect(Path(tmp) / "usage.db")
                self.addCleanup(conn.close)
                codex_local.ingest(conn, archive, "now")

                mtime = session.stat().st_mtime_ns
                self._write(session, *second)
                os.utime(session, ns=(mtime, mtime + 1_000_000))
                stats = codex_local.ingest(conn, archive, "now")

                self.assertEqual(self._totals(conn), (want_rows, want_tokens))
                self.assertEqual(stats["rewritten"], 0 if label == "plain append" else 1)

    def test_one_malformed_record_does_not_stall_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive"
            archive.mkdir()
            session = archive / "sess.jsonl"

            def usage(ts: str, tokens: int) -> dict:
                return {"timestamp": ts, "type": "event_msg", "payload": {
                    "type": "token_count", "info": {"last_token_usage": {
                        "total_tokens": tokens, "input_tokens": tokens}}}}

            session.write_text("".join(json.dumps(r) + "\n" for r in [
                {"timestamp": "2026-08-02T12:00:00Z", "type": "turn_context",
                 "payload": {"model": "gpt-5.6-terra"}},
                usage("2026-08-02T12:00:01Z", 5),
                usage("not-a-timestamp", 9),
                usage("2026-08-02T12:00:03Z", 7),
            ]))
            conn = db.connect(Path(tmp) / "usage.db")
            self.addCleanup(conn.close)

            stats = codex_local.ingest(conn, archive, "now")

            self.assertEqual(stats["bad_records"], 1)
            self.assertEqual(self._totals(conn), (2, 12))
            offset = json.loads(conn.execute(
                "SELECT value FROM ingest_state").fetchone()["value"])["offset"]
            self.assertEqual(offset, session.stat().st_size)


if __name__ == "__main__":
    unittest.main()
