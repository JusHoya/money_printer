from typing import List, Dict, Optional, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import re
import math
from src.utils.logger import logger


# ==============================================================================
# ORDER BOOK & LIMIT ORDER SUPPORT
# ==============================================================================

class OrderStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class LimitOrder:
    """Represents a limit order waiting to be filled."""
    order_id: int
    symbol: str
    side: str  # 'buy' or 'sell'
    limit_price: float
    quantity: int
    created_at: datetime
    expires_at: datetime
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_at: Optional[datetime] = None
    stop_loss: float = 0.0
    trailing_rules: Optional[dict] = None
    expiration_time: Optional[datetime] = None  # Contract expiration


@dataclass
class OrderBookLevel:
    """A single price level in the order book."""
    price: float
    quantity: int
    order_count: int = 1


class LimitOrderBook:
    """
    Manages limit orders and simulates order book depth tracking.
    
    Features:
    - Limit order placement with patience timeout
    - Order book depth simulation
    - Spread analysis for market-making opportunities
    - Fill simulation based on price movement
    """
    
    def __init__(self, 
                 on_fill: Optional[Callable] = None,
                 default_patience_seconds: int = 30):
        self.pending_orders: Dict[int, LimitOrder] = {}
        self.filled_orders: List[LimitOrder] = []
        self.cancelled_orders: List[LimitOrder] = []
        self.next_order_id = 1
        self.on_fill = on_fill
        self.default_patience = default_patience_seconds
        
        # Simulated order book state (for depth tracking)
        self.order_books: Dict[str, Dict] = {}  # {symbol: {'bids': [], 'asks': []}}
        
    def place_limit_order(self,
                          symbol: str,
                          side: str,
                          limit_price: float,
                          quantity: int,
                          patience_seconds: Optional[int] = None,
                          stop_loss: float = 0.0,
                          trailing_rules: Optional[dict] = None,
                          contract_expiration: Optional[datetime] = None) -> LimitOrder:
        """
        Place a limit order with patience timeout.
        
        :param patience_seconds: How long to wait for fill before cancelling
        :returns: The created LimitOrder
        """
        patience = patience_seconds or self.default_patience
        now = datetime.now()
        
        order = LimitOrder(
            order_id=self.next_order_id,
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            quantity=quantity,
            created_at=now,
            expires_at=now + timedelta(seconds=patience),
            stop_loss=stop_loss,
            trailing_rules=trailing_rules,
            expiration_time=contract_expiration
        )
        
        self.pending_orders[order.order_id] = order
        self.next_order_id += 1
        
        logger.info(f"[OrderBook] 📋 LIMIT ORDER #{order.order_id}: {side.upper()} {quantity}x {symbol} @ {limit_price:.2f} (patience: {patience}s)")
        
        return order
    
    def update_order_book(self, symbol: str, bids: List[tuple], asks: List[tuple]):
        """
        Update the simulated order book for a symbol.
        
        :param bids: List of (price, quantity) tuples, sorted descending
        :param asks: List of (price, quantity) tuples, sorted ascending
        """
        self.order_books[symbol] = {
            'bids': [OrderBookLevel(price=p, quantity=q) for p, q in bids],
            'asks': [OrderBookLevel(price=p, quantity=q) for p, q in asks],
            'updated_at': datetime.now()
        }
    
    def get_spread_info(self, symbol: str) -> Optional[Dict]:
        """
        Get spread information for a symbol.
        
        Returns: {
            'bid': best_bid,
            'ask': best_ask,
            'spread': spread,
            'spread_pct': spread_percentage,
            'mid': midpoint
        }
        """
        if symbol not in self.order_books:
            return None
        
        book = self.order_books[symbol]
        if not book['bids'] or not book['asks']:
            return None
        
        best_bid = book['bids'][0].price
        best_ask = book['asks'][0].price
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2
        spread_pct = (spread / mid) * 100 if mid > 0 else 0
        
        return {
            'bid': best_bid,
            'ask': best_ask,
            'spread': spread,
            'spread_pct': spread_pct,
            'mid': mid,
            'bid_depth': sum(lvl.quantity for lvl in book['bids'][:3]),
            'ask_depth': sum(lvl.quantity for lvl in book['asks'][:3])
        }
    
    def check_fills(self, symbol: str, current_bid: float, current_ask: float) -> List[LimitOrder]:
        """
        Check if any pending orders can be filled at current prices.
        
        :returns: List of orders that were just filled
        """
        now = datetime.now()
        newly_filled = []
        
        for order_id, order in list(self.pending_orders.items()):
            if order.symbol != symbol:
                continue
            
            # Check expiration
            if now >= order.expires_at:
                order.status = OrderStatus.EXPIRED
                self.cancelled_orders.append(order)
                del self.pending_orders[order_id]
                logger.info(f"[OrderBook] ⏰ EXPIRED: Order #{order_id} (patience exceeded)")
                continue
            
            # Check fill conditions
            filled = False
            fill_price = 0.0
            
            if order.side == 'buy':
                # Buy limit fills if ask drops to or below limit price
                if current_ask <= order.limit_price:
                    filled = True
                    fill_price = current_ask
            else:  # sell
                # Sell limit fills if bid rises to or at/above limit price
                if current_bid >= order.limit_price:
                    filled = True
                    fill_price = current_bid
            
            if filled:
                order.status = OrderStatus.FILLED
                order.filled_price = fill_price
                order.filled_at = now
                newly_filled.append(order)
                self.filled_orders.append(order)
                del self.pending_orders[order_id]
                
                logger.info(f"[OrderBook] ✅ FILLED: Order #{order_id} @ {fill_price:.2f} (limit was {order.limit_price:.2f})")
                
                if self.on_fill:
                    self.on_fill(order)
        
        return newly_filled
    
    def cancel_order(self, order_id: int) -> bool:
        """Cancel a pending order."""
        if order_id in self.pending_orders:
            order = self.pending_orders[order_id]
            order.status = OrderStatus.CANCELLED
            self.cancelled_orders.append(order)
            del self.pending_orders[order_id]
            logger.info(f"[OrderBook] ❌ CANCELLED: Order #{order_id}")
            return True
        return False
    
    def cancel_all_for_symbol(self, symbol: str) -> int:
        """Cancel all pending orders for a symbol."""
        cancelled = 0
        for order_id in list(self.pending_orders.keys()):
            if self.pending_orders[order_id].symbol == symbol:
                self.cancel_order(order_id)
                cancelled += 1
        return cancelled
    
    def get_pending_count(self) -> int:
        return len(self.pending_orders)
    
    def get_stats(self) -> Dict:
        return {
            'pending': len(self.pending_orders),
            'filled': len(self.filled_orders),
            'cancelled': len([o for o in self.cancelled_orders if o.status == OrderStatus.CANCELLED]),
            'expired': len([o for o in self.cancelled_orders if o.status == OrderStatus.EXPIRED])
        }


# ==============================================================================
# SIMULATED EXCHANGE (ENHANCED)
# ==============================================================================

class SimulatedExchange:
    """
    A lightweight Order Matching System (OMS) for paper trading.
    Tracks positions and simulates fills/exits based on live market data.
    """
    
    def __init__(self, on_close=None):
        self.positions = [] # List of active trades
        self.closed_trades = [] # History
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.on_close = on_close # Callback function(position)
        
        # Simulation Settings
        self.TAKE_PROFIT_PCT = 0.15  # +15% gain -> Close (Ravenous Mode)
        self.STOP_LOSS_PCT = 0.30    # -30% loss -> Close
        self.TIME_LIMIT_MIN = 60     # Force close after 60 mins (for hourly markets)

    def open_position(self, symbol: str, side: str, entry_price: float, quantity: int, stop_loss: float = 0.0, trailing_rules: dict = None, expiration_time: any = None, strategy_name: str = None):
        """
        Records a new position.
        :param expiration_time: ISO string or datetime of contract close.
        """
        expiry_dt = None
        if expiration_time:
            if isinstance(expiration_time, str):
                try:
                    # Handle Z suffix for ISO
                    expiry_dt = datetime.fromisoformat(expiration_time.replace('Z', '+00:00'))
                    # Convert to local if needed, but easier to keep as offset-aware
                except: expiry_dt = None
            else:
                expiry_dt = expiration_time

        position = {
            'id': len(self.positions) + len(self.closed_trades) + 1,
            'symbol': symbol,
            'side': side, 
            'entry_price': entry_price,
            'current_price': entry_price, 
            'quantity': quantity,
            'open_time': datetime.now(),
            'pnl': 0.0,
            'stop_loss': stop_loss,
            'trailing_rules': trailing_rules,
            'trailing_activated': False,
            'expiration_time': expiry_dt,
            'strategy_name': strategy_name or 'Unknown'
        }
        self.positions.append(position)
        
    def update_market(self, symbol_fragment: str, current_spot_price: float):
        """
        Updates the valuation of open positions based on the underlying spot price.
        """
        # Mapping station IDs to Ticker fragments
        symbol_map = {
            "KNYC": "NY", "KJFK": "NY", "KLAX": "LAX", "KORD": "CHI", "KMIA": "MIA", "BTC": "BTC"
        }
        
        # Determine Update Type
        update_type = "GENERIC"
        target_fragment = symbol_fragment
        
        if symbol_fragment.startswith("PRECIP_"):
            update_type = "PRECIP"
            target_fragment = symbol_map.get(symbol_fragment.replace("PRECIP_", ""), symbol_fragment)
        elif symbol_fragment.startswith("TEMP_"):
            update_type = "TEMP"
            target_fragment = symbol_map.get(symbol_fragment.replace("TEMP_", ""), symbol_fragment)
        else:
            target_fragment = symbol_map.get(symbol_fragment, symbol_fragment)
            if target_fragment in ["NY", "LAX", "CHI", "MIA"]: update_type = "TEMP"
        
        now_dt = datetime.now().astimezone() # Local aware for comparison

        for pos in self.positions[:]: 
            # --- EXPIRATION CHECK ---
            if pos.get('expiration_time'):
                exp = pos['expiration_time']
                # Ensure comparison is aware vs aware or naive vs naive
                if exp.tzinfo is None:
                    comp_now = datetime.now()
                else:
                    comp_now = datetime.now().astimezone()
                
                if comp_now >= exp:
                    self._close_position(pos, current_spot_price, reason="EXPIRATION")
                    continue

            if target_fragment not in pos['symbol']: 
                # Special Case: BTC matches kxbtcd (case mismatch / alias)
                if target_fragment == "BTC" and "kxbtcd" in pos['symbol']:
                    pass
                else: 
                    continue
            if update_type == "PRECIP" and "PRECIP" not in pos['symbol']: continue
            if update_type == "TEMP" and "TEMP" not in pos['symbol'] and "KXHIGH" not in pos['symbol']: continue

            # Check Time Limit (Legacy fallback)
            age = (datetime.now() - pos['open_time']).total_seconds() / 60
            if age >= self.TIME_LIMIT_MIN:
                # Use estimated option price, NOT raw spot price
                self._close_position(pos, pos.get('current_price', pos['entry_price']), reason="TIME_LIMIT")
                continue

            # Calculate Synthetic PnL
            try:
                estimated_price = pos['entry_price']
                
                # --- CASE 1: PRECIPITATION ---
                if "PRECIP" in pos['symbol']:
                    estimated_price = current_spot_price
                # --- CASE 2: STRIKE BASED (KXHIGH, KXBTC, kxbtcd) ---
                elif "KXHIGH" in pos['symbol'] or "KXBTC" in pos['symbol'] or "kxbtcd" in pos['symbol']:
                    try:
                        parts = pos['symbol'].split('-')
                        strike_str = parts[-1]
                        is_above = not strike_str.startswith('B')
                        strike = float(re.sub(r'[A-Za-z]', '', strike_str))
                        
                        if is_above:
                            diff = current_spot_price - strike
                        else:
                            diff = strike - current_spot_price
                        
                        # Sigmoid-like scaling using tanh for smooth probability mapping
                        # Scale factor: 10 for temp (degrees matter), 1000 for BTC (dollars matter)
                        scale = 10.0 if "KXHIGH" in pos['symbol'] else 1000.0
                        normalized_diff = diff / scale
                        # tanh maps (-inf, inf) -> (-1, 1), then scale to price range
                        probability_shift = math.tanh(normalized_diff) * 0.49
                        estimated_price = max(0.01, min(0.99, 0.50 + probability_shift))
                    except Exception as e:
                        logger.warning(f"[OMS] Price calc error for {pos['symbol']}: {e}")
                        estimated_price = pos['entry_price']

                # --- COMMON PNL CALC ---
                pos['current_price'] = estimated_price
                if pos['side'] == 'buy':
                    pos['pnl'] = (estimated_price - pos['entry_price']) * pos['quantity']
                else:
                    pos['pnl'] = (pos['entry_price'] - estimated_price) * pos['quantity']
                
                # --- EARLY SETTLEMENT (Liquidity/Heuristic) ---
                # If price is pegged at 0.99 or 0.01 for a sustained period (10m), assume market has decided.
                if age >= 10:
                    if (pos['side'] == 'buy' and estimated_price >= 0.99) or \
                       (pos['side'] == 'sell' and estimated_price <= 0.01):
                         self._close_position(pos, current_spot_price, reason="EARLY_SETTLEMENT")
                         continue
                    if (pos['side'] == 'buy' and estimated_price <= 0.01) or \
                       (pos['side'] == 'sell' and estimated_price >= 0.99):
                         self._close_position(pos, current_spot_price, reason="EARLY_SETTLEMENT")
                         continue
                
                # --- STOP LOSS / TRAILING LOGIC (Price Based) ---
                if pos['stop_loss'] > 0:
                    # 1. Check Trailing Trigger
                    if pos.get('trailing_rules') and not pos['trailing_activated']:
                        trig = pos['trailing_rules'].get('trigger', 999)
                        
                        # Trigger condition depends on side
                        activated = False
                        if pos['side'] == 'buy' and estimated_price >= trig: activated = True
                        elif pos['side'] == 'sell' and estimated_price <= trig: activated = True
                        
                        if activated:
                            new_sl = pos['trailing_rules'].get('new_sl', pos['stop_loss'])
                            pos['stop_loss'] = new_sl
                            pos['trailing_activated'] = True
                            logger.info(f"[OMS] ⛓️ Trailing Stop Activated for {pos['symbol']}: SL moved to {new_sl}")

                    # 2. Check Stop Loss Hit
                    hit = False
                    if pos['side'] == 'buy' and estimated_price <= pos['stop_loss']: hit = True
                    elif pos['side'] == 'sell' and estimated_price >= pos['stop_loss']: hit = True
                    
                    if hit:
                        self._close_position(pos, estimated_price, reason=f"STOP_LOSS_PRICE ({pos['stop_loss']})")
                        continue
                            
                # Fallback: PCT Based Stops
                pnl_pct = pos['pnl'] / (pos['entry_price'] * pos['quantity']) if pos['entry_price'] > 0 else 0
                if pnl_pct >= self.TAKE_PROFIT_PCT:
                    self._close_position(pos, estimated_price, reason="TAKE_PROFIT")
                elif pnl_pct <= -self.STOP_LOSS_PCT and pos['stop_loss'] == 0:
                    self._close_position(pos, estimated_price, reason="STOP_LOSS_PCT")
                        
            except Exception as e:
                logger.error(f"[OMS] PnL calculation error for {pos['symbol']}: {e}")
            
        self.unrealized_pnl = sum(p['pnl'] for p in self.positions)

    def _close_position(self, pos, final_spot_price, reason="MARKET"):
        """
        Simulates a closing trade.
        
        For BINARY SETTLEMENT (EXPIRATION, EARLY_SETTLEMENT):
            final_spot_price = the underlying spot value (temp, BTC$)
            exit_price is determined as 1.00 or 0.00 based on outcome.
        
        For NON-SETTLEMENT closes (TAKE_PROFIT, STOP_LOSS, TIME_LIMIT):
            final_spot_price should be the ESTIMATED OPTION PRICE (0.00-1.00),
            NOT the raw underlying spot price.
        """
        try:
            # Determine Exit Price Strategy
            is_binary_settlement = reason in ["EXPIRATION", "EARLY_SETTLEMENT"]
            
            if is_binary_settlement:
                # --- BINARY SETTLEMENT LOGIC (0.00 or 1.00) ---
                outcome_is_yes = False
                
                if "PRECIP" in pos['symbol']:
                    if final_spot_price > 0.50: outcome_is_yes = True
                else:
                    try:
                        parts = pos['symbol'].split('-')
                        strike_str = parts[-1]
                        is_above = not strike_str.startswith('B')
                        strike = float(re.sub(r'[A-Za-z]', '', strike_str))
                        
                        if is_above:
                            if final_spot_price >= strike: outcome_is_yes = True
                        else:
                            if final_spot_price <= strike: outcome_is_yes = True
                    except:
                        outcome_is_yes = False # Fail safe
                
                exit_price = 1.00 if outcome_is_yes else 0.00
            else:
                # --- NON-SETTLEMENT: Use estimated option price ---
                # final_spot_price here should already BE the estimated price
                # (passed from update_market using pos['current_price'])
                exit_price = final_spot_price
            
            # --- SANITY CHECK: Binary options must be in [0.00, 1.00] ---
            if exit_price > 1.0 or exit_price < 0.0:
                logger.error(f"[OMS] SANITY FAIL: exit_price={exit_price:.4f} for {pos['symbol']} "
                             f"(reason={reason}). Raw spot leaked! Clamping to entry_price.")
                exit_price = pos['entry_price']  # Neutral close (no PnL)
            
            # --- CALCULATE PNL ---
            if pos['side'] == 'buy':
                total_pnl = (exit_price - pos['entry_price']) * pos['quantity']
            else:
                total_pnl = (pos['entry_price'] - exit_price) * pos['quantity']
            
            pos['exit_price'] = exit_price
            pos['pnl'] = total_pnl
            pos['close_time'] = datetime.now()
            pos['reason'] = reason
            
            self.realized_pnl += total_pnl
            self.closed_trades.append(pos)
            self.positions.remove(pos)
            
            label = "WIN" if total_pnl > 0 else "LOSS"
            # Explicit Log for User Clarity
            logger.info(f"[OMS] 🔨 CLOSED {pos['symbol']} ({reason})")
            logger.info(f"      Entry: ${pos['entry_price']:.2f} | Exit: ${exit_price:.2f} | Qty: {pos['quantity']}")
            logger.info(f"      Realized PnL: ${total_pnl:+.2f} ({label})")
            
            if self.on_close:
                self.on_close(pos)
            
        except Exception as e:
            logger.error(f"[OMS] Error closing position: {e}")

    def get_stats(self):
        return {
            'realized': self.realized_pnl,
            'unrealized': self.unrealized_pnl,
            'open_count': len(self.positions)
        }

    def reset_stats(self):
        """Resets cumulative PnL counters (useful after a balance sync)."""
        self.realized_pnl = 0.0
        # self.unrealized_pnl is dynamic based on positions, so we don't zero it hard,
        # but we re-calc it next update anyway.
