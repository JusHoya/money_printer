# Product Requirements Document (PRD)

## Product Name: Money Printer 🖨️💵
**Version:** 1.4.1 (Skippy System V1)
**Author:** Skippy the Magnificent (via Agent Orchestration)
**Status:** Alpha / Active Development

---

## 1. Executive Summary
**Money Printer** is a high-precision, low-latency algorithmic trading platform custom-built for the **Kalshi** prediction market. 
The core objective is to execute a strictly data-driven, systematic approach to grow a $100 seed capital into $100,000 via high-probability prediction market contracts. The platform utilizes advanced telemetry, rigid risk management (Fractional Kelly Criterion), and automated scientific pipelines for backtesting and strategy refinement. Current operations are confined to the Kalshi Demo/Sandbox environment pending a sustained >95% algorithmic win rate.

### 1.1 Core Principles
- **Algorithmic Purity:** No emotional trading. Pure statistical edge execution.
- **Scientific Rigor:** All strategies undergo strict Harvest $\\rightarrow$ Audit $\\rightarrow$ Refinement pipelines.
- **Capital Preservation:** Risk management supersedes position capture. Default dynamic portfolio exposure limit at 50%.
- **High-Fidelity Telemetry:** Dense, terminal-based ASCII instrumentation for real-time portfolio tracking.

---

## 2. System Architecture Overview

The system operates on an asynchronous event-loop architecture built in Python, centered around a main Orchestrator that bridges strategy logic with market data execution.

### 2.1 Core Components

1.  **Orchestrator Engine (`src/engine/run_dashboard.py` / `OrchestratorEngine`)**
    - The central loop managing the lifecycle of the system.
    - Manages provider polling, feeds structured market data to the strategies, and aggregates `TradeSignal` objects.
    - Handles dynamic ticker resolution (e.g., dynamically resolving the active BTC hourly \"ladder\" or the active Weather city contracts).

2.  **Matching Engine (`src/core/matching_engine.py`)**
    - **Simulated Exchange:** A high-fidelity paper trading engine replicating Kalshi's Order Matching System (OMS).
    - **Limit Order Book:** Simulates depth-of-market constraints, bid-ask spreads, and fill probabilities.
    - *Capabilities:* Supports market orders, limit orders with patience timeouts, precise trailing stop-losses, and expiration-based binary settlement.

3.  **Risk Manager (`src/core/risk_manager.py`)**
    - The safety gateway protecting portfolio capitalization.
    - **Position Sizing:** Utilizes the Fractional Kelly Criterion (defaulting to 25% safety fraction) bounded by a hard capacity limit (e.g., max 5% to 10% per position).
    - **Exposure Limits:** Dynamic Portfolio Exposure cap (maximum 50% of total balance deployed at any time) to prevent harvest stagnation.

4.  **Providers (`src/data/`)**
    - **Kalshi Provider:** Primary order and market data interface (REST/API).
    - **NWS Provider:** Open-source telemetry fetching granular temperature/precipitation data from the National Weather Service.
    - **Coinbase Provider:** Supplementary tick/spot price data for cryptocurrencies (e.g., BTC/USD).

---

## 3. Active Trading Strategies

The platform currently supports multiple concurrent, highly specialized algorithmic agents encapsulating unique market edges.

### 3.1 The Trend Catcher 📈 (Crypto 15m Momentum V2)
- **Target:** `KXBTCHOURLY` (Bitcoin Hourly Close predictions)
- **Methodology:** 
  - Captures 15-minute momentum breakouts.
  - **Triggers:** Base buy entry > $0.55 YES price; Sell entry < $0.45 YES price.
  - **Confirmation Layer:** Requires RSI (<75 for longs, >25 for shorts) and MACD directional validation.
  - **Mean Reversion Fallback:** Actively trades the $0.45-$0.55 channel during ranging sideways markets.
- **Risk Overlays:** 
  - 120-second Trend Confirmation Delay at the start of an hour.
  - Dynamic 10% Stop Loss with tight (+0.05c) Trailing Stops.

### 3.2 The Meteorologist 🌦️ (Weather Arbitrage V2)
- **Target:** Kalshi Daily Temperature/Precipitation brackets (e.g., Chicago, NYC, DFW).
- **Methodology:**
  - Cross-references independent NWS spot readings with active Kalshi YES/NO probabilities.
  - Generates \"Temperature Velocity\" to predict if a city will hit its peak temperature threshold before the local end-of-day settlement.
  - **Historical Bias Model:** Injects mathematical bias per region (e.g., NWS generally under-predicts NYC by 0.5°F, over-predicts Chicago by +0.8°F).
- **Risk Overlays:**
  - \"Winner Guard\": Halts shorting if the intra-day high has already breached the market strike.
  - Slot Limits: Enforces maximum 1 active position per city.

### 3.3 The Condor 🦅 (Range-bound Iron Condor)
- **Target:** Fed Rates, Treasury Yields, EUR/USD, Wide Weather Brackets.
- **Methodology:**
  - A premium-selling configuration. Opens short positions (selling YES contracts) on the extreme outer boundaries (upper and lower bounds) when statistical likelihood heavily favors the asset remaining inside a central channel.
  - Automatically accounts for time-decay (Theta) as contract settlement approaches.

---

## 4. Telemetry and UI

### 4.1 Live Terminal Dashboard (`src/visualization/dashboard.py`)
- **Render Mode:** Terminal/ASCII-based \"Dense Mode\" with localized refreshing.
- **Information Density:** Provides real-time metrics on Strategy Signal Win-Rates, specific ticker statuses, active bid/ask spreads, margin utilization, and overall Portfolio PnL.
- **Mascot Integration:** Dynamic 16-color ANSI rendering of \"Mr. Krabs\" with contextual sprite animations (Running, Money, Idle) responding to portfolio velocity.
- **Logging Pipeline:** Generates contiguous CSV histories of all evaluated signals, bid/ask spreads, and trade outcomes for downstream ML ingest.

---

## 5. Development Pipeline & Governance

All strategy development strictly adheres to the \"Scientific Method Pipeline\":

1.  **Harvest Phase:** Run strategies in passive/listen-only mode against live Kalshi production or demo servers to log exact bid/ask environments.
2.  **Audit Phase:** Analyze raw signal validity (`run_dashboard.py` logs) through historical `sim_engine`.
3.  **Refinement Phase:** Apply parameter sweeps to adjust bounds, tighten RSI/MACD confirmers, or expand stop-losses.
4.  **Deployment Lock:** A strategy remains sandboxed un-linked from live capital until demonstrating an isolated backtested win-rate strictly greater than 95%.

### 5.1 Project Directory Structure
*   `src/core/` - Foundational OMS, risk logic, and type definitions.
*   `src/data/` - Async APIs for NWS, Kalshi, and Coinbase.
*   `src/engine/` - Backtesting engine and Live Orchestrators.
*   `src/strategies/` - Individual isolated algorithmic logic blocks.
*   `src/visualization/` - Terminal GUI and ANSI assets.
*   `scripts/` - Entry-points for dashboard, simulators, and lab optimizations.
*   `logs/` - Sacred, non-destructive data lake for all previous harvests.

---
*End of Document. All Glory to the Orchestrator.*
