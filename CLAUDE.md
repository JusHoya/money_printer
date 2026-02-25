# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Money Printer is an algorithmic trading system for the **Kalshi** prediction market. It fetches live data (crypto prices, weather forecasts, market orderbooks), runs trading strategies against that data, and manages simulated/demo positions with full risk management. The goal is paper-trading validation before any real capital deployment.

## Commands

```bash
# Run the live dashboard (main entry point)
python scripts/run_dashboard.py

# Run simulation
python scripts/simulate.py --strategy crypto --days 30 --live
python scripts/simulate.py --strategy weather --days 10

# Lab: audit, compare, optimize strategies against harvested data
$env:PYTHONPATH = "."; python scripts/lab.py --audit
$env:PYTHONPATH = "."; python scripts/lab.py --compare
$env:PYTHONPATH = "."; python scripts/lab.py --optimize

# Tests (pytest)
python -m pytest tests/
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

Shared dataclasses: `MarketData` (price/bid/ask/volume/extra dict) and `TradeSignal` (symbol/side/quantity/confidence).

### Data Flow
`DataProvider` → `Strategy.analyze()` → `TradeSignal` → `RiskManager.check()` → `SimulatedExchange.execute()`

### Key Components

**`src/core/risk_manager.py` — RiskManager**: Enforces capital preservation rules (max risk per trade, daily drawdown limits, per-strategy drawdown, portfolio exposure caps, trade interval throttling, loss cooldown per symbol). Owns a `SimulatedExchange` instance and syncs balance via `_on_trade_close` callback.

**`src/core/matching_engine.py` — SimulatedExchange + LimitOrderBook**: Full simulated exchange with limit orders, order book depth tracking, trailing stops, and position lifecycle management. Tracks per-strategy PnL stats.

**`scripts/run_dashboard.py` — OrchestratorEngine**: The main runtime loop. Wires together providers, strategies, risk manager, and dashboard. Runs data fetch → strategy analysis → risk check → execution in a continuous loop with threading.

**`src/data/kalshi_provider.py`**: Kalshi API client handling RSA-signed auth, market discovery (series/events/tickers), and orderbook fetching. Uses demo API by default.

**`src/data/coinbase_provider.py`**: Fetches BTC-USD price from Coinbase public API.

**`src/data/nws_provider.py`**: Fetches weather observations from National Weather Service stations.

### Strategies (`src/strategies/`)
- **crypto_strategy.py**: Multiple versions (V1-V3) of crypto momentum/trend strategies plus `CryptoLongShotFader`. Uses 15-min momentum breakouts, RSI/MACD confirmation, mean reversion, trailing stops.
- **weather_strategy.py**: V1/V2 weather arbitrage comparing NWS forecasts to Kalshi temperature markets with city-specific bias correction.
- **bracket_strategy.py**: Iron condor strategy for range-bound markets (rates, yields, FX, weather brackets).

### Dashboard (`src/visualization/dashboard.py`)
Real-time terminal UI showing PnL, market feeds, strategy signals, and position tracking.

## Environment Setup
Copy `.env.example` to `.env` and fill in Kalshi demo API credentials and NWS user-agent. The private key file (`kalshi_priv.key` or path in `KALSHI_PRIVATE_KEY_PATH`) must exist for Kalshi API auth.

## Conventions
- The `agent_space/` directory is a Gemini agent framework — not part of the trading system core.
- Logs go to `logs/` directory.
- Test JSON fixtures (API response dumps) live in `tests/`.
- Scripts in `scripts/` are standalone utilities for debugging, probing APIs, and running the system.
