"""Enhanced longshot fader with ML probability calibration.

Uses the probability calibrator to determine the *true* probability
of cheap contracts, then sells (buys NO) when the market over-prices
longshots.  Exploits the well-documented favourite-longshot bias on
Kalshi.

Sprint 3, Task 3.5 of Money Printer V2.
"""

import logging
from datetime import datetime
from typing import Dict, List

from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.ml.predictor import ModelPredictor

logger = logging.getLogger(__name__)


class LongshotFaderV2(Strategy):
    """ML-enhanced longshot fader.

    If a contract trades at *X* cents but the calibrated model says
    true probability is below ``X - min_edge``, sell the contract
    (buy NO) to capture the optimism tax.
    """

    def __init__(
        self,
        longshot_ceiling: float = 0.10,
        min_price: float = 0.03,
        min_edge: float = 0.03,
        predictor: ModelPredictor = None,
        cooldown_seconds: int = 600,
    ):
        self.longshot_ceiling = longshot_ceiling
        self.min_price = min_price
        self.min_edge = min_edge
        self.predictor = predictor or ModelPredictor()
        self.cooldown_seconds = cooldown_seconds
        self._last_trade: Dict[str, datetime] = {}

    def name(self) -> str:
        return f"LongShot Fader V2 (ML, <${self.longshot_ceiling:.2f})"

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        now = datetime.now()

        bid = market_data.bid
        if bid <= 0:
            return signals

        # Only fade cheap longshots
        if not (self.min_price <= bid <= self.longshot_ceiling):
            return signals

        # Per-symbol cooldown
        last = self._last_trade.get(market_data.symbol, datetime.min)
        if (now - last).total_seconds() < self.cooldown_seconds:
            return signals

        # --- ML calibration to get true probability ---
        extra = market_data.extra or {}
        try:
            pred = self.predictor.predict(
                market_data,
                time_to_expiry=extra.get("time_to_expiry"),
                market_type="btc" if "BTC" in market_data.symbol else "weather",
            )
            calibrated_prob = pred.get("probability", bid)
        except Exception:
            # Fallback: use implied win rate
            calibrated_prob = bid

        # Edge = market_bid (implied YES prob) - calibrated YES prob
        edge = bid - calibrated_prob
        if edge < self.min_edge:
            return signals

        # Buy NO (equivalent to selling YES)
        no_cost = 1.0 - bid
        # Implied win rate of the NO side
        no_win_rate = 1.0 - calibrated_prob
        confidence = min(0.95, no_win_rate * 0.95)

        sig = TradeSignal(
            symbol=market_data.symbol,
            side="buy",
            quantity=5,
            limit_price=no_cost,
            confidence=confidence,
            contract_side="NO",
        )
        # Stop loss: scale to entry, cap at 0.15
        sig.stop_loss = min(0.15, bid + 0.05)
        sig.expiration_time = extra.get("close_time")

        logger.info(
            "[FaderV2] BUY NO %s | bid=%.3f calibrated=%.3f edge=%.3f",
            market_data.symbol,
            bid,
            calibrated_prob,
            edge,
        )
        self._last_trade[market_data.symbol] = now
        signals.append(sig)
        return signals
