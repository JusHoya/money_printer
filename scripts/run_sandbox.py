"""Money Printer V2 — Sandbox (Paper Trading) Mode.

Runs the full trading pipeline against LIVE market data but places
NO real orders.  Tracks all simulated trades, computes a rolling
scorecard, and saves trade logs for model refinement.

Usage:
    python scripts/run_sandbox.py
    python scripts/run_sandbox.py --bot btc_15m --bot btc_hourly
    python scripts/run_sandbox.py --balance 300 --duration 480
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(override=True)

import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sandbox")

from src.backtest.sandbox import SandboxRunner  # noqa: E402
from src.bots.registry import BotRegistry  # noqa: E402
from src.data.coinbase_provider import CoinbaseProvider  # noqa: E402
from src.data.kalshi_provider import KalshiProvider  # noqa: E402
from src.data.nws_provider import NWSProvider  # noqa: E402
from src.utils.system_utils import prevent_sleep  # noqa: E402

# Import bots to trigger registration
import src.bots  # noqa: E402, F401

LOG_DIR = Path("data/sandbox_logs")


class SandboxOrchestrator:
    """Live paper trading orchestrator with scorecard tracking.

    Extends the existing OrchestratorEngine pattern with:
    - SandboxRunner for settlement tracking and validation
    - Trade log persistence to disk
    - Periodic scorecard printing
    - Circuit breaker integration
    """

    def __init__(self, bot_names=None, starting_balance=300.0):
        self.sandbox = SandboxRunner(
            starting_balance=starting_balance,
            min_trades_for_validation=100,
        )
        self.risk_manager = self.sandbox.risk_manager
        self.running = True
        self.start_time = time.time()

        # Providers
        self.coinbase = CoinbaseProvider("BTC-USD")

        nws_ua = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
        self.nws = NWSProvider(nws_ua, ["KNYC", "KLAX", "KMDW", "KMIA"])

        k_id = os.getenv("KALSHI_KEY_ID")
        k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        k_url = os.getenv("KALSHI_API_URL")
        self.kalshi = None
        if k_id and k_key:
            self.kalshi = KalshiProvider(k_id, k_key, k_url, read_only=True)

        # Bots
        if bot_names:
            self.bots = [BotRegistry.create(name) for name in bot_names]
        else:
            self.bots = BotRegistry.create_all()

        self.active_bots = {bot.name for bot in self.bots}
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Sandbox initialized: balance=$%.2f bots=%s",
            starting_balance,
            [b.name for b in self.bots],
        )

    def connect_providers(self):
        """Connect to live data feeds."""
        if self.coinbase.connect():
            logger.info("Coinbase connected")
        else:
            logger.warning("Coinbase connection failed — BTC strategies may not work")

        if self.nws.connect():
            logger.info("NWS connected")
        else:
            logger.warning("NWS connection failed — weather strategies may not work")

        if self.kalshi:
            if self.kalshi.connect():
                logger.info("Kalshi connected (read-only)")
            else:
                logger.warning("Kalshi connection failed")

        # Setup bots
        for bot in self.bots:
            bot.setup(kalshi=self.kalshi, coinbase=self.coinbase, nws=self.nws)

    def market_loop(self):
        """Background thread: position updates + bot ticks."""
        tick_count = 0

        while self.running:
            try:
                tick_count += 1

                # Update live prices on open positions
                if self.kalshi:
                    for pos in list(self.risk_manager.exchange.positions):
                        symbol = pos["symbol"]
                        if "KX" in symbol:
                            try:
                                k_data = self.kalshi.fetch_latest(symbol)
                                if k_data:
                                    real_price = (
                                        k_data.bid if k_data.bid > 0 else k_data.ask
                                    )
                                    if real_price > 0:
                                        self.risk_manager.exchange.update_market_price(
                                            symbol, real_price
                                        )
                                    self.risk_manager.update_market_data(
                                        symbol, k_data.price
                                    )
                            except Exception:
                                pass

                # Tick all active bots
                for bot in self.bots:
                    if bot.name not in self.active_bots:
                        continue
                    try:
                        bot.tick(self.risk_manager, _SilentDashboard())
                    except Exception as e:
                        logger.error("[%s] Tick error: %s", bot.name, e)

                # Periodic scorecard (every 60 ticks = ~5 minutes)
                if tick_count % 60 == 0:
                    self._print_scorecard()

                time.sleep(5)

            except Exception as e:
                logger.error("Market loop error: %s", e)
                time.sleep(5)

    def _print_scorecard(self):
        """Print current sandbox status."""
        sc = self.sandbox.get_scorecard()
        elapsed = (time.time() - self.start_time) / 60
        positions = len(self.risk_manager.exchange.positions)

        logger.info(
            "SCORECARD | %.0f min | Trades: %d | Wins: %d | WR: %.0f%% | "
            "PnL: $%.2f | Balance: $%.2f | Open: %d | Ready: %s",
            elapsed,
            sc["trades"],
            sc["wins"],
            sc["win_rate"] * 100,
            sc["pnl"],
            sc["balance"],
            positions,
            sc["ready_for_live"],
        )

    def save_trade_log(self):
        """Persist trade log to JSON for later analysis."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"sandbox_trades_{ts}.json"
        data = {
            "scorecard": self.sandbox.get_scorecard(),
            "trades": self.sandbox.trade_log,
            "equity_curve": self.sandbox.equity_curve,
            "circuit_breaker": self.sandbox.circuit_breaker.get_status(),
            "exchange_stats": self.risk_manager.exchange.get_stats(),
            "audit_trail": self.risk_manager.exchange.order_audit[-100:],
        }
        log_path.write_text(json.dumps(data, indent=2, default=str))
        logger.info("Trade log saved: %s", log_path)

    def run(self, duration_minutes=None):
        """Main entry point — run sandbox until stopped or duration reached."""
        prevent_sleep()
        self.connect_providers()

        logger.info("=" * 60)
        logger.info("  SANDBOX MODE — Paper Trading Active")
        logger.info("  No real orders will be placed")
        logger.info("  Press Ctrl+C to stop and save trade log")
        logger.info("=" * 60)

        # Start market thread
        t = threading.Thread(target=self.market_loop, daemon=True)
        t.start()

        try:
            deadline = time.time() + duration_minutes * 60 if duration_minutes else None
            while self.running:
                if deadline and time.time() >= deadline:
                    logger.info(
                        "Duration reached (%d min). Stopping.", duration_minutes
                    )
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")

        self.running = False
        time.sleep(2)  # Let market loop finish current tick

        # Final report
        self._print_scorecard()
        self.save_trade_log()

        sc = self.sandbox.get_scorecard()
        print("\n" + "=" * 60)
        print("  SANDBOX SESSION COMPLETE")
        print("=" * 60)
        print(f"  Duration:    {(time.time() - self.start_time) / 60:.1f} minutes")
        print(f"  Trades:      {sc['trades']}")
        print(f"  Win Rate:    {sc['win_rate']:.1%}")
        print(f"  Net PnL:     ${sc['pnl']:.2f}")
        print(f"  Balance:     ${sc['balance']:.2f}")
        print(f"  Max DD:      {sc['max_drawdown_pct']:.1%}")
        print(f"  Ready:       {'YES' if sc['ready_for_live'] else 'NO'}")
        print("=" * 60)


class _SilentDashboard:
    """Minimal dashboard stub for sandbox mode (no terminal UI)."""

    def log(self, msg):
        logger.debug(msg)

    def alert(self, msg):
        logger.warning(msg)

    def update_price(self, *args, **kwargs):
        pass

    def record_signal(self, *args, **kwargs):
        pass

    def record_strategy_trade_result(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser(description="Money Printer V2 — Sandbox Mode")
    parser.add_argument(
        "--bot",
        action="append",
        dest="bots",
        help=f"Bot to run (can specify multiple). Available: {BotRegistry.list_bots()}",
    )
    parser.add_argument(
        "--balance", type=float, default=300.0, help="Starting virtual balance"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Duration in minutes (None = until Ctrl+C)",
    )
    args = parser.parse_args()

    orchestrator = SandboxOrchestrator(
        bot_names=args.bots, starting_balance=args.balance
    )

    # Graceful shutdown
    def _handle_signal(sig, frame):
        orchestrator.running = False

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    orchestrator.run(duration_minutes=args.duration)


if __name__ == "__main__":
    main()
