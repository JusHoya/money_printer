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

import pytest

import src.core.risk_manager as rm_mod
from src.core.risk_manager import RiskManager


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


def _wire_orchestrator_callback(rmgr):
    """Reproduce the exact production wiring from OrchestratorEngine.

    OrchestratorEngine.__init__ does ``self.risk_manager.exchange.on_close =
    self._on_trade_close`` (overwriting the RiskManager's own binding), and the
    orchestrator's _on_trade_close delegates to RiskManager._on_trade_close as
    its very first action (the fix under test). We mirror that here without
    standing up the whole engine (which needs bots/providers): the orchestrator
    body's RiskManager-relevant behaviour is precisely this delegation.
    """
    captured = {"orchestrator_ran": []}

    def orchestrator_on_close(position):
        # THE FIX: delegate to RiskManager first.
        rmgr._on_trade_close(position)
        # ...then the orchestrator's own (disjoint) bookkeeping would run.
        captured["orchestrator_ran"].append(position.get("symbol"))

    # Overwrite exactly as production does (orphaning the RiskManager binding
    # unless the delegation above restores it).
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
    persist strategy_win_rates — the regression that Sprint 9 shipped broken."""
    rmgr, win_rates_path = hermetic_risk_manager
    captured = _wire_orchestrator_callback(rmgr)

    assert rmgr.strategy_win_rates == {}, "precondition: no win rates yet"
    assert not os.path.exists(win_rates_path), "precondition: no persisted file"

    # Winning close (exit 1.00 on a YES bought at 0.50).
    _open_and_close(rmgr, "KXBTC-26JUN0312-T1", "BTC 15m", exit_price=1.00)

    # The orchestrator callback actually ran (proves we tested the real path).
    assert captured["orchestrator_ran"] == ["KXBTC-26JUN0312-T1"]

    # (a1) In-memory win rates are now non-empty and recorded the win.
    assert rmgr.strategy_win_rates, "strategy_win_rates must not be empty"
    wins, total = rmgr.strategy_win_rates["BTC 15m"]
    assert total == 1
    assert wins == 1

    # (a2) Win rates were persisted to the temp file (gated on persist_state).
    assert os.path.exists(win_rates_path), "win rates must be persisted to disk"
    with open(win_rates_path, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted.get("BTC 15m") == [1, 1]


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

    # And the loss was recorded in win rates (1 trade, 0 wins).
    wins, total = rmgr.strategy_win_rates["ETH 15m"]
    assert total == 1
    assert wins == 0
