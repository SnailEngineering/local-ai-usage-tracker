#!/usr/bin/env python3
"""local-ai-usage-tracker collector.

Run once or twice a day. Every source is optional and independently
skippable, so a problem in one never blocks the other from running.

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
from aiusage.sources import claude_code_local, codex_local  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8787

# `--serve` re-collects on every /api/data hit, so an open tab writes two
# run_log rows a minute. Those drown out the launchd runs the log exists to
# surveil, so the health views show scheduled runs plus anything that failed.
SCHEDULED_OR_FAILED = "(triggered_by = 'scheduled' OR status = 'error')"


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
    """Resolve a configured path.

    Relative values are resolved against the repository, not the process's
    working directory. They come from `.env`, which lives in the repository,
    and the shell aliases invoke this script by absolute path from wherever
    you happen to be standing -- so a CWD-relative rule means `aiusage` run
    from another directory quietly starts a second, empty database there
    instead of using yours. Pass an absolute path to put data elsewhere.
    """
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def run_sources(conn, claude_dir: Path, codex_dir: Path, archive_dir: Path,
                 codex_archive_dir: Path, only: str | None = None,
                 quiet: bool = False, triggered_by: str = "scheduled") -> int:
    """Run every enabled source once, logging each to run_log. Returns the
    number of failures; one source raising never stops the other."""
    failures = 0

    def stage(name: str, enabled: bool, fn, skip_reason: str = ""):
        nonlocal failures
        if not enabled:
            if not quiet:
                print(f"  {name:<22} skipped ({skip_reason})")
            db.log_run(conn, name, "skipped", skip_reason,
                       datetime.now(timezone.utc).isoformat(),
                       datetime.now(timezone.utc).isoformat(), triggered_by)
            conn.commit()
            return
        started = datetime.now(timezone.utc).isoformat()
        try:
            stats = fn()
            conn.commit()
            detail = " ".join(f"{k}={v}" for k, v in stats.items())
            if not quiet:
                print(f"  {name:<22} ok       {detail}")
            db.log_run(conn, name, "ok", detail, started,
                       datetime.now(timezone.utc).isoformat(), triggered_by)
        except Exception as e:  # keep other sources running
            conn.rollback()
            failures += 1
            print(f"  {name:<22} ERROR    {e}", file=sys.stderr)
            db.log_run(conn, name, "error", f"{e}\n{traceback.format_exc()}", started,
                       datetime.now(timezone.utc).isoformat(), triggered_by)
        conn.commit()

    stage("claude_code_local", only in (None, "claude_code"),
          lambda: claude_code_local.run(conn, claude_dir, archive_dir),
          "--only excluded it")

    stage("codex_local", only in (None, "codex"),
          lambda: codex_local.run(conn, codex_dir, codex_archive_dir),
          "--only excluded it")

    return failures


def main() -> int:
    load_env(ROOT / ".env")

    ap = argparse.ArgumentParser(description="Collect AI usage from all configured sources.")
    ap.add_argument("--no-dashboard", action="store_true", help="skip regenerating dashboard.html")
    ap.add_argument("--status", action="store_true", help="print a summary and exit")
    ap.add_argument("--open", dest="open_after", action="store_true",
                    help="open the dashboard, or the local server with --serve, when ready")
    ap.add_argument("--only", choices=["claude_code", "codex"],
                    help="run a single source")
    ap.add_argument("--serve", action="store_true",
                    help="collect once, then serve the dashboard on localhost and "
                         "re-collect on every page refresh instead of exiting")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port for --serve (default {DEFAULT_PORT})")
    ap.add_argument("--prune", type=float, metavar="DAYS",
                    help="list archived session files older than DAYS that have "
                         "been fully ingested; add --yes to delete them")
    ap.add_argument("--yes", action="store_true",
                    help="with --prune, actually delete instead of listing")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between auto-refreshes while the dashboard tab "
                         "is left open (default 60)")
    args = ap.parse_args()

    db_path = _expand(os.environ.get("AIU_DB", str(ROOT / "data" / "usage.db")))
    archive_dir = _expand(os.environ.get("AIU_ARCHIVE", str(ROOT / "data" / "archive")))
    # Defaults beside the Claude archive, but settable on its own: the Codex
    # rollouts are much the larger half, so they are the ones you would want to
    # put on another volume.
    codex_archive_dir = _expand(os.environ.get(
        "AIU_ARCHIVE_CODEX", str(archive_dir.parent / "archive-codex")))
    claude_dir = _expand(os.environ.get("AIU_CLAUDE_DIR", "~/.claude"))
    codex_dir = _expand(os.environ.get("AIU_CODEX_DIR", "~/.codex"))
    out_html = _expand(os.environ.get("AIU_DASHBOARD", str(ROOT / "dashboard.html")))

    # --serve hands the connection to a ThreadingHTTPServer, where each
    # request runs on its own thread; server.py serializes every access with
    # a lock, so disabling sqlite's same-thread check here is safe.
    conn = db.connect(db_path, check_same_thread=not args.serve)

    if args.status:
        return print_status(conn, db_path)

    if args.prune is not None:
        return run_prune(conn, archive_dir, codex_archive_dir, args.prune, args.yes)

    print(f"local-ai-usage-tracker  db={db_path}")
    failures = run_sources(conn, claude_dir, codex_dir, archive_dir,
                           codex_archive_dir, args.only)

    from aiusage import dashboard
    if not args.no_dashboard or args.serve:
        dashboard.build(conn, out_html, refresh_seconds=args.interval)
        print(f"  dashboard              ok       {out_html}")

    if args.serve:
        from aiusage import server
        collect_fn = lambda: run_sources(conn, claude_dir, codex_dir, archive_dir,
                                          codex_archive_dir, args.only, quiet=True,
                                          triggered_by="serve")
        httpd = server.make_server(conn, collect_fn, out_html, "127.0.0.1", args.port)
        url = f"http://127.0.0.1:{args.port}/"
        print(f"  serving                {url}  (Ctrl+C to stop, re-collects every "
              f"request to /api/data)")
        if args.open_after:
            subprocess.run(["open", url], check=False)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            conn.close()
        return 0

    if args.open_after:
        subprocess.run(["open", str(out_html)], check=False)

    conn.commit()
    conn.close()
    return 1 if failures else 0


def run_prune(conn, archive_dir: Path, codex_archive_dir: Path,
              older_than_days: float, confirmed: bool) -> int:
    """List, or with --yes delete, archived files that are old and fully read.

    Listing is the default deliberately: this removes full session transcripts,
    and the events themselves are already in the database, so there is no undo
    beyond whatever the source tools still hold.
    """
    from aiusage import prune as pruner

    archives = {"claude_code_local": archive_dir, "codex_local": codex_archive_dir}
    candidates = pruner.survey(conn, archives, older_than_days)
    prunable = [c for c in candidates if c.prunable]
    total = sum(c.size for c in prunable)

    print(f"archives : {archive_dir}")
    print(f"           {codex_archive_dir}")
    print(f"scanned  : {len(candidates)} files, "
          f"{pruner.human_bytes(sum(c.size for c in candidates))}")

    kept: dict[str, list] = {}
    for c in candidates:
        if not c.prunable:
            kept.setdefault(c.reason, []).append(c)
    for reason, group in sorted(kept.items(), key=lambda kv: -len(kv[1])):
        print(f"  kept   : {len(group):>4} {reason} "
              f"({pruner.human_bytes(sum(c.size for c in group))})")

    if not prunable:
        print(f"\nNothing older than {older_than_days:g} days is fully ingested.")
        return 0

    print(f"\n{len(prunable)} file(s), {pruner.human_bytes(total)}, "
          f"older than {older_than_days:g} days and fully ingested:")
    for c in prunable[:20]:
        print(f"  {c.age_days:>6.0f}d  {pruner.human_bytes(c.size):>10}  {c.path.name}")
    if len(prunable) > 20:
        print(f"  ... and {len(prunable) - 20} more")

    if not confirmed:
        print("\nNothing deleted. These are full session transcripts and the "
              "delete cannot be undone;")
        print(f"the token counts are already in the database. Re-run with --yes "
              f"to remove them:")
        print(f"  ./collect.py --prune {older_than_days:g} --yes")
        return 0

    removed, reclaimed = pruner.prune(conn, candidates)
    print(f"\nRemoved {removed} file(s), reclaimed {pruner.human_bytes(reclaimed)}.")
    return 0


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
        "SELECT source, status, detail, finished_at, triggered_by FROM run_log "
        f"WHERE {SCHEDULED_OR_FAILED} ORDER BY id DESC LIMIT 8"
    ):
        via = "" if r["triggered_by"] == "scheduled" else f" ({r['triggered_by']})"
        print(f"  {r['finished_at'][:19]}  {r['source'] + via:<22}"
              f"{r['status']:<9}{(r['detail'] or '')[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
