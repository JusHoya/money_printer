# The cold-start sizing ceiling — quantified, with the options laid out

**Date:** 2026-09-05 · **Author:** F3 review follow-up, SIZING workstream · **Status:** analysis only,
no behaviour changed, no file outside `reports/factory/` touched.

**What this is.** The deployed genome emits signals the runtime cannot size. This document
reconstructs the size of that problem from the frozen frame, states what it does to the FR-5.2
paper gate, and lays out the options. **It takes no decision.** Every option that would actually
move the number touches `src/core/risk_manager.py`, which is protected by
`tests/test_protected_files.py` and can only be changed by owner ratification, exactly as the
NO-side settlement hunk was. The recommendation at the end is a recommendation, not an action.

---

## 1. The mechanism, exactly

`RiskManager.calculate_kelly_size` (`src/core/risk_manager.py`) deliberately refuses to size from raw
model confidence. Its docstring cites Bertsimas & Mundru 2024: *when model confidence is 0.95 but
actual WR is 0.45, full-Kelly sizing is catastrophic.* So it blends:

```
risk_manager.py:458   historical_wr = 0.50          # neutral prior until the window has data
risk_manager.py:464   if n_samples >= MIN_WIN_SAMPLES:   # MIN_WIN_SAMPLES = 20 (line 44)
risk_manager.py:467   p = 0.6 * historical_wr + 0.4 * confidence
risk_manager.py:469   f = p - (q / b)               # q = 1-p, b = net_win / net_loss
```

For a KXHIGH weather market the maker fee used at line 439 is **exactly 0.0** on the standard
schedule (verified: `compute_fee(price, 1, is_maker=True, series_fee_type='standard').per_contract
== 0.0` for every one of the genome's 130 trades). With zero fee, `b = (1-price)/price` and

> **f > 0  ⟺  p > price.**

At cold start `historical_wr` is pinned at 0.50, so

> **p = 0.30 + 0.40 · confidence ≤ 0.70 for any confidence in [0,1].**

**There is no caller-side workaround.** To reach a blended `p` of 0.80 the caller would have to pass
`confidence = 1.25`. The runtime therefore *cannot* buy anything above 70¢ until 20 closed trades
exist for that strategy — not "sizes it small", **sizes it zero**. `calculate_kelly_size` returns 0,
`mixins.py:455` drops the signal, and the only trace is one `KELLY_ZERO` INFO line.

The deployed genome `0c4b20502f2daf65` (`fr31a_taker`, far-bracket NO, taker, ≥12 h to close) buys at
a median price of **0.84**. The signal price is `row["price_paid"] = quote + 0.01`
(`genome_strategy.py:464`) and the confidence is the frame's `p_win` (`genome_strategy.py:465`).

**Live confirmation.** All four 2026-09-05T15:00:23Z EMITs re-run through the real `RiskManager` at the
sandbox's actual balance return 0 contracts:

| market | price | confidence (`p_win`) | blended `p` | Kelly qty |
|---|---|---|---|---|
| …-T76   | 0.92 | 0.9988 | 0.6995 | **0** |
| …-B76.5 | 0.92 | 0.9902 | 0.6961 | **0** |
| …-B88.5 | 0.86 | 0.9729 | 0.6892 | **0** |
| …-B78.5 | 0.74 | 0.9486 | 0.6794 | **0** |

`GET http://maia.local:8050/api/win_rates` (read-only, 2026-09-05T18:5x Z) returns only
`ML Weather` (3 samples) and `Meteorologist V2` (3 samples). **There is no win record for the genome
strategy at all** — the cold-start branch is what the sandbox is executing, confirmed live, not inferred.

---

## 2. Independent reconstruction

**Method.** Load the frozen search frame `data/factory/frames/weather_2026-07-25_bfcf94654a3a/search`,
rebuild the genome from `configs/factory/promoted/0c4b20502f2daf65.json`'s `genome_json`, take
`fitness.score(F, genome.to_mask(g, F), constraints=False).trade_rows` — the same construction
`scripts/factory_replay_parity.py` diffs the live path against — and drive each row's
`(price_paid, p_win, ticker)` through a real `RiskManager` instance with an empty win record.

**Reconstruction check.** `len(trade_rows) == 130`, matching the promoted spec's
`parity.n_offline == 130`. The reconstruction is right.

### 2.1 Figures reproduced

| quantity | prior analysis | **this reconstruction** | agrees |
|---|---|---|---|
| offline trades | 130 | **130** | ✅ |
| admitted by cold-start Kelly | 13 (10.0 %) | **13 (10.0 %)** | ✅ |
| target_dates spanned by admitted | 12 of 57 | **12 of 57** | ✅ |
| frame span | 69 days | **69 days** (2026-05-18…07-25) | ✅ |
| gate-units/day | 0.174 | **0.1739** (12/69) | ✅ |
| days to `n_min = 50` units | ~287 | **287.5** | ✅ |
| days to 20 closed trades | ~106 | **106.2** (13/69 trades/day) | ✅ |
| admitted price range / median | 0.38–0.68, 0.60 | **0.38–0.68, median 0.60** | ✅ |
| admitted win rate | 0.692 | **0.6923** (9/13) | ✅ |
| full-set win rate | 0.900 | **0.9000** (117/130) | ✅ |
| admitted realized/contract | +0.1155 | **+0.115500** | ✅ |
| full-set median price | 0.84 | **0.84** | ✅ |

### 2.2 The one figure that disagrees — flagged

The prior analysis reports the **full set** realizing **+0.0705/contract**. It does not.

- full set, per-trade mean: **+0.074985**
- full set, per-**date** mean (what `fitness.score` reports, and what the family was selected on): **+0.072287**
- **rejected** subset (the 117 the runtime cannot size), per-trade mean: **+0.070483**

+0.0705 is the **rejected** subset, not the full set. The direction of the comparison survives
(admitted +0.1155 > full +0.0750 > rejected +0.0705) but the labelled number was wrong. Use +0.0750
(per-trade) or +0.0723 (per-date, date-clustered — HANDOFF §3 rule 3 prefers this one).

### 2.3 Two facts the prior analysis did not establish

**The ceiling is balance-independent.** Re-running the admission at every bankroll stage:

| balance | stage | `trade_pct` | `kelly_frac` | admitted | qty median |
|---|---|---|---|---|---|
| 100 | Seed | 0.10 | 0.25 | **13/130** | 7 |
| 500 | Early | 0.05 | 0.25 | **13/130** | 38 |
| 2 873.12 (live) | Growth | 0.05 | 0.30 | **13/130** | **50 (cap)** |
| 10 000 | Scale | 0.05 | 0.35 | **13/130** | 50 |
| 60 000 | Compound | 0.025 | 0.25 | **13/130** | 50 |

Admission depends only on the sign of `f`, and the stage multiplies `f`. So this is **not** a
small-bankroll artifact and cannot be funded away. Note also that at the sandbox's real balance
(~$2 873, `GET /api/status`, not the $100 constructor default) the surviving 10 % size at the
`MAX_CONTRACTS = 50` cap. The behaviour is binary: **90 % of the shape gets 0 contracts, 10 % gets 50.**

**It is a price cut, not a confidence cut.** The admission threshold `0.30 + 0.40·p_win` ranges over
only `[0.6383, 0.7000]` across the 130 trades, so on the 1¢ price grid:

- price ≤ 0.63 → **always** admitted (10 trades)
- price ≥ 0.71 → **never** admitted (112 trades, 86.2 % of the shape)
- 0.64–0.70 → depends on `p_win` (8 trades, 3 admitted)

The genome's price histogram sits almost entirely above the wall: modes at 0.87–0.93 (40 of 130
trades). **45 of the 57 target_dates (78.9 %) have no fill the runtime could size at all.**

---

## 3. What the surviving 10 % actually is

It is a materially different strategy, and the fitness kernel says so:

| | trades | dates | realized/contract (date-clustered) | s.e. | t | boot 95 % | win rate | mean price |
|---|---|---|---|---|---|---|---|---|
| full genome | 130 | 57 | **+0.07229** | 0.0281 | +2.57 | [+0.0145, +0.1225] | 0.900 | 0.815 |
| cold-start admissible | 13 | 12 | **+0.17063** | 0.1279 | +1.33 | [−0.0816, +0.3881] | 0.692 | 0.560 |

The admissible subset *looks* better per contract and is **not statistically distinguishable from
zero**: its bootstrap lower bound is negative, its t is 1.33, and 12 clustered units is nothing. Its
apparent superiority is what you would expect from thirteen draws.

Worse, **the factory's own machinery would refuse to credit it.** The F2 run's competition set
(`reports/factory/run_2026-09-03b/summary.json` → `thresholds`) requires `min_trades = 40` and
`dates ≥ ceil(0.6 × 69) = 42`. The admissible sub-shape has **13 trades and 12 dates**. It fails both
feasibility criteria by more than 3×; it was never in the ledger, never Reality-Check corrected, and
could not have been. Whatever the runtime is executing, it is not something the factory scored.

### HANDOFF §3 rule 2 — beat the trivial baseline

Computed directly on the search frame, one entry per market, no genome mask:

| shape | trades | dates | realized/contract | t | boot 95 % |
|---|---|---|---|---|---|
| every NO taker row | 830 | 69 | **−0.02030** | −4.16 | [−0.0299, −0.0107] |
| NO taker & cold-start sizable | 500 | 69 | **−0.04796** | −3.83 | [−0.0727, −0.0247] |
| NO taker & price ≤ 0.70 | 568 | 69 | **−0.04739** | −5.65 | [−0.0639, −0.0316] |
| genome, restricted to sizable rows | 24* | 19 | +0.11476 | +1.10 | [−0.0863, +0.2998] |

\* 24, not 13 — see §5.

So on the cheap band the genome does beat an indiscriminate tail-sell, but **not significantly**
(boot_lo −0.086), and in-sample on dates PRD A3 declares already searched. The F2 run's own paired
comparison against its `nofilter_no` baseline is worse still: pooled delta **−0.0528**, boot
[−0.1910, +0.0734], `paired_vs_nofilter_lo_gt0: false`. The filter adds nothing there.

---

## 4. What this does to the FR-5.2 gate

`configs/factory/gate_registration.template.json`: `n_min = 50` units, `alpha = 0.05`,
`grouping_unit = target_date`, exact Poisson-binomial upper tail with per-fill breakeven
`q* = entry_price + entry_fee/contract`.

**Time to fifty units.** At 0.1739 units/day the point estimate is 287.5 days. Treating unit arrivals
as Poisson at that rate, the 95 % band on the waiting time is **≈ 208–367 days** — from 2026-09-05
that is **mid-2027**, and that is *before* any seasonal correction.

**Power, at the observed rates.** Running the gate's own exact test:

| shape | units | unit win rate | mean null `w` | p at n = 12/57 | p at n = 50 |
|---|---|---|---|---|---|
| cold-start admissible (at Kelly qty) | 12 | 0.750 (9/12) | 0.570 | 0.1624 | 0.0042 |
| full genome at the frame's C = 20 | 57 | 0.789 (45/57) | 0.683 | 0.0415 | **0.0472** |

Two things follow, and the second matters more than the first.

1. The cheap subset has a *lower* breakeven (0.57 vs 0.68), so per unit it is a more powerful test —
   if the rate held, 50 units would clear easily. But the rate is 9/12; Wilson 95 % CI **[0.468,
   0.911]**, which contains the null.
2. **Even the full, unfiltered genome barely clears the gate at n = 50 on its own in-sample rate**
   (p = 0.047 against alpha 0.05), and its unit-win-rate CI is **[0.667, 0.875]** against a null of
   **0.683** — i.e. the in-sample unit win rate is *not* significantly above breakeven. Any
   out-of-sample shrinkage at all and it fails. The gate was pre-registered against a shape with
   almost no margin.

**Is the gate still *valid* under the filter?** Yes, and I want to be precise rather than alarming.
The null is per-fill, derived from the realized entry price and fee, so the test is exact
*conditional on whichever fills occur*; and the selection rule (price ≤ 0.30 + 0.40·p_win, later
adapted to past outcomes) is predictable with respect to the past, so `E[won_i − q*_i | F_{i-1}] = 0`
still holds. A PASS would be a real PASS.

**What breaks is the pre-registration correspondence.** FR-5.2 condition 4 checks the *spec hash*,
not the executed shape. A PASS would therefore certify `0c4b20502f2daf65` while the record was
generated by a 13-trade sub-shape that fails the family's own feasibility floor and was never
multiplicity-corrected. **The gate freezes thresholds against a strategy the runtime cannot produce.**
That is the defect. It is a defect in the *promotion*, not in the RiskManager: the factory promoted a
shape the sandbox cannot express.

---

## 5. Two interactions the reconstruction turned up

**(a) `entries_per_market: 1` burns a market on EMIT, not on fill.** `genome_strategy.py:444` adds
`(target_date, symbol)` to `_traded` immediately after the signal is built, before the mixin ever
calls `calculate_kelly_size`. So a market whose first masked candle is Kelly-zeroed is **never
retried**, even if a later candle in the same market is cheap enough to size. If the strategy instead
waited for a sizable candle, the admissible set would be **24 trades over 19 dates** (0.275 units/day
→ ~182 days), not 13/12. That is the difference between 287 and 182 days, and it costs no
protected-file change — but it *would* break the FR-F3.4 replay parity (`n_discrepancies: 0` depends
on emitting at the first masked executable candle) and it lives in `src/strategies/genome_strategy.py`,
which this workstream does not own. **Flagged for coordination, not implemented.**

**(b) `MIN_TRADE_INTERVAL_SEC = 10` throttles same-tick fills.** All four live EMITs landed inside one
second. Combined with (a) this means at most ~1 fill per hourly decision tick survives to execution.
It reduces *fills per unit* toward 1; it does **not** reduce *units*, which is what the gate counts,
so every units/day figure above is robust to it. (`MAX_DAILY_TRADES = 40` and
`MAX_DAILY_DRAWDOWN_PCT = 0.50` are nowhere near binding at this cadence.)

---

## 6. The four options

Throughout: "admissible fraction" is of the genome's 130 offline trades; "days to 50" is
`50 / (units per day)` on the 69-day frame. Options 1–3 all edit **`src/core/risk_manager.py`**, which
`tests/test_protected_files.py` diffs against base commit `38d5fdd` and which permits exactly one
pre-approved hunk in `matching_engine.py`. Each would need the owner to ratify a new allowed hunk,
exactly as the NO-side settlement fix was ratified. None of them can be done by an agent.

---

### Option 1 — Seed the genome's win record from its parity trade set

**Mechanism.** Before the first live trade, write `data/strategy_win_rates.json` (or construct it in
`_load_win_rates`) with the genome's 130 offline outcomes. `WIN_RATE_WINDOW = 50` keeps the most
recent 50: win rate **0.9400**. Then `p = 0.564 + 0.4·confidence`, ceiling ≈ 0.94.

**Effect.** **130/130 admissible (100 %), 57 units, 0.826 units/day → ~61 days** (≈ 2026-11-05).
Kelly quantities 5–26 at $100, at the 50 cap at the live balance.

**Evidential cost — fatal.** This imports the search's own in-sample output into the live risk state
and lets it license position size. It is precisely HANDOFF §3's prohibition on letting a modelled
quantity stand in for a realized one, applied to the one number in the system whose entire purpose is
to be realized: `strategy_win_records` is *defined* as closed-trade outcomes. Worse, the 0.94 seed is
computed on the 69 dates PRD A3 declares already searched, so the sizing would be conditioned on the
same data that selected the genome. And it is not gate-neutral: fill quantity enters the unit PnL sum
and the per-fill fee, so seeded sizing changes which units win.

**Files.** `src/core/risk_manager.py` (**protected**) if done in code; alternatively a
pre-seeded `data/strategy_win_rates.json` shipped in the deploy bind — which requires no code change
at all and is *therefore more dangerous*, because it would silently do the same thing with no diff to
review. Note FR-0.6 exists specifically to stop win records from carrying history forward; this
inverts it.

**Pre-registration verdict: compromises it.** The registered strategy would be sized by a number
derived from the selection data. Do not do this.

---

### Option 2 — Lower `MIN_WIN_SAMPLES` for a pre-registered strategy

**Mechanism.** Let the realized win rate take over after fewer than 20 closes
(`risk_manager.py:44`, read at line 464).

**Effect — non-monotone and unstable.** Simulated chronologically, feeding each trade's outcome into
the record as its target_date settles:

| `MIN_WIN_SAMPLES` | fills admitted | units | units/day | days to 50 |
|---|---|---|---|---|
| 20 (today) | 13/130 | 12 | 0.174 | **288** |
| 10 | 29/130 | 23 | 0.333 | 150 |
| 5 | 48/130 | 30 | 0.435 | 115 |
| 3 | 89/130 | 49 | 0.710 | **70** |
| **1** | **2/130** | **2** | 0.029 | **1 725** |

The `= 1` row is not a bug in the simulation; it is the mechanism. The first cold-start-admissible
trade (2026-05-18 KXHIGHLAX-B71.5) **lost**. With one sample the win rate becomes 0.0, the ceiling
falls to `0.4·confidence ≤ 0.40`, nothing else is ever admissible, and the record — which is fed only
by admitted trades — can never grow. **It is an absorbing state**: exactly the poisoned-era deadlock
(`"ML BTC 15m": [30, 1048]`) that `WIN_RATE_WINDOW`/`MIN_WIN_SAMPLES` were written to prevent,
reintroduced at the low end.

This is not a tail risk. At the admissible subset's 0.692 win rate, the probability the first window
lands low enough to make the runtime *more* restrictive than the neutral prior is
**P(Bin(3, 0.692) ≤ 1) = 0.226** at `MIN_WIN_SAMPLES = 3`, **P(Bin(5, 0.692) ≤ 2) = 0.173** at 5, and
still **P(Bin(20, 0.692) ≤ 11) = 0.129** at 20. Roughly one run in five to one in eight stalls, and
the ceiling then random-walks on its own recent outcomes.

**Evidential cost.** The strategy actually executed becomes a function of the first handful of coin
flips. The gate's test survives this (adapted selection, §4), but "what passed" becomes
unreproducible: a rerun with different weather would execute a different strategy under the same spec
hash.

**Files.** `src/core/risk_manager.py` (**protected**), plus a per-strategy override mechanism that
does not exist today — the constant is module-level and shared by every strategy, so lowering it
lowers it for `Meteorologist V2` and any future strategy too.

**Pre-registration verdict: compromises it.** The executed strategy would be outcome-dependent and
not reproducible from the registration.

---

### Option 3 — Size from the genome's own `p_win`

**Mechanism.** Replace the blend at `risk_manager.py:467` with `p = confidence` (or a
strategy-scoped opt-out) when the strategy is a pre-registered genome whose `p_win` is a calibrated,
walk-forward probability rather than a model score.

**Effect.** **130/130 admissible, 57 units, 0.826 units/day → ~61 days**, same as Option 1.

**Evidential cost — the smallest of the three, but not zero.** There is a real argument here that the
others lack: `p_win` is *not* an uncalibrated model confidence. It comes from
`ev_analysis.WalkForwardCalibrator` with a declared embargo, pinned by sha in the promoted spec, and
the frame already trusts it as the fitness input. Bertsimas & Mundru's hazard is confidence that has
never been validated against settlement; this one has. **But** the validation is in-sample on the 69
already-searched dates, the family it belongs to is CLOSED (§7), and the mean `p_win` on the genome's
trades is **0.9567** against a realized win rate of **0.900** — so it is *optimistic by ~5.7 points
on the very sample it was fit on*. Sizing on it directly would systematically oversize by roughly the
amount the blend was designed to absorb.

**Files.** `src/core/risk_manager.py` (**protected**). The blend cannot be bypassed from the caller —
verified: reaching `p = 0.8` needs `confidence = 1.25`, and the parameter is used unclamped in a
convex combination. This is a genuine code change or nothing.

**Pre-registration verdict: does *not* compromise it, if and only if it is ratified as a general
sizing rule before the run and applied to every genome — not tuned to unblock this one.** If it is
introduced because this genome is stuck, it is post-hoc parameter selection wearing a research
citation.

---

### Option 4 — Accept and document a ~9-month F4

**Mechanism.** Change nothing. Run the sandbox at 0.174 units/day and reach `n_min = 50` around
**mid-2027** (287 days point estimate, 95 % band 208–367 days).

**Effect on admissible fraction.** Unchanged: **13/130 (10.0 %)**, and the record accumulates against
a 106-day wait for the 20 closes that would relax the ceiling to whatever the realized rate supports
(§6 sweep: at wr 0.69 the ceiling reaches ~0.82 and the rate roughly doubles — so the *realized*
trajectory is likely faster than 287 days, somewhere between 111 and 288, but that is a projection,
not a measurement, and I decline to headline it).

**Evidential cost.** Zero *statistical* cost and one large *practical* one. The record collected
would be a settlement-true record of the cheap sub-shape, which is honest — provided nobody writes
"genome 0c4b2050 passed the gate" at the end of it. It also runs past the ~2026-10-03 expiry of the
holdout-B ladders, so the option carries a deadline it does not control.

**Files.** None. **Pre-registration verdict: does not compromise it** — it is the only option that
leaves FR-5.2 exactly as registered. It just may not produce a usable answer.

---

## 7. The context that should decide this

Two facts from the run record change the cost/benefit of every option above.

**The family is CLOSED.** `configs/factory/promoted/0c4b20502f2daf65.json` carries
`registry_status: "CLOSED"` and `source: "seed"`. The F2 verdict
(`reports/factory/run_2026-09-03b/summary.json`) failed six of its twelve conditions:

- pooled OOS realized **+0.0308/contract**, boot 95 % **[−0.0900, +0.1417]**, one-sided **p = 0.2895**
- Reality-Check on ALL69: **p_RC = 0.886** (threshold 0.10); SPA **p = 0.664**
- Holm across families: **p_adj = 0.2895**, `reject: false`
- `paired_vs_nofilter_lo_gt0: false`, `beats_every_control: false`, `snapshot_pass: false`
- the real shape's rank among residual-null replicates: **21**

`CLOSED` means, in the PRD's own words, *"nothing beats the baseline after correction."* The planted-edge
control passed (`capture_ratio 1.09`), so the machinery can find edge when it is there. It was not there.

**The prior on this genome is therefore "no edge."** Spending governance capital — unprotecting the
risk gauntlet — to accelerate evidence collection on a CLOSED family's seed genome is a poor trade at
any speed. And per §4, even at its in-sample rate the full shape clears the gate at n = 50 with
p = 0.047: a coin's width of margin on data it was selected on.

---

## 8. A fifth option the reconstruction surfaced

The frame builder **already encodes one of the sandbox's two admission gates and not the other**:

```
src/factory/columns.py:106   "executable": ...  # search: evaluator's & sandbox_admissible
src/factory/columns.py:107   "sandbox_admissible": ...  # trade_is_profitable(p_win, price_paid, 1, is_maker=False)
```

`sandbox_admissible` is the mixin's `_ml_ev_gate`, folded into `executable` for the search frame
(`lanes/weather.py:248`, `fold_sandbox_admissible=True`). It passes **130/130** of these trades — it
never binds. The gate that *does* bind, `calculate_kelly_size > 0`, is not modelled at all.

**Option 5 — fold the runtime's sizing law into `sandbox_admissible` and re-search as a new family.**

The cold-start form is a closed-form, state-free predicate on columns the frame already has:

```
0.6 * 0.50 + 0.4 * p_win  >  price_paid + <maker fee at price_paid>
```

Fold that into `sandbox_admissible`, register `weather/gfs_mex/taker/v2` (the registry refuses to
re-register a CLOSED family name — `src/factory/registry.py:182` — a rerun is a new family, by
design), and search only over rows the runtime can actually size.

- **Admissible fraction: 100 % by construction.** Everything promoted is executable from trade 1.
- **Time to 50 units:** whatever the new family's shape yields, with no cold-start discount.
- **Evidential cost: none.** It makes the pre-registration true rather than working around it. The
  gate keeps its thresholds; the promoted spec becomes something the sandbox can express.
- **Files:** `src/factory/frame.py`, `src/factory/lanes/weather.py`, `src/factory/columns.py` —
  **none protected**, all offline, none imported by the runtime beyond `features.py`/`genome.py`.
  Requires a new freeze-frame (new frame sha), a new parity run, and a new gate registration.
- **Honest cost:** it removes 86 % of the executable NO-taker rows from the search space. On this
  frame the surviving universe realizes **−0.048/contract** unfiltered (§3), so a v2 search may well
  return CLOSED again — quickly, and for a defensible reason. That is information, and HANDOFF §3
  rule 6 says a documented "no" satisfies the criterion.

This option is not free and it is not fast, but it is the only one that fixes the actual defect: the
factory searched a space the runtime cannot trade in.

---

## 9. Recommendation

**Do not touch `src/core/risk_manager.py`.** Reject Options 1 and 2 outright; hold Option 3 in
reserve as a general sizing-policy question, decided on its own merits and never to unblock a
specific genome.

**Adopt Option 4 for the current deployment, with one correction to how it is described**, and start
Option 5 in parallel:

1. **Stop calling the current shadow run an F4 gate run.** It is a runtime instrumentation record. It
   is producing exactly the evidence it should — `KELLY_ZERO` lines proving the sandbox will not size
   the promoted shape — and that evidence is already complete. Leave it running; it costs nothing.
2. **Record the finding as a promotion defect, not a risk defect.** The `promote` path should refuse,
   or at minimum loudly warn, when the median `price_paid` of a genome's offline trade set exceeds the
   cold-start ceiling `0.30 + 0.40 · median(p_win)`. That check is three lines in
   `src/factory/promoted.py` or `scripts/factory.py`, touches nothing protected, and would have caught
   this before deployment.
3. **Fold the sizing law into `sandbox_admissible` (Option 5) and register `.../v2`.** This is the
   fix. Budget for it returning CLOSED.
4. **Decide the holdout-B question on its own timer.** `data/ladders_holdout/` exists on the lab
   machine and the ladders expire ~2026-10-03. It is a sealed root (`src/factory/holdout.py`,
   `--unseal RATIFIED-<date>`, ≤3 unseals/quarter, ≤3 finalists/family) and **I did not read it.**
   Whether to spend an unseal on a CLOSED family's seed genome is an owner call, but it is the one
   decision here with a hard external deadline.

**Why not just wait the 287 days.** Because the waiting produces a record of a strategy nobody
registered, on a family that already reported no edge, and the wait outlives the only virgin holdout
the project will ever have.

---

## 10. What I am uncertain of

- **Rate transfer.** 0.1739 units/day is measured on 2026-05-18…07-25 — summer, four cities. F4 runs
  in autumn. The binding input is the *price distribution* of far NO brackets, which is not
  stationary. The single live day observed (2026-09-05: 4 signals, one target date, prices 0.74–0.92,
  all above the ceiling) is directionally consistent but is n = 1.
- **The 287 days is a point estimate on a Poisson wait.** 95 % band 208–367 days, and that ignores
  rate non-stationarity entirely.
- **The realized trajectory is probably faster than 287 days** because the win record unlocks the
  ceiling after 20 closes (§6 sweep). I have deliberately *not* headlined the unlocked figure: it is a
  simulation of a feedback loop on in-sample data, and treating it as the expected wait would be
  exactly the modelled-for-realized substitution HANDOFF §3 forbids. The 287-day figure is the one
  that follows from measured quantities alone.
- **Whether `p_win` deserves to be trusted for sizing (Option 3)** is a genuinely open research
  question I have not resolved. My +5.7-point optimism figure (mean `p_win` 0.9567 vs realized 0.900)
  is in-sample; the honest out-of-sample calibration of `p_win` has not been measured and should be,
  independently of this decision.
- **The 24-trade "if it retried" figure (§5a)** assumes a strategy semantic that does not exist and
  would break FR-F3.4 parity. It is an upper bound on what a strategy-side fix could buy, not a plan.
- **I did not verify the live sandbox's genome behaviour beyond an 8–16 minute log window** (the tail
  endpoint is capped at 500 lines; the window I fetched, 18:43–18:58 Z, contains no genome activity
  because the genome decides on the hour). The 15:00 Z and 18:00 Z evidence is taken as established.
- **I did not open the sealed holdout.** Every number here is from the 69 development dates, which
  PRD A3 declares already searched. Nothing in this document is out-of-sample.

---

## Appendix — reproduction

```bash
# from a checkout with data/factory/frames present
export PYTHONPATH=.
python - <<'PY'
import json, numpy as np
from src.factory import frame as FR, genome as G, fitness
from src.core.risk_manager import RiskManager
F = FR.load('data/factory/frames/weather_2026-07-25_bfcf94654a3a/search')
spec = json.load(open('configs/factory/promoted/0c4b20502f2daf65.json'))
g = G.Genome.from_json(spec['genome_json'])
res = fitness.score(F, G.to_mask(g, F), constraints=False, genome=g)
rows = res.trade_rows                      # 130 == spec['parity']['n_offline']
vis = F.visible
price, pwin = vis['price_paid'][rows], vis['p_win'][rows]
mk = json.load(open('data/factory/frames/weather_2026-07-25_bfcf94654a3a/search/markets.json'))
rm = RiskManager(starting_balance=2873.12, persist_state=False); rm.strategy_win_records = {}
qty = np.array([rm.calculate_kelly_size(float(c), float(p), 'Genome 0c4b2050',
                                        symbol=mk[int(m)])
                for c, p, m in zip(pwin, price, vis['market_code'][rows])])
adm = qty >= 1
print(adm.sum(), 'of', len(rows),
      '|', len(set(vis['target_date_code'][rows][adm].tolist())), 'units')
PY
# -> 13 of 130 | 12 units
```

Sources: `data/factory/frames/weather_2026-07-25_bfcf94654a3a/{search,run.json}` ·
`configs/factory/promoted/0c4b20502f2daf65.json` · `configs/factory/gate_registration.template.json` ·
`reports/factory/run_2026-09-03b/summary.json` · `src/core/risk_manager.py` ·
`src/bots/mixins.py` · `src/strategies/genome_strategy.py` · `src/factory/{columns,fitness,frame,registry}.py` ·
`scripts/gate.py` · read-only `GET http://maia.local:8050/{healthz,api/status,api/win_rates,api/journal,api/logs/tail}`.
