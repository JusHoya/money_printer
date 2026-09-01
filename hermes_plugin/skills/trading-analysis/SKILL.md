---
name: trading-analysis
description: Analyze Money Printer trading performance — status reviews, loss patterns, harvest health, and anomaly detection
version: 2.0.1
metadata:
  hermes:
    category: finance
    tags: [trading, kalshi, analysis, pnl]
    requires_toolsets: []
---

# Trading System Analysis

## Current System Posture (2026-09 revival)

- Four registered bots. **weather** paper-trades in the simulator (activated
  2026-09-01 to exercise the settlement leg and build PnL history). **gas**,
  **mention**, and **crypto_annual** are feed-only harvesters — they record
  market tape and NEVER emit trades.
- ALL execution is simulated. The Kalshi client is read-only; real capital is
  structurally impossible. "PnL" always means simulated PnL.
- There is no per-cycle balance reset and no in-runtime ML retraining (both
  were removed in the Phase 0 pivot). PnL is continuous from the session's
  starting balance — the sandbox launches with `SIM_BALANCE` (default
  $3,000); read the actual figure from `mp_status`, never assume a number.
- Strategy names you will see in the journal: "ML Weather" and
  "Meteorologist V2" (both run under the weather bot). "Gas Convergence" and
  "Mention Base Rate" exist in code but are behind trading flags that are off,
  so trades from them indicate a posture change worth flagging.

## When to Use

- User asks about performance, PnL, win rate, or how things are going
- User asks to compare strategies or analyze losses
- User asks whether the harvesters are actually harvesting
- You detect an anomaly (extreme drawdown, system unreachable, unusual patterns)

## Procedure: Quick Status Check

1. Call `mp_health` to confirm the system is alive
2. Call `mp_status` for portfolio equity and open positions
3. Call `mp_rolling_stats` with hours=24
4. Summarize: lead with 24h PnL and WR%, then equity as a percentage of the
   starting balance from `mp_status` (e.g., at the $3,000 default, -$150 =
   5% drawdown)

## Procedure: Loss Analysis

1. Call `mp_journal` with last_n=100
2. Filter to losing trades (pnl < 0)
3. Group by:
   - **close_reason**: Which exit mechanism causes the most losses? (STOP_LOSS, EXPIRY, EARLY_SETTLEMENT)
   - **strategy_name**: "ML Weather" vs "Meteorologist V2"
   - **Time patterns**: Are losses clustered at certain hours or cities?
4. Call `mp_rolling_stats` with hours=1, hours=4, hours=24 to see if losses are accelerating
5. Report findings with specific numbers and actionable insights

## Procedure: Strategy Comparison

1. Call `mp_rolling_stats` with hours=24 (gives per-strategy breakdown)
2. Call `mp_win_rates` for the recency-window history (last 50 closed trades
   per strategy; legacy-format entries are ignored by the risk manager)
3. Call `mp_journal` with strategy_filter for each strategy of interest (last_n=30)
4. Compare:
   - PnL contribution (which strategy makes/loses the most?)
   - Win rate vs EV (high WR with low EV = grinding; low WR with high EV = big swings)
   - Average win vs average loss (risk-reward ratio)
5. Recommend: which strategy should stay active, which should be paused?

## Procedure: Harvest Health (feed-only bots)

1. Call `mp_data_log` — recent rows should include symbols from every active
   bot's series: KXHIGH* (weather), KXAAAGAS* (gas), KX*MENTION (mention),
   KXBTCY/KXETHY (crypto_annual)
2. Call `mp_session_log` — look for "FEED-ONLY: N markets recorded" lines and
   "Market Fetch Fail" errors
3. A series absent from the tape for hours is a harvest gap worth flagging
   even though no money is at stake — the tape is the product

## Procedure: Anomaly Detection (used by cron jobs)

1. Call `mp_health` — if unreachable, alert immediately
2. Call `mp_status` — check:
   - Portfolio equity vs the starting balance. If down 50% (at the $3,000
     default: equity below $1,500), the daily drawdown kill switch fires and
     trading stops — flag it
   - Number of open positions vs the current bankroll stage's cap (the
     $3,000 default starts in the Growth stage: 12 positions). Above the
     cap, flag — stage caps should prevent it
   - Any bot showing active=false unexpectedly
   - Any EXECUTED trade from a strategy other than "ML Weather" /
     "Meteorologist V2" — the other bots are feed-only, so that is a posture
     change, flag it
3. Call `mp_rolling_stats` with hours=1 — if 1h PnL is worse than -10% of the
   starting balance (at the $3,000 default: below -$300), flag rapid loss
4. Only alert if something is genuinely wrong. Normal operation = silence

## Key Numbers Reference

- Starting balance: `SIM_BALANCE` at sandbox launch (default $3,000);
  simulated, continuous — no per-cycle reset. Get the live figure from
  `mp_status`
- Daily drawdown kill switch: 50% of the day's starting balance
- Weather allocation bucket: 30% of balance; mention bucket (pre-wired,
  inert while feed-only): 20%
- Daily trade cap: 40; per-entry hard cap: 50 contracts
- Fees: Kelly sizing and the EV gate price them in; weather maker fee is $0
  on the standard schedule
- Max position risk: enforced by RiskManager bankroll stages, keyed to
  balance and sized in percentages — Seed 10%/trade (max 5 positions),
  Early 5%/trade (max 8), Growth 5%/trade (max 12 — the stage the $3,000
  default starts in), Scale 5%/trade (max 15), Compound 2.5%/trade (max 20)

## Pitfalls

- The journal timestamps may use "Z" suffix (UTC) or ISO format — both are valid
- PnL of exactly $0.00 means the trade broke even (not an error)
- `mp_training` reads offline training state; in-runtime retraining was
  removed (PRD FR-0.2), so stale or absent training data is normal, not an
  outage
- Old journal entries may reference deleted strategies ("Crypto Trend V3",
  "LongShot Fader") from before the Phase 0 teardown — history, not activity
- Feed-only bots log INFO lines every tick; volume of log lines is not
  evidence of trading
