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


if __name__ == "__main__":
    unittest.main()
