"""Anthropic Usage & Cost Admin API.

Structurally the mirror image of the OpenAI source: daily buckets, group-by
dimensions, cursor pagination. Requires an Admin API key (`sk-ant-admin01-...`),
which Anthropic only issues to organizations -- individual accounts cannot
create one. If you have no org, use the claude_code_local source instead; it
lands in the same table with the same shape.

Endpoints:
  /v1/organizations/usage_report/messages     tokens by model
  /v1/organizations/cost_report               billed dollars
  /v1/organizations/usage_report/claude_code  per-user Claude Code, incl. subscriptions
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import db, http

SOURCE = "anthropic_admin"
PROVIDER = "anthropic"
BASE = "https://api.anthropic.com/v1/organizations"

# 1d buckets cap at 31 per page; pagination handles the rest.
MAX_DAILY_BUCKETS = 31


def _headers(admin_key: str) -> dict:
    return {"x-api-key": admin_key, "anthropic-version": "2023-06-01"}


def _local_day(ts_iso: str) -> str:
    return datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")


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
                start: datetime, end: datetime, now: str) -> int:
    params = {
        "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket_width": "1d",
        "limit": MAX_DAILY_BUCKETS,
        "group_by": ["model", "service_tier"],
    }

    rows: list[dict] = []
    for body in _paginate(f"{BASE}/usage_report/messages", params, _headers(admin_key)):
        for bucket in body.get("data") or []:
            bucket_start = bucket.get("starting_at")
            if not bucket_start:
                continue
            day = _local_day(bucket_start)
            for r in bucket.get("results") or []:
                model = r.get("model") or "unknown"
                tier = r.get("service_tier") or "standard"
                creation = r.get("cache_creation") or {}
                rows.append({
                    "id": f"anth:{bucket_start}:{model}:{tier}",
                    "source": SOURCE,
                    "provider": PROVIDER,
                    "ts": bucket_start,
                    "day": day,
                    "model": model,
                    "input_tokens": r.get("uncached_input_tokens", 0) or 0,
                    "output_tokens": r.get("output_tokens", 0) or 0,
                    "cache_write_5m_tokens": creation.get("ephemeral_5m_input_tokens", 0) or 0,
                    "cache_write_1h_tokens": creation.get("ephemeral_1h_input_tokens", 0) or 0,
                    "cache_read_tokens": r.get("cache_read_input_tokens", 0) or 0,
                    "reasoning_tokens": 0,
                    "requests": 0,  # this endpoint reports tokens, not request counts
                    "project": None,
                    "git_branch": None,
                    "session_id": None,
                    "service_tier": tier,
                    "reported_cost_usd": None,
                    "ingested_at": now,
                })

    return db.upsert_usage(conn, rows)


def fetch_costs(conn: sqlite3.Connection, admin_key: str,
                start: datetime, end: datetime, now: str) -> int:
    """Real billed dollars. Reported in cents as decimal strings."""
    params = {
        "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group_by": ["description"],
    }

    rows: list[dict] = []
    for body in _paginate(f"{BASE}/cost_report", params, _headers(admin_key)):
        for bucket in body.get("data") or []:
            bucket_start = bucket.get("starting_at")
            if not bucket_start:
                continue
            day = _local_day(bucket_start)
            for i, r in enumerate(bucket.get("results") or []):
                amount = r.get("amount")
                if amount is None:
                    continue
                desc = r.get("description") or r.get("model") or f"item{i}"
                rows.append({
                    "id": f"anthcost:{bucket_start}:{desc}",
                    "provider": PROVIDER,
                    "day": day,
                    "line_item": desc,
                    "project_id": r.get("workspace_id"),
                    # Documented as "decimal strings in lowest units (cents)".
                    "amount_usd": float(amount) / 100.0,
                    "ingested_at": now,
                })

    return db.upsert_cost(conn, rows)


def fetch_claude_code(conn: sqlite3.Connection, admin_key: str,
                      start: datetime, end: datetime, now: str) -> int:
    """Per-user Claude Code analytics -- the only Anthropic endpoint that covers
    *subscription* (Pro/Max/Team) usage, not just metered API keys. One request
    per day; `starting_at` is a single YYYY-MM-DD, not a range."""
    rows: list[dict] = []
    day_cursor = start.date()
    end_date = end.date()

    while day_cursor < end_date:
        iso_day = day_cursor.isoformat()
        page = None
        while True:
            body = http.get_json(
                f"{BASE}/usage_report/claude_code",
                {"starting_at": iso_day, "limit": 1000, "page": page},
                _headers(admin_key),
            )
            for rec in body.get("data") or []:
                actor = rec.get("actor") or {}
                who = actor.get("email_address") or actor.get("api_key_name") or "unknown"
                for mb in rec.get("model_breakdown") or []:
                    model = mb.get("model") or "unknown"
                    tok = mb.get("tokens") or {}
                    est = mb.get("estimated_cost") or {}
                    amount = est.get("amount")
                    rows.append({
                        "id": f"anthcc:{iso_day}:{who}:{model}",
                        "source": "anthropic_claude_code",
                        "provider": PROVIDER,
                        "ts": f"{iso_day}T00:00:00Z",
                        "day": iso_day,
                        "model": model,
                        "input_tokens": tok.get("input", 0) or 0,
                        "output_tokens": tok.get("output", 0) or 0,
                        # This endpoint reports one cache_creation figure with no
                        # TTL split; attribute to the 5m tier (the default).
                        "cache_write_5m_tokens": tok.get("cache_creation", 0) or 0,
                        "cache_write_1h_tokens": 0,
                        "cache_read_tokens": tok.get("cache_read", 0) or 0,
                        "reasoning_tokens": 0,
                        "requests": (rec.get("core_metrics") or {}).get("num_sessions", 0) or 0,
                        "project": None,
                        "git_branch": None,
                        "session_id": None,
                        "service_tier": rec.get("customer_type"),
                        # Reported in cents USD.
                        "reported_cost_usd": (float(amount) / 100.0) if amount is not None else None,
                        "ingested_at": now,
                    })
            if not body.get("has_more"):
                break
            page = body.get("next_page")
            if not page:
                break
        day_cursor += timedelta(days=1)

    return db.upsert_usage(conn, rows)


def run(conn: sqlite3.Connection, admin_key: str, backfill_days: int,
        *, include_claude_code: bool = True) -> dict:
    """Re-read a trailing window each time. Buckets can be restated for a day or
    two after the fact, and INSERT OR REPLACE makes overlap free."""
    now = datetime.now(timezone.utc).isoformat()
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(days=1)

    first_run = db.get_state(conn, "anthropic_admin:last_run") is None
    window = backfill_days if first_run else 7
    start = end - timedelta(days=window)

    stats = {
        "window_days": window,
        "usage_rows": fetch_usage(conn, admin_key, start, end, now),
        "cost_rows": fetch_costs(conn, admin_key, start, end, now),
    }
    if include_claude_code:
        stats["claude_code_rows"] = fetch_claude_code(conn, admin_key, start, end, now)

    db.set_state(conn, "anthropic_admin:last_run", now, now)
    return stats
