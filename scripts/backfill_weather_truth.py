#!/usr/bin/env python3
"""backfill_weather_truth.py -- persist CLI daily-high ground truth (PRD FR-1.6).

Phase 1 exit criterion 4: ">=180 days of CLI daily highs per city persisted;
row counts and 5 spot-checked values match the IEM archive."

WHAT IT WRITES
--------------
Everything lands under ``data/weather_truth/`` in a greppable, diffable,
stable-column-order form::

    cli_daily_high_KNYC.csv     one file per settlement station
    cli_daily_high_KMDW.csv
    cli_daily_high_KLAX.csv
    cli_daily_high_KMIA.csv
    cli_daily_high_all.csv      the same rows, all stations, station-then-date
    coverage_report.json        per-city counts, span, and the explicit gap list
    cache/iem_cli_<STN>_<YR>.json   raw provider cache (station-year keyed)

Column order is fixed by ``iem_cli_provider.ROW_FIELDS``::

    station,date,high,low,high_time,source,source_url,product,fetched_at

``source``/``source_url``/``fetched_at`` are the per-row provenance required by
FR-1.6 -- every stored temperature names the authority that produced it and
when it was read.

IDEMPOTENCE
-----------
Existing CSVs are read first and merged by ``(station, date)``. Re-running
fills gaps and refreshes rows whose ``high`` was previously missing; it never
duplicates a row and never rewrites a row that already carries a value from the
same source. Output is sorted, so a re-run on unchanged data produces a
byte-identical file and an empty ``git diff``.

GAPS ARE RECORDED, NEVER INTERPOLATED
-------------------------------------
The archive genuinely lacks products for some dates (station outages, late
issuance, the current day before the next-morning product). Those dates are
listed in ``coverage_report.json`` under ``gap_dates`` and simply absent from
the CSV. Nothing is interpolated or carried forward -- a fabricated settlement
temperature is exactly the class of self-deception this pivot exists to remove
(project rule ``abort-on-missing-critical-input``).

WHY THE NWS FALLBACK IS OFF FOR BULK BACKFILL
---------------------------------------------
``api.weather.gov/products/types/CLI/locations/<loc>`` serves only a short
rolling window -- 14-15 products per site when probed on 2026-07-25, i.e. about
a week, since two products are issued per site per day. It therefore cannot
fill historical gaps and would just burn requests. It is enabled per-date by
``scripts/reconcile_weather.py`` (the daily job), where the window always
covers the date in question. ``--fill-gaps-nws`` turns it on here for the tail
end of the range if you want it.

USAGE
-----
    python scripts/backfill_weather_truth.py                    # 210 days, 4 cities
    python scripts/backfill_weather_truth.py --days 400
    python scripts/backfill_weather_truth.py --stations KNYC,KMDW
    python scripts/backfill_weather_truth.py --force-refresh    # bypass the cache
    python scripts/backfill_weather_truth.py --json             # machine-readable

Exit codes:
    0  every station met the required coverage
    1  a station fell short of --min-days of stored rows
    2  usage / fatal error
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

# Allow `python scripts/backfill_weather_truth.py` without PYTHONPATH=.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.data.iem_cli_provider import (  # noqa: E402
    ROW_FIELDS,
    STATIONS,
    WEATHER_TRUTH_DIR,
    IEMCLIError,
    IEMCLIProvider,
    date_range,
    get_station,
    normalize_date,
)

logger = logging.getLogger("backfill_weather_truth")

DEFAULT_DAYS = 210  # PRD requires >=180; the margin absorbs archive gaps
DEFAULT_MIN_DAYS = 180
COMBINED_BASENAME = "cli_daily_high_all.csv"
COVERAGE_BASENAME = "coverage_report.json"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# CSV persistence
# ---------------------------------------------------------------------------
def station_csv_path(out_dir: str, station: str) -> str:
    return os.path.join(out_dir, f"cli_daily_high_{station.upper()}.csv")


def read_csv_rows(path: str) -> List[Dict[str, object]]:
    """Load an existing truth CSV, coercing numerics back from text."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.error("could not read %s (%s); treating it as empty", path, exc)
        return []

    out: List[Dict[str, object]] = []
    for row in rows:
        clean: Dict[str, object] = {}
        for field in ROW_FIELDS:
            value = row.get(field)
            if value in (None, ""):
                clean[field] = None
            elif field in ("high", "low"):
                try:
                    clean[field] = int(float(value))
                except (TypeError, ValueError):
                    clean[field] = None
            else:
                clean[field] = value
        if clean.get("date") and clean.get("station"):
            out.append(clean)
    return out


def write_csv_rows(path: str, rows: Iterable[Dict[str, object]]) -> int:
    """Write rows in the fixed :data:`ROW_FIELDS` order. Returns the row count."""
    ordered = sorted(rows, key=lambda r: (str(r.get("station")), str(r.get("date"))))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ROW_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow({field: row.get(field) for field in ROW_FIELDS})
    os.replace(tmp, path)
    return len(ordered)


def merge_rows(
    existing: Iterable[Dict[str, object]], fetched: Iterable[Dict[str, object]]
) -> List[Dict[str, object]]:
    """Idempotent merge keyed by ``(station, date)``.

    A fetched row replaces the stored one only when it actually adds
    information -- i.e. the stored row has no ``high`` and the fetched one
    does. Otherwise the stored row (and its original ``fetched_at``
    provenance) is preserved, so re-running does not churn the file.
    """
    merged: Dict[tuple, Dict[str, object]] = {}
    for row in existing:
        merged[(str(row.get("station")), str(row.get("date")))] = dict(row)

    for row in fetched:
        key = (str(row.get("station")), str(row.get("date")))
        prior = merged.get(key)
        if prior is None:
            merged[key] = dict(row)
            continue
        if prior.get("high") is None and row.get("high") is not None:
            merged[key] = dict(row)
    return list(merged.values())


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------
def backfill_station(
    provider: IEMCLIProvider,
    station: str,
    start_date: str,
    end_date: str,
    *,
    out_dir: str,
    force_refresh: bool = False,
    fill_gaps_nws: bool = False,
    max_nws_fills: int = 10,
) -> Dict[str, object]:
    """Backfill one station and return its coverage summary."""
    station = station.upper()
    spec = get_station(station)
    path = station_csv_path(out_dir, station)

    try:
        fetched = provider.fetch_range(
            station, start_date, end_date, force_refresh=force_refresh
        )
    except IEMCLIError as exc:
        logger.error("backfill aborted for %s: %s", station, exc)
        return {
            "station": station,
            "series_ticker": spec.series_ticker,
            "city": spec.city,
            "error": str(exc),
            "rows_stored": 0,
            "gap_dates": [],
        }

    # Rows without a maximum carry no settlement truth: keep them out of the
    # CSV and surface them as gaps instead.
    usable = [r for r in fetched if r.get("high") is not None]
    null_high_dates = sorted(r["date"] for r in fetched if r.get("high") is None)

    existing = read_csv_rows(path)
    merged = merge_rows(existing, usable)

    in_window = {
        str(r["date"]) for r in merged if start_date <= str(r["date"]) <= end_date
    }
    expected = date_range(start_date, end_date)
    gap_dates = [d for d in expected if d not in in_window]

    # Optional NWS gap-fill for the tail of the range (see module docstring:
    # the NWS product window is only ~1 week deep).
    nws_filled: List[str] = []
    if fill_gaps_nws and gap_dates:
        for gap in list(reversed(gap_dates))[:max_nws_fills]:
            row = provider.fetch_daily_high_nws(station, gap)
            if row is not None:
                merged = merge_rows(merged, [row])
                nws_filled.append(gap)
        if nws_filled:
            in_window = {
                str(r["date"])
                for r in merged
                if start_date <= str(r["date"]) <= end_date
            }
            gap_dates = [d for d in expected if d not in in_window]

    rows_written = write_csv_rows(path, merged)

    window_rows = [r for r in merged if start_date <= str(r["date"]) <= end_date]
    dates = sorted(str(r["date"]) for r in window_rows)
    by_source: Dict[str, int] = {}
    for r in window_rows:
        src = str(r.get("source") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1

    return {
        "station": station,
        "series_ticker": spec.series_ticker,
        "city": spec.city,
        "csv_path": os.path.relpath(path, REPO_ROOT).replace("\\", "/"),
        "rows_in_file": rows_written,
        "rows_stored": len(window_rows),
        "requested_days": len(expected),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "missing_count": len(gap_dates),
        "gap_dates": gap_dates,
        "null_high_dates": null_high_dates,
        "nws_filled_dates": nws_filled,
        "by_source": by_source,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_report(summary: Dict[str, object], *, min_days: int) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("CLI DAILY-HIGH BACKFILL (PRD FR-1.6)")
    lines.append("=" * 78)
    lines.append(
        f"  Window            : {summary['start_date']} .. {summary['end_date']} "
        f"({summary['requested_days']} days)"
    )
    lines.append(f"  Required coverage : >= {min_days} stored rows per city")
    lines.append("")
    lines.append(
        f"  {'STATION':8s} {'SERIES':11s} {'ROWS':>5s} {'MISS':>5s}  "
        f"{'FIRST':10s} {'LAST':10s}  STATUS"
    )
    lines.append("  " + "-" * 74)
    for st in summary["stations"]:
        if st.get("error"):
            lines.append(
                f"  {st['station']:8s} {'-':11s} {'ERR':>5s}  {st['error'][:44]}"
            )
            continue
        status = "OK" if st["rows_stored"] >= min_days else "SHORT"
        lines.append(
            f"  {st['station']:8s} {st['series_ticker']:11s} "
            f"{st['rows_stored']:5d} {st['missing_count']:5d}  "
            f"{str(st['first_date']):10s} {str(st['last_date']):10s}  {status}"
        )

    any_gaps = [st for st in summary["stations"] if st.get("gap_dates")]
    if any_gaps:
        lines.append("")
        lines.append(
            "  Explicit gaps (no CLI product in the archive; NOT interpolated):"
        )
        for st in any_gaps:
            gaps = st["gap_dates"]
            shown = ", ".join(gaps[:12])
            more = f" ... +{len(gaps) - 12} more" if len(gaps) > 12 else ""
            lines.append(f"    {st['station']}: {len(gaps)} -> {shown}{more}")

    nulls = [st for st in summary["stations"] if st.get("null_high_dates")]
    if nulls:
        lines.append("")
        lines.append("  Archive rows present but carrying no maximum temperature:")
        for st in nulls:
            lines.append(
                f"    {st['station']}: {', '.join(st['null_high_dates'][:12])}"
            )

    lines.append("")
    lines.append("  Provenance by source:")
    for st in summary["stations"]:
        if st.get("by_source"):
            breakdown = ", ".join(
                f"{k}={v}" for k, v in sorted(st["by_source"].items())
            )
            lines.append(f"    {st['station']}: {breakdown}")

    lines.append("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backfill NWS CLI daily-high ground truth for the tracked cities."
    )
    p.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Days of history to back-fill (default {DEFAULT_DAYS}; PRD needs >=180).",
    )
    p.add_argument(
        "--end-date",
        default=None,
        help="Last date of the window (YYYY-MM-DD, default: today UTC).",
    )
    p.add_argument(
        "--stations",
        default=",".join(STATIONS),
        help=f"Comma-separated stations (default {','.join(STATIONS)}).",
    )
    p.add_argument(
        "--out-dir",
        default=WEATHER_TRUTH_DIR,
        help="Directory for the CSVs and the coverage report.",
    )
    p.add_argument(
        "--min-days",
        type=int,
        default=DEFAULT_MIN_DAYS,
        help=f"Rows per city required for exit 0 (default {DEFAULT_MIN_DAYS}).",
    )
    p.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass the on-disk provider cache.",
    )
    p.add_argument(
        "--fill-gaps-nws",
        action="store_true",
        help="Try the NWS CLI product for gaps (only ~1 week deep; see docstring).",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=0.5,
        help="Seconds between HTTP requests (default 0.5).",
    )
    p.add_argument(
        "--json", action="store_true", help="Emit JSON instead of the text report."
    )
    p.add_argument("--quiet", action="store_true", help="Suppress INFO logging.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.days < 1:
        log("FATAL: --days must be >= 1")
        return 2

    end_date = normalize_date(args.end_date or datetime.now(timezone.utc).date())
    start_dt = datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(
        days=args.days - 1
    )
    start_date = start_dt.isoformat()

    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]
    if not stations:
        log("FATAL: no stations selected")
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    provider = IEMCLIProvider(
        cache_dir=os.path.join(args.out_dir, "cache"), request_pause=args.pause
    )

    log(
        f"Backfilling {args.days} days ({start_date} .. {end_date}) for {', '.join(stations)}"
    )

    station_summaries: List[Dict[str, object]] = []
    combined: List[Dict[str, object]] = []
    for station in stations:
        try:
            st = backfill_station(
                provider,
                station,
                start_date,
                end_date,
                out_dir=args.out_dir,
                force_refresh=args.force_refresh,
                fill_gaps_nws=args.fill_gaps_nws,
            )
        except IEMCLIError as exc:
            log(f"FATAL: {exc}")
            return 2
        station_summaries.append(st)
        combined.extend(read_csv_rows(station_csv_path(args.out_dir, station)))
        if not st.get("error"):
            log(
                f"  {station}: {st['rows_stored']} rows stored, "
                f"{st['missing_count']} gaps, {st['first_date']} .. {st['last_date']}"
            )

    combined_path = os.path.join(args.out_dir, COMBINED_BASENAME)
    combined_count = write_csv_rows(combined_path, combined)

    summary: Dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date": start_date,
        "end_date": end_date,
        "requested_days": args.days,
        "min_days": args.min_days,
        "combined_csv": os.path.relpath(combined_path, REPO_ROOT).replace("\\", "/"),
        "combined_rows": combined_count,
        "stations": station_summaries,
    }

    coverage_path = os.path.join(args.out_dir, COVERAGE_BASENAME)
    with open(coverage_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=False)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_report(summary, min_days=args.min_days))
        log(f"Combined CSV : {combined_path} ({combined_count} rows)")
        log(f"Coverage     : {coverage_path}")

    short = [
        st
        for st in station_summaries
        if st.get("error") or int(st.get("rows_stored") or 0) < args.min_days
    ]
    if short:
        log(
            "SHORTFALL: "
            + ", ".join(
                f"{st['station']}={st.get('rows_stored', 0)}<{args.min_days}"
                for st in short
            )
        )
        return 1

    log(f"OK: every station has >= {args.min_days} stored CLI daily highs")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - top-level safety net
        log(f"FATAL: {exc}")
        sys.exit(2)
