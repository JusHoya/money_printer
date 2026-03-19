"""Run backtests from the command line.

Usage:
    python scripts/run_backtest.py --market btc_15m
    python scripts/run_backtest.py --market btc_hourly --balance 500
    python scripts/run_backtest.py --market all
    python scripts/run_backtest.py --stress
    python scripts/run_backtest.py --walk-forward --folds 3
"""

import argparse
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Money Printer V2 Backtester")
    parser.add_argument(
        "--market",
        default="btc_15m",
        help="Market type: btc_15m, btc_hourly, all (default: btc_15m)",
    )
    parser.add_argument("--balance", type=float, default=300.0, help="Starting balance")
    parser.add_argument("--stress", action="store_true", help="Run stress tests only")
    parser.add_argument("--walk-forward", action="store_true", help="Walk-forward mode")
    parser.add_argument("--folds", type=int, default=3, help="Walk-forward folds")
    parser.add_argument("--report", action="store_true", help="Generate HTML report")
    args = parser.parse_args()

    if args.stress:
        from src.backtest.stress import StressTestSuite

        suite = StressTestSuite(starting_balance=args.balance)
        results = suite.run_all()
        print("\n=== STRESS TEST RESULTS ===")
        for name, r in results.items():
            if isinstance(r, dict) and "passed" in r:
                status = "PASS" if r["passed"] else "FAIL"
                print(f"  {name}: {status} (balance=${r.get('final_balance', '?')})")
        print(
            f"\n  Overall: {'ALL PASSED' if results.get('all_passed') else 'SOME FAILED'}"
        )
        return

    from src.backtest.data_loader import BacktestDataLoader
    from src.backtest.engine import BacktestEngine
    from src.strategies.ml_btc_15m import MLBtc15mStrategy

    loader = BacktestDataLoader()
    strategies = {"ml_btc_15m": MLBtc15mStrategy()}
    engine = BacktestEngine(strategies, starting_balance=args.balance)

    if args.walk_forward:
        logger.info("Walk-forward backtest: %s (%d folds)", args.market, args.folds)
        results = engine.run_walk_forward(loader, args.market, n_folds=args.folds)
    else:
        logger.info("Single-pass backtest: %s", args.market)
        if args.market == "all":
            sequence = loader.build_multi_market_sequence()
        else:
            sequence = loader.build_replay_sequence(args.market)
        results = engine.run(sequence)

    # Print results
    print("\n=== BACKTEST RESULTS ===")
    for key in [
        "total_trades",
        "wins",
        "losses",
        "win_rate",
        "net_pnl",
        "total_fees",
        "sharpe_ratio",
        "max_drawdown_pct",
        "brier_score",
        "profit_factor",
        "signals_generated",
        "signals_rejected",
    ]:
        val = results.get(key)
        if val is not None:
            if isinstance(val, float):
                print(f"  {key}: {val:.4f}")
            else:
                print(f"  {key}: {val}")

    # Criteria
    criteria = results.get("criteria", {})
    if criteria:
        print("\n  Validation Criteria:")
        for k, v in criteria.items():
            print(f"    {k}: {'PASS' if v else 'FAIL'}")

    # Per-strategy
    ps = results.get("per_strategy", {})
    if ps:
        print("\n  Per-Strategy:")
        for name, data in ps.items():
            print(
                f"    {name}: {data.get('trades', 0)} trades, "
                f"WR={data.get('win_rate', 0):.1%}, PnL=${data.get('pnl', 0):.2f}"
            )

    # Generate report
    if args.report:
        from src.backtest.report import BacktestReportGenerator

        gen = BacktestReportGenerator()
        path = gen.generate(results)
        print(f"\n  Report: {path}")


if __name__ == "__main__":
    main()
