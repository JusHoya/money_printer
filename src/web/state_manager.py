"""
StateManager — produces a JSON-serializable snapshot of the full trading system
for the HTML dashboard. Reads directly from risk_manager, exchange, bots, and
the TUI Dashboard (for alerts/logs/strategy stats), bypassing the TUI render path.
"""

import csv
import glob
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


def _detect_mode(orchestrator) -> str:
    """Runtime mode shown in the dashboard header.

    'paper'   — the Kalshi provider is read-only (always true today: the
                orchestrator constructs KalshiProvider with read_only=True and
                place_order raises), regardless of which API URL it reads from.
    'sandbox' — read-only AND the demo API URL is configured.
    'live'    — ONLY if a provider with read_only=False exists. Structurally
                impossible in this codebase; kept so a regression is visible.
    """
    kalshi = getattr(orchestrator, "kalshi", None)
    if kalshi is not None and getattr(kalshi, "read_only", True) is False:
        return "live"
    api_url = os.getenv("KALSHI_API_URL", "")
    return "sandbox" if "demo" in api_url.lower() else "paper"


class StateManager:
    """
    Wraps an OrchestratorEngine reference and exposes a snapshot() method
    that serialises the full trading state for the web dashboard.
    """

    def __init__(self, orchestrator):
        self._orch = orchestrator
        self._pnl_history: deque = deque(maxlen=500)
        self._mode = _detect_mode(orchestrator)
        self._last_known_pnl = 0.0
        self._seed_pnl_history()

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

        # Drive the mascot exactly as the TUI render path does — the web
        # entrypoint never calls Dashboard.render(), so without this the
        # browser mascot is permanently IDLE. set_state owns the 2s cooldown.
        # (The portfolio CSV write that used to happen here moved to the
        # market loop: snapshot() must be side-effect free on disk.)
        self._update_mascot(dashboard, rm)

        return {
            "mode": self._mode,
            "uptime": self._fmt_uptime_seconds(orch.uptime_seconds)
            if hasattr(orch, "uptime_seconds")
            else "00:00:00",
            "portfolio": portfolio,
            "market_data": self._market_data(dashboard),
            "alerts": list(dashboard.alerts) if dashboard else [],
            "logs": list(dashboard.logs) if dashboard else [],
            "strategy_stats": self._strategy_stats(dashboard),
            "positions": self._positions(rm),
            "pnl_history": list(self._pnl_history),
            "bots": self._bots(orch),
            "mascot_state": self._mascot_state(dashboard),
            "data_log": self._data_log(dashboard),
            "cycle_history": getattr(orch, "cycle_history", []),
            "training_diagnostics": getattr(orch, "_training_diagnostics", {}),
            "training_history": getattr(orch, "_training_history", []),
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
        equity = bal + exposure
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
                    "no_bid": round(extra.get("no_bid", 0.0), 4),
                    "no_ask": round(extra.get("no_ask", 0.0), 4),
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

    @staticmethod
    def _fmt_uptime_seconds(total: float) -> str:
        total_seconds = int(total)
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _data_log(self, dashboard) -> list:
        if dashboard is None:
            return []
        log_path = getattr(dashboard, "data_log_path", None)
        if not log_path or not os.path.exists(log_path):
            return []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            return rows[-20:]
        except Exception:
            return []

    def _update_mascot(self, dashboard, rm) -> None:
        """Feed the mascot the same inputs Dashboard.render() computes:
        daily PnL, PnL change since the last snapshot, open-position count."""
        if dashboard is None or rm is None:
            return
        mascot = getattr(dashboard, "mascot", None)
        if mascot is None:
            return
        try:
            current_pnl = rm.daily_pnl
            pnl_change = current_pnl - self._last_known_pnl
            self._last_known_pnl = current_pnl
            has_open = len(rm.exchange.positions) > 0
            mascot.set_state(pnl_change, current_pnl, has_open_trades=has_open)
        except Exception:
            pass  # a mascot glitch must never break the snapshot

    def _mascot_state(self, dashboard) -> str:
        if dashboard is None:
            return "IDLE"
        mascot = getattr(dashboard, "mascot", None)
        if mascot is None:
            return "IDLE"
        return getattr(mascot, "state", "IDLE")

    def _seed_pnl_history(self) -> None:
        """Rehydrate the equity chart from prior sessions' portfolio CSVs so
        the card is not empty after a restart. The startup/cycle/shutdown
        sweeps move those CSVs into <log_dir>/_archive/<...>/, so the archive
        is globbed too, deduped by basename (the live log_dir copy wins, and
        a session duplicated across archive subdirs counts once). Rows are
        written by the market loop at a fixed cadence; newest files win,
        capped at the deque maxlen. Best-effort: any unreadable file or row
        is skipped silently."""
        dashboard = getattr(self._orch, "dashboard", None)
        log_dir = getattr(dashboard, "log_dir", None) if dashboard else None
        if not isinstance(log_dir, str):
            return
        try:
            by_name = {}
            for path in sorted(
                glob.glob(
                    os.path.join(log_dir, "_archive", "**", "portfolio_*.csv"),
                    recursive=True,
                )
            ):
                by_name[os.path.basename(path)] = path
            for path in glob.glob(os.path.join(log_dir, "portfolio_*.csv")):
                by_name[os.path.basename(path)] = path
            paths = sorted(
                by_name.values(),
                key=os.path.getmtime,
                reverse=True,
            )
        except Exception:
            return
        maxlen = self._pnl_history.maxlen or 500
        rows = []
        for path in paths:
            if len(rows) >= maxlen:
                break
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    for r in csv.DictReader(f):
                        try:
                            ts = datetime.fromisoformat(r["Timestamp"]).timestamp()
                            rows.append({"ts": ts, "equity": float(r["Equity"])})
                        except (KeyError, TypeError, ValueError):
                            continue
            except Exception:
                continue
        if not rows:
            return
        rows.sort(key=lambda x: x["ts"])
        self._pnl_history.extend(rows[-maxlen:])
