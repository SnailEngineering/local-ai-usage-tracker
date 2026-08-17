from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from aiusage import db, server


class ServerTests(unittest.TestCase):
    def _serve(self, collect_fn, dashboard_path: Path, min_interval: float = 0.0):
        conn = db.connect(Path(tempfile.mkdtemp()) / "usage.db", check_same_thread=False)
        self.addCleanup(conn.close)
        httpd = server.make_server(conn, collect_fn, dashboard_path, "127.0.0.1", 0,
                                   min_collect_interval=min_interval)
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

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

    def _dashboard(self) -> Path:
        path = Path(tempfile.mkdtemp()) / "dashboard.html"
        path.write_text("<!doctype html><title>x</title>")
        return path

    def test_a_failing_collector_answers_500_instead_of_dropping(self) -> None:
        """The page's fallback for "no answer" is location.reload(). If a
        broken collector closed the socket instead of replying, the tab would
        reload itself every refresh interval for as long as the fault lasted,
        and the reload would re-run the same broken collection."""
        def boom():
            raise RuntimeError("collector exploded")

        base = self._serve(boom, self._dashboard())
        body = json.loads(self._expect_error(base + "/api/data", 500))
        self.assertIn("collector exploded", body["error"])

        # ...and the server is still answering afterwards.
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            self.assertEqual(res.status, 200)

    def test_a_healthy_request_returns_the_payload_uncached(self) -> None:
        base = self._serve(lambda: None, self._dashboard())
        with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
            self.assertEqual(res.status, 200)
            self.assertEqual(res.headers["Cache-Control"], "no-store")
            payload = json.loads(res.read())
        for key in ("totals", "days", "months", "projects", "runs"):
            self.assertIn(key, payload)

    def test_an_unknown_path_is_a_404_not_a_crash(self) -> None:
        base = self._serve(lambda: None, self._dashboard())
        self._expect_error(base + "/nope", 404)

    def test_a_missing_dashboard_file_answers_500(self) -> None:
        """_send_file reads from disk; the file can be deleted underneath it."""
        base = self._serve(lambda: None, Path(tempfile.mkdtemp()) / "gone.html")
        self._expect_error(base + "/", 500)

    def test_rapid_refreshes_coalesce_into_one_collection(self) -> None:
        """Each open tab polls on its own timer, so without a floor N tabs mean
        N full collections per interval -- every one of them walking the whole
        archive while holding the lock the others are queued on."""
        calls = []
        base = self._serve(lambda: calls.append(1), self._dashboard(),
                           min_interval=30.0)

        for _ in range(5):
            with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
                self.assertEqual(res.status, 200)

        self.assertEqual(len(calls), 1)

    def test_every_request_still_returns_fresh_payload_when_throttled(self) -> None:
        """Skipping the collection must not skip the answer: the page still
        needs a payload, just one built from the database as it stands."""
        base = self._serve(lambda: None, self._dashboard(), min_interval=30.0)
        for _ in range(3):
            with urllib.request.urlopen(base + "/api/data", timeout=10) as res:
                self.assertIn("totals", json.loads(res.read()))


if __name__ == "__main__":
    unittest.main()
