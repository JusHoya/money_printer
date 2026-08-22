"""Emergency stop and circuit breakers.

Protects against cascading losses by halting trading when
configurable thresholds are breached.

Sprint 4, Task 4.7 of Money Printer V2.

Breaker types
-------------
- **Daily loss**: Halt all trading if daily loss > X% of bankroll.
- **Strategy streak**: Pause a strategy after N consecutive losses.
- **Brier score**: Pause a market type if model Brier > threshold.
- **Manual kill**: Global halt toggled by user/dashboard.
"""

import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Dict, Set

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Central circuit breaker for the trading system.

    Parameters
    ----------
    max_daily_loss_pct : float
        Daily loss as fraction of bankroll that triggers full halt (default 0.05).
    max_consecutive_losses : int
        Per-strategy consecutive loss count before pause (default 3).
    max_brier_score : float
        Brier score threshold above which a market type is paused (default 0.30).
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 0.05,
        max_consecutive_losses: int = 3,
        max_brier_score: float = 0.30,
    ):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.max_brier_score = max_brier_score

        # State
        self._kill_switch = False
        self._daily_halted = False
        self._paused_strategies: Set[str] = set()
        self._paused_market_types: Set[str] = set()

        # Tracking
        self._consecutive_losses: Dict[str, int] = defaultdict(int)
        self._trigger_log: list = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def activate_kill_switch(self, reason: str = "manual") -> None:
        """Halt ALL trading immediately."""
        with self._lock:
            self._kill_switch = True
            self._log_trigger("KILL_SWITCH", reason)
            logger.warning("[CircuitBreaker] KILL SWITCH ACTIVATED: %s", reason)

    def deactivate_kill_switch(self) -> None:
        """Resume trading after manual review."""
        with self._lock:
            self._kill_switch = False
            self._daily_halted = False
            self._paused_strategies.clear()
            self._paused_market_types.clear()
            self._consecutive_losses.clear()
            logger.info("[CircuitBreaker] Kill switch deactivated — all breakers reset")

    @property
    def is_killed(self) -> bool:
        return self._kill_switch

    # ------------------------------------------------------------------
    # Daily loss breaker
    # ------------------------------------------------------------------

    def check_daily_loss(self, daily_pnl: float, bankroll: float) -> bool:
        """Return True if trading should continue, False if halted.

        Automatically triggers the kill switch if the daily loss
        exceeds the threshold.
        """
        if bankroll <= 0:
            return False
        loss_pct = abs(daily_pnl) / bankroll if daily_pnl < 0 else 0.0
        if loss_pct >= self.max_daily_loss_pct:
            with self._lock:
                if not self._daily_halted:
                    self._daily_halted = True
                    self._log_trigger(
                        "DAILY_LOSS",
                        f"Loss {loss_pct:.1%} >= {self.max_daily_loss_pct:.1%} of ${bankroll:.2f}",
                    )
                    logger.warning(
                        "[CircuitBreaker] DAILY LOSS HALT: $%.2f (%.1f%% of $%.2f)",
                        daily_pnl,
                        loss_pct * 100,
                        bankroll,
                    )
            return False
        return True

    # ------------------------------------------------------------------
    # Strategy consecutive-loss breaker
    # ------------------------------------------------------------------

    def record_trade_result(self, strategy_name: str, won: bool) -> None:
        """Record win/loss for consecutive-loss tracking."""
        with self._lock:
            if won:
                self._consecutive_losses[strategy_name] = 0
                self._paused_strategies.discard(strategy_name)
            else:
                self._consecutive_losses[strategy_name] += 1
                streak = self._consecutive_losses[strategy_name]
                if streak >= self.max_consecutive_losses:
                    self._paused_strategies.add(strategy_name)
                    self._log_trigger(
                        "STRATEGY_STREAK",
                        f"{strategy_name}: {streak} consecutive losses",
                    )
                    logger.warning(
                        "[CircuitBreaker] STRATEGY PAUSED: %s (%d losses)",
                        strategy_name,
                        streak,
                    )

    def is_strategy_paused(self, strategy_name: str) -> bool:
        return strategy_name in self._paused_strategies

    def unpause_strategy(self, strategy_name: str) -> None:
        with self._lock:
            self._paused_strategies.discard(strategy_name)
            self._consecutive_losses[strategy_name] = 0

    # ------------------------------------------------------------------
    # Brier score breaker
    # ------------------------------------------------------------------

    def check_brier_score(self, market_type: str, brier_score: float) -> bool:
        """Return True if market type is OK, False if paused.

        Call this with rolling Brier scores for each market type
        (e.g., ``"btc"``, ``"weather"``).
        """
        if brier_score > self.max_brier_score:
            with self._lock:
                if market_type not in self._paused_market_types:
                    self._paused_market_types.add(market_type)
                    self._log_trigger(
                        "BRIER_DEGRADED",
                        f"{market_type}: Brier {brier_score:.3f} > {self.max_brier_score}",
                    )
                    logger.warning(
                        "[CircuitBreaker] MARKET TYPE PAUSED: %s (Brier %.3f)",
                        market_type,
                        brier_score,
                    )
            return False

        # Auto-unpause if score recovers
        with self._lock:
            self._paused_market_types.discard(market_type)
        return True

    def is_market_type_paused(self, market_type: str) -> bool:
        return market_type in self._paused_market_types

    # ------------------------------------------------------------------
    # Aggregate check
    # ------------------------------------------------------------------

    def can_trade(
        self,
        strategy_name: str = "",
        market_type: str = "",
        daily_pnl: float = 0.0,
        bankroll: float = 1.0,
    ) -> bool:
        """Single call to check ALL breakers.

        Returns ``True`` only if no breaker is tripped.
        """
        if self._kill_switch:
            return False
        if self._daily_halted:
            return False
        if not self.check_daily_loss(daily_pnl, bankroll):
            return False
        if strategy_name and self.is_strategy_paused(strategy_name):
            return False
        if market_type and self.is_market_type_paused(market_type):
            return False
        return True

    # ------------------------------------------------------------------
    # Logging / status
    # ------------------------------------------------------------------

    def _log_trigger(self, breaker_type: str, detail: str) -> None:
        self._trigger_log.append(
            {
                "timestamp": datetime.now().isoformat(),
                "type": breaker_type,
                "detail": detail,
            }
        )

    def get_status(self) -> dict:
        """Return a snapshot of all breaker states."""
        return {
            "kill_switch": self._kill_switch,
            "daily_halted": self._daily_halted,
            "paused_strategies": list(self._paused_strategies),
            "paused_market_types": list(self._paused_market_types),
            "consecutive_losses": dict(self._consecutive_losses),
            "trigger_log": list(self._trigger_log[-20:]),
        }
