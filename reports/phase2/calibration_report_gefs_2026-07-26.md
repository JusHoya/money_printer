# Phase 2 forecast calibration report -- 2026-07-26

- **Source:** `gefs` (GEFS ensemble derived products `geavg`/`gespr`, `TMAX:2 m above ground`, NOAA NODD `pgrb2sp25` 0.25 deg)
- **Calibration version:** v1
- **Forecast series:** `data/forecast_archive/forecast_series_gefs.csv`
- **Truth:** `data/weather_truth/cli_daily_high_<STATION>.csv` (NWS Climatological Report via IEM; verified 835/835 in Phase 1)
- **Error convention:** `error_f = forecast_high_f - truth_high_f` (**positive = forecast too warm**)

## Exit criterion 2 -- paired-day coverage and determinism

> "Calibration files exist for all 4 cities, each built from >=60 paired forecast-vs-CLI days (backfill + live), reporting bias and sigma by lead time; recomputation is deterministic from inputs (byte-identical on re-run)."

| City | Station | Paired rows (all leads) | Distinct target dates | Day-of paired days | Dropped: no truth | Dropped: null truth | >=60 day-of |
|---|---|---|---|---|---|---|---|
| LAX | KLAX | 833 | 209 | 209 | 7 | 0 | PASS |
| CHI | KMDW | 833 | 209 | 209 | 7 | 0 | PASS |
| MIA | KMIA | 829 | 208 | 208 | 11 | 0 | PASS |
| NY | KNYC | 833 | 209 | 209 | 7 | 0 | PASS |

**Determinism check PASSED.** `build_all()` was run twice against the same on-disk inputs and the canonical serialization of every city's payload compared byte-identical (4/4 cities). Content hashes: `LAX` `dee83a276c15`, `CHI` `0374eb1bf8d4`, `MIA` `97a27ff4e52f`, `NY` `446745edef19`. The artifact carries no timestamp, hostname, run id, or filesystem path; that metadata lives in the `.runlog.json` sidecar, which is excluded from the comparison.

## Exit criterion 3 -- day-of sigma sanity bound

> "Measured day-of sigma per city is published in the calibration report and is <=4 degF for at least 3 of 4 cities (sanity bound: published NWS accuracy ~2.5 degF; a city failing this is excluded, not fudged)."

`day_of` is the lead bucket `[-24, 12)` hours from model runtime to the start of the target **local** day. For this source every day-of row comes from the 00Z GEFS cycle -- the backfill requests no other cycle hour -- so the guidance is on the wire before that day's maximum occurs.

| City | Station | n (day-of) | bias (degF) | **sigma (degF)** | MAE (degF) | <= 4 degF |
|---|---|---|---|---|---|---|
| LAX | KLAX | 209 | -0.33 | **3.77** | 3.02 | PASS |
| CHI | KMDW | 209 | -1.10 | **3.76** | 2.93 | PASS |
| MIA | KMIA | 208 | -2.50 | **2.42** | 3.10 | PASS |
| NY | KNYC | 209 | 0.68 | **4.11** | 3.32 | FAIL |

**3 of 4 cities within the 4 degF bound -> EC-3 MET.**

### What "day-of" means -- and what these sigma may not be applied to

Read this before reusing a sigma above anywhere in Phase 3.

Across the cities above, the leads actually observed inside the `day_of` bucket are **4..8 h** to the **start of the target local day** -- every one of them from the 00Z cycle issued the *evening before* that day, roughly 10-16 h ahead of a typical afternoon maximum. The bucket's lower edge would admit a genuinely intraday run (a negative lead is a cycle initialised *during* the target day), but **no row in this sample has one**, which is why every bucket publishes its observed lead range.

So `day_of` bias and sigma describe an **evening-before forecast, not an intraday update**. PRD FR-3.1(b)'s lock-in strategy re-forecasts at midday and trades on the update; these numbers were not measured on a midday re-forecast and must not be applied to one as though they were.

The direction of the substitution error is known: a shorter-lead forecast is normally more accurate, so an evening-before sigma is an **upper bound** on a midday one. Reusing it therefore widens the predictive distribution and shrinks the apparent edge -- conservative, for a rule that buys a narrow bracket. But the *size* of the gap is unmeasured, and conservative is not correct: any rule whose EV improves under a wider distribution -- selling tails, pricing wide brackets, sizing on a fat sigma -- is flattered rather than penalised by it. Phase 3 must calibrate the lead it actually trades before pricing on it.

### The margin is thin everywhere, and the cold half fails

This source clears EC-3 by the letter of the rule and by very little else. Every city's margin to the bound, measured on the sample above and none of it reached by re-bucketing or trimming:

- **NY** (KNYC): sigma **4.11** against the 4.00 degF bound -- over by **0.11 degF** on n=209. -> FAIL
- **CHI** (KMDW): sigma **3.76** against the 4.00 degF bound -- under by **0.24 degF** on n=209. -> PASS
- **LAX** (KLAX): sigma **3.77** against the 4.00 degF bound -- under by **0.23 degF** on n=209. -> PASS
- **MIA** (KMIA): sigma **2.42** against the 4.00 degF bound -- under by **1.58 degF** on n=208. -> PASS

**NY fails the bound.** Per the exit criterion's own rule -- *"a city failing this is excluded, not fudged"* -- that city is **excluded from this source, not adjusted, re-bucketed, or re-fitted to fit**. No day was dropped, no error was trimmed or winsorised, and the failing city's number is published above in full rather than omitted. EC-3 is met on the remaining cities and on nothing else.

Sensitivity, split chronologically and leave-one-out, on the same sample:

| City | n | pooled sigma | first half (dates, n) | sigma | second half (dates, n) | sigma | leave-one-out sigma range |
|---|---|---|---|---|---|---|---|
| LAX | 209 | **3.77** | 2025-12-28..2026-04-10 (104) | **4.05** | 2026-04-11..2026-07-24 (105) | **3.34** | 3.72..3.78 |
| CHI | 209 | **3.76** | 2025-12-28..2026-04-10 (104) | **4.10** | 2026-04-11..2026-07-24 (105) | **3.31** | 3.65..3.77 |
| MIA | 208 | **2.42** | 2025-12-28..2026-04-10 (104) | **3.00** | 2026-04-12..2026-07-24 (104) | **1.33** | 2.23..2.42 |
| NY | 209 | **4.11** | 2025-12-28..2026-04-10 (104) | **4.38** | 2026-04-11..2026-07-24 (105) | **2.90** | 4.00..4.12 |

**On the cold half of the window the bound holds at MIA and fails at CHI, LAX, NY.** Read literally, this source would **not** meet EC-3's "at least 3 of 4" on its first half; it meets it on an annual sample that is roughly 60% warm-season. The leave-one-out ranges are tight at every city, so no single extraordinary day is holding any verdict up or down -- the season is.

That is the honest statement of what this calibration supports: a warm-season sigma that Phase 3 may price on, and a cold-season sigma that must be re-measured (or traded smaller) once autumn and winter truth exists. The month and season tables below are published so the split is visible rather than averaged away.

## Error by lead time (all cities)

| City | Bucket | n | bias (degF) | sigma (degF) | MAE (degF) | observed lead h | status |
|---|---|---|---|---|---|---|---|
| LAX | day_of | 209 | -0.33 | 3.77 | 3.02 | 7..8 | ok |
| LAX | lead_12_36 | 209 | -0.13 | 4.24 | 3.39 | 31..32 | ok |
| LAX | lead_36_60 | 208 | -0.24 | 4.50 | 3.55 | 55..56 | ok |
| LAX | lead_60_84 | 207 | -0.21 | 4.66 | 3.72 | 79..80 | ok |
| LAX | lead_84_108 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| LAX | lead_108_132 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| LAX | lead_132_156 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| LAX | lead_156_180 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| LAX | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| CHI | day_of | 209 | -1.10 | 3.76 | 2.93 | 5..6 | ok |
| CHI | lead_12_36 | 209 | -1.16 | 4.67 | 3.48 | 29..30 | ok |
| CHI | lead_36_60 | 208 | -1.16 | 5.13 | 3.84 | 53..54 | ok |
| CHI | lead_60_84 | 207 | -1.51 | 5.99 | 4.49 | 77..78 | ok |
| CHI | lead_84_108 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| CHI | lead_108_132 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| CHI | lead_132_156 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| CHI | lead_156_180 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| CHI | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| MIA | day_of | 208 | -2.50 | 2.42 | 3.10 | 4..5 | ok |
| MIA | lead_12_36 | 208 | -2.74 | 2.56 | 3.40 | 28..29 | ok |
| MIA | lead_36_60 | 207 | -2.84 | 2.72 | 3.56 | 52..53 | ok |
| MIA | lead_60_84 | 206 | -2.93 | 2.67 | 3.62 | 76..77 | ok |
| MIA | lead_84_108 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| MIA | lead_108_132 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| MIA | lead_132_156 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| MIA | lead_156_180 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| MIA | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| NY | day_of | 209 | 0.68 | 4.11 | 3.32 | 4..5 | ok |
| NY | lead_12_36 | 209 | 0.57 | 4.46 | 3.50 | 28..29 | ok |
| NY | lead_36_60 | 208 | 0.40 | 4.64 | 3.66 | 52..53 | ok |
| NY | lead_60_84 | 207 | 0.37 | 5.31 | 4.20 | 76..77 | ok |
| NY | lead_84_108 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| NY | lead_108_132 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| NY | lead_132_156 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| NY | lead_156_180 | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| NY | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |

## Day-of error by month

Computed on the `day_of` bucket only. Buckets with n < 20 are reported as insufficient and carry no statistics -- they are not merged into a neighbour to reach a quorum.

| City | Month | n | bias (degF) | sigma (degF) | MAE (degF) | status |
|---|---|---|---|---|---|---|
| LAX | 2025-12 | 4 | -- | -- | -- | insufficient |
| LAX | 2026-01 | 31 | -2.65 | 3.14 | 3.27 | ok |
| LAX | 2026-02 | 28 | -1.61 | 3.06 | 2.63 | ok |
| LAX | 2026-03 | 31 | 1.49 | 4.70 | 4.21 | ok |
| LAX | 2026-04 | 30 | -1.75 | 2.36 | 2.26 | ok |
| LAX | 2026-05 | 31 | 0.12 | 3.09 | 2.42 | ok |
| LAX | 2026-06 | 30 | 0.80 | 2.96 | 2.50 | ok |
| LAX | 2026-07 | 24 | 2.44 | 3.68 | 3.63 | ok |
| CHI | 2025-12 | 4 | -- | -- | -- | insufficient |
| CHI | 2026-01 | 31 | -1.57 | 2.23 | 2.03 | ok |
| CHI | 2026-02 | 28 | -3.07 | 3.17 | 3.60 | ok |
| CHI | 2026-03 | 31 | -1.65 | 5.40 | 4.25 | ok |
| CHI | 2026-04 | 30 | -0.15 | 5.24 | 4.23 | ok |
| CHI | 2026-05 | 31 | -0.72 | 3.24 | 2.59 | ok |
| CHI | 2026-06 | 30 | -0.31 | 2.99 | 2.29 | ok |
| CHI | 2026-07 | 24 | -0.39 | 1.85 | 1.46 | ok |
| MIA | 2025-12 | 4 | -- | -- | -- | insufficient |
| MIA | 2026-01 | 31 | -1.20 | 2.99 | 2.61 | ok |
| MIA | 2026-02 | 28 | -1.69 | 3.34 | 3.03 | ok |
| MIA | 2026-03 | 31 | -2.41 | 2.69 | 3.12 | ok |
| MIA | 2026-04 | 29 | -3.41 | 1.42 | 3.41 | ok |
| MIA | 2026-05 | 31 | -2.88 | 1.71 | 3.10 | ok |
| MIA | 2026-06 | 30 | -3.45 | 0.88 | 3.45 | ok |
| MIA | 2026-07 | 24 | -2.98 | 1.07 | 3.00 | ok |
| NY | 2025-12 | 4 | -- | -- | -- | insufficient |
| NY | 2026-01 | 31 | -0.79 | 3.94 | 2.80 | ok |
| NY | 2026-02 | 28 | -3.46 | 3.35 | 3.98 | ok |
| NY | 2026-03 | 31 | -0.21 | 4.59 | 3.73 | ok |
| NY | 2026-04 | 30 | 0.21 | 4.12 | 3.18 | ok |
| NY | 2026-05 | 31 | 1.34 | 2.13 | 1.87 | ok |
| NY | 2026-06 | 30 | 3.01 | 1.72 | 3.01 | ok |
| NY | 2026-07 | 24 | 5.33 | 2.09 | 5.33 | ok |

## Day-of error by season

| City | Season | n | bias (degF) | sigma (degF) | MAE (degF) | status |
|---|---|---|---|---|---|---|
| LAX | DJF | 63 | -2.36 | 3.20 | 3.12 | ok |
| LAX | JJA | 54 | 1.53 | 3.37 | 3.00 | ok |
| LAX | MAM | 92 | -0.03 | 3.74 | 2.97 | ok |
| CHI | DJF | 63 | -2.13 | 2.80 | 2.68 | ok |
| CHI | JJA | 54 | -0.34 | 2.52 | 1.92 | ok |
| CHI | MAM | 92 | -0.85 | 4.71 | 3.68 | ok |
| MIA | DJF | 63 | -1.32 | 3.24 | 2.84 | ok |
| MIA | JJA | 54 | -3.24 | 0.99 | 3.25 | ok |
| MIA | MAM | 91 | -2.89 | 2.04 | 3.21 | ok |
| NY | DJF | 63 | -1.88 | 3.86 | 3.26 | ok |
| NY | JJA | 54 | 4.04 | 2.21 | 4.04 | ok |
| NY | MAM | 92 | 0.45 | 3.78 | 2.92 | ok |

## Caveats -- read these before trusting a number above

1. **`forecast_high_f` is not the statistic the live provider returns, and the artifact says so.** Every `*_gefs_v1.json` carries a top-level `statistic` block. The series behind this report is `max_t(geavg TMAX)` -- the daily maximum of the ensemble **mean field** -- plus a measured per-city offset. The live `EnsembleProvider.fetch()` returns `mean_m(max_t member TMAX)`, the mean of the per-member daily maxima. `max` and `mean` do not commute, so these are different numbers. The offset (NY +0.2514, CHI +0.0489, LAX +0.0269, MIA +0.0549 degF, measured on 20 city-cycles at the full 31 members) is a **constant per city**: it moves `bias_f` by exactly its own value and cannot move `sigma_f` at all, so the EC-3 verdict above is insensitive to it.
2. **`spread_f` is populated, and must not be used as a predictive sigma.** It is `gespr` -- the ensemble standard deviation of the TMAX field inside a single published interval (3 or 6 h, depending on the step) -- sampled where `geavg` attains its daily maximum. That is not the standard deviation of the members' daily maxima, which NCEP does not publish. Use the calibrated per-bucket `sigma_f` above. The column ships because it is real data with a documented meaning, not because it is a substitute for calibration.
3. **Nearest-node sampling at 0.25 degrees, not the station.** Each city is read at the single nearest GEFS node: KNYC 4.6 km, KMDW 3.8 km, KLAX 12.3 km, KMIA 8.0 km. The residual bias per city is close to lead-invariant in the lead table above -- it barely changes from `day_of` out to 60-84 h -- which is the signature of a **siting/statistic** term rather than forecast decay. The 0.5 degree product was measured and rejected: it moves the node 24-31 km from three of the four stations. See `reports/phase2/ws_f_report.md`.
4. **Seasonal coverage is incomplete, and this source is more exposed to it than `gfs_mex`.** The paired window is 2025-12-28 to 2026-07-24: SON is entirely absent, DJF is thin, and -- per the sensitivity table above -- the 4 degF bound does not hold on the cold half at most cities. Any seasonal correction taken from this file is unvalidated for autumn.
5. **The CLI settlement window applies here too.** The truth is the CLI local midnight-to-midnight maximum. A day whose maximum lands overnight settles far outside any bracket a daytime-max model would price. That is a live trading risk for this source exactly as it is for `gfs_mex`; FR-2.3 and the FR-2.4 go/no-go must model or exclude the overnight-max regime rather than average over it.
6. **Drops are accounted, not hidden.** Every `dropped_no_truth` row is a forecast whose target date has no row in the CLI truth archive: 2025-12-27 (a cycle preceding the truth window) and 2026-07-25..27 (targets not yet settled). MIA carries 4 more, and one fewer paired day, because KMIA has no CLI row at all for 2026-04-11 -- a gap in the truth archive, recorded in `data/weather_truth/coverage_report.json`, not a day this pipeline chose to discard. No paired day was dropped for being an outlier, and no error was trimmed, winsorized or clipped anywhere.
7. **Determinism holds within a checkout.** Input provenance is hashed over normalized row content rather than raw file bytes, so a CRLF checkout of the inputs cannot move a published number. The artifacts themselves are written LF; if they are ever hash-gated in CI, pin `data/calibration/*.json` and `data/forecast_archive/*.csv` `eol=lf` in `.gitattributes`.
8. **The GRIB2 decoder behind this series is in-house.** Its independence from a second implementation is evidenced in `reports/phase2/ws_g_decoder_independence.md` (comparison against Open-Meteo's separate Swift GRIB stack), not by the `geavg` cross-check in `reports/phase2/ec1_ensemble_members.md`, which decodes `geavg` with the same decoder and therefore cannot detect a global scale, offset or sign error.

## Reproduction

```bash
$env:PYTHONPATH = "."
# fetch (resumable; a completed run costs no network)
python scripts/backfill_ensemble_history.py --start 2025-12-27 --end 2026-07-24
# rebuild the series from the resume cache, then calibrate
python scripts/backfill_ensemble_history.py --offline --start 2025-12-27 --end 2026-07-24 --build-calibration
# ...or, equivalently and byte-identically, through the generic builder:
python scripts/build_calibration.py --source gefs --version 1 --check-deterministic
```

The two commands emit the same bytes. That is enforced rather than assumed: the `statistic` block is applied inside `forecast_calibration.build_all()` via its `SOURCE_ANNOTATORS` registry, so no producer can omit it. Before that fix the generic builder stripped the block and emitted a 10 402-byte file where the committed artifact is 11 932 bytes, with a different `content_hash` and no statistic warning at all.
