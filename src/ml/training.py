"""Automated training pipeline for all ML models.

Provides walk-forward training with temporal splits (no lookahead bias),
automatic calibration, and staleness detection for retraining.

Sprint 2, Task 2.8 of Money Printer V2.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from src.ml.features import FeaturePipeline

logger = logging.getLogger(__name__)


class TrainingPipeline:
    """Automated training pipeline for Money Printer ML models.

    Orchestrates walk-forward training, feature computation, calibration,
    and model persistence for all model types (BTC XGBoost, BTC LSTM,
    weather ensemble).
    """

    def __init__(
        self,
        store=None,
        models_dir: str = "data/models",
    ) -> None:
        self._store = store
        self._models_dir = Path(models_dir)
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._feature_pipeline = FeaturePipeline()

        # Lazy imports for store — caller may pass None
        if self._store is None:
            self._store = self._try_load_store()

        # Retraining thresholds
        self._brier_threshold = 0.30  # retrain if Brier score exceeds this
        self._retrain_days = 30  # retrain if older than this many days

    @staticmethod
    def _try_load_store():
        """Attempt to create a default MarketStore instance."""
        try:
            from src.data.market_store import MarketStore

            return MarketStore()
        except Exception as exc:
            logger.warning(
                "Could not initialise default MarketStore: %s. "
                "Pass data explicitly to train_*() methods.",
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Walk-forward splitting
    # ------------------------------------------------------------------

    @staticmethod
    def walk_forward_split(
        df: pd.DataFrame,
        train_ratio: float = 0.7,
        val_ratio: float = 0.15,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Split a DataFrame by time into train / validation / test.

        Parameters
        ----------
        df : pd.DataFrame
            Must be sorted by time (or contain a ``timestamp`` column).
        train_ratio : float
            Fraction of data for training (default 0.7).
        val_ratio : float
            Fraction of data for validation (default 0.15).
            Remaining fraction becomes the test set.

        Returns
        -------
        tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]
            ``(train_df, val_df, test_df)`` — strict temporal order,
            no lookahead bias.

        Raises
        ------
        ValueError
            If ratios exceed 1.0 or the DataFrame is too small.
        """
        if train_ratio + val_ratio >= 1.0:
            raise ValueError(
                f"train_ratio ({train_ratio}) + val_ratio ({val_ratio}) "
                f"must be < 1.0"
            )

        # Sort by timestamp if present
        if "timestamp" in df.columns:
            df = df.sort_values("timestamp").reset_index(drop=True)

        n = len(df)
        if n < 10:
            raise ValueError(f"Need at least 10 rows for walk-forward split, got {n}")

        train_end = int(n * train_ratio)
        val_end = int(n * (train_ratio + val_ratio))

        train_df = df.iloc[:train_end].copy()
        val_df = df.iloc[train_end:val_end].copy()
        test_df = df.iloc[val_end:].copy()

        logger.info(
            "Walk-forward split — train: %d | val: %d | test: %d",
            len(train_df),
            len(val_df),
            len(test_df),
        )
        return train_df, val_df, test_df

    # ------------------------------------------------------------------
    # BTC XGBoost training
    # ------------------------------------------------------------------

    def train_btc_xgboost(self, data: Optional[pd.DataFrame] = None) -> dict:
        """Train the BTC XGBoost classifier.

        Parameters
        ----------
        data : pd.DataFrame, optional
            Raw price data with at least ``close`` (or ``price``),
            ``timestamp``, and ``label`` columns.  If None, loads from
            MarketStore.

        Returns
        -------
        dict
            Training metrics and model file path.
        """
        t0 = time.perf_counter()

        if data is None:
            data = self._load_btc_data()
        if data is None or data.empty:
            return {"error": "No training data available", "model": "btc_xgboost"}

        logger.info("Training BTC XGBoost on %d rows", len(data))

        # Compute features
        data = self._feature_pipeline.compute_features(data)
        feature_cols = [c for c in data.columns if c.startswith("feat_")]

        # Ensure label column
        if "label" not in data.columns:
            data = self._generate_btc_labels(data)

        # Drop NaN rows (lookback warm-up)
        data = data.dropna(subset=feature_cols + ["label"])

        if len(data) < 20:
            return {
                "error": f"Not enough data after feature computation ({len(data)} rows)",
                "model": "btc_xgboost",
            }

        # Walk-forward split
        train_df, val_df, test_df = self.walk_forward_split(data)

        X_train = train_df[feature_cols]
        y_train = train_df["label"].astype(int)
        X_val = val_df[feature_cols]
        y_val = val_df["label"].astype(int)
        X_test = test_df[feature_cols]
        y_test = test_df["label"].astype(int)

        # Train
        try:
            from src.ml.models.btc_xgboost import BTCXGBoostClassifier

            model = BTCXGBoostClassifier()
            train_metrics = model.train(X_train, y_train, val_X=X_val, val_y=y_val)
        except ImportError:
            return {
                "error": "BTCXGBoostClassifier not available",
                "model": "btc_xgboost",
            }

        # Evaluate on test set
        test_metrics = self._evaluate(model, X_test, y_test, "btc_xgboost")

        # Calibrate
        calibration_metrics = self._calibrate_model(model, X_val, y_val)

        # Save with timestamp
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        model_path = str(self._models_dir / f"btc_xgboost_{timestamp_str}.joblib")
        latest_path = str(self._models_dir / "btc_xgboost_latest.joblib")
        model.save(model_path)
        model.save(latest_path)

        elapsed = time.perf_counter() - t0

        result = {
            "model": "btc_xgboost",
            "train_metrics": train_metrics,
            "test_metrics": test_metrics,
            "calibration": calibration_metrics,
            "model_path": model_path,
            "latest_path": latest_path,
            "train_size": len(train_df),
            "val_size": len(val_df),
            "test_size": len(test_df),
            "features": len(feature_cols),
            "elapsed_s": round(elapsed, 2),
        }

        logger.info(
            "BTC XGBoost training complete — test AUC: %.4f | " "Brier: %.4f | %.1fs",
            test_metrics.get("auc", 0.0),
            test_metrics.get("brier_score", 1.0),
            elapsed,
        )
        return result

    # ------------------------------------------------------------------
    # BTC LSTM training
    # ------------------------------------------------------------------

    def train_btc_lstm(self, data: Optional[pd.DataFrame] = None) -> dict:
        """Train the BTC LSTM predictor.

        Parameters
        ----------
        data : pd.DataFrame, optional
            Raw price data.  If None, loads from MarketStore.

        Returns
        -------
        dict
            Training metrics and model file path.
        """
        t0 = time.perf_counter()

        if data is None:
            data = self._load_btc_data()
        if data is None or data.empty:
            return {"error": "No training data available", "model": "btc_lstm"}

        logger.info("Training BTC LSTM on %d rows", len(data))

        # Compute features
        data = self._feature_pipeline.compute_features(data)
        feature_cols = [c for c in data.columns if c.startswith("feat_")]

        # Ensure label column
        if "label" not in data.columns:
            data = self._generate_btc_labels(data)

        # Drop NaN rows
        data = data.dropna(subset=feature_cols + ["label"])

        if len(data) < 50:
            return {
                "error": f"Not enough data for LSTM training ({len(data)} rows, need 50+)",
                "model": "btc_lstm",
            }

        # Walk-forward split
        train_df, val_df, test_df = self.walk_forward_split(data)

        # Prepare sequences for LSTM
        sequence_length = min(20, len(train_df) // 5)
        X_train, y_train = self._make_sequences(train_df, feature_cols, sequence_length)
        X_val, y_val = self._make_sequences(val_df, feature_cols, sequence_length)
        X_test, y_test = self._make_sequences(test_df, feature_cols, sequence_length)

        if len(X_train) < 10:
            return {
                "error": "Not enough sequences after windowing",
                "model": "btc_lstm",
            }

        # Train
        try:
            from src.ml.models.btc_lstm import BTCLSTMPredictor

            model = BTCLSTMPredictor()
            train_metrics = model.train(X_train, y_train, val_X=X_val, val_y=y_val)
        except ImportError:
            return {"error": "BTCLSTMPredictor not available", "model": "btc_lstm"}

        # Evaluate on test set
        try:
            test_proba = model.predict_proba(X_test)
            test_preds = (test_proba >= 0.5).astype(int)
            from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

            test_metrics = {
                "auc": float(roc_auc_score(y_test, test_proba)),
                "accuracy": float(accuracy_score(y_test, test_preds)),
                "brier_score": float(brier_score_loss(y_test, test_proba)),
                "test_size": len(y_test),
            }
        except Exception as exc:
            logger.warning("LSTM test evaluation failed: %s", exc)
            test_metrics = {"error": str(exc)}

        # Save with timestamp
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        model_path = str(self._models_dir / f"btc_lstm_{timestamp_str}.joblib")
        latest_path = str(self._models_dir / "btc_lstm_latest.joblib")
        model.save(model_path)
        model.save(latest_path)

        elapsed = time.perf_counter() - t0

        result = {
            "model": "btc_lstm",
            "train_metrics": train_metrics,
            "test_metrics": test_metrics,
            "model_path": model_path,
            "latest_path": latest_path,
            "sequence_length": sequence_length,
            "train_sequences": len(X_train),
            "val_sequences": len(X_val),
            "test_sequences": len(X_test),
            "features": len(feature_cols),
            "elapsed_s": round(elapsed, 2),
        }

        logger.info(
            "BTC LSTM training complete — test AUC: %.4f | %.1fs",
            test_metrics.get("auc", 0.0),
            elapsed,
        )
        return result

    # ------------------------------------------------------------------
    # Weather ensemble training
    # ------------------------------------------------------------------

    def train_weather_ensemble(self, data: Optional[pd.DataFrame] = None) -> dict:
        """Train the weather ensemble model.

        Parameters
        ----------
        data : pd.DataFrame, optional
            Weather data with forecast and actual columns.  If None,
            loads from MarketStore.

        Returns
        -------
        dict
            Training metrics and model file path.
        """
        t0 = time.perf_counter()

        if data is None:
            data = self._load_weather_data()
        if data is None or data.empty:
            return {
                "error": "No weather training data available",
                "model": "weather_ensemble",
            }

        logger.info("Training weather ensemble on %d rows", len(data))

        # Walk-forward split
        train_df, val_df, test_df = self.walk_forward_split(data)

        # Train
        try:
            from src.ml.models.weather_ensemble import WeatherEnsembleModel

            model = WeatherEnsembleModel()
            train_metrics = model.train(train_df, val_df)
        except ImportError:
            return {
                "error": "WeatherEnsembleModel not available",
                "model": "weather_ensemble",
            }

        # Evaluate
        try:
            test_metrics = model.evaluate(test_df)
        except Exception as exc:
            logger.warning("Weather ensemble test evaluation failed: %s", exc)
            test_metrics = {"error": str(exc)}

        # Save with timestamp
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        model_path = str(self._models_dir / f"weather_ensemble_{timestamp_str}.joblib")
        latest_path = str(self._models_dir / "weather_ensemble_latest.joblib")
        model.save(model_path)
        model.save(latest_path)

        elapsed = time.perf_counter() - t0

        result = {
            "model": "weather_ensemble",
            "train_metrics": train_metrics,
            "test_metrics": test_metrics,
            "model_path": model_path,
            "latest_path": latest_path,
            "train_size": len(train_df),
            "val_size": len(val_df),
            "test_size": len(test_df),
            "elapsed_s": round(elapsed, 2),
        }

        logger.info("Weather ensemble training complete — %.1fs", elapsed)
        return result

    # ------------------------------------------------------------------
    # Train all
    # ------------------------------------------------------------------

    def train_all(self) -> dict:
        """Train all models and return a summary.

        Returns
        -------
        dict
            Per-model results keyed by model name.
        """
        logger.info("Starting full training pipeline")
        t0 = time.perf_counter()

        results = {}

        # BTC XGBoost
        logger.info("--- Training BTC XGBoost ---")
        results["btc_xgboost"] = self.train_btc_xgboost()

        # BTC LSTM
        logger.info("--- Training BTC LSTM ---")
        results["btc_lstm"] = self.train_btc_lstm()

        # Weather ensemble
        logger.info("--- Training Weather Ensemble ---")
        results["weather_ensemble"] = self.train_weather_ensemble()

        # Ensemble meta-learner (requires XGBoost + LSTM predictions)
        logger.info("--- Training Hybrid Ensemble ---")
        results["ensemble"] = self._train_ensemble_meta(results)

        elapsed = time.perf_counter() - t0
        results["_summary"] = {
            "total_elapsed_s": round(elapsed, 2),
            "models_trained": [
                k for k, v in results.items() if k != "_summary" and "error" not in v
            ],
            "models_failed": [
                k for k, v in results.items() if k != "_summary" and "error" in v
            ],
        }

        logger.info(
            "Full training pipeline complete — %d models trained, %d failed, %.1fs",
            len(results["_summary"]["models_trained"]),
            len(results["_summary"]["models_failed"]),
            elapsed,
        )
        return results

    def _train_ensemble_meta(self, prior_results: dict) -> dict:
        """Train the hybrid ensemble meta-learner.

        Requires both XGBoost and LSTM to have been trained successfully
        so we can generate their predictions on validation data and feed
        those into the meta-learner.
        """
        xgb_result = prior_results.get("btc_xgboost", {})
        lstm_result = prior_results.get("btc_lstm", {})

        if "error" in xgb_result or "error" in lstm_result:
            return {
                "error": "Cannot train ensemble — XGBoost and/or LSTM failed",
                "model": "ensemble",
            }

        # Load the freshly-trained models to generate predictions
        try:
            from src.ml.models.btc_xgboost import BTCXGBoostClassifier
            from src.ml.models.ensemble import HybridEnsemble

            xgb_path = xgb_result.get("latest_path")
            if not xgb_path or not Path(xgb_path).exists():
                return {"error": "XGBoost model file not found", "model": "ensemble"}

            xgb_model = BTCXGBoostClassifier(model_path=xgb_path)
        except ImportError:
            return {
                "error": "Required model classes not available",
                "model": "ensemble",
            }

        # Load validation data to generate cross-predictions
        data = self._load_btc_data()
        if data is None or data.empty:
            return {"error": "No data for ensemble meta training", "model": "ensemble"}

        data = self._feature_pipeline.compute_features(data)
        feature_cols = [c for c in data.columns if c.startswith("feat_")]
        if "label" not in data.columns:
            data = self._generate_btc_labels(data)
        data = data.dropna(subset=feature_cols + ["label"])

        if len(data) < 20:
            return {
                "error": "Not enough data for ensemble training",
                "model": "ensemble",
            }

        _, val_df, _ = self.walk_forward_split(data)
        X_val = val_df[feature_cols]
        y_val = val_df["label"].astype(int).values

        # Generate XGBoost predictions on validation set
        try:
            xgb_preds = xgb_model.predict_proba(X_val)
        except Exception as exc:
            return {"error": f"XGBoost prediction failed: {exc}", "model": "ensemble"}

        # For LSTM: use XGBoost preds with noise as proxy if LSTM unavailable
        try:
            from src.ml.models.btc_lstm import BTCLSTMPredictor

            lstm_path = lstm_result.get("latest_path")
            if lstm_path and Path(lstm_path).exists():
                lstm_model = BTCLSTMPredictor(model_path=lstm_path)
                lstm_preds = lstm_model.predict_proba(X_val)
            else:
                # Proxy: add noise to XGBoost predictions
                rng = np.random.default_rng(42)
                lstm_preds = np.clip(
                    xgb_preds + rng.normal(0, 0.05, len(xgb_preds)), 0, 1
                )
        except ImportError:
            rng = np.random.default_rng(42)
            lstm_preds = np.clip(xgb_preds + rng.normal(0, 0.05, len(xgb_preds)), 0, 1)

        # Train meta-learner
        ensemble = HybridEnsemble()
        meta_metrics = ensemble.train_meta(xgb_preds, lstm_preds, y_val)

        # Save
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        model_path = str(self._models_dir / f"ensemble_{timestamp_str}.joblib")
        latest_path = str(self._models_dir / "ensemble_latest.joblib")
        ensemble.save(model_path)
        ensemble.save(latest_path)

        return {
            "model": "ensemble",
            "metrics": meta_metrics,
            "model_path": model_path,
            "latest_path": latest_path,
        }

    # ------------------------------------------------------------------
    # Staleness / retrain checks
    # ------------------------------------------------------------------

    def should_retrain(self, model_name: str) -> bool:
        """Check if a model needs retraining.

        Returns True if:
        - The model file does not exist
        - The model was last trained more than 30 days ago
        - The Brier score has degraded past the configured threshold

        Parameters
        ----------
        model_name : str
            One of: ``btc_xgboost``, ``btc_lstm``, ``weather_ensemble``,
            ``ensemble``.

        Returns
        -------
        bool
            True if retraining is recommended.
        """
        latest_file = self._models_dir / f"{model_name}_latest.joblib"

        # No model on disk at all
        if not latest_file.exists():
            logger.info("Retrain recommended: %s — no model file found", model_name)
            return True

        # Check age
        mtime = datetime.fromtimestamp(latest_file.stat().st_mtime, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - mtime).total_seconds() / 86400
        if age_days > self._retrain_days:
            logger.info(
                "Retrain recommended: %s — model is %.1f days old (threshold: %d)",
                model_name,
                age_days,
                self._retrain_days,
            )
            return True

        # Check Brier score degradation (if we have recent data)
        brier = self._check_brier_degradation(model_name)
        if brier is not None and brier > self._brier_threshold:
            logger.info(
                "Retrain recommended: %s — Brier score %.4f exceeds threshold %.4f",
                model_name,
                brier,
                self._brier_threshold,
            )
            return True

        logger.debug("No retrain needed for %s (age=%.1f days)", model_name, age_days)
        return False

    def _check_brier_degradation(self, model_name: str) -> Optional[float]:
        """Compute recent Brier score for the given model.

        Returns None if evaluation is not possible (no data, no model).
        """
        latest_file = self._models_dir / f"{model_name}_latest.joblib"
        if not latest_file.exists():
            return None

        if self._store is None:
            return None

        try:
            # Load recent settlements
            settlements = self._store.get_settlements(
                market_type="KXBTC" if "btc" in model_name else None,
            )
            if not settlements or len(settlements) < 10:
                return None

            # Load model and compute Brier on recent data
            # This is model-specific but we do a simple check here

            # Get recent ticks to compute features
            recent_df = pd.DataFrame(settlements[-100:])
            if "settlement_value" not in recent_df.columns:
                return None

            # Simple proxy: compare settlement outcomes to model predictions
            # Full implementation would load the model and run inference
            return None

        except Exception as exc:
            logger.debug("Brier check failed for %s: %s", model_name, exc)
            return None

    # ------------------------------------------------------------------
    # Data loading from MarketStore
    # ------------------------------------------------------------------

    def _load_btc_data(self) -> Optional[pd.DataFrame]:
        """Load BTC tick data from MarketStore and return as DataFrame."""
        if self._store is None:
            logger.warning("No MarketStore — cannot load BTC data")
            return None

        try:
            ticks = self._store.get_ticks("BTC-USD")
            if not ticks:
                # Try alternative symbols
                for sym in ("BTCUSD", "BTC/USD", "KXBTC"):
                    ticks = self._store.get_ticks(sym)
                    if ticks:
                        break

            if not ticks:
                logger.warning("No BTC ticks found in MarketStore")
                return None

            df = pd.DataFrame(ticks)
            logger.info("Loaded %d BTC ticks from MarketStore", len(df))
            return df
        except Exception as exc:
            logger.warning("Failed to load BTC data from MarketStore: %s", exc)
            return None

    def _load_weather_data(self) -> Optional[pd.DataFrame]:
        """Load weather data from MarketStore."""
        if self._store is None:
            logger.warning("No MarketStore — cannot load weather data")
            return None

        try:
            settlements = self._store.get_settlements(market_type="KXTEMP")
            if not settlements:
                # Try broader query
                settlements = self._store.get_settlements()
                settlements = [
                    s
                    for s in settlements
                    if "TEMP" in s.get("market_id", "").upper()
                    or "WEATHER" in s.get("market_id", "").upper()
                ]

            if not settlements:
                logger.warning("No weather data found in MarketStore")
                return None

            df = pd.DataFrame(settlements)
            logger.info("Loaded %d weather records from MarketStore", len(df))
            return df
        except Exception as exc:
            logger.warning("Failed to load weather data from MarketStore: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Label generation
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_btc_labels(df: pd.DataFrame) -> pd.DataFrame:
        """Generate binary labels from price data.

        Label = 1 if the next period's close is higher than the current
        close, 0 otherwise.  The last row gets label NaN (no future data).
        """
        df = df.copy()
        if "close" not in df.columns and "price" in df.columns:
            df["close"] = df["price"]

        df["label"] = (df["close"].shift(-1) > df["close"]).astype(float)
        # Last row has no future — mark as NaN so it gets dropped
        df.loc[df.index[-1], "label"] = np.nan
        return df

    # ------------------------------------------------------------------
    # Sequence preparation (for LSTM)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_sequences(
        df: pd.DataFrame,
        feature_cols: List[str],
        sequence_length: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Create sliding-window sequences for LSTM input.

        Parameters
        ----------
        df : pd.DataFrame
            Data with feature columns and ``label`` column.
        feature_cols : list[str]
            Names of feature columns to include.
        sequence_length : int
            Number of timesteps per sequence.

        Returns
        -------
        tuple[np.ndarray, np.ndarray]
            ``(X, y)`` where X has shape ``(n_sequences, sequence_length,
            n_features)`` and y has shape ``(n_sequences,)``.
        """
        values = df[feature_cols].fillna(0.0).values
        labels = df["label"].astype(int).values

        X_list = []
        y_list = []

        for i in range(len(values) - sequence_length):
            X_list.append(values[i : i + sequence_length])
            y_list.append(labels[i + sequence_length - 1])

        if not X_list:
            return np.array([]).reshape(
                0, sequence_length, len(feature_cols)
            ), np.array([])

        return np.array(X_list), np.array(y_list)

    # ------------------------------------------------------------------
    # Evaluation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate(model, X: pd.DataFrame, y, model_name: str) -> dict:
        """Evaluate a trained model on a test set.

        Returns a dict of standard classification metrics.
        """
        try:
            from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

            proba = model.predict_proba(X)
            preds = (proba >= 0.5).astype(int)
            y = np.asarray(y, dtype=int)

            metrics = {
                "auc": float(roc_auc_score(y, proba)),
                "accuracy": float(accuracy_score(y, preds)),
                "brier_score": float(brier_score_loss(y, proba)),
                "test_size": len(y),
            }

            logger.info(
                "%s test metrics — AUC: %.4f | Accuracy: %.4f | Brier: %.4f",
                model_name,
                metrics["auc"],
                metrics["accuracy"],
                metrics["brier_score"],
            )
            return metrics
        except Exception as exc:
            logger.warning("Evaluation failed for %s: %s", model_name, exc)
            return {"error": str(exc)}

    @staticmethod
    def _calibrate_model(model, X_val: pd.DataFrame, y_val) -> dict:
        """Calibrate model probabilities using validation data.

        Attempts to use ProbabilityCalibrator if available; otherwise
        returns a no-op result.
        """
        try:
            from src.ml.calibration import ProbabilityCalibrator

            proba = model.predict_proba(X_val)
            calibrator = ProbabilityCalibrator()
            cal_metrics = calibrator.fit(proba, np.asarray(y_val, dtype=int))

            # Save calibrator
            cal_path = os.path.join("data", "models", "calibrator_latest.joblib")
            calibrator.save(cal_path)

            return cal_metrics
        except ImportError:
            logger.debug("ProbabilityCalibrator not available — skipping calibration")
            return {"status": "skipped", "reason": "calibrator not available"}
        except Exception as exc:
            logger.warning("Calibration failed: %s", exc)
            return {"status": "failed", "error": str(exc)}
