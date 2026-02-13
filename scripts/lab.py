import os
import glob
import pandas as pd
from datetime import datetime
from typing import List

from src.core.interfaces import MarketData, TradeSignal
from src.core.matching_engine import SimulatedExchange
from src.strategies.crypto_strategy import Crypto15mTrendStrategy, Crypto15mTrendStrategyV2
from src.strategies.weather_strategy import WeatherArbitrageStrategy, WeatherArbitrageStrategyV2
from src.strategies.bracket_strategy import BracketStrategy, WeatherBracketStrategy
from src.utils.logger import logger

class ReplayProvider:
    """
    Feeds historical data from CSV logs into the strategy.
    """
    def __init__(self, log_dir="logs"):
        self.data = []
        self._load_data(log_dir)
        
    def _load_data(self, log_dir):
        files = glob.glob(os.path.join(log_dir, "data_*.csv"))
        if not files:
            print("❌ No log files found.")
            return

        df_list = []
        for f in files:
            try:
                # Read only relevant cols to save memory
                df = pd.read_csv(f)
                if 'Type' in df.columns and 'MARKET_DATA' in df['Type'].values:
                     df_list.append(df[df['Type'] == 'MARKET_DATA'])
            except: pass
            
        if not df_list:
            print("❌ No MARKET_DATA found in logs.")
            return

        print(f"📂 Loading {len(df_list)} log files...")
        full_df = pd.concat(df_list, ignore_index=True)
        full_df['Timestamp'] = pd.to_datetime(full_df['Timestamp'])
        full_df = full_df.sort_values('Timestamp')
        
        # Convert to list of dicts for speed
        self.data = full_df.to_dict('records')
        print(f"✅ Loaded {len(self.data)} data points.")

    def stream(self):
        """Yields MarketData objects"""
        for row in self.data:
            # Reconstruct MarketData
            # CSV Cols: Timestamp, Type, Symbol, Price, Bid, Ask, Extra
            
            try:
                price = float(row['Price'])
                bid = float(row.get('Bid', 0.0))
                ask = float(row.get('Ask', 0.0))
                
                md = MarketData(
                    symbol=row['Symbol'],
                    price=price,
                    bid=bid,
                    ask=ask,
                    volume=0,
                    timestamp=row['Timestamp'],
                    extra={'source': 'replay', 'close_time': None} 
                    # Note: We might miss 'close_time' from CSV if not saved, 
                    # which affects expiration logic.
                )
                yield md
            except Exception as e:
                continue

def run_audit():
    print("🧪 LAB: STRATEGY AUDIT (TREND CATCHER)")
    print("=======================================")
    
    # 1. Setup Environment
    provider = ReplayProvider()
    if not provider.data: return

    strategy = Crypto15mTrendStrategy()
    oms = SimulatedExchange()
    
    print(f"🤖 Strategy: {strategy.name()}")
    print("⏳ Running Replay...")
    
    # 2. Replay Loop
    count = 0
    signals = 0
    
    for md in provider.stream():
        count += 1
        
        # A. Update OMS (Mark-to-Market)
        # We need to map symbol types. If log has 'BTC-USD', we update 'BTC'.
        if 'BTC' in md.symbol:
            oms.update_market('BTC', md.price)
            
        # B. Strategy Analysis
        # Trend Catcher works on 'BTC-USD' (Source data) to generate signals for 'KXBTC...'
        # The logs might contain 'BTC-USD' rows.
        
        generated_signals = strategy.analyze(md)
        
        for sig in generated_signals:
            signals += 1
            # C. Execution (Mock)
            # Create a fake ticker for the position since we might not have the real Kalshi ticker in history
            ticker = sig.symbol
            if "BTC-USD" in ticker:
                 # Map to a synthetic ticker for tracking
                 ticker = f"KXBTC-SIM-{count}"
            
            oms.open_position(
                symbol=ticker,
                side=sig.side,
                entry_price=sig.limit_price,
                quantity=sig.quantity,
                stop_loss=getattr(sig, 'stop_loss', 0.0),
                trailing_rules=getattr(sig, 'trailing_rules', None),
                expiration_time=getattr(sig, 'expiration_time', None)
            )
            
        if count % 1000 == 0:
            print(f"   ... Processed {count} ticks | Open Positions: {len(oms.positions)}")

    # 3. Final Report
    stats = oms.get_stats()
    print("\n📊 AUDIT RESULTS")
    print("----------------")
    print(f"Total Signals: {signals}")
    print(f"Realized PnL: ${stats['realized']:.2f}")
    print(f"Open PnL:     ${stats['unrealized']:.2f}")
    
    # Win Rate Calc
    wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
    losses = len([t for t in oms.closed_trades if t['pnl'] <= 0])
    total = wins + losses
    
    if total > 0:
        wr = (wins / total) * 100
        print(f"Win Rate:     {wr:.1f}% ({wins} W / {losses} L)")
    else:
        print("Win Rate:     N/A (No closed trades)")

import itertools

def run_optimization():
    print("🧬 LAB: EVOLUTIONARY OPTIMIZATION")
    print("===================================")
    
    provider = ReplayProvider()
    if not provider.data: return
    
    # 1. Define Search Space
    print("⚙️  Defining Gene Pool...")
    param_grid = {
        'bull_trigger': [0.55, 0.60, 0.65],
        'bear_trigger': [0.45, 0.40, 0.35],
        'confirmation_delay': [60, 120, 180],
        # 'stop_loss_buffer': [0.10, 0.15] # Keep simple for now
    }
    
    keys = list(param_grid.keys())
    combinations = list(itertools.product(*param_grid.values()))
    
    print(f"🧪 Testing {len(combinations)} Genomes against {len(provider.data)} historical ticks.")
    print("   (This may take a moment... or a coffee break)")
    
    results = []
    
    for idx, combo in enumerate(combinations):
        # Unpack params
        params = dict(zip(keys, combo))
        
        # Instantiate Strategy with these params
        strategy = Crypto15mTrendStrategy(
            bull_trigger=params['bull_trigger'],
            bear_trigger=params['bear_trigger'],
            confirmation_delay=params['confirmation_delay']
        )
        
        # Run Quick Simulation (Silent Mode)
        oms = SimulatedExchange()
        
        # Reset provider stream? No, ReplayProvider needs a reset method or we re-stream list
        # Since we loaded data into self.data (list), we can just iterate self.data
        
        # Optimization: We assume provider.data is sorted
        # State tracking per run
        count = 0
        for row in provider.data: # accessing internal list directly for speed
            # Reconstruct minimal MD
            try:
                md = MarketData(
                    symbol=row['Symbol'],
                    price=float(row['Price']),
                    bid=float(row.get('Bid', 0.0)),
                    ask=float(row.get('Ask', 0.0)),
                    volume=0,
                    timestamp=row['Timestamp'],
                    extra={'source': 'replay', 'close_time': None}
                )
            except: continue

            # Update OMS
            if 'BTC' in md.symbol: oms.update_market('BTC', md.price)
            
            # Analyze
            sigs = strategy.analyze(md)
            for s in sigs:
                ticker = s.symbol if "BTC-USD" not in s.symbol else f"KXBTC-SIM-{count}"
                oms.open_position(ticker, s.side, s.limit_price, s.quantity, s.stop_loss, s.trailing_rules)
            count += 1
            
        # Grade Performance
        stats = oms.get_stats()
        realized = stats['realized']
        unrealized = stats['unrealized']
        total_pnl = realized + unrealized
        
        wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
        total_trades = len(oms.closed_trades) + len(oms.positions)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        
        # Score = PnL (Primary) + WinRate tiebreaker
        results.append({
            'params': params,
            'pnl': total_pnl,
            'trades': total_trades,
            'win_rate': win_rate
        })
        
        if idx % 5 == 0:
            print(f"   🧬 Genome {idx+1}/{len(combinations)}: PnL ${total_pnl:.2f} ({win_rate:.1f}%)")

    # Sort by PnL
    results.sort(key=lambda x: x['pnl'], reverse=True)
    
    best = results[0]
    print("\n🏆 DOMINANT SPECIES DISCOVERED")
    print("------------------------------")
    print(f"Best PnL:      ${best['pnl']:.2f}")
    print(f"Win Rate:      {best['win_rate']:.1f}% ({best['trades']} trades)")
    print("Optimal Parameters:")
    for k, v in best['params'].items():
        print(f"   • {k}: {v}")
        
    print("\n💡 Apply these settings to 'src/strategies/crypto_strategy.py' manually (for now).")

def run_v2_comparison():
    """
    Compare V1 vs V2 strategies head-to-head on historical data.
    """
    print("=" * 60)
    print("🧪 LAB: V1 vs V2 STRATEGY COMPARISON")
    print("=" * 60)
    
    # 1. Setup
    provider = ReplayProvider()
    if not provider.data:
        print("❌ No data to replay.")
        return
    
    # Define strategies to compare (disable confirmation_delay for backtesting)
    strategies = {
        "Crypto V1 (Trend Catcher)": Crypto15mTrendStrategy(confirmation_delay=0),
        "Crypto V2 (Trend + RSI/MACD)": Crypto15mTrendStrategyV2(confirmation_delay=0),
    }
    
    results = {}
    
    for name, strategy in strategies.items():
        print(f"\n🤖 Testing: {name}")
        print("-" * 40)
        
        oms = SimulatedExchange()
        count = 0
        signals = 0
        
        # Replay data
        for row in provider.data:
            try:
                price = float(row['Price'])
                # For contract symbols, Price IS the bid/ask
                # If Bid/Ask columns not in CSV, use Price
                bid = float(row.get('Bid', price)) if 'Bid' in row and row['Bid'] else price
                ask = float(row.get('Ask', price)) if 'Ask' in row and row['Ask'] else price
                
                md = MarketData(
                    symbol=row['Symbol'],
                    price=price,
                    bid=bid,
                    ask=ask,
                    volume=0,
                    timestamp=row['Timestamp'],
                    extra={'source': 'replay', 'close_time': None}
                )
            except:
                continue
            
            # Update OMS
            if 'BTC' in md.symbol:
                oms.update_market('BTC', md.price)
            
            # Analyze
            sigs = strategy.analyze(md)
            for s in sigs:
                signals += 1
                ticker = s.symbol if "BTC-USD" not in s.symbol else f"KXBTC-SIM-{count}"
                oms.open_position(
                    ticker, s.side, s.limit_price, s.quantity,
                    getattr(s, 'stop_loss', 0.0),
                    getattr(s, 'trailing_rules', None),
                    getattr(s, 'expiration_time', None)
                )
            count += 1
        
        # Collect stats
        stats = oms.get_stats()
        wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
        total_trades = len(oms.closed_trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        
        results[name] = {
            'signals': signals,
            'trades': total_trades,
            'realized_pnl': stats['realized'],
            'unrealized_pnl': stats['unrealized'],
            'win_rate': win_rate,
            'wins': wins,
            'losses': total_trades - wins
        }
        
        print(f"   Signals: {signals}")
        print(f"   Closed Trades: {total_trades}")
        print(f"   Realized PnL: ${stats['realized']:.2f}")
        print(f"   Win Rate: {win_rate:.1f}%")
    
    # 2. Summary Table
    print("\n" + "=" * 60)
    print("📊 COMPARISON SUMMARY")
    print("=" * 60)
    print(f"{'Strategy':<30} {'Signals':<10} {'PnL':<12} {'Win Rate':<10}")
    print("-" * 60)
    
    for name, stats in results.items():
        pnl_str = f"${stats['realized_pnl']:+.2f}"
        wr_str = f"{stats['win_rate']:.1f}%"
        print(f"{name:<30} {stats['signals']:<10} {pnl_str:<12} {wr_str:<10}")
    
    # 3. Winner
    print("\n🏆 VERDICT:")
    best = max(results.items(), key=lambda x: x[1]['realized_pnl'])
    print(f"   Best Strategy: {best[0]}")
    print(f"   PnL: ${best[1]['realized_pnl']:.2f} ({best[1]['win_rate']:.1f}% win rate)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--optimize":
        run_optimization()
    elif len(sys.argv) > 1 and sys.argv[1] == "--compare":
        run_v2_comparison()
    elif len(sys.argv) > 1 and sys.argv[1] == "--audit":
        run_audit()
    else:
        print("Usage: python scripts/lab.py [--audit | --optimize | --compare]")
        print("Defaulting to AUDIT mode...")
        run_audit()