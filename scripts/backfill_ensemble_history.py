#!/usr/bin/env python3
"""Backfill a GEFS forecast series and calibrate it (PRD FR-2.1 + FR-2.2).

WHY THIS EXISTS
---------------
FR-2.1 makes the **GEFS ensemble** the primary live forecast source. FR-2.2
requires calibration on >=60 paired forecast-vs-CLI days. Workstream B delivered
a 209-day calibration -- but for GFS extended MOS (``gfs_mex``), because it could
not backfill an ensemble. Left there, the system would price a GEFS forecast with
a MOS-derived sigma: **the source that drives live probabilities would have no
calibration of its own.** This script closes that gap by producing
``data/forecast_archive/forecast_series_gefs.csv`` in workstream B's normalized
schema and running it through workstream B's calibrator *unmodified*.

WHAT IT FETCHES, AND WHAT THAT COSTS
------------------------------------
Not the 31 members. At ~11 forecast hours x ~210 cycles that is ~72 000 ranged
requests and ~30 GB. It reads the two pre-computed NCEP products in the same
directory -- ``geavg`` (ensemble mean) and ``gespr`` (ensemble spread) -- which
is 22 ranged requests per cycle and also supplies ``spread_f``, a column
workstream B could not populate at all.

The statistic that buys is not the live one. See
:mod:`src.calibration.gefs_series`: the offset is measured, bounded, applied as
an explicit per-city correction, and recorded in every row and every calibration
file. It cannot change ``sigma_f``, only ``bias_f``.

VERIFIED UPSTREAM CONTRACT (probed live 2026-07-26)
---------------------------------------------------
======================================================  ======  ==================
Request                                                 Status  Result
======================================================  ======  ==================
``gefs.20260720/00/atmos/pgrb2sp25/gespr...f030.idx``   200     ``TMAX:2 m above
                                                                ground:24-30 hour
                                                                max fcst:ens std
                                                                dev`` present
``...geavg...f030.idx``                                 200     ``...:ens mean``
``geavg``/``gespr`` f030 ``.idx`` at 20250801,          200     all present --
20251227, 20251228, 20260101, 20260215, 20260401,               the archive
20260601                                                        spans the whole
                                                                CLI truth window
``gefs.../atmos/pgrb2ap5/geavg.t00z.pgrb2a.0p50.f030``  200     0.5 deg product
                                                                carries the same
                                                                field, record
                                                                ~3.1x smaller
======================================================  ======  ==================

**0.5 deg was measured and rejected**, not assumed away. ``--compare-resolution``
re-runs the measurement; the numbers are in ``reports/phase2/ws_f_report.md``.

RESUMABILITY, AND WHY A MISSING DAY IS LOUD
-------------------------------------------
Each cycle's assembled rows are written to
``data/ensemble/cache/series/<cycle>.json`` the moment they are built. A re-run
skips any cycle whose file is complete for the requested lead days, so an
interrupted 200-cycle backfill resumes where it stopped and a completed one
costs no network at all. ``--offline`` rebuilds the CSV from those files with no
network and **fails loudly on a gap** rather than silently emitting a shorter
series.

Every cycle that could not be completed is written into
``data/forecast_archive/gefs_manifest.json`` under ``missing``, with the HTTP
reason. A day that could not be fetched appears as missing; it is never simply
absent. That distinction is this project's oldest lesson --
``make-silent-rejections-observable``.

USAGE
-----
::

    $env:PYTHONPATH = "."
    python scripts/backfill_ensemble_history.py --start 2025-12-27 --end 2026-07-24
    python scripts/backfill_ensemble_history.py --offline --build-calibration
    python scripts/backfill_ensemble_history.py --measure-offset
    python scripts/backfill_ensemble_history.py --compare-resolution
    python scripts/backfill_ensemble_history.py --validate-spread

Keep ``OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=NUMEXPR_NUM_THREADS=2``
and ``--max-workers`` <= 4: this machine machine-checks (WHEA 0x124) under
sustained all-core load.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import statistics
import sys
import time
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.calibration import forecast_calibration as fc  # noqa: E402
from src.calibration.gefs_series import (  # noqa: E402
    FORECAST_FIELDS,
    GEAVG_TO_MEMBER_MEAN_OFFSET_F,
    GEFS_GRID_0P5,
    HALF_DEGREE_OBJECT,
    HALF_DEGREE_PREFIX,
    OFFSET_PROVENANCE,
    SOURCE_GEFS_SERIES,
    SPREAD_RULE,
    STATISTIC_GEAVG_MAX,
    STATISTIC_MEMBER_MEAN_MAX,
    GefsSeriesError,
    assemble_daily_high,
    calibration_annotation,
    cycle_plan,
    half_degree_nearest_node,
    read_cycle_records,
    windows_for,
)
from src.data.ensemble_provider import (  # noqa: E402
    CITIES,
    FIELD_TMAX,
    GEFS_BUCKET_URL,
    GEFS_GRID,
    LEVEL_2M,
    MEAN_PRODUCT,
    SPREAD_PRODUCT,
    EnsembleProvider,
    EnsembleUnavailable,
    decode_grib2_record,
    get_city,
    haversine_km,
    kelvin_to_fahrenheit,
    parse_idx,
    select_idx_record,
    spec_nearest_node,
)

logger = logging.getLogger("backfill_ensemble_history")

REPO_ROOT = _REPO_ROOT
ENSEMBLE_CACHE = os.path.join(REPO_ROOT, "data", "ensemble", "cache")
SERIES_CACHE_DIR = os.path.join(ENSEMBLE_CACHE, "series")
FORECAST_ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "forecast_archive")
SERIES_CSV = os.path.join(
    FORECAST_ARCHIVE_DIR, f"forecast_series_{SOURCE_GEFS_SERIES}.csv"
)
MANIFEST_PATH = os.path.join(
    FORECAST_ARCHIVE_DIR, f"{SOURCE_GEFS_SERIES}_manifest.json"
)
CALIBRATION_DIR = os.path.join(REPO_ROOT, "data", "calibration")
EC1_ARTIFACT = os.path.join(REPO_ROOT, "reports", "phase2", "ec1_ensemble_members.json")

#: Schema of a per-cycle resume file. Bumped if the row shape changes, so a
#: stale cache is rebuilt rather than silently mixed with a new one.
SERIES_CACHE_VERSION = 1

DEFAULT_LEAD_DAYS = (0, 1)
CITY_ORDER = ("NY", "CHI", "LAX", "MIA")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_date(text: str) -> _date:
    return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()


def _cycle_key(init: datetime) -> str:
    return init.strftime("%Y%m%d%H")


def _daterange(start: _date, end: _date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _all_nodes() -> Tuple[Tuple[int, int], ...]:
    """Every tracked city's nearest 0.25 deg node, deduplicated.

    Requested together so **one** ranged download serves all four cities. Doing
    it per city is what left workstream A's record cache holding only the last
    city's node -- a cache that re-downloads on every other city is not a cache.
    """
    return tuple(dict.fromkeys(spec_nearest_node(CITIES[c]) for c in CITY_ORDER))


def _series_cache_path(init: datetime) -> str:
    return os.path.join(SERIES_CACHE_DIR, f"{_cycle_key(init)}.json")


def _load_series_cache(
    init: datetime, lead_days: Sequence[int]
) -> Optional[Dict[str, Any]]:
    """A cycle's cached rows, or ``None`` if absent/stale/short of ``lead_days``."""
    path = _series_cache_path(init)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("series cache %s unreadable (%s); refetching", path, exc)
        return None
    if blob.get("cache_version") != SERIES_CACHE_VERSION:
        return None
    if not set(int(x) for x in lead_days).issubset(set(blob.get("lead_days") or [])):
        return None
    return blob


def _save_series_cache(init: datetime, blob: Mapping[str, Any]) -> None:
    os.makedirs(SERIES_CACHE_DIR, exist_ok=True)
    path = _series_cache_path(init)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, sort_keys=True, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# One cycle
# ---------------------------------------------------------------------------
def backfill_cycle(
    provider: EnsembleProvider,
    init: datetime,
    lead_days: Sequence[int],
    *,
    sleep_s: float = 0.0,
) -> Dict[str, Any]:
    """Fetch and assemble every (city, lead-day) row for one model cycle.

    Returns the per-cycle blob that is both the resume file and the manifest
    entry: the rows, the S3 keys and byte ranges actually used, and an explicit
    record of everything that failed. A partial cycle is returned partial and
    labelled -- never padded, never dropped whole.
    """
    plan, fhours = cycle_plan(init, lead_days, CITY_ORDER)
    nodes = _all_nodes()

    fetched: List[Dict[str, Any]] = []

    def on_record(
        product: str, fhour: int, blob: Optional[Mapping[str, Any]], err: Optional[str]
    ) -> None:
        if blob is None:
            fetched.append({"product": product, "fhour": fhour, "error": err})
            return
        meta = blob.get("_meta") or {}
        fetched.append(
            {
                "product": product,
                "fhour": fhour,
                "s3_key": meta.get("s3_key"),
                "range": meta.get("range"),
                "bytes": meta.get("bytes"),
                "from_cache": bool(blob.get("from_cache")),
            }
        )

    records, failures = read_cycle_records(
        provider, init, fhours, nodes, on_record=on_record
    )
    if sleep_s and any(not r.get("from_cache") for r in fetched):
        # Rate limiting is applied *between cycles*, not inside one. Inside a
        # cycle the burst is already bounded by the <=4-wide pool; a sleep there
        # would only stretch the same burst. This pause is what keeps a
        # 200-cycle unattended run polite to the bucket.
        time.sleep(sleep_s)

    rows: List[Dict[str, Any]] = []
    row_errors: Dict[str, str] = {}
    for (city, offset), windows in sorted(plan.items()):
        spec = get_city(city)
        target = init.astimezone(timezone.utc).date() + timedelta(days=offset)
        try:
            derived = assemble_daily_high(
                spec=spec,
                target_date=target,
                init_time=init,
                windows=windows,
                records=records,
            )
        except GefsSeriesError as exc:
            row_errors[f"{city}+{offset}"] = str(exc)
            continue
        row = derived.csv_row()
        row["_lead_day"] = offset
        row["_raw_high_f"] = derived.raw_high_f
        row["_argmax_fhour"] = derived.argmax_fhour
        rows.append(row)

    expected = len(plan)
    return {
        "cache_version": SERIES_CACHE_VERSION,
        "cycle": _cycle_key(init),
        "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lead_days": sorted(int(x) for x in lead_days),
        "forecast_hours": list(fhours),
        "rows": rows,
        "rows_expected": expected,
        "complete": len(rows) == expected and expected > 0,
        "record_failures": {
            f"{p}:f{h:03d}": v for (p, h), v in sorted(failures.items())
        },
        "row_errors": row_errors,
        "records": fetched,
    }


# ---------------------------------------------------------------------------
# The whole range
# ---------------------------------------------------------------------------
def backfill_range(
    *,
    start: _date,
    end: _date,
    lead_days: Sequence[int],
    cycle_hour: int = 0,
    offline: bool = False,
    max_workers: int = 4,
    sleep_s: float = 0.0,
    force: bool = False,
) -> Dict[str, Any]:
    """Backfill every 00Z cycle in ``[start, end]``, resuming from the cache."""
    provider = EnsembleProvider(
        cache_dir=os.path.join(REPO_ROOT, "data", "ensemble"),
        max_workers=max_workers,
        offline=offline,
    )
    if not offline:
        provider.connect()

    cycles: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    n_cached = n_fetched = 0
    bytes_total = 0
    requests_total = 0
    t0 = time.time()

    days = list(_daterange(start, end))
    for k, day in enumerate(days, start=1):
        init = datetime(day.year, day.month, day.day, cycle_hour, tzinfo=timezone.utc)
        blob = None if force else _load_series_cache(init, lead_days)
        if blob is not None:
            n_cached += 1
        else:
            try:
                blob = backfill_cycle(provider, init, lead_days, sleep_s=sleep_s)
            except EnsembleUnavailable as exc:
                missing.append(
                    {
                        "cycle": _cycle_key(init),
                        "reason_code": exc.reason_code,
                        "detail": exc.detail,
                        "rows_written": 0,
                    }
                )
                logger.warning("cycle %s FAILED: %s", _cycle_key(init), exc)
                continue
            n_fetched += 1
            _save_series_cache(init, blob)

        for rec in blob.get("records", []):
            requests_total += 1
            if rec.get("bytes") and not rec.get("from_cache"):
                bytes_total += int(rec["bytes"])
        cycles.append(blob)

        if not blob.get("complete"):
            missing.append(
                {
                    "cycle": blob["cycle"],
                    "rows_written": len(blob.get("rows") or []),
                    "rows_expected": blob.get("rows_expected"),
                    "record_failures": blob.get("record_failures"),
                    "row_errors": blob.get("row_errors"),
                }
            )

        if k % 10 == 0 or k == len(days):
            done = time.time() - t0
            logger.info(
                "cycle %d/%d (%s) rows=%d cached=%d fetched=%d %.0f MB %.0f s",
                k,
                len(days),
                blob["cycle"],
                sum(len(c.get("rows") or []) for c in cycles),
                n_cached,
                n_fetched,
                bytes_total / 1e6,
                done,
            )

    rows: List[Dict[str, Any]] = []
    for blob in cycles:
        rows.extend(blob.get("rows") or [])

    return {
        "rows": rows,
        "cycles": cycles,
        "missing": missing,
        "stats": {
            "cycles_requested": len(days),
            "cycles_present": len(cycles),
            "cycles_from_cache": n_cached,
            "cycles_fetched": n_fetched,
            "cycles_incomplete_or_failed": len(missing),
            "records_seen": requests_total,
            "bytes_downloaded": bytes_total,
            "wall_seconds": round(time.time() - t0, 1),
        },
    }


def write_series_csv(rows: Sequence[Mapping[str, Any]], path: str) -> int:
    """Write the normalized forecast series, LF, deterministically ordered.

    Written in binary with an explicit ``\\n`` so the file does not depend on
    Windows text-mode newline translation -- ``data/forecast_archive/*.csv`` is
    pinned ``eol=lf`` in ``.gitattributes`` and a CRLF write would fight it.
    """
    ordered = sorted(
        rows,
        key=lambda r: (str(r["target_date"]), str(r["city"]), int(r["lead_hours"])),
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(FORECAST_FIELDS), lineterminator="\n"
        )
        writer.writeheader()
        for row in ordered:
            writer.writerow({k: row[k] for k in FORECAST_FIELDS})
    os.replace(tmp, path)
    return len(ordered)


def write_manifest(
    result: Mapping[str, Any], path: str, *, argv: Sequence[str]
) -> None:
    """Provenance: every S3 key + byte range, every failure, every missing day.

    Kept per (cycle, product, forecast hour) rather than per row, because that
    is the granularity a request was actually made at. A reader can reconstruct
    which bytes produced which number by joining on the cycle.
    """
    per_cycle = []
    for blob in result["cycles"]:
        records = []
        for r in blob.get("records", []):
            entry = {
                "product": r.get("product"),
                "fhour": r.get("fhour"),
                "s3_key": r.get("s3_key"),
                "range": r.get("range"),
                "bytes": r.get("bytes"),
            }
            if r.get("error"):
                entry["error"] = r["error"]
            records.append(entry)
        per_cycle.append(
            {
                "cycle": blob["cycle"],
                "init_time_utc": blob["init_time_utc"],
                "complete": blob["complete"],
                "rows": len(blob.get("rows") or []),
                "rows_expected": blob.get("rows_expected"),
                "forecast_hours": blob.get("forecast_hours"),
                "record_failures": blob.get("record_failures") or {},
                "row_errors": blob.get("row_errors") or {},
                "records": records,
            }
        )
    blob = {
        "generator": "scripts/backfill_ensemble_history.py",
        "argv": list(argv),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket": GEFS_BUCKET_URL,
        "products": [MEAN_PRODUCT, SPREAD_PRODUCT],
        "field": FIELD_TMAX,
        "level": LEVEL_2M,
        "resolution": "0p25",
        "resolution_note": (
            "0.5 deg (pgrb2ap5) carries the same field in a ~3.1x smaller record "
            "and was measured and rejected: its nearest node is 24-31 km from "
            "three of the four settlement stations and the daily highs disagree "
            "by up to 5.2 F. See reports/phase2/ws_f_report.md."
        ),
        "statistic": STATISTIC_GEAVG_MAX,
        "live_statistic_for_contrast": STATISTIC_MEMBER_MEAN_MAX,
        "spread_rule": SPREAD_RULE,
        "geavg_to_member_mean_offset_f": dict(GEAVG_TO_MEMBER_MEAN_OFFSET_F),
        "offset_provenance": dict(OFFSET_PROVENANCE),
        "stats": result["stats"],
        "missing": result["missing"],
        "missing_note": (
            "A cycle that could not be fetched, or whose covering set was "
            "incomplete, appears here. It is never silently absent from the "
            "series."
        ),
        "cycles": per_cycle,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, sort_keys=True, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Offline rebuild
# ---------------------------------------------------------------------------
def rebuild_from_cache(
    start: _date, end: _date, lead_days: Sequence[int], cycle_hour: int = 0
) -> Dict[str, Any]:
    """Rebuild the series from the resume files with no network.

    A gap is an error, not a shorter CSV: silently emitting fewer days is how a
    calibration ends up measured on a sample nobody chose.
    """
    cycles: List[Dict[str, Any]] = []
    gaps: List[str] = []
    for day in _daterange(start, end):
        init = datetime(day.year, day.month, day.day, cycle_hour, tzinfo=timezone.utc)
        blob = _load_series_cache(init, lead_days)
        if blob is None:
            gaps.append(_cycle_key(init))
            continue
        cycles.append(blob)
    if gaps:
        raise SystemExit(
            f"offline rebuild: {len(gaps)} cycle(s) absent from "
            f"{SERIES_CACHE_DIR} (first: {gaps[:5]}). Refusing to emit a shorter "
            f"series; re-run online for those cycles."
        )
    rows: List[Dict[str, Any]] = []
    for blob in cycles:
        rows.extend(blob.get("rows") or [])
    missing = [
        {
            "cycle": b["cycle"],
            "rows_written": len(b.get("rows") or []),
            "rows_expected": b.get("rows_expected"),
            "record_failures": b.get("record_failures"),
            "row_errors": b.get("row_errors"),
        }
        for b in cycles
        if not b.get("complete")
    ]
    return {
        "rows": rows,
        "cycles": cycles,
        "missing": missing,
        "stats": {
            "cycles_requested": len(cycles),
            "cycles_present": len(cycles),
            "cycles_from_cache": len(cycles),
            "cycles_fetched": 0,
            "cycles_incomplete_or_failed": len(missing),
            "records_seen": sum(len(b.get("records") or []) for b in cycles),
            "bytes_downloaded": 0,
            "wall_seconds": 0.0,
            "offline": True,
        },
    }


# ---------------------------------------------------------------------------
# Evidence probes
# ---------------------------------------------------------------------------
def measure_offset() -> Dict[str, Any]:
    """Re-measure ``mean_m(max_t member) - max_t(mean_m)`` from the EC-1 artifact.

    Reads workstream A's committed 20 city-cycles at the full 31 members. This
    is the number :data:`GEAVG_TO_MEMBER_MEAN_OFFSET_F` holds; running this is
    how anyone checks that the constant was not typed in by hand.
    """
    with open(EC1_ARTIFACT, "r", encoding="utf-8") as fh:
        artifact = json.load(fh)
    per_city: Dict[str, List[Dict[str, Any]]] = {}
    for run in artifact["runs"]:
        check = run.get("geavg_check") or {}
        if "member_mean_minus_geavg_f" not in check:
            continue
        per_city.setdefault(run["city"], []).append(
            {
                "target_date": run["target_date"],
                "member_mean_f": run["mean_f"],
                "geavg_high_f": check["daily_high_f"],
                "offset_f": check["member_mean_minus_geavg_f"],
                "member_sigma_f": run.get("sigma_f"),
            }
        )
    out: Dict[str, Any] = {"per_city": {}, "source": EC1_ARTIFACT}
    every: List[float] = []
    for city in CITY_ORDER:
        rows = per_city.get(city, [])
        offsets = [r["offset_f"] for r in rows]
        every.extend(offsets)
        out["per_city"][city] = {
            "n": len(offsets),
            "mean_f": round(statistics.fmean(offsets), 4) if offsets else None,
            "sd_f": round(statistics.stdev(offsets), 4) if len(offsets) > 1 else None,
            "min_f": round(min(offsets), 4) if offsets else None,
            "max_f": round(max(offsets), 4) if offsets else None,
            "constant_in_use_f": GEAVG_TO_MEMBER_MEAN_OFFSET_F.get(city),
            "runs": rows,
        }
    out["pooled"] = {
        "n": len(every),
        "mean_f": round(statistics.fmean(every), 4) if every else None,
        "sd_f": round(statistics.stdev(every), 4) if len(every) > 1 else None,
        "min_f": round(min(every), 4) if every else None,
        "max_f": round(max(every), 4) if every else None,
    }
    return out


def compare_resolution(
    provider: EnsembleProvider, start: _date, days: int, lead_day: int = 1
) -> Dict[str, Any]:
    """0.25 deg vs 0.5 deg daily highs on the same cycles, same windowing.

    Uses the same decoder, the same ``TMAX`` interval algebra and the same
    nearest-node rule -- only the grid changes -- so the delta measures the
    product, not two different pipelines.
    """
    session = provider.session
    rows: List[Dict[str, Any]] = []
    nodes50 = tuple(
        dict.fromkeys(half_degree_nearest_node(CITIES[c]) for c in CITY_ORDER)
    )
    total_bytes = 0

    for k in range(days):
        day = start + timedelta(days=k)
        init = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        target = day + timedelta(days=lead_day)
        per_city_windows = {c: windows_for(CITIES[c], target, init) for c in CITY_ORDER}
        fhours = sorted({w.fhour for ws in per_city_windows.values() for w in ws})

        values50: Dict[int, Dict[Tuple[int, int], float]] = {}
        hh = f"{init.hour:02d}"
        for fhour in fhours:
            key = HALF_DEGREE_PREFIX.format(
                yyyymmdd=init.strftime("%Y%m%d"), hh=hh
            ) + HALF_DEGREE_OBJECT.format(member=MEAN_PRODUCT, hh=hh, fhour=fhour)
            url = f"{GEFS_BUCKET_URL}/{key}"
            idx = session.get(url + ".idx", timeout=60)
            idx.raise_for_status()
            entry = select_idx_record(parse_idx(idx.text), FIELD_TMAX, LEVEL_2M)
            resp = session.get(url, headers={"Range": entry.range_header}, timeout=60)
            if resp.status_code not in (200, 206):
                raise SystemExit(f"{url} range read returned HTTP {resp.status_code}")
            total_bytes += len(resp.content)
            field = decode_grib2_record(resp.content)
            if (field.grid.ni, field.grid.nj) != (GEFS_GRID_0P5.ni, GEFS_GRID_0P5.nj):
                raise SystemExit(
                    f"{key} declares a {field.grid.ni}x{field.grid.nj} grid, "
                    f"expected {GEFS_GRID_0P5.ni}x{GEFS_GRID_0P5.nj}"
                )
            values50[fhour] = {n: field.at(field.grid.node_index(*n)) for n in nodes50}

        records25, _ = read_cycle_records(
            provider, init, fhours, _all_nodes(), products=(MEAN_PRODUCT,)
        )
        for city in CITY_ORDER:
            spec = CITIES[city]
            windows = per_city_windows[city]
            n25 = spec_nearest_node(spec)
            hi25 = kelvin_to_fahrenheit(
                max(
                    float(
                        records25[(MEAN_PRODUCT, w.fhour)]["nodes_k"][
                            f"{n25[0]},{n25[1]}"
                        ]
                    )
                    for w in windows
                )
            )
            n50 = half_degree_nearest_node(spec)
            hi50 = kelvin_to_fahrenheit(max(values50[w.fhour][n50] for w in windows))
            lat25, lon25 = GEFS_GRID.node_lat_lon(*n25)
            lat50, lon50 = GEFS_GRID_0P5.node_lat_lon(*n50)
            rows.append(
                {
                    "city": city,
                    "target_date": target.isoformat(),
                    "high_0p25_f": round(hi25, 4),
                    "high_0p50_f": round(hi50, 4),
                    "delta_f": round(hi50 - hi25, 4),
                    "node_km_0p25": round(
                        haversine_km(spec.latitude, spec.longitude, lat25, lon25), 1
                    ),
                    "node_km_0p50": round(
                        haversine_km(spec.latitude, spec.longitude, lat50, lon50), 1
                    ),
                }
            )

    summary: Dict[str, Any] = {}
    for city in CITY_ORDER:
        deltas = [r["delta_f"] for r in rows if r["city"] == city]
        summary[city] = {
            "n": len(deltas),
            "mean_delta_f": round(statistics.fmean(deltas), 4),
            "sd_delta_f": round(statistics.stdev(deltas), 4)
            if len(deltas) > 1
            else None,
            "min_delta_f": min(deltas),
            "max_delta_f": max(deltas),
        }
    return {
        "rows": rows,
        "per_city": summary,
        "bytes_0p50": total_bytes,
        "verdict_rule": (
            "adopt 0.5 deg only if the daily highs agree closely on overlapping "
            "days; otherwise keep 0.25 deg"
        ),
    }


def validate_spread(provider: EnsembleProvider, lead_day: int = 1) -> Dict[str, Any]:
    """Is ``gespr`` at the argmax interval a usable stand-in for member sigma?

    Compares the emitted ``spread_f`` against the **true** sample sd of the 31
    members' daily maxima, taken from workstream A's committed EC-1 artifact.
    They are different quantities by construction (see
    :data:`~src.calibration.gefs_series.SPREAD_RULE`); this measures how
    different, so FR-2.3 can decide whether to use the per-day spread at all.
    """
    with open(EC1_ARTIFACT, "r", encoding="utf-8") as fh:
        artifact = json.load(fh)
    truth = {(r["city"], r["target_date"]): r for r in artifact["runs"]}

    rows: List[Dict[str, Any]] = []
    inits = sorted({r["init_time"] for r in artifact["runs"]})
    for init_text in inits:
        init = datetime.strptime(init_text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        plan, fhours = cycle_plan(init, (lead_day,), CITY_ORDER)
        records, failures = read_cycle_records(provider, init, fhours, _all_nodes())
        if failures:
            logger.warning(
                "cycle %s: %d record failures", _cycle_key(init), len(failures)
            )
        for (city, offset), windows in sorted(plan.items()):
            target = init.date() + timedelta(days=offset)
            key = (city, target.isoformat())
            if key not in truth:
                continue
            derived = assemble_daily_high(
                spec=get_city(city),
                target_date=target,
                init_time=init,
                windows=windows,
                records=records,
            )
            member_sigma = truth[key].get("sigma_f")
            rows.append(
                {
                    "city": city,
                    "target_date": target.isoformat(),
                    "gespr_at_argmax_f": derived.spread_f,
                    "member_daily_high_sigma_f": member_sigma,
                    "ratio": (
                        round(derived.spread_f / member_sigma, 4)
                        if derived.spread_f and member_sigma
                        else None
                    ),
                    "argmax_fhour": derived.argmax_fhour,
                }
            )
    per_city: Dict[str, Any] = {}
    for city in CITY_ORDER:
        rs = [r for r in rows if r["city"] == city and r["ratio"] is not None]
        if not rs:
            continue
        per_city[city] = {
            "n": len(rs),
            "mean_gespr_f": round(
                statistics.fmean(r["gespr_at_argmax_f"] for r in rs), 4
            ),
            "mean_member_sigma_f": round(
                statistics.fmean(r["member_daily_high_sigma_f"] for r in rs), 4
            ),
            "mean_ratio": round(statistics.fmean(r["ratio"] for r in rs), 4),
        }
    return {"rows": rows, "per_city": per_city, "rule": SPREAD_RULE}


# ---------------------------------------------------------------------------
# Calibration (workstream B's code, unmodified)
# ---------------------------------------------------------------------------
def stamp_statistic(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Ensure a calibration payload carries its ``statistic`` block, and re-seal.

    The block itself now lives in :func:`gefs_series.statistic_block` and is
    registered against ``source="gefs"`` in
    :data:`forecast_calibration.SOURCE_ANNOTATORS`, so ``fc.build_all()`` already
    applies it. **That relocation is the fix for a real defect**: while the
    stamping lived here, the documented generic rebuild
    (``scripts/build_calibration.py --source gefs``) produced the same numbers
    with the block *stripped* -- a 10 402-byte file against this path's 11 932,
    a different ``content_hash``, and no statistic warning. Two producers, two
    artifacts, one of them silently missing the guard that stops a reader
    confusing ``max_t(geavg)`` with the live ``mean_m(max_t member)``.

    This function is retained as the explicit, **idempotent** way to seal a
    payload that was built by some other route. Applying it to an
    already-annotated payload rewrites the same block and re-computes the same
    hash, so it is safe to call twice and byte-identical either way.

    The block contains no timestamp, hostname, path or run id, so the artifact
    stays byte-identical on re-run -- the property EC-2 gates on.
    """
    out = {k: v for k, v in payload.items() if k != "content_hash"}
    out.update(calibration_annotation(out))
    return fc.finalize(out)


def build_calibration(
    *,
    csv_path: str,
    version: int,
    out_dir: str,
    check_deterministic: bool = True,
    extra_runlog: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run workstream B's ``build_all()`` on the GEFS series and write artifacts.

    ``forecast_calibration`` is imported and called, never copied and never
    edited: the whole point of this workstream is that the *same* calibrator
    ingests a second source unchanged. The ``statistic`` block is applied *by*
    that calibrator, through its ``SOURCE_ANNOTATORS`` registry, so this script
    and ``scripts/build_calibration.py --source gefs`` produce byte-identical
    artifacts. This function only adds runlog metadata on top.
    """
    from src.data.iem_cli_provider import CORE_STATIONS

    results = fc.build_all(
        forecast_csv=csv_path,
        stations=CORE_STATIONS,
        source=SOURCE_GEFS_SERIES,
        version=version,
    )
    for r in results:
        if "statistic" not in r.payload:
            raise SystemExit(
                f"{r.city}: build_all() did not apply the gefs statistic "
                f"annotation. Refusing to write an artifact that cannot be "
                f"distinguished from one built on the live member-mean "
                f"statistic; check forecast_calibration.SOURCE_ANNOTATORS."
            )

    determinism: Dict[str, Any] = {}
    if check_deterministic:
        again = fc.build_all(
            forecast_csv=csv_path,
            stations=CORE_STATIONS,
            source=SOURCE_GEFS_SERIES,
            version=version,
        )
        by_city = {r.city: r.payload for r in again}
        for r in results:
            a = fc.canonical_bytes(r.payload)
            b = fc.canonical_bytes(by_city[r.city])
            determinism[r.city] = {
                "identical": a == b,
                "bytes": len(a),
                "content_hash": r.payload.get("content_hash"),
            }

    written: List[str] = []
    for r in results:
        path = fc.write_calibration(out_dir, r.payload)
        written.append(path)
        fc.write_runlog(
            out_dir,
            r.payload,
            {
                "workstream": "F",
                "producer": "scripts/backfill_ensemble_history.py",
                "forecast_series_csv": os.path.relpath(csv_path, REPO_ROOT).replace(
                    "\\", "/"
                ),
                "manifest": os.path.relpath(MANIFEST_PATH, REPO_ROOT).replace(
                    "\\", "/"
                ),
                "statistic": STATISTIC_GEAVG_MAX,
                "live_statistic_for_contrast": STATISTIC_MEMBER_MEAN_MAX,
                "statistic_warning": (
                    "This calibration was built on the daily maximum of the GEFS "
                    "ensemble MEAN field plus a measured per-city offset, NOT on "
                    "the mean of the per-member daily maxima that "
                    "EnsembleProvider.fetch() returns live. The offset is a "
                    "constant per city: it shifts bias_f by exactly its own value "
                    "and leaves sigma_f unchanged."
                ),
                "geavg_to_member_mean_offset_f": GEAVG_TO_MEMBER_MEAN_OFFSET_F.get(
                    r.city
                ),
                "offset_provenance": dict(OFFSET_PROVENANCE),
                "spread_rule": SPREAD_RULE,
                "resolution": "0p25 (pgrb2sp25); 0.5 deg measured and rejected",
                "determinism_check": determinism.get(r.city),
                **dict(extra_runlog or {}),
            },
        )

    return {
        "results": results,
        "written": written,
        "determinism": determinism,
        "sigma_table": fc.day_of_sigma_table(results),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _lead_days(text: str) -> Tuple[int, ...]:
    return tuple(sorted({int(x) for x in str(text).split(",") if str(x).strip() != ""}))


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--start", type=_parse_date, help="first 00Z cycle date (YYYY-MM-DD)"
    )
    ap.add_argument("--end", type=_parse_date, help="last 00Z cycle date (YYYY-MM-DD)")
    ap.add_argument(
        "--lead-days",
        type=_lead_days,
        default=DEFAULT_LEAD_DAYS,
        help="target-date offsets from the cycle date (default 0,1)",
    )
    ap.add_argument("--cycle-hour", type=int, default=0)
    ap.add_argument("--max-workers", type=int, default=4, help="HTTP concurrency (<=4)")
    ap.add_argument(
        "--sleep", type=float, default=0.0, help="seconds between live records"
    )
    ap.add_argument(
        "--offline", action="store_true", help="rebuild from the resume cache only"
    )
    ap.add_argument("--force", action="store_true", help="ignore the resume cache")
    ap.add_argument("--no-csv", action="store_true", help="do not write the series CSV")
    ap.add_argument("--build-calibration", action="store_true")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--measure-offset", action="store_true")
    ap.add_argument("--compare-resolution", action="store_true")
    ap.add_argument("--resolution-days", type=int, default=5)
    ap.add_argument("--validate-spread", action="store_true")
    ap.add_argument("--json-out", help="write the probe/summary blob here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.max_workers > 4:
        ap.error("--max-workers must be <= 4 (hardware constraint)")

    probe_out: Dict[str, Any] = {}

    if args.measure_offset:
        probe_out["offset"] = measure_offset()
        print(json.dumps(probe_out["offset"], indent=1, sort_keys=True))

    if args.compare_resolution:
        provider = EnsembleProvider(
            cache_dir=os.path.join(REPO_ROOT, "data", "ensemble"),
            max_workers=args.max_workers,
        )
        start = args.start or _date(2026, 7, 20)
        probe_out["resolution"] = compare_resolution(
            provider, start, args.resolution_days
        )
        print(json.dumps(probe_out["resolution"]["per_city"], indent=1, sort_keys=True))

    if args.validate_spread:
        provider = EnsembleProvider(
            cache_dir=os.path.join(REPO_ROOT, "data", "ensemble"),
            max_workers=args.max_workers,
        )
        probe_out["spread"] = validate_spread(provider)
        print(json.dumps(probe_out["spread"]["per_city"], indent=1, sort_keys=True))

    did_backfill = False
    if (
        args.start
        and args.end
        and not (args.measure_offset or args.compare_resolution or args.validate_spread)
    ):
        if args.offline:
            result = rebuild_from_cache(
                args.start, args.end, args.lead_days, args.cycle_hour
            )
        else:
            result = backfill_range(
                start=args.start,
                end=args.end,
                lead_days=args.lead_days,
                cycle_hour=args.cycle_hour,
                max_workers=args.max_workers,
                sleep_s=args.sleep,
                force=args.force,
            )
        did_backfill = True
        logger.info("stats: %s", json.dumps(result["stats"], sort_keys=True))
        if result["missing"]:
            logger.warning(
                "%d cycle(s) incomplete or missing -- recorded in the manifest",
                len(result["missing"]),
            )
        if not args.no_csv:
            n = write_series_csv(result["rows"], SERIES_CSV)
            write_manifest(result, MANIFEST_PATH, argv=argv)
            logger.info("wrote %d rows -> %s", n, SERIES_CSV)
            logger.info("wrote manifest -> %s", MANIFEST_PATH)
        probe_out["backfill"] = result["stats"]

    if args.build_calibration:
        out = build_calibration(
            csv_path=SERIES_CSV,
            version=args.version,
            out_dir=CALIBRATION_DIR,
            extra_runlog={"backfill_stats": probe_out.get("backfill")},
        )
        print("\nEC-3 day-of sigma table (source=%s):" % SOURCE_GEFS_SERIES)
        print(
            f"{'city':5} {'station':8} {'n':>5} {'bias_f':>9} {'sigma_f':>9} {'mae_f':>8}  verdict"
        )
        for row in out["sigma_table"]:
            print(
                f"{row['city']:5} {row['station']:8} {row['n']:5d} "
                f"{row['bias_f']!s:>9} {row['sigma_f']!s:>9} {row['mae_f']!s:>8}  {row['verdict']}"
            )
        print("\ndeterminism (build twice, byte-compare canonical serialization):")
        for city, block in sorted(out["determinism"].items()):
            print(f"  {city:5} identical={block['identical']} bytes={block['bytes']}")
        probe_out["calibration"] = {
            "sigma_table": out["sigma_table"],
            "determinism": out["determinism"],
            "written": [
                os.path.relpath(p, REPO_ROOT).replace("\\", "/") for p in out["written"]
            ],
        }

    if args.json_out and probe_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(probe_out, fh, indent=1, sort_keys=True, default=str)
            fh.write("\n")

    if not (probe_out or did_backfill):
        ap.error("nothing to do: pass --start/--end or one of the probe flags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
