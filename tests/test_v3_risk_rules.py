import sys
import os
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.risk_manager import RiskManager


def test_kelly_dampener_0_75x():
    """
    Sprint 6 + PR#1 Kelly blends historical WR (60%) with confidence (40%) and
    deducts round-trip fees from effective odds.

    PRD Phase 2 corrected the fee model: Kalshi's maker multiplier defaults to
    zero, and the weather series this project trades are not on the
    non-standard-fee table, so a resting order costs $0.00 rather than the 1.75%
    previously charged. `calculate_kelly_size` prices the maker path, so on a
    standard series the fee term is zero and sizing equals the fee-free odds:

    p = 0.6 * 0.50 + 0.4 * 0.80 = 0.62
    fee_per (maker at 0.50, standard series) = 0.00
    net_win = 0.50, net_loss = 0.50  ->  b = 1.0
    f = 0.62 - 0.38/1.0 = 0.24
    Quarter-Kelly = 0.24 * 0.25 = 0.06
    At $100 balance (Seed stage): min(0.06, 0.10) = 0.06 -> $6.00
    $6.00 / $0.50 = 12 contracts

    This test pins the arithmetic, not the presence of the fee deduction:
    because the fee is zero here, deleting the deduction entirely would leave
    it green. `test_kelly_deducts_maker_fee_on_a_maker_fee_series` is the guard
    that fails if the deduction is removed.
    """
    rm = RiskManager(starting_balance=100.0)

    # Seed stage ($0-$500): 10% max trade, 0.25x Kelly
    qty = rm.calculate_kelly_size(
        confidence=0.8, price=0.5, symbol="KXHIGHNY-26JUL27-B82.5"
    )

    cost = qty * 0.5
    # Seed stage cap is 10% of $100 = $10
    assert cost <= 10.0 + 0.50, f"Cost {cost} exceeded Seed stage 10% cap."

    assert qty == 12, f"Expected 12, got {qty}. Fee-free maker Kelly at Seed stage."


def test_kelly_deducts_maker_fee_on_a_maker_fee_series():
    """Kelly's fee deduction must be live, provable on a series that is billed.

    This is the regression guard PR#1 intended. It cannot live on a KXHIGH*
    weather symbol: Kalshi's maker multiplier is zero there, so the fee-adjusted
    and fee-ignorant answers are both 12 and the guard is vacuous. KXAAAGASM
    (PRD Phase 4 gas) is on the schedule's "Non-Standard Fees" table and does
    bill resting liquidity, so the deduction moves the answer:

    fee_per (maker at 0.50, KXAAAGASM) = ceil(0.0175 * 0.50 * 0.50) = $0.01
    net_win = 0.50 - 0.02 = 0.48,  net_loss = 0.50 + 0.02 = 0.52
    b = 0.923077
    f = 0.62 - 0.38/0.923077 = 0.208333
    Quarter-Kelly = 0.052083 -> $5.2083 at $100 -> 10 contracts

    Deleting the fee deduction in `RiskManager.calculate_kelly_size` returns 12
    here and fails this test. Two facts are pinned together: the deduction
    exists, and the series fee type reaches it.
    """
    rm = RiskManager(starting_balance=100.0)

    gas_qty = rm.calculate_kelly_size(
        confidence=0.8, price=0.5, symbol="KXAAAGASM-26AUG-B3.25"
    )
    assert gas_qty == 10, (
        f"Expected 10 on a maker-fee series, got {gas_qty}. "
        "12 means the fee deduction was skipped or the series fee type "
        "never reached compute_fee."
    )

    weather_qty = rm.calculate_kelly_size(
        confidence=0.8, price=0.5, symbol="KXHIGHNY-26JUL27-B82.5"
    )
    assert gas_qty < weather_qty, (
        "A series that bills makers must size strictly smaller than one that "
        f"does not (gas={gas_qty}, weather={weather_qty})."
    )


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
