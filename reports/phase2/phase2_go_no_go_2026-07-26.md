# Phase 2 go/no-go — weather (KXHIGH*) — 2026-07-26

**PRD:** FR-2.4 decision point; Phase 2 exit criterion 5. **Branch:** `phase-2-forecast-calibration`. **Workstream E.**

Every number in this document was computed by `scripts/go_no_go.py` from the files named in §2. Nothing is carried over from another workstream's report without being recomputed here, and no number is quoted for a fill the recorded tape says was unavailable.

---

## 0. Verdict

> ## HALT.
>
> **No weather trade shape has demonstrated an edge that survives the checks in this report. Per FR-2.4, weather halts and Phase 4 (gas) becomes flagship.**

The one shape that looked positive — FR-3.1(a) far-bracket NO at its pre-registered parameters — **reverses sign when the forecast source is changed**, on the same 69 days, the same tape, the same rule and the same harness:

| forecast source | day-of σ (NY / CHI / LAX / MIA) | trades | modelled EV/ct | **realized/ct** | boot 95% CI |
|---|---|---|---|---|---|
| gfs_mex | 3.37 / 3.98 / 2.14 / 1.71 | 181 | +12.23¢ | +6.36¢ | [+1.22¢, +10.86¢] |
| gefs  *(PRD FR-2.1's designated primary)* | 4.11 / 3.76 / 3.77 / 2.42 | 210 | +15.35¢ | -6.22¢ | [-12.97¢, +0.22¢] |

**The modelled EV — the quantity FR-2.4 gates on — scores the losing configuration higher than the winning one** (+15.35¢ vs +12.23¢) while their realized outcomes have opposite signs. A gate that ranks a loser above a winner is not measuring the thing it is supposed to measure, and nothing may be sized from it.

### The full reasoning, strongest first

The four reasons that carry the decision are listed before the ones that merely support it. The first is internal to this report's own arithmetic and is on its own sufficient.

1. **FR-2.4's own gate quantity is anti-correlated with the outcome (§8.6).** Modelled EV ranks `gefs` (+15.35¢) **above** `gfs_mex` (+12.23¢) while their realized signs are opposite (-6.22¢ versus +6.36¢). FR-2.4 authorizes Phase 3 *on modelled EV*. A gate that ranks a loser above a winner cannot authorize anything, and nothing may be sized from it. This reason needs no assumption about which source is better, no out-of-sample data and no judgement call — it is a property of the two numbers this report computed — and it is by itself sufficient for HALT.
2. **The model understates its own loss rate by 2.0–3.8× at the trade level (§8.7).** On the trades that fired, `gfs_mex` models P(loss) = 0.0543 against a realized 0.1105 (2.04×); `gefs` models 0.0761 against 0.2857 (3.76×). The mean outcome of a losing trade is -76.52¢. The *mean* is positive; the *risk* is unquantified. This is a short-tail shape — many small wins funded by rare large losses — and it must not be sized from a distribution that is wrong in precisely the direction it is short. §7 shows the same defect in the residuals; this is that defect measured on the trades themselves.
3. **The majority of the sample has a confidence interval spanning zero (§8.8).** NY and CHI supply 131 of 181 trades (72%) and realize +4.76¢ at t = +1.43, bootstrap CI [-2.42¢, +10.76¢]. The pooled t = +2.57 is produced by LAX and MIA — 50 trades at a 100.0% win rate, whose t = +12.70 reflects the absence of loss variance rather than the size of an edge. A headline significance carried by an unbeaten streak in 28% of the sample is not a demonstrated edge.
4. **Source instability (§8.6)** — corroboration, not the load-bearing argument. Realized +6.36¢ under `gfs_mex` versus -6.22¢ under `gefs`, on the same days, tape, rule and harness. Which source is skilful was determined *by looking at this data*, and the PRD's own designated primary source is the one that loses money. It is listed fourth because, unlike reasons 1–3, a sufficiently good ex-ante argument for one source would answer it — §9.2 records that no such argument survived testing.

The remaining findings are consistent with the above and would not, on their own, have carried the decision:

5. **The literal FR-2.4 arithmetic gate is passed** — modelled EV is > 0 after fees and the 1¢ allowance for the far-bracket NO shape under *both* sources (§4.1). FR-2.4 makes that a **necessary** condition ("proceeds only for trade shapes with modeled EV > 0"), not a sufficient one, and this report declines to treat it as sufficient for the reasons above. EC-5 explicitly admits a HALT.
6. **The model misprices its own tail (§7).** Pooled walk-forward standardized residuals exceed |z| ≥ 3.0 at **3.74×** the Gaussian rate, while the shoulder is over-predicted by 0.59–0.89×. The shape is short exactly the part of the distribution that is understated.
7. **Regime instability (§8.5).** Under workstream D's better-specified N/X-window mixture — the regime model D explicitly recommends for NY and CHI — the same rule on the same days is indistinguishable from zero.
8. **A third of the apparent edge is model-free (§6.2).** A trivial baseline that sells far brackets without the divergence filter is itself positive in this window, and the two confidence intervals overlap, so the filter's incremental contribution is not established. That component is generic warm-season tail-selling — the part most exposed to reason 6.
9. **FR-3.1(b) has no evaluable sample at all (§6.3).** The archived sources publish no same-day update, so the model is frozen through the entire afternoon window the lock-in shape needs.
10. **Everything else tested is negative outright (§6.4, §4.1)** — including buying the cheap far tail, the shape the PRD did not anticipate and which workstream D's mixture appeared to favour.

### What HALT does *not* say

It does not say the weather signal is worthless. Under the better forecast the shape realized +6.36¢/contract over 181 trades with a pooled bootstrap CI excluding zero, its sign held across all four cities and all three months, and it survived entry timing, order size and 3¢ of slippage (§8.2–8.3). Something is probably there. What it says is that **this evidence cannot tell that apart from a source-selection artifact**, that the gate quantity FR-2.4 relies on demonstrably cannot either, and that the pooled significance is concentrated in two small-sample cities (§8.8). §9.3 pre-registers, now, exactly what would settle it.

---

## 1. Exit criterion 5, quoted verbatim, satisfied clause by clause

> **5.** Go/no-go report exists as a dated artifact: EV per bracket-distance band under maker and taker pricing with fees and a 1¢ adverse-fill allowance, on ≥30 days of recorded ladders; it names which trade shapes (if any) are +EV and states the PROCEED/HALT decision per FR-2.4. Red-team can recompute one band's EV from raw inputs and match within rounding.

**FR-2.4, verbatim:**

> Go/no-go analysis (decision point): using calibrated σ and recorded market ladders, compute expected value per bracket-distance band under maker and taker pricing. Phase 3 proceeds only for trade shapes with modeled EV > 0 after fees and 1¢ adverse-fill allowance; if none qualify, weather halts and Phase 4 (gas) becomes flagship.

| clause | where | status |
|---|---|---|
| exists as a dated artifact | this file | `reports/phase2/phase2_go_no_go_2026-07-26.md`, regenerated by `scripts/go_no_go.py` |
| EV per bracket-distance band | §4 | 6 bands on \|bracket midpoint − calibrated median\|, edges [0.0, 1.0, 2.0, 3.0, 4.0, 5.0] °F |
| under maker **and** taker pricing | §4 | both, each hitting the side of the book that shape actually requires; maker fills additionally require a later quote traversal |
| with fees | §3.3 | `src/core/fee_calculator.py`: maker $0.00 on KXHIGH*, taker ceil_to_cent(0.07·C·P·(1−P)), one leg (settlement is free, FR-1.5 holds to expiry) |
| and a 1¢ adverse-fill allowance | §3.4 | price_paid = quote + $0.01 on every cell; sensitivity to 2¢/3¢/4¢ in §8.3 |
| on ≥30 days of recorded ladders | §2 | 69 consecutive dates (2026-05-18 … 2026-07-25), 62,932 hourly rows — 2.3× the floor |
| names which trade shapes (if any) are +EV | §0, §6 | **none survive.** Every shape is named with its city set, band, direction, maker/taker and time-to-close window, and the one that is +EV under one forecast source is −EV under the other |
| states the PROCEED/HALT decision per FR-2.4 | §0, §9 | **HALT** — weather halts, Phase 4 (gas) becomes flagship |
| red-team can recompute one band's EV and match within rounding | §5 | fully worked single trade, every raw input and intermediate, hand-checkable against a normal table |

---

## 2. Provenance

### 2.1 Inputs

| input | path | content | hash |
|---|---|---|---|
| recorded ladders | data/ladders/<SERIES>/<date>.csv | 62,932 rows, 1,656 markets, 276 city-days, 69 dates 2026-05-18…2026-07-25 | manifest.json: 62932 rows recorded |
| forecast archive | data/forecast_archive/forecast_series_gfs_mex.csv | 10,750 snapshot→vintage matches; latest run with init_time_utc ≤ ts_utc | sha256:850a2a3f44cae5f4 |

### 2.2 Calibration artifacts

| city | file | file sha256 | content_hash | day-of n | bias °F | σ °F | fitted over |
|---|---|---|---|---|---|---|---|
| CHI | CHI_gfs_mex_v1.json | 8dbbfd57506176ba | ab32388c98e66632 | 209 | 0.1340 | 3.9809 | 2025-12-28…2026-07-24 |
| LAX | LAX_gfs_mex_v1.json | 1bae87eec0067ec9 | 7245f22c36937951 | 209 | -0.3828 | 2.1409 | 2025-12-28…2026-07-24 |
| MIA | MIA_gfs_mex_v1.json | af35b7371ed7db7b | cfd2dc3bbe87783c | 208 | -0.1683 | 1.7069 | 2025-12-28…2026-07-24 |
| NY | NY_gfs_mex_v1.json | c22a776032a867ea | f8d016c41909958a | 209 | 0.0574 | 3.3736 | 2025-12-28…2026-07-24 |

Truth (`data/weather_truth/cli_daily_high_<STATION>.csv`) content hashes: CHI `39c301625435`, LAX `58b543726cdf`, MIA `07cb88611415`, NY `44be692662c1`.

### 2.3 Fee model

`src/core/fee_calculator.py` as corrected by the orchestrator this sprint against the published schedule effective 2026-07-07 and live `/series` metadata (`reports/phase2/ws_c_fee_verification.md`):

* maker fee on `KXHIGH*` = **$0.00** (`fee_type == "quadratic"`, maker multiplier M = 0)
* taker fee = `ceil_to_cent(0.07 · C · P · (1−P))` on the **order total**, so per-contract cost falls with size
* **no settlement fee**; PRD FR-1.5 holds every weather position to expiry, so a position pays **one** fee leg, not a round trip

Modelled order size: **C = 20** contracts ('fixed base quantity', FR-3.4 / assumption A8). §8.2 shows the verdict is insensitive to this: C = 1 and C = 50 move the headline realized PnL by under half a cent.

### 2.4 Code state

* branch `phase-2-forecast-calibration`, HEAD `617dd668b542`
* working tree at generation time: **uncommitted changes present**
* generator: `scripts/go_no_go.py`; machinery: `src/backtest/ev_analysis.py`; tests: `tests/test_ev_analysis.py`

**Which parts of this artifact are byte-stable.** Everything below §2.4 is a function of `scripts/go_no_go.py` and of the input files hashed in §2.1–2.2, so re-running the generator against the same inputs reproduces it byte for byte. The three lines above describe the *checkout* rather than the data and are the only ones that move when the same inputs are re-run from a different commit. An earlier revision of this generator also embedded the first 40 lines of `git status --porcelain`; that made the artifact fail its own reproducibility check whenever any unrelated file in the tree changed, and it has been removed. A reproducibility check that fails cosmetically is worse than none, because it teaches the reader to ignore it.

### 2.5 Second calibration source (workstream F)

Workstream F landed during this workstream. Additional calibrated sources found on disk: `gefs`. Each is priced through the identical walk-forward harness and reported as a **sensitivity axis** in §8.6; `--source <name>` regenerates this whole artifact under any of them.

`gfs_mex` is the **headline** source because it is the better forecast at 3 of 4 cities: workstream F reports GEFS — the PRD's designated primary source (FR-2.1) — as the worse one, failing EC-3's 4°F day-of σ bound outright at NY. §8.6 asks the only question that matters here — does the result survive being computed on the other source? — and the answer, **no**, is what produces the HALT in §0. Note that "headline" here is itself a choice made after measuring which source worked; see §10.3 and §10.10.

**Per-day ensemble spread is not used as σ anywhere in this report.** Workstream F measured `gespr` at 0.19–0.37× the realized error σ with correlation to |error| of −0.013 / +0.065 / +0.022 / −0.197 across the four cities — i.e. no skill. Every σ here is the calibrated per-bucket `sigma_f`; the probability engine is called with `require_published_spread=False` and never reads `spread_f` or `mean_spread_f` as a dispersion.

---

## 3. Method — and the five things that would have made it dishonest

### 3.1 No lookahead in the forecast

The archive carries 15 model runs per target date, from 4 h to 176 h of lead. A ladder snapshot at `ts_utc` is priced with the most recent run whose `init_time_utc <= ts_utc` and nothing later. In practice the recorded tape sees exactly two vintages per city-day: the previous day's 12Z run (lead 16–19 h, calibration bucket `lead_12_36`) for snapshots before 00Z, and the target day's 00Z run (lead 4–7 h, the `day_of` chain) afterwards.

**A consequence worth stating up front:** this source publishes **no same-day update**. The model's median is frozen from 00Z on the target date until settlement. That is why every headline number below is restricted to ≥ 12 h before close, and it is the direct reason FR-3.1(b) cannot be evaluated (§6.3).

### 3.2 No lookahead in the calibration (the contamination fix)

| committed calibration fitted over | recorded ladders | ladder dates inside the calibration window |
|---|---|---|
| 2025-12-28 … 2026-07-24 | 2026-05-18 … 2026-07-25 | 68 of 69 (98.6%) |

**98.6% of the traded tape is inside the committed calibration's own fitting window.** Scoring these ladders with `<CITY>_gfs_mex_v1.json` would price each trade with a bias and σ that already saw its outcome — the exact failure `backtest-before-deploy` exists to prevent.

Every headline number in this report is therefore **walk-forward**: for each target date *D*, the calibration is refit through workstream B's own `build_city_calibration()` on paired days with `target_date ≤ D − 1`. The refit runs the same bucket chain (`by_month_day_of` → `by_season_day_of` → `day_of`), which under walk-forward usually falls through to a wider annual or seasonal σ because the current month has not yet accumulated 20 days. The in-sample tables are published beside them in §4.2 and the gap is quantified in §8.1.

The walk-forward mean σ actually used is **2.752 °F** against the in-sample artifacts' day-of σ of CHI 3.9809, LAX 2.1409, MIA 1.7069, NY 3.3736.

### 3.3 Availability, not spread, is the binding constraint

Workstream C's headline finding is that the book collapses **one-sidedly** near settlement: far brackets have a YES bid in 0.0% of snapshots inside 6 h, while their ask is there ~100% of the time at 1¢. Every candidate here therefore names the side it must hit:

| shape | hits | needs | pays |
|---|---|---|---|
| taker buy YES | the YES offer | `yes_ask < 1.0` | `yes_ask` |
| taker buy NO | the NO offer | `yes_bid > 0` | `1 − yes_bid` |
| maker buy YES | joins the YES bid | `yes_bid > 0` **and** a later `yes_ask ≤ yes_bid(t)` | `yes_bid` |
| maker buy NO | joins the NO bid | `yes_ask < 1.0` **and** a later `yes_bid ≥ yes_ask(t)` | `1 − yes_ask` |

`yes_bid = 0` and `yes_ask = 1` are Kalshi's empty-book sentinels and are treated as **absent**, never as a price. A snapshot where the required side is missing is counted in `n cand` and excluded from every price and EV statistic, and the **fill-opportunity rate** is printed beside every cell. A cell with a good EV and a low fill rate is not a trade; it is a quote that was not there.

The maker fill model is the quote-traversal proxy PRD FR-3.3 names: a resting order fills only if a **later** hourly snapshot before close shows the other side crossing your limit. It is a lower bound (an intra-hour traversal is invisible at this resolution) and an upper bound on *durable* fills (queue position is unobservable). It is also **forward-looking by construction** — which is correct for execution modelling but means maker cells carry an adverse-selection bias in the modelled EV that the realized column exposes. **The headline verdict is taken from the taker path, which uses no forward information at all.**

### 3.4 The 1¢ adverse-fill allowance

`price_paid = quote + $0.01`, applied to **every** cell, maker and taker, and the fee is charged on the price actually paid. On a 1¢ far bracket that allowance is 100% of the premium — which is the point of the criterion requiring it. A quote at 0.99 plus the allowance leaves the orderable grid and is marked unexecutable rather than booked as a certain loss.

### 3.5 Modelled EV is not the only number reported

Every cell carries **both** the modelled EV (FR-2.4's gate quantity) **and** the realized settlement-true PnL of the identical trade, using Kalshi's own `result` — which workstream C reconciled against `bracket_payoff` 1,656/1,656 and against NWS CLI truth 1,631/1,631. Where the two disagree, the realized number is the one that happened.

EV per contract, held to settlement:

```
EV = P_model(win) × $1.00  −  price_paid  −  entry_fee/contract
   where price_paid = quote + $0.01,  fee = ceil_to_cent(0.07·C·P·(1−P))/C (taker)
                                       or  $0.00                                    (maker)
```

---

## 4. EV per bracket-distance band × {maker, taker} × {buy YES, buy NO}

Band = `|bracket midpoint − calibrated forecast median|` in °F. The two open-ended tail contracts have no midpoint, so they are represented as a virtual bracket of the same width as the ladder's finite core (`greater` → `floor+1 + (w−1)/2`, `less` → `cap−1 − (w−1)/2`); on the standard 2°F ladder that puts T90 (≥91) at 91.5 and T83 (≤82) at 81.5. That is the least generous convention available — placing them further out would inflate their band and flatter far-bracket EV.

All rows below: walk-forward calibration, C = 20, 1¢ adverse fill, one fee leg. `realized/ct` is the settlement-true PnL of the same trades.

**Clustering unit in this section: the city-day.** `realized SE` is the standard error of the per-city-day mean. Four cities under one weather pattern are not independent draws, so these standard errors are optimistically small and they are **not comparable** with the `SE` column of §6, §8.5 and §8.8, which clusters on whole **dates** (the coarser unit, which absorbs that cross-city correlation). Every table in this report states its own clustering unit; no two are pooled.

### 4.1 Walk-forward (the verdict rests on this table)

| direction | mode | band | n cand | n fill | fill-opp | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | realized SE | city-days |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| buy_no | maker | 0-1F | 9,975 | 6,381 | 64.0% | 0.6369 | 0.5940 | 0.6040 | 0.0000 | +3.29¢ | -7.01¢ | +2.21¢ | 270 |
| buy_no | maker | 1-2F | 9,960 | 6,110 | 61.3% | 0.6968 | 0.6445 | 0.6545 | 0.0000 | +4.23¢ | -8.44¢ | +2.00¢ | 265 |
| buy_no | maker | 2-3F | 9,660 | 4,868 | 50.4% | 0.7996 | 0.7394 | 0.7494 | 0.0000 | +5.02¢ | -6.43¢ | +1.91¢ | 266 |
| buy_no | maker | 3-4F | 8,831 | 3,604 | 40.8% | 0.8706 | 0.8090 | 0.8190 | 0.0000 | +5.15¢ | -2.32¢ | +1.68¢ | 244 |
| buy_no | maker | 4-5F | 7,884 | 2,310 | 29.3% | 0.9131 | 0.8394 | 0.8494 | 0.0000 | +6.37¢ | +0.51¢ | +1.60¢ | 203 |
| buy_no | maker | 5F+ | 16,622 | 2,844 | 17.1% | 0.9474 | 0.8694 | 0.8794 | 0.0000 | +6.81¢ | +2.29¢ | +1.11¢ | 187 |
| buy_no | taker | 0-1F | 9,975 | 8,369 | 83.9% | 0.6307 | 0.5686 | 0.5786 | 0.0120 | +4.01¢ | -4.59¢ | +1.93¢ | 270 |
| buy_no | taker | 1-2F | 9,960 | 8,019 | 80.5% | 0.6953 | 0.6213 | 0.6313 | 0.0108 | +5.32¢ | -5.82¢ | +1.72¢ | 267 |
| buy_no | taker | 2-3F | 9,660 | 6,792 | 70.3% | 0.8023 | 0.7331 | 0.7431 | 0.0088 | +5.04¢ | -3.37¢ | +1.58¢ | 268 |
| buy_no | taker | 3-4F | 8,831 | 5,261 | 59.6% | 0.8727 | 0.8140 | 0.8240 | 0.0070 | +4.17¢ | -0.13¢ | +1.43¢ | 254 |
| buy_no | taker | 4-5F | 7,884 | 3,460 | 43.9% | 0.9158 | 0.8526 | 0.8626 | 0.0059 | +4.73¢ | +1.65¢ | +1.24¢ | 232 |
| buy_no | taker | 5F+ | 16,622 | 4,627 | 27.8% | 0.9457 | 0.8836 | 0.8936 | 0.0046 | +4.75¢ | +2.64¢ | +0.66¢ | 229 |
| buy_yes | maker | 0-1F | 9,975 | 6,515 | 65.3% | 0.3550 | 0.3392 | 0.3492 | 0.0000 | +0.58¢ | -6.31¢ | +2.07¢ | 271 |
| buy_yes | maker | 1-2F | 9,960 | 6,517 | 65.4% | 0.2933 | 0.2794 | 0.2894 | 0.0000 | +0.39¢ | -3.78¢ | +1.88¢ | 267 |
| buy_yes | maker | 2-3F | 9,660 | 6,254 | 64.7% | 0.1883 | 0.1852 | 0.1952 | 0.0000 | -0.70¢ | -4.24¢ | +1.72¢ | 268 |
| buy_yes | maker | 3-4F | 8,831 | 5,459 | 61.8% | 0.1158 | 0.1290 | 0.1390 | 0.0000 | -2.32¢ | -5.29¢ | +1.40¢ | 259 |
| buy_yes | maker | 4-5F | 7,884 | 4,039 | 51.2% | 0.0736 | 0.1002 | 0.1102 | 0.0000 | -3.66¢ | -5.52¢ | +1.13¢ | 245 |
| buy_yes | maker | 5F+ | 16,622 | 6,239 | 37.5% | 0.0376 | 0.0678 | 0.0778 | 0.0000 | -4.02¢ | -4.96¢ | +0.54¢ | 258 |
| buy_yes | taker | 0-1F | 9,975 | 9,115 | 91.4% | 0.3668 | 0.3202 | 0.3302 | 0.0116 | +2.50¢ | -1.81¢ | +1.91¢ | 271 |
| buy_yes | taker | 1-2F | 9,960 | 9,222 | 92.6% | 0.3008 | 0.2669 | 0.2769 | 0.0103 | +1.36¢ | -0.51¢ | +1.74¢ | 267 |
| buy_yes | taker | 2-3F | 9,660 | 9,255 | 95.8% | 0.1902 | 0.1679 | 0.1779 | 0.0078 | +0.45¢ | -2.23¢ | +1.57¢ | 268 |
| buy_yes | taker | 3-4F | 8,831 | 8,666 | 98.1% | 0.1159 | 0.1084 | 0.1184 | 0.0059 | -0.84¢ | -3.99¢ | +1.29¢ | 260 |
| buy_yes | taker | 4-5F | 7,884 | 7,815 | 99.1% | 0.0647 | 0.0704 | 0.0804 | 0.0043 | -2.00¢ | -4.22¢ | +0.96¢ | 253 |
| buy_yes | taker | 5F+ | 16,622 | 16,545 | 99.5% | 0.0258 | 0.0409 | 0.0509 | 0.0030 | -2.81¢ | -3.71¢ | +0.32¢ | 276 |

### 4.2 In-sample — published for comparison only, carries no verdict

| direction | mode | band | n cand | n fill | fill-opp | mean P(win) | mean quote | price+1c | fee/ct | EV/ct | realized/ct | realized SE | city-days |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| buy_no | maker | 0-1F | 10,257 | 6,747 | 65.8% | 0.6018 | 0.5751 | 0.5851 | 0.0000 | +1.68¢ | -11.60¢ | +2.20¢ | 270 |
| buy_no | maker | 1-2F | 9,934 | 6,107 | 61.5% | 0.6951 | 0.6630 | 0.6730 | 0.0000 | +2.21¢ | -8.06¢ | +2.00¢ | 267 |
| buy_no | maker | 2-3F | 9,681 | 5,001 | 51.7% | 0.8056 | 0.7477 | 0.7577 | 0.0000 | +4.79¢ | -2.97¢ | +1.86¢ | 270 |
| buy_no | maker | 3-4F | 8,712 | 3,445 | 39.5% | 0.8775 | 0.8065 | 0.8165 | 0.0000 | +6.10¢ | +1.89¢ | +1.47¢ | 245 |
| buy_no | maker | 4-5F | 8,008 | 2,370 | 29.6% | 0.9251 | 0.8561 | 0.8661 | 0.0000 | +5.90¢ | -0.11¢ | +1.37¢ | 215 |
| buy_no | maker | 5F+ | 16,340 | 2,447 | 15.0% | 0.9594 | 0.8905 | 0.9005 | 0.0000 | +5.89¢ | +3.17¢ | +1.07¢ | 185 |
| buy_no | taker | 0-1F | 10,257 | 8,800 | 85.8% | 0.5929 | 0.5411 | 0.5511 | 0.0120 | +2.98¢ | -8.49¢ | +1.92¢ | 270 |
| buy_no | taker | 1-2F | 9,934 | 8,012 | 80.7% | 0.6929 | 0.6448 | 0.6548 | 0.0108 | +2.73¢ | -5.43¢ | +1.74¢ | 267 |
| buy_no | taker | 2-3F | 9,681 | 6,884 | 71.1% | 0.8083 | 0.7387 | 0.7487 | 0.0089 | +5.07¢ | -0.64¢ | +1.57¢ | 270 |
| buy_no | taker | 3-4F | 8,712 | 5,055 | 58.0% | 0.8813 | 0.8195 | 0.8295 | 0.0068 | +4.50¢ | +2.68¢ | +1.25¢ | 256 |
| buy_no | taker | 4-5F | 8,008 | 3,641 | 45.5% | 0.9267 | 0.8598 | 0.8698 | 0.0055 | +5.14¢ | +1.46¢ | +1.10¢ | 242 |
| buy_no | taker | 5F+ | 16,340 | 4,136 | 25.3% | 0.9587 | 0.9080 | 0.9180 | 0.0041 | +3.66¢ | +3.13¢ | +0.72¢ | 232 |
| buy_yes | maker | 0-1F | 10,257 | 6,518 | 63.5% | 0.3894 | 0.3560 | 0.3660 | 0.0000 | +2.35¢ | -2.00¢ | +2.13¢ | 270 |
| buy_yes | maker | 1-2F | 9,934 | 6,644 | 66.9% | 0.2969 | 0.2712 | 0.2812 | 0.0000 | +1.58¢ | -4.17¢ | +1.85¢ | 266 |
| buy_yes | maker | 2-3F | 9,681 | 6,370 | 65.8% | 0.1805 | 0.1885 | 0.1985 | 0.0000 | -1.79¢ | -6.96¢ | +1.64¢ | 270 |
| buy_yes | maker | 3-4F | 8,712 | 5,353 | 61.4% | 0.1046 | 0.1278 | 0.1378 | 0.0000 | -3.32¢ | -6.95¢ | +1.22¢ | 260 |
| buy_yes | maker | 4-5F | 8,008 | 4,286 | 53.5% | 0.0606 | 0.0880 | 0.0980 | 0.0000 | -3.74¢ | -5.53¢ | +1.10¢ | 252 |
| buy_yes | maker | 5F+ | 16,340 | 5,852 | 35.8% | 0.0300 | 0.0576 | 0.0676 | 0.0000 | -3.76¢ | -4.85¢ | +0.64¢ | 259 |
| buy_yes | taker | 0-1F | 10,257 | 9,213 | 89.8% | 0.4046 | 0.3438 | 0.3538 | 0.0120 | +3.89¢ | +1.96¢ | +1.92¢ | 270 |
| buy_yes | taker | 1-2F | 9,934 | 9,287 | 93.5% | 0.3036 | 0.2541 | 0.2641 | 0.0103 | +2.92¢ | -0.88¢ | +1.75¢ | 267 |
| buy_yes | taker | 2-3F | 9,681 | 9,311 | 96.2% | 0.1794 | 0.1695 | 0.1795 | 0.0079 | -0.80¢ | -4.28¢ | +1.50¢ | 270 |
| buy_yes | taker | 3-4F | 8,712 | 8,580 | 98.5% | 0.1005 | 0.1056 | 0.1156 | 0.0056 | -2.07¢ | -5.56¢ | +1.11¢ | 261 |
| buy_yes | taker | 4-5F | 8,008 | 7,920 | 98.9% | 0.0506 | 0.0672 | 0.0772 | 0.0043 | -3.09¢ | -4.20¢ | +0.99¢ | 259 |
| buy_yes | taker | 5F+ | 16,340 | 16,307 | 99.8% | 0.0172 | 0.0341 | 0.0441 | 0.0028 | -2.96¢ | -3.67¢ | +0.37¢ | 276 |

### 4.3 Walk-forward × time-to-close window, far bands only

| direction | mode | band | window | n cand | n fill | fill-opp | mean P(win) | mean quote | EV/ct | realized/ct | city-days |
|---|---|---|---|---|---|---|---|---|---|---|---|
| buy_no | maker | 4-5F | 1-3h | 367 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_no | maker | 4-5F | 12-24h | 2,445 | 755 | 30.9% | 0.9286 | 0.8540 | +6.47¢ | -0.44¢ | 124 |
| buy_no | maker | 4-5F | 3-6h | 557 | 4 | 0.7% | 0.8858 | 0.2300 | +64.58¢ | -24.00¢ | 3 |
| buy_no | maker | 4-5F | 6-12h | 1,233 | 71 | 5.8% | 0.8789 | 0.5330 | +33.59¢ | -10.63¢ | 24 |
| buy_no | maker | 4-5F | <1h | 186 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_no | maker | 4-5F | >=24h | 3,096 | 1,480 | 47.8% | 0.9069 | 0.8484 | +4.86¢ | +1.60¢ | 201 |
| buy_no | maker | 5F+ | 1-3h | 743 | 1 | 0.1% | 0.9467 | 0.9800 | -4.33¢ | -99.00¢ | 1 |
| buy_no | maker | 5F+ | 12-24h | 5,303 | 770 | 14.5% | 0.9504 | 0.8472 | +9.32¢ | +2.59¢ | 106 |
| buy_no | maker | 5F+ | 3-6h | 1,160 | 4 | 0.3% | 0.9621 | 0.4900 | +46.21¢ | -0.00¢ | 1 |
| buy_no | maker | 5F+ | 6-12h | 2,657 | 67 | 2.5% | 0.9642 | 0.6206 | +33.36¢ | +1.12¢ | 19 |
| buy_no | maker | 5F+ | <1h | 368 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_no | maker | 5F+ | >=24h | 6,391 | 2,002 | 31.3% | 0.9457 | 0.8869 | +4.88¢ | +2.27¢ | 185 |
| buy_no | taker | 4-5F | 1-3h | 367 | 18 | 4.9% | 0.8367 | 0.0100 | +81.52¢ | -2.15¢ | 9 |
| buy_no | taker | 4-5F | 12-24h | 2,445 | 1,160 | 47.4% | 0.9321 | 0.8831 | +3.38¢ | +1.21¢ | 135 |
| buy_no | taker | 4-5F | 3-6h | 557 | 29 | 5.2% | 0.8455 | 0.1062 | +72.64¢ | -5.01¢ | 11 |
| buy_no | taker | 4-5F | 6-12h | 1,233 | 152 | 12.3% | 0.8976 | 0.6250 | +25.59¢ | +0.30¢ | 52 |
| buy_no | taker | 4-5F | <1h | 186 | 10 | 5.4% | 0.8430 | 0.0930 | +73.77¢ | -0.52¢ | 10 |
| buy_no | taker | 4-5F | >=24h | 3,096 | 2,091 | 67.5% | 0.9102 | 0.8735 | +2.04¢ | +2.12¢ | 226 |
| buy_no | taker | 5F+ | 1-3h | 743 | 16 | 2.2% | 0.7219 | 0.0100 | +70.04¢ | +10.35¢ | 8 |
| buy_no | taker | 5F+ | 12-24h | 5,303 | 1,302 | 24.6% | 0.9527 | 0.8844 | +5.39¢ | +2.75¢ | 139 |
| buy_no | taker | 5F+ | 3-6h | 1,160 | 28 | 2.4% | 0.7560 | 0.1218 | +62.16¢ | +4.41¢ | 10 |
| buy_no | taker | 5F+ | 6-12h | 2,657 | 158 | 5.9% | 0.8940 | 0.6297 | +24.84¢ | +5.06¢ | 39 |
| buy_no | taker | 5F+ | <1h | 368 | 7 | 1.9% | 0.6853 | 0.0457 | +62.63¢ | -5.90¢ | 7 |
| buy_no | taker | 5F+ | >=24h | 6,391 | 3,116 | 48.8% | 0.9488 | 0.9094 | +2.48¢ | +2.44¢ | 228 |
| buy_yes | maker | 4-5F | 1-3h | 367 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_yes | maker | 4-5F | 12-24h | 2,445 | 1,375 | 56.2% | 0.0593 | 0.0899 | -4.07¢ | -5.41¢ | 168 |
| buy_yes | maker | 4-5F | 3-6h | 557 | 4 | 0.7% | 0.0367 | 0.0675 | -4.08¢ | -7.75¢ | 2 |
| buy_yes | maker | 4-5F | 6-12h | 1,233 | 137 | 11.1% | 0.0662 | 0.1626 | -10.65¢ | -9.96¢ | 61 |
| buy_yes | maker | 4-5F | <1h | 186 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_yes | maker | 4-5F | >=24h | 3,096 | 2,523 | 81.5% | 0.0819 | 0.1024 | -3.05¢ | -5.34¢ | 243 |
| buy_yes | maker | 5F+ | 1-3h | 743 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_yes | maker | 5F+ | 12-24h | 5,303 | 1,836 | 34.6% | 0.0265 | 0.0652 | -4.87¢ | -4.91¢ | 179 |
| buy_yes | maker | 5F+ | 3-6h | 1,160 | 8 | 0.7% | 0.0303 | 0.3538 | -33.34¢ | -11.38¢ | 5 |
| buy_yes | maker | 5F+ | 6-12h | 2,657 | 157 | 5.9% | 0.0301 | 0.1603 | -14.01¢ | -10.02¢ | 47 |
| buy_yes | maker | 5F+ | <1h | 368 | 0 | 0.0% | -- | -- | -- | -- | -- |
| buy_yes | maker | 5F+ | >=24h | 6,391 | 4,238 | 66.3% | 0.0426 | 0.0649 | -3.23¢ | -4.78¢ | 258 |
| buy_yes | taker | 4-5F | 1-3h | 367 | 349 | 95.1% | 0.0487 | 0.0100 | +2.72¢ | -2.15¢ | 184 |
| buy_yes | taker | 4-5F | 12-24h | 2,445 | 2,445 | 100.0% | 0.0554 | 0.0695 | -2.83¢ | -3.99¢ | 207 |
| buy_yes | taker | 4-5F | 3-6h | 557 | 534 | 95.9% | 0.0496 | 0.0163 | +2.17¢ | -2.04¢ | 199 |
| buy_yes | taker | 4-5F | 6-12h | 1,233 | 1,214 | 98.5% | 0.0534 | 0.0433 | -0.22¢ | -2.68¢ | 206 |
| buy_yes | taker | 4-5F | <1h | 186 | 177 | 95.2% | 0.0470 | 0.0111 | +2.43¢ | -2.26¢ | 177 |
| buy_yes | taker | 4-5F | >=24h | 3,096 | 3,096 | 100.0% | 0.0818 | 0.1012 | -3.55¢ | -5.72¢ | 253 |
| buy_yes | taker | 5F+ | 1-3h | 743 | 727 | 97.8% | 0.0163 | 0.0100 | -0.53¢ | -1.88¢ | 265 |
| buy_yes | taker | 5F+ | 12-24h | 5,303 | 5,296 | 99.9% | 0.0205 | 0.0398 | -3.20¢ | -3.57¢ | 276 |
| buy_yes | taker | 5F+ | 3-6h | 1,160 | 1,138 | 98.1% | 0.0168 | 0.0126 | -0.73¢ | -2.15¢ | 273 |
| buy_yes | taker | 5F+ | 6-12h | 2,657 | 2,631 | 99.0% | 0.0173 | 0.0232 | -1.78¢ | -2.67¢ | 276 |
| buy_yes | taker | 5F+ | <1h | 368 | 362 | 98.4% | 0.0157 | 0.0123 | -0.81¢ | -2.11¢ | 239 |
| buy_yes | taker | 5F+ | >=24h | 6,391 | 6,391 | 100.0% | 0.0370 | 0.0592 | -3.64¢ | -4.82¢ | 276 |

Read the `fill-opp` column before the EV column. Inside 6 h to close the far-bracket NO shape has a fill-opportunity rate of a few percent and the surviving cells carry EVs of 60–80¢ on a handful of snapshots — those are not opportunities, they are the model being catastrophically stale against a market that already knows the answer (§3.1). They are excluded from every headline number.

---

## 5. Fully worked single trade — recompute this by hand

The **first** FR-3.1(a) taker trade in the tape, chosen by date order so the choice cannot be a selection. Every value below is either read directly from a named file or derived by one arithmetic step from values above it.

### 5.1 Raw inputs

| quantity | value | source |
|---|---|---|
| market | KXHIGHLAX-26MAY18-B69.5 | `data/ladders/KXHIGHLAX/2026-05-18.csv` |
| snapshot | 2026-05-17T15:00:00+00:00 | same row; `minutes_to_close` = 2459 (>=24h) |
| strike_type | between | Kalshi `/markets` (FR-1.1) |
| floor_strike | 69.0000 | same |
| cap_strike | 70.0000 | same |
| yes_sub_title | 69° to 70° | same |
| yes_bid | 0.2300 | candlestick close, same row |
| yes_ask | 0.2500 | candlestick close, same row |
| forecast_high_f | 75.0000 | `data/forecast_archive/forecast_series_gfs_mex.csv`, init `2026-05-17T12:00:00Z`, lead 19 h |
| calibration bucket | by_lead:lead_12_36 | walk-forward refit on truth ≤ 2026-05-18 minus 1 d |
| bias_f | -0.4007 | that bucket |
| sigma_f | 2.9052 | that bucket |
| settled high (expiration_value) | 71.0000 | Kalshi settlement, cross-checked to NWS CLI |

### 5.2 Step by step

```
1. debias the forecast          mu = forecast - bias
                                mu = 75.0000 - (-0.4007) = 75.4007 degF
   (sign per the artifact's error_convention:
    error_f = forecast - truth, positive = forecast too warm)
                                sigma = 2.9052 degF

2. bracket YES range            between, floor=69.0, cap=70.0  ->  pays YES for a
                                whole-degree high in [69, 70]   ("69° to 70°")

3. P(YES), integer outcome with a continuity correction
      z_hi = (cap + 0.5 - mu)/sigma = (70.0 + 0.5 - 75.4007)/2.9052 = -1.68687
      z_lo = (floor - 0.5 - mu)/sigma = (69.0 - 0.5 - 75.4007)/2.9052 = -2.37529
      Phi(z_hi) = 0.045814     (standard normal table)
      Phi(z_lo) = 0.008768
      P(YES)    = 0.045814 - 0.008768 = 0.037046

4. band                          midpoint = (69 + 70)/2 = 69.5 degF
                                 distance = |69.5 - 75.4007| = 5.9007 degF  ->  band 5F+
                                 nearest-edge distance = 5.4007 degF  (>= 4.0, so FR-3.1(a) is eligible)

5. FR-3.1(a) trigger             P(YES) <= yes_ask - 0.08 ?
                                 0.037046 <= 0.25 - 0.08 = 0.17   ->  YES, fire

6. the price this shape must hit (taker, buy NO)
      no_ask = 1 - yes_bid = 1 - 0.23 = 0.77
      yes_bid > 0, so the NO offer existed in the recorded tape

7. EC-5 adverse-fill allowance   price_paid = 0.77 + 0.01 = 0.78

8. fee (taker, C = 20, held to settlement -> ONE leg)
      raw   = 0.07 x 20 x 0.78 x (1 - 0.78)
            = 0.07 x 20 x 0.78 x 0.22 = $0.24024
      order fee = ceil to the cent          = $0.25
      per contract = $0.25 / 20 = $0.0125

9. modelled EV per contract
      P(win) = P(NO) = 1 - P(YES) = 1 - 0.037046 = 0.962954
      EV = 0.962954 - 0.78 - 0.0125
         = 0.170454   =  +17.0454 cents/contract

10. what actually happened
      settled daily high = 71 degF; the bracket pays YES only
      for [69, 70], so it settled 'no' -> the NO position WON
      realized = 1.0 - 0.78 - 0.0125 = +0.207500  =  +20.75 cents/contract
```

Every constant above appears in a named file; the only functions used are Φ (any normal table) and `ceil` to the cent. A red-team that reproduces steps 1–9 with a printed normal table will land within rounding of +17.05¢.

---

## 6. Which trade shapes are +EV — named precisely

**Clustering unit in this section: the date.** Inference is a non-parametric bootstrap over whole **dates** (4,000 resamples, seed 20260726), and the `SE` and `t` columns are computed on per-date means. The hourly snapshots of one market are the same bet on the same outcome, the six brackets of a city-day are one joint outcome, and four cities under one weather pattern move together, so a per-row standard error would be meaningless and a per-city-day one would still be optimistic. This is the coarser unit than §4's, and the two sections' `SE` columns are therefore not comparable. Entries are capped at **one per market** (FR-3.4 permits three); §8.3 shows the result is insensitive to which snapshot within the window is taken.

| shape | trades | markets | dates | fill-opp | modelled EV | realized | SE | t | boot 95% CI | win | losing dates | worst date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FR-3.1(a) far-bracket NO, taker, >=12h to close | 181 | 181 | 65 | 100.0% | +12.23¢ | +6.36¢ | +2.48¢ | +2.57 | [+1.22¢, +10.86¢] | 89.0% | 16/65 | -74.40¢ |
| FR-3.1(b) lock-in P>=0.95, taker, <12h to close | 4 | 4 | 4 | 27.1% | +30.23¢ | +8.19¢ | +6.03¢ | +1.36 | [-0.21¢, +20.12¢] | 75.0% | 1/4 | -2.15¢ |
| far-bracket YES (buy the tail), taker, >=12h | 813 | 813 | 69 | 100.0% | -4.51¢ | -6.20¢ | +0.57¢ | -10.85 | [-7.29¢, -5.06¢] | 4.7% | 61/69 | -14.57¢ |
| BASELINE far-bracket NO, no 8pt filter, taker, >=12h | 664 | 664 | 69 | 44.5% | +0.87¢ | +2.09¢ | +0.73¢ | +2.85 | [+0.65¢, +3.50¢] | 94.3% | 25/69 | -14.14¢ |
| FR-3.1(a) far-bracket NO, maker, >=12h to close | 131 | 131 | 60 | 72.1% | +16.62¢ | +8.55¢ | +3.09¢ | +2.77 | [+1.87¢, +14.36¢] | 84.7% | 17/60 | -78.50¢ |
| FR-3.1(b) lock-in P>=0.95, maker, <12h to close | 1 | 1 | 1 | 1.0% | -2.02¢ | +2.00¢ | -- | -- | [+2.00¢, +2.00¢] | 100.0% | 0/1 | +2.00¢ |
| far-bracket YES (buy the tail), maker, >=12h | 748 | 748 | 69 | 57.9% | -2.82¢ | -4.38¢ | +0.62¢ | -7.04 | [-5.60¢, -3.13¢] | 4.9% | 53/69 | -13.50¢ |
| BASELINE far-bracket NO, no 8pt filter, maker, >=12h | 513 | 513 | 69 | 29.1% | +3.17¢ | +3.13¢ | +0.94¢ | +3.32 | [+1.26¢, +4.96¢] | 92.6% | 24/69 | -20.00¢ |

### 6.1 The only shape that is ever +EV — and why it still fails

> **FR-3.1(a) far-bracket NO** — buy NO on a bracket whose model P(YES) is at least 8 points below the market's YES ask **and** whose nearest paying degree is ≥ 4°F from the calibrated median; cities **NY, CHI, LAX, MIA**; bands **4-5°F and 5°F+**; window **≥ 12 h before close**; **taker** (the maker variant is also positive but its fill model is forward-looking).

Everything in this subsection is measured on the `gfs_mex` source. **Under the other calibrated source on disk the identical rule loses money** (§8.6), which is why the verdict is HALT and not a scoped PROCEED. The numbers below describe the best case, not the case.

Fires on 181 distinct markets over 65 dates (~2.8 trades/date). Fill-opportunity rate 100.0%: when the signal fires ≥ 12 h out, the NO offer is essentially always there.

What the three probabilities actually were, on the trades that fired:

| quantity | value |
|---|---|
| model P(YES) | 0.0543 |
| market YES ask | 0.2195 |
| **realized** YES rate | 0.1105 |

**Both are wrong, and the model is wrong in the flattering direction.** The truth sits between the model and the market, closer to the model — which is why the trade makes money — but the model understates the bracket's true probability by roughly a factor of two, which is exactly the gap between its modelled EV and its realized PnL.

### 6.2 The trivial baselines, and how much of the edge is actually the model

`beat-the-trivial-baseline` is a standing rule in this project. Two baselines are measured, both on the same universe, the same windows, the same fees and the same 1¢ allowance, one entry per market.

**Baseline 1 — sell every far bracket, no divergence filter.**

Realizes **+2.09¢/contract** on 664 trades over 69 dates, t = +2.85, bootstrap CI [+0.65¢, +3.50¢].

**This baseline is itself positive, and the report will not pretend otherwise.** Roughly a third of the headline shape's per-trade edge is available from generic far-bracket tail-selling that barely uses the forecast. The 8-point divergence filter keeps 181 of those 664 trades and roughly triples the per-trade edge (+2.09¢ → +6.36¢), but the two confidence intervals overlap, so **the filter's incremental contribution is not itself established at conventional significance.** What is established is that both are positive and the filtered subset is the larger of the two.

This matters for the risk read, not just the accounting: generic tail-selling is precisely the component most exposed to the fat extreme tail measured in §7, and it is measured here on 69 warm-season days.

**Baseline 2 — bucket by the market's own price; use no model at all.**

| market yes_bid | trades | market-implied P(YES) | model P(YES) | realized YES rate | realized/ct | t (date-clustered) |
|---|---|---|---|---|---|---|
| (0.0, 0.03] | 352 | 0.0239 | 0.1199 | 0.0114 | +0.14¢ | +1.47 |
| (0.03, 0.06] | 230 | 0.0484 | 0.1431 | 0.0391 | -0.36¢ | -0.38 |
| (0.06, 0.1] | 131 | 0.0834 | 0.1501 | 0.0611 | +0.73¢ | +0.45 |
| (0.1, 0.15] | 125 | 0.1326 | 0.1701 | 0.1360 | -2.10¢ | -0.10 |
| (0.15, 0.25] | 251 | 0.2043 | 0.1869 | 0.2231 | -3.99¢ | -2.28 |
| (0.25, 0.5] | 390 | 0.3477 | 0.2262 | 0.3923 | -7.01¢ | -4.09 |
| (0.5, 1.0] | 45 | 0.5716 | 0.4000 | 0.6444 | -9.98¢ | -1.51 |

Compare `market-implied P(YES)` (the YES *bid*, so structurally below fair by roughly half a spread) with `realized YES rate`: they track each other closely across the whole price range. **The Kalshi weather book is well calibrated.** There is no favourite–longshot bias sitting there to be harvested, buying NO purely on cheapness is flat to negative once the 1¢ allowance and the taker fee are paid, and any real edge therefore has to come from the forecast rather than from market structure.

### 6.3 FR-3.1(b) lock-in — HALT, and the reason is structural

* `FR-3.1(b) lock-in P>=0.95, taker, <12h to close`: 4 trades on 4 dates (realized +8.19¢, CI [-0.21¢, +20.12¢]).
* `FR-3.1(b) lock-in P>=0.95, maker, <12h to close`: 1 trade on 1 date (realized +2.00¢, CI [+2.00¢, +2.00¢]).

This is not a small effect measured precisely; it is **no sample**. FR-3.1(b) needs the model to reach P ≥ 0.95 in the settlement-station afternoon, but the archived MOS source publishes no same-day update (§3.1), so the model is frozen at its 00Z value while the running maximum is still developing. It reaches 0.95 only by accident. Independently, workstream C measured the eventual winner's ask as available in **1.9%** of snapshots inside 1 h and **5.0%** inside 3–6 h — so even a correct signal would mostly have nothing to hit.

**FR-3.1(b) cannot proceed until an intra-day forecast source exists** (NBM/HRRR/GEFS short-range, plus the running station maximum), and it must then be re-gated against fresh recorded ladders. Nothing in this report supports it.

### 6.4 Buying the cheap tail — HALT

Workstream D found the far tail worth far more under the N/X mixture than under a single Gaussian (NY T90 at 10.7¢ vs 2.0¢ against a ~1¢ ask that is almost always available), which points at buying far-bracket YES — a shape the PRD did not anticipate. It was tested in both directions and in both regimes:

* `far-bracket YES (buy the tail), taker, >=12h`: modelled EV -4.51¢, realized **-6.20¢** on 813 trades / 69 dates.
* `far-bracket YES (buy the tail), maker, >=12h`: modelled EV -2.82¢, realized **-4.38¢** on 748 trades / 69 dates.

Under the mixture the modelled EV on the +EV subset turns positive (§8.5) and the realized PnL stays around −2.6¢ to −4.7¢. **The mixture's fat tail is real as a modelling correction but did not show up as realized outcomes in this window**, and the 1¢ adverse-fill allowance doubles the cost of a 1¢ contract before any of it matters. HALT.

---

## 7. Empirical tail calibration — the test workstream D said had never been run

Workstream D's gap #1: *"The distribution is Gaussian by assumption. No normality test was run on the 209 day-of residuals … a far-bracket strategy is a bet on the tails, and the tails are exactly where a Gaussian assumption is least defensible."* Every far-bracket EV above depends on that tail. Here is the test.

Realized frequency of large day-of forecast errors versus the frequency the calibrated model predicts. `error_f = forecast − truth`; both are whole degrees, so the modelled probability uses the same continuity correction the probability engine applies to its pmf. `binom p` is an exact two-sided test against the Gaussian prediction.

| city | \|error\| ≥ | n | observed | obs freq | Gaussian freq | obs/Gaussian | mixture freq | obs/mixture | binom p |
|---|---|---|---|---|---|---|---|---|---|
| NY | 3°F | 209 | 74 | 0.3541 | 0.4587 | 0.7718 | 0.4328 | 0.8182 | 0.0028 |
| NY | 4°F | 209 | 42 | 0.2010 | 0.2996 | 0.6708 | 0.2765 | 0.7267 | 0.0015 |
| NY | 5°F | 209 | 28 | 0.1340 | 0.1823 | 0.7349 | 0.1675 | 0.7998 | 0.0731 |
| NY | 6°F | 209 | 16 | 0.0766 | 0.1031 | 0.7426 | 0.0978 | 0.7831 | 0.2541 |
| NY | 8°F | 209 | 9 | 0.0431 | 0.0262 | 1.6419 | 0.0323 | 1.3335 | 0.1269 |
| CHI | 3°F | 209 | 75 | 0.3589 | 0.5302 | 0.6768 | 0.4633 | 0.7745 | 7.1e-07 |
| CHI | 4°F | 209 | 51 | 0.2440 | 0.3796 | 0.6429 | 0.3122 | 0.7816 | 4.3e-05 |
| CHI | 5°F | 209 | 32 | 0.1531 | 0.2586 | 0.5921 | 0.2043 | 0.7496 | 0.0003 |
| CHI | 6°F | 209 | 24 | 0.1148 | 0.1673 | 0.6862 | 0.1329 | 0.8641 | 0.0414 |
| CHI | 8°F | 209 | 13 | 0.0622 | 0.0597 | 1.0417 | 0.0607 | 1.0242 | 0.8834 |
| LAX | 3°F | 209 | 45 | 0.2153 | 0.2504 | 0.8598 | -- | -- | 0.2640 |
| LAX | 4°F | 209 | 20 | 0.0957 | 0.1076 | 0.8897 | -- | -- | 0.6557 |
| LAX | 5°F | 209 | 10 | 0.0478 | 0.0385 | 1.2423 | -- | -- | 0.4686 |
| LAX | 6°F | 209 | 6 | 0.0287 | 0.0114 | 2.5141 | -- | -- | 0.0340 |
| LAX | 8°F | 209 | 0 | 0.0000 | 0.0006 | 0.0000 | -- | -- | 1.0000 |
| MIA | 3°F | 208 | 24 | 0.1154 | 0.1450 | 0.7960 | -- | -- | 0.2778 |
| MIA | 4°F | 208 | 13 | 0.0625 | 0.0413 | 1.5138 | -- | -- | 0.1584 |
| MIA | 5°F | 208 | 3 | 0.0144 | 0.0087 | 1.6583 | -- | -- | 0.2716 |
| MIA | 6°F | 208 | 0 | 0.0000 | 0.0013 | 0.0000 | -- | -- | 1.0000 |
| MIA | 8°F | 208 | 0 | 0.0000 | 0.0000 | 0.0000 | -- | -- | 1.0000 |

Pooled walk-forward standardized residuals — the test of the **deployed** pipeline rather than of one annual block. `z = (error − bias) / σ` with the bias and σ the engine would actually have selected on that date (month → season → day-of chain, fitted only on earlier truth):

| \|z\| ≥ | n | observed | obs freq | N(0,1) freq | obs / normal |
|---|---|---|---|---|---|
| 1.0 | 595 | 167 | 0.2807 | 0.3173 | 0.8845 |
| 1.5 | 595 | 68 | 0.1143 | 0.1336 | 0.8553 |
| 2.0 | 595 | 31 | 0.0521 | 0.0455 | 1.1451 |
| 2.5 | 595 | 15 | 0.0252 | 0.0124 | 2.0299 |
| 3.0 | 595 | 6 | 0.0101 | 0.0027 | 3.7351 |

**Verdict: the residual distribution is leptokurtic — a peaked core and fat tails — and the Gaussian is wrong in both directions at once.**

* In the **shoulder** (|error| 3–6°F, |z| ≤ 2) the Gaussian **over**-predicts: observed is 0.59–0.89× predicted, significantly so for NY and CHI (p down to 7e-07).
* In the **extreme tail** it **under**-predicts: |z| ≥ 2.5 at 2.03× and |z| ≥ 3 at 3.74× the normal rate; NY at |error| ≥ 8°F at 1.64×; LAX at ≥ 6°F at 2.51× (p = 0.034); MIA's warm side at ≥ 4°F at 2.43× (p = 0.019).
* The N/X mixture is a genuine improvement in the shoulder for NY and CHI (ratios move from 0.64–0.77 toward 0.77–0.86) but does **not** fix the extreme tail.

**Consequence for this report, stated plainly: every far-bracket EV above is unreliable in a known direction.** The shape being recommended is short the extreme tail, and the extreme tail is the part of the distribution the model understates by 2–4×. That is not a reason to distrust the *realized* column — the realized column is what happened — but it is a decisive reason not to size Phase 3 from the modelled EV, and it is why the modelled EV is roughly double the realized PnL.

---

## 8. Robustness

### 8.1 In-sample vs walk-forward — the contamination gap

The headline shape, identical rule, in-sample calibration: 167 trades, realized +5.28¢, modelled EV +10.77¢. Walk-forward: 181 trades, realized +6.36¢, modelled EV +12.23¢.

**Embargo length.** The default rule withholds truth from the priced date onward. A 2-day embargo additionally withholds the previous day, whose NWS Climatological Report is only published on the morning the ladder is already open — i.e. it is arguably not available at the earliest snapshots either:

| embargo | mode | trades | dates | modelled EV | realized/ct | t | boot 95% CI |
|---|---|---|---|---|---|---|---|
| 1 day | taker | 181 | 65 | +12.23¢ | +6.36¢ | +2.57 | [+1.22¢, +10.86¢] |
| 1 day | maker | 131 | 60 | +16.62¢ | +8.55¢ | +2.77 | [+1.87¢, +14.36¢] |
| 2 days | taker | 183 | 66 | +12.26¢ | +6.05¢ | +2.28 | [+0.38¢, +11.05¢] |
| 2 days | maker | 133 | 61 | +16.64¢ | +8.67¢ | +2.86 | [+2.42¢, +14.36¢] |

One extra day of withheld truth moves the taker path's lower bound from +1.22¢ to +0.38¢ — still positive at the modelled order size, but close enough to zero that the single-contract variant (§8.2's fee assumption) crosses it. The result is therefore sensitive to a judgement call about the hour at which a CLI report becomes usable, which is not a quantity a strategy should be sensitive to.

Across the band table the gap is directional rather than dramatic: the walk-forward chain usually falls back to a wider seasonal or annual σ (mean σ 2.752°F walk-forward), which raises far-bracket P(YES), makes the 8-point filter *harder* to trigger, and therefore makes the walk-forward test the **more conservative** of the two on this shape. The in-sample table is published in §4.2 and carries no verdict.

### 8.2 Order size

| contracts | mean fee/ct | realized/ct |
|---|---|---|
| 1 | 0.0148 | +5.90¢ |
| 5 | 0.0109 | +6.29¢ |
| 20 | 0.0101 | +6.36¢ |
| 50 | 0.0100 | +6.37¢ |

The taker fee rounds up on the order **total**, so a C = 1 model overstates far-bracket cost. It moves the headline by under half a cent; the verdict does not depend on the size assumption.

### 8.3 Entry timing and slippage

Which snapshot inside the window is taken as the entry:

| entry rule | trades | dates | realized/ct | t |
|---|---|---|---|---|
| first executable snapshot | 181 | 65 | +6.36¢ | +2.57 |
| last executable snapshot | 181 | 65 | +7.75¢ | +3.39 |
| every executable snapshot | 1,576 | 65 | +7.86¢ | +2.91 |
| first 3 (FR-3.4 cap) | 473 | 65 | +6.68¢ | +2.61 |

Slippage beyond EC-5's mandated 1¢:

| total allowance | realized/ct | t |
|---|---|---|
| 1¢ | +6.36¢ | +2.57 |
| 2¢ | +5.36¢ | +2.16 |
| 3¢ | +4.36¢ | +1.76 |
| 4¢ | +3.36¢ | +1.36 |

The shape survives 2¢ and 3¢ of total slippage with the point estimate still positive, but the t-statistic decays through 2 at around 3¢. Execution quality is a first-order risk here, not a rounding detail.

### 8.4 Parameter sensitivity

| margin | dist °F | trades | dates | modelled EV | realized | t | boot 95% CI |
|---|---|---|---|---|---|---|---|
| 0.04 | 2 | 581 | 69 | +9.02¢ | +1.62¢ | +1.46 | [-0.51¢, +3.70¢] |
| 0.04 | 3 | 411 | 69 | +8.31¢ | +2.89¢ | +2.03 | [+0.06¢, +5.61¢] |
| 0.04 | 4 | 272 | 68 | +8.44¢ | +5.40¢ | +3.32 | [+2.17¢, +8.41¢] |
| 0.04 | 5 | 161 | 63 | +8.59¢ | +5.10¢ | +2.54 | [+1.00¢, +8.59¢] |
| 0.06 | 2 | 501 | 69 | +10.73¢ | +1.70¢ | +1.40 | [-0.59¢, +4.00¢] |
| 0.06 | 3 | 344 | 69 | +9.99¢ | +2.22¢ | +1.15 | [-1.89¢, +5.77¢] |
| 0.06 | 4 | 222 | 67 | +10.20¢ | +4.37¢ | +1.77 | [-0.67¢, +8.79¢] |
| 0.06 | 5 | 128 | 54 | +10.54¢ | +4.79¢ | +1.63 | [-1.39¢, +9.93¢] |
| 0.08 | 2 | 438 | 69 | +12.38¢ | +1.68¢ | +1.20 | [-1.04¢, +4.28¢] |
| 0.08 | 3 | 290 | 69 | +11.78¢ | +2.58¢ | +1.23 | [-1.65¢, +6.60¢] |
| 0.08 | 4 | 181 | 65 | +12.23¢ | +6.36¢ | +2.57 | [+1.22¢, +10.86¢] |
| 0.08 | 5 | 102 | 50 | +12.63¢ | +4.37¢ | +1.18 | [-3.43¢, +10.61¢] |
| 0.10 | 2 | 393 | 69 | +13.91¢ | +1.86¢ | +1.15 | [-1.25¢, +4.90¢] |
| 0.10 | 3 | 249 | 68 | +13.31¢ | +2.39¢ | +0.92 | [-2.80¢, +7.13¢] |
| 0.10 | 4 | 151 | 63 | +13.97¢ | +7.06¢ | +2.42 | [+0.90¢, +12.18¢] |
| 0.10 | 5 | 83 | 45 | +14.55¢ | +4.46¢ | +1.06 | [-4.30¢, +11.95¢] |
| 0.12 | 2 | 353 | 69 | +15.45¢ | +1.55¢ | +0.88 | [-1.87¢, +4.84¢] |
| 0.12 | 3 | 219 | 68 | +14.81¢ | +2.34¢ | +0.87 | [-3.05¢, +7.37¢] |
| 0.12 | 4 | 130 | 61 | +15.66¢ | +9.12¢ | +3.01 | [+2.64¢, +14.52¢] |
| 0.12 | 5 | 70 | 39 | +16.33¢ | +5.91¢ | +1.38 | [-2.96¢, +13.53¢] |
| 0.15 | 2 | 300 | 69 | +17.75¢ | +1.53¢ | +0.79 | [-2.25¢, +5.26¢] |
| 0.15 | 3 | 181 | 66 | +17.20¢ | +2.70¢ | +0.92 | [-3.03¢, +8.14¢] |
| 0.15 | 4 | 109 | 56 | +17.77¢ | +13.64¢ | +4.90 | [+7.95¢, +18.80¢] |
| 0.15 | 5 | 63 | 35 | +17.88¢ | +9.07¢ | +2.29 | [+0.71¢, +16.21¢] |

The **4°F distance condition is doing the work**: at `dist ≥ 4` the realized PnL is positive at every divergence margin from 4 to 15 points; at `dist` 2 or 3 it is +1.5 to +2.9¢ with a CI spanning zero. That the effect is strongest at exactly the pre-registered 4°F is reassuring (the parameters were not tuned here — they are the PRD's defaults) but `dist ≥ 5` is *weaker* than `dist ≥ 4`, which is not the monotone behaviour a clean structural effect would show. Treat the point estimate as noisy and the sign as the finding.

### 8.5 The N/X-window mixture — a second input the result is not robust to

| shape | trades | markets | dates | fill-opp | modelled EV | realized | SE | t | boot 95% CI | win | losing dates | worst date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FR-3.1(a) taker, NY+CHI, day-of lead, regime=single | 75 | 75 | 45 | 100.0% | +15.56¢ | +8.51¢ | +3.46¢ | +2.46 | [+1.11¢, +14.75¢] | 88.0% | 8/45 | -86.85¢ |
| far-bracket YES, NY+CHI, day-of lead, regime=single | 362 | 362 | 68 | 99.9% | -5.01¢ | -5.79¢ | +0.84¢ | -6.91 | [-7.37¢, -4.18¢] | 4.1% | 60/68 | -20.79¢ |
| FR-3.1(a) taker, NY+CHI, day-of lead, regime=mixture | 62 | 62 | 43 | 100.0% | +14.22¢ | -0.56¢ | +5.63¢ | -0.10 | [-11.99¢, +9.69¢] | 80.6% | 11/43 | -87.80¢ |
| far-bracket YES, NY+CHI, day-of lead, regime=mixture | 331 | 331 | 68 | 99.9% | -3.57¢ | -4.93¢ | +0.96¢ | -5.13 | [-6.76¢, -3.07¢] | 5.4% | 55/68 | -20.79¢ |

LAX and MIA are absent from this table by refusal, not omission: their outside-N/X-window sample is n=2, below the 20-day quorum, so the probability engine raises rather than fitting a second component.

Workstream D recommends `regime="mixture"` for NY and CHI, where ~18% of days set their maximum outside the window MOS `N/X` guidance covers and carry roughly double the σ plus a cold bias. Restricting to those two cities and the day-of lead, the **same** FR-3.1(a) rule realizes a materially positive number under the single Gaussian and a number indistinguishable from zero under the mixture. The two rules select overlapping but different trade sets (38 markets in common), and the difference is inside one standard error — but it means **the headline result is not robust to the regime model**, and the mixture is the better-specified of the two.

This is the same failure as §8.6 in a different variable: the shape's sign depends on a modelling choice the data cannot settle. Two independent such dependencies — the forecast source and the regime model — is what turns a promising point estimate into a HALT. Requirement R4 in §9.3 names what would close it.

### 8.6 Forecast source — does the verdict survive a worse forecast?

Workstream F's GEFS calibration is a second, independent forecast source for the same four cities and the same truth. It is priced here through the **identical** walk-forward harness — same ladders, same embargo, same availability conditioning, same fees, same 1¢ allowance — with only the forecast archive and the calibration artifacts swapped.

The σ that is actually being varied:

| source | city | day-of bias °F | day-of σ °F | walk-forward mean σ °F |
|---|---|---|---|---|
| gefs | CHI | -1.1041 | 3.7623 | 4.1334 |
| gfs_mex | CHI | 0.1340 | 3.9809 | 3.8548 |
| gefs | LAX | -0.3285 | 3.7722 | 3.7405 |
| gfs_mex | LAX | -0.3828 | 2.1409 | 2.1858 |
| gefs | MIA | -2.5034 | 2.4174 | 2.2031 |
| gfs_mex | MIA | -0.1683 | 1.7069 | 1.7457 |
| gefs | NY | 0.6767 | 4.1069 | 3.5162 |
| gfs_mex | NY | 0.0574 | 3.3736 | 3.2236 |

The same FR-3.1(a) rule, under each source:

| source | shape | trades | markets | dates | fill-opp | modelled EV | realized | t | boot 95% CI | win |
|---|---|---|---|---|---|---|---|---|---|---|
| gfs_mex | FR-3.1(a) far-bracket NO, taker, >=12h to close | 181 | 181 | 65 | 100.0% | +12.23¢ | +6.36¢ | +2.57 | [+1.22¢, +10.86¢] | 89.0% |
| gfs_mex | FR-3.1(a) far-bracket NO, maker, >=12h to close | 131 | 131 | 60 | 72.1% | +16.62¢ | +8.55¢ | +2.77 | [+1.87¢, +14.36¢] | 84.7% |
| gefs | FR-3.1(a) far-bracket NO, taker, >=12h to close | 210 | 210 | 64 | 100.0% | +15.35¢ | -6.22¢ | -1.84 | [-12.97¢, +0.22¢] | 71.4% |
| gefs | FR-3.1(a) far-bracket NO, maker, >=12h to close | 178 | 178 | 64 | 83.8% | +19.50¢ | -7.54¢ | -1.95 | [-15.18¢, -0.17¢] | 66.3% |

Per city (taker, one entry per market):

| source | city | trades | day-of σ °F | EC-3 σ ≤ 4°F | win | realized/ct (unclustered) | realized/ct (date-clustered) | modelled EV |
|---|---|---|---|---|---|---|---|---|
| gefs | CHI | 58 | 3.76 | pass | 79.3% | -1.48¢ | +0.90¢ | +11.59¢ |
| gfs_mex | CHI | 82 | 3.98 | pass | 84.1% | +4.83¢ | +5.70¢ | +13.56¢ |
| gefs | LAX | 72 | 3.77 | pass | 66.7% | -7.55¢ | -6.69¢ | +19.36¢ |
| gfs_mex | LAX | 33 | 2.14 | pass | 100.0% | +12.61¢ | +12.31¢ | +9.56¢ |
| gefs | MIA | 9 | 2.42 | pass | 100.0% | +10.83¢ | +10.64¢ | +7.43¢ |
| gfs_mex | MIA | 17 | 1.71 | pass | 100.0% | +9.76¢ | +8.80¢ | +8.53¢ |
| gefs | NY | 71 | 4.11 | FAIL | 66.2% | -9.12¢ | -10.25¢ | +15.35¢ |
| gfs_mex | NY | 49 | 3.37 | pass | 85.7% | +4.44¢ | +2.34¢ | +13.09¢ |

**This table is why this report says HALT.**

The same rule, the same 69 days, the same tape, the same walk-forward harness, the same fees and the same 1¢ allowance — and the sign of the realized PnL **reverses** when the forecast source changes. There is no reading of that under which the shape has a demonstrated edge: what was measured is not a property of the trade, it is a property of one forecast source's particular errors over one 69-day window.

Worse for FR-2.4 specifically: **the modelled EV scores the losing configuration higher than the winning one.** GEFS's modelled EV is the larger of the two while its realized PnL is negative. The quantity FR-2.4 gates on is therefore not merely optimistic (§0, §6.1) — across this comparison it is *anti*-correlated with the outcome. A gate that ranks a loser above a winner cannot authorize Phase 3.

The mechanism is not mysterious. Forecast skill over the traded window, measured on the same day-of pairs:

| source | city | days | MAE °F | bias °F | sd °F |
|---|---|---|---|---|---|
| gefs | CHI | 68 | 1.85 | -0.33 | 2.46 |
| gfs_mex | CHI | 68 | 2.15 | 1.82 | 2.28 |
| gefs | LAX | 68 | 2.78 | 1.14 | 3.27 |
| gfs_mex | LAX | 68 | 1.13 | -0.40 | 1.51 |
| gefs | MIA | 68 | 3.22 | -3.22 | 1.01 |
| gfs_mex | MIA | 68 | 1.19 | -0.75 | 1.32 |
| gefs | NY | 68 | 3.71 | 3.63 | 2.38 |
| gfs_mex | NY | 68 | 2.29 | 1.26 | 2.64 |

The shape's PnL tracks forecast skill, which is what a real information edge *should* do — and is exactly why it cannot be banked here. **Which source is the skilful one was determined by looking at this data.** The PRD's designated primary source is the ensemble (FR-2.1); workstream F measured it as the worse forecast at 3 of 4 cities *after* building it. Selecting `gfs_mex` and then reporting its PnL is a choice made with knowledge of the outcome, and it is the single largest unaccounted-for degree of freedom in the positive result.

**Is there an ex-ante rule that disqualifies the losing source?** The strongest one available is EC-3's own 4°F day-of σ bound, which `gefs` fails at NY and passes at CHI, LAX and MIA (column `EC-3` above). Dropping NY — the only city the bound excludes — leaves:

| source | cities | trades | dates | realized/ct | t | boot 95% CI |
|---|---|---|---|---|---|---|
| gfs_mex | CHI, LAX, MIA | 132 | 62 | +7.56¢ | +2.83 | [+1.89¢, +12.28¢] |
| gefs | CHI, LAX, MIA | 139 | 60 | -3.45¢ | -0.95 | [-10.78¢, +3.35¢] |

The bound removes the sample, not the loss. `gefs` is still negative over the three cities where it **passes** EC-3, and the per-city table shows why: the bound has no purchase on LAX, where `gefs` clears it at 3.77°F and realizes -6.69¢ on 72 trades. **There is therefore no ex-ante rule on the table that disqualifies `gefs` and leaves `gfs_mex` standing** — which is precisely what §9.2 turns on, and precisely why R2 in §9.3 is a weaker escape clause than it reads.

**A post-hoc observation, recorded because it is the most promising lead in this report and flagged so nobody mistakes it for a finding — and because the ordering in which the filter is applied changes what it says:**

| source | σ ≤ 4°F applied | all trades | surviving trades | dates | realized/ct (unclustered) | realized/ct (date-clustered) | t |
|---|---|---|---|---|---|---|---|
| gfs_mex | post-selection (as first observed) | 181 | 116 | 53 | +9.15¢ | +8.64¢ | +3.30 |
| gfs_mex | pre-selection (registered ordering) | 181 | 119 | 53 | +9.32¢ | +8.63¢ | +3.30 |
| gefs | post-selection (as first observed) | 210 | 45 | 34 | +8.69¢ | +10.02¢ | +2.33 |
| gefs | pre-selection (registered ordering) | 210 | 90 | 48 | -2.20¢ | -0.27¢ | -0.06 |

σ is an **ex-ante observable** — it is known before the trade — so there are two defensible places to apply the bound, and they are not the same rule:

* **post-selection**: take the FR-3.1(a) entries first (one per market), then discard those whose selected calibration bucket carries σ > 4°F. This is the ordering in which the observation was first made.
* **pre-selection**: apply the σ bound to the *candidate pool* first, then take one entry per market from what survives. Because σ is ex-ante, a live strategy would do it this way, and this ordering can select a **different snapshot** as the entry for a market whose earlier snapshots are filtered out.

**The pre-selection ordering is the one registered in §9.3 R3.** Under it the claim that the filter "turns both sources positive" does not survive: the mechanism is a change of entry vintage, not a change of skill.

| source | trade set | trades | day_of | lead_12_36 |
|---|---|---|---|---|
| gfs_mex | post-selection (as first observed) | 116 | 56 | 60 |
| gfs_mex | pre-selection (registered ordering) | 119 | 59 | 60 |
| gfs_mex | unfiltered | 181 | 67 | 114 |
| gefs | post-selection (as first observed) | 45 | 42 | 3 |
| gefs | pre-selection (registered ordering) | 90 | 87 | 3 |
| gefs | unfiltered | 210 | 45 | 165 |

Read the composition table. Under `gefs` the unfiltered rule enters predominantly on the **previous day's 12Z vintage** (`lead_12_36`), whose σ exceeds 4°F nearly everywhere; the post-selection filter therefore deletes most of those entries and what is left is almost entirely the **day-of** vintage. So under `gefs` the post-selection result is not "the same rule with a σ filter" — it is a materially later-entry rule measured on a fifth of the sample, and §8.3 already shows entry timing alone moves this shape. `gfs_mex`'s σ ≤ 4°F result is robust to the ordering; `gefs`'s is not.

The coherent story behind the filter is unchanged — the divergence signal should only be worth trading where the forecast is actually skilful, and σ is an ex-ante quantity that says so. But it is a filter discovered by inspecting these results, it is **not** pre-registered, it is **not** in FR-3.1(a), and it does **not** carry any part of this verdict. §9.3 R3 registers it, in the pre-selection ordering, for a future test on data this report has never seen.

One source-dependent caveat that bears directly on §8.5: workstream F measures GEFS's full-day `TMAX` as materially improving the overnight-maximum regime at CHI (regime σ 6.43 → 4.23°F). **The N/X-window regime penalty is therefore a property of the source, not of the city** — the mixture disagreement in §8.5 is specific to the MOS `N/X` daytime window and would be smaller on a full-day source. Requirement R1 in §9.3 must therefore be settled before the regime question in §8.5 can be, since the answer depends on which source is frozen.

Workstream F's own recommendation — an unweighted two-source blend at NY and CHI, `gfs_mex` alone at LAX and MIA — is **not** evaluated here, because F states the recommendation is in-sample and this report will not carry an in-sample input into a walk-forward verdict. Blending is a Phase 3 question and needs its own walk-forward gate.

### 8.7 Distribution of outcomes, not just the mean

16 of 65 dates lose money; the worst single date is -74.40¢/contract. This is a short-tail payoff — many small wins funded by rare large losses — which is exactly the shape whose risk the miscalibrated extreme tail (§7) understates. Excluding the worst 3 dates raises the mean by roughly half; that is a statement about concentration, not a permission to exclude them.

**How wrong the model is about the loss leg specifically.** For a buy-NO shape a loss is exactly "the bracket settled YES", so the model's mean P(YES) on the trades that fired and the realized YES rate on the same trades are one quantity measured two ways. One entry per market, taker, ≥12 h to close:

| source | trades | model P(loss) | realized loss rate | understatement | losing trades | mean PnL on a losing trade |
|---|---|---|---|---|---|---|
| gfs_mex | 181 | 0.0543 | 0.1105 | 2.04× | 20 | -76.52¢ |
| gefs | 210 | 0.0761 | 0.2857 | 3.76× | 60 | -73.65¢ |

Under both sources the model is wrong in the direction that flatters a short-tail trade: it predicts fewer losses than occurred, by a factor of two to four, and each loss that does occur costs most of the notional. §7 measures the same defect in the forecast residuals; this measures it on the trades, which is where it would be sized from. A positive mean computed from a loss probability that is understated 2–4× is not a quantity Phase 3 may be sized on, and that is true under the *winning* source as well as the losing one.

### 8.8 Where the headline t-statistic actually comes from

The pooled significance in §6 is reported city-set by city-set below. §10.2 notes that MIA and LAX post 100% win rates on small counts; this is what that does to the headline number. Same rule, same source, same harness, date-clustered inference throughout.

| city set | trades | dates | modelled EV | realized/ct | SE (date-clustered) | t | boot 95% CI | win |
|---|---|---|---|---|---|---|---|---|
| NY + CHI | 131 | 60 | +13.38¢ | +4.76¢ | +3.33¢ | +1.43 | [-2.42¢, +10.76¢] | 84.7% |
| LAX + MIA | 50 | 33 | +9.21¢ | +11.15¢ | +0.88¢ | +12.70 | [+9.56¢, +12.92¢] | 100.0% |
| pooled (all four) | 181 | 65 | +12.23¢ | +6.36¢ | +2.48¢ | +2.57 | [+1.22¢, +10.86¢] | 89.0% |

**A 100% win rate contributes a t-statistic through the absence of loss variance, not through the size of an edge.** LAX and MIA supply 50 trades on 33 dates with no losing trade at all; their per-date means are near-constant, their standard error collapses, and t = +12.70. That is a small-sample artifact (§10.2), not a stronger signal — and it is what carries the pooled t = +2.57.

Meanwhile the **majority** of the sample — NY and CHI, 131 of 181 trades (72%) on 60 dates — realizes +4.76¢ at t = +1.43, with a bootstrap CI of [-2.42¢, +10.76¢] that **spans zero**. The two cities that dominate the sample do not, on their own, demonstrate an edge.

This does not make the pooled point estimate wrong; it makes the pooled *significance* something other than what it appears to be. Reported here because §6's table alone would let a reader take t = +2.57 as evidence about the shape rather than about two cities' unbeaten streaks over a 69-day warm season.

---

## 9. The decision, and the named path back

### 9.1 What HALT means operationally, per FR-2.4

| action | authority |
|---|---|
| Phase 3 (weather trading) does **not** start. | FR-2.4: "if none qualify, weather halts and Phase 4 (gas) becomes flagship" |
| **Phase 4 — AAA gas convergence — becomes the flagship** and is the next phase executed. | FR-2.4; PRD §1 already names gas the second engine |
| `weather_bot` stays **feed-only**. No FR-3.1 strategy is registered, no weather signal reaches the risk manager. | Phase 0 teardown state; unchanged by this report |
| **Keep harvesting.** The ladder archive is the binding scarce resource and its upstream retention window is rolling — one day of recoverable history is lost per day. Continue `backfill_ladders.py` daily and continue the CLI-truth and forecast backfills for all four cities. | PRD goal 6; workstream C §9.2 |
| Nothing built in Phase 2 is discarded. The calibration pipeline, probability engine, ladder archive and this harness are the instruments that produced an honest negative and are what a future re-test runs on. | — |

Estimated sunk cost of the halt path, as the PRD budgeted it: 2–3 weeks. That is what was spent, and it bought a defensible negative plus a reusable measurement stack.

### 9.2 Why this is a HALT and not a scoped PROCEED

A scoped PROCEED was drafted and rejected. **The case for it is stronger than an earlier revision of this report credited**, and it is recorded here at full strength: Phase 3 deploys no capital, its own exit criterion is truthful evidence rather than positive PnL, a 30-day forward paper run is exactly the out-of-sample sample this evidence lacks, and running it would settle R1, R3 and R5 in §9.3 far faster than waiting does. Against a shape whose pooled point estimate is positive under the better forecast, that is a real argument and not a straw one.

It fails on one point, and the point was **tested rather than asserted**:

> A PROCEED would have to name **which forecast source** the strategy trades. There is no defensible way to make that choice from this data. Picking the one that made money is picking on the outcome; picking the PRD's designated primary (FR-2.1's ensemble) picks the one that lost money. A phase that cannot specify its own primary input is not ready to start, and "start it and see" is how four prior review cycles produced numbers that were not what they claimed to be.

The obvious rescue is an **ex-ante** disqualification of `gefs` — a rule usable before the outcome is known. The strongest one this project owns is EC-3's 4°F day-of σ bound, which `gefs` fails at NY. It was applied and it does not work (§8.6):

* NY is the **only** city the bound excludes. Dropping it leaves `gefs` at -3.45¢, t = -0.95 (139 trades / 60 dates) — still negative, still indistinguishable from zero. The disqualification removes the sample, not the loss.
* The bound has no purchase on the city that supplies most of the remaining loss: `gefs` **passes** EC-3 at LAX (day-of σ 3.77°F, inside the bound) and realizes -6.69¢ there on 72 trades.
* At the remaining cities that pass the bound the result is not evidence either way: CHI +0.90¢ on 58 trades; MIA +10.64¢ on 9 trades.

**There is no ex-ante rule on the table that disqualifies `gefs` and leaves `gfs_mex` standing.** That is what makes this section survive: the choice of source cannot be defended forward, only backward, and a phase authorized on a backward-defended input is the failure mode this project has already produced four times.

### 9.3 The path back — pre-registered now, before any new data exists

This is written before the data that would test it exists, which is the only condition under which it means anything. Weather may be re-gated when **all five** hold:

| id | requirement |
|---|---|
| R1 | **Source specified ex ante.** One forecast source (or one fixed blend rule) named and frozen *before* the evaluation window, with its choice justified on data predating that window. Workstream F's blend recommendation is in-sample and does not qualify as written. |
| R2 | **Sign stability across sources.** The rule's realized PnL is positive under *every* calibrated source on disk, or the report disqualifies a source using an ex-ante criterion **and demonstrates that the disqualification is what removes the loss** — i.e. the surviving cities or dates are themselves profitable under the disqualified source's own terms. The escape clause is deliberately narrow: EC-3's 4°F σ bound is an ex-ante criterion, but §8.6 shows that applying it to the present data removes the sample rather than the loss (`gefs` loses at CHI and LAX, where it passes the bound). An ex-ante criterion that happens to be satisfiable is not enough; "it lost money" is never enough. |
| R3 | **The σ ≤ 4°F bucket filter, tested as a pre-registered rule, applied PRE-selection.** σ is an ex-ante observable, so the registered rule applies the bound to the *candidate pool* and then takes one entry per market. §8.6 measures both orderings and they disagree: post-selection the filter turns both sources positive, pre-selection it does not, because under `gefs` post-selection filtering silently converts the rule into a later-entry rule. The pre-selection ordering is the registered one; it must be tested on ladder dates outside 2026-05-18…2026-07-25 — which accrue at one per day from the ongoing harvest — and a post-selection result may not be reported in its place. |
| R4 | **A tail model that passes §7's test**, or an explicit fat-tailed distribution (Student-t or the N/X mixture fitted walk-forward) whose own tail ratios sit inside [0.8, 1.25] at \|z\| ≥ 2.5. A short-tail trade may not be underwritten by a distribution that understates its extreme tail by 2–4×. |
| R5 | **An out-of-season sample.** At least one autumn or winter month of recorded ladders, with per-city day-of σ re-measured on it. The entire present result is 69 warm-season days, and CHI's σ is 4.59°F on the cold half of the calibration sample. |

Two further items are prerequisites for any *execution* claim, independent of the signal: measured top-of-book **depth** (§10.5, entirely absent from this dataset) and an **intra-day forecast update** without which FR-3.1(b) cannot exist at all (§6.3).

### 9.4 What would have flipped this to PROCEED

Recorded so the verdict is falsifiable rather than merely cautious. Any **one** of these, measured, would have carried a scoped PROCEED:

* The far-bracket NO shape realizing positive PnL under **both** calibrated sources (it realizes +6.36¢ under `gfs_mex` and -6.22¢ under `gefs`).
* The modelled EV **ranking** the two source configurations correctly, which would have made FR-2.4's own gate quantity informative (it ranks them backwards).
* An **ex-ante** criterion that disqualifies `gefs` and leaves `gfs_mex` standing. EC-3's σ bound was applied and does not do it (§8.6, §9.2).
* The majority sub-sample carrying its own weight: NY + CHI alone clearing zero, instead of +4.76¢ at t = +1.43 with a CI of [-2.42¢, +10.76¢] (§8.8).
* The model's realized loss rate landing near its modelled one instead of 2–4× above it (§8.7).
* The N/X mixture agreeing with the single Gaussian at NY and CHI rather than collapsing the result to zero (§8.5).
* The tail test in §7 coming back inside a factor of ~1.25 instead of 3.7× at |z| ≥ 3.0.

---

## 10. Limitations — every one of them, in the section you cannot miss

### 10.1 The season is not the year.

69 consecutive days, 2026-05-18 to 2026-07-25 — late spring and summer only. SON is entirely absent from the calibration and DJF is thin. Workstream B measured CHI's day-of σ at 4.59°F on the cold half of the sample (failing EC-3's 4°F bound) against 2.78°F on the warm half. Nothing here is validated outside the warm season, and the ladder retention window is rolling, so this dataset cannot be extended backwards.

### 10.2 The effective sample is far smaller than the row count.

62,932 ladder rows reduce to 181 fired trades on 181 markets across 65 dates under the headline source. Per-city counts fall to the tens (MIA is the smallest, at 17), and under `gfs_mex` both MIA and LAX post 100% win rates on those counts — a small-sample artifact, not a stronger edge. Read every per-city number as indicative only; a handful of trades moves any of them. **Those two unbeaten sub-samples are also what produces the headline t-statistic** — §8.8 decomposes it, and the majority sub-sample (NY + CHI) has a bootstrap CI that spans zero.

### 10.3 The forecast source is a free parameter, and it is the decisive one.

The headline is raw GFS extended MOS (`gfs_mex`), chosen by workstream B because it is the only archived model populating `n_x` for all four stations. The PRD's designated primary source is the GEFS ensemble (FR-2.1), which workstream F built and measured as the *worse* forecast at 3 of 4 cities. Both are priced here and they disagree on the sign of the only positive shape (§8.6). Nothing in this dataset selects between them on an ex-ante basis, and that unresolved choice — not any single number — is what produces the HALT.

### 10.4 σ is a bucket constant, and no per-day uncertainty exists in measured form.

The MOS calibration blocks report `mean_spread_f: null` because the IEM archive publishes no spread. GEFS *does* publish one (`gespr`), and workstream F measured it at 0.19–0.37× the realized error σ with correlation to |error| of −0.013 / +0.065 / +0.022 / −0.197 across the four cities — i.e. no skill. So a quiet high-pressure day and a frontal-passage day are priced with an identical σ, and nothing available fixes that. Every σ in this report is the calibrated per-bucket `sigma_f`; the probability engine is called with `require_published_spread=False` and no code path here reads `spread_f` or `mean_spread_f` as a dispersion.

### 10.5 Depth is unmeasured, and it is the largest hole.

The candlestick feed carries quotes but no book depth, and historical depth cannot be recovered. Every EV here assumes the modelled order size is available at the recorded quote. Median hourly volume is 133–538 contracts per snapshot, which makes a 20-contract order plausible, but plausible is not measured.

### 10.6 The maker fill model is a proxy, not an observation.

A resting order is deemed filled if a later hourly snapshot shows the other side crossing its limit. That is a lower bound on instantaneous fills (intra-hour traversals are invisible) and an upper bound on durable ones (queue position is unobservable), and it is forward-looking, so maker modelled EV carries an adverse-selection bias. The headline verdict is taken from the taker path for exactly this reason.

### 10.7 The mixture path is itself in-sample.

Workstream D's `NX_WINDOW_REGIME` constants are fitted over the whole 209-day archive and are a module constant, so the mixture results in §8.5 are *not* walk-forward. Making them so requires a change in `src/calibration/probability_engine.py`, which workstream E does not own; the proposed change is in §11.

### 10.8 Hourly resolution understates availability and overstates persistence.

Quotes are the close of each hourly candle. A quote that existed for ten minutes and vanished is recorded as absent; a quote that vanished mid-hour is recorded as present. Workstream C can supply 1-minute data at ~60× the request count if Phase 3 needs finer execution modelling.

### 10.9 This is a backtest, and the model never traded.

No order was placed and no live signal was generated. Market impact, queue position, partial fills, API latency, and the reflexive effect of the strategy's own orders on a thin far-bracket book are all outside this measurement.

### 10.10 Multiple shapes were examined, and one choice was made after seeing the answer.

Four directions × two modes × six bands × six windows were computed, plus a 24-cell parameter sweep and two forecast sources. The FR-3.1(a) rule itself is protected from that search by being **pre-registered in the PRD with its default parameters**. Two things are not: the ≥12h window restriction (justified by the model being frozen intra-day, §3.1, and conservative — the 6–12h cell is better, not worse) and, decisively, **the choice of forecast source**, which could only be made after measuring which one worked. The σ ≤ 4°F bucket filter in §8.6 is likewise post-hoc and is registered as a hypothesis in §9.3 rather than used. Individual p-values in §8.4 and the band tables in §4 are not corrected for multiplicity.

---

## 11. Changes to files workstream E does not own

**One was made, and it changes no behaviour.** `src/calibration/probability_engine.py`'s module docstring stated EC-4's acceptance claim as "doubling sigma measurably flattens the distribution" without naming the metric or its precondition. Entropy over the ladder does rise under σ-doubling in every configuration probed; the largest-*finite*-bracket criterion the EC-4 test also asserts holds only while μ sits inside the ladder. With μ placed 25°F below the ladder centre it reverses on **all ten** recorded EC-4 ladders — on KXHIGHNY-26JUL17 the largest finite bracket *rises* from 4.21e-18 to 7.24e-06 — because doubling σ moves mass back *toward* a ladder the forecast had abandoned. The reversal is not confined to the far field: it already appears at 10°F outside the ladder, again on 10/10. The docstring now names which metric carries the claim and under what condition, and a test pins the counterexample. No engine behaviour and no existing assertion changed. EC-4 rests on entropy, which rose under σ-doubling in every configuration probed.

Two further changes are proposed and were **not** made.

**(a) `src/calibration/probability_engine.py` — make the N/X regime walk-forwardable.** `NX_WINDOW_REGIME` is a module constant fitted on the whole archive, so §8.5's mixture numbers are in-sample (§10.7). `recompute_nx_window_regime()` already exists and already accepts the archives; it needs an `as_of` cutoff and `_build_components` needs to accept an injected regime table:

```diff
-def recompute_nx_window_regime(...) -> Dict[str, RegimeRisk]:
+def recompute_nx_window_regime(..., as_of: Optional[str] = None) -> Dict[str, RegimeRisk]:
+    # as_of: keep only paired days with target_date < as_of, so a mixture
+    # priced for date D is not parameterized on D's own outcome.

-def bracket_probabilities_point(*, ..., regime=REGIME_SINGLE, ...):
+def bracket_probabilities_point(*, ..., regime=REGIME_SINGLE,
+                                regime_table: Optional[Mapping[str, RegimeRisk]] = None, ...):
+    # regime_table overrides NX_WINDOW_REGIME; None keeps today's behaviour.
```

**(b) `.gitignore` — none needed for this workstream's outputs.** `reports/phase2/*.md` and the JSON sidecar are small and are the artifact.

---

## 12. Reproduction

```powershell
$env:PYTHONPATH = "."
$env:OMP_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"
$env:MKL_NUM_THREADS = "2"; $env:NUMEXPR_NUM_THREADS = "2"

# regenerate this artifact end to end (~2 minutes)
python scripts/go_no_go.py

# under the second calibration source (workstream F's GEFS)
python scripts/go_no_go.py --source gefs

# sensitivity: strict two-day embargo, single-contract fees
python scripts/go_no_go.py --embargo-days 2 --contracts 1

# tests (this file only -- never the full suite on this machine)
python -m pytest tests/test_ev_analysis.py -v
```

**One environment note that affects reproduction, not results.** On the pinned pandas 2.3.3 / numpy 2.4.0 / CPython 3.14 stack, `pandas.merge_asof` faults with a Windows access violation in its native join path after the first call in a process, and this report needs three forecast-vintage joins. `ev_analysis._asof_backward` therefore performs the backward as-of join with `searchsorted` instead. It is pinned against `merge_asof` itself in `tests/test_ev_analysis.py`, and on the real archive the two agree row for row, value for value and dtype for dtype (10,750 rows under each source). No number in this report changed.

Machine-readable companion: `reports/phase2/ws_e_go_no_go_data_2026-07-26.json` — every table above as records, plus the provenance block and the worked example's intermediates.
