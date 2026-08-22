import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.core.bracket_payoff import (
    BracketSpecError,
    attach_spec_to_signals,
    parse_bracket_spec,
    yes_bounds,
)
from src.core.interfaces import Strategy, MarketData, TradeSignal
from src.core.risk_manager import log_rejection
from src.utils.logger import logger

# FR-0.4 reason code for "the API did not give us this market's bracket
# semantics". A silent ``return []`` here is exactly the failure mode Phase 0
# was built to eliminate, so the skip is always logged once, at INFO.
REJECT_NO_BRACKET_SPEC = "BRACKET_SPEC_UNAVAILABLE"


def distance_to_yes_band(value: float, lo: float, hi: float) -> float:
    """Degrees of margin between ``value`` and the edge of the YES band.

    Inside the band it is the distance to the nearest *finite* edge (how safely
    inside we are); outside it is the distance to the nearest edge (how clearly
    we are out). Both are non-negative, which lets one ``min_edge_degrees``
    threshold gate both directions the way the old ``abs(forecast - strike)``
    did for one-sided contracts.

    A consequence worth stating plainly: a 2F-wide ``between`` bracket can never
    offer more than 1F of interior margin, so it cannot clear a 2F threshold on
    forecast alone. That is the honest answer given published NWS day-of
    accuracy (~2.5F), not a limitation to tune around.
    """
    if lo <= value <= hi:
        edges = [abs(value - e) for e in (lo, hi) if math.isfinite(e)]
        return min(edges) if edges else math.inf
    if value < lo:
        return lo - value
    return value - hi


# ==============================================================================
# CITY CONFIGURATION & BIAS CORRECTION
# ==============================================================================

# NWS Station codes and historical bias data per city
# Bias values are derived from historical NWS forecast accuracy
# Positive bias = NWS tends to over-predict temperature
# Negative bias = NWS tends to under-predict temperature
#
# PRD FR-1.4: the ``metar_station`` keys ("KJFK" for NY, "KORD" for CHI,
# "KDFW") were removed on 2026-07-25. Nothing ever read them, and they named
# the NON-settlement airports — the same wrong-station error that FR-1.4 exists
# to close. Observations now come from ``station`` (the settlement station) and
# from it alone; the authoritative per-city registry lives in
# ``src.bots.weather_bot.WEATHER_CITIES``.
CITY_CONFIG = {
    "KXHIGHNY": {
        "station": "KNYC",
        "name": "New York (Central Park)",
        "bias_f": -0.5,  # NWS slightly under-predicts NYC highs
        "accuracy_window_days": 3,  # Forecast accurate within 3 days
    },
    "KXHIGHCHI": {
        "station": "KMDW",
        "name": "Chicago (Midway)",
        # bias_f reset to 0.0 on 2026-04-16 pending 7-day data collection.
        # Prior value of 0.8 was set 2026-04-05 without empirical data.
        # Forecast+actual pairs are now logged via ml_context["nws_forecast_high"]
        # for retune once n >= 10 paired days are available.
        "bias_f": 0.0,
        "accuracy_window_days": 2,
    },
    "KXHIGHLAX": {
        "station": "KLAX",
        "name": "Los Angeles (LAX)",
        "bias_f": 0.2,
        "accuracy_window_days": 5,
    },
    "KXHIGHMIA": {
        "station": "KMIA",
        "name": "Miami (MIA)",
        "bias_f": -0.3,  # Humidity often pushes actual temps higher
        "accuracy_window_days": 4,
    },
    "KXHIGHDFW": {
        "station": "KDFW",
        "name": "Dallas-Fort Worth",
        "bias_f": 1.0,  # Great Plains volatility
        "accuracy_window_days": 2,
    },
}


# Confidence scores based on forecast lead time
def get_forecast_confidence(hours_until_settlement: float) -> float:
    """
    Returns confidence multiplier based on how far we are from settlement.
    Closer to settlement = higher confidence in NWS forecast.
    """
    if hours_until_settlement <= 2:
        return 1.0  # Very high confidence
    elif hours_until_settlement <= 6:
        return 0.9
    elif hours_until_settlement <= 12:
        return 0.8
    elif hours_until_settlement <= 24:
        return 0.7
    elif hours_until_settlement <= 48:
        return 0.5
    else:
        return 0.3  # Low confidence for distant forecasts


# ==============================================================================
# ENHANCED WEATHER STRATEGY V2
# ==============================================================================


class WeatherArbitrageStrategyV2(Strategy):
    """
    The Meteorologist V2 🌦️ (Enhanced)

    Improvements over V1:
    - Historical bias correction per city
    - Confidence scoring based on forecast lead time
    - NWS CLI settlement timing awareness (LST vs local DST)
    - Improved intraday logic with temperature velocity tracking
    """

    def __init__(
        self,
        threshold: float = 0.15,
        min_edge_degrees: float = 2.0,
        enable_bias_correction: bool = True,
    ):
        self.threshold = threshold
        self.min_edge_degrees = min_edge_degrees
        self.enable_bias_correction = enable_bias_correction

        # Track temperature observations for velocity calculation
        self.temp_history: Dict[
            str, List[tuple]
        ] = {}  # {city: [(timestamp, temp), ...]}

    def name(self) -> str:
        bias_mode = "Bias-Corrected" if self.enable_bias_correction else "Standard"
        return f"The Meteorologist V2 ({bias_mode})"

    def _get_city_from_symbol(self, symbol: str) -> Optional[dict]:
        """Extract city config from ticker symbol."""
        for city_key, config in CITY_CONFIG.items():
            if city_key in symbol:
                return config
        return None

    def _apply_bias_correction(self, forecast_temp: float, city_config: dict) -> float:
        """Apply historical bias correction to NWS forecast."""
        if not self.enable_bias_correction or not city_config:
            return forecast_temp

        bias = city_config.get("bias_f", 0.0)
        corrected = forecast_temp - bias  # Subtract over-prediction bias
        return corrected

    def _get_hours_until_settlement(self, symbol: str) -> float:
        """
        Calculate hours until NWS CLI settlement.
        NWS CLI records from 12:00 AM to 11:59 PM LST (Local Standard Time).
        During DST, this means settlement is at 1:00 AM local DAYLIGHT time.
        """
        now = datetime.now()

        # Check if symbol is for today
        today_str = now.strftime("%y%b%d").upper()
        if today_str in symbol:
            # Settlement at midnight LST = 1AM during DST
            # Simplified: assume settlement at 11:59 PM local
            settlement = now.replace(hour=23, minute=59, second=0, microsecond=0)
            delta = settlement - now
            return max(0.0, delta.total_seconds() / 3600)

        # Tomorrow or later - assume 24+ hours
        return 24.0

    def _calculate_temp_velocity(
        self, symbol: str, current_temp: float
    ) -> Optional[float]:
        """
        Calculate rate of temperature change (°F per hour).
        Positive = warming, Negative = cooling.
        """
        city_key = symbol.split("-")[0]
        now = datetime.now()

        # Initialize if needed
        if city_key not in self.temp_history:
            self.temp_history[city_key] = []

        # Add current observation
        self.temp_history[city_key].append((now, current_temp))

        # Keep only last hour of data
        cutoff = now - timedelta(hours=1)
        self.temp_history[city_key] = [
            (t, temp) for t, temp in self.temp_history[city_key] if t > cutoff
        ]

        # Need at least 2 points to calculate velocity
        history = self.temp_history[city_key]
        if len(history) < 2:
            return None

        first_time, first_temp = history[0]
        last_time, last_temp = history[-1]

        time_diff_hours = (last_time - first_time).total_seconds() / 3600
        if time_diff_hours < 0.1:  # Need at least 6 minutes
            return None

        velocity = (last_temp - first_temp) / time_diff_hours
        return velocity

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        """Public entry point: analyse, then stamp bracket semantics.

        Every signal leaves here carrying ``strike_type``/``floor_strike``/
        ``cap_strike`` so the resulting position can be settled through
        ``bracket_payoff`` at expiry (PRD FR-1.2). Stamping at the single
        return boundary means no internal early-return path can omit them.
        """
        return attach_spec_to_signals(self._analyze(market_data), market_data)

    def _analyze(self, market_data: MarketData) -> List[TradeSignal]:
        # 0. Warmup Period (Don't trade before 10 AM)
        if not (10 <= datetime.now().hour < 14):
            return []

        signals = []
        extra = market_data.extra
        symbol = market_data.symbol

        # Skip near-resolved markets (99-cent filter)
        if market_data.bid >= 0.95 or market_data.ask <= 0.05:
            logger.info(
                f"[MeteorV2] SKIP: near-resolved {market_data.symbol} bid={market_data.bid} ask={market_data.ask}"
            )
            return []

        # 1. Source Fidelity
        source = extra.get("source")
        if source not in ("live_nws", "live_metar"):
            return []

        # METAR staleness check: airport ASOS data older than 10 min may be stale,
        # but still likely fresher than NWS observations. Log warning, keep trading.
        if source == "live_metar":
            metar_age = extra.get("metar_age_seconds", 0)
            if metar_age > 600:
                logger.warning(
                    f"[MeteorV2] METAR data is {metar_age}s old (>{600}s) for {symbol}"
                )

        # 2. Extract Key Data
        forecasts = extra.get("forecast")
        current_temp = extra.get("temperature_f")
        daily_max_obs = extra.get("max_temp_today_f")

        # Get city configuration
        city_config = self._get_city_from_symbol(symbol)

        # Market Sentiment
        if market_data.bid > 0:
            market_bid = market_data.bid
            market_ask = market_data.ask
        else:
            return []

        # 3. Bracket semantics from the API fields — never from the ticker
        # (PRD FR-1.1). ``yes_bounds`` gives the inclusive daily-high interval
        # that settles YES: [86, 87] for a `between`, [88, inf) for a `greater`,
        # (-inf, 79] for a `less`.
        try:
            spec = parse_bracket_spec(symbol, extra)
        except BracketSpecError as exc:
            log_rejection(
                REJECT_NO_BRACKET_SPEC,
                strategy=self.name(),
                symbol=symbol,
                detail=str(exc),
            )
            return []

        band_lo, band_hi = yes_bounds(spec)
        today_str = datetime.now().strftime("%y%b%d").upper()
        is_today = today_str in symbol

        # 4. Calculate Confidence and Timing
        hours_until_settlement = self._get_hours_until_settlement(symbol)
        time_confidence = get_forecast_confidence(hours_until_settlement)

        # --- MANDATORY PROTECTION: THE WINNER GUARD ---
        # A daily maximum only ever rises, so:
        #   * the contract has WON once the observed max is inside a YES band
        #     that is open at the top (`greater`) — nothing can take it back;
        #   * the contract has LOST once the observed max is above a finite
        #     upper bound (`between` overshoot, or any `less` bracket).
        # A `between` bracket whose band the max has merely *entered* is NOT
        # won: the day can keep warming straight through the top of the band.
        #
        # This is where the ticker-suffix parser bit hardest. It read every
        # T-prefixed ticker as "above", so a `less` contract (T80 = "79 or
        # below") was evaluated exactly backwards: the guard called it a WINNER
        # the moment the observed max passed 80 — the precise moment it became
        # unwinnable — and bought YES into a certain loss.
        contract_has_won = False
        if is_today and daily_max_obs:
            if math.isinf(band_hi) and daily_max_obs >= band_lo:
                contract_has_won = True
            elif daily_max_obs > band_hi:
                # Already lost: the max can only go higher from here.
                logger.info(
                    f"[MeteorV2] SKIP: {symbol} already lost — observed max "
                    f"{daily_max_obs}F is above the YES band ({spec.describe()})"
                )
                return []

        # IF WON (buy remaining value):
        if contract_has_won:
            if market_ask < 0.98:
                logger.info(
                    f"[MeteorV2] 🏆 HIGH MET: {symbol} WON "
                    f"({spec.describe()}, observed max {daily_max_obs}F). BUY YES."
                )
                signals.append(
                    TradeSignal(
                        symbol=symbol,
                        side="buy",
                        quantity=100,
                        limit_price=market_ask,
                        confidence=1.0,
                    )
                )
            return signals

        # --- INTRADAY VELOCITY CHECK ---
        if is_today and current_temp:
            velocity = self._calculate_temp_velocity(symbol, current_temp)

            # YOGI BERRA LOGIC (End of Day Reality Check)
            # "It ain't over till it's over"... but sometimes it IS over.
            if hours_until_settlement < 1.0:  # Last hour
                # Max realistic temp rise in 1 hour is ~5-8 degrees?
                # Let's say 10 degrees to be safe.
                max_rise = 10.0
                projected_max = max(daily_max_obs or -999, current_temp + max_rise)

                # Even a miracle rise leaves us short of the YES band's lower
                # edge => certain NO. Only meaningful when that edge is finite:
                # a `less` bracket has no lower edge, and being cold is exactly
                # how it WINS, so it must never take this branch.
                if math.isfinite(band_lo) and projected_max < band_lo:
                    if market_bid > 0.05:
                        logger.info(
                            f"[MeteorV2] ⚾ YOGI BERRA: Proj Max {projected_max:.1f} "
                            f"below YES band ({spec.describe()}). BUY NO."
                        )
                        sig = TradeSignal(
                            symbol=symbol,
                            side="buy",
                            quantity=100,
                            limit_price=1.0 - market_bid,
                            confidence=0.99,
                        )
                        sig.contract_side = "NO"
                        sig.stop_loss = 0.20
                        signals.append(sig)
                        return signals

            if velocity is not None:
                # High confidence short: temp dropping and already well below
                # the YES band. Gated on a finite lower edge for the same
                # reason as the Yogi Berra branch above.
                if (
                    math.isfinite(band_lo)
                    and velocity < -1.0
                    and current_temp < (band_lo - 3)
                ):
                    if market_bid > 0.40:
                        logger.info(
                            f"[MeteorV2] ❄️ COOLING VELOCITY ({velocity:.1f}°/hr): BUY NO {symbol}"
                        )
                        sig = TradeSignal(
                            symbol=symbol,
                            side="buy",
                            quantity=50,
                            limit_price=1.0 - market_bid,
                            confidence=0.85,
                        )
                        sig.contract_side = "NO"
                        sig.stop_loss = 0.25
                        signals.append(sig)
                        return signals

                # High confidence long: temp rising rapidly toward the band
                if (
                    math.isfinite(band_lo)
                    and velocity > 2.0
                    and current_temp > (band_lo - 5)
                ):
                    if market_ask < 0.70:
                        logger.info(
                            f"[MeteorV2] 🔥 HEATING VELOCITY ({velocity:.1f}°/hr): Buy {symbol}"
                        )
                        signals.append(
                            TradeSignal(
                                symbol=symbol,
                                side="buy",
                                quantity=50,
                                limit_price=market_ask,
                                confidence=0.80,
                            )
                        )
                        return signals

        # --- FADE THE LONGSHOT ---
        # If market is pricing probability < 10%, and we agree (no strong signal), SELL into it.
        # Shorting 'pennies' (selling at 0.05-0.10) for 10% yield.
        # Risk: It hits. Payout 0.90 loss. Win 0.10. Odds 1:9.
        # Needs high confidence.
        if market_bid < 0.10 and market_bid > 0.02:
            # Only fade if we have NO other signal and time is running out (< 4 hours)
            if hours_until_settlement < 4.0 and not signals:
                # Check if we are comfortably safe: 5F clear of the YES band's
                # lower edge (finite-edge brackets only).
                if (
                    math.isfinite(band_lo)
                    and current_temp
                    and current_temp < (band_lo - 5)
                ):
                    logger.info(
                        f"[MeteorV2] 📉 FADE LONGSHOT: Bid {market_bid:.2f} < 0.10. BUY NO pennies."
                    )
                    sig = TradeSignal(
                        symbol=symbol,
                        side="buy",
                        quantity=20,
                        limit_price=1.0 - market_bid,
                        confidence=0.7,
                    )
                    sig.contract_side = "NO"
                    sig.stop_loss = 0.20
                    signals.append(sig)

        # 5. Forecast-Based Logic with Bias Correction
        if not forecasts:
            return []
        target_period = next((p for p in forecasts if p.get("isDaytime")), None)
        if not target_period:
            return []

        raw_nws_high = target_period.get("temperature")
        nws_high = self._apply_bias_correction(raw_nws_high, city_config)

        if city_config and self.enable_bias_correction:
            bias = city_config.get("bias_f", 0)
            if abs(bias) > 0.2:
                logger.info(
                    f"[MeteorV2] Bias correction for {city_config['name']}: {raw_nws_high}°F -> {nws_high:.1f}°F"
                )

        # Edge = margin between the corrected forecast and the YES band.
        # Inside the band -> how safely inside; outside -> how clearly out.
        forecast_says_yes = band_lo <= nws_high <= band_hi
        edge = distance_to_yes_band(nws_high, band_lo, band_hi)

        # Tag raw NWS forecast onto signals for trade journal instrumentation.
        # Downstream (mixins.py ml_context) picks up nws_forecast_high so each
        # journal entry records the forecast at trade time. Used for bias retune.
        def _tag_forecast(sig):
            if raw_nws_high is not None:
                sig.nws_forecast_high = float(raw_nws_high)
            return sig

        # Only trade if edge exceeds minimum threshold
        if edge < self.min_edge_degrees:
            return []

        # Adjust confidence based on time and edge
        base_confidence = min(0.95, 0.6 + (edge / 20))  # More edge = more confidence
        final_confidence = base_confidence * time_confidence

        # Forecast Arbitrage. One rule for all three contract types: the
        # forecast either lands inside the YES band (buy YES) or clearly
        # outside it (buy NO). The old code had a two-branch above/below split
        # driven by the ticker suffix, which mis-classified every `less`
        # bracket and had no representation at all for a two-sided `between`.
        if forecast_says_yes and market_ask < 0.80:
            logger.info(
                f"[MeteorV2] 🌡️ FORECAST LONG: {nws_high:.1f}°F inside YES band "
                f"({spec.describe()}), margin {edge:.1f}°F (conf={final_confidence:.2f})"
            )
            signals.append(
                _tag_forecast(
                    TradeSignal(
                        symbol=symbol,
                        side="buy",
                        quantity=50,
                        limit_price=market_ask,
                        confidence=final_confidence,
                    )
                )
            )
        elif not forecast_says_yes and market_bid > 0.20:
            logger.info(
                f"[MeteorV2] ❄️ FORECAST SHORT: {nws_high:.1f}°F outside YES band "
                f"({spec.describe()}) by {edge:.1f}°F (conf={final_confidence:.2f}) BUY NO"
            )
            sig = _tag_forecast(
                TradeSignal(
                    symbol=symbol,
                    side="buy",
                    quantity=50,
                    limit_price=1.0 - market_bid,
                    confidence=final_confidence,
                )
            )
            sig.contract_side = "NO"
            sig.stop_loss = 0.25
            signals.append(sig)

        return signals

    def _analyze_mock(self, market_data):
        return []


# Sprint 8: WeatherArbitrageStrategy (V1) removed — superseded by WeatherArbitrageStrategyV2
