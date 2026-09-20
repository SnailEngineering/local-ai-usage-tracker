from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import collect
from aiusage import db, prune
from aiusage.locking import archive_locks
from tests.test_claude_code_local import _assistant


class LockingTests(unittest.TestCase):
    def test_collection_holds_the_archive_lock_through_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive"
            conn = db.connect(root / "usage.db")
            checks = []

            def assert_locked():
                # A separate process must not acquire the lock while either
                # the source or its database commit is still running.
                result = subprocess.run([sys.executable, "-c", """
import fcntl, sys
with open(sys.argv[1], 'a+b') as fh:
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(1)
""", str(archive / ".aiusage.lock")], timeout=10)
                self.assertEqual(result.returncode, 0)
                checks.append(True)

            class Connection:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def commit(self):
                    if db.get_state(conn, "pending") == "yes":
                        assert_locked()
                        conn.execute("DELETE FROM ingest_state WHERE key = 'pending'")
                    conn.commit()

            def source(*args):
                assert_locked()
                db.set_state(conn, "pending", "yes", "now")
                return {}

            try:
                with patch.object(collect.claude_code_local, "run", source):
                    failures = collect.run_sources(Connection(), root, root, archive,
                                                   root / "codex", only="claude_code", quiet=True)
                self.assertEqual(failures, 0)
                self.assertEqual(len(checks), 2)
                with (archive / ".aiusage.lock").open("a+b") as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                conn.close()

    def test_prune_waits_for_collector_then_keeps_newly_appended_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "claude" / "projects"
            source.mkdir(parents=True)
            path = source / "s.jsonl"
            path.write_text(json.dumps(_assistant(1, 5)) + "\n")
            old = time.time() - 90 * 86400
            os.utime(path, (old, old))
            archive = root / "archive"
            conn = db.connect(root / "usage.db", check_same_thread=False)
            child = None
            worker = None
            results, errors = [], []
            try:
                collect.run_sources(conn, root / "claude", root / "codex", archive,
                                    root / "archive-codex", only="claude_code", quiet=True)
                candidates = prune.survey(conn, {"claude_code_local": archive}, 30)
                self.assertTrue(candidates[0].prunable)
                child = subprocess.Popen([sys.executable, "-u", "-c", """
import json, sys
from pathlib import Path
from unittest.mock import patch
import collect
from aiusage import db
from tests.test_claude_code_local import _assistant
root = Path(sys.argv[1])
conn = db.connect(root / 'usage.db')
original = collect.claude_code_local.run
def held(*args):
    print('locked', flush=True)
    sys.stdin.readline()
    with (root / 'claude/projects/s.jsonl').open('a') as fh:
        fh.write(json.dumps(_assistant(2, 7)) + '\\n')
    return original(*args)
with patch.object(collect.claude_code_local, 'run', held):
    failures = collect.run_sources(conn, root / 'claude', root / 'codex',
        root / 'archive', root / 'archive-codex', only='claude_code', quiet=True)
conn.close()
sys.exit(failures)
""", str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                self.assertTrue(select.select([child.stdout], [], [], 10)[0], "collector did not start")
                self.assertEqual(child.stdout.readline().strip(), "locked")
                attempting = threading.Event()

                @contextmanager
                def observed_lock(*directories):
                    attempting.set()
                    with archive_locks(*directories):
                        yield

                def delete():
                    try:
                        results.append(prune.prune(conn, candidates))
                    except BaseException as exc:
                        errors.append(exc)

                with patch.object(prune, "archive_locks", observed_lock):
                    worker = threading.Thread(target=delete)
                    worker.start()
                    self.assertTrue(attempting.wait(10))
                    # Collection still holds the lock. Let it finish and commit
                    # before prune can revalidate its stale survey.
                    _, stderr = child.communicate("continue\n", timeout=10)
                    self.assertEqual(child.returncode, 0, stderr)
                    worker.join(10)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [(0, 0)])
                self.assertTrue((archive / "s.jsonl").exists())
                self.assertEqual(conn.execute("SELECT SUM(input_tokens) FROM usage_event").fetchone()[0], 12)
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.communicate()
                if worker is not None:
                    worker.join(10)
                conn.close()
