"""
🔬 HARVEST ANALYSIS REPORT
Comprehensive analysis of collected trading data.
"""
import os
import glob
import pandas as pd
from datetime import datetime, timedelta

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Load Data ---
data_files = glob.glob('logs/data_*.csv')
portfolio_files = glob.glob('logs/portfolio_*.csv')

print("="*70)
print("🔬 MONEY PRINTER HARVEST ANALYSIS")
print("="*70)
print(f"\n📂 Files Found:")
print(f"   Data CSVs: {len(data_files)}")
print(f"   Portfolio CSVs: {len(portfolio_files)}")

# --- DATA ANALYSIS ---
all_data = []
for f in data_files:
    df = pd.read_csv(f)
    all_data.append(df)
    print(f"   • {os.path.basename(f)}: {len(df):,} rows")

df = pd.concat(all_data, ignore_index=True)
df['Timestamp'] = pd.to_datetime(df['Timestamp'])
df = df.sort_values('Timestamp')

print("\n" + "="*70)
print("📊 HARVEST SUMMARY")
print("="*70)

start_time = df['Timestamp'].min()
end_time = df['Timestamp'].max()
duration = end_time - start_time

print(f"\n⏰ Time Range:")
print(f"   Start: {start_time}")
print(f"   End:   {end_time}")
print(f"   Duration: {duration}")
print(f"   Total Rows: {len(df):,}")

# --- TYPE BREAKDOWN ---
print("\n📈 EVENT TYPES:")
for t, cnt in df['Type'].value_counts().items():
    pct = cnt / len(df) * 100
    print(f"   {t:20s}: {cnt:>7,} ({pct:>5.1f}%)")

# --- SIGNALS ANALYSIS ---
signals = df[df['Type'].str.contains('SIGNAL')]
print("\n" + "="*70)
print("🎯 SIGNAL ANALYSIS")
print("="*70)
print(f"\nTotal Signals Generated: {len(signals):,}")

if len(signals) > 0:
    print("\nBy Type:")
    for t in signals['Type'].unique():
        cnt = len(signals[signals['Type'] == t])
        print(f"   {t}: {cnt}")
    
    print("\nBy Status:")
    for s, cnt in signals['Status'].value_counts().items():
        print(f"   {s}: {cnt}")
    
    print("\nSignals by Symbol (Top 15):")
    for sym, cnt in signals['Symbol'].value_counts().head(15).items():
        # Derive strategy type
        if 'KXBTC' in sym:
            strat = "Crypto"
        elif 'KXHIGH' in sym:
            strat = "Weather"
        else:
            strat = "Other"
        print(f"   [{strat:8s}] {sym:40s}: {cnt:>4}")

# --- EXECUTED vs SHADOW ---
executed = signals[signals['Status'] == 'EXECUTED']
shadow = signals[signals['Status'] == 'HARVEST_ONLY']

print("\n" + "="*70)
print("🔥 EXECUTION ANALYSIS")
print("="*70)
print(f"\nExecuted Trades: {len(executed)}")
print(f"Shadow (Harvest Only): {len(shadow)}")

if len(executed) > 0:
    print("\nExecuted by Type:")
    for t in executed['Type'].unique():
        cnt = len(executed[executed['Type'] == t])
        print(f"   {t}: {cnt}")

    print("\nExecuted by Symbol (All):")
    for sym, cnt in executed['Symbol'].value_counts().items():
        avg_price = executed[executed['Symbol'] == sym]['Price'].mean()
        print(f"   {sym:40s}: {cnt:>3} trades @ avg ${avg_price:.2f}")

# --- MARKET COVERAGE ---
print("\n" + "="*70)
print("📍 MARKET COVERAGE")
print("="*70)

market_data = df[df['Type'] == 'MARKET_DATA']
print(f"\nTotal Market Ticks: {len(market_data):,}")

# Unique symbols
unique_symbols = market_data['Symbol'].nunique()
print(f"Unique Symbols: {unique_symbols}")

# Group by market type
btc_ticks = len(market_data[market_data['Symbol'].str.contains('BTC', na=False)])
weather_ticks = len(market_data[market_data['Symbol'].str.contains('KXHIGH', na=False)])
other_ticks = len(market_data) - btc_ticks - weather_ticks

print(f"\nBy Market:")
print(f"   BTC/Crypto: {btc_ticks:,} ticks")
print(f"   Weather:    {weather_ticks:,} ticks")
print(f"   Other:      {other_ticks:,} ticks")

# --- PRICE ANALYSIS (BTC) ---
btc_source = market_data[market_data['Symbol'].str.contains('BTC-USD', na=False)]
if len(btc_source) > 0:
    print("\n" + "="*70)
    print("💰 BTC-USD PRICE ANALYSIS")
    print("="*70)
    btc_prices = btc_source['Price'].astype(float)
    print(f"\n   Min:  ${btc_prices.min():,.2f}")
    print(f"   Max:  ${btc_prices.max():,.2f}")
    print(f"   Avg:  ${btc_prices.mean():,.2f}")
    print(f"   Open: ${btc_prices.iloc[0]:,.2f}")
    print(f"   Close:${btc_prices.iloc[-1]:,.2f}")
    
    pct_change = (btc_prices.iloc[-1] - btc_prices.iloc[0]) / btc_prices.iloc[0] * 100
    print(f"\n   Period Change: {pct_change:+.2f}%")

# --- PORTFOLIO ANALYSIS ---
if portfolio_files:
    print("\n" + "="*70)
    print("💼 PORTFOLIO ANALYSIS")
    print("="*70)
    
    all_portfolio = []
    for f in portfolio_files:
        pf = pd.read_csv(f)
        all_portfolio.append(pf)
    
    pf = pd.concat(all_portfolio, ignore_index=True)
    pf['Timestamp'] = pd.to_datetime(pf['Timestamp'])
    pf = pf.sort_values('Timestamp')
    
    print(f"\nSnapshots: {len(pf):,}")
    print(f"Columns: {list(pf.columns)}")
    
    if 'Equity' in pf.columns:
        print(f"\n💵 Equity:")
        print(f"   Start: ${pf['Equity'].iloc[0]:.2f}")
        print(f"   End:   ${pf['Equity'].iloc[-1]:.2f}")
        print(f"   Min:   ${pf['Equity'].min():.2f}")
        print(f"   Max:   ${pf['Equity'].max():.2f}")
        
        total_return = (pf['Equity'].iloc[-1] - pf['Equity'].iloc[0])
        print(f"\n   Total Return: ${total_return:+.2f}")
    
    if 'Realized_PnL' in pf.columns:
        print(f"\n📊 Realized PnL:")
        print(f"   Final: ${pf['Realized_PnL'].iloc[-1]:.2f}")
    
    if 'Unrealized_PnL' in pf.columns:
        print(f"\n📊 Unrealized PnL (Final):")
        print(f"   Final: ${pf['Unrealized_PnL'].iloc[-1]:.2f}")

print("\n" + "="*70)
print("✅ ANALYSIS COMPLETE")
print("="*70)
