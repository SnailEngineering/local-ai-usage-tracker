"""OpenAI Usage & Costs API.

Requires an admin key (`sk-admin-...`) created by an org owner under
Settings -> Organization -> Admin keys. Ordinary project keys (`sk-proj-...`)
return 401 here.

Unlike Anthropic, this one backfills: the endpoints serve historical buckets, so
nothing is lost by starting to collect today.

Endpoints:
  /v1/organization/usage/completions  tokens by model
  /v1/organization/costs              billed dollars
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import db, http

SOURCE = "openai_admin"
PROVIDER = "openai"
BASE = "https://api.openai.com/v1/organization"


def _headers(admin_key: str) -> dict:
    return {"Authorization": f"Bearer {admin_key}"}


def _local_day(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _paginate(url: str, params: dict, headers: dict):
    page = None
    while True:
        body = http.get_json(url, {**params, "page": page}, headers)
        yield body
        if not body.get("has_more"):
            return
        page = body.get("next_page")
        if not page:
            return


def fetch_usage(conn: sqlite3.Connection, admin_key: str,
                start: datetime, now: str) -> int:
    params = {
        "start_time": int(start.timestamp()),
        "bucket_width": "1d",
        "limit": 31,
        "group_by": ["model", "project_id"],
    }

    rows: list[dict] = []
    for body in _paginate(f"{BASE}/usage/completions", params, _headers(admin_key)):
        for bucket in body.get("data") or []:
            bucket_start = bucket.get("start_time")
            if bucket_start is None:
                continue
            day = _local_day(bucket_start)
            for r in bucket.get("results") or []:
                model = r.get("model") or "unknown"
                project = r.get("project_id") or "-"

                # OpenAI's `input_tokens` is the full input including the cached
                # portion; Anthropic's excludes it. Subtract so the two providers
                # mean the same thing in our table.
                total_in = r.get("input_tokens", 0) or 0
                cached_in = r.get("input_cached_tokens", 0) or 0

                rows.append({
                    "id": f"oai:{bucket_start}:{model}:{project}",
                    "source": SOURCE,
                    "provider": PROVIDER,
                    "ts": datetime.fromtimestamp(bucket_start, tz=timezone.utc).isoformat(),
                    "day": day,
                    "model": model,
                    "input_tokens": max(total_in - cached_in, 0),
                    "output_tokens": r.get("output_tokens", 0) or 0,
                    "cache_write_5m_tokens": 0,  # OpenAI caching is implicit; no write charge
                    "cache_write_1h_tokens": 0,
                    "cache_read_tokens": cached_in,
                    "reasoning_tokens": 0,
                    "requests": r.get("num_model_requests", 0) or 0,
                    "project": r.get("project_id"),
                    "git_branch": None,
                    "session_id": None,
                    "service_tier": None,
                    # Priced from /costs below, not from a local rate table.
                    "reported_cost_usd": None,
                    "ingested_at": now,
                })

    return db.upsert_usage(conn, rows)


def fetch_costs(conn: sqlite3.Connection, admin_key: str,
                start: datetime, now: str) -> int:
    params = {
        "start_time": int(start.timestamp()),
        "bucket_width": "1d",
        "limit": 180,
        "group_by": ["line_item", "project_id"],
    }

    rows: list[dict] = []
    for body in _paginate(f"{BASE}/costs", params, _headers(admin_key)):
        for bucket in body.get("data") or []:
            bucket_start = bucket.get("start_time")
            if bucket_start is None:
                continue
            day = _local_day(bucket_start)
            for i, r in enumerate(bucket.get("results") or []):
                amount = (r.get("amount") or {})
                value = amount.get("value")
                if value is None:
                    continue
                line_item = r.get("line_item") or f"item{i}"
                project = r.get("project_id") or "-"
                rows.append({
                    "id": f"oaicost:{bucket_start}:{line_item}:{project}",
                    "provider": PROVIDER,
                    "day": day,
                    "line_item": line_item,
                    "project_id": r.get("project_id"),
                    "amount_usd": float(value),  # already USD, not cents
                    "ingested_at": now,
                })

    return db.upsert_cost(conn, rows)


def run(conn: sqlite3.Connection, admin_key: str, backfill_days: int) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    first_run = db.get_state(conn, "openai_admin:last_run") is None
    window = backfill_days if first_run else 7
    start = (datetime.now(timezone.utc) - timedelta(days=window)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    stats = {
        "window_days": window,
        "usage_rows": fetch_usage(conn, admin_key, start, now),
        "cost_rows": fetch_costs(conn, admin_key, start, now),
    }
    db.set_state(conn, "openai_admin:last_run", now, now)
    return stats
