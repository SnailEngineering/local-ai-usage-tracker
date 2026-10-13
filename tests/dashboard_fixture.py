"""Synthetic payload shared with dependency-free JavaScript behavior tests."""
import json
import tempfile
from pathlib import Path

from aiusage import dashboard, db
from tests.test_dashboard import _event


def payload():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / 'usage.db')
        try:
            db.upsert_usage(conn, [
                _event(id='a', day='2026-07-01', input_tokens=1234567),
                _event(id='b', day='2026-08-01', input_tokens=1234),
                _event(id='c', day='2026-08-02', input_tokens=5678),
            ])
            return dashboard.build_payload(conn)
        finally:
            conn.close()


if __name__ == '__main__':
    print(json.dumps(payload()))
