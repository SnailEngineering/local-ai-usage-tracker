from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from aiusage import dashboard, db, server
from tests.test_dashboard import _event


class ServerTests(unittest.TestCase):
    def _serve(self, collect_fn, min_interval: float = 0.0,
               refresh_seconds: int = dashboard.DEFAULT_REFRESH_SECONDS,
               dashboard_path: Path | None = None):
        conn = db.connect(Path(tempfile.mkdtemp()) / "usage.db", check_same_thread=False)
        self.addCleanup(conn.close)
        self.conn = conn
        httpd = server.make_server(conn, collect_fn, "127.0.0.1", 0,
                                   min_collect_interval=min_interval,
                                   refresh_seconds=refresh_seconds,
                                   dashboard_path=dashboard_path)
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _written(self, path: Path, needle: str, timeout: float = 5.0) -> str:
        """Wait for the write-through to land. It runs after the response is
        flushed -- the client is back before the handler thread gets there --
        so a bare read races it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = path.read_text()
            except FileNotFoundError:
                text = ""
            if needle in text:
                return text
            time.sleep(0.02)
        self.fail(f"{needle!r} never reached {path} within {timeout}s")

    def _minute(self) -> str:
        """generated_at's format, as dashboard.build_payload writes it."""
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    def _expect_error(self, url: str, status: int) -> bytes:
        """Request `url` expecting `status`. The server prints a traceback for
        any 500 -- correct when you are running --serve and want to see the
        fault, just noise here -- so stderr is captured for the duration."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url, timeout=10)
        error = caught.exception
        self.addCleanup(error.close)
        self.assertEqual(error.code, status)
        return error.read()

    def test_a_failing_collector_answers_500_instead_of_dropping(self) -> None:
        """The page's fallback for "no answer" is location.reload(). If a
        broken collector closed the socket instead of replying, the tab would
        reload itself every refresh interval for as long as the fault lasted,
        and the reload would re-run the same broken collection."""
        def boom():
            raise RuntimeError("collector exploded")

        base = self._serve(boom)
        body = json.loads(self._expect_error(base + "/api/data", 500))
        self.assertIn("collector exploded", body["error"])

        # ...and the server is still answering afterwards.
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            self.assertEqual(res.status, 200)

    def test_a_healthy_request_returns_the_payload_uncached(self) -> None:
        base = self._serve(lambda: None)
        with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
            self.assertEqual(res.status, 200)
            self.assertEqual(res.headers["Cache-Control"], "no-store")
            payload = json.loads(res.read())
        for key in ("totals", "days", "months", "projects", "runs"):
            self.assertIn(key, payload)

    def test_an_unknown_path_is_a_404_not_a_crash(self) -> None:
        base = self._serve(lambda: None)
        self._expect_error(base + "/nope", 404)

    def test_a_page_load_is_rendered_not_the_startup_snapshot(self) -> None:
        """--serve writes dashboard.html once at startup. Serving that file
        as-is meant every browser reload, days into a session, showed the
        startup numbers until the next /api/data poll replaced them."""
        base = self._serve(lambda: None)

        # Land a row *after* the server is up. A startup snapshot cannot
        # contain it; a page rendered per request must.
        db.upsert_usage(self.conn, [_event(project="landed-after-startup")])
        self.conn.commit()

        before = self._minute()
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            self.assertEqual(res.headers["Cache-Control"], "no-store")
            body = res.read().decode()
        self.assertIn("<title>AI Usage</title>", body)
        self.assertIn("landed-after-startup", body)
        # ...and stamped when the request was served, not at process start.
        # Two candidates because the request can straddle a minute boundary.
        self.assertTrue(
            any(f'"generated_at":"{m}"' in body for m in (before, self._minute())),
            "served page was not stamped at request time")

    def test_a_failing_render_answers_500_instead_of_dropping(self) -> None:
        """Same invariant as the failing collector: every branch of do_GET
        must answer. A dropped socket is indistinguishable from "no server",
        and the page's fallback for that is location.reload()."""
        base = self._serve(lambda: None)
        with mock.patch.object(dashboard, "render", side_effect=RuntimeError("render exploded")):
            body = json.loads(self._expect_error(base + "/", 500))
        self.assertIn("render exploded", body["error"])

        # ...and the server recovers once rendering works again.
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            self.assertEqual(res.status, 200)

    def test_a_page_load_writes_the_dashboard_file_through(self) -> None:
        """--serve leaves dashboard.html on disk. Without a write-through it
        keeps whatever the startup build wrote, so opening the file after the
        server stops shows numbers from the start of the session."""
        out = Path(tempfile.mkdtemp()) / "dashboard.html"
        out.write_text("<!doctype html><title>startup snapshot</title>")
        base = self._serve(lambda: None, dashboard_path=out)

        db.upsert_usage(self.conn, [_event(project="landed-after-startup")])
        self.conn.commit()
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            body = res.read().decode()

        self.assertEqual(self._written(out, "landed-after-startup"), body)

    def test_a_poll_writes_the_dashboard_file_through(self) -> None:
        """The poll, not the page load, is what picks up new data over a long
        session -- a tab left open for hours never reloads, so if only / wrote
        the file it would sit as stale as the last page load."""
        out = Path(tempfile.mkdtemp()) / "dashboard.html"
        base = self._serve(lambda: None, dashboard_path=out)

        db.upsert_usage(self.conn, [_event(project="arrived-during-a-poll")])
        self.conn.commit()
        with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
            self.assertEqual(res.status, 200)

        self.assertIn("<title>AI Usage</title>",
                      self._written(out, "arrived-during-a-poll"))

    def _raw_get(self, base: str, path: str) -> bytes:
        """Everything the server puts on the socket, not just the bytes
        Content-Length says to read -- urllib stops at the declared length and
        would not notice a second response tacked on behind the first."""
        host, port = base.removeprefix("http://").split(":")
        with socket.create_connection((host, int(port)), timeout=10) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def test_a_failed_write_through_cannot_disturb_the_answered_request(self) -> None:
        """The write-through is a convenience for the file on disk, and it
        runs after the response is flushed. Letting it raise would send do_GET
        into its 500 handler with a complete response already on the wire,
        appending a second set of status line and headers to the page the
        browser is mid-parse of."""
        out = Path(tempfile.mkdtemp()) / "nested" / "dashboard.html"
        base = self._serve(lambda: None, dashboard_path=out)

        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(dashboard, "write_page",
                                   side_effect=OSError("read-only file system")):
                for path in ("/", "/api/data"):
                    raw = self._raw_get(base, path)
                    self.assertTrue(raw.startswith(b"HTTP/1.0 200 OK"), path)
                    self.assertEqual(raw.count(b"HTTP/1.0 "), 1,
                                     f"{path}: a second response followed the first")

    def test_write_through_is_off_when_no_path_is_given(self) -> None:
        base = self._serve(lambda: None, dashboard_path=None)
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            self.assertEqual(res.status, 200)

    def test_the_refresh_interval_reaches_the_served_page(self) -> None:
        """--interval is plumbed collect.py -> make_server -> the template's
        setInterval; nothing else proves the served page honours it."""
        base = self._serve(lambda: None, refresh_seconds=7)
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            self.assertIn("const REFRESH_MS = 7000;", res.read().decode())

    def test_rapid_refreshes_coalesce_into_one_collection(self) -> None:
        """Each open tab polls on its own timer, so without a floor N tabs mean
        N full collections per interval -- every one of them walking the whole
        archive while holding the lock the others are queued on."""
        calls = []
        base = self._serve(lambda: calls.append(1), min_interval=30.0)

        for _ in range(5):
            with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
                self.assertEqual(res.status, 200)

        self.assertEqual(len(calls), 1)

    def test_the_first_request_always_collects(self) -> None:
        """The throttle must not suppress the very first collection. Whether
        time.monotonic() counts from boot or from process start is
        unspecified and differs between CPython builds on macOS, so "never
        collected" cannot be spelled as a zero stamp."""
        calls = []
        base = self._serve(lambda: calls.append(1), min_interval=3600.0)
        with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
            self.assertEqual(res.status, 200)
        self.assertEqual(len(calls), 1)

    def test_every_request_still_returns_fresh_payload_when_throttled(self) -> None:
        """Skipping the collection must not skip the answer: the page still
        needs a payload, just one built from the database as it stands."""
        base = self._serve(lambda: None, min_interval=30.0)
        for _ in range(3):
            with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
                self.assertIn("totals", json.loads(res.read()))


if __name__ == "__main__":
    unittest.main()
