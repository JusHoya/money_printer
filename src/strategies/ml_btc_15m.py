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
        cooldown_seconds: int = 60,
        near_atm_threshold: float = 0.0,
    ):
        self.min_edge = min_edge
        self.predictor = predictor or ModelPredictor()
        self.cooldown_seconds = cooldown_seconds
        # Fractional distance (|spot-strike|/strike) below which a contract is
        # treated as near-ATM and skipped. Default 0.0 DISABLES the skip (see
        # the filter block in analyze() for the rationale).
        self.near_atm_threshold = near_atm_threshold
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

        # Strike proximity filter (near-ATM skip) -- DISABLED BY DEFAULT.
        #
        # Kalshi sets each 15-min contract floor_strike approximately equal to
        # BTC spot at the START of the interval, so spot is essentially always
        # within ~0.01-0.04% of the strike. Near-ATM is therefore the NORMAL
        # (and only) state for these contracts. The previous always-on adaptive
        # filter (0.15% overnight / 0.30% daytime) consequently rejected 100% of
        # available contracts, producing ZERO trades over 15 days.
        #
        # near_atm_threshold defaults to 0.0, which turns this skip OFF so
        # near-ATM contracts pass through to the predictor and volume is
        # restored. Set near_atm_threshold > 0 only if a future market structure
        # (e.g. wider strike spacing) makes skipping near-ATM contracts useful.
        if (
            self.near_atm_threshold > 0
            and spot > 1.0
            and (abs(spot - strike_val) / strike_val) < self.near_atm_threshold
        ):
            logger.debug(
                "[ML BTC 15m] SKIP near-ATM: spot=%.2f strike=%.0f proximity=%.4f%% (threshold=%.2f%%)",
                spot,
                strike_val,
                abs(spot - strike_val) / strike_val * 100,
                self.near_atm_threshold * 100,
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
