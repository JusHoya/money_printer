import unittest
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.risk_manager import RiskManager
from src.core.matching_engine import SimulatedExchange

class TestBalanceFlow(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(starting_balance=100.0)
        # Override exchange settings for predictable outcomes
        self.rm.exchange.TAKE_PROFIT_PCT = 100.0
        self.rm.exchange.STOP_LOSS_PCT = 100.0

    def test_basic_long_trade_flow(self):
        print("\n--- Test 1: Basic Long Trade Flow ---")
        # 1. Start
        self.assertEqual(self.rm.balance, 100.0)

        # 2. Open Long: Buy 10 @ $2.00 (Cost $20)
        # Use a "Strike" based symbol so we can test PnL
        # KXBTC strike 2.00
        self.rm.record_execution(20.0, "KXBTC-TEST-2", "buy", 10, 2.00)
        
        self.assertEqual(self.rm.balance, 80.0)

        # 3. Market Move: Price -> $3.00 (Unrealized Gain)
        # Update "BTC" market.
        # Logic: Spot(3) - Strike(2) = 1. Diff/Strike = 0.5. Price Delta = 50.
        # New Price = 2.00 + 50 = 52.00 (Capped at 0.99).
        # Wait, the prices in system are 0.01-0.99.
        # Let's use valid binary prices.
        pass

    def test_basic_long_trade_flow_valid_pricing(self):
        print("\n--- Test 1B: Valid Pricing Long Trade ---")
        # Strike 100. Entry 0.50. Cost $50 (100 qty).
        self.rm.record_execution(50.0, "KXBTC-TEST-100", "buy", 100, 0.50)
        self.assertEqual(self.rm.balance, 50.0)
        
        # Spot moves to 105 (+5 over strike of 100).
        # With tanh formula: diff=5, scale=1000 (BTC), norm=0.005
        # tanh(0.005) ≈ 0.005, shift = 0.005 * 0.49 ≈ 0.00245
        # estimated_price ≈ 0.50 + 0.00245 = 0.50245
        # PnL = (0.50245 - 0.50) * 100 ≈ 0.245
        
        self.rm.update_market_data("BTC", 105.0)
        
        print(f"Unrealized after move: {self.rm.unrealized_pnl}")
        # Tanh formula produces a small shift for small BTC moves relative to scale=1000
        self.assertGreater(self.rm.unrealized_pnl, 0.0)
        self.assertLess(self.rm.unrealized_pnl, 1.0)
        
        # Balance shouldn't change (unrealized, not realized)
        self.assertEqual(self.rm.balance, 50.0)

    def test_loss_scenario(self):
        print("\n--- Test 4: Loss Scenario (Bad Trade) ---")
        # 1. Start $100
        self.assertEqual(self.rm.balance, 100.0)
        
        # 2. Buy 10 @ $0.75 (Cost $7.50)
        # Protocol: Debit = Price * Qty
        self.rm.record_execution(7.50, "KX-LOSE-1", "buy", 10, 0.75)
        
        # Check Debit
        self.assertEqual(self.rm.balance, 92.50)
        print(f"Balance after Entry: {self.rm.balance}")
        
        # 3. Simulate Loss (Price goes to 0 / Event fails)
        pos = self.rm.exchange.positions[0]
        # Close with 0.00 Spot (Loss for Buy)
        self.rm.exchange._close_position(pos, 0.00, "LOSS_TEST")
        self.rm.update_market_data("TEST", 0.00)
        
        # 4. Verify PnL
        # Payout = 0.00. Cost = 7.50. PnL = -7.50.
        stats = self.rm.exchange.get_stats()
        print(f"Realized PnL: {stats['realized']}")
        self.assertEqual(stats['realized'], -7.50)
        
        # 5. Verify Balance
        # Start(100) + Realized(-7.50) - Exp(0) = 92.50.
        print(f"Final Balance: {self.rm.balance}")
        self.assertEqual(self.rm.balance, 92.50)

    def test_double_counting_bug(self):
        print("\n--- Test 3: Double Counting on Sync ---")
        rm = RiskManager(starting_balance=100.0)
        
        # 1. Open and close a winning trade
        rm.record_execution(20.0, "KX-TEST-50", "buy", 100, 0.20)
        pos = rm.exchange.positions[0]
        # Use EXPIRATION so binary settlement (spot=60 > strike=50 → YES → exit=1.00)
        # PnL = (1.00 - 0.20) * 100 = $80
        rm.exchange._close_position(pos, 60.0, "EXPIRATION")
        rm.update_market_data("TEST", 60.0)
        
        self.assertEqual(rm.balance, 180.0)
        
        # 2. SYNC from "Real" Account
        # Suppose we sync and the Real Account says $180 (correct).
        rm.update_balance(180.0)
        
        # update_balance resets daily_pnl=0 and exchange.reset_stats().
        # _sync_balance: balance = 180 + 0 - 0 = 180. Correct!
        print(f"Balance after Sync(180): {rm.balance}")
        self.assertEqual(rm.balance, 180.0, f"Balance Double Counted! Got {rm.balance}")

if __name__ == '__main__':
    unittest.main()
