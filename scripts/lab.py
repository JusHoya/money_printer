"""
🧪 MONEY PRINTER LABORATORY (v2.0)
The Scientific Method Pipeline for Trading Strategy Optimization.

Modes:
  --audit     : Comprehensive analysis of harvest logs (Accuracy, PnL, Coverage)
  --optimize  : Genetic evolutionary grid search for V2 strategy parameters
  --refine    : The Loop™ - Audits, checks threshold (default 80%), and auto-optimizes if needed.

Usage:
  python scripts/lab.py --audit
  python scripts/lab.py --optimize [--strategy weather]
  python scripts/lab.py --refine [--threshold 80]
"""

import os
import sys

# Force UTF-8 output on Windows: this script's banners are emoji-heavy and the
# default cp1252 console encoding aborted the run with UnicodeEncodeError
# before any analysis was printed. Same guard the dashboard uses.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import csv
import glob
import json
import itertools
import argparse
import pandas as pd
from typing import Dict, Any

# Ensure project root is in path
sys.path.append(os.getcwd())

from src.backtest.data_loader import (
    HARVEST_BRACKET_COLUMNS,
    HarvestBracketStats,
    bracket_extra_from_row,
    spec_for_harvest_row,
    strip_market_suffix,
)
from src.core.bracket_payoff import is_weather_symbol
from src.core.interfaces import MarketData
from src.core.matching_engine import SimulatedExchange
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2

# Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto strategies (V2/V3)
# were deleted; the lab now audits/optimizes weather only.

# --- CONFIGURATION ---
LOG_DIR = "logs"
AUDIT_REPORT_FILE = os.path.join(LOG_DIR, "audit_report.json")
OPTIMAL_PARAMS_FILE = os.path.join(LOG_DIR, "optimal_params.json")

BOT_SYMBOL_FILTERS = {
    "weather": ["HIGH", "LOW", "RAIN"],
}

BOT_STRATEGY_KEYS = {
    "weather": ["Weather V2"],
}


BASE_COLUMNS = ["Timestamp", "Symbol", "Price", "Type", "Bid", "Ask"]


def _csv_header(path):
    """Column names of a data CSV, or [] if unreadable."""
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh))
    except Exception:
        return []


class Lab:
    def __init__(self, bot_filter: str = None):
        self.bot_filter = bot_filter
        self.data = []
        # FR-1.1 bracket-usability tally over the replayed harvest.
        self.bracket_stats = HarvestBracketStats()
        self._load_data()

    def _load_data(self):
        """Loads and merges all data logs.

        Reads by column NAME so both harvest widths work: files written before
        the FR-1.1 bracket columns were appended simply lack them, and their
        rows are counted as unusable-for-brackets rather than being parsed with
        invented semantics.
        """
        files = glob.glob(os.path.join(LOG_DIR, "data_*.csv"))
        if not files:
            print("❌ No log files found.")
            return

        df_list = []
        for f in files:
            header = _csv_header(f)
            # Only ask pandas for columns this file actually has — a legacy
            # (pre-bracket) file must still load, not raise.
            wanted = [c for c in BASE_COLUMNS if c in header]
            wanted += [c for c in HARVEST_BRACKET_COLUMNS if c in header]
            if not all(c in header for c in HARVEST_BRACKET_COLUMNS):
                self.bracket_stats.files_missing_columns.append(os.path.basename(f))
            try:
                if wanted:
                    df = pd.read_csv(f, usecols=wanted)
                else:
                    df = pd.read_csv(f)
                self.bracket_stats.files += 1
                if "Type" in df.columns and "MARKET_DATA" in df["Type"].values:
                    df = df[df["Type"] == "MARKET_DATA"]
                    df_list.append(df)
            except Exception:
                pass

        if not df_list:
            print("❌ No MARKET_DATA found in logs.")
            return

        print(f"📂 Loading {len(df_list)} log files...")
        full_df = pd.concat(df_list, ignore_index=True)
        # Harvest timestamps are datetime.isoformat(), which omits the
        # microseconds when they happen to be zero. Concatenating files then
        # yields a mixed-precision column that pandas' single-format inference
        # rejects outright, so parse them as ISO8601 explicitly.
        try:
            full_df["Timestamp"] = pd.to_datetime(
                full_df["Timestamp"], format="ISO8601"
            )
        except (TypeError, ValueError):
            full_df["Timestamp"] = pd.to_datetime(full_df["Timestamp"], format="mixed")
        full_df = full_df.sort_values("Timestamp")

        self.data = full_df.to_dict("records")

        # Establish bracket semantics ONCE per row here (not per genome in the
        # optimizer). Each row's spec is cached on the record; failures are
        # counted at this call site and reported below.
        for row in self.data:
            row["_bracket_extra"] = bracket_extra_from_row(row)
            if not is_weather_symbol(strip_market_suffix(row.get("Symbol"))):
                continue
            self.bracket_stats.market_rows += 1
            self.bracket_stats.weather_rows += 1
            row["_bracket_spec"] = spec_for_harvest_row(row, self.bracket_stats)

        print(f"✅ Loaded {len(self.data):,} data points.")
        for line in self.bracket_stats.report_lines():
            print(f"   {line}")

    def _market_data(self, row, source: str):
        """Row -> MarketData, carrying FR-1.1 bracket semantics in ``extra``.

        Returns ``None`` when the row's prices are unusable. Bracket fields are
        forwarded verbatim: ``None`` for a pre-bracket row, which makes
        ``parse_bracket_spec`` raise downstream instead of guessing.
        """
        try:
            price = float(row["Price"])
            bid = float(row.get("Bid", price)) if not pd.isna(row.get("Bid")) else price
            ask = float(row.get("Ask", price)) if not pd.isna(row.get("Ask")) else price
        except Exception:
            return None

        extra = {"source": source}
        extra.update(row.get("_bracket_extra") or bracket_extra_from_row(row))
        return MarketData(
            symbol=row["Symbol"],
            price=price,
            bid=bid,
            ask=ask,
            volume=0,
            timestamp=row["Timestamp"],
            extra=extra,
        )

    def run_audit(self) -> Dict[str, Any]:
        """
        AUDIT MODE: Analyzes historical performance of CURRENT strategies on PAST data.
        Returns report dict.
        """
        print("\n🔍 LAB: AUDIT REPORT")
        print("====================")

        if not self.data:
            return {}

        # Instantiate strategies
        all_strategies = {
            "Weather V2": WeatherArbitrageStrategyV2(),
        }

        # Filter by bot if specified
        if self.bot_filter and self.bot_filter in BOT_STRATEGY_KEYS:
            allowed = BOT_STRATEGY_KEYS[self.bot_filter]
            strategies = {k: v for k, v in all_strategies.items() if k in allowed}
        else:
            strategies = all_strategies

        results = {}

        # We need a shared OMS? No, separate OMS per strategy to isolate PnL
        oms_instances = {name: SimulatedExchange() for name in strategies}

        count = 0
        for row in self.data:
            md = self._market_data(row, "audit")
            if md is None:
                continue

            # Route to correct strategy
            for name, strategy in strategies.items():
                # Filter for strategy relevance
                is_weather = (
                    "HIGH" in md.symbol or "LOW" in md.symbol or "RAIN" in md.symbol
                )

                if name == "Weather V2" and is_weather:
                    oms = oms_instances[name]
                    # Weather doesn't need "update_market" global price usually, specific to contract
                    # But we simulate fills
                    sigs = strategy.analyze(md)
                    for s in sigs:
                        oms.open_position(
                            s.symbol,
                            s.side,
                            s.limit_price,
                            s.quantity,
                            getattr(s, "stop_loss", 0),
                            getattr(s, "trailing_rules", None),
                            getattr(s, "expiration_time", None),
                        )

            count += 1

        print(f"Processed {count} ticks.")

        # FR-1.1: state offline-settleability explicitly, every run.
        print("\n🧾 BRACKET SEMANTICS (PRD FR-1.1)")
        for line in self.bracket_stats.report_lines():
            print(f"   {line}")
        results["_bracket_coverage"] = {
            "weather_rows": self.bracket_stats.weather_rows,
            "usable": self.bracket_stats.usable,
            "unusable_pre_bracket_rows": self.bracket_stats.unusable,
            "legacy_files": self.bracket_stats.files_missing_columns,
        }

        # Collate Results
        print("\n📊 STRATEGY PERFORMANCE")
        print(f"{'Strategy':<20} {'Win Rate':<10} {'PnL':<15} {'Trades':<10}")
        print("-" * 60)

        for name, oms in oms_instances.items():
            stats = oms.get_stats()
            wins = len([t for t in oms.closed_trades if t["pnl"] > 0])
            total_closed = len(oms.closed_trades)
            win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
            total_pnl = stats["realized"] + stats["unrealized"]

            results[name] = {
                "win_rate": win_rate,
                "pnl": total_pnl,
                "trades": total_closed + len(oms.positions),
                "closed": total_closed,
            }

            print(
                f"{name:<20} {win_rate:>5.1f}%    ${total_pnl:>10.2f}    {results[name]['trades']:>5}"
            )

        # Save Report
        with open(AUDIT_REPORT_FILE, "w") as f:
            json.dump(results, f, indent=2)

        print(f"\n✅ Audit saved to {AUDIT_REPORT_FILE}")
        return results

    def run_optimization(self, target_strategy: str = "all"):
        """
        OPTIMIZE MODE: Grid search for best parameters.
        """
        print("\n🧬 LAB: EVOLUTIONARY OPTIMIZATION")
        print("===================================")

        if not self.data:
            return

        # Parameter Grids
        # Weather V2
        weather_grid = {
            "threshold": [0.10, 0.15, 0.20],
            "min_edge_degrees": [1.5, 2.0, 3.0],
        }

        best_configs = {}

        strategies_to_test = []
        if target_strategy in ["all", "weather"]:
            strategies_to_test.append(
                ("Weather V2", WeatherArbitrageStrategyV2, weather_grid)
            )

        # Filter by bot if active
        if self.bot_filter and self.bot_filter in BOT_STRATEGY_KEYS:
            allowed = BOT_STRATEGY_KEYS[self.bot_filter]
            strategies_to_test = [
                (n, c, g) for n, c, g in strategies_to_test if n in allowed
            ]

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
                relevant_data = self.data  # Default all
                if name == "Weather V2":
                    relevant_data = [
                        d
                        for d in self.data
                        if "HIGH" in d["Symbol"] or "LOW" in d["Symbol"]
                    ]

                for row in relevant_data:
                    md = self._market_data(row, "opt")
                    if md is None:
                        continue

                    sigs = strategy.analyze(md)
                    for s in sigs:
                        oms.open_position(
                            s.symbol,
                            s.side,
                            s.limit_price,
                            s.quantity,
                            getattr(s, "stop_loss", 0),
                            getattr(s, "trailing_rules", None),
                            getattr(s, "expiration_time", None),
                        )
                    count += 1

                # Score
                stats = oms.get_stats()
                wins = len([t for t in oms.closed_trades if t["pnl"] > 0])
                total_closed = len(oms.closed_trades)
                win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
                total_pnl = stats["realized"] + stats["unrealized"]

                # Composite Score (PnL weighted, Win Rate tiebreaker)
                score = total_pnl + (win_rate * 0.1)

                results.append(
                    {
                        "params": params,
                        "score": score,
                        "pnl": total_pnl,
                        "win_rate": win_rate,
                        "trades": total_closed,
                    }
                )

                if (i + 1) % 5 == 0:
                    print(f"   Generating... {i+1}/{len(combos)}")

            # Find Best
            best = max(results, key=lambda x: x["score"])
            best_configs[name] = best

            print(f"\n🏆 BEST {name} GENOME:")
            print(f"   PnL: ${best['pnl']:.2f}")
            print(f"   Win Rate: {best['win_rate']:.1f}% ({best['trades']} trades)")
            print(f"   Params: {best['params']}")

        # Save Optimal Params
        with open(OPTIMAL_PARAMS_FILE, "w") as f:
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
            # Underscore-prefixed keys are report metadata (e.g. the FR-1.1
            # bracket-coverage block), not strategies.
            if name.startswith("_") or "win_rate" not in stats:
                continue
            wr = stats["win_rate"]
            if wr < threshold:
                print(
                    f"⚠️  {name} Win Rate ({wr:.1f}%) is BELOW threshold ({threshold}%). Queuing optimization."
                )
                strategies_to_optimize.append(name)
            else:
                print(f"✅ {name} Win Rate ({wr:.1f}%) is good.")

        if not strategies_to_optimize:
            print("\n✨ All systems nominal. No refinement needed.")
            return

        # 2. Optimize
        print(f"\n🔄 initiating Optimization for: {', '.join(strategies_to_optimize)}")

        # Only weather strategies remain post-teardown.
        for _s_name in strategies_to_optimize:
            self.run_optimization(target_strategy="weather")

        print("\n✅ Refinement Cycle Complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Money Printer Laboratory")
    parser.add_argument("--audit", action="store_true", help="Run strategy audit")
    parser.add_argument(
        "--optimize", action="store_true", help="Run parameter optimization"
    )
    parser.add_argument(
        "--refine", action="store_true", help="Run audit and optimize if needed"
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Statistical validation: learning vs lucky?",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="all",
        help="Target strategy for optimization (weather)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        help="Win rate threshold for refinement",
    )
    parser.add_argument(
        "--bot",
        type=str,
        default=None,
        choices=["weather"],
        help="Filter strategies by bot (weather)",
    )

    args = parser.parse_args()

    lab = Lab(bot_filter=args.bot)

    if args.validate:
        print("\n" + "=" * 60)
        print("  STATISTICAL VALIDATION HARNESS")
        print("=" * 60)
        from src.ml.trade_journal import TradeJournal
        from src.ml.validation_harness import TradingValidationHarness

        journal = TradeJournal()
        outcomes = journal.load_all()
        if not outcomes:
            print("  No trade outcomes in journal — run the dashboard first.")
        else:
            pnls = [o.pnl for o in outcomes]
            harness = TradingValidationHarness()
            result = harness.verdict(trade_pnls=pnls)
            print(f"\n  Verdict: {result['verdict']}")
            print(f"  Summary: {result['summary']}")
            details = result.get("details", {})
            mc = details.get("monte_carlo", {})
            if mc:
                print(f"\n  Monte Carlo p-value: {mc.get('p_value', 'N/A')}")
                print(f"  Observed Sharpe: {mc.get('observed_sharpe', 'N/A'):.4f}")
            ds = details.get("deflated_sharpe", {})
            if ds:
                print(f"  Deflated Sharpe: {ds.get('deflated_sharpe', 'N/A'):.4f}")
                print(f"  Significant: {ds.get('is_significant', 'N/A')}")
            wr = journal.win_rate_by_feature()
            if wr.get("by_confidence"):
                print("\n  Win Rate by Confidence:")
                for bucket, stats in wr["by_confidence"].items():
                    print(
                        f"    {bucket}: {stats['rate']:.1%} ({stats['wins']}/{stats['total']})"
                    )
            if wr.get("by_tte"):
                print("\n  Win Rate by Time-to-Expiry:")
                for bucket, stats in wr["by_tte"].items():
                    print(
                        f"    {bucket}: {stats['rate']:.1%} ({stats['wins']}/{stats['total']})"
                    )
        print("=" * 60)
    elif args.refine:
        lab.run_refinement(threshold=args.threshold)
    elif args.optimize:
        lab.run_optimization(target_strategy=args.strategy)
    elif args.audit:
        lab.run_audit()
    else:
        # Default behavior
        print("No mode selected. Defaulting to --audit")
        lab.run_audit()
