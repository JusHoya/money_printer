from datetime import datetime
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.crypto_strategy import (
    Crypto15mTrendStrategyV3,
    CryptoLongShotFader,
    Crypto15mLateSniper,
)
from src.strategies.ml_btc_15m import MLBtc15mStrategy
from src.strategies.latency_arb import LatencyArbStrategy
from src.strategies.longshot_fader_v2 import LongshotFaderV2
from src.strategies.time_decay import TimeDecayScalper
from src.strategies.cross_spread_arb import CrossSpreadArbStrategy
from src.data.coinbase_provider import CoinbaseProvider
from src.utils.logger import logger


@BotRegistry.register("btc_15m")
class BTC15mBot(Bot, TickerResolverMixin, SignalProcessorMixin):
    def __init__(self):
        Bot.__init__(self, name="BTC 15m")
        TickerResolverMixin.__init__(self)
        self.last_15m_trade_interval = None
        self.ticks = 0
        self.kalshi = None
        self.coinbase = None

        # ML-driven strategies (Sprint 3) with V3 rule-based fallback
        self.strategies = {
            "ml_btc_15m": MLBtc15mStrategy(),
            "latency_arb": LatencyArbStrategy(),
            "crypto": Crypto15mTrendStrategyV3(),  # V3 fallback
            "longshot_v2": LongshotFaderV2(),
            "longshot": CryptoLongShotFader(),  # V1 fallback
            "time_decay": TimeDecayScalper(),
            "cross_arb": CrossSpreadArbStrategy(),
            "late_sniper": Crypto15mLateSniper(),
        }

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        self.coinbase = coinbase or CoinbaseProvider("BTC-USD")
        if not coinbase:
            self.coinbase.connect()

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        self.ticks += 1

        btc_data = self.coinbase.fetch_latest()
        if not btc_data:
            if self.ticks % 10 == 0:
                dashboard.log("[BTC 15m] ⚠️ Coinbase Fetch Failed")
            return []

        dashboard.update_price("BTC-USD (Coinbase)", btc_data.price)

        if not self.kalshi:
            return []

        btc_15m_resolved = False
        try:
            btc_15m = self._resolve_smart_ticker(
                "KXBTC15M", criteria="time", kalshi=self.kalshi, coinbase=self.coinbase
            )
            if btc_15m:
                k_data_15 = self.kalshi.fetch_latest(btc_15m)
                if k_data_15:
                    best_price = (
                        k_data_15.bid
                        if k_data_15.bid > 0
                        else (k_data_15.ask if k_data_15.ask > 0 else k_data_15.price)
                    )
                    dashboard.update_price(
                        f"{btc_15m} (15m)",
                        best_price,
                        bid=k_data_15.bid,
                        ask=k_data_15.ask,
                        no_bid=k_data_15.extra.get("no_bid", 0.0)
                        if k_data_15.extra
                        else 0.0,
                        no_ask=k_data_15.extra.get("no_ask", 0.0)
                        if k_data_15.extra
                        else 0.0,
                        volume=k_data_15.volume,
                    )
                    # Fuse data
                    original_spot = btc_data.price
                    btc_data.bid = k_data_15.bid
                    btc_data.ask = k_data_15.ask
                    btc_data.symbol = btc_15m
                    if btc_data.extra is None:
                        btc_data.extra = {}
                    btc_data.extra["spot_price"] = original_spot
                    risk_manager.update_market_data(btc_15m, btc_data.price)
                    btc_15m_resolved = True
            else:
                if self.ticks % 60 == 0:
                    logger.warning(
                        "[BTC 15m] Ghost Ticker: No active KXBTC15M markets found."
                    )
        except Exception as e:
            logger.error(f"[BTC 15m] Market Fetch Fail: {e}")

        if not btc_15m_resolved:
            return []

        # 15m interval gating
        now = datetime.now()
        current_interval_id = now.hour * 4 + now.minute // 15
        minute_in_interval = now.minute % 15
        can_trade = (
            minute_in_interval >= 7
            and current_interval_id != self.last_15m_trade_interval
        )

        if not can_trade:
            if minute_in_interval < 7 and self.ticks % 30 == 0:
                logger.debug(
                    f"[BTC 15m Gate] Waiting for minute 7 (currently min {minute_in_interval})"
                )
            return []

        # Waterfall: Cross-Spread Arb → ML BTC 15m → Latency Arb → V3 Fallback
        #   → LongShot Fader V2 → Time Decay → Late Sniper
        # Cross-spread arb is risk-free, always check first
        traded = self._process_signals(
            self.strategies["cross_arb"].analyze(btc_data),
            strategy_name="Cross-Spread Arb",
            risk_manager=risk_manager,
            dashboard=dashboard,
        )
        if not traded:
            traded = self._process_signals(
                self.strategies["ml_btc_15m"].analyze(btc_data),
                strategy_name="ML BTC 15m",
                risk_manager=risk_manager,
                dashboard=dashboard,
            )
        if not traded:
            traded = self._process_signals(
                self.strategies["latency_arb"].analyze(btc_data),
                strategy_name="Latency Arb",
                risk_manager=risk_manager,
                dashboard=dashboard,
            )
        if not traded:
            traded = self._process_signals(
                self.strategies["crypto"].analyze(btc_data),
                strategy_name="Trend Catcher V3",
                risk_manager=risk_manager,
                dashboard=dashboard,
            )
        if not traded:
            traded = self._process_signals(
                self.strategies["longshot_v2"].analyze(btc_data),
                strategy_name="LongShot Fader V2",
                risk_manager=risk_manager,
                dashboard=dashboard,
            )
        if not traded:
            traded = self._process_signals(
                self.strategies["time_decay"].analyze(btc_data),
                strategy_name="Time Decay",
                risk_manager=risk_manager,
                dashboard=dashboard,
            )
        if not traded:
            traded = self._process_signals(
                self.strategies["late_sniper"].analyze(btc_data),
                strategy_name="Late Sniper",
                risk_manager=risk_manager,
                dashboard=dashboard,
            )

        if traded:
            self.last_15m_trade_interval = current_interval_id

        return []

    def get_symbols(self) -> List[str]:
        return ["KXBTC15M"]
