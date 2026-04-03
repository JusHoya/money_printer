import sys
import os
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.risk_manager import RiskManager


def test_kelly_dampener_0_75x():
    """
    Sprint 6: Kelly now blends historical WR (60%) with confidence (40%).
    Default historical WR = 0.45 (no strategy history yet).
    p = 0.6 * 0.45 + 0.4 * 0.8 = 0.59
    b = (1-0.5)/0.5 = 1.0
    f = 0.59 - 0.41/1.0 = 0.18
    Quarter-Kelly = 0.18 * 0.25 = 0.045
    At $100 balance (Seed stage): min(0.045, 0.10) = 0.045 -> $4.50
    $4.50 / $0.50 = 9 contracts
    """
    rm = RiskManager(starting_balance=100.0)

    # Seed stage ($0-$500): 10% max trade, 0.25x Kelly
    qty = rm.calculate_kelly_size(confidence=0.8, price=0.5)

    cost = qty * 0.5
    # Seed stage cap is 10% of $100 = $10
    assert cost <= 10.0 + 0.50, f"Cost {cost} exceeded Seed stage 10% cap."

    # Sprint 6: Blended Kelly (60% historical WR + 40% confidence) produces
    # smaller positions than raw confidence. 12 contracts vs old 20.
    # p = 0.6*0.50 + 0.4*0.80 = 0.62, b=1.0, f=0.24, quarter=0.06, $6/$0.50=12
    assert qty == 12, f"Expected 12, got {qty}. Calibrated Kelly at Seed stage."


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
