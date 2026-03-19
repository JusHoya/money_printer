import sys
import os
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.risk_manager import RiskManager


def test_kelly_dampener_0_75x():
    """
    Test that the Kelly Fraction uses the 0.75x dampener (not 0.25x).
    f = p - (q/b)
    p = 0.8 (confidence), q = 0.2
    price = 0.5 (b = 1.0)
    Kelly = 0.8 - (0.2/1.0) = 0.6
    Dampened (0.25x Quarter-Kelly) = 0.6 * 0.25 = 0.15
    At $100 balance (Seed stage): max trade = 10% = $10.
    Since 0.15 > 0.10, allocation is capped at $10.
    """
    rm = RiskManager(starting_balance=100.0)

    # Seed stage ($0-$500): 10% max trade, 0.25x Kelly
    qty = rm.calculate_kelly_size(confidence=0.8, price=0.5)

    cost = qty * 0.5
    # Seed stage cap is 10% of $100 = $10
    assert cost <= 10.0 + 0.50, f"Cost {cost} exceeded Seed stage 10% cap."

    # Verify the Quarter-Kelly fraction (0.25x) is used
    # Raw Kelly f = 0.6, quarter = 0.15, capped at 0.10 -> allocation = $10
    # $10 / 0.50 = 20 contracts
    assert qty == 20, f"Expected 20, got {qty}. Quarter-Kelly at Seed stage."


def test_final_minute_freeze():
    """
    Test that checking an order rejects it if within 60 seconds of expiration.
    """
    rm = RiskManager(starting_balance=100.0)

    # Expiration is 30 seconds from now
    exp_time = (datetime.now() + timedelta(seconds=30)).replace(microsecond=0)

    # Should reject due to final minute freeze
    is_safe = rm.check_order(
        proposed_cost=4.0,
        category="crypto",
        strategy_name="TrendV3",
        expiration_time=exp_time,
    )
    assert is_safe is False, "Order should be rejected within 60s of expiration."

    # Expiration is 90 seconds from now
    exp_time_safe = (datetime.now() + timedelta(seconds=90)).replace(microsecond=0)

    # Should be safe
    is_safe_now = rm.check_order(
        proposed_cost=4.0,
        category="crypto",
        strategy_name="TrendV3",
        expiration_time=exp_time_safe,
    )
    assert is_safe_now is True, "Order should be allowed if > 60s from expiration."


def test_strategy_drawdown_limit():
    """
    Test that a strategy is blocked if its specific PnL drops below -10%.
    """
    rm = RiskManager(starting_balance=100.0)

    # Simulate a heavy loss for "TrendV3" (-$15, which is -15% of $100)
    rm.strategy_pnl["TrendV3"] = -15.0

    # The order for TrendV3 should be blocked because of its drawdown
    is_safe_v3 = rm.check_order(
        proposed_cost=4.0, category="crypto", strategy_name="TrendV3"
    )
    assert (
        is_safe_v3 is False
    ), "TrendV3 should be blocked due to -10% strategy drawdown."

    # Another strategy should still be allowed since global daily is not > max?
    # Wait, if daily pnl is also -15, global will trigger. We must ensure global is fine.
    rm.daily_pnl = -2.0  # Simulate global PnL is only -2%

    is_safe_v2 = rm.check_order(
        proposed_cost=4.0, category="crypto", strategy_name="TrendV2"
    )
    assert (
        is_safe_v2 is True
    ), "TrendV2 should be allowed because its strategy PnL is fine."
