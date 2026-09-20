"""Static dashboard generator.

Queries the database, prices tokens at render time, and writes one
self-contained HTML file. No build step, no network at view time -- open it
from a bookmark. `collect.py --serve` (see aiusage/server.py) is optional and
only adds a /api/data endpoint the page can poll for fresh data; the default
workflow stays a plain file on disk.
"""

from __future__ import annotations

import json
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
    return (TEMPLATE
            .replace("__DATA__", data)
            .replace("__REFRESH_MS__", str(refresh_seconds * 1000)))


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


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Usage</title>
<style>
  :root {
    color-scheme: light;
    --page:#f9f9f7; --surface:#fcfcfb;
    --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
    --hover:rgba(11,11,11,0.045);
    --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
    --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
    --sother:#9b9a92;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --page:#0d0d0d; --surface:#1a1a19;
      --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --hover:rgba(255,255,255,0.055);
      --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
      --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
      --sother:#6f6e68;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --hover:rgba(255,255,255,0.055);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
    --sother:#6f6e68;
  }

  * { box-sizing: border-box; }
  body {
    margin:0; padding:32px 24px 64px;
    background:var(--page); color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  .wrap { max-width:1120px; margin:0 auto; }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:12px 20px; margin-bottom:28px; }
  h1 { font-size:22px; font-weight:640; margin:0; letter-spacing:-0.01em; }
  .stamp { color:var(--muted); font-size:13px; margin-left:auto; }
  .btn {
    appearance:none; border:1px solid var(--border); background:var(--surface);
    color:var(--ink); font:inherit; font-size:12.5px; font-weight:560;
    padding:6px 12px; border-radius:8px; cursor:pointer;
  }
  .btn:hover { border-color:var(--axis); }
  .btn:disabled { opacity:.6; cursor:default; }
  h2 { font-size:15px; font-weight:620; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 16px; }

  .card {
    background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:20px 22px; margin-bottom:20px;
  }

  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:14px; margin-bottom:20px; }
  .tile { display:flex; flex-direction:column; min-width:0; background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }
  .tile .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
  .tile .v { font-size:27px; font-weight:640; margin-top:6px; letter-spacing:-0.02em; }
  .tile .n { color:var(--ink-2); font-size:12px; margin-top:4px; }
  .provider-footer { margin-top:auto; padding-top:14px; }
  .provider-split { display:flex; justify-content:space-between; gap:8px;
    border-top:1px solid var(--border); padding-top:10px; }
  .provider-part { min-width:0; display:flex; flex-direction:column; gap:2px; }
  .provider-part:last-child { text-align:right; }
  .provider-name { color:var(--muted); font-size:11px; }
  .provider-value { color:var(--ink-2); font-size:13px; font-weight:600;
    font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }


  .legend { display:flex; flex-wrap:wrap; gap:8px 16px; margin:14px 0 0; }
  .legend span { display:inline-flex; align-items:center; gap:7px; font-size:12.5px; color:var(--ink-2); }
  .swatch { width:11px; height:11px; border-radius:3px; flex:none; }

  .chart { position:relative; }
  .chart svg { display:block; width:100%; height:auto; overflow:visible; }
  .tick { fill:var(--muted); font-size:11px; }
  .gridline { stroke:var(--grid); stroke-width:1; }
  .baseline { stroke:var(--axis); stroke-width:1; }
  .seg { stroke:var(--surface); stroke-width:2; }
  .hit { fill:transparent; cursor:crosshair; }

  .tip {
    position:absolute; pointer-events:none; opacity:0; transition:opacity .08s;
    background:var(--surface); border:1px solid var(--border); border-radius:9px;
    padding:9px 11px; font-size:12.5px; min-width:150px; z-index:5;
    box-shadow:0 6px 20px rgba(0,0,0,.14);
  }
  .tip .th { font-weight:640; margin-bottom:6px; }
  .tip .tr { display:flex; align-items:center; gap:7px; white-space:nowrap; }
  .tip .tr b { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:600; }
  .tip .tt { display:flex; gap:7px; margin-top:6px; padding-top:5px; border-top:1px solid var(--border); }
  .tip .tt b { margin-left:auto; font-variant-numeric:tabular-nums; }

  .scroll { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:13.5px; }
  th, td { padding:9px 12px; text-align:right; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  thead th { color:var(--muted); font-weight:560; font-size:11.5px;
             text-transform:uppercase; letter-spacing:.05em;
             border-bottom:1px solid var(--axis); }
  tbody tr + tr td { border-top:1px solid var(--grid); }
  tbody td { font-variant-numeric:tabular-nums; color:var(--ink-2); }
  tbody td:first-child { color:var(--ink); font-weight:560; }
  tbody td.num-strong { color:var(--ink); font-weight:600; }
  tfoot td { border-top:1px solid var(--axis); font-weight:640; color:var(--ink);
             font-variant-numeric:tabular-nums; padding-top:11px; }
  .models { color:var(--muted); font-size:11.5px; white-space:normal; }

  /* Month rows drill into a single month. The row is the click target; the
     month name is a real link so keyboard and middle-click work too. */
  tbody tr.link { cursor:pointer; }
  tbody tr.link:hover td { background:var(--hover); }
  tbody tr.link td:first-child { position:relative; }
  a.drill { color:inherit; text-decoration:none; }
  a.drill:hover, tr.link:hover a.drill { text-decoration:underline; }
  a.drill::after { content:' \203a'; color:var(--muted); font-weight:600; }

  .crumbs { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-bottom:18px; }
  .crumbs .btn { text-decoration:none; display:inline-block; }
  .crumbs .btn[aria-disabled="true"] { opacity:.4; pointer-events:none; }
  .crumbs h2 { font-size:17px; margin:0 4px 0 2px; letter-spacing:-0.01em; }
  .crumbs .nav { margin-left:auto; display:flex; gap:8px; }
  .delta.up { color:var(--s8); } .delta.down { color:var(--s6); }
  td .swatch { display:inline-block; margin-right:7px; vertical-align:baseline; }

  .note { font-size:12.5px; color:var(--ink-2); border-left:2px solid var(--s4);
          padding:2px 0 2px 12px; margin-top:16px; }
  .empty { color:var(--muted); padding:26px 0; text-align:center; }
  details.runs { margin-top:8px; }
  details.runs summary { cursor:pointer; color:var(--muted); font-size:12.5px; }
  details.runs table { margin-top:10px; }
  .ok { color:var(--s6); } .err { color:var(--s8); }

  /* Phone width. The tile grid's 168px minimum needs 350px for two columns and
     a 390px viewport minus the page gutters offers 342, so every tile fell into
     one tall column and pushed the first chart several screens down. Two
     compact columns fit; an odd tile left over spans the row instead of
     stranding a gap. */
  @media (max-width: 600px) {
    body { padding:20px 16px 48px; }
    header { margin-bottom:20px; }
    .stamp { margin-left:0; flex-basis:100%; }
    .card { padding:16px 14px; }
    .tiles { grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-bottom:14px; }
    .tile { padding:12px 13px; }
    .tile .k { font-size:11px; letter-spacing:.04em; }
    .tile .v { font-size:21px; margin-top:4px; }
    .tile .n { font-size:11.5px; }
    .provider-value { font-size:12px; }
    .tiles .tile:last-child:nth-child(odd) { grid-column:1 / -1; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AI Usage</h1>
    <button class="btn" id="refresh-btn" type="button">Refresh</button>
    <span class="stamp" id="stamp"></span>
  </header>
  <div id="app"></div>
</div>

<script>
const DATA = __DATA__;
const SLOT = ['--s1','--s2','--s3','--s4','--s5','--s6','--s7','--s8'];
const cssVar = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

/* Axis ticks drop cents once they are large enough that cents are noise;
   every figure in a table or tile keeps them, so column sums reconcile. */
const usd = n => n >= 10 ? '$' + Math.round(n).toLocaleString('en-US')
              : n >= 1  ? '$' + n.toFixed(2)
              : n > 0   ? '$' + n.toFixed(2) : '$0';
const usd2 = n => '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const tok = n => n >= 1e9 ? (n/1e9).toFixed(2) + 'B'
              : n >= 1e6 ? (n/1e6).toFixed(1) + 'M'
              : n >= 1e3 ? (n/1e3).toFixed(0) + 'K' : String(n);
const num = n => n.toLocaleString('en-US');
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* Anything without a slot -- the "Other" bucket, or a series that arrived after
   the payload was built -- gets the neutral hue, never a real series' colour.
   Falling back to the last slot made "Other" indistinguishable from the 8th
   model, in both the chart and the legend. */
function colorOf(key, slots) {
  const i = slots[key];
  return cssVar(i === undefined ? '--sother' : SLOT[i % SLOT.length]);
}

/* Stacked bar chart with a crosshair + per-day tooltip.
   Series identity is carried by the legend and the tooltip swatches, never by
   colour alone. */
function stackedBars(el, opts) {
  const { days, series, byDay, slots, fmt, label } = opts;
  const tickFmt = opts.tickFmt || (d => d.slice(5));
  const W = el.clientWidth || 900, H = 230;
  const padL = 56, padR = 12, padT = 12, padB = 26;
  const iw = Math.max(W - padL - padR, 10), ih = H - padT - padB;

  const totals = days.map(d => series.reduce((s, k) => s + ((byDay[d] || {})[k] || 0), 0));
  const max = Math.max(...totals, 1e-9);
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const bw = Math.max(Math.min(iw / Math.max(days.length, 1) - 2, 30), 1.5);
  const x = i => padL + (i + 0.5) * (iw / Math.max(days.length, 1));
  const y = v => padT + ih - (v / top) * ih;

  let g = '';
  for (let t = 0; t <= top + 1e-9; t += step) {
    g += `<line class="gridline" x1="${padL}" x2="${padL + iw}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>`
       + `<text class="tick" x="${padL - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${fmt(t)}</text>`;
  }

  // The 2px surface-coloured stroke is what separates stacked segments, but on
  // a bar narrower than a few pixels (a phone, a long range) it is wider than
  // the bar and paints over the whole fill -- the chart came out blank. Narrow
  // bars go without it.
  const gap = bw < 5 ? ' style="stroke-width:0"' : '';
  let bars = '';
  days.forEach((d, i) => {
    let acc = 0;
    series.forEach(k => {
      const v = (byDay[d] || {})[k] || 0;
      if (v <= 0) return;
      const y0 = y(acc + v), y1 = y(acc), h = Math.max(y1 - y0, 0.7);
      // 2px surface gap between stacked segments; rounded data-end on the top one.
      bars += `<rect class="seg" x="${(x(i) - bw / 2).toFixed(1)}" y="${y0.toFixed(1)}"
               width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="${Math.min(4, bw / 2).toFixed(1)}"
               fill="${colorOf(k, slots)}"${gap}/>`;
      acc += v;
    });
  });

  // Date ticks: first, last, and a few evenly spaced between.
  let ticks = '';
  const every = Math.max(1, Math.ceil(days.length / 7));
  days.forEach((d, i) => {
    // The last day is always labelled, so drop the regular tick just before it
    // rather than print two dates on top of each other.
    const last = days.length - 1;
    if ((i % every === 0 && (last - i >= every * 0.6 || i === last)) || i === last) {
      ticks += `<text class="tick" x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle">${tickFmt(d)}</text>`;
    }
  });

  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
      ${g}${bars}
      <line class="baseline" x1="${padL}" x2="${padL + iw}" y1="${y(0)}" y2="${y(0)}"/>
      ${ticks}
      <line id="ch" class="baseline" y1="${padT}" y2="${padT + ih}" style="opacity:0" stroke-dasharray="3 3"/>
      <rect class="hit" x="${padL}" y="${padT}" width="${iw}" height="${ih}"/>
    </svg>
    <div class="tip"></div>`;

  const svg = el.querySelector('svg'), tip = el.querySelector('.tip'), ch = el.querySelector('#ch');
  const hit = el.querySelector('.hit');

  hit.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect();
    const sx = (ev.clientX - r.left) * (W / r.width);
    let i = Math.round((sx - padL) / (iw / Math.max(days.length, 1)) - 0.5);
    i = Math.max(0, Math.min(days.length - 1, i));
    const d = days[i], vals = byDay[d] || {};

    ch.setAttribute('x1', x(i)); ch.setAttribute('x2', x(i)); ch.style.opacity = '.5';

    const parts = series
      .map(k => [k, vals[k] || 0]).filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<div class="tr"><span class="swatch" style="background:${colorOf(k, slots)}"></span>${esc(k)}<b>${fmt(v)}</b></div>`)
      .join('');

    tip.innerHTML = `<div class="th">${d}</div>${parts || '<div class="tr">no usage</div>'}`
      + `<div class="tt">total<b>${fmt(totals[i])}</b></div>`;
    tip.style.opacity = '1';

    const px = (x(i) / W) * r.width;
    tip.style.left = Math.min(Math.max(px - 75, 0), r.width - 190) + 'px';
    tip.style.top = '4px';
  });
  hit.addEventListener('mouseleave', () => { tip.style.opacity = '0'; ch.style.opacity = '0'; });
}

function niceStep(max) {
  const raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

function legend(keys, slots) {
  return '<div class="legend">' + keys.map(k =>
    `<span><i class="swatch" style="background:${colorOf(k, slots)}"></i>${esc(k)}</span>`).join('') + '</div>';
}

/* Routing is a single hash: '' is the all-time view, '#month=YYYY-MM' drills
   into one month. Keeping it in the URL means a month survives auto-refresh,
   reload, and back/forward, and can be bookmarked -- all without a server,
   since every month's numbers already ship inside DATA. */
function routedMonth() {
  const m = /^#month=(\d{4}-\d{2})$/.exec(location.hash || '');
  return m && DATA.months.some(x => x.month === m[1]) ? m[1] : null;
}

const monthName = m =>
  new Date(m + '-01T00:00:00').toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
const dayOfMonth = d => String(+d.slice(8));

function render() {
  document.getElementById('stamp').textContent = 'updated ' + DATA.generated_at;
  const app = document.getElementById('app');

  if (!DATA.days.length) {
    app.innerHTML = `<div class="card"><div class="empty">No usage recorded yet.<br>
      Run <code>./collect.py</code> to populate the database.</div></div>`;
    return;
  }

  const m = routedMonth();
  if (m) renderMonth(app, m); else renderAll(app);

  // Whole-row click for the month table; the inner <a> already handles keys.
  app.querySelectorAll('tr.link').forEach(tr => {
    tr.addEventListener('click', ev => {
      if (ev.target.closest('a')) return;
      location.hash = 'month=' + tr.dataset.month;
    });
  });
}

function providerSplit(summaries, metric) {
  const hints = {
    cost_usd: 'Cost at known rates; * means some usage is unpriced.',
    total_tokens: 'Detailed tokens for each provider.',
    cache_read: "Cache reads as a percentage of each provider's detailed tokens.",
    active_days: 'Days with detailed usage for each provider; shared days count for both.',
  };
  const value = s => {
    if (metric === 'cost_usd') {
      if (s.unpriced && !s.priced_events) return 'Unpriced';
      return usd2(s.cost_usd) + (s.unpriced ? ' *' : '');
    }
    if (metric === 'total_tokens') return tok(s.total_tokens);
    if (metric === 'cache_read') return s.total_tokens
      ? (s.cache_read / s.total_tokens * 100).toFixed(1) + '%' : '—';
    return num(s.active_days);
  };
  return `<div class="provider-footer"><div class="provider-split" title="${esc(hints[metric])}">
    ${[['anthropic', 'Claude'], ['openai', 'ChatGPT']].map(([key, label]) => {
      const summary = (summaries || {})[key];
      return `<div class="provider-part"><span class="provider-name">${label}</span>
        <span class="provider-value">${summary ? value(summary) : '—'}</span></div>`;
    }).join('')}
  </div></div>`;
}

function renderAll(app) {
  const T = DATA.totals;
  const P = DATA.provider_totals;
  const cacheShare = T.total_tokens ? (T.cache_read / T.total_tokens * 100) : 0;
  const span = DATA.days[0] + ' → ' + DATA.days[DATA.days.length - 1];
  // Days recovered from the stats cache sit outside every headline figure --
  // no input/output/cache split, so no price -- and the tiles say so rather
  // than let "Tokens" and "Active days" read as the whole history.
  const cDays = DATA.coarse.length;
  const cTok = DATA.coarse.reduce((s, c) => s + c.tokens, 0);

  let html = `
  <div class="tiles">
    <div class="tile"><div class="k">Total cost</div><div class="v">${usd2(T.cost_usd)}</div>
      <div class="n">${span}</div>${providerSplit(P, 'cost_usd')}</div>
    <div class="tile"><div class="k">Detailed tokens</div><div class="v">${tok(T.total_tokens)}</div>
      <div class="n">${num(T.events)} messages${cDays ? ` · +${tok(cTok)} earlier, unsplit` : ''}</div>${providerSplit(P, 'total_tokens')}</div>
    <div class="tile"><div class="k">Cache reads</div><div class="v">${cacheShare.toFixed(1)}%</div>
      <div class="n">of detailed tokens</div>${providerSplit(P, 'cache_read')}</div>
    <div class="tile"><div class="k">Days with detail</div><div class="v">${T.active_days}</div>
      <div class="n">${usd2(T.cost_usd / Math.max(T.active_days, 1))} / day${cDays ? ` · +${cDays} earlier` : ''}</div>${providerSplit(P, 'active_days')}</div>
  </div>

  <div class="tiles">
    <div class="tile"><div class="k">This month</div><div class="v">${usd2(T.cost_month)}</div>${providerSplit(DATA.period_providers?.month, 'cost_usd')}</div>
    <div class="tile"><div class="k">This week</div><div class="v">${usd2(T.cost_week)}</div>${providerSplit(DATA.period_providers?.week, 'cost_usd')}</div>
    <div class="tile"><div class="k">Today</div><div class="v">${usd2(T.cost_today)}</div>${providerSplit(DATA.period_providers?.today, 'cost_usd')}</div>
  </div>

  <div class="card">
    <h2>Daily cost by provider</h2>
    <p class="sub">Computed from token counts at list prices. Models with no published rate are counted in tokens but excluded here &mdash; see the note below.</p>
    <div class="chart" id="c-cost"></div>
    ${legend(DATA.providers, DATA.provider_slot)}
  </div>

  <div class="card">
    <h2>Daily tokens by model</h2>
    <p class="sub">All token types combined, including cache reads.</p>
    <div class="chart" id="c-tok"></div>
    ${legend(DATA.models, DATA.model_slot)}
  </div>

  <div class="card">
    <h2>By month</h2>
    <p class="sub">Same shape as <code>ccusage monthly</code>, but it keeps growing.
      Pick a month for its own charts, models and projects.</p>
    <div class="scroll"><table>
      <thead><tr><th>Month</th><th>Input</th><th>Output</th><th>Cache write</th>
        <th>Cache read</th><th>Total</th><th>Cost</th></tr></thead>
      <tbody>${DATA.months.map(m => `<tr class="link" data-month="${m.month}">
        <td><a class="drill" href="#month=${m.month}">${m.month}</a>
          <div class="models">${m.models.map(esc).join(', ')}</div></td>
        <td>${num(m.input_tokens)}</td><td>${num(m.output_tokens)}</td>
        <td>${num(m.cache_write)}</td><td>${num(m.cache_read)}</td>
        <td>${num(m.total_tokens)}</td>
        <td class="num-strong">${usd2(m.cost_usd)}${m.unpriced ? ' *' : ''}</td></tr>`).join('')}</tbody>
      <tfoot><tr><td>Total</td>
        <td>${num(DATA.months.reduce((s, m) => s + m.input_tokens, 0))}</td>
        <td>${num(DATA.months.reduce((s, m) => s + m.output_tokens, 0))}</td>
        <td>${num(DATA.months.reduce((s, m) => s + m.cache_write, 0))}</td>
        <td>${num(DATA.months.reduce((s, m) => s + m.cache_read, 0))}</td>
        <td>${num(T.total_tokens)}</td><td>${usd2(T.cost_usd)}</td></tr></tfoot>
    </table></div>
  </div>`;

  if (DATA.projects.length) {
    html += `<div class="card">
      <h2>Top projects</h2>
      <p class="sub">From the working directory recorded on each Claude Code and Codex turn.</p>
      <div class="scroll"><table>
        <thead><tr><th>Project</th><th>Messages</th><th>Tokens</th><th>Last used</th><th>Cost</th></tr></thead>
        <tbody>${DATA.projects.map(p => `<tr><td>${esc(p.project)}</td>
          <td>${num(p.messages)}</td><td>${tok(p.total_tokens)}</td>
          <td>${p.last_day}</td>
          <td class="num-strong">${usd2(p.cost_usd)}${p.unpriced ? ' *' : ''}</td></tr>`).join('')}</tbody>
      </table></div></div>`;
  }

  let notes = '';
  if (DATA.unpriced.tokens > 0) {
    notes += `<div class="note">${tok(DATA.unpriced.tokens)} tokens have no price on file
      (${DATA.unpriced.models.map(esc).join(', ')}), so they are counted but excluded from cost.
      Add rates in <code>aiusage/pricing.py</code>. Rows affected are marked *.</div>`;
  }
  if (DATA.coarse.length) {
    const lo = DATA.coarse[0].day, hi = DATA.coarse[DATA.coarse.length - 1].day;
    const t = DATA.coarse.reduce((s, c) => s + c.tokens, 0);
    notes += `<div class="note">${DATA.coarse.length} earlier active days (${lo} → ${hi},
      ~${tok(t)} tokens) were recovered from Claude Code's stats cache. It records only a daily
      total per model with no input/output/cache split, so those days cannot be priced and are
      not shown in the charts above.</div>`;
  }

  html += `<div class="card">
    <h2>Collector</h2>
    <p class="sub">Last runs, newest first.</p>
    ${notes || ''}
    <details class="runs" open><summary>Run log</summary>
      <div class="scroll"><table>
        <thead><tr><th>Finished</th><th>Source</th><th>Status</th><th>Detail</th></tr></thead>
        <tbody>${DATA.runs.map(r => `<tr>
          <td>${esc((r.finished_at || '').slice(0, 19).replace('T', ' '))}</td>
          <td>${esc(r.source)}${r.triggered_by && r.triggered_by !== 'scheduled'
              ? ` <span style="color:var(--muted)">(${esc(r.triggered_by)})</span>` : ''}</td>
          <td class="${r.status === 'ok' ? 'ok' : r.status === 'error' ? 'err' : ''}">${esc(r.status)}</td>
          <td style="text-align:left;white-space:normal">${esc((r.detail || '').slice(0, 110))}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    </details>
  </div>`;

  app.innerHTML = html;

  stackedBars(document.getElementById('c-cost'), {
    days: DATA.days, series: DATA.providers, byDay: DATA.cost_by_day,
    slots: DATA.provider_slot, fmt: usd, label: 'Daily cost by provider',
  });
  stackedBars(document.getElementById('c-tok'), {
    days: DATA.days, series: DATA.models, byDay: DATA.tokens_by_day,
    slots: DATA.model_slot, fmt: tok, label: 'Daily tokens by model',
  });
}

/* One month, same layout as the all-time view. Nothing is fetched: the charts
   reuse the daily series filtered to the month, the tables use the per-month
   rollups build_payload() already ships. */
function renderMonth(app, month) {
  const M = DATA.months.find(x => x.month === month);
  const P = (DATA.month_providers || {})[month];
  const i = DATA.months.indexOf(M);
  const prev = DATA.months[i - 1], next = DATA.months[i + 1];
  const models = DATA.month_models[month] || [];
  const projects = DATA.month_projects[month] || [];
  const title = monthName(month);

  // Whole calendar month, clipped at today for the month in progress, so idle
  // days show up as gaps in the bars instead of being collapsed away.
  const active = DATA.days.filter(d => d.startsWith(month));
  const today = DATA.generated_at.slice(0, 10);
  const lastActive = active[active.length - 1] || '';
  const cutoff = lastActive > today ? lastActive : today;
  const nDays = new Date(+month.slice(0, 4), +month.slice(5, 7), 0).getDate();
  const days = [];
  for (let d = 1; d <= nDays; d++) {
    const day = month + '-' + String(d).padStart(2, '0');
    if (day > cutoff) break;
    days.push(day);
  }

  // Only series that actually appear this month, so the legend stays honest.
  const providers = DATA.providers.filter(p => days.some(d => ((DATA.cost_by_day[d] || {})[p] || 0) > 0));
  const series = DATA.models.filter(k => days.some(d => ((DATA.tokens_by_day[d] || {})[k] || 0) > 0));

  const cacheShare = M.total_tokens ? (M.cache_read / M.total_tokens * 100) : 0;
  let delta = 'first month on record';
  if (prev && prev.cost_usd > 0) {
    const pct = (M.cost_usd - prev.cost_usd) / prev.cost_usd * 100;
    delta = `<span class="delta ${pct >= 0 ? 'up' : 'down'}">${pct >= 0 ? '▲' : '▼'} ${Math.abs(pct).toFixed(0)}%</span>`
          + ` vs ${prev.month}`;
  } else if (prev) {
    delta = 'vs ' + prev.month;
  }

  const navLink = (m, text) => m
    ? `<a class="btn" href="#month=${m.month}">${text}</a>`
    : `<span class="btn" aria-disabled="true">${text}</span>`;

  let html = `
  <div class="crumbs">
    <a class="btn" href="#">&larr; All time</a>
    <h2>${title}</h2>
    <div class="nav">
      ${navLink(prev, '‹ ' + (prev ? prev.month : 'Earlier'))}
      ${navLink(next, (next ? next.month : 'Later') + ' ›')}
    </div>
  </div>

  <div class="tiles">
    <div class="tile"><div class="k">Cost</div><div class="v">${usd2(M.cost_usd)}${M.unpriced ? ' *' : ''}</div>
      <div class="n">${delta}</div>${providerSplit(P, 'cost_usd')}</div>
    <div class="tile"><div class="k">Tokens</div><div class="v">${tok(M.total_tokens)}</div>
      <div class="n">${num(M.events)} messages</div>${providerSplit(P, 'total_tokens')}</div>
    <div class="tile"><div class="k">Cache reads</div><div class="v">${cacheShare.toFixed(1)}%</div>
      <div class="n">of this month's tokens</div>${providerSplit(P, 'cache_read')}</div>
    <div class="tile"><div class="k">Active days</div><div class="v">${M.active_days}</div>
      <div class="n">${usd2(M.cost_usd / Math.max(M.active_days, 1))} / active day</div>${providerSplit(P, 'active_days')}</div>
  </div>

  <div class="card">
    <h2>Daily cost by provider</h2>
    <p class="sub">${title}, at list prices. Days with no recorded usage are left blank.</p>
    <div class="chart" id="c-cost"></div>
    ${legend(providers, DATA.provider_slot)}
  </div>

  <div class="card">
    <h2>Daily tokens by model</h2>
    <p class="sub">All token types combined, including cache reads.</p>
    <div class="chart" id="c-tok"></div>
    ${legend(series, DATA.model_slot)}
  </div>

  <div class="card">
    <h2>By model</h2>
    <p class="sub">Every model used in ${title}, largest first. Models folded into the
      chart's &ldquo;Other&rdquo; series above are itemised here.</p>
    <div class="scroll"><table>
      <thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Cache write</th>
        <th>Cache read</th><th>Total</th><th>Cost</th></tr></thead>
      <tbody>${models.map(m => `<tr><td>${
          DATA.model_slot[m.model] === undefined ? ''
            : `<i class="swatch" style="background:${colorOf(m.model, DATA.model_slot)}"></i>`
        }${esc(m.model)}</td>
        <td>${num(m.input_tokens)}</td><td>${num(m.output_tokens)}</td>
        <td>${num(m.cache_write)}</td><td>${num(m.cache_read)}</td>
        <td>${num(m.total_tokens)}</td>
        <td class="num-strong">${usd2(m.cost_usd)}${m.unpriced ? ' *' : ''}</td></tr>`).join('')}</tbody>
      <tfoot><tr><td>Total</td>
        <td>${num(M.input_tokens)}</td><td>${num(M.output_tokens)}</td>
        <td>${num(M.cache_write)}</td><td>${num(M.cache_read)}</td>
        <td>${num(M.total_tokens)}</td><td>${usd2(M.cost_usd)}</td></tr></tfoot>
    </table></div>
  </div>`;

  if (projects.length) {
    html += `<div class="card">
      <h2>Top projects</h2>
      <p class="sub">Ranked within ${title} only &mdash; the all-time table ranks across every month.</p>
      <div class="scroll"><table>
        <thead><tr><th>Project</th><th>Messages</th><th>Tokens</th><th>Last used</th><th>Cost</th></tr></thead>
        <tbody>${projects.map(p => `<tr><td>${esc(p.project)}</td>
          <td>${num(p.messages)}</td><td>${tok(p.total_tokens)}</td>
          <td>${p.last_day}</td>
          <td class="num-strong">${usd2(p.cost_usd)}${p.unpriced ? ' *' : ''}</td></tr>`).join('')}</tbody>
      </table></div></div>`;
  }

  if (M.unpriced) {
    html += `<div class="card"><div class="note">Some tokens this month came from a model with no
      price on file, so they are counted but excluded from cost. Rows affected are marked *.
      Add rates in <code>aiusage/pricing.py</code>.</div></div>`;
  }

  app.innerHTML = html;

  stackedBars(document.getElementById('c-cost'), {
    days, series: providers, byDay: DATA.cost_by_day, tickFmt: dayOfMonth,
    slots: DATA.provider_slot, fmt: usd, label: 'Daily cost by provider, ' + title,
  });
  stackedBars(document.getElementById('c-tok'), {
    days, series, byDay: DATA.tokens_by_day, tickFmt: dayOfMonth,
    slots: DATA.model_slot, fmt: tok, label: 'Daily tokens by model, ' + title,
  });
}

render();
addEventListener('hashchange', () => { render(); scrollTo(0, 0); });
let t; addEventListener('resize', () => { clearTimeout(t); t = setTimeout(render, 180); });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);

/* `collect.py --serve` exposes /api/data, which re-runs the collectors and
   returns fresh JSON, so a poll or click re-renders in place with no reload.
   Opened as a plain file:// page there's no server to fetch from -- the only
   way to pick up newer data is a full reload of whatever the last
   ./collect.py run (or the launchd schedule) wrote to disk, so that's the
   fallback. */
async function refreshData() {
  const btn = document.getElementById('refresh-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
  try {
    const res = await fetch('/api/data', { cache: 'no-store' });
    if (res.status >= 500) {
      // A server that answered and failed. Reloading would just re-run the
      // same broken collection, so keep the numbers on screen and say so.
      const body = await res.json().catch(() => ({}));
      showError(body.error || 'the collector failed on the server');
      return;
    }
    if (!res.ok) throw new Error('bad response');
    Object.assign(DATA, await res.json());
    render();
    // A 200 can still carry a failed source: the other one's data is fresh,
    // so the figures are shown, with the failure said above them.
    showError(DATA.refresh_error, true);
  } catch (err) {
    // No answer at all: either this is a plain file:// page with no server to
    // ask, or the server went away. Either way the only newer data available
    // is whatever the last ./collect.py wrote to disk.
    location.reload();
    return;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
}

function showError(msg, partial) {
  let el = document.getElementById('refresh-error');
  if (!msg) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'refresh-error';
    el.className = 'note';
    el.style.borderLeftColor = 'var(--s8)';
    el.style.margin = '0 0 18px';
    document.getElementById('app').prepend(el);
  }
  el.textContent = partial
    ? 'Last refresh was incomplete: ' + msg + '.'
    : 'Last refresh failed: ' + msg
      + ' \u2014 the figures below are from ' + DATA.generated_at + '.';
}

document.getElementById('refresh-btn').addEventListener('click', refreshData);
const REFRESH_MS = __REFRESH_MS__;
if (REFRESH_MS > 0) setInterval(refreshData, REFRESH_MS);
</script>
</body>
</html>
"""
