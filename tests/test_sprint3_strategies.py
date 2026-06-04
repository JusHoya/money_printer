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
):
    """Create a MarketData that looks like fused weather data."""
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
        },
    )


def make_hourly_market(
    symbol="KXBTC-26MAR19-1500-T84000",
    bid=0.50,
    ask=0.55,
    spot_price=84200.0,
    timestamp=None,
):
    if timestamp is None:
        timestamp = datetime(2026, 3, 19, 14, 10, 0)
    return MarketData(
        symbol=symbol,
        timestamp=timestamp,
        price=spot_price,
        volume=100,
        bid=bid,
        ask=ask,
        extra={
            "spot_price": spot_price,
            "source": "test",
            "close_time": timestamp + timedelta(minutes=50),
            "no_bid": 0.0,
            "no_ask": 0.0,
        },
    )


# ===========================================================================
# 3.1  ML BTC 15m Strategy
# ===========================================================================


class TestMLBtc15mStrategy:
    def _make_strategy(self, min_edge=0.05):
        from src.strategies.ml_btc_15m import MLBtc15mStrategy

        predictor = MagicMock()
        strat = MLBtc15mStrategy(min_edge=min_edge, predictor=predictor)
        return strat, predictor

    def test_buy_yes_when_model_bullish(self):
        """Model prob > 0.5 and edge > min_edge → BUY YES."""
        strat, predictor = self._make_strategy()
        predictor.predict_btc.return_value = {
            "probability": 0.75,
            "confidence": 0.70,
            "fair_value": 0.75,
            "recommended_price": 0.73,
        }
        # spot well above the 84000 strike so it clears the near-ATM proximity
        # filter (>0.3% away during daytime UTC).
        md = make_btc_market(ask=0.60, spot_price=85000.0)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].side == "buy"
        assert signals[0].contract_side == "YES"
        assert signals[0].confidence == 0.70

    def test_buy_no_when_model_bearish(self):
        """Model prob < 0.5 → BUY NO if edge sufficient."""
        strat, predictor = self._make_strategy()
        predictor.predict_btc.return_value = {
            "probability": 0.25,
            "confidence": 0.70,
            "fair_value": 0.25,
            "recommended_price": 0.27,
        }
        # bid=0.30 → NO cost = 1-0.30 = 0.70, NO fair = 0.75, edge = 0.05
        # spot well below the 84000 strike so it clears the near-ATM proximity
        # filter (>0.3% away).
        md = make_btc_market(bid=0.30, ask=0.35, spot_price=83000.0)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].side == "buy"
        assert signals[0].contract_side == "NO"

    def test_no_signal_when_edge_too_small(self):
        """Edge below min_edge → no signal."""
        strat, predictor = self._make_strategy(min_edge=0.10)
        predictor.predict_btc.return_value = {
            "probability": 0.62,
            "confidence": 0.60,
            "fair_value": 0.62,
            "recommended_price": 0.60,
        }
        md = make_btc_market(ask=0.60)  # edge = 0.62 - 0.60 = 0.02 < 0.10
        signals = strat.analyze(md)
        assert len(signals) == 0

    def test_cooldown_prevents_rapid_signals(self):
        strat, predictor = self._make_strategy()
        predictor.predict_btc.return_value = {
            "probability": 0.80,
            "confidence": 0.75,
            "fair_value": 0.80,
            "recommended_price": 0.78,
        }
        t0 = datetime(2026, 3, 19, 14, 38, 0)
        # spot above the 84000 strike to clear the near-ATM proximity filter.
        md1 = make_btc_market(ask=0.60, spot_price=85000.0, timestamp=t0)
        signals1 = strat.analyze(md1)
        assert len(signals1) == 1

        # 10 seconds later — should be in cooldown
        md2 = make_btc_market(
            ask=0.60, spot_price=85000.0, timestamp=t0 + timedelta(seconds=10)
        )
        signals2 = strat.analyze(md2)
        assert len(signals2) == 0

    def test_no_signal_near_expiry(self):
        """<60s to expiry → no signal (circuit breaker)."""
        strat, predictor = self._make_strategy()
        predictor.predict_btc.return_value = {
            "probability": 0.80,
            "confidence": 0.75,
            "fair_value": 0.80,
            "recommended_price": 0.78,
        }
        t = datetime(2026, 3, 19, 14, 44, 30)  # 30s left in cycle
        md = make_btc_market(
            ask=0.60, timestamp=t, close_time=t + timedelta(seconds=30)
        )
        signals = strat.analyze(md)
        assert len(signals) == 0

    def test_risk_context_set_on_signals(self):
        # Sprint 6 removed hard stop-losses for 15m contracts; the ML strategy
        # now attaches strike + expiration + ML journal context to each signal
        # instead. Verify those load-bearing attributes are populated.
        strat, predictor = self._make_strategy()
        predictor.predict_btc.return_value = {
            "probability": 0.80,
            "confidence": 0.75,
            "fair_value": 0.80,
            "recommended_price": 0.78,
        }
        # spot above the 84000 strike to clear the near-ATM proximity filter.
        md = make_btc_market(ask=0.60, spot_price=85000.0)
        signals = strat.analyze(md)
        assert len(signals) == 1
        sig = signals[0]
        # Strike must propagate for binary settlement / exposure routing.
        assert sig.strike == 84000.0
        # close_time from the market feed becomes the contract expiration.
        assert hasattr(sig, "expiration_time")
        # ML context required by the trade journal.
        assert sig.model_probability == 0.80
        assert sig.btc_spot == 85000.0

    def test_invalid_bid_ask_returns_empty(self):
        strat, predictor = self._make_strategy()
        md = make_btc_market(bid=0.0, ask=0.0)
        assert strat.analyze(md) == []

    def test_bad_symbol_no_strike(self):
        strat, predictor = self._make_strategy()
        md = make_btc_market(symbol="NOSYMBOL")
        md.extra.pop("strike", None)  # Remove strike from extra too
        assert strat.analyze(md) == []


# ===========================================================================
# 3.2  ML BTC Hourly Strategy
# ===========================================================================


class TestMLBtcHourlyStrategy:
    def _make_strategy(self, min_edge=0.05):
        from src.strategies.ml_btc_hourly import MLBtcHourlyStrategy

        predictor = MagicMock()
        strat = MLBtcHourlyStrategy(min_edge=min_edge, predictor=predictor)
        return strat, predictor

    def test_accumulates_spot_history(self):
        strat, _ = self._make_strategy()
        spot_md = MarketData(
            symbol="BTC-USD",
            timestamp=datetime.now(),
            price=84000.0,
            volume=10,
            bid=83990,
            ask=84010,
            extra={"source": "live_coinbase"},
        )
        result = strat.analyze(spot_md)
        assert result == []  # Spot ticks never produce signals
        assert len(strat._spot_history) == 1

    def test_skips_15m_tickers(self):
        strat, _ = self._make_strategy()
        md = make_btc_market(symbol="KXBTC15M-26MAR19-1430-T84000")
        assert strat.analyze(md) == []

    def test_buy_yes_on_hourly_ticker(self):
        strat, predictor = self._make_strategy()
        predictor.predict_btc.return_value = {
            "probability": 0.78,
            "confidence": 0.72,
            "fair_value": 0.78,
            "recommended_price": 0.76,
        }
        # Seed spot history
        t0 = datetime(2026, 3, 19, 14, 5, 0)
        for i in range(10):
            strat._spot_history.append((t0 + timedelta(seconds=i * 10), 84200.0))

        md = make_hourly_market(ask=0.55, timestamp=t0 + timedelta(minutes=2))
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "YES"

    def test_rejects_far_from_spot_strike(self):
        strat, predictor = self._make_strategy()
        t0 = datetime(2026, 3, 19, 14, 5, 0)
        for i in range(10):
            strat._spot_history.append((t0 + timedelta(seconds=i), 84000.0))

        # Strike at T50000 — $34k away from spot
        md = make_hourly_market(
            symbol="KXBTC-26MAR19-1500-T50000",
            timestamp=t0 + timedelta(minutes=2),
        )
        assert strat.analyze(md) == []

    def test_outside_time_window_skips(self):
        strat, _ = self._make_strategy()
        t0 = datetime(2026, 3, 19, 14, 5, 0)
        for i in range(10):
            strat._spot_history.append((t0 + timedelta(seconds=i), 84200.0))
        # Minute 20 — outside all windows (0-15, 25-35, 45-60)
        md = make_hourly_market(
            timestamp=datetime(2026, 3, 19, 14, 20, 0),
        )
        assert strat.analyze(md) == []


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


# ===========================================================================
# 3.5  Longshot Fader V2
# ===========================================================================


class TestLongshotFaderV2:
    def _make_strategy(self):
        from src.strategies.longshot_fader_v2 import LongshotFaderV2

        predictor = MagicMock()
        strat = LongshotFaderV2(predictor=predictor, cooldown_seconds=0)
        return strat, predictor

    def test_fades_overpriced_longshot(self):
        strat, predictor = self._make_strategy()
        predictor.predict.return_value = {"probability": 0.03}
        md = make_btc_market(bid=0.07, ask=0.09)
        signals = strat.analyze(md)
        assert len(signals) == 1
        assert signals[0].contract_side == "NO"
        assert signals[0].side == "buy"

    def test_ignores_cheap_below_floor(self):
        strat, _ = self._make_strategy()
        md = make_btc_market(bid=0.02, ask=0.04)
        assert strat.analyze(md) == []

    def test_ignores_expensive_above_ceiling(self):
        strat, _ = self._make_strategy()
        md = make_btc_market(bid=0.15, ask=0.18)
        assert strat.analyze(md) == []

    def test_no_signal_when_calibrated_matches_market(self):
        """No edge → no signal."""
        strat, predictor = self._make_strategy()
        predictor.predict.return_value = {"probability": 0.06}
        md = make_btc_market(bid=0.07, ask=0.09)
        # edge = 0.07 - 0.06 = 0.01 < min_edge (0.03)
        assert strat.analyze(md) == []

    def test_stop_loss_set(self):
        strat, predictor = self._make_strategy()
        predictor.predict.return_value = {"probability": 0.02}
        md = make_btc_market(bid=0.06, ask=0.08)
        signals = strat.analyze(md)
        assert len(signals) == 1
        # stop_loss = min(0.15, bid + 0.05) = min(0.15, 0.06+0.05) = 0.11
        assert signals[0].stop_loss == 0.11


# ===========================================================================
# 3.8  Cross-Spread Arbitrage
# ===========================================================================


class TestCrossSpreadArb:
    def _make_strategy(self):
        from src.strategies.cross_spread_arb import CrossSpreadArbStrategy

        return CrossSpreadArbStrategy(min_profit_after_fees=0.005, cooldown_seconds=0)

    def test_detects_arb_opportunity(self):
        strat = self._make_strategy()
        # YES_ask=0.48, NO_ask=0.48 → total 0.96 < 1.00
        md = make_btc_market(bid=0.52, ask=0.48)
        md.extra["no_ask"] = 0.48
        signals = strat.analyze(md)
        assert len(signals) == 2
        sides = {s.contract_side for s in signals}
        assert sides == {"YES", "NO"}
        for sig in signals:
            assert sig.confidence == 0.99
            assert getattr(sig, "disable_profit_targets", False) is True

    def test_no_arb_when_prices_sum_above_one(self):
        strat = self._make_strategy()
        md = make_btc_market(bid=0.45, ask=0.55)
        md.extra["no_ask"] = 0.50
        # 0.55 + 0.50 = 1.05 > 1.00 → no arb
        assert strat.analyze(md) == []

    def test_derives_no_ask_from_bid(self):
        """If no_ask not in extra, derive from 1-bid."""
        strat = self._make_strategy()
        # bid=0.55 → implied NO_ask = 1-0.55 = 0.45
        # YES_ask=0.48 + NO_ask=0.45 = 0.93 < 1.00 → arb!
        md = make_btc_market(bid=0.55, ask=0.48)
        md.extra.pop("no_ask", None)
        signals = strat.analyze(md)
        assert len(signals) == 2

    def test_fee_check_filters_tiny_arbs(self):
        strat = self._make_strategy()
        # Very small arb: 0.499 + 0.499 = 0.998 → gross = 0.002
        # Fees will eat the profit
        md = make_btc_market(bid=0.501, ask=0.499)
        md.extra["no_ask"] = 0.499
        signals = strat.analyze(md)
        assert len(signals) == 0


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
            ("src.strategies.ml_btc_15m", "MLBtc15mStrategy"),
            ("src.strategies.ml_btc_hourly", "MLBtcHourlyStrategy"),
            ("src.strategies.ml_weather", "MLWeatherStrategy"),
            ("src.strategies.latency_arb", "LatencyArbStrategy"),
            ("src.strategies.longshot_fader_v2", "LongshotFaderV2"),
            ("src.strategies.cross_spread_arb", "CrossSpreadArbStrategy"),
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
            ("src.strategies.ml_btc_15m", "MLBtc15mStrategy"),
            ("src.strategies.ml_btc_hourly", "MLBtcHourlyStrategy"),
            ("src.strategies.ml_weather", "MLWeatherStrategy"),
            ("src.strategies.latency_arb", "LatencyArbStrategy"),
            ("src.strategies.longshot_fader_v2", "LongshotFaderV2"),
            ("src.strategies.cross_spread_arb", "CrossSpreadArbStrategy"),
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
