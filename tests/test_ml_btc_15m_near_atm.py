"""Regression tests for the ML BTC 15m near-ATM proximity filter.

The 5-agent production review (2026-06-26) found MLBtc15mStrategy made ZERO
trades in 15 days. Root cause: Kalshi sets each 15-min contract floor_strike
~= BTC spot at the interval start, so spot is permanently ~0.01-0.04% from the
strike -- always inside the old always-on near-ATM filter band, which therefore
rejected 100% of available contracts.

These tests guard the fix: the filter is OFF by default (near_atm_threshold=0.0)
so near-ATM contracts now produce signals, while the skip mechanism is preserved
and re-enables when near_atm_threshold > 0. They mirror the predictor stub
pattern in tests/test_sprint3_strategies.py and stay hermetic (no network, no
real model).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.core.interfaces import MarketData
from src.strategies.ml_btc_15m import MLBtc15mStrategy

# Live proof from the review: KXBTC15M-26JUN262100-00
#   strike=59874.81, spot=59882.00 -> |spot-strike|/strike = 0.0120% near-ATM.
NEAR_ATM_STRIKE = 59874.81
NEAR_ATM_SPOT = 59882.00


def _make_near_atm_market(bid=0.55, ask=0.60):
    """A real-world near-ATM BTC 15m contract (spot ~0.012% from strike)."""
    # Minute 8 of a 15-min cycle (inside the trading window), daytime UTC.
    timestamp = datetime(2026, 6, 26, 21, 8, 0)
    close_time = timestamp + timedelta(minutes=7)
    return MarketData(
        symbol="KXBTC15M-26JUN262100-00",
        timestamp=timestamp,
        price=NEAR_ATM_SPOT,
        volume=100,
        bid=bid,
        ask=ask,
        extra={
            "spot_price": NEAR_ATM_SPOT,
            "close_time": close_time,
            "no_bid": 0.0,
            "no_ask": 0.0,
            "source": "test",
            "strike": NEAR_ATM_STRIKE,
            "time_to_expiry": 420.0,
        },
    )


def _bullish_predictor():
    """MagicMock predictor with a strong bullish edge (fair 0.75 vs ask 0.60)."""
    predictor = MagicMock()
    predictor.predict_btc.return_value = {
        "probability": 0.75,
        "confidence": 0.70,
        "fair_value": 0.75,
        "recommended_price": 0.73,
    }
    return predictor


def test_near_atm_contract_produces_signal_by_default():
    """Default strategy (filter OFF) trades a near-ATM contract.

    This is THE guard proving the old 100% rejection of near-ATM contracts is
    gone: proximity here is ~0.012%, far inside any legacy band, yet a signal
    is produced because the edge (0.75 - 0.60 = 0.15) clears min_edge (0.05).
    """
    predictor = _bullish_predictor()
    strat = MLBtc15mStrategy(predictor=predictor)
    md = _make_near_atm_market(ask=0.60)

    signals = strat.analyze(md)

    assert len(signals) == 1
    assert signals[0].side == "buy"
    assert signals[0].contract_side == "YES"
    predictor.predict_btc.assert_called_once()


def test_near_atm_threshold_still_skips_when_enabled():
    """near_atm_threshold>0 preserves the skip for the same near-ATM contract."""
    predictor = _bullish_predictor()
    strat = MLBtc15mStrategy(predictor=predictor, near_atm_threshold=0.01)
    md = _make_near_atm_market(ask=0.60)

    # proximity 0.012% < 1% threshold -> skipped before the predictor runs.
    assert strat.analyze(md) == []
    predictor.predict_btc.assert_not_called()
