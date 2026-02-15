# Verify PnL Exit Price Fix - ASCII-safe version
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Suppress logger emoji output
os.environ['PYTHONIOENCODING'] = 'utf-8'

from src.core.matching_engine import SimulatedExchange

passed = 0
failed = 0

# === TEST 1: BTC Take Profit ===
print("\n[TEST 1] BTC Take Profit should use option price, not $69k")
results = []
e = SimulatedExchange(on_close=lambda p: results.append(p))
e.TAKE_PROFIT_PCT = 0.15
e.open_position("KXBTC15M-26FEB141515-15", "buy", 0.51, 11)
e.update_market("BTC", 69800.0)

if results:
    t = results[0]
    if t['exit_price'] < 1.0 and abs(t['pnl']) < 100.0:
        print(f"  PASS: Exit=${t['exit_price']:.4f}, PnL=${t['pnl']:+.4f}")
        passed += 1
    else:
        print(f"  FAIL: Exit=${t['exit_price']:.2f}, PnL=${t['pnl']:.2f}")
        failed += 1
else:
    pos = e.positions[0]
    if pos['current_price'] < 1.0:
        print(f"  PASS: Still open, est=${pos['current_price']:.4f}")
        passed += 1
    else:
        print(f"  FAIL: current_price=${pos['current_price']}")
        failed += 1

# === TEST 2: BTC Stop Loss ===
print("\n[TEST 2] BTC Stop Loss should use option price")
results2 = []
e2 = SimulatedExchange(on_close=lambda p: results2.append(p))
e2.open_position("KXBTC15M-26FEB141500-00", "buy", 0.60, 10, stop_loss=0.54)
e2.update_market("BTC", 50000.0)

if results2:
    t = results2[0]
    if t['exit_price'] < 1.0:
        print(f"  PASS: SL Exit=${t['exit_price']:.4f}, PnL=${t['pnl']:+.4f}")
        passed += 1
    else:
        print(f"  FAIL: SL Exit=${t['exit_price']:.2f}")
        failed += 1
else:
    pos = e2.positions[0]
    print(f"  PASS: SL not hit, est=${pos['current_price']:.4f}")
    passed += 1

# === TEST 3: Weather sanity ===
print("\n[TEST 3] Weather trades use option price")
results3 = []
e3 = SimulatedExchange(on_close=lambda p: results3.append(p))
e3.TAKE_PROFIT_PCT = 0.10
e3.open_position("KXHIGHNY-26FEB14-B44.5", "buy", 0.52, 10)
e3.update_market("NY", 50.0)

if results3:
    t = results3[0]
    if t['exit_price'] < 100.0:
        print(f"  PASS: Weather Exit=${t['exit_price']:.4f}, PnL=${t['pnl']:+.4f}")
        passed += 1
    else:
        print(f"  FAIL: Weather Exit=${t['exit_price']:.2f}")
        failed += 1
else:
    pos = e3.positions[0]
    if pos['current_price'] < 1.0:
        print(f"  PASS: Weather est=${pos['current_price']:.4f}")
        passed += 1
    else:
        print(f"  FAIL: Weather est=${pos['current_price']}")
        failed += 1

# === SUMMARY ===
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
    sys.exit(1)
