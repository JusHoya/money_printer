"""
Simulation Engine for Money Printer Strategies.

This script runs historical or real-time simulations for the trading strategies
in the demo environment. It is designed to optimize prediction parameters
before deployment.

Usage:
    python scripts/simulate.py --strategy <strategy_name> --days <number_of_days>

Options:
    --strategy  Name of the strategy to test (e.g., 'weather', 'crypto').
    --days      Number of days to simulate (default: 30).
    --optimize  Flag to run hyperparameter optimization.
"""

import argparse
import sys
import os

# Add project root to path so we can import src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.strategies.weather_strategy import WeatherArbitrageStrategy
from src.data.mock_providers import MockNWSProvider, MockKalshiProvider
from src.engine.sim_engine import SimulationEngine
from src.utils.system_utils import prevent_sleep
import os
from dotenv import load_dotenv

def run_simulation(strategy_name, days, optimize, use_live=False):
    prevent_sleep()
    print(f"Initializing Simulation Environment...")
    print(f"Target Strategy: {strategy_name}")
    print(f"Duration: {days} steps")
    print(f"Data Source: {'LIVE (Real-World)' if use_live else 'MOCK (Synthetic)'}")
    
    if use_live:
        load_dotenv()
        # Import Live Providers here to avoid hard dependency if just running mock
        from src.data.nws_provider import NWSProvider
        # from src.data.kalshi_provider import KalshiProvider

    # Strategy Selection Factory
    if strategy_name.lower() == 'weather':
        from src.strategies.weather_strategy import WeatherArbitrageStrategyV2
        strategy = WeatherArbitrageStrategyV2()
        if use_live:
            user_agent = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
            station = os.getenv("NWS_STATION_ID", "KJFK")
            providers = [NWSProvider(user_agent, station)]
        else:
            providers = [MockNWSProvider()]
            
    elif strategy_name.lower() == 'crypto':
        from src.strategies.crypto_strategy import Crypto15mTrendStrategyV2
        strategy = Crypto15mTrendStrategyV2()
        if use_live:
            from src.data.coinbase_provider import CoinbaseProvider
            # Default to BTC-USD
            providers = [CoinbaseProvider("BTC-USD")]
        else:
             # Mock provider for crypto (needs implementation if we want offline tests)
             print("Mock Crypto Provider not implemented yet. Use --live for now.")
             return
    else:
        print(f"Unknown strategy: {strategy_name}")
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
    parser.add_argument("--strategy", type=str, required=True, help="Strategy to simulate (weather|crypto)")
    parser.add_argument("--days", type=int, default=30, help="Days (steps) to simulate")
    parser.add_argument("--optimize", action="store_true", help="Enable parameter optimization loop")
    parser.add_argument("--live", action="store_true", help="Use LIVE data sources (NWS/Kalshi) instead of mocks")
    
    args = parser.parse_args()
    
    run_simulation(args.strategy, args.days, args.optimize, args.live)
