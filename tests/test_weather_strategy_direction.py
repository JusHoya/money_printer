"""Behavioural enforcement of FR-1.1: the side we trade must come from the API.

Phase 1 exit criterion 2 was a ``rg`` for one string. A 2026-07-25 verifier
swapped the strategies' band derivation::

    band_lo, band_hi = yes_bounds(spec)          # FR-1.1: API fields

back to the deleted ticker-suffix reading (``B<n>`` -> "<= n",
``T<n>`` -> ">= n") and ran every weather-relevant test file::

    BASELINE           : 365 passed in 4.36s
    SUFFIX-INFER MUTANT: 365 passed in 4.23s      <-- zero failures

The traded side inverted on exactly the contract type the 2026-07-24 review
found backwards, with the grep clean and the whole suite green::

    KXHIGHNY-26JUL25-T80 (less, cap=80, YES iff high<=79), forecast 74F:
      BASELINE -> contract_side='YES'
      MUTANT   -> contract_side='NO'

This file closes that hole. It drives the real ``WeatherArbitrageStrategyV2``
and ``MLWeatherStrategy`` end to end on API-shaped ``MarketData`` for a
``between``, a ``greater`` and a ``less`` market, and asserts the **emitted
side**. Every case is chosen so the legacy suffix reading produces the opposite
answer, and :class:`TestSuffixInferenceMutantIsKilled` proves that by running
the same cases with the mutant installed — so the power of these assertions is
re-verified on every run, not just the day they were written.

Nothing here weakens a gate to get a signal out: the clock is frozen inside the
strategies' own trade window, the observation carries a real ``live_metar``
source, the market is two-sided and far from resolved, and the ML strategy gets
a predictor whose answer is a genuine probability over whatever band it is
handed (so a wrong band inverts it, rather than a stub returning a constant).

Live provenance for every contract used (anonymous read, 2026-07-25)::

    GET /trade-api/v2/markets?series_ticker=KXHIGHNY&status=open
      KXHIGHNY-26JUL25-B86.5  between  floor=86  cap=None->87   "86 to 87"
      KXHIGHNY-26JUL25-T87    greater  floor=87  cap=None       "88 or above"
      KXHIGHNY-26JUL25-T80    less     floor=None cap=80        "79 or below"

Run: $env:PYTHONPATH="."; python -m pytest tests/test_weather_strategy_direction.py -v
"""

from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bots.weather_bot import BRACKET_FIELDS, CITY_CONFIG  # noqa: E402
from src.core.bracket_payoff import settles_yes, yes_bounds  # noqa: E402
from src.core.interfaces import MarketData  # noqa: E402
from src.strategies.ml_weather import MLWeatherStrategy  # noqa: E402
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2  # noqa: E402

# 11:30 ET on 2026-07-25 — inside both strategies' 10:00-13:59 ET trade window
# (the window is exchange-time since the 2026-09-01 UTC-container fix; a naive
# instant here would be misread as UTC by ml_weather's tape-stamp handling),
# and the date the live contracts below expire on, so ``is_today`` is true and
# the winner/lost guards are exercised rather than skipped.
FROZEN_NOW = datetime(2026, 7, 25, 11, 30, 0, tzinfo=ZoneInfo("America/New_York"))
DATE_CODE = FROZEN_NOW.strftime("%y%b%d").upper()  # "26JUL25"


# ===========================================================================
# The mutant: contract direction read off the ticker's suffix letter
# ===========================================================================


def legacy_suffix_bounds(spec):
    """The DELETED pre-Phase-1 band derivation, reproduced as the mutant.

    ``B<n>`` was read as "daily high <= n" and every other suffix as
    "daily high >= n". Substituted for ``yes_bounds`` this is byte-equivalent
    to editing ``band_lo, band_hi = yes_bounds(spec)`` in each strategy to read
    the ticker instead — the single call site in each module.
    """
    suffix = spec.ticker.split("-")[-1]
    number = float(re.sub(r"[A-Za-z]", "", suffix))
    if suffix[:1].upper() == "B":
        return (-math.inf, number)
    return (number, math.inf)


MUTATED_MODULES = (
    "src.strategies.weather_strategy",
    "src.strategies.ml_weather",
)


@pytest.fixture
def install_suffix_mutant(monkeypatch):
    """Replace the FR-1.1 band derivation with the legacy suffix reading."""

    def _install():
        for module in MUTATED_MODULES:
            monkeypatch.setattr(f"{module}.yes_bounds", legacy_suffix_bounds)

    return _install


# ===========================================================================
# Frozen clock (patched, never a weakened gate)
# ===========================================================================


class _FrozenDateTime(datetime):
    """``datetime`` whose ``now()`` is pinned to :data:`FROZEN_NOW`."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """``WeatherArbitrageStrategyV2`` reads the wall clock; pin it.

    ``MLWeatherStrategy`` takes its time from ``MarketData.timestamp``, which
    the fixtures below set to the same instant, so both strategies see 11:30
    on 2026-07-25 regardless of when the suite runs.
    """
    monkeypatch.setattr("src.strategies.weather_strategy.datetime", _FrozenDateTime)


# ===========================================================================
# API-shaped market data (provider -> weather_bot fusion -> strategy)
# ===========================================================================


def _kalshi_market(ticker, strike_type, floor_strike, cap_strike, sub_title, bid, ask):
    """A live-shaped ``/markets`` row. Kalshi OMITS the irrelevant strike."""
    market = {
        "ticker": ticker,
        "status": "active",
        "strike_type": strike_type,
        "yes_bid_dollars": f"{bid:.4f}",
        "yes_ask_dollars": f"{ask:.4f}",
        "no_bid_dollars": f"{1 - ask:.4f}",
        "no_ask_dollars": f"{1 - bid:.4f}",
        "last_price_dollars": f"{(bid + ask) / 2:.4f}",
        "volume_fp": "1840.00",
        "close_time": "2026-07-26T04:59:00Z",
        "yes_sub_title": sub_title,
    }
    if floor_strike is not None:
        market["floor_strike"] = floor_strike
    if cap_strike is not None:
        market["cap_strike"] = cap_strike
    return market


def _provider():
    from src.data.kalshi_provider import KalshiProvider

    return KalshiProvider(key_id=None, private_key_path=None, read_only=True)


def fused_market_data(case) -> MarketData:
    """Reproduce ``WeatherBot.tick``'s observation/market fusion exactly.

    The provider parses the API row (so ``extra`` carries the FR-1.1 fields as
    production sees them); the METAR observation is the object the strategies
    actually receive, with the active market's bracket fields copied onto it
    and its symbol/quotes overwritten — the same four steps ``tick`` performs.
    """
    api_row = _kalshi_market(
        case["ticker"],
        case["strike_type"],
        case["floor_strike"],
        case["cap_strike"],
        case["yes_sub_title"],
        case["bid"],
        case["ask"],
    )
    k_md = _provider()._parse_market_data(case["ticker"], api_row, "live_kalshi")

    city = CITY_CONFIG["NY"]
    obs = MarketData(
        symbol=city.settlement_station,
        timestamp=FROZEN_NOW,
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={
            "temperature_f": case["current_temp_f"],
            "max_temp_today_f": case["max_temp_today_f"],
            "source": "live_metar",
            "metar_age_seconds": 120.0,
            "forecast": [
                {"isDaytime": True, "temperature": case["nws_forecast_high"]},
                {"isDaytime": False, "temperature": case["nws_forecast_high"] - 12},
            ],
            "station_name": city.settlement_station,
            "settlement_station": city.settlement_station,
            "station_timezone": city.timezone,
            "city_key": city.key,
            "kalshi_series": city.kalshi_series,
        },
    )
    for field in BRACKET_FIELDS:
        obs.extra[field] = (k_md.extra or {}).get(field)
    obs.symbol = k_md.symbol
    obs.bid, obs.ask, obs.price = k_md.bid, k_md.ask, k_md.price
    return obs


# ===========================================================================
# The three direction cases
# ===========================================================================
#
# Each row is built so the FR-1.1 band and the legacy suffix band put the
# forecast on OPPOSITE sides of the YES boundary, and therefore make the
# strategies emit opposite ``contract_side`` values.
#
#   ticker                  API band (FR-1.1)  legacy suffix band   forecast
#   KXHIGHNY-26JUL25-B86.5  [86, 87]           (-inf, 86.5]         83
#   KXHIGHNY-26JUL25-T87    [88, +inf)         [87, +inf)           83, max 87
#   KXHIGHNY-26JUL25-T80    (-inf, 79]         [80, +inf)           74
#
# The ``greater`` row separates on the WINNER GUARD rather than the forecast
# arm: with an observed daily max of exactly 87F the legacy band declares the
# contract already won ("87 or above") and buys YES, while the true band
# ("88 or above") is still live and the forecast says NO. That is the same
# inversion the 2026-07-24 review found, on the guard that costs the most.

DIRECTION_CASES = [
    {
        "id": "between-B86.5",
        "ticker": f"KXHIGHNY-{DATE_CODE}-B86.5",
        "strike_type": "between",
        "floor_strike": 86,
        "cap_strike": 87,
        "yes_sub_title": "86° to 87°",
        "bid": 0.30,
        "ask": 0.34,
        "nws_forecast_high": 83,
        "current_temp_f": 79.0,
        "max_temp_today_f": 80.0,
        "expected_side": "NO",
        "mutant_side": "YES",
        "why": "83F is outside [86,87] -> NO; the legacy band (-inf,86.5] "
        "contains it -> YES",
    },
    {
        "id": "greater-T87",
        "ticker": f"KXHIGHNY-{DATE_CODE}-T87",
        "strike_type": "greater",
        "floor_strike": 87,
        "cap_strike": None,
        "yes_sub_title": "88° or above",
        "bid": 0.30,
        "ask": 0.34,
        "nws_forecast_high": 83,
        "current_temp_f": 86.0,
        "max_temp_today_f": 87.0,
        "expected_side": "NO",
        "mutant_side": "YES",
        "why": "observed max 87F has NOT won '88 or above'; the legacy band "
        "[87,inf) calls it won and buys YES at the ask",
    },
    {
        "id": "less-T80",
        "ticker": f"KXHIGHNY-{DATE_CODE}-T80",
        "strike_type": "less",
        "floor_strike": None,
        "cap_strike": 80,
        "yes_sub_title": "79° or below",
        "bid": 0.30,
        "ask": 0.34,
        "nws_forecast_high": 74,
        "current_temp_f": 72.0,
        "max_temp_today_f": 72.0,
        "expected_side": "YES",
        "mutant_side": "NO",
        "why": "74F is comfortably inside '79 or below' -> YES; the legacy "
        "band [80,inf) reads the contract exactly backwards -> NO",
    },
]

CASE_PARAMS = [pytest.param(c, id=c["id"]) for c in DIRECTION_CASES]


# ===========================================================================
# Strategies under test
# ===========================================================================


class BandProbabilityPredictor:
    """Stand-in for ``ModelPredictor`` that answers the question it is asked.

    ``P(bracket_lower <= daily high <= bracket_upper)`` under
    ``N(forecast, 2.5F)`` — 2.5F being published NWS day-of accuracy. It has no
    opinion of its own about direction: hand it the wrong band and it returns
    the wrong probability, which is precisely what makes the ML assertions
    below sensitive to the FR-1.1 defect instead of to a hardcoded constant.
    """

    SIGMA_F = 2.5

    def __init__(self):
        self.calls = []

    def predict_weather(
        self, nws_forecast, hrrr_forecast, station_id, bracket_lower, bracket_upper
    ):
        mu = (float(nws_forecast) + float(hrrr_forecast)) / 2.0
        self.calls.append((mu, float(bracket_lower), float(bracket_upper)))

        def cdf(x):
            return 0.5 * (1.0 + math.erf((x - mu) / (self.SIGMA_F * math.sqrt(2.0))))

        p = cdf(float(bracket_upper)) - cdf(float(bracket_lower))
        return {"probability": max(0.0, min(1.0, p)), "confidence": 0.80}


def _v2():
    """Production construction: bias correction on, default thresholds."""
    return WeatherArbitrageStrategyV2()


def _ml():
    """Production construction apart from the injected predictor."""
    return MLWeatherStrategy(predictor=BandProbabilityPredictor())


STRATEGY_FACTORIES = [
    pytest.param(_v2, id="MeteorologistV2"),
    pytest.param(_ml, id="MLWeather"),
]


def sole_signal(strategy, market_data):
    signals = strategy.analyze(market_data)
    assert signals, (
        f"{strategy.name()} emitted no signal for {market_data.symbol}; the "
        f"direction assertion below would be vacuous"
    )
    assert len(signals) == 1, f"expected one signal, got {len(signals)}"
    return signals[0]


# ===========================================================================
# 1. BASELINE — the emitted side matches the API's bracket semantics
# ===========================================================================


class TestEmittedSideFollowsApiBracketFields:
    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_contract_side_is_correct(self, factory, case):
        signal = sole_signal(factory(), fused_market_data(case))
        assert signal.contract_side == case["expected_side"], (
            f"{case['ticker']} ({case['yes_sub_title']}): expected "
            f"{case['expected_side']}, got {signal.contract_side}. {case['why']}"
        )
        assert signal.side == "buy", "both YES and NO are expressed as BUY orders"

    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_signal_carries_bracket_semantics_to_settlement(self, factory, case):
        """FR-1.1/FR-1.2: without these the position settles UNRESOLVED."""
        signal = sole_signal(factory(), fused_market_data(case))
        assert signal.strike_type == case["strike_type"]
        assert signal.floor_strike == (
            None if case["floor_strike"] is None else float(case["floor_strike"])
        )
        assert signal.cap_strike == (
            None if case["cap_strike"] is None else float(case["cap_strike"])
        )

    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_the_side_taken_would_pay_at_the_forecast_high(self, factory, case):
        """Cross-check the side against the shared payoff module.

        Rounding the (bias-corrected) forecast to whole degrees gives the most
        likely settled high; the side we take must be the side that pays there.
        This ties the behavioural assertion back to ``bracket_payoff`` rather
        than to a hand-written expectation.
        """
        md = fused_market_data(case)
        signal = sole_signal(factory(), md)
        from src.core.bracket_payoff import parse_bracket_spec

        spec = parse_bracket_spec(md.symbol, md.extra)
        likely_high = round(float(case["nws_forecast_high"]))
        pays_yes = settles_yes(spec, likely_high)
        assert (signal.contract_side == "YES") is pays_yes, (
            f"{case['ticker']}: took {signal.contract_side} but a settled high "
            f"of {likely_high}F pays {'YES' if pays_yes else 'NO'} under "
            f"{spec.describe()!r}"
        )


class TestGoldenBandsAreWhatTheStrategiesSee:
    """Pin the two bands that the cases depend on separating."""

    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_api_band_and_legacy_band_disagree(self, case):
        from src.core.bracket_payoff import parse_bracket_spec

        md = fused_market_data(case)
        spec = parse_bracket_spec(md.symbol, md.extra)
        api_band = yes_bounds(spec)
        legacy_band = legacy_suffix_bounds(spec)
        assert api_band != legacy_band, (
            f"{case['ticker']}: the legacy suffix band equals the API band, so "
            f"this case cannot detect the mutant"
        )
        forecast = float(case["nws_forecast_high"])
        in_api = api_band[0] <= forecast <= api_band[1]
        in_legacy = legacy_band[0] <= forecast <= legacy_band[1]
        # The greater case separates on the winner guard, not the forecast arm.
        if case["id"] != "greater-T87":
            assert in_api != in_legacy, (
                f"{case['ticker']}: forecast {forecast}F falls the same side of "
                f"both bands ({api_band} vs {legacy_band})"
            )


# ===========================================================================
# 2. POWER PROOF — the same cases with the suffix mutant installed
# ===========================================================================


class TestSuffixInferenceMutantIsKilled:
    """These assertions have power: the mutant flips every emitted side.

    If a future refactor makes the direction cases insensitive (a threshold
    change that suppresses the signal, a band no longer derived from
    ``yes_bounds``), this class fails and says so, instead of the file
    quietly becoming a no-op the way exit criterion 2's ``rg`` did.
    """

    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_mutant_emits_the_opposite_side(self, factory, case, install_suffix_mutant):
        install_suffix_mutant()
        signal = sole_signal(factory(), fused_market_data(case))
        assert signal.contract_side == case["mutant_side"], (
            f"{case['ticker']}: with the ticker-suffix parser installed the "
            f"strategy emitted {signal.contract_side}; this case was chosen "
            f"because it must emit {case['mutant_side']}. The baseline "
            f"assertion above no longer has power against FR-1.1."
        )

    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_mutant_and_baseline_always_disagree(
        self, factory, case, install_suffix_mutant
    ):
        baseline = sole_signal(factory(), fused_market_data(case)).contract_side
        install_suffix_mutant()
        mutated = sole_signal(factory(), fused_market_data(case)).contract_side
        assert baseline != mutated, (
            f"{case['ticker']}: baseline and suffix-mutant both emitted "
            f"{baseline} — this case is blind to the FR-1.1 defect"
        )


# ===========================================================================
# 3. Abort, never infer: a market with no strike_type produces no signal
# ===========================================================================


class TestMissingBracketSemanticsAbort:
    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    @pytest.mark.parametrize("case", CASE_PARAMS)
    def test_absent_strike_type_emits_nothing(self, factory, case):
        """The ticker is right there; the strategy must still refuse to guess."""
        md = fused_market_data(case)
        md.extra["strike_type"] = None
        assert factory().analyze(md) == []

    @pytest.mark.parametrize("factory", STRATEGY_FACTORIES)
    def test_absent_floor_and_cap_emit_nothing(self, factory):
        md = fused_market_data(DIRECTION_CASES[0])
        md.extra["floor_strike"] = None
        md.extra["cap_strike"] = None
        assert factory().analyze(md) == []
