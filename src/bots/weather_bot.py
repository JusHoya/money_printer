import time
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2
from src.strategies.ml_weather import MLWeatherStrategy
from src.data.nws_provider import NWSProvider
from src.data.metar_provider import METARProvider
from src.utils.logger import logger
import os


# 2026-06-03 review (LEAN config): ML Weather (-$137 net/21d, ~60% of all fees)
# and Meteorologist V2 (-$75 net, ~14% of fees) are net-negative fee bleeders.
# Both weather strategies are disabled from live trading. Flip this flag back
# to True to re-enable the weather waterfall. Data/price feeds still run.
WEATHER_TRADING_ENABLED = False


@BotRegistry.register("weather")
class WeatherBot(Bot, TickerResolverMixin, SignalProcessorMixin):
    # Legacy NWS station mapping (kept for backwards compatibility / forecasts)
    STATION_MAP = {
        "KNYC": "KXHIGHNY",
        "KLAX": "KXHIGHLAX",
        "KMDW": "KXHIGHCHI",
        "KMIA": "KXHIGHMIA",
    }

    # Airport ASOS stations for METAR data (faster, higher precision)
    METAR_STATIONS = ["KJFK", "KLAX", "KORD", "KMIA"]

    # Map METAR stations to Kalshi tickers
    METAR_TO_KALSHI = {
        "KJFK": "KXHIGHNY",
        "KLAX": "KXHIGHLAX",
        "KORD": "KXHIGHCHI",
        "KMIA": "KXHIGHMIA",
    }

    # Map METAR stations back to NWS stations (for forecasts)
    METAR_TO_NWS = {
        "KJFK": "KNYC",
        "KLAX": "KLAX",
        "KORD": "KMDW",
        "KMIA": "KMIA",
    }

    def __init__(self):
        Bot.__init__(self, name="Weather")
        TickerResolverMixin.__init__(self)
        self.kalshi = None
        self.nws = None
        self.metar = None
        self.nws_stations = ["KNYC", "KLAX", "KMDW", "KMIA"]

        # ML-driven primary + V2 rule-based fallback
        self.strategies = {
            "ml_weather": MLWeatherStrategy(),
            "weather": WeatherArbitrageStrategyV2(),
        }

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        if nws:
            self.nws = nws
        else:
            nws_ua = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
            self.nws = NWSProvider(nws_ua, self.nws_stations)
            self.nws.connect()

        # Initialize METAR provider for faster temperature observations
        self.metar = METARProvider(self.METAR_STATIONS)
        self.metar.connect()

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        if not self.nws:
            return []

        for metar_station in self.METAR_STATIONS:
            kalshi_ticker = self.METAR_TO_KALSHI.get(metar_station)
            nws_station = self.METAR_TO_NWS.get(metar_station)
            active_ticker = None

            # --- Fetch observation data (METAR primary, NWS fallback) ---
            obs_data = None
            if self.metar:
                try:
                    metar_data = self.metar.fetch_latest(metar_station)
                    if metar_data:
                        age = (
                            time.time() - metar_data.timestamp.timestamp()
                            if metar_data.timestamp
                            else 0
                        )
                        logger.info(
                            f"[Weather] Using METAR data for {metar_station} (age: {age:.0f}s)"
                        )
                        obs_data = metar_data
                except Exception as e:
                    logger.warning(
                        f"[Weather] METAR fetch failed for {metar_station}: {e}"
                    )

            # Fallback to NWS if METAR unavailable
            if obs_data is None and nws_station:
                nws_data = self.nws.fetch_latest(nws_station)
                if nws_data:
                    logger.info(f"[Weather] Falling back to NWS data for {nws_station}")
                    obs_data = nws_data

            if not obs_data:
                continue

            # --- Merge NWS forecast into METAR observation ---
            # Strategies expect extra["forecast"] for 7-day forecast data.
            # METAR gives better temps; NWS gives forecasts.
            if obs_data is not None and nws_station:
                nws_forecast_data = self.nws.fetch_latest(nws_station)
                if nws_forecast_data and nws_forecast_data.extra:
                    forecast = nws_forecast_data.extra.get("forecast")
                    if forecast and (
                        not obs_data.extra or not obs_data.extra.get("forecast")
                    ):
                        if obs_data.extra is None:
                            obs_data.extra = {}
                        obs_data.extra["forecast"] = forecast

            temp = obs_data.extra.get("temperature_f") if obs_data.extra else None

            # --- Fetch live Kalshi price and fuse ---
            if self.kalshi and kalshi_ticker:
                try:
                    active_ticker = self._resolve_smart_ticker(
                        kalshi_ticker, criteria="sentiment", kalshi=self.kalshi
                    )

                    if active_ticker:
                        k_data = self.kalshi.fetch_latest(active_ticker)
                        if k_data:
                            max_t = (
                                obs_data.extra.get("max_temp_today_f")
                                if obs_data.extra
                                else None
                            )
                            best_price = (
                                k_data.bid
                                if k_data.bid > 0
                                else (k_data.ask if k_data.ask > 0 else k_data.price)
                            )
                            dashboard.update_price(
                                f"{active_ticker} (Market)",
                                best_price,
                                bid=k_data.bid,
                                ask=k_data.ask,
                                no_bid=k_data.extra.get("no_bid", 0.0)
                                if k_data.extra
                                else 0.0,
                                no_ask=k_data.extra.get("no_ask", 0.0)
                                if k_data.extra
                                else 0.0,
                                volume=k_data.volume,
                                max_temp=max_t,
                            )

                            # Fuse Kalshi prices into observation data
                            obs_data.bid = k_data.bid
                            obs_data.ask = k_data.ask
                            obs_data.price = k_data.price
                            obs_data.symbol = active_ticker
                except Exception as e:
                    logger.error(f"[Weather] Market Fetch Fail ({kalshi_ticker}): {e}")

            # Use real Kalshi market price for position valuation (not raw temp)
            kalshi_market_price = None
            if active_ticker and obs_data.bid > 0:
                kalshi_market_price = obs_data.bid  # fused from k_data above
            elif active_ticker and obs_data.price > 0:
                kalshi_market_price = obs_data.price

            if temp:
                dashboard.update_price(f"{kalshi_ticker or metar_station} (F)", temp)
                if active_ticker and kalshi_market_price and kalshi_market_price > 0:
                    risk_manager.update_market_data(active_ticker, kalshi_market_price)
                    risk_manager.exchange.update_market_price(
                        active_ticker, kalshi_market_price
                    )
                elif active_ticker:
                    risk_manager.update_market_data(active_ticker, temp)
                else:
                    risk_manager.update_market_data(f"TEMP_{metar_station}", temp)

            # Extract PoP for Precip
            forecasts = (obs_data.extra.get("forecast") or []) if obs_data.extra else []
            pop_prob = 0.0
            for period in forecasts:
                if period.get("isDaytime"):
                    val = period.get("probabilityOfPrecipitation", {}).get("value", 0)
                    if val:
                        pop_prob = val / 100.0
                    break

            if pop_prob is not None:
                if active_ticker:
                    risk_manager.update_market_data(f"{active_ticker}_PRECIP", pop_prob)
                else:
                    risk_manager.update_market_data(f"PRECIP_{metar_station}", pop_prob)

            # Waterfall: ML Weather → V2 rule-based fallback
            # 2026-06-03 review: weather trading disabled (fee bleed). Price/data
            # feeds above still run for dashboard + future re-enable.
            if WEATHER_TRADING_ENABLED:
                traded = self._process_signals(
                    self.strategies["ml_weather"].analyze(obs_data),
                    strategy_name="ML Weather",
                    risk_manager=risk_manager,
                    dashboard=dashboard,
                )
                if not traded:
                    self._process_signals(
                        self.strategies["weather"].analyze(obs_data),
                        strategy_name="Meteorologist V2",
                        risk_manager=risk_manager,
                        dashboard=dashboard,
                    )

            time.sleep(1)  # 1 sec between cities

        return []

    def get_symbols(self) -> List[str]:
        return list(self.STATION_MAP.values())
