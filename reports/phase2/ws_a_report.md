# Workstream A — GEFS ensemble provider (PRD FR-2.1, Phase 2 EC-1)

**Date:** 2026-07-26 (artifacts stamped 2026-07-27 UTC)
**Branch:** `phase-2-forecast-calibration`
**Verdict:** **EC-1 MET, both halves.** Evidence and reproduction commands below.

---

## 1. The contract

PRD §5 **FR-2.1**:

> Ensemble provider: fetches GEFS members (NOMADS) for each city's daily high
> with graceful degradation to NWS point forecast + historical error
> distribution when the ensemble is unavailable; results cached; failures abort
> signal generation (never a silent default, per
> abort-on-missing-critical-input).

PRD §8 **Phase 2, exit criterion 1**:

> Ensemble provider returns ≥20 members for each of the 4 cities on ≥5
> consecutive days; on induced fetch failure it aborts signal generation with an
> INFO reason (no silent fallback default).

---

## 2. What was built

| File | Role |
| --- | --- |
| `src/data/ensemble_provider.py` | The provider: NOAA NODD S3 access, in-house GRIB2 decoder, local-calendar-day windowing, two-layer cache, abort contract, degradation path. |
| `scripts/fetch_ensemble.py` | Re-runnable producer of the EC-1 evidence artifact plus the validation probes. |
| `tests/test_ensemble_provider.py` | 98 offline tests. |
| `tests/fixtures/ensemble/` | One real GEFS `TMAX` record (431 982 B, stored base64 — see §10), its `.idx` sidecar, and a manifest pinning the decoded SHA-256. |
| `reports/phase2/ec1_ensemble_members.{json,md}` | The EC-1 evidence artifact. |
| `reports/phase2/ws_a_dependencies.txt` | Dependency request (none) + the rejected-library record. |
| `data/ensemble/cache/` | Regenerable download cache (records, forecasts, truth lookups). Already covered by the existing `.gitignore` entry. |

### Upstream source

FR-2.1 names NOMADS. **NOMADS OpenDAP (`nomads.ncep.noaa.gov/dods/gefs/…`)
returns HTTP 301 and is a dead end.** The provider instead reads the NOAA Open
Data Dissemination mirror of the same NCEP product on S3, anonymously over plain
HTTPS — no credentials, no `boto3`:

```
https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/00/atmos/pgrb2sp25/<member>.t00z.pgrb2s.0p25.f<HHH>
```

Each object has a `.idx` sidecar giving every record's byte offset, so a single
`Range` request pulls the one record needed (~430 KB) instead of the whole
~14 MB file. Verified live 2026-07-26: every date 2026-07-19 … 2026-07-26 is
present, `.idx` returns 200, ranged reads return 206.

Members: `gec00` (control) + `gep01`…`gep30` = **31 true members**. `geavg` and
`gespr` are derived products and are excluded from the member count — counting
them would inflate the number EC-1 gates on.

### The field: `TMAX`, not `TMP` — and the bias that avoids

The brief flagged that `pgrb2s` 3-hourly instantaneous `TMP` under-estimates a
true daily max. **The `pgrb2sp25` product also publishes a genuine
max-over-interval field**, `TMAX:2 m above ground`, so the provider uses that:

```
10:3759412:d=2026072500:TMP:2 m above ground:24 hour fcst:ENS=+1
13:5313475:d=2026072500:TMAX:2 m above ground:18-24 hour max fcst:ENS=+1
```

The bias avoided is **measured, not assumed**. Building the daily high from
3-hourly instantaneous `TMP` instead, on the 2026-07-24 00Z cycle across all 31
members:

| City | TMP daily high (mean °F) | TMAX daily high (mean °F) | TMP − TMAX |
| --- | --- | --- | --- |
| NY | 81.96 | 82.77 | **−0.81** |
| CHI | 82.05 | 82.48 | **−0.42** |
| LAX | 85.15 | 86.06 | **−0.91** |
| MIA | 89.20 | 89.83 | **−0.63** |

Reproduce with `--tmp-comparison`; per-member deltas are in the JSON under
`tmp_vs_tmax`.

### Windowing

`TMAX` interval structure, read off the live `.idx` files: a step at `6k+3`
maximises over `[6k, 6k+3]`; a step at `6k+6` over `[6k, 6k+6]`. The
half-interval `[6k+3, 6k+6]` is never published alone, so the finest tiling of a
24 h window is 6-hourly.

Each city's daily high is the max of that member's `TMAX` over the minimal set
of intervals covering the **station's local calendar day** (`zoneinfo`, no naive
datetimes anywhere). Because 6-hour buckets do not align with a local midnight,
the covered window spills past the requested one. **The spill is reported, not
absorbed** — every forecast's `provenance["coverage"]` carries requested vs
covered leads and the over-coverage in hours (measured: 4–5 h before / 1–2 h
after at day-ahead lead; the LAX window is tighter, 1 h / 2 h).

### GRIB2 decoding — in-house, zero new dependencies

`eccodes`/`cfgrib` and `pygrib` were both rejected on evidence, recorded in
`reports/phase2/ws_a_dependencies.txt`: `pip install eccodes` yields only the
Python binding, which raises `RuntimeError: Cannot find the ecCodes library`,
and no binary distribution of the native library exists for this platform
(`eccodeslib` and `eccodes-lib` both `No matching distribution found` on Python
3.14 / Windows). Both probe installs were uninstalled; the environment is clean.

The records use grid definition template **3.0** (regular lat/lon,
1440 × 721 @ 0.25°, `La1`=90N, `Lo1`=0, scan mode `0x00`) and data
representation template **5.3** (complex packing + 2nd-order spatial
differencing), no bitmap. The decoder is ~200 lines of `numpy` + `struct`.

**One trap cost real debugging time and is now guarded and documented.** WMO
data templates 7.2/7.3 pad each of the three group-descriptor lists to an octet
boundary; the widely copied `g2clib` `comunpack` reference reads them
bit-contiguously. With `NG=30714` at 7 bits per scaled group length the list
ends 2 bits short of an octet, so a bit-contiguous reader is 2 bits out of phase
on every packed value. **It does not crash.** The second differences still look
like small integers; they carry a ~0.5-count mean bias that double integration
amplifies into temperatures of order 1e9 K. Three independent checks now guard
it (group lengths must sum to the declared point count; packed values must fit
inside section 7; the minimum packed difference must be exactly 0), because
which one fires depends on where the phase error lands — on the record that
originally exposed the bug, only the last of the three caught it.

Product definition templates 4.1, 4.2, 4.11 and 4.12 are parsed, so the
interval and the statistical process are read **from the GRIB itself**, not from
the `.idx` label. A `TMAX` record is refused unless it declares the exact
interval the step layout promises *and* statistical process 2 (maximum).

---

## 3. EC-1 first half — ≥20 members × 4 cities × ≥5 consecutive days

**MET.** Artifact: `reports/phase2/ec1_ensemble_members.md` (human) and
`.json` (machine, 930 distinct S3 keys with byte ranges and every per-member
value). Produced only by `scripts/fetch_ensemble.py`; nothing in it is
hand-entered.

- 5 consecutive 00Z cycles: **2026-07-20 … 2026-07-24**, day-ahead targets
  2026-07-21 … 2026-07-25.
- **20/20 city-cycle runs succeeded with 31 members each** (floor is 20;
  lowest count on any run: 31).
- Grid nodes actually used:

| City | Station | Station lat/lon | Node (j, i) | Node lat/lon | Distance |
| --- | --- | --- | --- | --- | --- |
| NY | KNYC | 40.78333, −73.96667 | (197, 1144) | 40.75, −74.00 | 4.6 km |
| CHI | KMDW | 41.78417, −87.75528 | (193, 1089) | 41.75, −87.75 | 3.8 km |
| LAX | KLAX | 33.93806, −118.38889 | (224, 966) | 34.00, −118.50 | 12.3 km |
| MIA | KMIA | 25.79056, −80.31639 | (257, 1119) | 25.75, −80.25 | 8.0 km |

Nearest node, no interpolation. A bilinear estimate at the true station
coordinates is carried in provenance as a **diagnostic only**.

### One run initially failed, and that is worth recording

On the first pass, CHI 2026-07-21 returned **19 of 31 members** because the NODD
bucket answered several ranged requests with HTTP 503 under a 31-member burst.
The provider refused the forecast (`ENSEMBLE_INSUFFICIENT_MEMBERS`) rather than
returning a thin ensemble — the floor behaved exactly as designed on a real,
unplanned fault.

The fix was to retry the statuses that mean "ask again" (408/429/500/502/503/504
and transport faults) with bounded exponential backoff; 404 is never retried
because it is an answer, not a transient. **The member floor was not lowered and
no data was substituted.** Re-run: 20/20 at 31 members.

### Decoder cross-check against an artifact this repo did not produce

Over the same 20 city-cycles, the mean of the 31 decoded members' daily highs
differs from the daily high decoded from **NCEP's own `geavg` product** by
**−0.048 °F to +0.316 °F, mean +0.096 °F**. `max` and `mean` do not commute, so
Jensen puts the member mean at or above the mean product — the observed sign and
magnitude are what a correct decoder should produce. A decoding fault would show
up as degrees, not hundredths.

### Sanity check against CLI settlement truth

Paired against the Phase-1 CLI truth (committed backfill CSVs plus one live IEM
lookup for 2026-07-25), day-ahead lead:

| City | Paired days | Bias (nearest node) | Bias (bilinear) | MAE |
| --- | --- | --- | --- | --- |
| NY | 5 | **+2.92 °F** | +2.74 °F | 2.92 °F |
| CHI | 5 | **+1.61 °F** | +1.61 °F | 2.28 °F |
| LAX | 5 | **+5.78 °F** | +5.61 °F | 5.78 °F |
| MIA | 5 | **−3.16 °F** | −2.08 °F | 3.16 °F |

This is a 5-day sanity check on the decoder and the windowing. **It is not the
FR-2.2 calibration**, which requires ≥60 paired days.

**Three findings Workstream B must not average away:**

1. **The biases do not share a sign.** MIA is cold, the other three are warm. A
   single global bias term is wrong.
2. **LAX is the problem child.** Mean +5.78 °F, worst day **+9.71 °F**
   (2026-07-24: model 87.71, truth 78). LAX highs are marine-layer suppressed
   and the 12.3 km node on the Santa Monica shoreline does not represent that.
   The bilinear diagnostic does not rescue it (+5.61 °F), so interpolation is
   not the fix. Against EC-3's "σ ≤ 4 °F for at least 3 of 4 cities", LAX is the
   likely exclusion candidate.
3. **The raw ensemble is under-dispersed.** Measured member σ over the sample is
   0.53–2.93 °F while mean-vs-truth error reaches 9.71 °F. Feeding the raw
   spread into FR-2.3 as a predictive σ would price tails far too confidently.

I predicted, before measuring, that the offshore LAX node would bias *cold*. It
biases warm. That prediction has been removed from the module docstring and
replaced with the measured numbers.

---

## 4. EC-1 second half — abort on induced failure, never default

**MET.** `EnsembleProvider.get_forecast_or_abort(city, target_date, init_time=None)`
is the contract Phase-3 strategies consume.

On failure it emits **exactly one INFO line** carrying a machine-readable reason
code, then re-raises `EnsembleUnavailable` so signal generation stops:

```
ENSEMBLE_ABORT city=NY target_date=2026-07-26 reason=ENSEMBLE_INSUFFICIENT_MEMBERS detail=...
```

To keep "exactly one INFO line" literally true, internal fetch/decode faults log
detail at DEBUG and carry it in the exception message, which the single INFO
line then reproduces in full. There is no default temperature, no last-known-good
substitution and no bare `except`. Reason codes are a closed set
(`REASON_CODES`); `EnsembleUnavailable` refuses to be constructed with an
unregistered one.

Tests proving it (`tests/test_ensemble_provider.py::TestAbortNeverDefault`):

- `test_induced_fetch_failure_aborts_with_one_info_line` — a session that 404s
  every `pgrb2s` object: asserts (a) `EnsembleUnavailable` with a registered
  reason code, (b) **exactly one** record at INFO or above from the module
  logger, matching `ENSEMBLE_ABORT … reason=…`, and (c) that the sentinel
  variable is untouched, i.e. **no value was returned**.
- `test_abort_does_not_fall_back_to_a_default_temperature` — no forecast is
  written to the cache on the abort path.
- `test_success_path_logs_nothing_at_info` — the single line is an abort signal,
  not routine noise.

### The FR-2.1 degradation path

Degradation to an NWS point forecast is implemented, and made deliberately hard
to reach by accident:

- **Off by default** (`allow_degraded=False`).
- **Requires an injected `sigma_provider`** — `(city, lead_hours) -> σ in °F`.
  Without one it refuses with `ENSEMBLE_DEGRADED_UNCALIBRATED` rather than
  inventing a spread. A non-positive or non-finite σ is refused too. This is the
  seam Workstream B's calibration plugs into; no file of theirs was touched.
- **Announces itself** on its own INFO line
  (`ENSEMBLE_DEGRADED city=… source=nws_point_degraded primary_reason=… point_f=… sigma_f=…`).
- **Stamps `source="nws_point_degraded"`** on the returned object and sets
  `provenance["degraded"]=True` with a plain-English note, so no downstream
  consumer or report can mistake it for a real ensemble.
- Pseudo-members are **deterministic Gaussian quantiles** (`statistics.NormalDist`),
  not random draws, so a re-run is byte-identical.
- Caveat recorded in code: NWS daytime periods run ~06:00–18:00 local, so the
  point value is a daytime high, not strictly a calendar-day maximum.

---

## 5. Public API (stable — Workstreams D and E code against this)

```python
from src.data.ensemble_provider import (
    EnsembleProvider, EnsembleForecast, EnsembleUnavailable,
    CITIES, SOURCE_GEFS, SOURCE_NWS_DEGRADED, REASON_CODES,
)

provider = EnsembleProvider()                 # cache: data/ensemble/
forecast = provider.get_forecast_or_abort("NY", date(2026, 7, 26))
```

```python
@dataclass(frozen=True)
class EnsembleForecast:
    city: str                      # "NY" | "CHI" | "LAX" | "MIA"
    station: str                   # "KNYC" | "KMDW" | "KLAX" | "KMIA"
    target_date: datetime.date     # local calendar date of the daily high
    init_time: datetime            # tz-aware UTC model cycle
    lead_hours: int                # init -> start of the target local day
    members_f: tuple[float, ...]   # per-member daily high, degrees F
    source: str                    # SOURCE_GEFS | SOURCE_NWS_DEGRADED
    provenance: dict

    # derived, no extra I/O
    member_count: int
    mean_f: float
    sigma_f: float                 # sample sd (n-1)
    quantile_f(q: float) -> float  # empirical, linear interpolation
    to_dict() / from_dict()
```

```python
class EnsembleUnavailable(Exception):
    reason_code: str   # member of REASON_CODES
    detail: str
```

Provider surface:

| Member | Contract |
| --- | --- |
| `EnsembleProvider(cache_dir=None, *, session=None, members=DEFAULT_MEMBERS, min_members=20, allow_degraded=False, sigma_provider=None, max_workers=4, max_retries=4, retry_backoff=0.5, cycle_hour=0, publish_delay_hours=6, offline=False, timeout=60)` | Constructor. |
| `.connect() -> bool` | Bucket reachability; logs and returns `False`, does not raise. |
| `.fetch(city, target_date, init_time=None, *, use_cache=True) -> EnsembleForecast` | Real ensemble only. Raises `EnsembleUnavailable`; never degrades on its own. |
| `.get_forecast_or_abort(city, target_date, init_time=None, *, allow_degraded=None)` | **The contract for strategies.** One INFO line + re-raise on failure. |
| `.fetch_degraded(city, target_date, init_time=None, *, member_count=31)` | FR-2.1 fallback; needs `sigma_provider`. |
| `.default_init_time(reference=None) -> datetime` | Most recent cycle past `publish_delay_hours`. |
| `.fetch_record_values(init, member, fhour, nodes, *, field_name="TMAX")` | Low-level: Kelvin at grid nodes from one record. |

Useful module-level helpers: `CITIES`/`get_city`, `city_nodes()`,
`local_day_bounds_utc(date, tz)`, `tmax_windows(start_lead, end_lead)`,
`tmax_interval_for(fhour)`, `kelvin_to_fahrenheit`, `decode_grib2_record`,
`parse_idx`, `select_idx_record`, `verify_station_registry()`.

`provenance` keys on a real-ensemble forecast: `product`, `field`, `level`,
`bucket`, `members_used`, `members_requested`, `members_failed`, `s3_keys`,
`byte_ranges`, `forecast_hours`, `station{id,name,latitude,longitude,timezone}`,
`grid_node{j,i,latitude,longitude,distance_km,selection}`,
`coverage{local_day_start_utc,local_day_end_utc,requested_lead_hours,covered_lead_hours,over_coverage_hours,note}`,
`diagnostic_bilinear_f`, `fetched_at`.

---

## 6. Caching

Two layers under `data/ensemble/cache/`:

- `cache/records/<YYYYMMDDHH>/<member>_f<HHH>_<FIELD>.json` — decoded grid-node
  values per (cycle, member, forecast hour). **One 430 KB download serves all
  four cities**, because every registered city's node is requested together.
- `cache/forecasts/<CITY>_<target>_<cycle>Z.json` — assembled `EnsembleForecast`.
- (`cache/truth/` holds the script's read-only CLI-truth lookups, kept out of
  `data/weather_truth/` so this workstream cannot disturb the Phase-1 tree.)

Everything regenerable sits under one `cache/` directory, so the existing
`.gitignore` entry `data/ensemble/cache/` covers all of it. Verified:
`git status --porcelain data/ensemble` is empty.

A published model value is immutable, so both layers are cached without expiry.
**A failure is never cached** — not an empty listing, not a missing record, not a
decode error — mirroring the CACHING RULES in `src/data/iem_cli_provider.py`,
where a cached absence was a demonstrated defect. A cached forecast that no
longer clears the current `min_members` floor is not reused. Tests:
`TestRecordFetchAndCache`, `TestFetch::test_forecast_cache_round_trip`,
`test_cached_forecast_below_the_floor_is_not_reused`,
`test_failure_is_never_cached`.

The full EC-1 run cost ~9 minutes and ~0.5 GB (1 254 ranged requests) cold; the identical re-run that
produced the committed artifact took **49 seconds** off cache.

---

## 7. Tests

`tests/test_ensemble_provider.py` — **98 tests, all passing, fully offline.**

```
PYTHONPATH=. python -m pytest tests/test_ensemble_provider.py -q
98 passed in 2.19s
```

Coverage against the brief's list:

| Required | Where |
| --- | --- |
| Unit conversion | `TestUnitConversion` — known K/F pairs, round-trip, and an explicit guard that the Celsius formula is not used by accident. |
| Local-day max windowing across timezones | `TestLocalDayWindowing` (July + January bounds for all four cities; 23 h spring-forward and 25 h fall-back days; per-city leads 28/29/31/28 from one UTC instant) and `TestTmaxWindowSelection` (golden interval table off the live `.idx`; per-city window sets; coverage/contiguity/over-coverage properties). |
| Member-count floor | `TestFetch::test_member_floor_rejects_a_thin_ensemble`, `…accepts_exactly_the_floor`, `…a_member_missing_one_hour_is_dropped…`. |
| Cache behaviour | `TestRecordFetchAndCache` (cache hit makes zero HTTP calls; one download serves every city; a new node is a miss) + forecast-cache round trip and floor re-check. |
| Induced fetch failure | `TestAbortNeverDefault` — reason code, exactly one INFO line, no value returned, nothing cached. |
| Recorded fixtures, offline determinism | `tests/fixtures/ensemble/` — one real record + `.idx` + SHA-256-pinned manifest. No test touches the network. |

**Anti-circularity.** The manifest's expected node values were produced by this
repo's decoder, which alone would be a self-consistent golden. Three things
break that: (a) invariants the encoder did not choose — a physically plausible
global field range, the 1440 co-located north-pole nodes collapsing to one
value, and the spec's `min(packed) == 0`; (b)
`test_alignment_guard_is_not_vacuous`, a mutation test that removes the WMO
octet alignment and asserts the guard chain fires; (c) the out-of-band `geavg`
and CLI-truth comparisons in §3.

---

## 8. Reproduce

```bash
# Tests (targeted file only — never the full suite on this machine)
PYTHONPATH=. python -m pytest tests/test_ensemble_provider.py -q

# EC-1 artifact + validation probes (re-runs off cache in ~1 min)
PYTHONPATH=. python scripts/fetch_ensemble.py \
    --start-init 2026-07-20 --days 5 --validate --tmp-comparison

# Cold re-run (delete the cache first): ~9 min, ~0.5 GB (1 254 ranged requests)
rm -rf data/ensemble && PYTHONPATH=. python scripts/fetch_ensemble.py \
    --start-init 2026-07-20 --days 5 --validate --tmp-comparison
```

The script exits 0 only when the criterion is met; it exited 1 on the 19-member
run described in §3.

Keep `OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=NUMEXPR_NUM_THREADS=2`
and `--max-workers` ≤ 4 per the hardware constraint.

---

## 9. Dependencies

**None added.** `requirements.txt` was not touched. The provider uses only
`requests` and `numpy` (both already pinned) plus the standard library
(`zoneinfo`, `struct`, `statistics`, `concurrent.futures`, …). `pip install
eccodes` + `findlibs` were installed during evaluation, found unusable, and
**uninstalled**. Details and the rejection record:
`reports/phase2/ws_a_dependencies.txt`.

---

## 10. Interaction with sibling workstreams' `.gitignore` / `.gitattributes`

**No file outside this workstream's ownership was modified.** Two hazards
appeared from sibling edits to `.gitignore` and `.gitattributes`; both were
resolved inside this workstream rather than by asking for someone else's file to
change.

### (a) `.gitattributes` would have silently corrupted a binary fixture

A sibling added:

```
tests/fixtures/**                text eol=lf
```

An explicit `text` attribute **overrides git's binary auto-detection**. Measured
on the recorded GRIB2 record:

```
$ git check-attr text eol -- tests/fixtures/ensemble/…f030.TMAX.grib2
… text: set
… eol: lf
$ # the record contains 9 CRLF byte pairs and 1035 CR bytes in 431 982 bytes
```

Committing it as `text` would normalise those 9 CRLF pairs to LF, deleting 9
bytes from the middle of a compressed GRIB2 message — irreversibly, in the
repository. The SHA-256 gate in the test would catch it loudly, but the fixture
itself would be unrecoverable without re-downloading.

**Fix applied (inside this workstream):** the record is stored base64-encoded as
`gec00.t00z.pgrb2s.0p25.f030.TMAX.grib2.b64` (76-char lines, LF). Base64 is
CR-free, so `text eol=lf` is a no-op on it and the decoded bytes are exactly
what NOAA served. The test decodes and then hard-gates the SHA-256 of the
decoded record against the manifest. Cost: 583 KB of text for 432 KB of record.

**This is a live trap for anyone else adding a binary fixture** (recorded
`.parquet`, `.db`, images, GRIB). If the orchestrator prefers raw binaries, the
general rule needs a narrower exception *after* it, e.g.:

```
tests/fixtures/**                text eol=lf
tests/fixtures/**/*.grib2       -text
tests/fixtures/**/*.parquet     -text
```

### (b) `.gitignore` did not cover this cache

A sibling added `data/ensemble/cache/`, but the provider's original layout was
`data/ensemble/{records,forecasts}/` — 1 254 regenerable JSON files that would
have been committed.

**Fix applied (inside this workstream):** both cache layers were moved under
`data/ensemble/cache/` (`cache/records/`, `cache/forecasts/`, `cache/truth/`),
so the sibling's existing entry covers all of it and no `.gitignore` change is
needed. Verified: `git check-ignore` matches, and
`git status --porcelain data/ensemble` returns nothing.

---

## 11. Known gaps and what could not be verified

1. **Only 5 paired days of truth.** The biases in §3 are a sanity check, not a
   calibration. FR-2.2 needs ≥60 paired days, and the archive depth of
   `noaa-gefs-pds` `pgrb2sp25` was **not** probed — I confirmed 2026-07-19 …
   2026-07-26 exist and stopped there. If the bucket does not retain ~60 days of
   00Z cycles, Workstream B needs a different archive (or `--days 60` will fail
   partway and say so).
2. **Only the 00Z cycle and only day-ahead leads were exercised.** 06/12/18Z
   cycles and longer leads are supported by the code (`--cycle-hour`,
   `--target-offset-days`) but were not run. `MAX_LEAD_HOURS = 240` is the
   documented 3-hourly horizon; the 6-hourly f240–f384 tail is **not**
   implemented and will raise `ENSEMBLE_LEAD_OUT_OF_RANGE`.
3. **The interval over-coverage bias was not isolated.** It is measured and
   reported per forecast, but its contribution is entangled with the
   node-vs-station term in the §3 numbers. Isolating it needs the ≥60-day
   sample.
4. **The degraded path's live NWS calls were tested only against recorded
   doubles.** The `/points` → `/forecast` shape is the documented NWS API and is
   the same one `src/data/nws_provider.py` uses, but I did not exercise it
   end-to-end against `api.weather.gov` in this workstream.
5. **DST behaviour is tested but never observed live** — the EC-1 sample is all
   July. `test_spring_forward_day_is_23_hours` /
   `test_fall_back_day_is_25_hours` assert the window arithmetic; no forecast
   has actually been fetched across a transition.
6. **GRIB2 coverage is deliberately partial.** Grid template 3.0 and DRS
   templates 5.0/5.2/5.3, no bitmap, hour time units, PDTs 4.1/4.2/4.11/4.12.
   Anything else raises `ENSEMBLE_DECODE_FAILED` rather than guessing. If NCEP
   changes the packing, this breaks loudly — which is the intended failure mode.
7. **NODD throttles.** HTTP 503 under a 31-member burst is now retried, but a
   sustained outage will legitimately produce
   `ENSEMBLE_INSUFFICIENT_MEMBERS`. For the harvester, `max_workers=4` and the
   record cache keep the burst small; this has not been observed over a
   multi-day unattended run.
8. **ECMWF open data** (PRD A5's optional second source, open question 2) was
   not attempted. Given the LAX bias in §3, a second model is worth
   reconsidering at the Phase-2 go/no-go rather than being deferred by default.
