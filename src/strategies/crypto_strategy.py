from src.core.interfaces import Strategy, MarketData, TradeSignal
from typing import List, Optional
from datetime import datetime, timedelta
import numpy as np
import os
import joblib
import pandas as pd
from src.utils.logger import logger
from collections import deque
import re


# ==============================================================================
# TECHNICAL INDICATOR HELPERS
# ==============================================================================

def calculate_ema(prices: List[float], period: int) -> float:
    """Calculate Exponential Moving Average for the given period."""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0.0
    
    multiplier = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """
    Calculate Relative Strength Index.
    Returns: RSI value between 0-100
    """
    if len(prices) < period + 1:
        return 50.0  # Neutral when insufficient data
    
    # Calculate price changes
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    # Separate gains and losses
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    Calculate MACD (Moving Average Convergence Divergence).
    Returns: (macd_line, signal_line, histogram)
    """
    if len(prices) < slow + signal:
        return (0.0, 0.0, 0.0)
    
    # Calculate EMAs
    fast_ema = calculate_ema(prices[-fast:], fast)
    slow_ema = calculate_ema(prices[-slow:], slow)
    
    macd_line = fast_ema - slow_ema
    
    # For signal line, we need historical MACD values
    # Simplified: use current MACD as proxy
    signal_line = calculate_ema(prices[-signal:], signal) - calculate_ema(prices[-slow:], slow)
    
    histogram = macd_line - signal_line
    
    return (macd_line, signal_line, histogram)


# ==============================================================================
# MOMENTUM CONFIRMATION CLASS
# ==============================================================================

class MomentumConfirmation:
    """
    Provides technical confirmation for trade signals using RSI and MACD.
    """
    
    def __init__(self, 
                 rsi_overbought: float = 75.0,
                 rsi_oversold: float = 25.0,
                 rsi_period: int = 14):
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.rsi_period = rsi_period
        
    def should_confirm_buy(self, prices: List[float]) -> tuple:
        """
        Check if a BUY signal should be confirmed.
        Returns: (confirmed: bool, reason: str, strength: float)
        """
        rsi = calculate_rsi(prices, self.rsi_period)
        macd_line, signal_line, histogram = calculate_macd(prices)
        
        # Reject if RSI is overbought (too late to buy)
        if rsi > self.rsi_overbought:
            return (False, f"RSI overbought ({rsi:.1f})", 0.0)
        
        # Strong confirmation: RSI coming out of oversold + MACD crossing up
        if rsi < 40 and histogram > 0:
            return (True, f"RSI bullish ({rsi:.1f}) + MACD positive", 1.0)
        
        # Medium confirmation: MACD positive histogram
        if histogram > 0:
            return (True, f"MACD bullish (hist={histogram:.4f})", 0.7)
        
        # Weak confirmation: RSI neutral
        if 30 < rsi < 60:
            return (True, f"RSI neutral ({rsi:.1f})", 0.5)
        
        return (False, f"No confirmation (RSI={rsi:.1f})", 0.0)
    
    def should_confirm_sell(self, prices: List[float]) -> tuple:
        """
        Check if a SELL (short) signal should be confirmed.
        Returns: (confirmed: bool, reason: str, strength: float)
        """
        rsi = calculate_rsi(prices, self.rsi_period)
        macd_line, signal_line, histogram = calculate_macd(prices)
        
        # Reject if RSI is oversold (too late to short)
        if rsi < self.rsi_oversold:
            return (False, f"RSI oversold ({rsi:.1f})", 0.0)
        
        # Strong confirmation: RSI coming out of overbought + MACD crossing down
        if rsi > 60 and histogram < 0:
            return (True, f"RSI bearish ({rsi:.1f}) + MACD negative", 1.0)
        
        # Medium confirmation: MACD negative histogram
        if histogram < 0:
            return (True, f"MACD bearish (hist={histogram:.4f})", 0.7)
        
        # Weak confirmation: RSI neutral
        if 40 < rsi < 70:
            return (True, f"RSI neutral ({rsi:.1f})", 0.5)
        
        return (False, f"No confirmation (RSI={rsi:.1f})", 0.0)


# ==============================================================================
# ENHANCED CRYPTO STRATEGY V2
# ==============================================================================

class Crypto15mTrendStrategyV2(Strategy):
    """
    The Trend Catcher V2 📈 (15m Enhanced)
    Momentum breakout strategy with RSI/MACD confirmation and Mean Reversion fallback.
    
    Features:
    - RSI/MACD momentum confirmation before entries
    - Mean Reversion mode for ranging markets (0.45-0.55)
    - Dynamic position sizing based on signal strength
    """
    
    def __init__(self, 
                 bull_trigger: float = 0.75,
                 bear_trigger: float = 0.25,
                 stop_loss_buffer: float = 0.10,
                 trailing_trigger_delta: float = 0.15,
                 trailing_stop_delta: float = 0.05,
                 confirmation_delay: int = 120,
                 enable_mean_reversion: bool = True,
                 mean_reversion_threshold: float = 0.08,
                 trend_confirm_ticks: int = 3,
                 cooldown_seconds: int = 300):
        
        self.last_price = 0.50
        self.price_history = deque(maxlen=50)  # Option price history (for mean-reversion only)
        self.spot_price_history = deque(maxlen=50)  # BTC Spot price history (for RSI/MACD)
        
        # --- Tunable "Knobs" ---
        self.bull_trigger = bull_trigger
        self.bear_trigger = bear_trigger
        self.stop_loss_buffer = stop_loss_buffer
        self.trailing_trigger_delta = trailing_trigger_delta
        self.trailing_stop_delta = trailing_stop_delta
        self.confirmation_delay = confirmation_delay
        
        # N-Tick Trend Confirmation (anti-noise)
        self.trend_confirm_ticks = trend_confirm_ticks
        self.consecutive_above = 0  # Ticks consecutively above bull_trigger
        self.consecutive_below = 0  # Ticks consecutively below bear_trigger
        
        # Post-Trade Cooldown (anti-limit-cycle)
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_until = datetime.min
        
        # Mean Reversion Parameters
        self.enable_mean_reversion = enable_mean_reversion
        self.mean_reversion_threshold = mean_reversion_threshold  # 8% deviation from mean
        
        # Momentum Confirmation (now uses SPOT prices)
        self.momentum = MomentumConfirmation()
        
        # Fixed Cent Stop-Loss for binary options (avoids pct stops wiping out on spread)
        self.FIXED_STOP_CENTS = 0.05  # $0.05 hard floor stop for binary options
        
    def name(self) -> str:
        mode = "Trend+MR" if self.enable_mean_reversion else "Trend"
        return f"Trend Catcher V2 ({mode} | Trig:{self.bull_trigger}/{self.bear_trigger})"
    
    def _mean_reversion_signal(self, market_data: MarketData) -> Optional[TradeSignal]:
        """
        Generate mean reversion signals for ranging markets.
        Only active when price is between bear_trigger and bull_trigger.
        Requires 30+ sample history and 8%+ deviation from mean.
        """
        if len(self.price_history) < 30:
            return None
        
        # Respect cooldown
        now = market_data.timestamp or datetime.now()
        if now < self.cooldown_until:
            return None
        
        prices = list(self.price_history)
        mean_price = sum(prices) / len(prices)
        current = market_data.ask
        
        deviation = (current - mean_price) / mean_price if mean_price > 0 else 0
        
        # Only trade in ranging market
        if current >= self.bull_trigger or current <= self.bear_trigger:
            return None
        
        # Buy when price is significantly below mean
        if deviation < -self.mean_reversion_threshold:
            rsi = calculate_rsi(prices)
            if rsi < 40:  # Confirm with RSI oversold
                logger.info(f"[TrendV2] 🔄 MEAN REVERSION BUY: Price {current:.2f} < Mean {mean_price:.2f} (RSI={rsi:.1f})")
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=5,  # Smaller size for MR trades
                    limit_price=market_data.ask,
                    confidence=0.6
                )
                sig.stop_loss = market_data.ask * (1.0 - self.stop_loss_buffer)
                return sig
        
        # Sell when price is significantly above mean
        elif deviation > self.mean_reversion_threshold:
            rsi = calculate_rsi(prices)
            if rsi > 60:  # Confirm with RSI overbought
                logger.info(f"[TrendV2] 🔄 MEAN REVERSION SELL: Price {current:.2f} > Mean {mean_price:.2f} (RSI={rsi:.1f})")
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="sell",
                    quantity=5,
                    limit_price=market_data.bid,
                    confidence=0.6
                )
                sig.stop_loss = market_data.bid * (1.0 + self.stop_loss_buffer)
                return sig
        
        return None
        
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        now = market_data.timestamp or datetime.now()
        
        # --- UPDATE SPOT PRICE HISTORY (for RSI/MACD) ---
        # The spot price is the underlying BTC price from Coinbase, passed through
        # run_dashboard.py via btc_data.price. This ensures RSI is on real BTC prices
        # (~$90k range) not binary option prices (0-1 range), which caused RSI=0/100 bugs.
        spot_price = extra.get('spot_price') or market_data.price
        if spot_price and spot_price > 1.0:  # Only real spot prices (not option prob)
            self.spot_price_history.append(spot_price)
        
        # Update option price history (for mean-reversion)
        price_to_monitor = market_data.ask
        self.price_history.append(price_to_monitor)
        
        # Use spot prices for RSI/MACD if available, fallback to option prices for old behavior
        indicator_prices = list(self.spot_price_history) if len(self.spot_price_history) >= 15 else list(self.price_history)
        
        # 0.5. Strike Arbitrage (The "Strike" Arb)
        # Check if market pricing is dislocated from Spot Reality
        if "KXBTC" in market_data.symbol or "kxbtcd" in market_data.symbol:
            try:
                # Extract Strike from Ticker (Heuristic based on SimulatedExchange logic)
                # Supports both KXBTC-YYMONDD-HH00-Txxxxx and kxbtcd-YYMMMDDHH-Txxxxx
                parts = market_data.symbol.split('-')
                strike_str = parts[-1] 
                # Assuming standard Kalshi ticker format where non-B prefix means "Above"/"High"
                strike_val = float(re.sub(r'[A-Za-z]', '', strike_str))
                
                # Get Underlying Spot Price (Coinbase) directly if available in extra, else use current market price proxy?
                # market_data.price is Spot from Coinbase in run_dashboard.py
                spot_price = market_data.price 
                
                decision_buffer = 25.0 # $25 separation triggers heavy confidence
                
                # Case A: Spot is clearly ABOVE Strike (Reality = YES)
                if spot_price > (strike_val + decision_buffer):
                    # SAFETY: Ensure Strike is realistic for BTC (e.g. > 1000)
                    if strike_val < 1000:
                         # logger.warning(f"[StrikeArb] Ignoring suspicious strike value: {strike_val} from {market_data.symbol}")
                         pass
                    # Value should be high (~0.99). If Ask is cheap, Buy.
                    elif market_data.ask < 0.85 and market_data.ask > 0.01:
                         logger.info(f"[StrikeArb] 💎 ARB OPPORTUNITY: Spot ${spot_price:.2f} > Strike ${strike_val}. Ask ${market_data.ask:.2f}. BUY YES.")
                         signals.append(TradeSignal(
                             symbol=market_data.symbol, side="buy", quantity=10, 
                             limit_price=market_data.ask, confidence=0.95
                         ))
                         return signals # Priority execution
                         
                # Case B: Spot is clearly BELOW Strike (Reality = NO)
                elif spot_price < (strike_val - decision_buffer):
                     # SAFETY: Ensure Strike is realistic for BTC
                    if strike_val < 1000:
                         pass
                    # Value should be low (~0.01). If Bid is expensive, Sell.
                    elif market_data.bid > 0.15:
                         logger.info(f"[StrikeArb] 💎 ARB OPPORTUNITY: Spot ${spot_price:.2f} < Strike ${strike_val}. Bid ${market_data.bid:.2f}. SELL YES.")
                         signals.append(TradeSignal(
                             symbol=market_data.symbol, side="sell", quantity=10, 
                             limit_price=market_data.bid, confidence=0.95
                         ))
                         return signals # Priority execution
            except Exception:
                pass

        # 0. Delay Logic (Trend Confirmation at cycle start)
        minutes_into_cycle = now.minute % 15
        if minutes_into_cycle == 0 and now.second < self.confirmation_delay:
            return []
        
        # 0.5. Post-Trade Cooldown (anti-limit-cycle)
        if now < self.cooldown_until:
            return []
        
        prices = indicator_prices  # For momentum confirmation
        close_time = extra.get('close_time')
        
        # --- N-Tick Trend Confirmation Counters ---
        if price_to_monitor > self.bull_trigger:
            self.consecutive_above += 1
            self.consecutive_below = 0
        elif price_to_monitor < self.bear_trigger:
            self.consecutive_below += 1
            self.consecutive_above = 0
        else:
            # Price is in dead zone — reset both counters
            self.consecutive_above = 0
            self.consecutive_below = 0
        
        # 1. BULL BREAKOUT with N-Tick + Momentum Confirmation
        if self.consecutive_above >= self.trend_confirm_ticks:
            confirmed, reason, strength = self.momentum.should_confirm_buy(prices)
            
            if confirmed:
                logger.info(f"[TrendV2] 🚀 BULL BREAKOUT: {price_to_monitor:.2f} > {self.bull_trigger} ({self.consecutive_above} ticks) | {reason}")
                base_qty = 10
                qty = int(base_qty * (0.5 + strength * 0.5))  # Scale 5-10 based on strength
                
                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=qty,
                    limit_price=market_data.ask,
                    confidence=0.6 + (strength * 0.3)
                )
                sig.stop_loss = 0.50  # Hold to expiry, midpoint stop
                if close_time: sig.expiration_time = close_time
                signals.append(sig)

                # Activate cooldown & reset counter after signal
                self.cooldown_until = now + timedelta(seconds=self.cooldown_seconds)
                self.consecutive_above = 0
            else:
                logger.info(f"[TrendV2] ⚠️ BULL REJECTED: {reason}")

        # 2. BEAR BREAKOUT with N-Tick + Momentum Confirmation -> BUY NO
        elif self.consecutive_below >= self.trend_confirm_ticks:
            confirmed, reason, strength = self.momentum.should_confirm_sell(prices)

            if confirmed:
                logger.info(f"[TrendV2] 📉 BEAR BREAKOUT (BUY NO): {price_to_monitor:.2f} < {self.bear_trigger} ({self.consecutive_below} ticks) | {reason}")
                base_qty = 10
                qty = int(base_qty * (0.5 + strength * 0.5))

                sig = TradeSignal(
                    symbol=market_data.symbol,
                    side="buy",
                    quantity=qty,
                    limit_price=1.0 - market_data.bid,
                    confidence=0.6 + (strength * 0.3)
                )
                sig.contract_side = 'NO'
                sig.stop_loss = 0.50  # Hold to expiry, midpoint stop
                if close_time: sig.expiration_time = close_time
                signals.append(sig)

                # Activate cooldown & reset counter after signal
                self.cooldown_until = now + timedelta(seconds=self.cooldown_seconds)
                self.consecutive_below = 0
            else:
                logger.info(f"[TrendV2] ⚠️ BEAR REJECTED: {reason}")
        
        # 3. Mean Reversion Fallback (when ranging)
        elif self.enable_mean_reversion and not signals:
            mr_signal = self._mean_reversion_signal(market_data)
            if mr_signal:
                if close_time: mr_signal.expiration_time = close_time
                signals.append(mr_signal)
                # Cooldown after MR signal too
                self.cooldown_until = now + timedelta(seconds=self.cooldown_seconds)
        
        # Update State
        self.last_price = price_to_monitor
        return signals

class Crypto15mTrendStrategy(Strategy):
    """
    The Trend Catcher 📈 (15m)
    Momentum breakout strategy with tunable parameters.
    """
    def __init__(self, 
                 bull_trigger: float = 0.55,
                 bear_trigger: float = 0.45,
                 stop_loss_buffer: float = 0.10,
                 trailing_trigger_delta: float = 0.15,
                 trailing_stop_delta: float = 0.05,
                 confirmation_delay: int = 120):
        
        self.last_price = 0.50 
        
        # --- Tunable "Knobs" ---
        self.bull_trigger = bull_trigger
        self.bear_trigger = bear_trigger
        self.stop_loss_buffer = stop_loss_buffer         # e.g. 0.10 means 10% below entry
        self.trailing_trigger_delta = trailing_trigger_delta # e.g. 0.15 means activate if price moves 15c in favor
        self.trailing_stop_delta = trailing_stop_delta       # e.g. 0.05 means trail 5c behind peak
        self.confirmation_delay = confirmation_delay
        
    def name(self) -> str:
        return f"Trend Catcher (Trig:{self.bull_trigger}/{self.bear_trigger} | Delay:{self.confirmation_delay}s)"
        
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        now = market_data.timestamp or datetime.now() # Use data timestamp if replay
        
        # 0. Delay Logic (Trend Confirmation)
        # 15m cycles start at :00, :15, :30, :45
        minutes_into_cycle = now.minute % 15
        if minutes_into_cycle == 0 and now.second < self.confirmation_delay:
            return []
        
        price_to_monitor = market_data.ask
        close_time = extra.get('close_time')
        
        # 1. BULL BREAKOUT (YES > Bull Trigger)
        if price_to_monitor > self.bull_trigger and self.last_price <= self.bull_trigger:
            # logger.info(f"[Trend] 🚀 BULL BREAKOUT: Price {price_to_monitor:.2f} > {self.bull_trigger}")
            sig = TradeSignal(
                symbol=market_data.symbol,
                side="buy", 
                quantity=10,
                limit_price=market_data.ask,
                confidence=0.8
            )
            # Dynamic SL: Entry * (1 - buffer)
            sig.stop_loss = market_data.ask * (1.0 - self.stop_loss_buffer)
            # Trailing: Activate if price > Entry + Delta, New SL = Price - Delta
            trig_price = market_data.ask + self.trailing_trigger_delta
            new_sl = trig_price - self.trailing_stop_delta
            
            sig.trailing_rules = {'trigger': trig_price, 'new_sl': new_sl}
            if close_time: sig.expiration_time = close_time
            signals.append(sig)
            
        # 2. BEAR BREAKOUT (YES < Bear Trigger) -> Betting NO
        elif price_to_monitor < self.bear_trigger and self.last_price >= self.bear_trigger:
            # logger.info(f"[Trend] 📉 BEAR BREAKOUT: Price {price_to_monitor:.2f} < {self.bear_trigger}")
            sig = TradeSignal(
                symbol=market_data.symbol,
                side="sell", # Short YES (Betting NO)
                quantity=10,
                limit_price=market_data.bid,
                confidence=0.8
            )
            # Short SL: Entry * (1 + buffer)
            sig.stop_loss = market_data.bid * (1.0 + self.stop_loss_buffer)
            
            # Trailing Short: Activate if price < Entry - Delta
            trig_price = market_data.bid - self.trailing_trigger_delta
            new_sl = trig_price + self.trailing_stop_delta # Trail above price
            
            sig.trailing_rules = {'trigger': trig_price, 'new_sl': new_sl}
            if close_time: sig.expiration_time = close_time
            signals.append(sig)
            
        # Update State
        self.last_price = price_to_monitor
        return signals

class CryptoHourlyStrategy(Strategy):
    """
    The Time Traveler ⏳ (Hourly Prediction)
    Uses Linear Regression (20m window) to predict Top-of-Hour Price.
    """
    def __init__(self, confidence_margin: float = 50.0):
        self.confidence_margin = confidence_margin # $50 safety buffer
        self.price_history = [] 
        self.window_minutes = 20
        
    def name(self) -> str:
        return "The Time Traveler (Hourly)"

    def _predict_future_price(self, current_time: datetime, target_time: datetime) -> float:
        """
        Fits a linear trend to the history and extrapolates to target_time.
        """
        if len(self.price_history) < 10: return None
        
        # 1. Convert history to X (seconds from start) and Y (price)
        start_time = self.price_history[0][0]
        X = np.array([(t - start_time).total_seconds() for t, p in self.price_history])
        Y = np.array([p for t, p in self.price_history])
        
        # 2. Fit Linear Regression (Degree 1)
        try:
            slope, intercept = np.polyfit(X, Y, 1)
        except:
            return None
            
        # 3. Extrapolate
        target_seconds = (target_time - start_time).total_seconds()
        predicted_price = (slope * target_seconds) + intercept
        
        # Debug Log (Every now and then)
        if len(self.price_history) % 5 == 0:
            logger.info(f"[Hourly] Trend: Slope={slope:.4f}/sec | Current=${Y[-1]:.2f} -> Pred=${predicted_price:.2f}")
            
        return predicted_price

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        now = sorted(self.price_history)[-1][0] if self.price_history else datetime.now()
        
        # 1. Handle Spot Price Updates (Coinbase)
        if "Coinbase" in market_data.symbol or extra.get('source') == 'live_coinbase':
            spot_price = market_data.price
            now = datetime.now()
            
            # Maintain 20m Rolling Window
            self.price_history.append((now, spot_price))
            cutoff = now - timedelta(minutes=self.window_minutes)
            self.price_history = [x for x in self.price_history if x[0] > cutoff]
            return [] # No direct trades on Spot tick
            
        # 2. Handle Kalshi Market Updates (KXBTC Hourly)
        # Valid Market Check (Must be KXBTC/KXBTCD Hourly)
        symbol = market_data.symbol
        if ("KXBTC" not in symbol and "kxbtcd" not in symbol) or "15M" in symbol: 
            return [] 

        # Need Spot History to trade
        if not self.price_history: 
            return []
            
        current_spot = self.price_history[-1][1]
            
        # Extract Strike
        try:
            parts = symbol.split('-')
            # KXBTC-26JAN31-1800-T98000
            strike_part = parts[-1] 
            strike_val = float(re.sub(r'[A-Za-z]', '', strike_part))
            
            # --- FILTER: Relevance Check ---
            # Only trade markers within $750 of spot
            dist = abs(strike_val - current_spot)
            if dist > 750:
                return []
            
            # Target Time: The Hour in the ticker (1800 -> 18:00)
            target_time = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            
        except Exception as e:
            return []

        # Predict
        predicted_price = self._predict_future_price(datetime.now(), target_time)
        if not predicted_price: return []

        # Decision Logic (Arbitrage)
        # Compare Prediction vs Strike vs Market Probability
        
        # Case A: We predict HIGHER than Strike (Bullish for YES)
        if predicted_price > (strike_val + self.confidence_margin):
            # If Market Price is LOW (e.g. < 0.85), it's a steal.
            # Buying YES means we believe Price > Strike.
            if market_data.ask < 0.85 and market_data.ask > 0:
                 logger.info(f"[Hourly] 🚀 BULL SIGNAL: Pred ${predicted_price:.2f} > Strike ${strike_val} (Spot ${current_spot:.2f}). Ask {market_data.ask:.2f}. BUY YES.")
                 signals.append(TradeSignal(
                    symbol=symbol,
                    side="buy", # Buy YES
                    quantity=5, # Conservative size
                    limit_price=market_data.ask,
                    confidence=0.8
                ))
                
        # Case B: We predict LOWER than Strike (Bearish for YES)
        elif predicted_price < (strike_val - self.confidence_margin):
            # If Market Price is HIGH (e.g. > 0.15), we short it.
            # Selling YES (Buying NO) means we believe Price < Strike.
            if market_data.bid > 0.15 and market_data.bid < 1.0:
                 logger.info(f"[Hourly] 📉 BEAR SIGNAL: Pred ${predicted_price:.2f} < Strike ${strike_val}. Market Bid {market_data.bid:.2f}. SELL YES.")
                 signals.append(TradeSignal(
                    symbol=symbol,
                    side="sell", # Sell YES
                    quantity=5,
                    limit_price=market_data.bid,
                    confidence=0.8
                ))
        
        return signals

# ==============================================================================
# ENHANCED CRYPTO STRATEGIES V3 (PRD v1.5)
# ==============================================================================

class Crypto15mTrendStrategyV3(Strategy):
    """
    The Trend Catcher V3 📈 (15m High-Frequency)
    Features: Reciprocal math, BRTI 60s MA targeting, and OBI triggers.
    """
    def __init__(self, 
                 obi_threshold: float = 0.70,
                 confirmation_delay: int = 120,
                 window_seconds: int = 60):
        self.obi_threshold = obi_threshold
        self.confirmation_delay = confirmation_delay
        self.window_seconds = window_seconds
        
        self.spot_price_history = deque(maxlen=window_seconds * 2) # Store timestamped prices
        self.FIXED_STOP_CENTS = 0.05
    
    def name(self) -> str:
        return f"Trend Catcher V3 (15m | OBI>{self.obi_threshold})"
        
    def _calculate_60s_brti_ma(self, now: datetime) -> Optional[float]:
        # Filter prices within the last window_seconds
        cutoff = now - timedelta(seconds=self.window_seconds)
        recent_prices = [p for t, p in self.spot_price_history if t > cutoff]
        if not recent_prices: return None
        return sum(recent_prices) / len(recent_prices)
        
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        now = market_data.timestamp or datetime.now()
        
        spot_price = extra.get('spot_price') or market_data.price
        if spot_price and spot_price > 1.0:
            self.spot_price_history.append((now, spot_price))
            
        # 0. Delay Logic
        minutes_into_cycle = now.minute % 15
        if minutes_into_cycle == 0 and now.second < self.confirmation_delay:
            return []
            
        # Reciprocal Math
        no_bid = extra.get('no_bid', 0.0)
        no_ask = extra.get('no_ask', 0.0)
        yes_bid = market_data.bid
        yes_ask = market_data.ask
        
        implied_yes_ask = 1.0 - no_bid if no_bid > 0 else yes_ask
        implied_no_ask = 1.0 - yes_bid if yes_bid > 0 else no_ask
        
        # OBI (Order Book Imbalance)
        # Using a price-derived proxy
        obi_yes = (yes_bid / (yes_bid + implied_yes_ask)) if (yes_bid + implied_yes_ask) > 0 else 0.5
        obi_no = (no_bid / (no_bid + implied_no_ask)) if (no_bid + implied_no_ask) > 0 else 0.5

        # Cross-Spread Arbitrage 
        if yes_ask > 0 and no_ask > 0 and (yes_ask + no_ask < 1.0):
            logger.info(f"[TrendV3] 💎 RISK-FREE ARB: YesAsk={yes_ask:.2f} + NoAsk={no_ask:.2f} < 1.0")
            # In a full arb, we would fire simultaneous signals.
        
        # BRTI Target
        brti_ma = self._calculate_60s_brti_ma(now)
        if not brti_ma: return []

        try:
            parts = market_data.symbol.split('-')
            strike_val = float(re.sub(r'[A-Za-z]', '', parts[-1]))
        except:
            return []

        close_time = extra.get('close_time')

        # Momentum Breakout using BRTI MA explicitly
        if brti_ma > strike_val + 25.0: # Strongly above strike
            if obi_yes > self.obi_threshold and implied_yes_ask < 0.85:
                logger.info(f"[TrendV3] 🚀 BULL SIGNAL (BRTI MA: {brti_ma:.2f} > {strike_val}): OBI={obi_yes:.2f}. Ask={implied_yes_ask:.2f}.")
                sig = TradeSignal(symbol=market_data.symbol, side="buy", quantity=10, limit_price=implied_yes_ask, confidence=0.8)
                sig.stop_loss = implied_yes_ask - self.FIXED_STOP_CENTS
                sig.trailing_rules = {'trigger': implied_yes_ask + 0.10, 'new_sl': implied_yes_ask + 0.05}
                if close_time: sig.expiration_time = close_time
                signals.append(sig)
        elif brti_ma < strike_val - 25.0:
            if obi_no > self.obi_threshold and yes_bid > 0.15:
                no_ask_price = extra.get('no_ask', implied_no_ask)
                logger.info(f"[TrendV3] 📉 BEAR SIGNAL (BUY NO, BRTI MA: {brti_ma:.2f} < {strike_val}): OBI NO={obi_no:.2f}. NO Ask={no_ask_price:.2f}.")
                sig = TradeSignal(symbol=market_data.symbol, side="buy", quantity=10, limit_price=no_ask_price, confidence=0.8)
                sig.stop_loss = no_ask_price - self.FIXED_STOP_CENTS
                sig.trailing_rules = {'trigger': no_ask_price + 0.10, 'new_sl': no_ask_price + 0.05}
                sig.contract_side = 'NO'
                if close_time: sig.expiration_time = close_time
                signals.append(sig)

        return signals

class CryptoHourlyStrategyV3(Strategy):
    """
    The Time Traveler V3 ⏳ (Hourly Prediction)
    Targeting 60s BRTI MA, using Reciprocal Math and OBI triggers.
    """
    def __init__(self, confidence_margin: float = 50.0, obi_threshold: float = 0.70):
        self.confidence_margin = confidence_margin
        self.obi_threshold = obi_threshold
        self.price_history = [] 
        self.window_minutes = 20
        self.FIXED_STOP_CENTS = 0.05
        
    def name(self) -> str:
        return f"The Time Traveler V3 (Hourly | OBI>{self.obi_threshold})"

    def _predict_future_price(self, current_time: datetime, target_time: datetime) -> float:
        if len(self.price_history) < 10: return None
        start_time = self.price_history[0][0]
        X = np.array([(t - start_time).total_seconds() for t, p in self.price_history])
        Y = np.array([p for t, p in self.price_history])
        try:
            slope, intercept = np.polyfit(X, Y, 1)
            target_seconds = (target_time - start_time).total_seconds()
            predicted_price = (slope * target_seconds) + intercept
            return predicted_price
        except:
            return None

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        now = sorted(self.price_history)[-1][0] if self.price_history else datetime.now()
        
        if "Coinbase" in market_data.symbol or extra.get('source') == 'live_coinbase':
            spot_price = market_data.price
            now = datetime.now()
            self.price_history.append((now, spot_price))
            cutoff = now - timedelta(minutes=self.window_minutes)
            self.price_history = [x for x in self.price_history if x[0] > cutoff]
            return []
            
        symbol = market_data.symbol
        if ("KXBTC" not in symbol and "kxbtcd" not in symbol) or "15M" in symbol: 
            return [] 

        if not self.price_history: return []

        # Time window filter: first 15 min, mid-hour 25-35, last 15 min
        minute = datetime.now().minute
        if not (minute < 15 or (25 <= minute <= 35) or minute >= 45):
            return []

        current_spot = self.price_history[-1][1]

        try:
            parts = symbol.split('-')
            strike_val = float(re.sub(r'[A-Za-z]', '', parts[-1]))
            dist = abs(strike_val - current_spot)
            if dist > 750: return []
            target_time = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        except:
            return []

        predicted_price = self._predict_future_price(datetime.now(), target_time)
        if not predicted_price: return []

        # Reciprocal Math
        no_bid = extra.get('no_bid', 0.0)
        yes_bid = market_data.bid
        implied_yes_ask = 1.0 - no_bid if no_bid > 0 else market_data.ask

        # Synthetic OBI 
        obi_yes = (yes_bid / (yes_bid + implied_yes_ask)) if (yes_bid + implied_yes_ask) > 0 else 0.5
        no_ask_proxy = 1.0 - yes_bid
        obi_no = (no_bid / (no_bid + no_ask_proxy)) if (no_bid + no_ask_proxy) > 0 else 0.5
        
        close_time = extra.get('close_time')

        if predicted_price > (strike_val + self.confidence_margin):
            if obi_yes > self.obi_threshold and implied_yes_ask < 0.85 and implied_yes_ask > 0:
                 logger.info(f"[HourlyV3] 🚀 BULL SIGNAL: Pred ${predicted_price:.2f} > Strike ${strike_val}. OBI: {obi_yes:.2f}, Ask {implied_yes_ask:.2f}.")
                 sig = TradeSignal(symbol=symbol, side="buy", quantity=5, limit_price=implied_yes_ask, confidence=0.8)
                 sig.stop_loss = 0.50  # Hold through hour
                 if close_time: sig.expiration_time = close_time
                 signals.append(sig)

        elif predicted_price < (strike_val - self.confidence_margin):
            if obi_no > self.obi_threshold and yes_bid > 0.15 and yes_bid < 1.0:
                 no_ask = extra.get('no_ask', 1.0 - yes_bid)
                 logger.info(f"[HourlyV3] 📉 BEAR SIGNAL (BUY NO): Pred ${predicted_price:.2f} < Strike ${strike_val}. OBI NO: {obi_no:.2f}, NO Ask {no_ask:.2f}.")
                 sig = TradeSignal(symbol=symbol, side="buy", quantity=5, limit_price=no_ask, confidence=0.8)
                 sig.stop_loss = 0.50  # Hold through hour
                 sig.contract_side = 'NO'
                 if close_time: sig.expiration_time = close_time
                 signals.append(sig)
        return signals

class CryptoArbitrageStrategy(Strategy):
    """
    The Satoshi Arbitrageur ₿ (ML-Enhanced - 15m)
    Uses a Random Forest to predict price direction.
    """
    
    def __init__(self, threshold: float = 0.501):
        # Threshold is now "Probability Confidence" (0.5 to 1.0)
        self.threshold = threshold
        self.price_history = [] 
        self.window_size = 25 # Increased to 25 to avoid NaNs in SMA_20 and ROC_5
        
        # Load Model
        self.model = None
        model_path = os.path.join("models", "crypto_rf.pkl")
        if os.path.exists(model_path):
            try:
                self.model = joblib.load(model_path)
                logger.info("[Strategy] [ML] ML Model Loaded Successfully.")
            except Exception as e:
                logger.error(f"[Strategy] Failed to load ML model: {e}")
        else:
            logger.warning("[Strategy] No ML Model found. Using heuristic fallback.")
        
    def name(self) -> str:
        mode = "ML" if self.model else "Heuristic"
        return f"The Satoshi Arbitrageur ({mode})"
        
    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        extra = market_data.extra
        
        if extra.get('source') != 'live_coinbase': return []

        spot_price = market_data.price
        now = datetime.now()
        
        self.price_history.append((now, spot_price))
        if len(self.price_history) > self.window_size + 5: # Need slightly more history for ROC
            self.price_history.pop(0)
            
        # Warm Start Logic: Fill buffer if we have at least one point
        if len(self.price_history) < self.window_size:
            logger.info(f"[Strategy] ⚡ Warm Starting Crypto Buffer ({len(self.price_history)}/{self.window_size})...")
            while len(self.price_history) < self.window_size:
                self.price_history.insert(0, (now, spot_price))

        # Feature Engineering (Must match training!)
        prices = pd.Series([p for t, p in self.price_history])
        
        sma_3 = prices.rolling(window=3).mean().iloc[-1]
        sma_20 = prices.rolling(window=20).mean().iloc[-1]
        momentum = sma_3 - sma_20
        volatility = prices.rolling(window=20).std().iloc[-1]
        roc = prices.pct_change(periods=5).iloc[-1] # Rate of Change (5 ticks)
        
        # NaNs check
        if np.isnan(momentum) or np.isnan(volatility) or np.isnan(roc):
            logger.warning(f"[Strategy] ⚠️ NaN Features Detected. Momentum: {momentum}, Vol: {volatility}, ROC: {roc}")
            return []

        # Feature Summary for Debug
        logger.info(f"[Strategy] [ML] Features -> Mom: {momentum:.4f}, Vol: {volatility:.4f}, ROC: {roc:.6f}")

        # Inference
        prediction = 0
        probability = 0.0
        
        if self.model:
            # Create feature vector
            # ['Momentum', 'Volatility', 'ROC']
            X = pd.DataFrame([[momentum, volatility, roc]], columns=['Momentum', 'Volatility', 'ROC'])
            prediction = self.model.predict(X)[0] # 0 or 1
            probability = self.model.predict_proba(X)[0][1] # Probability of Class 1 (Up)
            
            logger.info(f"[Strategy] [ML] Prob(UP)={probability:.4f} | Target: >{self.threshold} OR <{1.0-self.threshold}")
        else:
            # Fallback Heuristic
            # Normalize momentum roughly to probability
            probability = 0.5 + (momentum / 100)
            probability = max(0.0, min(1.0, probability))
            logger.info(f"[Strategy] [Heuristic] Prob(UP)={probability:.4f}")
            
        # Target Generation (15m Market & Hourly)
        # 1. 15-Minute Synthetic / Real
        next_interval_min = (now.minute // 15 + 1) * 15
        if next_interval_min == 60:
            target_time = (now + timedelta(hours=1)).replace(minute=0, second=0)
        else:
            target_time = now.replace(minute=next_interval_min, second=0)
        time_str_15m = target_time.strftime("%H%M")
        
        # 2. Hourly Real (KXBTC / kxbtcd)
        # Production Series: KXBTC (Legacy) or kxbtcd (New)
        # New Format: kxbtcd-YYMMMDDHH
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0)
        
        # We need to construct the NEW format as it's likely the only one working?
        # kxbtcd-26feb1623
        yy = next_hour.strftime("%y")
        mmm = next_hour.strftime("%b").lower()
        dd = next_hour.strftime("%d")
        hh = next_hour.strftime("%H")
        
        real_ticker_base = f"kxbtcd-{yy}{mmm}{dd}{hh}"
        
        strike_price = round(spot_price / 100) * 100
        
        # Decision Logic
        # If Probability > Threshold -> BUY YES (Bet UP)
        if probability > self.threshold:
             # Check for real market price (Fidelity)
             if market_data.ask <= 0 or market_data.ask >= 1.0:
                 return []
                 
             logger.info(f"[Strategy] [ML] SIGNAL: UP ({probability:.2f}) @ {market_data.ask:.2f}")
             
             # Signal: Use the resolved ticker passed in market_data.symbol
             signals.append(TradeSignal(
                symbol=market_data.symbol,
                side="buy", 
                quantity=10,
                limit_price=market_data.ask,
                confidence=probability
            ))
        
        # If Probability < (1 - Threshold) -> SELL YES (Bet DOWN)
        elif probability < (1.0 - self.threshold):
             # Check for real market price (Fidelity)
             if market_data.bid <= 0 or market_data.bid >= 1.0:
                 return []
                 
             conf = 1.0 - probability
             logger.info(f"[Strategy] [ML] SIGNAL: DOWN ({conf:.2f}) @ {market_data.bid:.2f}")
             
             signals.append(TradeSignal(
                symbol=market_data.symbol,
                side="sell", 
                quantity=10,
                limit_price=market_data.bid,
                confidence=conf
            ))
        
        return signals


# ==============================================================================
# LONGSHOT FADER STRATEGY
# "The House Edge" - Research-backed Kalshi long-shot bias exploitation
# ==============================================================================
# Academic Finding: Kalshi contracts priced < $0.10 win only ~4% of the time.
# Sellers (shorters) of these contracts win ~96% of trades.
# This strategy sells low-probability contracts to collect the "optimism tax"
# from irrational bettors who overpay for long-shots.
# Source: ifo.de study + jbecker.dev analysis on Kalshi market inefficiencies

class CryptoLongShotFader(Strategy):
    """
    Fade the long-shot bias on Kalshi.
    Sells YES contracts priced below the longshot_ceiling.
    Win expectation: ~93-96% based on academic research.
    """

    def __init__(
        self,
        longshot_ceiling: float = 0.08,   # Sell anything below 8 cents
        min_price: float = 0.03,          # Don't sell sub-3c (too illiquid)
        quantity: int = 5,                # Small position — we rely on frequency
        cooldown_seconds: int = 600,      # 10 min cooldown per symbol
    ):
        self.longshot_ceiling = longshot_ceiling
        self.min_price = min_price
        self.quantity = quantity
        self.cooldown_seconds = cooldown_seconds
        # Track last trade time per symbol
        self._last_trade: dict = {}

    def name(self) -> str:
        return f"LongShot Fader (< ${self.longshot_ceiling:.2f})"

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals = []
        now = datetime.now()

        bid = market_data.bid
        if bid <= 0:
            return signals

        # --- LONGSHOT FILTER ---
        # Only fade contract where YES bid is very cheap (long-shot territory)
        if not (self.min_price <= bid <= self.longshot_ceiling):
            return signals

        # --- COOLDOWN CHECK per symbol ---
        last = self._last_trade.get(market_data.symbol, datetime.min)
        if (now - last).total_seconds() < self.cooldown_seconds:
            return signals

        # --- GENERATE SELL SIGNAL ---
        # Sell YES = bet it won't happen. Confidence derived from historical win rate
        # of fading contracts at this price level (~1.0 - bid as implied win rate)
        implied_win_rate = 1.0 - bid  # e.g. bid=0.07 → 0.93 win rate for seller
        confidence = implied_win_rate * 0.95  # Haircut for liquidity/spread risk

        sig = TradeSignal(
            symbol=market_data.symbol,
            side="sell",
            quantity=self.quantity,
            limit_price=bid,
            confidence=confidence,
        )
        # Stop loss at $0.20: caps loss at $0.16/contract for a $0.04 sell
        # (instead of $0.96/contract with no stop)
        sig.stop_loss = 0.20

        logger.info(
            f"[LongShotFader] SELL YES {market_data.symbol} @ ${bid:.3f} "
            f"(implied win: {implied_win_rate:.1%})"
        )
        self._last_trade[market_data.symbol] = now
        signals.append(sig)
        return signals
