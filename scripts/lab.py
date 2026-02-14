"""
🧪 MONEY PRINTER LABORATORY (v2.0)
The Scientific Method Pipeline for Trading Strategy Optimization.

Modes:
  --audit     : Comprehensive analysis of harvest logs (Accuracy, PnL, Coverage)
  --optimize  : Genetic evolutionary grid search for V2 strategy parameters
  --refine    : The Loop™ - Audits, checks threshold (default 80%), and auto-optimizes if needed.

Usage:
  python scripts/lab.py --audit
  python scripts/lab.py --optimize [--strategy crypto|weather]
  python scripts/lab.py --refine [--threshold 80]
"""

import os
import sys
import glob
import json
import itertools
import argparse
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any

# Ensure project root is in path
sys.path.append(os.getcwd())

from src.core.interfaces import MarketData
from src.core.matching_engine import SimulatedExchange
from src.strategies.crypto_strategy import Crypto15mTrendStrategyV2
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2
from src.utils.logger import logger

# --- CONFIGURATION ---
LOG_DIR = "logs"
AUDIT_REPORT_FILE = os.path.join(LOG_DIR, "audit_report.json")
OPTIMAL_PARAMS_FILE = os.path.join(LOG_DIR, "optimal_params.json")

class Lab:
    def __init__(self):
        self.data = []
        self._load_data()
        
    def _load_data(self):
        """Loads and merges all data logs."""
        files = glob.glob(os.path.join(LOG_DIR, "data_*.csv"))
        if not files:
            print("❌ No log files found.")
            return

        df_list = []
        for f in files:
            try:
                # Optimized load: only read needed columns
                df = pd.read_csv(f, usecols=['Timestamp', 'Symbol', 'Price', 'Type', 'Bid', 'Ask'])
                if 'MARKET_DATA' in df['Type'].values:
                    df = df[df['Type'] == 'MARKET_DATA']
                    df_list.append(df)
            except ValueError:
                 # Fallback if strict cols missing
                try:
                    df = pd.read_csv(f)
                    if 'MARKET_DATA' in df['Type'].values:
                        df = df[df['Type'] == 'MARKET_DATA']
                        df_list.append(df)
                except: pass
            except Exception: pass
            
        if not df_list:
            print("❌ No MARKET_DATA found in logs.")
            return

        print(f"📂 Loading {len(df_list)} log files...")
        full_df = pd.concat(df_list, ignore_index=True)
        full_df['Timestamp'] = pd.to_datetime(full_df['Timestamp'])
        full_df = full_df.sort_values('Timestamp')
        
        self.data = full_df.to_dict('records')
        print(f"✅ Loaded {len(self.data):,} data points.")

    def run_audit(self) -> Dict[str, Any]:
        """
        AUDIT MODE: Analyzes historical performance of CURRENT strategies on PAST data.
        Returns report dict.
        """
        print("\n🔍 LAB: AUDIT REPORT")
        print("====================")
        
        if not self.data: return {}

        # Instantiate V2 Strategies
        strategies = {
            "Crypto V2": Crypto15mTrendStrategyV2(),
            "Weather V2": WeatherArbitrageStrategyV2()
        }
        
        results = {}
        
        # We need a shared OMS? No, separate OMS per strategy to isolate PnL
        oms_instances = {name: SimulatedExchange() for name in strategies}
        
        count = 0
        for row in self.data:
            try:
                price = float(row['Price'])
                bid = float(row.get('Bid', price)) if not pd.isna(row.get('Bid')) else price
                ask = float(row.get('Ask', price)) if not pd.isna(row.get('Ask')) else price

                md = MarketData(
                    symbol=row['Symbol'],
                    price=price,
                    bid=bid,
                    ask=ask,
                    volume=0,
                    timestamp=row['Timestamp'],
                    extra={'source': 'audit'}
                )
            except: continue

            # Route to correct strategy
            for name, strategy in strategies.items():
                # Filter for strategy relevance
                is_crypto = "BTC" in md.symbol or "ETH" in md.symbol
                is_weather = "HIGH" in md.symbol or "LOW" in md.symbol or "RAIN" in md.symbol
                
                if name == "Crypto V2" and is_crypto:
                    oms = oms_instances[name]
                    # Update OMS market price for tracking
                    if "BTC" in md.symbol: oms.update_market('BTC', md.price)
                    
                    sigs = strategy.analyze(md)
                    for s in sigs:
                        ticker = s.symbol if "BTC-USD" not in s.symbol else f"KXBTC-AUDIT-{count}"
                        oms.open_position(ticker, s.side, s.limit_price, s.quantity, 
                                          getattr(s, 'stop_loss', 0), 
                                          getattr(s, 'trailing_rules', None),
                                          getattr(s, 'expiration_time', None))

                elif name == "Weather V2" and is_weather:
                    oms = oms_instances[name]
                    # Weather doesn't need "update_market" global price usually, specific to contract
                    # But we simulate fills
                    sigs = strategy.analyze(md)
                    for s in sigs:
                        oms.open_position(s.symbol, s.side, s.limit_price, s.quantity,
                                          getattr(s, 'stop_loss', 0),
                                          getattr(s, 'trailing_rules', None),
                                          getattr(s, 'expiration_time', None))
                                          
            count += 1
            
        print(f"Processed {count} ticks.")
        
        # Collate Results
        print("\n📊 STRATEGY PERFORMANCE")
        print(f"{'Strategy':<20} {'Win Rate':<10} {'PnL':<15} {'Trades':<10}")
        print("-" * 60)
        
        for name, oms in oms_instances.items():
            stats = oms.get_stats()
            wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
            total_closed = len(oms.closed_trades)
            win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
            total_pnl = stats['realized'] + stats['unrealized']
            
            results[name] = {
                "win_rate": win_rate,
                "pnl": total_pnl,
                "trades": total_closed + len(oms.positions),
                "closed": total_closed
            }
            
            print(f"{name:<20} {win_rate:>5.1f}%    ${total_pnl:>10.2f}    {results[name]['trades']:>5}")

        # Save Report
        with open(AUDIT_REPORT_FILE, 'w') as f:
            json.dump(results, f, indent=2)
            
        print(f"\n✅ Audit saved to {AUDIT_REPORT_FILE}")
        return results

    def run_optimization(self, target_strategy: str = "all"):
        """
        OPTIMIZE MODE: Grid search for best parameters.
        """
        print("\n🧬 LAB: EVOLUTIONARY OPTIMIZATION")
        print("===================================")
        
        if not self.data: return

        # Parameter Grids
        # Crypto V2
        crypto_grid = {
            'bull_trigger': [0.55, 0.60, 0.65],
            'bear_trigger': [0.45, 0.40, 0.35],
            'confirmation_delay': [0, 60], # Reduced for speed
            'mean_reversion_threshold': [0.03, 0.05]
        }
        
        # Weather V2
        weather_grid = {
            'threshold': [0.10, 0.15, 0.20],
            'min_edge_degrees': [1.5, 2.0, 3.0]
        }
        
        best_configs = {}
        
        strategies_to_test = []
        if target_strategy in ["all", "crypto"]:
            strategies_to_test.append(("Crypto V2", Crypto15mTrendStrategyV2, crypto_grid))
        if target_strategy in ["all", "weather"]:
            strategies_to_test.append(("Weather V2", WeatherArbitrageStrategyV2, weather_grid))
            
        for name, strat_class, grid in strategies_to_test:
            print(f"\n⚙️  Optimizing {name}...")
            keys = list(grid.keys())
            combos = list(itertools.product(*grid.values()))
            print(f"   Testing {len(combos)} genomes...")
            
            results = []
            
            for i, vals in enumerate(combos):
                params = dict(zip(keys, vals))
                
                # Run Simulation
                oms = SimulatedExchange()
                strategy = strat_class(**params)
                
                count = 0 
                
                # Optimized loop: filter data beforehand? 
                # For simplicity, we stream all, route relevance inside loop (slower but safer)
                # Or pre-filter self.data for this strategy type to speed up
                
                # Pre-filter for speed
                relevant_data = self.data # Default all
                if name == "Crypto V2":
                    relevant_data = [d for d in self.data if "BTC" in d['Symbol'] or "ETH" in d['Symbol']]
                elif name == "Weather V2":
                    relevant_data = [d for d in self.data if "HIGH" in d['Symbol'] or "LOW" in d['Symbol']]
                
                for row in relevant_data:
                    try:
                        md = MarketData(
                            symbol=row['Symbol'],
                            price=float(row['Price']),
                            bid=float(row.get('Bid', row['Price'])),
                            ask=float(row.get('Ask', row['Price'])),
                            volume=0, timestamp=row['Timestamp'], extra={'source': 'opt'}
                        )
                    except: continue
                    
                    if name == "Crypto V2" and "BTC" in md.symbol:
                        oms.update_market('BTC', md.price)
                        
                    sigs = strategy.analyze(md)
                    for s in sigs:
                        # Unique ID for simulation
                        ticker = s.symbol if "BTC-USD" not in s.symbol else f"KXBTC-{i}-{count}"
                        oms.open_position(ticker, s.side, s.limit_price, s.quantity,
                                          getattr(s, 'stop_loss', 0),
                                          getattr(s, 'trailing_rules', None),
                                          getattr(s, 'expiration_time', None))
                    count += 1
                    
                # Score
                stats = oms.get_stats()
                wins = len([t for t in oms.closed_trades if t['pnl'] > 0])
                total_closed = len(oms.closed_trades)
                win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
                total_pnl = stats['realized'] + stats['unrealized']
                
                # Composite Score (PnL weighted, Win Rate tiebreaker)
                score = total_pnl + (win_rate * 0.1) 
                
                results.append({
                    "params": params,
                    "score": score,
                    "pnl": total_pnl,
                    "win_rate": win_rate,
                    "trades": total_closed
                })
                
                if (i+1) % 5 == 0:
                    print(f"   Generating... {i+1}/{len(combos)}")

            # Find Best
            best = max(results, key=lambda x: x['score'])
            best_configs[name] = best
            
            print(f"\n🏆 BEST {name} GENOME:")
            print(f"   PnL: ${best['pnl']:.2f}")
            print(f"   Win Rate: {best['win_rate']:.1f}% ({best['trades']} trades)")
            print(f"   Params: {best['params']}")
            
        # Save Optimal Params
        with open(OPTIMAL_PARAMS_FILE, 'w') as f:
            json.dump(best_configs, f, indent=2)
        print(f"\n✅ Optimization complete. Saved to {OPTIMAL_PARAMS_FILE}")

    def run_refinement(self, threshold: float = 80.0):
        """
        REFINE MODE: The wrapper. 
        1. Run Audit.
        2. Check if any strategy < threshold.
        3. If so, Optimize specific strategy.
        """
        print(f"\n🔧 LAB: REFINEMENT LOOP (Threshold: {threshold}%)")
        print("==========================================")
        
        # 1. Audit
        report = self.run_audit()
        if not report: 
            print("❌ Audit failed (no data?). Aborting.")
            return

        strategies_to_optimize = []
        
        for name, stats in report.items():
            wr = stats['win_rate']
            if wr < threshold:
                print(f"⚠️  {name} Win Rate ({wr:.1f}%) is BELOW threshold ({threshold}%). Queuing optimization.")
                strategies_to_optimize.append(name)
            else:
                print(f"✅ {name} Win Rate ({wr:.1f}%) is good.")
                
        if not strategies_to_optimize:
            print("\n✨ All systems nominal. No refinement needed.")
            return
            
        # 2. Optimize
        print(f"\n🔄 initiating Optimization for: {', '.join(strategies_to_optimize)}")
        
        # Map friendly names to "crypto"/"weather" keys
        for s_name in strategies_to_optimize:
            target = "crypto" if "Crypto" in s_name else "weather"
            self.run_optimization(target_strategy=target)
            
        print("\n✅ Refinement Cycle Complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Money Printer Laboratory")
    parser.add_argument("--audit", action="store_true", help="Run strategy audit")
    parser.add_argument("--optimize", action="store_true", help="Run parameter optimization")
    parser.add_argument("--refine", action="store_true", help="Run audit and optimize if needed")
    parser.add_argument("--strategy", type=str, default="all", help="Target strategy for optimization (crypto/weather)")
    parser.add_argument("--threshold", type=float, default=80.0, help="Win rate threshold for refinement")
    
    args = parser.parse_args()
    
    lab = Lab()
    
    if args.refine:
        lab.run_refinement(threshold=args.threshold)
    elif args.optimize:
        lab.run_optimization(target_strategy=args.strategy)
    elif args.audit:
        lab.run_audit()
    else:
        # Default behavior
        print("No mode selected. Defaulting to --audit")
        lab.run_audit()