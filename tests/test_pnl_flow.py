"""
Comprehensive PnL Flow Tests
Tests the entire financial data flow from position open to dashboard display.
"""
import unittest
import sys
import os
import math

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.risk_manager import RiskManager
from src.core.matching_engine import SimulatedExchange


class TestPnLFlow(unittest.TestCase):
    """Tests covering the complete PnL lifecycle."""
    
    def setUp(self):
        self.rm = RiskManager(starting_balance=100.0)
        # Disable auto-stops for predictable testing
        self.rm.exchange.TAKE_PROFIT_PCT = 100.0
        self.rm.exchange.STOP_LOSS_PCT = 100.0
        self.rm.exchange.TIME_LIMIT_MIN = 9999
    
    # === TEST 1: Basic Position Opening ===
    def test_open_position_reduces_balance(self):
        """Opening a position should reduce available balance by cost."""
        print("\n--- Test: Open Position Reduces Balance ---")
        
        initial_balance = self.rm.balance
        cost = 50.0  # 100 qty @ 0.50
        
        self.rm.record_execution(cost, "KXBTC-TEST-50000", "buy", 100, 0.50)
        
        self.assertEqual(self.rm.balance, initial_balance - cost)
        self.assertEqual(len(self.rm.exchange.positions), 1)
        print(f"✅ Balance reduced from ${initial_balance} to ${self.rm.balance}")
    
    # === TEST 2: Unrealized PnL Updates ===
    def test_unrealized_pnl_updates_on_price_move(self):
        """Unrealized PnL should update when market price changes."""
        print("\n--- Test: Unrealized PnL Updates ---")
        
        # Open position: BUY 100 @ 0.50 on a $50000 strike
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        initial_unrealized = self.rm.unrealized_pnl
        
        # Price moves up (spot > strike by $5000)
        # With tanh formula: diff=5000, scale=1000, norm=5, tanh(5)≈0.9999
        # probability_shift = 0.9999 * 0.49 ≈ 0.49, estimated = 0.50 + 0.49 = 0.99
        self.rm.update_market_data("BTC", 55000.0)
        
        # Unrealized should be positive
        self.assertGreater(self.rm.unrealized_pnl, initial_unrealized)
        print(f"✅ Unrealized PnL updated: ${self.rm.unrealized_pnl:.2f}")
    
    # === TEST 3: Realized PnL on Close ===
    def test_realized_pnl_on_position_close(self):
        """Closing a position should move PnL from unrealized to realized."""
        print("\n--- Test: Realized PnL on Close ---")
        
        # Open position
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        pos = self.rm.exchange.positions[0]
        
        # Close with profit (spot > strike)
        self.rm.exchange._close_position(pos, 55000.0, reason="TEST_CLOSE")
        
        # Sync stats
        self.rm.update_market_data("BTC", 55000.0)
        
        stats = self.rm.exchange.get_stats()
        self.assertGreater(stats['realized'], 0)
        self.assertEqual(stats['open_count'], 0)
        print(f"✅ Realized PnL: ${stats['realized']:.2f}, Open positions: {stats['open_count']}")
    
    # === TEST 4: Balance Recovery After Profitable Close ===
    def test_balance_increases_on_profitable_close(self):
        """Balance should increase when a profitable trade closes."""
        print("\n--- Test: Balance Increases on Profit ---")
        
        initial_balance = self.rm.balance
        
        # Open position (costs $50)
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        balance_after_open = self.rm.balance
        self.assertEqual(balance_after_open, 50.0)
        
        # Close with WIN (exit @ 1.00)
        pos = self.rm.exchange.positions[0]
        self.rm.exchange._close_position(pos, 60000.0, reason="EXPIRATION") # Binary settles to 1.00
        self.rm.update_market_data("BTC", 60000.0)
        
        # PnL = (1.00 - 0.50) * 100 = $50
        # Balance should be: 100 + 50 = 150
        self.assertEqual(self.rm.balance, 150.0)
        print(f"✅ Balance after win: ${self.rm.balance:.2f}")
    
    # === TEST 5: Balance Decrease After Loss ===
    def test_balance_decreases_on_loss(self):
        """Balance should decrease when a losing trade closes."""
        print("\n--- Test: Balance Decreases on Loss ---")
        
        # Open position (costs $50)
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        
        # Close with LOSS (exit @ 0.00 - binary settles NO)
        pos = self.rm.exchange.positions[0]
        self.rm.exchange._close_position(pos, 45000.0, reason="EXPIRATION") # Spot < Strike
        self.rm.update_market_data("BTC", 45000.0)
        
        # PnL = (0.00 - 0.50) * 100 = -$50
        # Balance should be: 100 - 50 = 50
        self.assertEqual(self.rm.balance, 50.0)
        print(f"✅ Balance after loss: ${self.rm.balance:.2f}")
    
    # === TEST 6: Equity Calculation ===
    def test_equity_calculation(self):
        """Equity = Cash + Exposure + Unrealized."""
        print("\n--- Test: Equity Calculation ---")
        
        # Open position
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        
        # Update market (profitable)
        self.rm.update_market_data("BTC", 55000.0)
        
        cash = self.rm.balance
        exposure = self.rm.get_current_exposure()
        unrealized = self.rm.unrealized_pnl
        
        # This is what the dashboard calculates
        equity = cash + exposure + unrealized
        
        # Exposure = 0.50 * 100 = 50
        # Cash = 100 - 50 + 0 (no realized yet) = 50
        # Equity = 50 + 50 + unrealized
        
        print(f"  Cash: ${cash:.2f}")
        print(f"  Exposure: ${exposure:.2f}")
        print(f"  Unrealized: ${unrealized:.2f}")
        print(f"  Equity: ${equity:.2f}")
        
        # Equity should be close to starting balance + unrealized gains
        self.assertGreater(equity, 100.0)
        print(f"✅ Equity correctly calculated")
    
    # === TEST 7: Tanh Price Formula Behavior ===
    def test_tanh_price_formula_temperature(self):
        """Test tanh formula produces expected prices for temperature."""
        print("\n--- Test: Tanh Price Formula (Temperature) ---")
        
        exchange = SimulatedExchange()
        exchange.TAKE_PROFIT_PCT = 100.0
        exchange.STOP_LOSS_PCT = 100.0
        
        # Open position on KXHIGH with strike 75
        exchange.open_position("KXHIGH-TEST-75", "buy", 0.50, 100)
        
        # Test at strike (should be ~0.50)
        exchange.update_market("TEMP_NYC", 75.0)
        pos = exchange.positions[0]
        self.assertAlmostEqual(pos['current_price'], 0.50, places=2)
        print(f"  At strike (75°F): {pos['current_price']:.4f}")
        
        # Test above strike by 5° (scale=10, diff=5, tanh(0.5)≈0.46)
        # probability_shift = 0.46 * 0.49 ≈ 0.23, price ≈ 0.73
        exchange.update_market("TEMP_NYC", 80.0)
        self.assertGreater(pos['current_price'], 0.60)
        self.assertLess(pos['current_price'], 0.85)
        print(f"  Above strike (80°F): {pos['current_price']:.4f}")
        
        # Test far above strike (20°) - should approach 0.99
        exchange.update_market("TEMP_NYC", 95.0)
        self.assertGreater(pos['current_price'], 0.90)
        print(f"  Far above strike (95°F): {pos['current_price']:.4f}")
        
        print("✅ Tanh formula produces expected temperature scaling")


class TestExposureCalculation(unittest.TestCase):
    """Tests for exposure tracking."""
    
    def setUp(self):
        self.rm = RiskManager(starting_balance=100.0)
        self.rm.exchange.TAKE_PROFIT_PCT = 100.0
        self.rm.exchange.STOP_LOSS_PCT = 100.0
    
    def test_exposure_sums_position_costs(self):
        """Exposure should equal sum of (entry_price × quantity)."""
        print("\n--- Test: Exposure Calculation ---")
        
        # Open two positions
        self.rm.record_execution(20.0, "KXBTC-TEST-50000", "buy", 100, 0.20)
        self.rm.record_execution(15.0, "KXHIGH-TEST-75", "buy", 50, 0.30)
        
        exposure = self.rm.get_current_exposure()
        
        # Expected: (0.20 * 100) + (0.30 * 50) = 20 + 15 = 35
        self.assertEqual(exposure, 35.0)
        print(f"✅ Exposure correctly calculated: ${exposure:.2f}")
    
    def test_exposure_by_category(self):
        """Exposure filtering by category should work."""
        print("\n--- Test: Exposure by Category ---")
        
        self.rm.record_execution(20.0, "KXBTC-TEST-50000", "buy", 100, 0.20)
        self.rm.record_execution(15.0, "KXHIGH-TEST-75", "buy", 50, 0.30)
        
        crypto_exp = self.rm.get_current_exposure(category='crypto')
        weather_exp = self.rm.get_current_exposure(category='weather')
        
        self.assertEqual(crypto_exp, 20.0)
        self.assertEqual(weather_exp, 15.0)
        print(f"✅ Crypto: ${crypto_exp:.2f}, Weather: ${weather_exp:.2f}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
