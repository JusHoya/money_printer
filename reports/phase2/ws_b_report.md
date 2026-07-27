# Workstream B — archived-forecast backfill and calibration pipeline

Phase 2, PRD FR-1.6 + FR-2.2. Branch `phase-2-forecast-calibration`.
All numbers below were measured on 2026-07-26; none are estimates.

---

## 0. Verdict up front

| Criterion | Verdict | Evidence |
|---|---|---|
| **EC-2** — 4 calibration files, ≥60 paired days each, bias+σ by lead time, byte-identical on re-run | **MET** | 208–209 day-of paired days per city (3120–3135 paired rows across all leads); two independent byte-comparisons pass 4/4 |
| **EC-3** — day-of σ ≤ 4 °F for ≥3 of 4 cities | **MET, 4/4** — but **CHI passes by 0.02 °F and fails on the cold half of the sample** | σ: MIA 1.71, LAX 2.14, NY 3.37, CHI 3.98 |

Nothing was trimmed, winsorized, excluded, re-bucketed, or re-sourced to reach
either verdict. The CHI margin and the seasonal breakdown that undermines it are
published in the calibration report rather than averaged away.

---

## 1. Verified upstream contract — IEM MOS/NBM archive

Endpoint: `GET https://mesonet.agron.iastate.edu/api/1/mos.json`.
Every row probed live on 2026-07-26. The full table is reproduced in the
`src/data/mos_guidance_provider.py` module docstring (house style, matching
`iem_cli_provider.py`); the decision-relevant rows:

| Query | Status | Result |
|---|---|---|
| `station=KNYC&model=MEX&runtime=2026-06-01T00:00:00Z` | 200 | 15 rows, ftime +24 h…+192 h, **`n_x` populated on all 15** |
| `…&model=NBS&…` | 200 | 23 rows, 3-hourly +6 h…+72 h, **`n_x` null on all 23** |
| `…&model=NBE&…` | 200 | 20 rows, +24 h…+264 h, **`n_x` null on all 20** |
| `…&model=GFS` / `NAM` | 200 | 21 rows, `n_x` on 5 (00Z/12Z ftimes only) |
| `…&model=LAV` | 200 | 38 rows, `n_x` null on all |
| `…&model=ECM` | 404 | no ECMWF MOS archived for these ICAOs |
| `…&model=MAV` | 422 | **not a valid model id here**, despite MAV being a real MOS product |
| `…&model=ZZZ` | 422 | error body reveals the whitelist: `^(AVN\|GFS\|ETA\|NAM\|NBS\|NBE\|ECM\|LAV\|MEX)$` |
| `station=ZZZZ&…` | 404 | unknown station is an honest error (unlike the CLI endpoint) |
| `runtime=1990-…` / `2030-…` / `…T06:00:00Z` | 404 | pre-archive, future, and non-existent run hours all 404 |
| `station=KNYC&model=MEX` (**no `runtime`**) | **200** | **silently returns the LATEST run** (probed: `2026-07-26 12:00`) |
| `…&sts=…&ets=…` (no `runtime`) | **200** | `sts`/`ets` **ignored**; latest run again. No bulk-range route exists |
| `station=KNYC&station=KMDW&…` | 200 | repeated `station` params **batch** — 4 stations in one call = 60 rows |
| `station=KNYC,KMDW` | 422 | comma lists rejected |
| `station=knyc` | 422 | must be uppercase |
| `runtime=2026-06-01` (date only) | 200 | accepted, treated as 00Z |
| `model=` or `station=` omitted | 422 | both required |
| `runtime=2024-01-01T00:00:00Z` / `2020-01-01T00:00:00Z` | 200 / 404 | archive reaches back to at least 2024, not to 2020 |

### The traps, and how they are guarded

1. **Omitting `runtime` is an HTTP 200 that returns the latest run.** This is
   the same failure the CLI archive's ignored `date` produced (five-year-old
   data at HTTP 200). A backfill that dropped the parameter would stamp *today's*
   forecast onto every historical target date and produce a spectacular,
   entirely fictional calibration.
   **Guard:** `runtime` is always sent (no code path can omit it), *and* every
   returned row's `runtime` is re-validated against the request —
   `test_a_substituted_runtime_is_refused` injects the exact "latest run"
   response and asserts it raises.
2. **`sts`/`ets` look like a range query and are silently ignored.** The
   provider exposes no range parameter it cannot honour; one request per run is
   the only correct access pattern.
3. **Batched requests could hide a substitution.** Every row's `station` and
   `model` are re-validated per row; a mismatch raises rather than being
   filtered out.
4. **404 ≠ failure.** A run absent from the archive is counted as a gap and
   returns `[]`; it is never cached and never replaced by a neighbouring run.
5. **Empty 200 is never cached.** One transient blank response would otherwise
   freeze that run as a permanent gap (same rule as `iem_cli_provider`).

### Raw-text route: evaluated and rejected (with two more silent-200 traps)

`GET /cgi-bin/afos/retrieve.py?pil=NBSNYC&limit=1` **does** return NBM guidance
text carrying `TXN` (max/min) **and `XND` (its standard deviation — a real
spread)**. It is unusable for backfill:

* `&date=2026-06-01` → **HTTP 200 with today's bulletin**; the parameter is
  silently ignored.
* `&sdate=/&edate=` → `ERROR: Could not Find: NBSNYC` **inside an HTTP 200 body**.
* `/api/1/nws/afos/list.json?pil=NBSNYC&date=2026-06-01` → HTTP 200, `"data":[]`.

So NBM's spread field is reachable **live but not historically**. That is why
`spread_f` is blank for this source — blank meaning "this source publishes no
spread", which is never encoded as `0.0`.

---

## 2. What `n_x` means — verified empirically, not assumed

The orchestrator's brief guessed `n_x` was "very likely your daily-high
forecast". Two corrections came out of the probe:

* **`n_x` is null on every NBS and NBE row.** NBM cannot be the backfill source
  through this endpoint at all. (This is why the module is named
  `mos_guidance_provider.py`, not `nbm_provider.py` — naming it after NBM would
  have been a lie about what it returns.)
* **`n_x` is max *or* min**, depending on which 12-hour period the forecast hour
  closes, and nothing in the JSON says which.

Resolved by measurement: 840 rows (14 × 00Z MEX runs, 4 stations, one summer
week + one winter week) bucketed by the **local** hour of `ftime` and differenced
against CLI truth:

| bucket (local hour of `ftime`) | mean(`n_x` − CLI **high**) | mean(`n_x` − CLI **low**) |
|---|---|---|
| KNYC 19–20 (00Z ftime) | −0.70 / +0.62 | +11.3 / +15.9 |
| KNYC 07–08 (12Z ftime) | −10.2 / −14.5 | +1.7 / +0.6 |
| KMDW 18–19 (00Z ftime) | −0.48 / +2.80 | +14.6 / +19.5 |
| KMDW 06–07 (12Z ftime) | −11.0 / −14.0 | +4.1 / +2.4 |
| KLAX 16–17 (00Z ftime) | −1.68 / +0.00 | +22.8 / +9.7 |
| KLAX 04–05 (12Z ftime) | −22.9 / −9.0 | +0.9 / +0.6 |
| KMIA 19–20 (00Z ftime) | −0.68 / −1.57 | +16.3 / +13.2 |
| KMIA 07–08 (12Z ftime) | −16.2 / −14.0 | +1.1 / +0.9 |

Evening-`ftime` rows track the CLI **high**; morning-`ftime` rows track the CLI
**low** — in every city and both seasons. Independent corroboration from the raw
bulletin: `MEXNYC` for the 2026-07-26 12Z run prints `FHR 24 36| 48 60|` under
day labels `MON 27| TUE 28|` with `N/X 69 79| 70 79|` — FHR 24 (12Z Mon) and
FHR 36 (00Z Tue) are both filed under **Monday the 27th**, exactly the
"local calendar date of `ftime`" rule.

Classification is on the **local** hour (`zoneinfo`), never the UTC hour.
`test_utc_hour_classification_would_break_lax` pins this: for a 00Z run,
ftime `2026-06-02 00:00Z` is `2026-06-01 17:00 PDT` and belongs to **June 1**;
a naive UTC reading would shift every LAX label by a day.

**Model chosen: `MEX` (GFS extended MOS), source label `gfs_mex`.** It is the
only whitelisted model that populates `n_x` for all four stations across the
whole window, and its 8-day span gives lead buckets from 4 h to 176 h.

---

## 3. Backfill result

```
python scripts/backfill_forecasts.py --start 2025-12-20 --end 2026-07-24 --workers 4
```

| | |
|---|---|
| Runs requested (00Z + 12Z × 217 days) | **434** |
| Fetched live / served from cache | 428 / 6 |
| Missing (404) | **0** |
| Failed | **0** |
| Raw rows seen | 26,040 |
| Overnight minima discarded | 13,020 |
| `n_x` null rows | 0 |
| Unclassifiable local hour | 0 |
| **Forecast rows written** | **13,020** |

Output: `data/forecast_archive/forecast_series_gfs_mex.csv` (2.3 MB, LF, 13,020
rows), schema exactly as specified:

```
city,station,target_date,init_time_utc,lead_hours,source,forecast_high_f,spread_f,provenance
CHI,KMDW,2026-06-01,2026-06-01T00:00:00Z,5,gfs_mex,78.0,,https://mesonet.agron.iastate.edu/api/1/mos.json?station=KMDW&model=MEX&runtime=2026-06-01T00:00:00Z#ftime=2026-06-02T00:00Z
```

`provenance` is the re-fetchable URL plus the exact `ftime` the value came from.
`spread_f` is blank on every row (see §1).

### Paired-day counts per city

| City | Station | Forecast rows | Paired rows (all leads) | Distinct paired target dates | **Day-of paired days** | Dropped: no truth | Dropped: null truth |
|---|---|---|---|---|---|---|---|
| NY | KNYC | 3255 | 3135 | 209 | **209** | 120 | 0 |
| CHI | KMDW | 3255 | 3135 | 209 | **209** | 120 | 0 |
| LAX | KLAX | 3255 | 3135 | 209 | **209** | 120 | 0 |
| MIA | KMIA | 3255 | 3120 | 208 | **208** | 135 | 0 |

Target date coverage: **2025-12-28 … 2026-07-24** (the full truth window).
Lead times present: 4–8, 16–20, 28–32, 40–44, 52–56, 64–68, 76–80, 88–92,
100–104, 112–116, 124–128, 136–140, 148–152, 160–164, 172–176 h.

**Every dropped row is accounted for**: the unpaired target dates are exactly
`2025-12-20…27` (forecast runs preceding the truth window) and
`2026-07-25…31` (forecasts for days not yet settled), plus KMIA `2026-04-11`
which has no CLI row at all (already documented in
`data/weather_truth/coverage_report.json`). Verified by set difference; no day
was dropped for being an outlier.

Target was ≥150/city; **delivered 208–209/city day-of** and 3120–3135 rows
across all leads. EC-2's floor of ≥60 is exceeded by 3.5×.

---

## 4. Calibration schema — what workstream D codes against

`data/calibration/<CITY>_<SOURCE>_v<N>.json`, e.g. `NY_gfs_mex_v1.json`.
Written LF, canonical (`sort_keys`, `indent=1`, `ensure_ascii`), ~12 KB.

```jsonc
{
  "schema_version": 1,                 // refuse an unknown version
  "city": "NY", "station": "KNYC", "timezone": "America/New_York",
  "source": "gfs_mex", "version": 1,
  "units": "degF",
  "error_convention": "error_f = forecast_high_f - truth_high_f (positive = forecast too warm)",
  "min_bucket_n": 20,
  "sigma_sanity_bound_f": 4.0,

  "inputs": { "forecast_rows_for_city": 3255, "paired_rows": 3135,
              "dropped_no_truth": 120, "dropped_null_truth": 0,
              "dropped_lead_out_of_range": 0,
              "forecast_content_sha256": "sha256:…", "truth_content_sha256": "sha256:…" },

  "coverage": { "distinct_paired_target_dates": 209,
                "first_target_date": "2025-12-28", "last_target_date": "2026-07-24",
                "day_of_paired_days": 209, "day_of_distinct_target_dates": 209 },

  "day_of": { …stat block… },          // convenience alias of by_lead.day_of
  "by_lead":          { "day_of": {…}, "lead_12_36": {…}, …, "lead_180_plus": {…} },
  "by_month_day_of":  { "2026-01": {…}, … },   // day_of bucket only
  "by_season_day_of": { "DJF": {…}, "MAM": {…}, "JJA": {…} },
  "notes": [ … ],
  "content_hash": "sha256:…"           // sha256 of canonical payload minus this field
}
```

**Stat block** (identical shape everywhere; values below are the real
`NY_gfs_mex_v1.json` `day_of` block, not an illustration):

```jsonc
{ "n": 209, "sufficient": true,
  "bias_f": 0.0574, "sigma_f": 3.3736, "mae_f": 2.3828, "rmse_f": 3.366,
  "quantiles_f": { "p05": -5.0, "p25": -2.0, "p50": 0.0, "p75": 2.0, "p95": 5.0 },
  "lead_hours_observed": { "min": 4, "median": 4.0, "max": 5 },
  "lead_hours_range": [-24, 12],
  "distinct_target_dates": 209,
  "first_target_date": "2025-12-28", "last_target_date": "2026-07-24",
  "spread_f_coverage": { "rows_with_spread": 0, "rows_without_spread": 209,
                         "mean_spread_f": null } }
```

An insufficient bucket is exactly `{"n": k, "sufficient": false, "lead_hours_range": […]}`
— **no statistics at all**. Consumers must branch on `sufficient`, never on the
presence of `sigma_f`.

Contract notes for downstream:

* **Sign**: `bias_f > 0` means the forecast runs warm; the corrected forecast is
  `forecast − bias_f`. Getting this backwards inverts the edge and every
  downstream number still looks plausible.
* **`mean_spread_f: null`** means the source publishes no spread. It is not
  `0.0`, and treating a null as zero would size positions as if the forecast
  were certain.
* Load through `src.calibration.forecast_calibration.load_calibration(path)` —
  it validates `schema_version` and re-verifies `content_hash`.
* Lead buckets are half-open `[lo, hi)`; `day_of = [-24, 12)` h from model
  runtime to the start of the target **local** day. Every bucket publishes
  `lead_hours_observed`, so the actual band is never assumed.

---

## 5. EC-2 determinism evidence (the actual byte-compare)

Design:

* **No wall-clock timestamp, hostname, run id, or filesystem path in the
  artifact.** Run metadata goes to `<name>.runlog.json`, which is explicitly not
  the artifact under test. `test_artifact_contains_no_wallclock_or_host_metadata`
  walks the whole payload and fails on any timestamp-shaped key or value.
* **Input provenance is hashed over normalized row *content*, not raw file
  bytes**, so a CRLF checkout of the CSVs cannot move a published number
  (`test_content_fingerprint_is_immune_to_line_endings`).
* One rounding point (`round(x, 4)` at payload build); a local
  linear-interpolation `percentile()` instead of a library call, so a
  numpy/statistics bump cannot move a published number; `ddof=1` sample σ;
  sorted rows; `json.dumps(sort_keys=True, separators=(",",":"), indent=1)`;
  file written in **binary** with a literal `\n`.
* `content_hash` = sha256 over the canonical payload with `content_hash` removed.

Measured results:

| City | file bytes | file sha256 (16) | `content_hash` (16) | rebuild byte-identical |
|---|---|---|---|---|
| NY | 12064 | `c22a776032a867ea` | `f8d016c41909958a` | **yes** |
| CHI | 12065 | `8dbbfd57506176ba` | `ab32388c98e66632` | **yes** |
| LAX | 12091 | `1bae87eec0067ec9` | `7245f22c36937951` | **yes** |
| MIA | 12075 | `af35b7371ed7db7b` | `cfd2dc3bbe87783c` | **yes** |

Three independent checks, all green:

1. **In-process** — `scripts/build_calibration.py --check-deterministic` runs
   `build_all()` twice against the on-disk inputs and byte-compares the
   canonical serialization: 4/4 identical. Its verdict is embedded verbatim in
   the calibration report.
2. **Cross-process** — a separate `subprocess` invocation of the script
   overwrote the four committed files; the bytes on disk before and after were
   compared and are identical for all four.
3. **Test** — `test_rebuild_from_disk_is_byte_identical` builds twice **from
   disk**, writes to two different directories, and compares file bytes; it does
   not re-serialize an in-memory object.
   `test_committed_artifacts_match_a_fresh_rebuild` additionally asserts the
   committed files equal a fresh rebuild from the committed inputs.
4. **Anti-CRLF** — `test_written_file_is_lf_and_matches_the_canonical_bytes`
   asserts `b"\r\n" not in raw`.

---

## 6. EC-3 σ table

> "Measured day-of σ per city is published in the calibration report and is ≤4 °F
> for at least 3 of 4 cities (sanity bound: published NWS accuracy ~2.5 °F; a
> city failing this is excluded, not fudged)."

| City | Station | n (day-of) | bias °F | **σ °F** | MAE °F | ≤4 °F |
|---|---|---|---|---|---|---|
| MIA | KMIA | 208 | −0.17 | **1.71** | 1.29 | **PASS** |
| LAX | KLAX | 209 | −0.38 | **2.14** | 1.64 | **PASS** |
| NY | KNYC | 209 | +0.06 | **3.37** | 2.38 | **PASS** |
| CHI | KMDW | 209 | +0.13 | **3.98** | 2.65 | **PASS (by 0.02 °F)** |

**4 of 4 → EC-3 MET.** No city was excluded, because none needed to be.

### The CHI caveat, stated plainly

CHI clears the bound by 0.02 °F. Measured sensitivity on the same sample:

* First half, 2025-12-28…2026-04-10 (104 days): **σ = 4.59 → would FAIL**.
* Second half, 2026-04-11…2026-07-24 (105 days): **σ = 2.78**.
* Leave-one-out σ over all 209 days: 3.586…3.990 — **no single outlier** is
  holding it under the bound; the cold season is.

CHI meets the bound on an annual sample that is ~60 % warm-season and does not
meet it in winter/early spring. Read the seasonal blocks
(`by_season_day_of`: CHI DJF 4.17, MAM 4.25, JJA 2.35), not the headline.

### Bias suspected to be a source artifact, not forecast error

The MOS `N/X` daytime maximum covers ≈0700–1900 LST; the CLI maximum is local
midnight-to-midnight. Splitting the day-of sample on the CLI's own `high_time`:

| City | max inside 07–19 LST: n / bias / σ | max outside 07–19 LST: n / bias / σ |
|---|---|---|
| NY | 172 / +0.48 / 2.88 | 37 / −1.89 / 4.64 |
| CHI | 171 / +0.71 / 2.93 | 38 / −2.45 / 6.43 |
| LAX | 206 / −0.36 / 2.14 | 2 / −2.50 / 2.12 |
| MIA | 196 / −0.21 / 1.65 | 2 / +1.50 / 4.95 |

~18 % of NY/CHI days set their maximum outside the MOS window and carry roughly
double the σ plus a cold bias. LAX and MIA essentially never do — which is most
of why their σ is low; it is climate, not model skill.

Worst single day in the whole sample: **KMDW 2026-03-22, CLI high 71 °F set at
1:57 AM** ahead of a cold front, against a day-of MOS daytime max of 46 °F — a
−25 °F "error" that is not a forecast bust at all.

**This is a live trading risk, not a calibration nuisance.** Kalshi settles on
the CLI value, so a day whose maximum lands at 2 AM settles far outside any
bracket a daytime-max model would price. FR-2.3/FR-2.4 must model or exclude
that regime explicitly rather than average over it. Flagged for workstream D and
the go/no-go.

---

## 7. Files delivered

| Path | Note |
|---|---|
| `src/data/mos_guidance_provider.py` | archived-guidance provider + verified-contract docstring |
| `src/calibration/__init__.py`, `src/calibration/forecast_calibration.py` | source-agnostic calibrator |
| `scripts/backfill_forecasts.py` | FR-1.6 backfill → normalized series |
| `scripts/build_calibration.py` | FR-2.2 build + report + `--check-deterministic` |
| `data/forecast_archive/forecast_series_gfs_mex.csv` | 13,020 rows, LF |
| `data/forecast_archive/cache/` | **434 files, 30 MB — recommend gitignore, see §9** |
| `data/calibration/{NY,CHI,LAX,MIA}_gfs_mex_v1.json` | the artifacts |
| `data/calibration/*.runlog.json` | sidecar run metadata, **not** the artifact under test |
| `reports/phase2/calibration_report_gfs_mex_2026-07-26.md` | EC-3 σ table + caveats (renamed from `calibration_report_2026-07-26.md` when the report filename was parameterized by source, so a `gefs` build could no longer overwrite this one — see `ws_g_report.md`) |
| `tests/test_mos_guidance_provider.py` (26 tests) | golden-keyed to 2 captured API responses |
| `tests/test_forecast_calibration.py` (25 tests) | sign, pairing, min-n, determinism |
| `tests/fixtures/mos/mex_4city_{2026-06-01,2026-01-10}T00Z.json` | real captured responses |

**51/51 tests pass.** (Only these two files were run — the full suite is
prohibited on this machine.)

### Independent verification performed

* **Separate implementation**: day-of `n`, bias, σ, MAE recomputed for all four
  cities in pandas, importing none of `src.calibration`. Matches the published
  JSON to <5e-4 on every statistic for every city.
* **Live spot-checks**: 8 day-of rows (2 per city) and 3 long-lead rows
  re-fetched from IEM and compared to the CSV — **11/11 exact matches**.
* **Drop accounting**: unpaired dates enumerated and confirmed to be exactly the
  out-of-window ones.

---

## 8. Reproduction

```bash
$env:PYTHONPATH = "."
python scripts/backfill_forecasts.py --start 2025-12-20 --end 2026-07-24 --workers 4
python scripts/build_calibration.py --source gfs_mex --version 1 --check-deterministic
python -m pytest tests/test_mos_guidance_provider.py tests/test_forecast_calibration.py -q
```

`--offline` on the backfill rebuilds the CSV from `data/forecast_archive/cache/`
with no network at all, and fails loudly on a cache miss rather than silently
emitting a shorter series.

---

## 9. Gaps, caveats, and proposed changes to files I do not own

### Caveats

1. **This is raw GFS extended MOS, not the NWS forecast and not an ensemble.**
   The ~2.5 °F anchor in EC-3 is the accuracy of the *official human/NDFD*
   forecast, which routinely beats raw MOS. These σ are an **upper bound** on
   what FR-2.1's calibrated ensemble should achieve — workstream A's GEFS output
   should improve on them, and the same calibrator ingests it unchanged.
2. **No spread from this source.** `spread_f` blank on all 13,020 rows. NBM's
   `XND` exists live but not historically. FR-2.3 cannot take a per-day spread
   from this file; it must use the per-bucket σ.
3. **SON is entirely absent** and DJF holds only 63 day-of days (window starts
   2025-12-28). Any seasonal correction is unvalidated for autumn; rebuild once
   Sep–Nov truth exists.
4. **"Day-of" is 4–8 h before the local day starts** (00Z run), ≈10–16 h ahead
   of a typical afternoon max — a genuine pre-max forecast, but *not* intra-day.
   MEX's 12Z run produces no same-day maximum, so a later same-day number needs
   a short-range source (NBM/HRRR/GEFS), not this archive. Relevant to the
   FR-3.1(b) lock-in strategy, which needs an intra-day update.
5. **The overnight-max regime (§6) is a settlement risk**, not just error.
6. `MODELS_WITH_NX` lists `GFS`/`NAM`/`ETA` as also populating `n_x` (5 rows per
   run, 00Z/12Z ftimes). They were not backfilled; MEX was chosen for its 8-day
   span. A second source is a drop-in `--model`/`--source` change if a
   short-lead comparison is wanted.

### Proposed diffs (I did not edit these files)

**`.gitignore`** — the run cache is 434 files / 30 MB of re-fetchable responses.
The CSV is the artifact; the cache is a convenience:

```diff
+# Phase 2: re-fetchable IEM MOS run cache (the normalized CSV is the artifact)
+data/forecast_archive/cache/
```

**`.gitattributes`** — only needed if these are ever hash-gated in CI. The
pipeline is already immune to input EOL (content hashing), and the artifacts are
written LF in binary, but a CRLF checkout would still change the *file* sha256:

```diff
+data/calibration/*.json          eol=lf
+data/forecast_archive/*.csv      eol=lf
+tests/fixtures/mos/*.json        eol=lf
```

**Naming deviation, flagged deliberately**: the ownership grant named
`src/data/nbm_provider.py` and `tests/test_nbm_provider.py`. Because NBS/NBE
carry no `n_x`, the module provides **GFS MOS**, not NBM, so it ships as
`src/data/mos_guidance_provider.py` / `tests/test_mos_guidance_provider.py`
under the grant's "or similarly-named" clause. No other file was created or
edited outside the grant; `data/weather_truth/**` was opened read-only.
