"""Fetch GEFS ensemble daily highs and emit the PRD Phase 2 EC-1 evidence.

PRD Phase 2 exit criterion 1, first half: *"Ensemble provider returns >=20
members for each of the 4 cities on >=5 consecutive days."* This script is the
re-runnable producer of that evidence -- ``reports/phase2/ec1_ensemble_members.json``
and its human-readable ``.md`` sibling are written by this script and by nothing
else, so the numbers in them cannot be hand-authored.

Every value it writes is measured. A cycle or city that fails is written into
the artifact as a failure with its reason code; nothing is interpolated,
extrapolated or filled in.

Usage
-----
Produce the EC-1 artifact (5 consecutive 00Z cycles, day-ahead targets)::

    PYTHONPATH=. python scripts/fetch_ensemble.py --start-init 2026-07-20 --days 5

Add the decoder cross-checks (``geavg`` vs the member mean, and the ensemble
mean vs CLI settlement truth)::

    PYTHONPATH=. python scripts/fetch_ensemble.py --start-init 2026-07-20 --days 5 --validate

Measure how much a 3-hourly *instantaneous* ``TMP`` daily max would have
under-estimated the interval ``TMAX`` daily max, on the last cycle::

    PYTHONPATH=. python scripts/fetch_ensemble.py --start-init 2026-07-20 --days 5 \
        --validate --tmp-comparison

Re-runs are served from ``data/ensemble/`` and cost no bandwidth.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.ensemble_provider import (  # noqa: E402
    CITIES,
    DEFAULT_MEMBERS,
    DEFAULT_MIN_MEMBERS,
    FIELD_TMAX,
    FIELD_TMP,
    GEFS_GRID,
    MEAN_PRODUCT,
    SOURCE_GEFS,
    EnsembleProvider,
    EnsembleUnavailable,
    city_nodes,
    get_city,
    kelvin_to_fahrenheit,
    local_day_bounds_utc,
    tmax_windows,
)

logger = logging.getLogger("fetch_ensemble")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(REPO_ROOT, "reports", "phase2")
#: Kept inside data/ensemble/cache/ so the one .gitignore entry covers it, and
#: kept out of data/weather_truth/ so this script cannot disturb the Phase 1
#: ground-truth tree it only reads from.
TRUTH_CACHE_DIR = os.path.join(REPO_ROOT, "data", "ensemble", "cache", "truth")
TRUTH_CSV_TEMPLATE = os.path.join(
    REPO_ROOT, "data", "weather_truth", "cli_daily_high_{station}.csv"
)


# ---------------------------------------------------------------------------
# Settlement truth (read-only use of the Phase 1 provider and its artifacts)
# ---------------------------------------------------------------------------
def _truth_from_csv(station: str, target: _date) -> Optional[Tuple[int, str]]:
    """The committed Phase-1 backfill row for one station-date, if present."""
    path = TRUTH_CSV_TEMPLATE.format(station=station)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            header = handle.readline().rstrip("\n").split(",")
            try:
                date_col = header.index("date")
                high_col = header.index("high")
            except ValueError:
                return None
            wanted = target.isoformat()
            for line in handle:
                parts = line.rstrip("\n").split(",")
                if len(parts) <= max(date_col, high_col):
                    continue
                if parts[date_col] != wanted:
                    continue
                if not parts[high_col].strip():
                    return None
                return int(float(parts[high_col])), os.path.relpath(path, REPO_ROOT)
    except FileNotFoundError:
        return None
    return None


def load_truth(
    station: str, target: _date, *, offline: bool
) -> Optional[Dict[str, Any]]:
    """CLI settlement truth for a station-date: committed backfill, then live IEM.

    Returns ``None`` when neither source has a published maximum -- an explicit
    "not available", never a substitute.
    """
    hit = _truth_from_csv(station, target)
    if hit is not None:
        return {"high_f": hit[0], "source": "phase1_backfill_csv", "detail": hit[1]}
    if offline:
        return None
    try:
        from src.data.iem_cli_provider import IEMCLIProvider
    except Exception as exc:  # pragma: no cover
        logger.warning("CLI truth provider unavailable: %s", exc)
        return None
    provider = IEMCLIProvider(cache_dir=TRUTH_CACHE_DIR)
    try:
        high = provider.fetch_daily_high(station, target)
    except Exception as exc:
        logger.warning("CLI truth lookup failed for %s %s: %s", station, target, exc)
        return None
    if high is None:
        return None
    return {
        "high_f": int(high),
        "source": "iem_cli_live",
        "detail": "src.data.iem_cli_provider.IEMCLIProvider.fetch_daily_high",
    }


# ---------------------------------------------------------------------------
# Validation probes
# ---------------------------------------------------------------------------
def validate_against_mean_product(
    provider: EnsembleProvider,
    init_time: datetime,
    target: _date,
    city_key: str,
) -> Dict[str, Any]:
    """Cross-check the decoder against NCEP's own ensemble-mean product.

    ``geavg`` is produced by NCEP from the same members. Decoding it with the
    same code and comparing to the mean of the 31 member values tests the
    decoder against an artifact this project did not produce. The daily maxima
    are not expected to match to the millidegree -- ``max`` and ``mean`` do not
    commute, so ``max_t(mean_m TMAX)`` <= ``mean_m(max_t TMAX)`` by Jensen --
    but a decoder error shows up as degrees, not hundredths.
    """
    spec = get_city(city_key)
    day_start, day_end = local_day_bounds_utc(target, spec.timezone)
    start_lead = int(round((day_start - init_time).total_seconds() / 3600.0))
    end_lead = int(round((day_end - init_time).total_seconds() / 3600.0))
    windows = tmax_windows(start_lead, end_lead)
    node = GEFS_GRID.nearest_node(spec.latitude, spec.longitude)
    kelvins: List[float] = []
    for window in windows:
        blob = provider.fetch_record_values(
            init_time, MEAN_PRODUCT, window.fhour, (node,), field_name=FIELD_TMAX
        )
        kelvins.append(float(blob["nodes_k"][f"{node[0]},{node[1]}"]))
    return {
        "product": MEAN_PRODUCT,
        "daily_high_f": round(kelvin_to_fahrenheit(max(kelvins)), 4),
        "forecast_hours": [w.fhour for w in windows],
    }


def instantaneous_tmp_daily_high(
    provider: EnsembleProvider,
    init_time: datetime,
    target: _date,
    city_key: str,
    members: Tuple[str, ...],
) -> Dict[str, Any]:
    """Daily high built from 3-hourly *instantaneous* ``TMP`` instead of ``TMAX``.

    This measures the bias the module docstring warns about: sampling a
    continuous diurnal curve every 3 h misses the peak. The number it produces
    is the reason this provider reads ``TMAX``.
    """
    spec = get_city(city_key)
    day_start, day_end = local_day_bounds_utc(target, spec.timezone)
    start_lead = int(round((day_start - init_time).total_seconds() / 3600.0))
    end_lead = int(round((day_end - init_time).total_seconds() / 3600.0))
    steps = [h for h in range(0, 241, 3) if start_lead <= h <= end_lead]
    node = GEFS_GRID.nearest_node(spec.latitude, spec.longitude)
    # Ask for every tracked city's node in the same request so one 430 KB
    # download serves all four cities instead of being repeated per city.
    shared = tuple(
        dict.fromkeys(
            GEFS_GRID.nearest_node(other.latitude, other.longitude)
            for other in CITIES.values()
        )
    )

    highs: Dict[str, float] = {}
    failures: Dict[str, str] = {}
    for member in members:
        kelvins: List[float] = []
        try:
            for fhour in steps:
                blob = provider.fetch_record_values(
                    init_time, member, fhour, shared, field_name=FIELD_TMP
                )
                kelvins.append(float(blob["nodes_k"][f"{node[0]},{node[1]}"]))
        except EnsembleUnavailable as exc:
            failures[member] = f"{exc.reason_code}: {exc.detail}"
            continue
        highs[member] = round(kelvin_to_fahrenheit(max(kelvins)), 4)
    return {
        "field": FIELD_TMP,
        "forecast_hours": steps,
        "members_decoded": len(highs),
        "members_failed": failures,
        "mean_f": round(statistics.fmean(highs.values()), 4) if highs else None,
        "members_f_by_name": highs,
    }


# ---------------------------------------------------------------------------
# The EC-1 run
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> Dict[str, Any]:
    provider = EnsembleProvider(
        members=DEFAULT_MEMBERS,
        min_members=args.min_members,
        max_workers=args.max_workers,
        offline=args.offline,
    )
    start_init = datetime.strptime(args.start_init, "%Y-%m-%d").replace(
        hour=args.cycle_hour, tzinfo=timezone.utc
    )
    init_times = [start_init + timedelta(days=k) for k in range(args.days)]

    runs: List[Dict[str, Any]] = []
    for init_time in init_times:
        target = (init_time + timedelta(days=args.target_offset_days)).date()
        for city_key in args.cities:
            record: Dict[str, Any] = {
                "init_time": init_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "target_date": target.isoformat(),
                "city": city_key,
                "station": CITIES[city_key].station,
            }
            try:
                forecast = provider.fetch(city_key, target, init_time)
            except EnsembleUnavailable as exc:
                record.update(
                    status="failed",
                    reason_code=exc.reason_code,
                    detail=exc.detail,
                    member_count=0,
                )
                logger.error("%s %s from %s: %s", city_key, target, init_time, exc)
                runs.append(record)
                continue

            members = list(forecast.members_f)
            bilinear = forecast.provenance.get("diagnostic_bilinear_f") or []
            record.update(
                status="ok",
                source=forecast.source,
                lead_hours=forecast.lead_hours,
                member_count=forecast.member_count,
                members_used=forecast.provenance.get("members_used"),
                members_failed=forecast.provenance.get("members_failed"),
                members_f=[round(v, 4) for v in members],
                mean_f=round(forecast.mean_f, 4),
                sigma_f=round(forecast.sigma_f, 4),
                min_f=round(min(members), 4),
                max_f=round(max(members), 4),
                median_f=round(forecast.quantile_f(0.5), 4),
                s3_keys=forecast.provenance.get("s3_keys"),
                byte_ranges=forecast.provenance.get("byte_ranges"),
                forecast_hours=forecast.provenance.get("forecast_hours"),
                grid_node=forecast.provenance.get("grid_node"),
                coverage=forecast.provenance.get("coverage"),
                fetched_at=forecast.provenance.get("fetched_at"),
                diagnostic_bilinear_mean_f=(
                    round(statistics.fmean(bilinear), 4) if bilinear else None
                ),
            )
            if args.validate:
                truth = load_truth(record["station"], target, offline=args.offline)
                record["cli_truth"] = truth
                if truth is not None:
                    record["error_mean_minus_truth_f"] = round(
                        forecast.mean_f - truth["high_f"], 4
                    )
                    if bilinear:
                        record["error_bilinear_minus_truth_f"] = round(
                            statistics.fmean(bilinear) - truth["high_f"], 4
                        )
                try:
                    record["geavg_check"] = validate_against_mean_product(
                        provider, init_time, target, city_key
                    )
                    record["geavg_check"]["member_mean_minus_geavg_f"] = round(
                        forecast.mean_f - record["geavg_check"]["daily_high_f"], 4
                    )
                except EnsembleUnavailable as exc:
                    record["geavg_check"] = {
                        "error": f"{exc.reason_code}: {exc.detail}"
                    }
            runs.append(record)
            logger.info(
                "%s %s from %sZ: %d members, mean %.2f F, sigma %.2f F",
                city_key,
                target,
                init_time.strftime("%Y%m%d%H"),
                forecast.member_count,
                forecast.mean_f,
                forecast.sigma_f,
            )

    ok = [r for r in runs if r.get("status") == "ok"]
    passing = [r for r in ok if r.get("member_count", 0) >= args.min_members]
    by_cycle: Dict[str, set] = {}
    for r in passing:
        by_cycle.setdefault(r["init_time"], set()).add(r["city"])
    wanted_cities = set(args.cities)
    dates_all_cities = sorted(
        cycle for cycle, cities in by_cycle.items() if cities >= wanted_cities
    )

    artifact: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/fetch_ensemble.py",
        "criterion": (
            "PRD Phase 2 exit criterion 1 (first half): the ensemble provider "
            "returns >=20 members for each of the 4 cities on >=5 consecutive "
            "days"
        ),
        "configuration": {
            "cities": list(args.cities),
            "start_init": args.start_init,
            "days": args.days,
            "cycle_hour": args.cycle_hour,
            "target_offset_days": args.target_offset_days,
            "min_members": args.min_members,
            "members_requested": list(DEFAULT_MEMBERS),
            "member_count_requested": len(DEFAULT_MEMBERS),
            "field": FIELD_TMAX,
            "source": SOURCE_GEFS,
        },
        "grid": {
            "ni": GEFS_GRID.ni,
            "nj": GEFS_GRID.nj,
            "dlat": GEFS_GRID.dlat,
            "dlon": GEFS_GRID.dlon,
            "lat1": GEFS_GRID.lat1,
            "lon1": GEFS_GRID.lon1,
            "scan_mode": GEFS_GRID.scan_mode,
        },
        "city_nodes": city_nodes(),
        "runs": runs,
        "summary": {
            "runs_attempted": len(runs),
            "runs_ok": len(ok),
            "runs_meeting_member_floor": len(passing),
            "min_member_count_over_ok_runs": (
                min(r["member_count"] for r in ok) if ok else None
            ),
            "consecutive_cycles_with_all_cities": len(dates_all_cities),
            "cycles_with_all_cities": dates_all_cities,
            "criterion_met": (
                len(dates_all_cities) >= 5 and len(passing) == len(runs) and bool(runs)
            ),
        },
    }

    if args.validate:
        errors = [
            r["error_mean_minus_truth_f"]
            for r in runs
            if r.get("error_mean_minus_truth_f") is not None
        ]
        bilinear_errors = [
            r["error_bilinear_minus_truth_f"]
            for r in runs
            if r.get("error_bilinear_minus_truth_f") is not None
        ]
        per_city: Dict[str, Any] = {}
        for city_key in args.cities:
            city_errors = [
                r["error_mean_minus_truth_f"]
                for r in runs
                if r["city"] == city_key
                and r.get("error_mean_minus_truth_f") is not None
            ]
            city_bilinear = [
                r["error_bilinear_minus_truth_f"]
                for r in runs
                if r["city"] == city_key
                and r.get("error_bilinear_minus_truth_f") is not None
            ]
            per_city[city_key] = {
                "paired_days": len(city_errors),
                "bias_nearest_node_f": (
                    round(statistics.fmean(city_errors), 4) if city_errors else None
                ),
                "bias_bilinear_f": (
                    round(statistics.fmean(city_bilinear), 4) if city_bilinear else None
                ),
                "mae_nearest_node_f": (
                    round(statistics.fmean([abs(e) for e in city_errors]), 4)
                    if city_errors
                    else None
                ),
            }
        artifact["validation"] = {
            "note": (
                "Paired against CLI settlement truth on a 5-day sample. This is "
                "an order-of-magnitude sanity check on the decoder and the "
                "windowing, NOT the FR-2.2 calibration, which requires >=60 "
                "paired days."
            ),
            "paired_days_total": len(errors),
            "bias_nearest_node_f": (
                round(statistics.fmean(errors), 4) if errors else None
            ),
            "bias_bilinear_f": (
                round(statistics.fmean(bilinear_errors), 4) if bilinear_errors else None
            ),
            "per_city": per_city,
        }

    if args.tmp_comparison and init_times:
        comparison: Dict[str, Any] = {
            "note": (
                "Daily high built from 3-hourly instantaneous TMP versus the "
                "interval TMAX this provider uses. A negative delta is the "
                "under-estimation the instantaneous field would have introduced."
            ),
            "init_time": init_times[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cities": {},
        }
        last_init = init_times[-1]
        last_target = (last_init + timedelta(days=args.target_offset_days)).date()
        for city_key in args.cities:
            tmax_run = next(
                (
                    r
                    for r in runs
                    if r["city"] == city_key
                    and r["init_time"] == comparison["init_time"]
                    and r.get("status") == "ok"
                ),
                None,
            )
            if tmax_run is None:
                comparison["cities"][city_key] = {"error": "no successful TMAX run"}
                continue
            # Compare like with like: the TMAX values are re-selected by member
            # name, so a member that failed on one field cannot silently pair
            # with a different member's value on the other.
            tmax_by_member = dict(
                zip(tmax_run["members_used"] or [], tmax_run["members_f"])
            )
            chosen = [
                m for m in DEFAULT_MEMBERS[: args.tmp_members] if m in tmax_by_member
            ]
            tmp_result = instantaneous_tmp_daily_high(
                provider, last_init, last_target, city_key, tuple(chosen)
            )
            paired = [m for m in chosen if m in tmp_result["members_f_by_name"]]
            tmp_result["members_compared"] = paired
            if paired:
                tmp_result["mean_f"] = round(
                    statistics.fmean(
                        tmp_result["members_f_by_name"][m] for m in paired
                    ),
                    4,
                )
                tmp_result["tmax_mean_f_same_members"] = round(
                    statistics.fmean(tmax_by_member[m] for m in paired), 4
                )
                tmp_result["tmp_minus_tmax_f"] = round(
                    tmp_result["mean_f"] - tmp_result["tmax_mean_f_same_members"], 4
                )
                tmp_result["per_member_tmp_minus_tmax_f"] = {
                    m: round(tmp_result["members_f_by_name"][m] - tmax_by_member[m], 4)
                    for m in paired
                }
            comparison["cities"][city_key] = tmp_result
        artifact["tmp_vs_tmax"] = comparison

    return artifact


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_markdown(artifact: Dict[str, Any]) -> str:
    cfg = artifact["configuration"]
    summary = artifact["summary"]
    lines: List[str] = []
    lines.append("# EC-1 evidence: GEFS ensemble members per city per cycle")
    lines.append("")
    lines.append(
        f"Generated {artifact['generated_at']} by `{artifact['generator']}`. "
        "Every number below was measured by that script; none is hand-entered."
    )
    lines.append("")
    lines.append(f"**Criterion.** {artifact['criterion']}")
    lines.append("")
    lines.append(
        f"**Verdict: {'MET' if summary['criterion_met'] else 'NOT MET'}** -- "
        f"{summary['runs_meeting_member_floor']}/{summary['runs_attempted']} "
        f"city-cycle runs cleared the {cfg['min_members']}-member floor across "
        f"{summary['consecutive_cycles_with_all_cities']} consecutive cycles; the "
        f"lowest member count on any successful run was "
        f"{summary['min_member_count_over_ok_runs']}."
    )
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append("| Setting | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Source | `{cfg['source']}` (NOAA NODD, anonymous HTTPS) |")
    lines.append(f"| Field | `{cfg['field']}:2 m above ground` (max over interval) |")
    lines.append(
        f"| Members requested | {cfg['member_count_requested']} "
        f"(`gec00` + `gep01`..`gep30`) |"
    )
    lines.append(f"| Member floor | {cfg['min_members']} |")
    lines.append(
        f"| Cycles | {cfg['days']} consecutive {cfg['cycle_hour']:02d}Z runs from {cfg['start_init']} |"
    )
    lines.append(
        f"| Target | init date + {cfg['target_offset_days']} day(s), local calendar day |"
    )
    lines.append(f"| Cities | {', '.join(cfg['cities'])} |")
    lines.append("")

    lines.append("## Grid nodes actually used")
    lines.append("")
    lines.append(
        "| City | Station | Station lat/lon | Nearest 0.25 deg node (j, i) | Node lat/lon | Distance |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for key, node in artifact["city_nodes"].items():
        lines.append(
            f"| {key} | {node['station']} | "
            f"{node['station_latitude']:.5f}, {node['station_longitude']:.5f} | "
            f"({node['node_j']}, {node['node_i']}) | "
            f"{node['node_latitude']:.2f}, {node['node_longitude']:.2f} | "
            f"{node['distance_km']:.1f} km |"
        )
    lines.append("")

    lines.append("## Per city-cycle results")
    lines.append("")
    lines.append(
        "| Cycle (UTC) | Target (local) | City | Members | Lead h | Mean F | Sigma F | Min F | Max F | S3 objects | Fetched |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in artifact["runs"]:
        if r.get("status") != "ok":
            lines.append(
                f"| {r['init_time']} | {r['target_date']} | {r['city']} | "
                f"**FAILED** | - | - | - | - | - | - | "
                f"`{r.get('reason_code')}` |"
            )
            continue
        lines.append(
            f"| {r['init_time']} | {r['target_date']} | {r['city']} | "
            f"{r['member_count']} | {r['lead_hours']} | {r['mean_f']:.2f} | "
            f"{r['sigma_f']:.2f} | {r['min_f']:.2f} | {r['max_f']:.2f} | "
            f"{len(r['s3_keys'])} | {r['fetched_at']} |"
        )
    lines.append("")

    lines.append("## Coverage windows (over-coverage is reported, not hidden)")
    lines.append("")
    lines.append(
        "| Cycle | City | Local day (UTC) | Requested leads | Covered leads | Spill before/after |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    seen = set()
    for r in artifact["runs"]:
        if r.get("status") != "ok":
            continue
        key = (r["init_time"], r["city"])
        if key in seen:
            continue
        seen.add(key)
        cov = r["coverage"]
        lines.append(
            f"| {r['init_time']} | {r['city']} | "
            f"{cov['local_day_start_utc']} .. {cov['local_day_end_utc']} | "
            f"{cov['requested_lead_hours'][0]}-{cov['requested_lead_hours'][1]} | "
            f"{cov['covered_lead_hours'][0]}-{cov['covered_lead_hours'][1]} | "
            f"{cov['over_coverage_hours'][0]} h / {cov['over_coverage_hours'][1]} h |"
        )
    lines.append("")

    validation = artifact.get("validation")
    if validation:
        lines.append("## Sanity check against CLI settlement truth")
        lines.append("")
        lines.append(validation["note"])
        lines.append("")
        lines.append(
            "| City | Paired days | Bias (nearest node) F | Bias (bilinear diagnostic) F | MAE F |"
        )
        lines.append("| --- | --- | --- | --- | --- |")

        def _signed(value: Any) -> str:
            return "n/a" if value is None else f"{value:+.2f}"

        def _plain(value: Any) -> str:
            return "n/a" if value is None else f"{value:.2f}"

        for key, stats in validation["per_city"].items():
            lines.append(
                f"| {key} | {stats['paired_days']} | "
                f"{_signed(stats['bias_nearest_node_f'])} | "
                f"{_signed(stats['bias_bilinear_f'])} | "
                f"{_plain(stats['mae_nearest_node_f'])} |"
            )
        lines.append("")

        geavg_rows = [
            r
            for r in artifact["runs"]
            if isinstance(r.get("geavg_check"), dict)
            and "member_mean_minus_geavg_f" in r["geavg_check"]
        ]
        if geavg_rows:
            deltas = [r["geavg_check"]["member_mean_minus_geavg_f"] for r in geavg_rows]
            lines.append(
                f"Cross-check against NCEP's own `geavg` product on "
                f"{len(geavg_rows)} city-cycles: member-mean minus geavg daily high "
                f"ranges {min(deltas):+.3f} F to {max(deltas):+.3f} F "
                f"(mean {statistics.fmean(deltas):+.3f} F). `max` and `mean` do not "
                f"commute, so an exact match is not expected. This agreement rules "
                f"out a fault in **member selection, the TMAX interval algebra or "
                f"the local-day windowing** -- any of those would move the member "
                f"mean away from `geavg` by degrees."
            )
            lines.append("")
            lines.append(
                "**It is not decoder-independent, and must not be read as such.** "
                "`geavg` is a GRIB2 record from the same bucket decoded by the "
                "*same* in-house decoder, so a global decode fault -- a Kelvin "
                "offset, a binary/decimal scale exponent, a sign, a hemisphere or "
                "scan-mode error -- shifts both sides identically and cancels "
                "exactly here. Independence is evidenced separately, against a "
                "different GRIB2 implementation, in "
                "`reports/phase2/ws_g_decoder_independence.md`."
            )
            lines.append("")

    comparison = artifact.get("tmp_vs_tmax")
    if comparison:
        lines.append("## Instantaneous `TMP` versus interval `TMAX`")
        lines.append("")
        lines.append(comparison["note"])
        lines.append("")
        lines.append(
            "| City | Members | TMP daily high (mean F) | TMAX daily high (mean F) | TMP - TMAX |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for key, stats in comparison["cities"].items():
            if "tmp_minus_tmax_f" not in stats:
                lines.append(
                    f"| {key} | - | - | - | {stats.get('error', 'no paired members')} |"
                )
                continue
            lines.append(
                f"| {key} | {len(stats['members_compared'])} | {stats['mean_f']:.2f} | "
                f"{stats['tmax_mean_f_same_members']:.2f} | "
                f"{stats['tmp_minus_tmax_f']:+.2f} |"
            )
        lines.append("")

    lines.append("## Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append(
        f"PYTHONPATH=. python scripts/fetch_ensemble.py "
        f"--start-init {cfg['start_init']} --days {cfg['days']} --validate"
        + (" --tmp-comparison" if comparison else "")
    )
    lines.append("```")
    lines.append("")
    lines.append(
        "Full machine-readable evidence, including every S3 key, byte range and "
        "per-member value: `reports/phase2/ec1_ensemble_members.json`."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start-init",
        required=True,
        help="first model cycle date, YYYY-MM-DD (UTC)",
    )
    parser.add_argument(
        "--days", type=int, default=5, help="number of consecutive cycles"
    )
    parser.add_argument(
        "--cycle-hour", type=int, default=0, help="model cycle hour UTC"
    )
    parser.add_argument(
        "--target-offset-days",
        type=int,
        default=1,
        help="target local calendar day relative to the cycle date",
    )
    parser.add_argument(
        "--cities",
        nargs="+",
        default=list(CITIES),
        choices=list(CITIES),
        help="cities to fetch",
    )
    parser.add_argument("--min-members", type=int, default=DEFAULT_MIN_MEMBERS)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--offline", action="store_true", help="cache only, no network")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="add the geavg decoder cross-check and the CLI-truth comparison",
    )
    parser.add_argument(
        "--tmp-comparison",
        action="store_true",
        help="measure the instantaneous-TMP daily high against the TMAX one",
    )
    parser.add_argument(
        "--tmp-members",
        type=int,
        default=len(DEFAULT_MEMBERS),
        help="members to use for the TMP-vs-TMAX comparison",
    )
    parser.add_argument(
        "--out-prefix",
        default=os.path.join(REPORT_DIR, "ec1_ensemble_members"),
        help="output path prefix; .json and .md are written",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    artifact = run(args)

    os.makedirs(os.path.dirname(args.out_prefix), exist_ok=True)
    json_path = f"{args.out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=1, sort_keys=True)
        handle.write("\n")
    md_path = f"{args.out_prefix}.md"
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(artifact))

    summary = artifact["summary"]
    logger.info(
        "wrote %s and %s -- criterion_met=%s (%d/%d runs cleared the floor)",
        os.path.relpath(json_path, REPO_ROOT),
        os.path.relpath(md_path, REPO_ROOT),
        summary["criterion_met"],
        summary["runs_meeting_member_floor"],
        summary["runs_attempted"],
    )
    return 0 if summary["criterion_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
