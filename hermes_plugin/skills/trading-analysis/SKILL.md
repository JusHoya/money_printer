---
name: trading-analysis
description: Analyze Money Printer trading performance — cycle reviews, loss patterns, strategy comparisons, and anomaly detection
version: 1.0.0
metadata:
  hermes:
    category: finance
    tags: [trading, kalshi, analysis, pnl]
    requires_toolsets: []
---

# Trading System Analysis

## When to Use

- User asks about performance, PnL, win rate, or how things are going
- User asks to compare strategies or analyze losses
- A new cycle completes and you need to generate a report
- You detect an anomaly (extreme drawdown, system unreachable, unusual patterns)

## Procedure: Quick Status Check

1. Call `mp_health` to confirm the system is alive
2. Call `mp_rolling_stats` with hours=4 (current cycle window) AND hours=24 (daily)
3. Summarize: lead with 24h PnL and WR%, then current cycle PnL
4. If PnL is negative, note the magnitude relative to $3,000 balance (e.g., "-$150 = 5% drawdown")

## Procedure: Cycle Completion Report

1. Call `mp_training` to get the latest cycle record from cycle_history
2. Call `mp_rolling_stats` with hours=24
3. Call `mp_win_rates` for all-time per-strategy performance
4. Report:
   - Cycle number, duration, and PnL
   - Trade count with W/L split and win rate
   - Training AUC (>0.65 = learning, >0.75 = strong)
   - Best and worst strategy by PnL in the last 24h
   - Compare current cycle to previous cycles (trend: improving or declining?)

## Procedure: Loss Analysis

1. Call `mp_journal` with last_n=100
2. Filter to losing trades (pnl < 0)
3. Group by:
   - **close_reason**: Which exit mechanism causes the most losses? (STOP_LOSS, EXPIRY, EARLY_SETTLEMENT)
   - **strategy_name**: Which strategy is bleeding?
   - **Time patterns**: Are losses clustered at certain hours?
4. Call `mp_rolling_stats` with hours=1, hours=4, hours=24 to see if losses are accelerating
5. Report findings with specific numbers and actionable insights

## Procedure: Strategy Comparison

1. Call `mp_rolling_stats` with hours=24 (gives per-strategy breakdown)
2. Call `mp_win_rates` for historical context
3. Call `mp_journal` with strategy_filter for each strategy of interest (last_n=30)
4. Compare:
   - PnL contribution (which strategy makes/loses the most?)
   - Win rate vs EV (high WR with low EV = grinding; low WR with high EV = big swings)
   - Average win vs average loss (risk-reward ratio)
5. Recommend: which strategy should stay active, which should be paused?

## Procedure: Anomaly Detection (used by cron jobs)

1. Call `mp_health` — if unreachable, alert immediately
2. Call `mp_status` — check:
   - Portfolio equity vs $3,000 starting balance. If <$1,500 (50% drawdown), flag
   - Number of open positions. If >20, flag (unusual)
   - Any bot showing active=false unexpectedly
3. Call `mp_rolling_stats` with hours=1 — if 1h PnL < -$200, flag rapid loss
4. Only alert if something is genuinely wrong. Normal operation = silence

## Key Numbers Reference

- Starting balance: $3,000 per cycle
- Drawdown kill switch: 50% ($1,500 remaining)
- Cycle duration: ~4h (wall-clock) or until drawdown
- Graduation: 8h continuous positive PnL
- Fee tolerance: built into Kelly sizing
- Max position risk: enforced by RiskManager
- AUC target: >0.65 minimum, >0.75 strong

## Pitfalls

- The journal timestamps may use "Z" suffix (UTC) or ISO format — both are valid
- PnL of exactly $0.00 means the trade broke even (not an error)
- Strategy names in the journal may differ from bot names (btc_15m bot runs multiple strategies like "Crypto Trend V3", "LongShot Fader")
- Training AUC of 0.50 = random chance (model is not learning)
- The system resets balance to $3,000 every cycle — a "bad" PnL of -$300 in one cycle does NOT carry over
