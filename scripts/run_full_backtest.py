"""Comprehensive baseline backtest across all markets and strategies.

Runs before live paper trading to establish performance baselines.

Usage:
    python scripts/run_full_backtest.py
"""

import logging
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(
    level=logging.ERROR,  # Suppress expected feature-fallback warnings
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

from src.backtest.data_loader import BacktestDataLoader  # noqa: E402
from src.backtest.engine import BacktestEngine  # noqa: E402
from src.backtest.report import BacktestReportGenerator  # noqa: E402
from src.backtest.stress import StressTestSuite  # noqa: E402


def build_btc_strategies():
    """All BTC strategies in waterfall order."""
    from src.strategies.ml_btc_15m import MLBtc15mStrategy
    from src.strategies.latency_arb import LatencyArbStrategy
    from src.strategies.longshot_fader_v2 import LongshotFaderV2
    from src.strategies.cross_spread_arb import CrossSpreadArbStrategy

    return {
        "ml_btc_15m": MLBtc15mStrategy(min_edge=0.03),
        "latency_arb": LatencyArbStrategy(),
        "longshot_fader_v2": LongshotFaderV2(min_edge=0.02),
        "cross_spread_arb": CrossSpreadArbStrategy(),
    }


def build_hourly_strategies():
    """BTC hourly strategies."""
    from src.strategies.ml_btc_hourly import MLBtcHourlyStrategy
    from src.strategies.ml_btc_15m import MLBtc15mStrategy

    return {
        "ml_btc_hourly": MLBtcHourlyStrategy(min_edge=0.03),
        "ml_btc_15m": MLBtc15mStrategy(min_edge=0.03),
    }


def print_results(name, results):
    """Pretty-print backtest results."""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    print(f"  Trades:        {results['total_trades']}")
    print(f"  Wins/Losses:   {results['wins']}/{results['losses']}")
    print(f"  Win Rate:      {results['win_rate']:.1%}")
    print(f"  Net PnL:       ${results['net_pnl']:.2f}")
    print(f"  Total Fees:    ${results['total_fees']:.2f}")
    print(f"  Sharpe Ratio:  {results['sharpe_ratio']:.2f}")
    print(f"  Max Drawdown:  {results['max_drawdown_pct']:.1%}")
    print(f"  Brier Score:   {results['brier_score']:.4f}")
    print(f"  Profit Factor: {results['profit_factor']:.2f}")
    print(
        f"  Signals:       {results['signals_generated']} generated, {results['signals_rejected']} rejected"
    )

    if results.get("per_strategy"):
        print("\n  Per-Strategy:")
        for sname, data in sorted(
            results["per_strategy"].items(), key=lambda x: -x[1]["trades"]
        ):
            if data["trades"] > 0:
                print(
                    f"    {sname:25s}  {data['trades']:4d} trades  WR={data['win_rate']:.0%}  PnL=${data['pnl']:+.2f}"
                )

    criteria = results.get("criteria", {})
    if criteria:
        print("\n  Validation Criteria:")
        for k, v in criteria.items():
            print(f"    {k:25s}  {'PASS' if v else 'FAIL'}")


def main():
    start = time.time()
    loader = BacktestDataLoader()
    report_gen = BacktestReportGenerator()
    all_results = {}

    # ── BTC 15m ──────────────────────────────────────────────────
    print("\n[1/3] Loading BTC 15m data...")
    seq_15m = loader.build_replay_sequence("btc_15m")
    if seq_15m:
        print(f"       {len(seq_15m)} markets loaded")
        engine = BacktestEngine(build_btc_strategies(), starting_balance=300.0)
        results = engine.run(seq_15m)
        all_results["btc_15m"] = results
        print_results("BTC 15-Minute", results)
        report_gen.generate(results, "backtest_btc_15m.html")

    # ── BTC Hourly (cap at 5000 to avoid memory issues) ──────────
    print("\n[2/3] Loading BTC Hourly data...")
    seq_hr = loader.build_replay_sequence("btc_hourly")
    if seq_hr:
        cap = min(len(seq_hr), 5000)
        seq_hr = seq_hr[:cap]
        print(f"       {cap} markets loaded (of {len(seq_hr)} total)")
        engine = BacktestEngine(build_hourly_strategies(), starting_balance=300.0)
        results = engine.run(seq_hr)
        all_results["btc_hourly"] = results
        print_results("BTC Hourly", results)
        report_gen.generate(results, "backtest_btc_hourly.html")

    # ── Stress Tests ─────────────────────────────────────────────
    print("\n[3/3] Running stress tests...")
    stress = StressTestSuite(starting_balance=300.0)
    stress_results = stress.run_all()
    print("\n  Stress Tests:")
    for name, r in stress_results.items():
        if isinstance(r, dict) and "passed" in r:
            status = "PASS" if r["passed"] else "FAIL"
            print(f"    {name:25s}  {status}  balance=${r.get('final_balance', '?')}")
    print(
        f"    {'Overall':25s}  {'ALL PASSED' if stress_results.get('all_passed') else 'SOME FAILED'}"
    )

    # ── Summary ──────────────────────────────────────────────────
    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print("  BASELINE SUMMARY")
    print(f"{'='*60}")
    for name, results in all_results.items():
        wr = results["win_rate"]
        pnl = results["net_pnl"]
        trades = results["total_trades"]
        wr_ok = "PASS" if wr >= 0.85 else "FAIL"
        pnl_ok = "PASS" if pnl > 0 else "FAIL"
        print(
            f"  {name:20s}  {trades:4d} trades  WR={wr:.0%} [{wr_ok}]  PnL=${pnl:+.2f} [{pnl_ok}]"
        )
    print(f"\n  Elapsed: {elapsed:.1f}s")
    print("  Reports: data/backtest_results/")


if __name__ == "__main__":
    main()
