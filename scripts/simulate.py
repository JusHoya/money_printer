"""
Simulation Engine for Money Printer Strategies.

This script runs historical or real-time simulations for the trading strategies
in the demo environment. It is designed to optimize prediction parameters
before deployment.

Usage:
    python scripts/simulate.py --strategy <strategy_name> --days <number_of_days>

Options:
    --strategy  Name of the strategy to test (only 'weather' remains).
    --days      Number of days to simulate (default: 30).
    --optimize  Flag to run hyperparameter optimization.
"""

import argparse
import sys
import os

# Add project root to path so we can import src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.mock_providers import MockNWSProvider
from src.engine.sim_engine import SimulationEngine
from src.utils.system_utils import prevent_sleep
import os
from dotenv import load_dotenv


def run_simulation(strategy_name, days, optimize, use_live=False):
    prevent_sleep()
    print("Initializing Simulation Environment...")
    print(f"Target Strategy: {strategy_name}")
    print(f"Duration: {days} steps")
    print(f"Data Source: {'LIVE (Real-World)' if use_live else 'MOCK (Synthetic)'}")

    if use_live:
        load_dotenv()
        # Import Live Providers here to avoid hard dependency if just running mock
        from src.data.nws_provider import NWSProvider
        # from src.data.kalshi_provider import KalshiProvider

    # Strategy Selection Factory
    if strategy_name.lower() == "weather":
        from src.strategies.weather_strategy import WeatherArbitrageStrategyV2

        strategy = WeatherArbitrageStrategyV2()
        if use_live:
            user_agent = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
            station = os.getenv("NWS_STATION_ID", "KJFK")
            providers = [NWSProvider(user_agent, station)]
        else:
            providers = [MockNWSProvider()]

    # Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto strategy branches
    # (btc_15m / btc_hourly / crypto) were removed with the deleted crypto
    # strategies. Weather is the only simulatable strategy.
    else:
        print(f"Unknown strategy: {strategy_name} (only 'weather' is supported)")
        return

    print("\n--- Simulation Started ---")

    # Initialize Engine
    engine = SimulationEngine(strategy, providers)

    # Run Simulation
    # Mapping 'days' to 'steps' for this mock implementation
    engine.run(steps=days)

    print("\n--- Simulation Complete ---")
    if engine.pnl > 0:
        print(f"RESULT: PROFITABLE (+${engine.pnl:.2f}) 🚀")
    else:
        print(f"RESULT: LOSS (${engine.pnl:.2f}) 📉")
        print("Recommendation: DO NOT DEPLOY. Tweak parameters.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Money Printer Simulation Engine")
    parser.add_argument(
        "--strategy",
        type=str,
        default=None,
        help="Strategy to simulate (weather)",
    )
    parser.add_argument(
        "--bot",
        type=str,
        default=None,
        choices=["weather"],
        help="Bot preset (weather). Takes precedence over --strategy.",
    )
    parser.add_argument("--days", type=int, default=30, help="Days (steps) to simulate")
    parser.add_argument(
        "--optimize", action="store_true", help="Enable parameter optimization loop"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use LIVE data sources (NWS/Kalshi) instead of mocks",
    )

    args = parser.parse_args()

    # --bot maps to a strategy name for run_simulation
    BOT_MAP = {
        "weather": "weather",
    }

    if args.bot:
        strategy_name = BOT_MAP[args.bot]
    elif args.strategy:
        strategy_name = args.strategy
    else:
        parser.error("at least one of --bot or --strategy is required")

    run_simulation(strategy_name, args.days, args.optimize, args.live)
