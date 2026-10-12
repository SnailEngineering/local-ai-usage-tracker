import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiusage import db
from aiusage.sources import claude_code_local as cc, codex_local as cx
from tests.test_claude_code_local import _assistant
from tests.test_validation import codex_usage


def cumulative(last, total):
    record = codex_usage(last)
    record['payload']['info']['total_token_usage'] = {
        'input_tokens': total, 'total_tokens': total,
    }
    return record


class AccountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.path = self.archive / 's.jsonl'
        self.conn = db.connect(self.root / 'usage.db')
        self.addCleanup(self.conn.close)

    def append(self, *records):
        with self.path.open('a') as fh:
            fh.write(''.join(json.dumps(r) + '\n' for r in records))

    def tokens(self):
        return self.conn.execute('SELECT SUM(input_tokens) FROM usage_event').fetchone()[0]

    def test_repeats_across_reads_and_counter_resets(self):
        self.append(cumulative(100, 100), cumulative(100, 100))
        cx.ingest(self.conn, self.archive, 'now')
        self.conn.commit()
        self.assertEqual(self.tokens(), 100)
        self.append(cumulative(100, 100), cumulative(50, 150), cumulative(20, 20),
                    cumulative(20, 20), codex_usage(7), codex_usage(7))
        cx.ingest(self.conn, self.archive, 'now')
        self.assertEqual(self.tokens(), 184)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_event').fetchone()[0], 5)

    def test_upgrade_rebuilds_available_sessions_and_preserves_pruned_history(self):
        self.append(cumulative(100, 100), cumulative(100, 100))
        cx.ingest(self.conn, self.archive, 'now')
        # Recreate the old parser's duplicate and offset, plus a pruned session.
        row = dict(self.conn.execute('SELECT * FROM usage_event').fetchone())
        row['id'] = 'codex:s:2'
        db.upsert_usage(self.conn, [row])
        row.update(id='codex:pruned:1', session_id='pruned', input_tokens=9)
        db.upsert_usage(self.conn, [row])
        state = json.loads(db.get_state(self.conn, 'codex_offset:s.jsonl'))
        state.pop('accounting_version')
        db.set_state(self.conn, 'codex_offset:s.jsonl', json.dumps(state), 'now')
        self.conn.commit()
        self.assertEqual(self.tokens(), 209)
        # A failed rebuild must not commit the session deletion.
        with patch.object(cx, '_usage_row', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                cx.ingest(self.conn, self.archive, 'now')
        self.conn.rollback()
        self.assertEqual(self.tokens(), 209)
        cx.ingest(self.conn, self.archive, 'now')
        self.conn.commit()
        self.assertEqual(self.tokens(), 109)
        self.assertEqual(cx.ingest(self.conn, self.archive, 'now')['files_read'], 0)

    def test_single_file_batches_stay_bounded_for_both_sources_and_replays(self):
        for source in (cc, cx):
            with self.subTest(source=source.SOURCE):
                self.conn.execute('DELETE FROM usage_event')
                self.conn.execute('DELETE FROM ingest_state')
                records = [_assistant(i, 1, ts="2026-08-02T12:00:00Z") if source is cc else codex_usage(1)
                           for i in range(4501)]
                self.path.write_text(''.join(json.dumps(r) + '\n' for r in records))
                original = db.upsert_usage
                sizes = []

                def record_batch(conn, rows):
                    sizes.append(len(rows))
                    return original(conn, rows)

                with patch.object(db, 'upsert_usage', record_batch):
                    source.ingest(self.conn, self.archive, 'now')
                    # Force a replay with different contents, including Codex's delete.
                    self.path.write_text(self.path.read_text().replace('2026', '2025'))
                    source.ingest(self.conn, self.archive, 'now')
                self.assertLessEqual(max(sizes), 2000)
                self.assertEqual(self.tokens(), 4501)
