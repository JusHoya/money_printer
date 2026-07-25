# Money Printer Dashboard 🖨️
# A simple, high-fidelity ASCII dashboard for monitoring trading bot status.

import os
import sys
import time
import csv
import json
from datetime import datetime
import re
from src.visualization.mascot import Mascot

# Data-CSV schema (FR-0.7 harvester + FR-1.1 bracket semantics). The first
# five columns preserve the legacy layout (Timestamp, Symbol, Price, Type,
# Status) so existing consumers (train_from_csv.py, lab.py, web state_manager
# — all of which read by column name) keep working; every later column is
# APPEND-ONLY at the tail.
# "Price" keeps its legacy display semantics (best bid, falling back to
# ask/last); "Last" is the true last-trade price. "Depth" holds a JSON
# top-3 orderbook snapshot ({"yes": [[price, qty], ...], "no": [...]})
# and is only populated on Type=DEPTH rows, which MARKET_DATA consumers
# filter out by Type.
#
# StrikeType / FloorStrike / CapStrike (PRD FR-1.1) carry the market's
# settlement semantics onto every harvested row so a recorded ladder can be
# settled offline through ``src.core.bracket_payoff`` with no metadata
# re-fetch — the post-hoc re-fetch is exactly how the old weather book ended
# up with inverted B/T semantics. They are written EMPTY (never 0) when the
# market has no such strike: a ``greater`` contract genuinely has no cap, and
# 0.0 would read back as a 0F strike.
#
# BACKWARD COMPATIBILITY: rows written before these columns existed are
# narrower and their files' header rows lack the three names. Every reader in
# this repo goes through csv.DictReader or pandas-by-name, so an old file
# simply yields no bracket keys; readers must use ``.get()`` and treat the
# absence as "unusable for brackets" (fail loud), never as a default.
BRACKET_CSV_COLUMNS = [
    "StrikeType",
    "FloorStrike",
    "CapStrike",
]

DATA_CSV_HEADER = [
    "Timestamp",
    "Symbol",
    "Price",
    "Type",
    "Status",
    "Bid",
    "Ask",
    "NoBid",
    "NoAsk",
    "Last",
    "Volume",
    "Depth",
] + BRACKET_CSV_COLUMNS

# Force UTF-8 output on Windows to support emoji/Unicode
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# Clear screen helper
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


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
        self.portfolio_log_path = os.path.join(
            self.log_dir, f"portfolio_{timestamp}.csv"
        )

        # Init Data CSV (FR-0.7 harvester schema)
        with open(self.data_log_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(DATA_CSV_HEADER)

        # Init Portfolio CSV
        with open(self.portfolio_log_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Timestamp",
                    "Equity",
                    "Cash",
                    "Exposure",
                    "Realized_PnL",
                    "Unrealized_PnL",
                ]
            )

        self._write_to_log(f"--- SESSION STARTED: {self.start_time} ---")

    def log_portfolio(self, risk_manager):
        """Record portfolio metrics to CSV and Log."""
        if not risk_manager:
            return

        ts = datetime.now().isoformat()
        bal = risk_manager.balance
        realized = risk_manager.daily_pnl
        unrealized = risk_manager.unrealized_pnl
        exposure = risk_manager.get_current_exposure()
        equity = bal + exposure

        # CSV
        try:
            with open(self.portfolio_log_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([ts, equity, bal, exposure, realized, unrealized])
        except Exception:
            pass

        # Session Log (Summary every few minutes or on change? Let's just do a high-level summary periodically)
        # For now, we will let the render loop handle visual updates and CSV handle high-freq data.

    def _write_to_log(self, msg: str):
        """Append text to the session log file."""
        try:
            with open(self.session_log_path, "a", encoding="utf-8") as f:
                f.write(f"{msg}\n")
        except Exception:
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
        if len(self.alerts) > 50:
            self.alerts.pop(0)

        # File
        self._write_to_log(full_msg)

    def _append_data_row(self, row):
        """Append one row to the data CSV; never crash the dashboard."""
        try:
            with open(self.data_log_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except Exception:
            pass  # Don't crash dashboard on log fail

    @staticmethod
    def _bracket_cols(kwargs) -> list:
        """The FR-1.1 bracket columns for one row, in header order.

        ``strike_type`` is normalised to a lowercase string; the two strikes
        are written as numbers when present. Anything absent or None becomes
        an EMPTY string — never ``0`` — because a ``greater`` market has no
        cap and a ``less`` market has no floor, and a zero would replay as a
        0F strike.
        """
        raw_type = kwargs.get("strike_type")
        strike_type = "" if raw_type is None else str(raw_type).strip().lower()

        def _strike(key):
            val = kwargs.get(key)
            if val is None or val == "":
                return ""
            try:
                return float(val)
            except (TypeError, ValueError):
                # Unparseable metadata is recorded blank so the reader fails
                # loud on replay rather than inheriting a bogus strike.
                return ""

        return [strike_type, _strike("floor_strike"), _strike("cap_strike")]

    def update_price(self, symbol: str, price: float, **kwargs):
        # Store Price + Timestamp + Metadata
        self.latest_prices[symbol] = {
            "price": price,
            "ts": time.time(),
            "extra": kwargs,
        }

        # Record data for training / microstructure harvesting (FR-0.7) plus
        # bracket semantics (FR-1.1). Quote fields come from the caller's
        # kwargs (bid/ask/no_bid/no_ask/last/volume); absent fields are
        # recorded as empty strings so non-market rows (temperatures, spot
        # prices) stay valid.
        ts = datetime.now().isoformat()

        def _col(key):
            val = kwargs.get(key)
            return "" if val is None else val

        self._append_data_row(
            [
                ts,
                symbol,
                price,
                "MARKET_DATA",
                "REAL",
                _col("bid"),
                _col("ask"),
                _col("no_bid"),
                _col("no_ask"),
                _col("last"),
                _col("volume"),
                "",
            ]
            + self._bracket_cols(kwargs)
        )

    def record_depth(self, symbol: str, levels: dict, last_price=None, **kwargs):
        """Record an hourly top-3 orderbook snapshot row (FR-0.7).

        ``levels`` is ``{"yes": [(price, qty), ...], "no": [...]}`` — resting
        bids per side, best-first, float dollars (KalshiProvider.
        fetch_orderbook output). The row is tagged Type=DEPTH with the raw
        levels JSON-encoded in the Depth column; MARKET_DATA consumers filter
        on Type and never see these rows.

        ``strike_type`` / ``floor_strike`` / ``cap_strike`` keyword arguments
        are recorded in the same columns as a MARKET_DATA row (FR-1.1), so a
        depth snapshot is settleable offline on its own terms.
        """
        yes = list(levels.get("yes") or [])[:3]
        no = list(levels.get("no") or [])[:3]
        best_yes_bid = yes[0][0] if yes else ""
        best_no_bid = no[0][0] if no else ""
        # A resting NO bid at q implies a YES ask at 1-q (and vice versa)
        implied_yes_ask = round(1.0 - no[0][0], 4) if no else ""
        implied_no_ask = round(1.0 - yes[0][0], 4) if yes else ""

        ts = datetime.now().isoformat()
        depth_json = json.dumps({"yes": yes, "no": no}, separators=(",", ":"))
        lp = last_price if last_price is not None else ""
        self._append_data_row(
            [
                ts,
                symbol,
                lp,
                "DEPTH",
                "REAL",
                best_yes_bid,
                implied_yes_ask,
                best_no_bid,
                implied_no_ask,
                lp,
                "",
                depth_json,
            ]
            + self._bracket_cols(kwargs)
        )

    def record_signal(self, signal, status="EXECUTED", strategy_name=None):
        """Log a trade signal specifically for training data."""
        ts = datetime.now().isoformat()
        self._append_data_row(
            [
                ts,
                signal.symbol,
                signal.limit_price,
                f"SIGNAL_{signal.side.upper()}",
                status,
            ]
            # Pad to the full header width so pandas never sees a ragged file.
            # Derived from the header so appending a column cannot desync it.
            + [""] * (len(DATA_CSV_HEADER) - 5)
        )

        # Track strategy performance
        if strategy_name:
            self.record_strategy_signal(strategy_name)

    def record_strategy_signal(self, strategy_name: str):
        """Record a signal generated by a strategy."""
        if strategy_name not in self.strategy_stats:
            self.strategy_stats[strategy_name] = {
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "active": 0,
            }
        self.strategy_stats[strategy_name]["signals"] += 1
        self.strategy_stats[strategy_name]["active"] += 1

    def record_strategy_trade_result(self, strategy_name: str, pnl: float):
        """Record a closed trade result for strategy tracking."""
        if strategy_name not in self.strategy_stats:
            self.strategy_stats[strategy_name] = {
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "active": 0,
            }

        self.strategy_stats[strategy_name]["pnl"] += pnl
        self.strategy_stats[strategy_name]["active"] = max(
            0, self.strategy_stats[strategy_name]["active"] - 1
        )

        if pnl > 0:
            self.strategy_stats[strategy_name]["wins"] += 1
        else:
            self.strategy_stats[strategy_name]["losses"] += 1

    def render(self, risk_manager=None):
        if risk_manager:
            self.log_portfolio(risk_manager)

        clear_screen()
        # Header
        print(
            "================================================================================="
        )
        print(
            f"   MONEY PRINTER CONTROL CENTER 🖨️💵   |   Active: {str(datetime.now() - self.start_time).split('.')[0]}"
        )
        print(
            "================================================================================="
        )

        # Mascot Section
        if risk_manager:
            current_pnl = risk_manager.daily_pnl
            pnl_change = current_pnl - self.last_known_pnl
            self.last_known_pnl = current_pnl

            has_open = len(risk_manager.exchange.positions) > 0
            self.mascot.set_state(pnl_change, current_pnl, has_open_trades=has_open)
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
            total_equity = bal + exposure

            exposure_pct = (exposure / total_equity) * 100 if total_equity > 0 else 0

            # Task F: lifetime net PnL (survives daily/balance-sync resets).
            # Read straight off the exchange's immutable cumulative ledger.
            try:
                cumulative_net = risk_manager.exchange.get_cumulative_net_pnl()
            except AttributeError:
                cumulative_net = None

            print(" 💼 PORTFOLIO STATUS")
            print(
                f"    Equity: ${total_equity:.2f}    |   Cash: ${bal:.2f}     |   Exposure: ${exposure:.2f} ({exposure_pct:.1f}%)"
            )
            print(
                f"    Realized: ${realized_pnl:+.2f}    |   Unrealized: ${unreal_pnl:+.2f}"
            )
            if cumulative_net is not None:
                print(
                    f"    Cumulative Net (lifetime, all fees): ${cumulative_net:+.2f}"
                )
            print(
                "---------------------------------------------------------------------------------"
            )
        else:
            print(
                f" 💰 Total PnL: ${self.total_pnl:,.2f}          |   📉 Drawdown: 0.0%   "
            )
            print(
                "---------------------------------------------------------------------------------"
            )

        # Market Data
        print(" MARKET DATA (Live Feed - Active Only)")
        now = time.time()
        active_count = 0

        # Group by Series Prefix to prevent accumulation (e.g. KXBTC15M-A vs KXBTC15M-B)
        # We want to show only the NEWEST active ticker for each series.
        series_groups = {}

        for sym, data in self.latest_prices.items():
            # Check TTL (5 mins)
            if (now - data["ts"]) > 300:
                continue

            # SPECIAL: Capture Coinbase Price for reference
            if "Coinbase" in sym:
                series_groups.setdefault("COINBASE", []).append((sym, data))
                continue

            # SPECIAL: Handle BTC Hourly (KXBTCD) separately for Ladder View
            if "KXBTCD" in sym or "KXBTC-" in sym:
                # Check if it's an hourly (not daily/weekly)
                # Ticker format: KXBTCD-YYMMMDDHH-Txxxxx
                # 15m format: KXBTC15M...
                if "15M" not in sym:
                    if "BTC_HOURLY" not in series_groups:
                        series_groups["BTC_HOURLY"] = []
                    series_groups["BTC_HOURLY"].append((sym, data))
                    continue

            # Extract Base Series
            base = sym.split("-")[0]

            if base not in series_groups:
                series_groups[base] = []
            series_groups[base].append((sym, data))

        # Select Winner for each group (Newest Timestamp)
        display_list = []
        # Processing Groups for Display
        display_list = []

        # 1. Get Spot Price if available
        coinbase_price = 0.0
        if "COINBASE" in series_groups:
            # Sort by TS descending
            items = series_groups["COINBASE"]
            items.sort(key=lambda x: x[1]["ts"], reverse=True)
            winner_sym, winner_data = items[0]
            coinbase_price = winner_data["price"]
            display_list.append((winner_sym, winner_data))
            del series_groups["COINBASE"]

        # 2. Process BTC Hourly Ladder
        if "BTC_HOURLY" in series_groups:
            markets = series_groups["BTC_HOURLY"]

            if coinbase_price <= 0:
                # If we don't have a spot price, we can't calculate closest.
                # Fallback: Just show the ones with highest active price (most likely to be NTM)
                markets.sort(key=lambda x: x[1]["price"], reverse=True)
                for sym, data in markets[:3]:
                    display_list.append((sym, data))
            else:
                # 1. Parse all strikes
                parsed_markets = []
                for sym, data in markets:
                    try:
                        # Clean symbol of suffix (e.g. " (1h)")
                        clean_sym = sym.split(" ")[0]

                        # Parse Strike: KXBTCD-26FEB1718-T98000
                        parts = clean_sym.split("-")
                        strike_part = parts[-1]
                        # Remove 'T' and any other non-digit/dot chars just in case
                        strike_val = float(re.sub(r"[^\d.]", "", strike_part))
                        parsed_markets.append((strike_val, sym, data))
                    except Exception:
                        pass

                if parsed_markets:
                    # 2. Find Closest Markets to Spot
                    # Sort by distance to spot
                    parsed_markets.sort(key=lambda x: abs(x[0] - coinbase_price))

                    # 3. Take Top 3 Closest
                    # This naturally selects the Center and its immediate neighbors (Upper/Lower)
                    # regardless of whether the interval is 250, 100, or 500.
                    top_3 = parsed_markets[:3]

                    # 4. Sort by Strike Descending (Ladder View)
                    top_3.sort(key=lambda x: x[0], reverse=True)

                    for _, sym, data in top_3:
                        display_list.append((sym, data))

            del series_groups["BTC_HOURLY"]

        # 3. Standard Processing for others (Winner takes all)
        for base, items in series_groups.items():
            # Sort by TS descending
            items.sort(key=lambda x: x[1]["ts"], reverse=True)
            winner_sym, winner_data = items[0]
            display_list.append((winner_sym, winner_data))

        # Sort for display stability
        display_list.sort(key=lambda x: x[0])

        for sym, data in display_list:
            price = data["price"]
            extra_str = ""

            # Show Max Temp if available
            extra = data.get("extra", {})
            if extra.get("max_temp"):
                extra_str = f" | Max: {extra['max_temp']:.1f}F"

            print(f"  {sym:<35} | {price:>5.2f}{extra_str}")
            active_count += 1

        if active_count == 0:
            print("  (No active feeds)")

        print(
            "---------------------------------------------------------------------------------"
        )

        # Alerts
        if self.alerts:
            print(" ⚠️ ACTIVE ALERTS")
            for alert in self.alerts:
                print(f"  {alert}")
            print(
                "---------------------------------------------------------------------------------"
            )

        # Strategy Performance Section
        if self.strategy_stats:
            print(" 📊 STRATEGY PERFORMANCE")
            print(
                f"  {'Strategy':<25} {'Signals':<8} {'W/L':<10} {'Win%':<8} {'PnL':<12}"
            )
            print(f"  {'-'*65}")
            for name, stats in self.strategy_stats.items():
                total = stats["wins"] + stats["losses"]
                win_rate = (stats["wins"] / total * 100) if total > 0 else 0.0
                wl_str = f"{stats['wins']}/{stats['losses']}"
                pnl_str = f"${stats['pnl']:+.2f}"
                # Color indicator based on performance
                indicator = "🟢" if win_rate >= 60 else "🟡" if win_rate >= 40 else "🔴"
                print(
                    f"  {indicator} {name:<23} {stats['signals']:<8} {wl_str:<10} {win_rate:<7.1f}% {pnl_str:<12}"
                )
            print(
                "---------------------------------------------------------------------------------"
            )

        # System Logs
        print(" 📜 SYSTEM LOG")
        for log in self.logs:
            print(f"  {log}")

        print(
            "================================================================================="
        )
        print(" COMMANDS: [Q]uit | [K]ill Switch | [R]eset PnL")
