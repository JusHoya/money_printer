"""Time-decay scalper for BTC 15-minute contracts.

Enters in the final 2-5 minutes of a contract when the outcome
is already nearly certain — BTC is clearly above or below the
strike — and buys contracts priced at 90-93c that should settle
at 99-100c.  Small edge per trade but very high win rate.

Sprint 3, Task 3.7 of Money Printer V2.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional

from src.core.interfaces import MarketData, Strategy, TradeSignal

logger = logging.getLogger(__name__)


class TimeDecayScalper(Strategy):
    """Late-cycle theta scalper.

    Parameters
    ----------
    min_probability : float
        Minimum estimated probability for the trade to qualify
        (default 0.90).
    max_entry_price : float
        Maximum price to pay (default 0.93) — above this the risk/reward
        is unfavourable.
    spot_distance_min : float
        Minimum $ distance between spot and strike to consider the
        outcome "nearly certain" (default 150).
    entry_window_min : tuple[int, int]
        Minutes within the 15-min cycle to look for entries (default 10-13).
    """

    def __init__(
        self,
        min_probability: float = 0.90,
        max_entry_price: float = 0.93,
        spot_distance_min: float = 150.0,
        entry_window_min: tuple = (10, 13),
        cooldown_seconds: int = 60,
    ):
        self.min_probability = min_probability
        self.max_entry_price = max_entry_price
        self.spot_distance_min = spot_distance_min
        self.entry_lo = entry_window_min[0]
        self.entry_hi = entry_window_min[1]
        self.cooldown_seconds = cooldown_seconds

        self._cooldown_until = datetime.min
        self._last_cycle_key: Optional[str] = None
        self._traded_this_cycle = False

    def name(self) -> str:
        return f"Time Decay Scalper (>{self.min_probability:.0%}, <${self.max_entry_price})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_strike(symbol: str) -> Optional[float]:
        try:
            parts = symbol.split("-")
            return float(re.sub(r"[A-Za-z]", "", parts[-1]))
        except Exception:
            return None

    def _cycle_key(self, symbol: str) -> str:
        parts = symbol.split("-")
        return "-".join(parts[:2]) if len(parts) >= 2 else symbol

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        now = market_data.timestamp or datetime.now()

        # Detect new cycle
        ck = self._cycle_key(market_data.symbol)
        if ck != self._last_cycle_key:
            self._last_cycle_key = ck
            self._traded_this_cycle = False

        if self._traded_this_cycle:
            return signals

        # Cooldown
        if now < self._cooldown_until:
            return signals

        # Window gate: only trade in minutes entry_lo..entry_hi of the 15-min cycle
        minute_in_cycle = now.minute % 15
        if minute_in_cycle < self.entry_lo or minute_in_cycle > self.entry_hi:
            return signals

        # Need two-sided market
        if market_data.bid <= 0 or market_data.ask <= 0:
            return signals

        strike = self._extract_strike(market_data.symbol)
        if strike is None:
            return signals

        spot = extra.get("spot_price", market_data.price)
        if spot <= 1.0:
            return signals  # No valid spot price

        close_time = extra.get("close_time")
        distance = spot - strike  # Positive = spot above strike

        # --- YES side: spot clearly above strike → buy YES cheap ---
        if distance >= self.spot_distance_min:
            # YES should settle at ~1.00; look for ask ≤ max_entry_price
            if (
                market_data.ask <= self.max_entry_price
                and market_data.ask >= self.min_probability
            ):
                edge = 1.0 - market_data.ask  # Expected profit per contract
                lp = market_data.ask
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=10,
                    limit_price=lp,
                    confidence=min(0.95, self.min_probability + edge / 2),
                    contract_side="YES",
                )
                sig.stop_loss = max(0.01, lp - 0.05)
                sig.disable_profit_targets = True  # Hold to expiry
                if close_time:
                    sig.expiration_time = close_time
                logger.info(
                    "[TimeDecay] BUY YES %s @ %.2f | spot=$%.0f strike=$%.0f dist=$%.0f",
                    market_data.symbol,
                    lp,
                    spot,
                    strike,
                    distance,
                )
                signals.append(sig)
                self._traded_this_cycle = True
                self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)

        # --- NO side: spot clearly below strike → buy NO cheap ---
        elif distance <= -self.spot_distance_min:
            no_cost = 1.0 - market_data.bid
            if no_cost <= self.max_entry_price and no_cost >= self.min_probability:
                edge = 1.0 - no_cost
                lp = max(0.01, no_cost)
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=10,
                    limit_price=lp,
                    confidence=min(0.95, self.min_probability + edge / 2),
                    contract_side="NO",
                )
                sig.stop_loss = max(0.01, lp - 0.05)
                sig.disable_profit_targets = True
                if close_time:
                    sig.expiration_time = close_time
                logger.info(
                    "[TimeDecay] BUY NO %s @ %.2f | spot=$%.0f strike=$%.0f dist=$%.0f",
                    market_data.symbol,
                    lp,
                    spot,
                    strike,
                    abs(distance),
                )
                signals.append(sig)
                self._traded_this_cycle = True
                self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)

        return signals
