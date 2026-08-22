# Workstream D — probability engine (FR-2.3) and the Phase 2 EC-4 evidence

**Phase:** 2 (Forecast engine & calibration + go/no-go)
**Date:** 2026-07-26
**Branch:** `phase-2-forecast-calibration` (no git write command was run)
**Deliverable:** `src/calibration/probability_engine.py`, `tests/test_probability_engine.py`,
`tests/fixtures/ladders_probability/recorded_ladders.json`

Every number in this report was measured on this machine on 2026-07-26. Nothing
is an estimate, and nothing is carried over from another workstream's report
without being recomputed here.

---

## 0. Verdict up front

PRD §8, Phase 2, exit criterion 4, verbatim:

> **4.** Probability engine: for 10 recorded ladders, bracket probabilities sum
> to 1.0 ± 0.01 and are monotonically consistent with the calibrated CDF; a
> perturbation test (σ doubled) measurably flattens the distribution.

| Sub-claim | Verdict | Measured |
|---|---|---|
| 10 recorded ladders | **MET** | 10 real Kalshi events, 60 markets, 4 cities, captured read-only from `api.elections.kalshi.com` |
| Sum to 1.0 ± 0.01 | **MET** | worst \|1 − Σ P\| = **1.11e-16** over the 10 headline cases; **0.0** over a 280-case sweep (10 ladders × 7 μ offsets × 4 measured σ) |
| Monotonically consistent with the calibrated CDF | **MET** | worst \|cumulative − Φ(edge)\| = **1.11e-16** at every bracket boundary, against an analytic Φ recomputed in the test |
| σ doubled measurably flattens | **MET, with one metric corrected** | entropy ↑ on 10/10 (e.g. CHI 1.78341 → 2.43475 bits); finite-bracket max ↓ on 10/10 (CHI 0.46780 → 0.24827); pmf peak ↓ on 10/10 (halves) |

**EC-4 is MET.** One correction is carried in the open, in §5: the naive metric
"max bracket probability strictly decreases" is **false** on a ladder with
open-ended tail contracts, and there is a committed test that pins the
counterexample so nobody can "fix" a future failure by adopting it.

**44/44 tests pass** (`tests/test_probability_engine.py` only — the full suite is
prohibited on this machine). **9 of 10 seeded mutations were caught**; the tenth
gap was real, and is now closed (§8).

---

## 1. The API workstream E calls

```python
from src.calibration.probability_engine import (
    load_city_calibration, specs_from_markets,
    bracket_probabilities_point, bracket_probabilities_ensemble,
    validate_ladder_partition,
)

calibration = load_city_calibration("NY")                  # source="gfs_mex", version=1
specs       = specs_from_markets(markets)                  # raw Kalshi market dicts

result = bracket_probabilities_point(
    city="NY",
    target_date="2026-07-17",          # str | date | datetime
    forecast_high_f=86.5,
    specs=specs,
    calibration=calibration,
    lead_hours=None,                   # None -> the day-of chain
    regime="single",                   # "single" | "mixture" | "mixture_if_available"
    forecast_source="gfs_mex",         # enables the source-mismatch warning
    require_published_spread=False,
    support_sigmas=8.0,
)

result = bracket_probabilities_ensemble(
    city="NY", target_date="2026-07-17",
    members_f=(84.1, 85.0, 86.7, ...), # workstream A's EnsembleForecast.members_f
    specs=specs, calibration=calibration,
)                                      # same return type; mode == "ensemble"
```

### `BracketProbabilities` (frozen dataclass)

| Field | Meaning |
|---|---|
| `city`, `target_date` | echoed inputs, normalized |
| `mu_f`, `sigma_f` | the distribution's mean and standard deviation. For a mixture, `sigma_f` is the law-of-total-variance σ, not a component's |
| `mode` | `"point"` \| `"ensemble"` |
| `regime_model` | `"single_normal"` \| `"mixture_nx_window"` \| `"single_normal (mixture unavailable: …)"` |
| `calibration_bucket` | e.g. `"by_month_day_of:2026-07"`, `"day_of"`, `"by_lead:lead_60_84"` |
| `calibration_source` | the artifact's `source` field (`"gfs_mex"`) |
| `probabilities` | `{market_ticker: P(YES)}` |
| `pmf` | `{integer high: mass}` over the truncated support |
| `pmf_tail_mass` | mass outside the support. Conserved, not dropped: `Σ pmf + pmf_tail_mass == 1` |
| `uncovered_mass` | `1 − Σ probabilities`. Zero for a complete ladder; **positive** = gap or missing tail; **negative** = overlap |
| `partition` | a `PartitionReport` |
| `components` | the `NormalComponent`s actually used (1 or 2) |
| `calibration` | the `CalibrationSelection`, incl. `fallback_chain` |
| `regime_risk` | the measured `RegimeRisk` for the city (may be `None` for an unknown city) |
| `ensemble` | `EnsembleDiagnostics` in ensemble mode, else `None` |
| `warnings` | every caveat, as strings, always populated |

Methods: `p_yes(ticker)` (raises on an unknown ticker — never returns 0.0),
`cdf(high_f)` (closed-form discrete CDF, exact in the far tails),
`entropy_bits()`, `max_probability()`, `as_dict(nd=6)` (JSON-serializable; the
only place rounding happens).

`validate_ladder_partition(specs) -> PartitionReport` carries `complete`,
`n_brackets`, `has_low_tail`, `has_high_tail`, `covered_low`/`covered_high`
(`None` = unbounded), `gaps`, `overlaps`, `issues`.

Everything raises `ProbabilityEngineError` rather than degrading. There is no
silent path anywhere in this module.

---

## 2. The 10 recorded ladders — EC-4 sub-claims 1 and 2

Captured read-only via
`GET https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=<EVENT>&limit=200`
(anonymous, production) and persisted at
`tests/fixtures/ladders_probability/recorded_ladders.json` so the tests are
offline-deterministic. 10 events, 4 cities, 6 markets each = 60 bracket specs.

μ is the ladder centre debiased through the selected calibration block; σ is that
block's `sigma_f`. `F` is Φ recomputed in the test from `math.erfc`, **not** from
the engine's pmf.

| # | event ticker | city | partition | bucket | μ °F | σ °F | Σ P | \|1 − Σ P\| | max\|cum − F\| | uncovered |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | KXHIGHCHI-26JUL17 | CHI | complete | by_month_day_of:2026-07 | 91.208 | 1.5737 | 1.000000000000000 | 0.00e+00 | 1.11e-16 | 0.00e+00 |
| 2 | KXHIGHCHI-26JUL20 | CHI | complete | by_month_day_of:2026-07 | 85.208 | 1.5737 | 1.000000000000000 | 0.00e+00 | 1.11e-16 | 0.00e+00 |
| 3 | KXHIGHCHI-26JUL23 | CHI | complete | by_month_day_of:2026-07 | 78.208 | 1.5737 | 1.000000000000000 | 0.00e+00 | 1.11e-16 | 0.00e+00 |
| 4 | KXHIGHLAX-26JUL18 | LAX | complete | by_month_day_of:2026-07 | 76.042 | 1.9332 | 1.000000000000000 | 1.11e-16 | 1.11e-16 | 1.11e-16 |
| 5 | KXHIGHLAX-26JUL21 | LAX | complete | by_month_day_of:2026-07 | 77.042 | 1.9332 | 1.000000000000000 | 1.11e-16 | 1.11e-16 | 1.11e-16 |
| 6 | KXHIGHMIA-26JUL18 | MIA | complete | by_month_day_of:2026-07 | 92.083 | 1.4720 | 1.000000000000000 | 0.00e+00 | 1.11e-16 | 0.00e+00 |
| 7 | KXHIGHMIA-26JUL21 | MIA | complete | by_month_day_of:2026-07 | 93.083 | 1.4720 | 1.000000000000000 | 0.00e+00 | 1.11e-16 | 0.00e+00 |
| 8 | KXHIGHNY-26JUL17  | NY  | complete | by_month_day_of:2026-07 | 85.167 | 2.5988 | 1.000000000000000 | 0.00e+00 | 0.00e+00 | 0.00e+00 |
| 9 | KXHIGHNY-26JUL20  | NY  | complete | by_month_day_of:2026-07 | 79.167 | 2.5988 | 1.000000000000000 | 0.00e+00 | 0.00e+00 | 0.00e+00 |
| 10 | KXHIGHNY-26JUL23 | NY  | complete | by_month_day_of:2026-07 | 81.167 | 2.5988 | 1.000000000000000 | 0.00e+00 | 0.00e+00 | 0.00e+00 |

**Worst \|1 − Σ P\| = 1.11e-16. Worst \|cumulative − F\| = 1.11e-16.** The criterion
allows 0.01; the engine is at machine epsilon.

### Why "sum to 1" is structural rather than lucky

`test_ec4_the_ten_recorded_ladders_are_complete_partitions` runs **first** and
asserts every ladder is a partition of the integer line — one `less` at the
bottom, contiguous `between` brackets, one `greater` at the top, no gap, no
overlap. All 10 pass. Only then is sum-to-1 meaningful: a ladder with a hole
would sum to less than 1, and the *data* would be at fault, not the engine.

`test_uncovered_mass_is_reported_and_never_renormalized` builds a ladder with a
deliberate 84–86 hole and asserts the deficit equals
`Φ((86.5−85)/2) − Φ((83.5−85)/2) = 0.546745` exactly, that Σ P < 0.5, and that the result carries
a `"not a partition"` warning. Nothing is scaled back up to 1.
`test_overlapping_ladder_reports_negative_uncovered_mass` covers the other sign.

### The sum-to-1 sweep — 280 cases

Because the headline cases all sit with μ inside the ladder, the sweep walks μ
from 40 °F below the ladder to 40 °F above it (offsets −40, −8, −3, 0, +3, +8,
+40) across all four measured day-of σ (MIA 1.7069, LAX 2.1409, NY 3.3736,
CHI 3.9809). That exercises the case where an open-ended tail contract holds
essentially all the mass.

**280 cases, worst \|1 − Σ P\| = 0.0 (exact).**

### Cross-check against workstream C (read-only, not a test dependency)

C's archive landed while this work was in progress. All 60 bracket specs in my
independently-fetched fixture were compared against `data/ladders/<SERIES>/<date>.csv`:
**60/60 agree on `strike_type`, `floor_strike`, and `cap_strike`; 0 mismatches;
0 absent.** The tests do not import `src/data/kalshi_history.py` and do not read
`data/ladders/`.

---

## 3. The design decisions, and what each one assumes

### 3.1 The outcome is a discrete integer

Kalshi settles on the CLI daily maximum as a whole degree, and every bracket edge
is an integer. The engine builds a pmf over integer highs with a continuity
correction:

```
P(H = k) = Φ((k + 0.5 − μ)/σ) − Φ((k − 0.5 − μ)/σ)
```

Measured cost of getting this wrong: with μ = 85, σ = 3, bracket [85, 86] is
worth **0.257646** with the correction and **0.130559** without it — a
**12.7-point** mispricing on a single bracket, in the same direction on every
bracket in the ladder. Pinned in
`test_pmf_is_an_integer_interval_mass_not_a_density`.

### 3.2 Contract semantics are `bracket_payoff`'s, asked one integer at a time

`distribution_over_ladder` computes each bracket's probability as
`Σ pmf[k] for k in support if settles_yes(spec, k)`. There is **no**
`between`/`greater`/`less` inequality anywhere in this module and no ticker
string is ever inspected. `yes_bounds` is consulted only to discover which end of
a contract is unbounded, so the truncated tail mass can be attributed.

Two tests enforce it:
* `test_engine_asks_bracket_payoff_which_integers_pay` monkeypatches `settles_yes`
  to return `False` and asserts every finite bracket collapses to 0 and
  `uncovered_mass → 1.0`. A second copy of the rule would leave the answers
  unchanged.
* `test_module_contains_no_second_copy_of_the_settlement_rule` walks the module's
  AST and fails on any comparison against `.strike_type` and on any
  `startswith`/`endswith` call.

### 3.3 Mass is conserved, never renormalized

The integer support is `μ ± 8σ`, clipped to a physical `[−60, 135] °F` range and
then **widened to span the ladder's finite core**. Mass outside it is computed
analytically and added to whichever contracts are open in that direction, so
`Σ pmf + pmf_tail_mass == 1` exactly. Whatever is still uncovered — a gap, a
missing tail — is reported and left uncovered.

### 3.4 The sign of the bias

`μ = forecast_high_f − bias_f`, per workstream B's `error_convention` string,
which the test re-reads from the artifact and asserts verbatim.
`test_a_warm_bias_moves_mu_down_not_up` works the case both ways: forecast 87 with
`bias_f = +2` centres on 85 and makes **B85.5** modal; with the sign inverted it
would centre on 89 and make **B89.5** modal — four degrees away, a different
trade, and every number on the way there still plausible.

### 3.5 Calibration selection, and what falls back

Day-of chain: `by_month_day_of` → `by_season_day_of` → `day_of`. Each step
branches on the block's own `sufficient` flag, logs at WARNING, and records the
descent in `CalibrationSelection.fallback_chain` and in `warnings`.

A **non-day-of lead bucket that is insufficient raises**. Substituting the day-of
block for a 5-day lead would swap in a ~3 °F σ for one that is materially larger
and size positions as if a 5-day forecast were a same-day one.

All 10 headline ladders resolved to `by_month_day_of:2026-07` with no fallback.

### 3.6 `mean_spread_f: null` is refused, not coerced

All four committed calibrations report `mean_spread_f: null` on every block —
the IEM MOS archive publishes no spread. `require_published_spread=True` raises
with a message naming the consequence ("treating a null spread as 0.0 would size
every position as though the forecast were certain"). With the default `False`,
a warning is attached to every result stating that σ is the calibrated error σ
only and is identical for every day in the bucket.
`test_null_published_spread_is_refused_loudly_when_required` fires against all
four real artifacts, not a contrived payload.

---

## 4. Ensemble mode — the choice, and what it discards

**Chosen:** `μ = mean(members) − bias_f`, `σ = the calibrated σ`. One approach,
no options.

**Why.** The raw ensemble is under-dispersed, so its member spread is not the
forecast error σ. More decisively: **no spread-skill relationship has been
measured for these stations.** Workstream B's backfill carries `spread_f` blank on
all 13,020 rows because the IEM MOS archive exposes NBM's `XND` live but not
historically, so every calibration block reports `mean_spread_f: null`. Using
member spread as the dispersion — or as a kernel bandwidth — would assert a
relationship nobody has measured on this data.

**What it discards, plainly:** all shape information in the member cloud — skew,
multimodality, and any day-to-day variation in forecast confidence. Two ensembles
with the same mean and wildly different spread produce **bit-identical** bracket
probabilities. That is pinned by
`test_ensemble_uses_the_calibrated_sigma_not_the_member_spread` (21 members each,
member σ **0.3102** vs **9.3073** °F, same mean; every bracket probability agrees
to 1e-12), so it cannot be mistaken for a bug, and
so a future change to use member spread has to delete an assertion rather than
slip through.

The raw member statistics are still measured and returned in
`BracketProbabilities.ensemble` (`n_members`, `mean_f`, `median_f`, `min_f`,
`max_f`, `sigma_f`), and a warning fires when the member spread exceeds the
calibrated σ — the day looks less certain than the bucket average.

**Prerequisite for revisiting:** a calibration rebuilt on a source that publishes
a spread, demonstrating that high-spread days really do have larger realized
error. Until that exists, a kernel-smoothed variant would be preserving noise.

A single-member or empty "ensemble" raises rather than silently becoming a point
forecast, so `mode` never lies about provenance.

---

## 5. σ doubled — and the metric that had to be corrected

| event | H bits | H′ bits | max finite | max′ finite | pmf peak | pmf peak′ |
|---|---|---|---|---|---|---|
| KXHIGHCHI-26JUL17 | 1.78341 | 2.43475 | 0.46780 | 0.24827 | 0.24720 | 0.12595 |
| KXHIGHCHI-26JUL20 | 1.78341 | 2.43475 | 0.46780 | 0.24827 | 0.24720 | 0.12595 |
| KXHIGHCHI-26JUL23 | 1.78341 | 2.43475 | 0.46780 | 0.24827 | 0.24720 | 0.12595 |
| KXHIGHLAX-26JUL18 | 2.04438 | 2.56124 | 0.38502 | 0.20269 | 0.20404 | 0.10289 |
| KXHIGHLAX-26JUL21 | 2.04438 | 2.56124 | 0.38502 | 0.20269 | 0.20404 | 0.10289 |
| KXHIGHMIA-26JUL18 | 1.70711 | 2.45068 | 0.48613 | 0.26335 | 0.26549 | 0.13481 |
| KXHIGHMIA-26JUL21 | 1.70711 | 2.45068 | 0.48613 | 0.26335 | 0.26549 | 0.13481 |
| KXHIGHNY-26JUL17  | 2.30393 | 2.49494 | 0.29727 | 0.15226 | 0.15226 | 0.07660 |
| KXHIGHNY-26JUL20  | 2.30393 | 2.49494 | 0.29727 | 0.15226 | 0.15226 | 0.07660 |
| KXHIGHNY-26JUL23  | 2.30393 | 2.49494 | 0.29727 | 0.15226 | 0.15226 | 0.07660 |

All three metrics move in the flattening direction on 10/10 ladders. The pmf
peak halves, as it must for a Gaussian whose σ doubled.

### The correction: "max bracket probability decreases" is FALSE here

The brief specified "max bracket probability strictly decreases AND Shannon
entropy strictly increases". Measured, the first half is **false** on a ladder
with open-ended tail contracts. On **KXHIGHNY-26JUL17** (μ = 85.167,
σ = 2.5988) the largest bracket probability **rises** from **0.29727 to 0.30395**
when σ is doubled: the unbounded `T83` contract (high ≤ 82) gains mass faster
than the central bracket loses it. Continuing the sweep on that ladder:

| σ multiplier | σ °F | entropy (bits) | max over ALL brackets | max over finite brackets | pmf peak |
|---|---|---|---|---|---|
| 0.5 | 1.299 | 1.55325 | 0.54363 | 0.54363 | 0.29727 |
| 1.0 | 2.599 | 2.30393 | 0.29727 | 0.29727 | 0.15226 |
| **2.0** | **5.198** | **2.49494** | **0.30395 ↑** | 0.15226 | 0.07660 |
| 4.0 | 10.395 | 2.16572 ↓ | 0.39877 | 0.07660 | 0.03836 |
| 8.0 | 20.790 | 1.76557 | 0.44897 | 0.03836 | 0.01919 |
| 64.0 | 166.32 | 1.15600 | 0.49360 | 0.00480 | 0.00240 |

Two facts fall out, and both are stated rather than smoothed over:

1. **Max-over-all-brackets is not monotone in σ**, because as σ → ∞ the two tail
   contracts converge on ~0.5 each. It is an invalid flattening metric on any
   ladder with open tails.
2. **Entropy over a fixed finite ladder is unimodal in σ, not monotone** — it
   peaks near 2σ here and decays toward 1 bit (the two tails) as σ grows. The
   entropy claim is therefore asserted **at the perturbation the criterion
   names** (σ → 2σ), which is where it holds on 10/10, and this caveat is the
   honest boundary of that claim.

`test_max_over_all_brackets_is_not_a_valid_flattening_metric` commits the NY
counterexample with its numbers, so a future failure cannot be "fixed" by
switching to that metric.

The monotonicity assertion in
`test_ec4_between_brackets_decay_monotonically_away_from_the_mode` is likewise
restricted to the equal-width `between` brackets, and the restriction is
deliberate for the same reason: an unbounded `less` or `greater` contract can
legitimately hold more mass than the finite bracket beside it. A claim over all
six would be false for the data, not for the engine.

---

## 6. The N/X-window regime — how it is handled

### 6.1 Independently re-measured, not inherited

Workstream B flagged this as its top modeling risk. Rather than copy its numbers,
they were recomputed here from the same read-only archives
(`data/forecast_archive/forecast_series_gfs_mex.csv` day-of rows joined to
`data/weather_truth/cli_daily_high_<STATION>.csv`, split on the CLI's own
`high_time`). The recompute **reproduces B's day-of headline exactly** (NY bias
0.0574 / σ 3.3736, matching `NY_gfs_mex_v1.json` to the published digit) and its
regime table exactly:

| city | station | n inside | bias in | σ in | n outside | bias out | σ out | P(outside) | n no time |
|---|---|---|---|---|---|---|---|---|---|
| NY | KNYC | 172 | +0.4767 | 2.8846 | 37 | −1.8919 | 4.6355 | **17.70 %** | 0 |
| CHI | KMDW | 171 | +0.7076 | 2.9279 | 38 | −2.4474 | 6.4292 | **18.18 %** | 0 |
| LAX | KLAX | 206 | −0.3641 | 2.1410 | 2 | *(unmeasured)* | *(unmeasured)* | 0.96 % | 1 |
| MIA | KMIA | 196 | −0.2092 | 1.6524 | 2 | *(unmeasured)* | *(unmeasured)* | 1.01 % | 10 |

LAX and MIA carry **n = 2** outside the window — far below the calibrator's
20-day quorum — so their outside-window bias and σ are **not published and not
usable**. The frequency is measured; the error distribution is not. Days whose
`high_time` could not be parsed are counted (`n_unknown_time`) and excluded, never
defaulted into either regime.

This table is a module constant (`NX_WINDOW_REGIME`) **and** a function
(`recompute_nx_window_regime`), and
`test_regime_constants_match_a_fresh_recompute_from_the_archives` asserts they
still agree. The constants cannot drift away from the data.

### 6.2 Both a model and a first-class caveat

* **`regime="single"` (default).** Single calibrated Gaussian. A populated
  `RegimeRisk` object **and** a warning string are attached to *every* result,
  including LAX/MIA where the warning says the regime is measured-but-
  unquantified. `RegimeRisk.material` is `True` at P(outside) ≥ 5 %, i.e. for NY
  and CHI — a flag FR-3.1(a) can act on to refuse or discount far-bracket sells.
* **`regime="mixture"`.** Explicit two-component mixture,
  `(1−p) · N(f − bias_in, σ_in) + p · N(f − bias_out, σ_out)`. **Raises** for a
  city whose outside component is below quorum.
* **`regime="mixture_if_available"`.** Degrades to single for those cities, with
  the degradation recorded in `warnings` and stamped into `regime_model`.

There is no silent path between the three.

### 6.3 What the regime is worth, in cents

KXHIGHNY-26JUL17, forecast 86.5 °F, `by_month_day_of:2026-07`:

| bracket | rule | single | mixture | ratio |
|---|---|---|---|---|
| T83 | ≤ 82 | 0.15242 | 0.10935 | 0.72× |
| B83.5 | 83–84 | 0.24635 | 0.17199 | 0.70× |
| B85.5 | 85–86 | 0.29727 | 0.24462 | 0.82× |
| B87.5 | 87–88 | 0.20415 | 0.22645 | 1.11× |
| B89.5 | 89–90 | 0.07974 | 0.14045 | 1.76× |
| **T90** | **≥ 91** | **0.02007** | **0.10713** | **5.34×** |
| | | σ_eff 2.5988, H 2.3039 | σ_eff 3.3866, H 2.5111 | |

Both sum to 1.000000000000000.

**This is the finding, and it is a trading finding, not a modeling nicety.** The
far bracket FR-3.1(a) is designed to sell is worth **2.0 ¢ under the averaged
model and 10.7 ¢ once the overnight-maximum regime is modeled** — a 5.3× error,
entirely inside the tail, on the single trade shape the strategy exists to place.
Workstream B's worst observed day (KMDW 2026-03-22, CLI high 71 °F set at
1:57 AM against a 46 °F daytime-max forecast) is what that mass represents.

**Recommendation to FR-2.4 go/no-go and to workstream E:** compute NY and CHI
far-bracket EV under `regime="mixture"`, not under the default single Gaussian.
An EV computed on the single-Gaussian tails will look profitable for a reason
that is an artifact of averaging two populations. LAX and MIA cannot be modeled
this way at all — their second component is unmeasured — so their far-bracket EV
carries an unquantified tail risk that should be stated in the go/no-go rather
than assumed away.

---

## 7. Test inventory (44 tests, all passing)

| Area | Tests |
|---|---|
| Hand-worked example (published normal table) | `test_hand_worked_ladder_matches_a_published_normal_table` |
| Bias sign | `test_a_warm_bias_moves_mu_down_not_up`, `test_bias_sign_matches_the_calibration_artifacts_stated_convention` |
| Continuity correction & mass conservation | `test_pmf_over_integers_sums_to_one_minus_the_truncated_tail`, `test_pmf_is_an_integer_interval_mass_not_a_density`, `test_truncated_mass_is_absorbed_by_the_open_ended_contracts`, `test_truncating_the_support_cannot_misattribute_mass_to_a_tail_contract`, `test_far_tail_probability_survives_cancellation` |
| **EC-4** | `test_ec4_the_ten_recorded_ladders_are_complete_partitions`, `test_ec4_bracket_probabilities_sum_to_one` (×4 σ), `test_ec4_sum_to_one_end_to_end_through_the_public_api`, `test_ec4_cumulative_bracket_sums_match_the_calibrated_cdf`, `test_ec4_between_brackets_decay_monotonically_away_from_the_mode`, `test_ec4_doubling_sigma_measurably_flattens_the_distribution`, `test_max_over_all_brackets_is_not_a_valid_flattening_metric` |
| Semantics single-sourcing | `test_engine_asks_bracket_payoff_which_integers_pay`, `test_module_contains_no_second_copy_of_the_settlement_rule` |
| Partition validation | gap, overlap, missing low tail, missing high tail, empty, uncovered-mass-not-renormalized, negative-uncovered-on-overlap |
| Calibration selection | month preferred, month→season→day_of fallback recorded, `sufficient`-flag-alone rejection, insufficient lead bucket raises (×2), all-insufficient raises, null spread refused, source mismatch warned |
| N/X regime | constants match fresh recompute, risk attached & material for NY/CHI, mixture widens tails & sums to 1, mixture refused below quorum |
| Ensemble | calibrated σ not member spread, agrees with point mode at the same mean, degenerate ensembles refused |
| Reporting surface | `as_dict` carries mode/bucket/caveats and is JSON-serializable |

The hand-worked test's expected values come from a published 5-decimal
standard-normal table reproduced in its docstring (Φ(−1.25) = 0.10565,
Φ(−0.25) = 0.40129, Φ(0.75) = 0.77337, Φ(1.75) = 0.95994, Φ(2.75) = 0.99702),
differenced by hand, and asserted to 4 decimals. The CDF-consistency test
recomputes Φ from `math.erfc` in the test, not from the engine.

---

## 8. Mutation testing of the gates (a green gate proves nothing until it can fail)

Ten defects were seeded into the module one at a time and the suite re-run:

| seeded defect | caught | by |
|---|---|---|
| bias sign inverted (`μ = f + bias`) | ✅ | hand-worked example |
| continuity correction removed | ✅ | hand-worked example |
| truncated tail mass dropped | ✅ | support-truncation test |
| probabilities silently renormalized | ✅ | 4 tests, incl. uncovered-mass and overlap |
| support-widening (`must_cover`) removed | ✅ | support-truncation test |
| semantics re-implemented locally (bounds instead of `settles_yes`) | ✅ | hand-worked example |
| regime constant perturbed (CHI σ_out 6.4292 → 5.9) | ✅ | fresh-recompute test |
| partition validator blinded to gaps | ✅ | gap test |
| **`sufficient` flag branch deleted** | ❌ **MISSED** | — |
| null spread coerced to 0.0 | ✅ | null-spread test |

The miss was real and is now closed. Deleting the `sufficient` check broke
nothing because the FR-2.2 writer omits `bias_f`/`sigma_f` from insufficient
blocks, so a *second* guard caught it — defence in depth, not a test. A
hand-edited, stale, or schema-migrated payload can carry both the flag and the
numbers. `test_the_sufficient_flag_alone_is_enough_to_reject_a_bucket` and
`test_insufficient_lead_bucket_with_stats_still_raises` now supply blocks that
carry `sufficient: false` **alongside** plausible-looking `bias_f: 99.0` /
`sigma_f: 0.01`, and assert those numbers appear nowhere in the answer. Both
mutations are caught after the fix.

### A defect this found in my own code

The support-truncation mutation exposed a genuine bug that was present in the
first implementation: when the integer support did not span the ladder's finite
core, the analytic "mass above the support" was credited to the `greater`
contract even though most of it belonged to the top `between` brackets. At the
default 8 σ the residual is ~1e-15 and the bug is invisible; at 0.5 σ it is worth
~0.3 of the distribution and lands entirely on the far bracket FR-3.1(a) sells.
Fixed by widening the support to span the ladder (`integer_pmf(must_cover=…)`)
and pinned by `test_truncating_the_support_cannot_misattribute_mass_to_a_tail_contract`,
which squeezes the support to 0.5 σ and requires bit-for-bit agreement with the
8 σ answer.

---

## 9. Reproduction

```powershell
$env:PYTHONPATH = "."
python -m pytest tests/test_probability_engine.py -q     # 44 passed
```

Re-fetching the 10 ladders (read-only, regenerates the fixture):

```
GET https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=<EVENT>&limit=200
```
for `KXHIGHNY-26JUL{17,20,23}`, `KXHIGHCHI-26JUL{17,20,23}`,
`KXHIGHLAX-26JUL{18,21}`, `KXHIGHMIA-26JUL{18,21}`. The event-ticker date format
is `%y%b%d` uppercased.

Regenerating the regime table from the read-only archives:

```python
from src.calibration.probability_engine import recompute_nx_window_regime
recompute_nx_window_regime()   # == NX_WINDOW_REGIME
```

---

## 10. Gaps, caveats, and proposed changes to files I do not own

### Gaps and caveats

1. **The distribution is Gaussian by assumption.** No normality test was run on
   the 209 day-of residuals. B's published quantiles (NY p05 −5, p25 −2, p50 0,
   p75 +2, p95 +5) are roughly symmetric and roughly consistent with σ = 3.37,
   but "roughly" is the honest word. A far-bracket strategy is a bet on the
   tails, and the tails are exactly where a Gaussian assumption is least
   defensible. **Recommended before FR-3.1(a) trades: a QQ / Anderson–Darling
   check on the residuals, per city and per regime.** This is not blocking EC-4,
   which is about the engine's arithmetic, but it is blocking a confident
   far-bracket EV.
2. **σ is a bucket constant, not a per-day quantity.** Every day in a month
   bucket gets the same σ. With no published spread there is nothing to
   condition on. A quiet high-pressure day and a frontal-passage day are priced
   identically. Workstream A's ensemble does not fix this under the current
   design (§4) — fixing it requires a *measured* spread-skill relationship.
3. **The calibration is raw GFS extended MOS.** Applying its `bias_f`/`sigma_f`
   to workstream A's GEFS ensemble mean is a source mismatch. The engine warns
   (`forecast_source` != the artifact's `source`) but cannot correct it. **A
   calibration must be rebuilt on the ensemble's own output before FR-3.1
   sizes real positions from ensemble input.**
4. **`by_month_day_of` buckets are thin.** All 10 headline ladders resolved to
   `by_month_day_of:2026-07`, which for NY has n = 24 — just over the 20-day
   quorum. The month σ (NY 2.5988) is materially tighter than the annual day-of
   σ (3.3736); a thin bucket is exactly where an optimistic σ comes from. The
   fallback chain is honest about which bucket was used, and `n` is on every
   result, but a consumer that ignores `n` will size on 24 days of evidence.
5. **SON is entirely absent** and DJF is thin (B §9). Nothing here is validated
   for autumn.
6. **LAX/MIA far-bracket tails carry unquantified regime risk** (§6.1). Their
   second component has n = 2. The single Gaussian is all there is, and it is
   known to be incomplete.
7. **Entropy is not a monotone function of σ over a fixed ladder** (§5). Any
   future report using entropy as a "sharpness" metric must state the σ range.
8. **`recompute_nx_window_regime` reads two files I do not own.** If
   `data/forecast_archive/` or `data/weather_truth/` move or change schema, that
   function and its test break. The test skips (does not fail) when the archive
   is absent, so the constants would then go unchecked — a deliberate trade to
   keep the suite runnable in a clean checkout, but a real hole worth naming.
9. **The `mixture` path mixes two differently-bucketed samples**: the mixture's
   components come from the annual day-of regime split, while the single path
   uses the month bucket. The mixture is therefore *not* a refinement of the
   single-path σ; it is a different estimator. A warning says so on every mixture
   result. Reconciling them properly needs the regime split computed *per month
   bucket*, which n does not currently support.

### Proposed diffs (I did not edit these files)

**None.** `.gitattributes` (added by another workstream during this sprint)
already carries `tests/fixtures/** text eol=lf`, which covers
`tests/fixtures/ladders_probability/recorded_ladders.json`. The fixture is
compared as parsed JSON rather than hashed, so even that is precautionary.

In particular `src/core/bracket_payoff.py`
needed nothing: `settles_yes` and `yes_bounds` were exactly the entry points this
engine required, and `p_yes_from_cdf` is a usable independent cross-check.

### Ownership

Created/edited only: `src/calibration/probability_engine.py`,
`tests/test_probability_engine.py`,
`tests/fixtures/ladders_probability/recorded_ladders.json`,
`reports/phase2/ws_d_report.md`, and a 4-line appended comment at the end of
`src/calibration/__init__.py` (no import added, so B's "nothing is re-exported
here" property is preserved). `data/calibration/**`,
`data/forecast_archive/**`, `data/weather_truth/**`, and `data/ladders/**` were
opened read-only. No git write command was run.
