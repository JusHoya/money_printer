import time
import threading
import os
import sys
import argparse

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.visualization.dashboard import Dashboard
from src.data.coinbase_provider import CoinbaseProvider
from src.data.nws_provider import NWSProvider
from src.data.kalshi_provider import KalshiProvider
from src.core.risk_manager import RiskManager
from src.bots.registry import BotRegistry
from src.utils.system_utils import prevent_sleep
from src.utils.logger import logger

# Import bots to trigger registration
import src.bots  # noqa: F401


class OrchestratorEngine:
    def __init__(self, bot_names=None):
        self.dashboard = Dashboard()
        self.running = True
        self.risk_manager = RiskManager(starting_balance=100.0)

        # Wire the trade-close callback
        self.risk_manager.exchange.on_close = self._on_trade_close

        # Initialize shared providers
        self.coinbase = CoinbaseProvider("BTC-USD")

        nws_ua = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
        self.nws_stations = ["KNYC", "KLAX", "KMDW", "KMIA"]
        self.nws = NWSProvider(nws_ua, self.nws_stations)

        k_id = os.getenv("KALSHI_KEY_ID")
        k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        k_url = os.getenv("KALSHI_API_URL")
        self.kalshi = None
        if k_id and k_key:
            self.kalshi = KalshiProvider(k_id, k_key, k_url, read_only=True)

        # Instantiate bots
        if bot_names:
            self.bots = [BotRegistry.create(name) for name in bot_names]
        else:
            self.bots = BotRegistry.create_all()

        # Collect strategy names for dashboard
        all_strategies = []
        for bot in self.bots:
            all_strategies.extend(bot.strategies.keys())
        self.dashboard.active_strategies = all_strategies

        logger.info(f"[Orchestrator] Active bots: {[b.name for b in self.bots]}")

    def _on_trade_close(self, position: dict):
        """Callback from OMS when a trade is settled/closed."""
        strategy_name = position.get('strategy_name', 'Unknown')
        pnl = position.get('pnl', 0.0)
        self.dashboard.record_strategy_trade_result(strategy_name, pnl)
        logger.info(f"[Orchestrator] Strategy Result: {strategy_name} | PnL: ${pnl:+.2f}")

        # Forward Late Sniper closes for adaptive threshold
        for bot in self.bots:
            if strategy_name == "Late Sniper" and "late_sniper" in bot.strategies:
                bot.strategies["late_sniper"]._handle_position_close(position)

    def market_loop(self):
        """Background thread: position updates + bot ticks."""
        last_heartbeat = time.time()

        while self.running:
            try:
                # Heartbeat
                if time.time() - last_heartbeat > 60:
                    self.dashboard.log("[System] Heartbeat: Market Loop is Alive.")
                    last_heartbeat = time.time()

                # Update active positions (cross-bot)
                if self.risk_manager and self.kalshi:
                    active_positions = list(self.risk_manager.exchange.positions)
                    for pos in active_positions:
                        symbol = pos['symbol']
                        if "KX" in symbol:
                            try:
                                k_data = self.kalshi.fetch_latest(symbol)
                                if k_data:
                                    real_price = k_data.bid if k_data.bid > 0 else k_data.ask
                                    if real_price > 0:
                                        self.risk_manager.exchange.update_market_price(symbol, real_price)
                                    self.risk_manager.update_market_data(symbol, k_data.price)
                            except Exception:
                                pass

                # Tick all active bots
                for bot in self.bots:
                    try:
                        bot.tick(self.risk_manager, self.dashboard)
                    except Exception as e:
                        logger.error(f"[{bot.name}] Tick error: {e}")

                time.sleep(5)

            except Exception as e:
                self.dashboard.log(f"Error in loop: {str(e)}")
                time.sleep(5)

    def run(self):
        prevent_sleep()
        self.dashboard.log("System Initializing...")

        # Connect shared providers
        if self.nws.connect():
            self.dashboard.log("NWS Connected")
        else:
            self.dashboard.alert("NWS Connection Failed")

        if self.coinbase.connect():
            self.dashboard.log("Coinbase Connected")
        else:
            self.dashboard.alert("Coinbase Connection Failed")

        # Setup all bots with shared providers
        for bot in self.bots:
            bot.setup(kalshi=self.kalshi, coinbase=self.coinbase, nws=self.nws)

        # Balance sync
        if self.kalshi:
            try:
                bal = self.kalshi.get_balance()
                self.risk_manager.update_balance(bal)
                self.dashboard.log(f"Piggy Bank Initialized: ${bal:.2f}")
            except Exception as e:
                self.dashboard.alert(f"Balance Sync Failed: {e}")

        # Start market thread
        t = threading.Thread(target=self.market_loop)
        t.daemon = True
        t.start()

        self.dashboard.log(f"Trading Engine STARTED. Bots: {[b.name for b in self.bots]}")

        # UI loop
        while self.running:
            self.dashboard.render(self.risk_manager)
            time.sleep(1)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Money Printer Trading Dashboard")
    parser.add_argument("--bot", action="append", dest="bots",
                        help="Bot to run (can specify multiple). Default: all bots. "
                             f"Available: {BotRegistry.list_bots()}")

    args = parser.parse_args()

    engine = OrchestratorEngine(bot_names=args.bots)
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n[System] Shutdown Signal Received.")
