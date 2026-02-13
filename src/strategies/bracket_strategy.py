"""
Bracket Strategy 📊 (Iron Condor Style)

Implements range-bound trading strategies for Kalshi's mutually exclusive markets:
- Fed Rates
- Treasury Yields  
- EUR/USD
- Weather Bracket Markets

The strategy profits when events settle within a predicted range by selling
contracts on outer strikes (low probability events) and collecting premium.
"""

from src.core.interfaces import Strategy, MarketData, TradeSignal
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
import re
from src.utils.logger import logger


# ==============================================================================
# BRACKET CONFIGURATION
# ==============================================================================

@dataclass
class BracketConfig:
    """Configuration for a bracket trade."""
    market_type: str
    expected_value: float
    inner_buffer: float  # Distance from expected to inner strikes (safe zone)
    outer_buffer: float  # Distance from expected to outer strikes (sell zone)
    max_contracts: int
    min_premium: float  # Minimum price to sell at (e.g., 0.15 = 15 cents)
    

# Default configurations for different market types
BRACKET_CONFIGS = {
    'FED_RATE': BracketConfig(
        market_type='FED_RATE',
        expected_value=4.75,  # Current Fed Funds target
        inner_buffer=0.25,
        outer_buffer=0.50,
        max_contracts=50,
        min_premium=0.10,
    ),
    'TREASURY_10Y': BracketConfig(
        market_type='TREASURY_10Y',
        expected_value=4.25,  # Expected 10Y yield
        inner_buffer=0.10,
        outer_buffer=0.25,
        max_contracts=50,
        min_premium=0.12,
    ),
    'EUR_USD': BracketConfig(
        market_type='EUR_USD',
        expected_value=1.085,  # Expected EUR/USD rate
        inner_buffer=0.005,
        outer_buffer=0.015,
        max_contracts=50,
        min_premium=0.15,
    ),
}


# ==============================================================================
# IRON CONDOR BRACKET STRATEGY
# ==============================================================================

class BracketStrategy(Strategy):
    """
    The Condor 🦅 (Iron Condor Style)
    
    Profits from range-bound markets by:
    1. Selling YES contracts on "Above X" strikes when price is expected to stay below
    2. Selling YES contracts on "Below Y" strikes when price is expected to stay above
    
    This creates a "condor" profit zone between the two strikes.
    
    Risk Management:
    - Max loss is defined by the spread width
    - Position sizing based on portfolio exposure limits
    - Early exit if market moves against position
    """
    
    def __init__(self,
                 max_exposure_per_bracket: float = 0.05,  # 5% of portfolio per bracket
                 min_time_to_settlement_hours: float = 2.0,
                 max_time_to_settlement_hours: float = 48.0):
        
        self.max_exposure_per_bracket = max_exposure_per_bracket
        self.min_time_to_settlement = min_time_to_settlement_hours
        self.max_time_to_settlement = max_time_to_settlement_hours
        
        # Track active bracket positions
        self.active_brackets: Dict[str, dict] = {}
        
    def name(self) -> str:
        return "The Condor (Iron Condor)"
    
    def _identify_market_type(self, symbol: str) -> Optional[str]:
        """Identify what type of bracket market this is."""
        symbol_upper = symbol.upper()
        
        if 'FOMC' in symbol_upper or 'FEDFUNDS' in symbol_upper or 'FED' in symbol_upper:
            return 'FED_RATE'
        elif '10Y' in symbol_upper or 'TREASURY' in symbol_upper or 'YIELD' in symbol_upper:
            return 'TREASURY_10Y'
        elif 'EUR' in symbol_upper or 'EURUSD' in symbol_upper:
            return 'EUR_USD'
        
        return None
    
    def _parse_strike(self, symbol: str) -> Optional[Tuple[float, bool]]:
        """
        Parse strike value and direction from symbol.
        Returns: (strike_value, is_above_strike)
        """
        try:
            parts = symbol.split('-')
            strike_str = parts[-1]
            
            # Determine direction
            is_above = strike_str.upper().startswith('A') or strike_str.upper().startswith('T')
            is_below = strike_str.upper().startswith('B')
            
            # Extract numeric value
            strike_value = float(re.sub(r'[A-Za-z]', '', strike_str))
            
            if is_above:
                return (strike_value, True)
            elif is_below:
                return (strike_value, False)
            
            return None
        except:
            return None
    
    def _calculate_time_to_settlement(self, symbol: str) -> float:
        """Estimate hours until settlement based on symbol."""
        now = datetime.now()
        
        # Try to parse date from symbol
        try:
            parts = symbol.split('-')
            for part in parts:
                # Look for date patterns like 26FEB05
                if len(part) >= 7 and part[:2].isdigit():
                    # Parse YYMMMDD
                    year = 2000 + int(part[:2])
                    month_str = part[2:5].upper()
                    day = int(part[5:7])
                    
                    months = {
                        'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
                        'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
                        'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
                    }
                    
                    if month_str in months:
                        settlement_date = datetime(year, months[month_str], day, 23, 59)
                        delta = settlement_date - now
                        return max(0.0, delta.total_seconds() / 3600)
        except:
            pass
        
        return 24.0  # Default assumption
    
    def _is_outer_strike(self, strike: float, is_above: bool, config: BracketConfig) -> bool:
        """Check if this strike is in the outer (sellable) zone."""
        expected = config.expected_value
        
        if is_above:
            # For "Above X" contracts, outer zone is high strikes
            return strike >= (expected + config.outer_buffer)
        else:
            # For "Below Y" contracts, outer zone is low strikes  
            return strike <= (expected - config.outer_buffer)
    
    def _calculate_position_size(self, 
                                  market_price: float, 
                                  portfolio_balance: float,
                                  config: BracketConfig) -> int:
        """Calculate appropriate position size based on risk limits."""
        # Max exposure in dollars
        max_exposure = portfolio_balance * self.max_exposure_per_bracket
        
        # Each contract costs (1 - price) to sell (max potential loss)
        max_loss_per_contract = 1.0 - market_price
        
        if max_loss_per_contract <= 0.01:
            return 0  # Don't sell at 99c+
        
        # Calculate max contracts
        max_contracts = int(max_exposure / max_loss_per_contract)
        
        # Apply config limit
        return min(max_contracts, config.max_contracts)
    
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        symbol = market_data.symbol
        extra = market_data.extra
        
        # 1. Identify market type
        market_type = self._identify_market_type(symbol)
        if not market_type or market_type not in BRACKET_CONFIGS:
            return []
        
        config = BRACKET_CONFIGS[market_type]
        
        # 2. Parse strike information
        strike_info = self._parse_strike(symbol)
        if not strike_info:
            return []
        
        strike_value, is_above = strike_info
        
        # 3. Check timing
        hours_to_settlement = self._calculate_time_to_settlement(symbol)
        if hours_to_settlement < self.min_time_to_settlement:
            return []  # Too close to settlement (high gamma risk)
        if hours_to_settlement > self.max_time_to_settlement:
            return []  # Too far out (low theta value)
        
        # 4. Check if this is an outer strike (profitable to sell)
        if not self._is_outer_strike(strike_value, is_above, config):
            return []  # Not in our sell zone
        
        # 5. Check market price meets minimum premium
        market_bid = market_data.bid
        if market_bid < config.min_premium:
            return []  # Premium too low
        
        # 6. Calculate position size
        portfolio_balance = extra.get('portfolio_balance', 1000.0)
        position_size = self._calculate_position_size(market_bid, portfolio_balance, config)
        
        if position_size < 5:
            return []  # Too small to be worth it
        
        # 7. Generate sell signal
        direction = "Above" if is_above else "Below"
        logger.info(
            f"[Condor] 🦅 BRACKET SELL: {symbol} | "
            f"{direction} {strike_value} @ {market_bid:.2f} | "
            f"Expected: {config.expected_value} | "
            f"Qty: {position_size}"
        )
        
        sig = TradeSignal(
            symbol=symbol,
            side="sell",  # Selling YES contracts
            quantity=position_size,
            limit_price=market_bid,
            confidence=0.75  # Moderate confidence for bracket trades
        )
        
        # Set stop loss at higher price (if market moves against us)
        sig.stop_loss = min(0.95, market_bid + 0.20)
        
        signals.append(sig)
        
        return signals
    
    def _analyze_mock(self, market_data):
        return []


# ==============================================================================
# WEATHER BRACKET STRATEGY (Specialized)
# ==============================================================================

class WeatherBracketStrategy(Strategy):
    """
    Weather Bracket Strategy 🌡️📊
    
    Specialized bracket strategy for Kalshi weather markets.
    Sells both "Above High Strike" and "Below Low Strike" contracts
    when forecast indicates temperature will land in the middle.
    
    Example:
    - Forecast: 75°F
    - Sell "Above 85°F" YES contracts (temp unlikely to hit 85)
    - Sell "Below 65°F" YES contracts (temp unlikely to drop to 65)
    """
    
    def __init__(self,
                 buffer_degrees: float = 8.0,  # Distance from forecast to sell zone
                 min_premium: float = 0.08,
                 max_contracts: int = 30):
        
        self.buffer_degrees = buffer_degrees
        self.min_premium = min_premium
        self.max_contracts = max_contracts
        
    def name(self) -> str:
        return "Weather Condor 🌡️🦅"
    
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        symbol = market_data.symbol
        
        # Must be weather data with forecast
        if extra.get('source') != 'live_nws':
            return []
        
        forecasts = extra.get('forecast')
        if not forecasts:
            return []
        
        # Get forecast high
        target_period = next((p for p in forecasts if p.get('isDaytime')), None)
        if not target_period:
            return []
        
        forecast_high = target_period.get('temperature')
        if not forecast_high:
            return []
        
        # Parse symbol for strike
        try:
            parts = symbol.split('-')
            strike_str = parts[-1]
            
            is_above = not strike_str.startswith('B')
            strike_val = float(re.sub(r'[A-Za-z]', '', strike_str))
        except:
            return []
        
        market_bid = market_data.bid
        
        # Check if this is an outer strike we can sell
        if is_above:
            # Sell "Above X" if forecast is well below X
            if strike_val > (forecast_high + self.buffer_degrees):
                if market_bid >= self.min_premium:
                    logger.info(
                        f"[WeatherCondor] 🦅 SELL ABOVE: {symbol} | "
                        f"Strike {strike_val}°F > Forecast {forecast_high}°F + {self.buffer_degrees}°"
                    )
                    signals.append(TradeSignal(
                        symbol=symbol,
                        side="sell",
                        quantity=self.max_contracts,
                        limit_price=market_bid,
                        confidence=0.80
                    ))
        else:
            # Sell "Below Y" if forecast is well above Y
            if strike_val < (forecast_high - self.buffer_degrees):
                if market_bid >= self.min_premium:
                    logger.info(
                        f"[WeatherCondor] 🦅 SELL BELOW: {symbol} | "
                        f"Strike {strike_val}°F < Forecast {forecast_high}°F - {self.buffer_degrees}°"
                    )
                    signals.append(TradeSignal(
                        symbol=symbol,
                        side="sell",
                        quantity=self.max_contracts,
                        limit_price=market_bid,
                        confidence=0.80
                    ))
        
        return signals
    
    def _analyze_mock(self, market_data):
        return []
