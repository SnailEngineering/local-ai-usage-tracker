from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from aiusage import db


class BusyTimeoutTests(unittest.TestCase):
    def test_a_concurrent_writer_waits_for_the_configured_timeout(self) -> None:
        """launchd, a manual run and `--serve` all write this database. sqlite3
        defaults to a 5s wait, which a full re-ingest can exceed while holding
        the write lock; the loser then raises 'database is locked' and
        run_sources logs an error and skips that run's usage entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            # check_same_thread=False so the timer below can release the lock;
            # the holder is only ever touched from one thread at a time.
            holder = db.connect(path, check_same_thread=False)
            self.addCleanup(holder.close)
            other = db.connect(path)
            self.addCleanup(other.close)

            self.assertEqual(other.execute("PRAGMA busy_timeout").fetchone()[0],
                             int(db.BUSY_TIMEOUT_S * 1000))

            # Hold a write transaction open, then release it from a timer.
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "INSERT INTO ingest_state (key, value, updated_at) VALUES ('a','1','t')")
            threading.Timer(0.15, holder.commit).start()

            try:
                other.execute(
                    "INSERT INTO ingest_state (key, value, updated_at) VALUES ('b','2','t')")
                other.commit()
            except sqlite3.OperationalError as e:  # pragma: no cover
                self.fail(f"concurrent writer was not given time to wait: {e}")

            self.assertEqual(
                other.execute("SELECT COUNT(*) c FROM ingest_state").fetchone()["c"], 2)


if __name__ == "__main__":
    unittest.main()
