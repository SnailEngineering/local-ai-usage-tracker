import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import collect
from aiusage import db, dashboard
from aiusage.sources import claude_code_local as cc
from tests.test_claude_code_local import _assistant


class StatsCacheTests(unittest.TestCase):
    def test_bad_optional_cache_never_rolls_back_detailed_usage(self):
        cases = [None, [], 42, {'dailyModelTokens': [None]},
                 {'dailyModelTokens': 'bad'}, {'dailyModelTokens': [{'date': '2026-02-30'}]},
                 {'dailyModelTokens': [{'date': '2026-08-02', 'tokensByModel': []}]}]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / 'claude' / 'projects'
                projects.mkdir(parents=True)
                (projects / 's.jsonl').write_text(json.dumps(_assistant(1, 5)) + '\n')
                (projects.parent / 'stats-cache.json').write_text(json.dumps(payload))
                conn = db.connect(root / 'usage.db')
                try:
                    self.assertEqual(collect.run_sources(conn, projects.parent, root / 'codex',
                        root / 'archive', root / 'archive-codex', only='claude_code', quiet=True), 0)
                    self.assertEqual(conn.execute('SELECT SUM(input_tokens) FROM usage_event').fetchone()[0], 5)
                    detail = conn.execute("SELECT detail FROM run_log WHERE source = ?", (cc.SOURCE,)).fetchone()[0]
                    self.assertIn('bad_coarse_records=1', detail)
                finally:
                    conn.close()

    def test_invalid_dates_and_tokens_are_skipped_but_valid_neighbors_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / 'stats-cache.json'
            cache.write_text(json.dumps({'dailyModelTokens': [
                {'date': '<img src=x onerror=alert(1)>', 'tokensByModel': {'m': 10}},
                {'date': '2026-02-30', 'tokensByModel': {'m': 10}},
                {'date': '2026-08-02', 'tokensByModel': {
                    'valid': 5, 'negative': -1, 'huge': 2**63, 'bool': True,
                    'float': 1.5, 'str': '10', 'object': {},
                }},
                {'date': '2026-08-03', 'tokensByModel': {'valid': 7}},
            ]}))
            conn = db.connect(root / 'usage.db')
            try:
                stats = {}
                self.assertEqual(cc.backfill_stats_cache(conn, cache, 'now', stats), 2)
                self.assertEqual(stats['bad_coarse_records'], 8)
                self.assertEqual(conn.execute('SELECT SUM(total_tokens) FROM coarse_daily_tokens').fetchone()[0], 12)
            finally:
                conn.close()

    def test_content_policy_hash_matches_the_actual_generated_script(self):
        for seconds in (0, 60):
            html = dashboard.render_payload({'value': '</script><img onerror=alert(1)>__SCRIPT_HASH____REFRESH_MS__'}, seconds)
            script = html.split('<script>', 1)[1].split('</script>', 1)[0]
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn("script-src 'sha256-" + digest + "'", html)
            self.assertIn("connect-src 'self'", html)
            self.assertIn('(${esc(lo)} → ${esc(hi)},', html)
