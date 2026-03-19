# Money Printer V2 — Product Requirements Document

**Version:** 2.0
**Date:** 2026-03-18
**Goal:** Overhaul the Money Printer platform into a profitable ML-driven Kalshi trading system. Start with ~$300, target $200k portfolio growth. Achieve 85%+ sandbox success rate before live deployment.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current State Assessment](#2-current-state-assessment)
3. [Target Architecture](#3-target-architecture)
4. [Research Findings — Profitable Strategies](#4-research-findings--profitable-strategies)
5. [Sprint Plan](#5-sprint-plan)
   - [Sprint 0: Foundation & Tooling](#sprint-0-foundation--tooling-1-2-days)
   - [Sprint 1: Data Infrastructure](#sprint-1-data-infrastructure--collection-3-4-days)
   - [Sprint 2: ML Model Development](#sprint-2-ml-model-development-5-7-days)
   - [Sprint 3: Strategy Overhaul](#sprint-3-strategy-overhaul-4-5-days)
   - [Sprint 4: Execution Engine & Risk Overhaul](#sprint-4-execution-engine--risk-overhaul-3-4-days)
   - [Sprint 5: Sandbox Validation & Backtesting](#sprint-5-sandbox-validation--backtesting-5-7-days)
   - [Sprint 6: Live Trading Integration](#sprint-6-live-trading-integration-3-4-days)
   - [Sprint 7: Documentation & Polish](#sprint-7-documentation--polish-2-3-days)
6. [Agent Assignments](#6-agent-assignments)
7. [Success Criteria](#7-success-criteria)
8. [Risk Register](#8-risk-register)
9. [Tooling & Dependencies](#9-tooling--dependencies)
10. [Bankroll Growth Model](#10-bankroll-growth-model)

---

## 1. Executive Summary

Money Printer V1 is a functional paper-trading platform with 3 active bots (BTC 15m, BTC Hourly, Weather), rule-based strategies, a simulated exchange, risk management, and both terminal and web dashboards. It has solid bones but relies entirely on hand-tuned technical indicators with no ML, no real-time data feeds, no backtesting framework, and no live order placement.

**V2 transforms it into:**
- An ML-driven prediction engine trained on historical + real-time Kalshi data
- A maker-first execution engine that minimizes fees (4x savings over taker)
- A probability-calibrated system that only trades when it has measurable edge
- A backtested, sandbox-validated platform requiring 85%+ win rate before going live
- A portfolio growth engine designed for compounding $300 → $200k

**Key strategic insights from research:**
- Makers earn +1.12% excess return on Kalshi; takers lose -1.12% (always be a maker)
- NO contracts outperform YES at 69/99 price levels (exploit the "optimism tax")
- Cheap longshots (1-10 cents) lose 60%+ of money invested (never buy them — sell them)
- BTC latency arbitrage (Coinbase→Kalshi lag) is the single highest-volume edge
- Weather model ensemble (HRRR vs NWS forecast divergence) is the lowest-competition edge
- Quarter-Kelly sizing with 10% max position is optimal for $300 bankroll

---

## 2. Current State Assessment

### What Works Well (Keep)
| Component | File | Status |
|---|---|---|
| Core interfaces (MarketData, TradeSignal, ABCs) | `src/core/interfaces.py` | Mature, stable |
| Risk manager (Kelly, drawdown, exposure caps) | `src/core/risk_manager.py` | Production-ready |
| Simulated exchange (positions, PnL, stops) | `src/core/matching_engine.py` | Feature-rich |
| Bot architecture (base, registry, mixins) | `src/bots/` | Clean abstractions |
| Kalshi data provider (V1/V2, RSA auth) | `src/data/kalshi_provider.py` | Handles API evolution |
| Coinbase provider | `src/data/coinbase_provider.py` | Simple, stable |
| NWS provider | `src/data/nws_provider.py` | Multi-station |
| Web dashboard (FastAPI + WebSocket) | `src/web/` | Production-ready |
| Terminal dashboard | `src/visualization/dashboard.py` | Mature |
| Test suite (2,769 lines, 15 files) | `tests/` | High coverage |

### What Needs Overhaul
| Gap | Impact | Sprint |
|---|---|---|
| No ML models — all rule-based strategies | Cannot learn from data or improve | 2 |
| No real-time WebSocket feeds | Seconds of latency = missed arbitrage | 1 |
| No historical data storage | Cannot train models or backtest | 1 |
| No backtesting framework | Cannot validate 85% target | 5 |
| No probability calibration | Cannot measure true edge | 2 |
| No maker-only execution | Paying 4x fees unnecessarily | 4 |
| No live order placement | Cannot trade real money | 6 |
| Weather strategy is YES-only | Missing half the opportunity | 3 |
| No Brier score tracking | No objective model quality metric | 2 |
| No time-decay exploitation | Missing theta strategies | 3 |
| No latency arbitrage | Missing highest-volume edge | 3 |
| Simulation requires `--live` flag | Cannot test offline | 1 |

### Codebase Stats
- **40 files, ~9,014 lines** of production code
- **3 active bots**, 8 strategy classes (V1-V3), 3 data providers
- **YES/NO support**: Implemented in V3 crypto strategies, missing in weather
- **ML**: scikit-learn in requirements but no trained models exist

---

## 3. Target Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (run_dashboard.py)                   │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌────────────────┐  │
│  │ BTC 15m   │  │ BTC Hour  │  │ Weather   │  │ Market Maker   │  │
│  │ Bot       │  │ Bot       │  │ Bot       │  │ Bot (NEW)      │  │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └───────┬────────┘  │
│        │              │              │                 │            │
│  ┌─────▼──────────────▼──────────────▼─────────────────▼────────┐  │
│  │              ML PREDICTION ENGINE (NEW)                       │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ XGBoost      │ │ LSTM/GRU     │ │ Probability          │  │  │
│  │  │ Classifier   │ │ Time Series  │ │ Calibrator (Platt)   │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ Feature      │ │ Brier Score  │ │ Time-to-Expiry       │  │  │
│  │  │ Pipeline     │ │ Tracker      │ │ Optimizer            │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │              DATA LAYER (NEW: Real-time + Historical)        │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ Coinbase WS  │ │ Kalshi WS    │ │ SQLite/Parquet       │  │  │
│  │  │ (real-time)  │ │ (real-time)  │ │ Historical Store     │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ HRRR Model   │ │ NWS Obs      │ │ Feature Store        │  │  │
│  │  │ Feed (NEW)   │ │ (existing)   │ │ (computed features)  │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │              EXECUTION LAYER (OVERHAULED)                    │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ Maker-First  │ │ Risk Manager │ │ Live Kalshi          │  │  │
│  │  │ Order Router │ │ (enhanced)   │ │ Order Gateway        │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ Simulated    │ │ Backtester   │ │ Fee Calculator       │  │  │
│  │  │ Exchange     │ │ (NEW)        │ │ (maker/taker)        │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │              MONITORING & VALIDATION                         │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ Web Dash     │ │ Terminal     │ │ Playwright Visual    │  │  │
│  │  │ (enhanced)   │ │ Dashboard    │ │ Verification (NEW)   │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │  │
│  │  │ Brier Score  │ │ Sandbox      │ │ Portfolio Growth     │  │  │
│  │  │ Dashboard    │ │ Scorecard    │ │ Tracker              │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Research Findings — Profitable Strategies

### 4.1 Maker vs Taker Economics (Critical)
Academic research on 72.1M Kalshi trades proves:
- **Makers**: +1.12% average excess return
- **Takers**: -1.12% average excess return
- **Maker fee**: `ceil(0.0175 * C * P * (1-P))` per contract (~0.44 cents at 50c)
- **Taker fee**: `ceil(0.07 * C * P * (1-P))` per contract (~1.75 cents at 50c)
- **Implication**: Every order we place MUST be a limit order (maker). This alone is a 2.24% swing.

### 4.2 Favorite-Longshot Bias
- Cheap contracts (1-10 cents) are systematically overpriced — they win far less than implied
- NO contracts outperform YES at 69/99 price levels
- YES takers overpay by 1.85% vs NO takers (the "optimism tax")
- **Strategy**: Sell longshots (buy NO on cheap YES contracts), buy favorites (buy YES on expensive contracts)

### 4.3 BTC Latency Arbitrage
- Coinbase/Binance price movements lead Kalshi contract repricing by seconds
- A documented case: $300 → $400,000 in one month via 15-min BTC latency arb
- **Implementation**: WebSocket feed from Coinbase, detect >0.3% moves, immediately place limit orders on Kalshi before market adjusts

### 4.4 Weather Model Ensemble
- HRRR (3km resolution, hourly updates) is the best same-day temperature predictor
- When HRRR diverges from NWS forecast (which Kalshi prices off), there is exploitable edge
- NWS has systematic biases: warm bias overall, station-specific corrections needed
- **Implementation**: Pull HRRR data, compare to NWS forecast and Kalshi prices, trade the divergence

### 4.5 Time-Decay Exploitation
- Prediction market "theta": uncertain contracts (near 50c) carry maximum time value
- As expiration approaches, contracts converge sharply to 0 or 100
- **BTC 15-min final minutes**: If BTC is clearly above/below strike, contracts at 90+ cents should be 95-99c but may lag
- **Weather brackets**: Sell extreme brackets early; as observations confirm forecast, they decay to 0

### 4.6 Market Making (Avellaneda-Stoikov)
- Documented 20.3% single-day return on S&P bracket markets
- Reservation price: `r(t) = s(t) - q * gamma * sigma^2 * (T - t)`
- Quote skewing based on inventory to stay neutral
- **Implementation**: Adapt existing open-source Kalshi market maker code

### 4.7 ML Model Stack
- **XGBoost/LightGBM**: Best for tabular features (price, volume, OBI, RSI)
- **LSTM/GRU**: Best for time-series (price history, order flow sequences)
- **Platt Scaling**: Critical for calibrating raw model outputs to true probabilities
- **Brier Score**: The metric — lower is better, below 0.20 means you have edge

### 4.8 Position Sizing for $300
- Quarter-Kelly (0.25x) with hard 10% bankroll cap per trade
- At $300: max $30 per trade, target $7.50 per trade
- 5 max concurrent positions
- If bankroll drops to $200: reduce to $5/trade and audit model

---

## 5. Sprint Plan

---

### Sprint 0: Foundation & Tooling (1-2 days)

**Goal:** Install all free tools, set up development infrastructure, establish visual testing pipeline.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 0.1 | Install Playwright for visual dashboard testing | Tooling Agent | `pip install playwright && playwright install chromium` — enables screenshot capture of web dashboard for verification |
| 0.2 | Install ML dependencies | Tooling Agent | `pip install xgboost lightgbm torch torchvision torchaudio tensorflow-lite optuna matplotlib seaborn` (all free) |
| 0.3 | Install data infrastructure deps | Tooling Agent | `pip install websocket-client pyarrow aiohttp` for real-time feeds and efficient storage |
| 0.4 | Install additional analysis tools | Tooling Agent | `pip install ta-lib statsmodels scipy shap` for technical analysis and model explainability |
| 0.5 | Set up Playwright visual test harness | Testing Agent | Create `tests/visual/` — automated screenshot capture of web dashboard, compare before/after changes |
| 0.6 | Create data directories | Tooling Agent | `data/historical/`, `data/features/`, `data/models/`, `data/backtest_results/` |
| 0.7 | Update requirements.txt | Tooling Agent | Add all new dependencies with version pins |
| 0.8 | Set up pre-commit hooks | Tooling Agent | Linting, type checking on changed files |

#### Deliverables
- [ ] All dependencies installed and importable
- [ ] Playwright can capture screenshot of web dashboard
- [ ] Directory structure for data pipeline created
- [ ] Updated requirements.txt

#### Visual Verification Gate
**Before marking Sprint 0 complete**: Run Playwright to capture a screenshot of the current web dashboard at `http://localhost:8000`. Save as `docs/screenshots/sprint0_baseline.png`. This is the baseline for all future visual comparisons.

---

### Sprint 1: Data Infrastructure & Collection (3-4 days)

**Goal:** Build real-time data feeds, historical data storage, and feature engineering pipeline. This is the foundation everything else depends on.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 1.1 | Coinbase WebSocket real-time feed | Data Agent | `src/data/coinbase_ws.py` — Subscribe to BTC-USD ticker via WebSocket. Capture price, volume, bid, ask at sub-second frequency. Store in ring buffer (last 24h). Critical for latency arbitrage. |
| 1.2 | Kalshi WebSocket feed | Data Agent | `src/data/kalshi_ws.py` — Subscribe to orderbook updates and trade feed via `wss://api.elections.kalshi.com/trade-api/ws/v2`. Real-time contract prices, order book depth. |
| 1.3 | Historical data harvester | Data Agent | `src/data/harvester.py` — Batch download historical Kalshi market data (settled contracts + outcomes). Store in Parquet files partitioned by market type and date. Target: 6+ months of BTC 15m and weather data. |
| 1.4 | SQLite market data store | Data Agent | `src/data/market_store.py` — Local SQLite database for tick data, OHLCV candles, orderbook snapshots. Schema: `ticks(timestamp, symbol, price, bid, ask, volume)`, `candles(timestamp, symbol, open, high, low, close, volume, period)`, `orderbooks(timestamp, symbol, bids_json, asks_json)`, `settlements(market_id, outcome, settlement_time, settlement_value)` |
| 1.5 | Feature engineering pipeline | ML Agent | `src/ml/features.py` — Compute features from raw data: RSI(14), MACD, Bollinger Bands, EMA(9/21/50), ATR, OBI (order book imbalance), VWAP, realized volatility (1m/5m/15m), price momentum (1m/5m/15m lookback), time-to-expiry (normalized), spread width, volume surge ratio, funding rate proxy |
| 1.6 | HRRR weather model feed | Data Agent | `src/data/hrrr_provider.py` — Fetch HRRR model temperature forecasts from NOAA NOMADS or AWS Open Data. Compare against NWS forecast for each city. Store divergence history. |
| 1.7 | Data quality monitors | Data Agent | Staleness detection (alert if no tick in 10s), gap detection, duplicate filtering, timezone normalization (all UTC internally) |
| 1.8 | Mock data generator overhaul | Data Agent | `src/data/mock_providers.py` — Generate realistic synthetic BTC price paths (geometric Brownian motion with jump diffusion) and weather data for offline testing. Remove `--live` requirement from simulation. |

#### Deliverables
- [ ] Coinbase WebSocket streaming BTC-USD at sub-second frequency
- [ ] Kalshi WebSocket streaming orderbook updates
- [ ] Historical data for 6+ months downloaded and stored
- [ ] SQLite store with tick, candle, orderbook, settlement tables
- [ ] Feature pipeline computing 15+ features per tick
- [ ] HRRR weather model data accessible
- [ ] Simulation works fully offline with mock data

#### Agent Utilization
- **Data Agent (primary)**: Builds all providers, store, harvester
- **ML Agent**: Designs feature pipeline schema, ensures features are model-ready
- **Testing Agent**: Validates data quality, writes integration tests for each feed

---

### Sprint 2: ML Model Development (5-7 days)

**Goal:** Build, train, and calibrate ML models for BTC contract pricing and weather prediction. This is the core competitive advantage.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 2.1 | BTC XGBoost classifier | ML Agent | `src/ml/models/btc_xgboost.py` — Binary classifier: will BTC be above/below strike at contract expiry? Features: RSI, MACD, momentum, OBI, volatility, spread, volume surge, time-to-expiry. Train on historical settlement data. Target: AUC > 0.70 |
| 2.2 | BTC LSTM price predictor | ML Agent | `src/ml/models/btc_lstm.py` — LSTM/GRU network predicting BTC price direction over next 15min/1hr. Input: last 60 1-minute candles + order flow features. Output: probability of up/down move + magnitude estimate. |
| 2.3 | Time-to-expiry optimizer | ML Agent | `src/ml/models/time_optimizer.py` — **Critical model**: Given a BTC contract with strike K, current price S, and time T remaining, what is the optimal limit order price to place? Uses: historical fill rates at various price levels vs time-to-expiry, realized volatility cone, contract settlement distribution. Output: recommended limit price and confidence. |
| 2.4 | Probability calibration layer | ML Agent | `src/ml/calibration.py` — Platt scaling (sigmoid fit) for XGBoost outputs. Isotonic regression for LSTM outputs. Ensures model P(win) = actual win rate. Evaluate with reliability diagrams. |
| 2.5 | Brier score tracker | ML Agent | `src/ml/brier.py` — Real-time Brier score computation per model per market type. Rolling 100-prediction window. Dashboard integration. Alert if Brier > 0.25 (model degrading). Decomposition: reliability + resolution - uncertainty. |
| 2.6 | Weather ensemble model | ML Agent | `src/ml/models/weather_ensemble.py` — Combine NWS forecast, HRRR model, historical bias correction, and temperature velocity into a probabilistic temperature forecast. Output: P(high temp > X) for each bracket. Compare against Kalshi bracket prices to find mispricings. |
| 2.7 | Hybrid ensemble (XGBoost + LSTM) | ML Agent | `src/ml/models/ensemble.py` — Stack XGBoost and LSTM predictions. XGBoost handles tabular features, LSTM handles sequential patterns. Meta-learner combines outputs. This is the production model. |
| 2.8 | Model training pipeline | ML Agent | `src/ml/training.py` — Automated training with: train/val/test split (70/15/15), walk-forward validation (no lookahead bias), hyperparameter tuning via Optuna, model versioning (save with timestamp + metrics), auto-retrain trigger when Brier score degrades. |
| 2.9 | Feature importance analysis | ML Agent | SHAP values for XGBoost, attention weights for LSTM. Identify which features drive predictions. Prune low-importance features. Document in model cards. |
| 2.10 | Model serving interface | ML Agent | `src/ml/predictor.py` — Unified interface: `predict(market_data, time_to_expiry) -> (probability, confidence, recommended_price)`. Loads latest trained model. Handles feature computation from raw MarketData. <50ms inference time. |

#### Deliverables
- [ ] XGBoost BTC classifier with AUC > 0.70
- [ ] LSTM price predictor trained on 6+ months of data
- [ ] Time-to-expiry optimizer recommending optimal limit prices
- [ ] Calibrated probabilities (reliability diagram shows <5% calibration error)
- [ ] Brier score tracking integrated into dashboard
- [ ] Weather ensemble model comparing HRRR vs NWS vs Kalshi
- [ ] Hybrid ensemble as production model
- [ ] Automated training pipeline with Optuna tuning
- [ ] Model serving with <50ms inference

#### Agent Utilization
- **ML Agent (primary)**: All model development, training, evaluation
- **Data Agent**: Ensures training data quality, builds data loaders
- **Research Agent**: Finds optimal hyperparameter ranges from literature
- **Testing Agent**: Validates model outputs, checks for data leakage

#### Key Design Decisions
- **Walk-forward validation only**: Never use future data to predict the past. Split by time, not random.
- **Calibration is non-negotiable**: Raw model outputs are meaningless without Platt scaling. A model that says "70% chance" must actually win 70% of the time.
- **Time-to-expiry is the most important feature**: A BTC 15-min contract at 50c with 14 minutes left is fundamentally different from one with 1 minute left. The model must learn this.

---

### Sprint 3: Strategy Overhaul (4-5 days)

**Goal:** Replace rule-based strategies with ML-driven strategies. Add new high-edge strategies discovered in research.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 3.1 | ML-driven BTC 15m strategy | Strategy Agent | `src/strategies/ml_btc_15m.py` — Replaces V3 rule-based strategy. Uses ensemble model prediction + time-to-expiry optimizer. Signal generation: if `model_prob - market_price > min_edge` (default 0.05), generate signal. Side (YES/NO) determined by model direction. Limit price from time optimizer. |
| 3.2 | ML-driven BTC hourly strategy | Strategy Agent | `src/strategies/ml_btc_hourly.py` — Same ML backbone with hourly-specific features. Wider time horizon means more features are useful (MACD, Bollinger, support/resistance). |
| 3.3 | ML-driven weather strategy | Strategy Agent | `src/strategies/ml_weather.py` — Uses weather ensemble model. Supports YES and NO wagers (fixes current YES-only limitation). Signal: if HRRR-based probability diverges from Kalshi bracket price by >8%, trade the divergence. |
| 3.4 | Latency arbitrage strategy | Strategy Agent | `src/strategies/latency_arb.py` — **New**. Monitors Coinbase WS feed. When BTC moves >0.3% in <60 seconds, check if corresponding Kalshi 15-min contract has repriced. If not, place limit order at edge. Expected hold: seconds to minutes. This is the highest-volume strategy. |
| 3.5 | Longshot fader (enhanced) | Strategy Agent | `src/strategies/longshot_fader_v2.py` — **Enhanced**. Uses ML probability calibration to identify overpriced longshots. If market prices a contract at X cents but calibrated model says true probability is <X-3 cents, sell it (buy NO). Exploits the documented favorite-longshot bias. |
| 3.6 | Market maker strategy | Strategy Agent | `src/strategies/market_maker.py` — **New**. Implements Avellaneda-Stoikov adapted for Kalshi. Parameters: gamma=0.2, sigma from realized vol, inventory-aware quote skewing. Targets bracket markets (weather, rates) with wider spreads. Maximum 3 contracts per market, 20 global. |
| 3.7 | Time-decay scalper | Strategy Agent | `src/strategies/time_decay.py` — **New**. In the final 2-5 minutes of a BTC 15-min contract, if the outcome is nearly certain (BTC clearly above/below strike), buy contracts at 90-93c that should settle at 99-100c. Small edge per trade but very high win rate. |
| 3.8 | Cross-spread arbitrage detector | Strategy Agent | `src/strategies/cross_spread_arb.py` — **New**. Monitors for YES_ask + NO_ask < 1.00 within the same Kalshi market. When found, simultaneously buy YES and NO for risk-free profit minus fees. Rare but zero-risk. |
| 3.9 | Strategy waterfall with ML gating | Strategy Agent | Update `SignalProcessorMixin` — All signals now pass through ML calibrator before risk manager. If calibrated probability implies negative expected value after fees, reject signal regardless of rule-based confidence. |
| 3.10 | YES/NO wager support for all strategies | Strategy Agent | Ensure every strategy can generate both YES and NO signals. Weather V2 currently YES-only — add NO support. Verify `contract_side` is set correctly in all signal paths. |

#### Deliverables
- [ ] ML-driven strategies replace all V3 rule-based strategies
- [ ] 4 new strategies: latency arb, market maker, time-decay scalper, cross-spread arb
- [ ] All strategies support both YES and NO wagers
- [ ] ML gating layer rejects negative-EV signals
- [ ] Longshot fader enhanced with calibrated probabilities

#### Agent Utilization
- **Strategy Agent (primary)**: Implements all strategy classes
- **ML Agent**: Provides model integration guidance, reviews probability usage
- **Testing Agent**: Unit tests for each strategy, mock market scenarios
- **Research Agent**: Validates strategy parameters against published results

---

### Sprint 4: Execution Engine & Risk Overhaul (3-4 days)

**Goal:** Transform execution from simulated taker to maker-first with fee awareness, enhanced risk management, and live order readiness.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 4.1 | Maker-first order router | Execution Agent | `src/core/order_router.py` — **New**. All orders default to limit (maker). Compute optimal limit price from time-optimizer model + current spread. Only use market (taker) if urgency flag set (e.g., latency arb with fast-decaying edge). Track maker/taker ratio — target >90% maker. |
| 4.2 | Fee calculator | Execution Agent | `src/core/fee_calculator.py` — **New**. Accurate Kalshi fee computation: maker `ceil(0.0175 * contracts * P * (1-P))`, taker `ceil(0.07 * contracts * P * (1-P))`. Include fees in all EV calculations. Reject trades where edge < fees. |
| 4.3 | Live Kalshi order gateway | Execution Agent | `src/core/live_gateway.py` — **New**. Implements `ExecutionEngine` ABC. Places real orders via Kalshi V2 API (`POST /portfolio/orders`). Order types: limit, market. Handles: order confirmation, partial fills, cancellation, position queries. Uses demo API first, production API behind explicit flag. |
| 4.4 | Enhanced risk manager | Execution Agent | Update `src/core/risk_manager.py` — Add: fee-aware EV check (reject if EV < 0 after fees), maker/taker routing decision, bankroll-stage-aware sizing (see §10), correlation limit (max 2 BTC contracts in same direction simultaneously), Brier-score-gated trading (pause if Brier > 0.25). |
| 4.5 | Quarter-Kelly position sizing | Execution Agent | Update Kelly implementation — Enforce 0.25x Kelly with hard caps: $30 max at $300 bankroll, scales with portfolio value. Implement bankroll stages from §10. |
| 4.6 | Simulated exchange enhancements | Execution Agent | Update `src/core/matching_engine.py` — Add: realistic fill simulation (maker orders fill with 60% probability based on research), fee deduction on every trade, slippage modeling, partial fill simulation, maker queue position estimation. |
| 4.7 | Kill switch & circuit breakers | Execution Agent | `src/core/circuit_breaker.py` — **New**. Emergency stop: halt all trading if daily loss > 5% of bankroll. Pause individual strategy if 3 consecutive losses. Pause market type if Brier score > 0.30. Manual kill switch via web dashboard button. |
| 4.8 | Order lifecycle tracking | Execution Agent | Enhance position tracking — Full order lifecycle: signal → order placed → pending → filled/cancelled/expired → position open → position closed → settlement. Store complete audit trail in SQLite. |

#### Deliverables
- [ ] All orders routed as limit (maker) by default
- [ ] Fee calculator integrated into all EV computations
- [ ] Live Kalshi gateway ready (tested on demo API)
- [ ] Risk manager rejects negative-EV-after-fees trades
- [ ] Quarter-Kelly sizing with bankroll-stage awareness
- [ ] Simulated exchange models realistic fill rates and fees
- [ ] Circuit breakers protect against cascading losses
- [ ] Full order audit trail in SQLite

#### Agent Utilization
- **Execution Agent (primary)**: Order router, gateway, fee calculator
- **Strategy Agent**: Reviews integration points between strategies and execution
- **Testing Agent**: Tests order lifecycle, fee calculations, kill switch triggers

---

### Sprint 5: Sandbox Validation & Backtesting (5-7 days)

**Goal:** Build comprehensive backtesting framework. Achieve 85%+ success rate in sandbox with simulated trades against real market data. Visual verification of all dashboards.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 5.1 | Backtesting engine | Testing Agent | `src/backtest/engine.py` — **New**. Replays historical market data through strategy → ML model → risk manager → simulated exchange pipeline. Walk-forward: train on data[0:T], test on data[T:T+window], slide forward. Metrics: win rate, Sharpe ratio, max drawdown, Brier score, total PnL, fee impact. |
| 5.2 | Backtest data loader | Testing Agent | `src/backtest/data_loader.py` — Load historical tick data, candles, orderbooks from SQLite/Parquet. Reconstruct market state at each timestamp. Handle gaps and missing data. |
| 5.3 | Backtest report generator | Testing Agent | `src/backtest/report.py` — Generate HTML report with: equity curve, drawdown chart, win rate by strategy, win rate by market type, win rate by time-of-day, Brier score over time, fee analysis, best/worst trades. Save to `data/backtest_results/`. |
| 5.4 | 85% success rate validation | Testing Agent | Run full backtest on 3+ months of data. Measure: (a) overall trade win rate ≥ 85%, (b) per-strategy win rate ≥ 75%, (c) positive cumulative PnL after fees, (d) max drawdown < 15% of peak, (e) Brier score < 0.20. All five criteria must pass. |
| 5.5 | Sandbox mode (paper trading vs real market) | Testing Agent | `src/backtest/sandbox.py` — **New**. Real-time paper trading mode: fetches live Kalshi prices via WebSocket, runs full ML pipeline, simulates order placement with realistic fill rates and fees, but places NO real orders. Tracks virtual portfolio. Must achieve 85% win rate over 100+ trades before live flag is enabled. |
| 5.6 | Playwright visual verification suite | Testing Agent | `tests/visual/test_dashboard.py` — Automated tests: (1) Start web dashboard, (2) Capture screenshot, (3) Verify all panels render (portfolio, positions, strategies, alerts, mascot), (4) Verify data is populated (not empty/error states), (5) Compare against baseline screenshot from Sprint 0. Run after every sprint. |
| 5.7 | Dashboard enhancements for V2 | Frontend Agent | Update web dashboard — Add panels: ML model Brier score, predicted vs actual outcomes chart, fee tracker (maker vs taker breakdown), sandbox scorecard (trades/wins/loss/rate), equity curve chart, time-to-expiry heatmap showing optimal entry zones. |
| 5.8 | Stress testing | Testing Agent | Simulate adverse scenarios: (a) 10% BTC flash crash, (b) API outage (no data for 5 min), (c) 20 rapid consecutive losses, (d) all positions expire worthless. Verify circuit breakers trigger and portfolio survives. |
| 5.9 | A/B strategy comparison | Testing Agent | Run ML strategies vs V3 rule-based strategies on same historical data. Quantify improvement. ML strategies must outperform on: win rate, PnL, Sharpe ratio, max drawdown. |
| 5.10 | Regression test suite | Testing Agent | Expand test suite — Add tests for: ML model predictions (deterministic given fixed input), fee calculations, order routing logic, circuit breaker triggers, calibration accuracy. Target: 90%+ code coverage on new code. |

#### Deliverables
- [ ] Backtesting engine with walk-forward validation
- [ ] HTML backtest reports with equity curves and analytics
- [ ] **85% win rate achieved in backtest on 3+ months of data**
- [ ] **85% win rate achieved in sandbox (100+ live-data paper trades)**
- [ ] Playwright captures dashboard screenshots — all panels render correctly
- [ ] Stress tests pass — system survives flash crash, API outage, losing streaks
- [ ] ML strategies outperform V3 rule-based in A/B test
- [ ] Expanded test suite with 90%+ coverage

#### Visual Verification Gate (MANDATORY)
**Before marking Sprint 5 complete**, the following Playwright checks must pass:
1. Dashboard renders at `http://localhost:8000` — screenshot saved as `docs/screenshots/sprint5_dashboard.png`
2. Portfolio panel shows simulated balance, equity, PnL
3. Strategy panel shows ML model names, win rates, Brier scores
4. Positions panel shows open positions with YES/NO sides
5. Equity curve chart renders with historical data
6. Compare Sprint 5 screenshot against Sprint 0 baseline — all new panels visible

#### Agent Utilization
- **Testing Agent (primary)**: Backtesting engine, validation, Playwright
- **ML Agent**: Reviews backtest methodology for data leakage, validates metrics
- **Frontend Agent**: Dashboard enhancements
- **Strategy Agent**: Provides strategy parameters for A/B testing

---

### Sprint 6: Live Trading Integration (3-4 days)

**Goal:** Enable live trading on Kalshi with real money. Start with minimum viable deployment ($50 test, then full $300). All safety rails active.

#### Prerequisites
- Sprint 5 complete: 85% sandbox win rate achieved
- Demo API testing complete (Sprint 4.3)
- Circuit breakers tested (Sprint 5.8)

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 6.1 | Production API configuration | Execution Agent | Switch from demo to production Kalshi API (`api.elections.kalshi.com`). Verify RSA auth works. Test with read-only calls first (portfolio balance, market data). |
| 6.2 | Gradual rollout: Phase 1 ($50) | Execution Agent | Deploy with $50 of the $300 bankroll. Run 1 bot only (BTC 15m — highest volume). Max $5 per trade. Run for 48 hours. Success criteria: positive PnL, no bugs, all orders fill correctly. |
| 6.3 | Gradual rollout: Phase 2 ($150) | Execution Agent | If Phase 1 passes, deploy with $150. Enable BTC 15m + weather bots. Max $15 per trade. Run for 1 week. |
| 6.4 | Gradual rollout: Phase 3 ($300) | Execution Agent | If Phase 2 passes, deploy full $300 bankroll. All bots active. Full Kelly sizing. Begin portfolio growth compounding. |
| 6.5 | Live monitoring dashboard | Frontend Agent | Add to web dashboard: real Kalshi portfolio balance, pending orders, order fill status, realized vs unrealized PnL (real), fee expenditure tracker, daily/weekly/monthly PnL charts. |
| 6.6 | Alerting system | Execution Agent | Email/log alerts for: circuit breaker triggered, daily loss > 3%, order rejection by Kalshi, API errors, model Brier score degradation. |
| 6.7 | Playwright live verification | Testing Agent | Capture dashboard screenshot during live trading. Verify: real balance displayed, real positions shown, PnL updating, no error states. Save as `docs/screenshots/sprint6_live.png`. |
| 6.8 | Reconciliation check | Execution Agent | Every hour: compare system's tracked positions/balance against Kalshi API's reported positions/balance. Alert on any discrepancy > $1. |

#### Deliverables
- [ ] Live trading active on production Kalshi API
- [ ] Phase 1 ($50) completed with positive PnL
- [ ] Phase 2 ($150) completed with positive PnL
- [ ] Phase 3 ($300) deployed with full strategy suite
- [ ] Live dashboard showing real portfolio, verified by Playwright screenshot
- [ ] Alerting system active
- [ ] Hourly reconciliation running

#### Visual Verification Gate (MANDATORY)
Playwright captures of live dashboard at each phase:
- `docs/screenshots/sprint6_phase1.png` — after 48h with $50
- `docs/screenshots/sprint6_phase2.png` — after 1 week with $150
- `docs/screenshots/sprint6_phase3.png` — full deployment with $300

---

### Sprint 7: Documentation & Polish (2-3 days)

**Goal:** Update all documentation to reflect V2 changes. Clean up code. Final polish.

#### Tasks

| # | Task | Agent | Details |
|---|---|---|---|
| 7.1 | Update CLAUDE.md | Docs Agent | Reflect new architecture: ML pipeline, data infrastructure, new strategies, execution engine, backtesting framework. Update commands section. |
| 7.2 | Update README.md | Docs Agent | Project overview, setup instructions, architecture diagram, strategy descriptions, model training guide, dashboard screenshots. |
| 7.3 | API documentation | Docs Agent | Document all new modules: `src/ml/`, `src/data/` (new providers), `src/backtest/`, `src/core/` (new components). Docstrings on public interfaces. |
| 7.4 | Model cards | Docs Agent | For each ML model: training data description, feature list, performance metrics (AUC, Brier score), known limitations, retrain schedule. |
| 7.5 | Strategy playbook | Docs Agent | `docs/strategy_playbook.md` — Each strategy: theory, edge source, parameters, expected win rate, when it works, when it fails, risk profile. |
| 7.6 | Operations runbook | Docs Agent | `docs/runbook.md` — How to: start/stop system, retrain models, handle circuit breaker triggers, scale bankroll, add new markets, debug common issues. |
| 7.7 | Code cleanup | Cleanup Agent | Remove deprecated V1/V2 strategy code (keep V3 as fallback, remove older). Remove unused imports. Ensure consistent naming. Type hints on all public functions in new code. |
| 7.8 | Final visual verification | Testing Agent | Capture final dashboard screenshots with Playwright. Include in README. Compare against Sprint 0 baseline to show complete transformation. |
| 7.9 | Update .env.example | Docs Agent | Add new environment variables for HRRR data, model paths, live trading flags. |
| 7.10 | Architecture decision records | Docs Agent | `docs/adr/` — Record key decisions: why XGBoost+LSTM hybrid, why maker-only, why quarter-Kelly, why Platt scaling over isotonic. |

#### Deliverables
- [ ] CLAUDE.md updated with V2 architecture
- [ ] README.md with screenshots and full setup guide
- [ ] Model cards for all ML models
- [ ] Strategy playbook documenting all strategies
- [ ] Operations runbook for day-to-day management
- [ ] Deprecated code removed
- [ ] Final dashboard screenshot in README

---

## 6. Agent Assignments

Each sprint utilizes specialized agents. Here is the team roster and their responsibilities:

| Agent | Role | Primary Sprints | Key Skills |
|---|---|---|---|
| **Tooling Agent** | Dependency management, environment setup | 0 | pip, system configuration, directory setup |
| **Data Agent** | Data infrastructure, feeds, storage | 1 | WebSocket, SQLite, Parquet, API integration |
| **ML Agent** | Model development, training, calibration | 2, (3, 5) | XGBoost, LSTM, Optuna, Platt scaling, Brier score |
| **Strategy Agent** | Trading strategy implementation | 3, (4) | Strategy patterns, signal generation, YES/NO logic |
| **Execution Agent** | Order routing, risk, live gateway | 4, 6 | Kalshi API, fee math, circuit breakers |
| **Testing Agent** | Backtesting, validation, Playwright | 5, (0, 1, 3, 4, 6) | pytest, Playwright, walk-forward, stress testing |
| **Frontend Agent** | Dashboard UI enhancements | (5, 6) | FastAPI, WebSocket, HTML/JS/CSS |
| **Research Agent** | Strategy research, parameter validation | (2, 3) | Web research, academic papers, open-source repos |
| **Docs Agent** | Documentation, model cards, runbooks | 7 | Markdown, architecture diagrams |
| **Cleanup Agent** | Code cleanup, deprecation removal | 7 | Refactoring, type hints, import cleanup |

**Parallelization opportunities:**
- Sprint 1 tasks 1.1-1.4 (data feeds) can all run in parallel
- Sprint 2 tasks 2.1-2.3 (different models) can train in parallel
- Sprint 3 strategies are independent and can be developed in parallel
- Sprint 5 backtesting + Playwright testing can run in parallel
- Sprint 7 documentation tasks are fully parallelizable

---

## 7. Success Criteria

### Must-Have (Release Blockers)

| # | Criterion | Measurement | Target |
|---|---|---|---|
| S1 | Sandbox win rate | Wins / total trades in paper trading mode | ≥ 85% over 100+ trades |
| S2 | Backtest win rate | Walk-forward backtest on 3+ months | ≥ 85% |
| S3 | Positive PnL after fees | Cumulative PnL in sandbox | > $0 after realistic fees |
| S4 | Brier score | Model calibration on held-out data | < 0.20 |
| S5 | Max drawdown | Largest peak-to-trough decline | < 15% of peak equity |
| S6 | YES and NO wagers | All strategies support both sides | 100% of strategies |
| S7 | Maker order ratio | Limit orders / total orders | > 90% |
| S8 | Dashboard visual | Playwright screenshot verification | All panels render with data |
| S9 | Circuit breakers | Stress test passage | All 4 scenarios survived |
| S10 | Live Phase 1 | 48h live trading with $50 | Positive PnL, no bugs |

### Should-Have (Quality Targets)

| # | Criterion | Target |
|---|---|---|
| Q1 | Sharpe ratio (annualized) | > 2.0 |
| Q2 | Model inference time | < 50ms per prediction |
| Q3 | Data feed latency | < 500ms Coinbase WS to strategy |
| Q4 | Test coverage (new code) | > 90% |
| Q5 | ML strategies beat V3 rules | Higher PnL in A/B backtest |

---

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ML model overfits to historical data | High | High | Walk-forward validation only; retrain monthly; monitor Brier score drift |
| Kalshi API changes field names again | Medium | High | Live API tests (existing); `_parse_price()` handles both formats; alert on parse failures |
| $300 bankroll wiped in first week | Medium | Critical | Phase 1 starts with only $50; 5% daily drawdown kill switch; quarter-Kelly sizing |
| Latency arbitrage edge disappears as more bots compete | Medium | Medium | Multiple strategy types (not dependent on single edge); market maker strategy as backup |
| WebSocket feed drops / data gaps | High | Medium | Reconnect logic; staleness detection; fall back to REST polling; alert on gaps |
| Coinbase price manipulation / flash crash | Low | High | Circuit breaker halts trading on >5% moves; multi-source price confirmation |
| Kalshi reduces maker rebate or increases fees | Low | Medium | Fee calculator is parameterized; re-evaluate EV thresholds if fees change |
| HRRR model data becomes unavailable | Low | Medium | NWS forecast alone (current approach) as fallback; cache last HRRR run |
| Model training takes too long on local hardware | Medium | Low | Use lightweight models (XGBoost); LSTM with small architecture; Optuna with early stopping |
| Regulatory changes to prediction markets | Low | High | Monitor Kalshi announcements; system can be paused instantly via kill switch |

---

## 9. Tooling & Dependencies

### New Dependencies (All Free)

| Package | Purpose | Sprint |
|---|---|---|
| `playwright` | Visual dashboard testing and verification | 0 |
| `xgboost` | Gradient boosting classifier for BTC prediction | 2 |
| `lightgbm` | Alternative gradient boosting (faster training) | 2 |
| `torch` | LSTM/GRU neural network for price time series | 2 |
| `optuna` | Hyperparameter optimization for model tuning | 2 |
| `shap` | Model explainability and feature importance | 2 |
| `websocket-client` | Real-time Coinbase WebSocket feed | 1 |
| `pyarrow` | Parquet file storage for historical data | 1 |
| `aiohttp` | Async HTTP for concurrent data fetching | 1 |
| `matplotlib` | Backtest report charts and equity curves | 5 |
| `seaborn` | Statistical visualizations for model analysis | 5 |
| `statsmodels` | Statistical tests and time series analysis | 2 |
| `ta` | Technical analysis indicators (alternative to ta-lib) | 1 |

### Existing Dependencies (Keep)
`requests`, `python-dotenv`, `cryptography`, `fastapi`, `uvicorn`, `websockets`, `scikit-learn`, `joblib`, `pandas`, `numpy`, `pillow`

### Development Tools
| Tool | Purpose |
|---|---|
| Playwright (Chromium) | Automated screenshot capture of web dashboard |
| SQLite | Local database for tick data and audit trail |
| Parquet (via PyArrow) | Efficient columnar storage for historical data |
| Optuna Dashboard | Visual hyperparameter optimization tracking |

---

## 10. Bankroll Growth Model

### Assumptions
- Starting capital: $300
- Quarter-Kelly sizing (0.25x)
- Average edge per trade: 3-5% (conservative, post-fees)
- Average trades per day: 5-10
- Maker fee rate: ~0.44 cents per contract at 50c
- Win rate: 85% target (55-65% realistic initial)

### Growth Stages

| Stage | Bankroll | Max Trade Size | Max Positions | Strategies Active | Kelly Fraction |
|---|---|---|---|---|---|
| **Seed** | $300 - $500 | $30 (10%) | 5 | BTC 15m + Weather | 0.25x |
| **Early** | $500 - $2,000 | $100 (5%) | 8 | + BTC Hourly + Longshot Fader | 0.25x |
| **Growth** | $2,000 - $10,000 | $500 (5%) | 12 | + Market Maker + Time Decay | 0.30x |
| **Scale** | $10,000 - $50,000 | $2,500 (5%) | 15 | + Latency Arb (higher capital needed) | 0.35x |
| **Compound** | $50,000 - $200,000 | $5,000 (2.5%) | 20 | All strategies, reduced sizing % | 0.25x |

### Conservative Projections

| Month | Projected Balance | Monthly Return | Notes |
|---|---|---|---|
| 0 | $300 | — | Phase 1-3 deployment |
| 1 | $350 - $420 | 15-40% | Seed stage, learning period |
| 3 | $500 - $1,200 | 15-30% | Early stage, model stabilizing |
| 6 | $1,500 - $5,000 | 20-35% | Growth stage, more strategies |
| 9 | $5,000 - $20,000 | 20-30% | Scale stage, latency arb active |
| 12 | $15,000 - $60,000 | 15-25% | Compound stage |
| 18 | $50,000 - $200,000 | 10-20% | Target reached, reduce risk |

**Reality check**: These projections assume consistent edge and no major drawdowns. The documented $300→$400k case was exceptional. A more realistic timeline to $200k is 12-24 months with disciplined compounding. The system is designed to survive drawdowns and compound steadily rather than swing for home runs.

### Key Growth Principles
1. **Never increase position size after a loss** — only scale up after new equity highs
2. **Withdraw 10% of profits monthly after $10k** — protects against total wipeout
3. **Retrain models monthly** — market dynamics shift, models must adapt
4. **Diversify across market types** — BTC, weather, brackets reduce correlation risk
5. **If drawdown hits 20%, pause for 1 week** — audit models, check for regime change

---

## Appendix A: File Map (New Files)

```
src/
├── ml/
│   ├── __init__.py
│   ├── features.py              # Feature engineering pipeline
│   ├── calibration.py           # Platt scaling, isotonic regression
│   ├── brier.py                 # Brier score tracking
│   ├── predictor.py             # Unified model serving interface
│   ├── training.py              # Automated training pipeline
│   └── models/
│       ├── __init__.py
│       ├── btc_xgboost.py       # XGBoost BTC classifier
│       ├── btc_lstm.py          # LSTM BTC price predictor
│       ├── time_optimizer.py    # Time-to-expiry optimal pricing
│       ├── weather_ensemble.py  # Weather multi-model ensemble
│       └── ensemble.py          # Hybrid XGBoost+LSTM meta-model
├── data/
│   ├── coinbase_ws.py           # Coinbase WebSocket feed
│   ├── kalshi_ws.py             # Kalshi WebSocket feed
│   ├── hrrr_provider.py         # HRRR weather model data
│   ├── harvester.py             # Historical data batch download
│   ├── market_store.py          # SQLite tick/candle/orderbook store
│   └── mock_providers.py        # Enhanced mock data (GBM + jump diffusion)
├── strategies/
│   ├── ml_btc_15m.py            # ML-driven BTC 15-min
│   ├── ml_btc_hourly.py         # ML-driven BTC hourly
│   ├── ml_weather.py            # ML-driven weather (YES + NO)
│   ├── latency_arb.py           # Coinbase→Kalshi latency arbitrage
│   ├── longshot_fader_v2.py     # Enhanced longshot fader with ML
│   ├── market_maker.py          # Avellaneda-Stoikov market maker
│   ├── time_decay.py            # End-of-contract time decay scalper
│   └── cross_spread_arb.py      # Cross-spread arbitrage detector
├── core/
│   ├── order_router.py          # Maker-first order routing
│   ├── fee_calculator.py        # Kalshi fee computation
│   ├── live_gateway.py          # Live Kalshi order placement
│   └── circuit_breaker.py       # Emergency stop & circuit breakers
├── backtest/
│   ├── __init__.py
│   ├── engine.py                # Backtesting engine
│   ├── data_loader.py           # Historical data loader
│   ├── report.py                # HTML report generator
│   └── sandbox.py               # Real-time paper trading mode
data/
├── historical/                  # Parquet files (BTC, weather, brackets)
├── features/                    # Pre-computed feature sets
├── models/                      # Trained model artifacts (.joblib, .pt)
└── backtest_results/            # Backtest HTML reports
tests/
├── visual/
│   └── test_dashboard.py        # Playwright visual verification
docs/
├── screenshots/                 # Playwright dashboard captures
├── strategy_playbook.md         # Strategy documentation
├── runbook.md                   # Operations guide
└── adr/                         # Architecture decision records
```

## Appendix B: Key Research Sources

- Makers vs Takers on Kalshi: [jbecker.dev/research/prediction-market-microstructure](https://www.jbecker.dev/research/prediction-market-microstructure)
- Kalshi Fee Schedule: [kalshi.com/fee-schedule](https://kalshi.com/fee-schedule)
- Avellaneda-Stoikov Market Making: [github.com/rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker)
- S&P Bracket Market Making (20.3% return): [github.com/nikhilnd/kalshi-market-making](https://github.com/nikhilnd/kalshi-market-making)
- BTC Latency Arbitrage: [github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot)
- Weather Trading Guide: [wethr.net/edu/trading-guide](https://wethr.net/edu/trading-guide)
- NWS Data & Settlement: [wethr.net/edu/nws-data-guide](https://wethr.net/edu/nws-data-guide)
- Prediction Market Arbitrage ($40M extracted): [trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works)
- Kelly Criterion for Prediction Markets: [navnoorbawa.substack.com/p/the-math-of-prediction-markets-binary](https://navnoorbawa.substack.com/p/the-math-of-prediction-markets-binary)
- ML Augmentation in Forecasting: [PMC10502359](https://pmc.ncbi.nlm.nih.gov/articles/PMC10502359/)
- Probability Calibration (scikit-learn): [scikit-learn.org/stable/modules/calibration.html](https://scikit-learn.org/stable/modules/calibration.html)
