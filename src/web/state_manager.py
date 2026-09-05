"""
StateManager — produces a JSON-serializable snapshot of the full trading system
for the HTML dashboard. Reads directly from risk_manager, exchange, bots, and
the TUI Dashboard (for alerts/logs/strategy stats), bypassing the TUI render path.

Genome observability (F3 red team, 2026-09-05). ``snapshot()["genome"]`` reports
the promoted genome the weather bot is running: whether it was built at all or
REFUSED at startup, its id, its spec mode, the EFFECTIVE shadow flag, its
counters, and a bounded view of the persisted state file that decides whether it
may emit. It is assembled by DUCK TYPING off the orchestrator's bots — this
module must import nothing from ``src.factory`` or
``src.strategies.genome_strategy`` (``tests/test_factory_isolation.py`` pins the
runtime import graph of the two entry points).
"""

import csv
import glob
import os
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid circular import; OrchestratorEngine imported at runtime

# ---------------------------------------------------------------------------
# Genome block bounds. The snapshot ships over /ws at 1 Hz, so the persisted
# genome state is summarised, never mirrored: the per-city last evaluated hour
# in full (at most four cities), plus only the most recent days of the
# missed-day and traded sets, plus counts of the whole thing.
# ---------------------------------------------------------------------------
GENOME_STATE_DAYS = 2
GENOME_STATE_MAX_ROWS = 100
#: ``state_dict()`` iterates sets the market thread mutates. One retry turns the
#: usual transient "Set changed size during iteration" into a served payload.
GENOME_STATE_ATTEMPTS = 2


def _fmt_uptime(start_time: datetime) -> str:
    delta = datetime.now() - start_time
    total_seconds = int(delta.total_seconds())
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _iso_or_none(value):
    """ISO 8601 (with offset when tz-aware) for a datetime; strings pass
    through; anything else (None, legacy junk) becomes ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value or None
    return None


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


def _as_float(value):
    """A JSON-safe float, or None. NaN becomes None: Starlette's JSONResponse
    serialises with ``allow_nan=False`` and would 500 the whole endpoint."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN != NaN


def _as_str(value):
    """A non-empty str, or None. Anything else (a MagicMock in a test stub, a
    dataclass, None) is dropped rather than serialised as its repr."""
    return value if isinstance(value, str) and value else None


def _recent_day_rows(rows, date_index, days=GENOME_STATE_DAYS, cap=GENOME_STATE_MAX_ROWS):
    """The rows whose date field is one of the ``days`` most recent dates.

    ``rows`` has the genome state file's shape: a list of ``[city, target_date]``
    (missed_days) or ``[target_date, symbol]`` (traded), where ``date_index``
    says which element is the ISO date. Sorted, then hard-capped at ``cap``
    rows — the newest survive.
    """
    parsed = []
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) > date_index:
            parsed.append([str(x) for x in row])
    if not parsed:
        return []
    keep = set(sorted({row[date_index] for row in parsed})[-days:])
    kept = sorted(row for row in parsed if row[date_index] in keep)
    return kept[-cap:]


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

        genome = self._genome(orch)

        return {
            "mode": self._mode,
            "modes": self._modes(genome),
            "genome": genome,
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

    def genome_snapshot(self) -> dict:
        """Just the genome block and the mode disambiguation.

        Side-effect free, unlike ``snapshot()``: it appends no equity point and
        drives no mascot, so a monitoring agent can poll it as often as it
        likes without writing phantom rows into the equity curve.
        """
        genome = self._genome(self._orch)
        return {"mode": self._mode, "modes": self._modes(genome), "genome": genome}

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


    # ---------------- genome (F3 red team, 2026-09-05) ----------------
    #
    # Before this block the ONLY genome-bearing endpoint was /api/logs/tail,
    # hard-capped at 500 lines (an 8-16 minute keyhole), so "the genome stopped
    # emitting", "a poll failure silently closed the day" and "the strategy was
    # REFUSED at startup and you have been watching V2 for a week" were the
    # same observation from outside the container.

    def _modes(self, genome: dict) -> dict:
        """``mode`` disambiguated.

        ``snapshot["mode"]`` describes the KALSHI CREDENTIAL (the provider's
        read_only flag plus the API URL) and nothing else — on maia it reads
        'paper' while the promoted genome's execution mode is 'shadow'. Both
        are reported here under names that say what they describe; ``mode``
        keeps its historical value and meaning for the front-end pill.
        """
        if genome.get("present"):
            genome_mode = genome.get("execution_mode") or "unknown"
        elif genome.get("refused"):
            genome_mode = "refused"
        else:
            genome_mode = "none"
        return {
            "capital": self._mode,
            "genome": genome_mode,
            "label": f"capital={self._mode} · genome={genome_mode}",
        }

    def _genome(self, orch) -> dict:
        """The promoted genome's live status — never raises, never imports.

        The genome's persisted state (last evaluated hour per city, missed
        city-days, traded markets) is what decides whether it may emit at all,
        so it belongs in the snapshot next to the positions it does or does not
        produce. Every read is defensive: the market thread mutates those sets
        while this runs on the web thread.
        """
        block = {
            "present": False,
            "refused": False,
            "refused_reason": None,
            "bot": None,
            "strategy": None,
            "genome_id": None,
            "family": None,
            "spec_mode": None,
            "registry_status": None,
            "shadow": None,
            "execution_mode": None,
            "state_path": None,
            "stats": {},
            "state": None,
            "state_error": None,
            "error": None,
        }
        try:
            bot, strategy = self._find_genome(orch)
        except Exception as exc:  # noqa: BLE001 — the snapshot outranks the block
            block["error"] = f"{type(exc).__name__}: {exc}"
            return block
        if bot is None and strategy is None:
            return block

        try:
            block["bot"] = _as_str(getattr(bot, "name", None))
            reason = getattr(bot, "genome_refused_reason", None)
            if isinstance(reason, str) and reason.strip():
                block["refused"] = True
                block["refused_reason"] = reason.strip()[:500]
            shadow = getattr(bot, "genome_shadow", None)
            if isinstance(shadow, bool):
                block["shadow"] = shadow
                block["execution_mode"] = "shadow" if shadow else "paper"
        except Exception as exc:  # noqa: BLE001
            block["error"] = f"{type(exc).__name__}: {exc}"

        spec = getattr(bot, "genome_spec", None) if bot is not None else None
        if spec is None and strategy is not None:
            spec = getattr(strategy, "spec", None)
        try:
            block["genome_id"] = _as_str(getattr(spec, "genome_id", None))
            block["family"] = _as_str(getattr(spec, "family", None))
            block["spec_mode"] = _as_str(getattr(spec, "mode", None))
            block["registry_status"] = _as_str(getattr(spec, "registry_status", None))
        except Exception as exc:  # noqa: BLE001
            block["error"] = f"{type(exc).__name__}: {exc}"
        if block["execution_mode"] is None and block["spec_mode"]:
            # The bot's effective flag was unreadable; the spec is the floor
            # (the env can only tighten a spec to shadow, never loosen it).
            block["execution_mode"] = block["spec_mode"]

        if strategy is None:
            # Refused at startup: no strategy, no state, and NO execution mode
            # — the bot zeroes ``genome_shadow`` on the refusal path, which
            # would otherwise read out as the far more alarming "paper".
            block["shadow"] = None
            block["execution_mode"] = None
            return block

        block["present"] = True
        block["strategy"] = _as_str(getattr(strategy, "name", None))
        block["state_path"] = _as_str(getattr(strategy, "state_path", None))
        block["stats"] = self._genome_stats(strategy)
        state, state_error = self._genome_state(strategy)
        block["state"] = state
        block["state_error"] = state_error
        return block

    @staticmethod
    def _find_genome(orch):
        """``(bot, strategy)`` for the first bot carrying a genome strategy;
        ``(bot, None)`` for the first bot that recorded a refusal and carries
        none; ``(None, None)`` when no bot knows about a genome.

        Duck typing ONLY. ``state_dict`` + ``spec`` is the GenomeStrategy
        signature (it is the only strategy in ``src/`` with a ``state_dict``),
        and importing the class here would pull ``src.factory`` into the web
        process — ``tests/test_factory_isolation.py`` forbids exactly that.
        """
        refused = None
        for bot in getattr(orch, "bots", None) or ():
            try:
                strategies = getattr(bot, "strategies", None)
                if isinstance(strategies, Mapping):
                    for strategy in strategies.values():
                        if callable(getattr(strategy, "state_dict", None)) and (
                            getattr(strategy, "spec", None) is not None
                        ):
                            return bot, strategy
                if refused is None and isinstance(
                    getattr(bot, "genome_refused_reason", None), str
                ):
                    refused = bot
            except Exception:
                continue
        return refused, None

    @staticmethod
    def _genome_stats(strategy) -> dict:
        """The strategy's own counters (analyze_calls, hours_evaluated,
        signals, rejects, poll_failures), ints only."""
        out = {}
        try:
            stats = getattr(strategy, "stats", None)
            if isinstance(stats, Mapping):
                for key, value in list(stats.items()):
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    number = _as_float(value)
                    if number is not None:
                        out[str(key)] = int(number)
        except Exception:
            return out
        return out

    def _genome_state(self, strategy):
        """``(bounded state dict, error string)`` from ``state_dict()``.

        ``GenomeStrategy.state_dict`` iterates ``_last_hour`` / ``_missed_days``
        / ``_traded`` — sets the market thread mutates on every tick — so an
        unlucky snapshot raises ``RuntimeError: Set changed size during
        iteration``. Retried once (the collision is transient), and any failure
        degrades to an error string: the endpoint must never go down because
        the genome happened to be writing.
        """
        error = None
        for _ in range(max(1, GENOME_STATE_ATTEMPTS)):
            try:
                doc = strategy.state_dict()
            except Exception as exc:  # noqa: BLE001 — includes the mutation race
                error = f"{type(exc).__name__}: {exc}"
                continue
            try:
                return self._bound_state(doc), None
            except Exception as exc:  # noqa: BLE001
                return None, f"{type(exc).__name__}: {exc}"
        return None, error

    @staticmethod
    def _bound_state(doc) -> dict:
        """The state file, summarised for a 1 Hz broadcast: every city's last
        evaluated hour (at most four), the most recent days of the missed and
        traded sets, and counts of the full sets."""
        if not isinstance(doc, Mapping):
            raise TypeError(f"state_dict() returned {type(doc).__name__}, not a mapping")
        last_hour = {}
        raw_last_hour = doc.get("last_hour_epoch")
        if isinstance(raw_last_hour, Mapping):
            for city, hour in list(raw_last_hour.items()):
                epoch = _as_float(hour)
                if epoch is not None:
                    last_hour[str(city)] = int(epoch)
        # missed_days rows are [city, target_date]; traded rows are
        # [target_date, symbol] — the date sits in a different column in each
        # (genome_strategy.py's own pruning uses k[1] and k[0] respectively).
        missed = list(doc.get("missed_days") or [])
        traded = list(doc.get("traded") or [])
        missed_rows = _recent_day_rows(missed, date_index=1)
        traded_rows = _recent_day_rows(traded, date_index=0)
        return {
            "genome_id": _as_str(doc.get("genome_id")),
            "last_hour_epoch": last_hour,
            "cities": len(last_hour),
            "missed_days": missed_rows,
            "missed_days_total": len(missed),
            "traded": traded_rows,
            "traded_total": len(traded),
            "days_shown": GENOME_STATE_DAYS,
            "truncated": len(missed_rows) < len(missed) or len(traded_rows) < len(traded),
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
                    # PRD_STRATEGY_FACTORY FR-F0.1: the settlement-day close the
                    # position will settle at (ISO 8601 with offset), or null
                    # for a legacy row that has not been backfilled. The F0
                    # exit criterion reads this field off /api/status.
                    "expiration_time": _iso_or_none(pos.get("expiration_time")),
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
