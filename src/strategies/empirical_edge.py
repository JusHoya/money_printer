"""Empirical edge strategy for BTC 15-minute markets.

Uses historical settlement data to build a conditional probability
table: at each price level, what is the actual probability of YES
settling vs what the market implies?  Trades where the empirical
edge exceeds a configurable threshold.

Sprint 5+ — data-driven microstructure exploitation.

Key finding from 8,974 settled BTC 15m contracts:
- YES is overpriced below 50c (optimism tax)
- YES is underpriced above 70c
- The 20-50c range has 14-18% edge for NO
- The 70-90c range has 10-18% edge for YES
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.core.interfaces import MarketData, Strategy, TradeSignal

logger = logging.getLogger(__name__)

# Empirical settlement rates from 8,974 BTC 15m contracts.
# Format: (price_lo, price_hi) -> actual_yes_win_rate
# Built from data/historical/btc_15m/ settlement analysis.
EMPIRICAL_YES_RATE: Dict[Tuple[float, float], float] = {
    (0.00, 0.02): 0.019,
    (0.02, 0.05): 0.008,
    (0.05, 0.10): 0.021,
    (0.10, 0.15): 0.071,
    (0.15, 0.20): 0.106,
    (0.20, 0.30): 0.107,
    (0.30, 0.40): 0.175,
    (0.40, 0.50): 0.311,
    (0.50, 0.60): 0.481,
    (0.60, 0.70): 0.725,
    (0.70, 0.80): 0.909,
    (0.80, 0.85): 1.000,
    (0.85, 0.90): 0.967,
    (0.90, 0.95): 0.968,
    (0.95, 0.98): 0.971,
    (0.98, 1.01): 0.998,
}


def lookup_empirical_rate(price: float) -> Optional[float]:
    """Look up the empirical YES win rate for a given price level."""
    for (lo, hi), rate in EMPIRICAL_YES_RATE.items():
        if lo <= price < hi:
            return rate
    return None


def compute_edge(price: float) -> Tuple[Optional[str], float]:
    """Compute the empirical edge at a price level.

    Returns (side, edge_magnitude) where:
    - side = "YES" if actual > implied (underpriced YES)
    - side = "NO" if actual < implied (overpriced YES / underpriced NO)
    - edge_magnitude = absolute difference in probability
    """
    actual = lookup_empirical_rate(price)
    if actual is None:
        return None, 0.0

    implied = price  # Market price IS the implied probability
    edge = actual - implied

    if edge > 0:
        return "YES", edge
    elif edge < 0:
        return "NO", abs(edge)
    return None, 0.0


def compute_ev_per_dollar(price: float, side: str = "YES") -> float:
    """Expected value per dollar wagered using empirical settlement rates.

    For a $1 wager:
    - Multiplier = 1/price (total payout if correct)
    - EV = P(win) * multiplier - 1.0

    Returns the expected profit per $1 wagered (positive = profitable).
    """
    if price <= 0 or price >= 1.0:
        return -1.0

    if side == "YES":
        actual_prob = lookup_empirical_rate(price) or price
        multiplier = 1.0 / price  # e.g., at $0.30 → 3.33x
        return actual_prob * multiplier - 1.0
    else:
        # NO side: cost = 1-price, win when YES doesn't settle
        no_cost = 1.0 - price
        actual_yes = lookup_empirical_rate(price) or price
        actual_no = 1.0 - actual_yes
        no_multiplier = 1.0 / no_cost  # e.g., at 70c NO cost → 3.33x
        return actual_no * no_multiplier - 1.0


class EmpiricalEdgeStrategy(Strategy):
    """Trade BTC 15m contracts based on empirical settlement patterns.

    Instead of predicting BTC direction, this strategy identifies
    price levels where the market consistently misprices contracts
    and trades the statistical edge.

    Parameters
    ----------
    min_ev_per_dollar : float
        Minimum expected value per $1 wagered (default 0.10 = 10%
        expected return).  A $100 bet with 0.10 EV expects $110 back.
    min_edge : float
        Minimum probability edge (default 0.05 = 5%).  Both min_ev
        AND min_edge must be met for a trade.
    min_minutes_remaining : int
        Only trade contracts with at least this many minutes left
        (default 3). Avoids final-minute volatility.
    max_minutes_remaining : int
        Only trade contracts with at most this many minutes left
        (default 12). Later entries have more informational advantage.
    cooldown_seconds : int
        Per-symbol cooldown between trades.
    """

    def __init__(
        self,
        min_ev_per_dollar: float = 0.10,
        min_edge: float = 0.05,
        min_minutes_remaining: int = 3,
        max_minutes_remaining: int = 12,
        cooldown_seconds: int = 120,
    ):
        self.min_ev_per_dollar = min_ev_per_dollar
        self.min_edge = min_edge
        self.min_minutes = min_minutes_remaining
        self.max_minutes = max_minutes_remaining
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until: Dict[str, datetime] = {}

    def name(self) -> str:
        return f"Empirical Edge (>{self.min_edge:.0%}, {self.min_minutes}-{self.max_minutes}min)"

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        now = market_data.timestamp or datetime.now()

        # Need valid two-sided market
        if market_data.bid <= 0 or market_data.ask <= 0:
            return signals
        if market_data.ask >= 1.0 or market_data.bid <= 0.005:
            return signals

        # Per-symbol cooldown
        sym = market_data.symbol
        if now < self._cooldown_until.get(sym, datetime.min):
            return signals

        # Time remaining check
        close_time = extra.get("close_time")
        if close_time:
            if isinstance(close_time, str):
                try:
                    close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    close_dt = close_dt.replace(tzinfo=None)
                except Exception:
                    close_dt = None
            else:
                close_dt = close_time
                if hasattr(close_dt, "tzinfo") and close_dt.tzinfo is not None:
                    close_dt = close_dt.replace(tzinfo=None)

            if close_dt:
                check_now = now.replace(tzinfo=None) if now.tzinfo else now
                minutes_left = (close_dt - check_now).total_seconds() / 60
                if minutes_left < self.min_minutes or minutes_left > self.max_minutes:
                    return signals
        else:
            # Use cycle position as fallback
            minutes_in_cycle = now.minute % 15
            minutes_left = 15 - minutes_in_cycle
            if minutes_left < self.min_minutes or minutes_left > self.max_minutes:
                return signals

        # Get strike for position tracking
        strike = extra.get("strike")

        # Compute empirical edge AND EV per dollar for both sides
        yes_side, yes_edge = compute_edge(market_data.ask)
        yes_ev = compute_ev_per_dollar(market_data.ask, side="YES")
        yes_mult = 1.0 / market_data.ask if market_data.ask > 0 else 0

        no_cost = 1.0 - market_data.bid
        no_side, no_edge = compute_edge(market_data.bid)
        no_ev = compute_ev_per_dollar(market_data.bid, side="NO")
        no_mult = 1.0 / no_cost if no_cost > 0 else 0

        # Both min_edge AND min_ev_per_dollar must be met
        yes_ok = (
            yes_side == "YES"
            and yes_edge >= self.min_edge
            and yes_ev >= self.min_ev_per_dollar
        )
        no_ok = (
            no_side == "NO"
            and no_edge >= self.min_edge
            and no_ev >= self.min_ev_per_dollar
        )

        # Pick the side with higher EV per dollar
        if yes_ok and no_ok:
            best_side = "YES" if yes_ev >= no_ev else "NO"
        elif yes_ok:
            best_side = "YES"
        elif no_ok:
            best_side = "NO"
        else:
            return signals

        if best_side == "YES":
            lp = market_data.ask
            confidence = min(0.95, lookup_empirical_rate(lp) or 0.5)
            sig = TradeSignal(
                symbol=sym,
                side="buy",
                quantity=10,
                limit_price=lp,
                confidence=confidence,
                contract_side="YES",
            )
            sig.stop_loss = max(0.01, lp - 0.05)  # Tighten from 10c to 5c
            sig.strike = strike
            if close_time:
                sig.expiration_time = close_time
            logger.info(
                "[EmpEdge] BUY YES %s @ $%.2f | mult=%.2fx | "
                "empirical=%.1f%% vs market=%.1f%% | EV=$%.2f per $1",
                sym,
                lp,
                yes_mult,
                (lookup_empirical_rate(lp) or 0) * 100,
                lp * 100,
                yes_ev,
            )
            signals.append(sig)
            self._cooldown_until[sym] = now + timedelta(seconds=self.cooldown_seconds)

        elif best_side == "NO":
            lp = no_cost
            actual_yes = lookup_empirical_rate(market_data.bid) or 0.5
            confidence = min(0.95, 1.0 - actual_yes)
            sig = TradeSignal(
                symbol=sym,
                side="buy",
                quantity=10,
                limit_price=max(0.01, lp),
                confidence=confidence,
                contract_side="NO",
            )
            sig.stop_loss = max(0.01, lp - 0.05)  # Tighten from 10c to 5c
            sig.strike = strike
            if close_time:
                sig.expiration_time = close_time
            logger.info(
                "[EmpEdge] BUY NO %s | YES@$%.2f NO=$%.2f mult=%.2fx | "
                "empirical_yes=%.1f%% vs market=%.1f%% | EV=$%.2f per $1",
                sym,
                market_data.bid,
                lp,
                no_mult,
                actual_yes * 100,
                market_data.bid * 100,
                no_ev,
            )
            signals.append(sig)
            self._cooldown_until[sym] = now + timedelta(seconds=self.cooldown_seconds)

        return signals
