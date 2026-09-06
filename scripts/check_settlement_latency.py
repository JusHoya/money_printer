#!/usr/bin/env python3
"""check_settlement_latency.py -- do maia's weather positions settle within 3 days? (F4 exit criterion 4)

PRD_STRATEGY_FACTORY Phase F4: *"maia positions settle via ``reconcile_weather.py``
within 3 days"*. This script measures that from the sandbox's own record, per
settled KXHIGH position and per strategy, and prints PASS/FAIL against
``--max-days`` (default 3). It reads maia over HTTP (no ssh to the Pi exists
from the dev box) or the state/journal files.

WHAT IS MEASURED
----------------
For every journal row closed by settlement (``close_reason == EXPIRATION`` with
``settlement_outcome`` recorded, the gate's own admission) on a ``KXHIGH*``
market::

    market_close_utc = src.core.weather_settlement.settlement_close_for(symbol):
                       local midnight AFTER the event date at the settlement
                       station (~04:00Z NY/MIA, 05:00Z CHI, 07:00Z LAX in
                       summer) -- the runtime's one definition of a weather
                       contract's expiry, the instant the exchange stamps as
                       expiration_time (a fixed-offset table is the fallback
                       when src.core is not importable)
    settled_utc      = the row's exit_time (the sandbox clock is UTC; a naive
                       stamp is read as UTC)
    gap              = settled_utc - market_close_utc

A row without ``target_date`` (journal rows older than the field) takes the
ticker's own event-date label (``settlement_date_for``), which is the same
fallback ``scripts/gate.py`` applies; it is never skipped for that reason.

A position PASSES when ``gap <= max_days``. A negative gap (settled before its
market closed) is reported as an anomaly, never as a pass. Open positions whose
market closed more than ``max_days`` ago and are still open are OVERDUE and
fail the check too -- a settlement that never happens has no journal row to
measure, so the open book has to be read as well.

PER STRATEGY, BECAUSE THE GENOME HAS NO ROWS
--------------------------------------------
The deployed genome runs in shadow: it books nothing, so it has NO settled
positions and this check can say nothing about it beyond that fact. The report
therefore lists every strategy separately -- ``Meteorologist V2`` has settled
positions since 2026-09-01, ``Genome <id8>`` has zero -- and names the genome's
line ``NO EVIDENCE (0 settled; shadow books nothing)`` rather than folding it
into a pass. The registered deviation in PRD_STRATEGY_FACTORY Phase F4 says
exactly that the shadow run accrues nothing toward the gate.

WHAT THIS IS EVIDENCE OF
------------------------
The settlement rows carry ``settlement_high`` / ``settlement_rule`` /
``settlement_outcome`` / ``settlement_error``: the sandbox settled the position
against the station's CLI high through ``src.core.weather_settlement`` at
expiration. ``scripts/reconcile_weather.py`` (the daily 13:30Z
``mp-reconcile-weather.timer`` on maia) is the *cross-check* of those rows
against IEM CLI and Kalshi's published result; it is read-only and writes its
report under ``data/``, which no HTTP route serves, so its own run log is NOT
visible from here. What IS visible is its subject: every settled row's
settlement fields and the time the settlement landed. Read the PASS as "the
sandbox's settlements land within the bound", not as "the reconcile timer
fired" -- confirm the timer on maia with ``systemctl list-timers mp-reconcile-weather.timer``.

USAGE
-----
    python scripts/check_settlement_latency.py --url http://maia.local:8050
    python scripts/check_settlement_latency.py --journal data/trade_journal.jsonl --state data/exchange_state.json
    ... [--max-days 3] [--genome 0c4b20502f2daf65] [--out reports/factory/settlement_latency_<date>.json]

Exit codes: 0 PASS (every measured position within the bound, nothing overdue),
1 FAIL, 2 usage/input error, 3 NO DATA (no settled KXHIGH rows at all).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)

DEFAULT_URL = "http://maia.local:8050"
DEFAULT_MAX_DAYS = 3.0
HTTP_CAP = 500
MARKET_FAMILY = "KXHIGH"
#: FALLBACK ONLY (used when ``src.core.weather_settlement`` cannot be imported):
#: (hour, minute) UTC on target_date + 1 at which the city's KXHIGH markets close --
#: local midnight after the event date in summer time (04:00Z NY/MIA, 05:00Z CHI,
#: 07:00Z LAX), which is what the exchange stamps as ``expiration_time`` and where
#: maia's settlement rows land (04:00:01Z, 05:00:10Z, 07:00:00Z on 2026-09-05/06).
CITY_CLOSE_UTC: Dict[str, Tuple[int, int]] = {"NY": (4, 0), "MIA": (4, 0), "CHI": (5, 0), "LAX": (7, 0)}
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_NO_DATA = 0, 1, 2, 3


class LatencyInputError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (LAN GET)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise LatencyInputError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LatencyInputError(f"{url}: {exc}") from exc


def load_http(base_url: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    notes: List[str] = []
    q = urllib.parse.urlencode({"last_n": HTTP_CAP})
    journal = _get_json(f"{base}/api/journal?{q}", timeout)
    rows = [r for r in (journal or {}).get("trades") or [] if isinstance(r, dict)] if isinstance(journal, dict) else []
    if isinstance(journal, dict) and int(journal.get("count") or 0) >= HTTP_CAP:
        notes.append(f"/api/journal returned its {HTTP_CAP}-row cap; older rows not seen")
    genome = _get_json(f"{base}/api/genome", timeout) or {}
    gblock = genome.get("genome") if isinstance(genome, dict) else None
    positions: List[Dict[str, Any]] = []
    closed = _get_json(f"{base}/api/closed_trades?{q}", timeout)
    if isinstance(closed, dict) and closed.get("ok"):
        positions = [p for p in closed.get("positions") or [] if isinstance(p, dict)]
        pos_source = "/api/closed_trades"
    else:
        # /api/status appends an equity point per call (a side effect); it is the
        # fallback for an image that predates /api/closed_trades, read ONCE.
        status = _get_json(f"{base}/api/status", timeout) or {}
        positions = [p for p in (status.get("positions") or []) if isinstance(p, dict)] if isinstance(status, dict) else []
        pos_source = "/api/status (fallback; /api/closed_trades absent on this image)"
        notes.append("open positions read from /api/status because /api/closed_trades is absent on this sandbox image")
    return {
        "source": {"mode": "http", "base_url": base, "positions_from": pos_source},
        "journal_rows": rows,
        "positions": positions,
        "genome": gblock if isinstance(gblock, dict) else {},
        "notes": notes,
    }


def load_files(journal_path: str, state_path: Optional[str]) -> Dict[str, Any]:
    p = Path(journal_path)
    if not p.exists():
        raise LatencyInputError(f"journal not found: {p}")
    rows: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    positions: List[Dict[str, Any]] = []
    notes: List[str] = []
    if state_path:
        sp = Path(state_path)
        if not sp.exists():
            raise LatencyInputError(f"exchange state not found: {sp}")
        try:
            with open(sp, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except ValueError as exc:
            raise LatencyInputError(f"{sp}: not valid JSON ({exc})") from exc
        positions = [q for q in (state.get("positions") or []) if isinstance(q, dict)] if isinstance(state, dict) else []
    else:
        notes.append("no --state: open positions not checked for overdue settlement")
    return {
        "source": {"mode": "files", "journal": journal_path, "state": state_path},
        "journal_rows": rows,
        "positions": positions,
        "genome": {},
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------
def city_of(symbol: str) -> Optional[str]:
    s = str(symbol or "")
    if not s.startswith(MARKET_FAMILY):
        return None
    rest = s[len(MARKET_FAMILY):]
    city = rest.split("-", 1)[0]
    return city or None


def _runtime_close(symbol: str) -> Tuple[Optional[datetime], Optional[str]]:
    """``(close instant UTC, ticker's target_date)`` from ``src.core.weather_settlement`` -- the
    runtime's ONE definition of a weather contract's expiry (``settlement_close_for``); ``(None, None)``
    when the module is unavailable or the symbol is not a registered weather series."""
    try:
        from src.core.weather_settlement import settlement_close_for, settlement_date_for
    except Exception:  # pragma: no cover - src.core not importable (bare checkout)
        return None, None
    try:
        close = settlement_close_for(symbol)
        label = settlement_date_for(symbol)
    except Exception:
        return None, None
    if close is not None and close.tzinfo is not None:
        close = close.astimezone(timezone.utc)
    return close, (label.isoformat() if label is not None else None)


def market_close_utc(symbol: str, target_date: Optional[str]) -> Optional[datetime]:
    """When the city-day's markets close: the runtime's ``settlement_close_for`` when it knows the
    symbol (local midnight after the event date, DST-aware), else the fixed-offset table above."""
    close, _label = _runtime_close(symbol)
    if close is not None:
        return close
    city = city_of(symbol)
    if city is None or city not in CITY_CLOSE_UTC or not target_date:
        return None
    try:
        d = date.fromisoformat(str(target_date)[:10])
    except ValueError:
        return None
    hh, mm = CITY_CLOSE_UTC[city]
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1, hours=hh, minutes=mm)


def target_date_of(row: Mapping[str, Any]) -> Optional[str]:
    """The row's ``target_date``, else the ticker's own event-date label (gate.py's fallback rule)."""
    td = row.get("target_date")
    if isinstance(td, str) and td.strip():
        return td.strip()[:10]
    _close, label = _runtime_close(str(row.get("symbol") or ""))
    return label


def parse_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_settled(row: Mapping[str, Any]) -> bool:
    if str(row.get("close_reason") or "").upper() != "EXPIRATION":
        return False
    return str(row.get("settlement_outcome") or "").lower() in ("yes", "no")


def measure(
    journal_rows: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    *,
    max_days: float = DEFAULT_MAX_DAYS,
    now: Optional[datetime] = None,
    genome_strategy: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure: the per-position rows, per-strategy summaries and the overall verdict."""
    now = now or datetime.now(timezone.utc)
    bound = timedelta(days=float(max_days))
    settled: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in journal_rows:
        symbol = str(row.get("symbol") or "")
        if not symbol.startswith(MARKET_FAMILY):
            continue
        if not is_settled(row):
            continue
        strategy = str(row.get("strategy_name") or "")
        td = target_date_of(row)
        close = market_close_utc(symbol, td)
        settled_at = parse_utc(row.get("exit_time") or row.get("close_time"))
        if close is None or settled_at is None:
            skipped.append({
                "symbol": symbol, "strategy": strategy, "target_date": td,
                "reason": "no target_date and no event-date label" if close is None else "no exit_time",
            })
            continue
        gap = settled_at - close
        gap_days = gap.total_seconds() / 86400.0
        settled.append({
            "symbol": symbol,
            "strategy": strategy,
            "target_date": str(td),
            "market_close_utc": close.isoformat(),
            "settled_utc": settled_at.isoformat(),
            "gap_days": round(gap_days, 4),
            "within_bound": (0.0 <= gap_days <= float(max_days)),
            "anomaly": "settled before market close" if gap_days < 0 else None,
            "settlement_outcome": row.get("settlement_outcome"),
            "settlement_high": row.get("settlement_high"),
            "settlement_error": row.get("settlement_error"),
            "exit_price": row.get("exit_price"),
        })
    overdue: List[Dict[str, Any]] = []
    open_checked = 0
    for pos in positions:
        symbol = str(pos.get("symbol") or "")
        if not symbol.startswith(MARKET_FAMILY):
            continue
        open_checked += 1
        # the position's own stamp first (it IS the exchange's close), else the runtime rule
        close = parse_utc(pos.get("expiration_time")) or market_close_utc(symbol, target_date_of(pos))
        if close is None:
            continue
        age_past_close = (now - close).total_seconds() / 86400.0
        if age_past_close > float(max_days):
            overdue.append({
                "symbol": symbol,
                "strategy": str(pos.get("strategy") or pos.get("strategy_name") or ""),
                "market_close_utc": close.isoformat(),
                "days_past_close": round(age_past_close, 3),
            })
    per_strategy: Dict[str, Dict[str, Any]] = {}
    by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in settled:
        by[r["strategy"]].append(r)
    names = set(by) | {o["strategy"] for o in overdue}
    if genome_strategy:
        names.add(genome_strategy)
    for name in sorted(names):
        rows = by.get(name, [])
        gaps = [r["gap_days"] for r in rows]
        n_over = sum(1 for o in overdue if o["strategy"] == name)
        n_fail = sum(1 for r in rows if not r["within_bound"])
        if not rows and not n_over:
            verdict = "NO EVIDENCE (0 settled" + ("; shadow books nothing)" if name == genome_strategy else ")")
        else:
            verdict = "PASS" if (n_fail == 0 and n_over == 0) else "FAIL"
        per_strategy[name] = {
            "settled": len(rows),
            "within_bound": len(rows) - n_fail,
            "max_gap_days": max(gaps) if gaps else None,
            "mean_gap_days": (math.fsum(gaps) / len(gaps)) if gaps else None,
            "overdue_open": n_over,
            "verdict": verdict,
            "is_genome": bool(genome_strategy and name == genome_strategy),
        }
    measured = len(settled)
    n_fail = sum(1 for r in settled if not r["within_bound"])
    if measured == 0 and not overdue:
        overall = "NO DATA"
    else:
        overall = "PASS" if (n_fail == 0 and not overdue) else "FAIL"
    return {
        "max_days": float(max_days),
        "now_utc": now.isoformat(),
        "verdict": overall,
        "measured": measured,
        "failing": n_fail,
        "overdue_open": overdue,
        "open_positions_checked": open_checked,
        "skipped": skipped,
        "per_strategy": per_strategy,
        "positions": settled,
        "genome_strategy": genome_strategy,
    }


def render_text(rep: Mapping[str, Any], *, source: Mapping[str, Any], notes: Sequence[str]) -> str:
    out: List[str] = []
    src = source.get("base_url") or source.get("journal") or "?"
    out.append(f"settlement latency vs {rep['max_days']:g} days -- {src} -- {rep['verdict']} "
               f"({rep['measured']} settled KXHIGH positions measured, {rep['failing']} over the bound, "
               f"{len(rep['overdue_open'])} overdue open)")
    for name, s in rep["per_strategy"].items():
        tag = " [genome]" if s.get("is_genome") else ""
        if s["settled"]:
            out.append(f"  {name}{tag}: {s['verdict']} -- {s['within_bound']}/{s['settled']} within bound, "
                       f"max gap {s['max_gap_days']:.3f} d, mean {s['mean_gap_days']:.3f} d, overdue open {s['overdue_open']}")
        else:
            out.append(f"  {name}{tag}: {s['verdict']}; overdue open {s['overdue_open']}")
    for r in rep["positions"]:
        flag = "ok " if r["within_bound"] else "OVER"
        anomaly = f" !! {r['anomaly']}" if r.get("anomaly") else ""
        out.append(f"    {flag} {r['symbol']:<28} {r['strategy']:<18} target {r['target_date']} close {r['market_close_utc'][:16]}Z "
                   f"settled {r['settled_utc'][:19]}Z gap {r['gap_days']:+.3f} d outcome={r['settlement_outcome']} "
                   f"high={r['settlement_high']}{anomaly}")
    for o in rep["overdue_open"]:
        out.append(f"    OVERDUE open {o['symbol']} ({o['strategy']}) closed {o['market_close_utc'][:16]}Z, "
                   f"{o['days_past_close']:.2f} d ago, still unsettled")
    for s in rep["skipped"]:
        out.append(f"    skipped {s['symbol']} ({s['strategy']}): {s['reason']}")
    for n in notes:
        out.append(f"  note: {n}")
    out.append("  evidence: settlement_outcome/settlement_high on each row = the sandbox's in-process CLI settlement; "
               "reconcile_weather.py (daily 13:30Z timer on maia) cross-checks them and leaves no HTTP trace -- "
               "confirm the timer on maia with `systemctl list-timers mp-reconcile-weather.timer`")
    return "\n".join(out)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=None, help=f"sandbox base URL (e.g. {DEFAULT_URL}); reads /api/journal, /api/genome, /api/closed_trades")
    ap.add_argument("--journal", default=None, help="trade_journal.jsonl (file mode)")
    ap.add_argument("--state", default=None, help="exchange_state.json (file mode; open positions)")
    ap.add_argument("--max-days", type=float, default=DEFAULT_MAX_DAYS)
    ap.add_argument("--genome", default=None, help="genome id whose strategy line must appear even with 0 rows (HTTP reads it from /api/genome)")
    ap.add_argument("--http-timeout", type=float, default=15.0)
    ap.add_argument("--out", default=None, help="write the JSON report here")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if bool(args.url) == bool(args.journal):
        print("check_settlement_latency: pass exactly one of --url or --journal", file=sys.stderr)
        return EXIT_USAGE
    try:
        got = load_http(args.url, timeout=args.http_timeout) if args.url else load_files(args.journal, args.state)
    except LatencyInputError as exc:
        print(f"check_settlement_latency: {exc}", file=sys.stderr)
        return EXIT_USAGE
    gid = (args.genome or (got.get("genome") or {}).get("genome_id") or "").strip()
    genome_strategy = (got.get("genome") or {}).get("strategy") or (f"Genome {gid[:8]}" if gid else None)
    rep = measure(got["journal_rows"], got["positions"], max_days=args.max_days, genome_strategy=genome_strategy)
    rep["source"] = got["source"]
    rep["notes"] = list(got.get("notes") or [])
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rep, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    else:
        print(render_text(rep, source=got["source"], notes=rep["notes"]))
    if rep["verdict"] == "NO DATA":
        return EXIT_NO_DATA
    return EXIT_PASS if rep["verdict"] == "PASS" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
