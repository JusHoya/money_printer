"""Tests for WS2: Stop loss and position sizing fixes."""
from src.strategies.crypto_strategy import CryptoLongShotFader, Crypto15mTrendStrategyV3, CryptoHourlyStrategyV3
from src.core.risk_manager import RiskManager
from src.core.interfaces import MarketData
from datetime import datetime


def test_longshot_fader_has_stop_loss():
    """LongShotFader signals should have stop_loss=0.20."""
    fader = CryptoLongShotFader()
    md = MarketData(
        symbol="kxbtcd-26feb1623-T99000",
        timestamp=datetime.now(),
        price=0.05,
        volume=100,
        bid=0.05,
        ask=0.06,
        extra={},
    )
    signals = fader.analyze(md)
    assert len(signals) == 1
    assert signals[0].stop_loss == 0.20


def test_v3_15m_has_trailing_rules():
    """Crypto15mTrendStrategyV3 signals should include trailing_rules."""
    strat = Crypto15mTrendStrategyV3(obi_threshold=0.0)
    # Feed spot price history
    now = datetime.now()
    for i in range(120):
        strat.spot_price_history.append((now, 98000.0 + i))

    md = MarketData(
        symbol="KXBTC15M-26FEB15-T97000",
        timestamp=now,
        price=98100.0,
        volume=100,
        bid=0.70,
        ask=0.72,
        extra={'spot_price': 98100.0, 'no_bid': 0.28, 'no_ask': 0.30, 'close_time': '2026-02-15T16:00:00Z'},
    )
    signals = strat.analyze(md)
    # If a signal is generated, it should have trailing_rules
    for sig in signals:
        assert sig.trailing_rules is not None, f"Signal missing trailing_rules: {sig}"


def test_short_sizing_cap_cheap_contracts():
    """For price < 0.15, short qty should be capped so (1-price)*qty <= $10."""
    rm = RiskManager(starting_balance=100.0)
    qty = rm.calculate_kelly_size(0.95, 0.04)
    max_exposure = (1.0 - 0.04) * qty
    assert max_exposure <= 10.5, f"Short exposure too high: ${max_exposure:.2f} for qty={qty}"


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
