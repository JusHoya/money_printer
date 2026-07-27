"""Tests for Sprint 3 — ML-driven strategies, new strategies, and ML gating.

Covers all 8 new strategy files plus the ML EV gating layer in
SignalProcessorMixin.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.core.interfaces import MarketData, TradeSignal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_btc_market(
    symbol="KXBTC15M-26MAR19-1430-T84000",
    bid=0.55,
    ask=0.60,
    spot_price=84200.0,
    close_time=None,
    no_bid=0.0,
    no_ask=0.0,
    timestamp=None,
):
    """Create a MarketData that looks like fused BTC 15m data."""
    if timestamp is None:
        # Minute 8 of 15-min cycle (within trading window)
        timestamp = datetime(2026, 3, 19, 14, 38, 0)
    if close_time is None:
        close_time = timestamp + timedelta(minutes=7)
    return MarketData(
        symbol=symbol,
        timestamp=timestamp,
        price=spot_price,
        volume=100,
        bid=bid,
        ask=ask,
        extra={
            "spot_price": spot_price,
            "close_time": close_time,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "source": "test",
            "strike": 84000.0,
            "time_to_expiry": 420.0,
        },
    )


def make_weather_market(
    symbol="KXHIGHNY-26MAR19-T80",
    bid=0.60,
    ask=0.65,
    temperature_f=78.0,
    max_temp_today_f=79.0,
    nws_high=82,
    timestamp=None,
    strike_type="greater",
    floor_strike=80,
    cap_strike=None,
):
    """Create a MarketData that looks like fused weather data.

    PRD FR-1.1 (2026-07-25): the bracket fields are what the strategies now
    read contract direction from (``KalshiProvider`` supplies them on every
    market). The default models the existing ``-T80`` symbol as ``greater``
    floor=80 — "81 or above" — which is the reading the removed suffix parser
    was trying to express. No assertion in this file changed.
    """
    if timestamp is None:
        timestamp = datetime(2026, 3, 19, 12, 0, 0)
    return MarketData(
        symbol=symbol,
        timestamp=timestamp,
        price=0.60,
        volume=50,
        bid=bid,
        ask=ask,
        extra={
            "source": "live_nws",
            "temperature_f": temperature_f,
            "max_temp_today_f": max_temp_today_f,
            "forecast": [{"isDaytime": True, "temperature": nws_high}],
            "hrrr_forecast": nws_high + 1,
            "strike_type": strike_type,
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
        },
    )


# Phase 0 teardown (2026-07-24): sections 3.1 (ML BTC 15m) and 3.2 (ML BTC
# Hourly) were removed with the deleted strategies.

# ===========================================================================
# 3.3  ML Weather Strategy
# ===========================================================================


class TestMLWeatherStrategy:
    def _make_strategy(self, min_edge=0.08):
        from src.strategies.ml_weather import MLWeatherStrategy

        predictor = MagicMock()
        strat = MLWeatherStrategy(min_edge=min_edge, predictor=predictor)
        return strat, predictor

    def test_buy_yes_when_ml_bullish(self):
        strat, predictor = self._make_strategy()
        predictor.predict_weather.return_value = {
            "probability": 0.85,
            "confidence": 0.80,
        }
        md = make_weather_market(ask=0.65)  # edge = 0.85 - 0.65 = 0.20
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "YES"

    def test_buy_no_when_ml_bearish(self):
        strat, predictor = self._make_strategy()
        predictor.predict_weather.return_value = {
            "probability": 0.15,  # YES prob low → NO prob 0.85
            "confidence": 0.80,
        }
        # bid=0.20 → NO cost=0.80, NO prob=0.85, edge=0.05
        # Need higher edge to pass 0.08 threshold
        # bid=0.10 → NO cost=0.90, NO prob=0.85, edge=-0.05 → no
        # Let's use predictor prob 0.08 → NO prob 0.92, bid=0.20 → NO cost=0.80, edge=0.12
        predictor.predict_weather.return_value = {
            "probability": 0.08,
            "confidence": 0.80,
        }
        md = make_weather_market(bid=0.20, ask=0.25)
        signals = strat.analyze(md)
        assert len(signals) >= 1
        no_signals = [s for s in signals if s.contract_side == "NO"]
        assert len(no_signals) >= 1

    def test_winner_guard_buys_remaining(self):
        """If daily max already >= strike, contract has won — buy at market."""
        strat, predictor = self._make_strategy()
        md = make_weather_market(
            symbol="KXHIGHNY-26MAR19-T80",
            max_temp_today_f=82.0,  # Above T80 strike → won
            ask=0.95,
        )
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].side == "buy"
        assert signals[0].confidence == 1.0

    def test_skips_non_nws_source(self):
        strat, _ = self._make_strategy()
        md = make_weather_market()
        md.extra["source"] = "mock"
        assert strat.analyze(md) == []

    def test_skips_near_resolved(self):
        strat, _ = self._make_strategy()
        md = make_weather_market(bid=0.96, ask=0.98)
        assert strat.analyze(md) == []

    def test_outside_trading_hours(self):
        strat, _ = self._make_strategy()
        md = make_weather_market(timestamp=datetime(2026, 3, 19, 8, 0, 0))
        assert strat.analyze(md) == []


# ===========================================================================
# 3.4  Latency Arbitrage Strategy
# ===========================================================================


class TestLatencyArbStrategy:
    def _make_strategy(self):
        from src.strategies.latency_arb import LatencyArbStrategy

        return LatencyArbStrategy(move_threshold=0.003, cooldown_seconds=30)

    def test_detects_upward_move(self):
        strat = self._make_strategy()
        t0 = datetime(2026, 3, 19, 14, 38, 0)

        # Seed buffer with baseline price
        for i in range(10):
            strat._spot_buf.append((t0 + timedelta(seconds=i), 84000.0))

        # Rapid 0.5% jump (> 0.3% threshold)
        strat._spot_buf.append((t0 + timedelta(seconds=30), 84420.0))

        # Kalshi contract still cheap (hasn't repriced)
        md = make_btc_market(
            bid=0.50,
            ask=0.55,
            spot_price=84420.0,
            timestamp=t0 + timedelta(seconds=30),
        )
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "YES"

    def test_detects_downward_move(self):
        strat = self._make_strategy()
        t0 = datetime(2026, 3, 19, 14, 38, 0)

        for i in range(10):
            strat._spot_buf.append((t0 + timedelta(seconds=i), 84000.0))

        # Drop of 0.5%
        strat._spot_buf.append((t0 + timedelta(seconds=30), 83580.0))

        # Contract for T84000 — spot below strike now
        md = make_btc_market(
            symbol="KXBTC15M-26MAR19-1430-T84000",
            bid=0.50,
            ask=0.55,
            spot_price=83580.0,
            timestamp=t0 + timedelta(seconds=30),
        )
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "NO"

    def test_no_signal_on_small_move(self):
        strat = self._make_strategy()
        t0 = datetime(2026, 3, 19, 14, 38, 0)
        for i in range(10):
            strat._spot_buf.append((t0 + timedelta(seconds=i), 84000.0))

        # Only 0.1% move — below threshold
        strat._spot_buf.append((t0 + timedelta(seconds=30), 84084.0))
        md = make_btc_market(
            bid=0.55,
            ask=0.60,
            spot_price=84084.0,
            timestamp=t0 + timedelta(seconds=30),
        )
        assert strat.analyze(md) == []

    def test_needs_spot_history(self):
        """No spot data → no detection."""
        strat = self._make_strategy()
        md = make_btc_market()
        # Don't seed any spot data
        strat._spot_buf.clear()
        assert strat.analyze(md) == []

    def test_cooldown_after_signal(self):
        strat = self._make_strategy()
        t0 = datetime(2026, 3, 19, 14, 38, 0)
        for i in range(10):
            strat._spot_buf.append((t0 + timedelta(seconds=i), 84000.0))
        strat._spot_buf.append((t0 + timedelta(seconds=30), 84500.0))

        md1 = make_btc_market(
            bid=0.50, ask=0.55, spot_price=84500.0, timestamp=t0 + timedelta(seconds=30)
        )
        signals1 = strat.analyze(md1)
        assert len(signals1) >= 1

        # Immediately again — should be in cooldown
        md2 = make_btc_market(
            bid=0.50, ask=0.55, spot_price=84500.0, timestamp=t0 + timedelta(seconds=31)
        )
        assert strat.analyze(md2) == []


# Phase 0 teardown (2026-07-24): sections 3.5 (Longshot Fader V2) and 3.8
# (Cross-Spread Arb) were removed with the deleted strategies.

# ===========================================================================
# 3.9  ML EV Gating Layer (SignalProcessorMixin)
# ===========================================================================


class TestMLGating:
    def test_positive_ev_passes(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.70
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is True

    def test_negative_ev_rejected(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.55, confidence=0.52
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is False

    def test_break_even_plus_fees_rejected(self):
        """Exactly break-even before fees → negative after fees."""
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.50
        )
        # EV = 0.50 - 0.50 - 0.004375 < 0
        assert SignalProcessorMixin._ml_ev_gate(sig) is False

    def test_cheap_contract_low_fee(self):
        """Cheap contract (low fee) with small positive EV passes."""
        from src.bots.mixins import SignalProcessorMixin

        # P=0.05, confidence=0.08 → EV = 0.08-0.05 - 0.0175*0.05*0.95 ≈ 0.03 - 0.0008 > 0
        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.05, confidence=0.08
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is True

    def test_zero_price_rejected(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.0, confidence=0.50
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is False

    def test_high_confidence_high_price_passes(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.90, confidence=0.95
        )
        # EV = 0.95 - 0.90 - 0.0175*0.90*0.10 = 0.05 - 0.00158 > 0
        assert SignalProcessorMixin._ml_ev_gate(sig) is True


# ===========================================================================
# 3.10  YES/NO Support & Strategy Name Contract
# ===========================================================================


class TestYesNoSupport:
    @pytest.mark.parametrize(
        "strategy_module,strategy_class",
        [
            ("src.strategies.ml_weather", "MLWeatherStrategy"),
            ("src.strategies.latency_arb", "LatencyArbStrategy"),
        ],
    )
    def test_strategy_implements_abc(self, strategy_module, strategy_class):
        """All Sprint 3 strategies must implement Strategy ABC."""
        import importlib
        from src.core.interfaces import Strategy

        mod = importlib.import_module(strategy_module)
        cls = getattr(mod, strategy_class)
        assert issubclass(cls, Strategy)
        inst = cls()
        assert callable(getattr(inst, "analyze", None))
        assert isinstance(inst.name(), str)
        assert len(inst.name()) > 0

    @pytest.mark.parametrize(
        "strategy_module,strategy_class",
        [
            ("src.strategies.ml_weather", "MLWeatherStrategy"),
            ("src.strategies.latency_arb", "LatencyArbStrategy"),
        ],
    )
    def test_strategy_source_references_contract_side(
        self, strategy_module, strategy_class
    ):
        """Every new strategy must set contract_side on its signals."""
        import importlib
        import inspect

        mod = importlib.import_module(strategy_module)
        cls = getattr(mod, strategy_class)
        src = inspect.getsource(cls)
        assert (
            "contract_side" in src
        ), f"{strategy_class} never references contract_side"
