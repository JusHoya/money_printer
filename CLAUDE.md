# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Start with [`HANDOFF.md`](HANDOFF.md).** As of 2026-08-22 the project has taken
> **two consecutive HALT verdicts** — weather (Phase 2, 2026-07-26) and AAA gas
> (Phase 4, 2026-07-30) — so **no strategy is currently cleared to trade** and
> `PRD.md` has no third engine. The Google Cloud VM was archived and stopped on
> 2026-08-22; its data lives in `vm_snapshot_2026_08_22/` (see that folder's
> `MANIFEST.md`). `HANDOFF.md` records what is dead, what is proven, and the open
> decision — read it before acting on the roadmap in `PRD.md`, which describes a
> path whose Phase 3 and Phase 5 are downstream of a proceed decision that never came.

## What This Is

Money Printer is an algorithmic trading system for the **Kalshi** prediction market. It fetches live data (weather forecasts, market orderbooks), runs trading strategies against that data, and manages simulated/demo positions with full risk management. The goal is paper-trading validation before any real capital deployment.

**Pivot in progress (see `PRD.md`)**: a 22-agent review (`review_2026_07_24/` — gitignored, no longer on disk; conclusions survive in PRD/HANDOFF/reports) proved short-horizon crypto structurally unwinnable, so Phase 0 tore out all crypto surface area. TWO feed-only bots are registered post-Phase-4: `weather` and `gas` (both gated off trading by module-level flags). `PRD.md` drives all pivot work; `deploy/README.md` describes the 2026-09 split deployment onto the Pleiades home cluster (sandbox on maia/Pi 4, Hermes agent + offline lab on alcyone/DGX Spark).

## Commands

```bash
# Run the live dashboard (weather bot, feed-only)
python scripts/run_dashboard.py
python scripts/run_dashboard.py --bot weather

# Run simulation (weather only)
python scripts/simulate.py --bot weather --days 10

# Lab: audit / optimize the weather strategy against harvested data
$env:PYTHONPATH = "."; python scripts/lab.py --audit
$env:PYTHONPATH = "."; python scripts/lab.py --optimize

# Offline ML training (NEVER runs in the runtime process — PRD FR-0.2)
$env:PYTHONPATH = "."; python scripts/train_from_csv.py
$env:PYTHONPATH = "."; python scripts/train_models.py

# Tests (pytest) — run targeted files; avoid the full suite on this machine
python -m pytest tests/test_v3_risk_rules.py -v

# Install deps
pip install -r requirements.txt
```

Note: On Windows, scripts that import `src.*` modules need `PYTHONPATH=.` set, or run via `python -m pytest` which handles it.

## Architecture

### Core Abstractions (`src/core/interfaces.py`)
All components implement these ABCs:
- **DataProvider** — `connect()`, `fetch_latest(symbol) -> MarketData`
- **Strategy** — `analyze(data: MarketData) -> Optional[TradeSignal]`
- **ExecutionEngine** — `execute(signal: TradeSignal) -> bool`
- **Bot** (`src/bots/base.py`) — `setup()`, `tick()`, `get_symbols()` — encapsulates a data provider, strategy waterfall, and tick loop for a specific market

Shared dataclasses: `MarketData` (price/bid/ask/volume/extra dict) and `TradeSignal` (symbol/side/quantity/confidence).

### Data Flow
`DataProvider` → `Bot.tick()` → `Strategy.analyze()` → `TradeSignal` → `SignalProcessorMixin._process_signals()` → `RiskManager.check()` → `SimulatedExchange.execute()`

### Key Components

**`src/bots/`** — Bot implementations:
- `base.py`: Bot ABC defining `setup()`, `tick()`, `get_symbols()`
- `registry.py`: Bot registry for CLI `--bot` selection — registers `weather` and `gas` (both feed-only) post-teardown
- `mixins.py`: `SignalProcessorMixin` — shared signal processing logic (risk check → execution)
- `weather_bot.py`: The only concrete bot. Feed-only: `WEATHER_TRADING_ENABLED = False` gates the strategy waterfall; price/data feeds still run. (Crypto bots deleted 2026-07-24, PRD FR-0.1.)

**`src/core/risk_manager.py` — RiskManager**: Enforces capital preservation rules (max risk per trade, daily drawdown limits, per-strategy drawdown, portfolio exposure caps, trade interval throttling, loss cooldown per symbol). Owns a `SimulatedExchange` instance and syncs balance via `_on_trade_close` callback.

**`src/core/matching_engine.py` — SimulatedExchange + LimitOrderBook**: Full simulated exchange with limit orders, order book depth tracking, trailing stops, and position lifecycle management. Tracks per-strategy PnL stats.

**`scripts/run_dashboard.py` — OrchestratorEngine**: The main runtime loop. Wires together bots, risk manager, and dashboard. Uses `--bot` flag to select which bots to run. Runs bot tick loops in a continuous cycle with threading. **No ML retraining runs in this process** (PRD FR-0.2): the periodic/cycle-boundary/startup retrains and the on-trade-close online updater were removed; `src/ml/` and `scripts/train_*.py` are offline-only.

**`src/data/kalshi_provider.py`**: Kalshi API client handling RSA-signed auth, market discovery (series/events/tickers), and orderbook fetching. Uses demo API by default. See **Kalshi API** section below for critical field-name details.

**`src/data/coinbase_provider.py`**: Fetches BTC-USD price from Coinbase public API.

**`src/data/nws_provider.py`**: Fetches weather observations from National Weather Service stations.

### Strategies (`src/strategies/`)
- **weather_strategy.py**: V2 weather arbitrage comparing NWS forecasts to Kalshi temperature markets with city-specific bias correction. Phase 1-3 rebuild target; currently not trading (feed-only flag in `weather_bot.py`).
- **ml_weather.py**: ML-driven weather bracket strategy. Phase 1-3 rebuild target; currently not trading.
- **counter_trade.py**: `CounterTradeAnalyzer` — LOG-ONLY hedge analyzer still invoked by the orchestrator's market loop.
- **latency_arb.py**: MOTHBALLED (2026-07-24, PRD §4 A2) — kept on disk, unregistered, not-for-capital. See its header for revival preconditions (websocket feeds, <1s loop, maker/IOC, realistic fills, ≥200 paper trades).
- All crypto strategies (`crypto_strategy.py`, `ml_btc_15m.py`, `ml_btc_hourly.py`, `longshot_fader_v2.py`, `cross_spread_arb.py`) were **deleted** in the Phase 0 teardown.

### Dashboard (`src/visualization/dashboard.py`)
Real-time terminal UI showing PnL, market feeds, strategy signals, and position tracking.

## Environment Setup
Copy `.env.example` to `.env` and fill in Kalshi demo API credentials and NWS user-agent. The private key file (`kalshi_priv.key` or path in `KALSHI_PRIVATE_KEY_PATH`) must exist for Kalshi API auth.

## Kalshi API — Critical Reference

**API URLs:**
- Production: `https://api.elections.kalshi.com/trade-api/v2` (the ONLY valid production endpoint; `api.kalshi.co` and `trading-api.kalshi.com` are defunct)
- Demo: `https://demo-api.kalshi.co/trade-api/v2` (sandbox, empty orderbooks — falls back to production for price reads)
- V1 (BTC Hourly discovery only): `https://api.elections.kalshi.com/v1`

**Price field names (V2 API — current):**
The V2 API uses `_dollars` suffix string fields, NOT the old integer-cents fields:
- `yes_bid_dollars`, `yes_ask_dollars` (string, e.g. `"0.0700"`)
- `no_bid_dollars`, `no_ask_dollars` (string)
- `last_price_dollars` (string)
- `volume_fp` (string float, e.g. `"1234.00"`)
- Old field names (`yes_bid`, `yes_ask` as integers in cents) are **gone from V2 responses**.
- V1 API returns BOTH old and new field names.
- `_parse_price()` in `kalshi_provider.py` handles both formats; always use it.

**Market statuses:**
- `active` — open for trading (weather markets)
- `initialized` — created but not yet open (BTC 15m markets before their interval)
- `finalized`, `settled`, `closed` — expired
- Discovery must include BOTH `active` and `initialized` to find BTC 15m markets.

**Live API tests:** `tests/test_web_dashboard.py::TestKalshiMarketDataLive` — 10 tests that hit the real API to verify field names, price parsing, and market discovery. Run these if you suspect API changes.

## Conventions
- The `agent_space/` directory is a Gemini agent framework — not part of the trading system core.
- Logs go to `logs/` directory.
- Test JSON fixtures (API response dumps) live in `tests/fixtures/`.
- Scripts in `scripts/` are the main entry points; `scripts/debug/` has standalone utilities for debugging and probing APIs.
