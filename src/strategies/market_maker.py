"""Avellaneda-Stoikov market-making strategy adapted for Kalshi.

Quotes bid and ask around a reservation price that accounts for
inventory risk and time-to-expiry.  Targets bracket markets
(weather, rates) with wider spreads.

Sprint 3, Task 3.6 of Money Printer V2.

Reference:
  r(t) = s(t) - q * gamma * sigma^2 * (T - t)
  spread = gamma * sigma^2 * (T - t) + 2/gamma * ln(1 + gamma/kappa)
"""

import logging
import math
from datetime import datetime
from typing import Dict, List

from src.core.interfaces import MarketData, Strategy, TradeSignal

logger = logging.getLogger(__name__)


class MarketMakerStrategy(Strategy):
    """Avellaneda-Stoikov market maker for Kalshi bracket markets.

    Parameters
    ----------
    gamma : float
        Risk aversion parameter.  Higher → tighter quotes, less risk.
    sigma : float
        Default volatility estimate (overridden by realised vol when available).
    max_per_market : int
        Maximum open contracts per individual market.
    max_global : int
        Maximum total open contracts across all markets.
    min_spread : float
        Minimum bid-ask spread to quote (below this the market is too tight).
    """

    def __init__(
        self,
        gamma: float = 0.20,
        sigma: float = 0.15,
        max_per_market: int = 3,
        max_global: int = 20,
        min_spread: float = 0.04,
    ):
        self.gamma = gamma
        self.sigma = sigma
        self.max_per_market = max_per_market
        self.max_global = max_global
        self.min_spread = min_spread

        # Track inventory per market: positive = long YES, negative = short YES
        self._inventory: Dict[str, int] = {}
        self._global_count: int = 0

    def name(self) -> str:
        return f"Market Maker (AS, gamma={self.gamma})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reservation_price(
        self,
        mid: float,
        inventory: int,
        sigma: float,
        time_left: float,
    ) -> float:
        """Avellaneda-Stoikov reservation price."""
        return mid - inventory * self.gamma * (sigma**2) * time_left

    def _optimal_spread(self, sigma: float, time_left: float) -> float:
        """Theoretical optimal spread adapted for binary prediction markets.

        Uses a high kappa (order-arrival intensity) since Kalshi markets
        have relatively frequent order flow, keeping the adverse-selection
        component small relative to the 0-1 contract range.
        """
        kappa = 50.0  # High for liquid prediction markets
        spread = self.gamma * (sigma**2) * time_left
        if self.gamma > 0 and kappa > 0:
            spread += (2.0 / self.gamma) * math.log(1.0 + self.gamma / kappa)
        # Cap spread for binary contracts (0-1 range)
        return max(self.min_spread, min(0.30, spread))

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}

        # Need two-sided market with a visible spread
        if market_data.bid <= 0 or market_data.ask <= 0:
            return signals
        if market_data.ask >= 1.0 or market_data.bid >= 1.0:
            return signals

        spread = market_data.ask - market_data.bid
        if spread < self.min_spread:
            return signals  # Too tight to make markets

        # Global inventory cap
        if self._global_count >= self.max_global:
            return signals

        # Per-market cap
        sym = market_data.symbol
        inv = self._inventory.get(sym, 0)
        if abs(inv) >= self.max_per_market:
            return signals

        # Time to expiry
        close_time = extra.get("close_time")
        if close_time and isinstance(close_time, datetime):
            tte_s = max(1.0, (close_time - datetime.now()).total_seconds())
        else:
            tte_s = 3600.0  # Default 1 hour

        # Normalise time to [0, 1] (1 = full contract life)
        time_left = min(1.0, tte_s / 3600.0)

        # Use provided sigma or default
        sigma = extra.get("realised_vol", self.sigma)

        mid = (market_data.bid + market_data.ask) / 2.0
        r_price = self._reservation_price(mid, inv, sigma, time_left)
        half_spread = self._optimal_spread(sigma, time_left) / 2.0

        our_bid = max(0.01, r_price - half_spread)
        our_ask = min(0.99, r_price + half_spread)

        # Only quote if we improve on (or match) the market
        # Buy (YES) if our bid > market bid — we can penny the book
        if our_bid >= market_data.bid and inv < self.max_per_market:
            lp = round(max(0.01, min(0.99, our_bid)), 2)
            sig = TradeSignal(
                symbol=sym,
                side="buy",
                quantity=1,
                limit_price=lp,
                confidence=0.55,
                contract_side="YES",
            )
            sig.stop_loss = max(0.01, lp - 0.10)
            if close_time:
                sig.expiration_time = close_time
            logger.info(
                "[MM] QUOTE BID %s @ %.2f (mid=%.2f r=%.3f inv=%d)",
                sym,
                lp,
                mid,
                r_price,
                inv,
            )
            signals.append(sig)

        # Sell (buy NO) if our ask < market ask — penny the other side
        if our_ask <= market_data.ask and inv > -self.max_per_market:
            no_cost = round(max(0.01, min(0.99, 1.0 - our_ask)), 2)
            sig = TradeSignal(
                symbol=sym,
                side="buy",
                quantity=1,
                limit_price=no_cost,
                confidence=0.55,
                contract_side="NO",
            )
            sig.stop_loss = max(0.01, no_cost - 0.10)
            if close_time:
                sig.expiration_time = close_time
            logger.info(
                "[MM] QUOTE ASK %s @ %.2f (no_cost=%.2f inv=%d)",
                sym,
                our_ask,
                no_cost,
                inv,
            )
            signals.append(sig)

        return signals

    # ------------------------------------------------------------------
    # Inventory callbacks (called by bot/orchestrator after fills)
    # ------------------------------------------------------------------

    def on_fill(self, symbol: str, side: str, qty: int) -> None:
        """Update inventory tracking after a fill."""
        delta = qty if side == "buy" else -qty
        self._inventory[symbol] = self._inventory.get(symbol, 0) + delta
        self._global_count += qty

    def on_close(self, symbol: str, qty: int) -> None:
        """Position closed — reduce inventory."""
        self._inventory.pop(symbol, None)
        self._global_count = max(0, self._global_count - qty)
