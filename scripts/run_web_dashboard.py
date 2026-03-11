"""
Money Printer — Web Dashboard entry point.

Usage:
    python scripts/run_web_dashboard.py
    python scripts/run_web_dashboard.py --bot btc_15m
    python scripts/run_web_dashboard.py --bot btc_15m --bot weather
    python scripts/run_web_dashboard.py --port 8050
"""

import argparse
import os
import sys
import threading
import time
import webbrowser

# Make sure the project root is on the path regardless of cwd
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv(override=True)

from src.bots.registry import BotRegistry
from src.utils.system_utils import prevent_sleep
from src.utils.logger import logger

# Import bots so they register themselves
import src.bots  # noqa: F401


# ---------------------------------------------------------------------------
# Re-use OrchestratorEngine from run_dashboard, extended with active_bots
# ---------------------------------------------------------------------------
from scripts.run_dashboard import OrchestratorEngine  # noqa: E402

from src.web.state_manager import StateManager
from src.web.server import create_app


def _run_market_loop(engine: OrchestratorEngine):
    """Background thread: mirrors the market loop in OrchestratorEngine.run()
    but does not block the main thread (which is taken by uvicorn)."""
    engine.market_loop()


def main():
    parser = argparse.ArgumentParser(description="Money Printer Web Dashboard")
    parser.add_argument(
        "--bot",
        action="append",
        dest="bots",
        help=(
            "Bot to run (can specify multiple). Default: all bots. "
            f"Available: {BotRegistry.list_bots()}"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8050,
        help="Port to listen on (default: 8050)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the browser",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------ #
    # Build orchestrator
    # ------------------------------------------------------------------ #
    engine = OrchestratorEngine(bot_names=args.bots)

    prevent_sleep()
    engine.dashboard.log("Web Dashboard Initializing...")

    # Connect shared providers (same as OrchestratorEngine.run())
    if engine.nws.connect():
        engine.dashboard.log("NWS Connected")
    else:
        engine.dashboard.alert("NWS Connection Failed")

    if engine.coinbase.connect():
        engine.dashboard.log("Coinbase Connected")
    else:
        engine.dashboard.alert("Coinbase Connection Failed")

    for bot in engine.bots:
        bot.setup(kalshi=engine.kalshi, coinbase=engine.coinbase, nws=engine.nws)

    if engine.kalshi:
        try:
            bal = engine.kalshi.get_balance()
            engine.risk_manager.update_balance(bal)
            engine.dashboard.log(f"Piggy Bank Initialized: ${bal:.2f}")
        except Exception as e:
            engine.dashboard.alert(f"Balance Sync Failed: {e}")

    # ------------------------------------------------------------------ #
    # Start market loop in a background thread
    # ------------------------------------------------------------------ #
    market_thread = threading.Thread(target=_run_market_loop, args=(engine,), daemon=True)
    market_thread.start()
    engine.dashboard.log(f"Market loop started. Bots: {[b.name for b in engine.bots]}")

    # ------------------------------------------------------------------ #
    # Build web layer
    # ------------------------------------------------------------------ #
    state_manager = StateManager(engine)
    app = create_app(state_manager, engine)

    # ------------------------------------------------------------------ #
    # Open browser after a short delay
    # ------------------------------------------------------------------ #
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        def _open_browser():
            time.sleep(1.5)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Start uvicorn (blocks main thread)
    # ------------------------------------------------------------------ #
    import uvicorn

    logger.info(f"[Web] Starting server at {url}")
    print(f"\n  Money Printer Web Dashboard → {url}\n  Press Ctrl+C to stop.\n")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",  # keep uvicorn quiet; app logs go to logger
    )


if __name__ == "__main__":
    main()
