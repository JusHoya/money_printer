from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime, date
from src.utils.logger import logger

@dataclass
class PortfolioState:
    balance: float
    daily_pnl: float
    active_positions: int
    last_trade_time: datetime

from src.core.matching_engine import SimulatedExchange

class RiskManager:
    """
    The Safety Officer 🛡️
    Enforces capital preservation rules and tracks Simulated PnL via OMS.
    """
    
    def __init__(self, starting_balance: float = 100.0):
        self.balance = starting_balance
        self.starting_balance_day = starting_balance
        # Pass callback to OMS
        self.exchange = SimulatedExchange(on_close=self._on_trade_close) 
        
        self.daily_pnl = 0.0 
        self.unrealized_pnl = 0.0 
        self.active_positions = 0
        
        self.last_trade_time = datetime.min
        self.today = date.today()
        
        # RULES
        self.MAX_RISK_PER_TRADE_PCT = 0.05 
        self.MAX_DAILY_DRAWDOWN_PCT = 0.10 
        self.MAX_PORTFOLIO_EXPOSURE_PCT = 0.50 # Max 50% of funds active at once
        self.MIN_TRADE_INTERVAL_SEC = 5 # Reduced to 5s for Ravenous Mode 

    def _on_trade_close(self, position: dict):
        """Callback from OMS when a trade is settled/closed."""
        # Sync daily_pnl from exchange (source of truth) BEFORE recalculating balance
        stats = self.exchange.get_stats()
        self.daily_pnl = stats['realized']
        self._sync_balance()
        pnl = position.get('pnl', 0.0)
        logger.info(f"[Risk] 💰 SETTLEMENT: Profit ${pnl:+.2f} -> Balance: ${self.balance:.2f}")

    def _sync_balance(self):
        """
        Calculates available cash balance based on realized PnL and current exposure.
        Formula: Starting Cash + Realized PnL - Cash tied up in open positions.
        """
        exposure = self.get_current_exposure()
        self.balance = self.starting_balance_day + self.daily_pnl - exposure

    def update_balance(self, real_balance: float):
        """Syncs simulated balance with real exchange balance."""
        # Detect if this is the initial sync
        if self.starting_balance_day == 100.0 and self.balance == 100.0 and real_balance != 100.0:
            logger.info(f"[Risk] [SYNC] Initial Sync: Balance updated to ${real_balance:.2f}")
            
        self.starting_balance_day = real_balance
        
        # CRITICAL: Reset internal PnL counters to prevent double counting
        # The 'real_balance' already includes all past PnL.
        self.daily_pnl = 0.0
        self.exchange.reset_stats() 
        
        self._sync_balance()

    def update_market_data(self, symbol: str, price: float):
        """Passes live data to OMS to update PnL."""
        self.exchange.update_market(symbol, price)
        stats = self.exchange.get_stats()
        self.daily_pnl = stats['realized']
        self.unrealized_pnl = stats['unrealized']
        
        self._sync_balance()


    def _reset_daily_stats_if_needed(self):
        if date.today() > self.today:
            self.today = date.today()
            self.daily_pnl = 0.0
            # Reset exchange realized PnL for the new day? 
            # In simulation, we usually keep cumulative, but for 'Daily' reporting:
            # Let's just update the baseline.
            self.starting_balance_day = self.balance 
            logger.info(f"[RiskManager] [NEW DAY] Daily PnL reset.")

    def calculate_kelly_size(self, confidence: float, price: float) -> int:
        """
        Calculates the optimal position size (quantity) using Fractional Kelly Criterion.
        
        Formula: f* = p - q/b
        where:
          p = probability of win (confidence)
          q = probability of loss (1 - p)
          b = odds received (Profit / Risk) = (1.00 - price) / price
          
        Applies a 25% fraction (Safety) and a hard 5% Portfolio Cap.
        """
        if price <= 0 or price >= 1.0: return 0
        
        # 1. Calculate Odds (b)
        # Risk = Price, Profit = 1.00 - Price
        b = (1.0 - price) / price
        
        # 2. Kelly Fraction (f)
        p = confidence
        q = 1.0 - p
        
        # f = p - q/b
        f = p - (q / b)
        
        # 3. Apply Fractional Multiplier (Safety)
        # User Rule: Fractional Kelly (0.25x to 0.3x)
        f_fractional = f * 0.25
        
        if f_fractional <= 0: return 0
        
        # 4. Apply Hard Cap (5% of Bankroll)
        f_capped = min(f_fractional, self.MAX_RISK_PER_TRADE_PCT)
        
        # 5. Calculate Dollar Amount
        allocation = self.balance * f_capped
        
        # 6. Convert to Quantity
        quantity = int(allocation / price)
        # Hard cap: Prevent runaway position growth
        return max(1, min(quantity, 500))

    def get_current_exposure(self, category: Optional[str] = None) -> float:
        """
        Sums the cost of active positions. 
        If category is provided, filters by symbol heuristics.
        """
        total = 0.0
        for p in self.exchange.positions:
            # Simple Heuristic for Categorization
            sym = p['symbol'].upper()
            is_crypto = "BTC" in sym or "ETH" in sym
            is_weather = "HIGH" in sym or "PRECIP" in sym or "TEMP" in sym
            
            match = False
            if category == 'crypto' and is_crypto: match = True
            elif category == 'weather' and is_weather: match = True
            elif category is None: match = True
            
            if match:
                total += p['entry_price'] * p['quantity']
        return total

    def check_order(self, proposed_cost: float, category: str = "general") -> bool:
        """
        Returns True if the order is safe to execute.
        """
        self._reset_daily_stats_if_needed()
        
        # 1. Capital Check
        if proposed_cost > self.balance:
            logger.warning(f"[Risk] [REJECT] Insufficient Funds (${self.balance:.2f} < ${proposed_cost:.2f})")
            return False
            
        # 2. Position Sizing
        # NOTE: Kelly sizing ensures we maximize growth, but we double check strict limit here.
        max_trade_size = self.balance * self.MAX_RISK_PER_TRADE_PCT
        if proposed_cost > max_trade_size + 1.0: # Allow $1 rounding buffer
            logger.warning(f"[Risk] [REJECT] Position Size Too Large (${proposed_cost:.2f} > ${max_trade_size:.2f})")
            # In simulation, we might prefer to CLAMP instead of reject, but Orchestrator handles clamping now.
            return False
            
        # 3. Drawdown Limit (Kill Switch)
        if self.daily_pnl < -(self.starting_balance_day * self.MAX_DAILY_DRAWDOWN_PCT):
            logger.warning(f"[Risk] [KILL] KILL SWITCH: Daily Drawdown Limit Hit (${self.daily_pnl:.2f})")
            return False
            
        # 4. Dynamic Exposure Limit (Smart Slots)
        current_total_exposure = self.get_current_exposure()
        max_exposure = self.balance * self.MAX_PORTFOLIO_EXPOSURE_PCT
        
        if (current_total_exposure + proposed_cost) > max_exposure:
            logger.warning(f"[Risk] [REJECT] Max Portfolio Exposure ({current_total_exposure:.2f}/{max_exposure:.2f})")
            return False

        # 5. Asset Class Buckets (Capital Velocity)
        # Weather = Low Velocity (Max 30%)
        # Crypto = High Velocity (No specific cap beyond Portfolio Max)
        if category == 'weather':
            max_weather_alloc = self.balance * 0.30
            current_weather_exp = self.get_current_exposure(category='weather')
            if (current_weather_exp + proposed_cost) > max_weather_alloc:
                logger.warning(f"[Risk] [REJECT] Max Weather Allocation Exceeded ({current_weather_exp:.2f}/{max_weather_alloc:.2f})")
                return False
            
        # 6. Rate Limiting
        seconds_since_last = (datetime.now() - self.last_trade_time).total_seconds()
        if seconds_since_last < self.MIN_TRADE_INTERVAL_SEC:
            logger.info(f"[Risk] [WAIT] Rate Limit ({seconds_since_last:.1f}s < {self.MIN_TRADE_INTERVAL_SEC}s)")
            return False
            
        return True

    def record_execution(self, cost: float, symbol: str, side: str, quantity: int, price: float, stop_loss: float = 0.0, trailing_rules: dict = None, expiration_time: any = None):
        """Call this AFTER a trade is executed."""
        # OMS HANDOFF
        # Use exact quantity and price from the signal
        self.exchange.open_position(symbol, side, price, quantity, stop_loss=stop_loss, trailing_rules=trailing_rules, expiration_time=expiration_time)
        
        self._sync_balance()
        self.last_trade_time = datetime.now()
        self.active_positions = len(self.exchange.positions)
        logger.info(f"[Risk] [OK] Trade Recorded. New Balance: ${self.balance:.2f}")

    def record_pnl(self, pnl: float):
        """Manual PnL injection (not typically used with OMS)."""
        # If we use this, we'd need to update realized pnl in exchange or baseline
        self.starting_balance_day += pnl
        self._sync_balance()