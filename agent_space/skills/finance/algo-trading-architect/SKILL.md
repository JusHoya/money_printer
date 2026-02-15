---
name: algo-trading-architect
description: Designs, scaffolds, and reviews algorithmic trading strategies and bot infrastructure.
version: 1.0.0
---

# Algo Trading Architect Skill

<skill_definition>
The **Algo Trading Architect** skill is the engineering backbone of the Financial Strategist. It translates high-level investment theses (from Market Analysis) into concrete, executable code structures. It understands the lifecycle of a bot: Strategy -> Backtest -> Paper Trade -> Live Trade.
</skill_definition>

<usage_instructions>
1.  **Design**: Define the class structure, event loops, and data ingestion pipelines for a bot.
2.  **Scaffold**: Generate boilerplate code (Python/C++) for standard frameworks (e.g., `backtrader`, `lean`, `ccxt`).
3.  **Backtest Config**: Define the parameters for testing (timeframe, slippage, commission models).
4.  **Review**: Analyze existing bot code for logic errors, look-ahead bias, or overfitting.
</usage_instructions>

<output_template>
## Algo Strategy Blueprint
*   **Strategy Name**: [Name]
*   **Target Asset**: [Asset Class]
*   **Logic Pseudocode**:
    ```python
    def on_market_data(self, data):
        if indicator > threshold:
            buy()
    ```
*   **Risk Constraints**: [Max Position Size, Stop Loss]
*   **Required Libraries**: [pandas, numpy, ccxt, etc.]
</output_template>

<kalshi_operational_knowledge>
## Kalshi API — Critical Lessons Learned

### 1. Pagination Bug (CRITICAL)
The Kalshi `/markets` endpoint returns paginated results (~200 per page). High-volume series like `KXBTC15M` accumulate **2000+ markets** over time (mostly finalized). The API does **NOT** support a `status` query parameter (returns HTTP 400).
- **ALWAYS** use cursor-based pagination when querying high-volume series.
- Filter for `status == 'active'` **client-side** after fetching.
- Break pagination early once active markets are found to minimize API calls.

### 2. Price Domain Mismatch (CRITICAL)
Kalshi options trade in the `0.00-1.00` price range (probability). The underlying asset (e.g., BTC) has its own price domain (e.g., `~$69,000`).
- **NEVER** use the underlying spot price as an exit price for option trades. Always use `estimated_price` (the option's bid/ask).
- **Gate strategy execution** on successful Kalshi market resolution. If no active Kalshi market is found, do NOT run the strategy on raw spot data — this causes false signals (e.g., comparing $69,000 against a 0.55 threshold).

### 3. Market Lifecycle Statuses
Kalshi markets cycle through: `initialized` → `active` → `finalized` → `settled`.
- Only `active` markets can be traded.
- `initialized` markets are created but not yet open (e.g., tomorrow's batch).
- The "Ghost Ticker" phenomenon occurs when a series has zero active markets during off-hours.

### 4. Ticker Format Reference
- **15-Min BTC**: `KXBTC15M-{YY}{MON}{DD}{HHMM}-{MM}` (e.g., `KXBTC15M-26FEB060145-45`)
- **Hourly BTC**: `KXBTC-{YY}{MON}{DD}-{HH}00-T{strike}` (e.g., `KXBTC-26JAN31-1800-T98000`)
- **Weather**: `KXHTEMP{CITY}-{YY}{MON}{DD}-T{strike}` (e.g., `KXHTEMPCNYC-26FEB14-T45`)

### 5. Strategy Overhaul Checklist
When reworking strategies, **ALWAYS** verify:
- [ ] `_resolve_smart_ticker` uses cursor-based pagination (Kalshi API does NOT support `status` filter param)
- [ ] Strategy `.analyze()` only runs if Kalshi market data was successfully fused (not raw spot data)
- [ ] Exit prices use option price (`estimated_price`), not underlying spot price (`current_spot_price`)
- [ ] Log entries confirm `Smart Resolve {series} -> {ticker}` appears for each target market
</kalshi_operational_knowledge>
