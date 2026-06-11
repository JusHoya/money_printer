import copy
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.ml_btc_hourly import MLBtcHourlyStrategy
from src.data.coinbase_provider import CoinbaseProvider
from src.utils.logger import logger

# 2026-06-10 ML-label fix: reuse the single source of truth for the
# logged-price selection so a cleared/locked book (bid==0, ask pinned ~1.0)
# is not recorded as a misleading 1.0 in the harvested training CSV.
from src.bots.btc_15m_bot import _best_observable_price


# 2026-06-03 review (LEAN config): ML BTC Hourly is net-negative (-$4 net/21d,
# -$440 lifetime). Disabled from live trading. The strategy still receives spot
# ticks (price-history warmup) but its signals are not executed. Flip this flag
# back to True to re-enable hourly execution.
BTC_HOURLY_TRADING_ENABLED = False


@BotRegistry.register("btc_hourly")
class BTCHourlyBot(Bot, TickerResolverMixin, SignalProcessorMixin):
    def __init__(self):
        Bot.__init__(self, name="BTC Hourly")
        TickerResolverMixin.__init__(self)
        self.ticks = 0
        self.kalshi = None
        self.coinbase = None

        self.strategies = {
            "ml_hourly": MLBtcHourlyStrategy(min_edge=0.08, cooldown_seconds=120),
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
            return []

        # Feed raw spot price to hourly strategies for price history
        spot_feed = copy.deepcopy(btc_data)
        spot_feed.symbol = "BTC-USD (Coinbase)"
        if spot_feed.extra is None:
            spot_feed.extra = {}
        spot_feed.extra["source"] = "live_coinbase"
        self.strategies["ml_hourly"].analyze(spot_feed)

        if not self.kalshi:
            return []

        try:
            ladder = self._resolve_btc_ladder(
                kalshi=self.kalshi, coinbase=self.coinbase
            )

            if ladder:
                for ticker in ladder:
                    k_data_ladder = self.kalshi.fetch_latest(ticker)
                    if k_data_ladder:
                        # 2026-06-10 ML-label fix: route through the shared
                        # helper so a cleared/locked book (bid==0, ask pinned
                        # at ~1.0) is not logged as 1.0 and mislabeled YES.
                        best_price = _best_observable_price(
                            k_data_ladder.bid,
                            k_data_ladder.ask,
                            k_data_ladder.price,
                        )
                        dashboard.update_price(
                            f"{ticker} (1h)",
                            best_price,
                            bid=k_data_ladder.bid,
                            ask=k_data_ladder.ask,
                            no_bid=k_data_ladder.extra.get("no_bid", 0.0)
                            if k_data_ladder.extra
                            else 0.0,
                            no_ask=k_data_ladder.extra.get("no_ask", 0.0)
                            if k_data_ladder.extra
                            else 0.0,
                            volume=k_data_ladder.volume,
                        )
                        # Cache real Kalshi price for position valuation
                        if best_price > 0:
                            risk_manager.exchange.update_market_price(
                                ticker, best_price
                            )

                # Use center ticker for strategy
                center_ticker = ladder[0]
                k_data_center = self.kalshi.fetch_latest(center_ticker)

                if k_data_center:
                    btc_data_hr = copy.deepcopy(btc_data)
                    btc_data_hr.bid = k_data_center.bid
                    btc_data_hr.ask = k_data_center.ask
                    btc_data_hr.symbol = center_ticker
                    if k_data_center.extra is None:
                        k_data_center.extra = {}
                    if btc_data_hr.extra is None:
                        btc_data_hr.extra = {}
                    # Clear Coinbase source so strategies don't treat this as a spot tick
                    btc_data_hr.extra.pop("source", None)
                    btc_data_hr.extra["no_bid"] = k_data_center.extra.get("no_bid", 0.0)
                    btc_data_hr.extra["no_ask"] = k_data_center.extra.get("no_ask", 0.0)
                    btc_data_hr.extra["close_time"] = k_data_center.extra.get(
                        "close_time"
                    )
                    btc_data_hr.extra["strike"] = k_data_center.extra.get("strike")
                    btc_data_hr.extra["spot_price"] = btc_data.price

                    if self.ticks % 20 == 0:
                        logger.info(
                            f"[BTC Hourly] Evaluating {center_ticker} | "
                            f"spot={btc_data.price:.0f}, "
                            f"bid={btc_data_hr.bid}, ask={btc_data_hr.ask}, "
                            f"strike={btc_data_hr.extra.get('strike', '?')}"
                        )
                    ml_signals = self.strategies["ml_hourly"].analyze(btc_data_hr)
                    if ml_signals and self.ticks % 10 == 0:
                        logger.info(
                            f"[BTC Hourly] ML Hourly → {len(ml_signals)} signal(s)"
                        )
                    # 2026-06-03 review: ML BTC Hourly disabled (net-negative).
                    # analyze() still runs above to keep model state warm.
                    if BTC_HOURLY_TRADING_ENABLED:
                        self._process_signals(
                            ml_signals,
                            strategy_name="ML BTC Hourly",
                            risk_manager=risk_manager,
                            dashboard=dashboard,
                        )
            else:
                if self.ticks % 10 == 0:
                    logger.warning(
                        "[BTC Hourly] Ghost Ticker: No active BTC Hourly ladder found."
                    )
        except Exception as e:
            logger.error(f"[BTC Hourly] Market Fetch Fail: {e}")

        return []

    def get_symbols(self) -> List[str]:
        return ["KXBTCHOURLY"]
