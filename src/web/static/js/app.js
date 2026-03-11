/**
 * app.js — Money Printer Web Dashboard
 *
 * Vanilla JS. No frameworks.
 *
 * Responsibilities:
 *   - WebSocket client with exponential-backoff reconnect
 *   - Section updaters for every snapshot key
 *   - Chart.js PnL equity curve
 *   - Bot start/stop controls (POST to REST API)
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
/* Plugin registry                                                              */
/* ========================================================================== */

const sectionPlugins = {};

function registerSection(key, renderFn) {
  sectionPlugins[key] = renderFn;
}

/* ========================================================================== */
/* PnL Chart (Chart.js)                                                         */
/* ========================================================================== */

let pnlChart = null;
let _lastChartTs = -1;

function initChart() {
  const canvas = $('pnl-chart');
  if (!canvas || typeof Chart === 'undefined') {
    setTimeout(initChart, 200);
    return;
  }
  const ctx = canvas.getContext('2d');

  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height || 120);
  gradient.addColorStop(0, 'rgba(88, 166, 255, 0.35)');
  gradient.addColorStop(1, 'rgba(63, 185, 80, 0.05)');

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: 'Equity',
        data: [],
        borderColor: '#58a6ff',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: true,
        backgroundColor: gradient,
      }],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#161a22',
          borderColor: '#252b38',
          borderWidth: 1,
          titleColor: '#6e7681',
          bodyColor: '#c9d1d9',
          callbacks: {
            label: ctx => formatCurrency(ctx.parsed.y).text,
          },
        },
      },
      scales: {
        x: {
          display: false,
          grid: { color: '#1e242f' },
          ticks: {
            color: '#6e7681',
            maxTicksLimit: 8,
            font: { family: '"JetBrains Mono", "Fira Code", monospace', size: 10 },
          },
        },
        y: {
          display: true,
          grid: { color: '#1e242f' },
          ticks: {
            color: '#6e7681',
            font: { family: '"JetBrains Mono", "Fira Code", monospace', size: 10 },
            callback: v => formatCurrency(v).text,
          },
        },
      },
    },
  });
}

function updatePnLChart(history) {
  if (!pnlChart || !Array.isArray(history) || history.length === 0) return;

  const newerPoints = history.filter(p => p.ts > _lastChartTs);
  if (newerPoints.length === 0) return;

  const MAX_POINTS = 500;
  const ds = pnlChart.data;

  newerPoints.forEach(p => {
    ds.labels.push(formatTime(p.ts));
    ds.datasets[0].data.push(p.equity);
  });

  if (ds.labels.length > MAX_POINTS) {
    const excess = ds.labels.length - MAX_POINTS;
    ds.labels.splice(0, excess);
    ds.datasets[0].data.splice(0, excess);
  }

  _lastChartTs = newerPoints[newerPoints.length - 1].ts;

  const data = ds.datasets[0].data;
  if (data.length >= 2) {
    const trending = data[data.length - 1] >= data[0];
    ds.datasets[0].borderColor = trending ? '#3fb950' : '#f85149';
  }

  pnlChart.update('none');
}

/* ========================================================================== */
/* Disconnected overlay                                                          */
/* ========================================================================== */

let _overlayEl = null;

function _getOrCreateOverlay() {
  if (_overlayEl) return _overlayEl;
  _overlayEl = document.createElement('div');
  _overlayEl.id = 'disconnect-overlay';
  Object.assign(_overlayEl.style, {
    position:       'fixed',
    inset:          '0',
    background:     'rgba(13,15,20,0.82)',
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'center',
    zIndex:         '9999',
    fontFamily:     '"JetBrains Mono","Fira Code",monospace',
    fontSize:       '14px',
    color:          '#f85149',
    letterSpacing:  '1px',
    pointerEvents:  'none',
  });
  _overlayEl.textContent = 'DISCONNECTED — reconnecting…';
  document.body.appendChild(_overlayEl);
  return _overlayEl;
}

function showDisconnectedOverlay() {
  _getOrCreateOverlay().style.display = 'flex';
}

function hideDisconnectedOverlay() {
  _getOrCreateOverlay().style.display = 'none';
}

/* ========================================================================== */
/* Section updaters                                                              */
/* ========================================================================== */

/**
 * Update mode pill. state_manager sends snapshot.mode = 'sandbox' | 'live'.
 * HTML: <span id="mode-pill" class="sandbox"> ... <span id="mode-text">SANDBOX</span>
 */
function updateMode(mode) {
  const pill = $('mode-pill');
  const text = $('mode-text');
  const m = (mode || 'sandbox').toLowerCase();
  if (pill) pill.className = m;   // CSS: #mode-pill.sandbox / #mode-pill.live
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
    const bid = Number(m.bid || 0);
    const ask = Number(m.ask || 0);
    const bidStr = bid > 0 ? bid.toFixed(2) : '';
    const askStr = ask > 0 ? ask.toFixed(2) : '';
    return `<div class="mkt-row">` +
      `<span class="mkt-sym" title="${escHtml(m.symbol)}">${escHtml(m.symbol)}</span>` +
      `<span class="mkt-price">${Number(m.price).toFixed(2)}</span>` +
      `<span class="mkt-bid">${bidStr}</span>` +
      `<span class="mkt-ask">${askStr}</span>` +
      `</div>`;
  }).join('');
}

/* Log/alert state — accumulated client-side */
let _lastAlertCount = 0;
let _lastLogCount   = 0;

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
    _lastAlertCount = 0;
    return;
  }

  el.innerHTML = alerts.map(a =>
    `<div class="alert-item"><span class="alert-icon">&#9888;</span><span>${escHtml(a)}</span></div>`
  ).join('');
}

/**
 * Update system log panel.
 * HTML: <div id="log-list">
 * CSS: .log-line, .log-line.log-alert  (note: CSS uses "log-alert" not "alert-line")
 */
function updateLogs(logs) {
  const el = $('log-list');
  if (!el || !Array.isArray(logs)) return;
  if (logs.length === 0) { el.innerHTML = ''; return; }

  el.innerHTML = logs.map(l =>
    `<div class="log-line">${escHtml(l)}</div>`
  ).join('');

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

  tbody.innerHTML = positions.map(p => {
    const { text: pnlText, cls: pnlCls } = formatCurrency(p.pnl);
    const sideCls = (p.side === 'buy' || p.side === 'BUY') ? 'buy' : 'sell';
    return `<tr>` +
      `<td class="col-id">${escHtml(String(p.id || ''))}</td>` +
      `<td class="col-symbol" title="${escHtml(p.symbol)}">${escHtml(p.symbol)}</td>` +
      `<td class="col-side ${sideCls}">${escHtml((p.side || '').toUpperCase())}</td>` +
      `<td class="col-contract">${escHtml(p.contract_side || 'YES')}</td>` +
      `<td class="col-num">${p.quantity}x</td>` +
      `<td class="col-num">${Number(p.entry).toFixed(2)}</td>` +
      `<td class="col-num">${Number(p.current).toFixed(2)}</td>` +
      `<td class="col-pnl ${pnlCls}">${pnlText}</td>` +
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
  _updateBotChips(bots);
  _rebuildBotDropdownIfChanged(bots);
}

let _knownBotsJson = '';

function _updateBotChips(bots) {
  const el = $('bot-chips');
  if (!el) return;
  if (!bots || bots.length === 0) { el.innerHTML = ''; return; }
  el.innerHTML = bots.map(b => {
    const cls = b.active ? 'active' : 'inactive';
    return `<span class="bot-chip ${cls}"><span class="chip-dot"></span>${escHtml(b.name)}</span>`;
  }).join('');
}

function _rebuildBotDropdownIfChanged(bots) {
  const botsJson = JSON.stringify(bots || []);
  if (botsJson === _knownBotsJson) return;
  _knownBotsJson = botsJson;

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

/**
 * Update mascot state chip and canvas animation.
 * HTML: <span id="mascot-state">
 * mascot.js exposes: window.setMascotState(state)
 */
function updateMascotState(state) {
  const el = $('mascot-state');
  if (el) el.textContent = (state || 'IDLE').toUpperCase();
  if (typeof window.setMascotState === 'function') {
    window.setMascotState(state || 'IDLE');
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
    const resp = await fetch(url, { method: 'POST' });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      console.warn(`[bots] ${action} ${name} failed:`, body.detail || resp.status);
    }
  } catch (err) {
    console.error('[bots] fetch error:', err);
  }
}

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
/* Snapshot dispatch                                                             */
/* ========================================================================== */

function _dispatchSnapshot(snap) {
  for (const [key, fn] of Object.entries(sectionPlugins)) {
    if (key in snap) {
      try { fn(snap[key]); }
      catch (err) { console.error(`[section:${key}]`, err); }
    }
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
registerSection('pnl_history',    updatePnLChart);

/* ========================================================================== */
/* Boot                                                                          */
/* ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  showDisconnectedOverlay();
  initChart();
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
};
