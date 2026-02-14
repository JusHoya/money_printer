
# Money Printer Dashboard 🖨️
# A simple, high-fidelity ASCII dashboard for monitoring trading bot status.

import os
import sys
import time
import csv
from datetime import datetime
from src.visualization.mascot import Mascot

# Clear screen helper
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

class Dashboard:
    def __init__(self):
        self.start_time = datetime.now()
        self.total_pnl = 0.0
        self.active_strategies = []
        self.logs = []
        self.alerts = []
        self.latest_prices = {}
        
        # Mascot
        self.mascot = Mascot()
        self.last_known_pnl = 0.0
        
        # Strategy Performance Tracking
        self.strategy_stats = {}  # {strategy_name: {signals, wins, losses, pnl}}
        
        # Logging Setup
        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        self.log_dir = "logs"
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
            
        self.session_log_path = os.path.join(self.log_dir, f"session_{timestamp}.log")
        self.data_log_path = os.path.join(self.log_dir, f"data_{timestamp}.csv")
        self.portfolio_log_path = os.path.join(self.log_dir, f"portfolio_{timestamp}.csv")
        
        # Init Data CSV
        with open(self.data_log_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Symbol", "Price", "Type", "Status"]) 

        # Init Portfolio CSV
        with open(self.portfolio_log_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Equity", "Cash", "Exposure", "Realized_PnL", "Unrealized_PnL"])

        self._write_to_log(f"--- SESSION STARTED: {self.start_time} ---")

    def log_portfolio(self, risk_manager):
        """Record portfolio metrics to CSV and Log."""
        if not risk_manager: return
        
        ts = datetime.now().isoformat()
        bal = risk_manager.balance
        realized = risk_manager.daily_pnl
        unrealized = risk_manager.unrealized_pnl
        exposure = risk_manager.get_current_exposure()
        equity = bal + exposure + unrealized
        
        # CSV
        try:
            with open(self.portfolio_log_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([ts, equity, bal, exposure, realized, unrealized])
        except Exception:
            pass
            
        # Session Log (Summary every few minutes or on change? Let's just do a high-level summary periodically)
        # For now, we will let the render loop handle visual updates and CSV handle high-freq data.

    def _write_to_log(self, msg: str):
        """Append text to the session log file."""
        try:
            with open(self.session_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
        except Exception as e:
            # Fallback if logging fails, don't crash app
            pass

    def log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{ts}] {message}"
        
        # UI
        self.logs.append(full_msg)
        if len(self.logs) > 10:
            self.logs.pop(0)
            
        # File
        self._write_to_log(full_msg)

    def alert(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{ts}] 🚨 {message}"
        
        # UI
        self.alerts.append(full_msg)
        if len(self.alerts) > 5:
            self.alerts.pop(0)
            
        # File
        self._write_to_log(full_msg)

    def update_price(self, symbol: str, price: float, **kwargs):
        # Store Price + Timestamp + Metadata
        self.latest_prices[symbol] = {
            'price': price,
            'ts': time.time(),
            'extra': kwargs
        }
        
        # Record Data for Training
        ts = datetime.now().isoformat()
        try:
            with open(self.data_log_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([ts, symbol, price, "MARKET_DATA", "REAL"])
        except Exception:
            pass # Don't crash dashboard on log fail

    def record_signal(self, signal, status="EXECUTED", strategy_name=None):
        """Log a trade signal specifically for training data."""
        ts = datetime.now().isoformat()
        try:
            with open(self.data_log_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([ts, signal.symbol, signal.limit_price, f"SIGNAL_{signal.side.upper()}", status])
        except Exception:
            pass
        
        # Track strategy performance
        if strategy_name:
            self.record_strategy_signal(strategy_name)
    
    def record_strategy_signal(self, strategy_name: str):
        """Record a signal generated by a strategy."""
        if strategy_name not in self.strategy_stats:
            self.strategy_stats[strategy_name] = {
                'signals': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'active': 0
            }
        self.strategy_stats[strategy_name]['signals'] += 1
        self.strategy_stats[strategy_name]['active'] += 1
    
    def record_strategy_trade_result(self, strategy_name: str, pnl: float):
        """Record a closed trade result for strategy tracking."""
        if strategy_name not in self.strategy_stats:
            self.strategy_stats[strategy_name] = {
                'signals': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'active': 0
            }
        
        self.strategy_stats[strategy_name]['pnl'] += pnl
        self.strategy_stats[strategy_name]['active'] = max(0, self.strategy_stats[strategy_name]['active'] - 1)
        
        if pnl > 0:
            self.strategy_stats[strategy_name]['wins'] += 1
        else:
            self.strategy_stats[strategy_name]['losses'] += 1

    def render(self, risk_manager=None):
        if risk_manager:
            self.log_portfolio(risk_manager)
            
        clear_screen()
        # Header
        print(f"=================================================================================")
        print(f"   MONEY PRINTER CONTROL CENTER 🖨️💵   |   Active: {str(datetime.now() - self.start_time).split('.')[0]}")
        print(f"=================================================================================")
        
        # Mascot Section
        if risk_manager:
            current_pnl = risk_manager.daily_pnl
            pnl_change = current_pnl - self.last_known_pnl
            self.last_known_pnl = current_pnl
            
            self.mascot.set_state(pnl_change, current_pnl)
            frame = self.mascot.get_frame()
            print(frame)
        
        # Portfolio Section (Enhanced)
        if risk_manager:
            bal = risk_manager.balance
            realized_pnl = risk_manager.daily_pnl
            unreal_pnl = risk_manager.unrealized_pnl
            exposure = risk_manager.get_current_exposure()
            
            # Total Equity = Cash + Positions Value
            # Positions Value = Cost Basis (Exposure) + Unrealized PnL
            total_equity = bal + exposure + unreal_pnl
            
            exposure_pct = (exposure / total_equity) * 100 if total_equity > 0 else 0
            
            print(f" 💼 PORTFOLIO STATUS")
            print(f"    Equity: ${total_equity:.2f}    |   Cash: ${bal:.2f}     |   Exposure: ${exposure:.2f} ({exposure_pct:.1f}%)")
            print(f"    Realized: ${realized_pnl:+.2f}    |   Unrealized: ${unreal_pnl:+.2f}")
            print(f"---------------------------------------------------------------------------------")
        else:
            print(f" 💰 Total PnL: ${self.total_pnl:,.2f}          |   📉 Drawdown: 0.0%   ")
            print(f"---------------------------------------------------------------------------------")
        
        # Market Data
        print(f" MARKET DATA (Live Feed - Active Only)")
        now = time.time()
        active_count = 0
        
        # Group by Series Prefix to prevent accumulation (e.g. KXBTC15M-A vs KXBTC15M-B)
        # We want to show only the NEWEST active ticker for each series.
        series_groups = {}
        
        for sym, data in self.latest_prices.items():
            # Check TTL (5 mins)
            if (now - data['ts']) > 300:
                continue
                
            # Extract Base Series
            base = sym.split('-')[0]
            if "Coinbase" in sym: base = "COINBASE"
            
            if base not in series_groups:
                series_groups[base] = []
            series_groups[base].append((sym, data))
            
        # Select Winner for each group (Newest Timestamp)
        display_list = []
        for base, items in series_groups.items():
            # Sort by TS descending
            items.sort(key=lambda x: x[1]['ts'], reverse=True)
            winner_sym, winner_data = items[0]
            display_list.append((winner_sym, winner_data))
            
        # Sort for display stability
        display_list.sort(key=lambda x: x[0])
        
        for sym, data in display_list:
            price = data['price']
            extra_str = ""
            
            # Show Max Temp if available
            extra = data.get('extra', {})
            if extra.get('max_temp'):
                extra_str = f" | Max: {extra['max_temp']:.1f}F"
                
            print(f"  {sym:<35} | {price:>5.2f}{extra_str}")
            active_count += 1
                
        if active_count == 0:
            print(f"  (No active feeds)")
            
        print(f"---------------------------------------------------------------------------------")
        
        # Alerts
        if self.alerts:
            print(f" ⚠️ ACTIVE ALERTS")
            for alert in self.alerts:
                print(f"  {alert}")
            print(f"---------------------------------------------------------------------------------")
        
        # Strategy Performance Section
        if self.strategy_stats:
            print(f" 📊 STRATEGY PERFORMANCE")
            print(f"  {'Strategy':<25} {'Signals':<8} {'W/L':<10} {'Win%':<8} {'PnL':<12}")
            print(f"  {'-'*65}")
            for name, stats in self.strategy_stats.items():
                total = stats['wins'] + stats['losses']
                win_rate = (stats['wins'] / total * 100) if total > 0 else 0.0
                wl_str = f"{stats['wins']}/{stats['losses']}"
                pnl_str = f"${stats['pnl']:+.2f}"
                # Color indicator based on performance
                indicator = "🟢" if win_rate >= 60 else "🟡" if win_rate >= 40 else "🔴"
                print(f"  {indicator} {name:<23} {stats['signals']:<8} {wl_str:<10} {win_rate:<7.1f}% {pnl_str:<12}")
            print(f"---------------------------------------------------------------------------------")

        # System Logs
        print(f" 📜 SYSTEM LOG")
        for log in self.logs:
            print(f"  {log}")
            
        print(f"=================================================================================")
        print(f" COMMANDS: [Q]uit | [K]ill Switch | [R]eset PnL")
