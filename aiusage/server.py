"""Optional local dev server for `collect.py --serve`.

Not part of the default pipeline -- that stays static, per dashboard.py's own
docstring. This just gives an already-open tab a way to pull fresh data
without a human re-running ./collect.py: it serves the same dashboard.html
collect.py just wrote, plus a /api/data endpoint that re-runs the collectors
and returns the JSON payload dashboard.py would otherwise bake into the file.
The page's own JS (see dashboard.py's TEMPLATE) polls that endpoint and
re-renders in place.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import dashboard


def make_server(
    conn: sqlite3.Connection,
    collect_fn: Callable[[], None],
    dashboard_path: Path,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    # Collection and the payload query both touch `conn`; a lock keeps
    # concurrent requests (e.g. a stray double-click on Refresh) from
    # interleaving writes and reads on the one connection.
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
            pass

        def _send_json(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - stdlib signature
            if self.path == "/api/data":
                with lock:
                    collect_fn()
                    payload = dashboard.build_payload(conn)
                self._send_json(payload)
                return
            if self.path in ("/", "/dashboard.html"):
                with lock:
                    self._send_file(dashboard_path, "text/html; charset=utf-8")
                return
            self.send_error(404)

    return ThreadingHTTPServer((host, port), Handler)
