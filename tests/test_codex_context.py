from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiusage import db
from aiusage.sources import codex_local as cx
from tests.test_validation import codex_usage


class CodexContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.path = self.archive / "s.jsonl"
        self.conn = db.connect(self.root / "usage.db")
        self.addCleanup(self.conn.close)
        self.records = [
            {"type": "session_meta", "payload": {"cwd": "/work/project"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
            codex_usage(5),
        ]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.records))
        cx.ingest(self.conn, self.archive, "now")
        self.conn.commit()

    def append(self, *records):
        with self.path.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in records))

    def test_resume_uses_saved_context_without_parsing_previous_records(self):
        self.append(codex_usage(7))
        with patch.object(cx.json, "loads", wraps=json.loads) as loads:
            cx.ingest(self.conn, self.archive, "now")
        parsed = [call.args[0].strip() for call in loads.call_args_list]
        for record in self.records:
            self.assertNotIn(json.dumps(record), parsed)
        rows = self.conn.execute("SELECT model, project_path, input_tokens FROM usage_event ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("gpt-5.6-terra", "/work/project", 5), ("gpt-5.6-terra", "/work/project", 7)])

    def test_new_context_is_saved_for_the_following_resume(self):
        self.append({"type": "session_meta", "payload": {"cwd": "/work/other"}},
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}})
        cx.ingest(self.conn, self.archive, "now")
        self.append(codex_usage(7))
        cx.ingest(self.conn, self.archive, "now")
        row = self.conn.execute("SELECT model, project_path FROM usage_event WHERE input_tokens = 7").fetchone()
        self.assertEqual(tuple(row), ("gpt-5.6-sol", "/work/other"))

    def test_missing_or_invalid_saved_context_is_recovered_once(self):
        for context in (None, {"model": []}, {"model": "wrong", "cwd": []}):
            with self.subTest(context=context):
                state = json.loads(db.get_state(self.conn, "codex_offset:s.jsonl"))
                state["context"] = context
                db.set_state(self.conn, "codex_offset:s.jsonl", json.dumps(state), "now")
                self.append(codex_usage(7))
                cx.ingest(self.conn, self.archive, "now")
                state = json.loads(db.get_state(self.conn, "codex_offset:s.jsonl"))
                self.assertEqual(state["context"], {"model": "gpt-5.6-terra", "cwd": "/work/project"})
                rows = self.conn.execute("SELECT DISTINCT model, project_path FROM usage_event").fetchall()
                self.assertEqual([tuple(row) for row in rows], [("gpt-5.6-terra", "/work/project")])

    def test_rewrite_discards_context_from_the_previous_file(self):
        self.path.write_text(json.dumps(codex_usage(9)) + "\n")
        stats = cx.ingest(self.conn, self.archive, "now")
        self.assertEqual(stats["rewritten"], 1)
        rows = self.conn.execute("SELECT model, project_path, input_tokens FROM usage_event").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("unknown", None, 9)])
