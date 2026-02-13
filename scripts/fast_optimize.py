"""Lightweight Optimization with Sampling"""
import os, glob, itertools
import pandas as pd
import sys

os.chdir(r'c:\Users\hoyer\WorkSpace\Projects\Hoya_Box\programming\money_printer')
sys.path.insert(0, '.')

from src.core.interfaces import MarketData
from src.core.matching_engine import SimulatedExchange
from src.strategies.crypto_strategy import Crypto15mTrendStrategy

# Load and SAMPLE data for speed
df = pd.concat([pd.read_csv(f) for f in glob.glob('logs/data_*.csv')], ignore_index=True)
df['Timestamp'] = pd.to_datetime(df['Timestamp'])
df = df.sort_values('Timestamp')

# Only look at KXBTC data (Crypto strategy)
df = df[df['Symbol'].str.contains('KXBTC', na=False)]
print(f'Crypto Data Points: {len(df):,}')

# Sample for speed
sampled = df.to_dict('records')

# Parameter Grid - smaller
param_grid = {
    'bull_trigger': [0.55, 0.60, 0.65],
    'bear_trigger': [0.45, 0.40, 0.35],
    'confirmation_delay': [0, 60, 120]
}

keys = list(param_grid.keys())
combos = list(itertools.product(*param_grid.values()))
print(f'Testing {len(combos)} combinations on {len(sampled)} points...')

results = []

for i, combo in enumerate(combos):
    params = dict(zip(keys, combo))
    
    strategy = Crypto15mTrendStrategy(
        bull_trigger=params['bull_trigger'],
        bear_trigger=params['bear_trigger'],
        confirmation_delay=params['confirmation_delay']
    )
    oms = SimulatedExchange()
    count = 0
    
    for row in sampled:
        try:
            price = float(row['Price'])
            md = MarketData(
                symbol=row['Symbol'],
                price=price,
                bid=price,
                ask=price,
                volume=0,
                timestamp=row['Timestamp'],
                extra={'source': 'replay'}
            )
        except:
            continue
        
        oms.update_market('BTC', price)
        
        sigs = strategy.analyze(md)
        for s in sigs:
            oms.open_position(
                f'KXBTC-SIM-{count}', s.side, s.limit_price, s.quantity,
                getattr(s, 'stop_loss', 0),
                getattr(s, 'trailing_rules', None)
            )
        count += 1
    
    stats = oms.get_stats()
    total_pnl = stats['realized'] + stats['unrealized']
    
    wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
    total_trades = len(oms.closed_trades) + len(oms.positions)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    results.append({
        'params': params,
        'pnl': total_pnl,
        'realized': stats['realized'],
        'trades': total_trades,
        'closed': len(oms.closed_trades),
        'win_rate': win_rate
    })
    
    print(f'  [{i+1}/{len(combos)}] Bull:{params["bull_trigger"]} Bear:{params["bear_trigger"]} Delay:{params["confirmation_delay"]} | PnL: ${total_pnl:.2f} | WR: {win_rate:.0f}%')

# Sort by PnL
results.sort(key=lambda x: x['pnl'], reverse=True)

print('\n' + '='*60)
print('TOP 5 CONFIGURATIONS')
print('='*60)

for i, r in enumerate(results[:5]):
    print(f"\n#{i+1}: PnL ${r['pnl']:.2f} | WR {r['win_rate']:.0f}% | Trades {r['trades']}")
    print(f"    bull_trigger: {r['params']['bull_trigger']}")
    print(f"    bear_trigger: {r['params']['bear_trigger']}")
    print(f"    confirmation_delay: {r['params']['confirmation_delay']}")

print('\n' + '='*60)
print('WORST 3 CONFIGURATIONS')
print('='*60)

for i, r in enumerate(results[-3:]):
    print(f"\n#{len(results)-2+i}: PnL ${r['pnl']:.2f} | WR {r['win_rate']:.0f}% | Trades {r['trades']}")
    print(f"    bull_trigger: {r['params']['bull_trigger']}")
    print(f"    bear_trigger: {r['params']['bear_trigger']}")
    print(f"    confirmation_delay: {r['params']['confirmation_delay']}")

print('\n' + '='*60)
print('OPTIMIZATION RECOMMENDATION')
print('='*60)
best = results[0]
print('Apply these parameters to crypto_strategy.py:')
for k, v in best['params'].items():
    print(f'  {k} = {v}')
print(f"\nExpected PnL: ${best['pnl']:.2f}")
print(f"Win Rate: {best['win_rate']:.1f}%")
