# PRD: Deep Research - Kalshi BTC High-Frequency Trading System

## 1. Executive Summary
This document specifies the architectural and implementation requirements for a fully autonomous trading agent targeting Kalshi’s 15-minute and 1-hour Bitcoin (BTC) prediction markets. The system leverages regulated CFTC-DCM infrastructure to execute high-frequency events based on CF Benchmarks settlement logic, reciprocal order book mathematics, and multi-agent LLM ensemble probability forecasting.

## 2. Market Microstructure & Contract Specifications

### 2.1 Ticker Nomenclature
- **Series KXBTC15M**: 15-minute intervals. Ticker format: `KXBTC15M-<YYMMDDHHMM>-<STRIKE>`.
- **Hourly Series**: 1-hour intervals. Based on discrete price targets (e.g., "$68,250 or above").
- **Price Precision**: $0.01 increments (1¢ to 99¢).

### 2.2 Settlement Oracle (CF Benchmarks)
- **Primary Oracle**: CF Benchmarks Real Time Index (BRTI).
- **60-Second Smoothing Window**: Expiration is calculated from the average of 60 RTI data points (one per second) in the final minute before expiration.
- **Anti-Manipulation**: Trimmed mean calculation (removing top/bottom 20% for specific structures).
- **Requirement**: Predictive models must forecast the 60-second moving average of the BRTI, not a discrete terminal price.

## 3. Order Book Mathematical Framework

### 3.1 Reciprocal Physics
Kalshi only returns **Bids**. Implied **Asks** must be calculated locally:
- `Implied Ask (Yes) = 1.00 - Highest Bid (No)`
- `Implied Ask (No) = 1.00 - Highest Bid (Yes)`

### 3.2 Action Logic
| Objective | Market Action | Rationale |
| :--- | :--- | :--- |
| **Buy YES** | Match against NO BID | Collateralizes the $1.00 contract. |
| **Buy NO** | Match against YES BID | Collateralizes the $1.00 contract. |
| **Close YES** | Submit Buy order for NO | Neutralizes risk, guaranteed $1.00 payout. |

## 4. API & Technical Integration

### 4.1 Authentication (RSA-PSS)
- **Mechanism**: RSA-PSS signature-based authentication.
- **Headers**: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.
- **Sync Requirement**: NTP synchronization is mandatory to avoid 401 Unauthorized errors (clock skew).

### 4.2 Order Execution
- **Endpoint**: `POST /trade-api/v2/portfolio/orders`
- **Preferred Time-In-Force**: `fill_or_kill` (FoK) to prevent orphan orders in volatile 15M windows.
- **Batching**: Support for up to 20 orders per request via `/portfolio/orders/batched`.

### 4.3 WebSocket Ingestion (WSS)
- **Endpoint**: `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Channels**: `orderbook_delta` (Level 2 state reconstruction) and `fill` (execution tracking).
- **Sequence Integrity**: Must track `seq` field to detect dropped packets and trigger cache refreshes.

## 5. Quantitative Strategies

### 5.1 Cross-Exchange Statistical Arbitrage
- **Platform**: Kalshi vs. Polymarket.
- **Trigger**: Combined cost of complementary positions < $1.00 (including fees).
- **Execution**: Concurrent API calls with size clamped to the lowest top-of-book volume.

### 5.2 Order Book Imbalance (OBI)
- **Formula**: `Imbalance = Bid_Qty / (Bid_Qty + Ask_Qty)`
- **Threshold**: `Imbalance > 0.7` signals structural momentum.
- **Glitch Exploitation**: Detecting combined Yes/No spreads < $1.00 for risk-free round-trips.

### 5.3 Multi-Agent LLM Ensembles
- **Lead (Grok-4)**: Sentiment & X-flow (30%).
- **News (Claude 3.5 Sonnet)**: Macro releases (20%).
- **Bull/Bear Specialists (GPT-4o/Gemini 2.5 Flash)**: Catalyst identification (35% total).
- **Risk (DeepSeek R1)**: Logical auditing (15%).
- **Position Sizing**: Fractional Kelly Criterion: `f* = p - (q / b)` with a 0.75x dampener.

## 6. Implementation Blueprint

### Phase 1: Foundation
- RSA-PSS signer implementation.
- NTP sync daemon.
- Basic REST client for Kalshi V2.

### Phase 2: State Engine
- WSS client with automatic reconnection.
- L2 Order Book reconstruction with reciprocal ask calculation.
- Fill management and P&L tracking database (SQLite).

### Phase 3: Strategy & Execution
- Multi-model LLM inference loop with Pydantic schema enforcement.
- Cross-platform price normalization (Kalshi vs Polymarket).
- Execution router with `fill_or_kill` logic.

### Phase 4: Risk Management
- Circuit breakers: 5% daily loss cutoff, 10% max drawdown.
- Sector concentration caps (20% per strike).
- 60-second "Final Minute" freeze to avoid settlement variance.

## 7. Reference Architectures
- **`ryanfrigo/kalshi-ai-trading-bot`**: LLM-driven autonomous execution.
- **`CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot`**: Cross-platform ticker mapping.
- **`taetaehoho/poly-kalshi-arb`**: High-performance Rust-based arbitration.
- **`OctagonAI/kalshi-deep-trading-bot`**: Hedged AI evaluation with Pydantic models.

## 8. Conclusion
Deployment of this system requires transitioning from linear spot mechanics to binary probability mathematics. Success is predicated on sub-second execution, rigorous state synchronization, and the exploitation of microstructural imbalances in 15-minute settlement windows.
