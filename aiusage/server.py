"""Optional local dev server for `collect.py --serve`.

Not part of the default pipeline -- that stays static, per dashboard.py's own
docstring. This just gives an already-open tab a way to pull fresh data
without a human re-running ./collect.py: it renders the dashboard page from
the database on every request -- a --serve process left running for days
would otherwise hand each new tab the snapshot it wrote at startup -- plus a
/api/data endpoint that re-runs the collectors and returns the JSON payload
dashboard.py would otherwise bake into the file.

The page's own JS (see dashboard.py's TEMPLATE) polls that endpoint and
re-renders in place.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import dashboard


# Shortest gap between two collections, however many tabs are asking. The page
# polls on its own timer, so N open tabs otherwise means N collections per
# interval -- each one walking every archived file while holding the lock that
# every other request is waiting on.
MIN_COLLECT_INTERVAL_S = 20.0


def make_server(
    conn: sqlite3.Connection,
    collect_fn: Callable[[], int],   # returns the failure count, like run_sources
    host: str,
    port: int,
    min_collect_interval: float = MIN_COLLECT_INTERVAL_S,
    refresh_seconds: int = dashboard.DEFAULT_REFRESH_SECONDS,
) -> ThreadingHTTPServer:
    # Collection and the payload query both touch `conn`; a lock keeps
    # concurrent requests (e.g. a stray double-click on Refresh) from
    # interleaving writes and reads on the one connection.
    lock = threading.Lock()
    # Monotonic stamp of the last collection, boxed so the closure can rebind
    # it. None means "never", which is not the same as 0.0: time.monotonic()'s
    # epoch is not specified, and on macOS it is time since boot under some
    # builds and time since process start under others -- a 0.0 sentinel
    # silently suppresses the very first collection on the latter.
    last_collect: list[float | None] = [None]

    def collect_if_due() -> None:
        """Collect unless one just ran. Held under `lock`, so a burst of
        refreshes coalesces into one collection and the rest simply read the
        database it just wrote."""
        previous = last_collect[0]
        if previous is not None and time.monotonic() - previous < min_collect_interval:
            return
        collect_fn()
        last_collect[0] = time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
            pass

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # The page polls this on a timer; a cached reply would show stale
            # numbers that look like a collector that has stopped working.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Same reason as the JSON: a cached page would show the startup
            # numbers again on the next reload, which is the bug this path
            # exists to avoid.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - stdlib signature
            # Every branch must answer. An exception escaping here closes the
            # socket with no response, which the page cannot tell apart from
            # "no server on this port" -- so its fallback fires and reloads the
            # tab, every refresh interval, for as long as the fault lasts.
            try:
                if self.path == "/api/data":
                    with lock:
                        collect_if_due()
                        payload = dashboard.build_payload(conn)
                    self._send_json(payload)
                    return
                if self.path in ("/", "/dashboard.html"):
                    # Render from the database rather than serving the file
                    # written at startup: a long-running --serve would
                    # otherwise hand every browser reload that old snapshot,
                    # and the tab would show days-stale numbers until its next
                    # /api/data poll replaced them. Rendered in memory, never
                    # through the dashboard file -- a page load should not depend
                    # on that file being writable, nor race the scheduled
                    # collect.py writing it. Not collecting here keeps page
                    # loads fast; the poll does that.
                    with lock:
                        html = dashboard.render(conn, refresh_seconds)
                    self._send_html(html)
                    return
                self.send_error(404)
            except Exception as e:
                traceback.print_exc()
                try:
                    self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
                except Exception:  # client hung up mid-write; nothing to say
                    pass

    return ThreadingHTTPServer((host, port), Handler)
