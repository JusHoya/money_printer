# Product Requirements Document (PRD)

## Product Name: Money Printer 🖨️💵
**Version:** 1.5.0 (Next Steps & Simulation Refinement)
**Author:** Skippy the Magnificent (via Agent Orchestration)
**Status:** Alpha / Strategy Overhaul Phase

---

## 1. Executive Summary
**Money Printer** remains a high-precision, low-latency algorithmic trading platform targeted at the Kalshi prediction market, operating within a strict `Harvest -> Audit -> Refinement -> Deployment` pipeline. The core objective is executing a strictly data-driven, systematic approach to grow a $100 seed into $100,000. 

To break out of the current simulation phase and begin trading live capital, the system must achieve a sustained algorithmic win-rate of >95%. The latest performance data indicates our previous momentum strategies have degraded. This V1.5 PRD serves as the master roadmap for upgrading the **Bitcoin (Hourly and 15-Minute) Strategies** by harmonizing our currently working system architecture with the advanced order book mathematics and settlement logic detailed in the Deep Research PRD.

### 1.1 Core Principles (Unchanged)
- **Algorithmic Purity:** Pure statistical edge execution. No emotional trading.
- **Scientific Rigor:** "Shadow Analysis" phase validation before live execution.
- **Capital Preservation:** Dynamic Portfolio Exposure cap at 50% and Fractional Kelly Criterion sizing.
- **High-Fidelity Telemetry:** Terminal-based ASCII instrumentation for real-time tracking ("Dense Mode").

---

## 2. System Architecture (The Ground Truth)
The existing event-loop architecture, Orchestrator, and Provider API interfaces are functioning correctly and remain the baseline truth. We will **not** rewrite how the system communicates with Kalshi; rather, we will augment how the system interprets the data it receives.

1. **Providers (`src/data/`)**: The current Kalshi REST/API implementation is our truth state, securely using the read-only key (`Kalshi_priv_key_1_readOnly.key`). 
2. **Matching Engine (`src/core/matching_engine.py`)**: High-fidelity paper trading engine.
3. **Risk Manager (`src/core/risk_manager.py`)**: Will be augmented with new circuit breakers specifically for high-frequency operations.
4. **Orchestrator Engine (`src/engine/run_dashboard.py`)**: Manages the feed and ticker resolutions (e.g., dynamic "Ladder" resolution).

---

## 3. BTC Market Strategy Upgrades (The Trend Catcher V3)
To hit the 95% threshold, the "Trend Catcher" strategy is being overhauled for both `KXBTCHOURLY` and the new `KXBTC15M` intervals. Strategy logic will be updated to reflect the mechanics of high-frequency events.

### 3.1 Reciprocal Order Book Mathematics
Because Kalshi natively returns only Bids, the strategy logic must now dynamically reconstruct the Level 2 order book using **Reciprocal Physics**:
- `Implied Ask (Yes) = 1.00 - Highest Bid (No)`
- `Implied Ask (No) = 1.00 - Highest Bid (Yes)`

### 3.2 CF Benchmarks (BRTI) Settlement Targeting
Predictions will pivot from abstract price spotting to structural alignment with Kalshi’s oracle. 
- **Methodology:** Models must explicitly forecast the **60-second moving average of the CF Benchmarks Real Time Index (BRTI)** in the final minute before contract expiration.

### 3.3 Enhanced Quantitative Triggers
The strategy will layer the following Order Book mechanics over existing momentum confirmers:
1. **Order Book Imbalance (OBI):** Calculated as `Imbalance = Bid_Qty / (Bid_Qty + Ask_Qty)` using Reciprocal Physics.
   - **Trigger:** A structural momentum signal fires only when `Imbalance > 0.70`.
2. **Cross-Spread Exploitation:** Detecting when combined Yes/No cost falls strictly under $1.00, representing a risk-free round trip (Arbitrage logic).

---

## 4. Upgraded Risk Management & Safety Gates
To protect the portfolio during the volatile 15M/Hourly execution windows, `risk_manager.py` must adopt these constraints:

1. **"Final Minute" Freeze:** The orchestrator will block any new position entries in the final 60 seconds of a 15-minute or hourly contract to avoid unpredictable BRTI settlement variance.
2. **Stricter Circuit Breakers:**
   - **5% Daily Loss Cutoff:** Halts all strategy execution for 24 hours.
   - **10% Max Drawdown Limit:** Per active strategy instance.
3. **Kelly Dampener:** Position sizing will utilize a strict **0.75x dampener** on the Fractional Kelly Criterion calculation to throttle over-leveraging.

---

## 5. Maintained Active Strategies
The integration of BTC V3 mechanics will not disrupt existing active modules:
- **The Meteorologist 🌦️ (Weather Arbitrage V2):** Operates on NWS bias models, Winner Guard, and Regional Slot Limits.
- **The Condor 🦅:** Maintains range-bound premium selling operations for macro events.

---

## 6. Implementation Blueprint & Next Steps

This document governs the immediate development pathway to cross the 95% win-rate threshold:

### Phase 1: Microstructure Upgrades (Immediate)
- Integrate `KXBTC15M` ticker resolution seamlessly alongside `KXBTCHOURLY`.
- Inject the **Reciprocal Order Book Mathematics** (Implied Ask calculations) into the strategy state logic.
- Implement the `Final Minute Freeze` and `0.75x Dampener` within the Risk Manager.
- Refine the Quantitative triggers to execute based on `OBI > 0.70`.

### Phase 2: Shadow Analysis Validation
- Run the upgraded V3 strategy in purely "Shadow Mode" without theoretical capital execution to ensure Reciprocal Physics and OBI calculations accurately track underlying Kalshi behavior.
- Validate that the strategy effectively predicts the 60-second BRTI moving average.
- Maintain the non-destructive logging of all market data/signals into the `logs/` directory for historical auditing.

### Phase 3: Live Capital Deployment
- Upon demonstrating a sterile >95% algorithmic win-rate across sequential 15-minute and hourly trading windows, the Orchestrator will be authorized to transition off the `read_only` constraint and commence live execution on the $100 -> $100k mission.
