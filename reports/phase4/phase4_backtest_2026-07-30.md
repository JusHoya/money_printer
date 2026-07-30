# Phase 4 backtest — AAA gas convergence — 2026-07-30

**PRD:** FR-4.2 / FR-4.3; Phase 4 exit criterion 2. **Branch:** `phase-4-gas-convergence`. **Workstream D.**

Every number in this document was computed by `scripts/gas_backtest.py` from the inputs hashed in §2. Nothing is carried over from another workstream's report without being recomputed here, and no EV is quoted for a fill the recorded tape says was unavailable.

> **What this run saw.** AAA daily national average `2022-01-01` .. `2026-07-29` — 1671 days, 4.57 yr — 1494 usable rows (56 `suspect` excluded, 177 missing calendar days interpolated in memory), content hash `d03ed6bf70e8f022`. That span yields **37 held-out month-ends** against the 6 exit criterion 2 requires, so the clause is **MET** (§3.1). Every number below is a function of this input: **if the AAA content hash changes, regenerate before citing this artifact** — §0's verdict must be re-derived rather than assumed to carry over.

---

## 0. Verdict

> ## HALT.
>
> **The strategy's simulated historical EV is large and positive (+21.43c/contract monthly, +24.91c weekly, maker fees included). The realized settlement-true PnL of the same trades is negative in every configuration tested, and the modelled EV lies far outside the realized confidence interval. The quantity FR-4.3 would size from is decisively wrong in the optimistic direction, so nothing may be sized from it. `GAS_TRADING_ENABLED` must stay `False`.**
>
> Stated precisely, because the distinction matters: this report does **not** establish that the strategy loses money — the realized confidence interval contains zero on this sample. It establishes that the modelled EV is not measuring the thing FR-4.3 believes it measures, and that the market's own price forecasts the settlement better than the model does.

| quantity | monthly `KXAAAGASM` | weekly `KXAAAGASW` |
|---|---|---|
| modelled EV/ct, taker, +1c allowance | +21.43c | +24.91c |
| modelled EV/ct, maker, +1c allowance | +27.09c | +32.67c |
| **realized/ct, taker, settlement-true** | **-9.88c** | **-1.78c** |
| realized 95% CI (clustered on the settlement event) | [-23.91c, +4.16c] | [-9.41c, +5.85c] |
| **is the modelled EV inside that interval?** | **NO** | **NO** |
| t on (modelled EV - realized) | 28.33 | 7.92 |
| settled trades / independent settlements | 120 / 2 | 351 / 10 |
| settlements with negative mean | 2 of 2 | 5 of 10 |
| mean modelled P(win) vs realized win rate | 0.6295 vs 33.3% | 0.6011 vs 31.1% |
| trade-level realized SE / t *(optimistic — see §5.2)* | 3.32c / -2.94 | 2.00c / -1.85 |

**The clustering unit is the settlement event, not the trade.** Every bracket on one ladder resolves against a single AAA publication, so 351 weekly trades are 10 independent draws. The trade-level standard error in the last row is printed only so nobody has to wonder what it was; it is not the number the verdict uses, and using it would have produced a much more confident-looking negative result than this sample supports.

### The full reasoning, strongest first

1. **FR-4.3's gate quantity is irreconcilable with what happened, by a wide margin.** On the trades the strategy itself accepted, modelled EV is +24.91c/contract on the weekly series and +21.43c on the monthly. The realized settlement-true PnL of those same trades, clustered on the settlement event (the only independent unit — every bracket on a ladder shares one AAA publication), is -1.78c weekly across 10 settlements, 95% CI [-9.41c, +5.85c]. **The modelled EV lies 7.9 standard errors above the realized mean and far outside that interval**; the maker leg is worse (+32.67c modelled, t = 9.9). FR-4.3 authorises paper trading *on modelled EV*. This report cannot show that the strategy loses money — the realized interval contains zero — but it does show decisively that the number FR-4.3 would size from is wrong, and wrong in the optimistic direction. Nothing may be sized from a quantity the data rejects. Note also that the realized point estimate is **negative in every configuration tested** (§6) and negative at 5 of 10 weekly settlements.

2. **The market's own price forecasts the settlement better than the model does.** Over the 1013 settled weekly brackets, scored per settlement event and averaged, the model's Brier score is 0.1332 against the market mid's 0.0775 — the model is worse by +0.0557 (SE 0.0141, t = 3.94 on 10 events; the model was the better forecaster at 0 of them). This needs no fee model, no fill model and no EV: it says that when this model and this market disagree, the market is more often right. An 8pt divergence gate on top of that is a filter for the model's own error, and the direction of the trade it produces is the wrong one. This is the mechanism behind finding 1 and it is why a wider backfill alone will not fix it.

3. **Tightening the divergence gate makes the modelled EV better and the outcome worse, monotonically.** A gate that selects genuine mispricings should improve the realized result as it is tightened. Raising the threshold from 8pt (headline) to 15pt then 25pt moves the weekly modelled EV from +24.91c to +31.18c then +37.47c while the realized result moves from -1.78c to -5.16c then -7.62c on 278 then 205 trades (§8). The two quantities move in **opposite** directions across the whole sweep. That is not a weak edge being diluted by noise; it is a filter that selects harder for the model's own disagreement with a better-informed price, and it rules out the 'raise the threshold' fix before anyone proposes it.

4. **The probability model is miscalibrated in exactly the direction that generates the trades.** Across the model-`P(YES)` deciles 0.2 to 0.8 — 368 settled weekly brackets over 10 distinct settlements — the realized YES rate is below the model's probability in **every** decile, by up to 0.292 (decile 0.6-0.7: model 0.65, realized 0.358). Mean modelled P(win) on accepted monthly trades is 0.6295 against a realized win rate of 33.3%. **The clustering caveat applies here too** — brackets within a settlement are not independent, so the per-decile n overstates the evidence. What is not explained by clustering is the *direction*: a monotone one-sided gap across seven consecutive deciles is not what sampling noise on a well-specified model looks like, and it is consistent with finding 2 measured a different way.

5. **The projection's *level* accuracy is not the problem.** *(supporting, not load-bearing)* At the 14-day lead the projection's held-out MAE over 1136 daily targets is $0.0705/gal with a bias of only -0.0030 — 4% of the average error, i.e. mostly noise rather than offset — and the reported sigma ($0.1422) is close to the $0.0883 a Gaussian of that MAE implies. The defect is not that the model cannot forecast the level; it is that a $0.01-spaced strike ladder magnifies a 7-cent level uncertainty into a probability the model cannot resolve, while the market can. This is listed as supporting because it explains the halting findings above rather than adding to them.

6. **The negative sign survives every perturbation.** *(supporting, not load-bearing)* §6 recomputes the headline under 3 RBOB sources, the EIA covariate on and off, `suspect` rows in and out, and both truth channels. The realized column is negative in every one, on both series. Unlike Phase 2's weather result there is no configuration in which this shape makes money, so the verdict does not depend on a source-selection judgement.

7. **Quote availability, not spread, bounds what is tradable.** *(supporting, not load-bearing)* Of the 1052 monthly snapshots that reached a usable projection (out of 1052 in the window), only 34.4% had a two-sided book at all (362); the YES offer is present in 56.9% of candidates and the NO offer in 70.2%. Where both sides are quoted the median spread is 2.0pt, but the p90 is 8.0pt and the max 33.0pt. This is Phase 2's finding repeated: a good EV on a one-sided book is a quote that was not there. It is listed as supporting because the verdict is already carried by outcomes measured on fills that *were* available.

### What this verdict does *not* say

It does not say the AAA series is unpredictable, and it does not say the projection is badly built. Over admissible daily targets the held-out MAE is $0.0080/gal at a 1-day lead and $0.0705 at 14 days, the 14-day bias is 4% of that error, the reported sigma ($0.1422) is close to the $0.0883 a Gaussian of that MAE implies, and the settlement rule reconciles 644/644 against Kalshi's own results (§3.4). Nor does it say the *sign* of the realized result is established — it is not, on 2 monthly and 10 weekly settlements.

What it says is narrower and harder to argue with: a `KXAAAGASM` ladder is spaced $0.01 apart while the projection's honest 14-day uncertainty is about $0.14, so the model cannot resolve which bracket will settle; the market can, and does so better than the model (§3.6). Every 8pt divergence the gate sees is therefore predominantly the model's own ignorance, and the EV computed from it is a number about the model rather than about the market.

---

## 1. Exit criterion 2, quoted verbatim, satisfied clause by clause

> **2.** Backtest artifact: the lag/drift projection, fit on >=12 months of backfilled AAA/EIA/RBOB history, reports month-end projection MAE on >=6 held-out month-ends; the strategy's simulated historical EV (maker fees included) is documented, and the bot trades in paper only if that EV > 0 (else the phase closes with a documented HALT, which still satisfies this criterion).

| clause | where | status |
|---|---|---|
| exists as a dated artifact | this file | `reports/phase4/phase4_backtest_2026-07-30.md`, regenerated by `scripts/gas_backtest.py run` |
| fit on >=12 months of backfilled AAA/EIA/RBOB history | §2.1 | AAA 2022-01-01 .. 2026-07-29 = 1671 days (4.57 yr), 1494 rows; RBOB 1541 rows; EIA 291 rows. `min_history_days=365` is enforced per fit and **aborts** rather than fitting short — 0 monthly decision-market pairs and 51 month-end MAE attempts aborted for exactly that reason |
| month-end MAE on >=6 held-out month-ends | §3.1 | **37 month-ends** against the 6 required — **MET**; the corresponding register item is CLOSED at §10.1 |
| simulated historical EV, maker fees included | §4, §5 | documented for both fee legs, per bracket-distance band and for the strategy's own accepted shape, with the 1c adverse-fill allowance; `KXAAAGASM` billed on `quadratic_with_maker_fees`, `KXAAAGASW` on `quadratic` |
| bot trades in paper only if that EV > 0 | §0, §9 | **HALT** — recommendation is that `GAS_TRADING_ENABLED` stays `False` |
| a documented HALT still satisfies this criterion | §0 | the HALT and its reasoning are documented here |
| red-team can recompute one case from raw inputs | §7 | one accepted trade worked end to end, hand-checkable against a normal table |

---

## 2. Provenance

### 2.1 Series actually fitted on

| series | path | rows | span | notes |
|---|---|---|---|---|
| AAA daily national | `data/gas_truth/aaa_daily_national.csv` | 1494 usable | 2022-01-01 .. 2026-07-29 (1671 d) | 56 rows flagged `quality=suspect` in the file and excluded by default; 177 calendar days inside the span have no row and are interpolated in memory only (contract §1.1) |
| RBOB daily spot | `reports/phase4/covariates/la_rbob_spot/rbob_daily.csv` | 1541 | 2020-06-01 .. 2026-07-27 | EIA series `PET.EER_EPMRR_PF4_Y05LA_DPG.D`; see §6.1 for why this workstream re-fetched all three alternatives itself |
| EIA weekly retail | `data/gas_truth/eia_weekly_regular.csv` | 291 | — | loaded and level-checked; **not** a regressor in the headline (near-collinear with AAA momentum). §6.2 turns it on |
| live series metadata | `reports/phase4/gas_series_metadata.json` | 4 series | — | `GET /series/{ticker}`; the fee-schedule and settlement-source check in §2.4 |
| Kalshi-pinned settlement truth | `tests/fixtures/gas/kalshi_pinned_truth.csv` | 79 | — | WS-B; source-independent of AAA. §3.3 scores against it |
| quote tape | `reports/phase4/gas_quote_tape.csv` | 39623 hourly candles | 405 markets, 14 events | sha256 `5f9a0c0a08340561` |

### 2.2 Where the quote tape comes from, and why it had to be built

**This project has never recorded a gas orderbook.** `data/ladders/` holds `KXHIGHCHI`, `KXHIGHLAX`, `KXHIGHMIA` and `KXHIGHNY` and nothing else, and Kalshi prunes settled markets from the public API after roughly two months, so when this tape was fetched only 74 settled `KXAAAGASM` markets (3 month-end event(s) reach the FR-4.3 window) and 266 settled `KXAAAGASW` markets (11 week-end event(s)) were retrievable at all. WS-B's settled-ladder fixture carries results and volumes but no quotes.

A historical quote surface therefore had to be recovered from the public **candlesticks** endpoint, which answers anonymously and returns `yes_bid` and `yes_ask` OHLC per hour:

```
GET /series/{series}/markets/{ticker}/candlesticks?period_interval=60 (anonymous, public)
base https://api.elections.kalshi.com/trade-api/v2
431 markets enumerated, 26 skipped (no elapsed life), 39623 hourly rows kept (last 16 days of each market's life)
```

`yes_bid == 0` and `yes_ask == 1` are Kalshi's empty-book sentinels, not prices; they are stored as absent and every EV statistic below excludes them while still counting them in `n cand`. The NO side is derived by Kalshi's identity `no_ask = 1 - yes_bid`, `no_bid = 1 - yes_ask`, and is absent whenever the YES side it derives from is.

### 2.3 Reproducibility

Every line of this artifact except the `Generated ...` timestamp in the footer is a function of `scripts/gas_backtest.py` and of the files hashed in §2.1, so re-running the generator against the same inputs reproduces it byte for byte. That was checked by generating twice and diffing: exactly one line differs, the footer timestamp. Two candidates for that list were deliberately removed — the generator's own wall time, which measures the machine rather than the data, and the working tree's git status, which made an earlier draft fail its own reproducibility check whenever any unrelated file in the repository changed. A check that fails cosmetically teaches the reader to ignore it.

### 2.4 Fee model

| series | live `fee_type` | maker fee | taker fee |
|---|---|---|---|
| `KXAAAGASM` (monthly, the FR-4.3 target) | `quadratic_with_maker_fees` | `ceil_to_cent(0.0175 * C * P * (1-P))` on the order total | `ceil_to_cent(0.07 * C * P * (1-P))` on the order total |
| `KXAAAGASW` (weekly) | `quadratic` | **$0.00** — absent from the non-standard table | same taker formula |
| `KXAAAGASD` (daily) | `quadratic` | **$0.00** | same taker formula |

The `fee_type` column above is not this project's opinion. `KNOWN_MAKER_FEE_SERIES` encodes the belief, so checking the code against itself would prove nothing; the exchange's own answer was pulled from `GET /series/{ticker}` and committed to `reports/phase4/gas_series_metadata.json`:

| series | live `fee_type` | `fee_multiplier` | settlement source | code agrees |
|---|---|---|---|---|
| `KXAAAGASM` | `quadratic_with_maker_fees` | 1 | AAA | yes |
| `KXAAAGASW` | `quadratic` | 1 | AAA | yes |
| `KXAAAGASD` | `quadratic` | 1 | AAA | yes |
| `KXHIGHNY` | `quadratic` | 1 | NWS Climatological Report | yes |

`KXHIGHNY` is included as the weather control the Phase 2 fee correction rests on. All three gas series settle on **AAA**, which is why the weekly series is admissible as evidence about the *shape* even though its fee schedule differs.

`KNOWN_MAKER_FEE_SERIES` = `['KXAAAGASM']`. Settlement is free, and a gas position is held to the AAA publication, so **one** fee leg is charged, not a round trip.

The PRD's phrase "maker fees (25% of taker on this series)" is correct as a **rate** and wrong as a **charged fee at small size**, because each leg is ceil'd to the cent independently. §7 shows the arithmetic on a real trade. Every fee in this report comes from `compute_fee(...)` with `fee_type_for_symbol(symbol)` threaded, at the actual contract count; no fee is ever scaled from the other.

### 2.5 Method — and the four things that would have made it dishonest

1. **Lookahead in the projection.** For each decision date the whole series is clamped with `GasSeries.observed_through(decision_date)` *before* the strategy sees it, so the `as_of` the strategy selects (`max(aaa.date)`) cannot be a row published after the decision. `project()` re-clamps and re-scans internally and raises `GasLookaheadError` on any unclamped path. Trading closes at 23:59 ET the evening before the value publishes, so the decision is always made on a projection — there is no version of this backtest in which reading the target date's value is legitimate.
2. **A re-implementation of the gates instead of the gates.** Every accept/reject below is `GasConvergenceStrategy.analyze()` returning a signal or not, on a `MarketData` built from the tape. Fees come from `GasConvergenceStrategy._ev`. Nothing here re-derives a gate or a fee.
3. **Pricing a fill the book never offered.** Each shape names the side of the book it must hit and is excluded — while still counted — when that side is absent. §4.1 prints the executable fraction beside every cell.
4. **Quoting the model's number and not the outcome.** Every cell carries both the modelled EV and the realized settlement-true PnL of the identical trade, using Kalshi's own `result`, which reconciled 644/644 against `settles_yes_gas(expiration_value, floor_strike)` (§3.4). Where the two numbers disagree, the realized one is what happened.

**Structural gates applied before a snapshot becomes a candidate:** the FR-4.3 window (`0 < settlement - today <= 14` d) and data freshness (newest AAA row <= 2 d old). One decision snapshot is taken per (market, ET date) — the last hourly candle at or before 18:00 ET — so a per-day result cannot be an artifact of which hour happened to be quoted. Order size C = 5 contracts (FR-4.3 "sized small", the strategy's own `base_quantity` default). Fit budget for this artifact: 18990 regressions across 6 configurations plus the §8 sweep, all sequential (the wall time is in the JSON companion; it is a property of the machine, not of the data, so it is kept out of this file to preserve the byte-for-byte reproducibility claimed in §2.3).

---

## 3. Projection accuracy

### 3.1 Held-out month-ends (the criterion's own table)

Strict walk-forward: for each month-end the fit sees only rows dated before it. `as_of` is the newest **observed** AAA date at or before `target - nominal lead`, because the projection anchors on `A(as_of)` and `require_observed_as_of` forbids extrapolating that anchor; the realized lead is therefore `>=` the nominal one and is printed per row.

| nominal lead (d) | n | MAE | bias | RMSE | median \|err\| | max \|err\| | mean model sigma |
|---|---|---|---|---|---|---|---|
| 1 | 37 | $0.0081 | -0.0003 | $0.0119 | $0.0065 | $0.0462 | $0.0112 |
| 7 | 37 | $0.0464 | +0.0059 | $0.0700 | $0.0370 | $0.2839 | $0.0750 |
| 14 | 37 | $0.0644 | +0.0052 | $0.0849 | $0.0524 | $0.2697 | $0.1423 |

**Month-ends held out: 37.** The criterion asks for >= 6, so this clause is **MET**. The register item that tracked it is CLOSED at §10.1. For the record of what produced them: the series starts 2022-01-01 and FR-4.2's `min_history_days = 365` makes 2022-12-31 the earliest admissible `as_of`, so every month-end after that date qualifies.

| target | nom lead | as_of | real lead | point | sigma | truth | error | n_train | model | inputs_hash |
|---|---|---|---|---|---|---|---|---|---|---|
| 2023-01-31 | 1 | 2023-01-30 | 1 | 3.5183 | 0.0134 | 3.5050 | +0.0133 | 387 | lagdrift_v1+rbobL8 | 0f15aeeb02da |
| 2023-01-31 | 7 | 2023-01-24 | 7 | 3.5176 | 0.0997 | 3.5050 | +0.0126 | 375 | lagdrift_v1+rbobL8 | 50555cf3f805 |
| 2023-01-31 | 14 | 2023-01-17 | 14 | 3.3826 | 0.1947 | 3.5050 | -0.1224 | 361 | lagdrift_v1+rbobL8 | b2e09206cd5e |
| 2023-02-28 | 1 | 2023-02-27 | 1 | 3.3593 | 0.0130 | 3.3570 | +0.0023 | 415 | lagdrift_v1+rbobL8 | bf5729b921c8 |
| 2023-02-28 | 7 | 2023-02-21 | 7 | 3.3940 | 0.0971 | 3.3570 | +0.0370 | 403 | lagdrift_v1+rbobL8 | 898e03969805 |
| 2023-02-28 | 14 | 2023-02-14 | 14 | 3.3809 | 0.1905 | 3.3570 | +0.0239 | 389 | lagdrift_v1+rbobL8 | f668ad16fa62 |
| 2023-03-31 | 1 | 2023-03-30 | 1 | 3.4941 | 0.0128 | 3.5010 | -0.0069 | 446 | lagdrift_v1+rbobL8 | bf4fe8777ce2 |
| 2023-03-31 | 7 | 2023-03-24 | 7 | 3.4335 | 0.0946 | 3.5010 | -0.0675 | 434 | lagdrift_v1+rbobL8 | f8467378828c |
| 2023-03-31 | 14 | 2023-03-16 | 15 | 3.4690 | 0.1955 | 3.5010 | -0.0320 | 418 | lagdrift_v1+rbobL8 | 08b08f58a75f |
| 2023-04-30 | 1 | 2023-04-29 | 1 | 3.6086 | 0.0126 | 3.6110 | -0.0024 | 476 | lagdrift_v1+rbobL8 | 971f6d19b0ff |
| 2023-04-30 | 7 | 2023-04-23 | 7 | 3.6713 | 0.0921 | 3.6110 | +0.0603 | 464 | lagdrift_v1+rbobL8 | 67574e56577f |
| 2023-04-30 | 14 | 2023-04-16 | 14 | 3.7324 | 0.1799 | 3.6110 | +0.1214 | 450 | lagdrift_v1+rbobL8 | c34d316a454a |
| 2023-05-31 | 1 | 2023-05-30 | 1 | 3.5828 | 0.0123 | 3.5760 | +0.0068 | 507 | lagdrift_v1+rbobL8 | b1c45d36f9ec |
| 2023-05-31 | 7 | 2023-05-24 | 7 | 3.5794 | 0.0898 | 3.5760 | +0.0034 | 495 | lagdrift_v1+rbobL8 | 8fe365b06207 |
| 2023-05-31 | 14 | 2023-05-17 | 14 | 3.5345 | 0.1756 | 3.5760 | -0.0415 | 481 | lagdrift_v1+rbobL8 | 339af2a07132 |
| 2023-06-30 | 1 | 2023-06-29 | 1 | 3.5448 | 0.0121 | 3.5430 | +0.0018 | 537 | lagdrift_v1+rbobL8 | 12a1cc967025 |
| 2023-06-30 | 7 | 2023-06-23 | 7 | 3.5870 | 0.0877 | 3.5430 | +0.0440 | 525 | lagdrift_v1+rbobL8 | b4a7e014237d |
| 2023-06-30 | 14 | 2023-06-16 | 14 | 3.5801 | 0.1707 | 3.5430 | +0.0371 | 511 | lagdrift_v1+rbobL8 | f74851d0a2ef |
| 2023-07-31 | 1 | 2023-07-30 | 1 | 3.7714 | 0.0121 | 3.7570 | +0.0144 | 568 | lagdrift_v1+rbobL8 | 83ea42a0c59b |
| 2023-07-31 | 7 | 2023-07-24 | 7 | 3.6165 | 0.0854 | 3.7570 | -0.1405 | 556 | lagdrift_v1+rbobL8 | 89548daf9b96 |
| 2023-07-31 | 14 | 2023-07-17 | 14 | 3.5867 | 0.1660 | 3.7570 | -0.1703 | 542 | lagdrift_v1+rbobL8 | 278790c09d4f |
| 2023-08-31 | 1 | 2023-08-30 | 1 | 3.8247 | 0.0118 | 3.8250 | -0.0003 | 599 | lagdrift_v1+rbobL8 | 14b9c7597ea3 |
| 2023-08-31 | 7 | 2023-08-24 | 7 | 3.8177 | 0.0845 | 3.8250 | -0.0073 | 587 | lagdrift_v1+rbobL8 | 51af7ac1c521 |
| 2023-08-31 | 14 | 2023-08-17 | 14 | 3.9198 | 0.1640 | 3.8250 | +0.0948 | 573 | lagdrift_v1+rbobL8 | 6b4a9306207a |
| 2023-09-30 | 1 | 2023-09-29 | 1 | 3.8287 | 0.0117 | 3.8230 | +0.0057 | 629 | lagdrift_v1+rbobL6 | 1569e6f72314 |
| 2023-09-30 | 7 | 2023-09-23 | 7 | 3.8331 | 0.0833 | 3.8230 | +0.0101 | 617 | lagdrift_v1+rbobL8 | b30d8bbd0127 |
| 2023-09-30 | 14 | 2023-09-16 | 14 | 3.9159 | 0.1603 | 3.8230 | +0.0929 | 603 | lagdrift_v1+rbobL8 | a39e8b695b32 |
| 2023-10-31 | 1 | 2023-10-30 | 1 | 3.4871 | 0.0114 | 3.4780 | +0.0091 | 660 | lagdrift_v1+rbobL5 | dc41c3a9aad2 |
| 2023-10-31 | 7 | 2023-10-24 | 7 | 3.5271 | 0.0784 | 3.4780 | +0.0491 | 648 | lagdrift_v1+rbobL2 | ce4089d7cbb7 |
| 2023-10-31 | 14 | 2023-10-17 | 14 | 3.5272 | 0.1549 | 3.4780 | +0.0492 | 634 | lagdrift_v1+rbobL2 | 79ac8eb07e29 |
| 2023-11-30 | 1 | 2023-11-29 | 1 | 3.2419 | 0.0108 | 3.2460 | -0.0041 | 690 | lagdrift_v1+rbobL2 | 27c05f58dd17 |
| 2023-11-30 | 7 | 2023-11-23 | 7 | 3.2133 | 0.0771 | 3.2460 | -0.0327 | 678 | lagdrift_v1+rbobL2 | bbc3816b4998 |
| 2023-11-30 | 14 | 2023-11-16 | 14 | 3.3102 | 0.1521 | 3.2460 | +0.0642 | 664 | lagdrift_v1+rbobL2 | fa4acb2b73d5 |
| 2023-12-31 | 1 | 2023-12-30 | 1 | 3.1184 | 0.0108 | 3.1120 | +0.0064 | 721 | lagdrift_v1+rbobL2 | d3984f85d785 |
| 2023-12-31 | 7 | 2023-12-24 | 7 | 3.1619 | 0.0762 | 3.1120 | +0.0499 | 709 | lagdrift_v1+rbobL2 | 2999898d9813 |
| 2023-12-31 | 14 | 2023-12-16 | 15 | 2.9843 | 0.1588 | 3.1120 | -0.1277 | 693 | lagdrift_v1+rbobL2 | 3844f04fcd79 |
| 2024-01-31 | 1 | 2024-01-30 | 1 | 3.1329 | 0.0107 | 3.1410 | -0.0081 | 752 | lagdrift_v1+rbobL2 | ebbded31b174 |
| 2024-01-31 | 7 | 2024-01-24 | 7 | 3.0995 | 0.0751 | 3.1410 | -0.0415 | 740 | lagdrift_v1+rbobL2 | ba6ef50832ec |
| 2024-01-31 | 14 | 2024-01-17 | 14 | 3.0896 | 0.1472 | 3.1410 | -0.0514 | 726 | lagdrift_v1+rbobL2 | 24438e7c808e |
| 2024-02-29 | 1 | 2024-02-28 | 1 | 3.2944 | 0.0107 | 3.3190 | -0.0246 | 781 | lagdrift_v1+rbobL2 | b0b5283a35bc |
| 2024-02-29 | 7 | 2024-02-22 | 7 | 3.2623 | 0.0744 | 3.3190 | -0.0567 | 769 | lagdrift_v1+rbobL2 | 43c8ff3ebe38 |
| 2024-02-29 | 14 | 2024-02-15 | 14 | 3.3771 | 0.1449 | 3.3190 | +0.0581 | 755 | lagdrift_v1+rbobL2 | 325666c2bfc5 |
| 2024-03-31 | 1 | 2024-03-30 | 1 | 3.5364 | 0.0109 | 3.5350 | +0.0014 | 812 | lagdrift_v1+rbobL5 | e1e8860ff9a7 |
| 2024-03-31 | 7 | 2024-03-24 | 7 | 3.5767 | 0.0763 | 3.5350 | +0.0417 | 800 | lagdrift_v1+rbobL5 | bb3a4e25e56d |
| 2024-03-31 | 14 | 2024-03-17 | 14 | 3.5350 | 0.1431 | 3.5350 | -0.0000 | 786 | lagdrift_v1+rbobL2 | b414963ead2c |
| 2024-04-30 | 1 | 2024-04-29 | 1 | 3.6571 | 0.0108 | 3.6570 | +0.0001 | 842 | lagdrift_v1+rbobL5 | 4825bef15be9 |
| 2024-04-30 | 7 | 2024-04-22 | 8 | 3.6854 | 0.0839 | 3.6570 | +0.0284 | 828 | lagdrift_v1+rbobL2 | 41631b957e1a |
| 2024-04-30 | 14 | 2024-04-16 | 14 | 3.6717 | 0.1410 | 3.6570 | +0.0147 | 816 | lagdrift_v1+rbobL2 | ade8958ba8cd |
| 2024-05-31 | 1 | 2024-05-30 | 1 | 3.5636 | 0.0107 | 3.5590 | +0.0046 | 873 | lagdrift_v1+rbobL5 | 6d6cdcecfe70 |
| 2024-05-31 | 7 | 2024-05-24 | 7 | 3.6170 | 0.0740 | 3.5590 | +0.0580 | 861 | lagdrift_v1+rbobL5 | 5117df16ec5d |
| 2024-05-31 | 14 | 2024-05-17 | 14 | 3.5657 | 0.1414 | 3.5590 | +0.0067 | 847 | lagdrift_v1+rbobL5 | 3390f8c702b2 |
| 2024-06-30 | 1 | 2024-06-29 | 1 | 3.5009 | 0.0103 | 3.4920 | +0.0089 | 903 | lagdrift_v1+rbobL2 | b89af22a025e |
| 2024-06-30 | 7 | 2024-06-23 | 7 | 3.4548 | 0.0710 | 3.4920 | -0.0372 | 891 | lagdrift_v1+rbobL2 | e4da70856fe3 |
| 2024-06-30 | 14 | 2024-06-16 | 14 | 3.4599 | 0.1369 | 3.4920 | -0.0321 | 877 | lagdrift_v1+rbobL2 | 17a46378c7d5 |
| 2024-07-31 | 1 | 2024-07-30 | 1 | 3.5009 | 0.0102 | 3.4920 | +0.0089 | 934 | lagdrift_v1+rbobL2 | 0fe153aca956 |
| 2024-07-31 | 7 | 2024-07-24 | 7 | 3.5026 | 0.0700 | 3.4920 | +0.0106 | 922 | lagdrift_v1+rbobL2 | 243cdc9dce66 |
| 2024-07-31 | 14 | 2024-07-17 | 14 | 3.4833 | 0.1348 | 3.4920 | -0.0087 | 908 | lagdrift_v1+rbobL2 | 3b3cd4f76f4d |
| 2024-08-31 | 1 | 2024-08-30 | 1 | 3.3495 | 0.0101 | 3.3390 | +0.0105 | 965 | lagdrift_v1+rbobL2 | e14e2c5ce341 |
| 2024-08-31 | 7 | 2024-08-24 | 7 | 3.3287 | 0.0691 | 3.3390 | -0.0103 | 953 | lagdrift_v1+rbobL2 | 9704ce7454cb |
| 2024-08-31 | 14 | 2024-08-17 | 14 | 3.4092 | 0.1328 | 3.3390 | +0.0702 | 939 | lagdrift_v1+rbobL2 | 04a4fe3f23fc |
| 2024-10-31 | 1 | 2024-10-30 | 1 | 3.1326 | 0.0102 | 3.1310 | +0.0016 | 1026 | lagdrift_v1+rbobL6 | c6692b34b872 |
| 2024-10-31 | 7 | 2024-10-24 | 7 | 3.1220 | 0.0696 | 3.1310 | -0.0090 | 1014 | lagdrift_v1+rbobL6 | 0600e36fcdb6 |
| 2024-10-31 | 14 | 2024-10-17 | 14 | 3.1862 | 0.1321 | 3.1310 | +0.0552 | 1000 | lagdrift_v1+rbobL6 | 43ee3c13ceaf |
| 2024-11-30 | 1 | 2024-11-29 | 1 | 3.0635 | 0.0101 | 3.0570 | +0.0065 | 1056 | lagdrift_v1+rbobL6 | ada5f7f6ae37 |
| 2024-11-30 | 7 | 2024-11-23 | 7 | 3.0451 | 0.0686 | 3.0570 | -0.0119 | 1044 | lagdrift_v1+rbobL6 | b1bf31cb9f5e |
| 2024-11-30 | 14 | 2024-11-16 | 14 | 3.0667 | 0.1304 | 3.0570 | +0.0097 | 1030 | lagdrift_v1+rbobL6 | 1c8ec2d2aa98 |
| 2024-12-31 | 1 | 2024-12-29 | 2 | 3.0210 | 0.0196 | 3.0430 | -0.0220 | 1085 | lagdrift_v1+rbobL6 | 8e282afa0e68 |
| 2024-12-31 | 7 | 2024-12-24 | 7 | 3.0457 | 0.0677 | 3.0430 | +0.0027 | 1075 | lagdrift_v1+rbobL6 | 36ca0b2e87ad |
| 2024-12-31 | 14 | 2024-12-17 | 14 | 3.0376 | 0.1285 | 3.0430 | -0.0054 | 1061 | lagdrift_v1+rbobL6 | 902643740e61 |
| 2025-01-31 | 1 | 2025-01-30 | 1 | 3.1168 | 0.0096 | 3.1100 | +0.0068 | 1118 | lagdrift_v1+rbobL2 | 65aa4c9da72c |
| 2025-01-31 | 7 | 2025-01-24 | 7 | 3.1435 | 0.0650 | 3.1100 | +0.0335 | 1106 | lagdrift_v1+rbobL2 | c1b5cbc76a5f |
| 2025-01-31 | 14 | 2025-01-17 | 14 | 3.1624 | 0.1243 | 3.1100 | +0.0524 | 1092 | lagdrift_v1+rbobL2 | bbdcc37a65c9 |
| 2025-02-28 | 1 | 2025-02-27 | 1 | 3.1171 | 0.0096 | 3.1140 | +0.0031 | 1146 | lagdrift_v1+rbobL2 | e63d86ffe5fb |
| 2025-02-28 | 7 | 2025-02-21 | 7 | 3.1525 | 0.0643 | 3.1140 | +0.0385 | 1134 | lagdrift_v1+rbobL2 | 39b5e1b5f6c2 |
| 2025-02-28 | 14 | 2025-02-14 | 14 | 3.1971 | 0.1230 | 3.1140 | +0.0831 | 1120 | lagdrift_v1+rbobL2 | 1bc3598c8b45 |
| 2025-03-31 | 1 | 2025-03-30 | 1 | 3.1654 | 0.0095 | 3.1680 | -0.0026 | 1177 | lagdrift_v1+rbobL2 | 1ccc6496b39e |
| 2025-03-31 | 7 | 2025-03-24 | 7 | 3.1592 | 0.0637 | 3.1680 | -0.0088 | 1165 | lagdrift_v1+rbobL2 | 02102802441e |
| 2025-03-31 | 14 | 2025-03-17 | 14 | 3.0823 | 0.1216 | 3.1680 | -0.0857 | 1151 | lagdrift_v1+rbobL2 | 8759c77cdf34 |
| 2025-05-31 | 1 | 2025-05-30 | 1 | 3.1586 | 0.0098 | 3.1500 | +0.0086 | 1238 | lagdrift_v1+rbobL6 | c8abe69df412 |
| 2025-05-31 | 7 | 2025-05-22 | 9 | 3.1945 | 0.0831 | 3.1500 | +0.0445 | 1222 | lagdrift_v1+rbobL6 | e1e05727b89b |
| 2025-05-31 | 14 | 2025-05-17 | 14 | 3.2217 | 0.1222 | 3.1500 | +0.0717 | 1212 | lagdrift_v1+rbobL6 | b52bc129197b |
| 2025-06-30 | 1 | 2025-06-29 | 1 | 3.1834 | 0.0098 | 3.1850 | -0.0016 | 1268 | lagdrift_v1+rbobL6 | 8c3fd31fc9c7 |
| 2025-06-30 | 7 | 2025-06-23 | 7 | 3.2641 | 0.0646 | 3.1850 | +0.0791 | 1256 | lagdrift_v1+rbobL6 | 3952c1fb35d8 |
| 2025-06-30 | 14 | 2025-06-16 | 14 | 3.1516 | 0.1209 | 3.1850 | -0.0334 | 1242 | lagdrift_v1+rbobL6 | 66a1bf31c794 |
| 2025-08-31 | 1 | 2025-08-30 | 1 | 3.1968 | 0.0097 | 3.1880 | +0.0088 | 1330 | lagdrift_v1+rbobL6 | 370c85f60f5d |
| 2025-08-31 | 7 | 2025-08-24 | 7 | 3.1607 | 0.0633 | 3.1880 | -0.0273 | 1318 | lagdrift_v1+rbobL6 | cf1250ef5925 |
| 2025-08-31 | 14 | 2025-08-17 | 14 | 3.1342 | 0.1187 | 3.1880 | -0.0538 | 1304 | lagdrift_v1+rbobL6 | cdc5e6bfb9e7 |
| 2025-10-31 | 1 | 2025-10-30 | 1 | 3.0342 | 0.0096 | 3.0400 | -0.0058 | 1391 | lagdrift_v1+rbobL6 | 50d5b4c45def |
| 2025-10-31 | 7 | 2025-10-23 | 8 | 3.0767 | 0.0710 | 3.0400 | +0.0367 | 1377 | lagdrift_v1+rbobL6 | a5edda837a43 |
| 2025-10-31 | 14 | 2025-10-17 | 14 | 3.0103 | 0.1164 | 3.0400 | -0.0297 | 1365 | lagdrift_v1+rbobL6 | 8b508f061195 |
| 2025-11-30 | 1 | 2025-11-28 | 2 | 3.0066 | 0.0186 | 3.0050 | +0.0016 | 1419 | lagdrift_v1+rbobL6 | 37a8e7f56b95 |
| 2025-11-30 | 7 | 2025-11-22 | 8 | 3.0798 | 0.0705 | 3.0050 | +0.0748 | 1407 | lagdrift_v1+rbobL6 | 08d3b4a4f468 |
| 2025-11-30 | 14 | 2025-11-16 | 14 | 3.0722 | 0.1153 | 3.0050 | +0.0672 | 1395 | lagdrift_v1+rbobL6 | cc9bb1cc20a8 |
| 2025-12-31 | 1 | 2025-12-30 | 1 | 2.8311 | 0.0095 | 2.8390 | -0.0079 | 1452 | lagdrift_v1+rbobL6 | b2f90595d48e |
| 2025-12-31 | 7 | 2025-12-24 | 7 | 2.8256 | 0.0614 | 2.8390 | -0.0134 | 1440 | lagdrift_v1+rbobL6 | 7930acf9f5d4 |
| 2025-12-31 | 14 | 2025-12-17 | 14 | 2.8724 | 0.1145 | 2.8390 | +0.0334 | 1426 | lagdrift_v1+rbobL6 | 3a86674175b5 |
| 2026-01-31 | 1 | 2026-01-30 | 1 | 2.8742 | 0.0095 | 2.8700 | +0.0042 | 1483 | lagdrift_v1+rbobL6 | fc6340bec522 |
| 2026-01-31 | 7 | 2026-01-24 | 7 | 2.8785 | 0.0608 | 2.8700 | +0.0085 | 1471 | lagdrift_v1+rbobL6 | e76ed2fdd283 |
| 2026-01-31 | 14 | 2026-01-17 | 14 | 2.8514 | 0.1134 | 2.8700 | -0.0186 | 1457 | lagdrift_v1+rbobL6 | 8f680f837d72 |
| 2026-03-31 | 1 | 2026-03-30 | 1 | 3.9942 | 0.0098 | 4.0180 | -0.0238 | 1542 | lagdrift_v1+rbobL2 | d09a89a9f2f9 |
| 2026-03-31 | 7 | 2026-03-24 | 7 | 4.0864 | 0.0640 | 4.0180 | +0.0684 | 1530 | lagdrift_v1+rbobL2 | d8b3bacd32e7 |
| 2026-03-31 | 14 | 2026-03-17 | 14 | 4.0044 | 0.1218 | 4.0180 | -0.0136 | 1516 | lagdrift_v1+rbobL2 | 3732e5bee5c3 |
| 2026-04-30 | 1 | 2026-04-29 | 1 | 4.2538 | 0.0104 | 4.3000 | -0.0462 | 1572 | lagdrift_v1+rbobL5 | 4ed084afc368 |
| 2026-04-30 | 7 | 2026-04-23 | 7 | 4.0161 | 0.0642 | 4.3000 | -0.2839 | 1560 | lagdrift_v1+rbobL2 | 8c65bb4cc1ac |
| 2026-04-30 | 14 | 2026-04-16 | 14 | 4.0303 | 0.1216 | 4.3000 | -0.2697 | 1546 | lagdrift_v1+rbobL2 | ccad05605082 |
| 2026-05-31 | 1 | 2026-05-30 | 1 | 4.3378 | 0.0104 | 4.3360 | +0.0018 | 1603 | lagdrift_v1+rbobL2 | 60a690112cfb |
| 2026-05-31 | 7 | 2026-05-24 | 7 | 4.4989 | 0.0670 | 4.3360 | +0.1629 | 1591 | lagdrift_v1+rbobL2 | 8517cfb55996 |
| 2026-05-31 | 14 | 2026-05-17 | 14 | 4.5066 | 0.1271 | 4.3360 | +0.1706 | 1577 | lagdrift_v1+rbobL2 | e33ce8d5ddd2 |
| 2026-06-30 | 1 | 2026-06-29 | 1 | 3.8541 | 0.0104 | 3.8470 | +0.0071 | 1633 | lagdrift_v1+rbobL2 | c59d38a00da4 |
| 2026-06-30 | 7 | 2026-06-23 | 7 | 3.8605 | 0.0676 | 3.8470 | +0.0135 | 1621 | lagdrift_v1+rbobL2 | e43b5ee7a21b |
| 2026-06-30 | 14 | 2026-06-16 | 14 | 3.9574 | 0.1289 | 3.8470 | +0.1104 | 1607 | lagdrift_v1+rbobL2 | 52e4570ac880 |

**The independence limitation, stated plainly.** Truth for these month-ends is the AAA series the model is also fitted on. They are held out in **time**, not in **source**: a systematic error in the Wayback scrape would cancel between prediction and truth and this table could not see it. §3.3 scores the same estimator against a different measurement channel; the two numbers are reported separately and are never blended.

### 3.2 All admissible daily targets (the same estimator, a larger sample)

The month-end sample is too small to measure a bias, so the identical walk-forward machinery is run over every AAA date the projection can legally be scored on. The targets overlap heavily — a 14-day lead shares 13 days with its neighbour — so these rows are strongly dependent and no standard error is quoted from them. What they do measure reliably is the **sign and size of the bias**.

| nominal lead (d) | n | MAE | bias | RMSE | median \|err\| | max \|err\| | mean model sigma |
|---|---|---|---|---|---|---|---|
| 1 | 1148 | $0.0080 | -0.0001 | $0.0122 | $0.0056 | $0.1151 | $0.0120 |
| 7 | 1142 | $0.0410 | -0.0023 | $0.0611 | $0.0316 | $0.4682 | $0.0752 |
| 14 | 1136 | $0.0705 | -0.0030 | $0.1075 | $0.0494 | $0.6964 | $0.1422 |

At the 14-day lead FR-4.3 actually trades, the projection is biased **low** by $-0.0030/gal against an MAE of $0.0705 on 1136 targets — i.e. the error is 4% bias rather than noise. On a `strictly greater` ladder a level bias of that sign is directly an upward bias in `P(YES)` at every strike, which is the mechanism §3.5 measures on outcomes.

### 3.3 Source-independent cross-check (Kalshi-pinned truth)

WS-B recovered settlement truth from **settled Kalshi ladders alone** — which strikes paid YES, which paid NO — giving a closed interval `(low, high]` per settlement date plus Kalshi's own published `expiration_value`. That is a different measurement channel from the Wayback AAA scrape: neither derives from the other.

#### 3.3.1 Do the two channels agree? (measured by this run)

Recomputed here from `data/gas_truth/aaa_daily_national.csv` (1550 rows, 56 `suspect`) and `tests/fixtures/gas/kalshi_pinned_truth.csv` (79 rows over 67 distinct settlement dates) rather than quoted from anywhere, so it moves when the AAA series moves. Counts are **per pinned row**; a settlement date carried by two or three series contributes a row each.

| check | result |
|---|---|
| our value inside the ladder-implied interval `(low, high]` | **76 of 76** rows that have an AAA value |
| pinned rows with no AAA row at all | 3 (2026-05-28, 2026-06-02, 2026-07-28) |
| max |ours − Kalshi `expiration_value`| | **$0.0040** on 2026-07-13 |
| pinned dates whose AAA row is flagged `suspect` | 3 (2026-06-05, 2026-06-07, 2026-07-25) |

**ET attribution.** AAA republishes during the morning, so a capture taken at the wrong hour can carry the previous day's figure. If our series were systematically shifted by a day, the previous-day column below would hold most of the mass rather than a handful of dates — which is the only reason this breakdown is worth rendering.

| our value vs Kalshi's published `expiration_value` | rows |
|---|---|
| matches our **same-day** value | **75** |
| matches our **previous-day** value | 1 (2026-07-13) |
| matches **neither** | **0** |
| no AAA row to compare | 3 |
| **total** pinned rows carrying a Kalshi value | 79 |

Both checks pass: every AAA value on a pinned date falls inside the interval the exchange's own settled ladder implies, and no pinned settlement matches neither our same-day nor our previous-day value. The single previous-day match (2026-07-13) is also the related maximum deviation above; one isolated date is a publication-hour artifact on that day, not a systematic offset. The publication-hour effect itself is quantified in `docs/phase4_data_contract.md` §6.3, which names three distinct metrics for it — this section deliberately restates none of them, because it measures a different property (agreement with the exchange, not stability under re-dating).

#### 3.3.2 The projection scored against that channel

| nominal lead (d) | n | MAE | bias | RMSE | median \|err\| | max \|err\| | mean model sigma |
|---|---|---|---|---|---|---|---|
| 1 | 67 | $0.0122 | +0.0026 | $0.0167 | $0.0097 | $0.0562 | $0.0113 |
| 7 | 67 | $0.0680 | +0.0197 | $0.0832 | $0.0592 | $0.1901 | $0.0682 |
| 14 | 67 | $0.1472 | +0.0514 | $0.1739 | $0.1313 | $0.3719 | $0.1287 |

67 pinned settlement dates scored (79 pinned rows across daily, weekly and monthly series). The two channels agree to within the $0.0040 maximum deviation measured in §3.3.1, which is the expected result and is why the §0 verdict does not rest on a truth-channel argument.

### 3.4 Settlement-rule reconcile

Kalshi's `result` versus a recompute of `settles_yes_gas(expiration_value, floor_strike)` — the strict-greater rule the model implements — over every settled market in the tape:

| series | match | MISMATCH | unsettled | no expiration_value |
|---|---|---|---|---|
| `KXAAAGASM` | 644 | 0 | 408 | 0 |
| `KXAAAGASW` | 1460 | 0 | 73 | 0 |

Zero mismatches means the payoff rule in `src/models/gas_projection.py` is the exchange's rule, including the strict `>` at the boundary. This is the one thing in this report that is unambiguously working.

### 3.5 Probability calibration — where the strategy actually breaks

Model `P(YES)` decile against the realized YES rate, over **every** executable YES candidate rather than only the accepted ones, so the answer is a property of the probability model and not of the selection.

**`KXAAAGASM` (monthly)**

| model P(YES) decile | n brackets | n distinct settlements | decile midpoint | realized YES rate | gap |
|---|---|---|---|---|---|
| 0.0-0.1 | 265 | 2 | 0.05 | 0.000 | -0.050 |
| 0.1-0.2 | 13 | 2 | 0.15 | 0.000 | -0.150 |
| 0.2-0.3 | 16 | 2 | 0.25 | 0.000 | -0.250 |
| 0.3-0.4 | 8 | 2 | 0.35 | 0.000 | -0.350 |
| 0.4-0.5 | 13 | 2 | 0.45 | 0.000 | -0.450 |
| 0.5-0.6 | 14 | 2 | 0.55 | 0.000 | -0.550 |
| 0.6-0.7 | 16 | 2 | 0.65 | 0.000 | -0.650 |
| 0.7-0.8 | 18 | 2 | 0.75 | 0.444 | -0.306 |
| 0.8-0.9 | 24 | 2 | 0.85 | 0.583 | -0.267 |
| 0.9-1.0 | 42 | 2 | 0.95 | 0.690 | -0.260 |

**`KXAAAGASW` (weekly)** — 10 settled week-ends, the larger sample

| model P(YES) decile | n brackets | n distinct settlements | decile midpoint | realized YES rate | gap |
|---|---|---|---|---|---|
| 0.0-0.1 | 454 | 10 | 0.05 | 0.020 | -0.030 |
| 0.1-0.2 | 75 | 10 | 0.15 | 0.093 | -0.057 |
| 0.2-0.3 | 66 | 10 | 0.25 | 0.106 | -0.144 |
| 0.3-0.4 | 61 | 10 | 0.35 | 0.164 | -0.186 |
| 0.4-0.5 | 53 | 10 | 0.45 | 0.226 | -0.224 |
| 0.5-0.6 | 59 | 10 | 0.55 | 0.322 | -0.228 |
| 0.6-0.7 | 67 | 10 | 0.65 | 0.358 | -0.292 |
| 0.7-0.8 | 62 | 10 | 0.75 | 0.500 | -0.250 |
| 0.8-0.9 | 69 | 10 | 0.85 | 0.580 | -0.270 |
| 0.9-1.0 | 47 | 10 | 0.95 | 0.766 | -0.184 |

**Read the `n distinct settlements` column before the `n brackets` column.** Every bracket on one ladder resolves against a single AAA publication, so the brackets inside a decile are not independent draws and the per-decile n overstates the evidence badly. What clustering does *not* explain is the direction: the gap is one-sided across seven consecutive deciles, which is a specification defect rather than sampling noise.

### 3.6 Model versus market as forecasters

The cleanest statement this sample supports, because it needs no fee model, no fill model and no EV. Over the same settled brackets, which of the two forecasters was closer to the outcome? Brier score, computed per settlement event and then averaged so the unit is the event; lower is better.

| series | n brackets | n settlements | Brier model | Brier market mid | model - market | SE | t | settlements model won |
|---|---|---|---|---|---|---|---|---|
| `KXAAAGASM` (monthly) | 429 | 2 | 0.0938 | 0.0506 | +0.0432 | 0.0101 | 4.30 | 0/2 |
| `KXAAAGASW` (weekly) | 1013 | 10 | 0.1332 | 0.0775 | +0.0557 | 0.0141 | 3.94 | 0/10 |

A positive `model - market` means the market forecast the settlement better. This is the finding a longer AAA backfill cannot fix on its own: the strategy's premise is that the model knows something the price does not, and over the retrievable history the reverse holds.

---

## 4. EV per bracket-distance band

`n cand` counts every priced snapshot, including those the strategy's gates rejected and those where the required side of the book was absent. Band = `|floor_strike - projection point|` in cents/gal, edges [0, 1, 2, 3, 5, 8]c. All rows: walk-forward projection, C = 5, **1c adverse-fill allowance applied to every cell**, fee charged on the price actually paid, one leg (settlement is free). `realized/ct` is the settlement-true PnL of the same trades. Clustering unit for `SE`: the individual trade — these standard errors are optimistically small because brackets on one ladder share one settlement, and they are **not** comparable with the per-event numbers in §5.

### 4.1 `KXAAAGASM` — the FR-4.3 target, maker fees billed

**buy YES / taker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 33 | 33 | 100.0% | 33 | 100.0% | 0.5048 | 0.4212 | 0.4312 | 1.261c | +6.10c | -30.11c | 6.00c | 21 |
| 1-2c | 38 | 38 | 100.0% | 38 | 100.0% | 0.5160 | 0.4200 | 0.4300 | 1.063c | +7.54c | -21.70c | 4.72c | 25 |
| 2-3c | 32 | 29 | 90.6% | 29 | 90.6% | 0.4781 | 0.3490 | 0.3590 | 1.076c | +10.83c | -13.67c | 6.80c | 18 |
| 3-5c | 64 | 56 | 87.5% | 56 | 87.5% | 0.4711 | 0.4098 | 0.4198 | 0.986c | +4.14c | -16.10c | 4.54c | 35 |
| 5-8c | 72 | 58 | 80.6% | 58 | 80.6% | 0.5353 | 0.4778 | 0.4878 | 0.900c | +3.86c | -12.13c | 4.62c | 30 |
| 8c+ | 813 | 385 | 47.4% | 385 | 47.4% | 0.1367 | 0.1482 | 0.1582 | 0.450c | -2.59c | -6.33c | 1.10c | 300 |

**buy YES / maker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 33 | 33 | 100.0% | 30 | 90.9% | 0.4969 | 0.3540 | 0.3640 | 0.360c | +12.93c | -26.81c | 5.80c | 19 |
| 1-2c | 38 | 37 | 97.4% | 34 | 89.5% | 0.5324 | 0.3906 | 0.4006 | 0.365c | +12.81c | -19.52c | 4.25c | 22 |
| 2-3c | 32 | 28 | 87.5% | 23 | 71.9% | 0.5533 | 0.3583 | 0.3683 | 0.383c | +18.12c | -13.14c | 7.81c | 13 |
| 3-5c | 64 | 53 | 82.8% | 44 | 68.8% | 0.5604 | 0.4786 | 0.4886 | 0.373c | +6.81c | -16.25c | 6.10c | 25 |
| 5-8c | 72 | 59 | 81.9% | 49 | 68.1% | 0.6120 | 0.5329 | 0.5429 | 0.327c | +6.59c | -10.68c | 4.73c | 26 |
| 8c+ | 813 | 224 | 27.6% | 156 | 19.2% | 0.3924 | 0.3817 | 0.3917 | 0.276c | -0.21c | -12.20c | 3.22c | 99 |

**buy NO / taker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 33 | 32 | 97.0% | 32 | 97.0% | 0.4936 | 0.6284 | 0.6384 | 1.181c | -15.67c | +20.46c | 6.26c | 20 |
| 1-2c | 38 | 32 | 84.2% | 32 | 84.2% | 0.4326 | 0.5544 | 0.5644 | 1.150c | -14.33c | +17.11c | 4.59c | 20 |
| 2-3c | 32 | 25 | 78.1% | 25 | 78.1% | 0.3926 | 0.5388 | 0.5488 | 1.168c | -16.78c | +5.37c | 9.09c | 13 |
| 3-5c | 64 | 50 | 78.1% | 50 | 78.1% | 0.3596 | 0.4266 | 0.4366 | 1.108c | -8.81c | +9.39c | 5.42c | 29 |
| 5-8c | 72 | 62 | 86.1% | 62 | 86.1% | 0.3054 | 0.3668 | 0.3768 | 0.906c | -8.04c | +1.89c | 4.11c | 34 |
| 8c+ | 813 | 537 | 66.1% | 537 | 66.1% | 0.1116 | 0.1226 | 0.1326 | 0.378c | -2.48c | +0.81c | 1.28c | 266 |

**buy NO / maker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 33 | 33 | 100.0% | 25 | 75.8% | 0.4949 | 0.5608 | 0.5708 | 0.360c | -7.95c | +26.15c | 7.47c | 16 |
| 1-2c | 38 | 38 | 100.0% | 27 | 71.1% | 0.4565 | 0.5844 | 0.5944 | 0.363c | -14.16c | +19.28c | 5.28c | 19 |
| 2-3c | 32 | 27 | 84.4% | 21 | 65.6% | 0.4866 | 0.6476 | 0.6576 | 0.381c | -17.48c | +10.98c | 10.43c | 11 |
| 3-5c | 64 | 51 | 79.7% | 42 | 65.6% | 0.4271 | 0.5112 | 0.5212 | 0.367c | -9.77c | +17.92c | 5.98c | 25 |
| 5-8c | 72 | 56 | 77.8% | 48 | 66.7% | 0.3806 | 0.4592 | 0.4692 | 0.338c | -9.19c | +8.99c | 4.71c | 29 |
| 8c+ | 813 | 212 | 26.1% | 135 | 16.6% | 0.4744 | 0.4696 | 0.4796 | 0.293c | -0.81c | +12.54c | 4.10c | 80 |

### 4.2 `KXAAAGASW` — same shape, standard fee schedule, 11 week-ends (10 settled)

**buy YES / taker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 169 | 157 | 92.9% | 157 | 92.9% | 0.4876 | 0.3908 | 0.4008 | 0.976c | +7.71c | -14.36c | 2.49c | 153 |
| 1-2c | 145 | 128 | 88.3% | 128 | 88.3% | 0.4573 | 0.3313 | 0.3413 | 0.912c | +10.69c | -13.19c | 2.51c | 124 |
| 2-3c | 111 | 97 | 87.4% | 97 | 87.4% | 0.4809 | 0.3895 | 0.3995 | 0.915c | +7.23c | -17.43c | 3.54c | 93 |
| 3-5c | 196 | 155 | 79.1% | 155 | 79.1% | 0.4065 | 0.3908 | 0.4008 | 0.711c | -0.14c | -13.08c | 2.58c | 148 |
| 5-8c | 238 | 164 | 68.9% | 164 | 68.9% | 0.2752 | 0.2820 | 0.2920 | 0.607c | -2.28c | -6.98c | 2.02c | 155 |
| 8c+ | 674 | 377 | 55.9% | 377 | 55.9% | 0.1106 | 0.1345 | 0.1445 | 0.481c | -3.87c | -5.26c | 0.95c | 340 |

**buy YES / maker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 169 | 141 | 83.4% | 117 | 69.2% | 0.5037 | 0.3673 | 0.3773 | 0.000c | +12.64c | -13.29c | 2.89c | 115 |
| 1-2c | 145 | 115 | 79.3% | 99 | 68.3% | 0.5148 | 0.3424 | 0.3524 | 0.000c | +16.24c | -10.26c | 2.80c | 97 |
| 2-3c | 111 | 81 | 73.0% | 62 | 55.9% | 0.5880 | 0.4142 | 0.4242 | 0.000c | +16.38c | -21.38c | 4.52c | 61 |
| 3-5c | 196 | 121 | 61.7% | 91 | 46.4% | 0.5615 | 0.5171 | 0.5271 | 0.000c | +3.43c | -17.55c | 3.59c | 87 |
| 5-8c | 238 | 111 | 46.6% | 81 | 34.0% | 0.4675 | 0.4354 | 0.4454 | 0.000c | +2.20c | -10.82c | 3.42c | 76 |
| 8c+ | 674 | 185 | 27.4% | 120 | 17.8% | 0.3241 | 0.3394 | 0.3494 | 0.000c | -2.53c | -7.12c | 2.59c | 107 |

**buy NO / taker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 169 | 136 | 80.5% | 136 | 80.5% | 0.4803 | 0.5271 | 0.5371 | 1.054c | -6.74c | +5.46c | 2.83c | 133 |
| 1-2c | 145 | 111 | 76.6% | 111 | 76.6% | 0.4307 | 0.5350 | 0.5450 | 0.910c | -12.33c | +4.19c | 2.68c | 108 |
| 2-3c | 111 | 86 | 77.5% | 86 | 77.5% | 0.3586 | 0.4785 | 0.4885 | 0.977c | -13.97c | +7.45c | 4.28c | 82 |
| 3-5c | 196 | 140 | 71.4% | 140 | 71.4% | 0.3131 | 0.3328 | 0.3428 | 0.806c | -3.77c | +4.87c | 2.76c | 134 |
| 5-8c | 238 | 162 | 68.1% | 162 | 68.1% | 0.2992 | 0.3163 | 0.3263 | 0.617c | -3.32c | -1.57c | 2.17c | 154 |
| 8c+ | 674 | 406 | 60.2% | 406 | 60.2% | 0.1735 | 0.1856 | 0.1956 | 0.426c | -2.63c | -1.97c | 0.83c | 387 |

**buy NO / maker**

| band | n cand | n exec | exec frac | n fill | fill frac | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | SE | n settled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0-1c | 169 | 146 | 86.4% | 109 | 64.5% | 0.5009 | 0.5055 | 0.5155 | 0.000c | -1.46c | +10.48c | 3.17c | 108 |
| 1-2c | 145 | 116 | 80.0% | 83 | 57.2% | 0.4607 | 0.5517 | 0.5617 | 0.000c | -10.10c | +8.89c | 3.29c | 83 |
| 2-3c | 111 | 83 | 74.8% | 63 | 56.8% | 0.3838 | 0.4695 | 0.4795 | 0.000c | -9.57c | +15.62c | 4.86c | 60 |
| 3-5c | 196 | 125 | 63.8% | 93 | 47.4% | 0.4166 | 0.3945 | 0.4045 | 0.000c | +1.20c | +9.26c | 3.58c | 90 |
| 5-8c | 238 | 125 | 52.5% | 83 | 34.9% | 0.4223 | 0.3886 | 0.3986 | 0.000c | +2.38c | +2.69c | 3.52c | 81 |
| 8c+ | 674 | 233 | 34.6% | 116 | 17.2% | 0.5346 | 0.5075 | 0.5175 | 0.000c | +1.71c | +4.48c | 3.13c | 102 |

### 4.3 Quote availability is the binding constraint

Phase 2's central finding for weather was that the book's *availability*, not its spread, decides what is tradable. The same holds here, more sharply: most gas strikes are quoted one-sidedly for most of their life.

**`KXAAAGASM`**

| required side | n cand | n present | fraction |
|---|---|---|---|
| YES_taker | 1052 | 599 | 56.9% |
| YES_maker | 1052 | 434 | 41.3% |
| NO_taker | 1052 | 738 | 70.2% |
| NO_maker | 1052 | 417 | 39.6% |
| two-sided book | 1052 | 362 | 34.4% |

YES spread where both sides quoted (n = 362): median 2.0pt, p90 8.0pt, max 33.0pt.

**`KXAAAGASW`**

| required side | n cand | n present | fraction |
|---|---|---|---|
| YES_taker | 1533 | 1078 | 70.3% |
| YES_maker | 1533 | 754 | 49.2% |
| NO_taker | 1533 | 1041 | 67.9% |
| NO_maker | 1533 | 828 | 54.0% |
| two-sided book | 1533 | 677 | 44.2% |

YES spread where both sides quoted (n = 677): median 3.0pt, p90 15.0pt, max 77.0pt.

An EV computed on a quote that was not there is fiction. Every EV cell above is restricted to snapshots where the required side of the book existed, and the excluded count is printed rather than absorbed.

---

## 5. The strategy's own accepted shape

Only what `GasConvergenceStrategy.analyze()` accepted: inside the 14-day window, `|P(YES) - market| >= 8`pt, both fee legs' EV clearing zero on the raw quote, AAA data <= 2 days old. `EV/ct (+1c)` is the same trade with the adverse-fill allowance; `EV/ct (no allowance)` is the number the live gate itself computes.

**`KXAAAGASM` (monthly)**

| fee leg | n accepted | n filled | dates | markets | events (incl. unsettled) | mean P(win) | mean price+1c | EV/ct (no allowance) | EV/ct (+1c) | realized/ct | SE | n settled | win rate | sides |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| taker | 198 | 198 | 41 | 49 | 3 | 0.6295 | 0.4026 | +22.43c | +21.43c | -9.79c | 3.32c | 120 | 33.3% | {"NO": 80, "YES": 118} |
| maker | 198 | 175 | 40 | 46 | 3 | 0.6170 | 0.3423 | +28.08c | +27.09c | -6.99c | 3.44c | 107 | 27.1% | {"NO": 69, "YES": 106} |

**`KXAAAGASW` (weekly)**

| fee leg | n accepted | n filled | dates | markets | events (incl. unsettled) | mean P(win) | mean price+1c | EV/ct (no allowance) | EV/ct (+1c) | realized/ct | SE | n settled | win rate | sides |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| taker | 376 | 376 | 71 | 183 | 11 | 0.6011 | 0.3403 | +25.93c | +24.91c | -3.69c | 2.00c | 351 | 31.1% | {"NO": 166, "YES": 210} |
| maker | 376 | 331 | 70 | 167 | 11 | 0.5908 | 0.2641 | +33.67c | +32.67c | -0.86c | 1.94c | 316 | 25.0% | {"NO": 146, "YES": 185} |

### 5.1 By lead time — does it work where the model is sharp?

The obvious question a red-team asks: at a 1-day lead the projection's sigma is $0.0120/gal against a $0.01 strike spacing, so the model is genuinely informative there. Does the shape work in the short part of the window? Accepted taker trades bucketed by days to settlement, realized clustered on the settlement event within each bucket.

| lead | n M | EV/ct M | realized/ct M | sign | settlements M | n W | EV/ct W | realized/ct W | sign | settlements W |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 d | 3 | +16.54c | -1.50c | **-** | 2 | 30 | +30.25c | -13.17c | **-** | 9 |
| 2-3 d | 30 | +38.34c | +0.46c | **+** | 2 | 98 | +26.46c | -7.57c | **-** | 10 |
| 4-7 d | 53 | +18.89c | -33.45c | **-** | 2 | 248 | +23.65c | +2.58c | **+** | 10 |
| 8-14 d | 112 | +18.23c | -1.62c | **-** | 2 | 0 | n/a | n/a | n/a | 0 |

7 buckets carry data and 2 of them is positive: 2-3 d M at +0.46c on 30 trades, 4-7 d W at +2.58c on 248 trades. A cell within a cent of zero on a handful of settlements is not evidence of an edge, and it is reported here rather than dropped so nobody has to discover it in the JSON. The result that matters is the other way round: the **1-day bucket, where the projection is sharpest**, is the **worst** weekly bucket at -13.17c. That is the signature of the model disagreeing confidently with a market that has already priced a near-settled outcome correctly — not of a model that needs a shorter horizon. Shortening the FR-4.3 window is therefore not the fix, and §8 confirms it: a 3-day window is worse than a 14-day one.

### 5.2 The same trades, clustered on the settlement event

The table above clusters on the trade, which is the wrong unit and the flattering one. Below, each settlement's accepted trades are averaged first and the interval is taken across settlements. This is the number §0 uses.

| series / leg | trades | settlements | modelled EV/ct | realized/ct | SE | t vs 0 | 95% CI | modelled EV inside CI? | settlements negative |
|---|---|---|---|---|---|---|---|---|---|
| `KXAAAGASM` taker | 120 | 2 | +21.43c | -9.88c | 1.10c | -8.94 | [-23.91c, +4.16c] | **NO** | 2/2 |
| `KXAAAGASM` maker | 107 | 2 | +27.09c | -6.99c | 0.08c | -85.65 | [-8.03c, -5.95c] | **NO** | 2/2 |
| `KXAAAGASW` taker | 351 | 10 | +24.91c | -1.78c | 3.37c | -0.53 | [-9.41c, +5.85c] | **NO** | 5/10 |
| `KXAAAGASW` maker | 316 | 10 | +32.67c | +0.46c | 3.24c | 0.14 | [-6.87c, +7.80c] | **NO** | 6/10 |

Per-settlement realized mean, weekly taker (cents/contract):

| settlement | realized/ct |
|---|---|
| 2026-05-25 | +0.7c |
| 2026-06-01 | -18.0c |
| 2026-06-08 | +3.7c |
| 2026-06-15 | +10.6c |
| 2026-06-22 | -9.0c |
| 2026-06-29 | -14.6c |
| 2026-07-06 | -3.2c |
| 2026-07-13 | +14.7c |
| 2026-07-20 | -7.8c |
| 2026-07-27 | +5.0c |

### 5.3 Rejection reason codes

Contract §3 requires every rejection to be reconstructible from the logs alone. These counts are read from the strategy's own `log_rejection` channel during the replay, which is also a check on that requirement.

| reason code | `KXAAAGASM` | `KXAAAGASW` |
|---|---|---|
| `GAS_DIVERGENCE_BELOW_MIN` | 104 | 257 |
| `GAS_EV_NOT_POSITIVE` | 2 | 13 |
| `GAS_NEAR_RESOLVED` | 742 | 863 |
| `GAS_NO_USABLE_QUOTE` | 6 | 24 |

---

## 6. The robustness test that decides the verdict

Phase 2 HALTed weather because its EV flipped sign between two forecast sources while the gate ranked the loser higher. The same discipline is applied here: the headline is recomputed under each perturbation and the signs are tabulated. `M` = `KXAAAGASM`, `W` = `KXAAAGASW`.

| perturbation | n trades M | EV/ct M | sign | realized/ct M | sign | trades/settlements M | EV/ct W | realized/ct W | sign | trades/settlements W | t (EV-realized) W | daily MAE (all leads) | daily bias (all leads) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| headline (LA RBOB, EIA off, suspect excluded, AAA truth) | 198 | +21.43c | **+** | -9.88c | **-** | 120/2 | +24.91c | -1.78c | **-** | 351/10 | 7.9 | 0.0397 | -0.0018 |
| RBOB source = ny_harbor_conventional_spot | 161 | +20.01c | **+** | -15.35c | **-** | 110/2 | +24.65c | -7.29c | **-** | 320/10 | 6.7 | 0.0390 | -0.0016 |
| RBOB source = gulf_coast_conventional_spot | 202 | +21.17c | **+** | -11.00c | **-** | 122/2 | +25.30c | -0.76c | **-** | 361/10 | 7.1 | 0.0397 | -0.0014 |
| EIA weekly covariate ON | 198 | +21.28c | **+** | -10.42c | **-** | 119/2 | +24.90c | -2.58c | **-** | 351/10 | 8.9 | 0.0397 | -0.0019 |
| suspect AAA rows INCLUDED | 196 | +21.63c | **+** | -10.19c | **-** | 119/2 | +24.95c | -2.22c | **-** | 348/10 | 8.1 | 0.0396 | -0.0018 |
| truth channel = Kalshi-pinned (source-independent) | 198 | +21.43c | **+** | -9.88c | **-** | 120/2 | +24.91c | -1.78c | **-** | 351/10 | 7.9 | 0.0723 | +0.0205 |

### 6.1 RBOB source

WS-A defaulted to `PET.EER_EPMRR_PF4_Y05LA_DPG.D` — **Los Angeles** RBOB spot — because every NY Harbor *RBOB* series in EIA's bulk archive is a futures series that ends 2024-04-05. LA is a CARB-specific benchmark and an imperfect national proxy. `RBOB_ALTERNATIVES` exposes NY Harbor and Gulf Coast **conventional** spot; all three were re-fetched by this workstream from the same archive over an identical window so the comparison is not confounded by a different start date per series:

| alias | EIA series id | rows | coverage | EV/ct M taker | realized/ct M (event-clustered) |
|---|---|---|---|---|---|
| la_rbob_spot | `PET.EER_EPMRR_PF4_Y05LA_DPG.D` | 1541 | 2020-06-01 .. 2026-07-27 | +21.43c | -9.88c |
| ny_harbor_conventional_spot | `PET.EER_EPMRU_PF4_Y35NY_DPG.D` | 1542 | 2020-06-01 .. 2026-07-27 | +20.01c | -15.35c |
| gulf_coast_conventional_spot | `PET.EER_EPMRU_PF4_RGC_DPG.D` | 1542 | 2020-06-01 .. 2026-07-27 | +21.17c | -11.00c |

Only `rbob_daily.csv` is consumed from those directories. Each also contains an `eia_weekly_regular.csv` written as a byproduct by `backfill_covariates`; the EIA series actually used is WS-A's committed copy in `data/gas_truth/`, and the three byproduct copies are identical to each other and to nothing this report reads.

### 6.2 EIA covariate on/off

WS-C defaults the EIA weekly retail series off: it measures the same quantity as AAA at one seventh the frequency, so its trailing drift is near-collinear with the AAA momentum term. The `eia:on` row above is that decision tested rather than assumed.

### 6.3 `suspect` rows in/out

56 of the AAA rows carry `quality=suspect` (a parse that moved more than $0.15/day against its neighbours, or landed outside [1.00, 9.00]). They are excluded by default; the `suspect:included` row is the same analysis with them in.

### 6.4 Truth channel

AAA versus Kalshi-pinned, per §3.3. For the **EV** tables the realized outcome already comes from Kalshi's `result` and never from AAA, so the EV columns are unchanged by this axis by construction — the axis moves the MAE columns only, and it is reported for the MAE.

---

## 7. One worked example, recomputable by hand

* **market** `KXAAAGASM-26MAY31-4.65` — `strike_type=greater`, `floor_strike=4.65`, settlement date 2026-05-31, event `KXAAAGASM-26MAY31`
* **decision snapshot** 2026-05-22 at the 18:00 ET hourly candle close, lead 9 days
* **projection** (`lagdrift_v1+rbobL2`, `n_train=1585`, `inputs_hash=6504b99e482123da`): point = $4.583893, sigma = $0.094597 (printed to six places so the recompute below is exact, not within-rounding)

**Step 1 — strict-greater probability.** AAA publishes to three decimals, so `> K` is `>= K + $0.001`; the half-tick continuity correction puts the threshold at `K + $0.0005`:

```
z      = (K + 0.0005 - point) / sigma
       = (4.6500 + 0.0005 - 4.583893) / 0.094597
       = +0.704111
P(YES) = 1 - Phi(z) = 0.5 * erfc(z / sqrt(2)) = 0.240682
```

**Step 2 — divergence.** Market YES reference 0.0250 (`mid`); divergence = 0.2407 - 0.0250 = +0.2157 (21.57pt), which clears the 8pt gate, so the model prefers **YES** and `P(win)` = 0.240682.

**Step 3 — price paid.** The executable YES offer quoted 0.0400; with the 1c adverse-fill allowance the price paid is 0.0400 + 0.01 = **0.0500**.

**Step 4 — fee.** `KXAAAGASM-26MAY31-4.65` is series `KXAAAGASM`, whose live `/series` metadata reports `fee_type = quadratic_with_maker_fees`, so **both** legs are billed. At C = 5 contracts:

```
taker raw = 0.07   * C * P * (1-P) = 0.07   * 5 * 0.0500 * 0.9500 = $0.016625
          -> ceil to cent = $0.02 total, $0.004000/contract
maker raw = 0.0175 * C * P * (1-P) = 0.0175 * 5 * 0.0500 * 0.9500 = $0.004156
          -> ceil to cent = $0.01 total, $0.002000/contract
```

The published *rate* ratio is 25% (0.0175 / 0.07). The *charged* ratio here is 50% at C = 5, and at C = 1 it is 100% ($0.01 vs $0.01) because each leg is ceil'd to the cent independently. That is why no fee in this report is ever scaled from the other.

**Step 5 — EV and outcome.**

```
EV/ct       = P(win) - price_paid - fee/ct
            = 0.240682 - 0.0500 - 0.004000 = +0.186682  (+18.67c)
settled     : AAA published $4.336 on 2026-05-31; 4.336 > 4.65 is FALSE, so YES LOST
realized/ct = 0 - 0.0500 - 0.004000 = -0.054000  (-5.40c)
```

This trade is the **median accepted trade by modelled EV**, chosen deterministically so it is neither the best nor the worst. It lost, and one trade proves nothing either way — it is here so the arithmetic of every cell in §4 and §5 can be checked without rerunning the script. The aggregate is in §5.1.

**The fee ratio across prices, at the two order sizes that matter.** Computed by `compute_fee` on `KXAAAGASM`'s schedule, so the "maker is 25% of taker" shortcut can be seen failing rather than asserted:

| price P | taker C=1 | maker C=1 | maker/taker C=1 | taker C=5 | maker C=5 | maker/taker C=5 |
|---|---|---|---|---|---|---|
| 0.05 | $0.01 | $0.01 | 100% | $0.02 | $0.01 | 50% |
| 0.10 | $0.01 | $0.01 | 100% | $0.04 | $0.01 | 25% |
| 0.25 | $0.02 | $0.01 | 50% | $0.07 | $0.02 | 29% |
| 0.50 | $0.02 | $0.01 | 50% | $0.09 | $0.03 | 33% |
| 0.75 | $0.02 | $0.01 | 50% | $0.07 | $0.02 | 29% |
| 0.90 | $0.01 | $0.01 | 100% | $0.04 | $0.01 | 25% |

The rate ratio is 25% everywhere. The charged ratio equals it in 2 of the 12 cells above and reaches 100% elsewhere. FR-4.3 says "sized small", which places this bot in exactly the regime where the shortcut is most wrong.

---

## 8. Sensitivities

Each row is the headline **re-run** with one knob moved, not an assertion about what would happen. All passes share one projection cache: none of these knobs enters the regression, so refitting for each would be redundant work. The `leg scored` column is the taker leg except for the maker-fill rows, where the maker leg is the one the knob affects.

| knob | variant | leg scored | n trades M | EV/ct M | realized/ct M | sign | n trades W | EV/ct W | realized/ct W | sign |
|---|---|---|---|---|---|---|---|---|---|---|
| order size C | 5 (headline) | taker | 198 | +21.43c | -9.88c | **-** | 376 | +24.91c | -1.78c | **-** |
| order size C | 1 | taker | 197 | +21.19c | -10.54c | **-** | 375 | +24.60c | -2.19c | **-** |
| order size C | 20 | taker | 198 | +21.50c | -9.81c | **-** | 376 | +24.98c | -1.71c | **-** |
| decision hour ET | 12:00 | taker | 171 | +19.53c | -12.67c | **-** | 352 | +23.08c | -1.18c | **-** |
| decision hour ET | 18:00 (headline) | taker | 198 | +21.43c | -9.88c | **-** | 376 | +24.91c | -1.78c | **-** |
| decision hour ET | 23:00 | taker | 201 | +22.11c | -13.51c | **-** | 367 | +24.90c | -2.96c | **-** |
| adverse-fill allowance | 0c | taker | 198 | +22.43c | -8.88c | **-** | 376 | +25.93c | -0.76c | **-** |
| adverse-fill allowance | 1c (headline) | taker | 198 | +21.43c | -9.88c | **-** | 376 | +24.91c | -1.78c | **-** |
| adverse-fill allowance | 2c | taker | 198 | +20.41c | -10.89c | **-** | 376 | +23.89c | -2.80c | **-** |
| adverse-fill allowance | 3c | taker | 198 | +19.39c | -11.92c | **-** | 376 | +22.86c | -3.83c | **-** |
| maker fill rule | candle high/low traversal (headline) | maker | 175 | +27.09c | -6.99c | **-** | 331 | +32.67c | +0.46c | **+** |
| maker fill rule | candle close traversal only | maker | 168 | +27.40c | -7.95c | **-** | 306 | +32.88c | -2.24c | **-** |
| divergence gate | 8pt (headline) | taker | 198 | +21.43c | -9.88c | **-** | 376 | +24.91c | -1.78c | **-** |
| divergence gate | 15pt | taker | 148 | +26.01c | -15.00c | **-** | 278 | +31.18c | -5.16c | **-** |
| divergence gate | 25pt | taker | 82 | +34.58c | -17.42c | **-** | 205 | +37.47c | -7.62c | **-** |
| FR-4.3 window | 14 d (headline) | taker | 198 | +21.43c | -9.88c | **-** | 376 | +24.91c | -1.78c | **-** |
| FR-4.3 window | 7 d | taker | 86 | +25.59c | -17.31c | **-** | 376 | +24.91c | -1.78c | **-** |
| FR-4.3 window | 3 d | taker | 33 | +36.36c | +0.24c | **+** | 128 | +27.35c | -9.38c | **-** |

**Weekly realized sign across all 18 variants:** positive in maker fill rule = candle high/low traversal (headline), which must be read as a warning that the result is not robust to that knob. The monthly column moves far more, which is what a 2-settlement sample does; it is printed for completeness and carries little weight.

---

## 9. Recommendation

**`GAS_TRADING_ENABLED` stays `False`.** `src/bots/gas_bot.py` is WS-C's file and this workstream does not touch it; this is a recommendation to the orchestrator, not an action.

What would change the answer, pre-registered now so a later positive result cannot be produced by moving the target. The list is assembled from this run's measurements, so an item the inputs have already satisfied does not appear:

1. **A calibrated probability, not a raw OLS prediction interval.** §3.5 shows the realized YES rate below the model's probability across the whole middle of the distribution, and §3.6 shows the market beating the model outright. Until `prob_above` is calibrated against held-out outcomes — the same treatment Phase 2 gave weather σ — every divergence this strategy measures is the model's own error. This is the item the other three depend on.
2. **A divergence threshold expressed in sigma, not in points.** At a 14-day lead the model's sigma is $0.1422/gal against a $0.01 strike spacing, so the model's `P(YES)` moves only about 2.8pt per strike while the market's price moves far faster. An 8pt gate therefore passes 200 of the 304 monthly snapshots that reach it (66%) — it is not selecting rare disagreements, it is passing much of the ladder. §8 shows tightening it does not rescue the sign; it makes the outcome worse.
3. **Recorded gas ladders of its own.** The Phase 0 harvester records `KXHIGH*` only. Pointing it at `KXAAAGASM`/`KXAAAGASW` would remove this artifact's dependence on an endpoint that prunes history after about two months, and would close §10.2 and §10.3 over time. It is cheap and it is the only item on this list that the AAA backfill cannot address.

---

## 10. Deferral register

Per `register-deferred-evidence-not-waived`, this is one dated register that closes item by item, not a list of complaints. An item the inputs to *this* run have satisfied is recorded **CLOSED with the evidence that closed it** rather than deleted: deleting it would make the register under-report completion, and a reader reconciling §1 against §10 could not tell which was authoritative. Nothing here is waived and nothing is replaced by a proxy number presented as the real thing. **3 open, 1 closed.**

| # | status | item | why it could not be obtained / what closed it | what would close it |
|---|---|---|---|---|
| 10.1 | **CLOSED** | month-end projection MAE on >= 6 held-out month-ends | **37 month-ends held out** on the AAA span 2022-01-01 .. 2026-07-29 (1494 usable rows). Reported in §3.1; the clause is MET in §1. | — |
| 10.2 | OPEN | realized PnL on more than 2 settled `KXAAAGASM` settlement(s) | Kalshi prunes settled markets from the public API after roughly two months, so only 3 monthly event(s) are retrievable at all and only 2 of them had settled when the tape was fetched. **A longer AAA backfill does not fix this** — the missing data is exchange-side market history, not price history. | record `KXAAAGASM` ladders live from now on. Meanwhile the weekly series supplies 10 settled week-ends as evidence about the *shape*, reported separately in §4.2/§5.2 and never pooled into the monthly headline. |
| 10.3 | OPEN | intra-hour quote path and resting-order queue position | the candlesticks endpoint is hourly, so any sub-hour traversal is invisible and queue position is unobservable at any resolution. The maker leg is therefore a bound, not a measurement, which is why §0's verdict is taken from the taker path. | record gas ladders live at the Phase 0 harvester's cadence |
| 10.4 | OPEN | an ex-ante argument for one RBOB benchmark over another | every NY Harbor *RBOB* series in EIA's bulk archive is a futures series ending 2024-04-05, so the national-benchmark comparison has to be made against conventional-spot series, which are a different product | not needed for this verdict — §6.1 recomputes the headline under all three and the realized sign does not depend on the choice |

---

*Generated 2026-07-30T05:04:20.927509+00:00 by `scripts/gas_backtest.py run`. Machine-readable companion: `reports/phase4/phase4_backtest_data_2026-07-30.json`.*
