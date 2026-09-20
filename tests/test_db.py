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


class MigrationTests(unittest.TestCase):
    def test_a_fresh_database_is_stamped_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "usage.db")
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             db.SCHEMA_VERSION)

    def test_migrations_run_once_and_are_idempotent(self) -> None:
        """A database from before versioning reports user_version 0, so every
        step has to run against it -- and must not run a second time."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            conn = db.connect(path)
            self.addCleanup(conn.close)

            conn.execute("PRAGMA user_version = 0")   # pretend it predates this
            conn.commit()
            self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
            self.assertEqual(db.migrate(conn), 0)     # already current
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             db.SCHEMA_VERSION)

    def test_every_version_up_to_current_has_a_migration_entry(self) -> None:
        """Guards the bump-without-a-step mistake: raising SCHEMA_VERSION and
        forgetting MIGRATIONS leaves existing databases quietly unchanged."""
        for version in range(2, db.SCHEMA_VERSION + 1):
            self.assertIn(version, db.MIGRATIONS,
                          f"SCHEMA_VERSION is {db.SCHEMA_VERSION} but no "
                          f"MIGRATIONS entry exists for version {version}")

    def test_project_path_arrives_on_an_old_database_and_reingest_is_forced(self) -> None:
        """v2 databases have no project_path, and the only place the path still
        exists is the archived session files. The migration has to add the column
        and forget the read offsets so the next run replays them and fills it --
        without touching any other bookkeeping."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            raw = sqlite3.connect(path)
            raw.executescript("""
                CREATE TABLE usage_event (
                  id TEXT PRIMARY KEY, source TEXT NOT NULL, provider TEXT NOT NULL,
                  ts TEXT NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL,
                  project TEXT, ingested_at TEXT NOT NULL);
                INSERT INTO usage_event
                  VALUES ('a', 's', 'p', 't', '2026-08-01', 'm', 'backend', 't');
                CREATE TABLE ingest_state (key TEXT PRIMARY KEY, value TEXT NOT NULL,
                                           updated_at TEXT NOT NULL);
                INSERT INTO ingest_state VALUES ('cc_offset:s.jsonl', '{}', 't');
                INSERT INTO ingest_state VALUES ('codex_offset:c.jsonl', '{}', 't');
                INSERT INTO ingest_state VALUES ('something_else', 'keep', 't');
                PRAGMA user_version = 2;
            """)
            raw.commit()
            raw.close()

            conn = db.connect(path)
            self.addCleanup(conn.close)

            self.assertEqual(
                conn.execute("SELECT project, project_path FROM usage_event").fetchone()[:],
                ("backend", None))
            self.assertEqual(
                [r["key"] for r in conn.execute("SELECT key FROM ingest_state")],
                ["something_else"])


class RunLogTests(unittest.TestCase):
    def test_serve_runs_are_hidden_unless_they_failed(self) -> None:
        """`--serve` re-collects per request, so an open tab writes two rows a
        minute. The health views must still surface the scheduled runs they
        exist for -- while never hiding a failure, whatever triggered it."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "usage.db")
            self.addCleanup(conn.close)

            db.log_run(conn, "claude_code_local", "ok", "", "t", "t")
            for _ in range(50):  # a serve session's worth of noise
                db.log_run(conn, "claude_code_local", "ok", "", "t", "t", "serve")
            db.log_run(conn, "codex_local", "error", "boom", "t", "t", "serve")
            conn.commit()

            visible = conn.execute(
                "SELECT source, status, triggered_by FROM run_log "
                "WHERE triggered_by = 'scheduled' OR status = 'error' "
                "ORDER BY id").fetchall()

            self.assertEqual([(r["source"], r["status"], r["triggered_by"]) for r in visible],
                             [("claude_code_local", "ok", "scheduled"),
                              ("codex_local", "error", "serve")])

    def test_log_run_defaults_to_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "usage.db")
            self.addCleanup(conn.close)
            db.log_run(conn, "claude_code_local", "ok", "", "t", "t")
            self.assertEqual(
                conn.execute("SELECT triggered_by FROM run_log").fetchone()[0],
                "scheduled")

    def test_an_existing_database_gains_the_column(self) -> None:
        """The pre-versioning shape: run_log without triggered_by, user_version
        still 0. Migration has to add the column and keep the rows."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            raw = sqlite3.connect(path)
            raw.executescript("""
                CREATE TABLE run_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                  finished_at TEXT, source TEXT NOT NULL, status TEXT NOT NULL,
                  detail TEXT);
                INSERT INTO run_log (started_at, source, status)
                  VALUES ('t', 'claude_code_local', 'ok');
            """)
            raw.commit()
            raw.close()

            conn = db.connect(path)
            self.addCleanup(conn.close)

            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             db.SCHEMA_VERSION)
            row = conn.execute("SELECT source, triggered_by FROM run_log").fetchone()
            self.assertEqual((row["source"], row["triggered_by"]),
                             ("claude_code_local", "scheduled"))


if __name__ == "__main__":
    unittest.main()
