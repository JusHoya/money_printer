"""Regression tests for METAR source acceptance in WeatherArbitrageStrategyV2.

The strategy filters incoming MarketData by `extra["source"]`. METAR
observations (~10x more precise than NWS forecasts) must be accepted
alongside `live_nws`. These tests guard against regressions that would
silently reject METAR data.
"""

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime
from unittest.mock import patch

import pytest

from src.core.interfaces import MarketData
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2


def _make_weather_market(source: str, symbol: str = "KXHIGHNY-26MAR19-T80"):
    """Build a MarketData with the given source and a forecast that triggers a signal.

    PRD FR-1.1 (2026-07-25): ``strike_type``/``floor_strike``/``cap_strike`` are
    the API fields the strategy now derives contract direction from, and
    ``KalshiProvider`` puts them on every market's ``extra``. They are added
    here so the fixture matches production data; no assertion changed. T80 is
    modelled as ``greater`` floor=80 ("81 or above"), the reading the old
    suffix parser intended.
    """
    return MarketData(
        symbol=symbol,
        timestamp=datetime(2026, 3, 19, 12, 0, 0),
        price=0.60,
        volume=50,
        bid=0.60,
        ask=0.65,
        extra={
            "source": source,
            "temperature_f": 78.0,
            "max_temp_today_f": 79.0,
            "forecast": [{"isDaytime": True, "temperature": 85}],
            "metar_age_seconds": 120,
            "strike_type": "greater",
            "floor_strike": 80,
            "cap_strike": None,
        },
    )


@pytest.fixture
def strat():
    return WeatherArbitrageStrategyV2()


@pytest.fixture
def trading_hours():
    """Pin datetime.now() inside weather_strategy.py to a trading hour."""
    fixed_now = datetime(2026, 3, 19, 12, 0, 0)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    with patch("src.strategies.weather_strategy.datetime", _FixedDateTime):
        yield


def test_rejects_unknown_source(strat, trading_hours):
    """Unknown sources should be filtered out (sanity check)."""
    md = _make_weather_market(source="bogus_source")
    assert strat.analyze(md) == []


def test_accepts_live_nws(strat, trading_hours):
    """Regression guard: live_nws must continue to produce signals."""
    md = _make_weather_market(source="live_nws")
    signals = strat.analyze(md)
    assert len(signals) >= 1, "live_nws source must yield at least one signal"


def test_accepts_live_metar(strat, trading_hours):
    """METAR data is ~10x more precise than NWS — must be accepted."""
    md = _make_weather_market(source="live_metar")
    signals = strat.analyze(md)
    assert len(signals) >= 1, "live_metar source must yield at least one signal"


def test_rejects_empty_source(strat, trading_hours):
    """Missing/empty source string must not slip through."""
    md = _make_weather_market(source="")
    assert strat.analyze(md) == []


def test_rejects_none_source(strat, trading_hours):
    """Explicit None source must not slip through."""
    md = MarketData(
        symbol="KXHIGHNY-26MAR19-T80",
        timestamp=datetime(2026, 3, 19, 12, 0, 0),
        price=0.60,
        volume=50,
        bid=0.60,
        ask=0.65,
        extra={
            "temperature_f": 78.0,
            "max_temp_today_f": 79.0,
            "forecast": [{"isDaytime": True, "temperature": 85}],
        },
    )
    assert strat.analyze(md) == []
