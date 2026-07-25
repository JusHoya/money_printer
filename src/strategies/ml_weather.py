"""ML-driven weather strategy with YES and NO wager support.

Uses the weather ensemble model (NWS + HRRR blend) to predict
temperature bracket probabilities and trades when the model's
estimate diverges from the Kalshi market price.

Sprint 3, Task 3.3 of Money Printer V2.
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.core.bracket_payoff import (
    BracketSpecError,
    attach_spec_to_signals,
    parse_bracket_spec,
    yes_bounds,
)
from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.core.risk_manager import log_rejection
from src.ml.predictor import ModelPredictor

logger = logging.getLogger(__name__)

# FR-0.4 reason code, shared with weather_strategy: the API did not give us
# this market's bracket semantics, so we skip it loudly instead of silently.
REJECT_NO_BRACKET_SPEC = "BRACKET_SPEC_UNAVAILABLE"

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
        """Public entry point: analyse, then stamp bracket semantics.

        See ``WeatherArbitrageStrategyV2.analyze`` — signals must carry
        ``strike_type``/``floor_strike``/``cap_strike`` to be settleable at
        expiry (PRD FR-1.2).
        """
        return attach_spec_to_signals(self._analyze(market_data), market_data)

    def _analyze(self, market_data: MarketData) -> List[TradeSignal]:
        # Only trade 10 AM – 2 PM (use data timestamp when available)
        check_time = market_data.timestamp or datetime.now()
        if not (10 <= check_time.hour < 14):
            return []

        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        symbol = market_data.symbol

        # Source check — accept both NWS forecasts and METAR observations (METAR ~10x more precise)
        if extra.get("source") not in ("live_nws", "live_metar"):
            return signals

        # Skip near-resolved markets
        if market_data.bid >= 0.95 or market_data.ask <= 0.05:
            return signals

        # Need two-sided market
        if market_data.bid <= 0:
            return signals

        # Bracket semantics from the API fields only (PRD FR-1.1). ``yes_bounds``
        # is the inclusive daily-high interval that settles YES; ``band_lo`` is
        # the temperature we need to reach, ``band_hi`` the one we must not
        # exceed. Either may be infinite for a one-sided contract.
        try:
            spec = parse_bracket_spec(symbol, extra)
        except BracketSpecError as exc:
            log_rejection(
                REJECT_NO_BRACKET_SPEC,
                strategy=self.name(),
                symbol=symbol,
                detail=str(exc),
            )
            return signals

        band_lo, band_hi = yes_bounds(spec)

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

        # A daily maximum only rises: a band that is open at the top is WON the
        # moment the observed max reaches it, and any band with a finite top is
        # LOST the moment the observed max passes it. A `between` bracket the
        # max has merely entered is neither — the day can warm through it.
        if is_today and daily_max:
            if math.isinf(band_hi) and daily_max >= band_lo:
                # Already won — buy remaining value
                if market_data.ask < 0.98:
                    logger.info(
                        "[ML Weather] WON: %s (%s, observed max %sF). BUY YES.",
                        symbol,
                        spec.describe(),
                        daily_max,
                    )
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
            if daily_max > band_hi:
                # Already lost — the observed max is above the YES band.
                logger.info(
                    "[ML Weather] SKIP %s: observed max %sF is above the YES "
                    "band (%s)",
                    symbol,
                    daily_max,
                    spec.describe(),
                )
                return signals

        # ── Yogi Berra end-of-day check ──────────────────────────────
        # Only for bands with a finite lower edge: falling short of the band is
        # a certain NO for `greater`/`between`, but it is how a `less` bracket
        # WINS, so a `less` contract must never take this branch.
        if is_today and current_temp and hours_left < 1.0 and math.isfinite(band_lo):
            max_rise = 10.0
            projected = max(daily_max or -999, current_temp + max_rise)
            if projected < band_lo and market_data.bid > 0.05:
                logger.info(
                    "[ML Weather] YOGI BERRA: proj %.1f below YES band (%s). BUY NO %s",
                    projected,
                    spec.describe(),
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

        # The predictor wants a finite [lower, upper] window and returns
        # P(daily high lands in it) — which IS P(YES) once the window is the
        # contract's own YES band. Open ends are clamped to a 50F tail, wide
        # enough to carry effectively all the mass of a day-ahead temperature
        # distribution (day-of NWS sigma is ~2.5F).
        OPEN_END_F = 50.0
        bracket_lower = band_lo if math.isfinite(band_lo) else band_hi - OPEN_END_F
        bracket_upper = band_hi if math.isfinite(band_hi) else band_lo + OPEN_END_F

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

        # The window handed to the predictor IS the contract's YES band, so its
        # probability is already P(YES). The old code asked for a band derived
        # from the ticker suffix and then flipped the answer for "below"
        # contracts — two guesses that had to cancel out, and did not.
        yes_prob = ml_prob

        # ── Temperature velocity boost ───────────────────────────────
        if current_temp:
            vel = self._temp_velocity(symbol, current_temp)
            if vel is not None:
                # Both boosts are about distance from the YES band's lower edge,
                # so they apply only when that edge is finite.
                if math.isfinite(band_lo):
                    # Rapid cooling → boost NO confidence
                    if vel < -1.0 and current_temp < band_lo - 3:
                        ml_conf = min(0.95, ml_conf + 0.10)
                    # Rapid heating → boost YES confidence
                    if vel > 2.0 and current_temp > band_lo - 5:
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
