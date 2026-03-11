"""
StateManager — produces a JSON-serializable snapshot of the full trading system
for the HTML dashboard. Reads directly from risk_manager, exchange, bots, and
the TUI Dashboard (for alerts/logs/strategy stats), bypassing the TUI render path.
"""

import os
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid circular import; OrchestratorEngine imported at runtime


def _fmt_uptime(start_time: datetime) -> str:
    delta = datetime.now() - start_time
    total_seconds = int(delta.total_seconds())
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _detect_mode() -> str:
    """Return 'sandbox' if demo API, else 'live'."""
    api_url = os.getenv("KALSHI_API_URL", "")
    return "sandbox" if "demo" in api_url.lower() else "live"


class StateManager:
    """
    Wraps an OrchestratorEngine reference and exposes a snapshot() method
    that serialises the full trading state for the web dashboard.
    """

    def __init__(self, orchestrator):
        self._orch = orchestrator
        self._pnl_history: deque = deque(maxlen=500)
        self._mode = _detect_mode()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a fully JSON-serializable dict representing the current state."""
        orch = self._orch
        rm = getattr(orch, "risk_manager", None)
        dashboard = getattr(orch, "dashboard", None)

        portfolio = self._portfolio(rm)
        equity = portfolio.get("equity", 0.0)
        self._pnl_history.append({"ts": time.time(), "equity": equity})

        return {
            "mode": self._mode,
            "uptime": _fmt_uptime(dashboard.start_time) if dashboard else "00:00:00",
            "portfolio": portfolio,
            "market_data": self._market_data(dashboard),
            "alerts": list(dashboard.alerts) if dashboard else [],
            "logs": list(dashboard.logs) if dashboard else [],
            "strategy_stats": self._strategy_stats(dashboard),
            "positions": self._positions(rm),
            "pnl_history": list(self._pnl_history),
            "bots": self._bots(orch),
            "mascot_state": self._mascot_state(dashboard),
        }

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _portfolio(self, rm) -> dict:
        if rm is None:
            return {
                "equity": 0.0,
                "cash": 0.0,
                "exposure": 0.0,
                "exposure_pct": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
            }
        bal = rm.balance
        realized_pnl = rm.daily_pnl
        unrealized_pnl = rm.unrealized_pnl
        exposure = rm.get_current_exposure()
        equity = bal + exposure + unrealized_pnl
        exposure_pct = (exposure / equity * 100) if equity > 0 else 0.0
        return {
            "equity": round(equity, 4),
            "cash": round(bal, 4),
            "exposure": round(exposure, 4),
            "exposure_pct": round(exposure_pct, 2),
            "realized_pnl": round(realized_pnl, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
        }

    def _market_data(self, dashboard) -> list:
        if dashboard is None:
            return []
        now = time.time()
        result = []
        for sym, data in dashboard.latest_prices.items():
            # Respect the 5-minute TTL used by the TUI
            if (now - data["ts"]) > 300:
                continue
            extra = data.get("extra", {}) or {}
            result.append(
                {
                    "symbol": sym,
                    "price": round(data["price"], 4),
                    "bid": round(extra.get("bid", 0.0), 4),
                    "ask": round(extra.get("ask", 0.0), 4),
                    "volume": round(extra.get("volume", 0.0), 4),
                    "extra": {k: v for k, v in extra.items()},
                }
            )
        result.sort(key=lambda x: x["symbol"])
        return result

    def _strategy_stats(self, dashboard) -> dict:
        if dashboard is None:
            return {}
        out = {}
        for name, stats in dashboard.strategy_stats.items():
            out[name] = {
                "signals": stats.get("signals", 0),
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0),
                "pnl": round(stats.get("pnl", 0.0), 4),
                "active": stats.get("active", 0),
            }
        return out

    def _positions(self, rm) -> list:
        if rm is None:
            return []
        now = datetime.now()
        result = []
        for pos in rm.exchange.positions:
            open_time = pos.get("open_time", now)
            age_sec = int((now - open_time).total_seconds())
            result.append(
                {
                    "id": pos.get("id"),
                    "symbol": pos.get("symbol", ""),
                    "side": pos.get("side", ""),
                    "contract_side": pos.get("contract_side", "YES"),
                    "entry": round(pos.get("entry_price", 0.0), 4),
                    "current": round(pos.get("current_price", 0.0), 4),
                    "quantity": pos.get("quantity", 0),
                    "pnl": round(pos.get("pnl", 0.0), 4),
                    "strategy": pos.get("strategy_name", "Unknown"),
                    "age": age_sec,
                }
            )
        return result

    def _bots(self, orch) -> list:
        bots = getattr(orch, "bots", [])
        active_set = getattr(orch, "active_bots", None)
        result = []
        for bot in bots:
            if active_set is not None:
                active = bot.name in active_set
            else:
                active = True
            result.append({"name": bot.name, "active": active})
        return result

    def _mascot_state(self, dashboard) -> str:
        if dashboard is None:
            return "IDLE"
        mascot = getattr(dashboard, "mascot", None)
        if mascot is None:
            return "IDLE"
        return getattr(mascot, "state", "IDLE")
