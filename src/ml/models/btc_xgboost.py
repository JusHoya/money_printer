"""XGBoost binary classifier for BTC contract pricing.

Predicts whether BTC will be above or below strike price at contract expiry.
Sprint 2, Task 2.1 of Money Printer V2.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score
from xgboost import XGBClassifier

from src.ml.features import FeaturePipeline

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = os.path.join("data", "models", "btc_xgboost_latest.joblib")


class BTCXGBoostClassifier:
    """XGBoost binary classifier for BTC contract pricing.

    Predicts probability that BTC will be above the strike price at contract
    expiry (label 1 = above strike, 0 = below strike).
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._pipeline = FeaturePipeline()
        self._model: Optional[XGBClassifier] = None
        self._feature_names: List[str] = self._pipeline.get_feature_names()
        self._is_trained = False

        if model_path and Path(model_path).exists():
            self.load(model_path)

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        val_X: Optional[pd.DataFrame] = None,
        val_y: Optional[pd.Series] = None,
    ) -> dict:
        """Train the XGBoost classifier.

        Parameters
        ----------
        X : pd.DataFrame
            Feature matrix with ``feat_*`` columns.
        y : pd.Series
            Binary labels (1 = above strike, 0 = below).
        val_X, val_y : optional
            Validation set for early stopping.

        Returns
        -------
        dict
            Training metrics: ``auc``, ``accuracy``, ``train_size``.
        """
        xgb_params = {
            "max_depth": 6,
            "n_estimators": 200,
            "learning_rate": 0.05,
            "objective": "binary:logistic",
            "eval_metric": "auc",
        }

        fit_kwargs: dict = {}
        if val_X is not None and val_y is not None:
            xgb_params["early_stopping_rounds"] = 20
            fit_kwargs["eval_set"] = [(val_X, val_y)]
            fit_kwargs["verbose"] = False

        self._model = XGBClassifier(**xgb_params)
        self._model.fit(X, y, **fit_kwargs)
        self._is_trained = True
        self._feature_names = list(X.columns)

        # Compute training metrics
        train_proba = self._model.predict_proba(X)[:, 1]
        train_preds = (train_proba >= 0.5).astype(int)

        auc = roc_auc_score(y, train_proba)
        accuracy = accuracy_score(y, train_preds)

        metrics = {
            "auc": float(auc),
            "accuracy": float(accuracy),
            "train_size": len(X),
        }

        logger.info(
            "Training complete — AUC: %.4f | Accuracy: %.4f | Samples: %d",
            metrics["auc"],
            metrics["accuracy"],
            metrics["train_size"],
        )

        return metrics

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return probability of class 1 (above strike) for each row.

        Returns
        -------
        np.ndarray
            Shape ``(n_samples,)`` with probabilities in [0, 1].
        """
        self._check_trained()
        return self._model.predict_proba(X)[:, 1]

    def predict(self, X: pd.DataFrame, threshold: float = 0.5) -> np.ndarray:
        """Return binary predictions.

        Parameters
        ----------
        threshold : float
            Classification threshold (default 0.5).

        Returns
        -------
        np.ndarray
            Shape ``(n_samples,)`` with values 0 or 1.
        """
        proba = self.predict_proba(X)
        return (proba >= threshold).astype(int)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        """Save model to a joblib file.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to ``data/models/btc_xgboost_latest.joblib``.
        """
        self._check_trained()
        path = path or DEFAULT_MODEL_PATH
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"model": self._model, "feature_names": self._feature_names},
            path,
        )
        logger.info("Model saved to %s", path)

    def load(self, path: str) -> None:
        """Load model from a joblib file.

        Parameters
        ----------
        path : str
            Path to a previously saved ``.joblib`` file.
        """
        data = joblib.load(path)
        self._model = data["model"]
        self._feature_names = data["feature_names"]
        self._is_trained = True
        logger.info("Model loaded from %s", path)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_feature_importance(self) -> pd.Series:
        """Return feature importance scores (gain-based).

        Returns
        -------
        pd.Series
            Indexed by feature name, sorted descending.
        """
        self._check_trained()
        importances = self._model.feature_importances_
        series = pd.Series(importances, index=self._feature_names)
        return series.sort_values(ascending=False)

    def get_feature_names(self) -> List[str]:
        """Return the list of expected feature column names."""
        return list(self._feature_names)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_trained(self) -> None:
        """Raise if the model has not been trained or loaded."""
        if not self._is_trained or self._model is None:
            raise ValueError("Model is not trained. Call train() or load() first.")
