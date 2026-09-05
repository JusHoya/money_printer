#!/usr/bin/env python
"""repair_no_settlement_pnl.py -- re-price NO-side settlements booked with the YES payoff.

Until commit 724d93c (2026-09-04, F3) ``SimulatedExchange._close_position`` booked
every binary settlement (``EXPIRATION`` / ``EARLY_SETTLEMENT``) at the YES-leg
payoff (1.00 when the bracket settled yes, else 0.00) and computed the PnL
against ``entry_price`` without looking at ``contract_side``.  ``entry_price``
of a NO position is the NO cost, so every settled NO paper trade had its sign
flipped: a BUY NO at 0.33 on a bracket that settled *no* (NO won) was booked
``exit_price=0.00, pnl=-16.50`` instead of ``exit_price=1.00, pnl=+33.50``.

The inverted sign did not stop at the PnL. Four artifacts were written FROM it,
and this script repairs all of them from the one recorded truth every settled
row carries -- ``settlement_outcome`` (what the bracket actually did) -- so the
repair never has to guess a direction:

1. ``--state data/exchange_state.json``  (closed_trades + the cumulative ledger)
2. ``--journal data/trade_journal.jsonl`` (``exit_price``/``pnl`` per row)
3. ``--journal``  (``prediction_correct``, derived from ``pnl`` at write time)
4. ``--win-rates data/strategy_win_rates.json`` (the FR-0.6 Kelly windows)

1. exchange state (``--state``)
-------------------------------
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

2/3. trade journal (``--journal``)
----------------------------------
``closed_trades`` is cleared on a cycle reset, after which the gate reads the
journal's numbers, so the journal needs the same PnL repair: every stale line
is rewritten with the corrected ``exit_price``/``pnl`` and the marker
``repaired_no_side_settlement: true``.  ``scripts/gate.py`` REFUSES a record
that still carries an unrepaired stale row.

The SAME pass also repairs ``prediction_correct``.  That flag was derived from
``pnl`` at write time, so the inverted sign inverted the flag with it, and a
PnL-only repair leaves a row asserting the opposite of its own corrected PnL.
It matters because ``scripts/settlement_reconcile.py`` derives the sim's
recorded outcome from ``prediction_correct`` IN PREFERENCE to ``pnl`` -- a
contradicting flag makes the scheduled 06:00Z reconcile raise a *false*
settlement-truth breach every day, on precisely the rows repaired for a
settlement bug.  The rule is not restated here: it is
:func:`src.ml.trade_journal.prediction_correct_for`, the one the writer now
uses, so writer and repair agree by construction.  Only rows that ALREADY carry
the field and disagree with settlement truth are rewritten -- a row without the
field is silent, not wrong, and is left byte-for-byte alone.

4. win rates (``--win-rates``)
------------------------------
``data/strategy_win_rates.json`` holds the FR-0.6 recency windows that feed
``RiskManager.calculate_kelly_size``.  ``_on_trade_close`` appended
``1 if pnl > -FEE_TOLERANCE else 0`` at close time, from the buggy PnL, so the
windows record wins for trades that lost money (maia: ML Weather ``[1,1,1]``
for three trades totalling -$44.00).  The rebuild REPLAYS that exact one-liner
over the repaired journal, in journal order -- ``scripts/run_dashboard.py``
calls ``_on_trade_close`` and ``trade_journal.record`` on the same event, in
that order, for every close, so replaying the journal reproduces the windows
the RiskManager would have built had the PnL been right.  ``FEE_TOLERANCE`` and
``WIN_RATE_WINDOW`` are imported from ``src.core.risk_manager`` rather than
re-declared, so the replay cannot drift from the live rule.

Why not delegate to ``scripts/reset_contaminated_state.py``?  Its
``rebuild_win_rates`` answers a different question: it rebuilds from EXTERNAL
settlement truth in ``data/settlement_cache.json``, which is the right source
when the recorded PnL is *fiction* (the 2026-06-10 bug invented settlements).
Here the repaired PnL is truth and the cache is not required; and its rebuild
skips every non-settlement close, which ``_on_trade_close`` counts -- replaying
it would silently drop the stop-loss and time-limit outcomes from the window.
Its ``repair_journal``/``rebuild_win_rates`` remain the right tool for a
cache-driven rebuild; this script is the sibling for a sign inversion whose
truth is already on the row.  (Its ``prediction_correct`` rule and this one now
both reduce to "``contract_side`` won iff the contract settled its way".)

Safety
------
* **Dry run is the default** for every artifact.  Exit 0 when nothing needs
  repair, 1 when repairs are pending; ``--apply`` rewrites the files.
* Each file is backed up to ``<path>.bak-<n>`` (lowest free ``n``) before it is
  replaced, and the backup is copied from the file AS IT EXISTS AT SWAP TIME --
  not from the snapshot read at the start of the run.
* **Concurrency.** The sandbox settles positions on a timer and
  ``TradeJournal.record`` appends while this runs.  Rebuilding a file from a
  stale ``readlines()`` snapshot and swapping it in with ``os.replace`` would
  silently and irrecoverably destroy every row appended in between (the backup,
  written from the same snapshot, would not save them either).  So every
  ``--apply`` holds an exclusive ``<path>.repair-lock``, records
  ``(st_mtime_ns, st_size)`` before AND after reading, and re-checks it
  immediately before the swap; ANY change aborts loudly with
  ``ConcurrentWriteError`` and writes nothing.  Aborting is safe precisely
  because the repair is idempotent: just run it again.

    python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl
    python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl --win-rates data/strategy_win_rates.json --apply


OPERATOR RUNBOOK -- maia sandbox (HTTP-only host, no ssh)
=========================================================
Run every command from a shell on the maia box (or wherever the ``mp-sandbox``
container runs).  The container mounts the live ``data/`` directory, so the
paths below are the real artifacts.  Nothing here restarts or redeploys the
sandbox; the running process keeps its in-memory copies, which is why step 5
matters.

Step 0 -- capture the "before" from the HTTP API (from any machine)::

    curl -s http://maia.local:8050/api/win_rates
    # expect: {"ok":true,"data":{"ML Weather":{"window":[1,1,1],...},
    #                            "Meteorologist V2":{"window":[0,1,0],...}}}

Step 1 -- DRY RUN.  Writes nothing.  Read the output before going on::

    docker exec mp-sandbox python scripts/repair_no_settlement_pnl.py \
        --state data/exchange_state.json \
        --journal data/trade_journal.jsonl \
        --win-rates data/strategy_win_rates.json

    Expected, VERBATIM (this is the output of a real dry run against a copy of
    maia's ``/api/journal?last_n=500`` taken 2026-09-05T07:44Z).  The state and
    journal PnL were already repaired on 2026-09-05, so those report 0
    corrections; the two artifacts the earlier repair missed report the work
    that is left::

        0 NO-side settlement(s) to repair, total delta +0.00; realized_pnl +0.00 -> +0.00; dry run (pass --apply to rewrite)
        journal prediction_correct line 1 KXHIGHNY-26SEP01-T83 settled=yes side=NO pnl -8.50: True -> False
        journal prediction_correct line 2 KXHIGHLAX-26SEP01-B76.5 settled=yes side=NO pnl -18.50: True -> False
        journal prediction_correct line 3 KXHIGHMIA-26SEP01-B89.5 settled=yes side=NO pnl -17.00: True -> False
        journal prediction_correct line 4 KXHIGHNY-26SEP03-T83 settled=no side=NO pnl +20.16: False -> True
        journal: 0 stale NO-side row(s), 4 prediction_correct flag(s) to correct; dry run (pass --apply to rewrite)
        win_rates ML Weather: window n=3 wins=3 (100%)  ->  window n=3 wins=0 (0%)
        win_rates Meteorologist V2: window n=3 wins=1 (33%)  ->  window n=3 wins=2 (67%)
        win_rates: 2 strategy window(s) to rebuild; dry run (pass --apply to rewrite)
        (exit code 1 -- repairs pending)

    The real ``--state`` on maia is NOT empty the way the verification copy was
    (``closed_trades`` was cleared by a cycle reset there), so its first line
    may legitimately report more closed trades; what must match is
    ``0 NO-side settlement(s) to repair``.

    STOP if the counts differ (a fifth flag, or a ``WARN win_rates ... shorter``
    line, means the journal no longer matches what this runbook was written
    against).  A ``WARN`` shrink line means the journal cannot account for the
    whole live window; that strategy is skipped unless you pass
    ``--allow-window-shrink``, and you should work out why first.

Step 2 -- APPLY::

    docker exec mp-sandbox python scripts/repair_no_settlement_pnl.py \
        --state data/exchange_state.json \
        --journal data/trade_journal.jsonl \
        --win-rates data/strategy_win_rates.json \
        --apply

    Expect the same lines, each tail now reading ``APPLIED (backup
    data/....bak-N)``, and exit code 0.

    If it prints ``ABORTED: data/trade_journal.jsonl changed while the repair
    was running`` nothing was written -- a position settled mid-run.  Just run
    the same command again.

Step 3 -- confirm the backups exist (they are the undo)::

    docker exec mp-sandbox ls -la data/ | grep bak-

Step 4 -- verify the files on disk::

    docker exec mp-sandbox python scripts/repair_no_settlement_pnl.py \
        --state data/exchange_state.json --journal data/trade_journal.jsonl \
        --win-rates data/strategy_win_rates.json
    # expect: 0 corrections everywhere, exit code 0 (idempotent)

    docker exec mp-sandbox python scripts/settlement_reconcile.py --offline --json
    # expect: no mismatch attributable to these four rows -- this is the check
    # that was raising a FALSE breach at 06:00Z every day.

Step 5 -- make the RUNNING process pick up the new win rates.
    ``RiskManager`` loaded ``strategy_win_rates.json`` at startup and holds the
    windows in memory; it rewrites the file on every close, which would put the
    old windows straight back.  The rebuilt file only takes effect after a
    restart, so restart the sandbox once the apply is verified::

        docker restart mp-sandbox
        curl -s http://maia.local:8050/healthz

Step 6 -- verify through the API (the acceptance check)::

    curl -s http://maia.local:8050/api/win_rates
    # expect: ML Weather        -> "window":[0,0,0]
    #         Meteorologist V2  -> "window":[1,1,0]

    curl -s "http://maia.local:8050/api/journal?last_n=500" | grep -o '"prediction_correct":[a-z]*'
    # expect, in row order: false,false,false,true,true,false

Rollback -- restore the ``.bak-<n>`` copies and restart::

    docker exec mp-sandbox sh -c 'cp data/trade_journal.jsonl.bak-1 data/trade_journal.jsonl'
    docker exec mp-sandbox sh -c 'cp data/strategy_win_rates.json.bak-1 data/strategy_win_rates.json'
    docker restart mp-sandbox
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, Iterator, List, Optional, Tuple

BINARY_SETTLEMENT_REASONS = ("EXPIRATION", "EARLY_SETTLEMENT")
TOL = 1e-6

# The repo root, so ``src.ml.trade_journal`` / ``src.core.risk_manager`` import
# whether this is run as ``python scripts/repair_no_settlement_pnl.py`` (sys.path[0]
# is scripts/) or from anywhere else.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ConcurrentWriteError(RuntimeError):
    """The file changed under us; nothing was written. Re-run (this is idempotent)."""


class RepairLockedError(RuntimeError):
    """Another repair holds the lock on this file."""


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pnl_for(entry: float, exit_price: float, qty: float, side: str, exit_fee: float) -> float:
    gross = (exit_price - entry) * qty if side != "sell" else (entry - exit_price) * qty
    return gross - exit_fee


# ---------------------------------------------------------------------------
# Concurrency: nothing is swapped in over a file that moved under us
# ---------------------------------------------------------------------------
def _stat_key(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


@contextlib.contextmanager
def repair_lock(path: str) -> Iterator[str]:
    """Exclusive ``<path>.repair-lock`` for the whole read->swap of ``path``.

    Advisory: it stops a second *repair* from interleaving with this one. It
    cannot stop ``TradeJournal.record`` from appending -- that is what the
    stat guard is for.
    """
    lock = f"{path}.repair-lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RepairLockedError(
            f"{lock} exists: another repair is running. If you are sure none is, "
            f"delete it and re-run."
        )
    try:
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        finally:
            os.close(fd)
        yield lock
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def read_lines_snapshot(path: str) -> Tuple[List[str], Tuple[int, int]]:
    """``(lines, stat_key)``; raises if the file changed *during* the read.

    Stat before and after: an append that lands mid-``readlines()`` can hand us
    a torn final line, and comparing only a post-read stat at swap time would
    then bless a snapshot that already lost data.
    """
    before = _stat_key(path)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()
    after = _stat_key(path)
    if before != after:
        raise ConcurrentWriteError(
            f"{path} changed while it was being read (was {before}, now {after}); "
            f"nothing written -- re-run"
        )
    return lines, after


def read_json_snapshot(path: str) -> Tuple[Any, Tuple[int, int]]:
    """``(obj, stat_key)``; raises if the file changed during the read."""
    before = _stat_key(path)
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)
    after = _stat_key(path)
    if before != after:
        raise ConcurrentWriteError(
            f"{path} changed while it was being read (was {before}, now {after}); "
            f"nothing written -- re-run"
        )
    return obj, after


def _guard_unchanged(path: str, expected: Tuple[int, int]) -> None:
    now = _stat_key(path)
    if now != expected:
        raise ConcurrentWriteError(
            f"{path} changed while the repair was running (was {expected}, now "
            f"{now}); a row was appended and swapping in our snapshot would "
            f"destroy it. Nothing written -- re-run (the repair is idempotent)."
        )


def _next_backup(path: str) -> str:
    n = 1
    while os.path.exists(f"{path}.bak-{n}"):
        n += 1
    return f"{path}.bak-{n}"


def swap_in(path: str, payload: str, expected: Tuple[int, int]) -> str:
    """Back up ``path`` then atomically replace it with ``payload``.

    The backup is ``shutil.copy2`` of the file AS IT IS NOW, not a re-emission
    of whatever snapshot the caller read -- a stale backup cannot undo a stale
    write. The stat guard runs before the copy and again after it, so an append
    that lands during the copy aborts with the file untouched.
    """
    _guard_unchanged(path, expected)
    backup = _next_backup(path)
    shutil.copy2(path, backup)
    try:
        _guard_unchanged(path, expected)
    except ConcurrentWriteError:
        with contextlib.suppress(OSError):
            os.unlink(backup)
        raise
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        with contextlib.suppress(OSError):
            os.unlink(backup)
        raise
    return backup


# ---------------------------------------------------------------------------
# 1/2. the sign inversion itself (exchange state + journal PnL)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 3. prediction_correct -- the flag derived from the inverted sign
# ---------------------------------------------------------------------------
def classify_prediction_correct(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The correction owed to ``row["prediction_correct"]``, or None.

    Delegates the rule to :func:`src.ml.trade_journal.prediction_correct_for` --
    the same function the writer uses -- so this repair cannot disagree with
    what the runtime will write next. Call this AFTER the PnL repair: the
    fallback branch reads ``pnl``.

    A row that does not carry the field is left alone. It is silent, not wrong,
    and adding a field would rewrite a line the repair has no business touching.
    """
    if "prediction_correct" not in row:
        return None
    from src.ml.trade_journal import prediction_correct_for

    expected = prediction_correct_for(row, _f(row.get("pnl")))
    if expected == row["prediction_correct"]:
        return None
    return {
        "symbol": row.get("symbol"),
        "settlement_outcome": row.get("settlement_outcome"),
        "contract_side": str(row.get("contract_side") or "YES").upper(),
        "pnl": _f(row.get("pnl")),
        "old": row["prediction_correct"],
        "new": expected,
    }


def repair_journal(journal_path: str, apply: bool) -> Dict[str, Any]:
    """Repair the journal's NO-side PnL *and* its ``prediction_correct`` flags.

    Untouched lines are copied byte-for-byte (the journal is append-only and
    other readers hash it); a repaired line is re-serialised with the corrected
    ``exit_price``/``pnl`` and the ``repaired_no_side_settlement`` marker,
    and/or the corrected ``prediction_correct``.

    Under ``apply`` the whole read->swap runs inside ``repair_lock`` and is
    guarded by the file's ``(mtime_ns, size)``: a row appended by
    ``TradeJournal.record`` while we work aborts the swap rather than being
    silently destroyed by it.
    """
    if not os.path.exists(journal_path):
        return {"corrections": [], "flag_corrections": [], "skipped_unmatched": [],
                "rows": [], "applied": False, "backup": None,
                "note": f"journal not found: {journal_path}"}
    if not apply:
        raw_lines, _ = read_lines_snapshot(journal_path)
        return _repair_journal_lines(raw_lines)[0]
    with repair_lock(journal_path):
        raw_lines, key = read_lines_snapshot(journal_path)
        result, out_lines = _repair_journal_lines(raw_lines)
        if result["corrections"] or result["flag_corrections"]:
            result["backup"] = swap_in(journal_path, "".join(out_lines), key)
            result["applied"] = True
        return result


def _repair_journal_lines(raw_lines: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    """Pure classify+rewrite over the snapshot. No I/O, so it is trivially testable."""
    corrections: List[Dict[str, Any]] = []
    flag_corrections: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    out_lines: List[str] = []
    # The repaired rows, in journal order -- the win-rate replay runs off these
    # so a DRY RUN previews the post-repair windows rather than re-reading the
    # un-repaired file and reporting "nothing to do".
    rows: List[Dict[str, Any]] = []
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
        dirty = False
        if kind == "stale" and info is not None:
            row["exit_price"] = info["exit_price_new"]
            row["pnl"] = info["pnl_new"]
            row["repaired_no_side_settlement"] = True
            corrections.append({**info, "line": n})
            dirty = True
        elif kind == "skip" and info is not None:
            unmatched.append({"line": n, "symbol": row.get("symbol"), **info})
        # AFTER the PnL repair: the flag's fallback branch reads the repaired pnl.
        flag = classify_prediction_correct(row)
        if flag is not None:
            row["prediction_correct"] = flag["new"]
            flag_corrections.append({**flag, "line": n})
            dirty = True
        rows.append(row)
        if dirty:
            eol = "\r\n" if raw.endswith("\r\n") else "\n"
            out_lines.append(json.dumps(row) + eol)
        else:
            out_lines.append(raw)
    result: Dict[str, Any] = {"corrections": corrections, "flag_corrections": flag_corrections,
                              "skipped_unmatched": unmatched, "rows": rows,
                              "applied": False, "backup": None}
    return result, out_lines


# ---------------------------------------------------------------------------
# 4. strategy_win_rates.json -- the FR-0.6 Kelly windows
# ---------------------------------------------------------------------------
def rebuild_win_rates(rows: List[Dict[str, Any]], existing: Dict[str, Any],
                      allow_shrink: bool = False) -> Dict[str, Any]:
    """Replay ``RiskManager._on_trade_close``'s win record over the repaired journal.

    Returns ``{"win_rates": <the file to write>, "changes": [...], "warnings": [...]}``.

    The rule is imported, not restated: ``1 if pnl > -FEE_TOLERANCE else 0``,
    last ``WIN_RATE_WINDOW`` per strategy, in journal order. Every closed row
    counts -- ``_on_trade_close`` does not care why a position closed, so a
    rebuild that only counted settlements would quietly drop stop-loss and
    time-limit outcomes.

    A strategy whose rebuilt window is SHORTER than the one on disk is reported
    and SKIPPED unless ``allow_shrink``: the journal cannot account for the
    whole live window (it was rotated, or predates the file), and writing the
    short version would destroy outcomes this script cannot verify. Strategies
    absent from the journal are likewise left exactly as they are.
    """
    from src.core.risk_manager import FEE_TOLERANCE, WIN_RATE_WINDOW

    outcomes: Dict[str, List[int]] = {}
    order: List[str] = []
    for row in rows:
        name = str(row.get("strategy_name") or "Unknown")
        if name not in outcomes:
            outcomes[name] = []
            order.append(name)
        outcomes[name].append(1 if _f(row.get("pnl")) > -FEE_TOLERANCE else 0)

    out = {k: dict(v) if isinstance(v, dict) else v for k, v in (existing or {}).items()}
    changes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for name in order:
        window = outcomes[name][-WIN_RATE_WINDOW:]
        prev = existing.get(name) if isinstance(existing, dict) else None
        prev_window = prev.get("window") if isinstance(prev, dict) else None
        if isinstance(prev_window, list) and len(prev_window) > len(window) and not allow_shrink:
            warnings.append(
                f"{name}: rebuilt window is shorter than the stored one "
                f"(n={len(window)} < n={len(prev_window)}); the journal cannot account "
                f"for the whole window, so it is left untouched "
                f"(pass --allow-window-shrink to overwrite it anyway)"
            )
            continue
        if prev_window == window:
            continue  # idempotent: identical window, keep the stored `updated`
        entry = dict(prev) if isinstance(prev, dict) else {}
        entry["window"] = window
        entry["updated"] = _rebuild_stamp()
        out[name] = entry
        changes.append({"strategy": name, "old": prev_window, "new": window})
    return {"win_rates": out, "changes": changes, "warnings": warnings}


def _rebuild_stamp() -> str:
    """UTC ISO stamp for a rebuilt window, matching what RiskManager persists."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _tail(applied: bool, backup: Optional[str], apply_requested: bool, pending: bool) -> str:
    """The trailing clause of a per-artifact summary line."""
    if applied:
        return f"APPLIED (backup {backup})"
    if apply_requested and not pending:
        return "nothing to write"
    return "dry run (pass --apply to rewrite)" if not apply_requested else "NOT APPLIED"


def _summarize_window(window: Any) -> str:
    """One-line summary of a win window, in the same format as
    ``reset_contaminated_state._summarize_wr`` so the two scripts' logs read alike."""
    if window is None:
        return "-"
    if isinstance(window, dict):
        window = window.get("window")
    if not isinstance(window, list):
        return repr(window)
    n = len(window)
    if not n:
        return "window n=0"
    wins = sum(1 for x in window if x)
    return f"window n={n} wins={wins} ({wins / n:.0%})"


def repair_win_rates(win_rates_path: str, rows: Optional[List[Dict[str, Any]]], apply: bool,
                     allow_shrink: bool = False) -> Dict[str, Any]:
    """Rebuild ``strategy_win_rates.json`` from the REPAIRED journal rows.

    ``rows`` comes from :func:`repair_journal`, not from a fresh read of the
    file: on a dry run the file on disk still holds the buggy PnL, and
    replaying that would report "nothing to do" for the very windows the run is
    supposed to preview.
    """
    if rows is None:
        return {"changes": [], "warnings": [], "applied": False, "backup": None,
                "note": "win-rate rebuild needs --journal (the outcomes come from it)"}
    if not os.path.exists(win_rates_path):
        return {"changes": [], "warnings": [], "applied": False, "backup": None,
                "note": f"win rates not found: {win_rates_path}"}
    if not apply:
        existing, _ = read_json_snapshot(win_rates_path)
        result = rebuild_win_rates(rows, existing if isinstance(existing, dict) else {}, allow_shrink)
        return {"changes": result["changes"], "warnings": result["warnings"],
                "applied": False, "backup": None}
    with repair_lock(win_rates_path):
        existing, key = read_json_snapshot(win_rates_path)
        result = rebuild_win_rates(rows, existing if isinstance(existing, dict) else {}, allow_shrink)
        out: Dict[str, Any] = {"changes": result["changes"], "warnings": result["warnings"],
                               "applied": False, "backup": None}
        if result["changes"]:
            payload = json.dumps(result["win_rates"], indent=2) + "\n"
            out["backup"] = swap_in(win_rates_path, payload, key)
            out["applied"] = True
        return out


def write_state_atomic(path: str, state: Dict[str, Any], expected: Tuple[int, int]) -> str:
    """Back up and replace the exchange state, guarded by ``expected``.

    The live ``SimulatedExchange`` persists this file on its own schedule, so
    the same rule as the journal applies: if it moved since we read it, our
    in-memory copy is stale and writing it would discard whatever the exchange
    just saved.
    """
    return swap_in(path, json.dumps(state, indent=2, default=str), expected)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True, help="SimulatedExchange state JSON (data/exchange_state.json)")
    ap.add_argument("--journal", default=None, help="data/trade_journal.jsonl: repair its stale rows and prediction_correct flags too (listed on a dry run)")
    ap.add_argument("--win-rates", dest="win_rates", default=None,
                    help="data/strategy_win_rates.json: rebuild the FR-0.6 Kelly windows from the repaired journal (needs --journal)")
    ap.add_argument("--allow-window-shrink", dest="allow_shrink", action="store_true",
                    help="write a rebuilt win-rate window even when it is shorter than the stored one (default: skip and warn)")
    ap.add_argument("--apply", action="store_true", help="rewrite the file(s); default: dry run")
    ap.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    args = ap.parse_args(argv)

    state, state_key = read_json_snapshot(args.state)
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
    # The state is swapped first, while its snapshot is freshest -- the journal
    # and win-rate passes below take time the live exchange could use to persist.
    if corrections and args.apply:
        summary["backup"] = write_state_atomic(args.state, state, state_key)
        summary["applied"] = True
    journal_result = None
    if args.journal:
        journal_result = repair_journal(args.journal, apply=bool(args.apply))
        summary["journal"] = {
            "path": args.journal,
            "n_corrected": len(journal_result["corrections"]),
            "n_flags_corrected": len(journal_result["flag_corrections"]),
            "n_unmatched_skipped": len(journal_result["skipped_unmatched"]),
            "corrections": journal_result["corrections"],
            "flag_corrections": journal_result["flag_corrections"],
            "skipped_unmatched": journal_result["skipped_unmatched"],
            "applied": journal_result["applied"],
            "backup": journal_result["backup"],
        }
    # After the journal: the windows are replayed from the REPAIRED rows.
    win_result = None
    if args.win_rates:
        # No journal (or no journal FILE) means no outcomes to replay. Rebuilding
        # from an empty list would propose emptying every window.
        rows = journal_result["rows"] if journal_result and not journal_result.get("note") else None
        win_result = repair_win_rates(
            args.win_rates, rows, apply=bool(args.apply), allow_shrink=bool(args.allow_shrink)
        )
        summary["win_rates"] = {"path": args.win_rates, **win_result}
    pending = (
        bool(corrections)
        or bool(journal_result and (journal_result["corrections"] or journal_result["flag_corrections"]))
        or bool(win_result and win_result["changes"])
    )
    applied_all = (
        (not corrections or summary["applied"])
        and (journal_result is None
             or not (journal_result["corrections"] or journal_result["flag_corrections"])
             or journal_result["applied"])
        and (win_result is None or not win_result["changes"] or win_result["applied"])
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
            + _tail(summary["applied"], summary.get("backup"), bool(args.apply), bool(corrections))
        )
        if journal_result is not None:
            if journal_result.get("note"):
                print(f"journal: {journal_result['note']}")
            for c in journal_result["corrections"]:
                print(
                    f"journal line {c['line']} {c['symbol']} {c['reason']} settled={c['settlement_outcome']}: "
                    f"exit {c['exit_price_old']:.2f}->{c['exit_price_new']:.2f} pnl {c['pnl_old']:+.2f}->{c['pnl_new']:+.2f}"
                )
            for c in journal_result["flag_corrections"]:
                print(
                    f"journal prediction_correct line {c['line']} {c['symbol']} "
                    f"settled={c['settlement_outcome']} side={c['contract_side']} "
                    f"pnl {c['pnl']:+.2f}: {c['old']} -> {c['new']}"
                )
            print(
                f"journal: {len(journal_result['corrections'])} stale NO-side row(s), "
                f"{len(journal_result['flag_corrections'])} prediction_correct flag(s) to correct; "
                + _tail(journal_result["applied"], journal_result["backup"], bool(args.apply),
                        bool(journal_result["corrections"] or journal_result["flag_corrections"]))
            )
        if win_result is not None:
            if win_result.get("note"):
                print(f"win_rates: {win_result['note']}")
            for w in win_result["warnings"]:
                print(f"WARN win_rates {w}")
            for c in win_result["changes"]:
                print(
                    f"win_rates {c['strategy']}: {_summarize_window(c['old'])}  ->  "
                    f"{_summarize_window(c['new'])}"
                )
            print(
                f"win_rates: {len(win_result['changes'])} strategy window(s) to rebuild; "
                + _tail(win_result["applied"], win_result["backup"], bool(args.apply),
                        bool(win_result["changes"]))
            )
    return 0 if (not pending or applied_all) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConcurrentWriteError, RepairLockedError) as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        sys.exit(2)
