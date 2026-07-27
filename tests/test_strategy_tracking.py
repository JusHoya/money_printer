"""
test_strategy_tracking.py
Tests the strategy_name propagation through:
  SimulatedExchange.open_position → position dict → _close_position → on_close callback
"""

import pytest
from src.core.matching_engine import SimulatedExchange


def test_strategy_name_stored_in_position():
    """Verify strategy_name is stored in the active position dict."""
    ex = SimulatedExchange()
    ex.open_position(
        symbol="KXHIGHNY-26FEB19-T44",
        side="buy",
        entry_price=0.35,
        quantity=5,
        strategy_name="Meteorologist V1",
    )
    assert len(ex.positions) == 1
    assert ex.positions[0]["strategy_name"] == "Meteorologist V1"


def test_strategy_name_defaults_to_unknown():
    """Verify missing strategy_name defaults to 'Unknown'."""
    ex = SimulatedExchange()
    ex.open_position(
        symbol="KXBTC15M-TEST-T50000",
        side="buy",
        entry_price=0.50,
        quantity=3,
    )
    assert ex.positions[0]["strategy_name"] == "Unknown"


def test_on_close_callback_receives_strategy_name():
    """Critical: on_close must fire with the strategy_name on the closed position."""
    closed = []

    def capture(pos):
        closed.append(pos)

    ex = SimulatedExchange(on_close=capture)
    # 2026-07-25 (PRD FR-1.5): was KXHIGHNY-26FEB19-T44. TAKE_PROFIT is now
    # refused on a weather bracket (held to settlement), so the reason and the
    # symbol have to agree. strategy_name propagation is symbol-agnostic and
    # the assertions below are unchanged; the weather families are covered by
    # tests/test_weather_lifecycle.py and test_weather_settlement_semantics.py.
    ex.open_position(
        symbol="KXBTC15M-26FEB19-44",
        side="buy",
        entry_price=0.35,
        quantity=5,
        strategy_name="Trend Catcher V2",
        strike=64000.0,
    )

    # Use TAKE_PROFIT reason (non-binary settlement) — passes exit_price directly
    pos = ex.positions[0]
    ex._close_position(pos, 0.50, reason="TAKE_PROFIT")

    assert len(closed) == 1, "on_close should have fired"
    assert (
        closed[0]["strategy_name"] == "Trend Catcher V2"
    ), f"Got: {closed[0].get('strategy_name')}"


def test_strategy_name_preserved_in_closed_trades():
    """Verify closed_trades record includes strategy_name for historical review."""
    ex = SimulatedExchange()
    # 2026-07-25 (PRD FR-1.5): was KXHIGHCHI-26FEB19-T35. EARLY_SETTLEMENT
    # invents a 1.00/0.00 outcome from a price peg, which is exactly NOT
    # "settle via FR-1.2", so it is now refused on a weather bracket. The
    # closed_trades bookkeeping under test is symbol-agnostic.
    ex.open_position(
        symbol="KXBTC15M-26FEB19-35",
        side="sell",
        entry_price=0.20,
        quantity=3,
        strategy_name="LongShot Fader",
        strike=64000.0,
    )
    pos = ex.positions[0]
    ex._close_position(pos, 0.01, reason="EARLY_SETTLEMENT")

    assert len(ex.closed_trades) == 1
    assert ex.closed_trades[0]["strategy_name"] == "LongShot Fader"


# Phase 0 teardown (2026-07-24): the Crypto15mTrendStrategyV2 fixed-cent
# stop-loss test and the CryptoLongShotFader price-range test were removed
# with the deleted crypto strategies.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
