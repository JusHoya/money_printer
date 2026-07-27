"""Regression test for the orphaned-callback bug (the recurring "Fix B").

Background
----------
``RiskManager`` wires ``self.exchange.on_close = self._on_trade_close`` in its
constructor so that, on every settled trade, it records per-strategy win rates,
persists them, and arms loss / strategy cooldowns.

The OrchestratorEngine then *overwrites* ``exchange.on_close`` with its OWN
``_on_trade_close`` (for journaling / dashboard / online-update) both at
construction (run_dashboard.py:~52) and again on every cycle reset
(run_dashboard.py:~817). Before the fix the orchestrator callback did NOT
delegate back to ``RiskManager._on_trade_close``, so all of RiskManager's
win-rate recording, persistence, and cooldown logic was silently DEAD — the
exact failure that shipped in Sprint 9 (``strategy_win_rates.json`` empty in
prod, cooldowns never firing).

The fix makes the orchestrator callback call ``risk_manager._on_trade_close``
FIRST. This test reproduces production wiring (orchestrator callback assigned
to ``exchange.on_close``, delegating to RiskManager) and asserts the chain is
intact: win rates get recorded AND persisted, and a losing close arms the
per-symbol loss cooldown.
"""

import json
import os
import types

import pytest

import src.core.risk_manager as rm_mod
from src.core.risk_manager import RiskManager

# 2026-06-10 fix: import the REAL orchestrator so the regression test exercises
# the actual OrchestratorEngine._on_trade_close bound method (not an inline
# replica). Reverting the delegation at scripts/run_dashboard.py:167 must now
# FAIL this test. Importing the module is heavy (pulls providers/bots) but does
# not start any threads or network calls at import time.
from scripts.run_dashboard import OrchestratorEngine


@pytest.fixture
def hermetic_risk_manager(tmp_path, monkeypatch):
    """A RiskManager(persist_state=True) whose persistence is redirected to a
    temp dir so the test never touches prod state files.

    Both the exchange state file and the win-rates file are patched at the
    module level BEFORE construction, because the constructor reads
    ``_DEFAULT_STATE_FILE`` (via ``_load_state``) and ``WIN_RATES_PATH`` (via
    ``_load_win_rates``) eagerly.
    """
    win_rates_path = tmp_path / "strategy_win_rates.json"
    exchange_state_path = tmp_path / "exchange_state.json"

    monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", str(win_rates_path))
    monkeypatch.setattr(rm_mod, "_DEFAULT_STATE_FILE", exchange_state_path)

    rmgr = RiskManager(starting_balance=3000.0, persist_state=True)
    return rmgr, win_rates_path


class _OrchestratorStub:
    """Minimal carrier for the REAL OrchestratorEngine._on_trade_close.

    2026-06-10 fix: we bind the actual ``OrchestratorEngine._on_trade_close``
    function onto this stub (via ``types.MethodType``) rather than re-implementing
    the delegation inline. The bound method reaches into ``self.risk_manager``,
    ``self.dashboard``, ``self.trade_journal``, ``self.online_updater`` and
    ``self.bots`` — so we supply exactly those, with the real RiskManager and
    cheap no-op fakes for the disjoint journaling/dashboard/online-update bits.

    Because the binding is the real method body, deleting the
    ``self.risk_manager._on_trade_close(position)`` delegation in
    OrchestratorEngine._on_trade_close makes the win-rate / cooldown assertions
    below FAIL.
    """

    def __init__(self, rmgr, captured):
        self.risk_manager = rmgr
        self._captured = captured

        # --- disjoint bookkeeping the real method also touches (kept inert) ---
        class _Dashboard:
            def record_strategy_trade_result(_self, strategy_name, pnl):
                captured["dashboard_results"].append((strategy_name, pnl))

        class _Journal:
            def record(_self, outcome):
                captured["journaled"].append(outcome)

        class _Online:
            def on_trade_close(_self):
                captured["online_checked"] += 1

        self.dashboard = _Dashboard()
        self.trade_journal = _Journal()
        self.online_updater = _Online()
        self.bots = []  # Late Sniper forwarding loop iterates this (empty = no-op)


def _wire_orchestrator_callback(rmgr):
    """Reproduce the exact production wiring from OrchestratorEngine, binding
    the REAL OrchestratorEngine._on_trade_close as exchange.on_close.

    OrchestratorEngine.__init__ does ``self.risk_manager.exchange.on_close =
    self._on_trade_close`` (overwriting the RiskManager's own binding). Here we
    bind the genuine method to a lightweight stub and assign it identically, so
    the only thing keeping RiskManager's win-rate/cooldown logic alive is the
    delegation inside the real method body (the fix under test).
    """
    captured = {
        "orchestrator_ran": [],
        "dashboard_results": [],
        "journaled": [],
        "online_checked": 0,
    }
    stub = _OrchestratorStub(rmgr, captured)

    # Bind the REAL function as a bound method on the stub.
    bound_on_close = types.MethodType(OrchestratorEngine._on_trade_close, stub)

    def orchestrator_on_close(position):
        bound_on_close(position)  # runs the genuine delegation + bookkeeping
        captured["orchestrator_ran"].append(position.get("symbol"))

    # Overwrite exactly as production does (orphaning the RiskManager binding
    # unless the real method's delegation restores it).
    rmgr.exchange.on_close = orchestrator_on_close
    return captured


def _open_and_close(rmgr, symbol, strategy_name, exit_price):
    """Open a YES position and close it at ``exit_price`` to fire on_close.

    Entry at $0.50; exit > 0.50 is a win, < 0.50 a loss (modulo fees).
    """
    ex = rmgr.exchange
    pos = ex.open_position(
        symbol=symbol,
        side="buy",
        entry_price=0.50,
        quantity=10,
        strategy_name=strategy_name,
        contract_side="YES",
    )
    # open_position returns the position dict (or appends to ex.positions).
    if pos is None:
        pos = ex.positions[-1]
    ex._close_position(pos, exit_price, reason="EARLY_SETTLEMENT")
    return pos


def test_win_rate_recorded_and_persisted_through_orchestrator_callback(
    hermetic_risk_manager,
):
    """A close routed through the orchestrator callback must populate AND
    persist the win-rate window (strategy_win_rates.json) — the regression
    that Sprint 9 shipped broken."""
    rmgr, win_rates_path = hermetic_risk_manager
    captured = _wire_orchestrator_callback(rmgr)

    # FR-0.6: win-rate storage is now a recency window per strategy
    # (strategy_win_records: name -> deque of 1/0), not cumulative
    # (wins, total) counters. Same callback chain under test, new schema.
    assert rmgr.strategy_win_records == {}, "precondition: no win records yet"
    assert not os.path.exists(win_rates_path), "precondition: no persisted file"

    # Winning close (exit 1.00 on a YES bought at 0.50).
    _open_and_close(rmgr, "KXBTC-26JUN0312-T1", "BTC 15m", exit_price=1.00)

    # The orchestrator callback actually ran (proves we tested the real path).
    assert captured["orchestrator_ran"] == ["KXBTC-26JUN0312-T1"]

    # (a1) In-memory win records are now non-empty and recorded the win.
    assert rmgr.strategy_win_records, "strategy_win_records must not be empty"
    assert list(rmgr.strategy_win_records["BTC 15m"]) == [1]

    # (a2) Win records were persisted to the temp file (gated on
    # persist_state) in the FR-0.6 shape:
    # {"strategy": {"window": [...], "updated": iso}}.
    assert os.path.exists(win_rates_path), "win rates must be persisted to disk"
    with open(win_rates_path, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted.get("BTC 15m", {}).get("window") == [1]
    assert persisted["BTC 15m"].get("updated"), "persisted entry lacks timestamp"


def test_losing_close_arms_loss_cooldown_through_orchestrator_callback(
    hermetic_risk_manager,
):
    """A losing close routed through the orchestrator callback must arm the
    per-symbol loss cooldown (dead before the fix)."""
    rmgr, _ = hermetic_risk_manager
    _wire_orchestrator_callback(rmgr)

    symbol = "KXETH-26JUN0312-T1"
    assert symbol not in rmgr.loss_cooldown

    # Losing close (exit 0.00 on a YES bought at 0.50 => clear loss).
    _open_and_close(rmgr, symbol, "ETH 15m", exit_price=0.00)

    # (b) Loss cooldown is now armed for this exact ticker.
    assert symbol in rmgr.loss_cooldown, "losing close must arm loss_cooldown"

    # And the loss was recorded in the FR-0.6 win window (one outcome: a loss).
    assert list(rmgr.strategy_win_records["ETH 15m"]) == [0]
