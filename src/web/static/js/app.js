/**
 * app.js — Money Printer Web Dashboard
 *
 * Vanilla JS. No frameworks. No CDN dependencies (uPlot is vendored —
 * the production host is a Raspberry Pi with no guaranteed internet).
 *
 * Responsibilities:
 *   - WebSocket client with exponential-backoff reconnect
 *   - Section updaters for every snapshot key, with per-section
 *     change-hash skipping so unchanged sections never rebuild DOM
 *   - uPlot equity curve: LIVE stream + /api/portfolio_history ranges
 *   - Rolling stats / trade journal / log viewer panels (REST)
 *   - Bot start/stop controls (POST to REST API)
 *   - Light/dark theme toggle persisted to localStorage
 *   - Plugin registry for extensible section rendering
 */

"use strict";

/* ========================================================================== */
/* Convenience selector                                                         */
/* ========================================================================== */

const $ = id => document.getElementById(id);

/* ========================================================================== */
/* Formatting helpers                                                           */
/* ========================================================================== */

function formatCurrency(n) {
  if (n == null || isNaN(Number(n))) return { text: '--', cls: 'neu' };
  const v = Number(n);
  const abs = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const text = `${v < 0 ? '-' : ''}$${abs}`;
  const cls = v > 0.001 ? 'pos' : v < -0.001 ? 'neg' : 'neu';
  return { text, cls };
}

function formatPercent(n) {
  if (n == null || isNaN(Number(n))) return '--';
  return `${Number(n).toFixed(1)}%`;
}

function formatTime(ts) {
  if (!ts) return '--';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(d.getTime())) return String(ts);
  return d.toLocaleTimeString('en-US', { hour12: false });
}

function winPctNum(w, l) {
  const t = (w || 0) + (l || 0);
  return t > 0 ? Math.round((w / t) * 100) : 0;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtAge(sec) {
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  if (sec >= 60)   return `${Math.floor(sec / 60)}m`;
  return `${sec}s`;
}

/* ========================================================================== */
/* Theme system                                                                 */
/* ========================================================================== */

function _applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = $('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? 'LIGHT' : 'DARK';
}

function initTheme() {
  let theme = null;
  try { theme = localStorage.getItem('mp-theme'); } catch (e) { /* storage blocked */ }
  if (theme !== 'light' && theme !== 'dark') {
    theme = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches)
      ? 'light' : 'dark';
  }
  _applyTheme(theme);
}

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  try { localStorage.setItem('mp-theme', next); } catch (e) { /* storage blocked */ }
  _applyTheme(next);
  _rebuildChart(); // re-resolve axis/grid colors from CSS tokens
}

function _cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/* ========================================================================== */
/* Plugin registry                                                              */
/* ========================================================================== */

const sectionPlugins = {};

function registerSection(key, renderFn) {
  sectionPlugins[key] = renderFn;
}

/* ========================================================================== */
/* Equity chart (uPlot, vendored)                                               */
/*                                                                              */
/* LIVE streams snapshot.pnl_history; 1H/24H/7D/ALL fetch                       */
/* GET /api/portfolio_history?hours=N with a 60s client cache.                  */
/* ========================================================================== */

const CHART_HEIGHT = 170;
const HIST_CACHE_MS = 60000;
const RANGE_HOURS = { '1': 1, '24': 24, '168': 168, 'all': 2160 };

let _uplot = null;
let _resizeObserver = null;   // created once; survives _rebuildChart()
let _chartTrendUp = true;
let _liveHistory = null;      // last snapshot.pnl_history seen
let _chartRange = 'live';     // 'live' | '1' | '24' | '168' | 'all'
let _histCache = {};          // range -> { t, xs, ys }

function _chartStatus(msg) {
  const el = $('chart-status');
  if (el) el.textContent = msg || '';
}

function initChart() {
  const mount = $('pnl-chart');
  if (!mount) return;
  if (typeof uPlot === 'undefined') {
    _chartStatus('chart library missing');
    return;
  }

  const axisFont = '10px "JetBrains Mono", "Fira Code", monospace';
  const opts = {
    width: mount.clientWidth || 600,
    height: CHART_HEIGHT,
    legend: { show: false },
    cursor: { y: false },
    scales: { x: { time: true } },
    series: [
      {},
      {
        label: 'Equity',
        width: 1.5,
        // Functions are re-evaluated on every draw — trend recolor needs
        // only a setData/redraw, never a chart rebuild.
        stroke: () => _cssVar(_chartTrendUp ? '--green' : '--red', _chartTrendUp ? '#22c55e' : '#ef4444'),
        fill:   () => _cssVar(_chartTrendUp ? '--chart-fill-up' : '--chart-fill-down', 'rgba(34,197,94,0.1)'),
        points: { show: false },
      },
    ],
    axes: [
      {
        stroke: () => _cssVar('--text-dim', '#475569'),
        font: axisFont,
        grid:  { stroke: () => _cssVar('--border', '#1e293b'), width: 1 },
        ticks: { stroke: () => _cssVar('--border', '#1e293b'), width: 1 },
      },
      {
        stroke: () => _cssVar('--text-dim', '#475569'),
        font: axisFont,
        size: 64,
        values: (u, splits) => splits.map(v => formatCurrency(v).text),
        grid:  { stroke: () => _cssVar('--border', '#1e293b'), width: 1 },
        ticks: { stroke: () => _cssVar('--border', '#1e293b'), width: 1 },
      },
    ],
  };

  _uplot = new uPlot(opts, [[], []], mount);

  window.addEventListener('resize', _resizeChart); // same fn ref — browser dedupes
  if (typeof ResizeObserver === 'function') {
    // Create once and reuse across rebuilds (theme toggle destroys the uPlot
    // instance but not the mount); re-observing an observed node is a no-op.
    if (!_resizeObserver) _resizeObserver = new ResizeObserver(_resizeChart);
    _resizeObserver.observe(mount);
  }
}

function _resizeChart() {
  if (!_uplot) return;
  const mount = $('pnl-chart');
  if (!mount) return;
  const w = mount.clientWidth;
  if (w > 0 && w !== _uplot.width) _uplot.setSize({ width: w, height: CHART_HEIGHT });
}

function _rebuildChart() {
  if (!_uplot) return;
  const data = _uplot.data;
  _uplot.destroy();
  _uplot = null;
  initChart();
  if (_uplot && data) _uplot.setData(data);
}

function _setChartData(xs, ys) {
  if (!_uplot) return;
  _chartTrendUp = ys.length >= 2 ? ys[ys.length - 1] >= ys[0] : true;
  _uplot.setData([xs, ys]);
}

function _renderLive() {
  if (!_liveHistory) return;
  const xs = new Array(_liveHistory.length);
  const ys = new Array(_liveHistory.length);
  for (let i = 0; i < _liveHistory.length; i++) {
    xs[i] = _liveHistory[i].ts;
    ys[i] = _liveHistory[i].equity;
  }
  _setChartData(xs, ys);
}

/** Section updater: LIVE points come from snapshot.pnl_history. */
function updatePnLChart(history) {
  if (!Array.isArray(history) || history.length === 0) return;
  _liveHistory = history;
  if (_chartRange === 'live') _renderLive();
}

async function setChartRange(range) {
  _chartRange = range;

  const tabs = $('chart-range-tabs');
  if (tabs) {
    tabs.querySelectorAll('.range-tab').forEach(t =>
      t.classList.toggle('active', t.dataset.range === range));
  }

  if (range === 'live') {
    _chartStatus('');
    _renderLive();
    return;
  }

  const cached = _histCache[range];
  if (cached && Date.now() - cached.t < HIST_CACHE_MS) {
    _chartStatus(cached.xs.length === 0 ? 'no history in window' : '');
    _setChartData(cached.xs, cached.ys);
    return;
  }

  _chartStatus('loading…');
  const hours = RANGE_HOURS[range] || 24;
  try {
    const resp = await fetch(`/api/portfolio_history?hours=${hours}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    const hist = Array.isArray(body.history) ? body.history : [];
    const xs = new Array(hist.length);
    const ys = new Array(hist.length);
    for (let i = 0; i < hist.length; i++) {
      // ts is epoch seconds (number). Tolerate an old server's ISO string —
      // a mis-timed point beats an exception.
      const ts = hist[i].ts;
      xs[i] = typeof ts === 'number' ? ts : (Number(ts) || Date.parse(ts) / 1000);
      ys[i] = hist[i].equity;
    }
    _histCache[range] = { t: Date.now(), xs, ys };
    if (_chartRange !== range) return; // user switched tabs mid-fetch
    _chartStatus(hist.length === 0 ? 'no history in window' : '');
    _setChartData(xs, ys);
  } catch (err) {
    console.warn('[chart] portfolio_history fetch failed:', err);
    if (_chartRange === range) _chartStatus('history unavailable');
  }
}

function initChartRangeTabs() {
  const tabs = $('chart-range-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', e => {
    const tab = e.target.closest('.range-tab');
    if (tab && tab.dataset.range) setChartRange(tab.dataset.range);
  });
}

/* ========================================================================== */
/* Disconnected overlay                                                          */
/* ========================================================================== */

let _overlayEl = null;

function _getOrCreateOverlay() {
  if (_overlayEl) return _overlayEl;
  _overlayEl = document.createElement('div');
  _overlayEl.id = 'disconnect-overlay'; // styled in dashboard.css
  _overlayEl.textContent = 'DISCONNECTED — reconnecting…';
  document.body.appendChild(_overlayEl);
  return _overlayEl;
}

function showDisconnectedOverlay() {
  _getOrCreateOverlay().hidden = false;
}

function hideDisconnectedOverlay() {
  _getOrCreateOverlay().hidden = true;
}

/* ========================================================================== */
/* Section updaters                                                              */
/* ========================================================================== */

/**
 * Update mode pill. state_manager sends snapshot.mode = 'sandbox' | 'paper' | 'live'.
 * HTML: <span id="mode-pill" class="sandbox"> ... <span id="mode-text">SANDBOX</span>
 */
function updateMode(mode) {
  const pill = $('mode-pill');
  const text = $('mode-text');
  const m = (mode || 'sandbox').toLowerCase();
  if (pill) pill.className = m;   // CSS: #mode-pill.sandbox / #mode-pill.paper / #mode-pill.live
  if (text) text.textContent = m.toUpperCase();
}

/**
 * Update uptime clock.
 */
function updateUptime(uptime) {
  const el = $('uptime');
  if (el) el.textContent = uptime || '--:--:--';
}

/**
 * Update portfolio bar cards.
 * HTML IDs: pf-equity, pf-cash, pf-exposure, pf-realized, pf-unrealized
 * CSS classes on .pf-value: pos / neg / neu
 */
function updatePortfolio(pf) {
  if (!pf) return;

  const fields = [
    ['pf-equity',     pf.equity],
    ['pf-cash',       pf.cash],
    ['pf-exposure',   pf.exposure],
    ['pf-realized',   pf.realized_pnl],
    ['pf-unrealized', pf.unrealized_pnl],
  ];

  fields.forEach(([id, v]) => {
    const el = $(id);
    if (!el) return;
    const { text, cls } = formatCurrency(v);
    const prev = el.dataset.prev;
    el.textContent = text;
    // CSS selector is .pf-value.pos/.pf-value.neg/.pf-value.neu
    el.className = `pf-value ${cls}`;
    if (prev != null && Number(v) !== Number(prev)) {
      el.classList.remove('flash-up', 'flash-down');
      void el.offsetWidth; // force reflow to restart animation
      el.classList.add(Number(v) > Number(prev) ? 'flash-up' : 'flash-down');
    }
    el.dataset.prev = v;
  });

  // Exposure % appears in two places in the HTML
  const pct = formatPercent(pf.exposure_pct);
  const expPct  = $('pf-exposure-pct');
  const expPct2 = $('pf-exposure-pct2');
  if (expPct)  expPct.textContent  = pct;
  if (expPct2) expPct2.textContent = pct;
}

/**
 * Update market data feed rows.
 * HTML: <div id="market-list"> inside <div id="market-list-wrap">
 * CSS: .mkt-row, .mkt-sym, .mkt-price, .mkt-bid, .mkt-ask
 */
function updateMarketData(items) {
  const el    = $('market-list');
  const count = $('market-count');
  if (count) count.textContent = (items || []).length;
  if (!el) return;

  if (!items || items.length === 0) {
    el.innerHTML = '<div class="empty-state">No feeds</div>';
    return;
  }

  el.innerHTML = items.map(m => {
    const yesBid = Number(m.bid || 0);
    const yesAsk = Number(m.ask || 0);
    const noBid  = Number(m.no_bid || 0);
    const noAsk  = Number(m.no_ask || 0);
    const hasContract = yesBid > 0 || yesAsk > 0 || noBid > 0 || noAsk > 0;
    const priceStr = Number(m.price).toFixed(2);
    const fmtP = v => v > 0 ? v.toFixed(2) : '-';
    if (hasContract) {
      return `<div class="mkt-row mkt-row-contract">` +
        `<span class="mkt-sym" title="${escHtml(m.symbol)}">${escHtml(m.symbol)}</span>` +
        `<span class="mkt-price">${priceStr}</span>` +
        `<span class="mkt-yes-bid">${fmtP(yesBid)}</span>` +
        `<span class="mkt-yes-ask">${fmtP(yesAsk)}</span>` +
        `<span class="mkt-no-bid">${fmtP(noBid)}</span>` +
        `<span class="mkt-no-ask">${fmtP(noAsk)}</span>` +
        `</div>`;
    }
    return `<div class="mkt-row">` +
      `<span class="mkt-sym" title="${escHtml(m.symbol)}">${escHtml(m.symbol)}</span>` +
      `<span class="mkt-price" style="grid-column:2/-1">${priceStr}</span>` +
      `</div>`;
  }).join('');
}

/**
 * Update alerts panel.
 * HTML: <div id="alerts-list">
 * CSS: .alert-item, .alert-icon
 */
function updateAlerts(alerts) {
  const el    = $('alerts-list');
  const count = $('alerts-count');
  if (count) count.textContent = (alerts || []).length;
  if (!el) return;

  if (!alerts || alerts.length === 0) {
    el.innerHTML = '<div class="empty-state">No alerts</div>';
    return;
  }

  el.innerHTML = alerts.map(a =>
    `<div class="alert-item"><span class="alert-icon">&#9888;</span><span>${escHtml(a)}</span></div>`
  ).join('');
}

/* Alert marker: dashboard.alert() prefixes messages with U+1F6A8 */
const _ALERT_RE = /\u{1F6A8}|ALERT/iu;

/**
 * Update system log panel.
 * HTML: <div id="log-list">
 * CSS: .log-line, .log-line.log-alert
 */
function updateLogs(logs) {
  const el = $('log-list');
  if (!el || !Array.isArray(logs)) return;
  if (logs.length === 0) { el.innerHTML = ''; return; }

  el.innerHTML = logs.map(l => {
    const cls = _ALERT_RE.test(l) ? 'log-line log-alert' : 'log-line';
    return `<div class="${cls}">${escHtml(l)}</div>`;
  }).join('');

  el.scrollTop = el.scrollHeight;
}

/**
 * Update strategy performance table.
 * HTML: <tbody id="strategy-tbody"> inside <table id="strategy-table">
 * Columns: Strategy | Signals | Wins | Losses | Win Rate | PnL | Active
 */
function updateStrategyStats(stats) {
  const tbody = $('strategy-tbody');
  const count = $('strategy-count');
  const entries = Object.keys(stats || {}).map(k => [k, stats[k]]);
  if (count) count.textContent = entries.length;
  if (!tbody) return;

  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No signals yet</td></tr>';
    return;
  }

  tbody.innerHTML = entries
    .sort(([, a], [, b]) => (b.pnl || 0) - (a.pnl || 0))
    .map(([name, s]) => {
      const { text: pnlText, cls: pnlCls } = formatCurrency(s.pnl);
      const pct   = winPctNum(s.wins, s.losses);
      const color = pct >= 60 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)';
      return `<tr>` +
        `<td class="col-strat" title="${escHtml(name)}">${escHtml(name)}</td>` +
        `<td class="col-num">${s.signals || 0}</td>` +
        `<td class="col-num" style="color:var(--green)">${s.wins || 0}</td>` +
        `<td class="col-num" style="color:var(--red)">${s.losses || 0}</td>` +
        `<td><div class="winrate-cell"><div class="winrate-bar-bg"><div class="winrate-bar-fill" style="width:${pct}%;background:${color}"></div></div><span class="winrate-pct" style="color:${color}">${pct}%</span></div></td>` +
        `<td class="col-pnl ${pnlCls}">${pnlText}</td>` +
        `<td class="col-num">${s.active || 0}</td>` +
        `</tr>`;
    }).join('');
}

/**
 * Update open positions table.
 * HTML: <tbody id="positions-tbody"> inside <table id="positions-table">
 * Columns: ID | Symbol | Side | Contract | Qty | Entry | Current | PnL | Strategy | Age
 * The PnL cell carries a mini-bar scaled to the largest |pnl| on the board.
 */
function updatePositions(positions) {
  const tbody = $('positions-tbody');
  const count = $('positions-count');
  if (count) count.textContent = (positions || []).length;
  if (!tbody) return;

  if (!positions || positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No open positions</td></tr>';
    return;
  }

  const maxAbsPnl = Math.max(...positions.map(p => Math.abs(Number(p.pnl) || 0)), 0.0001);

  tbody.innerHTML = positions.map(p => {
    const { text: pnlText, cls: pnlCls } = formatCurrency(p.pnl);
    const sideCls = (p.side === 'buy' || p.side === 'BUY') ? 'buy' : 'sell';
    const barPct = Math.round(Math.abs(Number(p.pnl) || 0) / maxAbsPnl * 100);
    return `<tr>` +
      `<td class="col-id">${escHtml(String(p.id || ''))}</td>` +
      `<td class="col-symbol" title="${escHtml(p.symbol)}">${escHtml(p.symbol)}</td>` +
      `<td class="col-side ${sideCls}">${escHtml((p.side || '').toUpperCase())}</td>` +
      `<td class="col-contract">${escHtml(p.contract_side || 'YES')}</td>` +
      `<td class="col-num">${p.quantity}x</td>` +
      `<td class="col-num">${Number(p.entry).toFixed(2)}</td>` +
      `<td class="col-num">${Number(p.current).toFixed(2)}</td>` +
      `<td class="col-pnl ${pnlCls}"><div class="pnl-cell"><span>${pnlText}</span>` +
        `<div class="pnl-mini-bar"><div class="pnl-mini-fill ${pnlCls}" style="width:${barPct}%"></div></div></div></td>` +
      `<td class="col-strat" title="${escHtml(p.strategy)}">${escHtml(p.strategy)}</td>` +
      `<td class="col-age">${fmtAge(p.age || 0)}</td>` +
      `</tr>`;
  }).join('');
}

/**
 * Update bot chips and dropdown.
 * HTML: <div id="bot-chips">, <div id="bot-dropdown-menu">
 * CSS: .bot-chip.active / .bot-chip.inactive, .chip-dot
 */
function updateBots(bots) {
  _syncSelectedBotsWithActive(bots);
  _updateBotChips(bots);
  _rebuildBotDropdownIfChanged(bots);
}

let _knownBotNames = '';
let _pendingBots = {};

function _updateBotChips(bots) {
  const el = $('bot-chips');
  if (!el) return;
  if (!bots || bots.length === 0) { el.innerHTML = ''; return; }

  el.innerHTML = bots.map(b => {
    const cls = b.active ? 'active' : 'inactive';
    const pending = _pendingBots[b.name] ? ' pending' : '';
    return `<span class="bot-chip ${cls}${pending}" data-bot-chip="${escHtml(b.name)}"><span class="chip-dot"></span>${escHtml(b.name)}</span>`;
  }).join('');
}

function _syncSelectedBotsWithActive(bots) {
  if (!bots) return;
  bots.forEach(b => { _selectedBots[b.name] = b.active; });
  _updateDropdownLabel();
}

function _rebuildBotDropdownIfChanged(bots) {
  // Only rebuild when bot *names* change, not active status
  const names = (bots || []).map(b => b.name).sort().join(',');
  if (names === _knownBotNames) {
    // Names unchanged — just update checkbox state without rebuilding DOM
    _syncCheckboxStates(bots);
    return;
  }
  _knownBotNames = names;

  const menu = $('bot-dropdown-menu');
  if (!menu) return;

  if (!bots || bots.length === 0) {
    menu.innerHTML = '<div class="bot-option" style="color:var(--text-dim);cursor:default;font-size:11px;">No bots registered</div>';
    return;
  }

  menu.innerHTML = bots.map(b => {
    const checked = _selectedBots[b.name] ? 'checked' : '';
    return `<label class="bot-option"><input type="checkbox" value="${escHtml(b.name)}" ${checked} onchange="onBotCheckChange(this)" />${escHtml(b.name)}</label>`;
  }).join('');

  _updateDropdownLabel();
}

function _syncCheckboxStates(bots) {
  const menu = $('bot-dropdown-menu');
  if (!menu || !bots) return;
  const checkboxes = menu.querySelectorAll('input[type="checkbox"]');
  checkboxes.forEach(cb => {
    cb.checked = !!_selectedBots[cb.value];
  });
}

/**
 * Update mascot state chip and canvas animation.
 * HTML: <span id="mascot-state">
 * mascot.js exposes: window.setMascotState(state)
 */
const _MASCOT_CHIP_CLASSES = {
  IDLE:        'state-idle',
  RUNNING:     'state-running',
  MONEY_EYES:  'state-money',
  PANIC:       'state-panic',
  TINY_VIOLIN: 'state-violin',
};

function updateMascotState(state) {
  const s = (state || 'IDLE').toUpperCase();
  const el = $('mascot-state');
  if (el) {
    el.textContent = s;
    el.className = _MASCOT_CHIP_CLASSES[s] || 'state-idle';
  }
  if (typeof window.setMascotState === 'function') {
    window.setMascotState(s);
  }
}

/* ========================================================================== */
/* Bot dropdown (multi-select) — global functions called from HTML onclick      */
/* ========================================================================== */

var _selectedBots = {};

function toggleBotDropdown() {
  const btn  = $('bot-dropdown-btn');
  const menu = $('bot-dropdown-menu');
  if (btn)  btn.classList.toggle('open');
  if (menu) menu.classList.toggle('open');
}

document.addEventListener('click', function(e) {
  const wrap = document.querySelector('.bot-dropdown-wrap');
  if (wrap && !wrap.contains(e.target)) {
    const btn  = $('bot-dropdown-btn');
    const menu = $('bot-dropdown-menu');
    if (btn)  btn.classList.remove('open');
    if (menu) menu.classList.remove('open');
  }
});

function onBotCheckChange(cb) {
  _selectedBots[cb.value] = cb.checked;
  _updateDropdownLabel();
}

function _updateDropdownLabel() {
  const selected = Object.keys(_selectedBots).filter(k => _selectedBots[k]);
  const lbl = $('bot-dropdown-label');
  if (!lbl) return;
  if (selected.length === 0)      lbl.textContent = 'Select Bots';
  else if (selected.length === 1) lbl.textContent = selected[0];
  else                            lbl.textContent = `${selected.length} bots selected`;
}

function startSelected() {
  Object.keys(_selectedBots).forEach(name => {
    if (_selectedBots[name]) botAction(name, 'start');
  });
}

function stopSelected() {
  Object.keys(_selectedBots).forEach(name => {
    if (_selectedBots[name]) botAction(name, 'stop');
  });
}

/* ========================================================================== */
/* Bot control — REST API                                                        */
/* ========================================================================== */

/**
 * POST to /api/bots/{name}/start or /api/bots/{name}/stop
 * server.py routes: POST /api/bots/{name}/start  and  POST /api/bots/{name}/stop
 */
async function botAction(name, action) {
  const url = `/api/bots/${encodeURIComponent(name)}/${action}`;
  try {
    // When the server sets MP_CONTROL_TOKEN, POSTs need a matching X-MP-Token.
    // Operators store it once via localStorage.setItem('mp_token', '<token>').
    let headers;
    try {
      const token = localStorage.getItem('mp_token');
      if (token) headers = { 'X-MP-Token': token };
    } catch (_) { /* storage unavailable: send unauthenticated */ }
    const resp = await fetch(url, { method: 'POST', headers });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      console.warn(`[bots] ${action} ${name} failed:`, body.detail || resp.status);
      if (resp.status === 401) {
        console.warn("[bots] control token required: localStorage.setItem('mp_token', '<token>')");
      }
    }
  } catch (err) {
    console.error('[bots] fetch error:', err);
  }
}

// Delegate click events for bot chips — click to toggle start/stop
document.addEventListener('click', e => {
  const chip = e.target.closest('[data-bot-chip]');
  if (!chip) return;
  const name = chip.dataset.botChip;
  const isActive = chip.classList.contains('active');
  const action = isActive ? 'stop' : 'start';
  _pendingBots[name] = true;
  chip.classList.add('pending');
  botAction(name, action).finally(() => {
    // Clear pending and force a repaint on the next snapshot even if the
    // bots section hash is unchanged (e.g. the action failed).
    delete _pendingBots[name];
    delete _sectionHashes['bots'];
  });
});

// Delegate click events for inline start/stop buttons (future bots panel extension)
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const name   = btn.dataset.bot;
  if ((action === 'start' || action === 'stop') && name) {
    btn.disabled = true;
    btn.style.opacity = '0.4';
    botAction(name, action);
  }
});

/* ========================================================================== */
/* WebSocket client                                                              */
/* ========================================================================== */

// ws-dot: HTML id="ws-dot", CSS: #ws-dot and #ws-dot.connected
const WS_INDICATOR = $('ws-dot');
let _ws           = null;
let _reconnDelay  = 1000;
const WS_DELAY_MAX = 10000;

function _wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/ws`;

  _ws = new WebSocket(url);

  _ws.onopen = () => {
    if (WS_INDICATOR) WS_INDICATOR.classList.add('connected');
    hideDisconnectedOverlay();
    _reconnDelay = 1000;
    _sectionHashes = {}; // force a full repaint from the next snapshot
    console.info('[ws] connected');
  };

  _ws.onclose = () => {
    if (WS_INDICATOR) WS_INDICATOR.classList.remove('connected');
    showDisconnectedOverlay();
    console.info(`[ws] closed — reconnecting in ${_reconnDelay}ms`);
    setTimeout(_wsConnect, _reconnDelay);
    _reconnDelay = Math.min(_reconnDelay * 2, WS_DELAY_MAX);
  };

  _ws.onerror = () => {
    _ws.close();
  };

  _ws.onmessage = ev => {
    let snap;
    try { snap = JSON.parse(ev.data); }
    catch (e) { return; }
    _dispatchSnapshot(snap);
  };
}

/* ========================================================================== */
/* Snapshot dispatch — per-section change-hash skip (cheap on a Pi)             */
/* ========================================================================== */

let _sectionHashes = {};

// pnl_history gains a point every snapshot — hashing it would never skip,
// so don't pay the stringify cost on the largest section.
const _NO_HASH_KEYS = new Set(['pnl_history']);

function _dispatchSnapshot(snap) {
  for (const [key, fn] of Object.entries(sectionPlugins)) {
    if (!(key in snap)) continue;
    if (!_NO_HASH_KEYS.has(key)) {
      let hash = null;
      try { hash = JSON.stringify(snap[key]); } catch (e) { /* render unconditionally */ }
      if (hash !== null && _sectionHashes[key] === hash) continue;
      _sectionHashes[key] = hash;
    }
    try { fn(snap[key]); }
    catch (err) { console.error(`[section:${key}]`, err); }
  }
}

/* ========================================================================== */
/* Data Log updater                                                              */
/* ========================================================================== */

/**
 * Update data log panel.
 * HTML: <div id="datalog-list">
 * CSS: .datalog-row, .datalog-ts, .datalog-sym, .datalog-price, .datalog-type
 */
function updateDataLog(entries) {
  const el    = $('datalog-list');
  const count = $('datalog-count');
  if (count) count.textContent = (entries || []).length;
  if (!el) return;

  if (!entries || entries.length === 0) {
    el.innerHTML = '<div class="empty-state">No data log entries</div>';
    return;
  }

  el.innerHTML = entries.map(e => {
    const ts = (e.Timestamp || '').split('T')[1] || e.Timestamp || '';
    const tsShort = ts.length > 8 ? ts.substring(0, 8) : ts;
    return `<div class="datalog-row">` +
      `<span class="datalog-ts">${escHtml(tsShort)}</span>` +
      `<span class="datalog-sym">${escHtml(e.Symbol || '')}</span>` +
      `<span class="datalog-price">${escHtml(e.Price || '')}</span>` +
      `<span class="datalog-type">${escHtml(e.Type || '')}</span>` +
      `</div>`;
  }).join('');

  el.scrollTop = el.scrollHeight;
}

/* ========================================================================== */
/* Training History updater                                                      */
/* ========================================================================== */

/**
 * Update training history panel.
 * HTML: <div id="training-history-list"> inside <div id="training-history-card">
 * CSS: .th-entry, .th-entry-latest, .th-badge, .th-metric, .th-metric.good, .th-metric.bad
 *
 * Each entry in `history` is:
 *   { timestamp, cycle, diagnostics: {...}, cycle_record: {...} }
 */
function updateTrainingHistory(history) {
  const card  = $('training-history-card');
  const el    = $('training-history-list');
  const count = $('training-history-count');

  if (!history || history.length === 0) {
    if (card) card.style.display = 'none';
    return;
  }

  if (card) card.style.display = '';
  if (count) count.textContent = history.length;
  if (!el) return;

  // Render newest-first
  const reversed = history.slice().reverse();

  el.innerHTML = reversed.map((entry, idx) => {
    const cr = entry.cycle_record || {};
    const isLatest = idx === 0;
    const entryCls = isLatest ? 'th-entry th-entry-latest' : 'th-entry';

    // Format timestamp to time only
    let tsDisplay = '--';
    if (entry.timestamp) {
      const d = new Date(entry.timestamp);
      if (!isNaN(d.getTime())) {
        tsDisplay = d.toLocaleTimeString('en-US', { hour12: false });
      } else {
        tsDisplay = entry.timestamp;
      }
    }

    // Win rate color
    const wr = cr.win_rate || 0;
    const wrCls = wr >= 60 ? 'good' : wr < 40 ? 'bad' : '';

    // PnL color
    const pnl = cr.pnl || 0;
    const pnlCls = pnl > 0 ? 'good' : pnl < 0 ? 'bad' : '';

    // AUC color (>= 0.60 good, < 0.50 bad)
    const auc = cr.train_val_auc || 0;
    const aucCls = auc >= 0.60 ? 'good' : auc > 0 && auc < 0.50 ? 'bad' : '';

    // Build ML metrics line if available
    let mlLine = '';
    if (auc > 0 || cr.train_samples > 0) {
      const parts = [];
      if (auc > 0) parts.push(`<span class="th-metric ${aucCls}">AUC ${auc.toFixed(4)}</span>`);
      if (cr.train_samples > 0) parts.push(`<span class="th-metric">${cr.train_samples} samples</span>`);
      if (cr.train_contracts > 0) parts.push(`<span class="th-metric">${cr.train_contracts} contracts</span>`);
      mlLine = `<div class="th-ml">${parts.join('<span class="th-sep">|</span>')}</div>`;
    }

    const latestBadge = isLatest ? '<span class="th-badge">LATEST</span>' : '';

    return `<div class="${entryCls}">` +
      `<div class="th-header">` +
        `<span class="th-cycle">Cycle #${cr.cycle || entry.cycle || '?'}</span>` +
        `${latestBadge}` +
        `<span class="th-ts">${escHtml(tsDisplay)}</span>` +
      `</div>` +
      `<div class="th-metrics">` +
        `<span class="th-metric">${cr.trades || 0} trades</span>` +
        `<span class="th-sep">|</span>` +
        `<span class="th-metric ${wrCls}">${cr.wins || 0}W/${cr.losses || 0}L (${wr.toFixed(0)}%)</span>` +
        `<span class="th-sep">|</span>` +
        `<span class="th-metric ${pnlCls}">$${pnl.toFixed(2)}</span>` +
        `<span class="th-sep">|</span>` +
        `<span class="th-metric">${cr.duration_min || 0}min</span>` +
      `</div>` +
      `${mlLine}` +
    `</div>`;
  }).join('');
}

/* ========================================================================== */
/* Rolling Stats panel — GET /api/stats/rolling                                 */
/* ========================================================================== */

const ROLLING_REFRESH_MS = 60000;
let _rollingHours = 24;

async function fetchRollingStats() {
  try {
    const resp = await fetch(`/api/stats/rolling?hours=${_rollingHours}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _renderRollingStats(await resp.json());
  } catch (err) {
    console.warn('[rolling] fetch failed:', err);
  }
}

function _renderRollingStats(d) {
  const pnlEl = $('rs-pnl');
  if (pnlEl) {
    const { text, cls } = formatCurrency(d.pnl);
    pnlEl.textContent = text;
    pnlEl.className = `stat-value ${cls}`;
  }
  const wrEl = $('rs-winrate');
  if (wrEl) {
    wrEl.textContent = formatPercent(d.win_rate);
    wrEl.className = `stat-value ${d.win_rate >= 60 ? 'pos' : d.win_rate < 40 && d.trades > 0 ? 'neg' : 'neu'}`;
  }
  const wlEl = $('rs-wl');
  if (wlEl) wlEl.textContent = `${d.wins || 0}W / ${d.losses || 0}L`;
  const trEl = $('rs-trades');
  if (trEl) trEl.textContent = d.trades != null ? String(d.trades) : '--';
  const evEl = $('rs-ev');
  if (evEl) {
    const { text, cls } = formatCurrency(d.ev_per_trade);
    evEl.textContent = text;
    evEl.className = `stat-value ${cls}`;
  }

  const tbody = $('rolling-strat-tbody');
  if (!tbody) return;
  const entries = Object.entries(d.by_strategy || {});
  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No trades in window</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(([name, s]) => {
    const { text: pnlText, cls: pnlCls } = formatCurrency(s.pnl);
    const pct = Math.round(s.win_rate || 0);
    const color = pct >= 60 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)';
    return `<tr>` +
      `<td class="col-strat" title="${escHtml(name)}">${escHtml(name)}</td>` +
      `<td class="col-num">${s.trades || 0}</td>` +
      `<td><div class="winrate-cell"><div class="winrate-bar-bg"><div class="winrate-bar-fill" style="width:${pct}%;background:${color}"></div></div><span class="winrate-pct" style="color:${color}">${pct}%</span></div></td>` +
      `<td class="col-pnl ${pnlCls}">${pnlText}</td>` +
      `</tr>`;
  }).join('');
}

function initRollingStats() {
  const tabs = $('rolling-range-tabs');
  if (tabs) {
    tabs.addEventListener('click', e => {
      const tab = e.target.closest('.range-tab');
      if (!tab || !tab.dataset.hours) return;
      _rollingHours = Number(tab.dataset.hours);
      tabs.querySelectorAll('.range-tab').forEach(t =>
        t.classList.toggle('active', t === tab));
      fetchRollingStats();
    });
  }
  fetchRollingStats();
  setInterval(() => {
    if (document.visibilityState === 'visible') fetchRollingStats();
  }, ROLLING_REFRESH_MS);
}

/* ========================================================================== */
/* Trade Journal drawer — GET /api/journal                                      */
/* ========================================================================== */

const JOURNAL_PAGE_SIZE = 25;
let _journalTrades = null;   // chronological, as returned by the API
let _journalPage = 1;

function toggleJournalDrawer() {
  const card = $('journal-card');
  const body = $('journal-body');
  if (!body) return;
  body.hidden = !body.hidden;
  if (card) card.classList.toggle('open', !body.hidden);
  if (!body.hidden && _journalTrades === null) fetchJournal();
}

async function fetchJournal() {
  const tbody = $('journal-tbody');
  try {
    const resp = await fetch('/api/journal?last_n=200');
    if (resp.status === 404) {
      _journalTrades = [];
      if (tbody) tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No trade journal on disk yet</td></tr>';
      const count = $('journal-count');
      if (count) count.textContent = '0';
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    _journalTrades = Array.isArray(body.trades) ? body.trades : [];
    _journalPage = 1;
    _rebuildJournalFilter();
    renderJournal();
  } catch (err) {
    console.warn('[journal] fetch failed:', err);
    if (tbody) tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Journal unavailable</td></tr>';
  }
}

function _rebuildJournalFilter() {
  const sel = $('journal-filter');
  if (!sel || !_journalTrades) return;
  const prev = sel.value;
  const names = [...new Set(_journalTrades.map(t => t.strategy_name || 'Unknown'))].sort();
  sel.innerHTML = '<option value="">All strategies</option>' +
    names.map(n => `<option value="${escHtml(n)}">${escHtml(n)}</option>`).join('');
  if (names.includes(prev)) sel.value = prev;
}

function onJournalFilterChange() {
  _journalPage = 1;
  renderJournal();
}

function journalPage(delta) {
  _journalPage += delta;
  renderJournal();
}

function renderJournal() {
  const tbody = $('journal-tbody');
  if (!tbody || _journalTrades === null) return;

  const sel = $('journal-filter');
  const filter = sel ? sel.value : '';
  let rows = _journalTrades;
  if (filter) rows = rows.filter(t => (t.strategy_name || 'Unknown') === filter);

  const count = $('journal-count');
  if (count) count.textContent = String(rows.length);

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No trades match</td></tr>';
    _updateJournalPager(1, 1);
    return;
  }

  // Cumulative PnL in chronological order, then display newest-first
  let cum = 0;
  const withCum = rows.map(t => ({ t, cum: (cum += Number(t.pnl) || 0) }));
  withCum.reverse();

  const totalPages = Math.max(1, Math.ceil(withCum.length / JOURNAL_PAGE_SIZE));
  _journalPage = Math.min(Math.max(_journalPage, 1), totalPages);
  const start = (_journalPage - 1) * JOURNAL_PAGE_SIZE;
  const pageRows = withCum.slice(start, start + JOURNAL_PAGE_SIZE);

  tbody.innerHTML = pageRows.map(({ t, cum }) => {
    const { text: pnlText, cls: pnlCls } = formatCurrency(t.pnl);
    const { text: cumText, cls: cumCls } = formatCurrency(cum);
    const sideCls = String(t.side || '').toLowerCase() === 'buy' ? 'buy' : 'sell';
    const sideLabel = `${escHtml(String(t.side || '?').toUpperCase())} ${escHtml(t.contract_side || 'YES')}`;
    const when = formatTime(t.exit_time || t.entry_time);
    return `<tr title="${escHtml(t.close_reason || '')}">` +
      `<td class="col-time">${escHtml(when)}</td>` +
      `<td class="col-symbol" title="${escHtml(t.symbol || '')}">${escHtml(t.symbol || '')}</td>` +
      `<td class="col-side ${sideCls}">${sideLabel}</td>` +
      `<td class="col-num">${Number(t.quantity) || 0}x</td>` +
      `<td class="col-num">${(Number(t.entry_price) || 0).toFixed(2)}</td>` +
      `<td class="col-num">${(Number(t.exit_price) || 0).toFixed(2)}</td>` +
      `<td class="col-pnl ${pnlCls}">${pnlText}</td>` +
      `<td class="col-pnl ${cumCls}">${cumText}</td>` +
      `<td class="col-strat" title="${escHtml(t.strategy_name || '')}">${escHtml(t.strategy_name || 'Unknown')}</td>` +
      `</tr>`;
  }).join('');

  _updateJournalPager(_journalPage, totalPages);
}

function _updateJournalPager(page, totalPages) {
  const lbl = $('journal-page-label');
  if (lbl) lbl.textContent = `Page ${page}/${totalPages}`;
  const prev = $('journal-prev');
  const next = $('journal-next');
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= totalPages;
}

/* ========================================================================== */
/* Log Viewer drawer — GET /api/logs/tail                                       */
/* ========================================================================== */

let _logViewerLoaded = false;

function toggleLogViewerDrawer() {
  const card = $('logview-card');
  const body = $('logview-body');
  if (!body) return;
  body.hidden = !body.hidden;
  if (card) card.classList.toggle('open', !body.hidden);
  if (!body.hidden && !_logViewerLoaded) fetchLogTail();
}

async function fetchLogTail() {
  const out    = $('logview-output');
  const fileEl = $('logview-file');
  const status = $('logview-status');
  const input  = $('logview-pattern');
  const pattern = (input && input.value.trim()) || '*.log';
  if (status) status.textContent = 'loading…';
  try {
    const resp = await fetch(`/api/logs/tail?pattern=${encodeURIComponent(pattern)}&lines=200`);
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || !body.ok) {
      if (status) status.textContent = '';
      if (out) out.textContent = body.error || body.detail || `Request failed (${resp.status})`;
      if (fileEl) fileEl.textContent = '--';
      return;
    }
    _logViewerLoaded = true;
    if (status) status.textContent = `${body.lines} lines`;
    if (fileEl) fileEl.textContent = body.file || '--';
    if (out) {
      out.textContent = body.content || '(empty)';
      out.scrollTop = out.scrollHeight;
    }
  } catch (err) {
    if (status) status.textContent = '';
    if (out) out.textContent = `Fetch error: ${err}`;
  }
}

/* ========================================================================== */
/* Register built-in section updaters                                           */
/* ========================================================================== */

// Keys match exactly what state_manager.py's snapshot() returns:
//   mode, uptime, portfolio, market_data, alerts, logs,
//   strategy_stats, positions, pnl_history, bots, mascot_state
registerSection('mode',           updateMode);
registerSection('uptime',         updateUptime);
registerSection('portfolio',      updatePortfolio);
registerSection('market_data',    updateMarketData);
registerSection('alerts',         updateAlerts);
registerSection('logs',           updateLogs);
registerSection('strategy_stats', updateStrategyStats);
registerSection('positions',      updatePositions);
registerSection('bots',           updateBots);
registerSection('mascot_state',   updateMascotState);
registerSection('pnl_history',       updatePnLChart);
registerSection('data_log',          updateDataLog);
registerSection('training_history',  updateTrainingHistory);

/* ========================================================================== */
/* Grid column resizer                                                         */
/* ========================================================================== */

function initGridResizer() {
  const resizer = document.getElementById('grid-resizer');
  const grid = document.getElementById('main-grid');
  if (!resizer || !grid) return;

  // Restore saved width
  const saved = localStorage.getItem('mp-grid-left');
  if (saved) {
    grid.style.setProperty('--left-col-width', saved);
    const pct = parseFloat(saved);
    if (!isNaN(pct)) grid.style.setProperty('--right-col-width', (100 - pct - 1) + '%');
  }

  let startX, startLeftWidth;

  resizer.addEventListener('mousedown', (e) => {
    startX = e.clientX;
    startLeftWidth = document.getElementById('left-col').getBoundingClientRect().width;
    resizer.classList.add('dragging');
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    e.preventDefault();
  });

  function onMouseMove(e) {
    const gridWidth = grid.getBoundingClientRect().width;
    const newLeftWidth = startLeftWidth + (e.clientX - startX);
    const pct = Math.max(30, Math.min(80, (newLeftWidth / gridWidth) * 100));
    grid.style.setProperty('--left-col-width', pct + '%');
    grid.style.setProperty('--right-col-width', (100 - pct - 1) + '%');
  }

  function onMouseUp() {
    resizer.classList.remove('dragging');
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', onMouseUp);
    const leftW = grid.style.getPropertyValue('--left-col-width');
    if (leftW) localStorage.setItem('mp-grid-left', leftW);
  }
}

/* ========================================================================== */
/* Boot                                                                          */
/* ========================================================================== */

initTheme(); // before first paint — avoid a theme flash

document.addEventListener('DOMContentLoaded', () => {
  showDisconnectedOverlay();
  initChart();
  initChartRangeTabs();
  initGridResizer();
  initRollingStats();
  _wsConnect();
});

/* ========================================================================== */
/* Public API                                                                   */
/* ========================================================================== */

window.MoneyPrinter = {
  registerSection,
  formatCurrency,
  formatPercent,
  formatTime,
  botAction,
  setChartRange,
};
