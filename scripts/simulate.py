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

# Force UTF-8 output on Windows: the result banner ends in an emoji and the
# default cp1252 console encoding made the script die with UnicodeEncodeError
# *after* a successful run. Same guard the dashboard uses.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path so we can import src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bots.weather_bot import CITY_CONFIG, SETTLEMENT_STATIONS, WEATHER_CITIES
from src.data.mock_providers import MockNWSProvider
from src.engine.sim_engine import SimulationEngine
from src.utils.system_utils import prevent_sleep
import os
from dotenv import load_dotenv

# Environment variable that may override the observation station.
STATION_ENV_VAR = "NWS_STATION_ID"


class NonSettlementStationError(RuntimeError):
    """``NWS_STATION_ID`` names a station Kalshi does not settle on.

    PRD FR-1.4 / Phase 1 exit criterion 6: no non-settlement value may feed a
    strategy input. ``simulate.py --live`` builds an ``NWSProvider`` whose
    observations go straight into ``WeatherArbitrageStrategyV2``, so an
    override here is a strategy input in the fullest sense. Per the project's
    abort-on-missing-critical-input rule the run stops rather than quietly
    observing the wrong microclimate: a nearby airport differs from the
    settlement station by up to 3F on a daily high, which is enough to flip a
    2F-wide bracket on roughly half of all days, and the simulation would
    report a confident, wrong result instead of an error.
    """


def resolve_settlement_station(env=None) -> str:
    """The station live simulation observes, validated against the registry.

    Returns the NY settlement station by default. An ``NWS_STATION_ID``
    override is honoured ONLY when it names one of the settlement stations in
    ``src.bots.weather_bot.WEATHER_CITIES``; anything else raises
    :class:`NonSettlementStationError`.

    :param env: mapping to read from (defaults to ``os.environ``); present so
        the guard test can exercise the override path without mutating the
        process environment.
    """
    env = os.environ if env is None else env
    # PRD FR-1.4 / Phase 1 exit criterion 6: the default comes from the
    # authoritative city registry, never from a hardcoded airport.
    default_station = CITY_CONFIG["NY"].settlement_station

    raw = env.get(STATION_ENV_VAR)
    if raw is None or not str(raw).strip():
        return default_station

    station = str(raw).strip().upper()
    if station in SETTLEMENT_STATIONS:
        return station

    known = ", ".join(
        f"{c.settlement_station} ({c.kalshi_series})" for c in WEATHER_CITIES
    )
    raise NonSettlementStationError(
        f"{STATION_ENV_VAR}={raw!r} is not a Kalshi settlement station. "
        f"Kalshi settles the daily-high markets on: {known}. "
        f"Observing any other station feeds a different microclimate into the "
        f"strategy, so this run is aborted (PRD FR-1.4, Phase 1 exit criterion "
        f"6). Remove {STATION_ENV_VAR} from your .env to use "
        f"{default_station}, or set it to one of the stations above."
    )


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
            # PRD FR-1.4 / Phase 1 exit criterion 6. The default AND any
            # environment override are validated against the authoritative
            # city registry; a non-settlement station aborts the run instead
            # of silently feeding the strategy the wrong microclimate.
            station = resolve_settlement_station()
            print(f"Observation station: {station} (settlement station)")
            providers = [NWSProvider(user_agent, station)]
        else:
            # MockNWSProvider's own default also comes from CITY_CONFIG.
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

    try:
        run_simulation(strategy_name, args.days, args.optimize, args.live)
    except NonSettlementStationError as exc:
        # Abort loudly with the remediation, never a traceback the operator
        # has to decode (PRD FR-1.4 / Phase 1 exit criterion 6).
        print(f"\nABORT: {exc}", file=sys.stderr)
        sys.exit(2)
