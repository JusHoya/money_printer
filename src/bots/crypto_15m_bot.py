"""Generic 15-minute crypto contract bot for non-BTC assets.

BTC is handled by the dedicated BTC15mBot which also runs the trained
ML strategy. This module expands coverage to ETH, SOL, DOGE, XRP by
running the asset-agnostic arb strategies against their KX{asset}15M
Kalshi series. ML strategies per-asset will follow once per-asset
models are trained (deferred to PR #3).
"""

from datetime import datetime
from typing import List

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.cross_spread_arb import CrossSpreadArbStrategy
from src.strategies.latency_arb import LatencyArbStrategy
from src.data.coinbase_provider import CoinbaseProvider
from src.utils.logger import logger


# Discovered 2026-04-22: Kalshi lists 15-minute contracts for these assets.
# BNB and HYPE are also listed on Kalshi but not on Coinbase; defer.
SUPPORTED_ASSETS = ("ETH", "SOL", "DOGE", "XRP")


class Crypto15mBot(Bot, TickerResolverMixin, SignalProcessorMixin):
    """Runs arb-only strategies against KX{asset}15M contracts."""

    def __init__(self, asset: str):
        self.asset = asset.upper()
        self.series_base = f"KX{self.asset}15M"
        Bot.__init__(self, name=f"{self.asset} 15m")
        TickerResolverMixin.__init__(self)
        self.ticks = 0
        self.kalshi = None
        self.coinbase = None

        self.strategies = {
            "cross_arb": CrossSpreadArbStrategy(),
            "latency_arb": LatencyArbStrategy(),
        }

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        # Coinbase spot for this specific asset
        self.coinbase = coinbase or CoinbaseProvider(f"{self.asset}-USD")
        if not coinbase:
            self.coinbase.connect()

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        self.ticks += 1

        spot_data = self.coinbase.fetch_latest()
        if not spot_data:
            if self.ticks % 10 == 0:
                dashboard.log(f"[{self.asset} 15m] ⚠️ Coinbase Fetch Failed")
            return []

        dashboard.update_price(f"{self.asset}-USD (Coinbase)", spot_data.price)

        if not self.kalshi:
            return []

        resolved = False
        try:
            ticker = self._resolve_smart_ticker(
                self.series_base,
                criteria="time",
                kalshi=self.kalshi,
                coinbase=self.coinbase,
            )
            if ticker:
                k_data = self.kalshi.fetch_latest(ticker)
                if k_data:
                    best_price = (
                        k_data.bid
                        if k_data.bid > 0
                        else (k_data.ask if k_data.ask > 0 else k_data.price)
                    )
                    dashboard.update_price(
                        f"{ticker} (15m)",
                        best_price,
                        bid=k_data.bid,
                        ask=k_data.ask,
                        no_bid=k_data.extra.get("no_bid", 0.0) if k_data.extra else 0.0,
                        no_ask=k_data.extra.get("no_ask", 0.0) if k_data.extra else 0.0,
                        volume=k_data.volume,
                    )
                    original_spot = spot_data.price
                    spot_data.bid = k_data.bid
                    spot_data.ask = k_data.ask
                    spot_data.symbol = ticker
                    if spot_data.extra is None:
                        spot_data.extra = {}
                    spot_data.extra["spot_price"] = original_spot
                    if k_data.extra:
                        spot_data.extra["strike"] = k_data.extra.get("strike")
                        spot_data.extra["close_time"] = k_data.extra.get("close_time")
                        spot_data.extra["no_bid"] = k_data.extra.get("no_bid", 0)
                        spot_data.extra["no_ask"] = k_data.extra.get("no_ask", 0)
                    risk_manager.update_market_data(ticker, spot_data.price)
                    resolved = True
            else:
                if self.ticks % 60 == 0:
                    logger.warning(
                        f"[{self.asset} 15m] Ghost Ticker: No active {self.series_base} markets."
                    )
        except Exception as e:
            logger.error(f"[{self.asset} 15m] Market Fetch Fail: {e}")

        if not resolved:
            return []

        # Same entry timing windows as BTC 15m
        now = datetime.now()
        minute_in_interval = now.minute % 15
        if minute_in_interval < 1 or 6 <= minute_in_interval <= 8:
            return []

        if self.ticks % 20 == 0:
            logger.info(
                f"[{self.asset} 15m] Evaluating {spot_data.symbol} | "
                f"spot={spot_data.extra.get('spot_price', '?')}, "
                f"bid={spot_data.bid}, ask={spot_data.ask}, "
                f"strike={spot_data.extra.get('strike', '?')}"
            )

        for strat_key, strat_name in [
            ("cross_arb", f"{self.asset} Cross-Spread Arb"),
            ("latency_arb", f"{self.asset} Latency Arb"),
        ]:
            signals = self.strategies[strat_key].analyze(spot_data)
            if signals and self.ticks % 10 == 0:
                logger.info(
                    f"[{self.asset} 15m] {strat_name} → {len(signals)} signal(s)"
                )
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
        return [self.series_base]


def _register_per_asset_bots():
    """Create and register one BotRegistry entry per asset."""
    for asset in SUPPORTED_ASSETS:
        name = f"{asset.lower()}_15m"

        def _make_cls(_asset):
            class _AssetBot(Crypto15mBot):
                def __init__(self):
                    super().__init__(asset=_asset)

            _AssetBot.__name__ = f"{_asset}15mBot"
            _AssetBot.__qualname__ = f"{_asset}15mBot"
            return _AssetBot

        cls = _make_cls(asset)
        BotRegistry._bots[name] = cls


_register_per_asset_bots()
