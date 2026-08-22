"""Real-time Brier score computation and tracking.

The Brier score is THE metric for probability prediction quality.
``BS = mean((predicted - actual)^2)`` — lower is better, below 0.20
indicates a genuine edge.

Sprint 2, Task 2.5 of Money Printer V2.

Usage::

    tracker = BrierScoreTracker(window_size=100)
    rolling = tracker.update(0.80, 1)   # predicted 80%, happened
    rolling = tracker.update(0.70, 0)   # predicted 70%, didn't happen

    if tracker.is_degraded():
        logger.warning("Model performance below threshold")

    # Per-model tracking
    tracker.update_tagged("btc_xgb", 0.75, 1)
    tracker.update_tagged("weather_lr", 0.60, 0)
    print(tracker.per_model_scores())
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np

logger = logging.getLogger(__name__)


class BrierScoreTracker:
    """Track and decompose Brier scores in real time.

    Parameters
    ----------
    window_size : int
        Number of most-recent predictions to include in the rolling
        Brier score (default 100).
    """

    def __init__(self, window_size: int = 100) -> None:
        if window_size < 1:
            raise ValueError("window_size must be >= 1")

        self._window_size = window_size
        self._lock = threading.Lock()

        # Rolling window for fast score computation
        self._window: deque = deque(maxlen=window_size)

        # Full history (unbounded)
        self._history: List[dict] = []

        # Per-model tracking
        self._model_history: Dict[str, List[dict]] = {}

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(self, predicted_probability: float, actual_outcome: int) -> float:
        """Record one prediction-outcome pair and return the rolling Brier score.

        Parameters
        ----------
        predicted_probability : float
            Model's predicted probability, should be in [0, 1].
        actual_outcome : int
            Binary label: 0 or 1.

        Returns
        -------
        float
            Current rolling Brier score over the window.
        """
        predicted_probability = float(predicted_probability)
        actual_outcome = int(actual_outcome)
        timestamp = datetime.now(timezone.utc).isoformat()

        entry = {
            "predicted": predicted_probability,
            "actual": actual_outcome,
            "timestamp": timestamp,
        }

        with self._lock:
            self._window.append((predicted_probability, actual_outcome))
            self._history.append(entry)

        return self.score()

    def update_tagged(self, model_name: str, predicted: float, actual: int) -> float:
        """Like :meth:`update` but tagged with a model name for per-model tracking.

        Parameters
        ----------
        model_name : str
            Identifier for the model that produced this prediction.
        predicted : float
            Predicted probability in [0, 1].
        actual : int
            Binary label: 0 or 1.

        Returns
        -------
        float
            Current rolling Brier score (global, not per-model).
        """
        predicted = float(predicted)
        actual = int(actual)
        timestamp = datetime.now(timezone.utc).isoformat()

        entry = {
            "predicted": predicted,
            "actual": actual,
            "timestamp": timestamp,
            "model": model_name,
        }

        with self._lock:
            self._window.append((predicted, actual))
            self._history.append(entry)

            if model_name not in self._model_history:
                self._model_history[model_name] = []
            self._model_history[model_name].append(entry)

        return self.score()

    # ------------------------------------------------------------------
    # Score computation
    # ------------------------------------------------------------------

    def score(self) -> float:
        """Current rolling Brier score over the window.

        Returns
        -------
        float
            ``mean((predicted - actual)^2)`` over the most recent
            ``window_size`` predictions.  Returns 0.0 if no predictions
            have been recorded.
        """
        with self._lock:
            if not self._window:
                return 0.0
            preds = np.array([(p, a) for p, a in self._window])
            return float(np.mean((preds[:, 0] - preds[:, 1]) ** 2))

    @property
    def overall_score(self) -> float:
        """Brier score over ALL predictions (not just the rolling window).

        Returns
        -------
        float
            Global Brier score.  Returns 0.0 if no predictions recorded.
        """
        with self._lock:
            if not self._history:
                return 0.0
            preds = np.array([(h["predicted"], h["actual"]) for h in self._history])
            return float(np.mean((preds[:, 0] - preds[:, 1]) ** 2))

    @property
    def n_predictions(self) -> int:
        """Total number of predictions recorded (all time)."""
        with self._lock:
            return len(self._history)

    # ------------------------------------------------------------------
    # Decomposition
    # ------------------------------------------------------------------

    def decompose(self) -> dict:
        """Brier score decomposition into reliability, resolution, and uncertainty.

        Uses the Murphy (1973) decomposition:

        .. math::

            BS = \\text{reliability} - \\text{resolution} + \\text{uncertainty}

        - **Reliability** (lower is better): measures calibration error.
        - **Resolution** (higher is better): measures how much predictions
          differ from the base rate.
        - **Uncertainty**: base-rate variance ``p_bar * (1 - p_bar)``.

        Returns
        -------
        dict
            ``{'brier': float, 'reliability': float, 'resolution': float,
            'uncertainty': float}``
        """
        with self._lock:
            if not self._window:
                return {
                    "brier": 0.0,
                    "reliability": 0.0,
                    "resolution": 0.0,
                    "uncertainty": 0.0,
                }
            data = np.array([(p, a) for p, a in self._window])

        predictions = data[:, 0]
        outcomes = data[:, 1]
        n = len(predictions)

        # Overall base rate
        p_bar = outcomes.mean()
        uncertainty = float(p_bar * (1.0 - p_bar))

        # Bin predictions for decomposition
        n_bins = min(10, n)
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)

        reliability = 0.0
        resolution = 0.0

        for i in range(n_bins):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            if i < n_bins - 1:
                mask = (predictions >= lo) & (predictions < hi)
            else:
                mask = (predictions >= lo) & (predictions <= hi)

            n_k = mask.sum()
            if n_k == 0:
                continue

            # Average predicted probability in this bin
            f_k = predictions[mask].mean()
            # Observed frequency in this bin
            o_k = outcomes[mask].mean()

            reliability += (n_k / n) * (f_k - o_k) ** 2
            resolution += (n_k / n) * (o_k - p_bar) ** 2

        brier = self.score()

        return {
            "brier": float(brier),
            "reliability": float(reliability),
            "resolution": float(resolution),
            "uncertainty": float(uncertainty),
        }

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def is_degraded(self, threshold: float = 0.25) -> bool:
        """Check whether the model's Brier score exceeds the threshold.

        Parameters
        ----------
        threshold : float
            Maximum acceptable Brier score (default 0.25).

        Returns
        -------
        bool
            ``True`` if the rolling Brier score is above *threshold*,
            indicating degraded model performance.
        """
        return self.score() > threshold

    # ------------------------------------------------------------------
    # History & per-model
    # ------------------------------------------------------------------

    def get_history(self) -> List[dict]:
        """Return full prediction history.

        Returns
        -------
        list of dict
            Each dict has ``{'predicted': float, 'actual': int,
            'timestamp': str}``, and optionally ``'model': str``.
        """
        with self._lock:
            return list(self._history)

    def per_model_scores(self) -> dict:
        """Compute Brier score for each tagged model.

        Returns
        -------
        dict
            ``{model_name: brier_score, ...}`` for all models that have
            been tracked via :meth:`update_tagged`.
        """
        with self._lock:
            scores = {}
            for model_name, entries in self._model_history.items():
                if not entries:
                    scores[model_name] = 0.0
                    continue
                preds = np.array([(e["predicted"], e["actual"]) for e in entries])
                scores[model_name] = float(np.mean((preds[:, 0] - preds[:, 1]) ** 2))
            return scores

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all recorded history and reset the tracker."""
        with self._lock:
            self._window.clear()
            self._history.clear()
            self._model_history.clear()
        logger.info("BrierScoreTracker reset.")
