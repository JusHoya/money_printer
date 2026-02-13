# Money Printer 🖨️💵

> "Because why earn money when you can print it? (Legally, via algorithmic prediction markets, obviously. Put the counterfeiting press away, monkey.)"

## Overview
**Money Printer** is a high-precision algorithmic trading tool designed for the **Kalshi** prediction market. 

**The Mission:** Turn a meager **$100** seed into a **$100,000** empire. 
**The Method:** Cold, hard, data-driven logic. No feelings, just profit.

## Current Strategies (Alpha)

### 1. The Meteorologist 🌦️ (V1 & V2)
*Targeting Weather Markets*
- **Logic:** Cross-references National Weather Service (NWS) data to predict temperature outcomes.
- **V2 Enhancements:**
    - **Bias Correction:** City-specific forecast adjustments (NYC: -0.5°F, Chicago: +0.8°F)
    - **Temperature Velocity:** Tracks heating/cooling rate for intraday signals
    - **CLI Timing Awareness:** Accounts for NWS settlement timing (LST vs DST)

### 2. The Trend Catcher 📈 (V1 & V2)
*Targeting Crypto Markets*
- **Logic:** 15-Minute Momentum Breakout with Trend Confirmation.
- **V2 Enhancements:**
    - **RSI/MACD Confirmation:** Technical indicators filter false breakouts
    - **Mean Reversion Mode:** Trades ranging markets when price is between 0.45-0.55
    - **Dynamic Position Sizing:** Scales 5-10 contracts based on signal strength
- **Key Features (Both Versions):**
    - **120s Delay:** Waits 2 minutes at cycle start to filter fake-outs
    - **Dynamic Stops:** Trailing Stop Loss activates when profitable
    - **Early Settlement:** Auto-closes if price hits 0.99/0.01 for >10 minutes

### 3. The Condor 🦅 (NEW)
*Iron Condor Strategy for Range-Bound Markets*
- **Targets:** Fed Rates, Treasury Yields, EUR/USD, Weather Brackets
- **Logic:** Sells outer-strike contracts for premium when outcome expected within range
- **Risk Management:** Position sizing based on max portfolio exposure (5% per bracket)

## Development Protocol 🛡️

We don't gamble; we calculate.
1.  **Research & Validation:** Extensive validation of data sources before a single line of code is committed.
2.  **The Sandbox (Demo):** All algorithms start in the Kalshi Demo environment.
3.  **The 95% Rule:** No bot touches real money until it sustains a **>95% accuracy rate** in simulation.
4.  **The Kill Switch:** A verbose dashboard with real-time charting and an immediate manual override.

## Setup Instructions

### 1. Kalshi API (Demo Environment)
You need to practice before you play.
1.  Go to the **[Kalshi Demo Site](https://demo.kalshi.co/)**.
2.  Sign up (Fake data is fine, it's a sandbox).
3.  Navigate to **Account Settings > API Keys**.
4.  Click **Create New API Key**.
5.  **CRITICAL:** Save the `Key ID` and the `RSA Private Key` immediately. They will not be shown again.
6.  Add these to your local `.env` file (see `.env.example`).

### 2. Weather Data (NWS)
The government gives this away for free. Let's take it.
- **Requirement:** No API Key needed yet.
- **Config:** You MUST provide a specific `User-Agent` header in your HTTP requests (e.g., `(my-app-name, my-email@example.com)`).

### 3. Crypto Data (e.g., Coinbase)
1.  Log in to Coinbase (or your preferred exchange).
2.  Go to **Settings > API**.
3.  Create a new API Key with `public` read permissions (we just need price data, not wallet access).

## Usage

### 1. The Command Center (Live Dashboard) 🖥️
Run the real-time monitoring dashboard to see the bots in action.
```bash
python scripts/run_dashboard.py
```
*   **Features:** Real-time PnL tracking, live market feeds (BTC/Weather), and strategy signal logs.
*   **Safety:** Press `Ctrl+C` for an immediate emergency stop.
*   **Demo Limitations:** The Kalshi Demo environment has fewer active markets than the paid platform. Signals for `KXBTCHOURLY` (Hourly Bitcoin) may generate "Ghost Trades" (Simulated execution without real order book match). This is intentional for data harvesting and model training purposes.

### 2. Simulation & Training 🏋️
Before risking real capital, run simulations using live or mock data.
```bash
# Run a 30-step simulation for the Crypto strategy using LIVE data
python scripts/simulate.py --strategy crypto --days 30 --live

# Run a mock Weather simulation for 10 steps
python scripts/simulate.py --strategy weather --days 10
```

## Strategy Guide 📚

### The Meteorologist 🌦️ (V1 → V2)
| Version | Approach |
|---------|----------|
| V1 | NWS forecast vs market sentiment |
| V2 | + City bias correction, temperature velocity, CLI timing |

- **Triggers:** Buy YES if forecast > strike + buffer, Sell YES if forecast < strike - buffer
- **V2 Bias Table:** NYC (-0.5°F), Chicago (+0.8°F), LAX (+0.2°F), Miami (-0.3°F), DFW (+1.0°F)

### The Trend Catcher 📈 (V1 → V2)
| Version | Approach |
|---------|----------|
| V1 | Momentum breakout (price crosses 0.55/0.45) |
| V2 | + RSI/MACD confirmation, mean reversion fallback |

- **Triggers:**
    - **Buy YES:** Price > $0.55 (V2: requires RSI < 75 AND MACD bullish)
    - **Sell YES:** Price < $0.45 (V2: requires RSI > 25 AND MACD bearish)
    - **Mean Reversion (V2 only):** Trade against extremes when price is 0.45-0.55
- **Risk Management:** 10% Stop Loss, Trailing Stops, 120s confirmation delay

### The Condor 🦅 (NEW - Bracket Strategy)
- **Targets:** Fed Rates (±0.50 bps), Treasury 10Y (±0.25%), EUR/USD (±0.015), Weather (±3°F)
- **Logic:** Sell contracts at outer strikes when price expected to stay within range
- **Position Sizing:** Max 5% portfolio exposure per bracket

## The Scientific Method Pipeline 🧪

We do not deploy until we have empirical proof of profitability.

1.  **The Harvest (Data Collection)** 🚜
    Run the dashboard for extended periods (e.g., 6-24 hours) to capture live market data.
    ```bash
    python scripts/run_dashboard.py
    ```

2.  **The Audit (Simulation)** 🔬
    Replay harvested logs through the **Lab** to verify strategy performance.
    ```bash
    # Single strategy audit
    $env:PYTHONPATH = "."; python scripts/lab.py --audit
    
    # V1 vs V2 head-to-head comparison
    $env:PYTHONPATH = "."; python scripts/lab.py --compare
    
    # Parameter optimization (grid search)
    $env:PYTHONPATH = "."; python scripts/lab.py --optimize
    ```

3.  **The Refinement (Optimization)** 🧬
    If Win Rate < 95%, manually adjust strategy parameters (e.g., Trailing Trigger, Confirmation Delay) in `src/strategies/crypto_strategy.py` and re-run the Audit.
    *Future: Automated parameter swarms.*

4.  **Deployment** 🚀
    Only when the Audit returns **>95% Win Rate** do we switch the `sim_engine` to Live Execution Mode.

## Troubleshooting
- **API Errors:** Ensure `.env` is populated and `kalshi_priv.key` is in the root directory.
- **Missing Deps:** Run `pip install -r requirements.txt`.

---
*Powered by Skippy the Magnificent. Optimized for Scientific Rigor.*