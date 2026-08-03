from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
