"""Xcode's assistants share the terminal tools' formats but are separate roots.

The same relative session path under two archives, and the same message or
session ids, must never overwrite each other or share ingest state.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import collect
from aiusage import dashboard, db, prune
from aiusage.sources import claude_code_local as cc
from aiusage.sources import codex_local as codex


def _claude_session(root: Path) -> None:
    f = root / "projects" / "-work-app" / "s1.jsonl"
    f.parent.mkdir(parents=True)
    rec = {"type": "assistant", "timestamp": "2026-08-02T12:00:01Z", "requestId": "r1",
           "cwd": "/work/app", "sessionId": "s1",
           "message": {"id": "m1", "model": "claude-opus-5",
                       "usage": {"input_tokens": 10, "output_tokens": 5}}}
    f.write_text(json.dumps(rec) + "\n")


def _codex_session(root: Path) -> None:
    f = root / "sessions" / "2026" / "08" / "02" / "rollout-s1.jsonl"
    f.parent.mkdir(parents=True)
    lines = [
        {"timestamp": "2026-08-02T12:00:00Z", "type": "turn_context",
         "payload": {"model": "gpt-5", "cwd": "/work/app"}},
        {"timestamp": "2026-08-02T12:00:01Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 20, "output_tokens": 4,
                                  "total_tokens": 24},
             "total_token_usage": {"input_tokens": 20, "output_tokens": 4,
                                   "total_tokens": 24}}}},
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in lines))


class XcodeVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.conn = db.connect(self.root / "t.db")
        self.addCleanup(self.conn.close)

    def test_same_session_in_both_roots_stays_two_sets_of_rows(self) -> None:
        for name in ("terminal", "xcode"):
            _claude_session(self.root / name)
        cc.run(self.conn, self.root / "terminal", self.root / "a-term")
        cc.run(self.conn, self.root / "xcode", self.root / "a-xcode", cc.XCODE)
        rows = self.conn.execute("SELECT id, source FROM usage_event").fetchall()
        self.assertEqual({(r["id"], r["source"]) for r in rows},
                         {("ccx:m1:r1", "claude_xcode"), ("cc:m1:r1", "claude_code_local")})
        # Separate state keys: neither root's offset shadows the other.
        keys = {r["key"] for r in self.conn.execute("SELECT key FROM ingest_state")}
        self.assertEqual({k.split(":")[0] for k in keys}, {"cc_offset", "ccx_offset"})

    def test_xcode_stats_cache_does_not_overwrite_terminal_totals(self) -> None:
        for name, total in (("terminal", 100), ("xcode", 999)):
            _claude_session(self.root / name)
            (self.root / name / "stats-cache.json").write_text(json.dumps(
                {"dailyModelTokens": [{"date": "2026-08-02",
                                       "tokensByModel": {"claude-opus-5": total}}]}))
        cc.run(self.conn, self.root / "terminal", self.root / "a-term")
        cc.run(self.conn, self.root / "xcode", self.root / "a-xcode", cc.XCODE)
        rows = self.conn.execute("SELECT total_tokens FROM coarse_daily_tokens").fetchall()
        self.assertEqual([r[0] for r in rows], [100])

    def test_codex_rows_are_namespaced(self) -> None:
        for name in ("terminal", "xcode"):
            _codex_session(self.root / name)
        codex.run(self.conn, self.root / "terminal", self.root / "a-term")
        codex.run(self.conn, self.root / "xcode", self.root / "a-xcode", codex.XCODE)
        sources = {r["source"]: r["id"] for r in
                   self.conn.execute("SELECT source, id FROM usage_event")}
        self.assertEqual(set(sources), {"codex_local", "codex_xcode"})
        self.assertTrue(sources["codex_xcode"].startswith("codexx:"))

    def test_codex_rewrite_in_one_root_leaves_the_other_alone(self) -> None:
        for name in ("terminal", "xcode"):
            _codex_session(self.root / name)
        codex.run(self.conn, self.root / "terminal", self.root / "a-term")
        codex.run(self.conn, self.root / "xcode", self.root / "a-xcode", codex.XCODE)
        # Truncate the Xcode original; its rewrite must only clear its own rows.
        src = next((self.root / "xcode" / "sessions").rglob("*.jsonl"))
        src.write_text(src.read_text().splitlines()[0] + "\n")
        codex.run(self.conn, self.root / "xcode", self.root / "a-xcode", codex.XCODE)
        counts = {r["source"]: r["n"] for r in self.conn.execute(
            "SELECT source, COUNT(*) n FROM usage_event GROUP BY source")}
        self.assertEqual(counts, {"codex_local": 1})

    def test_run_sources_collects_xcode_and_only_flag_selects_it(self) -> None:
        _claude_session(self.root / "xcode-claude")
        _codex_session(self.root / "xcode-codex")
        xcode = collect.XcodeRoots(self.root / "xcode-claude", self.root / "xcode-codex",
                                   self.root / "a-xc", self.root / "a-xx")
        failures = collect.run_sources(
            self.conn, self.root / "none", self.root / "none", self.root / "a1",
            self.root / "a2", only="claude_xcode", quiet=True, xcode=xcode)
        self.assertEqual(failures, 0)
        self.assertEqual(
            [r["source"] for r in self.conn.execute("SELECT DISTINCT source FROM usage_event")],
            ["claude_xcode"])

    def test_prune_understands_xcode_archives(self) -> None:
        self.assertEqual(set(prune.STATE_PREFIXES),
                         {"claude_code_local", "codex_local", "claude_xcode", "codex_xcode"})

    def test_dashboard_splits_usage_by_origin(self) -> None:
        _claude_session(self.root / "terminal")
        _claude_session(self.root / "xcode")
        cc.run(self.conn, self.root / "terminal", self.root / "a-term")
        cc.run(self.conn, self.root / "xcode", self.root / "a-xcode", cc.XCODE)
        sources = dashboard.build_payload(self.conn)["sources"]
        self.assertEqual({s["label"] for s in sources},
                         {"Claude Code (terminal)", "Claude in Xcode"})
        self.assertTrue(all(s["messages"] == 1 for s in sources))


if __name__ == "__main__":
    unittest.main()
