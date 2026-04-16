"""ML-driven BTC 15-minute strategy.

Replaces V3 rule-based strategy with ensemble model prediction
+ time-to-expiry optimizer.  Generates signals when the model's
fair value diverges from the market price by more than *min_edge*.

Sprint 3, Task 3.1 of Money Printer V2.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import List

from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.ml.predictor import ModelPredictor

logger = logging.getLogger(__name__)


class MLBtc15mStrategy(Strategy):
    """ML-driven BTC 15-minute contract strategy.

    Uses the ensemble model (XGBoost + LSTM) to estimate the true
    probability that BTC will be above the contract strike at expiry.
    Trades when ``model_prob - market_price > min_edge``.
    """

    def __init__(
        self,
        min_edge: float = 0.05,
        predictor: ModelPredictor = None,
        cooldown_seconds: int = 120,
    ):
        self.min_edge = min_edge
        self.predictor = predictor or ModelPredictor()
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until = datetime.min

    def name(self) -> str:
        return f"ML BTC 15m (edge>{self.min_edge})"

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        now = market_data.timestamp or datetime.now()

        # Cooldown gate
        if now < self._cooldown_until:
            return signals

        # Need valid two-sided market
        if market_data.bid <= 0 or market_data.ask <= 0:
            return signals
        if market_data.ask >= 1.0:
            return signals

        # Extract strike: prefer extra["strike"] (from floor_strike API field),
        # fall back to ticker parsing
        strike_val = extra.get("strike")
        if not strike_val or strike_val < 1000:
            try:
                parts = market_data.symbol.split("-")
                parsed = float(re.sub(r"[A-Za-z]", "", parts[-1]))
                if parsed > 1000:
                    strike_val = parsed
            except Exception:
                pass
        if not strike_val or strike_val < 1000:
            return signals

        # Time-to-expiry
        close_time = extra.get("close_time")
        if close_time and isinstance(close_time, datetime):
            tte_s = max(0.0, (close_time - now).total_seconds())
        else:
            minutes_in = now.minute % 15
            tte_s = max(60.0, (15 - minutes_in) * 60 - now.second)

        # Don't trade with <60 s remaining (circuit-breaker alignment)
        if tte_s < 60:
            return signals

        # Build MarketData with spot price for predictor
        spot = extra.get("spot_price", market_data.price)

        # Strike proximity filter: skip near-ATM contracts (EARLY_SETTLEMENT risk).
        # Adaptive threshold: tighter during low-vol overnight (UTC 04-12) to capture more trades,
        # wider during high-vol daytime to avoid choppy settlements.
        hour_utc = now.hour if hasattr(now, 'hour') else datetime.now().hour
        prox_threshold = 0.0015 if 4 <= hour_utc <= 12 else 0.003
        if spot > 1.0 and (abs(spot - strike_val) / strike_val) < prox_threshold:
            logger.debug(
                "[ML BTC 15m] SKIP near-ATM: spot=%.2f strike=%.0f proximity=%.4f%% (threshold=%.2f%%)",
                spot,
                strike_val,
                abs(spot - strike_val) / strike_val * 100,
                prox_threshold * 100,
            )
            return signals

        md_for_pred = MarketData(
            symbol=market_data.symbol,
            timestamp=now,
            price=spot if spot > 1.0 else market_data.price,
            volume=market_data.volume,
            bid=market_data.bid,
            ask=market_data.ask,
            extra={**extra, "strike": strike_val, "time_to_expiry": tte_s},
        )

        try:
            pred = self.predictor.predict_btc(md_for_pred, strike_val, tte_s)
        except Exception as exc:
            logger.debug("[ML BTC 15m] Prediction failed: %s", exc)
            return signals

        prob = pred["probability"]
        confidence = pred["confidence"]
        fair = pred.get("fair_value", prob)
        rec_price = pred.get("recommended_price", fair)
        model_used = pred.get("model_used", "analytical")

        # --- YES direction ---
        if prob > 0.5:
            edge = fair - market_data.ask
            if edge >= self.min_edge:
                lp = min(rec_price, market_data.ask)
                lp = max(0.01, min(0.99, lp))
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=10,
                    limit_price=lp,
                    confidence=confidence,
                    contract_side="YES",
                )
                sig.stop_loss = max(0.01, lp - 0.05)  # Tighten from 8c to 5c
                sig.strike = strike_val
                if close_time:
                    sig.expiration_time = close_time
                sig.disable_profit_targets = tte_s < 300
                # ML context for trade journal
                sig.model_probability = prob
                sig.model_used = model_used
                sig.btc_spot = spot
                sig.tte_at_entry = tte_s
                logger.info(
                    "[ML BTC 15m] BUY YES %s | prob=%.3f ask=%.2f edge=%.3f strike=$%.0f",
                    market_data.symbol,
                    prob,
                    market_data.ask,
                    edge,
                    strike_val,
                )
                signals.append(sig)
                self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)

        # --- NO direction ---
        else:
            no_fair = 1.0 - fair
            no_cost = 1.0 - market_data.bid
            edge = no_fair - no_cost
            if edge >= self.min_edge and no_cost > 0.01:
                lp = max(0.01, min(0.99, no_cost))
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=10,
                    limit_price=lp,
                    confidence=confidence,
                    contract_side="NO",
                )
                sig.stop_loss = max(0.01, lp - 0.05)  # Tighten from 8c to 5c
                sig.strike = strike_val
                if close_time:
                    sig.expiration_time = close_time
                sig.disable_profit_targets = tte_s < 300
                # ML context for trade journal
                sig.model_probability = prob
                sig.model_used = model_used
                sig.btc_spot = spot
                sig.tte_at_entry = tte_s
                logger.info(
                    "[ML BTC 15m] BUY NO %s | prob=%.3f bid=%.2f edge=%.3f strike=$%.0f",
                    market_data.symbol,
                    prob,
                    market_data.bid,
                    edge,
                    strike_val,
                )
                signals.append(sig)
                self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)

        return signals
