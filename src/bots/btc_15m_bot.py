from datetime import datetime
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.ml_btc_15m import MLBtc15mStrategy
from src.strategies.latency_arb import LatencyArbStrategy
from src.strategies.longshot_fader_v2 import LongshotFaderV2
from src.strategies.cross_spread_arb import CrossSpreadArbStrategy
from src.data.coinbase_provider import CoinbaseProvider
from src.utils.logger import logger


@BotRegistry.register("btc_15m")
class BTC15mBot(Bot, TickerResolverMixin, SignalProcessorMixin):
    def __init__(self):
        Bot.__init__(self, name="BTC 15m")
        TickerResolverMixin.__init__(self)
        self.ticks = 0
        self.kalshi = None
        self.coinbase = None

        # Microstructure-first strategy waterfall (Sprint 5+)
        # Relaxed thresholds for data collection / ML training bootstrap
        self.strategies = {
            "cross_arb": CrossSpreadArbStrategy(),
            # "empirical_edge": DISABLED — 2231 trades, -$38,452 PnL, 47.8% WR, expectancy -$17.24/trade
            "latency_arb": LatencyArbStrategy(),
            # "time_decay": DISABLED — 25% WR, -$769 over 20 trades, avg entry 0.91
            "longshot_v2": LongshotFaderV2(),
            "ml_btc_15m": MLBtc15mStrategy(),
            # "late_sniper": Crypto15mLateSniper(),  # DISABLED: 8% WR, -$8,310 over 113 trades
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
                    # Fuse data: Coinbase spot + Kalshi contract prices
                    original_spot = btc_data.price
                    btc_data.bid = k_data_15.bid
                    btc_data.ask = k_data_15.ask
                    btc_data.symbol = btc_15m
                    if btc_data.extra is None:
                        btc_data.extra = {}
                    btc_data.extra["spot_price"] = original_spot
                    # Pass through Kalshi metadata for strategies
                    if k_data_15.extra:
                        btc_data.extra["strike"] = k_data_15.extra.get("strike")
                        btc_data.extra["close_time"] = k_data_15.extra.get("close_time")
                        btc_data.extra["no_bid"] = k_data_15.extra.get("no_bid", 0)
                        btc_data.extra["no_ask"] = k_data_15.extra.get("no_ask", 0)
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

        # Entry timing windows for 15-min contracts:
        #   Early window  (min 1-4):  max time for thesis to play out
        #   Dead zone     (min 5-9):  worst of both worlds — skip
        #   Late window   (min 10-13): max info available, theta favors sellers
        # Risk manager (rate limit + cooldowns) controls trade frequency.
        now = datetime.now()
        minute_in_interval = now.minute % 15
        if minute_in_interval < 1 or 5 <= minute_in_interval <= 9:
            return []

        # Waterfall: risk-free arb > latency > time decay > longshot > ML > sniper
        if self.ticks % 20 == 0:
            logger.info(
                f"[BTC 15m] Evaluating {btc_data.symbol} | "
                f"spot={btc_data.extra.get('spot_price', '?')}, "
                f"bid={btc_data.bid}, ask={btc_data.ask}, "
                f"strike={btc_data.extra.get('strike', '?')}"
            )
        for strat_key, strat_name in [
            ("cross_arb", "Cross-Spread Arb"),
            # ("empirical_edge", "Empirical Edge"),  # DISABLED — see strategies dict
            ("latency_arb", "Latency Arb"),
            # ("time_decay", "Time Decay"),  # DISABLED
            ("longshot_v2", "LongShot Fader V2"),
            ("ml_btc_15m", "ML BTC 15m"),
            # ("late_sniper", "Late Sniper"),  # DISABLED
        ]:
            signals = self.strategies[strat_key].analyze(btc_data)
            if signals and self.ticks % 10 == 0:
                logger.info(f"[BTC 15m] {strat_name} → {len(signals)} signal(s)")
            traded = self._process_signals(
                signals,
                strategy_name=strat_name,
                risk_manager=risk_manager,
                dashboard=dashboard,
            )
            if traded:
                break

        return []

    def get_symbols(self) -> List[str]:
        return ["KXBTC15M"]
