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
    <svg viewBox="0 0 ${W} ${H}" role="group" aria-label="${esc(label)}">
      <g aria-hidden="true">${g}${bars}
      <line class="baseline" x1="${padL}" x2="${padL + iw}" y1="${y(0)}" y2="${y(0)}"/>
      ${ticks}
      <line id="ch" class="baseline" y1="${padT}" y2="${padT + ih}" style="opacity:0" stroke-dasharray="3 3"/>
      </g>
      <rect class="hit" id="${el.id}-hit" x="${padL}" y="${padT}" width="${iw}" height="${ih}"
        tabindex="0" role="slider" aria-label="${esc(label)}: choose a day with arrow keys"
        aria-valuemin="1" aria-valuemax="${Math.max(1, days.length)}" aria-valuenow="1"/>

    </svg>
    <div class="tip" aria-hidden="true"></div>
    <details class="daily-data" id="${el.id}-data">
      <summary id="${el.id}-summary">View daily values as a table</summary>
      <div class="scroll" tabindex="0" role="region" aria-label="${esc(label)} daily values"></div>
    </details>`;

  const svg = el.querySelector('svg'), tip = el.querySelector('.tip'), ch = el.querySelector('#ch');
  const hit = el.querySelector('.hit');

  let selected = 0;
  const exact = opts.valueFmt || fmt;
  const valueText = i => days[i] + ': ' + series.map(k =>
    k + ' ' + exact((byDay[days[i]] || {})[k] || 0)).join(', ');
  hit.setAttribute('aria-valuetext', valueText(0));
  function showDay(index) {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(days.length - 1, index));
    selected = i;
    hit.setAttribute('aria-valuenow', String(i + 1));
    hit.setAttribute('aria-valuetext', valueText(i));
    const d = days[i], vals = byDay[d] || {};

    ch.setAttribute('x1', x(i)); ch.setAttribute('x2', x(i)); ch.style.opacity = '.5';

    const parts = series
      .map(k => [k, vals[k] || 0]).filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<div class="tr"><span class="swatch" style="background:${colorOf(k, slots)}"></span>${esc(k)}<b>${exact(v)}</b></div>`)
      .join('');

    tip.innerHTML = `<div class="th">${esc(d)}</div>${parts || '<div class="tr">no usage</div>'}`
      + `<div class="tt">total<b>${exact(totals[i])}</b></div>`;
    tip.style.opacity = '1';

    const px = (x(i) / W) * r.width;
    tip.style.left = Math.min(Math.max(px - 75, 0), r.width - 190) + 'px';
    tip.style.top = '4px';
  }
  hit.selectDay = showDay;
  const hide = () => { tip.style.opacity = '0'; ch.style.opacity = '0'; };
  const point = ev => {
    const r = svg.getBoundingClientRect();
    const sx = (ev.clientX - r.left) * (W / r.width);
    showDay(Math.round((sx - padL) / (iw / Math.max(days.length, 1)) - 0.5));
  };
  hit.addEventListener('pointermove', point);
  hit.addEventListener('pointerdown', point);
  hit.addEventListener('pointerleave', () => { if (document.activeElement !== hit) hide(); });
  hit.addEventListener('focus', () => showDay(selected));
  hit.addEventListener('blur', hide);
  hit.addEventListener('keydown', ev => {
    const next = {ArrowLeft: selected - 1, ArrowDown: selected - 1,
      ArrowRight: selected + 1, ArrowUp: selected + 1, Home: 0, End: days.length - 1};
    if (ev.key === 'Escape') { hide(); return; }
    if (next[ev.key] !== undefined) { ev.preventDefault(); showDay(next[ev.key]); }
  });
  // Render the potentially long table only when requested. Restored disclosure
  // state also fires toggle, keeping multi-year refreshes cheap when closed.
  const details = el.querySelector('details');
  details.populate = () => {
    const region = details.querySelector('.scroll');
    if (details.open && !region.innerHTML) region.innerHTML = dailyTable(opts);
  };
  details.addEventListener('toggle', details.populate);
}

function dailyTable({days, series, byDay, label, valueFmt, fmt}) {
  const format = valueFmt || fmt;
  return `<table><caption>${esc(label)} — daily values</caption><thead><tr>
    <th scope="col">Date</th>${series.map(k => `<th scope="col">${esc(k)}</th>`).join('')}
    <th scope="col">Total</th></tr></thead><tbody>${days.map(d => {
      const values = series.map(k => (byDay[d] || {})[k] || 0);
      return `<tr><th scope="row">${esc(d)}</th>${values.map(v => `<td>${format(v)}</td>`).join('')}
        <td>${format(values.reduce((a, b) => a + b, 0))}</td></tr>`;
    }).join('')}</tbody></table>`;
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

// Rebuilds preserve the user's position within a view. Keep navigation distinct
// from data refresh: changing month moves focus to the new heading, while a
// poll leaves the current keyboard target and disclosures where they were.
let currentView = null;
let redrawCharts = () => {};
const viewStates = new Map();
const fileStateKey = 'aiusage-view:' + location.pathname;
let fileScroll = null;
if (location.protocol === 'file:') {
  try {
    const saved = JSON.parse(sessionStorage.getItem(fileStateKey));
    sessionStorage.removeItem(fileStateKey);
    if (saved && typeof saved.view === 'string' && Array.isArray(saved.state?.details)
        && Array.isArray(saved.state?.scroll)) {
      viewStates.set(saved.view, saved.state);
      fileScroll = saved.scroll;
    }
  } catch (_) { /* File storage may be unavailable. Pause remains available. */ }
}
function rememberView() {
  const app = document.getElementById('app');
  const active = document.activeElement;
  const focus = app.contains(active)
    ? {id: active.id, href: active.getAttribute('href'), dayIndex: active.getAttribute('aria-valuenow')} : null;
  const state = {
    focus,
    details: Array.from(app.querySelectorAll('details[id]'), d => [d.id, d.open]),
    scroll: Array.from(app.querySelectorAll('.scroll'), d => [d.scrollLeft, d.scrollTop]),
  };
  if (currentView !== null) viewStates.set(currentView, state);
  return state;
}
function restoreView(state, navigating = false) {
  const app = document.getElementById('app');
  if (state) {
    for (const [id, open] of state.details) {
      const details = document.getElementById(id);
      if (details) {
        details.open = open;
        if (open && details.populate) details.populate();
      }
    }
  }
  app.querySelectorAll('.scroll').forEach((region, index) => {
    if (!region.id) region.id = 'scroll-region-' + index;
    if (state && state.scroll[index]) {
      [region.scrollLeft, region.scrollTop] = state.scroll[index];
    }
  });
  if (navigating) {
    document.getElementById('view-heading')?.focus({preventScroll: true});
  } else if (state?.focus) {
    const target = state.focus.id ? document.getElementById(state.focus.id)
      : Array.from(app.querySelectorAll('a')).find(a => a.getAttribute('href') === state.focus.href);
    if (target?.selectDay && state.focus.dayIndex) target.selectDay(Number(state.focus.dayIndex) - 1);
    (target || document.getElementById('view-heading'))?.focus({preventScroll: true});
  }
}
function redraw() {
  const state = rememberView();
  redrawCharts();
  restoreView(state);
}

function render() {
  rememberView();
  const nextView = routedMonth() || 'all';
  const navigating = currentView !== null && currentView !== nextView;
  currentView = nextView;
  document.getElementById('stamp').textContent = 'updated ' + DATA.generated_at;
  const app = document.getElementById('app');

  if (!DATA.days.length) {
    redrawCharts = () => {};
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
  restoreView(viewStates.get(currentView), navigating);
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
      return `<div class="provider-part" style="--provider-color:${cssVar("--provider-" + key)}"><span class="provider-name">${label}</span>
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
  <h2 id="view-heading" tabindex="-1" class="sr-only">All time usage</h2>
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
      <tbody>${DATA.months.slice().reverse().map(m => `<tr class="link" data-month="${m.month}">
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
    notes += `<div class="note">${DATA.coarse.length} earlier active days (${esc(lo)} → ${esc(hi)},
      ~${tok(t)} tokens) were recovered from Claude Code's stats cache. It records only a daily
      total per model with no input/output/cache split, so those days cannot be priced and are
      not shown in the charts above.</div>`;
  }

  html += `<div class="card">
    <h2>Collector</h2>
    <p class="sub">Last runs, newest first.</p>
    ${notes || ''}
    <details id="run-log" class="runs" open><summary id="run-log-summary">Run log</summary>
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

  redrawCharts = () => {
    stackedBars(document.getElementById('c-cost'), {
      days: DATA.days, series: DATA.providers, byDay: DATA.cost_by_day,
      slots: DATA.provider_slot, fmt: usd, valueFmt: usd2, label: 'Daily cost by provider',
    });
    stackedBars(document.getElementById('c-tok'), {
      days: DATA.days, series: DATA.models, byDay: DATA.tokens_by_day,
      slots: DATA.model_slot, fmt: tok, valueFmt: num, label: 'Daily tokens by model',
    });
  };
  redrawCharts();
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
    <h2 id="view-heading" tabindex="-1">${title}</h2>
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

  redrawCharts = () => {
    stackedBars(document.getElementById('c-cost'), {
      days, series: providers, byDay: DATA.cost_by_day, tickFmt: dayOfMonth,
      slots: DATA.provider_slot, fmt: usd, valueFmt: usd2, label: 'Daily cost by provider, ' + title,
    });
    stackedBars(document.getElementById('c-tok'), {
      days, series, byDay: DATA.tokens_by_day, tickFmt: dayOfMonth,
      slots: DATA.model_slot, fmt: tok, valueFmt: num, label: 'Daily tokens by model, ' + title,
    });
  };
  redrawCharts();
}

const REFRESH_MS = __REFRESH_MS__;
const REQUEST_TIMEOUT_MS = 30000;
let refreshing = false;
let refreshTimer;
let autoPaused = REFRESH_MS <= 0;
try { autoPaused = autoPaused || localStorage.getItem('aiusage-auto-paused') === 'true'; }
catch (_) { /* Storage can be unavailable on file:// or in private browsing. */ }

async function refreshData(manual = false) {
  if (refreshing) return;
  if (location.protocol === 'file:') {
    const state = rememberView();
    try {
      sessionStorage.setItem(fileStateKey, JSON.stringify({
        view: currentView, state, scroll: [scrollX, scrollY],
      }));
    } catch (_) {}
    location.reload();
    return;
  }
  refreshing = true;
  const btn = document.getElementById('refresh-btn');
  const hadFocus = document.activeElement === btn;
  btn.disabled = true;
  btn.textContent = 'Refreshing…';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch('/api/data', {cache: 'no-store', signal: controller.signal});
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || 'server returned HTTP ' + res.status);
    }
    const payload = await res.json();
    Object.assign(DATA, payload);
    render();
    showError(DATA.refresh_error, true);
    if (manual && !DATA.refresh_error) {
      document.getElementById('refresh-status').textContent = 'Usage updated ' + DATA.generated_at;
    }
  } catch (err) {
    // Keep the loaded HTTP page usable when the server is stopped or busy.
    // Reloading its URL cannot open the saved dashboard.html on disk.
    showError(err.name === 'AbortError' ? 'the request timed out'
      : err.message || 'the local server is unavailable');
  } finally {
    clearTimeout(timeout);
    refreshing = false;
    btn.disabled = false;
    btn.textContent = 'Refresh';
    if (hadFocus && document.activeElement === document.body) btn.focus({preventScroll: true});
  }
}

function showError(msg, partial = false) {
  const el = document.getElementById('refresh-error');
  el.hidden = !msg;
  el.textContent = !msg ? '' : partial
    ? 'Last refresh was incomplete: ' + msg + '.'
    : 'Last refresh failed: ' + msg + ' — the figures below are from '
      + DATA.generated_at + '. Use Refresh to retry.';
}

function scheduleRefresh() {
  clearInterval(refreshTimer);
  const btn = document.getElementById('pause-btn');
  // The label names the action, so it carries no aria-pressed as well: a
  // changing label plus a pressed state would announce the state twice.
  btn.textContent = (autoPaused ? 'Resume' : 'Pause') + ' auto-refresh';
  if (!autoPaused) {
    refreshTimer = setInterval(() => {
      if (!document.hidden) refreshData();
    }, Math.max(1000, REFRESH_MS || 60000));
  }
}

render();
if (Array.isArray(fileScroll) && fileScroll.length === 2) scrollTo(...fileScroll);
showError(DATA.refresh_error, true);
scheduleRefresh();
addEventListener('hashchange', () => { render(); scrollTo(0, 0); });
let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(redraw, 180);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redraw);
document.getElementById('refresh-btn').addEventListener('click', () => refreshData(true));
document.getElementById('pause-btn').addEventListener('click', () => {
  autoPaused = !autoPaused;
  try { localStorage.setItem('aiusage-auto-paused', String(autoPaused)); } catch (_) {}
  scheduleRefresh();
  document.getElementById('refresh-status').textContent = autoPaused
    ? 'Automatic refresh paused. Manual refresh is still available.'
    : 'Automatic refresh resumed.';
});
