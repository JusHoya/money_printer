"""Full Optimization Analysis"""
import os, glob, itertools
import pandas as pd
import sys

# Setup path
os.chdir(r'c:\Users\hoyer\WorkSpace\Projects\Hoya_Box\programming\money_printer')
sys.path.insert(0, '.')

from src.core.interfaces import MarketData
from src.core.matching_engine import SimulatedExchange
from src.strategies.crypto_strategy import Crypto15mTrendStrategy

# Load data
df = pd.concat([pd.read_csv(f) for f in glob.glob('logs/data_*.csv')], ignore_index=True)
df['Timestamp'] = pd.to_datetime(df['Timestamp'])
df = df.sort_values('Timestamp')
df = df[df['Type'] == 'MARKET_DATA']

print('='*60)
print('LAB: PARAMETER OPTIMIZATION')
print('='*60)
print(f'Data Points: {len(df):,}')

# Parameter Grid
param_grid = {
    'bull_trigger': [0.55, 0.60, 0.65, 0.70],
    'bear_trigger': [0.45, 0.40, 0.35, 0.30],
    'confirmation_delay': [0, 60, 120, 180]
}

keys = list(param_grid.keys())
combos = list(itertools.product(*param_grid.values()))
print(f'Testing {len(combos)} parameter combinations...')

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
    
    for _, row in df.iterrows():
        try:
            md = MarketData(
                symbol=row['Symbol'],
                price=float(row['Price']),
                bid=float(row.get('Price', 0)),
                ask=float(row.get('Price', 0)),
                volume=0,
                timestamp=row['Timestamp'],
                extra={'source': 'replay'}
            )
        except:
            continue
        
        if 'BTC' in md.symbol:
            oms.update_market('BTC', md.price)
        
        sigs = strategy.analyze(md)
        for s in sigs:
            ticker = s.symbol if 'BTC-USD' not in s.symbol else f'KXBTC-SIM-{count}'
            oms.open_position(
                ticker, s.side, s.limit_price, s.quantity,
                getattr(s, 'stop_loss', 0),
                getattr(s, 'trailing_rules', None)
            )
        count += 1
    
    stats = oms.get_stats()
    realized = stats['realized']
    unrealized = stats['unrealized']
    total_pnl = realized + unrealized
    
    wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
    total_trades = len(oms.closed_trades) + len(oms.positions)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    results.append({
        'params': params,
        'pnl': total_pnl,
        'realized': realized,
        'trades': total_trades,
        'win_rate': win_rate
    })
    
    if (i+1) % 10 == 0:
        print(f'  Tested {i+1}/{len(combos)}...')

# Sort by PnL
results.sort(key=lambda x: x['pnl'], reverse=True)

print('\n' + '='*60)
print('TOP 5 PARAMETER COMBINATIONS')
print('='*60)

for i, r in enumerate(results[:5]):
    print(f'\n#{i+1}:')
    print(f"  PnL: ${r['pnl']:.2f}")
    print(f"  Win Rate: {r['win_rate']:.1f}% ({r['trades']} trades)")
    print(f"  Params: {r['params']}")

print('\n' + '='*60)
print('WORST 3 PARAMETER COMBINATIONS')
print('='*60)

for i, r in enumerate(results[-3:]):
    print(f'\n#{len(results)-2+i}:')
    print(f"  PnL: ${r['pnl']:.2f}")
    print(f"  Win Rate: {r['win_rate']:.1f}% ({r['trades']} trades)")
    print(f"  Params: {r['params']}")

print('\n' + '='*60)
print('RECOMMENDATION')
print('='*60)
best = results[0]
print(f"Best Config:")
for k, v in best['params'].items():
    print(f"  {k}: {v}")
print(f"\nExpected PnL: ${best['pnl']:.2f}")
print(f"Win Rate: {best['win_rate']:.1f}%")
