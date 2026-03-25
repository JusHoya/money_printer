"""Online model updater — triggers incremental retraining between drawdown cycles.

Not online gradient descent — XGBoost doesn't support that.
This triggers a full retrain from accumulated data when enough
new trade outcomes arrive, rather than waiting for drawdown.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class OnlineModelUpdater:
    """Triggers incremental retraining between drawdown cycles.

    Not online gradient descent -- XGBoost doesn't support that.
    This triggers a full retrain from accumulated data when enough
    new trade outcomes arrive, rather than waiting for drawdown.
    """

    def __init__(
        self,
        predictor,
        trade_journal,
        kalshi_provider=None,
        min_new_outcomes: int = 15,
        min_interval_min: int = 60,
    ):
        self._predictor = predictor  # ModelPredictor instance
        self._journal = trade_journal  # TradeJournal instance
        self._kalshi = kalshi_provider
        self._min_new = min_new_outcomes
        self._min_interval = min_interval_min
        self._outcomes_since_update = 0
        self._last_update: datetime = datetime.min
        self._update_count = 0

    def on_trade_close(self) -> bool:
        """Called after a trade closes. Increments counter and checks if update needed."""
        self._outcomes_since_update += 1
        if self._should_update():
            success = self._retrain_and_reload()
            if success:
                self._outcomes_since_update = 0
                self._last_update = datetime.now()
                self._update_count += 1
            return success
        return False

    def _should_update(self) -> bool:
        """True if >=min_new outcomes AND >=min_interval since last update."""
        if self._outcomes_since_update < self._min_new:
            return False
        elapsed = datetime.now() - self._last_update
        if elapsed < timedelta(minutes=self._min_interval):
            return False
        return True

    def _retrain_and_reload(self) -> bool:
        """Retrain XGBoost from all archived CSV data, validate against old model, reload if better.

        Uses the same pipeline as _run_drawdown_cycle but with a validation guardrail:
        new model must not be >2% worse on recent data than old model.

        Returns True if model was updated.
        """
        try:
            import joblib
            import pandas as pd
            from sklearn.metrics import roc_auc_score

            from scripts.train_from_csv import (
                build_features,
                compute_labels_with_settlement,
                extract_strikes_from_logs,
                infer_strikes,
                load_session,
                train_xgboost,
                walk_forward_split,
            )

            logger.info(
                "[OnlineUpdater] Retraining triggered (%d new outcomes, update #%d)",
                self._outcomes_since_update,
                self._update_count + 1,
            )

            # ---------------------------------------------------------- #
            # 1. Discover all archived CSVs (with filename dedup)
            # ---------------------------------------------------------- #
            archive_dir = Path("logs/_archive")
            logs_dir = Path("logs")
            model_path = Path("data/models/btc_xgboost_latest.joblib")

            data_csvs = (
                sorted(archive_dir.rglob("data_*.csv")) if archive_dir.exists() else []
            )
            log_files = (
                sorted(archive_dir.rglob("money_printer_*.log"))
                if archive_dir.exists()
                else []
            )

            # Also pick up CSVs in the current logs/ directory
            if logs_dir.exists():
                data_csvs.extend(sorted(logs_dir.glob("data_*.csv")))
                log_files.extend(sorted(logs_dir.glob("money_printer_*.log")))

            if not data_csvs:
                logger.warning("[OnlineUpdater] No data CSVs found, aborting retrain")
                return False

            # Deduplicate by filename
            seen_names: set[str] = set()
            unique_csvs = []
            for csv_path in data_csvs:
                if csv_path.name not in seen_names:
                    seen_names.add(csv_path.name)
                    unique_csvs.append(csv_path)

            skipped = len(data_csvs) - len(unique_csvs)
            logger.info(
                "[OnlineUpdater] Found %d CSVs (%d unique, %d dupes skipped)",
                len(data_csvs),
                len(unique_csvs),
                skipped,
            )

            # ---------------------------------------------------------- #
            # 2. Load all sessions
            # ---------------------------------------------------------- #
            log_strikes = extract_strikes_from_logs([str(p) for p in log_files])

            all_btc = []
            all_contract = []
            for csv_path in unique_csvs:
                btc_df, contract_df = load_session(str(csv_path))
                if not btc_df.empty:
                    all_btc.append(btc_df)
                if not contract_df.empty:
                    all_contract.append(contract_df)

            if not all_btc or not all_contract:
                logger.warning("[OnlineUpdater] No BTC or contract data found")
                return False

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

            logger.info(
                "[OnlineUpdater] Data: %d BTC rows, %d contract rows from %d CSVs",
                len(btc_df),
                len(contract_df),
                len(unique_csvs),
            )

            # ---------------------------------------------------------- #
            # 3. Build features and train
            # ---------------------------------------------------------- #
            labels = compute_labels_with_settlement(
                contract_df, kalshi_provider=self._kalshi
            )
            if not labels:
                logger.warning("[OnlineUpdater] No contracts could be labeled")
                return False

            strikes = infer_strikes(btc_df, contract_df, log_strikes)
            df = build_features(
                btc_df, contract_df, strikes, labels, sample_interval_s=60
            )

            if len(df) < 20:
                logger.warning(
                    "[OnlineUpdater] Not enough samples (%d), need >=20", len(df)
                )
                return False

            X_train, y_train, X_val, y_val, X_test, y_test = walk_forward_split(df)
            new_model = train_xgboost(X_train, y_train, X_val, y_val)
            feat_cols = [c for c in df.columns if c.startswith("feat_")]

            # ---------------------------------------------------------- #
            # 4. Compute new model AUC on validation set
            # ---------------------------------------------------------- #
            new_auc = 0.0
            eval_X = X_val if len(X_val) > 0 else X_test
            eval_y = y_val if len(y_val) > 0 else y_test
            if len(eval_X) > 0 and len(eval_y.unique()) > 1:
                try:
                    new_proba = new_model.predict_proba(eval_X)[:, 1]
                    new_auc = roc_auc_score(eval_y, new_proba)
                except Exception as exc:
                    logger.warning(
                        "[OnlineUpdater] New model AUC computation failed: %s", exc
                    )

            logger.info("[OnlineUpdater] New model val AUC: %.4f", new_auc)

            # ---------------------------------------------------------- #
            # 5. Compare against old model (if it exists)
            # ---------------------------------------------------------- #
            old_auc = 0.0
            if model_path.exists():
                try:
                    old_bundle = joblib.load(str(model_path))
                    old_model = old_bundle["model"]
                    old_features = old_bundle.get("feature_names", feat_cols)

                    # Align features: use intersection of old and new feature sets
                    common_feats = [f for f in old_features if f in eval_X.columns]
                    if common_feats and len(eval_y.unique()) > 1:
                        old_proba = old_model.predict_proba(eval_X[common_feats])[:, 1]
                        old_auc = roc_auc_score(eval_y, old_proba)
                        logger.info("[OnlineUpdater] Old model val AUC: %.4f", old_auc)
                except Exception as exc:
                    logger.warning(
                        "[OnlineUpdater] Could not evaluate old model: %s", exc
                    )

            # ---------------------------------------------------------- #
            # 6. Guardrail: reject if new model is >2% worse
            # ---------------------------------------------------------- #
            if old_auc > 0 and new_auc < old_auc - 0.02:
                logger.warning(
                    "[OnlineUpdater] New model rejected: AUC %.4f vs old %.4f "
                    "(regression > 2%%)",
                    new_auc,
                    old_auc,
                )
                return False

            # ---------------------------------------------------------- #
            # 7. Save new model and hot-reload
            # ---------------------------------------------------------- #
            Path("data/models").mkdir(parents=True, exist_ok=True)
            joblib.dump(
                {"model": new_model, "feature_names": feat_cols},
                str(model_path),
            )
            logger.info(
                "[OnlineUpdater] Model saved to %s (AUC %.4f -> %.4f, %d samples, %d contracts)",
                model_path,
                old_auc,
                new_auc,
                len(df),
                len(labels),
            )

            self._predictor.load_models()
            logger.info("[OnlineUpdater] Hot-reloaded models into predictor")

            return True

        except Exception as exc:
            logger.error("[OnlineUpdater] Retrain failed: %s", exc, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return current updater state for dashboard display."""
        return {
            "outcomes_since_update": self._outcomes_since_update,
            "update_count": self._update_count,
            "last_update": (
                self._last_update.isoformat()
                if self._last_update != datetime.min
                else None
            ),
            "min_new_outcomes": self._min_new,
            "min_interval_min": self._min_interval,
        }
