#!/usr/bin/env python3
"""reset_contaminated_state.py — Repair state contaminated by the settlement bug.

2026-06-10 fix — companion to scripts/settlement_reconcile.py.

WHY THIS EXISTS
---------------
The 2026-06-10 settlement bug booked fictional YES settlements (the 15m-crypto
minute-label suffix was parsed as a price strike, forcing near-100% auto-YES).
That fiction poisoned three persisted artifacts the live system trusts:

  * data/strategy_win_rates.json  — wins/total per strategy that drive
    calibrated-Kelly sizing. Contaminated win rates inflate position sizes.
  * data/exchange_state.json      — closed_trades PnL + cumulative aggregates.
    Fictional +PnL makes the capital-transition gate (72h positive) pass on a
    lie.
  * data/trade_journal.jsonl      — prediction_correct flags (and the PnL they
    derive from), which feed ML label generation. Garbage-in -> garbage model.

This script rebuilds those artifacts from GROUND TRUTH — the actual Kalshi
settlement results cached in data/settlement_cache.json (populated by
settlement_reconcile.py) — so the validation clock can restart honestly.

SAFETY (this is run by a human, manually, after deploy)
-------------------------------------------------------
  * --dry-run is the DEFAULT. It writes NOTHING; it only prints what WOULD
    change. You must pass --apply to mutate files.
  * Every --apply ALWAYS writes a timestamped backup of each touched file to
    data/backups/<name>.<UTC-timestamp>.bak BEFORE modifying it.
  * Repair is RECOMPUTE-FROM-ACTUAL, not a destructive wipe: trades whose
    contracts actually settled are re-scored against the real result; trades
    with no cached truth are left untouched (and reported as "unresolved") so
    we never destroy data we can't verify.

USAGE
-----
    python scripts/reset_contaminated_state.py                 # DRY RUN (default)
    python scripts/reset_contaminated_state.py --apply         # actually repair
    python scripts/reset_contaminated_state.py --apply --only win_rates
    python scripts/reset_contaminated_state.py --apply --only journal --only state

Run scripts/settlement_reconcile.py FIRST so settlement_cache.json is populated
with actual results; this script reads that cache and never hits the network.

Exit codes:
    0  completed (dry-run or apply)
    2  usage / fatal error
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Reuse the reconcile module's loaders / cache / derivation so the two scripts
# share ONE definition of "what the sim recorded" and "what actually settled".
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import settlement_reconcile as sr  # noqa: E402  (sibling script, see sys.path above)

# Win-rate counting must match RiskManager._on_trade_close exactly: a "win" is
# pnl > -FEE_TOLERANCE (fees-tolerant break-even counts as a win).
FEE_TOLERANCE = 0.05

DATA_DIR = sr.DATA_DIR
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

WIN_RATES_PATH = os.path.join(DATA_DIR, "strategy_win_rates.json")
EXCHANGE_STATE_PATH = sr.EXCHANGE_STATE_PATH
TRADE_JOURNAL_PATH = sr.TRADE_JOURNAL_PATH


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def backup_file(path: str) -> str | None:
    """Copy `path` to data/backups/<name>.<UTC-timestamp>.bak. Returns dest or None."""
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak")
    shutil.copy2(path, dest)
    return dest


# ---------------------------------------------------------------------------
# Settlement-truth recompute helpers
# ---------------------------------------------------------------------------
def _actual_result(ticker: str, cache: dict) -> str | None:
    """Return 'yes'/'no' from the cache, or None if not present. No network."""
    entry = cache.get(ticker) if ticker else None
    if isinstance(entry, dict):
        r = entry.get("result")
        return r if r in ("yes", "no") else None
    return None


def _true_pnl_from_settlement(trade: dict, actual: str) -> float | None:
    """Recompute the settlement PnL a binary trade SHOULD have realized.

    Binary settlement pays 1.00 if the holder's side won, else 0.00. Net PnL is
    approximated as (payout - entry_price) * quantity - fees, mirroring the
    simulator's own accounting (entry_fee/exit_fee on the position). Returns
    None if the trade lacks the fields needed to recompute cleanly.
    """
    contract_side = (trade.get("contract_side") or "YES").upper()
    won = (contract_side == "YES" and actual == "yes") or (
        contract_side == "NO" and actual == "no"
    )

    try:
        entry_price = float(trade.get("entry_price"))
        quantity = float(trade.get("quantity"))
    except (TypeError, ValueError):
        return None

    # Cost basis per contract: a YES holder pays entry_price; a NO holder's
    # collateral is (1 - entry_price) per the project's short-cost convention.
    if contract_side == "NO":
        cost = (1.0 - entry_price) * quantity
        payout = quantity if won else 0.0
    else:
        cost = entry_price * quantity
        payout = quantity if won else 0.0

    fees = 0.0
    for fk in ("entry_fee", "exit_fee"):
        try:
            fees += float(trade.get(fk) or 0.0)
        except (TypeError, ValueError):
            pass

    return round(payout - cost - fees, 4)


# ---------------------------------------------------------------------------
# 1. strategy_win_rates.json
# ---------------------------------------------------------------------------
def rebuild_win_rates(trades: list[dict], cache: dict) -> dict:
    """Recompute per-strategy (wins, total) from settlement truth.

    For each settled trade we can verify against the cache, re-derive the
    holder's win from the ACTUAL result; trades with no cached truth fall back
    to the recorded pnl sign (so non-15m-crypto strategies keep their history).
    """
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # name -> [wins, total]

    for t in trades:
        sim = sr.sim_recorded_result(t)
        if sim is None:
            continue  # non-settlement close — RiskManager still counts these,
            # but they are price-based exits whose pnl sign is authoritative; we
            # include them below via the pnl-sign branch.
        name = t.get("strategy_name", "Unknown")
        ticker = t.get("symbol") or t.get("ticker")
        actual = _actual_result(ticker, cache)

        if actual is not None:
            contract_side = (t.get("contract_side") or "YES").upper()
            won = (contract_side == "YES" and actual == "yes") or (
                contract_side == "NO" and actual == "no"
            )
        else:
            # No truth available — fall back to recorded pnl sign (fees-tolerant).
            try:
                pnl = float(t.get("pnl") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            won = pnl > -FEE_TOLERANCE

        counts[name][1] += 1
        if won:
            counts[name][0] += 1

    return {k: [v[0], v[1]] for k, v in counts.items()}


# ---------------------------------------------------------------------------
# 2. exchange_state.json (closed_trades pnl + cumulative aggregates)
# ---------------------------------------------------------------------------
def repair_exchange_state(state: dict, cache: dict) -> dict:
    """Re-score contaminated closed_trades against actual results.

    Returns a report dict. Mutates `state` in place (caller decides to persist).
    Only trades that (a) are binary settlements and (b) have cached truth AND
    (c) disagree with the recorded outcome are rewritten; everything else is
    left exactly as-is. Cumulative aggregates are recomputed from the (repaired)
    closed_trades so the gate reflects honest PnL.
    """
    closed = state.get("closed_trades")
    if not isinstance(closed, list):
        return {"repaired": 0, "unresolved": 0, "pnl_before": 0.0, "pnl_after": 0.0}

    repaired = 0
    unresolved = 0
    pnl_before = 0.0

    for t in closed:
        try:
            pnl_before += float(t.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pass

        sim = sr.sim_recorded_result(t)
        if sim is None:
            continue
        ticker = t.get("symbol")
        actual = _actual_result(ticker, cache)
        if actual is None:
            unresolved += 1
            continue
        if sim == actual:
            continue  # already correct — leave the recorded pnl untouched

        true_pnl = _true_pnl_from_settlement(t, actual)
        if true_pnl is None:
            unresolved += 1
            continue

        # Capture the ORIGINAL pnl before we overwrite it, for the audit stamp.
        was_pnl = _safe_float(t.get("pnl"))
        side = (t.get("contract_side") or "YES").upper()
        won = (side == "YES" and actual == "yes") or (side == "NO" and actual == "no")

        # Rewrite the contaminated trade to reflect reality.
        t["pnl"] = true_pnl
        t["exit_price"] = 1.00 if won else 0.00
        # Stamp the repair so the change is auditable in the ledger.
        t["settlement_repaired"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actual_result": actual,
            "was_pnl": round(was_pnl, 4),
        }
        repaired += 1

    # Recompute cumulative aggregates from the repaired ledger.
    pnl_after = 0.0
    total_fees = 0.0
    entry_fees = 0.0
    for t in closed:
        try:
            pnl_after += float(t.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            entry_fees += float(t.get("entry_fee") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            total_fees += float(t.get("entry_fee") or 0.0) + float(
                t.get("exit_fee") or 0.0
            )
        except (TypeError, ValueError):
            pass

    state["cumulative_realized_pnl"] = round(pnl_after, 4)
    state["cumulative_fees_paid"] = round(total_fees, 4)
    state["cumulative_entry_fees"] = round(entry_fees, 4)

    return {
        "repaired": repaired,
        "unresolved": unresolved,
        "pnl_before": round(pnl_before, 2),
        "pnl_after": round(pnl_after, 2),
    }


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 3. trade_journal.jsonl (prediction_correct flags)
# ---------------------------------------------------------------------------
def repair_journal(trades: list[dict], cache: dict) -> tuple[list[dict], dict]:
    """Recompute prediction_correct for journal rows against actual results.

    Returns (repaired_rows, report). Only rows that are binary settlements with
    cached truth that DISAGREES are flipped; all other rows pass through
    untouched (object identity preserved so unrelated fields are never lost).
    """
    flipped = 0
    unresolved = 0
    out: list[dict] = []

    for t in trades:
        sim = sr.sim_recorded_result(t)
        ticker = t.get("symbol") or t.get("ticker")
        actual = _actual_result(ticker, cache) if sim is not None else None

        if sim is not None and actual is None:
            unresolved += 1

        if sim is not None and actual is not None:
            contract_side = (t.get("contract_side") or "YES").upper()
            won = (contract_side == "YES" and actual == "yes") or (
                contract_side == "NO" and actual == "no"
            )
            if bool(t.get("prediction_correct")) != won:
                t = dict(t)  # copy before mutating so caller's input is intact
                t["prediction_correct"] = won
                flipped += 1

        out.append(t)

    return out, {"flipped": flipped, "unresolved": unresolved}


# ---------------------------------------------------------------------------
# Writers (only invoked under --apply, always after a backup)
# ---------------------------------------------------------------------------
def write_json(path: str, obj) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def write_jsonl(path: str, rows: list[dict]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Repair state contaminated by the 2026-06-10 settlement bug."
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="(DEFAULT) Show what would change; write nothing.",
    )
    g.add_argument(
        "--apply",
        action="store_true",
        help="Actually repair files (always backs up first).",
    )
    p.add_argument(
        "--only",
        action="append",
        choices=["win_rates", "state", "journal"],
        help="Limit repair to specific artifact(s); repeatable. Default: all.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    apply = bool(args.apply)
    targets = set(args.only) if args.only else {"win_rates", "state", "journal"}

    mode = "APPLY" if apply else "DRY-RUN"
    log(f"reset_contaminated_state — mode={mode}, targets={sorted(targets)}")

    cache = sr.load_cache()
    if not cache:
        log(
            "WARN: settlement_cache.json is empty — run settlement_reconcile.py "
            "first to populate actual results. Recompute will fall back to "
            "recorded-pnl sign where no truth exists."
        )

    # ---- 1. win rates -----------------------------------------------------
    if "win_rates" in targets:
        # Win rates are best rebuilt from the FULL journal history (the same
        # source RiskManager's counts ultimately derive from).
        journal = sr.load_journal_trades()
        new_wr = rebuild_win_rates(journal, cache)
        old_wr = _load_json(WIN_RATES_PATH)
        log("[win_rates] rebuilt from settlement truth:")
        for name in sorted(set(new_wr) | set(old_wr or {})):
            old = (old_wr or {}).get(name)
            new = new_wr.get(name)
            log(f"  {name[:30]:30s}  {old}  ->  {new}")
        if apply:
            bk = backup_file(WIN_RATES_PATH)
            log(f"[win_rates] backup: {bk}")
            write_json(WIN_RATES_PATH, new_wr)
            log(f"[win_rates] WROTE {WIN_RATES_PATH}")

    # ---- 2. exchange_state -----------------------------------------------
    if "state" in targets:
        state = _load_json(EXCHANGE_STATE_PATH)
        if state is None:
            log(f"[state] {EXCHANGE_STATE_PATH} missing/unreadable — skipping")
        else:
            report = repair_exchange_state(state, cache)
            log(
                f"[state] {report['repaired']} trades re-scored, "
                f"{report['unresolved']} unresolved; cumulative_realized_pnl "
                f"${report['pnl_before']:+.2f} -> ${report['pnl_after']:+.2f}"
            )
            if apply:
                bk = backup_file(EXCHANGE_STATE_PATH)
                log(f"[state] backup: {bk}")
                write_json(EXCHANGE_STATE_PATH, state)
                log(f"[state] WROTE {EXCHANGE_STATE_PATH}")

    # ---- 3. trade journal -------------------------------------------------
    if "journal" in targets:
        journal = sr.load_journal_trades()
        repaired_rows, report = repair_journal(journal, cache)
        log(
            f"[journal] {report['flipped']} prediction_correct flags flipped, "
            f"{report['unresolved']} settled rows unresolved (left as-is)"
        )
        if apply:
            bk = backup_file(TRADE_JOURNAL_PATH)
            log(f"[journal] backup: {bk}")
            write_jsonl(TRADE_JOURNAL_PATH, repaired_rows)
            log(f"[journal] WROTE {TRADE_JOURNAL_PATH}")

    if not apply:
        log("DRY-RUN complete — no files written. Re-run with --apply to commit.")
    else:
        log("APPLY complete — backups in data/backups/.")
    return 0


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log(f"WARN: could not parse {path}: {exc}")
        return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - top-level safety net
        log(f"FATAL: {exc}")
        sys.exit(2)
