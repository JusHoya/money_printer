"""Backfill Kalshi weather bracket-ladder history for the Phase 2 go/no-go EV report.

PRD Phase 2 exit criterion 5 needs ">=30 days of recorded ladders" priced under
BOTH maker and taker pricing -- which needs bid AND ask. The VM's own harvest
CSVs cannot supply that (776 of 803 archived files carry only a single ``Price``
column), so the ladder history is pulled from Kalshi's own recorded market
history via the public candlesticks endpoint instead.

Usage
-----
::

    $env:PYTHONPATH = "."
    python scripts/backfill_ladders.py --start 2026-05-18 --end 2026-07-25
    python scripts/backfill_ladders.py --days 60           # ending yesterday
    python scripts/backfill_ladders.py --probe-retention   # find the earliest
                                                           # retrievable date

Writes ``<out>/<SERIES>/<YYYY-MM-DD>.csv`` plus the provenance manifest
``<out>/manifest.json`` (``--out`` defaults to ``data/ladders``; the manifest
always lives under the same root as the CSVs, never in ``data/ladders`` when
``--out`` points elsewhere). Read-only against the Kalshi API and against
``data/weather_truth/``; it never places an order and never writes outside
``--out``. Note ``manifest.json`` describes the *last run* only -- the M0
capture timer (``deploy/spark/ladder_capture.sh``) keeps a per-run copy.

Sealed roots (PRD_STRATEGY_FACTORY.md FR-F0.5): ``--out data/ladders_holdout``
and ``--out data/ladders_2026-09`` are written by this script but refused by
``load_ladders``; ``--stats`` on them reads through the explicit unchecked
loader and prints coverage only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.kalshi_history import (  # noqa: E402
    DEFAULT_PERIOD_INTERVAL,
    LADDER_DIR,
    OBSERVED_RETENTION_FLOOR,
    WEATHER_CITY_SPECS,
    KalshiHistoryClient,
    _load_ladders_unchecked,
    backfill,
    event_ticker_for,
    load_ladders,
    load_manifest,
)
from src.backtest.sealed_roots import sealed_reason  # noqa: E402

# Bracket distance from the settled outcome, in degrees F. 0 = the bracket
# that actually paid; larger = further out of the money. This is the banding
# FR-2.4 asks the EV report to be sliced on.
DISTANCE_BANDS = ((0, 0), (1, 2), (3, 4), (5, 6), (7, 999))

# Hours remaining until the market's close_time.
TTC_BANDS = (
    ("<1h", 0, 1),
    ("1-3h", 1, 3),
    ("3-6h", 3, 6),
    ("6-12h", 6, 12),
    ("12-24h", 12, 24),
    (">=24h", 24, 1e9),
)


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def probe_retention(client: KalshiHistoryClient, series: str, today: dt.date) -> None:
    """Print the earliest target date for which Kalshi still returns markets.

    Kalshi keeps the ``/events`` listing back to 2021 but drops the nested
    market metadata (``strike_type`` / ``result`` / strikes) after a retention
    window. Without those fields a day cannot be settled through
    ``bracket_payoff``, so it is unusable regardless of what ``/events`` says.
    """
    lo, hi = 1, 400
    # Expand until we find a date with no markets.
    while hi > lo:
        mid = (lo + hi) // 2
        d = today - dt.timedelta(days=mid)
        markets, _ = client.fetch_event_markets(series, d)
        if markets:
            lo = mid + 1
        else:
            hi = mid
    earliest = today - dt.timedelta(days=lo - 1)
    print(
        f"{series}: earliest retrievable target date = {earliest} "
        f"({lo - 1} days back from {today}); event {event_ticker_for(series, earliest)}"
    )


def bracket_distance(row) -> float:
    """Degrees F between the settled daily high and the bracket's YES band.

    ``0`` for the bracket that actually paid. Derived only from the API's
    ``strike_type`` / ``floor_strike`` / ``cap_strike`` (PRD FR-1.1) and the
    settled ``expiration_value`` -- never from the ticker string.
    """
    import math as _m

    high = row["expiration_value"]
    st = row["strike_type"]
    if (
        high is None
        or (isinstance(high, float) and _m.isnan(high))
        or not isinstance(st, str)
    ):
        return float("nan")
    if st == "between":
        lo, hi = row["floor_strike"], row["cap_strike"]
    elif st == "greater":
        lo, hi = row["floor_strike"] + 1.0, float("inf")
    elif st == "less":
        lo, hi = float("-inf"), row["cap_strike"] - 1.0
    else:
        return float("nan")
    if lo <= high <= hi:
        return 0.0
    return float(lo - high) if high < lo else float(high - hi)


def band_label(distance: float) -> str:
    import math as _m

    if distance is None or _m.isnan(distance):
        return "unknown"
    for lo, hi in DISTANCE_BANDS:
        if lo <= distance <= hi:
            return f"{lo}F" if lo == hi else (f">={lo}F" if hi > 900 else f"{lo}-{hi}F")
    return "unknown"


def ttc_label(hours: float) -> str:
    import math as _m

    if hours is None or _m.isnan(hours):
        return "unknown"
    for name, lo, hi in TTC_BANDS:
        if lo <= hours < hi:
            return name
    return "unknown"


def print_stats(root: Path) -> int:
    """Descriptive statistics over the backfilled ladders (deliverable 2)."""

    reason = sealed_reason(root)
    if reason is None:
        df = load_ladders(root)
    else:
        # Coverage statistics only (no fitness, no strategy input); the
        # search frame itself keeps refusing this root -- FR-F0.5.
        print(f"NOTE: {reason}; --stats prints descriptive coverage only.")
        df = _load_ladders_unchecked(root)
    if df.empty:
        print(f"No ladder rows under {root}. Run the backfill first.")
        return 1
    manifest = load_manifest(Path(root) / "manifest.json")

    df["hours_to_close"] = df["minutes_to_close"] / 60.0
    df["distance"] = df.apply(bracket_distance, axis=1)
    df["band"] = df["distance"].map(band_label)
    df["ttc"] = df["hours_to_close"].map(ttc_label)
    df["spread_c"] = (df["yes_ask"] - df["yes_bid"]) * 100.0
    q = df[df["has_quote"] == True]  # noqa: E712

    print("=== coverage ===")
    print(f"rows                       : {len(df):,}")
    print(f"target dates               : {df['target_date'].nunique()}")
    print(f"cities                     : {sorted(df['city'].unique())}")
    print(f"city-days                  : {df.groupby(['city','target_date']).ngroups}")
    print(f"markets                    : {df['market_ticker'].nunique():,}")
    mpd = df.groupby(["city", "target_date"])["market_ticker"].nunique()
    print(
        f"markets/city-day           : min={mpd.min()} median={mpd.median():.0f} max={mpd.max()}"
    )
    cpm = df.groupby("market_ticker").size()
    print(
        f"candles/market             : min={cpm.min()} median={cpm.median():.0f} max={cpm.max()}"
    )
    pct = 100.0 * len(q) / len(df)
    print(f"two-sided quoted rows      : {len(q):,} / {len(df):,} ({pct:.1f}%)")
    if manifest:
        t = manifest.get("totals", {})
        print(
            f"bracket_payoff vs Kalshi   : {t.get('payoff_matched')}/{t.get('payoff_checked')}"
        )
        print(
            f"Kalshi expval vs CLI truth : "
            f"{t.get('truth_checked', 0) - t.get('truth_disagreements', 0)}"
            f"/{t.get('truth_checked')} agree"
        )

    print("\n=== bid-ask spread (cents), quoted rows only ===")
    print("by time-to-close:")
    order = [n for n, _, _ in TTC_BANDS]
    g = q.groupby("ttc")["spread_c"]
    for name in order:
        if name not in g.groups:
            continue
        s = g.get_group(name)
        allrows = df[df["ttc"] == name]
        print(
            f"  {name:>7}  n={len(s):>6,}  quoted={100.0*len(s)/max(1,len(allrows)):>5.1f}%"
            f"  median={s.median():>5.1f}  p25={s.quantile(.25):>5.1f}"
            f"  p75={s.quantile(.75):>5.1f}  p90={s.quantile(.90):>5.1f}"
        )
    print("by bracket distance from settled outcome:")
    for name in ("0F", "1-2F", "3-4F", "5-6F", ">=7F"):
        sub = df[df["band"] == name]
        if sub.empty:
            continue
        sq = sub[sub["has_quote"] == True]  # noqa: E712
        med = sq["spread_c"].median() if len(sq) else float("nan")
        p90 = sq["spread_c"].quantile(0.90) if len(sq) else float("nan")
        print(
            f"  {name:>6}  rows={len(sub):>7,}  quoted={100.0*len(sq)/len(sub):>5.1f}%"
            f"  median_spread={med:>5.1f}c  p90={p90:>5.1f}c"
            f"  median_yes_ask={sq['yes_ask'].median() if len(sq) else float('nan'):.3f}"
        )

    print("\n=== liquidity ===")
    print(
        f"rows with volume>0         : {(df['volume'] > 0).sum():,} "
        f"({100.0*(df['volume']>0).mean():.1f}%)"
    )
    per_mkt = df.groupby(["city", "target_date", "market_ticker"])["volume"].sum()
    traded = (per_mkt > 0).groupby(level=[0, 1]).sum()
    print(
        f"brackets that ever trade   : median {traded.median():.0f} of "
        f"{mpd.median():.0f} per city-day (min {traded.min():.0f}, max {traded.max():.0f})"
    )
    oi = df.groupby("market_ticker")["open_interest"].max()
    print(
        f"peak open interest/market  : p10={oi.quantile(.10):,.0f} median={oi.median():,.0f} "
        f"p90={oi.quantile(.90):,.0f}"
    )
    dv = df.groupby(["city", "target_date"])["volume"].sum()
    print(
        f"volume per city-day        : p10={dv.quantile(.10):,.0f} median={dv.median():,.0f} "
        f"p90={dv.quantile(.90):,.0f}"
    )
    print("\nby city:")
    for city, sub in df.groupby("city"):
        sq = sub[sub["has_quote"] == True]  # noqa: E712
        print(
            f"  {city:>4}  rows={len(sub):>7,}  quoted={100.0*len(sq)/len(sub):>5.1f}%"
            f"  median_spread={sq['spread_c'].median():>5.1f}c"
            f"  median_vol/row={sub['volume'].median():>7.1f}"
        )
    print("\nfar-bracket (>=5F out) quote availability by time-to-close:")
    far = df[df["band"].isin(("5-6F", ">=7F"))]
    for name in order:
        sub = far[far["ttc"] == name]
        if sub.empty:
            continue
        sq = sub[sub["has_quote"] == True]  # noqa: E712
        med = sq["spread_c"].median() if len(sq) else float("nan")
        print(
            f"  {name:>7}  rows={len(sub):>6,}  quoted={100.0*len(sq)/len(sub):>5.1f}%"
            f"  median_spread={med:>5.1f}c"
            f"  median_yes_bid={sq['yes_bid'].median() if len(sq) else float('nan'):.3f}"
        )

    # One-sided availability. `has_quote` demands BOTH sides, but the two
    # FR-3.1 trade shapes only need one each:
    #   (a) far-bracket NO  = sell YES / buy NO -> needs a YES BID > 0
    #   (b) lock-in tails   = buy YES           -> needs a YES ASK < 1
    # Reporting only the two-sided number would understate what is actually
    # executable, so slice both sides explicitly.
    df["bid_avail"] = df["yes_bid"].fillna(0) > 0
    df["ask_avail"] = df["yes_ask"].fillna(1) < 1
    print("\none-sided availability (what each FR-3.1 shape actually needs):")
    hdr = (
        f"  {'band':>7} {'rows':>8} {'yes_bid>0':>10} {'yes_ask<1':>10} "
        f"{'both':>7} {'med_bid':>8} {'med_ask':>8}"
    )
    print(hdr)

    def _row(label, sub):
        b = sub[sub["yes_bid"].fillna(0) > 0]
        a = sub[sub["yes_ask"].fillna(1) < 1]
        print(
            f"  {label:>7} {len(sub):>8,} {100.0*len(b)/len(sub):>9.1f}% "
            f"{100.0*len(a)/len(sub):>9.1f}% {100.0*sub['has_quote'].mean():>6.1f}% "
            f"{b['yes_bid'].median() if len(b) else float('nan'):>8.3f} "
            f"{a['yes_ask'].median() if len(a) else float('nan'):>8.3f}"
        )

    for name in ("0F", "1-2F", "3-4F", "5-6F", ">=7F"):
        sub = df[df["band"] == name]
        if not sub.empty:
            _row(name, sub)
    print("\n  far brackets (>=5F out) by time-to-close:")
    print(hdr)
    for name in order:
        sub = far[far["ttc"] == name]
        if not sub.empty:
            _row(name, sub)
    print("\n  winning bracket (0F) by time-to-close  [FR-3.1(b) lock-in buys this]:")
    print(hdr)
    win = df[df["band"] == "0F"]
    for name in order:
        sub = win[win["ttc"] == name]
        if not sub.empty:
            _row(name, sub)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_date, help="first target date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_date, help="last target date (YYYY-MM-DD)")
    parser.add_argument(
        "--days",
        type=int,
        help="number of days ending at --end (default: yesterday) when --start is absent",
    )
    parser.add_argument(
        "--cities",
        nargs="*",
        default=None,
        help="city keys to backfill (default: NY CHI LAX MIA)",
    )
    parser.add_argument(
        "--period-interval",
        type=int,
        default=DEFAULT_PERIOD_INTERVAL,
        choices=(1, 60, 1440),
        help="candlestick period in minutes (default: 60)",
    )
    parser.add_argument("--out", default=str(LADDER_DIR), help="output directory")
    parser.add_argument(
        "--probe-retention",
        action="store_true",
        help="report the earliest retrievable date per series and exit",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="print ladder-quality descriptive statistics over --out and exit",
    )
    args = parser.parse_args(argv)

    if args.stats:
        return print_stats(Path(args.out))

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    client = KalshiHistoryClient()
    today = dt.datetime.now(dt.timezone.utc).date()

    specs = list(WEATHER_CITY_SPECS)
    if args.cities:
        wanted = {c.upper() for c in args.cities}
        specs = [s for s in specs if s[0] in wanted]
        if not specs:
            parser.error(f"no known cities in {sorted(wanted)}")

    if args.probe_retention:
        for _, series, _ in specs:
            probe_retention(client, series, today)
        return 0

    end = args.end or (today - dt.timedelta(days=1))
    if args.start:
        start = args.start
    elif args.days:
        start = end - dt.timedelta(days=args.days - 1)
    else:
        start = max(OBSERVED_RETENTION_FLOOR, end - dt.timedelta(days=59))
    if start > end:
        parser.error(f"--start {start} is after --end {end}")

    n_days = (end - start).days + 1
    print(
        f"Backfilling {n_days} day(s) {start}..{end} x {len(specs)} cities "
        f"at period_interval={args.period_interval}m -> {args.out}"
    )

    done = {"n": 0}
    total = n_days * len(specs)

    def _progress(day_meta):
        done["n"] += 1
        print(
            f"  [{done['n']:>4}/{total}] {day_meta['event_ticker']:<24} "
            f"markets={day_meta['markets']:<2} rows={day_meta['rows']:<5} "
            f"quoted={day_meta['quoted_rows']:<5}"
            + (
                "  EMPTY: " + str(day_meta["empty_reason"]) if day_meta["empty"] else ""
            ),
            flush=True,
        )

    manifest = backfill(
        start,
        end,
        city_specs=specs,
        root=Path(args.out),
        period_interval=args.period_interval,
        client=client,
        progress=_progress,
    )

    t = manifest["totals"]
    print("\n=== backfill summary ===")
    print(f"  city-days requested : {t['days_requested']}")
    print(f"  city-days with rows : {t['days_with_rows']}")
    print(f"  city-days empty     : {t['days_empty']}")
    print(f"  markets             : {t['markets']}")
    print(f"  rows                : {t['rows']}")
    pct = 100.0 * t["quoted_rows"] / t["rows"] if t["rows"] else 0.0
    print(f"  two-sided quotes    : {t['quoted_rows']} ({pct:.1f}%)")
    print(
        f"  bracket_payoff vs Kalshi result (from expiration_value): "
        f"{t['payoff_matched']}/{t['payoff_checked']}"
    )
    print(
        f"  bracket_payoff vs Kalshi result (from CLI truth):        "
        f"{t['payoff_matched_cli']}/{t['payoff_checked_cli']}"
    )
    print(
        f"  markets with blank expiration_value: "
        f"{t['markets_missing_expiration_value']}"
    )
    print(
        f"  Kalshi expiration_value vs CLI truth: "
        f"{t['truth_checked'] - t['truth_disagreements']}/{t['truth_checked']} agree"
    )
    print(f"  HTTP failures       : {len(manifest['http_failures'])}")
    print(f"  manifest            : {Path(args.out) / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
