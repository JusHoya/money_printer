"""Backfill archived model guidance into the normalized forecast series (FR-1.6).

Writes ``data/forecast_archive/forecast_series_<SOURCE>.csv`` in the
source-agnostic schema :data:`~src.data.mos_guidance_provider.FORECAST_FIELDS`,
which is the only input (besides CLI truth) that
``scripts/build_calibration.py`` reads.

Usage::

    $env:PYTHONPATH = "."
    python scripts/backfill_forecasts.py --start 2025-12-20 --end 2026-07-24
    python scripts/backfill_forecasts.py --offline          # rebuild from cache only

Every run's raw response is cached under ``data/forecast_archive/cache/``, so a
re-run costs no network and reproduces the same CSV byte for byte. ``--offline``
makes that guarantee enforceable: it refuses to touch the network at all and
fails loudly on a cache miss instead of silently emitting a shorter series.

The CSV is written with ``\\n`` line endings and rows sorted by
``(city, target_date, init_time_utc)``. Both are load-bearing for the FR-2.2
determinism requirement; see ``src/calibration/forecast_calibration.py``.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.iem_cli_provider import STATIONS  # noqa: E402
from src.data.mos_guidance_provider import (  # noqa: E402
    FORECAST_ARCHIVE_DIR,
    FORECAST_FIELDS,
    MODEL_RUN_HOURS,
    SOURCE_GFS_MEX,
    GuidanceForecast,
    MOSGuidanceError,
    MOSGuidanceProvider,
    format_runtime,
    run_times,
)

logger = logging.getLogger("backfill_forecasts")

DEFAULT_STATIONS = tuple(STATIONS)  # KNYC, KMDW, KLAX, KMIA
#: Hard ceiling on HTTP concurrency. This machine machine-checks under sustained
#: all-core load; the archive is also a shared public service.
MAX_WORKERS_CAP = 4


def _write_csv(path: str, forecasts: Sequence[GuidanceForecast]) -> int:
    """Write the normalized series. ``newline=""`` + ``\\n`` == LF on Windows too."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(FORECAST_FIELDS), lineterminator="\n"
        )
        writer.writeheader()
        for f in forecasts:
            row = f.as_row()
            # spread_f is None for sources that publish no spread. csv writes
            # None as an empty field, which is exactly the intent: blank means
            # "not available" and must never become 0.0.
            writer.writerow(row)
    os.replace(tmp, path)
    return len(forecasts)


def _read_existing(path: str) -> Dict[tuple, GuidanceForecast]:
    """Load a previously written series so a backfill can extend it, not clobber it."""
    out: Dict[tuple, GuidanceForecast] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            spread = row.get("spread_f")
            f = GuidanceForecast(
                city=row["city"],
                station=row["station"],
                target_date=row["target_date"],
                init_time_utc=row["init_time_utc"],
                lead_hours=int(row["lead_hours"]),
                source=row["source"],
                forecast_high_f=float(row["forecast_high_f"]),
                spread_f=float(spread) if spread not in (None, "") else None,
                provenance=row.get("provenance", ""),
            )
            out[f.key] = f
    return out


def backfill(
    *,
    start: str,
    end: str,
    model: str,
    source: str,
    stations: Sequence[str],
    run_hours: Sequence[int],
    out_path: str,
    workers: int,
    offline: bool,
    merge: bool,
    request_pause: float,
) -> Dict[str, object]:
    runtimes = run_times(start, end, run_hours)
    workers = max(1, min(int(workers), MAX_WORKERS_CAP))

    local = threading.local()

    def provider() -> MOSGuidanceProvider:
        p = getattr(local, "p", None)
        if p is None:
            p = MOSGuidanceProvider(offline=offline, request_pause=request_pause)
            local.p = p
        return p

    collected: Dict[tuple, GuidanceForecast] = _read_existing(out_path) if merge else {}
    lock = threading.Lock()
    missing: List[str] = []
    failed: List[str] = []
    providers: List[MOSGuidanceProvider] = []

    def one(rt) -> None:
        p = provider()
        with lock:
            if p not in providers:
                providers.append(p)
        try:
            fcs = p.fetch_daily_highs(stations, model, rt, source=source)
        except MOSGuidanceError as exc:
            with lock:
                failed.append(f"{format_runtime(rt)}: {exc}")
            logger.error("run %s failed: %s", format_runtime(rt), exc)
            return
        if not fcs:
            with lock:
                missing.append(format_runtime(rt))
            return
        with lock:
            for f in fcs:
                collected[f.key] = f

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, runtimes))

    ordered = sorted(
        collected.values(), key=lambda f: (f.city, f.target_date, f.init_time_utc)
    )
    n = _write_csv(out_path, ordered)

    stats: Dict[str, int] = {}
    for p in providers:
        for k, v in p.stats.items():
            stats[k] = stats.get(k, 0) + v

    per_city: Dict[str, int] = {}
    per_city_days: Dict[str, set] = {}
    for f in ordered:
        per_city[f.city] = per_city.get(f.city, 0) + 1
        per_city_days.setdefault(f.city, set()).add(f.target_date)

    return {
        "out_path": out_path,
        "rows_written": n,
        "runs_requested": len(runtimes),
        "runs_missing": len(missing),
        "runs_failed": len(failed),
        "failed_examples": failed[:5],
        "provider_stats": stats,
        "rows_per_city": per_city,
        "distinct_target_dates_per_city": {
            c: len(d) for c, d in sorted(per_city_days.items())
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2025-12-20", help="first model run date (UTC)")
    ap.add_argument("--end", default="2026-07-24", help="last model run date (UTC)")
    ap.add_argument("--model", default="MEX", help="IEM MOS model id (MEX has n_x)")
    ap.add_argument("--source", default=SOURCE_GFS_MEX, help="source label in the CSV")
    ap.add_argument("--stations", nargs="*", default=list(DEFAULT_STATIONS))
    ap.add_argument(
        "--run-hours",
        nargs="*",
        type=int,
        default=None,
        help="UTC run hours; defaults to the model's known run grid",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--request-pause", type=float, default=0.4)
    ap.add_argument(
        "--offline",
        action="store_true",
        help="serve only from cache; fail on a miss instead of shrinking the series",
    )
    ap.add_argument(
        "--no-merge",
        action="store_true",
        help="rewrite the series from this window only, discarding prior rows",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    run_hours = args.run_hours
    if not run_hours:
        run_hours = MODEL_RUN_HOURS.get(str(args.model).upper(), (0, 12))

    out_path = args.out or os.path.join(
        FORECAST_ARCHIVE_DIR, f"forecast_series_{args.source}.csv"
    )

    result = backfill(
        start=args.start,
        end=args.end,
        model=args.model,
        source=args.source,
        stations=args.stations,
        run_hours=run_hours,
        out_path=out_path,
        workers=args.workers,
        offline=args.offline,
        merge=not args.no_merge,
        request_pause=args.request_pause,
    )

    print(f"wrote {result['rows_written']} rows -> {result['out_path']}")
    print(
        f"runs: requested={result['runs_requested']} "
        f"missing(404)={result['runs_missing']} failed={result['runs_failed']}"
    )
    if result["failed_examples"]:
        for line in result["failed_examples"]:
            print("  FAILED", line)
    print("provider stats:", result["provider_stats"])
    print("rows per city:", result["rows_per_city"])
    print("distinct target dates per city:", result["distinct_target_dates_per_city"])
    return 0 if result["runs_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
