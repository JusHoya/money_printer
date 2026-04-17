"""ML-driven weather strategy with YES and NO wager support.

Uses the weather ensemble model (NWS + HRRR blend) to predict
temperature bracket probabilities and trades when the model's
estimate diverges from the Kalshi market price.

Sprint 3, Task 3.3 of Money Printer V2.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.ml.predictor import ModelPredictor

logger = logging.getLogger(__name__)

# City configuration (aligned with weather_strategy.py)
CITY_CONFIG = {
    "KXHIGHNY": {"station": "KNYC", "name": "New York", "bias_f": -0.5},
    # bias_f reset to 0.0 on 2026-04-16 pending empirical retune (n >= 10 paired days)
    "KXHIGHCHI": {"station": "KMDW", "name": "Chicago", "bias_f": 0.0},
    "KXHIGHLAX": {"station": "KLAX", "name": "Los Angeles", "bias_f": 0.2},
    "KXHIGHMIA": {"station": "KMIA", "name": "Miami", "bias_f": -0.3},
    "KXHIGHDFW": {"station": "KDFW", "name": "Dallas", "bias_f": 1.0},
}


class MLWeatherStrategy(Strategy):
    """ML-driven weather bracket strategy.

    Supports both YES and NO wagers.  Trades when the ML ensemble's
    probability diverges from the Kalshi bracket price by more than
    *min_edge* (default 8%).
    """

    def __init__(
        self,
        min_edge: float = 0.08,
        predictor: ModelPredictor = None,
    ):
        self.min_edge = min_edge
        self.predictor = predictor or ModelPredictor()

        # Temperature velocity tracking per city
        self._temp_history: Dict[str, List[tuple]] = {}

    def name(self) -> str:
        return f"ML Weather (edge>{self.min_edge})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _city_config(symbol: str) -> Optional[dict]:
        for key, cfg in CITY_CONFIG.items():
            if key in symbol:
                return cfg
        return None

    @staticmethod
    def _parse_strike(symbol: str):
        """Return (strike_val, is_above_contract) from ticker."""
        try:
            parts = symbol.split("-")
            strike_str = parts[-1]
            is_above = not strike_str.startswith("B")
            val = float(re.sub(r"[A-Za-z]", "", strike_str))
            return val, is_above
        except Exception:
            return None, None

    def _hours_until_settlement(self, symbol: str, now: datetime = None) -> float:
        now = now or datetime.now()
        today_str = now.strftime("%y%b%d").upper()
        if today_str in symbol:
            settlement = now.replace(hour=23, minute=59, second=0)
            return max(0.0, (settlement - now).total_seconds() / 3600)
        return 24.0

    def _temp_velocity(self, symbol: str, current_temp: float) -> Optional[float]:
        city_key = symbol.split("-")[0]
        now = datetime.now()
        hist = self._temp_history.setdefault(city_key, [])
        hist.append((now, current_temp))
        cutoff = now - timedelta(hours=1)
        self._temp_history[city_key] = [(t, v) for t, v in hist if t > cutoff]
        hist = self._temp_history[city_key]
        if len(hist) < 2:
            return None
        dt = (hist[-1][0] - hist[0][0]).total_seconds() / 3600
        if dt < 0.1:
            return None
        return (hist[-1][1] - hist[0][1]) / dt

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        # Only trade 10 AM – 2 PM (use data timestamp when available)
        check_time = market_data.timestamp or datetime.now()
        if not (10 <= check_time.hour < 14):
            return []

        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        symbol = market_data.symbol

        # Source check
        if extra.get("source") != "live_nws":
            return signals

        # Skip near-resolved markets
        if market_data.bid >= 0.95 or market_data.ask <= 0.05:
            return signals

        # Need two-sided market
        if market_data.bid <= 0:
            return signals

        strike, is_above = self._parse_strike(symbol)
        if strike is None:
            return signals

        # Extract weather data
        current_temp = extra.get("temperature_f")
        daily_max = extra.get("max_temp_today_f")
        forecasts = extra.get("forecast", [])
        city_cfg = self._city_config(symbol)
        station = city_cfg["station"] if city_cfg else "unknown"
        hours_left = self._hours_until_settlement(symbol, now=check_time)

        # ── Winner guard ─────────────────────────────────────────────
        today_str = check_time.strftime("%y%b%d").upper()
        is_today = today_str in symbol

        if is_today and daily_max:
            if is_above and daily_max >= strike:
                # Already won — buy remaining value
                if market_data.ask < 0.98:
                    logger.info("[ML Weather] WON: %s. BUY YES.", symbol)
                    signals.append(
                        TradeSignal(
                            symbol=symbol,
                            side="buy",
                            quantity=100,
                            limit_price=market_data.ask,
                            confidence=1.0,
                        )
                    )
                return signals
            if not is_above and daily_max > strike:
                # Below-contract already lost
                return signals

        # ── Yogi Berra end-of-day check ──────────────────────────────
        if is_today and current_temp and hours_left < 1.0 and is_above:
            max_rise = 10.0
            projected = max(daily_max or -999, current_temp + max_rise)
            if projected < strike and market_data.bid > 0.05:
                logger.info(
                    "[ML Weather] YOGI BERRA: proj %.1f < strike %.1f. BUY NO %s",
                    projected,
                    strike,
                    symbol,
                )
                sig = TradeSignal(
                    symbol=symbol,
                    side="buy",
                    quantity=100,
                    limit_price=1.0 - market_data.bid,
                    confidence=0.99,
                    contract_side="NO",
                )
                sig.stop_loss = 0.20
                signals.append(sig)
                return signals

        # ── ML prediction ────────────────────────────────────────────
        nws_high = None
        target_period = next((p for p in forecasts if p.get("isDaytime")), None)
        if target_period:
            nws_high = target_period.get("temperature")

        hrrr_forecast = extra.get("hrrr_forecast", nws_high or 0)
        nws_forecast = nws_high or (current_temp or 70)

        # Determine bracket bounds from strike
        if is_above:
            bracket_lower = strike
            bracket_upper = strike + 10
        else:
            bracket_lower = strike - 10
            bracket_upper = strike

        try:
            pred = self.predictor.predict_weather(
                nws_forecast=nws_forecast,
                hrrr_forecast=hrrr_forecast,
                station_id=station,
                bracket_lower=bracket_lower,
                bracket_upper=bracket_upper,
            )
        except Exception as exc:
            logger.debug("[ML Weather] Prediction failed: %s", exc)
            return signals

        ml_prob = pred["probability"]
        ml_conf = pred["confidence"]

        # For "above" contracts: ml_prob ≈ P(temp in bracket above strike)
        # Translate to P(YES wins) for the specific contract
        if is_above:
            yes_prob = ml_prob
        else:
            yes_prob = 1.0 - ml_prob

        # ── Temperature velocity boost ───────────────────────────────
        if current_temp:
            vel = self._temp_velocity(symbol, current_temp)
            if vel is not None:
                # Rapid cooling → boost NO confidence
                if vel < -1.0 and current_temp < strike - 3 and is_above:
                    ml_conf = min(0.95, ml_conf + 0.10)
                # Rapid heating → boost YES confidence
                if vel > 2.0 and current_temp > strike - 5 and is_above:
                    ml_conf = min(0.95, ml_conf + 0.10)

        # ── Signal generation (edge-based) ───────────────────────────
        # YES side
        yes_edge = yes_prob - market_data.ask
        if yes_edge >= self.min_edge:
            lp = max(0.01, min(0.99, market_data.ask))
            sig = TradeSignal(
                symbol=symbol,
                side="buy",
                quantity=50,
                limit_price=lp,
                confidence=ml_conf,
                contract_side="YES",
            )
            sig.stop_loss = max(0.01, lp - 0.15)
            logger.info(
                "[ML Weather] BUY YES %s | prob=%.3f ask=%.2f edge=%.3f",
                symbol,
                yes_prob,
                market_data.ask,
                yes_edge,
            )
            signals.append(sig)

        # NO side
        no_prob = 1.0 - yes_prob
        no_cost = 1.0 - market_data.bid
        no_edge = no_prob - no_cost
        if no_edge >= self.min_edge and no_cost > 0.01 and not signals:
            lp = max(0.01, min(0.99, no_cost))
            sig = TradeSignal(
                symbol=symbol,
                side="buy",
                quantity=50,
                limit_price=lp,
                confidence=ml_conf,
                contract_side="NO",
            )
            sig.stop_loss = 0.25
            logger.info(
                "[ML Weather] BUY NO %s | prob=%.3f no_cost=%.2f edge=%.3f",
                symbol,
                no_prob,
                no_cost,
                no_edge,
            )
            signals.append(sig)

        return signals
