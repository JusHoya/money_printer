"""Real-time sandbox (paper trading) runner.

Sprint 5, Task 5.5.

Runs the full bot pipeline against live WebSocket feeds but places
no real orders.  Tracks virtual portfolio and validates against the
85%+ win rate target over 100+ trades.
"""

import logging
from datetime import datetime
from typing import List

from src.backtest import metrics as bmetrics
from src.core.circuit_breaker import CircuitBreaker
from src.core.risk_manager import RiskManager
from src.ml.brier import BrierScoreTracker

logger = logging.getLogger(__name__)


class SandboxRunner:
    """Live paper trading sandbox.

    Wraps the existing bot infrastructure with settlement tracking
    and validation scoring.  Uses ``SimulatedExchange`` (via RiskManager)
    as the execution engine — no real orders are placed.

    Parameters
    ----------
    starting_balance : float
        Virtual bankroll.
    min_trades_for_validation : int
        Number of settled trades required before the scorecard
        can declare ``ready_for_live``.
    """

    def __init__(
        self,
        starting_balance: float = 300.0,
        min_trades_for_validation: int = 100,
    ):
        self.starting_balance = starting_balance
        self.min_trades = min_trades_for_validation

        self.risk_manager = RiskManager(starting_balance=starting_balance)
        self.circuit_breaker = CircuitBreaker()
        self.risk_manager.circuit_breaker = self.circuit_breaker
        self.brier_tracker = BrierScoreTracker(window_size=100)

        self.trade_log: List[dict] = []
        self.equity_curve: List[float] = [starting_balance]
        self._settled_count = 0

        # Wire settlement callback
        self._original_on_close = self.risk_manager.exchange.on_close
        self.risk_manager.exchange.on_close = self._on_settlement

    def _on_settlement(self, position: dict):
        """Called when a position is closed/settled."""
        # Delegate to original callback first (risk manager PnL tracking)
        if self._original_on_close:
            self._original_on_close(position)

        pnl = position.get("pnl", 0)
        self._settled_count += 1
        self.trade_log.append(
            {
                "symbol": position.get("symbol"),
                "strategy": position.get("strategy_name"),
                "pnl": pnl,
                "entry_price": position.get("entry_price"),
                "exit_price": position.get("exit_price"),
                "contract_side": position.get("contract_side", "YES"),
                "reason": position.get("reason"),
                "timestamp": datetime.now().isoformat(),
            }
        )
        self.equity_curve.append(self.risk_manager.balance)

        won = pnl > 0
        strategy = position.get("strategy_name", "Unknown")
        self.circuit_breaker.record_trade_result(strategy, won)

        logger.info(
            "[Sandbox] Trade #%d settled: %s %s PnL=$%.2f | Balance=$%.2f",
            self._settled_count,
            strategy,
            "WIN" if won else "LOSS",
            pnl,
            self.risk_manager.balance,
        )

    def get_scorecard(self) -> dict:
        """Return the sandbox validation scorecard."""
        win_rate = bmetrics.compute_win_rate(self.trade_log)
        total_pnl = bmetrics.compute_total_pnl(self.trade_log)
        dd_pct, _, _ = bmetrics.compute_max_drawdown(self.equity_curve)

        enough_trades = self._settled_count >= self.min_trades
        passes_win_rate = win_rate >= 0.85
        passes_pnl = total_pnl > 0
        passes_dd = dd_pct < 0.15

        return {
            "trades": self._settled_count,
            "wins": sum(1 for t in self.trade_log if t.get("pnl", 0) > 0),
            "win_rate": win_rate,
            "pnl": total_pnl,
            "max_drawdown_pct": dd_pct,
            "balance": self.risk_manager.balance,
            "enough_trades": enough_trades,
            "passes_win_rate": passes_win_rate,
            "passes_pnl": passes_pnl,
            "passes_drawdown": passes_dd,
            "ready_for_live": enough_trades
            and passes_win_rate
            and passes_pnl
            and passes_dd,
        }
