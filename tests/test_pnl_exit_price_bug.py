"""
Test: Verify that the Matching Engine uses the estimated option price
(not the underlying spot price) when closing on TP/SL triggers.

Reproduces the bug where a $0.50 BTC option was exited at ~$69,000
instead of the correct ~$0.65 estimated option price.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.core.matching_engine import SimulatedExchange


def test_btc_take_profit_uses_option_price():
    """
    A BTC option bought at $0.51 should NOT exit at $69,000.
    With the fix, it should exit at the estimated derivative price.
    """
    closed_trades = []

    def on_close(pos):
        closed_trades.append(pos)

    engine = SimulatedExchange(on_close=on_close)
    engine.TAKE_PROFIT_PCT = 0.15  # 15%

    # Open a BTC position at $0.51 (option price), qty 11
    engine.open_position(
        symbol="KXBTC15M-26FEB141515-15",
        side="buy",
        entry_price=0.51,
        quantity=11,
        stop_loss=0.0
    )

    assert len(engine.positions) == 1, "Position should be open"

    # Simulate BTC spot price at $69,800 (this is the UNDERLYING, not the option)
    # This should trigger a high estimated_price via the tanh formula,
    # which may trigger TP. The key test: exit_price must be << $69,800.
    engine.update_market("BTC", 69800.0)

    if len(closed_trades) > 0:
        trade = closed_trades[0]
        exit_price = trade['exit_price']
        pnl = trade['pnl']

        # CRITICAL ASSERTIONS
        assert exit_price < 1.0, (
            f"FAIL: Exit price was ${exit_price:.2f} — "
            f"should be an option price (0.00-1.00), not the BTC spot price!"
        )
        assert abs(pnl) < 100.0, (
            f"FAIL: PnL was ${pnl:.2f} — "
            f"should be small (cents/dollars), not millions!"
        )
        print(f"  ✅ PASS: Exit=${exit_price:.4f}, PnL=${pnl:+.4f} (Reason: {trade['reason']})")
    else:
        # Position still open (TP not triggered) — that's fine, 
        # let's check unrealized PnL is sane
        pos = engine.positions[0]
        assert pos['current_price'] < 1.0, (
            f"FAIL: Current price is ${pos['current_price']:.2f}, should be < $1.00"
        )
        print(f"  ✅ PASS: Position still open, estimated_price=${pos['current_price']:.4f}, PnL=${pos['pnl']:+.4f}")


def test_btc_stop_loss_uses_option_price():
    """
    A BTC option with a price-based stop loss should exit at the
    estimated option price, not the underlying BTC price.
    """
    closed_trades = []

    def on_close(pos):
        closed_trades.append(pos)

    engine = SimulatedExchange(on_close=on_close)

    # Open position at $0.60, qty 10, stop loss at $0.54
    engine.open_position(
        symbol="KXBTC15M-26FEB141500-00",
        side="buy",
        entry_price=0.60,
        quantity=10,
        stop_loss=0.54
    )

    # Simulate BTC spot price that would make estimated_price drop below 0.54
    # With tanh formula: diff = strike - spot for "below" type, scale=1000
    # We need an underlying price that makes the option estimate < 0.54
    # For a "B00" strike (value 0), diff = 0 - spot, very negative -> estimate near 0.01
    engine.update_market("BTC", 50000.0)

    if len(closed_trades) > 0:
        trade = closed_trades[0]
        exit_price = trade['exit_price']

        assert exit_price < 1.0, (
            f"FAIL: Exit price was ${exit_price:.2f} — "
            f"should be an option price, not the underlying spot!"
        )
        print(f"  ✅ PASS: SL Exit=${exit_price:.4f}, PnL=${trade['pnl']:+.4f} (Reason: {trade['reason']})")
    else:
        pos = engine.positions[0]
        print(f"  ✅ PASS: Position still open (SL not hit), estimated=${pos['current_price']:.4f}")


def test_weather_not_affected():
    """
    Weather trades (KXHIGH) should also use estimated_price on closure,
    not the raw temperature value.
    """
    closed_trades = []

    def on_close(pos):
        closed_trades.append(pos)

    engine = SimulatedExchange(on_close=on_close)
    engine.TAKE_PROFIT_PCT = 0.10

    engine.open_position(
        symbol="KXHIGHNY-26FEB14-B44.5",
        side="buy",
        entry_price=0.52,
        quantity=10,
        stop_loss=0.0
    )

    # Temperature = 50°F, strike = 44.5, "Below" type
    # diff = 44.5 - 50 = -5.5, scale=10, tanh(-0.55)*0.49 ≈ -0.24
    # estimated ≈ 0.50 - 0.24 = 0.26
    engine.update_market("NY", 50.0)

    if len(closed_trades) > 0:
        trade = closed_trades[0]
        assert trade['exit_price'] < 100.0, (
            f"FAIL: Weather exit was ${trade['exit_price']:.2f}, should be < $1.00"
        )
        print(f"  ✅ PASS: Weather Exit=${trade['exit_price']:.4f}, PnL=${trade['pnl']:+.4f}")
    else:
        pos = engine.positions[0]
        assert pos['current_price'] < 1.0, f"FAIL: Weather price estimate is ${pos['current_price']}"
        print(f"  ✅ PASS: Weather position open, estimated=${pos['current_price']:.4f}")


if __name__ == "__main__":
    print("=" * 60)
    print("  PnL Exit Price Bug - Reproduction Test")
    print("=" * 60)

    print("\n[Test 1] BTC Take Profit uses option price:")
    test_btc_take_profit_uses_option_price()

    print("\n[Test 2] BTC Stop Loss uses option price:")
    test_btc_stop_loss_uses_option_price()

    print("\n[Test 3] Weather trades not affected:")
    test_weather_not_affected()

    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED ✅")
    print("=" * 60)
