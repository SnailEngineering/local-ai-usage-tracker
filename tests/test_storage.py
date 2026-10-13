import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiusage import db, prune
from aiusage.sources import archiving, claude_code_local as cc, codex_local as cx
from tests.test_claude_code_local import _assistant
from tests.test_validation import codex_usage


class StorageTests(unittest.TestCase):
    def test_failed_copy_keeps_previous_archive_and_cleans_temporary_file(self):
        for source in (cc, cx):
            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                src = root / 'source'
                src.mkdir()
                path = src / 's.jsonl'
                path.write_text('old\n')
                archive = root / 'archive'
                source.archive(src, archive)
                path.write_text('replacement\n')

                def fail(reader, writer, **kwargs):
                    writer.write(b'partial')
                    raise OSError('simulated disk full')

                with patch.object(archiving.shutil, 'copyfileobj', fail):
                    with self.assertRaises(OSError):
                        source.archive(src, archive)
                self.assertEqual((archive / 's.jsonl').read_text(), 'old\n')
                self.assertEqual([p.name for p in archive.iterdir()], ['s.jsonl'])
                source.archive(src, archive)
                self.assertEqual((archive / 's.jsonl').read_text(), 'replacement\n')

    def test_private_database_and_archives_without_changing_shared_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            old_mask = os.umask(0o022)
            conn = None
            try:
                path = root / 'data' / 'usage.db'
                conn = db.connect(path)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(root.stat().st_mode & 0o777, 0o755)
                for file in path.parent.iterdir():
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                    file.chmod(0o644)
                # Existing database and sidecars are tightened on reconnect.
                other = db.connect(path)
                other.close()
                for file in path.parent.iterdir():
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                src = root / 'source'
                src.mkdir()
                (src / 's.jsonl').write_text('contents\n')
                (src / 's.jsonl').chmod(0o644)
                archive = root / 'archive'
                cc.archive(src, archive)
                self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
                copy = archive / 's.jsonl'
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
                copy.chmod(0o644)
                cc.archive(src, archive)  # unchanged copies are tightened too
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
                self.assertEqual((src / 's.jsonl').stat().st_mode & 0o777, 0o644)
            finally:
                if conn is not None:
                    conn.close()
                os.umask(old_mask)

    def test_pruned_sources_stay_pruned_until_contents_change(self):
        for source, folder in ((cc, 'projects'), (cx, 'sessions')):
            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                upstream = root / 'upstream'
                original = upstream / folder / 's.jsonl'
                original.parent.mkdir(parents=True)
                event = _assistant(1, 5) if source is cc else codex_usage(5)
                original.write_text(json.dumps(event) + '\n')
                stamp = time.time() - 90 * 86400
                os.utime(original, (stamp, stamp))
                archive = root / 'archive'
                conn = db.connect(root / 'usage.db')
                try:
                    source.run(conn, upstream, archive)
                    conn.commit()
                    candidates = prune.survey(conn, {source.SOURCE: archive}, 30)
                    self.assertEqual(prune.prune(conn, candidates)[0], 1)
                    # Reload the database to prove the decision survives processes.
                    conn.close()
                    conn = db.connect(root / 'usage.db')
                    for touch in (False, True):
                        if touch:
                            os.utime(original, None)
                        source.run(conn, upstream, archive)
                        conn.commit()
                        self.assertFalse((archive / 's.jsonl').exists())
                        mark = json.loads(db.get_state(conn, archiving.pruned_key(archive / 's.jsonl')))
                        self.assertEqual(mark['mtime_ns'], original.stat().st_mtime_ns)
                        self.assertEqual(conn.execute('SELECT SUM(input_tokens) FROM usage_event').fetchone()[0], 5)
                    with original.open('a') as fh:
                        fh.write(json.dumps(_assistant(2, 7) if source is cc else codex_usage(7)) + '\n')
                    source.run(conn, upstream, archive)
                    conn.commit()
                    self.assertTrue((archive / 's.jsonl').exists())
                    self.assertEqual(conn.execute('SELECT SUM(input_tokens) FROM usage_event').fetchone()[0], 12)
                    self.assertIsNone(db.get_state(conn, archiving.pruned_key(archive / 's.jsonl')))
                finally:
                    conn.close()
