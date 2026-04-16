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
from src.data.kalshi_provider import KalshiProvider
from src.core.risk_manager import RiskManager
from src.bots.registry import BotRegistry
from src.utils.system_utils import prevent_sleep
from src.utils.logger import logger
from src.ml.trade_journal import TradeJournal, TradeOutcome
from src.ml.online_updater import OnlineModelUpdater
from src.strategies.counter_trade import CounterTradeAnalyzer
from src.ml.settlement_resolver import SettlementResolver
from src.notifications.discord import send_discord_notification

# Import bots to trigger registration
import src.bots  # noqa: F401


class OrchestratorEngine:
    _TRAINING_STATE_PATH = os.path.join("data", "training_state.json")
    _PERIODIC_RETRAIN_INTERVAL = 2 * 3600  # 2 hours

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
        self._training_diagnostics = {}  # latest training metrics
        self._training_history = []  # accumulated training history (max 20)
        self._last_periodic_retrain = time.time()

        # Load persisted training state from prior runs
        self._load_training_state()

        # Trade journal — records every closed trade for learning feedback
        self.trade_journal = TradeJournal()

        # Online model updater — retrains between drawdown cycles
        self.online_updater = OnlineModelUpdater(
            predictor=None,  # set after bots are wired up
            trade_journal=self.trade_journal,
            kalshi_provider=self.kalshi,
        )

        # Counter-trade analyzer — LOG-ONLY mode until validated
        self.counter_analyzer = CounterTradeAnalyzer(live=False)

        # Settlement resolver — background label resolution
        self.settlement_resolver = SettlementResolver(
            kalshi_provider=self.kalshi,
        )

        # Wire online updater to the first available predictor
        for bot in self.bots:
            for strat in bot.strategies.values():
                pred = getattr(strat, "predictor", None)
                if pred:
                    self.online_updater._predictor = pred
                    break
            if self.online_updater._predictor:
                break

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
        strategy_name = position.get("strategy_name", "Unknown")
        pnl = position.get("pnl", 0.0)
        self.dashboard.record_strategy_trade_result(strategy_name, pnl)
        logger.info(
            f"[Orchestrator] Strategy Result: {strategy_name} | PnL: ${pnl:+.2f}"
        )

        # Record to trade journal for learning feedback
        try:
            outcome = TradeOutcome.from_position(position)
            self.trade_journal.record(outcome)
        except Exception as exc:
            logger.warning("[Orchestrator] Journal record failed: %s", exc)

        # Trigger online model update check (retrains if enough new data)
        try:
            self.online_updater.on_trade_close()
        except Exception as exc:
            logger.warning("[Orchestrator] Online update failed: %s", exc)

        # Forward Late Sniper closes for adaptive threshold
        for bot in self.bots:
            if strategy_name == "Late Sniper" and "late_sniper" in bot.strategies:
                bot.strategies["late_sniper"]._handle_position_close(position)

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
    # Shared retrain logic
    # ------------------------------------------------------------------

    def _retrain_from_all_data(self, include_live=False) -> dict:
        """Retrain model from all archived (and optionally live) CSV data.

        Returns training metrics dict, empty on failure.
        """
        import json as _json

        try:
            from scripts.train_from_csv import (
                load_session,
                extract_strikes_from_logs,
                compute_labels_with_settlement,
                infer_strikes,
                build_features,
                walk_forward_split,
                train_xgboost,
                compute_outcome_weights,
            )
            from pathlib import Path
            import pandas as pd

            archive_dir = Path("logs/_archive")
            data_csvs = (
                sorted(archive_dir.rglob("data_*.csv")) if archive_dir.exists() else []
            )
            log_files = (
                sorted(archive_dir.rglob("money_printer_*.log"))
                if archive_dir.exists()
                else []
            )

            # Optionally include live CSVs from logs/ (for periodic/startup retrain)
            if include_live:
                logs_dir = Path("logs")
                if logs_dir.exists():
                    data_csvs.extend(sorted(logs_dir.glob("data_*.csv")))
                    log_files.extend(sorted(logs_dir.glob("money_printer_*.log")))

            if not data_csvs:
                logger.warning("[Retrain] No data CSVs found")
                return {}

            log_strikes = extract_strikes_from_logs([str(p) for p in log_files])

            # Deduplicate by filename
            seen_names = set()
            unique_csvs = []
            for csv_path in data_csvs:
                if csv_path.name not in seen_names:
                    seen_names.add(csv_path.name)
                    unique_csvs.append(csv_path)
            skipped = len(data_csvs) - len(unique_csvs)
            if skipped:
                logger.info(
                    "[Retrain] Dedup: %d unique CSVs (skipped %d duplicates)",
                    len(unique_csvs),
                    skipped,
                )

            all_btc, all_contract = [], []
            for csv_path in unique_csvs:
                btc_df, contract_df = load_session(str(csv_path))
                if not btc_df.empty:
                    all_btc.append(btc_df)
                if not contract_df.empty:
                    all_contract.append(contract_df)

            if not all_btc or not all_contract:
                logger.warning("[Retrain] No BTC or contract data found")
                return {}

            btc_df = (
                pd.concat(all_btc, ignore_index=True)
                .drop_duplicates(subset=["timestamp"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            contract_df = (
                pd.concat(all_contract, ignore_index=True)
                .drop_duplicates(subset=["timestamp", "symbol"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )

            unique_contracts = contract_df["symbol"].nunique()
            logger.info(
                "[Retrain] Data: %d BTC rows, %d contract rows, %d unique contracts, "
                "%d unique CSVs",
                len(btc_df),
                len(contract_df),
                unique_contracts,
                len(unique_csvs),
            )

            labels = compute_labels_with_settlement(
                contract_df, kalshi_provider=self.kalshi
            )
            if not labels:
                logger.warning("[Retrain] No contracts could be labeled")
                return {}

            strikes = infer_strikes(btc_df, contract_df, log_strikes)
            df = build_features(
                btc_df, contract_df, strikes, labels, sample_interval_s=60
            )

            if len(df) < 20:
                logger.warning("[Retrain] Not enough samples (%d) to train", len(df))
                return {}

            X_tr, y_tr, X_v, y_v, X_te, y_te = walk_forward_split(df)

            # Compute outcome-weighted samples from trade journal
            journal_outcomes = self.trade_journal.load_all()
            weights = compute_outcome_weights(df, journal_outcomes)
            n_tr = len(X_tr)
            weights_tr = weights[:n_tr] if len(weights) >= n_tr else None

            model = train_xgboost(X_tr, y_tr, X_v, y_v, sample_weight=weights_tr)

            import joblib

            feat_cols = [c for c in df.columns if c.startswith("feat_")]
            joblib.dump(
                {"model": model, "feature_names": feat_cols},
                "data/models/btc_xgboost_latest.joblib",
            )

            # Compute metrics
            from sklearn.metrics import roc_auc_score

            val_auc = 0.0
            try:
                val_auc = roc_auc_score(y_v, model.predict_proba(X_v)[:, 1])
            except Exception:
                pass

            train_metrics = {
                "contracts_labeled": len(labels),
                "training_samples": len(df),
                "unique_contracts_observed": unique_contracts,
                "unique_csvs": len(unique_csvs),
                "feature_names": feat_cols,
                "results": {
                    "val": {"auc": float(val_auc), "n": len(X_v)},
                },
                "label_distribution": {
                    "yes": int(sum(labels.values())),
                    "no": len(labels) - int(sum(labels.values())),
                },
            }

            # Save metadata
            meta_path = os.path.join("data", "models", "btc_xgboost_feature_meta.json")
            with open(meta_path, "w") as mf:
                _json.dump(train_metrics, mf, indent=2)

            logger.info(
                "[Retrain] OK: %d labeled of %d observed contracts, "
                "val AUC=%.4f, %d samples from %d CSVs",
                len(labels),
                unique_contracts,
                val_auc,
                len(df),
                len(unique_csvs),
            )
            return train_metrics

        except Exception as exc:
            logger.error("[Retrain] Error: %s", exc)
            return {}

    def _reload_models_into_strategies(self) -> int:
        """Hot-reload retrained models into all running strategies."""
        reloaded = 0
        for bot in self.bots:
            for strat in bot.strategies.values():
                pred = getattr(strat, "predictor", None)
                if pred and hasattr(pred, "load_models"):
                    pred.load_models()
                    reloaded += 1
        if reloaded:
            logger.info("[Retrain] Reloaded models into %d strategies", reloaded)
        return reloaded

    # ------------------------------------------------------------------
    # Startup archive + retrain
    # ------------------------------------------------------------------

    def _startup_archive_and_retrain(self):
        """Archive stale CSVs from previous sessions and retrain from all data."""
        # Log data inventory BEFORE archiving to see what survived from last run
        self._log_data_inventory("STARTUP-BEFORE")

        # Archive any leftover CSVs from logs/ that aren't the current session
        active_files = {
            os.path.abspath(self.dashboard.data_log_path),
            os.path.abspath(self.dashboard.session_log_path),
            os.path.abspath(self.dashboard.portfolio_log_path),
        }

        stale_files = []
        for f in os.listdir("logs"):
            fpath = os.path.join("logs", f)
            if not os.path.isfile(fpath):
                continue
            if not (f.endswith(".csv") or f.endswith(".log")):
                continue
            if os.path.abspath(fpath) in active_files:
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

        # Retrain from all accumulated data (archive + any live CSVs)
        prev_samples = self._training_diagnostics.get("training_samples", 0)
        logger.info("[Startup] Retraining from all accumulated data...")
        train_metrics = self._retrain_from_all_data(include_live=True)

        if train_metrics:
            new_samples = train_metrics.get("training_samples", 0)
            self._training_diagnostics = train_metrics
            self._reload_models_into_strategies()
            self._save_training_state()

            growth = new_samples - prev_samples
            self.dashboard.log(
                f"[Startup] Retrained: {new_samples} samples "
                f"({growth:+d} vs last), "
                f"{train_metrics.get('contracts_labeled', 0)} contracts"
            )
            if growth > 0:
                self.dashboard.alert(
                    f"STARTUP RETRAIN | {new_samples} samples "
                    f"(+{growth} new) | "
                    f"{train_metrics.get('contracts_labeled', 0)} contracts"
                )
        else:
            logger.info("[Startup] No training data available (yet)")

        # Log data inventory AFTER archiving/retrain for comparison
        self._log_data_inventory("STARTUP-AFTER")

    # ------------------------------------------------------------------
    # Periodic retrain (every 2 hours)
    # ------------------------------------------------------------------

    def _periodic_retrain(self):
        """Retrain from all data including the current live session."""
        from datetime import datetime as _dt

        prev_samples = self._training_diagnostics.get("training_samples", 0)
        logger.info("[Periodic] Scheduled retrain from all data (including live)...")
        train_metrics = self._retrain_from_all_data(include_live=True)

        if train_metrics:
            new_samples = train_metrics.get("training_samples", 0)
            self._training_diagnostics = train_metrics

            # Append to training history
            self._training_history.append(
                {
                    "timestamp": _dt.now().isoformat(),
                    "trigger": "periodic",
                    "diagnostics": train_metrics,
                }
            )
            if len(self._training_history) > 20:
                self._training_history = self._training_history[-20:]

            self._reload_models_into_strategies()
            self._save_training_state()

            growth = new_samples - prev_samples
            self.dashboard.log(
                f"[Periodic] Retrained: {new_samples} samples "
                f"({growth:+d} vs last), "
                f"{train_metrics.get('contracts_labeled', 0)} contracts"
            )
            if growth > 0:
                self.dashboard.alert(
                    f"PERIODIC RETRAIN | {new_samples} samples "
                    f"(+{growth} new) | "
                    f"{train_metrics.get('contracts_labeled', 0)} contracts"
                )

        self._last_periodic_retrain = time.time()

    def _run_drawdown_cycle(self):
        """Archive logs, retrain model, reset state for next cycle."""
        from datetime import datetime as _dt

        # Immediately clear the flag to prevent re-triggering
        self.risk_manager.drawdown_kill_triggered = False

        # Record cycle metrics BEFORE reset
        cycle_duration_s = time.time() - self._cycle_start_time
        cycle_duration_m = cycle_duration_s / 60
        cycle_pnl = self.risk_manager.daily_pnl
        cycle_trades = sum(
            s.get("signals", 0) for s in self.dashboard.strategy_stats.values()
        )
        cycle_wins = sum(
            s.get("wins", 0) for s in self.dashboard.strategy_stats.values()
        )
        cycle_losses = sum(
            s.get("losses", 0) for s in self.dashboard.strategy_stats.values()
        )

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

        # Clean up ALL CSV/log files from logs/ — they are now in the archive.
        # The current dashboard files will be closed soon (new Dashboard below),
        # so aggressively remove everything we can.
        for f in os.listdir("logs"):
            fpath = os.path.join("logs", f)
            if not os.path.isfile(fpath):
                continue
            if not (f.endswith(".csv") or f.endswith(".log")):
                continue
            try:
                os.remove(fpath)
            except Exception as exc:
                logger.warning("[Cycle] Could not remove old file %s: %s", f, exc)

        # Retrain model from all accumulated archive data
        logger.info("[Cycle] Retraining model in-process...")
        train_metrics = self._retrain_from_all_data(include_live=False)

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
            "train_contracts": train_metrics.get("contracts_labeled", 0),
            "train_val_auc": round(
                train_metrics.get("results", {}).get("val", {}).get("auc", 0), 4
            ),
            "train_samples": train_metrics.get("training_samples", 0),
        }
        self.cycle_history.append(cycle_record)

        # Preserve training history across cycles (max 20 entries)
        self._training_history.append(
            {
                "timestamp": _dt.now().isoformat(),
                "cycle": self._cycle_count,
                "diagnostics": train_metrics,
                "cycle_record": cycle_record,
            }
        )
        if len(self._training_history) > 20:
            self._training_history = self._training_history[-20:]

        self._training_diagnostics = train_metrics

        # Persist win rates before cycle reset (they survive across cycles)
        self.risk_manager._save_win_rates()

        # Reset risk manager for new cycle
        new_bal = self.sim_balance if self.sim_balance > 0 else 3000.0
        self.risk_manager.update_balance(new_bal)
        self.risk_manager.daily_pnl = 0.0
        self.risk_manager.strategy_pnl = {}
        self.risk_manager.loss_cooldown = {}
        self.risk_manager.last_trade_time = _dt.min
        self.risk_manager.exchange.positions.clear()
        self.risk_manager.exchange._next_id = 1
        self.risk_manager.active_positions = 0

        # Reload retrained model into all running strategies
        self._reload_models_into_strategies()

        # Re-init dashboard with fresh log files, preserving alerts
        prev_alerts = list(self.dashboard.alerts) if self.dashboard else []
        self.dashboard = Dashboard()
        self.dashboard.alerts = prev_alerts
        self.risk_manager.exchange.on_close = self._on_trade_close
        self._cycle_start_time = time.time()

        # Second cleanup pass — old dashboard file handles are now closed,
        # so we can remove orphaned files that Windows may have locked earlier.
        new_active = {
            os.path.abspath(self.dashboard.data_log_path),
            os.path.abspath(self.dashboard.session_log_path),
            os.path.abspath(self.dashboard.portfolio_log_path),
        }
        for f in os.listdir("logs"):
            fpath = os.path.join("logs", f)
            if not os.path.isfile(fpath):
                continue
            if not (f.endswith(".csv") or f.endswith(".log")):
                continue
            if os.path.abspath(fpath) in new_active:
                continue
            try:
                os.remove(fpath)
            except Exception:
                pass  # best effort
        self._profitable_since = None

        # Post diagnostics to ALERTS (persistent, visible anytime)
        cr = cycle_record
        self.dashboard.alert(
            f"CYCLE #{cr['cycle']} COMPLETE | "
            f"{cr['duration_min']:.0f}min | {cr['trades']} trades | "
            f"{cr['wins']}W/{cr['losses']}L ({cr['win_rate']:.0f}%) | "
            f"PnL=${cr['pnl']:.0f}"
        )

        if cr["train_val_auc"] > 0:
            self.dashboard.alert(
                f"RETRAINED | {cr['train_contracts']} contracts | "
                f"val AUC={cr['train_val_auc']:.4f} | "
                f"{cr['train_samples']} samples"
            )
        elif cr["train_contracts"] == 0:
            self.dashboard.alert("RETRAIN FAILED — model unchanged")

        # Show trend vs previous cycle
        if len(self.cycle_history) >= 2:
            prev = self.cycle_history[-2]
            dur_d = cr["duration_min"] - prev["duration_min"]
            wr_d = cr["win_rate"] - prev["win_rate"]
            auc_d = cr["train_val_auc"] - prev["train_val_auc"]
            trend_parts = []
            if dur_d != 0:
                trend_parts.append(f"duration {dur_d:+.0f}min")
            if wr_d != 0:
                trend_parts.append(f"winrate {wr_d:+.1f}%")
            if auc_d != 0 and cr["train_val_auc"] > 0:
                trend_parts.append(f"AUC {auc_d:+.4f}")
            if trend_parts:
                self.dashboard.alert(f"TREND | {' | '.join(trend_parts)}")

        # Show top features learned
        feat_names = train_metrics.get("feature_names", [])
        if feat_names:
            self.dashboard.alert(f"TOP FEATURES | {', '.join(feat_names[:5])}")

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

        # Discord notification — DISABLED by operator directive (2026-04-16)
        # Updates moved to Hermes Agent cron in #herms_space channel.
        # discord_url = os.getenv("DISCORD_WEBHOOK_URL")
        # if discord_url:
        #     send_discord_notification(discord_url, cycle_record, journal_count)

    def _graduate_model(self, hours: float, pnl: float):
        """Model is profitable after 8+ hours — save and exit training loop."""
        import json as _json

        self.auto_cycle = False  # Stop the training loop

        # Save model as graduated
        grad_path = os.path.join("data", "models", "btc_xgboost_graduated.joblib")
        src_path = os.path.join("data", "models", "btc_xgboost_latest.joblib")
        if os.path.exists(src_path):
            shutil.copy2(src_path, grad_path)

        # Save graduation metadata
        meta = {
            "graduated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cycle_count": self._cycle_count,
            "final_cycle_hours": round(hours, 1),
            "final_cycle_pnl": round(pnl, 2),
            "cycle_history": self.cycle_history,
        }
        meta_path = os.path.join("data", "models", "graduation_meta.json")
        with open(meta_path, "w") as f:
            _json.dump(meta, f, indent=2)

        # Switch to small seed balance
        self.risk_manager.update_balance(300.0)
        self.sim_balance = 300.0

        # Alert on dashboard
        self.dashboard.alert(
            f"MODEL GRADUATED after {self._cycle_count} cycles! "
            f"Profitable for {hours:.1f}h (PnL=${pnl:.2f}). "
            f"Switched to $300 seed. Saved to {grad_path}"
        )
        self.dashboard.log(
            f"[Graduation] Training loop complete. {self._cycle_count} cycles, "
            f"{len(self.cycle_history)} drawdowns survived."
        )
        logger.info(
            "[Graduation] Model graduated after %d cycles. PnL=$%.2f over %.1fh",
            self._cycle_count,
            pnl,
            hours,
        )

    def market_loop(self):
        """Background thread: position updates + bot ticks."""
        last_heartbeat = time.time()

        # Startup: archive stale CSVs and retrain from all accumulated data
        try:
            self._startup_archive_and_retrain()
        except Exception as exc:
            logger.error("[Startup] Archive/retrain failed: %s", exc)

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

                # Graduation check: 8+ continuous hours of positive PnL
                if self.auto_cycle:
                    pnl = self.risk_manager.daily_pnl
                    if pnl > 0:
                        if self._profitable_since is None:
                            self._profitable_since = time.time()
                        profitable_h = (time.time() - self._profitable_since) / 3600
                        if profitable_h >= 8:
                            self._graduate_model(profitable_h, pnl)
                    else:
                        self._profitable_since = None  # reset clock

                # Periodic retrain: every 2 hours, re-label settled contracts
                # and retrain from all data (including current live session)
                if (
                    time.time() - self._last_periodic_retrain
                    > self._PERIODIC_RETRAIN_INTERVAL
                ):
                    try:
                        self._periodic_retrain()
                    except Exception as exc:
                        logger.error("[Periodic] Retrain failed: %s", exc)
                        self._last_periodic_retrain = time.time()

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
