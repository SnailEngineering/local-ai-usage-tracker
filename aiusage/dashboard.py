"""Static dashboard generator.

Queries the database, prices tokens at render time, and writes one
self-contained HTML file. No build step, no network at view time -- open it
from a bookmark. `collect.py --serve` (see aiusage/server.py) is optional and
only adds a /api/data endpoint the page can poll for fresh data; the default
workflow stays a plain file on disk.
"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import posixpath
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import pricing


# Seconds between the page's own auto-refreshes. Defined here, next to the
# template that consumes it, so collect.py's --interval default and server.py
# do not each carry their own copy of the number.
DEFAULT_REFRESH_SECONDS = 60

# Fixed provider -> categorical slot. Colour follows the entity, so adding a
# third provider never repaints the first two.
PROVIDER_SLOT = {"anthropic": 0, "openai": 1, "ollama": 2}

MAX_MODEL_SERIES = 8  # past this, everything folds into "Other"


def _agg(conn: sqlite3.Connection) -> list[dict]:
    """One row per (day, provider, source, model) with tokens and priced cost."""
    sql = """
        SELECT day, provider, source, model,
               SUM(input_tokens)          AS input_tokens,
               SUM(output_tokens)         AS output_tokens,
               SUM(cache_write_5m_tokens) AS cache_write_5m_tokens,
               SUM(cache_write_1h_tokens) AS cache_write_1h_tokens,
               SUM(cache_read_tokens)     AS cache_read_tokens,
               SUM(requests)              AS requests,
               COUNT(*)                   AS n_events
        FROM usage_event
        GROUP BY day, provider, source, model
        ORDER BY day
    """
    rows = []
    for r in conn.execute(sql):
        d = dict(r)
        d["model"] = pricing.normalize_model(d["model"])
        cost = pricing.cost_usd(d)
        d["cost_usd"] = cost
        d["priced"] = cost is not None
        d["total_tokens"] = (
            d["input_tokens"] + d["output_tokens"]
            + d["cache_write_5m_tokens"] + d["cache_write_1h_tokens"]
            + d["cache_read_tokens"]
        )
        rows.append(d)
    return rows


def project_labels(projects) -> dict[str, str]:
    """Display name for each project, keyed by `path or name`.

    `projects` is an iterable of (path, name): the session's full working
    directory (None for a row ingested before paths were stored) and its
    basename. Two unrelated checkouts both called `backend` are different
    projects, so identity is the path; the label stays the compact basename
    unless that would put two rows under one name, and then only those rows
    grow -- to the shortest trailing path that tells them apart
    (`acme/backend`). Whether a row has a path is the column's say, never
    guessed from the string: a bare name may contain a slash too."""
    by_name: dict[str, list[str]] = defaultdict(list)    # name -> path keys
    unpathed: dict[str, bool] = {}                       # name of rows with no path
    for path, name in set(projects):
        if path is None:
            unpathed[name] = True
        else:
            by_name[name].append(path)

    labels: dict[str, str] = {}
    for name in unpathed:
        # No path survived (its archive was pruned before the column existed),
        # so it cannot be told apart from a same-named path. Say so only when
        # there is something to be confused with.
        labels[name] = f"{name} (path unknown)" if name in by_name else name
    for name, paths in by_name.items():
        if len(paths) == 1:
            labels[paths[0]] = name
            continue
        parts = {k: [p for p in k.split("/") if p] for k in paths}
        depth = 2
        while (len({"/".join(v[-depth:]) for v in parts.values()}) < len(paths)
               and depth < max(len(v) for v in parts.values())):
            depth += 1
        for k in paths:
            labels[k] = "/".join(parts[k][-depth:])
    return labels


def _provider_summary(rows) -> dict:
    """Card metrics per provider; active days may overlap across providers."""
    summaries = {}
    for provider in ("anthropic", "openai"):
        summaries[provider] = {
            "cost_usd": 0.0, "total_tokens": 0, "cache_read": 0,
            "days": set(), "unpriced": False, "priced_events": 0,
        }
    for row in rows:
        provider = row["provider"]
        if provider not in summaries:
            continue
        summary = summaries[provider]
        summary["total_tokens"] += row["total_tokens"]
        summary["cache_read"] += row["cache_read_tokens"]
        summary["days"].add(row["day"])
        if row["priced"]:
            summary["cost_usd"] += row["cost_usd"]
            summary["priced_events"] += row["n_events"]
        else:
            summary["unpriced"] = True
    for summary in summaries.values():
        summary["active_days"] = len(summary.pop("days"))
    return summaries


def build_payload(conn: sqlite3.Connection) -> dict:
    rows = _agg(conn)

    # Rank models by lifetime tokens so the busiest get the leading hues. The
    # set is stable across rebuilds; nothing in the page filters series out.
    model_totals: dict[str, int] = defaultdict(int)
    for r in rows:
        model_totals[r["model"]] += r["total_tokens"]
    ranked = sorted(model_totals, key=lambda m: -model_totals[m])
    top_models = ranked[:MAX_MODEL_SERIES]
    model_slot = {m: i for i, m in enumerate(top_models)}

    def bucket(model: str) -> str:
        return model if model in model_slot else "Other"

    days = sorted({r["day"] for r in rows})

    cost_by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    tokens_by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unpriced_tokens = 0
    unpriced_models: set[str] = set()

    for r in rows:
        if r["priced"]:
            cost_by_day[r["day"]][r["provider"]] += r["cost_usd"]
        else:
            unpriced_tokens += r["total_tokens"]
            unpriced_models.add(r["model"])
        tokens_by_day[r["day"]][bucket(r["model"])] += r["total_tokens"]

    # Monthly table, mirroring the shape ccusage prints. Each row also carries
    # the counts the month drill-down needs for its own tiles, so clicking a
    # month is a pure client-side re-render -- no server, no second query.
    months: dict[str, dict] = {}
    month_models: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        m = r["day"][:7]
        e = months.setdefault(m, {
            "month": m, "models": set(), "days": set(), "events": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_write": 0, "cache_read": 0, "total_tokens": 0,
            "cost_usd": 0.0, "unpriced": False,
        })
        e["models"].add(r["model"])
        e["days"].add(r["day"])
        e["events"] += r["n_events"]
        e["input_tokens"] += r["input_tokens"]
        e["output_tokens"] += r["output_tokens"]
        e["cache_write"] += r["cache_write_5m_tokens"] + r["cache_write_1h_tokens"]
        e["cache_read"] += r["cache_read_tokens"]
        e["total_tokens"] += r["total_tokens"]
        if r["priced"]:
            e["cost_usd"] += r["cost_usd"]
        else:
            e["unpriced"] = True

        me = month_models[m].setdefault(r["model"], {
            "model": r["model"], "input_tokens": 0, "output_tokens": 0,
            "cache_write": 0, "cache_read": 0, "total_tokens": 0,
            "cost_usd": 0.0, "unpriced": False,
        })
        me["input_tokens"] += r["input_tokens"]
        me["output_tokens"] += r["output_tokens"]
        me["cache_write"] += r["cache_write_5m_tokens"] + r["cache_write_1h_tokens"]
        me["cache_read"] += r["cache_read_tokens"]
        me["total_tokens"] += r["total_tokens"]
        if r["priced"]:
            me["cost_usd"] += r["cost_usd"]
        else:
            me["unpriced"] = True

    month_rows = []
    for e in sorted(months.values(), key=lambda x: x["month"]):
        e = dict(e)
        e["active_days"] = len(e["days"])
        e["models"] = sorted(e["models"])
        del e["days"]
        month_rows.append(e)
    month_model_rows = {
        m: sorted(v.values(), key=lambda x: -x["total_tokens"])
        for m, v in month_models.items()
    }

    # Per-project spend (project comes from the session's cwd, both sources set
    # it). Grouped by month so the drill-down gets its own ranking; the all-time
    # table is the same numbers summed back up.
    #
    # The grouping has to include `day`, not just the month: `rates_for()` is a
    # function of the day, so a DATED_OVERRIDES boundary landing mid-month would
    # otherwise price the whole month's tokens at whichever rate applied on one
    # arbitrary day of it. Price each day, then roll the dollars up -- that way
    # this table reconciles with the month table instead of drifting from it.
    proj: dict[str, dict] = {}
    month_proj: dict[str, dict[str, dict]] = defaultdict(dict)

    def _proj_entry(store: dict, d: dict) -> dict:
        return store.setdefault(d["key"], {
            "project": d["label"], "cost_usd": 0.0, "total_tokens": 0,
            "last_day": d["day"], "messages": 0, "unpriced": False,
        })

    proj_rows = [dict(r) for r in conn.execute("""
        SELECT day, project_path, project, provider, model,
               SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
               SUM(cache_write_5m_tokens) cache_write_5m_tokens,
               SUM(cache_write_1h_tokens) cache_write_1h_tokens,
               SUM(cache_read_tokens) cache_read_tokens,
               COUNT(*) n
        FROM usage_event WHERE project IS NOT NULL
        GROUP BY day, project_path, project, provider, model
    """)]
    # Labels are settled over every key first: whether `backend` needs a longer
    # name depends on the other projects, not just the rows of this month.
    labels = project_labels((r["project_path"], r["project"]) for r in proj_rows)

    for d in proj_rows:
        d["key"] = d["project_path"] or d["project"]
        d["label"] = labels[d["key"]]
        d["month"] = d["day"][:7]
        d["model"] = pricing.normalize_model(d["model"])
        # None means "no rate on file" and must not collapse to zero -- flag the
        # project instead, the same way the month and model tables do.
        c = pricing.cost_usd(d)
        tokens = (
            d["input_tokens"] + d["output_tokens"] + d["cache_write_5m_tokens"]
            + d["cache_write_1h_tokens"] + d["cache_read_tokens"]
        )
        for e in (_proj_entry(proj, d), _proj_entry(month_proj[d["month"]], d)):
            if c is None:
                e["unpriced"] = True
            else:
                e["cost_usd"] += c
            e["total_tokens"] += tokens
            e["messages"] += d["n"]
            e["last_day"] = max(e["last_day"], d["day"])
    project_rows = sorted(proj.values(), key=lambda x: -x["cost_usd"])[:15]
    month_project_rows = {
        m: sorted(v.values(), key=lambda x: -x["cost_usd"])[:15]
        for m, v in month_proj.items()
    }

    # Coarse backfill: days we know were active but can no longer price.
    coarse = [
        {"day": r["day"], "tokens": r["t"]}
        for r in conn.execute(
            "SELECT day, SUM(total_tokens) t FROM coarse_daily_tokens "
            "WHERE day NOT IN (SELECT DISTINCT day FROM usage_event) GROUP BY day ORDER BY day"
        )
    ]

    # "This month / week / today" cost, bucketed by local calendar day (same
    # convention as `_local_day` at ingest time) so it matches what the user
    # actually lived through, not a UTC-shifted view of it.
    now_local = datetime.now().astimezone()
    today_str = now_local.strftime("%Y-%m-%d")
    week_start = (now_local - timedelta(days=now_local.weekday())).strftime("%Y-%m-%d")
    month_start = now_local.strftime("%Y-%m") + "-01"

    def day_cost(d: str) -> float:
        return sum(cost_by_day.get(d, {}).values())

    totals = {
        "cost_usd": sum(r["cost_usd"] for r in rows if r["priced"]),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "cache_read": sum(r["cache_read_tokens"] for r in rows),
        "output": sum(r["output_tokens"] for r in rows),
        "active_days": len(days),
        "events": sum(r["n_events"] for r in rows),
        "cost_today": day_cost(today_str),
        "cost_week": sum(day_cost(d) for d in cost_by_day if d >= week_start),
        "cost_month": sum(day_cost(d) for d in cost_by_day if d >= month_start),
    }

    rows_by_month = defaultdict(list)
    for row in rows:
        rows_by_month[row["day"][:7]].append(row)
    month_providers = {month: _provider_summary(group)
                       for month, group in rows_by_month.items()}
    period_providers = {
        "month": _provider_summary(r for r in rows if r["day"] >= month_start),
        "week": _provider_summary(r for r in rows if r["day"] >= week_start),
        "today": _provider_summary(r for r in rows if r["day"] == today_str),
    }

    # Scheduled runs plus any failure. A `--serve` session logs two rows a
    # minute, which would otherwise be the entire panel and hide exactly the
    # thing it is here to show: whether the launchd job is still working.
    runs = [dict(r) for r in conn.execute(
        "SELECT source, status, detail, finished_at, triggered_by FROM run_log "
        "WHERE triggered_by = 'scheduled' OR status = 'error' "
        "ORDER BY id DESC LIMIT 10")]

    providers = sorted({r["provider"] for r in rows},
                       key=lambda p: PROVIDER_SLOT.get(p, 9))

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "providers": providers,
        "provider_slot": PROVIDER_SLOT,
        "models": top_models + (["Other"] if len(ranked) > MAX_MODEL_SERIES else []),
        "model_slot": model_slot,
        "cost_by_day": {d: dict(v) for d, v in cost_by_day.items()},
        "tokens_by_day": {d: dict(v) for d, v in tokens_by_day.items()},
        "months": month_rows,
        "month_models": month_model_rows,
        "month_projects": month_project_rows,
        "projects": project_rows,
        "coarse": coarse,
        "totals": totals,
        "provider_totals": _provider_summary(rows),
        "month_providers": month_providers,
        "period_providers": period_providers,
        "unpriced": {"tokens": unpriced_tokens, "models": sorted(unpriced_models)},
        "runs": runs,
    }


def render_payload(payload: dict,
                   refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> str:
    """The page for an already-built payload. Separate from `render` so
    `--serve`'s /api/data, which has just built one, can write the file
    through without querying everything a second time."""
    # The payload is spliced into an inline <script>, where the HTML parser wins
    # over the JS one: a project directory literally named `</script>...` would
    # otherwise close the block early and inject live markup. Escaping `<` keeps
    # the JSON valid and identical once parsed.
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    html = (TEMPLATE
            .replace("__REFRESH_MS__", str(refresh_seconds * 1000), 1)
            .replace("__DATA__", data, 1))
    # Only this generated script may execute. In particular, event handlers
    # smuggled in through imported strings cannot run or load external assets.
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    return html.replace("__SCRIPT_HASH__", digest, 1)


def render(conn: sqlite3.Connection,
           refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> str:
    """The whole page as a string. Split out from `build` so `--serve` can
    answer a request without a disk round-trip -- writing the file just to
    read it back would put a page load at the mercy of an unwritable
    directory, and would race the scheduled `collect.py` writing the same
    path from another process."""
    return render_payload(build_payload(conn), refresh_seconds)


def write_page(html: str, out_path: Path) -> Path:
    """Put `html` at `out_path`, atomically."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Written through a temp file in the same directory and renamed, because
    # more than one writer targets this path -- the launchd schedule, a manual
    # ./collect.py, and every --serve request that writes through. A plain
    # write_text lets a reader (a browser on file://, another collector) see a
    # half-written page; the rename is atomic, so a reader sees either the old
    # file or the new one, never a torn mix.
    #
    # mkstemp rather than a pid-derived name: --serve writes through from
    # request threads, so two writers can share a pid and would otherwise pick
    # the same temp path and corrupt each other.
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent,
                                    prefix=out_path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out_path


def build(conn: sqlite3.Connection, out_path: Path,
          refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> Path:
    return write_page(render(conn, refresh_seconds), out_path)


# Source assets stay separately editable; the output still contains everything
# inline and never fetches a stylesheet, script, font, or charting dependency.
_TEMPLATE_DIR = Path(__file__).with_name("templates")
TEMPLATE = (_TEMPLATE_DIR.joinpath("dashboard.html").read_text(encoding="utf-8")
            .replace("__CSS__", _TEMPLATE_DIR.joinpath("dashboard.css").read_text(encoding="utf-8"))
            .replace("__JS__", _TEMPLATE_DIR.joinpath("dashboard.js").read_text(encoding="utf-8")))
