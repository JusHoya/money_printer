# Workstream F — GEFS forecast-series backfill and calibration

Phase 2, PRD FR-2.1 + FR-2.2. Branch `phase-2-forecast-calibration`.
All numbers measured 2026-07-26/27 on this machine. Nothing below is an estimate,
a projection, or carried over from another workstream without attribution.

---

## 0. Verdict up front

The gap this workstream existed to close is closed, and the answer it produced is
**not the flattering one**.

| Question | Answer |
|---|---|
| Can a GEFS forecast series be backfilled ≥60 days? | **Yes — 209 paired day-of days per city** (208 at MIA), 2025-12-28 … 2026-07-24, 0 missing cycles, 0 failed records. |
| Does the same calibrator ingest it unmodified? | **Yes.** `forecast_calibration.build_all()` was called unchanged; four `*_gefs_v1.json` files written. |
| Byte-identical on re-run? | **Yes, 4/4**, on three independent checks. |
| Does GEFS beat the `gfs_mex` fallback? | **No. GEFS day-of σ is worse at 3 of the 4 cities**, and **NY fails the 4 °F EC-3 bound at σ = 4.11** where `gfs_mex` passes at 3.37. GEFS wins only at CHI (3.76 vs 3.98). |
| Recommendation for Phase 3 | **Do not make GEFS the sole live source.** Use `gfs_mex` at LAX and MIA; use the **measured blend** at NY and CHI. Details in §9. |

The PRD's designated primary source is, on this sample, the inferior one. That is
the finding, and it is reported here rather than tuned away. §7 enumerates every
variant that was tried and the rule by which each was decided — including the two
variants that would have made GEFS look better and were rejected.

---

## 1. The gap, restated

FR-2.1 makes the GEFS ensemble the **primary live forecast source**. FR-2.2
requires calibration on ≥60 paired forecast-vs-CLI days. Workstream B delivered a
209-day calibration, but for **GFS extended MOS** (`gfs_mex`) — it could not
backfill an ensemble. Left there, the runtime would have priced a GEFS forecast
using a MOS-derived σ: the source that drives live probabilities would have had no
calibration of its own. That is not a documentation nit; the two sources' day-of σ
differ by up to 1.63 °F (§6), which is a first-order input to P(bracket).

---

## 2. Verified upstream contract

Every row probed live, 2026-07-26. Anonymous plain HTTPS against the NOAA Open
Data Dissemination mirror; no credentials, no `boto3`, no new dependencies.

| Request | Status | Result |
|---|---|---|
| `gefs.20260720/00/atmos/pgrb2sp25/gespr…f030.idx` | 200 | `13:3821080:…:TMAX:2 m above ground:24-30 hour max fcst:ens std dev` — **`gespr` publishes TMAX** |
| `…geavg…f030.idx` | 200 | `…:TMAX:…:ens mean` |
| `geavg`/`gespr` f030 `.idx` at `20250801`, `20251227`, `20251228`, `20260101`, `20260215`, `20260401`, `20260601` | 200 (14/14) | archive spans the whole CLI truth window and ~1 y beyond |
| `gefs.20260720/00/atmos/pgrb2ap5/geavg.t00z.pgrb2a.0p50.f030.idx` | 200 | 0.5° product carries the same field; TMAX record **130 817 B vs 407 628 B** at 0.25° (**3.1× smaller**) |

GRIB metadata read from the records themselves, not from the `.idx` labels:

| Product | PDT | `derived_forecast_type` | `ensemble_size` | `statistical_process` |
|---|---|---|---|---|
| `geavg` | 4.12 | 0 (unweighted mean of all members) | **30** | 2 (maximum) |
| `gespr` | 4.12 | 2 (standard deviation w.r.t. cluster mean) | **30** | 2 (maximum) |

**NCEP declares `ensemble_size = 30`, while the live provider averages 31 members**
(`gec00` + `gep01…30`). That is a second, independent difference between the
backfill statistic and the live one, and it is *not* separately identified from the
Jensen term — the measured offset in §4 absorbs both together. Stated here so
nobody later reads that offset as pure Jensen.

---

## 3. The design decision: derived products, not 31 members

Fetching all 31 members for 210 cycles × 21 forecast hours is ~72 000 ranged
requests and ~30 GB. Instead the backfill reads the two pre-computed NCEP products
in the same directory — `geavg` (mean field) and `gespr` (spread field) — which is
**42 ranged requests per cycle** and additionally supplies `spread_f`, a column
workstream B could not populate at all.

**Measured cost of the run actually performed:** 210 cycles, 8 820 ranged reads
(plus 8 820 `.idx` reads ⇒ ~17 640 HTTP requests), **4.06 GB**, **1 254 s
(20.9 min)** at ≤4-wide concurrency. 0 HTTP failures, 0 retries exhausted, 0
missing cycles.

The saving is not free, and the price is a different statistic. That is §4.

---

## 4. The statistic mismatch — measured, bounded, applied, recorded

The live path computes `live = mean_m( max_t TMAX(m,t) )`.
A `geavg` backfill can only compute `backfill = max_t( mean_m TMAX(m,t) )`.
`max` and `mean` do not commute; Jensen puts `backfill ≤ live`.

**Measured on workstream A's 20 city-cycles at the full 31 members** (5 consecutive
00Z cycles 2026-07-20…24 × 4 cities, `reports/phase2/ec1_ensemble_members.json`,
field `runs[].geavg_check.member_mean_minus_geavg_f`). Re-runnable:
`python scripts/backfill_ensemble_history.py --measure-offset`.

| City | n | mean °F | sd °F | min °F | max °F |
|---|---|---|---|---|---|
| NY | 5 | **+0.2514** | 0.0590 | +0.1870 | +0.3155 |
| CHI | 5 | **+0.0489** | 0.0605 | −0.0118 | +0.1305 |
| LAX | 5 | **+0.0269** | 0.0450 | −0.0300 | +0.0914 |
| MIA | 5 | **+0.0549** | 0.0883 | −0.0483 | +0.1427 |
| pooled | 20 | +0.0955 | 0.1105 | −0.0483 | +0.3155 |

**Small and stable at every city**, so the derived-product route proceeds and the
offset is applied as an explicit per-city correction
(`GEAVG_TO_MEMBER_MEAN_OFFSET_F` in `src/calibration/gefs_series.py`), carried in
every CSV row's `provenance` cell, in the manifest, in each calibration file's new
`statistic` block, and in each `.runlog.json`.

**The correction cannot change the EC-3 verdict, and this is measured, not
argued.** A constant per-city shift moves `bias_f` by exactly its own value and
leaves `sigma_f` untouched:

| City | offset | bias applied | bias raw | σ applied | σ raw |
|---|---|---|---|---|---|
| NY | +0.2514 | 0.6767 | 0.4253 | 4.106873 | **4.106873** |
| CHI | +0.0489 | −1.1041 | −1.1530 | 3.762338 | **3.762338** |
| LAX | +0.0269 | −0.3285 | −0.3554 | 3.772247 | **3.772247** |
| MIA | +0.0549 | −2.5034 | −2.5583 | 2.417381 | **2.417381** |

σ is identical to six decimal places. `tests/test_gefs_backfill.py::TestOffsetCannotMoveSigma`
pins the property, and `TestOffsetIsMeasuredNotTyped` recomputes the four constants
from the committed artifact so they cannot drift from the measurement they cite.
A ceiling (`MAX_TRUSTED_OFFSET_F = 1.0 °F`) makes a future re-measurement that
outgrows "rounding term" fail loudly instead of being applied silently; a mutation
test proves that guard fires.

### Independent confirmation the backfill path equals workstream A's path

The 209-day series was re-derived and compared to workstream A's independently
produced 31-member run on the 4 overlapping days (2026-07-25 is outside the truth
window):

| City | WS-A n=5 bias, 31 members | this series, same days | annual day-ahead bias |
|---|---|---|---|
| NY | +2.921 | **+3.216** | +0.571 |
| CHI | +1.615 | **+1.134** | −1.163 |
| LAX | +5.785 | **+5.452** | −0.125 |
| MIA | −3.159 | **−3.385** | −2.742 |

The raw `max_t(geavg)` values also reproduce workstream A's `geavg_check.daily_high_f`
**exactly** (83.03 / 84.47 / 81.41 / 88.79 °F for 2026-07-21) — same node, same
windowing, same interval algebra. Pinned as a test.

**And the third column is the point of this workstream.** LAX's day-ahead bias on
workstream A's 5 July days is **+5.45 °F**; on 208 days it is **−0.13 °F**. n=5 was
not merely imprecise — it was unrepresentative by nearly 6 °F, and it pointed at
the wrong city (§6).

---

## 5. Coverage

Backfill: 00Z cycles **2025-12-27 … 2026-07-24** (210 cycles), lead-days 0,1,2,3.

```
python scripts/backfill_ensemble_history.py --start 2025-12-27 --end 2026-07-24 \
       --lead-days 0,1,2,3 --sleep 0.2
python scripts/backfill_ensemble_history.py --build-calibration --version 1
```

| | |
|---|---|
| Cycles requested / present / **missing** | 210 / 210 / **0** |
| Records requested / failed | 8 820 / **0** |
| Cycles incomplete | **0** |
| Bytes downloaded | 4 061 091 506 (4.06 GB) |
| Wall time | 1 254 s |
| Rows written | **3 360** (`data/forecast_archive/forecast_series_gefs.csv`, 908 KB, LF) |
| Manifest | `data/forecast_archive/gefs_manifest.json`, 1.81 MB — every S3 key, byte range, byte count, failure and missing day |

| City | Station | Forecast rows | Paired (all leads) | Distinct dates | **Day-of paired days** | Dropped: no truth | Dropped: null truth | `spread_f` coverage |
|---|---|---|---|---|---|---|---|---|
| NY | KNYC | 840 | 833 | 209 | **209** | 7 | 0 | **209/209**, mean 1.358 °F |
| CHI | KMDW | 840 | 833 | 209 | **209** | 7 | 0 | **209/209**, mean 1.362 °F |
| LAX | KLAX | 840 | 833 | 209 | **209** | 7 | 0 | **209/209**, mean 0.710 °F |
| MIA | KMIA | 840 | 829 | 208 | **208** | 11 | 0 | **208/208**, mean 0.893 °F |

Target window 2025-12-28 … 2026-07-24 — the full extent of the CLI truth archive,
so **the sample is truth-limited, not backfill-limited**. Every drop is accounted
for: the 7 unpaired dates are 2025-12-27 (a cycle preceding the truth window) and
2026-07-25…27 (targets not yet settled); MIA carries 4 more because KMIA has no CLI
row for 2026-04-11 (already documented in `data/weather_truth/coverage_report.json`).
No day was dropped for being an outlier.

Requirement was ≥60 (target ≥90). **Delivered 208–209 per city**, 3.5× the floor.

**DST is now exercised on real data, not only in unit tests** (workstream A gap #5).
The window spans the 2026-03-08 spring-forward transition; the local day is
correctly 23 h and CHI's covering set correctly narrows from 5 records to 4.

---

## 6. The numbers — GEFS vs `gfs_mex`

### 6.1 EC-3 day-of σ, side by side

Both sources use the same `day_of` bucket (lead `[-24, 12)` h) and both observe
leads of 4–8 h from a 00Z cycle, so this is a like-for-like comparison.

| City | **GEFS** n | bias °F | **σ °F** | MAE °F | | `gfs_mex` n | bias °F | **σ °F** | MAE °F | | Δσ (GEFS − MEX) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| MIA | 208 | **−2.503** | **2.417** | 3.105 | | 208 | −0.168 | **1.707** | 1.293 | | **+0.710** worse |
| CHI | 209 | **−1.104** | **3.762** | 2.926 | | 209 | +0.134 | **3.981** | 2.651 | | **−0.219** better |
| LAX | 209 | **−0.328** | **3.772** | 3.024 | | 209 | −0.383 | **2.141** | 1.636 | | **+1.631** worse |
| NY | 209 | **+0.677** | **4.107** | 3.316 | | 209 | +0.057 | **3.374** | 2.383 | | **+0.733** worse |

**EC-3 for `source=gefs`: 3 of 4 pass** (LAX 3.77, CHI 3.76, MIA 2.42 ≤ 4 °F).
**NY fails at σ = 4.107 °F.** Per the EC-3 rule — *"a city failing this is
excluded, not fudged"* — **NY is excluded for the GEFS source**. It is not
excluded for `gfs_mex`, which passes there at 3.374.

Nothing was trimmed, winsorized, re-bucketed or re-sourced to reach that. The
`day_of` bucket is workstream B's, untouched.

Two consequences worth naming plainly:

* **σ is what matters and GEFS loses it.** Bias is removable by calibration — a
  −2.50 °F MIA bias is a subtraction. σ is irreducible spread and sets the width
  of every bracket probability. GEFS is wider at 3 of 4 cities.
* **The one city GEFS wins is the one with the most overnight maxima** (§6.3),
  which is a mechanism, not a coincidence.

### 6.2 σ by lead time (GEFS, EC-2 requirement)

00Z cycle only, so every bucket is n≈209 (workstream B's non-day-of buckets pool
00Z and 12Z and carry n≈418).

| Bucket | leads | NY | CHI | LAX | MIA |
|---|---|---|---|---|---|
| `day_of` | 4–8 h | n=209 b=+0.68 **σ=4.11** | n=209 b=−1.10 **σ=3.76** | n=209 b=−0.33 **σ=3.77** | n=208 b=−2.50 **σ=2.42** |
| `lead_12_36` | 28–32 h | n=209 b=+0.57 σ=4.46 | n=209 b=−1.16 σ=4.67 | n=209 b=−0.13 σ=4.24 | n=208 b=−2.74 σ=2.56 |
| `lead_36_60` | 52–56 h | n=208 b=+0.40 σ=4.64 | n=208 b=−1.16 σ=5.13 | n=208 b=−0.24 σ=4.50 | n=207 b=−2.84 σ=2.72 |
| `lead_60_84` | 76–80 h | n=207 b=+0.37 σ=5.31 | n=207 b=−1.51 σ=5.99 | n=207 b=−0.21 σ=4.66 | n=206 b=−2.93 σ=2.67 |

σ grows monotonically with lead at every city — the expected shape, and a weak
sanity check that the lead labelling is not scrambled. Bias is close to
lead-invariant, i.e. it is a **siting/statistic** term, not a forecast-decay term.

### 6.3 The overnight-maximum regime (workstream B's flagged settlement risk)

Workstream B found ~18 % of NY/CHI days set their CLI maximum outside 07–19 LST,
where the MOS daytime `N/X` window cannot see it, and carry roughly double σ. GEFS
`TMAX` tiles the **whole local day**, so I predicted before measuring that GEFS
would beat MOS in exactly that regime. **The prediction held at CHI and failed at
NY**, and both halves are reported:

| City | source | max **inside** 07–19 LST: n / bias / σ | max **outside** 07–19 LST: n / bias / σ |
|---|---|---|---|
| NY | gefs | 172 / +0.51 / 3.90 | 37 / +1.46 / **4.93** |
| NY | gfs_mex | 172 / +0.48 / 2.88 | 37 / −1.89 / **4.64** |
| CHI | gefs | 171 / −1.73 / 3.36 | 38 / +1.70 / **4.23** |
| CHI | gfs_mex | 171 / +0.71 / 2.93 | 38 / −2.45 / **6.43** |
| LAX | gefs | 206 / −0.32 / 3.79 | 3 / −0.59 / 2.91 |
| LAX | gfs_mex | 206 / −0.36 / 2.14 | 3 / −1.67 / 2.08 |
| MIA | gefs | 196 / −2.59 / 2.18 | 12 / −1.02 / 4.81 |
| MIA | gfs_mex | 196 / −0.21 / 1.65 | 12 / +0.50 / 2.43 |

At CHI, GEFS cuts the overnight-regime σ from 6.43 to 4.23 and removes the −2.45 °F
cold bias — and CHI is the only city where GEFS wins overall. That is the
mechanism. At NY the same regime is 4.93 vs 4.64: GEFS is marginally *worse*, so
full-day coverage is not a universal win. `gfs_mex` remains better in the ordinary
daytime regime at all four cities.

### 6.4 Seasonal day-of σ

| City | DJF (n=63) | MAM (n=92) | JJA (n=54) |
|---|---|---|---|
| NY | gefs b=−1.88 σ=3.86 · mex b=+0.21 σ=3.17 | gefs b=+0.45 σ=3.78 · mex b=−0.98 σ=3.64 | gefs b=**+4.04** σ=2.21 · mex b=+1.65 σ=2.38 |
| CHI | gefs b=−2.13 **σ=2.80** · mex b=−0.54 **σ=4.17** | gefs b=−0.85 σ=4.71 · mex b=−0.55 σ=4.25 | gefs b=−0.34 σ=2.52 · mex b=+2.09 σ=2.35 |
| LAX | gefs b=−2.36 σ=3.20 · mex b=−0.56 σ=2.38 | gefs b=−0.03 σ=3.74 · mex b=−0.20 σ=2.29 | gefs b=+1.53 σ=3.37 · mex b=−0.50 σ=1.50 |
| MIA | gefs b=−1.32 σ=3.24 · mex b=+0.40 σ=1.94 | gefs b=−2.89 σ=2.04 · mex b=−0.16 σ=1.61 | gefs b=−3.24 **σ=0.99** · mex b=−0.83 σ=1.33 |

GEFS bias **swings by season at every city** (NY: −1.88 DJF → +4.04 JJA; MIA:
−1.32 → −3.24). A single annual bias term is wrong for GEFS in a way it is not for
`gfs_mex`; FR-2.3 must use `by_season_day_of`, which the artifacts carry. **SON is
entirely absent** and DJF holds only 63 days — inherited from the truth window, and
the same caveat workstream B recorded.

### 6.5 `gespr` has no per-day skill, and the ensemble is badly under-dispersed

This is the most consequential negative result here.

| City | n | mean `gespr` °F | mean \|error\| °F | realised σ °F | corr(`spread_f`, \|error\|) |
|---|---|---|---|---|---|
| NY | 209 | 1.358 | 3.316 | 4.107 | **−0.013** |
| CHI | 209 | 1.362 | 2.926 | 3.762 | **+0.065** |
| LAX | 209 | 0.710 | 3.024 | 3.772 | **+0.022** |
| MIA | 208 | 0.893 | 3.105 | 2.417 | **−0.197** |

* The published spread is **0.19–0.37× the realised error σ**. Feeding it into
  FR-2.3 as a predictive σ would price tails 3–5× too confidently.
* Its correlation with the day's actual error magnitude is **indistinguishable
  from zero** at three cities and *negative* at MIA. It does not even rank days.

Workstream A measured under-dispersion on 5 days and warned about it. On 209 days
it is confirmed and quantified. **FR-2.3 must use the per-bucket calibrated
`sigma_f`, never the per-day `spread_f`.** The column is still shipped — populated
on 100 % of rows, which is more than `gfs_mex` could offer — but it is shipped with
this measurement attached, and the caveat is written into every calibration file's
`statistic.spread_caveat`.

---

## 7. Every variant tried, and the rule that decided it

Reported in full, winners and losers, because keeping the best of many silent
attempts is how a calibration becomes fiction.

### 7.1 Resolution: 0.25° vs 0.5° — **0.25° kept**

Rule fixed before measuring: *adopt 0.5° only if the daily highs agree closely on
overlapping days.* Re-runnable: `--compare-resolution`.

Nearest-node distance to the settlement station:

| City | 0.25° node | km | 0.5° node | km |
|---|---|---|---|---|
| NY | 40.75, −74.00 | **4.6** | 41.00, −74.00 | 24.3 |
| CHI | 41.75, −87.75 | **3.8** | 42.00, −88.00 | 31.4 |
| LAX | 34.00, −118.50 | 12.3 | 34.00, −118.50 | 12.3 (same node) |
| MIA | 25.75, −80.25 | **8.0** | 26.00, −80.50 | 29.7 |

Daily-high delta (0.50° − 0.25°) on 5 overlapping cycles, same decoder, same
windowing, only the grid changed:

| City | n | mean °F | sd °F | min | max |
|---|---|---|---|---|---|
| NY | 5 | +0.432 | 1.748 | −1.26 | +2.88 |
| CHI | 5 | +0.432 | 1.171 | −1.08 | +1.62 |
| LAX | 5 | 0.000 | 0.000 | 0.00 | 0.00 |
| MIA | 5 | **+4.752** | 0.373 | +4.32 | +5.22 |

They do not agree. **Decision: keep 0.25°.** The 3.1× transfer saving is real and
declined.

**The uncomfortable part, stated because it is the part a tuner would hide.** On
those same 5 days the 0.5° MIA values (93.8, 93.5, 94.6, 93.5, 94.4) are *closer to
CLI truth* (92, 92, 93, 93, 92) than the 0.25° values (88.8, 89.0, 89.3, 89.2,
89.7) — the coarser node sits inland and warm, which happens to match KMIA better,
and MIA's annual GEFS bias is −2.50 °F cold. Switching MIA to 0.5° would have
improved its headline number. It was **not** done, for two reasons: (a) the live
`EnsembleProvider` reads 0.25°, so a 0.5° calibration would recreate exactly the
source-incoherence this workstream exists to remove; (b) picking a grid per city
because it scored better on 5 days is fitting the fixture. The honest way to test
it is to move the *live* provider's node and re-measure both together — logged as a
Phase-3 experiment in §10, not adopted here.

### 7.2 Statistic: `geavg` max vs member-mean max — **`geavg` + measured offset**

Full 31-member backfill was not attempted: ~72 000 requests / ~30 GB, against a
measured offset of ≤0.32 °F that provably cannot move σ (§4). Disclosed as a cost
decision, not a quality one.

### 7.3 Spread rule — `gespr` at the argmax interval, **fixed before measuring**

Three candidate rules were defined before any comparison. Measured against the
*true* sample sd of the 31 members' daily maxima on all 20 EC-1 city-cycles
(day-ahead lead; re-runnable as `--validate-spread`):

| Rule | NY | CHI | LAX | MIA | pooled | mean \|rule − member σ\| |
|---|---|---|---|---|---|---|
| true member σ (31 members) | 2.291 | 2.150 | 1.656 | 0.846 | 1.736 | — |
| **`gespr` at argmax (chosen a priori)** | 2.628 | 2.196 | 1.656 | 0.828 | 1.827 | **0.120** |
| max over intervals | 2.628 | 2.412 | 1.908 | 0.900 | 1.962 | 0.233 |
| mean over intervals | 1.930 | 1.786 | 1.555 | 0.691 | 1.490 | 0.271 |

(An earlier version of this table was computed while the record cache was still
being filled and covered only a subset of the 20 city-cycles. The numbers above
are the complete 20/20 sample; the ordering of the three rules is unchanged.)

The pre-chosen rule also happens to be the closest. That ordering is reported
because it was measured, not because it was used to select — the rule was fixed
first, and `spread_f` does not enter the bias/σ calibration at all (§6.5 shows why
it should not enter FR-2.3 either).

### 7.4 Grid node — **no variant tried, deliberately**

Nearest node only, no re-selection, no bilinear substitution. Workstream A already
measured that bilinear interpolation to the true station coordinates moves the
result by ≤1.1 °F and does not rescue LAX. Hunting for a node that flatters a city
is the textbook version of `audit-fixtures-for-degeneracy` in reverse.

### 7.5 Lead definition and day window — **no variant tried, deliberately**

`local_day_bounds_utc()` and `tmax_windows()` are imported from
`src/data/ensemble_provider.py` and called, not reimplemented. There is exactly one
windowing implementation in the repo, so the backfill cannot drift from the live
path — and there was no opportunity to "adjust" it.

---

## 8. Determinism (EC-2: "byte-identical on re-run")

Four independent checks, all green.

| City | file bytes | file sha256 (16) | `content_hash` (16) | CRLF |
|---|---|---|---|---|
| NY | 11 900 | `cbd56707f614cf7c` | `446745edef198346` | none |
| CHI | 11 932 | `242c20a687321aea` | `0374eb1bf8d49fd0` | none |
| LAX | 11 926 | `9268ac1bd8bb2c9a` | `dee83a276c150a6a` | none |
| MIA | 11 949 | `9fb28284c359b44e` | `97a27ff4e52febfa` | none |

1. **In-process** — `--build-calibration` builds twice from disk and byte-compares
   the canonical serialization: **4/4 identical**.
2. **Two directories, file bytes** —
   `test_rebuild_from_disk_into_two_directories_is_byte_identical` builds from disk
   into two separate directories and compares the written files (not a
   re-serialized in-memory object): **4/4 identical**.
3. **Cross-process** — a separate `subprocess` invocation overwrote all four files;
   sha256 before and after are **identical for all four**.
4. **Committed == fresh rebuild** — `test_committed_artifacts_match_a_fresh_rebuild`,
   plus `load_calibration()` re-verifying each `content_hash`.

The artifacts carry no timestamp, hostname, run id or filesystem path — all of that
is in the `.runlog.json` sidecars, which are explicitly not the artifact under test.
The series CSV is written in binary with literal `\n` and matches the existing
`.gitattributes` pin `data/forecast_archive/*.csv text eol=lf`.

### The calibration file names its own statistic

`forecast_calibration.build_all()` hashes only `(city, station, target_date,
init_time_utc, lead_hours, source, forecast_high_f, spread_f)` and never reads the
CSV's `provenance` column — correct for a source-agnostic calibrator, and dangerous
here, because two different statistics could both be labelled `source="gefs"`.

So `stamp_statistic()` appends one additive top-level `statistic` block to the
payload **after** `build_all()` returns it, then re-seals `content_hash` via
`finalize()`. `build_all()` itself is called unmodified and every field it produced
is left byte-for-byte as produced; the block contains no wall-clock data, so
determinism is preserved (verified above, and pinned by
`test_stamping_is_additive_and_carries_no_wallclock`). It records `built_on`,
`live_provider_statistic`, `are_they_the_same: false`, the applied offset and its
provenance, the spread rule and the §6.5 spread caveat.

**Note for workstream D:** `*_gefs_v1.json` carries this one extra top-level key
that `*_gfs_mex_v1.json` does not. `schema_version` is unchanged and every existing
field is untouched, so a consumer reading named keys is unaffected. A consumer that
asserts an exact key set would need to allow it.

---

## 9. Which source should Phase 3 use?

Measured, not asserted. A simple unweighted mean of the two day-of forecasts —
**no fitted parameters**, so there is nothing to overfit — on the days both sources
cover:

| City | n | blend bias °F | **blend σ °F** | best single σ | corr(err_gefs, err_mex) |
|---|---|---|---|---|---|
| NY | 209 | +0.367 | **3.042** | 3.374 (mex) | 0.316 |
| CHI | 209 | −0.485 | **3.104** | 3.762 (gefs) | 0.285 |
| LAX | 209 | −0.356 | 2.573 | **2.141 (mex)** | 0.475 |
| MIA | 208 | −1.336 | 1.787 | **1.707 (mex)** | 0.487 |

The two sources' errors are only 0.28–0.49 correlated, so they carry genuinely
partly independent information. **Recommendation:**

* **NY** — blend. σ 3.04 beats `gfs_mex` 3.37 and GEFS 4.11, and it converts the
  one EC-3 failure into a comfortable pass.
* **CHI** — blend. σ 3.10 beats both, and beats `gfs_mex`'s marginal 3.98 by
  enough to remove workstream B's "passes by 0.02 °F" caveat.
* **LAX, MIA** — `gfs_mex` alone. The blend is worse at both; GEFS adds noise, not
  information, where the errors are most correlated.
* **Do not make GEFS the sole live source anywhere.** It is the better source at no
  city on σ, and the worse source at three.

**Caveat, and it is not small:** the blend numbers are in-sample on the same 209
days. No weights were fitted (a fixed 0.5/0.5 average), and the per-city
keep/blend choice is a 3-way selection on 4 cities, which is itself a mild
selection. Before Phase 3 relies on it, the choice should be re-checked on a held-out
period — the natural one being the SON window the truth archive does not yet cover.
Treat §9 as a measured hypothesis with a stated test, not as a settled parameter.

This also means the FR-2.1 architecture needs revisiting at the go/no-go: the PRD
names GEFS primary with NWS-point degradation. What the data supports is *both*
model sources live, with `gfs_mex` as the anchor. The degradation ladder in
`EnsembleProvider` (which already refuses to invent a σ) is unaffected.

---

## 10. Gaps, caveats, and everything that could not be verified

1. **The whole comparison is truth-limited to 209 days, 2025-12-28…2026-07-24.**
   SON is absent, DJF has 63 days. Every seasonal conclusion is unvalidated for
   autumn. Rebuild when Sep–Nov truth exists.
2. **00Z cycle only.** GEFS *can* do something `gfs_mex` structurally cannot — a
   **12Z cycle covers the remaining hours of the same local day**, i.e. a genuine
   intra-day update, which is exactly what the FR-3.1(b) lock-in strategy needs and
   which MEX's 12Z run cannot produce (workstream B §9 caveat 4). It was not
   backfilled here and is **not** reflected in any number above. This is the
   strongest remaining argument for keeping GEFS in the stack and is the single
   highest-value follow-up: `--cycle-hour 12 --lead-days 0`.
3. **Leads stop at ~80 h.** `lead_days 0,1,2,3` gives four buckets; `gfs_mex`
   reaches 176 h. Extending is a flag change and ~1.5 GB per extra lead-day.
4. **The offset conflates two effects** (Jensen's max/mean gap and NCEP's
   30-member vs the provider's 31-member set) and is measured on n=5 per city with
   sd ≤0.09 °F. It is applied as one constant. Since it provably cannot move σ, the
   only exposure is a ≤0.1 °F error in `bias_f`.
5. **`spread_f` is an approximation** — `gespr` at the argmax interval is the
   ensemble spread of the TMAX field in that interval, not the spread of the
   members' daily maxima, which NCEP does not publish. §6.5 shows it has no per-day
   skill regardless.
6. **MIA's −2.50 °F cold bias is not explained**, only measured. It is
   lead-invariant and season-varying (−1.32 DJF → −3.24 JJA), which points at the
   grid node rather than at forecast skill, and §7.1 shows the 0.5° node would be
   warmer. Untested hypothesis; do not act on it without moving the live node too.
7. **NY's EC-3 failure is a real exclusion, not a rounding miss.** σ = 4.107 vs a
   4.0 bound, on 209 days. It is not a single outlier: NY's σ exceeds 3.7 in DJF
   and MAM independently.
8. **The manifest is 1.81 MB** (8 820 record entries). It is the provenance
   deliverable; if that is too large to commit, the alternative is to drop the
   per-record `records` array and keep only failures — but then a reader can no
   longer join a published number to the bytes that produced it.
9. **The `data/ensemble/cache/series/` resume layer is already gitignored** by the
   existing `data/ensemble/cache/` entry (verified with `git check-ignore`). No
   `.gitignore` change is requested.
10. **No binary fixture was added.** `tests/fixtures/gefs_backfill/cycle_2026072000_records.json`
    is plain JSON, so the `.gitattributes` `tests/fixtures/** text eol=lf` rule is
    safe on it and **no `.gitattributes` exception is needed**.

---

## 11. Files delivered

| Path | Note |
|---|---|
| `scripts/backfill_ensemble_history.py` | resumable, rate-limited backfill + `--measure-offset`, `--compare-resolution`, `--validate-spread`, `--offline`, `--build-calibration` |
| `src/calibration/gefs_series.py` | derived-product daily highs; offset table; spread units; 0.5° comparison constants |
| `data/forecast_archive/forecast_series_gefs.csv` | 3 360 rows, LF, `spread_f` populated on 100 % |
| `data/forecast_archive/gefs_manifest.json` | every S3 key, byte range, byte count, failure, missing day |
| `data/calibration/{NY,CHI,LAX,MIA}_gefs_v1.json` + `.runlog.json` | the artifacts; existing `*_gfs_mex_v1.json` untouched |
| `tests/test_gefs_backfill.py` | **48 tests, all passing, fully offline** |
| `tests/fixtures/gefs_backfill/cycle_2026072000_records.json` | 22 real decoded records (text JSON) |
| `reports/phase2/ws_f_report.md` | this file |

```
PYTHONPATH=. python -m pytest tests/test_gefs_backfill.py -q     # 48 passed
```

Only this file was run — the full suite is prohibited on this machine.

**No file outside the ownership grant was created or modified.**
`src/data/ensemble_provider.py` and `src/calibration/forecast_calibration.py` were
imported, never edited; `data/weather_truth/**` was opened read-only;
`.gitignore`, `.gitattributes`, `requirements.txt` and `PRD.md` were not touched.
No dependency was added. A `--json-out` probe artifact was written to
`reports/phase2/` during development and **deleted**, because the grant covers
`reports/phase2/ws_f_*.md` only; regenerate it with
`--compare-resolution --json-out <path>`.
