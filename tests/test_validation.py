from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiusage import db
from aiusage.sources import claude_code_local as cc, codex_local as cx
from tests.test_claude_code_local import _assistant


def codex_usage(tokens: int) -> dict:
    return {"type": "event_msg", "timestamp": "2026-08-02T12:00:00Z",
            "payload": {"type": "token_count", "info": {"last_token_usage": {
                "total_tokens": tokens, "input_tokens": tokens}}}}


class ValidationTests(unittest.TestCase):
    def test_malformed_records_do_not_block_good_records_or_resumed_reads(self):
        for source in (cc, cx):
            bad = [None, [], 42, "text"]
            if source is cc:
                good = [_assistant(1, 5), _assistant(2, 7), _assistant(3, 11)]
                bad += [{"type": "assistant", "message": []},
                        {"type": "assistant", "message": {"usage": "bad"}}]
                for value in ([], {}, "10", -1, True, 1.5, 2**63):
                    rec = _assistant(4, 9)
                    rec["message"]["usage"]["input_tokens"] = value
                    bad.append(rec)
                rec = _assistant(4, 9)
                rec["message"]["model"] = ["invalid"]
                bad.append(rec)
                rec = _assistant(4, 9)
                rec["gitBranch"] = {"invalid": "binding"}
                bad.append(rec)
            else:
                good = [codex_usage(n) for n in (5, 7, 11)]
                bad += [{"payload": []}, {"payload": {"type": "token_count", "info": []}},
                        {"payload": {"type": "token_count", "info": {"last_token_usage": []}}},
                        {"type": "turn_context", "payload": {"model": []}},
                        {"type": "session_meta", "payload": {"cwd": []}}]
                for value in ([], {}, "10", -1, True, 1.5, 2**63):
                    rec = codex_usage(9)
                    rec["payload"]["info"]["last_token_usage"]["input_tokens"] = value
                    bad.append(rec)
                rec = codex_usage(9)
                rec["payload"]["rate_limits"] = ["invalid"]
                bad.append(rec)

            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "archive"
                archive.mkdir()
                path = archive / "s.jsonl"
                path.write_text("".join(json.dumps(r) + "\n" for r in [good[0], *bad, good[1]]))
                conn = db.connect(root / "usage.db")
                try:
                    stats = source.ingest(conn, archive, "now")
                    conn.commit()
                    self.assertEqual(stats["bad_records"], len(bad))
                    with path.open("a") as fh:
                        fh.write(json.dumps(good[2]) + "\n")
                    source.ingest(conn, archive, "now")
                    conn.commit()
                    rows = list(conn.execute("SELECT id, input_tokens FROM usage_event ORDER BY id"))
                    self.assertEqual(sorted(r[1] for r in rows), [5, 7, 11])
                    state = json.loads(conn.execute("SELECT value FROM ingest_state").fetchone()[0])
                    self.assertEqual(state["offset"], path.stat().st_size)
                    conn.execute("DELETE FROM ingest_state")
                    source.ingest(conn, archive, "now")
                    replay = list(conn.execute("SELECT id, input_tokens FROM usage_event ORDER BY id"))
                    self.assertEqual(rows, replay)
                finally:
                    conn.close()
