"""Hybrid ensemble combining XGBoost + LSTM predictions.

Production model that uses a logistic regression meta-learner to
optimally blend XGBoost and LSTM probabilities.  Falls back to a
weighted average when the meta-learner has not been trained.

Sprint 2, Task 2.7 of Money Printer V2.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = os.path.join("data", "models", "ensemble_latest.joblib")


class HybridEnsemble:
    """Hybrid ensemble combining XGBoost and LSTM predictions.

    When trained, a logistic regression meta-learner blends the two
    models' probabilities.  Before training (or if training data is
    insufficient) the ensemble falls back to a simple weighted average.
    """

    def __init__(
        self,
        xgboost_weight: float = 0.6,
        lstm_weight: float = 0.4,
        model_path: Optional[str] = None,
    ) -> None:
        self._xgboost_weight = xgboost_weight
        self._lstm_weight = lstm_weight
        self._meta_learner: Optional[LogisticRegression] = None
        self._is_trained = False

        if model_path and Path(model_path).exists():
            self.load(model_path)

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train_meta(
        self,
        xgb_predictions: np.ndarray,
        lstm_predictions: np.ndarray,
        actual_outcomes: np.ndarray,
    ) -> dict:
        """Train the logistic regression meta-learner.

        Parameters
        ----------
        xgb_predictions : np.ndarray
            Raw probabilities from the XGBoost model, shape ``(n,)``.
        lstm_predictions : np.ndarray
            Raw probabilities from the LSTM model, shape ``(n,)``.
        actual_outcomes : np.ndarray
            Binary ground-truth labels (0 or 1), shape ``(n,)``.

        Returns
        -------
        dict
            Training metrics: ``auc``, ``accuracy``, ``brier_score``,
            ``log_loss``, ``train_size``, ``meta_weights``.
        """
        xgb_predictions = np.asarray(xgb_predictions, dtype=np.float64).ravel()
        lstm_predictions = np.asarray(lstm_predictions, dtype=np.float64).ravel()
        actual_outcomes = np.asarray(actual_outcomes, dtype=np.int32).ravel()

        if len(xgb_predictions) != len(lstm_predictions):
            raise ValueError(
                "xgb_predictions and lstm_predictions must have the same length."
            )
        if len(xgb_predictions) != len(actual_outcomes):
            raise ValueError(
                "Predictions and actual_outcomes must have the same length."
            )

        # Stack into (n, 2) feature matrix for the meta-learner
        X_meta = np.column_stack([xgb_predictions, lstm_predictions])

        self._meta_learner = LogisticRegression(
            solver="lbfgs",
            max_iter=1000,
            C=1.0,
        )
        self._meta_learner.fit(X_meta, actual_outcomes)
        self._is_trained = True

        # Compute training metrics
        meta_proba = self._meta_learner.predict_proba(X_meta)[:, 1]
        meta_preds = (meta_proba >= 0.5).astype(int)

        auc = float(roc_auc_score(actual_outcomes, meta_proba))
        accuracy = float(accuracy_score(actual_outcomes, meta_preds))
        brier = float(brier_score_loss(actual_outcomes, meta_proba))
        logloss = float(log_loss(actual_outcomes, meta_proba))

        # Extract the learned blending weights for inspection
        meta_weights = {
            "xgb_coef": float(self._meta_learner.coef_[0][0]),
            "lstm_coef": float(self._meta_learner.coef_[0][1]),
            "intercept": float(self._meta_learner.intercept_[0]),
        }

        metrics = {
            "auc": auc,
            "accuracy": accuracy,
            "brier_score": brier,
            "log_loss": logloss,
            "train_size": len(actual_outcomes),
            "meta_weights": meta_weights,
        }

        logger.info(
            "Meta-learner trained — AUC: %.4f | Accuracy: %.4f | "
            "Brier: %.4f | Samples: %d",
            auc,
            accuracy,
            brier,
            len(actual_outcomes),
        )
        logger.info(
            "Meta-learner weights — XGB coef: %.4f | LSTM coef: %.4f | "
            "Intercept: %.4f",
            meta_weights["xgb_coef"],
            meta_weights["lstm_coef"],
            meta_weights["intercept"],
        )

        return metrics

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_proba(
        self,
        xgb_prob: np.ndarray,
        lstm_prob: np.ndarray,
    ) -> np.ndarray:
        """Return calibrated ensemble probabilities.

        If the meta-learner has been trained, it combines the two
        probability streams through logistic regression.  Otherwise,
        falls back to a weighted average using the constructor weights.

        Parameters
        ----------
        xgb_prob : np.ndarray
            XGBoost probabilities, shape ``(n,)`` or scalar.
        lstm_prob : np.ndarray
            LSTM probabilities, shape ``(n,)`` or scalar.

        Returns
        -------
        np.ndarray
            Blended probabilities, shape ``(n,)``.
        """
        xgb_prob = np.asarray(xgb_prob, dtype=np.float64).ravel()
        lstm_prob = np.asarray(lstm_prob, dtype=np.float64).ravel()

        if self._is_trained and self._meta_learner is not None:
            X_meta = np.column_stack([xgb_prob, lstm_prob])
            return self._meta_learner.predict_proba(X_meta)[:, 1]

        # Fallback: weighted average
        logger.debug(
            "Meta-learner not trained — using weighted average "
            "(XGB=%.2f, LSTM=%.2f)",
            self._xgboost_weight,
            self._lstm_weight,
        )
        return self._xgboost_weight * xgb_prob + self._lstm_weight * lstm_prob

    def predict(
        self,
        xgb_prob: np.ndarray,
        lstm_prob: np.ndarray,
        threshold: float = 0.5,
    ) -> np.ndarray:
        """Return binary ensemble predictions.

        Parameters
        ----------
        xgb_prob : np.ndarray
            XGBoost probabilities.
        lstm_prob : np.ndarray
            LSTM probabilities.
        threshold : float
            Classification threshold (default 0.5).

        Returns
        -------
        np.ndarray
            Binary predictions (0 or 1), shape ``(n,)``.
        """
        proba = self.predict_proba(xgb_prob, lstm_prob)
        return (proba >= threshold).astype(int)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        """Save ensemble state to a joblib file.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to ``data/models/ensemble_latest.joblib``.
        """
        path = path or DEFAULT_MODEL_PATH
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "meta_learner": self._meta_learner,
                "is_trained": self._is_trained,
                "xgboost_weight": self._xgboost_weight,
                "lstm_weight": self._lstm_weight,
            },
            path,
        )
        logger.info("Ensemble saved to %s", path)

    def load(self, path: Optional[str] = None) -> None:
        """Load ensemble state from a joblib file.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to ``data/models/ensemble_latest.joblib``.
        """
        path = path or DEFAULT_MODEL_PATH
        data = joblib.load(path)
        self._meta_learner = data["meta_learner"]
        self._is_trained = data["is_trained"]
        self._xgboost_weight = data.get("xgboost_weight", self._xgboost_weight)
        self._lstm_weight = data.get("lstm_weight", self._lstm_weight)
        logger.info("Ensemble loaded from %s (trained=%s)", path, self._is_trained)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def is_trained(self) -> bool:
        """Whether the meta-learner has been trained."""
        return self._is_trained

    def get_weights(self) -> dict:
        """Return the current blending configuration.

        If the meta-learner is trained, returns its learned coefficients.
        Otherwise returns the static fallback weights.
        """
        if self._is_trained and self._meta_learner is not None:
            return {
                "mode": "meta_learner",
                "xgb_coef": float(self._meta_learner.coef_[0][0]),
                "lstm_coef": float(self._meta_learner.coef_[0][1]),
                "intercept": float(self._meta_learner.intercept_[0]),
            }
        return {
            "mode": "weighted_average",
            "xgb_weight": self._xgboost_weight,
            "lstm_weight": self._lstm_weight,
        }
