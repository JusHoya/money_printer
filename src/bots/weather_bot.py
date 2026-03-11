import time
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2
from src.data.nws_provider import NWSProvider
from src.utils.logger import logger
import os


@BotRegistry.register("weather")
class WeatherBot(Bot, TickerResolverMixin, SignalProcessorMixin):

    STATION_MAP = {
        "KNYC": "KXHIGHNY",
        "KLAX": "KXHIGHLAX",
        "KMDW": "KXHIGHCHI",
        "KMIA": "KXHIGHMIA",
    }

    def __init__(self):
        Bot.__init__(self, name="Weather")
        TickerResolverMixin.__init__(self)
        self.kalshi = None
        self.nws = None
        self.nws_stations = ["KNYC", "KLAX", "KMDW", "KMIA"]

        self.strategies = {
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

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        if not self.nws:
            return []

        for station in self.nws_stations:
            nws_data = self.nws.fetch_latest(station)
            if not nws_data:
                continue

            temp = nws_data.extra.get('temperature_f')
            kalshi_ticker = self.STATION_MAP.get(station)
            active_ticker = None

            # Fetch live Kalshi price
            if self.kalshi and kalshi_ticker:
                try:
                    active_ticker = self._resolve_smart_ticker(
                        kalshi_ticker, criteria="sentiment",
                        kalshi=self.kalshi
                    )

                    if active_ticker:
                        k_data = self.kalshi.fetch_latest(active_ticker)
                        if k_data:
                            max_t = nws_data.extra.get('max_temp_today_f')
                            dashboard.update_price(f"{active_ticker} (Market)", k_data.bid, max_temp=max_t)

                            # Fuse data
                            nws_data.bid = k_data.bid
                            nws_data.ask = k_data.ask
                            nws_data.price = k_data.price
                            nws_data.symbol = active_ticker
                except Exception as e:
                    logger.error(f"[Weather] Market Fetch Fail ({kalshi_ticker}): {e}")

            if temp:
                dashboard.update_price(f"{kalshi_ticker or station} (F)", temp)
                if active_ticker:
                    risk_manager.update_market_data(active_ticker, temp)
                else:
                    risk_manager.update_market_data(f"TEMP_{station}", temp)

            # Extract PoP for Precip
            forecasts = nws_data.extra.get('forecast') or []
            pop_prob = 0.0
            for period in forecasts:
                if period.get('isDaytime'):
                    val = period.get('probabilityOfPrecipitation', {}).get('value', 0)
                    if val: pop_prob = val / 100.0
                    break

            if pop_prob is not None:
                if active_ticker:
                    risk_manager.update_market_data(f"{active_ticker}_PRECIP", pop_prob)
                else:
                    risk_manager.update_market_data(f"PRECIP_{station}", pop_prob)

            self._process_signals(
                self.strategies['weather'].analyze(nws_data),
                strategy_name="Meteorologist V1", risk_manager=risk_manager, dashboard=dashboard
            )

            time.sleep(1)  # 1 sec between cities

        return []

    def get_symbols(self) -> List[str]:
        return list(self.STATION_MAP.values())
