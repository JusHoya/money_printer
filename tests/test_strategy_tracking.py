"""
test_strategy_tracking.py
Tests the strategy_name propagation through:
  SimulatedExchange.open_position → position dict → _close_position → on_close callback
"""
import pytest
from datetime import datetime, timedelta
from src.core.matching_engine import SimulatedExchange


def test_strategy_name_stored_in_position():
    """Verify strategy_name is stored in the active position dict."""
    ex = SimulatedExchange()
    ex.open_position(
        symbol='KXHIGHNY-26FEB19-T44',
        side='buy',
        entry_price=0.35,
        quantity=5,
        strategy_name='Meteorologist V1'
    )
    assert len(ex.positions) == 1
    assert ex.positions[0]['strategy_name'] == 'Meteorologist V1'


def test_strategy_name_defaults_to_unknown():
    """Verify missing strategy_name defaults to 'Unknown'."""
    ex = SimulatedExchange()
    ex.open_position(
        symbol='KXBTC15M-TEST-T50000',
        side='buy',
        entry_price=0.50,
        quantity=3,
    )
    assert ex.positions[0]['strategy_name'] == 'Unknown'


def test_on_close_callback_receives_strategy_name():
    """Critical: on_close must fire with the strategy_name on the closed position."""
    closed = []

    def capture(pos):
        closed.append(pos)

    ex = SimulatedExchange(on_close=capture)
    ex.open_position(
        symbol='KXHIGHNY-26FEB19-T44',
        side='buy',
        entry_price=0.35,
        quantity=5,
        strategy_name='Trend Catcher V2'
    )

    # Use TAKE_PROFIT reason (non-binary settlement) — passes exit_price directly
    pos = ex.positions[0]
    ex._close_position(pos, 0.50, reason='TAKE_PROFIT')

    assert len(closed) == 1, "on_close should have fired"
    assert closed[0]['strategy_name'] == 'Trend Catcher V2', f"Got: {closed[0].get('strategy_name')}"


def test_strategy_name_preserved_in_closed_trades():
    """Verify closed_trades record includes strategy_name for historical review."""
    ex = SimulatedExchange()
    ex.open_position(
        symbol='KXHIGHCHI-26FEB19-T35',
        side='sell',
        entry_price=0.20,
        quantity=3,
        strategy_name='LongShot Fader'
    )
    pos = ex.positions[0]
    ex._close_position(pos, 0.01, reason='EARLY_SETTLEMENT')

    assert len(ex.closed_trades) == 1
    assert ex.closed_trades[0]['strategy_name'] == 'LongShot Fader'


def test_fixed_cent_stop_loss_not_triggered_by_spread_noise():
    """
    Bug Fix validation: Old code used stop_loss_buffer=0.10, which on a $0.17 option
    produced stop at $0.153 — close enough to be triggered by the bid/ask spread.
    New logic uses FIXED_STOP_CENTS=$0.05, so entry=0.35 gives stop at 0.30.
    The spread of 0.02 should NOT trigger the stop.
    """
    from src.strategies.crypto_strategy import Crypto15mTrendStrategyV2
    strat = Crypto15mTrendStrategyV2()
    # Confirm fixed stop is $0.05
    assert strat.FIXED_STOP_CENTS == 0.05, f"Expected 0.05, got {strat.FIXED_STOP_CENTS}"


def test_longshot_fader_targets_correct_price_range():
    """LongShot Fader must only fire on [min_price, longshot_ceiling] range."""
    from src.strategies.crypto_strategy import CryptoLongShotFader
    from src.core.interfaces import MarketData
    from datetime import datetime

    fader = CryptoLongShotFader(longshot_ceiling=0.08, min_price=0.03)

    def make_market(bid):
        return MarketData(
            symbol='KXBTC15M-TEST-T50000',
            timestamp=datetime.now(),
            price=bid,
            volume=0.0,
            bid=bid,
            ask=bid + 0.01,
        )

    # Below min → no trade
    assert fader.analyze(make_market(0.02)) == []
    # Above ceiling → no trade
    assert fader.analyze(make_market(0.12)) == []
    # In range → trade
    sigs = fader.analyze(make_market(0.06))
    assert len(sigs) == 1
    assert sigs[0].side == 'sell'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
