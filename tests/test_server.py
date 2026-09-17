from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
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
               refresh_seconds: int = dashboard.DEFAULT_REFRESH_SECONDS):
        conn = db.connect(Path(tempfile.mkdtemp()) / "usage.db", check_same_thread=False)
        self.addCleanup(conn.close)
        self.conn = conn
        httpd = server.make_server(conn, collect_fn, "127.0.0.1", 0,
                                   min_collect_interval=min_interval,
                                   refresh_seconds=refresh_seconds)
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

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
