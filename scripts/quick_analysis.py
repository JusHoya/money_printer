"""Quick Harvest Analysis"""
import os, glob
import pandas as pd

os.chdir(r'c:\Users\hoyer\WorkSpace\Projects\Hoya_Box\programming\money_printer')

# Load data
df = pd.concat([pd.read_csv(f) for f in glob.glob('logs/data_*.csv')], ignore_index=True)
df['Timestamp'] = pd.to_datetime(df['Timestamp'])

# Summary
print('=== HARVEST SUMMARY ===')
print('Duration:', df.Timestamp.max() - df.Timestamp.min())
print('Total Rows:', len(df))

# Signals
signals = df[df['Type'].str.contains('SIGNAL')]
executed = signals[signals['Status'] == 'EXECUTED']
shadow = signals[signals['Status'] == 'HARVEST_ONLY']

print('\n=== SIGNALS ===')
print('Total:', len(signals))
print('Executed:', len(executed))
print('Shadow:', len(shadow))

print('\nExecuted by Symbol:')
for sym, cnt in executed['Symbol'].value_counts().items():
    print(f'  {sym}: {cnt}')

print('\nExecuted by Type:')
for t, cnt in executed['Type'].value_counts().items():
    print(f'  {t}: {cnt}')

# Portfolio
pf = pd.concat([pd.read_csv(f) for f in glob.glob('logs/portfolio_*.csv')])
pf['Timestamp'] = pd.to_datetime(pf['Timestamp'])
pf = pf.sort_values('Timestamp')

print('\n=== PORTFOLIO ===')
print(f'Start Equity: ${pf.Equity.iloc[0]:.2f}')
print(f'End Equity: ${pf.Equity.iloc[-1]:.2f}')
print(f'Return: ${pf.Equity.iloc[-1] - pf.Equity.iloc[0]:+.2f}')
print(f'Realized PnL: ${pf.Realized_PnL.iloc[-1]:.2f}')

# Positions count
if 'Positions' in pf.columns:
    print(f'Max Positions: {pf.Positions.max()}')
