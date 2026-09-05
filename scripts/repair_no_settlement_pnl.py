#!/usr/bin/env python
"""repair_no_settlement_pnl.py -- re-price NO-side settlements booked with the YES payoff.

Until commit 724d93c (2026-09-04, F3) ``SimulatedExchange._close_position`` booked
every binary settlement (``EXPIRATION`` / ``EARLY_SETTLEMENT``) at the YES-leg
payoff (1.00 when the bracket settled yes, else 0.00) and computed the PnL
against ``entry_price`` without looking at ``contract_side``.  ``entry_price``
of a NO position is the NO cost, so every settled NO paper trade had its sign
flipped: a BUY NO at 0.33 on a bracket that settled *no* (NO won) was booked
``exit_price=0.00, pnl=-16.50`` instead of ``exit_price=1.00, pnl=+33.50``.

This script repairs a persisted ``SimulatedExchange`` state file
(``data/exchange_state.json`` on maia, ``--state``):

* a closed trade is a candidate when ``contract_side == "NO"``, ``reason`` is a
  binary settlement, ``exit_price`` equals the YES payoff of its recorded
  ``settlement_outcome`` (1.00 for "yes", 0.00 for "no") and the stored ``pnl``
  equals the OLD formula ``(exit - entry) * qty - exit_fee``.  A trade already
  priced on the NO leg (``exit_price == 1 - yes_payoff``) or carrying the
  ``repaired_no_side_settlement`` marker is left alone (idempotent); a trade
  without a recorded outcome is ambiguous (exit 1.00 / pnl +x reads the same
  for a repaired winner and a buggy loser) and is reported, never guessed.
* correction: ``exit_price -> 1 - exit_price``, ``pnl -> (exit' - entry) * qty
  - exit_fee``; ``realized_pnl`` moves by the total delta; the cumulative ledger
  is rebuilt exactly as ``_backfill_cumulative_from_closed_trades`` does
  (``cumulative_realized_pnl = sum(pnl)``, entry/exit fee sums unchanged).

Dry run by default (prints per-trade corrections and the total delta, exits 0
when nothing needs repair, 1 when repairs are pending); ``--apply`` rewrites the
file atomically after saving ``<state>.bak-<n>``.

``--journal data/trade_journal.jsonl`` repairs the append-only journal the same
way (``closed_trades`` is cleared on a cycle reset, after which the gate reads
the journal's numbers): every stale line is rewritten with the corrected
``exit_price``/``pnl`` and the marker ``repaired_no_side_settlement: true``;
untouched lines are copied byte-for-byte; ``<journal>.bak-<n>`` is written
first. Without ``--apply`` the affected lines are only listed. ``scripts/gate.py``
REFUSES a record that still carries an unrepaired stale row.

    python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl
    python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

BINARY_SETTLEMENT_REASONS = ("EXPIRATION", "EARLY_SETTLEMENT")
TOL = 1e-6


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pnl_for(entry: float, exit_price: float, qty: float, side: str, exit_fee: float) -> float:
    gross = (exit_price - entry) * qty if side != "sell" else (entry - exit_price) * qty
    return gross - exit_fee


def classify(trade: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """``("skip"|"stale"|"ok", correction)`` for one closed trade or journal row.

    Journal rows carry ``close_reason`` where the state carries ``reason``.
    """
    if str(trade.get("contract_side", "YES")).upper() != "NO":
        return "skip", None
    reason = str(trade.get("reason") or trade.get("close_reason") or "")
    if reason not in BINARY_SETTLEMENT_REASONS:
        return "skip", None
    exit_price = _f(trade.get("exit_price"), float("nan"))
    if exit_price not in (0.0, 1.0):
        return "skip", None
    if trade.get("repaired_no_side_settlement"):
        return "ok", None
    outcome = str(trade.get("settlement_outcome") or "").lower()
    if outcome not in ("yes", "no"):
        # exit 1.00 / pnl +x is the same number for a repaired NO winner and a
        # buggy NO loser; without the outcome the trade is ambiguous -> report, never guess
        return "skip", {"note": "no settlement_outcome recorded; ambiguous, left untouched",
                        "stored": _f(trade.get("pnl")), "old": None, "new": None}
    yes_payoff = 1.0 if outcome == "yes" else 0.0
    if exit_price == 1.0 - yes_payoff:
        return "ok", None  # already priced on the NO leg
    entry = _f(trade.get("entry_price"))
    qty = _f(trade.get("quantity"))
    side = str(trade.get("side", "buy"))
    exit_fee = _f(trade.get("exit_fee"))
    stored = _f(trade.get("pnl"))
    old = pnl_for(entry, exit_price, qty, side, exit_fee)
    new_exit = 1.0 - exit_price
    new = pnl_for(entry, new_exit, qty, side, exit_fee)
    if abs(stored - old) > TOL:
        return "skip", {"note": "exit_price is the YES payoff but pnl matches neither formula; left untouched",
                        "stored": stored, "old": old, "new": new}
    return "stale", {
        "id": trade.get("id"),
        "symbol": trade.get("symbol"),
        "reason": reason,
        "settlement_outcome": trade.get("settlement_outcome"),
        "entry_price": entry,
        "quantity": qty,
        "exit_price_old": exit_price,
        "exit_price_new": new_exit,
        "pnl_old": stored,
        "pnl_new": new,
        "delta": new - stored,
    }


def repair(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{corrections, skipped_unmatched, delta, state}`` with ``state`` repaired in place."""
    corrections: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    trades = state.get("closed_trades") or []
    for trade in trades:
        kind, info = classify(trade)
        if kind == "stale" and info is not None:
            trade["exit_price"] = info["exit_price_new"]
            trade["pnl"] = info["pnl_new"]
            trade["repaired_no_side_settlement"] = True
            corrections.append(info)
        elif kind == "skip" and info is not None:
            unmatched.append({"id": trade.get("id"), "symbol": trade.get("symbol"), **info})
    delta = float(sum(c["delta"] for c in corrections))
    if corrections:
        state["realized_pnl"] = _f(state.get("realized_pnl")) + delta
        # exactly _backfill_cumulative_from_closed_trades
        state["cumulative_realized_pnl"] = float(sum(_f(t.get("pnl")) for t in trades))
        state["cumulative_entry_fees"] = float(sum(_f(t.get("entry_fee")) for t in trades))
        exit_fees = float(sum(_f(t.get("exit_fee")) for t in trades))
        state["cumulative_fees_paid"] = state["cumulative_entry_fees"] + exit_fees
    return {"corrections": corrections, "skipped_unmatched": unmatched, "delta": delta, "state": state}


def repair_journal(journal_path: str, apply: bool) -> Dict[str, Any]:
    """Classify every journal line with ``classify``; rewrite the stale ones on ``apply``.

    Untouched lines are copied byte-for-byte (the journal is append-only and
    other readers hash it); stale lines are re-serialised with the corrected
    ``exit_price``/``pnl`` and the ``repaired_no_side_settlement`` marker.
    """
    corrections: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    out_lines: List[str] = []
    if not os.path.exists(journal_path):
        return {"corrections": [], "skipped_unmatched": [], "applied": False, "backup": None,
                "note": f"journal not found: {journal_path}"}
    with open(journal_path, "r", encoding="utf-8", newline="") as fh:
        raw_lines = fh.readlines()
    for n, raw in enumerate(raw_lines, 1):
        stripped = raw.strip()
        if not stripped:
            out_lines.append(raw)
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            out_lines.append(raw)
            continue
        if not isinstance(row, dict):
            out_lines.append(raw)
            continue
        kind, info = classify(row)
        if kind == "stale" and info is not None:
            row["exit_price"] = info["exit_price_new"]
            row["pnl"] = info["pnl_new"]
            row["repaired_no_side_settlement"] = True
            info = {**info, "line": n}
            corrections.append(info)
            eol = "\r\n" if raw.endswith("\r\n") else "\n"
            out_lines.append(json.dumps(row) + eol)
        else:
            if kind == "skip" and info is not None:
                unmatched.append({"line": n, "symbol": row.get("symbol"), **info})
            out_lines.append(raw)
    result: Dict[str, Any] = {"corrections": corrections, "skipped_unmatched": unmatched,
                              "applied": False, "backup": None}
    if corrections and apply:
        n = 1
        while os.path.exists(f"{journal_path}.bak-{n}"):
            n += 1
        backup = f"{journal_path}.bak-{n}"
        with open(backup, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(raw_lines)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(journal_path)) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(out_lines)
            os.replace(tmp, journal_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        result.update({"applied": True, "backup": backup})
    return result


def write_state_atomic(path: str, state: Dict[str, Any]) -> str:
    n = 1
    while os.path.exists(f"{path}.bak-{n}"):
        n += 1
    backup = f"{path}.bak-{n}"
    with open(path, "r", encoding="utf-8") as fh:
        original = fh.read()
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write(original)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True, help="SimulatedExchange state JSON (data/exchange_state.json)")
    ap.add_argument("--journal", default=None, help="data/trade_journal.jsonl: repair its stale rows too (listed on a dry run)")
    ap.add_argument("--apply", action="store_true", help="rewrite the state (and journal) file(s); default: dry run")
    ap.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    args = ap.parse_args(argv)

    with open(args.state, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    realized_before = _f(state.get("realized_pnl"))
    cum_before = _f(state.get("cumulative_realized_pnl"))
    result = repair(state)
    corrections = result["corrections"]
    summary: Dict[str, Any] = {
        "state": args.state,
        "n_closed_trades": len(state.get("closed_trades") or []),
        "n_corrected": len(corrections),
        "n_unmatched_skipped": len(result["skipped_unmatched"]),
        "delta": result["delta"],
        "realized_pnl": {"before": realized_before, "after": _f(state.get("realized_pnl"))},
        "cumulative_realized_pnl": {"before": cum_before, "after": _f(state.get("cumulative_realized_pnl"))},
        "corrections": corrections,
        "skipped_unmatched": result["skipped_unmatched"],
        "applied": False,
    }
    journal_result = None
    if args.journal:
        journal_result = repair_journal(args.journal, apply=bool(args.apply))
        summary["journal"] = {
            "path": args.journal,
            "n_corrected": len(journal_result["corrections"]),
            "n_unmatched_skipped": len(journal_result["skipped_unmatched"]),
            "corrections": journal_result["corrections"],
            "skipped_unmatched": journal_result["skipped_unmatched"],
            "applied": journal_result["applied"],
            "backup": journal_result["backup"],
        }
    if corrections and args.apply:
        summary["backup"] = write_state_atomic(args.state, state)
        summary["applied"] = True
    pending = bool(corrections) or bool(journal_result and journal_result["corrections"])
    applied_all = (not corrections or summary["applied"]) and (
        journal_result is None or not journal_result["corrections"] or journal_result["applied"]
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        for c in corrections:
            print(
                f"{c['symbol']} id={c['id']} {c['reason']} settled={c['settlement_outcome']} "
                f"entry={c['entry_price']:.2f} qty={c['quantity']:g}: exit {c['exit_price_old']:.2f}->{c['exit_price_new']:.2f} "
                f"pnl {c['pnl_old']:+.2f}->{c['pnl_new']:+.2f} (delta {c['delta']:+.2f})"
            )
        for u in result["skipped_unmatched"]:
            print(f"SKIP {u.get('symbol')} id={u.get('id')}: {u.get('note')} (stored {u.get('stored')})")
        print(
            f"{len(corrections)} NO-side settlement(s) to repair, total delta {result['delta']:+.2f}; "
            f"realized_pnl {realized_before:+.2f} -> {_f(state.get('realized_pnl')):+.2f}; "
            + ("APPLIED (backup %s)" % summary.get("backup") if summary["applied"] else "dry run (pass --apply to rewrite)")
        )
        if journal_result is not None:
            for c in journal_result["corrections"]:
                print(
                    f"journal line {c['line']} {c['symbol']} {c['reason']} settled={c['settlement_outcome']}: "
                    f"exit {c['exit_price_old']:.2f}->{c['exit_price_new']:.2f} pnl {c['pnl_old']:+.2f}->{c['pnl_new']:+.2f}"
                )
            print(
                f"journal: {len(journal_result['corrections'])} stale NO-side row(s); "
                + ("APPLIED (backup %s)" % journal_result["backup"] if journal_result["applied"] else "dry run")
            )
    return 0 if (not pending or applied_all) else 1


if __name__ == "__main__":
    sys.exit(main())
