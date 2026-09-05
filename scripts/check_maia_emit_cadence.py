#!/usr/bin/env python
"""Verify the F3 maia shadow-deploy criterion over HTTP GET only (no ssh).

PRD_STRATEGY_FACTORY.md Phase F3 exit criterion: *"On maia: EMIT lines at :00
UTC only, each with exactly one EXECUTED or REJECT line; ``limit_price`` =
quote + 0.01."*  In shadow mode the one outcome line per EMIT is
``[Risk] REJECT ... reason=GENOME_SHADOW`` (the bot logs the EMIT exactly as for
other strategies, then rejects GENOME_SHADOW and never reaches
``_process_signals``).

    python scripts/check_maia_emit_cadence.py                       # http://maia.local:8050
    python scripts/check_maia_emit_cadence.py --url http://maia.local:8050 --lines 500
    python scripts/check_maia_emit_cadence.py --file logs/money_printer_20260905_000001.log

Reads ``GET /api/logs/tail?pattern=money_printer_*.log&lines=N`` (N <= 500, the
newest log file) and, for the quote check, ``GET /api/logs/data`` (the last 100
rows of the dashboard data-log CSV). Log timestamps are the container clock,
which is pinned ``TZ=UTC`` (deploy/pi/Dockerfile + compose), so ``:00`` is UTC.

The limit-price check uses, in order: ``quote=<x>`` (and ``limit=<y>`` when
present, else ``price=``) on the EMIT line -- the bot writes both on the shadow
EMIT and GENOME_SHADOW lines -- or on any line for the same symbol within the
same log second (the strategy's ``[Genome] DECIDE ... quote= limit=`` line,
which paper mode relies on because the protected mixin's EMIT carries no
quote); otherwise the traded-side ask from the data-log row for that symbol
nearest at-or-before the EMIT timestamp; otherwise the EMIT is UNVERIFIED.
A run with EMITs but ZERO verified limit prices is NOT a pass (F3 red team,
2026-09-05): verdict ``UNVERIFIED``, exit 3, unless ``--allow-unverified``
downgrades it to a reported PASS. Stdlib only, so it runs anywhere.

Exit codes: 0 PASS, 1 FAIL, 3 NO_EMIT (no genome EMIT lines in the window yet)
or UNVERIFIED (EMITs present but no verified limit price).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

LINE_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?P<level>\w+)\s*\| (?P<msg>.*)$")
EMIT_RE = re.compile(
    r"\[Signal\] EMIT strategy=(?P<strategy>.+?) symbol=(?P<symbol>\S+) side=(?P<side>\S+) "
    r"contract=(?P<contract>\S+) price=(?P<price>\S+) qty=(?P<qty>\S+) confidence=(?P<conf>\S+)"
)
EXEC_RE = re.compile(r"\[Signal\] EXECUTED strategy=(?P<strategy>.+?) symbol=(?P<symbol>\S+) ")
REJECT_RE = re.compile(r"\[Risk\] REJECT strategy=(?P<strategy>.+?) symbol=(?P<symbol>\S+) reason=(?P<reason>\S+)")
QUOTE_RE = re.compile(r"\bquote=(?P<quote>\d+(?:\.\d+)?)")
LIMIT_RE = re.compile(r"\blimit=(?P<limit>\d+(?:\.\d+)?)")
SYMBOL_IN_LINE_RE = re.compile(r"symbol=(?P<symbol>\S+)")

EXIT_PASS, EXIT_FAIL, EXIT_NO_EMIT = 0, 1, 3

# Strategy-internal skip diagnostics (``[] + log_rejection(CODE)`` on every skip,
# FACTORY_ARCHITECTURE section 1.2). They are NOT FR-0.4 outcomes of an EMIT --
# a market the genome already traded logs GENOME_ALREADY_TRADED every later hour
# -- so they never pair with an EMIT. GENOME_SHADOW (bot-side, one per EMIT) and
# every risk-manager reason are outcomes.
STRATEGY_SKIP_CODES = {
    "GENOME_NO_VINTAGE", "GENOME_MASK_FALSE", "GENOME_ALREADY_TRADED", "GENOME_FEE_MISMATCH",
    "GENOME_NOT_TOP_OF_HOUR", "GENOME_NOT_EXECUTABLE", "GENOME_SIGMA_CAP", "GENOME_MISSED_HOUR",
}
# A paper-mode EMIT (mixin line, no quote=) is paired with the strategy's own
# ``[Genome] DECIDE ... quote= limit=`` line for the same symbol within this
# many seconds (nearest wins); the two are logged microseconds apart but may
# straddle a second boundary.
DECIDE_PAIR_TOLERANCE_S = 2.0


def _get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (LAN GET)
        return resp.read()


def fetch_log_tail(base_url: str, pattern: str, lines: int, timeout: float) -> str:
    q = urllib.parse.urlencode({"pattern": pattern, "lines": min(max(lines, 1), 500)})
    payload = json.loads(_get(f"{base_url.rstrip('/')}/api/logs/tail?{q}", timeout))
    if not payload.get("ok"):
        raise RuntimeError(f"/api/logs/tail: {payload.get('error')}")
    return payload.get("content", "")


def fetch_data_log(base_url: str, timeout: float) -> List[Dict[str, Any]]:
    rows = json.loads(_get(f"{base_url.rstrip('/')}/api/logs/data", timeout))
    return rows if isinstance(rows, list) else []


def parse_lines(text: str) -> List[Dict[str, Any]]:
    out = []
    for raw in text.splitlines():
        m = LINE_RE.match(raw.rstrip())
        if not m:
            continue
        out.append({"ts": datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"),
                    "level": m.group("level"), "msg": m.group("msg")})
    return out


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _quote_from_data_log(rows: List[Dict[str, Any]], symbol: str, contract: str, ts: datetime) -> Optional[float]:
    best = None
    for row in rows:
        sym = str(row.get("Symbol", row.get("symbol", "")))
        if not sym.startswith(symbol):
            continue
        rts = _parse_ts(row.get("Timestamp", row.get("timestamp", "")))
        if rts is None or rts > ts:
            continue
        if best is None or rts > best[0]:
            best = (rts, row)
    if best is None:
        return None
    row = best[1]
    keys = ("No Ask", "no_ask", "NoAsk") if contract.upper() == "NO" else ("Ask", "ask", "yes_ask")
    for k in keys:
        if k in row and str(row[k]).strip() not in ("", "None", "nan"):
            try:
                return float(row[k])
            except ValueError:
                return None
    if contract.upper() == "NO":
        # Fall back to 1 - yes bid when no NO-side column is logged.
        for k in ("Bid", "bid", "yes_bid"):
            if k in row and str(row[k]).strip() not in ("", "None", "nan"):
                try:
                    return round(1.0 - float(row[k]), 4)
                except ValueError:
                    return None
    return None


def evaluate(entries: List[Dict[str, Any]], *, strategy_substr: str, adverse_fill: float,
             data_rows: Optional[List[Dict[str, Any]]] = None, tolerance: float = 0.0051,
             allow_unverified: bool = False) -> Dict[str, Any]:
    emits: List[Dict[str, Any]] = []
    outcomes: Dict[str, int] = {}
    skips: Dict[str, int] = {}
    for i, e in enumerate(entries):
        m = EMIT_RE.search(e["msg"])
        if m and strategy_substr.lower() in m.group("strategy").lower():
            emits.append({"index": i, "ts": e["ts"], **m.groupdict(), "outcomes": []})
            continue
        m = EXEC_RE.search(e["msg"]) or REJECT_RE.search(e["msg"])
        if m and strategy_substr.lower() in m.group("strategy").lower():
            code = m.groupdict().get("reason", "EXECUTED")
            if code in STRATEGY_SKIP_CODES:
                skips[code] = skips.get(code, 0) + 1
                continue
            outcomes[code] = outcomes.get(code, 0) + 1
            # Resolve against the most recent unresolved EMIT for this symbol.
            for em in reversed(emits):
                if em["symbol"] == m.group("symbol") and em["index"] < i:
                    em["outcomes"].append(code)
                    break

    off_hour = [f"{em['ts']:%Y-%m-%d %H:%M:%S} {em['symbol']}" for em in emits if em["ts"].minute != 0]
    no_outcome = [f"{em['ts']:%H:%M:%S} {em['symbol']}" for em in emits if len(em["outcomes"]) == 0]
    multi_outcome = [f"{em['ts']:%H:%M:%S} {em['symbol']} {em['outcomes']}" for em in emits if len(em["outcomes"]) > 1]
    executed = [f"{em['ts']:%H:%M:%S} {em['symbol']}" for em in emits if "EXECUTED" in em["outcomes"]]

    price_ok = price_bad = 0
    unverified: List[str] = []
    bad_examples: List[str] = []
    for em in emits:
        quote = None
        limit = None
        msg0 = entries[em["index"]]["msg"]
        qm = QUOTE_RE.search(msg0)
        if qm:
            quote = float(qm.group("quote"))
            lm = LIMIT_RE.search(msg0)
            limit = float(lm.group("limit")) if lm else None
        if quote is None:
            best: Optional[Tuple[float, str]] = None
            for e in entries:
                delta = abs((e["ts"] - em["ts"]).total_seconds())
                if delta > DECIDE_PAIR_TOLERANCE_S:
                    continue
                sm = SYMBOL_IN_LINE_RE.search(e["msg"])
                qm = QUOTE_RE.search(e["msg"])
                if qm and sm and sm.group("symbol") == em["symbol"]:
                    if best is None or delta < best[0]:
                        best = (delta, e["msg"])
            if best is not None:
                quote = float(QUOTE_RE.search(best[1]).group("quote"))
                lm = LIMIT_RE.search(best[1])
                limit = float(lm.group("limit")) if lm else None
        if quote is None and data_rows:
            quote = _quote_from_data_log(data_rows, em["symbol"], em["contract"], em["ts"])
        if quote is None:
            unverified.append(f"{em['ts']:%H:%M:%S} {em['symbol']}")
            continue
        try:
            price = float(em["price"])
        except ValueError:
            price_bad += 1
            bad_examples.append(f"{em['symbol']} price={em['price']!r}")
            continue
        if limit is not None and abs(limit - price) > tolerance:
            # the line's own limit= disagrees with the EMIT price: never a pass
            price_bad += 1
            bad_examples.append(f"{em['ts']:%H:%M:%S} {em['symbol']} price={price} limit={limit}")
            continue
        if abs(price - (quote + adverse_fill)) <= tolerance:
            price_ok += 1
        else:
            price_bad += 1
            bad_examples.append(f"{em['ts']:%H:%M:%S} {em['symbol']} price={price} quote={quote} "
                                f"expected={quote + adverse_fill:.4f}")

    if not emits:
        verdict = "NO_EMIT"
    elif off_hour or no_outcome or multi_outcome or price_bad:
        verdict = "FAIL"
    elif price_ok == 0 and not allow_unverified:
        verdict = "UNVERIFIED"  # no verified limit prices: not evidence for the third clause
    else:
        verdict = "PASS"
    return {
        "strategy_filter": strategy_substr,
        "adverse_fill": adverse_fill,
        "n_lines": len(entries),
        "window": {"first": entries[0]["ts"].isoformat() if entries else None,
                   "last": entries[-1]["ts"].isoformat() if entries else None},
        "n_emit": len(emits),
        "emit_off_hour": off_hour,
        "emit_without_outcome": no_outcome,
        "emit_multiple_outcomes": multi_outcome,
        "emit_executed": executed,
        "outcome_codes": dict(sorted(outcomes.items())),
        "strategy_skip_codes": dict(sorted(skips.items())),
        "limit_price": {"verified_ok": price_ok, "verified_bad": price_bad,
                        "unverified": unverified, "bad_examples": bad_examples,
                        "allow_unverified": bool(allow_unverified)},
        "verdict": verdict,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default="http://maia.local:8050", help="dashboard base URL (LAN)")
    p.add_argument("--pattern", default="money_printer_*.log", help="/api/logs/tail file glob")
    p.add_argument("--lines", type=int, default=500, help="tail length (server caps at 500)")
    p.add_argument("--file", default=None, help="parse a local log file instead of fetching")
    p.add_argument("--data-log", default=None, help="local data-log CSV for the quote check")
    p.add_argument("--no-data-log", action="store_true", help="skip /api/logs/data")
    p.add_argument("--strategy", default="Genome", help="substring selecting the strategy's lines")
    p.add_argument("--adverse-fill", type=float, default=0.01)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true", help="print only the verdict JSON")
    p.add_argument("--allow-unverified", action="store_true",
                   help="report PASS even when no EMIT has a verifiable limit price (dry runs only)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        source = args.file
    else:
        text = fetch_log_tail(args.url, args.pattern, args.lines, args.timeout)
        source = f"{args.url}/api/logs/tail?pattern={args.pattern}&lines={args.lines}"
    rows: Optional[List[Dict[str, Any]]] = None
    if args.data_log:
        with open(args.data_log, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(io.StringIO(fh.read())))
    elif not args.file and not args.no_data_log:
        try:
            rows = fetch_data_log(args.url, args.timeout)
        except Exception as exc:  # noqa: BLE001
            rows = None
            print(f"[cadence] /api/logs/data unavailable ({exc}); quote check limited to log lines",
                  file=sys.stderr)
    result = evaluate(parse_lines(text), strategy_substr=args.strategy,
                      adverse_fill=args.adverse_fill, data_rows=rows,
                      allow_unverified=bool(args.allow_unverified))
    result["source"] = source
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.json:
        print(f"[cadence] verdict: {result['verdict']} ({result['n_emit']} EMIT lines from "
              f"{result['window']['first']} to {result['window']['last']})", file=sys.stderr)
    if result["verdict"] == "UNVERIFIED":
        print("[cadence] no verified limit prices: every EMIT lacks a quote= (log line or data-log row); "
              "pass --allow-unverified to downgrade", file=sys.stderr)
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL}.get(result["verdict"], EXIT_NO_EMIT)


if __name__ == "__main__":
    sys.exit(main())
