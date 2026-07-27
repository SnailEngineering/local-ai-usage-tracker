"""Static dashboard generator.

Queries the database, prices tokens at render time, and writes one
self-contained HTML file. No server, no build step, no network at view time --
open it from a bookmark.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import pricing

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


def _build_payload(conn: sqlite3.Connection) -> dict:
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

    # Monthly table, mirroring the shape ccusage prints.
    months: dict[str, dict] = {}
    for r in rows:
        m = r["day"][:7]
        e = months.setdefault(m, {
            "month": m, "models": set(), "input_tokens": 0, "output_tokens": 0,
            "cache_write": 0, "cache_read": 0, "total_tokens": 0,
            "cost_usd": 0.0, "unpriced": False,
        })
        e["models"].add(r["model"])
        e["input_tokens"] += r["input_tokens"]
        e["output_tokens"] += r["output_tokens"]
        e["cache_write"] += r["cache_write_5m_tokens"] + r["cache_write_1h_tokens"]
        e["cache_read"] += r["cache_read_tokens"]
        e["total_tokens"] += r["total_tokens"]
        if r["priced"]:
            e["cost_usd"] += r["cost_usd"]
        else:
            e["unpriced"] = True
    month_rows = [
        {**e, "models": sorted(e["models"])}
        for e in sorted(months.values(), key=lambda x: x["month"])
    ]

    # Per-project spend (project comes from the session's cwd, both sources set it).
    proj: dict[str, dict] = {}
    for r in conn.execute("""
        SELECT COALESCE(project,'(unknown)') AS project, provider, model,
               SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
               SUM(cache_write_5m_tokens) cache_write_5m_tokens,
               SUM(cache_write_1h_tokens) cache_write_1h_tokens,
               SUM(cache_read_tokens) cache_read_tokens,
               MAX(day) last_day, COUNT(*) n
        FROM usage_event WHERE project IS NOT NULL
        GROUP BY project, provider, model
    """):
        d = dict(r)
        d["day"] = d["last_day"]
        d["model"] = pricing.normalize_model(d["model"])
        c = pricing.cost_usd(d)
        e = proj.setdefault(d["project"], {
            "project": d["project"], "cost_usd": 0.0, "total_tokens": 0,
            "last_day": d["last_day"], "messages": 0,
        })
        e["cost_usd"] += c or 0.0
        e["total_tokens"] += (
            d["input_tokens"] + d["output_tokens"] + d["cache_write_5m_tokens"]
            + d["cache_write_1h_tokens"] + d["cache_read_tokens"]
        )
        e["messages"] += d["n"]
        e["last_day"] = max(e["last_day"], d["last_day"])
    project_rows = sorted(proj.values(), key=lambda x: -x["cost_usd"])[:15]

    # Coarse backfill: days we know were active but can no longer price.
    coarse = [
        {"day": r["day"], "tokens": r["t"]}
        for r in conn.execute(
            "SELECT day, SUM(total_tokens) t FROM coarse_daily_tokens "
            "WHERE day NOT IN (SELECT DISTINCT day FROM usage_event) GROUP BY day ORDER BY day"
        )
    ]

    totals = {
        "cost_usd": sum(r["cost_usd"] for r in rows if r["priced"]),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "cache_read": sum(r["cache_read_tokens"] for r in rows),
        "output": sum(r["output_tokens"] for r in rows),
        "active_days": len(days),
        "events": sum(r["n_events"] for r in rows),
    }

    runs = [dict(r) for r in conn.execute(
        "SELECT source, status, detail, finished_at FROM run_log ORDER BY id DESC LIMIT 10")]

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
        "projects": project_rows,
        "coarse": coarse,
        "totals": totals,
        "unpriced": {"tokens": unpriced_tokens, "models": sorted(unpriced_models)},
        "runs": runs,
    }


def build(conn: sqlite3.Connection, out_path: Path) -> Path:
    payload = _build_payload(conn)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    out_path.write_text(html, encoding="utf-8")
    return out_path


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
    --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
    --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --page:#0d0d0d; --surface:#1a1a19;
      --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
      --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
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
  h2 { font-size:15px; font-weight:620; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 16px; }

  .card {
    background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:20px 22px; margin-bottom:20px;
  }

  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:14px; margin-bottom:20px; }
  .tile { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }
  .tile .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
  .tile .v { font-size:27px; font-weight:640; margin-top:6px; letter-spacing:-0.02em; }
  .tile .n { color:var(--ink-2); font-size:12px; margin-top:4px; }

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
  .hit:hover ~ .crosshair { opacity:1; }

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

  .note { font-size:12.5px; color:var(--ink-2); border-left:2px solid var(--s4);
          padding:2px 0 2px 12px; margin-top:16px; }
  .empty { color:var(--muted); padding:26px 0; text-align:center; }
  details.runs { margin-top:8px; }
  details.runs summary { cursor:pointer; color:var(--muted); font-size:12.5px; }
  details.runs table { margin-top:10px; }
  .ok { color:var(--s6); } .err { color:var(--s8); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AI Usage</h1>
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

function colorOf(key, slots) {
  const i = slots[key];
  return cssVar(SLOT[(i === undefined ? 7 : i) % 8]);
}

/* Stacked bar chart with a crosshair + per-day tooltip.
   Series identity is carried by the legend and the tooltip swatches, never by
   colour alone. */
function stackedBars(el, opts) {
  const { days, series, byDay, slots, fmt, label } = opts;
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
               fill="${colorOf(k, slots)}"/>`;
      acc += v;
    });
  });

  // Date ticks: first, last, and a few evenly spaced between.
  let ticks = '';
  const every = Math.max(1, Math.ceil(days.length / 7));
  days.forEach((d, i) => {
    if (i % every === 0 || i === days.length - 1) {
      ticks += `<text class="tick" x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle">${d.slice(5)}</text>`;
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

function render() {
  document.getElementById('stamp').textContent = 'updated ' + DATA.generated_at;
  const app = document.getElementById('app');
  const T = DATA.totals;

  if (!DATA.days.length) {
    app.innerHTML = `<div class="card"><div class="empty">No usage recorded yet.<br>
      Run <code>./collect.py</code> to populate the database.</div></div>`;
    return;
  }

  const cacheShare = T.total_tokens ? (T.cache_read / T.total_tokens * 100) : 0;
  const span = DATA.days[0] + ' → ' + DATA.days[DATA.days.length - 1];

  let html = `
  <div class="tiles">
    <div class="tile"><div class="k">Total cost</div><div class="v">${usd2(T.cost_usd)}</div>
      <div class="n">${span}</div></div>
    <div class="tile"><div class="k">Tokens</div><div class="v">${tok(T.total_tokens)}</div>
      <div class="n">${num(T.events)} messages</div></div>
    <div class="tile"><div class="k">Cache reads</div><div class="v">${cacheShare.toFixed(1)}%</div>
      <div class="n">of all tokens</div></div>
    <div class="tile"><div class="k">Active days</div><div class="v">${T.active_days}</div>
      <div class="n">${usd2(T.cost_usd / Math.max(T.active_days, 1))} / day</div></div>
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
    <p class="sub">Same shape as <code>ccusage monthly</code>, but it keeps growing.</p>
    <div class="scroll"><table>
      <thead><tr><th>Month</th><th>Input</th><th>Output</th><th>Cache write</th>
        <th>Cache read</th><th>Total</th><th>Cost</th></tr></thead>
      <tbody>${DATA.months.map(m => `<tr>
        <td>${m.month}<div class="models">${m.models.map(esc).join(', ')}</div></td>
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
          <td>${p.last_day}</td><td class="num-strong">${usd2(p.cost_usd)}</td></tr>`).join('')}</tbody>
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
          <td>${esc(r.source)}</td>
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

render();
let t; addEventListener('resize', () => { clearTimeout(t); t = setTimeout(render, 180); });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);
</script>
</body>
</html>
"""
