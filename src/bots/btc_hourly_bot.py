import copy
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.crypto_strategy import CryptoHourlyStrategyV3
from src.data.coinbase_provider import CoinbaseProvider
from src.utils.logger import logger


@BotRegistry.register("btc_hourly")
class BTCHourlyBot(Bot, TickerResolverMixin, SignalProcessorMixin):

    def __init__(self):
        Bot.__init__(self, name="BTC Hourly")
        TickerResolverMixin.__init__(self)
        self.ticks = 0
        self.kalshi = None
        self.coinbase = None

        self.strategies = {
            "crypto_hr": CryptoHourlyStrategyV3(),
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

        # Feed raw spot price to hourly strategy for price history
        spot_feed = copy.deepcopy(btc_data)
        spot_feed.symbol = "BTC-USD (Coinbase)"
        if spot_feed.extra is None:
            spot_feed.extra = {}
        spot_feed.extra['source'] = 'live_coinbase'
        self.strategies['crypto_hr'].analyze(spot_feed)

        if not self.kalshi:
            return []

        try:
            ladder = self._resolve_btc_ladder(kalshi=self.kalshi, coinbase=self.coinbase)

            if ladder:
                for ticker in ladder:
                    k_data_ladder = self.kalshi.fetch_latest(ticker)
                    if k_data_ladder:
                        dashboard.update_price(f"{ticker} (1h)", k_data_ladder.bid)

                # Use center ticker for strategy
                center_ticker = ladder[0]
                k_data_center = self.kalshi.fetch_latest(center_ticker)

                if k_data_center:
                    btc_data_hr = copy.deepcopy(btc_data)
                    btc_data_hr.bid = k_data_center.bid
                    btc_data_hr.ask = k_data_center.ask
                    btc_data_hr.symbol = center_ticker
                    if k_data_center.extra:
                        if btc_data_hr.extra is None:
                            btc_data_hr.extra = {}
                        btc_data_hr.extra['no_bid'] = k_data_center.extra.get('no_bid', 0.0)
                        btc_data_hr.extra['no_ask'] = k_data_center.extra.get('no_ask', 0.0)
                        btc_data_hr.extra['close_time'] = k_data_center.extra.get('close_time')

                    self._process_signals(
                        self.strategies['crypto_hr'].analyze(btc_data_hr),
                        strategy_name="Crypto Hourly", risk_manager=risk_manager, dashboard=dashboard
                    )
            else:
                if self.ticks % 10 == 0:
                    logger.warning("[BTC Hourly] Ghost Ticker: No active BTC Hourly ladder found.")
        except Exception as e:
            logger.error(f"[BTC Hourly] Market Fetch Fail: {e}")

        return []

    def get_symbols(self) -> List[str]:
        return ["KXBTCHOURLY"]
