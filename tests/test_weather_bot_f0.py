"""Phase F0 sandbox-admissibility fixes on the weather bot.

PRD_STRATEGY_FACTORY.md §5, exercised here:

* FR-F0.2 ``ML_WEATHER_ENABLED`` (default False) removes ``MLWeatherStrategy``
  from the WeatherBot waterfall; the tick goes straight to Meteorologist V2.
  With the flag True the waterfall is exactly the pre-flag one (ML first).
* FR-F0.3 ``_ladder_for_city`` picks its tracked-date window on the Eastern
  Time calendar (D-1, D, D+1), not the host wall clock, so the last hours of
  a city-day after 00:00Z are still captured on a UTC host.
* FR-F0.4 the ``[Signal] EXECUTED`` line, the dashboard EXEC line and
  ``check_order`` all see the post-cap quantity — the one
  ``record_execution`` books — when Kelly sizes above ``MAX_CONTRACTS``.

Conventions follow ``tests/test_phase0_harvester.py`` (bot wiring with
mocked providers) and ``tests/test_phase0_state_hygiene.py`` (caplog wired
to the ``MoneyPrinter`` logger, which has ``propagate=False``).
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.bots.weather_bot as weather_bot  # noqa: E402
from src.bots.mixins import SignalProcessorMixin  # noqa: E402
from src.bots.weather_bot import CITY_CONFIG, ET, WeatherBot  # noqa: E402
from src.core.interfaces import MarketData, TradeSignal  # noqa: E402
from src.core.risk_manager import MAX_CONTRACTS, RiskManager  # noqa: E402
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2  # noqa: E402
from src.utils.logger import logger as mp_logger  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mp_caplog(caplog):
    """caplog wired to the project logger (it has propagate=False)."""
    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    yield caplog
    mp_logger.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr("src.bots.weather_bot.time.sleep", lambda s: None)


def _md(symbol, bid=0.40, ask=0.42, price=0.41):
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        price=price,
        volume=100,
        bid=bid,
        ask=ask,
        extra={"no_bid": 1 - ask, "no_ask": 1 - bid, "strike_type": "between"},
    )


def _wire(bot, ladder, city_key="NY"):
    """Point a constructed WeatherBot at mocked providers (one city)."""
    city = CITY_CONFIG[city_key]
    bot.CITIES = (city,)
    obs = MarketData(
        symbol=city.settlement_station,
        timestamp=datetime.now(timezone.utc),
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={"temperature_f": 75.0, "max_temp_today_f": 80.0, "forecast": []},
    )
    bot.metar = MagicMock()
    bot.metar.fetch_latest.return_value = obs
    bot.nws = MagicMock()
    bot.nws.fetch_latest.return_value = MarketData(
        symbol=city.settlement_station,
        timestamp=datetime.now(timezone.utc),
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={"forecast": []},
    )
    bot.kalshi = MagicMock()
    bot.kalshi.fetch_market_ladder.return_value = ladder
    bot.kalshi.fetch_orderbook.return_value = {"yes": [(0.4, 10.0)], "no": []}
    return bot


def _freeze_clock(monkeypatch, utc_now):
    """Replace weather_bot.datetime with one whose now() is pinned.

    ``now(tz=None)`` returns the *naive UTC* wall clock (what the maia
    container sees); ``now(tz)`` converts properly. Records the tz arguments
    so a test can prove the ET clock was asked for.
    """
    calls = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            if tz is None:
                return utc_now.replace(tzinfo=None)
            return utc_now.astimezone(tz)

    monkeypatch.setattr(weather_bot, "datetime", FrozenDatetime)
    return calls


# ---------------------------------------------------------------------------
# FR-F0.2 — ML_WEATHER_ENABLED
# ---------------------------------------------------------------------------


class TestMLWeatherFlag:
    def test_flag_defaults_off(self):
        assert weather_bot.ML_WEATHER_ENABLED is False

    def test_flag_off_builds_v2_only_waterfall(self, monkeypatch):
        ml_cls = MagicMock(name="MLWeatherStrategy")
        monkeypatch.setattr(weather_bot, "ML_WEATHER_ENABLED", False)
        monkeypatch.setattr(weather_bot, "MLWeatherStrategy", ml_cls)

        bot = WeatherBot()

        assert list(bot.strategies) == ["weather"]
        assert isinstance(bot.strategies["weather"], WeatherArbitrageStrategyV2)
        ml_cls.assert_not_called()

    def test_flag_on_restores_ml_first_waterfall(self, monkeypatch):
        ml_cls = MagicMock(name="MLWeatherStrategy")
        monkeypatch.setattr(weather_bot, "ML_WEATHER_ENABLED", True)
        monkeypatch.setattr(weather_bot, "MLWeatherStrategy", ml_cls)

        bot = WeatherBot()

        # Dict order is waterfall order: ML first, V2 fallback — as before.
        assert list(bot.strategies) == ["ml_weather", "weather"]
        ml_cls.assert_called_once_with()

    def test_tick_goes_straight_to_v2_when_flag_off(self, monkeypatch, mp_caplog):
        monkeypatch.setattr(weather_bot, "ML_WEATHER_ENABLED", False)
        monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
        ml_cls = MagicMock(name="MLWeatherStrategy")
        monkeypatch.setattr(weather_bot, "MLWeatherStrategy", ml_cls)

        bot = _wire(WeatherBot(), [_md("KXHIGHNY-26SEP02-B81.5")])
        v2 = MagicMock(analyze=MagicMock(return_value=[]))
        bot.strategies["weather"] = v2
        bot._process_signals = MagicMock(return_value=False)

        bot.tick(MagicMock(), MagicMock())

        names = [c.kwargs["strategy_name"] for c in bot._process_signals.call_args_list]
        assert names == ["Meteorologist V2"]
        v2.analyze.assert_called_once()
        ml_cls.assert_not_called()
        assert "ML Weather" not in mp_caplog.text

    def test_tick_runs_ml_first_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(weather_bot, "ML_WEATHER_ENABLED", True)
        monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
        ml_instance = MagicMock(analyze=MagicMock(return_value=[]))
        monkeypatch.setattr(
            weather_bot, "MLWeatherStrategy", MagicMock(return_value=ml_instance)
        )

        bot = _wire(WeatherBot(), [_md("KXHIGHNY-26SEP02-B81.5")])
        v2 = MagicMock(analyze=MagicMock(return_value=[]))
        bot.strategies["weather"] = v2
        bot._process_signals = MagicMock(return_value=False)

        bot.tick(MagicMock(), MagicMock())

        names = [c.kwargs["strategy_name"] for c in bot._process_signals.call_args_list]
        assert names == ["ML Weather", "Meteorologist V2"]
        ml_instance.analyze.assert_called_once()
        v2.analyze.assert_called_once()

    def test_tick_v2_not_reached_when_ml_trades(self, monkeypatch):
        """Waterfall semantics unchanged with the flag on: ML trade short-circuits V2."""
        monkeypatch.setattr(weather_bot, "ML_WEATHER_ENABLED", True)
        monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
        monkeypatch.setattr(
            weather_bot,
            "MLWeatherStrategy",
            MagicMock(return_value=MagicMock(analyze=MagicMock(return_value=[]))),
        )
        bot = _wire(WeatherBot(), [_md("KXHIGHNY-26SEP02-B81.5")])
        v2 = MagicMock(analyze=MagicMock(return_value=[]))
        bot.strategies["weather"] = v2
        bot._process_signals = MagicMock(return_value=True)

        bot.tick(MagicMock(), MagicMock())

        names = [c.kwargs["strategy_name"] for c in bot._process_signals.call_args_list]
        assert names == ["ML Weather"]
        v2.analyze.assert_not_called()


# ---------------------------------------------------------------------------
# FR-F0.3 — _ladder_for_city on the ET calendar
# ---------------------------------------------------------------------------


class TestLadderEtDate:
    def _bot_with(self, symbols, city_key="NY"):
        bot = _wire(WeatherBot(), [_md(s) for s in symbols], city_key=city_key)
        return bot

    def test_late_evening_et_keeps_todays_ladder(self, monkeypatch):
        # 2026-09-02T02:30Z == 22:30 ET on 2026-09-01. A naive UTC clock would
        # call 09-02 "today" and drop the still-open 26SEP01 ladder.
        calls = _freeze_clock(monkeypatch, datetime(2026, 9, 2, 2, 30, tzinfo=timezone.utc))
        bot = self._bot_with(
            [
                "KXHIGHNY-26AUG31-B79.5",
                "KXHIGHNY-26SEP01-B81.5",
                "KXHIGHNY-26SEP02-B82.5",
                "KXHIGHNY-26SEP03-B83.5",
            ]
        )

        tracked = {m.symbol for m in bot._ladder_for_city("KXHIGHNY")}

        assert "KXHIGHNY-26SEP01-B81.5" in tracked  # ET "today"
        assert "KXHIGHNY-26SEP02-B82.5" in tracked  # ET "tomorrow"
        assert "KXHIGHNY-26AUG31-B79.5" in tracked  # ET "yesterday" (D-1)
        # A dropped D+2 proves the date filter ran (not the full-ladder fallback).
        assert "KXHIGHNY-26SEP03-B83.5" not in tracked
        # The clock consulted was the ET one, never the host wall clock.
        assert calls and all(tz is ET for tz in calls)

    def test_early_morning_et_keeps_yesterday_for_lax(self, monkeypatch):
        # 2026-09-02T07:00Z == 03:00 ET on 09-02. LAX's 09-01 ladder closes
        # 07:59Z (03:59 ET), so it is still open and must stay tracked even
        # though the ET date has already rolled to 09-02.
        _freeze_clock(monkeypatch, datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc))
        bot = self._bot_with(
            [
                "KXHIGHLAX-26SEP01-B76.5",
                "KXHIGHLAX-26SEP02-B77.5",
                "KXHIGHLAX-26SEP03-B78.5",
                "KXHIGHLAX-26SEP04-B79.5",
            ],
            city_key="LAX",
        )

        tracked = {m.symbol for m in bot._ladder_for_city("KXHIGHLAX")}

        assert "KXHIGHLAX-26SEP01-B76.5" in tracked  # D-1, still open
        assert "KXHIGHLAX-26SEP02-B77.5" in tracked
        assert "KXHIGHLAX-26SEP03-B78.5" in tracked
        assert "KXHIGHLAX-26SEP04-B79.5" not in tracked

    def test_no_match_falls_back_to_full_ladder(self, monkeypatch):
        _freeze_clock(monkeypatch, datetime(2026, 9, 2, 2, 30, tzinfo=timezone.utc))
        bot = self._bot_with(["KXHIGHNY-26OCT15-B70.5"])

        assert [m.symbol for m in bot._ladder_for_city("KXHIGHNY")] == [
            "KXHIGHNY-26OCT15-B70.5"
        ]


# ---------------------------------------------------------------------------
# FR-F0.4 — EXECUTED line / check_order use the post-cap quantity
# ---------------------------------------------------------------------------


class _Host(SignalProcessorMixin):
    pass


class TestExecutedQtyIsPostCap:
    SYMBOL = "KXHIGHNY-26SEP02-B81.5"
    STRATEGY = "F0 qty probe"  # not a name with a persisted win-rate window

    def test_logged_checked_and_booked_quantities_agree(self, mp_caplog):
        rm = RiskManager(starting_balance=100_000.0)

        # Precondition: at this bankroll Kelly sizes ABOVE the per-entry cap
        # (its own ceiling is 75), so the clamp is what makes the numbers agree.
        kelly = rm.calculate_kelly_size(0.8, 0.50, self.STRATEGY, symbol=self.SYMBOL)
        assert kelly > MAX_CONTRACTS

        check_spy = MagicMock(wraps=rm.check_order)
        rm.check_order = check_spy
        dash = MagicMock()
        sig = TradeSignal(
            symbol=self.SYMBOL,
            side="buy",
            quantity=1,
            limit_price=0.50,
            confidence=0.8,
            strike_type="between",
            floor_strike=81.0,
            cap_strike=82.0,
        )

        traded = _Host()._process_signals([sig], self.STRATEGY, rm, dash)

        assert traded is True
        pos = rm.exchange.positions[-1]
        booked_qty = pos["quantity"]
        assert booked_qty == MAX_CONTRACTS == 50
        assert sig.quantity == booked_qty

        # check_order saw the booked quantity and its cost, not Kelly's.
        _, kw = check_spy.call_args
        assert kw["quantity"] == booked_qty
        assert check_spy.call_args[0][0] == pytest.approx(0.50 * booked_qty)

        executed = [
            r.getMessage() for r in mp_caplog.records if "[Signal] EXECUTED" in r.getMessage()
        ]
        assert len(executed) == 1
        assert f"qty={booked_qty} " in executed[0]
        assert f"cost={0.50 * booked_qty:.2f}" in executed[0]

        # Dashboard EXEC line agrees too.
        exec_lines = [c.args[0] for c in dash.log.call_args_list if "EXEC:" in c.args[0]]
        assert exec_lines and f"{booked_qty}x {self.SYMBOL}" in exec_lines[0]

        # The exchange-side cap stays as a last line of defence but no longer
        # has to fire on this path: what was logged is what was booked.
        assert not any(
            "Position size capped" in r.getMessage() for r in mp_caplog.records
        )
