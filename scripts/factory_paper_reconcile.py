#!/usr/bin/env python3
"""factory_paper_reconcile.py -- weekly lab-vs-paper reconciliation of a promoted genome.

FACTORY_ARCHITECTURE section 9 item 7 / FACTORY_ROADMAP section F3 item 7 /
PRD_STRATEGY_FACTORY FR-F3.4: once a promoted genome paper-trades in the maia
sandbox, every week the sandbox record is re-read against the lab that produced
the genome. Two questions, answered separately and never merged:

1. **Re-pricing.** Is each sandbox fill worth what the lab formula says it is?
   Every fill is re-priced as the lab priced its frame rows::

       quote      = logged limit_price - adverse_fill_at_entry   (the promoted
                    strategy's limit is quote + adverse_fill and the simulated
                    exchange fills at limit, so the quote is recovered exactly)
       price_lab  = quote + adverse_fill_lab                      (--adverse-fill;
                    equals the booked price unless the fill-realism study raised it)
       fee_lab    = src.factory.fees.fee_per_contract(price_lab, ts, series,
                    contracts=C, is_maker=False)                  (the frame's own
                    fee function: taker, ceil-to-cent on a C=20 order, per contract)
       realized_lab per contract = won - price_lab - fee_lab      (held to settlement)

   and set against the sandbox's own per-contract realized
   ``(pnl - entry_fee) / quantity`` at ACTUAL quantity (Kelly sizing differs
   from the 20-contract frame assumption, CONTRA 10, so the fee per contract
   differs by the cent-ceiling; the report shows both).

2. **Trade set.** Is the sandbox trade set a subset of the lab trade set on
   the same dates? The lab set is the genome's ``fitness.score(F, to_mask(g,
   F)).trade_rows`` on the frozen search frame restricted to the date range.
   ``lab \\ sandbox`` are lab-admissible trades the sandbox skipped: each is
   annotated with the REJECT codes the runtime logged for that market
   (``[Risk] REJECT strategy=... symbol=... reason=CODE`` lines from
   ``risk_manager.log_rejection`` -- KELLY_ZERO, WEATHER_SLOT_FULL, cooldowns,
   allocation, GENOME_* ...). ``sandbox \\ lab`` should be EMPTY; anything
   there is a live-path/offline discrepancy and is flagged as such.

   The frozen frame ends at its cutoff (2026-07-25 for
   ``weather_2026-07-25_bfcf94654a3a``). When the requested dates are not in
   the frame the report SAYS SO (``lab_trade_set.coverage``) and falls back to
   re-pricing only plus the REJECT-code profile from the log; it does not
   pretend the frame covers dates it never saw.

PnL is read from ``closed_trades`` / the journal, never from equity (the
UTC-midnight reset double-subtracts).

USAGE
-----
    python scripts/factory_paper_reconcile.py \\
        --promoted configs/factory/promoted/<id>.json \\
        --journal data/trade_journal.jsonl --state data/exchange_state.json \\
        --log logs/money_printer_*.log --from 2026-09-08 --to 2026-09-14

Writes ``reports/factory/paper_reconcile_<from>_<to>.json`` and ``.md``.
Exit 0 when ``sandbox \\ lab`` is empty (or the frame does not cover the dates),
1 when it is not, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.fee_calculator import series_ticker_from_symbol  # noqa: E402
from src.factory import fees as fees_mod  # noqa: E402
from src.factory.report import write_json, write_text  # noqa: E402


def _load_gate_module():
    """``scripts/gate.py`` as a module (shared journal/state assembly)."""
    spec = importlib.util.spec_from_file_location(
        "mp_gate", os.path.join(_THIS_DIR, "gate.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate_module()

DEFAULT_FRAMES_DIR = os.path.join(
    REPO_ROOT, "data", "factory", "frames", "weather_2026-07-25_bfcf94654a3a"
)
MAX_ORDERABLE_PRICE = 0.99

#: ``2026-09-05 01:24:01 | INFO    | [Risk] REJECT strategy=Genome 7d857b00 symbol=KXHIGHNY-26SEP04-B84.5 reason=KELLY_ZERO k=v``
_REJECT_RE = re.compile(
    r"\[Risk\] REJECT strategy=(?P<strategy>.*?) symbol=(?P<symbol>\S+) "
    r"reason=(?P<reason>\S+)(?P<rest>.*)$"
)
_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


class ReconcileError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Promoted spec
# ---------------------------------------------------------------------------
def load_spec(path: str) -> Dict[str, Any]:
    """The promoted spec as a plain dict (``load_promoted`` when available)."""
    full = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
    if not os.path.exists(full):
        raise ReconcileError(f"promoted spec not found: {path}")
    try:
        from src.factory.promoted import PromotedSpecError, load_promoted  # type: ignore
    except ImportError:
        PromotedSpecError = None  # type: ignore[assignment]
        load_promoted = None  # type: ignore[assignment]
    if load_promoted is not None:
        try:
            spec = load_promoted(full)
            if isinstance(spec, Mapping):
                return dict(spec)
            out = {}
            for key in (
                "genome_id", "genome_json", "family", "adverse_fill", "contracts_frame",
                "fee", "mode", "spec_hash", "frame_search_sha256",
            ):
                if hasattr(spec, key):
                    val = getattr(spec, key)
                    out[key] = dict(val.__dict__) if hasattr(val, "__dict__") else val
            if out.get("genome_json") is not None:
                out["spec_source"] = "src.factory.promoted.load_promoted (content hash verified)"
                return out
        except PromotedSpecError as exc:  # type: ignore[misc]
            msg = str(exc)
            if "does not verify" in msg or "genome_id" in msg:
                raise ReconcileError(f"{path}: promoted spec changed since it was written: {msg}")
            # not a full promoted spec: fall back to the raw document below
    with open(full, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or "genome_json" not in raw:
        raise ReconcileError(f"{path}: not a promoted spec (no genome_json)")
    raw["spec_source"] = "raw promoted spec file (not a full spec; hash unverified)"
    return raw


def strategy_name_for(spec: Mapping[str, Any]) -> str:
    gid = str(spec.get("genome_id") or "")
    return f"Genome {gid[:8]}"


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
def parse_reject_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Every ``log_rejection`` line -> ``{ts, strategy, symbol, reason, context}``."""
    out: List[Dict[str, Any]] = []
    for line in lines:
        m = _REJECT_RE.search(line)
        if not m:
            continue
        ctx: Dict[str, str] = {}
        for tok in m.group("rest").split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                ctx[k] = v
        tm = _LINE_TS_RE.match(line)
        out.append(
            {
                "ts": tm.group(1).replace(" ", "T") if tm else None,
                "strategy": m.group("strategy").strip(),
                "symbol": m.group("symbol"),
                "reason": m.group("reason"),
                "context": ctx,
            }
        )
    return out


def load_reject_lines(paths: Sequence[str]) -> List[Dict[str, Any]]:
    files: List[str] = []
    for p in paths:
        files.extend(sorted(glob.glob(p)) or ([p] if os.path.exists(p) else []))
    rejects: List[Dict[str, Any]] = []
    for f in files:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            rejects.extend(parse_reject_lines(fh))
    return rejects


# ---------------------------------------------------------------------------
# Re-pricing
# ---------------------------------------------------------------------------
def _epoch(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # sandbox clock is UTC (deploy/pi TZ=UTC)
    return int(dt.timestamp())


def reprice_fill(
    fill: Mapping[str, Any],
    *,
    adverse_fill_at_entry: float,
    adverse_fill_lab: float,
    contracts: int,
    regime: fees_mod.FeeRegime,
) -> Dict[str, Any]:
    """One sandbox fill through the lab formula (see module docstring)."""
    price_booked = float(fill["entry_price"])
    quote = round(price_booked - adverse_fill_at_entry, 4)
    price_lab = round(quote + adverse_fill_lab, 10)
    ts = _epoch(fill.get("entry_time"))
    series = series_ticker_from_symbol(fill["symbol"])
    out: Dict[str, Any] = {
        "symbol": fill["symbol"],
        "target_date": fill.get("target_date"),
        "entry_time": fill.get("entry_time"),
        "contract_side": fill.get("contract_side"),
        "quantity": fill["quantity"],
        "price_booked": price_booked,
        "quote_recovered": quote,
        "adverse_fill_at_entry": adverse_fill_at_entry,
        "adverse_fill_lab": adverse_fill_lab,
        "price_lab": None,
        "fee_lab_per_contract": None,
        "realized_lab_per_contract": None,
        "sandbox_fee_per_contract": float(fill["entry_fee"]) / float(fill["quantity"]),
        "sandbox_realized_per_contract": float(fill["net_pnl"]) / float(fill["quantity"]),
        "won": bool(fill["won"]),
        "settled": True,
        "note": None,
    }
    if quote <= 0.0 or price_lab > MAX_ORDERABLE_PRICE + 1e-12:
        out["note"] = "price_lab off the orderable grid (ev_analysis.adverse_fill_price -> None)"
        return out
    if ts is None:
        out["note"] = "entry_time unparseable; fee regime lookup impossible"
        return out
    fee = float(
        fees_mod.fee_per_contract(
            [price_lab], [ts], series, contracts=contracts, is_maker=False, regime=regime
        )[0]
    )
    out["price_lab"] = price_lab
    out["fee_lab_per_contract"] = fee
    out["realized_lab_per_contract"] = (1.0 if fill["won"] else 0.0) - price_lab - fee
    out["fee_delta_per_contract_sandbox_minus_lab"] = out["sandbox_fee_per_contract"] - fee
    out["realized_delta_per_contract_sandbox_minus_lab"] = (
        out["sandbox_realized_per_contract"] - out["realized_lab_per_contract"]
    )
    return out


# ---------------------------------------------------------------------------
# Lab trade set
# ---------------------------------------------------------------------------
def lab_trade_set(
    frames_dir: str,
    genome_json: Any,
    date_from: date,
    date_to: date,
) -> Dict[str, Any]:
    """The genome's offline trade set on the frozen search frame, restricted to the dates.

    Imports the factory modules lazily (numpy/pyarrow); returns a ``coverage``
    of ``"none"`` with a reason when the frame is absent or ends before the
    range -- the caller then falls back to re-pricing only.
    """
    if not os.path.isdir(frames_dir):
        return {
            "coverage": "none",
            "reason": f"frame dir not found: {frames_dir}",
            "trades": [],
        }
    from src.factory import fitness, gen0
    from src.factory import genome as G

    fs = gen0.load_frameset(Path(frames_dir))
    F = fs.search
    frame_first, frame_last = str(F.dates[0]), str(F.dates[-1])
    if date.fromisoformat(frame_last) < date_from or date.fromisoformat(frame_first) > date_to:
        return {
            "coverage": "none",
            "reason": (
                f"frame {os.path.basename(frames_dir)} covers {frame_first}..{frame_last}; "
                f"requested {date_from.isoformat()}..{date_to.isoformat()} lies outside it"
            ),
            "frame_dates": [frame_first, frame_last],
            "trades": [],
        }
    import numpy as np

    g = G.Genome.from_json(genome_json if not isinstance(genome_json, str) else genome_json)
    in_range = np.asarray(
        [date_from <= date.fromisoformat(str(d)) <= date_to for d in F.dates], dtype=bool
    )
    date_mask = in_range[F.visible["target_date_code"]]
    mask = G.to_mask(g, F)
    res = fitness.score(F, mask, date_mask=date_mask, n_boot=100, constraints=False)
    from src.factory.columns import DIRECTION_LABELS

    trades = []
    for r in res.trade_rows.tolist():
        trades.append(
            {
                "market_ticker": str(F.markets[int(F.visible["market_code"][r])]),
                "target_date": str(F.dates[int(F.visible["target_date_code"][r])]),
                "ts_utc": int(F.visible["ts_utc"][r]),
                "direction": DIRECTION_LABELS[int(F.visible["direction_code"][r])],
                "quote": float(F.visible["quote"][r]),
                "price_paid": float(F.visible["price_paid"][r]),
                "fee_per_contract": float(F.visible["fee_per_contract"][r]),
                "realized_per_contract": float(F.hidden["realized_per_contract"][r]),
            }
        )
    covered_dates = [str(d) for d, ok in zip(F.dates, in_range) if ok]
    partial = (
        date.fromisoformat(frame_first) > date_from or date.fromisoformat(frame_last) < date_to
    )
    return {
        "coverage": "partial" if partial else "full",
        "reason": None
        if not partial
        else f"frame covers {frame_first}..{frame_last}; dates outside it are not scored",
        "frame_dates": [frame_first, frame_last],
        "frame_search_sha256": F.provenance.get("frame_sha256"),
        "dates_scored": covered_dates,
        "n_trades": int(res.trades),
        "realized_mean_per_contract": None if math.isnan(res.realized) else float(res.realized),
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
def _in_range(td: Optional[str], d0: date, d1: date) -> bool:
    if not td:
        return False
    try:
        d = date.fromisoformat(td)
    except ValueError:
        return False
    return d0 <= d <= d1


def reconcile(
    *,
    spec: Mapping[str, Any],
    journal_rows: Sequence[Mapping[str, Any]],
    closed_trades: Sequence[Mapping[str, Any]],
    open_positions: Sequence[Mapping[str, Any]],
    rejects: Sequence[Mapping[str, Any]],
    date_from: date,
    date_to: date,
    frames_dir: Optional[str],
    adverse_fill_lab: Optional[float] = None,
    contracts: Optional[int] = None,
    strategy_name: Optional[str] = None,
    regime: Optional[fees_mod.FeeRegime] = None,
) -> Dict[str, Any]:
    """Pure reconciliation (no file writes); the tests drive this."""
    strategy = strategy_name or strategy_name_for(spec)
    adverse_at_entry = float(spec.get("adverse_fill", 0.01))
    adverse_lab = float(adverse_fill_lab if adverse_fill_lab is not None else adverse_at_entry)
    C = int(contracts if contracts is not None else spec.get("contracts_frame", 20))
    regime = regime or fees_mod.load_regime()

    fills, counts = _GATE.collect_settled_trades(
        journal_rows, closed_trades, strategy_name=strategy
    )
    fills = [f for f in fills if _in_range(f["target_date"], date_from, date_to)]
    counts["settled_fills_all_dates"] = counts.pop("settled_fills")
    counts["settled_fills"] = len(fills)
    counts["fills_by_source"] = dict(Counter(f["source"] for f in fills))
    counts["fills_by_fee_source"] = dict(Counter(f["fee_source"] for f in fills))
    repriced = [
        reprice_fill(
            f,
            adverse_fill_at_entry=adverse_at_entry,
            adverse_fill_lab=adverse_lab,
            contracts=C,
            regime=regime,
        )
        for f in fills
    ]

    pending = []
    for pos in open_positions:
        if str(pos.get("strategy_name") or "") != strategy:
            continue
        td = _GATE._target_date(pos)
        if not _in_range(td, date_from, date_to):
            continue
        pending.append(
            {
                "symbol": pos.get("symbol"),
                "target_date": td,
                "entry_time": _GATE._iso(pos.get("open_time")),
                "price_booked": pos.get("entry_price"),
                "quantity": pos.get("quantity"),
                "settled": False,
            }
        )

    lab = (
        lab_trade_set(frames_dir, spec.get("genome_json"), date_from, date_to)
        if frames_dir
        else {"coverage": "none", "reason": "no --frames given", "trades": []}
    )

    sandbox_markets = {f["symbol"] for f in fills} | {p["symbol"] for p in pending}
    lab_markets = {t["market_ticker"] for t in lab["trades"]}

    strat_rejects = [
        r for r in rejects if r["strategy"] == strategy and _in_range(
            _GATE._target_date({"symbol": r["symbol"]}), date_from, date_to
        )
    ]
    rejects_by_symbol: Dict[str, List[str]] = defaultdict(list)
    for r in strat_rejects:
        rejects_by_symbol[r["symbol"]].append(r["reason"])

    lab_only = []
    for t in lab["trades"]:
        if t["market_ticker"] in sandbox_markets:
            continue
        codes = rejects_by_symbol.get(t["market_ticker"], [])
        lab_only.append(
            {
                **t,
                "reject_codes": sorted(Counter(codes).items()),
                "explained": bool(codes),
            }
        )
    sandbox_only = []
    if lab["coverage"] != "none":
        for f in fills:
            if f["symbol"] not in lab_markets:
                sandbox_only.append(
                    {
                        "symbol": f["symbol"],
                        "target_date": f["target_date"],
                        "entry_time": f["entry_time"],
                        "price_booked": f["entry_price"],
                        "flag": "NOT_IN_LAB_TRADE_SET (live-path/offline discrepancy)",
                    }
                )

    settled_lab = [r for r in repriced if r["realized_lab_per_contract"] is not None]
    summary = {
        "n_sandbox_fills_settled": len(fills),
        "n_sandbox_pending": len(pending),
        "sandbox_net_pnl": math.fsum(f["net_pnl"] for f in fills),
        "sandbox_realized_per_contract_mean": (
            math.fsum(r["sandbox_realized_per_contract"] for r in repriced) / len(repriced)
            if repriced
            else None
        ),
        "lab_repriced_realized_per_contract_mean": (
            math.fsum(r["realized_lab_per_contract"] for r in settled_lab) / len(settled_lab)
            if settled_lab
            else None
        ),
        "n_lab_trades": len(lab["trades"]),
        "n_lab_only": len(lab_only),
        "n_lab_only_explained_by_reject": sum(1 for x in lab_only if x["explained"]),
        "n_sandbox_only": len(sandbox_only),
        "sandbox_subset_of_lab": (
            None if lab["coverage"] == "none" else len(sandbox_only) == 0
        ),
        "reject_profile": dict(sorted(Counter(r["reason"] for r in strat_rejects).items())),
        "n_reject_lines_strategy": len(strat_rejects),
        "n_reject_lines_total": len(rejects),
    }
    return {
        "genome_id": spec.get("genome_id"),
        "strategy_name": strategy,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "formula": {
            "quote": "price_booked - adverse_fill_at_entry",
            "price_lab": "quote + adverse_fill_lab",
            "fee_lab": (
                f"src.factory.fees.fee_per_contract(price_lab, entry_ts, series, "
                f"contracts={C}, is_maker=False)"
            ),
            "realized_lab_per_contract": "won - price_lab - fee_lab (held to settlement)",
            "sandbox_realized_per_contract": "(pnl - entry_fee) / quantity at actual quantity",
            "pnl_source": "closed_trades / journal, never equity",
        },
        "parameters": {
            "adverse_fill_at_entry": adverse_at_entry,
            "adverse_fill_lab": adverse_lab,
            "contracts_frame": C,
            "fee_regime_sha256": regime.sha256,
            "frames_dir": frames_dir,
        },
        "summary": summary,
        "counts": counts,
        "repriced_fills": repriced,
        "pending_positions": pending,
        "lab_trade_set": {k: v for k, v in lab.items() if k != "trades"},
        "lab_only": lab_only,
        "sandbox_only": sandbox_only,
    }


def render_markdown(rep: Mapping[str, Any]) -> str:
    s = rep["summary"]
    lab = rep["lab_trade_set"]
    lines = [
        f"# Paper reconcile {rep['date_from']} .. {rep['date_to']} -- {rep['strategy_name']}",
        "",
        f"Genome `{rep['genome_id']}`; adverse_fill at entry {rep['parameters']['adverse_fill_at_entry']}, "
        f"lab {rep['parameters']['adverse_fill_lab']}; C={rep['parameters']['contracts_frame']} taker; "
        f"fee regime `{rep['parameters']['fee_regime_sha256'][:12]}`.",
        "",
        "## Re-pricing (sandbox fills through the lab formula)",
        "",
        f"- settled fills: {s['n_sandbox_fills_settled']}; pending (open) positions: {s['n_sandbox_pending']}",
        f"- sandbox net PnL (closed_trades pnl - entry_fee): {s['sandbox_net_pnl']:+.2f}",
        f"- mean realized/contract: sandbox {s['sandbox_realized_per_contract_mean']}, "
        f"lab re-priced {s['lab_repriced_realized_per_contract_mean']}",
        "",
        "| symbol | target_date | side | qty | booked | quote | price_lab | fee sandbox | fee lab | realized sandbox | realized lab |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["repriced_fills"]:
        lines.append(
            f"| {r['symbol']} | {r['target_date']} | {r['contract_side']} | {r['quantity']:.0f} | "
            f"{r['price_booked']:.2f} | {r['quote_recovered']:.2f} | {r['price_lab']} | "
            f"{r['sandbox_fee_per_contract']:.4f} | {r['fee_lab_per_contract']} | "
            f"{r['sandbox_realized_per_contract']:+.4f} | {r['realized_lab_per_contract']} |"
        )
    lines += ["", "## Lab trade set", ""]
    if lab["coverage"] == "none":
        lines.append(f"**Frame does not cover the dates**: {lab['reason']}. Re-pricing only; "
                     "the REJECT profile below is still taken from the runtime log.")
    else:
        lines.append(
            f"coverage {lab['coverage']} (frame {lab['frame_dates'][0]}..{lab['frame_dates'][1]}); "
            f"lab trades {s['n_lab_trades']}; lab-only {s['n_lab_only']} "
            f"({s['n_lab_only_explained_by_reject']} explained by a REJECT line); "
            f"sandbox-only {s['n_sandbox_only']}; sandbox subset of lab: {s['sandbox_subset_of_lab']}"
        )
        if rep["lab_only"]:
            lines += ["", "| lab market | target_date | direction | price_paid | REJECT codes |", "|---|---|---|---|---|"]
            for t in rep["lab_only"]:
                codes = ", ".join(f"{c}x{n}" for c, n in t["reject_codes"]) or "(none logged)"
                lines.append(
                    f"| {t['market_ticker']} | {t['target_date']} | {t['direction']} | "
                    f"{t['price_paid']:.2f} | {codes} |"
                )
        if rep["sandbox_only"]:
            lines += ["", "**sandbox \\ lab (must be empty):**", ""]
            for x in rep["sandbox_only"]:
                lines.append(f"- {x['symbol']} {x['target_date']} booked {x['price_booked']} -- {x['flag']}")
    lines += [
        "",
        "## REJECT profile for the strategy in range",
        "",
        f"{s['n_reject_lines_strategy']} lines for `{rep['strategy_name']}` "
        f"(of {s['n_reject_lines_total']} REJECT lines parsed):",
        "",
    ]
    for code, n in s["reject_profile"].items():
        lines.append(f"- {code}: {n}")
    if not s["reject_profile"]:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--promoted", required=True, help="promoted spec JSON")
    ap.add_argument("--journal", default="data/trade_journal.jsonl")
    ap.add_argument("--state", default=None, help="exchange_state.json")
    ap.add_argument("--log", nargs="*", default=[], help="runtime log path(s)/globs")
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD target_date")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD target_date")
    ap.add_argument("--frames", default=DEFAULT_FRAMES_DIR)
    ap.add_argument("--no-frame", action="store_true", help="skip the lab trade set")
    ap.add_argument("--adverse-fill", type=float, default=None, help="lab allowance (default: spec)")
    ap.add_argument("--contracts", type=int, default=None, help="lab order size (default: spec/20)")
    ap.add_argument("--strategy", default=None, help="override 'Genome <id8>'")
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "reports", "factory"))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        spec = load_spec(args.promoted)
        d0 = date.fromisoformat(args.date_from)
        d1 = date.fromisoformat(args.date_to)
        if d1 < d0:
            raise ReconcileError("--to precedes --from")
        journal_rows = _GATE.load_journal(args.journal)
        closed = _GATE.load_closed_trades(args.state)
        open_positions: List[Dict[str, Any]] = []
        if args.state:
            with open(args.state, "r", encoding="utf-8") as fh:
                open_positions = [
                    p for p in (json.load(fh).get("positions") or []) if isinstance(p, dict)
                ]
        rejects = load_reject_lines(args.log)
    except (_GATE.GateError, ReconcileError, ValueError) as exc:
        print(f"factory_paper_reconcile: {exc}", file=sys.stderr)
        return 2
    rep = reconcile(
        spec=spec,
        journal_rows=journal_rows,
        closed_trades=closed,
        open_positions=open_positions,
        rejects=rejects,
        date_from=d0,
        date_to=d1,
        frames_dir=None if args.no_frame else args.frames,
        adverse_fill_lab=args.adverse_fill,
        contracts=args.contracts,
        strategy_name=args.strategy,
    )
    out_dir = Path(args.out_dir)
    stem = f"paper_reconcile_{d0.isoformat()}_{d1.isoformat()}"
    write_json(out_dir / f"{stem}.json", rep)
    write_text(out_dir / f"{stem}.md", render_markdown(rep))
    s = rep["summary"]
    print(
        f"{stem}: fills={s['n_sandbox_fills_settled']} pending={s['n_sandbox_pending']} "
        f"lab={s['n_lab_trades']} lab_only={s['n_lab_only']} sandbox_only={s['n_sandbox_only']} "
        f"coverage={rep['lab_trade_set']['coverage']} -> {out_dir / (stem + '.json')}"
    )
    return 1 if s["n_sandbox_only"] else 0


if __name__ == "__main__":
    sys.exit(main())
