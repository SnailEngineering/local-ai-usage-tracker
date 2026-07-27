#!/usr/bin/env python3
"""local-ai-usage-tracker collector.

Run once or twice a day. Every source is optional and independently skippable,
so a missing OpenAI key never blocks the Claude Code archive from running.

    ./collect.py                 # collect everything configured, rebuild dashboard
    ./collect.py --no-dashboard  # collect only
    ./collect.py --status        # what's in the database right now
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiusage import db  # noqa: E402
from aiusage.sources import (  # noqa: E402
    anthropic_admin, claude_code_local, codex_local, openai_admin,
)

ROOT = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Minimal .env reader. Real environment variables always win, so you can
    override a single value for one run without editing the file."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def main() -> int:
    load_env(ROOT / ".env")

    ap = argparse.ArgumentParser(description="Collect AI usage from all configured sources.")
    ap.add_argument("--no-dashboard", action="store_true", help="skip regenerating dashboard.html")
    ap.add_argument("--backfill-days", type=int,
                    default=int(os.environ.get("AIU_BACKFILL_DAYS", "365")),
                    help="how far back to reach on the first API run (default 365)")
    ap.add_argument("--status", action="store_true", help="print a summary and exit")
    ap.add_argument("--open", dest="open_after", action="store_true",
                    help="open the dashboard in your browser when done")
    ap.add_argument("--only", choices=["claude_code", "codex", "anthropic", "openai"],
                    help="run a single source")
    args = ap.parse_args()

    db_path = _expand(os.environ.get("AIU_DB", str(ROOT / "data" / "usage.db")))
    archive_dir = _expand(os.environ.get("AIU_ARCHIVE", str(ROOT / "data" / "archive")))
    claude_dir = _expand(os.environ.get("AIU_CLAUDE_DIR", "~/.claude"))
    codex_dir = _expand(os.environ.get("AIU_CODEX_DIR", "~/.codex"))
    out_html = _expand(os.environ.get("AIU_DASHBOARD", str(ROOT / "dashboard.html")))

    conn = db.connect(db_path)

    if args.status:
        return print_status(conn, db_path)

    failures = 0

    def stage(name: str, enabled: bool, fn, skip_reason: str = ""):
        nonlocal failures
        if not enabled:
            print(f"  {name:<22} skipped ({skip_reason})")
            db.log_run(conn, name, "skipped", skip_reason,
                       datetime.now(timezone.utc).isoformat(),
                       datetime.now(timezone.utc).isoformat())
            conn.commit()
            return
        started = datetime.now(timezone.utc).isoformat()
        try:
            stats = fn()
            conn.commit()
            detail = " ".join(f"{k}={v}" for k, v in stats.items())
            print(f"  {name:<22} ok       {detail}")
            db.log_run(conn, name, "ok", detail, started,
                       datetime.now(timezone.utc).isoformat())
        except Exception as e:  # keep other sources running
            conn.rollback()
            failures += 1
            print(f"  {name:<22} ERROR    {e}", file=sys.stderr)
            db.log_run(conn, name, "error", f"{e}\n{traceback.format_exc()}", started,
                       datetime.now(timezone.utc).isoformat())
        conn.commit()

    anthropic_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    only = args.only

    print(f"local-ai-usage-tracker  db={db_path}")

    stage("claude_code_local", only in (None, "claude_code"),
          lambda: claude_code_local.run(conn, claude_dir, archive_dir),
          "--only excluded it")

    stage("codex_local", only in (None, "codex"),
          lambda: codex_local.run(conn, codex_dir, archive_dir.parent / "archive-codex"),
          "--only excluded it")

    stage("anthropic_admin", bool(anthropic_key) and only in (None, "anthropic"),
          lambda: anthropic_admin.run(conn, anthropic_key, args.backfill_days),
          "ANTHROPIC_ADMIN_KEY not set" if not anthropic_key else "--only excluded it")

    stage("openai_admin", bool(openai_key) and only in (None, "openai"),
          lambda: openai_admin.run(conn, openai_key, args.backfill_days),
          "OPENAI_ADMIN_KEY not set" if not openai_key else "--only excluded it")

    if not args.no_dashboard:
        from aiusage import dashboard
        dashboard.build(conn, out_html)
        print(f"  dashboard              ok       {out_html}")
        if args.open_after:
            subprocess.run(["open", str(out_html)], check=False)

    conn.commit()
    conn.close()
    return 1 if failures else 0


def print_status(conn, db_path: Path) -> int:
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(day) lo, MAX(day) hi FROM usage_event"
    ).fetchone()
    print(f"database : {db_path}")
    print(f"events   : {row['n']:,}")
    print(f"range    : {row['lo']} .. {row['hi']}" if row["n"] else "range    : (empty)")
    print()
    print(f"{'provider':<12}{'source':<26}{'events':>10}{'days':>7}")
    for r in conn.execute(
        "SELECT provider, source, COUNT(*) n, COUNT(DISTINCT day) d "
        "FROM usage_event GROUP BY provider, source ORDER BY provider, source"
    ):
        print(f"{r['provider']:<12}{r['source']:<26}{r['n']:>10,}{r['d']:>7}")

    coarse = conn.execute("SELECT COUNT(*) n, MIN(day) lo, MAX(day) hi "
                          "FROM coarse_daily_tokens").fetchone()
    if coarse["n"]:
        print(f"\ncoarse backfill: {coarse['n']} rows, {coarse['lo']} .. {coarse['hi']} "
              f"(daily totals only, not priceable)")

    print("\nlast runs:")
    for r in conn.execute(
        "SELECT source, status, detail, finished_at FROM run_log "
        "ORDER BY id DESC LIMIT 8"
    ):
        print(f"  {r['finished_at'][:19]}  {r['source']:<22}{r['status']:<9}{(r['detail'] or '')[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
