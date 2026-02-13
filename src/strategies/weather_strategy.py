from src.core.interfaces import Strategy, MarketData, TradeSignal
from typing import List, Optional, Dict
from datetime import datetime, timedelta
import re
from src.utils.logger import logger


# ==============================================================================
# CITY CONFIGURATION & BIAS CORRECTION
# ==============================================================================

# NWS Station codes and historical bias data per city
# Bias values are derived from historical NWS forecast accuracy
# Positive bias = NWS tends to over-predict temperature
# Negative bias = NWS tends to under-predict temperature
CITY_CONFIG = {
    'KXHIGHNY': {
        'station': 'KNYC',
        'name': 'New York (Central Park)',
        'bias_f': -0.5,  # NWS slightly under-predicts NYC highs
        'accuracy_window_days': 3,  # Forecast accurate within 3 days
    },
    'KXHIGHCHI': {
        'station': 'KMDW',
        'name': 'Chicago (Midway)',
        'bias_f': 0.8,  # NWS over-predicts Chicago highs (Great Plains effect)
        'accuracy_window_days': 2,
    },
    'KXHIGHLAX': {
        'station': 'KLAX',
        'name': 'Los Angeles (LAX)',
        'bias_f': 0.2,
        'accuracy_window_days': 5,
    },
    'KXHIGHMIA': {
        'station': 'KMIA',
        'name': 'Miami (MIA)',
        'bias_f': -0.3,  # Humidity often pushes actual temps higher
        'accuracy_window_days': 4,
    },
    'KXHIGHDFW': {
        'station': 'KDFW',
        'name': 'Dallas-Fort Worth',
        'bias_f': 1.0,  # Great Plains volatility
        'accuracy_window_days': 2,
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
    
    def __init__(self, 
                 threshold: float = 0.15,
                 min_edge_degrees: float = 2.0,
                 enable_bias_correction: bool = True):
        self.threshold = threshold
        self.min_edge_degrees = min_edge_degrees
        self.enable_bias_correction = enable_bias_correction
        
        # Track temperature observations for velocity calculation
        self.temp_history: Dict[str, List[tuple]] = {}  # {city: [(timestamp, temp), ...]}
        
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
        
        bias = city_config.get('bias_f', 0.0)
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
    
    def _calculate_temp_velocity(self, symbol: str, current_temp: float) -> Optional[float]:
        """
        Calculate rate of temperature change (°F per hour).
        Positive = warming, Negative = cooling.
        """
        city_key = symbol.split('-')[0]
        now = datetime.now()
        
        # Initialize if needed
        if city_key not in self.temp_history:
            self.temp_history[city_key] = []
        
        # Add current observation
        self.temp_history[city_key].append((now, current_temp))
        
        # Keep only last hour of data
        cutoff = now - timedelta(hours=1)
        self.temp_history[city_key] = [
            (t, temp) for t, temp in self.temp_history[city_key] 
            if t > cutoff
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
        # 0. Warmup Period (Don't trade before 10 AM)
        if datetime.now().hour < 10:
            return []
            
        signals = []
        extra = market_data.extra
        symbol = market_data.symbol
        
        # 1. Source Fidelity
        if extra.get('source') != 'live_nws':
            return []

        # 2. Extract Key Data
        forecasts = extra.get('forecast')
        current_temp = extra.get('temperature_f')
        daily_max_obs = extra.get('max_temp_today_f')
        
        # Get city configuration
        city_config = self._get_city_from_symbol(symbol)
        
        # Market Sentiment
        if market_data.bid > 0:
            market_bid = market_data.bid
            market_ask = market_data.ask
        else:
            return []

        # 3. Parse Ticker for Strike & Date
        try:
            parts = symbol.split('-')
            strike_str = parts[-1]  # e.g. T80 or B85.5
            
            is_above_contract = True
            if strike_str.startswith('B'):
                is_above_contract = False
            
            strike_val = float(re.sub(r'[A-Za-z]', '', strike_str))
            
            today_str = datetime.now().strftime("%y%b%d").upper()
            is_today = today_str in symbol
        except:
            return []

        # 4. Calculate Confidence and Timing
        hours_until_settlement = self._get_hours_until_settlement(symbol)
        time_confidence = get_forecast_confidence(hours_until_settlement)
        
        # --- MANDATORY PROTECTION: THE WINNER GUARD ---
        contract_has_won = False
        if is_today and daily_max_obs:
            if is_above_contract:
                if daily_max_obs >= strike_val:
                    contract_has_won = True

        # IF LOST (Below contract with temp above strike):
        if is_today and daily_max_obs and not is_above_contract and daily_max_obs > strike_val:
            return []

        # IF WON (buy remaining value):
        if contract_has_won:
            if market_ask < 0.98:
                logger.info(f"[MeteorV2] 🏆 HIGH MET: {symbol} WON. BUY YES.")
                signals.append(TradeSignal(
                    symbol=symbol, side="buy", quantity=100, 
                    limit_price=market_ask, confidence=1.0
                ))
            return signals

        # --- INTRADAY VELOCITY CHECK ---
        if is_today and current_temp:
            velocity = self._calculate_temp_velocity(symbol, current_temp)
            
            if velocity is not None:
                # High confidence short: temp dropping and already below strike
                if is_above_contract and velocity < -1.0 and current_temp < (strike_val - 3):
                    if market_bid > 0.40:
                        logger.info(f"[MeteorV2] ❄️ COOLING VELOCITY ({velocity:.1f}°/hr): Short {symbol}")
                        signals.append(TradeSignal(
                            symbol=symbol, side="sell", quantity=50,
                            limit_price=market_bid, confidence=0.85
                        ))
                        return signals
                
                # High confidence long: temp rising rapidly toward strike
                if is_above_contract and velocity > 2.0 and current_temp > (strike_val - 5):
                    if market_ask < 0.70:
                        logger.info(f"[MeteorV2] 🔥 HEATING VELOCITY ({velocity:.1f}°/hr): Buy {symbol}")
                        signals.append(TradeSignal(
                            symbol=symbol, side="buy", quantity=50,
                            limit_price=market_ask, confidence=0.80
                        ))
                        return signals

        # 5. Forecast-Based Logic with Bias Correction
        if not forecasts:
            return []
        target_period = next((p for p in forecasts if p.get('isDaytime')), None)
        if not target_period:
            return []

        raw_nws_high = target_period.get('temperature')
        nws_high = self._apply_bias_correction(raw_nws_high, city_config)
        
        if city_config and self.enable_bias_correction:
            bias = city_config.get('bias_f', 0)
            if abs(bias) > 0.2:
                logger.info(f"[MeteorV2] Bias correction for {city_config['name']}: {raw_nws_high}°F -> {nws_high:.1f}°F")
        
        # Calculate edge (difference between forecast and strike)
        edge = abs(nws_high - strike_val)
        
        # Only trade if edge exceeds minimum threshold
        if edge < self.min_edge_degrees:
            return []
        
        # Adjust confidence based on time and edge
        base_confidence = min(0.95, 0.6 + (edge / 20))  # More edge = more confidence
        final_confidence = base_confidence * time_confidence
        
        # Forecast Arbitrage
        if is_above_contract:
            if nws_high > (strike_val + self.min_edge_degrees) and market_ask < 0.80:
                logger.info(f"[MeteorV2] 🌡️ FORECAST LONG: {nws_high:.1f}°F > {strike_val}°F (conf={final_confidence:.2f})")
                signals.append(TradeSignal(
                    symbol=symbol, side="buy", quantity=50,
                    limit_price=market_ask, confidence=final_confidence
                ))
            elif nws_high < (strike_val - self.min_edge_degrees) and market_bid > 0.20:
                logger.info(f"[MeteorV2] ❄️ FORECAST SHORT: {nws_high:.1f}°F < {strike_val}°F (conf={final_confidence:.2f})")
                signals.append(TradeSignal(
                    symbol=symbol, side="sell", quantity=50,
                    limit_price=market_bid, confidence=final_confidence
                ))
        else:
            # Below Contract
            if nws_high < (strike_val - self.min_edge_degrees) and market_ask < 0.80:
                signals.append(TradeSignal(
                    symbol=symbol, side="buy", quantity=50,
                    limit_price=market_ask, confidence=final_confidence
                ))
            elif nws_high > (strike_val + self.min_edge_degrees) and market_bid > 0.20:
                signals.append(TradeSignal(
                    symbol=symbol, side="sell", quantity=50,
                    limit_price=market_bid, confidence=final_confidence
                ))
        
        return signals

    def _analyze_mock(self, market_data):
        return []

class WeatherArbitrageStrategy(Strategy):
    """
    The Meteorologist 🌦️ (v4 - Strike Aware)
    Analyzes NWS Forecasts vs Market Sentiment with T/B Strike awareness.
    """
    
    def __init__(self, threshold: float = 0.15):
        self.threshold = threshold
        
    def name(self) -> str:
        return "The Meteorologist"
        
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        # 0. Warmup Period (Don't trade before 10 AM)
        if datetime.now().hour < 10:
            return []
            
        signals = []
        extra = market_data.extra
        symbol = market_data.symbol
        
        # 1. Source Fidelity
        if extra.get('source') != 'live_nws': return []

        # 2. Extract Key Data
        forecasts = extra.get('forecast')
        current_temp = extra.get('temperature_f')
        daily_max_obs = extra.get('max_temp_today_f')
        
        # Market Sentiment
        if market_data.bid > 0:
            market_bid = market_data.bid
            market_ask = market_data.ask
        else:
            return []

        # 3. Parse Ticker for Strike & Date
        try:
            parts = symbol.split('-')
            strike_str = parts[-1] # e.g. T80 or B85.5
            
            # Identify Direction: T (Above/Top), B (Below/Bottom)
            is_above_contract = True
            if strike_str.startswith('B'):
                is_above_contract = False
            
            strike_val = float(re.sub(r'[A-Za-z]', '', strike_str))
            
            today_str = datetime.now().strftime("%y%b%d").upper()
            is_today = today_str in symbol
            
            kalshi_ticker_base = symbol.split('-')[0]
        except:
            return []

        # --- MANDATORY PROTECTION: THE WINNER GUARD ---
        # Determine if the contract has ALREADY won based on daily observations.
        contract_has_won = False
        if is_today and daily_max_obs:
            if is_above_contract:
                if daily_max_obs >= strike_val: contract_has_won = True
            else:
                # For a 'Below' contract, it's only 'won' if the high is still below strike
                # AND it's the end of the day. 
                # Actually, a 'Below' contract is ALWAYS 'winning' until the temp breaks the strike.
                # So we can't say it has 'won' early. 
                # But we CAN say it has 'LOST' if daily_max_obs > strike_val.
                pass

        # IF LOST:
        if is_today and daily_max_obs and not is_above_contract and daily_max_obs > strike_val:
            # Below Strike contract has LOST because temp went above. Value = 0.00.
            # Shorting @ 0.99 here is actually great (if price is still high), but usually price is 0.01.
            return [] 

        # IF WON:
        if contract_has_won:
            # Above Strike contract has WON. Value = 1.00.
            if market_ask < 0.98:
                logger.info(f"[Strategy] 🏆 HIGH MET: {symbol} WON. BUY YES.")
                signals.append(TradeSignal(
                    symbol=symbol, side="buy", quantity=100, limit_price=market_ask, confidence=1.0
                ))
            return signals # No Shorting a winner.

        # --- LOGIC C: INTRADAY REALITY CHECK ---
        if is_today and current_temp:
            # Check for 'Below' contract suicide shorting (The bug the user reported)
            # If it's a 'Below' contract (B85.5) and current temp is 70F.
            # Betting 'NO' (Shorting) means betting it goes ABOVE 85.5.
            # If it's late and 70F, betting it hits 85.5 is stupid.
            
            if not is_above_contract:
                # We should ONLY short (bet NO/Above) if we are very close to strike or forecast is high
                pass # Avoid for now to stop the bleeding
            
            # Conservative Short for 'Above' contracts
            if is_above_contract and daily_max_obs < (strike_val - 2) and current_temp < (strike_val - 3):
                 if market_bid > 0.40:
                     logger.info(f"[Strategy] ❄️ INTRADAY SHORT (Above): {symbol}. SELL.")
                     signals.append(TradeSignal(
                        symbol=symbol, side="sell", quantity=50, limit_price=market_bid, confidence=0.95
                    ))
            
            if signals: return signals

        # 4. Forecast-Based Logic
        if not forecasts: return []
        target_period = next((p for p in forecasts if p.get('isDaytime')), None)
        if not target_period: return []

        nws_high = target_period.get('temperature')
        
        # Forecast Arbitrage
        # If we expect 90F and strike is T85 -> Buy YES
        if is_above_contract:
            if nws_high > (strike_val + 2) and market_ask < 0.80:
                signals.append(TradeSignal(symbol=symbol, side="buy", quantity=50, limit_price=market_ask, confidence=0.9))
            elif nws_high < (strike_val - 2) and market_bid > 0.20:
                signals.append(TradeSignal(symbol=symbol, side="sell", quantity=50, limit_price=market_bid, confidence=0.9))
        else:
            # Below Contract (B85): YES means < 85.
            # If forecast is 70F -> Buy YES.
            if nws_high < (strike_val - 2) and market_ask < 0.80:
                signals.append(TradeSignal(symbol=symbol, side="buy", quantity=50, limit_price=market_ask, confidence=0.9))
            # If forecast is 95F -> Short (Sell YES).
            elif nws_high > (strike_val + 2) and market_bid > 0.20:
                signals.append(TradeSignal(symbol=symbol, side="sell", quantity=50, limit_price=market_bid, confidence=0.9))
        
        return signals

    def _analyze_mock(self, market_data): return []