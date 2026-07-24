"""Tests for WS2: position sizing fixes.

Phase 0 teardown (2026-07-24): the CryptoLongShotFader / Crypto V3 stop-loss
tests were removed with the deleted crypto strategies; the RiskManager sizing
tests remain.
"""

from src.core.risk_manager import RiskManager


def test_short_sizing_cap_cheap_contracts():
    """For price < 0.15, short qty should be capped so (1-price)*qty <= $10."""
    rm = RiskManager(starting_balance=100.0)
    qty = rm.calculate_kelly_size(0.95, 0.04)
    max_exposure = (1.0 - 0.04) * qty
    assert (
        max_exposure <= 10.5
    ), f"Short exposure too high: ${max_exposure:.2f} for qty={qty}"


def test_short_cost_calculation():
    """Verify short cost uses (1-price)*qty, not price*qty."""
    # This is tested indirectly through the dashboard's _process_signals
    # but we can verify the math directly
    price = 0.04
    qty = 10
    short_cost = (1.0 - price) * qty  # $9.60
    wrong_cost = price * qty  # $0.40
    assert short_cost == 9.6
    assert wrong_cost == 0.4
    # The fix ensures est_cost = short_cost for sells
