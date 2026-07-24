# PRD — Money Printer Strategic Pivot (pivot_weather_v1)

**Status:** Draft v1.0 — 2026-07-24
**Driving document:** `review_2026_07_24/STATE_OF_THE_PRINTER_2026_07_24.md` (comprehensive review, 22-agent verified)
**Branch:** `pivot_weather_v1`
**Execution model:** each numbered phase below is executed with `/sprint Phase N` and red-teamed against its exit criteria before the next phase begins.

---

## 1. Overview & Vision

Money Printer pivots from short-horizon crypto binaries (proven structurally unwinnable: fee+spread floor +2.25–4.5pt vs a measured +2.1–2.8pt signal ceiling, a 0.999 price-echo model, and a real-account loss record) to **information-edge trading on slow-settlement Kalshi markets**, with **daily-high weather markets (KXHIGH\*) as the flagship** and **AAA gas-price convergence (KXAAAGASM/W) as the second engine**.

The vision: a small, observable, honest system that (a) computes a genuine probability distribution the market does not already price — an ensemble-forecast distribution over a city's daily high, or a lag-model projection of a published settlement index — (b) trades it maker-first in fee-free books against casual counterparties, (c) holds to settlement, and (d) accounts for itself exclusively in settlement-true terms. Success is defined statistically, not aspirationally: the first milestone in this project's history of a **statistically defensible positive expectancy**, then compounding from it.

Everything in this PRD is subordinate to one lesson from four review cycles: **the system must be incapable of lying to itself.** Settlement truth is recorded from the authoritative external source, fills are simulated pessimistically, every signal and rejection is observable, and no phase advances on unverifiable evidence.

## 2. Target User & Context

- **User:** the project owner (single operator) — an expert engineer, comfortable with Python, GCP, and statistical reasoning; time-constrained (evenings/weekends); interacts via the web dashboard, Discord notifications, and log/journal review sessions.
- **Operator context:** the system runs unattended 24/7 on a GCE VM; the operator checks in daily-to-weekly and must be able to answer "what did it do and why" in under five minutes from the dashboard and logs alone.
- **Agent context:** development is executed by orchestrated agent teams via `/sprint`; every requirement here must be verifiable by a red-team without the author's help.

## 3. Goals & Non-Goals

### Goals
1. Remove all negative-expectancy surface area: 15m/hourly crypto trading, the ML retrain pipeline, and ~8–9k LOC of dead strategy code.
2. Fix the ops defects that have blinded three consecutive reviews: production log deletion, unlogged signals/rejections, watchdog false alarms, retrain freeze.
3. Rebuild weather trading correctly: API-derived bracket semantics, settlement-station data, hold-to-settlement lifecycle, ensemble-based probability distributions, per-city calibration on ≥60 days of ground truth, maker-first execution.
4. Add the AAA gas convergence bot as a second, uncorrelated engine.
5. Replace the capital gate with a statistically valid promotion mechanism (settlement-true, realistic fills, minimum sample, pre-registered threshold).
6. Keep the VM harvesting weather/gas market data continuously from Phase 0 onward, so calibration and backtesting data accumulate while code is built.

### Non-Goals
- **No real-capital trading in this PRD's scope.** The output of Phase 5 is a *passed or failed gate*; deploying real money is a separate, explicit user decision afterward.
- No revival of 15m/hourly crypto strategies or the price-echo ML pipeline. The pre-registered KXBTCD "always-NO" forward study is optional research, not a deliverable.
- No sports, politics, mentions, or in-play markets.
- No sub-second/websocket latency infrastructure.
- No new ML models until a calibrated analytical baseline exists to beat (per the beat-the-trivial-baseline rule).
- No dashboard redesign beyond the observability requirements in §6.

## 4. Key Assumptions & Decisions

| # | Assumption / Decision | Rationale |
|---|---|---|
| A1 | The GCE VM stays (possibly downsized to e2-small after Phase 0) and becomes a weather/gas data harvester immediately | Calibration data accumulates during development; ~$50/mo is acceptable and reviewed at Phase 3 |
| A2 | Crypto bot/strategy code is **deleted**, not flagged off (except `latency_arb.py`, retained mothballed and unregistered) | Dead flags rot and got re-investigated as mysteries three times; git preserves history |
| A3 | Core abstractions are kept: `DataProvider`/`Strategy`/`Bot` interfaces, `RiskManager`, `SimulatedExchange`, dashboard, Discord | Review confirmed they are strategy-agnostic and sound; the defects are in strategies, config, and fill/lifecycle policy |
| A4 | Initial city set: NY (KNYC), Chicago (KMDW), LAX (KLAX), Miami (KMIA); expansion set: Denver, Philadelphia, Austin, Houston after calibration | The four have existing plumbing and verified settlement stations; expansion only after the pipeline is proven |
| A5 | Forecast stack: NWS point forecast + GEFS ensemble (NOMADS, free) as primary; ECMWF open data as optional second source | Free, documented, sufficient members (31+) for a distribution; documented operators use exactly this |
| A6 | Ground truth: NWS CLI product via the IEM archive (`mesonet.agron.iastate.edu/json/cli.py`), demonstrated working in the review | It is the settlement source Kalshi cites |
| A7 | Trading remains simulation-only throughout; the demo/prod split and read-only key are unchanged | Non-goal above; gate first |
| A8 | Paper-trading sizing uses fixed small quantities with per-symbol re-entry throttles, not Kelly, until Phase 5 | Kelly with poisoned/thin win-rate history was an execution kill-switch; win-rate state resets with the pivot |
| A9 | Phases 0–2 are sequential; Phase 4 (gas) may run in parallel with Phase 3 once Phase 2's exit criteria pass | Gas shares only the provider/engine interfaces |

## 5. Functional Requirements

### FR-0 Teardown & ops hardening (Phase 0)
- FR-0.1 Remove from the bot registry and delete: `btc_hourly` bot + `ml_btc_hourly` strategy, `sol_15m`/`doge_15m`/`xrp_15m`/`eth_15m`/`btc_15m` bots, `ml_btc_15m.py`, `crypto_strategy.py`, `longshot_fader_v2.py`, `cross_spread_arb.py`, `counter_trade.py` (after confirming its analyzer output is unused). Retain `latency_arb.py` unregistered with a header comment stating its mothball status and revival preconditions.
- FR-0.2 Remove the ML retrain pipeline from the runtime (periodic, cycle-boundary, and startup retrains); `src/ml/` training entry points remain invocable offline only. No retrain may run on the tick thread.
- FR-0.3 Fix production logging: the active `money_printer_*.log` is whitelisted in all archive sweeps (startup + cycle); `run_web_dashboard.py` configures handlers so module-level (`logging.getLogger(__name__)`) INFO logs from strategies, bots, mixins, and providers reach the file.
- FR-0.4 Every emitted signal and every rejection (risk check, sizing, EV gate, throttle) is logged at INFO with strategy, symbol, side, price, quantity, and a reason code. Cycle summaries report per-bot status: `TRADING` / `FEED-ONLY` / `DISABLED`.
- FR-0.5 Watchdog: cycle rollover creates the new session log before archiving the old; staleness margin derives from actual max quiet window + buffer. Target false-alarm rate: <1/day.
- FR-0.6 Exchange-state hygiene: positions with quantity 0 are removed on final partial close and by the expiration sweep; the stuck id-1582 shell is purged; poisoned `strategy_win_rates.json` entries are archived and reset; win-rate history becomes recency-windowed (last 50 closed trades per strategy).
- FR-0.7 The VM harvester records, for every tracked weather/gas market on every tick: best bid, best ask, last price, volume, and (hourly) top-3 orderbook levels — persisted in the data CSVs (current recording keeps only a single best price, which blocked spread analysis in the review).

### FR-1 Weather market foundation (Phase 1)
- FR-1.1 `KalshiProvider` surfaces `floor_strike`, `cap_strike`, and `strike_type` for every market in `MarketData.extra`. All bracket logic derives from these fields; **no code path may infer contract direction from ticker suffix letters.**
- FR-1.2 A single shared payoff module computes P(YES) and settlement outcomes for all three contract types: `between` (floor ≤ high ≤ cap), `greater` (high ≥ floor+1), `less` (high ≤ cap−1) — matching live-API semantics verified in the review (KXHIGHNY-26JUL25-B86.5 = "86° to 87°"; T87 = "88° or above"; T80 = "79° or below"). The sim settlement path uses this same module.
- FR-1.3 Settlement recorder: a daily job fetches each tracked city's CLI high (IEM primary, NWS CLI product fallback) and the Kalshi market results, writing both to the settlement cache; a reconcile report flags any sim-vs-truth mismatch (same pattern as the crypto settlement reconcile).
- FR-1.4 Observations come from the settlement stations (KNYC, KMDW, KLAX, KMIA), including the running daily max; JFK/ORD feeds are removed or explicitly labeled non-settlement.
- FR-1.5 Weather positions are exempt from `TIME_LIMIT_MIN` and cycle-reset liquidation; they persist across cycles and settle via FR-1.2. No stop-losses on binary weather positions.
- FR-1.6 Historical backfill: CLI daily highs backfilled ≥180 days for all tracked cities; archived model guidance (MOS/NBM via IEM) backfilled where available to seed forecast-error calibration.

### FR-2 Forecast engine & calibration (Phase 2)
- FR-2.1 Ensemble provider: fetches GEFS members (NOMADS) for each city's daily high with graceful degradation to NWS point forecast + historical error distribution when the ensemble is unavailable; results cached; failures abort signal generation (never a silent default, per abort-on-missing-critical-input).
- FR-2.2 Calibration pipeline: per city, computes forecast bias and error σ (by lead time and by month/season where sample permits) against CLI truth on ≥60 paired days; outputs a versioned calibration file consumed by the strategy; recomputed nightly.
- FR-2.3 Probability engine: converts calibrated ensemble/forecast into P(bracket) for every bracket in a city's ladder; output is a full distribution that sums to 1 across the ladder (±1%).
- FR-2.4 Go/no-go analysis (decision point): using calibrated σ and recorded market ladders, compute expected value per bracket-distance band under maker and taker pricing. Phase 3 proceeds only for trade shapes with modeled EV > 0 after fees and 1¢ adverse-fill allowance; if none qualify, weather halts and Phase 4 (gas) becomes flagship.

### FR-3 Weather trading (Phase 3)
- FR-3.1 Strategies (initial two, both consuming FR-2.3 distributions):
  (a) **Far-bracket NO**: buy NO on brackets whose model probability is below market-implied by a configured margin (default: model P(YES) ≤ market ask − 8pt, bracket ≥ 4°F from calibrated forecast median);
  (b) **Lock-in**: in the settlement-station afternoon window (local time), when the running max + remaining-heating model makes an outcome near-certain (model P ≥ 0.95), take tails still quoted against it.
- FR-3.2 Per-city local-time trade windows (no UTC-naive hour gates anywhere; all window logic in the city's timezone).
- FR-3.3 Maker-first execution: signals rest limit orders inside the spread; the sim models resting orders with fill probability tied to observed trade flow/quote traversal, plus `realistic_fills=True` with top-of-book depth cap and adverse-selection penalty for any taker path.
- FR-3.4 Risk: per-city and total weather exposure caps; per-symbol re-entry throttle (min 15 minutes between entries on the same market, max 3 entries/market/day — the Mar-27 runaway lesson); fixed base quantity per A8.
- FR-3.5 Paper run: ≥30 consecutive days settlement-true paper trading across ≥4 cities, reported daily to Discord with per-strategy settlement-true PnL.

### FR-4 AAA gas convergence bot (Phase 4)
- FR-4.1 Data provider scrapes the AAA daily national average (with EIA weekly and delayed RBOB as covariates); values persisted daily with provenance; scrape failure alerts and aborts signals.
- FR-4.2 Projection model: month-end (and week-end) settlement value projected from the daily AAA series via a lag/drift model fit on backfilled history (≥12 months); projection carries a confidence interval.
- FR-4.3 Strategy: in the final N days of a period (default 14), buy brackets the projection prices ≥8pt away from market, sized small; maker fees (25% of taker on this series) included in EV.
- FR-4.4 Same settlement-true recorder/reconcile pattern as weather (AAA published value = truth).

### FR-5 Capital gate & promotion (Phase 5)
- FR-5.1 Gate engine computes, per strategy: grouped settled-trade count, settlement-true net PnL (entry+exit fees included), win rate vs fee-adjusted breakeven at actual entry prices, and an exact binomial p-value.
- FR-5.2 Promotion criteria (pre-registered in the gate config, immutable during a run): ≥50 grouped settled trades AND binomial p < 0.05 vs breakeven AND settlement-true net PnL > 0, evaluated only on runs with realistic fills enabled. "72 hours positive" is removed everywhere.
- FR-5.3 Auto-pause: any promoted strategy whose rolling-30-settlement win rate falls below breakeven is demoted automatically and alerts.
- FR-5.4 Gate reports are generated as a dated artifact (per register-deferred-evidence-not-waived: deferrals/exceptions recorded inline, never silently waived).

## 6. Design & Experience Requirements

- **Observability first.** From dashboard + Discord alone the operator can answer, within 5 minutes: what is each bot's status (TRADING/FEED-ONLY/DISABLED), what signals fired today and why, what was rejected and why (reason codes), what settled and against what truth, and the settlement-true PnL per strategy. Testable: a red-team given only the dashboard and Discord must reconstruct the day's decisions without reading code.
- **Silence is always explained.** A day with zero trades produces an explicit "0 signals emitted, N rejected (reasons...)" summary — never an empty log.
- **Alerts are actionable.** Discord messages are only sent for events requiring operator attention (real staleness, scrape failure, gate transitions, settlement mismatches, daily digest). Target: false alarms <1/day (vs ~10/day today).
- **Neutral professional tone** in all logs, reports, and committed artifacts.
- **Terminal + web dashboard parity** is maintained (both consume `OrchestratorEngine`).

## 7. Architecture & Tech Stack

Unchanged core (kept deliberately): Python 3, `DataProvider`/`Strategy`/`Bot` ABCs (`src/core/interfaces.py`), `RiskManager` → `SimulatedExchange` flow, `SignalProcessorMixin`, web/terminal dashboard, Discord notifier, GCE VM + tmux runtime, pytest suite.

New/changed components:

```
src/data/
  kalshi_provider.py      # + floor/cap/strike_type passthrough, orderbook_fp handling, bid/ask/depth recording
  nws_provider.py         # re-pointed at settlement stations; running daily max
  ensemble_provider.py    # NEW: GEFS/NOMADS (+ optional ECMWF open data) daily-high members
  iem_cli_provider.py     # NEW: CLI ground truth (daily + backfill)
  aaa_gas_provider.py     # NEW: AAA daily average scrape + EIA/RBOB covariates
src/core/
  bracket_payoff.py       # NEW: single source of truth for between/greater/less payoffs (strategy + settlement)
  matching_engine.py      # realistic_fills default ON for gate runs; resting-order fill model; qty-0 cleanup
  risk_manager.py         # recency-windowed win rates; weather/gas exposure caps; fixed-size mode
  gate.py                 # NEW: FR-5 promotion engine + reports
src/strategies/
  weather_far_bracket.py  # NEW (FR-3.1a)
  weather_lockin.py       # NEW (FR-3.1b)
  gas_convergence.py      # NEW (FR-4.3)
src/calibration/
  forecast_calibration.py # NEW: FR-2.2 bias/σ pipeline + versioned calibration files
scripts/
  run_web_dashboard.py    # logging config fix; no in-process retrain
  reconcile_weather.py    # NEW: FR-1.3 daily settlement reconcile
```

Data flow (weather): `ensemble_provider` + `nws_provider` (station obs) + `kalshi_provider` (ladder w/ bid/ask/depth) → `probability engine` (calibrated) → strategies → `RiskManager` → `SimulatedExchange` (maker resting-order model, hold-to-settlement) → `bracket_payoff` settlement vs `iem_cli_provider` truth → `gate.py`.

## 8. Phased Roadmap

> Mapping to review §11: review-Phase 0 = PRD Phase 0; review-Phase 1 (weather rebuild) = PRD Phases 1–3; review-Phase 2 (gas) = PRD Phase 4; review-Phase 3 (gate) = PRD Phase 5.

### Phase 0 — Teardown & observability hardening (~1–2 days)
**Objective:** remove all negative-EV surface area and make the system incapable of silent failure; VM becomes a correct data harvester.
**Deliverables:** FR-0.1 – FR-0.7 implemented, deployed to the VM, dead code deleted, tests updated.
**Exit / Acceptance Criteria:**
1. `git grep` finds no registered bot other than `weather` (feed-only) in the registry; deleted strategy files are absent from `src/`; the full test suite passes with zero references to deleted modules.
2. On the VM, 24h after deploy: `logs/money_printer_*.log` exists on disk, is >1h old, contains INFO lines from at least one strategy-module logger, and survives one cycle boundary (file still present and growing afterward).
3. No retrain occurs on the tick thread: 24h of logs show zero `RETRAINED` lines from the runtime process, and max inter-heartbeat gap < 5 minutes across two cycle boundaries (vs ~55 min freezes today).
4. Kill-switch observability: a synthetic signal injected in a test is either executed or produces exactly one INFO rejection line with a reason code; the cycle summary lists every bot with TRADING/FEED-ONLY/DISABLED status.
5. Watchdog: ≤1 Discord alert per 24h across 48h of normal operation (vs ~10/day baseline), while a deliberately stopped process (test window) still alerts within 10 minutes.
6. Harvester: data CSVs on the VM contain bid AND ask columns for ≥4 cities' full ladders, and an hourly top-3-depth record, verified over 24h.
7. `exchange_state.json` contains zero positions with quantity 0; `strategy_win_rates.json` contains no crypto-era keys.

### Phase 1 — Weather market foundation: semantics, truth, lifecycle (~1 week)
**Objective:** the system understands weather contracts exactly as Kalshi settles them, records ground truth daily, and can hold positions to settlement.
**Deliverables:** FR-1.1 – FR-1.6; unit tests golden-keyed to live-API examples.
**Exit / Acceptance Criteria:**
1. `bracket_payoff` unit tests pass a golden table covering all three `strike_type` values, including the review's live-verified cases (B86.5 pays YES only for 86–87; T87 pays YES only ≥88; T80 pays YES only ≤79) and boundary temperatures (floor−1, floor, cap, cap+1); a mutation test (suffix-letter parser swapped back in) fails ≥1 test per contract type.
2. `rg "startswith\('B'\)|startswith\(\"B\"\)" src/` returns zero direction-inference hits; strategies and settlement consume only API fields.
3. Settlement recorder has run ≥3 consecutive days on the VM: settlement cache contains KXHIGH entries for all tracked cities with CLI values matching the IEM archive exactly, and the reconcile report shows 0 unexplained mismatches.
4. Backfill: ≥180 days of CLI daily highs per city persisted; row counts and 5 spot-checked values match the IEM archive.
5. Lifecycle: a sim weather position opened before a cycle boundary survives the boundary and settles at expiry with the FR-1.2 payoff (integration test on VM, verified in exchange state and journal); no TIME_LIMIT or CYCLE_RESET close reason appears on any weather position.
6. Station correctness: recorded observations for NY and CHI match the settlement stations (KNYC/KMDW) — cross-checked against IEM station data for 3 days; no KJFK/KORD values feed any strategy input.

### Phase 2 — Forecast engine & calibration (+ go/no-go) (~1–2 weeks)
**Objective:** a calibrated probability distribution over each city's daily high, and an honest EV verdict on whether weather trading proceeds.
**Deliverables:** FR-2.1 – FR-2.4; calibration report artifact.
**Exit / Acceptance Criteria:**
1. Ensemble provider returns ≥20 members for each of the 4 cities on ≥5 consecutive days; on induced fetch failure it aborts signal generation with an INFO reason (no silent fallback default).
2. Calibration files exist for all 4 cities, each built from ≥60 paired forecast-vs-CLI days (backfill + live), reporting bias and σ by lead time; recomputation is deterministic from inputs (byte-identical on re-run).
3. Measured day-of σ per city is published in the calibration report and is ≤4°F for at least 3 of 4 cities (sanity bound: published NWS accuracy ~2.5°F; a city failing this is excluded, not fudged).
4. Probability engine: for 10 recorded ladders, bracket probabilities sum to 1.0 ± 0.01 and are monotonically consistent with the calibrated CDF; a perturbation test (σ doubled) measurably flattens the distribution.
5. Go/no-go report exists as a dated artifact: EV per bracket-distance band under maker and taker pricing with fees and a 1¢ adverse-fill allowance, on ≥30 days of recorded ladders; it names which trade shapes (if any) are +EV and states the PROCEED/HALT decision per FR-2.4. Red-team can recompute one band's EV from raw inputs and match within rounding.

### Phase 3 — Weather trading, settlement-true paper run (~1–2 weeks build + ≥30 days run)
**Objective:** the flagship trades the shapes Phase 2 approved, maker-first, with pessimistic fills, and accumulates gate-quality evidence.
**Deliverables:** FR-3.1 – FR-3.5; both strategies live in paper on the VM.
**Exit / Acceptance Criteria:**
1. All trade-window logic is city-local: unit tests assert correct behavior across DST and for LAX vs NY at the same UTC instant; no `datetime.now()` without timezone appears in strategy code (`rg "datetime.now\(\)" src/strategies/` clean or tz-aware).
2. Fill realism: `realistic_fills=True` in the production config path (constructor call site verified); resting maker orders in sim fill only when the recorded market trades through the limit price; a regression test shows a taker order larger than recorded top-of-book depth fills partially, not fully.
3. Throttle: replaying the Mar-27 Chicago tape through the new stack produces ≤3 entries on that market that day (vs 12 historically).
4. 30-day paper run integrity: ≥30 consecutive days, ≥4 cities, zero settlement-reconcile mismatches, every position closed by settlement (not time/cycle), daily Discord digest delivered ≥28 of 30 days.
5. Honest reporting: the run report presents settlement-true net PnL (fees included) per strategy with grouped-trade counts and the FR-5.1 binomial vs breakeven — whatever the sign of the result. (Positive PnL is NOT an exit criterion for this phase; complete, truthful evidence is.)

### Phase 4 — AAA gas convergence bot (~1 week; may run parallel to Phase 3 after Phase 2 passes)
**Objective:** second engine on an uncorrelated, slow, public settlement index.
**Deliverables:** FR-4.1 – FR-4.4.
**Exit / Acceptance Criteria:**
1. AAA provider has ≥14 consecutive daily values persisted with provenance; an induced scrape failure produces an alert and zero signals that day.
2. Backtest artifact: the lag/drift projection, fit on ≥12 months of backfilled AAA/EIA/RBOB history, reports month-end projection MAE on ≥6 held-out month-ends; the strategy's simulated historical EV (maker fees included) is documented, and the bot trades in paper only if that EV > 0 (else the phase closes with a documented HALT, which still satisfies this criterion).
3. In paper: entries occur only within the configured final-N-day window at ≥8pt model-market divergence (verified from journal + logs over ≥1 settlement period), and settlement reconcile vs the published AAA value shows 0 mismatches.

### Phase 5 — Capital gate & promotion engine (~1 week build + gate accumulation)
**Objective:** a promotion mechanism that cannot be passed by luck, phantom PnL, or optimistic fills.
**Deliverables:** FR-5.1 – FR-5.4; gate wired to Phases 3–4 output; legacy gate removed.
**Exit / Acceptance Criteria:**
1. The gate config is pre-registered and versioned (thresholds: ≥50 grouped settled trades, binomial p<0.05 vs fee-adjusted breakeven at actual entries, settlement-true net PnL>0, realistic-fills-only); changing it mid-run invalidates the run (tested).
2. Mutation tests: a synthetic strategy with true 50% win rate against a >50% breakeven is rejected in ≥95% of 1,000 simulated gate runs; a synthetic +10pt-edge strategy passes in ≥80%; a phantom-PnL journal (settlement mismatches injected) is rejected with a reconcile failure, not a pass.
3. "72h positive" logic is deleted from the codebase (`rg -i "72h|72 hour"` in gate paths clean); the old gate cannot be invoked.
4. Auto-pause: a promoted synthetic strategy whose rolling-30 win rate is forced below breakeven is demoted within one evaluation cycle and produces an alert.
5. A dated gate report for every live paper strategy exists (pass, fail, or insufficient-n each explicitly stated), with deferrals recorded inline per the deferred-evidence rule.

## 9. Risks & Open Questions

**Risks**
1. **The weather edge may not survive calibration** (Phase 2 go/no-go). Mitigated by design: the halt path is cheap (~2–3 weeks sunk), gas becomes flagship, and the harvested data retains value.
2. **Maker fill modeling is the softest part of the sim.** Resting-order fill realism against recorded tape is an approximation; mitigated by pessimistic defaults (fill only on trade-through), the 1¢ adverse-fill allowance in EV, and gate criteria that discount marginal results.
3. **Ensemble data operational fragility** (NOMADS outages, format drift). Mitigated by graceful degradation (FR-2.1), caching, and abort-not-default semantics.
4. **Small paper samples.** At realistic weather trade rates, 50 settled trades may take >30 days; the gate is deliberately patient — do not shorten the sample to hit a date.
5. **Scope creep back toward crypto.** The mothballed latency-arb file and the KXBTCD study are the only sanctioned crypto remnants; both are explicitly not-for-capital.
6. **Kalshi structural change** (fee schedule, weather series delisting, API v2 changes). Low probability short-term; the landscape dump in `review_2026_07_24/landscape_out/` is the baseline to diff against.

**Open questions (non-blocking; defaults chosen)**
1. VM downsizing to e2-small after Phase 0 (default: yes, if harvester RSS < 1.5GB).
2. ECMWF open-data as a second ensemble source in Phase 2 or deferred (default: deferred unless GEFS-only calibration σ is marginal).
3. Expansion cities order after the core four (default: by observed volume — Denver, Philadelphia, Austin, Houston).
4. Whether the optional KXBTCD direction-controlled study is ever scheduled (default: not scheduled; requires explicit user request).
