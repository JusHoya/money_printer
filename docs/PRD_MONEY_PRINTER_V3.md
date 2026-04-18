# Money Printer V3 — Product Requirements Document

**Version:** 3.0
**Date:** 2026-04-16
**Author:** Synthesis of 5-agent audit + user directive (multi-crypto expansion)
**Supersedes:** Sections 5-7 of `PRD_MONEY_PRINTER_V2.md`

---

## 0. Context & Motivation

A comprehensive audit conducted on 2026-04-16 produced five independent agent reports covering: VM operational state, trade-journal statistics, strategy + ML code, quantitative-finance literature, and hidden-trend mining. The audit was run after 23 days of continuous paper trading on a $3,000 simulated bankroll.

**Headline findings:**

- Paper book: **-$68,894** over 5,078 trades (positive days: 5/23; max drawdown -$69k).
- Three interlocking bugs — each <30 lines of code — cause ~80% of the bleed:
  - `ProbabilityCalibrator` never loads at inference → model confidence is **anti-predictive** (high-conf bucket = 39% WR; low-conf bucket = 73% WR).
  - Fee-aware EV gate deducts only one leg of fees; every round trip is underpriced by ~$0.01/contract.
  - Cycle-rollover `positions.clear()` bypasses `_on_trade_close`; `SimulatedExchange` is in-memory so watchdog restarts also nuke open positions. Result: `strategy_win_rates.json` = `{}`, cycles report `trades=N, wins=0, pnl=0`.
- One strategy is quietly profitable: **ML BTC 15m — +$1,228, WR 66%, PF 3.81, 193 trades**.
- One large untapped edge was discovered: **BTC spot leads Kalshi hourly contracts by ~30 seconds** with +5c/contract mean PnL and 67% WR (N=1,607 trades across 54 hours of microstructure data).

**User directive for V3:** In addition to the audit fixes, expand the 15m-bot pattern from BTC-only to all available 15-minute crypto markets on Kalshi (ETH, SOL, XRP, DOGE — to be confirmed via market discovery).

The capital-promotion gate documented in memory (72h positive on $3k → 72h positive on $500 → real capital) has been met 0/3 times in the 23-day window. V3 MUST close that gate before any real-capital discussion.

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Current State Summary](#2-current-state-summary)
3. [Multi-Crypto 15m Expansion — Design](#3-multi-crypto-15m-expansion--design)
4. [Sprint Plan](#4-sprint-plan)
   - [Sprint 8: Stop the Bleeding](#sprint-8-stop-the-bleeding-1-2-days)
   - [Sprint 9: Recalibrate What's Left](#sprint-9-recalibrate-whats-left-3-5-days)
   - [Sprint 10: Concentrate Capital on the Winner](#sprint-10-concentrate-capital-on-the-winner-1-week)
   - [Sprint 11: Multi-Crypto 15m Expansion](#sprint-11-multi-crypto-15m-expansion-2-weeks)
   - [Sprint 12: Build Flagship "Spot-Lead" Strategy](#sprint-12-build-flagship-spot-lead-strategy-2-weeks)
   - [Sprint 13: Quant Infrastructure Upgrade](#sprint-13-quant-infrastructure-upgrade-3-4-weeks-ongoing)
   - [Sprint 14: Ops Hardening](#sprint-14-ops-hardening-3-5-days)
5. [Success Criteria & Capital-Promotion Gate](#5-success-criteria--capital-promotion-gate)
6. [Risk Register](#6-risk-register)
7. [Testing Strategy](#7-testing-strategy)
8. [Rollout & Deployment Protocol](#8-rollout--deployment-protocol)
9. [Dependencies](#9-dependencies)
10. [Appendix A — Bug Autopsies](#appendix-a--bug-autopsies)
11. [Appendix B — Literature References](#appendix-b--literature-references)

---

## 1. Goals and Non-Goals

### Primary Goals

1. **Stop the bleed.** Close the three interlocking bugs so that the system's internal accounting (win-rate persistence, fee debit, cycle PnL) is consistent and truthful.
2. **Make model confidence trustworthy.** Fix the calibrator load path + add isotonic calibration so `model_confidence` input to Kelly sizing is genuinely calibrated.
3. **Concentrate capital on what works.** Amplify ML BTC 15m in its proven high-WR regime (overnight ET, tight strike proximity); disable strategies with statistically significant negative edge (Late Sniper, Empirical Edge NO-side).
4. **Expand the 15m bot to all supported Kalshi crypto markets** (ETH, SOL, XRP, DOGE — list pending discovery) with per-asset or shared-model variants.
5. **Prototype and validate the BTC-spot-leads-hourly-contract edge** discovered by Agent 5 (+5c/contract, 67% WR flagship finding).
6. **Close the capital-promotion gate** with a defensible statistical criterion (Deflated Sharpe + MinTRL), not a naïve 72h-positive rule.

### Non-Goals

- Real-capital deployment in V3. V3 ends at "promotion-gate-ready on $3k sim, 72h positive on $500 sim."
- HFT market making. Kalshi's 7% fee rules this out.
- Full Almgren-Chriss execution optimization. 15-minute TTE + 7% fee dwarfs impact modeling.
- UI/UX rewrites. The web dashboard is adequate; all changes are backend.
- Equities/futures/options. Stay on Kalshi.

### Out of scope (for later)

- Live order placement via the real Kalshi API.
- Multi-asset portfolio optimization across crypto and weather (the two domains stay siloed for now).
- Alternative brokerages (PredictIt, Polymarket).

---

## 2. Current State Summary

### What's Working

| Component | Status | Evidence |
|---|---|---|
| Kalshi market discovery & auth | ✅ Working | 798 unique strikes parsed, 2,333 labels, API auth holds |
| BTC 15m ML model (with bugs!) | ✅ Net-profitable | +$1,228 over 193 trades, WR 66%, PF 3.81 |
| Coinbase BTC-USD spot feed | ✅ Stable | Used for fusion + lead-signal generation |
| NWS weather data pipeline | ✅ Working | Forecasts ingested; edge unclear pending calibrator fix |
| Web dashboard | ✅ Adequate | Terminal dashboard deprecated |
| Simulated exchange core | ⚠️ Works but leaks | In-memory positions lost on restart; cycle-rollover skips callback |
| Training loop | ⚠️ Runs but miscalibrated | Val AUC 0.77-0.79; symbol-level label leakage suspected |

### What's Broken (ranked by $ impact)

1. Calibrator never loads at inference (`src/ml/predictor.py:158`)
2. Fee EV gate single-leg deduction (`src/core/fee_calculator.py:92`)
3. Cycle rollover bypasses `_on_trade_close` (`scripts/run_dashboard.py:791`)
4. Stop-loss fills 8-10¢ below configured threshold (simulator microstructure — root cause TBD)
5. `strategy_win_rates.json` persistence is dead code (consequence of #3)
6. SimulatedExchange has no disk persistence; restarts nuke open positions
7. Train/val split has symbol-level label leakage
8. No `scale_pos_weight` for 72:28 class imbalance
9. Kelly blend mixes `model_confidence` with `entry_price`-derived quantities
10. `tanh_estimate(scale=1000)` calibrated for $1k BTC swings; BTC is at $90k
11. Hermes has no systemd unit; no `crontab -l` entry for the 5-min watchdog (despite memory stating deploy-complete)

### Dead or unused code (to prune, Sprint 8 cleanup)

- `CryptoArbitrageStrategy`, `Crypto15mTrendStrategy` (V1), `CryptoHourlyStrategy` (V1), `WeatherArbitrageStrategy` (V1), `CryptoLongShotFader`, `Crypto15mLateSniper`, `Crypto15mTrendStrategyV3` — all defined but not registered in any bot.
- Stale `sig.stop_loss = max(0.01, lp - 0.05)` in `ml_btc_15m.py:138,174` — `is_binary_event` in matching engine skips stops for 15m; this field is dead.

---

## 3. Multi-Crypto 15m Expansion — Design

The current `BTC15mBot` (`src/bots/btc_15m_bot.py`) hardcodes BTC at every layer: `KXBTC15M` series string, `CoinbaseProvider("BTC-USD")`, `get_symbols() → ["KXBTC15M"]`, and downstream `matching_engine.py` does substring match on `"KXBTC15M"` for binary-event classification.

### Goal

Support all 15-minute crypto markets Kalshi lists, with each market independently tradeable, sized, and monitored — without duplicating strategy code.

### Markets to discover (expected, verify in Sprint 11)

Based on Kalshi's public product catalog circa 2026-01:

| Series Expected | Underlying | Coinbase Symbol | Expected availability |
|---|---|---|---|
| `KXBTC15M` | BTC | `BTC-USD` | ✅ Live, already supported |
| `KXETH15M` | ETH | `ETH-USD` | Highly likely live — verify |
| `KXSOL15M` | SOL | `SOL-USD` | Possibly live — verify |
| `KXXRP15M` | XRP | `XRP-USD` | Possibly live — verify |
| `KXDOGE15M` | DOGE | `DOGE-USD` | Possibly live — verify |

**Discovery task (first sub-step of Sprint 11):** call the Kalshi `/series` endpoint with no filter and enumerate all series whose ticker matches `KX[A-Z]+15M$`. For each, check there are `active` or `initialized` markets in the current trading session. Write the discovered set to `config/crypto_15m_markets.yaml`.

### Architectural decisions

**AD-1: One `Crypto15mBot` class, instantiated per market.**
Rename `BTC15mBot` → `Crypto15mBot` in `src/bots/crypto_15m_bot.py`. Accept `market_config: CryptoMarketConfig` in `__init__`. Register one bot per discovered market.

```python
@dataclass
class CryptoMarketConfig:
    kalshi_series: str       # e.g. "KXETH15M"
    coinbase_symbol: str     # e.g. "ETH-USD"
    display_name: str        # e.g. "ETH 15m"
    tanh_scale: float        # per-asset price-swing magnitude
    min_strike_proximity: float  # per-asset sweet-spot filter in USD
    enabled: bool = True
```

**AD-2: Per-asset ML model, shared training pipeline.**
- Features: same 16-feature set, but `feat_btc_return_*` → `feat_spot_return_*`, `feat_btc_vol_*` → `feat_spot_vol_*`, `feat_btc_range` → `feat_spot_range`.
- Training script accepts `--asset=BTC|ETH|SOL|...`. Output file: `data/models/{asset}_15m_xgboost_latest.joblib`.
- Calibrator per asset: `data/models/{asset}_15m_calibrator_latest.joblib`.
- `ModelPredictor` keyed by asset string.
- Rationale: each crypto has its own volatility regime and spot-to-contract elasticity; a multi-asset model with asset-ID feature would muddle this. Revisit if training data per asset falls short of ~5k trades and predictive power degrades.

**AD-3: Parameterize matching engine binary-event detection.**
Replace `"KXBTC15M" in pos["symbol"]` (`matching_engine.py:468, 538`) with a regex `re.compile(r"^KX[A-Z]+15M-")`. Same for dashboard grouping logic.

**AD-4: Per-market Kelly budget.**
Risk manager applies the bankroll-stage caps per-market, not per-bot. No single crypto market may exceed 30% of portfolio exposure. Correlation among crypto markets is high (BTC-ETH daily corr > 0.7) — implement a soft cap: combined crypto exposure ≤ 60% of portfolio (weather fills the rest).

**AD-5: Shared strategy waterfall, asset-agnostic.**
`MLCrypto15mStrategy` (renamed from `MLBtc15mStrategy`) keys its model lookup by asset string parsed from the ticker. `LatencyArbStrategy`, `LongshotFaderV2`, `CrossSpreadArbStrategy` should already be asset-agnostic — verify and parameterize any BTC-specific thresholds.

**AD-6: Overnight-window gating stays per-asset.**
Agent 2 found BTC 15m WR 69-82% in the 20:00-02:00 ET overnight window. Whether this transfers to ETH/SOL depends on their own price-activity cycles. The overnight filter is PER-ASSET and gets re-calibrated as data accrues for each new market.

### Observability for multi-market

Dashboard groups and displays per-asset:
- Market counter (active tickers by series)
- Per-asset PnL day/week
- Per-asset model AUC + calibration slope
- Per-asset effective position count and exposure %

Trade journal line gets a `asset` field (BTC/ETH/SOL/...) for easy post-hoc slicing.

---

## 4. Sprint Plan

### Sprint 8: Stop the Bleeding (1–2 days)

**Outcome:** the three interlocking bugs are fixed, the two worst strategies are off, and the code is in a state where subsequent sprints can trust the internal accounting.

#### Scope

| # | Task | File:Line | Effort |
|---|---|---|---|
| 8.1 | Fix calibrator load path | `src/ml/predictor.py:158` (replace `cls(model_path=...)` with `ProbabilityCalibrator()` + `.load()` call) | 1 line + test |
| 8.2 | Fix fee EV gate to charge both legs | `src/core/fee_calculator.py:92` (`- 2*fee_per` instead of `- fee_per`) | 1 line + test |
| 8.3 | Fix cycle rollover to close via `_close_position` | `scripts/run_dashboard.py:791` (loop over positions, call `exchange._close_position(p, p["entry_price"], reason="CYCLE_RESET")`) | ~20 LoC + test |
| 8.4 | Fix `cycle_trades` counter to sum wins+losses, not signals | `scripts/run_dashboard.py:676-683` | ~5 LoC |
| 8.5 | Add `positions.json` / `exchange_state.json` persistence | `src/core/matching_engine.py` (serialize on every close, load on startup) | ~80 LoC + test |
| 8.6 | Disable Late Sniper in bot registry | Bot registry; comment out | 1 line |
| 8.7 | Disable Empirical Edge NO-side entries | `src/strategies/crypto_strategy.py` — NO-branch early-return with metric counter; keep YES-side live | ~10 LoC |
| 8.8 | Prune confirmed-dead strategies | See "Dead or unused code" in §2 — delete classes entirely | ~500 LoC removed |
| 8.9 | Delete stale `sig.stop_loss` assignments for binary 15m | `ml_btc_15m.py:138,174` | ~4 LoC |
| 8.10 | Commit, push, deploy to VM, monitor first 3 cycles | n/a | 30 min |

#### Acceptance criteria

- A new test file `tests/test_sprint8_bug_fixes.py` with:
  - `test_calibrator_loads_from_joblib_path()` — creates a fake calibrator file, asserts ModelPredictor loads and invokes it.
  - `test_ev_after_fees_charges_both_legs()` — asserts a round-trip at p=0.5, q=100 subtracts `2 × fee_per` from EV.
  - `test_cycle_rollover_closes_positions_via_callback()` — opens 3 positions, triggers rollover, asserts `_on_trade_close` fired 3× and `strategy_win_rates.json` was updated.
  - `test_exchange_state_persists_across_restart()` — opens positions, serializes, re-instantiates, asserts state matches.
- On VM after deploy: 3 consecutive cycles show `strategy_win_rates.json` non-empty with real win rates, `trades = wins + losses`, and `pnl != 0` unless genuinely break-even.
- `trade_journal.jsonl` gains valid `model_confidence` values that now correlate (positively) with outcome (manually spot-check 50 recent trades post-deploy).

#### Risks

- Writing calibrator bug-fix may expose that the trained calibrator itself is stale — if so, Sprint 9 task 9.1 gets pulled forward.
- Disabling Empirical Edge NO-side may starve the bot of trades entirely during thin hours; Sprint 10 will address with ML BTC 15m concentration. Interim: monitor and re-enable YES-side lightly if trade count collapses.

---

### Sprint 9: Recalibrate What's Left (3–5 days)

**Outcome:** ML training pipeline is free of label leakage and class-imbalance bias. Kelly sizing no longer mixes orthogonal signals. Stop-loss slippage root-caused.

#### Scope

| # | Task | Where | Effort |
|---|---|---|---|
| 9.1 | Split walk-forward CV by `symbol`, not row | `scripts/train_from_csv.py:573` (groupby symbol, time-sort symbols, split 60/20/20 by symbol list) | ~30 LoC + test |
| 9.2 | Add `scale_pos_weight = N_neg / N_pos` to XGBoost training | `scripts/train_from_csv.py:685` | 2 LoC + test |
| 9.3 | Decouple Kelly `confidence` from `entry_price` for rule-based strategies | `src/core/risk_manager.py:265` — require strategies to pass `model_probability` explicitly OR skip the confidence blend entirely when strategy is not ML-based | ~40 LoC + test |
| 9.4 | Make `tanh_scale` adaptive to rolling 20-min BTC stddev | `src/ml/predictor.py:640` — replace fixed 1000 with `5 × rolling_std_20min(spot)` | ~15 LoC + test |
| 9.5 | **Investigate stop-loss slippage** — why are stops filling 8-10¢ below threshold? | Instrument `_close_position` with stop-threshold vs fill-price logging; correlate with Kalshi orderbook depth snapshots at fill moment | ~1 day investigation, then ~50 LoC fix |
| 9.6 | Retrain model, deploy, verify val AUC + calibration slope | Run training, ship | 2 hrs |
| 9.7 | Investigate weather-ML "index 13 out of bounds" warning | `src/strategies/ml_weather.py` — grep for index 13, reproduce | 2 hrs |

#### Acceptance criteria

- `test_purged_split_excludes_symbol_leakage` — asserts no symbol appears in both train and val set.
- `test_scale_pos_weight_applied` — asserts XGBoost param is set to the right ratio.
- Validation AUC ≥ 0.80 AND Brier score ≤ 0.20 on held-out symbols.
- Calibration slope in bucket [0.6, 0.9] is within [0.85, 1.15] (measured weekly).
- Stop-loss fills: median (fill_price - configured_threshold) ≤ 2¢ post-fix, down from -8¢.

#### Known unknowns

- The stop-loss slippage may be a Kalshi real-orderbook artifact during 15m expiry minutes, in which case the fix is to either (a) skip stops in the last 90 seconds of expiry, or (b) use tight-limit stops instead of market stops. Decision after instrumentation.

---

### Sprint 10: Concentrate Capital on the Winner (1 week)

**Outcome:** ML BTC 15m runs in its proven high-edge regime; all other strategies reduced to supporting roles. Expected paper PnL: transition from ~-$0/day to consistent +$20–$50/day (pre-multi-crypto).

#### Scope

| # | Task | Details | Effort |
|---|---|---|---|
| 10.1 | Per-strategy time-of-day gate | YAML config: for each strategy × day-of-week × ET-hour, assign an enable/disable flag derived from trade journal stats | ~80 LoC + test |
| 10.2 | ML BTC 15m: default-on only 20:00-02:00 ET; elsewhere require higher EV threshold (+3¢ over baseline) | Per-strategy config | ~20 LoC |
| 10.3 | Tighten strike-proximity filter to \|strike-spot\| ≤ 150 for ML BTC 15m | `ml_btc_15m.py` — early-reject signals beyond 150 | ~15 LoC + test |
| 10.4 | Prefer TAKE_PROFIT (trailing) over PROFIT_TARGET (fixed) where both are offered | Strategy configs | ~10 LoC |
| 10.5 | Raise min-bankroll-impact threshold: skip signals with Kelly-sized quantity < 5 contracts | `risk_manager.py` | ~5 LoC |
| 10.6 | Post-deploy: 72-hour monitoring window, paper only | n/a | 3 days passive |

#### Acceptance criteria

- ML BTC 15m trades ≥ 70% of all trades in a sampled 24h window.
- Overnight (20:00-02:00 ET) trades ≥ 55% of ML BTC 15m's daily total.
- No ML BTC 15m trade has |strike-spot| > 150.
- Cumulative PnL over 72h window ≥ +$0 (first positive-72h milestone).

---

### Sprint 11: Multi-Crypto 15m Expansion (2 weeks)

**Outcome:** the bot family supports every 15m crypto market Kalshi offers, each independently configured and tradeable; per-asset models trained and validated.

#### Scope

| # | Task | Details | Effort |
|---|---|---|---|
| 11.1 | Market-discovery script | `scripts/discover_crypto_15m_markets.py` calls Kalshi `/series` endpoint, filters for `^KX[A-Z]+15M$`, checks `active` / `initialized` market counts, writes `config/crypto_15m_markets.yaml` | ~100 LoC |
| 11.2 | Add Coinbase symbol validation helper | `scripts/verify_coinbase_products.py` — confirm each Kalshi crypto has a matching Coinbase `{ASSET}-USD` product | ~40 LoC |
| 11.3 | Rename + parameterize bot: `BTC15mBot` → `Crypto15mBot` | `src/bots/crypto_15m_bot.py`, takes `CryptoMarketConfig`. Keep `BTC15mBot` as thin shim alias for back-compat during transition | ~150 LoC |
| 11.4 | Parameterize `_resolve_smart_ticker` call and log messages | Pull series name from config, not hardcoded | ~20 LoC |
| 11.5 | Generalize matching-engine binary-event detection to regex `^KX[A-Z]+15M-` | `matching_engine.py:468, 538` | ~10 LoC + test |
| 11.6 | Generalize dashboard series-prefix grouping | `dashboard.py:264, 282` | ~10 LoC |
| 11.7 | Rename `MLBtc15mStrategy` → `MLCrypto15mStrategy`; key model file by asset | Strategy reads `asset = parse_asset(ticker)` and loads `data/models/{asset}_15m_xgboost_latest.joblib` | ~80 LoC + test |
| 11.8 | Rename training script to `train_crypto_15m.py` with `--asset` flag; produce per-asset models + calibrators | `scripts/train_crypto_15m.py` | ~60 LoC |
| 11.9 | Add harvester entries for each discovered series | `src/data/harvester.py:48` | ~10 LoC |
| 11.10 | Multi-bot registration in orchestrator | `scripts/run_dashboard.py` reads `config/crypto_15m_markets.yaml`, instantiates one `Crypto15mBot` per enabled market | ~40 LoC |
| 11.11 | Per-asset Kelly exposure tracking + 60% combined-crypto soft cap | `risk_manager.py` | ~30 LoC + test |
| 11.12 | Add `asset` field to trade journal | `src/bots/mixins.py` signal processing | ~5 LoC |
| 11.13 | Bootstrap-training data collection phase | Run all new bots in data-collection mode (no trading) for 5 days per new asset to accumulate ≥ 500 observed contracts before enabling trades | 5 days passive |
| 11.14 | Per-asset tanh-scale and strike-proximity defaults (configurable) | Per-asset tuning based on first week of data | ~30 LoC |

#### Acceptance criteria

- `config/crypto_15m_markets.yaml` exists with ≥ 2 markets including BTC. Each market has discovered `active|initialized` markets verified.
- `tests/test_crypto_15m_bot_parameterization.py` — asserts bot instances for BTC and ETH coexist without cross-talk.
- Matching engine + dashboard tests updated to cover the generalized series pattern.
- For each newly enabled market, val AUC on held-out data ≥ 0.72 (slightly lower bar than BTC since less history) AND calibration slope in [0.80, 1.20].
- After bootstrap, at least 2 non-BTC markets are live and trading, each generating ≥ 10 trades/day.
- No market exceeds its per-asset exposure cap; combined crypto exposure never > 60%.

#### Risks & mitigations

| Risk | Mitigation |
|---|---|
| Kalshi may not list 15m contracts for ETH/SOL/etc. | Sprint 11.1 discovery script is the first sub-step; scope collapses to whatever is actually listed |
| Per-asset volatility differs wildly (DOGE swings 20% intraday; BTC 2%) | Per-asset `tanh_scale` and strike-proximity filter; data-driven via Sprint 11.14 |
| Training data per new asset is sparse | Bootstrap phase Sprint 11.13 before enabling trades; require ≥ 500 labeled contracts before live-enable |
| Correlation among crypto markets inflates effective exposure | AD-4: soft 60% combined-crypto cap in `risk_manager.py` |
| Coinbase rate limits if we subscribe to 5+ products | Use existing REST polling with 1-req/sec-per-product; consider WebSocket (already scaffolded in `coinbase_ws.py`) if needed |

---

### Sprint 12: Build Flagship "Spot-Lead" Strategy (2 weeks)

**Outcome:** the BTC-spot-leads-hourly-contract edge discovered by Agent 5 (+5¢/contract, 67% WR, N=1,607) is productionized as a standalone strategy — first on BTC, then extended to other cryptos via Sprint 11 infrastructure.

#### Hypothesis (from Agent 5's analysis of 54h of saved_runs)

> For BTC spot moves > 5 bp in 40 seconds, buying the nearest ATM hourly contract in the direction of the move and holding 30 seconds yields mean PnL +5.04¢/contract, win rate 67.3%, t-stat 18.1. Edge scales with move size and persists to a 300-second horizon.

#### Scope

| # | Task | Details | Effort |
|---|---|---|---|
| 12.1 | Replicate Agent 5's backtest on a fresh 2-week VM pull | Pull `saved_runs/` fresh; run same regression; verify edge survives; quantify edge after realistic bid-ask crossing cost (~1¢/leg) | 2 days |
| 12.2 | Implement `SpotLeadStrategy` in `src/strategies/spot_lead.py` | Subscribes to Coinbase tick feed; on 40s \|ret\| > 5bp triggers, selects best ATM hourly contract; emits signal with TP at +15¢ / time-stop at 90s | ~200 LoC + test |
| 12.3 | Bootstrap shadow mode (log-only) for 3 days | Strategy runs, logs hypothetical trades with same entry/exit logic, does NOT execute | 3 days passive |
| 12.4 | Analyze shadow trades vs live BTC spot data | Confirm live edge matches backtest within 30% | 1 day |
| 12.5 | Enable live paper trading with small size cap ($100 max exposure) | 3-day first-run | 3 days monitored |
| 12.6 | Scale-up plan if edge holds | Graduate to normal Kelly sizing | 1 day |
| 12.7 | Extension to multi-crypto (after Sprint 11) | Apply same logic to ETH/SOL via per-asset Coinbase feeds | ~50 LoC |

#### Acceptance criteria

- Replicated edge ≥ +2¢/contract (net of fees + crossing cost), WR ≥ 60% on fresh 2-week data.
- Shadow-mode trades vs live movement correlate r ≥ 0.6 with backtest-predicted PnL.
- First 3 days of paper trading have cumulative PnL ≥ 0.
- Max drawdown on this strategy alone ≤ $100 during bootstrap phase.

#### Risks

- Kalshi orderbook latency: the contract may already be repriced by the time our signal fires. Mitigation: measure end-to-end latency (Coinbase tick → Kalshi order placed) in shadow mode; abort if > 500 ms median.
- Adverse selection: if sophisticated counterparties see our orders, our fills get picked off. Mitigation: Agent 4 rec #10 — post-fill-reversion monitor, throttle if reversion > 2¢ within 10s of fill.
- Edge may be regime-specific (Agent 5 caveat: 54h window only). Mitigation: 12.1 replication on 2 weeks; if edge evaporates, demote the strategy to research-only.

---

### Sprint 13: Quant Infrastructure Upgrade (3–4 weeks, ongoing)

**Outcome:** system upgraded from ad-hoc heuristics to research-backed practices. Published literature replaces lore.

Parallelizable; order by impact/effort.

| # | Technique | Reference | Deliverable |
|---|---|---|---|
| 13.1 | Purged + embargoed walk-forward CV | López de Prado 2018 Ch. 7 | Replace `walk_forward_split` with `PurgedKFold(n=5, pct_embargo=0.02, label_duration=15min)` |
| 13.2 | Isotonic calibration + Brier-score reliability dashboard | Niculescu-Mizil & Caruana 2005 | `src/ml/calibration.py` adds `IsotonicCalibrator`; weekly reliability plot written to `data/reports/calibration_{asset}_{week}.png` |
| 13.3 | Drop any SMOTE-style resampling; use `scale_pos_weight` + threshold tuning | Molnar 2024 | Verify training script; grep for `SMOTE`, `imblearn`, etc. — remove if present |
| 13.4 | Deflated Sharpe Ratio + MinTRL capital gate | Bailey & López de Prado 2014 | `src/analytics/dsr.py` with daily rollup; promotion gate = DSR p < 0.05 AND days ≥ MinTRL(SR_target=1.0) |
| 13.5 | Drawdown-constrained Kelly | Busseti, Ryu, Boyd 2016 | `src/core/kelly_dd.py` — daily convex solve with 20% max-DD constraint at 95%-CL; shrink factor multiplied into quarter-Kelly |
| 13.6 | Climatology prior + NOAA GEFS ensemble for weather bot | NOAA GEFS | `src/data/gefs_provider.py` — pull AWS Open Data; blend with NWS; weather strategy gains ensemble P(T>strike) |
| 13.7 | Order-book-imbalance (OBI) feature | Cont, Kukanov, Stoikov 2014 | Add `feat_obi_top5` to ML 15m feature set; retrain |
| 13.8 | Adverse-selection monitor (post-fill reversion) | Agent 4 quant research | `src/analytics/adverse_selection.py` — rolling 1-hour reversion score per strategy; auto-throttle if score > threshold |
| 13.9 | 2-state Gaussian HMM regime gate | Standard HMM literature | `src/analytics/regime.py` fits 2-state HMM on 5-min BTC log-returns daily; feed state into strategy gate |

Each item is self-contained; Sprint 13 is a "backlog" that engineers pull from based on what the data shows needs attention. Target: complete 4-6 of these in the first month post-Sprint-12.

---

### Sprint 14: Ops Hardening (3–5 days)

**Outcome:** system survives a VM reboot, a Hermes crash, and a dashboard process crash without manual intervention.

| # | Task | Details | Effort |
|---|---|---|---|
| 14.1 | Install systemd unit `money-printer.service` with `Restart=always` | `/etc/systemd/system/money-printer.service`; runs tmux attach | 1 hr + test |
| 14.2 | Install systemd unit `hermes.service` with `Restart=always` | Similar | 1 hr |
| 14.3 | Install the 5-min watchdog as OS `crontab -l` entry AND keep Hermes copy | Memory documented deploy but it's absent on the VM; ensure fallback at OS level | 1 hr |
| 14.4 | Persist SimulatedExchange state to `data/exchange_state.json` on every close | Delivered in 8.5; verified in 14 | already done |
| 14.5 | Resume open positions on startup via Kalshi ticker resolution | On startup, load `exchange_state.json`, re-subscribe to each open ticker, restart tracking | ~80 LoC + test |
| 14.6 | Alert on consecutive cycle restart within 10 min | Discord webhook alert with tail of crash log | ~30 LoC |
| 14.7 | Rotate `logs/_archive/` — retain last 30 dirs, compress older | cron or systemd timer | 30 min |
| 14.8 | Add `scripts/diagnose.sh` — one-command snapshot of systemd state, recent logs, open positions, disk/memory | Ops ergonomics | 1 hr |

#### Acceptance criteria

- `systemctl is-active money-printer` and `systemctl is-active hermes` both return `active`.
- Kill `money-printer` process manually → auto-restart within 10s, open positions load from disk.
- Discord channel receives a restart-alert within 30s of crash.

---

## 5. Success Criteria & Capital-Promotion Gate

The single external signal of success is: **would I give this system real money?** The gate is layered.

### Tier-0 gate (end of Sprint 8)

- `strategy_win_rates.json` is non-empty and updates on every cycle rollover.
- `trade_journal.jsonl` shows `trades = wins + losses` per cycle (no phantom-trade cycles).
- EV gate rejects at least one trade per day that it would have accepted pre-fix, confirming the fee-doubling takes effect.
- Model confidence is positively (not negatively) correlated with win-rate over 50 sampled trades.

### Tier-1 gate (end of Sprint 10)

- 72 consecutive hours of **non-negative PnL** on the $3,000 sim account (first-ever occurrence).
- ML BTC 15m is responsible for ≥ 70% of daily trades; Late Sniper is disabled.
- No cycle shows `pnl = 0, trades > 20` (phantom-trade pattern gone).

### Tier-2 gate (end of Sprint 11)

- At least 2 non-BTC crypto markets live; each generates ≥ 10 trades/day.
- Combined crypto exposure stays below 60% of portfolio at all times.
- Weekly Sharpe on each enabled market ≥ 0.5 (trade-level, not annualized).

### Tier-3 gate (end of Sprint 12)

- Spot-lead strategy in live paper trading with cumulative PnL > 0 over 3 days.
- System-wide 7-day rolling Sharpe ≥ 1.0.

### Final capital-promotion gate (supersedes the naïve 72h rule)

**Do not deploy real capital until all of the following hold simultaneously:**

1. **Deflated Sharpe Ratio p-value < 0.05** over at least MinTRL(SR_target=1.0) days of paper trading — typically 15-25 days depending on return distribution (computed by Sprint 13.4).
2. **Max drawdown over rolling 7-day window < 10% of bankroll** (currently $3,000 sim).
3. **Calibration slope on ML models in [0.90, 1.10]** for 2 consecutive weeks.
4. **Zero unplanned process restarts** for 7 consecutive days.
5. **72-hour positive PnL on a down-sized $500 sim account** (from memory's capital-gate doc).

Only after all five → begin a staged real-capital test at $100.

---

## 6. Risk Register

| Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|
| Sprint 8 bug fixes regress existing behavior | M | H | Exhaustive tests per fix; 3-cycle VM monitoring before declaring done | Sprint 8 engineer |
| Stop-loss slippage root cause is an unfixable Kalshi orderbook artifact | M | H | Sprint 9.5 fallback: skip stops in final 90s of 15m expiry | Sprint 9 |
| Kalshi doesn't list 15m markets for non-BTC cryptos | M | M | Sprint 11.1 discovery is first step; scope adapts to reality | Sprint 11 |
| Per-asset data is too sparse for model training | H | M | Bootstrap mode Sprint 11.13; require ≥ 500 labeled contracts | Sprint 11 |
| Spot-lead edge is regime-specific and evaporates | M | L | Sprint 12.1 replication on fresh data; demote if edge < 2¢/contract | Sprint 12 |
| Calibrator file on disk is stale / wrong-schema | M | M | Sprint 8.1 test validates load path; Sprint 13.2 retrains on isotonic | Sprint 8/13 |
| Multi-crypto bot proliferation creates orchestration bugs | L | M | Single `Crypto15mBot` class (AD-1) instantiated per market, not separate classes | Sprint 11 |
| Combined crypto exposure inflates effective risk (BTC-ETH corr > 0.7) | H | M | AD-4: 60% combined-crypto soft cap in risk manager | Sprint 11 |
| VM watchdog + Hermes loop interact badly after systemd install | L | M | Staged rollout Sprint 14; test Hermes/money-printer restart independently first | Sprint 14 |
| Training label-leakage fix reduces AUC substantially | M | M | Expected: true AUC is lower than contaminated AUC. Accept ≥ 0.72 as pass; focus on calibration slope | Sprint 9 |
| User deploys real capital before gate is met | L | Catastrophic | Final-gate checklist in §5; code-level safeguard: real-money API flag requires explicit `REAL_CAPITAL_GATE_UNLOCKED=true` env var not set until all 5 conditions pass | all sprints |

---

## 7. Testing Strategy

### Unit test coverage goals

- Sprint 8: +4 new tests for the three bugs + persistence.
- Sprint 9: +3 new tests for split purity, class weight, adaptive tanh.
- Sprint 11: +6 new tests for parameterization + per-asset isolation.
- Sprint 12: +2 tests for spot-lead signal generation + time-stop.
- Sprint 13/14: +1 test per technique added.

### Integration / backtest coverage

- All sprints must pass `pytest tests/` cleanly before deploy.
- Sprint 11 and later must pass a 24h dry-run on a locally-cached saved-run before VM deploy.
- Each sprint ends with a **regression cycle**: run the existing live dashboard on the VM for ≥ 3 cycles and verify no new error pattern in `logs/` vs the pre-deploy baseline.

### Instrumentation

- Add a "canary" trade/cycle every N cycles with known-good inputs; assert output matches. Detects silent regressions.
- Per-sprint, log a one-liner summary (`[SPRINT 8] calibrator loaded | strat_win_rates path = <path> | n_keys = <n>`) at startup so deploy-verification is one `grep`.

---

## 8. Rollout & Deployment Protocol

Per `feedback_always_commit_and_deploy.md` memory: after *any* code changes, commit + push + deploy to VM immediately.

### Standard deploy sequence (per sprint end)

1. Run `pytest tests/` locally — must pass.
2. Commit with Co-Authored-By line.
3. Push to `refactor_v0.1` branch.
4. SSH to VM; `cd money_printer && git pull`.
5. Kill tmux session `money`; start new session via `run_web_dashboard.py --auto-cycle --sim-balance 3000`.
6. `tail -F logs/session_*.log` for 5 minutes; confirm no errors.
7. Check `trade_journal.jsonl` and `training_state.json` update within 2 cycles.
8. Post deploy-confirmation to Discord webhook.

### Rollback triggers

- Any `ERROR` or `CRITICAL` pattern in the first cycle post-deploy not present in baseline.
- Cycle fails to close (`training_state.json` doesn't update within 1.5× expected cycle duration).
- Watchdog fires > 2× in the first hour.

### Rollback procedure

1. `git reset --hard HEAD~1` on VM.
2. Restart tmux session.
3. Investigate locally; re-plan; re-deploy.

---

## 9. Dependencies

### Runtime

- Python 3.14 (venv at `/home/hoyer/money_printer/venv/` on VM)
- XGBoost, scikit-learn, pandas, numpy, scipy, statsmodels
- Kalshi API (RSA-signed auth, private key at `KALSHI_PRIVATE_KEY_PATH`)
- Coinbase public API
- NWS public API
- NOAA GEFS (Sprint 13.6) — AWS S3 open-data bucket
- Discord webhook (`DISCORD_WEBHOOK_URL` in `~/.hermes/.env` on VM)

### Development / infrastructure

- `gcloud` CLI for VM access (Windows — use `--ssh-flag=` not `--`)
- `pytest` for test suite
- `cvxpy` (new for Sprint 13.5 drawdown-Kelly)
- `mlfinlab` (optional drop-in for Sprint 13.1 purged CV)

### Human

- Engineer with Python + pandas + basic ML to execute Sprints 8-12.
- Familiarity with Kalshi API conventions (V2 `_dollars` fields, series/events/tickers, market statuses) — see CLAUDE.md.

---

## Appendix A — Bug Autopsies

### A.1 Calibrator never loads
**File:** `src/ml/predictor.py:158`
**Symptom:** `self._calibrator` is always `None`; the branch `if self._calibrator is not None` at line 495 is dead.
**Root cause:** `cls(model_path=str(model_file))` — `ProbabilityCalibrator.__init__(self, method='platt')` does not accept `model_path`. TypeError is swallowed by the generic `except Exception` at line 183; `_model_status` is set but no one reads it.
**Fix:** `instance = ProbabilityCalibrator(); instance.load(str(model_file))`.
**Symptom in data (Agent 2):** confidence [0.8, 0.9) → 39% WR; confidence [0.5, 0.6) → 73% WR. Inversion is diagnostic of feeding raw uncalibrated XGBoost scores into a system that expects calibrated probabilities.

### A.2 Fee EV gate single-leg
**File:** `src/core/fee_calculator.py:92`
**Symptom:** EV gate admits trades with EV as low as $0.01/contract that actually lose $0.01/contract after round-trip.
**Root cause:** `return probability - price - fee_per` — subtracts one leg of fees. Binary contracts pay fees on entry AND exit (settlement or close). At p=0.5 with maker fees, that's $0.01/contract/leg = $0.02/contract round-trip.
**Fix:** `return probability - price - 2 * fee_per`.
**Financial impact:** Agent 2 estimates ~16% of gross PnL magnitude goes to fees; half of that is not being priced into EV decisions.

### A.3 Cycle rollover phantom trades
**File:** `scripts/run_dashboard.py:791` + `src/core/matching_engine.py` (persistence gap)
**Symptom:** `training_state.json` cycles 60+ (post 2026-04-13 mid-day) show `trades=N, wins=0, losses=0, pnl=0` despite N up to 1940. `strategy_win_rates.json` = `{}` (2 bytes) on VM.
**Root causes (two compounding):**
1. `run_dashboard.py:791` calls `self.risk_manager.exchange.positions.clear()` at cycle end without invoking `_close_position`. The `_on_trade_close` callback (which writes to `strategy_win_rates.json` and the journal) never fires.
2. `SimulatedExchange` has no disk persistence; every VM restart (watchdog fires frequently — 157 archive dirs in `logs/_archive/`) nukes open positions entirely.
**Fix:** Loop `for p in list(exchange.positions.values()): exchange._close_position(p, p["entry_price"], reason="CYCLE_RESET")`. Add `exchange_state.json` serialization on every close.

### A.4 Stop-loss slippage (Sprint 9 investigation)
**Symptom (Agent 2):** 980 STOP_LOSS_PRICE fills filled > 10¢ below configured threshold. Total leak on these 980 trades alone: -$51,825.
**Hypotheses:**
1. Stale `last_market_price` in simulator during expiry-minute price gapping.
2. `_close_position` crosses spread aggressively when orderbook is thin in final minutes.
3. Missing slippage model for binary events near expiry.
**Investigation plan:** instrument `_close_position` for stop fills, correlate with Kalshi orderbook depth at fill timestamp, decide between (a) skip stops in final 90s, (b) use limit-stop instead of market-stop, (c) fix simulator fidelity.

---

## Appendix B — Literature References

From Agent 4's quant-research report (full URLs in audit artifacts):

- Bailey & López de Prado (2014) — *The Deflated Sharpe Ratio* — capital-gate math.
- Bailey & López de Prado (2012) — *Sharpe Ratio Efficient Frontier* — MinTRL.
- Busseti, Ryu, Boyd (2016) — *Risk-Constrained Kelly Gambling* — DD-capped sizing.
- López de Prado (2018) — *Advances in Financial Machine Learning* Ch. 7, 12, 14 — purged CV, CPCV, backtest statistics.
- MacLean, Thorp, Ziemba (2011) — *The Kelly Capital Growth Investment Criterion* — definitive Kelly reference.
- Niculescu-Mizil & Caruana (ICML 2005) — *Predicting Good Probabilities with Supervised Learning* — calibration foundations.
- Cont, Kukanov, Stoikov (2014) — order-book imbalance as short-horizon predictor.
- Molnar (2024) — *Don't Fix Your Imbalanced Data* — why SMOTE breaks calibration.
- NOAA GEFS — NCEI product page + AWS Open Data Registry.
- Unravelling the Probabilistic Forest (arXiv 2508.03474, 2025) — prediction-market arbitrage empirics.

Internal audit artifacts (for full evidence):
- Agent 1 report: VM state audit
- Agent 2 report: trade-journal statistics + artifacts in `C:/tmp/audit_agent2_*.csv`
- Agent 3 report: code audit with file:line citations
- Agent 4 report: quant research 10-recommendation table
- Agent 5 report: hidden-trend mining + spot-lead-contract flagship finding
