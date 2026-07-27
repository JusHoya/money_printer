# Phase 2 forecast calibration report -- 2026-07-26

- **Source:** `gfs_mex` (GFS extended MOS `N/X`, IEM `/api/1/mos.json`)
- **Calibration version:** v1
- **Forecast series:** `data/forecast_archive/forecast_series_gfs_mex.csv`
- **Truth:** `data/weather_truth/cli_daily_high_<STATION>.csv` (NWS Climatological Report via IEM; verified 835/835 in Phase 1)
- **Error convention:** `error_f = forecast_high_f - truth_high_f` (**positive = forecast too warm**)

## Exit criterion 2 -- paired-day coverage and determinism

> "Calibration files exist for all 4 cities, each built from >=60 paired forecast-vs-CLI days (backfill + live), reporting bias and sigma by lead time; recomputation is deterministic from inputs (byte-identical on re-run)."

| City | Station | Paired rows (all leads) | Distinct target dates | Day-of paired days | Dropped: no truth | Dropped: null truth | >=60 day-of |
|---|---|---|---|---|---|---|---|
| LAX | KLAX | 3135 | 209 | 209 | 120 | 0 | PASS |
| CHI | KMDW | 3135 | 209 | 209 | 120 | 0 | PASS |
| MIA | KMIA | 3120 | 208 | 208 | 135 | 0 | PASS |
| NY | KNYC | 3135 | 209 | 209 | 120 | 0 | PASS |

**Determinism check PASSED.** `build_all()` was run twice against the same on-disk inputs and the canonical serialization of every city's payload compared byte-identical (4/4 cities). Content hashes: `LAX` `7245f22c3693`, `CHI` `ab32388c98e6`, `MIA` `cfd2dc3bbe87`, `NY` `f8d016c41909`. The artifact carries no timestamp, hostname, run id, or filesystem path; that metadata lives in the `.runlog.json` sidecar, which is excluded from the comparison.

## Exit criterion 3 -- day-of sigma sanity bound

> "Measured day-of sigma per city is published in the calibration report and is <=4 degF for at least 3 of 4 cities (sanity bound: published NWS accuracy ~2.5 degF; a city failing this is excluded, not fudged)."

`day_of` is the lead bucket `[-24, 12)` hours from model runtime to the start of the target **local** day. For this source every day-of row comes from the 00Z GFS run, whose guidance is on the wire before that day's maximum occurs.

| City | Station | n (day-of) | bias (degF) | **sigma (degF)** | MAE (degF) | <= 4 degF |
|---|---|---|---|---|---|---|
| LAX | KLAX | 209 | -0.38 | **2.14** | 1.64 | PASS |
| CHI | KMDW | 209 | 0.13 | **3.98** | 2.65 | PASS |
| MIA | KMIA | 208 | -0.17 | **1.71** | 1.29 | PASS |
| NY | KNYC | 209 | 0.06 | **3.37** | 2.38 | PASS |

**4 of 4 cities within the 4 degF bound -> EC-3 MET.**

### What "day-of" means -- and what these sigma may not be applied to

Read this before reusing a sigma above anywhere in Phase 3.

Across the cities above, the leads actually observed inside the `day_of` bucket are **4..8 h** to the **start of the target local day** -- every one of them from the 00Z cycle issued the *evening before* that day, roughly 10-16 h ahead of a typical afternoon maximum. The bucket's lower edge would admit a genuinely intraday run (a negative lead is a cycle initialised *during* the target day), but **no row in this sample has one**, which is why every bucket publishes its observed lead range.

So `day_of` bias and sigma describe an **evening-before forecast, not an intraday update**. PRD FR-3.1(b)'s lock-in strategy re-forecasts at midday and trades on the update; these numbers were not measured on a midday re-forecast and must not be applied to one as though they were.

The direction of the substitution error is known: a shorter-lead forecast is normally more accurate, so an evening-before sigma is an **upper bound** on a midday one. Reusing it therefore widens the predictive distribution and shrinks the apparent edge -- conservative, for a rule that buys a narrow bracket. But the *size* of the gap is unmeasured, and conservative is not correct: any rule whose EV improves under a wider distribution -- selling tails, pricing wide brackets, sizing on a fat sigma -- is flattered rather than penalised by it. Phase 3 must calibrate the lead it actually trades before pricing on it.

### CHI passes on a knife edge -- do not read it as a comfortable pass

KMDW's day-of sigma is **3.98 degF against a 4.00 degF bound**, a margin of 0.02 degF on n=209. Measured sensitivity, all on the same sample:

- First half of the window (2025-12-28..2026-04-10, 104 days): sigma **4.59** -- would **FAIL** the bound outright.
- Second half (2026-04-11..2026-07-24, 105 days): sigma **2.78** -- passes comfortably.
- Leave-one-out sigma across all 209 days ranges 3.59..3.99, so no single outlier is holding it under the bound; the cold season is.

The honest reading is that CHI meets the bound *on an annual sample that is 60% warm-season*, and does not meet it in winter and early spring. Nothing was trimmed, excluded, or re-bucketed to produce the passing number -- the seasonal tables below are published precisely so this is visible rather than averaged away.

## Error by lead time (all cities)

| City | Bucket | n | bias (degF) | sigma (degF) | MAE (degF) | observed lead h | status |
|---|---|---|---|---|---|---|---|
| LAX | day_of | 209 | -0.38 | 2.14 | 1.64 | 7..8 | ok |
| LAX | lead_12_36 | 418 | -0.56 | 2.64 | 2.00 | 19..32 | ok |
| LAX | lead_36_60 | 418 | -0.70 | 3.14 | 2.37 | 43..56 | ok |
| LAX | lead_60_84 | 418 | -0.58 | 3.35 | 2.47 | 67..80 | ok |
| LAX | lead_84_108 | 418 | -1.03 | 3.59 | 2.66 | 91..104 | ok |
| LAX | lead_108_132 | 418 | -1.22 | 4.09 | 3.01 | 115..128 | ok |
| LAX | lead_132_156 | 418 | -1.74 | 4.22 | 3.28 | 139..152 | ok |
| LAX | lead_156_180 | 418 | -2.25 | 4.51 | 3.70 | 163..176 | ok |
| LAX | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| CHI | day_of | 209 | 0.13 | 3.98 | 2.65 | 5..6 | ok |
| CHI | lead_12_36 | 418 | 0.10 | 4.52 | 3.25 | 17..30 | ok |
| CHI | lead_36_60 | 418 | 0.10 | 4.92 | 3.69 | 41..54 | ok |
| CHI | lead_60_84 | 418 | 0.23 | 4.92 | 3.76 | 65..78 | ok |
| CHI | lead_84_108 | 418 | -0.78 | 6.04 | 4.55 | 89..102 | ok |
| CHI | lead_108_132 | 418 | -1.43 | 6.73 | 5.06 | 113..126 | ok |
| CHI | lead_132_156 | 418 | -1.36 | 7.84 | 5.79 | 137..150 | ok |
| CHI | lead_156_180 | 418 | -0.97 | 9.06 | 6.93 | 161..174 | ok |
| CHI | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| MIA | day_of | 208 | -0.17 | 1.71 | 1.29 | 4..5 | ok |
| MIA | lead_12_36 | 416 | -0.08 | 1.88 | 1.33 | 16..29 | ok |
| MIA | lead_36_60 | 416 | -0.02 | 2.07 | 1.40 | 40..53 | ok |
| MIA | lead_60_84 | 416 | -0.05 | 2.21 | 1.56 | 64..77 | ok |
| MIA | lead_84_108 | 416 | -0.25 | 2.34 | 1.65 | 88..101 | ok |
| MIA | lead_108_132 | 416 | -0.25 | 2.82 | 1.98 | 112..125 | ok |
| MIA | lead_132_156 | 416 | -0.28 | 2.98 | 2.12 | 136..149 | ok |
| MIA | lead_156_180 | 416 | -0.36 | 3.49 | 2.40 | 160..173 | ok |
| MIA | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |
| NY | day_of | 209 | 0.06 | 3.37 | 2.38 | 4..5 | ok |
| NY | lead_12_36 | 418 | -0.27 | 3.68 | 2.64 | 16..29 | ok |
| NY | lead_36_60 | 418 | -0.06 | 3.75 | 2.83 | 40..53 | ok |
| NY | lead_60_84 | 418 | -0.63 | 4.33 | 3.23 | 64..77 | ok |
| NY | lead_84_108 | 418 | -0.76 | 4.76 | 3.60 | 88..101 | ok |
| NY | lead_108_132 | 418 | -0.42 | 5.86 | 4.42 | 112..125 | ok |
| NY | lead_132_156 | 418 | -0.99 | 7.11 | 5.42 | 136..149 | ok |
| NY | lead_156_180 | 418 | -0.47 | 7.72 | 5.82 | 160..173 | ok |
| NY | lead_180_plus | 0 | -- | -- | -- | -- | insufficient (n < 20) |

## Day-of error by month

Computed on the `day_of` bucket only. Buckets with n < 20 are reported as insufficient and carry no statistics -- they are not merged into a neighbour to reach a quorum.

| City | Month | n | bias (degF) | sigma (degF) | MAE (degF) | status |
|---|---|---|---|---|---|---|
| LAX | 2025-12 | 4 | -- | -- | -- | insufficient |
| LAX | 2026-01 | 31 | -0.10 | 2.62 | 2.16 | ok |
| LAX | 2026-02 | 28 | -1.07 | 2.02 | 1.93 | ok |
| LAX | 2026-03 | 31 | 0.23 | 3.05 | 2.35 | ok |
| LAX | 2026-04 | 30 | -0.63 | 2.13 | 1.63 | ok |
| LAX | 2026-05 | 31 | -0.19 | 1.35 | 0.90 | ok |
| LAX | 2026-06 | 30 | -0.47 | 1.07 | 0.93 | ok |
| LAX | 2026-07 | 24 | -0.54 | 1.93 | 1.46 | ok |
| CHI | 2025-12 | 4 | -- | -- | -- | insufficient |
| CHI | 2026-01 | 31 | -0.74 | 4.80 | 3.19 | ok |
| CHI | 2026-02 | 28 | -0.50 | 3.74 | 2.50 | ok |
| CHI | 2026-03 | 31 | -2.16 | 5.75 | 3.97 | ok |
| CHI | 2026-04 | 30 | -0.13 | 3.55 | 2.33 | ok |
| CHI | 2026-05 | 31 | 0.65 | 2.32 | 2.13 | ok |
| CHI | 2026-06 | 30 | 2.73 | 2.68 | 2.87 | ok |
| CHI | 2026-07 | 24 | 1.29 | 1.57 | 1.54 | ok |
| MIA | 2025-12 | 4 | -- | -- | -- | insufficient |
| MIA | 2026-01 | 31 | 0.48 | 2.01 | 1.52 | ok |
| MIA | 2026-02 | 28 | 0.14 | 1.84 | 1.36 | ok |
| MIA | 2026-03 | 31 | -0.35 | 1.17 | 0.94 | ok |
| MIA | 2026-04 | 29 | -0.07 | 1.81 | 1.31 | ok |
| MIA | 2026-05 | 31 | -0.06 | 1.81 | 1.35 | ok |
| MIA | 2026-06 | 30 | -1.03 | 1.19 | 1.30 | ok |
| MIA | 2026-07 | 24 | -0.58 | 1.47 | 1.17 | ok |
| NY | 2025-12 | 4 | -- | -- | -- | insufficient |
| NY | 2026-01 | 31 | 0.00 | 2.98 | 2.32 | ok |
| NY | 2026-02 | 28 | 0.39 | 3.61 | 2.68 | ok |
| NY | 2026-03 | 31 | -2.45 | 4.61 | 3.16 | ok |
| NY | 2026-04 | 30 | -0.53 | 3.12 | 2.33 | ok |
| NY | 2026-05 | 31 | 0.06 | 2.46 | 1.94 | ok |
| NY | 2026-06 | 30 | 1.90 | 2.20 | 2.37 | ok |
| NY | 2026-07 | 24 | 1.33 | 2.60 | 2.08 | ok |

## Day-of error by season

| City | Season | n | bias (degF) | sigma (degF) | MAE (degF) | status |
|---|---|---|---|---|---|---|
| LAX | DJF | 63 | -0.56 | 2.38 | 2.05 | ok |
| LAX | JJA | 54 | -0.50 | 1.50 | 1.17 | ok |
| LAX | MAM | 92 | -0.20 | 2.29 | 1.63 | ok |
| CHI | DJF | 63 | -0.54 | 4.17 | 2.73 | ok |
| CHI | JJA | 54 | 2.09 | 2.35 | 2.28 | ok |
| CHI | MAM | 92 | -0.55 | 4.25 | 2.82 | ok |
| MIA | DJF | 63 | 0.40 | 1.94 | 1.48 | ok |
| MIA | JJA | 54 | -0.83 | 1.33 | 1.24 | ok |
| MIA | MAM | 91 | -0.16 | 1.61 | 1.20 | ok |
| NY | DJF | 63 | 0.21 | 3.17 | 2.37 | ok |
| NY | JJA | 54 | 1.65 | 2.38 | 2.24 | ok |
| NY | MAM | 92 | -0.98 | 3.64 | 2.48 | ok |

## Caveats -- read these before trusting a number above

1. **This is raw GFS extended MOS, not the NWS forecast and not an ensemble.** The ~2.5 degF figure the exit criterion cites as a sanity anchor is the accuracy of the *official human/NDFD* forecast, which routinely beats raw model output statistics. The sigmas here should therefore be read as an **upper bound** on what a calibrated ensemble (FR-2.1) can achieve, not as the best available forecast.
2. **A large part of the error is a sampling-window artifact, and it is measured, not assumed.** The MOS `N/X` daytime maximum covers roughly 0700-1900 local standard time; the CLI daily maximum is local midnight-to-midnight. Splitting the day-of sample on the CLI's own `high_time`:

   | City | max inside 07-19 LST | | | max outside 07-19 LST | | |
   |---|---|---|---|---|---|---|
   | | n | bias | sigma | n | bias | sigma |
   | NY | 172 | +0.48 | 2.88 | 37 | -1.89 | 4.64 |
   | CHI | 171 | +0.71 | 2.93 | 38 | -2.45 | 6.43 |
   | LAX | 206 | -0.36 | 2.14 | 2 | -2.50 | 2.12 |
   | MIA | 196 | -0.21 | 1.65 | 2 | +1.50 | 4.95 |

   About 18% of NY and CHI days set their maximum outside the MOS window, and those days carry roughly twice the sigma and a cold bias. LAX and MIA almost never do (2 days each) -- which is most of why their sigmas are so much lower. The worst single day in the sample is KMDW 2026-03-22: CLI high **71 degF set at 1:57 AM** ahead of a cold front, against a day-of MOS daytime max of 46 degF -- a -25 degF error that is not a forecast bust at all.

   **This is a live trading risk, not just a calibration nuisance.** Kalshi settles on the CLI value, so a day whose maximum lands at 2 AM settles far outside any bracket a daytime-max model would price. FR-2.3 and the FR-2.4 go/no-go must either model the overnight-max regime explicitly or exclude days with that synoptic setup, not average over them.
3. **No spread is available from this source.** `spread_f` is blank in every row. NBM's guidance text does carry a max/min standard deviation (`XND`) but the IEM AFOS route serves only the latest bulletin, so it is not retrievable historically. A blank is recorded as a blank; it is never written as 0.0.
4. **Seasonal coverage is incomplete.** The paired window is 2025-12-28 to 2026-07-24: SON is entirely absent and DJF holds only 63 days. Any seasonal correction taken from this file is unvalidated for autumn and should be rebuilt once Sep-Nov truth exists.
5. **Day-of means 4-8 hours before the local day starts**, i.e. the 00Z run, roughly 10-16 hours ahead of a typical afternoon maximum. It is genuinely a forecast made before the max occurs, but it is *not* an intra-day forecast. MEX's 12Z run does not produce a same-day maximum, so a later-issued same-day number needs a short-range source (NBM/HRRR/GEFS), not this archive.
6. **Drops are accounted, not hidden.** Every `dropped_no_truth` row is a forecast whose target date lies outside the truth window (2025-12-20..27 at the start, 2026-07-25..31 at the end). No paired day was discarded for being an outlier, and no error was trimmed, winsorized or clipped anywhere in this pipeline.
7. **Determinism holds within a checkout.** Input provenance is hashed over normalized row content rather than raw file bytes, so a CRLF checkout of the inputs cannot move a published number. The artifacts themselves are written LF; if they are ever hash-gated in CI, pin `data/calibration/*.json` and `data/forecast_archive/*.csv` `eol=lf` in `.gitattributes`.

## Reproduction

```bash
$env:PYTHONPATH = "."
python scripts/backfill_forecasts.py --start 2025-12-20 --end 2026-07-24
python scripts/build_calibration.py --source gfs_mex --version 1 --check-deterministic
```
