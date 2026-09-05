#!/usr/bin/env python3
"""measure_fill_realism.py -- intra-cadence ask drift on the maia tape (F3 fill-realism study).

FACTORY_ARCHITECTURE section 9 item 6 / FACTORY_ROADMAP section F3 item 8 /
PRD_STRATEGY_FACTORY FR-F3.4 and exit criterion: the factory's fill claim is
``limit = quote + adverse_fill`` with ``adverse_fill`` covering the 90th
percentile of the drift a taker sees between the decision snapshot and the
moment the order would reach the book. This script measures that drift on the
sandbox's own 14-s tape (the dashboard data-log CSV, ``DATA_CSV_HEADER`` in
``src/visualization/dashboard.py``; ``TZ=UTC`` in ``deploy/pi``, so its naive
``Timestamp`` is UTC).

WHAT IS MEASURED
----------------
The promoted ``GenomeStrategy`` decides once per market at the top of each UTC
hour, on the first poll at/after ``:00``. For every ``(market, hour)`` on the
tape:

* the **decision poll** is the first ``MARKET_DATA`` row with ``ts >= H``,
  admitted only if it arrives within ``--max-decision-lag`` seconds of ``H``
  (else the hour is a *tape gap*, counted and skipped);
* the **traded-side asks** are ``Ask`` (YES ask, what a buy-YES taker pays)
  and ``NoAsk`` (NO ask, what a buy-NO taker pays) at that poll; a blank
  quote is a *missing quote*, counted and skipped for that side;
* for each window ``W`` in ``--windows`` (default 20 s and 60 s) the
  **adverse drift** is ``max(0, max_t(ask_t - ask_0))`` over the follow-up
  polls with ``ts_dec < ts <= ts_dec + W`` -- how far the ask moved AGAINST
  the taker before ``W`` elapsed (price improvement is clipped to 0 because
  the allowance only has to cover moves against us); the **signed next-poll
  drift** ``ask_next - ask_0`` is reported too. An hour with no follow-up poll
  inside ``W`` is a *follow-up gap* for that window and is skipped.

Percentiles use the nearest-rank (ceiling) definition -- ``sorted[ceil(q n) - 1]``
-- so p90 is an observed drift, never an interpolated one.

RECOMMENDATION
--------------
    adverse_fill = max(0.01, ceil_to_cent(p90 of the adverse drift at the
                       primary (first) window))

The report states in plain words whether p90 exceeds 1c. If it does, the
registry must record the raised ``adverse_fill`` and family #1 must be
re-scored under it (a re-score, not a re-search) -- this script only flags it.

INPUT
-----
    --csv PATH [PATH ...]        local data-log CSV(s) (``data_*.csv`` or a collected tape)
    --url http://maia.local:8050/api/logs/data
                                 the sandbox route; it returns only the LAST 100 rows
                                 (~15 s of tape, no parameters), so a study needs
                                 ``--collect-seconds N`` to poll it repeatedly (every
                                 ``--poll-interval`` s) and accumulate into ``--cache``.

    python scripts/measure_fill_realism.py --url http://maia.local:8050/api/logs/data \\
        --collect-seconds 7200 --cache data/fill_realism/maia_tape.csv
    python scripts/measure_fill_realism.py --csv data/fill_realism/maia_tape.csv

Writes ``reports/factory/fill_realism_<date>.json`` and ``.md`` (``<date>`` =
the UTC date of the last tape row unless ``--date`` is given). Exit 0.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.factory.report import write_json, write_text  # noqa: E402

DATA_CSV_HEADER = [
    "Timestamp", "Symbol", "Price", "Type", "Status", "Bid", "Ask", "NoBid", "NoAsk",
    "Last", "Volume", "Depth", "StrikeType", "FloorStrike", "CapStrike",
]
MARKET_SUFFIX = " (Market)"
DEFAULT_WINDOWS = (20.0, 60.0)
DEFAULT_MAX_DECISION_LAG = 60.0
FLOOR_ADVERSE_FILL = 0.01


# ---------------------------------------------------------------------------
# Tape I/O
# ---------------------------------------------------------------------------
def read_csv_rows(paths: Sequence[str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for p in paths:
        with open(p, "r", encoding="utf-8", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def fetch_rows(url: str, timeout: float = 15.0) -> List[Dict[str, str]]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.load(resp)
    return [r for r in data if isinstance(r, dict)]


def _row_key(r: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (str(r.get("Timestamp", "")), str(r.get("Symbol", "")), str(r.get("Type", "")))


def collect(
    url: str,
    cache_path: str,
    seconds: float,
    poll_interval: float = 3.0,
    _fetch=fetch_rows,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> Dict[str, int]:
    """Poll ``url`` for ``seconds``, appending unseen rows to ``cache_path`` (CSV)."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    new_file = not path.exists() or path.stat().st_size == 0
    if not new_file:
        for r in read_csv_rows([str(path)]):
            seen.add(_row_key(r))
    added = errors = polls = 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(DATA_CSV_HEADER)
        t_end = _now() + seconds
        while True:
            polls += 1
            try:
                for r in _fetch(url):
                    k = _row_key(r)
                    if k in seen:
                        continue
                    seen.add(k)
                    w.writerow([r.get(h, "") for h in DATA_CSV_HEADER])
                    added += 1
                fh.flush()
            except Exception:  # noqa: BLE001 -- a poll failure is counted, not fatal
                errors += 1
            if _now() >= t_end:
                break
            _sleep(poll_interval)
    return {"polls": polls, "rows_added": added, "errors": errors}


# ---------------------------------------------------------------------------
# Analysis (pure)
# ---------------------------------------------------------------------------
def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _ts(v: str, tz: timezone) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _iso_or_none(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def market_ticker(symbol: str) -> Optional[str]:
    """``'KXHIGHNY-26SEP04-B84.5 (Market)'`` -> ticker; ``None`` for non-market rows."""
    s = str(symbol or "")
    if not s.endswith(MARKET_SUFFIX):
        return None
    return s[: -len(MARKET_SUFFIX)].strip()


def _city(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def percentile_nearest_rank(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[idx]


def ceil_to_cent(x: float) -> float:
    return math.ceil(round(x * 100.0, 6)) / 100.0


def _dist(values: Sequence[float]) -> Dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile_nearest_rank(values, 0.50),
        "p90": percentile_nearest_rank(values, 0.90),
        "p95": percentile_nearest_rank(values, 0.95),
        "max": max(values) if values else None,
        "mean": (math.fsum(values) / len(values)) if values else None,
        "share_gt_0": (sum(1 for v in values if v > 1e-12) / len(values)) if values else None,
        "share_gt_1c": (sum(1 for v in values if v > 0.01 + 1e-12) / len(values)) if values else None,
    }


def analyse(
    rows: Iterable[Mapping[str, Any]],
    *,
    series_prefix: str = "KXHIGH",
    windows: Sequence[float] = DEFAULT_WINDOWS,
    max_decision_lag: float = DEFAULT_MAX_DECISION_LAG,
    tz: timezone = timezone.utc,
) -> Dict[str, Any]:
    """The study over parsed tape rows. See the module docstring for definitions."""
    windows = tuple(float(w) for w in windows)
    series: Dict[str, List[Tuple[datetime, Optional[float], Optional[float]]]] = defaultdict(list)
    n_rows = n_market_rows = 0
    for r in rows:
        n_rows += 1
        if str(r.get("Type", "")) != "MARKET_DATA":
            continue
        ticker = market_ticker(r.get("Symbol", ""))
        if ticker is None or not ticker.upper().startswith(series_prefix.upper()):
            continue
        ts = _ts(r.get("Timestamp", ""), tz)
        if ts is None:
            continue
        n_market_rows += 1
        series[ticker].append((ts, _f(r.get("Ask")), _f(r.get("NoAsk"))))

    counts: Counter = Counter()
    lag_samples: List[float] = []
    adverse: Dict[float, Dict[str, List[float]]] = {w: {"yes": [], "no": []} for w in windows}
    signed_next: Dict[str, List[float]] = {"yes": [], "no": []}
    next_adverse: Dict[str, List[float]] = {"yes": [], "no": []}
    next_gap: List[float] = []
    per_city: Dict[str, Counter] = defaultdict(Counter)
    hours_seen: set = set()
    poll_gaps: List[float] = []
    examples: List[Dict[str, Any]] = []

    for ticker, pts in series.items():
        pts.sort(key=lambda t: t[0])
        for (a, _, _), (b, _, _) in zip(pts, pts[1:]):
            poll_gaps.append((b - a).total_seconds())
        first, last = pts[0][0], pts[-1][0]
        h = first.replace(minute=0, second=0, microsecond=0)
        if h < first:
            h += timedelta(hours=1)
        while h <= last:
            hours_seen.add(h.isoformat())
            counts["market_hours"] += 1
            dec_i = next((i for i, p in enumerate(pts) if p[0] >= h), None)
            if dec_i is None:
                counts["gap_no_decision_poll"] += 1
                h += timedelta(hours=1)
                continue
            dec_ts = pts[dec_i][0]
            lag = (dec_ts - h).total_seconds()
            if lag > max_decision_lag:
                counts["gap_no_decision_poll"] += 1
                h += timedelta(hours=1)
                continue
            lag_samples.append(lag)
            counts["decision_polls"] += 1
            per_city[_city(ticker)]["decision_polls"] += 1
            for side, col in (("yes", 1), ("no", 2)):
                a0 = pts[dec_i][col]
                if a0 is None:
                    counts[f"missing_quote_{side}"] += 1
                    continue
                follow = [p for p in pts[dec_i + 1 :] if (p[0] - dec_ts).total_seconds() <= max(windows)]
                nxt = next((p for p in follow if p[col] is not None), None)
                if nxt is not None:
                    signed_next[side].append(nxt[col] - a0)
                    next_adverse[side].append(max(0.0, nxt[col] - a0))
                    next_gap.append((nxt[0] - dec_ts).total_seconds())
                else:
                    counts[f"gap_no_next_poll_{int(max(windows))}s_{side}"] += 1
                for w in windows:
                    inside = [p[col] for p in follow if (p[0] - dec_ts).total_seconds() <= w and p[col] is not None]
                    if not inside:
                        counts[f"gap_no_followup_{int(w)}s_{side}"] += 1
                        continue
                    drift = max(0.0, max(x - a0 for x in inside))
                    adverse[w][side].append(drift)
                    per_city[_city(ticker)][f"samples_{int(w)}s"] += 1
                    if drift > 0.01 + 1e-12 and len(examples) < 25:
                        examples.append(
                            {"market": ticker, "hour_utc": h.isoformat(), "side": side,
                             "window_s": w, "ask_0": a0, "ask_max": a0 + drift, "drift": round(drift, 4)}
                        )
            h += timedelta(hours=1)

    primary = windows[0] if windows else None
    by_window: Dict[str, Any] = {}
    for w in windows:
        both = adverse[w]["yes"] + adverse[w]["no"]
        by_window[f"{int(w)}s"] = {
            "yes_ask": _dist(adverse[w]["yes"]),
            "no_ask": _dist(adverse[w]["no"]),
            "both_sides": _dist(both),
        }
    p90_window = by_window[f"{int(primary)}s"]["both_sides"]["p90"] if primary is not None else None
    n_window = by_window[f"{int(primary)}s"]["both_sides"]["n"] if primary is not None else 0
    next_both = next_adverse["yes"] + next_adverse["no"]
    next_poll = {
        "yes_ask": _dist(next_adverse["yes"]),
        "no_ask": _dist(next_adverse["no"]),
        "both_sides": _dist(next_both),
        "gap_s": _dist(next_gap) if next_gap else None,
    }
    p90_next = next_poll["both_sides"]["p90"]
    candidates = [(v, k) for v, k in ((p90_window, f"{int(primary)}s window" if primary else None), (p90_next, "next poll")) if v is not None]
    if not candidates:
        p90 = None
        basis = None
        recommendation = None
        exceeds = None
        statement = "no drift samples (no covered :00 boundary with a follow-up poll); no recommendation can be made"
    else:
        p90, basis = max(candidates)
        recommendation = max(FLOOR_ADVERSE_FILL, ceil_to_cent(p90))
        exceeds = p90 > 0.01 + 1e-12
        statement = (
            f"p90 adverse drift = {p90:.4f} (basis: {basis}; "
            f"{int(primary) if primary else '?'} s window p90={p90_window} n={n_window}, "
            f"next-poll p90={p90_next} n={next_poll['both_sides']['n']}) "
            + ("EXCEEDS 1c: the registry must record adverse_fill="
               f"{recommendation:.2f} and family #1 must be re-scored (not re-searched)"
               if exceeds
               else "does not exceed 1c: adverse_fill=0.01 stands")
        )
    poll_gap_dist = _dist(poll_gaps) if poll_gaps else None
    return {
        "series_prefix": series_prefix,
        "windows_s": list(windows),
        "primary_window_s": primary,
        "max_decision_lag_s": max_decision_lag,
        "definitions": {
            "decision_poll": "first MARKET_DATA row with ts >= H (top of UTC hour), lag <= max_decision_lag",
            "decision_points": "only hour boundaries H with a poll strictly before H on the tape (a fragment starting after :00 cannot say which poll was first)",
            "adverse_drift": "max(0, max_t(ask_t - ask_0)) over follow-up polls with ts_dec < ts <= ts_dec + W",
            "signed_next": "ask_next - ask_0 for the first follow-up poll carrying the quote",
            "percentile": "nearest-rank: sorted[ceil(q*n) - 1]",
            "next_poll_adverse": "max(0, ask_next - ask_0) to the first follow-up poll carrying the quote within max(windows); at the tape's ~40 s per-market cadence this is an UPPER bound on the 20 s drift",
            "recommendation": "adverse_fill = max(0.01, ceil_to_cent(max(p90 adverse drift at the primary window, p90 next-poll adverse drift)))",
        },
        "tape": {
            "rows": n_rows,
            "market_rows_in_series": n_market_rows,
            "markets": len(series),
            "hours_utc": sorted(hours_seen),
            "first_ts": _iso_or_none(min((p[0] for pts in series.values() for p in pts), default=None)),
            "last_ts": _iso_or_none(max((p[0] for pts in series.values() for p in pts), default=None)),
            "poll_gap_s": poll_gap_dist,
        },
        "counts": dict(sorted(counts.items())),
        "decision_lag_s": _dist(lag_samples),
        "per_city": {c: dict(v) for c, v in sorted(per_city.items())},
        "adverse_drift": by_window,
        "next_poll_adverse_drift": next_poll,
        "signed_next_poll": {"yes_ask": _dist(signed_next["yes"]), "no_ask": _dist(signed_next["no"])},
        "examples_gt_1c": examples,
        "p90_primary_window": p90_window,
        "p90_next_poll": p90_next,
        "p90_primary": p90,
        "p90_basis": basis,
        "p90_exceeds_1c": exceeds,
        "recommended_adverse_fill": recommendation,
        "statement": statement,
    }


def render_markdown(rep: Mapping[str, Any], label: str) -> str:
    t = rep["tape"]
    lines = [
        f"# Fill-realism study {label} -- {rep['series_prefix']} on the maia tape",
        "",
        f"**{rep['statement']}**",
        "",
        f"- tape rows {t['rows']}, {rep['series_prefix']} market rows {t['market_rows_in_series']}, "
        f"markets {t['markets']}, first {t['first_ts']}, last {t['last_ts']}",
        f"- UTC hours with a decision point: {len(t['hours_utc'])} ({', '.join(h[11:16] for h in t['hours_utc'])})",
        f"- poll gap (s): {t['poll_gap_s']}",
        f"- decision lag after :00 (s): {rep['decision_lag_s']}",
        f"- counts: {rep['counts']}",
        "",
        "## Adverse drift of the traded-side ask (max(0, max ask_t - ask_0))",
        "",
        "| window | side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for w, d in rep["adverse_drift"].items():
        for side in ("yes_ask", "no_ask", "both_sides"):
            x = d[side]
            lines.append(
                f"| {w} | {side} | {x['n']} | {x['p50']} | {x['p90']} | {x['p95']} | {x['max']} | "
                f"{x['share_gt_0']} | {x['share_gt_1c']} |"
            )
    npd = rep["next_poll_adverse_drift"]
    lines += [
        "",
        "## Next-poll adverse drift (max(0, ask_next - ask_0); upper bound on the 20 s drift at this cadence)",
        "",
        "| side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for side in ("yes_ask", "no_ask", "both_sides"):
        x = npd[side]
        lines.append(
            f"| {side} | {x['n']} | {x['p50']} | {x['p90']} | {x['p95']} | {x['max']} | "
            f"{x['share_gt_0']} | {x['share_gt_1c']} |"
        )
    lines += [
        "",
        f"- gap from the decision poll to the next poll (s): {npd['gap_s']}",
        f"- signed next-poll drift, YES ask: {rep['signed_next_poll']['yes_ask']}",
        f"- signed next-poll drift, NO ask: {rep['signed_next_poll']['no_ask']}",
        "",
        "## Recommendation",
        "",
        f"- p90 at the primary {rep['primary_window_s']} s window (both sides): {rep['p90_primary_window']}",
        f"- p90 next poll (both sides): {rep['p90_next_poll']}",
        f"- p90 used: {rep['p90_primary']} (basis: {rep['p90_basis']})",
        f"- p90 exceeds 1c: {rep['p90_exceeds_1c']}",
        f"- recommended adverse_fill = max(0.01, ceil_to_cent(p90)) = {rep['recommended_adverse_fill']}",
        "",
        "Per city: " + json.dumps(rep["per_city"], sort_keys=True),
        "",
    ]
    if rep["examples_gt_1c"]:
        lines += ["## Examples of drift > 1c", ""]
        for e in rep["examples_gt_1c"]:
            lines.append(
                f"- {e['market']} {e['hour_utc']} {e['side']} {int(e['window_s'])}s: "
                f"{e['ask_0']} -> {e['ask_max']} (+{e['drift']})"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", nargs="*", default=[], help="local data-log CSV(s)")
    ap.add_argument("--url", default=None, help="e.g. http://maia.local:8050/api/logs/data")
    ap.add_argument("--cache", default=None, help="CSV to accumulate fetched rows into (with --url)")
    ap.add_argument("--collect-seconds", type=float, default=0.0, help="poll --url this long")
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--series-prefix", default="KXHIGH")
    ap.add_argument("--windows", default="20,60", help="comma-separated seconds; first is primary")
    ap.add_argument("--max-decision-lag", type=float, default=DEFAULT_MAX_DECISION_LAG)
    ap.add_argument("--tz", default="UTC", help="zone of naive tape timestamps (deploy/pi: UTC)")
    ap.add_argument("--date", default=None, help="report label (default: UTC date of the last row)")
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "reports", "factory"))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = list(args.csv)
    if args.url:
        if not args.cache:
            print("measure_fill_realism: --url needs --cache", file=sys.stderr)
            return 2
        stats = collect(args.url, args.cache, args.collect_seconds, args.poll_interval)
        print(f"collected from {args.url}: {stats}")
        paths.append(args.cache)
    if not paths:
        print("measure_fill_realism: give --csv and/or --url", file=sys.stderr)
        return 2
    if args.tz.upper() == "UTC":
        tz = timezone.utc
    else:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(args.tz)  # type: ignore[assignment]
    rows = read_csv_rows(paths)
    windows = [float(w) for w in args.windows.split(",") if w.strip()]
    rep = analyse(
        rows,
        series_prefix=args.series_prefix,
        windows=windows,
        max_decision_lag=args.max_decision_lag,
        tz=tz,
    )
    rep["inputs"] = {"csv": paths, "url": args.url}
    label = args.date or (
        rep["tape"]["last_ts"][:10] if rep["tape"]["last_ts"] else "undated"
    )
    out_dir = Path(args.out_dir)
    write_json(out_dir / f"fill_realism_{label}.json", rep)
    write_text(out_dir / f"fill_realism_{label}.md", render_markdown(rep, label))
    print(rep["statement"])
    print(f"-> {out_dir / f'fill_realism_{label}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
