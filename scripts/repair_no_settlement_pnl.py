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
  ``--preflight`` writes nothing at all and exits 3 if a live writer is found.
* Each file is backed up to ``<path>.bak-<n>`` (lowest free ``n``) before it is
  replaced, and the backup is copied from the file AS IT EXISTS AT SWAP TIME --
  not from the snapshot read at the start of the run.
* **The orchestrator must be STOPPED, not restarted, around an apply.**  See
  the runbook below: the running process holds these windows in memory and
  writes them out on every close and again on shutdown, so a ``docker restart``
  after an apply *reverts* it.  ``--apply`` scans for a running orchestrator
  and REFUSES when it finds one (``--allow-live-writer`` overrides).
* **Concurrency.** The sandbox settles positions on a timer and
  ``TradeJournal.record`` appends while this runs.  Rebuilding a file from a
  stale ``readlines()`` snapshot and swapping it in with ``os.replace`` would
  silently and irrecoverably destroy every row appended in between (the backup,
  written from the same snapshot, would not save them either).  So every
  ``--apply`` holds an exclusive ``<path>.repair-lock`` for the whole read ->
  swap of EACH file it rewrites -- state, journal and win rates alike -- records
  ``(mtime_ns, size, sha256)`` before AND after reading, and re-checks it
  immediately before the swap; ANY change aborts loudly with
  ``ConcurrentWriteError``.  The content hash is load-bearing rather than
  decorative: ``strategy_win_rates.json`` is rewritten IN PLACE by
  ``RiskManager._save_win_rates``, and the rewrite that matters -- a window
  flipping ``[1,1,1]`` -> ``[0,0,0]`` -- is byte-identical in LENGTH, so a
  ``(mtime, size)`` guard is blind on exactly the file and mutation at stake.
* **An abort is per-file, and the script says so.**  The passes run
  state -> journal -> win rates, so an abort in a later pass can leave an
  earlier artifact already rewritten.  The abort message then lists what landed
  ("PARTIALLY APPLIED ... NOT rolled back") instead of claiming nothing was
  written.  Re-running is always safe: every pass is idempotent, and the
  artifacts that already landed report 0 corrections the second time.
* **What the reconcile can no longer see.**  ``prediction_correct`` is now
  derived from settlement truth, which makes
  ``settlement_reconcile.sim_recorded_result`` a partly circular check of the
  same inputs (documented in
  :func:`src.ml.trade_journal.prediction_correct_for`).  The independent
  detector it used to provide by accident is
  :func:`src.ml.trade_journal.settlement_pnl_disagreement` -- recorded PnL vs
  the PnL the row's own settlement inputs imply -- and every run of this script
  reports it as ``PNL/SETTLEMENT DISAGREEMENT`` lines.  Those are REPORTED, not
  repaired: ``classify`` repairs the single shape it can prove; anything else
  is a finding for a human.

    python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl
    python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl --win-rates data/strategy_win_rates.json --apply


OPERATOR RUNBOOK -- maia sandbox (HTTP-only host, no ssh)
=========================================================
Run every command from a shell on the maia box (or wherever the ``mp-sandbox``
container runs).  The container bind-mounts ``/srv/money_printer/data`` at
``/app/data``, so the paths below are the real artifacts.

READ THIS FIRST -- why the container is STOPPED, not restarted
--------------------------------------------------------------
``RiskManager`` loads ``strategy_win_rates.json`` once at startup and keeps the
windows in memory.  It writes that memory back to the file:

* on EVERY position close -- ``risk_manager.py`` ``_on_trade_close`` ends with
  ``if self._persist_state: self._save_win_rates()``; and
* on shutdown -- ``OrchestratorEngine.shutdown`` step "2b. Save win rates"
  (``run_dashboard.py``), which ``scripts/run_web_dashboard.py`` reaches from
  BOTH ``atexit.register(engine.shutdown)`` and its SIGTERM handler.

``docker restart`` is SIGTERM (-> shutdown -> ``_save_win_rates`` writes the
PRE-repair in-memory windows over the file you just repaired) followed by a
start that loads those stale windows straight back.  It reverts the win-rate
half of this repair DETERMINISTICALLY.  ``docker stop`` fires the very same
save -- but it fires BEFORE the repair, so the repair writes last and the
subsequent ``docker start`` loads the repaired file.  The ordering is the whole
point:

    stop  ->  apply  ->  start           correct
    apply ->  restart                    reverts the win rates
    apply while running                  reverts them at the next settlement

The same is true mid-run: a position settling between the dry run and the apply
fires ``_save_win_rates`` and breaks Step 5's "0 corrections everywhere".  That
is why ``--apply`` refuses when it can see a running orchestrator, and why
Step 2 exists.

The journal half (``prediction_correct``) is NOT exposed to this -- the journal
is append-only and the runtime never rewrites earlier rows -- but do not use
that as a reason to skip the stop: the win-rate half is.

Step 0 -- capture the "before" from the HTTP API (from any machine)::

    curl -s http://maia.local:8050/api/win_rates
    # expect: {"ok":true,"data":{"ML Weather":{"window":[1,1,1],...},
    #                            "Meteorologist V2":{"window":[0,1,0],...}}}

Step 1 -- STOP the sandbox (NOT restart).  ``restart: unless-stopped`` will not
    bring it back by itself, and mp-autoheal only restarts containers that are
    running-but-unhealthy, so a stopped container stays stopped::

        docker stop mp-sandbox
        docker inspect -f '{{.State.Running}}' mp-sandbox
        # MUST print: false     <- this is the conclusive check; do not go on
        #                          until it does

Step 2 -- PRE-FLIGHT.  Writes nothing; exits 3 and refuses if it sees a writer.
    Run it in a throwaway container against the same bind mount (``docker exec``
    is not available while the sandbox is stopped -- that is the point)::

        docker run --rm -v /srv/money_printer/data:/app/data \
            money-printer-sandbox:latest \
            python scripts/repair_no_settlement_pnl.py --preflight \
                --state data/exchange_state.json \
                --journal data/trade_journal.jsonl \
                --win-rates data/strategy_win_rates.json

    It watches the data directory for 10s and scans for an orchestrator
    process.  Expect ``preflight: CLEAR`` and exit 0.  A ``preflight: REFUSE``
    means something is still writing -- go back to Step 1.  A ``preflight:
    INCONCLUSIVE`` means neither detector could run (no ``/proc`` and
    ``--watch-seconds 0``) so nothing was checked -- it exits 3 rather than
    handing back a green light nobody earned.

    Note what the process scan can and cannot do: a throwaway container has its
    own PID namespace, so it cannot see the sandbox container's process.  The
    write probe can (both see the same bind mount), and ``docker inspect`` in
    Step 1 is conclusive.  Treat CLEAR as corroboration of Step 1, not a
    substitute for it.

Step 3 -- DRY RUN.  Writes nothing.  Read the output before going on::

    docker run --rm -v /srv/money_printer/data:/app/data \
        money-printer-sandbox:latest \
        python scripts/repair_no_settlement_pnl.py \
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
        journal: 0 stale NO-side row(s), 4 prediction_correct flag(s) to correct, 0 PnL/settlement disagreement(s); dry run (pass --apply to rewrite)
        win_rates ML Weather: window n=3 wins=3 (100%)  ->  window n=3 wins=0 (0%)
        win_rates Meteorologist V2: window n=3 wins=1 (33%)  ->  window n=3 wins=2 (67%)
        win_rates: 2 strategy window(s) to rebuild, 0 legacy entry/entries to drop; dry run (pass --apply to rewrite)
        (exit code 1 -- repairs pending)

    The real ``--state`` on maia is NOT empty the way the verification copy was
    (``closed_trades`` was cleared by a cycle reset there), so its first line
    may legitimately report more closed trades; what must match is
    ``0 NO-side settlement(s) to repair``.

    STOP if the counts differ.  In particular:
    * a fifth flag, or a ``WARN win_rates ... shorter`` line, means the journal
      no longer matches what this runbook was written against.  A shrink WARN
      means the journal cannot account for the whole live window; that strategy
      is skipped unless you pass ``--allow-window-shrink``, and you should work
      out why first;
    * a ``WARN win_rates ... LONGER`` line means a window would GROW, which
      changes Kelly sizing -- check the extra closes belong to that strategy;
    * any ``PNL/SETTLEMENT DISAGREEMENT`` line is a row whose money contradicts
      its own bracket and which this script will NOT repair.  Investigate
      before applying.

Step 4 -- APPLY::

    docker run --rm -v /srv/money_printer/data:/app/data \
        money-printer-sandbox:latest \
        python scripts/repair_no_settlement_pnl.py \
            --state data/exchange_state.json \
            --journal data/trade_journal.jsonl \
            --win-rates data/strategy_win_rates.json \
            --apply

    Expect the same lines, each tail now reading ``APPLIED (backup
    data/....bak-N)``, and exit code 0.

    If it prints ``REFUSED: a live writer was detected`` the sandbox is still
    up -- go back to Step 1; nothing was written.

    If it prints ``ABORTED: ... changed while the repair was running`` read the
    next line before re-running.  ``Nothing was written.`` means exactly that;
    ``PARTIALLY APPLIED`` lists the artifacts that DID land (they are not rolled
    back).  Either way the fix is the same: run the same command again -- every
    pass is idempotent, so what already landed reports 0 corrections.

Step 5 -- confirm the backups exist (they are the undo) and verify on disk::

    ls -la /srv/money_printer/data/ | grep bak-

    docker run --rm -v /srv/money_printer/data:/app/data \
        money-printer-sandbox:latest \
        python scripts/repair_no_settlement_pnl.py \
            --state data/exchange_state.json --journal data/trade_journal.jsonl \
            --win-rates data/strategy_win_rates.json
    # expect: 0 corrections everywhere, 0 disagreements, exit code 0 (idempotent)

    docker run --rm -v /srv/money_printer/data:/app/data \
        money-printer-sandbox:latest \
        python scripts/settlement_reconcile.py --offline --json
    # expect: no mismatch attributable to these four rows -- this is the check
    # that was raising a FALSE breach at 06:00Z every day.  Note it is now a
    # partly circular check (see Safety, "what the reconcile can no longer
    # see"); the PNL/SETTLEMENT DISAGREEMENT count above is the independent one.

Step 6 -- START the sandbox again.  It loads the repaired windows at startup::

        docker start mp-sandbox
        curl -s http://maia.local:8050/healthz

Step 7 -- verify through the API (the acceptance check)::

    curl -s http://maia.local:8050/api/win_rates
    # expect: ML Weather        -> "window":[0,0,0]
    #         Meteorologist V2  -> "window":[1,1,0]

    curl -s "http://maia.local:8050/api/journal?last_n=500" | grep -o '"prediction_correct":[a-z]*'
    # expect, in row order: false,false,false,true,true,false

    If ML Weather still reads [1,1,1] here, the container was RESTARTED rather
    than stopped-applied-started, and step 2b of the shutdown put the old
    windows back.  Restore nothing -- just stop it and redo from Step 1.

Rollback -- stop, restore the ``.bak-<n>`` copies, start::

    docker stop mp-sandbox
    cp /srv/money_printer/data/trade_journal.jsonl.bak-1 /srv/money_printer/data/trade_journal.jsonl
    cp /srv/money_printer/data/strategy_win_rates.json.bak-1 /srv/money_printer/data/strategy_win_rates.json
    docker start mp-sandbox
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

BINARY_SETTLEMENT_REASONS = ("EXPIRATION", "EARLY_SETTLEMENT")
TOL = 1e-6

# A process running one of these is the live writer of every artifact this
# script rewrites: ``RiskManager._on_trade_close`` -> ``_save_win_rates`` on
# EVERY close, and ``OrchestratorEngine.shutdown`` step 2b on the way out.
LIVE_WRITER_MARKERS = ("run_web_dashboard.py", "run_dashboard.py")

# Indirection so a test can drive the quiescence probe without wall-clock waits.
_sleep = time.sleep

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


class LiveWriterError(RuntimeError):
    """The orchestrator is still running; it would overwrite this repair."""


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pnl_for(entry: float, exit_price: float, qty: float, side: str, exit_fee: float) -> float:
    gross = (exit_price - entry) * qty if side != "sell" else (entry - exit_price) * qty
    return gross - exit_fee


# ---------------------------------------------------------------------------
# Pre-flight: refuse to repair underneath a live writer
# ---------------------------------------------------------------------------
def find_live_writers(proc_root: str = "/proc") -> Dict[str, Any]:
    """Running orchestrator processes visible from here.

    ``{"supported": bool, "writers": [(pid, cmdline), ...], "why": str}``.

    A hit is CONCLUSIVE: that process rewrites ``strategy_win_rates.json`` on
    every position close and again from ``OrchestratorEngine.shutdown``, so any
    repair applied beside it is provisional at best.

    A miss is NOT proof.  ``/proc`` only shows this PID namespace, so a repair
    run in a throwaway ``docker run`` container cannot see the sandbox
    container's process at all.  ``supported`` says whether the scan could even
    run (it cannot on Windows); ``docker inspect -f '{{.State.Running}}'``
    remains the conclusive check and the runbook leads with it.
    """
    if not os.path.isdir(proc_root):
        return {"supported": False, "writers": [],
                "why": f"{proc_root} is not available on this host, so no process scan was possible"}
    me = os.getpid()
    writers: List[Tuple[int, str]] = []
    try:
        entries = sorted(os.listdir(proc_root))
    except OSError as exc:
        return {"supported": False, "writers": [], "why": f"could not read {proc_root}: {exc}"}
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue  # the process exited between listdir and open
        cmd = " ".join(p for p in raw.decode("utf-8", "replace").split("\0") if p)
        if any(marker in cmd for marker in LIVE_WRITER_MARKERS):
            writers.append((pid, cmd))
    return {"supported": True, "writers": writers, "why": ""}


def _watch_paths(targets: Sequence[str]) -> List[str]:
    """The targets plus every sibling file the runtime also writes.

    The orchestrator touches far more than the three artifacts under repair --
    the harvested CSV tapes, the exchange state, the journal -- so watching the
    whole directory is a much better liveness probe than watching the targets
    alone.  Our own ``.bak-*`` / ``.repair-lock`` files are excluded: they are
    written by US.
    """
    seen: Dict[str, None] = {}
    for target in targets:
        candidates = [os.path.abspath(target)]
        parent = os.path.dirname(os.path.abspath(target)) or "."
        with contextlib.suppress(OSError):
            candidates += [os.path.join(parent, n) for n in sorted(os.listdir(parent))]
        for path in candidates:
            if ".bak-" in path or path.endswith((".repair-lock", ".tmp")):
                continue
            if os.path.isfile(path):
                seen[path] = None
    return sorted(seen)


def _sample(paths: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for p in paths:
        with contextlib.suppress(OSError):
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
    return out


def watch_for_writes(targets: Sequence[str], seconds: float) -> List[str]:
    """Paths that changed over ``seconds`` -- evidence a writer is still alive.

    Cheap ``(mtime_ns, size)`` sampling twice, not hashing: this asks "is
    something writing RIGHT NOW", where a size-preserving rewrite is not the
    threat the swap guard has to worry about.  Files that APPEAR or VANISH
    during the window count too -- a new CSV tape row file is as much a live
    writer as a modified one.  An empty list is evidence of quiescence, not
    proof of it: a market loop between passes writes nothing either.
    """
    before = _sample(_watch_paths(targets))
    if seconds > 0:
        _sleep(seconds)
    after = _sample(_watch_paths(targets))
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def preflight(targets: Sequence[str], proc_root: str = "/proc",
              watch_seconds: float = 0.0) -> Dict[str, Any]:
    """``{"clear": bool, "writers": [...], "changed": [...], "notes": [...]}``.

    ``clear`` is False when EITHER detector fires, and ALSO when NEITHER could
    run (``inconclusive``): a check that checked nothing must not hand back a
    green light.  ``clear`` being True still means only "nothing proved a writer
    is alive" -- the notes spell out what could not be checked, so the verdict is
    never read as more than it is.
    """
    scan = find_live_writers(proc_root)
    notes: List[str] = []
    if not scan["supported"]:
        notes.append(f"process scan skipped: {scan['why']}")
    else:
        notes.append(
            f"process scan: no {'/'.join(LIVE_WRITER_MARKERS)} process in THIS pid "
            f"namespace (a container running one of its own is invisible from here)"
            if not scan["writers"] else
            f"process scan: {len(scan['writers'])} orchestrator process(es) running"
        )
    changed = watch_for_writes(targets, watch_seconds) if watch_seconds > 0 else []
    if watch_seconds > 0:
        notes.append(
            f"write probe: {len(changed)} file(s) changed in {watch_seconds:g}s "
            f"beside {', '.join(sorted({os.path.dirname(os.path.abspath(t)) or '.' for t in targets}))}"
        )
    else:
        notes.append("write probe skipped (--watch-seconds 0)")
    inconclusive = not scan["supported"] and watch_seconds <= 0
    return {"clear": not scan["writers"] and not changed and not inconclusive,
            "inconclusive": inconclusive,
            "writers": scan["writers"], "changed": changed,
            "proc_scan_supported": scan["supported"], "notes": notes}


def _live_writer_message(result: Dict[str, Any]) -> str:
    parts: List[str] = []
    for pid, cmd in result["writers"]:
        parts.append(f"pid {pid}: {cmd}")
    for path in result["changed"]:
        parts.append(f"{path} was written during the probe")
    return (
        "a live writer was detected -- "
        + "; ".join(parts)
        + ". The orchestrator rewrites strategy_win_rates.json on EVERY position "
          "close and again from OrchestratorEngine.shutdown, so a repair applied "
          "now is reverted the moment it closes a trade or the container stops. "
          "Stop the container first (docker stop mp-sandbox), then apply, then "
          "docker start. Pass --allow-live-writer only if you know this process "
          "does not write these files."
    )


# ---------------------------------------------------------------------------
# Concurrency: nothing is swapped in over a file that moved under us
# ---------------------------------------------------------------------------
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _stat_key(path: str) -> Tuple[int, int, str]:
    """``(mtime_ns, size, sha256)`` -- the identity a swap is guarded against.

    The hash is not belt-and-braces, it is the load-bearing part for
    ``strategy_win_rates.json``. That file is REWRITTEN IN PLACE by
    ``RiskManager._save_win_rates`` on every close, and the rewrite this repair
    exists to survive -- a window flipping ``[1,1,1]`` -> ``[0,0,0]`` -- is
    byte-identical in LENGTH. A ``(mtime_ns, size)`` guard is therefore blind
    on precisely the artifact and precisely the mutation that matter (mtime
    catches it only as far as the filesystem's timestamp granularity and the
    absence of a ``utime`` reaches, neither of which is a guarantee).
    """
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size, _sha256(path))


def _describe_key(key: Tuple[int, int, str]) -> str:
    mtime_ns, size, digest = key
    return f"mtime_ns={mtime_ns} size={size} sha256={digest[:12]}"


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


def read_lines_snapshot(path: str) -> Tuple[List[str], Tuple[int, int, str]]:
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
            f"{path} changed while it was being read (was {_describe_key(before)}, "
            f"now {_describe_key(after)}); this file was not written -- re-run"
        )
    return lines, after


def read_json_snapshot(path: str) -> Tuple[Any, Tuple[int, int, str]]:
    """``(obj, stat_key)``; raises if the file changed during the read."""
    before = _stat_key(path)
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)
    after = _stat_key(path)
    if before != after:
        raise ConcurrentWriteError(
            f"{path} changed while it was being read (was {_describe_key(before)}, "
            f"now {_describe_key(after)}); this file was not written -- re-run"
        )
    return obj, after


def _guard_unchanged(path: str, expected: Tuple[int, int, str]) -> None:
    now = _stat_key(path)
    if now != expected:
        raise ConcurrentWriteError(
            f"{path} changed while the repair was running (was "
            f"{_describe_key(expected)}, now {_describe_key(now)}); a live writer "
            f"touched it and swapping in our snapshot would destroy that. This "
            f"file was NOT written -- re-run (the repair is idempotent)."
        )


def _next_backup(path: str) -> str:
    n = 1
    while os.path.exists(f"{path}.bak-{n}"):
        n += 1
    return f"{path}.bak-{n}"


def swap_in(path: str, payload: str, expected: Tuple[int, int, str]) -> str:
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
                "pnl_settlement_disagreements": [], "rows": [], "applied": False,
                "backup": None, "note": f"journal not found: {journal_path}"}
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
    from src.ml.trade_journal import (
        expected_settlement_pnl,
        settlement_pnl_disagreement,
    )

    corrections: List[Dict[str, Any]] = []
    flag_corrections: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    # The INDEPENDENT settlement-truth detector (see the module docstring's
    # "what the reconcile can no longer see"): recorded pnl vs the pnl this
    # row's own settlement inputs imply. Reported, never repaired -- classify()
    # repairs the one shape it can prove; anything else needs a human.
    disagreements: List[Dict[str, Any]] = []
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
        delta = settlement_pnl_disagreement(row)
        if delta is not None:
            disagreements.append({
                "line": n,
                "symbol": row.get("symbol"),
                "settlement_outcome": row.get("settlement_outcome"),
                "contract_side": str(row.get("contract_side") or "YES").upper(),
                "pnl": _f(row.get("pnl")),
                "expected_pnl": expected_settlement_pnl(row),
                "delta": delta,
            })
        rows.append(row)
        if dirty:
            eol = "\r\n" if raw.endswith("\r\n") else "\n"
            out_lines.append(json.dumps(row) + eol)
        else:
            out_lines.append(raw)
    result: Dict[str, Any] = {"corrections": corrections, "flag_corrections": flag_corrections,
                              "skipped_unmatched": unmatched,
                              "pnl_settlement_disagreements": disagreements, "rows": rows,
                              "applied": False, "backup": None}
    return result, out_lines


# ---------------------------------------------------------------------------
# 4. strategy_win_rates.json -- the FR-0.6 Kelly windows
# ---------------------------------------------------------------------------
def rebuild_win_rates(rows: List[Dict[str, Any]], existing: Dict[str, Any],
                      allow_shrink: bool = False) -> Dict[str, Any]:
    """Replay ``RiskManager._on_trade_close``'s win record over the repaired journal.

    Returns ``{"win_rates": <the file to write>, "changes": [...],
    "drops": [...], "warnings": [...]}``.

    The rule is imported, not restated: ``1 if pnl > -FEE_TOLERANCE else 0``,
    last ``WIN_RATE_WINDOW`` per strategy, in journal order. Every closed row
    counts -- ``_on_trade_close`` does not care why a position closed, so a
    rebuild that only counted settlements would quietly drop the stop-loss and
    time-limit outcomes from the window.

    Window LENGTH is warned about in BOTH directions, because both change Kelly
    sizing (``calculate_kelly_size`` weighs the window's win rate against
    ``MIN_WIN_SAMPLES``, so ``n`` is an input, not a detail):

    * SHORTER than the stored window -> reported and SKIPPED unless
      ``allow_shrink``. The journal cannot account for the whole live window
      (it was rotated, or predates the file) and writing the short version
      would destroy outcomes this script cannot verify.
    * LONGER than the stored window -> reported and WRITTEN. The journal holds
      closes the stored window no longer does, so the longer window is the
      better record -- but growing ``n`` is a change of sizing input and must
      not land silently.

    Strategies absent from the journal are left exactly as they are, with ONE
    exception: an entry that is not in the FR-0.6 ``{"window": [...]}`` shape is
    a legacy ``[wins, total]`` counter, which ``RiskManager._load_win_rates``
    deliberately IGNORES (pivot reset) and ``_save_win_rates`` therefore drops
    at the next close. Copying it into the rebuilt file would resurrect, in the
    artifact, a record the runtime has already discarded -- so the rebuild drops
    it too, reported in ``drops`` rather than vanishing quietly.
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

    out: Dict[str, Any] = {}
    legacy: Dict[str, Any] = {}
    for name, value in (existing or {}).items():
        if isinstance(value, dict) and isinstance(value.get("window"), list):
            out[name] = dict(value)
        else:
            legacy[name] = value

    changes: List[Dict[str, Any]] = []
    drops: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for name in order:
        window = outcomes[name][-WIN_RATE_WINDOW:]
        prev = out.get(name)
        prev_window = prev.get("window") if isinstance(prev, dict) else None
        if isinstance(prev_window, list):
            if len(prev_window) > len(window):
                if not allow_shrink:
                    warnings.append(
                        f"{name}: rebuilt window is shorter than the stored one "
                        f"(n={len(window)} < n={len(prev_window)}); the journal cannot account "
                        f"for the whole window, so it is left untouched "
                        f"(pass --allow-window-shrink to overwrite it anyway)"
                    )
                    continue
            elif len(window) > len(prev_window):
                warnings.append(
                    f"{name}: rebuilt window is LONGER than the stored one "
                    f"(n={len(window)} > n={len(prev_window)}); the journal holds closes the "
                    f"stored window no longer does. It IS written -- but n is an input to "
                    f"Kelly sizing, so confirm the extra closes really belong to this "
                    f"strategy before you start the sandbox"
                )
        if prev_window == window:
            continue  # idempotent: identical window, keep the stored `updated`
        entry = dict(prev) if isinstance(prev, dict) else {}
        entry["window"] = window
        entry["updated"] = _rebuild_stamp()
        out[name] = entry
        changes.append({"strategy": name, "old": prev_window, "new": window})

    for name, value in legacy.items():
        if name in out:
            continue  # the journal rebuilt it into the FR-0.6 shape; not a drop
        drops.append({"strategy": name, "value": value})
        warnings.append(
            f"{name}: legacy/unknown win-rate entry {value!r} is not the FR-0.6 "
            f"{{'window': [...]}} shape; RiskManager._load_win_rates ignores it and "
            f"_save_win_rates drops it at the next close, so the rebuild drops it too "
            f"rather than resurrecting it"
        )
    return {"win_rates": out, "changes": changes, "drops": drops, "warnings": warnings}


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
        return {"changes": [], "drops": [], "warnings": [], "applied": False, "backup": None,
                "note": "win-rate rebuild needs --journal (the outcomes come from it)"}
    if not os.path.exists(win_rates_path):
        return {"changes": [], "drops": [], "warnings": [], "applied": False, "backup": None,
                "note": f"win rates not found: {win_rates_path}"}
    if not apply:
        existing, _ = read_json_snapshot(win_rates_path)
        result = rebuild_win_rates(rows, existing if isinstance(existing, dict) else {}, allow_shrink)
        return {"changes": result["changes"], "drops": result["drops"],
                "warnings": result["warnings"], "applied": False, "backup": None}
    with repair_lock(win_rates_path):
        existing, key = read_json_snapshot(win_rates_path)
        result = rebuild_win_rates(rows, existing if isinstance(existing, dict) else {}, allow_shrink)
        out: Dict[str, Any] = {"changes": result["changes"], "drops": result["drops"],
                               "warnings": result["warnings"], "applied": False, "backup": None}
        if result["changes"] or result["drops"]:
            payload = json.dumps(result["win_rates"], indent=2) + "\n"
            out["backup"] = swap_in(win_rates_path, payload, key)
            out["applied"] = True
        return out


def write_state_atomic(path: str, state: Dict[str, Any], expected: Tuple[int, int, str]) -> str:
    """Back up and replace the exchange state, guarded by ``expected``.

    The live ``SimulatedExchange`` persists this file on its own schedule, so
    the same rule as the journal applies: if it moved since we read it, our
    in-memory copy is stale and writing it would discard whatever the exchange
    just saved.
    """
    return swap_in(path, json.dumps(state, indent=2, default=str), expected)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default=None, help="SimulatedExchange state JSON (data/exchange_state.json)")
    ap.add_argument("--journal", default=None, help="data/trade_journal.jsonl: repair its stale rows and prediction_correct flags too (listed on a dry run)")
    ap.add_argument("--win-rates", dest="win_rates", default=None,
                    help="data/strategy_win_rates.json: rebuild the FR-0.6 Kelly windows from the repaired journal (needs --journal)")
    ap.add_argument("--allow-window-shrink", dest="allow_shrink", action="store_true",
                    help="write a rebuilt win-rate window even when it is shorter than the stored one (default: skip and warn)")
    ap.add_argument("--apply", action="store_true", help="rewrite the file(s); default: dry run")
    ap.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    ap.add_argument("--preflight", action="store_true",
                    help="ONLY check whether a live writer would revert this repair, then exit "
                         "(0 = nothing detected, 3 = a writer is running). Writes nothing, ever.")
    ap.add_argument("--watch-seconds", dest="watch_seconds", type=float, default=10.0,
                    help="--preflight: seconds to watch the artifacts' directory for writes (0 disables)")
    ap.add_argument("--allow-live-writer", dest="allow_live_writer", action="store_true",
                    help="apply even though a running orchestrator was detected. It WILL overwrite "
                         "the win rates on its next close or shutdown; only pass this if you know "
                         "the detected process does not write these files.")
    ap.add_argument("--proc-root", dest="proc_root", default="/proc",
                    help=argparse.SUPPRESS)  # test hook for the process scan
    return ap


def _preflight_targets(args: argparse.Namespace) -> List[str]:
    return [p for p in (args.state, args.journal, args.win_rates) if p]


def _run_preflight_command(args: argparse.Namespace) -> int:
    """``--preflight``: report, refuse, write nothing."""
    targets = _preflight_targets(args) or ["data/exchange_state.json"]
    result = preflight(targets, proc_root=args.proc_root, watch_seconds=args.watch_seconds)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for note in result["notes"]:
            print(f"preflight: {note}")
        for pid, cmd in result["writers"]:
            print(f"preflight: LIVE WRITER pid {pid}: {cmd}")
        for path in result["changed"]:
            print(f"preflight: LIVE WRITER wrote {path} during the probe")
        if result["clear"]:
            print("preflight: CLEAR -- nothing proved a writer is alive. This is evidence, "
                  "not proof: confirm with `docker inspect -f '{{.State.Running}}' mp-sandbox` "
                  "== false before applying.")
        elif result["inconclusive"]:
            print("preflight: INCONCLUSIVE -- neither detector could run (no /proc, and "
                  "--watch-seconds 0), so nothing was actually checked. Re-run with "
                  "--watch-seconds 10 on a host that shares the data bind mount, and "
                  "confirm `docker inspect -f '{{.State.Running}}' mp-sandbox` == false.")
        else:
            print("preflight: REFUSE -- " + _live_writer_message(result))
    return 0 if result["clear"] else 3


def _repair_all(args: argparse.Namespace, written: List[Tuple[str, str]]) -> int:
    """The repair itself. ``written`` accumulates (path, backup) AS THEY LAND, so
    an abort part-way through can say what is already on disk instead of
    claiming nothing was."""
    # Hold the lock for the state's whole read -> swap, exactly as the journal
    # and win-rate passes do for theirs. It is released before the next pass,
    # so the three locks are never held at once (no deadlock against a second
    # repair that takes them in the same order anyway).
    with contextlib.ExitStack() as stack:
        if args.apply:
            stack.enter_context(repair_lock(args.state))
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
            written.append((args.state, summary["backup"]))
    journal_result = None
    if args.journal:
        journal_result = repair_journal(args.journal, apply=bool(args.apply))
        summary["journal"] = {
            "path": args.journal,
            "n_corrected": len(journal_result["corrections"]),
            "n_flags_corrected": len(journal_result["flag_corrections"]),
            "n_unmatched_skipped": len(journal_result["skipped_unmatched"]),
            "n_pnl_settlement_disagreements": len(journal_result["pnl_settlement_disagreements"]),
            "corrections": journal_result["corrections"],
            "flag_corrections": journal_result["flag_corrections"],
            "skipped_unmatched": journal_result["skipped_unmatched"],
            "pnl_settlement_disagreements": journal_result["pnl_settlement_disagreements"],
            "applied": journal_result["applied"],
            "backup": journal_result["backup"],
        }
        if journal_result["applied"]:
            written.append((args.journal, journal_result["backup"]))
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
        if win_result["applied"]:
            written.append((args.win_rates, win_result["backup"]))
    pending = (
        bool(corrections)
        or bool(journal_result and (journal_result["corrections"] or journal_result["flag_corrections"]))
        or bool(win_result and (win_result["changes"] or win_result["drops"]))
    )
    applied_all = (
        (not corrections or summary["applied"])
        and (journal_result is None
             or not (journal_result["corrections"] or journal_result["flag_corrections"])
             or journal_result["applied"])
        and (win_result is None
             or not (win_result["changes"] or win_result["drops"])
             or win_result["applied"])
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
            for d in journal_result["pnl_settlement_disagreements"]:
                print(
                    f"PNL/SETTLEMENT DISAGREEMENT journal line {d['line']} {d['symbol']} "
                    f"settled={d['settlement_outcome']} side={d['contract_side']}: recorded pnl "
                    f"{d['pnl']:+.2f} but its own inputs imply {d['expected_pnl']:+.2f} "
                    f"(delta {d['delta']:+.2f}) -- NOT repaired, investigate"
                )
            print(
                f"journal: {len(journal_result['corrections'])} stale NO-side row(s), "
                f"{len(journal_result['flag_corrections'])} prediction_correct flag(s) to correct, "
                f"{len(journal_result['pnl_settlement_disagreements'])} PnL/settlement disagreement(s); "
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
            for d in win_result["drops"]:
                print(f"win_rates {d['strategy']}: legacy entry {d['value']!r} DROPPED")
            print(
                f"win_rates: {len(win_result['changes'])} strategy window(s) to rebuild, "
                f"{len(win_result['drops'])} legacy entry/entries to drop; "
                + _tail(win_result["applied"], win_result["backup"], bool(args.apply),
                        bool(win_result["changes"] or win_result["drops"]))
            )
    return 0 if (not pending or applied_all) else 1


def main(argv: Optional[List[str]] = None) -> int:
    """Parse, pre-flight, repair.

    Raises ``ConcurrentWriteError`` / ``RepairLockedError`` / ``LiveWriterError``
    rather than swallowing them; :func:`cli` is the layer that turns those into
    an exit code and an honest message about what did and did not land.
    """
    ap = _build_parser()
    args = ap.parse_args(argv)
    if args.preflight:
        return _run_preflight_command(args)
    if not args.state:
        ap.error("--state is required (or use --preflight, which needs no paths)")

    if args.apply:
        # The pre-flight guard on the apply path is the PROCESS SCAN only: it is
        # instant, whereas the write probe would add a wall-clock wait to every
        # run. A miss here is not proof of quiescence (see find_live_writers) --
        # `--preflight` and `docker inspect` are the operator-facing checks.
        scan = find_live_writers(args.proc_root)
        if scan["writers"] and not args.allow_live_writer:
            raise LiveWriterError(_live_writer_message(
                {"writers": scan["writers"], "changed": []}))
        if not scan["supported"]:
            print(f"WARN preflight: {scan['why']}; confirm the sandbox is STOPPED "
                  f"(docker inspect -f '{{{{.State.Running}}}}' mp-sandbox) before trusting "
                  f"this apply", file=sys.stderr)

    written: List[Tuple[str, str]] = []
    try:
        return _repair_all(args, written)
    except (ConcurrentWriteError, RepairLockedError) as exc:
        # main() swaps the state first and the journal second, so an abort in a
        # later pass can leave an EARLIER artifact already rewritten. Carrying
        # the list on the exception is what stops the message saying "nothing
        # written" when something was.
        exc.written = list(written)  # type: ignore[attr-defined]
        raise


def format_abort(exc: BaseException) -> str:
    """The operator-facing text for an aborted run -- what landed, what did not."""
    lines = [f"ABORTED: {exc}"]
    written = list(getattr(exc, "written", None) or [])
    if written:
        lines.append(
            "PARTIALLY APPLIED -- these artifacts WERE rewritten before the abort "
            "and are NOT rolled back:"
        )
        for path, backup in written:
            lines.append(f"  {path} (backup {backup})")
        lines.append(
            "  Re-run the same command to finish the rest; every pass is idempotent, "
            "so the artifacts above will report 0 corrections the second time."
        )
    else:
        lines.append("Nothing was written.")
    return "\n".join(lines)


def cli(argv: Optional[List[str]] = None) -> int:
    """``main`` plus the exit codes and messages: 2 = aborted, 3 = live writer."""
    try:
        return main(argv)
    except LiveWriterError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    except (ConcurrentWriteError, RepairLockedError) as exc:
        print(format_abort(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
