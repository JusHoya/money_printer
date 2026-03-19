"""Tests for the empirical edge strategy.

Validates the settlement probability lookup table, edge computation,
time window gating, and signal generation for both YES and NO sides.
"""

from datetime import datetime, timedelta


from src.core.interfaces import MarketData
from src.strategies.empirical_edge import (
    EmpiricalEdgeStrategy,
    compute_edge,
    compute_ev_per_dollar,
    lookup_empirical_rate,
)


def _make_market(
    bid=0.50,
    ask=0.55,
    minutes_left=7,
    symbol="KXBTC15M-26MAR191430-30",
    strike=70000.0,
):
    now = datetime(2026, 3, 19, 14, 23, 0)  # Minute 8 of cycle
    close = now + timedelta(minutes=minutes_left)
    return MarketData(
        symbol=symbol,
        timestamp=now,
        price=70200.0,
        volume=100,
        bid=bid,
        ask=ask,
        extra={
            "strike": strike,
            "close_time": close,
            "time_to_expiry": minutes_left * 60,
            "source": "test",
        },
    )


# ===========================================================================
# Lookup table tests
# ===========================================================================


class TestLookupTable:
    def test_cheap_contract_rate(self):
        rate = lookup_empirical_rate(0.01)
        assert rate is not None
        assert rate < 0.05  # Cheap YES wins < 5% of time

    def test_mid_range_rate(self):
        rate = lookup_empirical_rate(0.45)
        assert rate is not None
        assert 0.20 < rate < 0.50  # Below implied (overpriced YES)

    def test_expensive_contract_rate(self):
        rate = lookup_empirical_rate(0.85)
        assert rate is not None
        assert rate > 0.90  # High YES actually wins > 90%

    def test_out_of_range_returns_none(self):
        assert lookup_empirical_rate(1.5) is None
        assert lookup_empirical_rate(-0.1) is None

    def test_all_buckets_covered(self):
        """Every price from 0.01 to 0.99 should have a lookup."""
        for cents in range(1, 100):
            price = cents / 100.0
            assert lookup_empirical_rate(price) is not None, f"No rate for {price}"


class TestComputeEdge:
    def test_overpriced_yes_returns_no(self):
        """At 35c, YES wins only 17.5% vs 35% implied -> BUY NO."""
        side, edge = compute_edge(0.35)
        assert side == "NO"
        assert edge > 0.10  # ~17.5% edge

    def test_underpriced_yes_returns_yes(self):
        """At 75c, YES wins 90.9% vs 75% implied -> BUY YES."""
        side, edge = compute_edge(0.75)
        assert side == "YES"
        assert edge > 0.10

    def test_near_efficient_returns_small_edge(self):
        """At 97c, YES wins 97.1% vs 96.5% implied -> tiny edge."""
        side, edge = compute_edge(0.97)
        assert edge < 0.02  # Near efficient

    def test_extreme_cheap_small_edge(self):
        """At 1c, YES wins 1.9% vs 1% implied -> small positive edge for YES."""
        side, edge = compute_edge(0.01)
        assert edge < 0.02


class TestEVPerDollar:
    def test_overpriced_yes_negative_ev(self):
        """At 35c YES, actual=17.5% -> EV for YES buyer is negative."""
        ev = compute_ev_per_dollar(0.35, side="YES")
        assert ev < 0  # Bad bet to buy YES here

    def test_overpriced_yes_positive_no_ev(self):
        """At 35c YES bid, NO side should have positive EV."""
        ev = compute_ev_per_dollar(0.35, side="NO")
        assert ev > 0  # Good bet to buy NO

    def test_underpriced_yes_positive_ev(self):
        """At 75c YES, actual=90.9% -> EV for YES buyer is positive."""
        ev = compute_ev_per_dollar(0.75, side="YES")
        assert ev > 0

    def test_ev_matches_edge_direction(self):
        """EV sign should match edge direction."""
        for price in [0.25, 0.35, 0.45, 0.75, 0.85]:
            side, edge = compute_edge(price)
            if side == "YES":
                assert compute_ev_per_dollar(price, "YES") > 0
            elif side == "NO":
                assert compute_ev_per_dollar(price, "NO") > 0


# ===========================================================================
# Strategy signal tests
# ===========================================================================


class TestEmpiricalEdgeStrategy:
    def test_buy_no_in_overpriced_yes_zone(self):
        """Contract at 25c YES bid -> empirical says YES wins 10.7% -> BUY NO."""
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.25, ask=0.28, minutes_left=7)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "NO"

    def test_buy_yes_in_underpriced_zone(self):
        """Contract at 75c YES ask -> empirical says YES wins 90.9% -> BUY YES."""
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.73, ask=0.75, minutes_left=7)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "YES"

    def test_no_signal_in_efficient_zone(self):
        """At 55c, edge is ~7% (close to threshold) — depends on min_edge."""
        strat = EmpiricalEdgeStrategy(min_edge=0.10, min_ev_per_dollar=0.10)
        md = _make_market(bid=0.53, ask=0.55, minutes_left=7)
        signals = strat.analyze(md)
        assert len(signals) == 0  # 6.9% edge < 10% threshold

    def test_no_signal_near_settlement(self):
        """< 3 minutes remaining -> no signal."""
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_minutes_remaining=3)
        md = _make_market(bid=0.25, ask=0.28, minutes_left=2)
        signals = strat.analyze(md)
        assert len(signals) == 0

    def test_no_signal_too_early(self):
        """> max_minutes remaining -> no signal."""
        strat = EmpiricalEdgeStrategy(min_edge=0.05, max_minutes_remaining=12)
        md = _make_market(bid=0.25, ask=0.28, minutes_left=14)
        signals = strat.analyze(md)
        assert len(signals) == 0

    def test_cooldown_prevents_double_trade(self):
        strat = EmpiricalEdgeStrategy(min_edge=0.05, cooldown_seconds=300)
        md = _make_market(bid=0.25, ask=0.28, minutes_left=7)
        signals1 = strat.analyze(md)
        assert len(signals1) == 1

        signals2 = strat.analyze(md)
        assert len(signals2) == 0  # Cooldown active

    def test_strike_set_on_signal(self):
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.25, ask=0.28, minutes_left=7, strike=70332.0)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert getattr(signals[0], "strike", None) == 70332.0

    def test_stop_loss_set(self):
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.25, ask=0.28, minutes_left=7)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert hasattr(signals[0], "stop_loss")
        assert signals[0].stop_loss > 0

    def test_skip_near_settled_market(self):
        """Ask >= 1.0 means already settled."""
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.99, ask=1.00, minutes_left=7)
        assert strat.analyze(md) == []

    def test_skip_zero_bid(self):
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.0, ask=0.0, minutes_left=7)
        assert strat.analyze(md) == []

    def test_confidence_reflects_empirical_rate(self):
        """Confidence should be based on actual win rate, not market price."""
        strat = EmpiricalEdgeStrategy(min_edge=0.05, min_ev_per_dollar=0.05)
        md = _make_market(bid=0.73, ask=0.75, minutes_left=7)
        signals = strat.analyze(md)
        assert len(signals) == 1
        # At 75c, empirical YES rate is ~91% -> confidence should be high
        assert signals[0].confidence > 0.85
