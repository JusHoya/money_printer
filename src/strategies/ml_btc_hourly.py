"""ML-driven BTC hourly strategy.

Same ML backbone as the 15-minute strategy but tuned for the longer
hourly time horizon.  Wider features (MACD, Bollinger, support/resistance)
become more useful with more data.

Sprint 3, Task 3.2 of Money Printer V2.
"""

import logging
import re
from collections import deque
from datetime import datetime, timedelta
from typing import List, Optional

from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.ml.predictor import ModelPredictor

logger = logging.getLogger(__name__)


class MLBtcHourlyStrategy(Strategy):
    """ML-driven BTC hourly contract strategy.

    Collects spot-price history for richer feature computation and
    only trades during favourable time windows within the hour.
    """

    # Time windows (minute-of-hour) when we consider trading
    TRADE_WINDOWS = [(0, 20), (25, 40), (45, 60)]

    def __init__(
        self,
        min_edge: float = 0.05,
        predictor: ModelPredictor = None,
        cooldown_seconds: int = 300,
    ):
        self.min_edge = min_edge
        self.predictor = predictor or ModelPredictor()
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until = datetime.min

        # Spot price history for richer features
        self._spot_history: deque = deque(maxlen=120)

    def name(self) -> str:
        return f"ML BTC Hourly (edge>{self.min_edge})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _in_trade_window(self, now: datetime) -> bool:
        m = now.minute
        return any(lo <= m < hi for lo, hi in self.TRADE_WINDOWS)

    @staticmethod
    def _extract_strike(symbol: str) -> Optional[float]:
        try:
            parts = symbol.split("-")
            return float(re.sub(r"[A-Za-z]", "", parts[-1]))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        now = market_data.timestamp or datetime.now()

        # Accumulate spot-price history (Coinbase ticks only)
        source = extra.get("source", "")
        if "coinbase" in source.lower():
            self._spot_history.append((now, market_data.price))
            return signals  # Don't trade on spot ticks

        # Only trade Kalshi hourly tickers
        sym = market_data.symbol
        if ("KXBTC" not in sym and "kxbtcd" not in sym) or "15M" in sym:
            return signals

        # Cooldown
        if now < self._cooldown_until:
            return signals

        # Time window gate
        if not self._in_trade_window(now):
            return signals

        # Need spot history
        if len(self._spot_history) < 5:
            return signals

        # Need two-sided market
        if market_data.bid <= 0 or market_data.ask <= 0:
            return signals
        if market_data.ask >= 1.0:
            return signals

        strike = self._extract_strike(sym)
        if strike is None:
            return signals

        current_spot = self._spot_history[-1][1]

        # Only trade within $750 of spot
        if abs(strike - current_spot) > 750:
            return signals

        # TTE for hourly contracts (seconds until top-of-next-hour)
        close_time = extra.get("close_time")
        if close_time and isinstance(close_time, datetime):
            tte_s = max(0.0, (close_time - now).total_seconds())
        else:
            top_of_hour = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            tte_s = max(60.0, (top_of_hour - now).total_seconds())

        if tte_s < 60:
            return signals

        # ML prediction
        md_for_pred = MarketData(
            symbol=sym,
            timestamp=now,
            price=current_spot,
            volume=market_data.volume,
            bid=market_data.bid,
            ask=market_data.ask,
            extra={
                **extra,
                "strike": strike,
                "time_to_expiry": tte_s,
                "spot_price": current_spot,
            },
        )

        try:
            pred = self.predictor.predict_btc(md_for_pred, strike, tte_s)
        except Exception as exc:
            logger.debug("[ML Hourly] Prediction failed: %s", exc)
            return signals

        prob = pred["probability"]
        confidence = pred["confidence"]
        fair = pred.get("fair_value", prob)
        rec = pred.get("recommended_price", fair)

        # YES direction
        if prob > 0.5:
            edge = fair - market_data.ask
            if edge >= self.min_edge:
                lp = max(0.01, min(0.99, min(rec, market_data.ask)))
                sig = TradeSignal(
                    symbol=sym,
                    side="buy",
                    quantity=5,
                    limit_price=lp,
                    confidence=confidence,
                    contract_side="YES",
                )
                sig.stop_loss = max(0.01, lp - 0.10)
                if close_time:
                    sig.expiration_time = close_time
                logger.info(
                    "[ML Hourly] BUY YES %s | prob=%.3f edge=%.3f",
                    sym,
                    prob,
                    edge,
                )
                signals.append(sig)
                self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)

        # NO direction
        else:
            no_fair = 1.0 - fair
            no_cost = 1.0 - market_data.bid
            edge = no_fair - no_cost
            if edge >= self.min_edge and no_cost > 0.01:
                lp = max(0.01, min(0.99, no_cost))
                sig = TradeSignal(
                    symbol=sym,
                    side="buy",
                    quantity=5,
                    limit_price=lp,
                    confidence=confidence,
                    contract_side="NO",
                )
                sig.stop_loss = max(0.01, lp - 0.10)
                if close_time:
                    sig.expiration_time = close_time
                logger.info(
                    "[ML Hourly] BUY NO %s | prob=%.3f edge=%.3f",
                    sym,
                    prob,
                    edge,
                )
                signals.append(sig)
                self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)

        return signals
