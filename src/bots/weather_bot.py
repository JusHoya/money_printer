import time
from datetime import datetime, timedelta
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

    # FR-0.7 harvester cadence: orderbook depth snapshots are HOURLY only —
    # the per-tick path must never issue per-market orderbook calls.
    DEPTH_SNAPSHOT_INTERVAL_S = 3600
    # Safety cap on orderbook calls per city per hourly snapshot pass.
    MAX_DEPTH_MARKETS_PER_CITY = 30

    def __init__(self):
        Bot.__init__(self, name="Weather")
        TickerResolverMixin.__init__(self)
        self.kalshi = None
        self.nws = None
        self.metar = None
        self.nws_stations = ["KNYC", "KLAX", "KMDW", "KMIA"]
        # Epoch of last hourly orderbook-depth snapshot (0 = snapshot on
        # first tick so a fresh session records a baseline immediately).
        self._last_depth_snapshot = 0.0

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

        # FR-0.7: decide ONCE per tick whether the hourly depth snapshot is
        # due. Timestamp is advanced after the city loop so all cities get
        # snapshotted in the same pass.
        depth_due = (
            time.time() - self._last_depth_snapshot
        ) >= self.DEPTH_SNAPSHOT_INTERVAL_S

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

            # --- FR-0.7 harvester: record the FULL ladder, fuse the active ---
            # One /markets list call per city per tick supplies bid/ask/no-side/
            # last/volume for every bracket; no per-market quote calls.
            k_data = None
            if self.kalshi and kalshi_ticker:
                try:
                    max_t = (
                        obs_data.extra.get("max_temp_today_f")
                        if obs_data.extra
                        else None
                    )
                    ladder = self._ladder_for_city(kalshi_ticker)

                    if ladder:
                        # Active market = highest YES bid (same "sentiment"
                        # criterion the legacy resolver used).
                        k_data = max(ladder, key=lambda m: m.bid)
                        active_ticker = k_data.symbol

                        for m in ladder:
                            best_price = (
                                m.bid
                                if m.bid > 0
                                else (m.ask if m.ask > 0 else m.price)
                            )
                            m_extra = m.extra or {}
                            kwargs = dict(
                                bid=m.bid,
                                ask=m.ask,
                                no_bid=m_extra.get("no_bid", 0.0),
                                no_ask=m_extra.get("no_ask", 0.0),
                                last=m.price,
                                volume=m.volume,
                            )
                            if m.symbol == active_ticker and max_t is not None:
                                kwargs["max_temp"] = max_t
                            dashboard.update_price(
                                f"{m.symbol} (Market)", best_price, **kwargs
                            )
                    else:
                        # Fallback: legacy single-market resolution path
                        active_ticker = self._resolve_smart_ticker(
                            kalshi_ticker, criteria="sentiment", kalshi=self.kalshi
                        )
                        if active_ticker:
                            k_data = self.kalshi.fetch_latest(active_ticker)
                            if k_data:
                                best_price = (
                                    k_data.bid
                                    if k_data.bid > 0
                                    else (
                                        k_data.ask if k_data.ask > 0 else k_data.price
                                    )
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
                                    last=k_data.price,
                                    volume=k_data.volume,
                                    max_temp=max_t,
                                )

                    # Hourly top-3 orderbook depth snapshot (never per tick)
                    if depth_due and ladder:
                        self._snapshot_depth(ladder, dashboard)

                    if k_data and active_ticker:
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

        if depth_due:
            self._last_depth_snapshot = time.time()

        return []

    def _ladder_for_city(self, series_base):
        """Full-quote ladder for a city's series, filtered to tracked dates.

        One /markets list call (via ``fetch_market_ladder``) — no per-market
        fetches. Tracked = today's and tomorrow's events, mirroring the
        legacy sentiment resolver's date targeting; if none match, the full
        active ladder is returned defensively.
        """
        if not hasattr(self.kalshi, "fetch_market_ladder"):
            return []
        ladder = self.kalshi.fetch_market_ladder(series_base) or []
        if not ladder:
            return []
        now = datetime.now()
        target_dates = [
            now.strftime("%y%b%d").upper(),
            (now + timedelta(days=1)).strftime("%y%b%d").upper(),
        ]
        tracked = [m for m in ladder if any(d in m.symbol for d in target_dates)]
        return tracked or ladder

    def _snapshot_depth(self, ladder, dashboard):
        """Record hourly top-3 orderbook levels for each tracked market.

        Called only when the hourly snapshot is due (see tick); throttled
        between calls to stay well under Kalshi rate limits.
        """
        for m in ladder[: self.MAX_DEPTH_MARKETS_PER_CITY]:
            try:
                book = self.kalshi.fetch_orderbook(m.symbol, depth=3)
                if book and (book.get("yes") or book.get("no")):
                    dashboard.record_depth(m.symbol, book, last_price=m.price)
            except Exception as e:
                logger.warning(f"[Weather] Depth snapshot failed for {m.symbol}: {e}")
            time.sleep(0.15)

    def get_symbols(self) -> List[str]:
        return list(self.STATION_MAP.values())
