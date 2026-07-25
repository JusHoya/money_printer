import fnmatch
import shutil
import time
import threading
import os
import sys
import argparse

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.visualization.dashboard import Dashboard
from src.data.coinbase_provider import CoinbaseProvider
from src.data.nws_provider import NWSProvider
from src.data.kalshi_provider import KalshiProvider, is_test_symbol
from src.core.risk_manager import RiskManager
from src.core.bracket_payoff import is_weather_symbol
from src.bots.registry import BotRegistry
from src.utils.system_utils import prevent_sleep
from src.utils.logger import logger, get_active_log_path, configure_root_logging
from src.ml.trade_journal import TradeJournal, TradeOutcome
from src.strategies.counter_trade import CounterTradeAnalyzer
from src.ml.settlement_resolver import SettlementResolver
from src.notifications.discord import send_discord_notification

# Import bots to trigger registration
import src.bots  # noqa: F401


class OrchestratorEngine:
    _TRAINING_STATE_PATH = os.path.join("data", "training_state.json")

    # Phase 0 teardown (2026-07-24, PRD FR-0.2): ALL runtime retrains removed —
    # the periodic 2h daemon retrain, the cycle-boundary in-process retrain, the
    # startup retrain, and the on-trade-close online updater. Training modules
    # (src/ml/, scripts/train_*.py) remain on disk and are invocable OFFLINE
    # only; nothing in the runtime path may import-and-run training.

    # Phase 0 (2026-07-24): the ".orchestrator_state" runtime marker was
    # removed — the redesigned watchdog no longer reads it (see comments in
    # scripts/host_watchdog.sh, scripts/watchdog_cron.sh, scripts/vm_watchdog.py).

    # 2026-06-10 fix (b) — records the sim balance this process launched with so
    # a watchdog auto-restart reuses it instead of hardcoding $3000 (which would
    # silently override a future Phase-2 $500 run).
    _SIM_BALANCE_MARKER_PATH = os.path.join("logs", ".sim_balance")

    def __init__(self, bot_names=None):
        self.dashboard = Dashboard()
        self.running = True
        self.risk_manager = RiskManager(starting_balance=100.0, persist_state=True)

        # Wire the trade-close callback
        self.risk_manager.exchange.on_close = self._on_trade_close
        # Operator-visible alerts from the exchange (e.g. a weather position
        # whose bracket semantics cannot be established, or a refused FR-1.5
        # lifecycle close). Re-wired after every cycle-boundary Dashboard swap.
        self.risk_manager.exchange.on_alert = self.dashboard.alert

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

        # Track which bots are currently active (all bots active by default)
        self.active_bots = {bot.name for bot in self.bots}

        # Uptime tracking — pauses when all bots are stopped
        self._uptime_accumulated = 0.0
        self._uptime_resumed_at = time.time()

        # Collect strategy names for dashboard
        all_strategies = []
        for bot in self.bots:
            all_strategies.extend(bot.strategies.keys())
        self.dashboard.active_strategies = all_strategies

        # Auto-cycle config (set by caller)
        self.auto_cycle = False
        self.sim_balance = 0.0
        self._cycle_count = 0
        self._cycle_start_time = time.time()
        self._profitable_since = None  # timestamp when PnL last went positive
        self.cycle_history = []  # list of cycle result dicts
        self._training_diagnostics = {}  # legacy training metrics (offline only)
        self._training_history = []  # accumulated training history (max 20)

        # Load persisted training state from prior runs
        self._load_training_state()

        # Trade journal — records every closed trade for offline analysis
        self.trade_journal = TradeJournal()

        # Counter-trade analyzer — LOG-ONLY mode until validated
        self.counter_analyzer = CounterTradeAnalyzer(live=False)

        # Settlement resolver — background label resolution
        self.settlement_resolver = SettlementResolver(
            kalshi_provider=self.kalshi,
        )

        logger.info(
            f"[Orchestrator] Active bots: {[b.name for b in self.bots]} | "
            f"Journal: {self.trade_journal.get_sample_count()} outcomes"
        )

    @property
    def uptime_seconds(self) -> float:
        """Total seconds bots have been active (pauses when all bots stopped)."""
        if self.active_bots:
            return self._uptime_accumulated + (time.time() - self._uptime_resumed_at)
        return self._uptime_accumulated

    def start_bot(self, name: str):
        """Activate a bot by name so it participates in the market loop."""
        bot_names = [b.name for b in self.bots]
        if name not in bot_names:
            raise ValueError(f"Bot '{name}' not found. Available: {bot_names}")
        was_empty = len(self.active_bots) == 0
        self.active_bots.add(name)
        if was_empty:
            self._uptime_resumed_at = time.time()
        logger.info(f"[Orchestrator] Bot '{name}' started.")

    def stop_bot(self, name: str):
        """Deactivate a bot by name so it is skipped in the market loop."""
        bot_names = [b.name for b in self.bots]
        if name not in bot_names:
            raise ValueError(f"Bot '{name}' not found. Available: {bot_names}")
        was_active = name in self.active_bots
        self.active_bots.discard(name)
        if was_active and len(self.active_bots) == 0:
            self._uptime_accumulated += time.time() - self._uptime_resumed_at
        logger.info(f"[Orchestrator] Bot '{name}' stopped.")

    def _on_trade_close(self, position: dict):
        """Callback from OMS when a trade is settled/closed."""
        # Delegate to RiskManager FIRST: the orchestrator overwrites
        # exchange.on_close (at __init__ and on cycle reset), which would
        # otherwise orphan RiskManager's win-rate recording, loss/strategy
        # cooldowns, consecutive-loss tracking, and strategy_pnl/peak
        # accumulation. The two callbacks touch disjoint state and
        # RiskManager._on_trade_close reads cumulative exchange state
        # idempotently, so chaining is safe and non-duplicative. Wrapped in
        # try/except so a RiskManager error can never break journaling below.
        try:
            self.risk_manager._on_trade_close(position)
        except Exception as exc:
            logger.warning("[Orchestrator] RiskManager close handler failed: %s", exc)

        strategy_name = position.get("strategy_name", "Unknown")
        pnl = position.get("pnl", 0.0)
        self.dashboard.record_strategy_trade_result(strategy_name, pnl)
        logger.info(
            f"[Orchestrator] Strategy Result: {strategy_name} | PnL: ${pnl:+.2f}"
        )

        # Record to trade journal for offline analysis
        try:
            outcome = TradeOutcome.from_position(position)
            self.trade_journal.record(outcome)
        except Exception as exc:
            logger.warning("[Orchestrator] Journal record failed: %s", exc)

        # NOTE (FR-0.2): the online-model-update trigger that used to run here
        # (a full in-runtime retrain on the closing thread) was removed in the
        # Phase 0 teardown. Training is offline-only now.

        # Forward Late Sniper closes for adaptive threshold
        for bot in self.bots:
            if strategy_name == "Late Sniper" and "late_sniper" in bot.strategies:
                bot.strategies["late_sniper"]._handle_position_close(position)

    # ------------------------------------------------------------------
    # Runtime state markers (2026-06-10 fix b) — best-effort, never raise
    # ------------------------------------------------------------------

    def _write_sim_balance_marker(self) -> None:
        """Persist the sim balance this process launched with so a watchdog
        auto-restart reuses it (instead of hardcoding $3000). Best-effort."""
        try:
            bal = self.sim_balance if self.sim_balance > 0 else 3000.0
            os.makedirs("logs", exist_ok=True)
            with open(self._SIM_BALANCE_MARKER_PATH, "w", encoding="utf-8") as f:
                f.write(f"{bal:.2f}\n")
        except Exception:
            pass

    # FR-0.3: files in logs/ that NO cleanup pass may ever remove — watchdog
    # state/timestamp files, watchdog logs (the Phase-0 watchdog redesign
    # depends on these surviving the startup and cycle sweeps), and the
    # restart-balance marker. fnmatch patterns so timestamped variants match.
    _PRESERVED_LOG_PATTERNS = (
        "host_watchdog*.ts",
        "watchdog_cron_endpoint_fail.ts",
        "watchdog_last_alert.ts",
        ".sim_balance",
        "host_watchdog.log",
        "watchdog_*.log",
        "watchdog.log",  # legacy name, kept from the 2026-06-10 fix (c)
    )

    @classmethod
    def _is_preserved_log(cls, filename: str) -> bool:
        return any(
            fnmatch.fnmatch(filename, pattern)
            for pattern in cls._PRESERVED_LOG_PATTERNS
        )

    def _protected_log_paths(self) -> set:
        """Absolute paths the sweeps must never remove (FR-0.3): the current
        dashboard session files plus this process's own active
        money_printer_*.log (the 2026-07-24 review found the startup sweep
        deleting the log the process was actively writing to)."""
        protected = set()
        dashboard = getattr(self, "dashboard", None)
        if dashboard is not None:
            for attr in ("data_log_path", "session_log_path", "portfolio_log_path"):
                path = getattr(dashboard, attr, None)
                if path:
                    protected.add(os.path.abspath(path))
        active_log = get_active_log_path()
        if active_log:
            protected.add(os.path.abspath(active_log))
        return protected

    # ------------------------------------------------------------------
    # Training state persistence
    # ------------------------------------------------------------------

    def _load_training_state(self):
        """Load persisted training history from prior process runs."""
        import json as _json

        try:
            if os.path.exists(self._TRAINING_STATE_PATH):
                with open(self._TRAINING_STATE_PATH, "r") as f:
                    state = _json.load(f)
                self._cycle_count = state.get("cycle_count", 0)
                self.cycle_history = state.get("cycle_history", [])
                self._training_history = state.get("training_history", [])
                self._training_diagnostics = state.get("training_diagnostics", {})
                logger.info(
                    "[State] Loaded training state: %d cycles, %d history entries, "
                    "%d last samples",
                    self._cycle_count,
                    len(self._training_history),
                    self._training_diagnostics.get("training_samples", 0),
                )
        except Exception as exc:
            logger.warning("[State] Could not load training state: %s", exc)

    def _save_training_state(self):
        """Persist training history and cycle history to disk."""
        import json as _json

        state = {
            "cycle_count": self._cycle_count,
            "cycle_history": self.cycle_history[-20:],
            "training_history": self._training_history[-20:],
            "training_diagnostics": self._training_diagnostics,
        }
        try:
            os.makedirs(os.path.dirname(self._TRAINING_STATE_PATH), exist_ok=True)
            with open(self._TRAINING_STATE_PATH, "w") as f:
                _json.dump(state, f, indent=2)
        except Exception as exc:
            logger.warning("[State] Could not save training state: %s", exc)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Graceful shutdown: archive current session data and save state.

        Safe to call multiple times (idempotent).
        """
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self.running = False

        logger.info("[Shutdown] Graceful shutdown initiated...")

        # 1. Archive current CSV/log files (COPY to archive — startup will clean logs/)
        try:
            files_to_archive = []
            if os.path.isdir("logs"):
                for f in os.listdir("logs"):
                    fpath = os.path.join("logs", f)
                    if os.path.isfile(fpath) and (
                        f.endswith(".csv") or f.endswith(".log")
                    ):
                        files_to_archive.append((f, fpath))

            if files_to_archive:
                ts = time.strftime("%Y%m%d_%H%M%S")
                archive_dir = os.path.join("logs", "_archive", f"shutdown_{ts}")
                os.makedirs(archive_dir, exist_ok=True)

                archived = 0
                for f, fpath in files_to_archive:
                    try:
                        shutil.copy2(fpath, os.path.join(archive_dir, f))
                        archived += 1
                    except Exception:
                        pass

                logger.info("[Shutdown] Archived %d files to %s", archived, archive_dir)
        except Exception as exc:
            logger.error("[Shutdown] Archive failed: %s", exc)

        # 2. Save training state
        try:
            self._save_training_state()
            logger.info("[Shutdown] Training state saved.")
        except Exception as exc:
            logger.error("[Shutdown] Could not save training state: %s", exc)

        # 2b. Save win rates
        try:
            self.risk_manager._save_win_rates()
            logger.info("[Shutdown] Win rates saved.")
        except Exception as exc:
            logger.error("[Shutdown] Could not save win rates: %s", exc)

        # 3. Log final data inventory
        try:
            self._log_data_inventory("SHUTDOWN")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Data inventory (debugging aid)
    # ------------------------------------------------------------------

    def _log_data_inventory(self, context: str = "INVENTORY"):
        """Log a summary of all persisted data for debugging data-loss issues."""
        from pathlib import Path

        archive_dir = Path("logs/_archive")
        journal_path = Path("data/trade_journal.jsonl")
        state_path = Path(self._TRAINING_STATE_PATH)

        # Count archive subdirectories and CSV files
        archive_dirs = 0
        archive_csvs = 0
        total_csv_bytes = 0
        if archive_dir.exists():
            for d in archive_dir.iterdir():
                if d.is_dir():
                    archive_dirs += 1
                    for f in d.glob("data_*.csv"):
                        archive_csvs += 1
                        try:
                            total_csv_bytes += f.stat().st_size
                        except OSError:
                            pass

        # Count live CSV files in logs/
        live_csvs = 0
        live_csv_bytes = 0
        logs_dir = Path("logs")
        if logs_dir.exists():
            for f in logs_dir.glob("data_*.csv"):
                live_csvs += 1
                try:
                    live_csv_bytes += f.stat().st_size
                except OSError:
                    pass

        # Trade journal
        journal_lines = 0
        journal_bytes = 0
        if journal_path.exists():
            try:
                journal_bytes = journal_path.stat().st_size
                with open(journal_path) as jf:
                    journal_lines = sum(1 for line in jf if line.strip())
            except OSError:
                pass

        # Training state
        state_exists = state_path.exists()
        prev_samples = self._training_diagnostics.get("training_samples", 0)

        logger.info(
            "[%s] Data inventory: "
            "%d archive dirs | %d archived CSVs (%.1f MB) | "
            "%d live CSVs (%.1f MB) | "
            "%d journal entries (%.1f KB) | "
            "training_state=%s | last_samples=%d",
            context,
            archive_dirs,
            archive_csvs,
            total_csv_bytes / 1024 / 1024,
            live_csvs,
            live_csv_bytes / 1024 / 1024,
            journal_lines,
            journal_bytes / 1024,
            "exists" if state_exists else "MISSING",
            prev_samples,
        )

    # ------------------------------------------------------------------
    # Startup archive
    # ------------------------------------------------------------------

    def _startup_archive(self):
        """Archive stale CSVs from previous sessions.

        FR-0.2: the startup retrain that used to follow the archive pass was
        removed in the Phase 0 teardown — training is offline-only now.
        """
        # Log data inventory BEFORE archiving to see what survived from last run
        self._log_data_inventory("STARTUP-BEFORE")

        # Archive any leftover CSVs from logs/ that aren't the current session.
        # FR-0.3: the protected set includes this process's own active
        # money_printer_*.log — the old whitelist held only the dashboard's
        # three files, so the sweep deleted the log it was writing to.
        active_files = self._protected_log_paths()

        stale_files = []
        for f in os.listdir("logs"):
            fpath = os.path.join("logs", f)
            if not os.path.isfile(fpath):
                continue
            if not (f.endswith(".csv") or f.endswith(".log")):
                continue
            if os.path.abspath(fpath) in active_files:
                continue
            if self._is_preserved_log(f):  # FR-0.3: keep watchdog state/logs
                continue
            stale_files.append((f, fpath))

        if stale_files:
            ts = time.strftime("%Y%m%d_%H%M%S")
            archive_dir = os.path.join("logs", "_archive", f"startup_{ts}")
            os.makedirs(archive_dir, exist_ok=True)

            archived = 0
            for f, fpath in stale_files:
                try:
                    shutil.copy2(fpath, os.path.join(archive_dir, f))
                    os.remove(fpath)
                    archived += 1
                except Exception as exc:
                    logger.warning("[Startup] Could not archive %s: %s", f, exc)

            logger.info(
                "[Startup] Archived %d stale files from previous session(s)",
                archived,
            )

        # Log data inventory AFTER archiving for comparison
        self._log_data_inventory("STARTUP-AFTER")

    def _cycle_archive_and_clean(self, archive_dir):
        """Copy session CSV/log files into ``archive_dir``, then remove the
        old ones from logs/ in a single pass.

        Called AFTER the new Dashboard exists (FR-0.5) so the removal pass
        can protect the new session files. Never removes (FR-0.3):
          - the current dashboard session files,
          - this process's own active money_printer_*.log,
          - watchdog state/log files (_PRESERVED_LOG_PATTERNS).
        """
        # COPY only NEW files — use a manifest to prevent re-archiving.
        manifest_path = os.path.join("logs", "_archive", "_archived_files.json")
        archived_set = set()
        try:
            if os.path.exists(manifest_path):
                import json as _mj

                with open(manifest_path, encoding="utf-8") as _mf:
                    archived_set = set(_mj.load(_mf))
        except Exception:
            pass

        protected = self._protected_log_paths()

        newly_archived = []
        for f in os.listdir("logs"):
            fpath = os.path.join("logs", f)
            if os.path.isfile(fpath) and (f.endswith(".csv") or f.endswith(".log")):
                try:
                    shutil.copy2(fpath, os.path.join(archive_dir, f))
                    newly_archived.append(f)
                except Exception as exc:
                    logger.warning("[Cycle] Could not copy %s: %s", f, exc)

        # Update manifest with all known archived filenames
        archived_set.update(newly_archived)
        try:
            import json as _mj

            with open(manifest_path, "w", encoding="utf-8") as _mf:
                _mj.dump(sorted(archived_set), _mf, indent=2)
        except Exception:
            pass

        # Remove archived files from logs/ — except protected + preserved.
        for f in os.listdir("logs"):
            fpath = os.path.join("logs", f)
            if not os.path.isfile(fpath):
                continue
            if not (f.endswith(".csv") or f.endswith(".log")):
                continue
            if os.path.abspath(fpath) in protected:
                continue  # FR-0.3: active session files + own process log
            if self._is_preserved_log(f):
                continue  # FR-0.3: watchdog state/logs
            try:
                os.remove(fpath)
            except Exception as exc:
                logger.warning("[Cycle] Could not remove old file %s: %s", f, exc)

    # ------------------------------------------------------------------
    # FR-0.4: per-bot status for cycle summaries
    # ------------------------------------------------------------------

    def _bot_status(self, bot) -> str:
        """TRADING / FEED-ONLY / DISABLED for one bot.

        DISABLED: the bot is deactivated in the orchestrator (not ticking).
        FEED-ONLY: ticking (feeds/prices run) but trading is switched off.
        Trading state is derived generically: a ``trading_enabled`` attribute
        on the bot wins; otherwise any boolean ``*TRADING_ENABLED`` constant
        in the bot's module is read (weather_bot.py gates its waterfall on
        module-level WEATHER_TRADING_ENABLED and exposes no attribute).
        """
        if bot.name not in self.active_bots:
            return "DISABLED"

        enabled = getattr(bot, "trading_enabled", None)
        if enabled is None:
            module = sys.modules.get(type(bot).__module__)
            if module is not None:
                flags = [
                    value
                    for name, value in vars(module).items()
                    if name.endswith("TRADING_ENABLED") and isinstance(value, bool)
                ]
                if flags:
                    enabled = all(flags)
        if enabled is None:
            enabled = True
        return "TRADING" if enabled else "FEED-ONLY"

    def _bot_status_summary(self) -> str:
        """One line listing every registered bot with its status,
        e.g. ``Weather=FEED-ONLY`` (FR-0.4)."""
        if not self.bots:
            return "(no bots registered)"
        return ", ".join(f"{bot.name}={self._bot_status(bot)}" for bot in self.bots)

    def _rollover_positions(self):
        """Liquidate the book at a cycle boundary, holding weather positions.

        PRD FR-1.5: weather positions are EXEMPT from cycle-reset liquidation.
        They are binary daily-high contracts that settle against the NWS
        Climatological Report at expiry (FR-1.2), so a cycle boundary — an
        internal drawdown-management event with no market meaning — must not
        crystallize them. Everything else is liquidated exactly as before.

        Split out of ``_run_drawdown_cycle`` so the boundary behaviour can be
        exercised directly (Phase 1 exit criterion 5) without standing up log
        archiving, Discord and a new Dashboard.

        PER-CYCLE ACCOUNTING OF A SURVIVOR (measured 2026-07-25, documented not
        fixed — see the return value and the cycle record's
        ``carried_weather_*`` keys)
        ---------------------------------------------------------------------
        ``_run_drawdown_cycle`` calls ``risk_manager.update_balance(new_bal)``
        BEFORE this method, while the survivor is still open. That resets
        ``starting_balance_day`` to the configured sim balance and zeroes
        ``daily_pnl``/``exchange.realized_pnl``, so a survivor's economics
        SPLIT across two cycle records:

          * the opening cycle is charged the ENTRY FEE only (the entry cost
            itself is never booked to PnL — it is held as ``exposure``, a live
            view of currently-open collateral);
          * the settling cycle is credited the FULL settlement PnL
            ``(exit - entry) * qty - exit_fee``;
          * the survivor's collateral is re-deducted from ``balance`` as
            exposure against each new cycle's fresh ``starting_balance_day``,
            which is correct — that capital really is still deployed — but it
            means cycle N+1 starts with less headroom than its nominal base.

        Measured on a $0.40x10 weather survivor settling YES: cycle N reports
        -$0.05, cycle N+1 reports +$6.00, and the two sum to the cumulative
        ledger's +$5.95 exactly. So nothing is double-charged or lost; the
        artifact is ATTRIBUTION, not arithmetic.

        Not fixed, deliberately: (1) the sum over cycles is exact, so no gate
        that reads the cumulative ledger or the settled-trade journal is
        affected — and the Phase 3 gate is specified to be settlement-true over
        >=50 settled trades, which is attribution-independent; (2) a fix would
        have to move ``starting_balance_day``/``update_balance`` semantics in
        ``risk_manager.py``, changing the daily-drawdown baseline for every
        strategy, to buy nothing the cumulative ledger does not already give.
        The consequence an operator MUST know is that a survivor's whole
        settlement outcome lands as one realized hit in the cycle it settles
        (it cannot move the daily-drawdown breaker while unrealized), so the
        carried exposure is logged here and stamped on the cycle record.
        """
        exchange = self.risk_manager.exchange
        for p in list(exchange.positions):
            if is_weather_symbol(p.get("symbol", "")):
                continue
            exchange._close_position(p, p["entry_price"], reason="CYCLE_RESET")
        # A non-weather position whose close failed is still dropped from the
        # open book, exactly as the legacy positions.clear() did; weather
        # positions survive with their id, quantity and entry price intact.
        survivors = [
            p for p in exchange.positions if is_weather_symbol(p.get("symbol", ""))
        ]
        dropped = len(exchange.positions) - len(survivors)
        if dropped:
            logger.warning(
                "[Cycle] Dropped %d non-weather position(s) that failed to close",
                dropped,
            )
        exchange.positions[:] = survivors
        # Ids must not be reissued while their owners are still open, so the
        # counter restarts above the highest surviving id rather than at 1.
        exchange._bump_next_id()
        self.risk_manager.active_positions = len(survivors)
        if survivors:
            # Same formula as RiskManager.get_current_exposure (short YES locks
            # (1-price)*qty), computed locally so the boundary code needs only
            # the exchange.
            carried = sum(
                (1.0 - p["entry_price"]) * p["quantity"]
                if p.get("side") == "sell" and p.get("contract_side", "YES") == "YES"
                else p["entry_price"] * p["quantity"]
                for p in survivors
            )
            logger.info(
                "[Cycle] %d weather position(s) held across the boundary "
                "(FR-1.5), carrying $%.2f of collateral and their unsettled "
                "PnL into the next cycle: %s",
                len(survivors),
                carried,
                ", ".join(f"id={p.get('id')} {p.get('symbol')}" for p in survivors),
            )
        # Persist immediately so a restart right after rollover restores the
        # survivors rather than the pre-rollover book.
        exchange._save_state()
        return survivors

    def _run_drawdown_cycle(self):
        """Archive logs and reset state for next cycle (no retrain — FR-0.2)."""
        from datetime import datetime as _dt

        # Immediately clear the flag to prevent re-triggering
        self.risk_manager.drawdown_kill_triggered = False

        # Record cycle metrics BEFORE reset
        cycle_duration_s = time.time() - self._cycle_start_time
        cycle_duration_m = cycle_duration_s / 60
        cycle_pnl = self.risk_manager.daily_pnl
        cycle_wins = sum(
            s.get("wins", 0) for s in self.dashboard.strategy_stats.values()
        )
        cycle_losses = sum(
            s.get("losses", 0) for s in self.dashboard.strategy_stats.values()
        )
        cycle_trades = cycle_wins + cycle_losses

        self._cycle_count += 1
        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_name = f"cycle_{ts}_dd{self._cycle_count}"
        archive_dir = os.path.join("logs", "_archive", archive_name)
        os.makedirs(archive_dir, exist_ok=True)

        self.dashboard.log(
            f"[Cycle] Drawdown hit after {cycle_duration_m:.0f}min "
            f"({cycle_trades} trades, {cycle_wins}W/{cycle_losses}L, "
            f"PnL=${cycle_pnl:.2f}). Archiving..."
        )
        logger.info("[Cycle] Archiving session data to %s", archive_dir)

        # FR-0.2 (Phase 0 teardown): the cycle-boundary retrain that used to
        # run here (in-process or via the 2h daemon) was removed. Cycle records
        # no longer carry training metrics.

        # Save cycle record
        cycle_record = {
            "cycle": self._cycle_count,
            "timestamp": ts,
            "duration_min": round(cycle_duration_m, 1),
            "pnl": round(cycle_pnl, 2),
            "trades": cycle_trades,
            "wins": cycle_wins,
            "losses": cycle_losses,
            "win_rate": round(cycle_wins / max(1, cycle_wins + cycle_losses) * 100, 1),
        }
        self.cycle_history.append(cycle_record)

        # Persist win rates before cycle reset (they survive across cycles)
        self.risk_manager._save_win_rates()

        # Reset risk manager for new cycle. Position closes below still record
        # against the OLD dashboard/session (self.dashboard is swapped after).
        new_bal = self.sim_balance if self.sim_balance > 0 else 3000.0
        self.risk_manager.update_balance(new_bal)
        self.risk_manager.daily_pnl = 0.0
        self.risk_manager.strategy_pnl = {}
        self.risk_manager.loss_cooldown = {}
        self.risk_manager.last_trade_time = _dt.min
        survivors = self._rollover_positions()

        # FR-1.5 accounting provenance (see _rollover_positions' docstring):
        # this cycle's ``pnl`` above excludes the survivors entirely — their
        # settlement PnL will be reported by whichever cycle they settle in.
        # Record how much is in flight so a per-cycle PnL reader can see it.
        cycle_record["carried_weather_positions"] = len(survivors)
        cycle_record["carried_weather_exposure"] = round(
            sum(
                (1.0 - p["entry_price"]) * p["quantity"]
                if p.get("side") == "sell" and p.get("contract_side", "YES") == "YES"
                else p["entry_price"] * p["quantity"]
                for p in survivors
            ),
            2,
        )

        # FR-0.5: create the NEW session log files BEFORE archiving/removing
        # the old ones, so there is never a zero-session-log window at
        # rollover (the watchdog reads session-log freshness; the old order
        # archived first and left a gap until the new Dashboard appeared).
        # Dashboard.__init__ writes the SESSION STARTED line immediately, so
        # the new session log exists on disk from this point on. The
        # dashboard opens its files per write (no held handles), so the old
        # files can be archived and removed right away.
        prev_alerts = list(self.dashboard.alerts) if self.dashboard else []
        self.dashboard = Dashboard()
        self.dashboard.alerts = prev_alerts
        self.risk_manager.exchange.on_close = self._on_trade_close
        self.risk_manager.exchange.on_alert = self.dashboard.alert
        self._cycle_start_time = time.time()

        # Archive + clean in a single pass, now that the new session files
        # exist and are protected (FR-0.3/FR-0.5).
        self._cycle_archive_and_clean(archive_dir)
        self._profitable_since = None

        # FR-0.4: cycle summary lists EVERY registered bot with its status.
        bot_status_line = self._bot_status_summary()
        cycle_record["bot_status"] = bot_status_line
        logger.info("[Cycle] Bot status: %s", bot_status_line)
        self.dashboard.alert(f"BOTS | {bot_status_line}")

        # Post diagnostics to ALERTS (persistent, visible anytime)
        cr = cycle_record
        self.dashboard.alert(
            f"CYCLE #{cr['cycle']} COMPLETE | "
            f"{cr['duration_min']:.0f}min | {cr['trades']} trades | "
            f"{cr['wins']}W/{cr['losses']}L ({cr['win_rate']:.0f}%) | "
            f"PnL=${cr['pnl']:.0f}"
        )

        # Show trend vs previous cycle
        if len(self.cycle_history) >= 2:
            prev = self.cycle_history[-2]
            dur_d = cr["duration_min"] - prev["duration_min"]
            wr_d = cr["win_rate"] - prev["win_rate"]
            trend_parts = []
            if dur_d != 0:
                trend_parts.append(f"duration {dur_d:+.0f}min")
            if wr_d != 0:
                trend_parts.append(f"winrate {wr_d:+.1f}%")
            if trend_parts:
                self.dashboard.alert(f"TREND | {' | '.join(trend_parts)}")

        # Show running sample count from trade journal
        journal_count = self.trade_journal.get_sample_count()
        self.dashboard.alert(f"JOURNAL | {journal_count} total trade outcomes recorded")

        # Loss analysis from trade journal
        try:
            analysis = self.trade_journal.analyze_losses(n=50)
            if analysis.get("summary"):
                self.dashboard.alert(f"LOSS ANALYSIS | {analysis['summary']}")
        except Exception as exc:
            logger.warning("[Cycle] Loss analysis failed: %s", exc)

        # Resolve pending settlements in background
        try:
            pending = self.settlement_resolver.get_pending_count()
            if pending > 0:
                resolved = self.settlement_resolver.resolve_batch(max_queries=30)
                if resolved:
                    self.dashboard.alert(
                        f"SETTLEMENT | Resolved {resolved} ambiguous contracts"
                    )
        except Exception as exc:
            logger.warning("[Cycle] Settlement resolution failed: %s", exc)

        # Reset counter-trade tracker for new cycle
        self.counter_analyzer.reset_cycle()

        # Persist training state to disk (survives process restarts)
        self._save_training_state()

        # Log for file record
        self.dashboard.log(
            f"[Cycle] #{self._cycle_count} reset to ${new_bal:.2f}. "
            f"History: {len(self.cycle_history)} cycles. "
            f"Journal: {journal_count} outcomes."
        )
        logger.info("[Cycle] Reset complete. Cycle #%d", self._cycle_count)

        # Discord cycle notification — the dashboard's own in-process webhook
        # post (NOT the Hermes Agent cron). Gated on DISCORD_WEBHOOK_URL so it
        # is a silent no-op when unconfigured. send_discord_notification is
        # fire-and-forget (posts in a daemon thread).
        discord_url = os.getenv("DISCORD_WEBHOOK_URL")
        if discord_url:
            try:
                send_discord_notification(discord_url, cycle_record, journal_count)
            except Exception as exc:
                logger.warning("[Cycle] Discord notification failed: %s", exc)

    # ------------------------------------------------------------------
    # FakeEngine guard — defense in depth against silent fixture-only mode
    # (2026-04-16 incident: 2-week silent run on synthetic KX-TEST-* data)
    # ------------------------------------------------------------------

    _FAKE_ENGINE_RECHECK_INTERVAL = 900  # 15 minutes

    def _check_fake_engine(self, context: str = "startup") -> bool:
        """Return True if Kalshi market discovery is healthy (real symbols visible).

        Returns False (and logs ERROR) if discovery returns empty or only
        test/fixture symbols — signal of FakeEngine fallback / lost API connection.
        Skipped (returns True) if no Kalshi provider is configured.
        """
        if not self.kalshi:
            return True
        try:
            result = self.kalshi.search_markets(limit=200)
            markets = result[0] if isinstance(result, tuple) else (result or [])
        except Exception as exc:
            logger.error(
                "[FakeEngineGuard] Discovery probe failed (%s): %s", context, exc
            )
            return False

        real = [m for m in markets if not is_test_symbol(m.get("ticker", ""))]
        test_count = len(markets) - len(real)

        if not real:
            logger.error(
                "FakeEngine detected: market discovery returned only test/fixture "
                "symbols. Real Kalshi connection lost. Refusing to start trading. "
                "Investigate auth, env vars, provider status. (context=%s, "
                "total=%d, test=%d)",
                context,
                len(markets),
                test_count,
            )
            try:
                self.dashboard.alert(
                    "FAKE ENGINE DETECTED — market discovery returned no real symbols. "
                    "Trading halted."
                )
            except Exception:
                pass
            return False

        if test_count:
            logger.warning(
                "[FakeEngineGuard] %s probe saw %d test symbol(s) alongside %d real",
                context,
                test_count,
                len(real),
            )
        return True

    def market_loop(self):
        """Background thread: position updates + bot ticks."""
        last_heartbeat = time.time()
        last_fake_engine_check = time.time()

        # 2026-06-10 fix (b): publish the launch balance so a watchdog
        # auto-restart reuses it. (The ".orchestrator_state" marker write that
        # used to accompany this was removed in Phase 0 — the redesigned
        # watchdog no longer reads it.)
        self._write_sim_balance_marker()

        # Layer 2: refuse to start the trading loop if discovery is fixture-only
        if not self._check_fake_engine(context="startup"):
            self.running = False
            sys.exit(1)

        # Startup: archive stale CSVs from previous sessions (fast — the
        # startup retrain was removed per FR-0.2).
        try:
            self._startup_archive()
        except Exception as exc:
            logger.error("[Startup] Archive failed: %s", exc)

        while self.running:
            try:
                # Heartbeat
                if time.time() - last_heartbeat > 60:
                    self.dashboard.log("[System] Heartbeat: Market Loop is Alive.")
                    last_heartbeat = time.time()

                # Layer 3: re-check that discovery still returns real symbols
                if (
                    time.time() - last_fake_engine_check
                    > self._FAKE_ENGINE_RECHECK_INTERVAL
                ):
                    if not self._check_fake_engine(context="recheck"):
                        self.running = False
                        sys.exit(1)
                    last_fake_engine_check = time.time()

                # Update active positions (cross-bot)
                if self.risk_manager and self.kalshi:
                    active_positions = list(self.risk_manager.exchange.positions)
                    for pos in active_positions:
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

                # Counter-trade analysis on losing positions (LOG-ONLY by default)
                for pos in list(self.risk_manager.exchange.positions):
                    if pos.get("pnl", 0) >= 0:
                        continue  # only check losers
                    try:
                        mkt_price = pos.get("last_market_price", 0)
                        if mkt_price > 0:
                            self.counter_analyzer.should_counter(pos, mkt_price)
                    except Exception:
                        pass

                # Auto-cycle: detect drawdown kill switch
                if self.auto_cycle and self.risk_manager.drawdown_kill_triggered:
                    self._run_drawdown_cycle()
                    last_heartbeat = time.time()
                    continue

                # Auto-cycle: wall-clock fallback (~4h). Sprint 6's 40-trades/day
                # cap made the 50% drawdown trigger geometrically unreachable, so
                # without this fallback the auto-training loop is silently dead.
                _CYCLE_MAX_SECONDS = 4 * 3600
                if (
                    self.auto_cycle
                    and (time.time() - self._cycle_start_time) > _CYCLE_MAX_SECONDS
                ):
                    logger.info(
                        "[Cycle] Wall-clock fallback fired after %.1fh — completing cycle",
                        (time.time() - self._cycle_start_time) / 3600,
                    )
                    self.risk_manager.drawdown_kill_triggered = (
                        True  # reuse existing path
                    )
                    self._run_drawdown_cycle()
                    last_heartbeat = time.time()
                    continue

                # NOTE (FR-0.2): the "graduation" check and the 2h periodic
                # retrain trigger that used to run here were removed in the
                # Phase 0 teardown — no training runs in the runtime process.

                # Tick all active bots (skip deactivated ones)
                for bot in self.bots:
                    if bot.name not in self.active_bots:
                        continue
                    try:
                        bot.tick(self.risk_manager, self.dashboard)
                    except Exception as e:
                        logger.error(f"[{bot.name}] Tick error: {e}")

                time.sleep(2)

            except Exception as e:
                self.dashboard.log(f"Error in loop: {str(e)}")
                time.sleep(2)

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

        self.dashboard.log(
            f"Trading Engine STARTED. Bots: {[b.name for b in self.bots]}"
        )

        # UI loop
        while self.running:
            self.dashboard.render(self.risk_manager)
            time.sleep(1)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=True)

    # FR-0.3: route module-level loggers (strategies, bots, mixins,
    # providers) into the shared money_printer_*.log via the root logger.
    # Console output stays WARNING+ so the terminal UI is not flooded.
    configure_root_logging()

    parser = argparse.ArgumentParser(description="Money Printer Trading Dashboard")
    parser.add_argument(
        "--bot",
        action="append",
        dest="bots",
        help="Bot to run (can specify multiple). Default: all bots. "
        f"Available: {BotRegistry.list_bots()}",
    )

    args = parser.parse_args()

    engine = OrchestratorEngine(bot_names=args.bots)
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n[System] Shutdown Signal Received.")
    finally:
        engine.shutdown()
