"""Tests for the configurable probabilistic fill model (Task E, 2026-06-03).

The exchange has an opt-in ``realistic_fills`` flag (DEFAULT OFF). When off,
every order fills exactly as before (byte-identical legacy behaviour). When on,
penny-floor orders ($0.01-$0.05) fill with probability < 1, scaled by a
queue-position / adverse-selection penalty so the cheapest contracts fill
least often.
"""

import pytest

from src.core.matching_engine import SimulatedExchange


# ----------------------------------------------------------------------
# Default OFF must reproduce current behaviour exactly
# ----------------------------------------------------------------------


def test_default_is_off():
    ex = SimulatedExchange()
    assert ex.realistic_fills is False


def test_off_always_fills_penny_floor():
    """With the model off, even a $0.01 order always opens a position."""
    ex = SimulatedExchange()
    for i in range(200):
        ex.open_position(
            f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="ML BTC 15m"
        )
    assert len(ex.positions) == 200
    assert ex.penny_floor_requested == 0
    assert ex.penny_floor_skipped == 0


def test_off_fill_probability_is_one_everywhere():
    ex = SimulatedExchange()  # off
    for price in (0.01, 0.02, 0.05, 0.06, 0.50, 0.99):
        assert ex.penny_floor_fill_probability(price, "buy") == 1.0


def test_off_path_byte_identical_to_legacy_open():
    """Position dict + scalar counters identical whether flag is unset or False."""
    ex_default = SimulatedExchange()
    ex_explicit = SimulatedExchange(realistic_fills=False)
    for ex in (ex_default, ex_explicit):
        ex.open_position(
            "KXBTC15M-26JAN010000-45", "buy", 0.01, 10, strategy_name="ML BTC 15m"
        )
    p1 = ex_default.positions[0]
    p2 = ex_explicit.positions[0]
    # Compare the load-bearing fields (ids/timestamps differ by construction).
    for key in ("entry_price", "quantity", "entry_fee", "is_maker", "contract_side"):
        assert p1[key] == p2[key]
    assert ex_default.realized_pnl == ex_explicit.realized_pnl
    assert ex_default.cumulative_entry_fees == ex_explicit.cumulative_entry_fees


# ----------------------------------------------------------------------
# Realistic ON reduces penny-floor fill rate
# ----------------------------------------------------------------------


def test_on_reduces_penny_floor_fill_rate():
    """With the model on, far fewer $0.01 orders fill than requested."""
    n = 2000
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.5, fill_rng_seed=7)
    for i in range(n):
        ex.open_position(
            f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="ML BTC 15m"
        )
    filled = len(ex.positions)
    # p_fill at $0.01 = 0.5 (base) * 0.5 (max adverse-selection penalty) = 0.25
    assert filled < n  # strictly fewer than requested
    assert ex.penny_floor_requested == n
    assert ex.penny_floor_skipped == n - filled
    # Observed fill rate near the modelled 0.25 (loose band for RNG noise).
    assert 0.18 < (filled / n) < 0.32


def test_on_off_diverge_for_penny_floor():
    """Same workload: ON fills strictly fewer than OFF."""
    n = 1000
    off = SimulatedExchange()
    on = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.5, fill_rng_seed=1)
    for i in range(n):
        sym = f"KXBTC15M-X-{i}-45"
        off.open_position(sym, "buy", 0.02, 10, strategy_name="S")
        on.open_position(sym, "buy", 0.02, 10, strategy_name="S")
    assert len(off.positions) == n
    assert len(on.positions) < n


def test_on_does_not_touch_non_penny_orders():
    """Orders priced above the penny band always fill, even with the model on."""
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.25, fill_rng_seed=3)
    for i in range(300):
        ex.open_position(f"KXBTC15M-X-{i}-45", "buy", 0.40, 10, strategy_name="S")
    assert len(ex.positions) == 300  # none skipped
    assert ex.penny_floor_requested == 0
    assert ex.penny_floor_skipped == 0


def test_adverse_selection_cheaper_fills_less():
    """The penalty makes $0.01 fill less often than $0.05."""
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.6)
    p_low = ex.penny_floor_fill_probability(0.01, "buy")
    p_high = ex.penny_floor_fill_probability(0.05, "buy")
    assert p_low < p_high
    assert p_low == pytest.approx(0.6 * 0.5, abs=1e-9)  # bottom of band: half base
    assert p_high == pytest.approx(0.6 * 1.0, abs=1e-9)  # top of band: full base


def test_fill_probability_clamped_to_band():
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.5)
    assert ex.penny_floor_fill_probability(0.009, "buy") == 1.0  # below band
    assert ex.penny_floor_fill_probability(0.051, "buy") == 1.0  # above band
    assert ex.penny_floor_fill_probability(0.50, "buy") == 1.0


def test_reproducible_with_seed():
    """Same seed -> same number of fills."""

    def run():
        ex = SimulatedExchange(
            realistic_fills=True, penny_fill_prob=0.5, fill_rng_seed=99
        )
        for i in range(500):
            ex.open_position(f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="S")
        return len(ex.positions)

    assert run() == run()


def test_p_fill_zero_skips_all_penny_floor():
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.0, fill_rng_seed=5)
    for i in range(100):
        ex.open_position(f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="S")
    # 0.0 base * any penalty = 0.0 -> nothing fills.
    assert len(ex.positions) == 0
    assert ex.penny_floor_skipped == 100


def test_skipped_order_charges_no_fee():
    """A no-fill order must not deduct a fee or bump the cumulative ledger."""
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.0, fill_rng_seed=5)
    ex.open_position("KXBTC15M-X-0-45", "buy", 0.01, 10, strategy_name="S")
    assert len(ex.positions) == 0
    assert ex.realized_pnl == 0.0
    assert ex.total_fees_paid == 0.0
    assert ex.cumulative_entry_fees == 0.0
    assert ex.cumulative_fees_paid == 0.0
