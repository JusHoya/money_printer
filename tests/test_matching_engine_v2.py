import unittest
import sys
import os
import math
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.matching_engine import SimulatedExchange

class TestMatchingEngineV2(unittest.TestCase):
    def setUp(self):
        self.exchange = SimulatedExchange()
        self.exchange.TAKE_PROFIT_PCT = 10.0 # Disable TP for testing
        self.exchange.STOP_LOSS_PCT = 10.0   # Disable SL for testing

    def test_precip_pnl_update(self):
        print("\n--- Testing KXPRECIP PnL Update ---")
        # 1. Open Position: Buy YES (Long) on Precip NYC
        # Symbol: KXPRECIP-TestPeriod-NYC
        # Entry Price: 0.20
        self.exchange.open_position("KXPRECIP-TestPeriod-NYC", "buy", 0.20, 100)
        
        # Initial PnL should be 0
        self.assertEqual(self.exchange.unrealized_pnl, 0.0)
        
        # 2. Update Market: PoP increases to 0.50
        # Precip uses direct price pass-through
        # PnL = (0.50 - 0.20) * 100 = 30.0
        self.exchange.update_market("PRECIP_NYC", 0.50)
        
        stats = self.exchange.get_stats()
        print(f"Stats after update: {stats}")
        
        self.assertAlmostEqual(stats['unrealized'], 30.0)
        
        # 3. Update Market: PoP drops to 0.10
        # Loss. Current Price -> 0.10.
        # PnL = (0.10 - 0.20) * 100 = -10.0
        self.exchange.update_market("PRECIP_NYC", 0.10)
        stats = self.exchange.get_stats()
        self.assertAlmostEqual(stats['unrealized'], -10.0)

    def test_temp_pnl_update(self):
        print("\n--- Testing KXHIGH (Temp) PnL Update ---")
        # 1. Open Position: Buy YES on Temp NYC > 75
        # Symbol: KXHIGH-TestPeriod-NYC-75
        # Entry Price: 0.50
        self.exchange.open_position("KXHIGH-TestPeriod-NYC-75", "buy", 0.50, 100)
        
        # 2. Update Market: Temp is 75 (At Strike)
        # With tanh formula: diff=0, tanh(0)=0, price=0.50
        self.exchange.update_market("TEMP_NYC", 75.0)
        stats = self.exchange.get_stats()
        self.assertAlmostEqual(stats['unrealized'], 0.0)
        
        # 3. Update Market: Temp is 80 (Above Strike by 5°)
        # With tanh formula: diff=5, scale=10, norm=0.5
        # tanh(0.5) ≈ 0.462, shift = 0.462 * 0.49 ≈ 0.226
        # estimated_price ≈ 0.50 + 0.226 = 0.726
        # PnL = (0.726 - 0.50) * 100 ≈ 22.6
        self.exchange.update_market("TEMP_NYC", 80.0)
        stats = self.exchange.get_stats()
        
        # Expected: positive PnL between 20 and 25
        self.assertGreater(stats['unrealized'], 20.0)
        self.assertLess(stats['unrealized'], 30.0)
        print(f"Unrealized at 80°F: {stats['unrealized']:.2f}")

    def test_cross_contamination(self):
        print("\n--- Testing Cross Contamination ---")
        # Open Precip Position
        self.exchange.open_position("KXPRECIP-Test-NYC", "buy", 0.20, 100)
        
        # Update TEMP for NYC. Should NOT affect Precip.
        self.exchange.update_market("TEMP_NYC", 99.0) # Extreme temp
        
        stats = self.exchange.get_stats()
        self.assertEqual(stats['unrealized'], 0.0) # Should be untouched

if __name__ == '__main__':
    unittest.main()

