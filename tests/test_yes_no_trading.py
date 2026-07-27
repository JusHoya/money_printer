"""Tests for WS4: YES/NO dual-side contract handling.

Phase 0 teardown (2026-07-24): the CryptoHourlyStrategyV3 signal test was
removed with the deleted crypto strategies; the exchange-level YES/NO
mechanics tests remain.
"""

from src.core.matching_engine import SimulatedExchange
from src.core.interfaces import TradeSignal


def test_no_contract_pnl_calculation():
    """NO contract PnL should be based on inverted price."""
    closed = []
    ex = SimulatedExchange(on_close=lambda p: closed.append(p))

    # BUY NO at $0.96 (equivalent to YES at $0.04)
    ex.open_position(
        symbol="kxbtcd-26feb1623-T99000",
        side="buy",
        entry_price=0.96,
        quantity=10,
        stop_loss=0.0,
        contract_side="NO",
        strategy_name="Test NO",
    )

    pos = ex.positions[0]
    assert pos["contract_side"] == "NO"

    # When YES estimate is 0.02 (meaning NO is worth 0.98), we should be in profit
    # The update_market calculates estimated YES price, then inverts for NO
    pos["last_market_price"] = 0.98  # NO price went up
    ex._check_profit_targets(pos, 0.98)
    # +0.02 move, should not hit first target (needs +0.05)
    assert len(closed) == 0


def test_trade_signal_has_contract_side():
    """TradeSignal should default to 'YES' and support 'NO'."""
    sig_default = TradeSignal(symbol="TEST", side="buy", quantity=1)
    assert sig_default.contract_side == "YES"

    sig_no = TradeSignal(symbol="TEST", side="buy", quantity=1, contract_side="NO")
    assert sig_no.contract_side == "NO"


def test_position_stores_contract_side():
    """Positions should track contract_side."""
    ex = SimulatedExchange()
    ex.open_position("TEST", "buy", 0.96, 10, contract_side="NO")
    pos = ex.positions[0]
    assert pos["contract_side"] == "NO"

    ex.open_position("TEST2", "buy", 0.50, 10)
    pos2 = ex.positions[1]
    assert pos2["contract_side"] == "YES"
