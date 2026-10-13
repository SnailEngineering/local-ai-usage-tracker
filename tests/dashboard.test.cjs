// These are JavaScript behavior tests with a small DOM test double, not a
// browser/accessibility audit. No packages, network, or user data are involved.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const fixture = spawnSync(process.env.PYTHON || 'python3', ['-m', 'tests.dashboard_fixture'],
  {cwd: root, encoding: 'utf8'});
assert.equal(fixture.status, 0, fixture.stderr);
const payload = JSON.parse(fixture.stdout);
const source = fs.readFileSync(path.join(root, 'aiusage/templates/dashboard.js'), 'utf8');

function browserDouble(options = {}) {
  const document = {hidden: false};
  class Element {
    constructor(tag, attrs = {}, parent = null) {
      this.tag = tag; this.attrs = attrs; this.parent = parent; this.children = [];
      this.listeners = {}; this.style = {}; this._html = ''; this.textContent = '';
      this.scrollLeft = this.scrollTop = 0; this.clientWidth = 900;
      this.open = 'open' in attrs;
    }
    get id() { return this.attrs.id || ''; }
    set id(value) { this.attrs.id = value; }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    getAttribute(key) { return this.attrs[key] ?? null; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    emit(type, event = {}) {
      for (const fn of this.listeners[type] || []) fn({target: this, preventDefault() {}, ...event});
    }
    contains(el) { return el === this || this.children.some(c => c.contains(el)); }
    matches(selector) {
      const tag = selector.match(/^[\w-]+/)?.[0];
      const id = selector.match(/#([\w-]+)/)?.[1];
      const cls = selector.match(/\.([\w-]+)/)?.[1];
      return (!tag || tag === this.tag) && (!id || id === this.id)
        && (!cls || (this.attrs.class || '').split(' ').includes(cls))
        && (!selector.includes('[id]') || !!this.id);
    }
    querySelectorAll(selector) {
      return this.children.flatMap(c => [...(c.matches(selector) ? [c] : []), ...c.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
    focus() {
      document.activeElement?.emit('blur'); document.activeElement = this; this.emit('focus');
    }
    getBoundingClientRect() { return {width: 900, left: 0}; }
    get innerHTML() { return this._html; }
    set innerHTML(html) {
      if (this.contains(document.activeElement)) document.activeElement = document.body;
      this._html = html; this.children = [];
      const stack = [this];
      for (const match of html.matchAll(/<(\/?)([\w-]+)([^>]*)>/g)) {
        const [, closing, tag, rest] = match;
        if (closing) { if (stack.length > 1) stack.pop(); continue; }
        const attrs = {};
        for (const a of rest.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] ?? '';
        const el = new Element(tag, attrs, stack.at(-1));
        stack.at(-1).children.push(el);
        if (!rest.endsWith('/') && !['br', 'hr', 'input', 'img', 'meta', 'link'].includes(tag)) stack.push(el);
      }
    }
  }
  document.body = new Element('body');
  document.activeElement = document.body;
  document.body.innerHTML = '<button id="refresh-btn"></button><button id="pause-btn"></button>'
    + '<span id="stamp"></span><div id="refresh-error"></div><p id="refresh-status"></p><main id="app"></main>';
  document.getElementById = id => document.body.querySelector('#' + id);
  let serial = 0, reloads = 0, calls = 0;
  const timeouts = new Map(), intervals = new Map(), events = {}, storage = new Map();
  const session = options.session || new Map();
  if (options.paused) storage.set('aiusage-auto-paused', 'true');
  const data = structuredClone(options.data || payload);
  const context = vm.createContext({
    document, console, AbortController,
    location: {protocol: options.protocol || 'http:', hash: '', pathname: '/dashboard.html', reload: () => reloads++},
    getComputedStyle: () => ({getPropertyValue: () => '#1f62af'}),
    addEventListener: (type, fn) => { events[type] = fn; },
    matchMedia: () => ({addEventListener() {}}), scrollTo() {}, scrollX: 0, scrollY: 0,
    setTimeout: (fn, ms) => { const id = ++serial; timeouts.set(id, {fn, ms}); return id; },
    clearTimeout: id => timeouts.delete(id),
    setInterval: (fn, ms) => { const id = ++serial; intervals.set(id, {fn, ms}); return id; },
    clearInterval: id => intervals.delete(id),
    sessionStorage: {getItem: key => session.get(key), setItem: (key, value) => session.set(key, value),
      removeItem: key => session.delete(key)},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    fetch: async (...args) => { calls++; return options.fetch ? options.fetch(...args)
      : {ok: true, json: async () => structuredClone(data)}; },
  });
  vm.runInContext(source.replace('__DATA__', JSON.stringify(data)).replace('__REFRESH_MS__', '60000'), context);
  const api = vm.runInContext('({refreshData, render, redraw, dailyTable, DATA})', context);
  return {document, context, api, events, storage, session, timeouts, intervals,
    el: id => document.getElementById(id), reloads: () => reloads, calls: () => calls};
}

test('network failure retains the loaded HTTP view; successful retry clears status', async () => {
  let failed = true;
  const b = browserDouble({fetch: async () => {
    if (failed) throw new Error('disconnected');
    return {ok: true, json: async () => ({...payload, refresh_error: null})};
  }});
  const view = b.el('app').innerHTML;
  await b.api.refreshData(true);
  assert.equal(b.reloads(), 0);
  assert.equal(b.el('app').innerHTML, view);
  assert.match(b.el('refresh-error').textContent, /disconnected/);
  assert.equal(b.el('refresh-btn').disabled, false);
  failed = false;
  await b.api.refreshData(true);
  assert.equal(b.el('refresh-error').hidden, true);
  assert.match(b.el('refresh-status').textContent, /Usage updated/);
});

test('server errors keep figures and file refresh reloads without fetching', async () => {
  const b = browserDouble({fetch: async () => ({ok: false, status: 500,
    json: async () => ({error: 'collector failed'})})});
  await b.api.refreshData();
  assert.equal(b.reloads(), 0);
  assert.match(b.el('refresh-error').textContent, /collector failed/);
  const file = browserDouble({protocol: 'file:'});
  await file.api.refreshData();
  assert.equal(file.calls(), 0);
  assert.equal(file.reloads(), 1);
});

test('file pages auto-reload at most every 10 minutes; served pages keep the interval', () => {
  assert.equal([...browserDouble({protocol: 'file:'}).intervals.values()][0].ms, 600000);
  assert.equal([...browserDouble().intervals.values()][0].ms, 60000);
});

test('refresh requests cannot overlap and stalled requests time out', async () => {
  const b = browserDouble({fetch: (_, {signal}) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(Object.assign(new Error(), {name: 'AbortError'})));
  })});
  const first = b.api.refreshData();
  await b.api.refreshData();
  assert.equal(b.calls(), 1);
  [...b.timeouts.values()].find(t => t.ms === 30000).fn();
  await first;
  assert.match(b.el('refresh-error').textContent, /timed out/);
  assert.equal(b.el('refresh-btn').disabled, false);
  assert.equal(b.timeouts.size, 0);
});

test('pause persists, resuming schedules updates, and hidden tabs do not poll', () => {
  const b = browserDouble({paused: true});
  assert.equal(b.intervals.size, 0);
  assert.equal(b.el('pause-btn').textContent, 'Resume auto-refresh');
  b.el('pause-btn').emit('click');
  assert.equal(b.intervals.size, 1);
  assert.equal(b.el('pause-btn').textContent, 'Pause auto-refresh');
  b.document.hidden = true;
  [...b.intervals.values()][0].fn();
  assert.equal(b.calls(), 0);
  b.el('pause-btn').emit('click');
  assert.equal(b.intervals.size, 0);
  assert.equal(b.storage.get('aiusage-auto-paused'), 'true');
});

test('polls preserve keyboard focus, disclosure state, and table scroll', async () => {
  const b = browserDouble();
  const link = b.el('app').querySelectorAll('a').find(a => a.getAttribute('href') === '#month=2026-08');
  link.focus();
  b.el('run-log').open = false;
  b.el('c-cost-data').open = true;
  b.el('c-cost-data').emit('toggle');
  const region = b.el('c-cost-data').querySelector('.scroll');
  region.scrollLeft = 37; region.scrollTop = 22;
  await b.api.refreshData();
  assert.equal(b.document.activeElement.getAttribute('href'), '#month=2026-08');
  assert.equal(b.el('run-log').open, false);
  assert.equal(b.el('c-cost-data').open, true);
  assert.equal(b.el('c-cost-data').querySelector('.scroll').scrollTop, 22);
  assert.equal(b.el('c-cost-data').querySelector('.scroll').scrollLeft, 37);
});

test('resize keeps non-chart nodes and errors; navigation focuses the view heading', async () => {
  const b = browserDouble({fetch: async () => { throw new Error('offline'); }});
  await b.api.refreshData();
  const log = b.el('run-log'); log.open = false;
  b.api.redraw();
  assert.equal(b.el('run-log'), log);
  assert.match(b.el('refresh-error').textContent, /offline/);
  b.context.location.hash = '#month=2026-08'; b.events.hashchange();
  assert.equal(b.document.activeElement.id, 'view-heading');
  assert.match(b.el('refresh-error').textContent, /offline/);
  b.context.location.hash = ''; b.events.hashchange();
  assert.equal(b.el('run-log').open, false);
});

test('chart keyboard and pointer inspection expose exact values and preserve selection', async () => {
  const b = browserDouble();
  const hit = b.el('c-tok-hit'); hit.focus();
  assert.match(hit.getAttribute('aria-valuetext'), /1,234,/);
  hit.emit('keydown', {key: 'End'});
  assert.equal(hit.getAttribute('aria-valuenow'), '3');
  await b.api.refreshData();
  assert.equal(b.document.activeElement.id, 'c-tok-hit');
  assert.equal(b.document.activeElement.getAttribute('aria-valuenow'), '3');
  b.document.activeElement.emit('keydown', {key: 'ArrowLeft'});
  assert.equal(b.document.activeElement.getAttribute('aria-valuenow'), '2');
  b.document.activeElement.emit('pointerdown', {clientX: 60});
  assert.equal(b.document.activeElement.getAttribute('aria-valuenow'), '1');
});

test('daily tables are lazy, exact, labelled, and escaped; legacy coarse dates are escaped', () => {
  const data = structuredClone(payload);
  data.coarse = [{day: '<img src=x onerror=alert(1)>', tokens: 123}];
  const b = browserDouble({data});
  assert.ok(!b.el('app').innerHTML.includes('<img'));
  assert.ok(b.el('app').innerHTML.includes('&lt;img'));
  const detail = b.el('c-tok-data');
  assert.equal(detail.querySelector('.scroll').innerHTML, '');
  detail.open = true; detail.emit('toggle');
  const html = detail.querySelector('.scroll').innerHTML;
  assert.match(html, /<caption>Daily tokens by model/);
  assert.match(html, /scope="row"/);
  assert.match(html, /1,234,/);
  const escaped = b.api.dailyTable({days: ['<date>'], series: ['<model>'], byDay: {}, label: '<label>', fmt: String});
  assert.ok(!escaped.includes('<model>'));
  assert.match(escaped, /&lt;date&gt;/);
});

test('month and project dates are escaped at every sink', () => {
  const data = structuredClone(payload);
  const bad = '"><img src=x>';
  data.months[0].month = bad;
  data.projects = [{project: 'p', messages: 1, total_tokens: 1, last_day: bad, cost_usd: 0, unpriced: false}];
  const b = browserDouble({data});
  const html = b.el('app').innerHTML;
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&quot;&gt;&lt;img'));
});

test('small text colors meet 4.5:1 in both themes', () => {
  const css = fs.readFileSync(path.join(root, 'aiusage/templates/dashboard.css'), 'utf8');
  function lum(hex) {
    const rgb = hex.match(/[a-f\d]{2}/gi).map(c => parseInt(c, 16) / 255)
      .map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
    return rgb.reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
  }
  for (const block of [...css.matchAll(/:root[^{}]*\{([^}]+)\}/g)].map(m => m[1])) {
    const colors = Object.fromEntries([...block.matchAll(/--([\w-]+):(#\w{6})/g)].map(m => [m[1], m[2]]));
    for (const key of ['ink', 'ink-2', 'muted', 'provider-anthropic', 'provider-openai', 'positive', 'negative']) {
      for (const bg of ['surface', 'page']) {
        const [lo, hi] = [lum(colors[key]), lum(colors[bg])].sort((a, b) => a - b);
        assert.ok((hi + .05) / (lo + .05) >= 4.5, `${key} on ${bg}: ${colors[key]}`);
      }
    }
  }
});

test('empty and fully unpriced datasets render without a frontend exception', () => {
  const empty = structuredClone(payload); empty.days = [];
  assert.match(browserDouble({data: empty}).el('app').innerHTML, /No usage recorded/);
  const unknown = structuredClone(payload); unknown.cost_by_day = {};
  assert.ok(browserDouble({data: unknown}).el('c-cost-hit'));
});

test('file reload restores disclosure state and focus within the tab', async () => {
  const b = browserDouble({protocol: 'file:'});
  b.el('run-log').open = false;
  b.el('run-log-summary').focus();
  await b.api.refreshData();
  const reloaded = browserDouble({protocol: 'file:', session: b.session});
  assert.equal(reloaded.el('run-log').open, false);
  assert.equal(reloaded.document.activeElement.id, 'run-log-summary');
});
