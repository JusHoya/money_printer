"""Tests for WS3: Profit-taking / partial exits."""

from src.core.matching_engine import SimulatedExchange


def test_partial_exit_at_first_target():
    """At +$0.15 move, 33% of position should be closed."""
    closed = []
    ex = SimulatedExchange(on_close=lambda p: closed.append(p))

    ex.open_position(
        symbol="KXBTC15M-TEST-T98000",
        side="buy",
        entry_price=0.50,
        quantity=30,
        stop_loss=0.40,
        strategy_name="Test",
    )

    pos = ex.positions[0]
    pos["last_market_price"] = 0.65  # +0.15 move (first target)

    # Manually trigger profit target check
    ex._check_profit_targets(pos, 0.65)

    assert len(closed) == 1
    assert closed[0]["quantity"] == 9  # 33% of 30 = ~10, int(30*0.33)=9
    assert closed[0]["pnl"] > 0
    assert pos["quantity"] == 21  # 30 - 9


def test_partial_exit_at_second_target():
    """At +$0.30 move, 50% of remaining should be closed."""
    closed = []
    ex = SimulatedExchange(on_close=lambda p: closed.append(p))

    ex.open_position(
        symbol="KXBTC15M-TEST-T98000",
        side="buy",
        entry_price=0.50,
        quantity=30,
        stop_loss=0.40,
        strategy_name="Test",
    )

    pos = ex.positions[0]

    # Hit first target (+0.15)
    ex._check_profit_targets(pos, 0.65)
    assert pos["quantity"] == 21

    # Hit second target (+0.30)
    ex._check_profit_targets(pos, 0.80)
    assert len(closed) == 2
    assert closed[1]["quantity"] == 10  # 50% of 21 = 10
    assert pos["quantity"] == 11


def test_profit_targets_on_sell_side():
    """Profit targets should work for sell positions too."""
    closed = []
    ex = SimulatedExchange(on_close=lambda p: closed.append(p))

    ex.open_position(
        symbol="KXBTC15M-TEST-T98000",
        side="sell",
        entry_price=0.50,
        quantity=30,
        stop_loss=0.60,
        strategy_name="Test",
    )

    pos = ex.positions[0]

    # For sell, profit = entry - current, so price drop = profit
    ex._check_profit_targets(pos, 0.35)  # +0.15 move in our favor (first target)
    assert len(closed) == 1
    assert closed[0]["pnl"] > 0


def test_position_fields_initialized():
    """New positions should have profit_targets, original_quantity, and last_market_price."""
    ex = SimulatedExchange()
    ex.open_position("TEST", "buy", 0.50, 10)
    pos = ex.positions[0]
    assert "profit_targets" in pos
    assert "original_quantity" in pos
    assert pos["original_quantity"] == 10
    # last_market_price is initialized to 0 as a sentinel meaning "no real
    # market price yet" — matching_engine relies on `lmp > 0` to detect when
    # a real Kalshi orderbook price has been observed (see lines 533, 628).
    assert pos["last_market_price"] == 0
    assert pos["contract_side"] == "YES"
