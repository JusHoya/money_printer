import unittest
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.risk_manager import RiskManager


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
        # Strike 50000. Entry 0.50. Requests 100 qty but risk_manager caps
        # entries at MAX_CONTRACTS=50 → recorded as 50 qty, cost $25.
        # Use realistic BTC strike (> 1000) so tanh estimation works
        self.rm.record_execution(
            50.0, "KXBTC-TEST-T50000", "buy", 100, 0.50, strike=50000.0
        )

        # Balance = starting - exposure (fees already in realized_pnl)
        # exposure = 0.50 * 50 (capped) = 25 → balance ≈ 75
        self.assertAlmostEqual(self.rm.balance, 75.0, delta=1.0)

        # Spot moves to 55000 (+5000 over strike).
        # tanh(5000/1000) ≈ 1.0, shift = 0.49, estimated ≈ 0.99
        # PnL = (0.99 - 0.50) * 100 ≈ 49.0 (but grace period blocks exits)
        self.rm.update_market_data("BTC", 55000.0)

        print(f"Unrealized after move: {self.rm.unrealized_pnl}")
        # Spot $5K above strike → tanh gives ~0.99 → large unrealized gain
        self.assertGreater(self.rm.unrealized_pnl, 0.0)

        # Balance = starting + realized + unrealized - exposure
        # After Fix 6, _sync_balance includes unrealized_pnl in balance.
        # exposure = 0.50 * 50 (capped) = 25.
        # balance = 100 + 0 + ~24.5 - 25 - fees ≈ 99.5 - fees
        fees = self.rm.exchange.total_fees_paid
        unrealized = self.rm.unrealized_pnl
        expected_balance = 100.0 + 0.0 + unrealized - 25.0 - fees
        self.assertAlmostEqual(self.rm.balance, expected_balance, places=2)

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

        # 4. Verify PnL (includes fees from Sprint 4)
        # Trade PnL = -7.50, plus entry fee (maker fee on 10 contracts @ 0.75)
        stats = self.rm.exchange.get_stats()
        print(f"Realized PnL: {stats['realized']}")
        entry_fee = stats.get("total_fees", 0)
        # PnL = -7.50 - entry_fee
        self.assertAlmostEqual(stats["realized"], -7.50 - entry_fee, places=2)

        # 5. Verify Balance
        # Start(100) + Realized - Exp(0) = ~92.50 - fees
        print(f"Final Balance: {self.rm.balance}")
        self.assertAlmostEqual(self.rm.balance, 92.50 - entry_fee, places=2)

    def test_double_counting_bug(self):
        print("\n--- Test 3: Double Counting on Sync ---")
        rm = RiskManager(starting_balance=100.0)

        # 1. Open and close a winning trade. Requests 100 qty but risk_manager
        # caps entries at MAX_CONTRACTS=50 → recorded as 50 qty.
        rm.record_execution(20.0, "KX-TEST-50", "buy", 100, 0.20)
        pos = rm.exchange.positions[0]
        # Use EXPIRATION so binary settlement (spot=60 > strike=50 → YES → exit=1.00)
        # PnL = (1.00 - 0.20) * 50 (capped) = $40
        rm.exchange._close_position(pos, 60.0, "EXPIRATION")
        rm.update_market_data("TEST", 60.0)

        # Balance = 100 + 40 (PnL) - fees
        fees = rm.exchange.total_fees_paid
        self.assertAlmostEqual(rm.balance, 140.0 - fees, places=2)

        # 2. SYNC from "Real" Account
        # Suppose we sync and the Real Account says the actual balance.
        actual_balance = rm.balance  # Use the fee-adjusted balance
        rm.update_balance(actual_balance)

        # update_balance resets daily_pnl=0 and exchange.reset_stats().
        print(f"Balance after Sync({actual_balance:.2f}): {rm.balance}")
        self.assertAlmostEqual(rm.balance, actual_balance, places=2)


if __name__ == "__main__":
    unittest.main()
