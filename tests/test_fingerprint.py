from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiusage import db
from aiusage.sources import claude_code_local as cc, codex_local as cx
from tests.test_claude_code_local import _assistant
from tests.test_validation import codex_usage


class FingerprintTests(unittest.TestCase):
    def test_growing_rewrites_beyond_the_first_4kb_are_replayed(self):
        for source in (cc, cx):
            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "archive"
                archive.mkdir()
                path = archive / "s.jsonl"
                prefix = json.dumps({"type": "padding", "text": "x" * 5000}) + "\n"
                def body(tokens):
                    records = [_assistant(i, n) if source is cc else codex_usage(n)
                               for i, n in enumerate(tokens)]
                    return prefix + "".join(json.dumps(r) + "\n" for r in records)
                path.write_text(body([5, 7]))
                conn = db.connect(root / "usage.db")
                try:
                    source.ingest(conn, archive, "now")
                    conn.commit()
                    path.write_text(body([9, 7, 8]))
                    stats = source.ingest(conn, archive, "now")
                    self.assertEqual(stats["rewritten"], 1)
                    self.assertEqual(conn.execute("SELECT SUM(input_tokens) FROM usage_event").fetchone()[0], 24)
                    self.assertEqual(source.ingest(conn, archive, "now")["files_read"], 0)
                finally:
                    conn.close()

    def test_completing_a_partial_line_is_an_append_not_a_rewrite(self):
        for source in (cc, cx):
            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "archive"
                archive.mkdir()
                path = archive / "s.jsonl"
                records = [_assistant(i, n) if source is cc else codex_usage(n)
                           for i, n in enumerate((5, 7))]
                first, second = [json.dumps(r) + "\n" for r in records]
                path.write_text(first + second[:20])
                conn = db.connect(root / "usage.db")
                try:
                    source.ingest(conn, archive, "now")
                    with path.open("a") as fh:
                        fh.write(second[20:])
                    stats = source.ingest(conn, archive, "now")
                    self.assertEqual(stats["rewritten"], 0)
                    self.assertEqual(conn.execute("SELECT SUM(input_tokens) FROM usage_event").fetchone()[0], 12)
                finally:
                    conn.close()

    def test_head_only_state_is_upgraded_even_for_an_unchanged_file(self):
        for source in (cc, cx):
            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "archive"
                archive.mkdir()
                path = archive / "s.jsonl"
                path.write_text(json.dumps(_assistant(1, 5) if source is cc else codex_usage(5)) + "\n")
                conn = db.connect(root / "usage.db")
                try:
                    source.ingest(conn, archive, "now")
                    row = conn.execute("SELECT key, value FROM ingest_state").fetchone()
                    legacy = json.loads(row["value"])
                    del legacy["prefix_hash"]
                    del legacy["prefix_len"]
                    legacy.update(head="old-head-hash", head_len=4096)
                    db.set_state(conn, row["key"], json.dumps(legacy), "now")
                    self.assertEqual(source.ingest(conn, archive, "now")["rewritten"], 1)
                    self.assertEqual(source.ingest(conn, archive, "now")["files_read"], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0], 1)
                finally:
                    conn.close()
