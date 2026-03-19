"""Probability calibration layer for ML model outputs.

Ensures predicted probabilities are well-calibrated — when the model says
"70% chance", the event should actually occur ~70% of the time.  Critical
for correct Kelly-based position sizing.

Sprint 2, Task 2.4 of Money Printer V2.

Supports two methods:
- **Platt scaling** (sigmoid): fits a/b in ``1/(1+exp(a*p+b))``
- **Isotonic regression**: non-parametric monotone mapping

Usage::

    cal = ProbabilityCalibrator(method="platt")
    cal.fit(raw_probs, outcomes)
    calibrated = cal.calibrate(raw_probs)
    ece = cal.calibration_error(calibrated, outcomes)
"""

import logging
import os
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
from scipy.optimize import minimize
from sklearn.isotonic import IsotonicRegression

logger = logging.getLogger(__name__)


def _default_save_path(method: str) -> str:
    return os.path.join("data", "models", f"calibrator_{method}_latest.joblib")


class ProbabilityCalibrator:
    """Calibrate raw model probabilities via Platt scaling or isotonic regression.

    Parameters
    ----------
    method : str
        ``"platt"`` for sigmoid (Platt) scaling, ``"isotonic"`` for isotonic
        regression.  Default is ``"platt"``.
    """

    _VALID_METHODS = ("platt", "isotonic")

    def __init__(self, method: str = "platt") -> None:
        if method not in self._VALID_METHODS:
            raise ValueError(
                f"method must be one of {self._VALID_METHODS}, got '{method}'"
            )
        self.method = method
        self._fitted = False

        # Platt parameters (sigmoid: 1 / (1 + exp(a*p + b)))
        self._platt_a: float = 0.0
        self._platt_b: float = 0.0

        # Isotonic regression model
        self._isotonic: Optional[IsotonicRegression] = None

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, raw_probabilities: np.ndarray, actual_outcomes: np.ndarray) -> None:
        """Fit the calibration mapping from raw probabilities to calibrated ones.

        Parameters
        ----------
        raw_probabilities : np.ndarray
            Model output probabilities, shape ``(n,)``, values in [0, 1].
        actual_outcomes : np.ndarray
            Binary ground-truth labels (0 or 1), shape ``(n,)``.
        """
        raw_probabilities = np.asarray(raw_probabilities, dtype=np.float64)
        actual_outcomes = np.asarray(actual_outcomes, dtype=np.float64)

        if raw_probabilities.shape != actual_outcomes.shape:
            raise ValueError(
                f"Shape mismatch: raw_probabilities {raw_probabilities.shape} "
                f"vs actual_outcomes {actual_outcomes.shape}"
            )
        if len(raw_probabilities) < 2:
            raise ValueError("Need at least 2 samples to fit calibration.")

        if self.method == "platt":
            self._fit_platt(raw_probabilities, actual_outcomes)
        else:
            self._fit_isotonic(raw_probabilities, actual_outcomes)

        self._fitted = True
        logger.info(
            "Calibrator fitted (method=%s) on %d samples.",
            self.method,
            len(raw_probabilities),
        )

    def _fit_platt(self, raw: np.ndarray, outcomes: np.ndarray) -> None:
        """Fit Platt scaling: sigmoid 1/(1+exp(a*p+b)) via log-loss minimisation."""

        def neg_log_likelihood(params: np.ndarray) -> float:
            a, b = params
            # Calibrated probability via sigmoid
            cal = 1.0 / (1.0 + np.exp(a * raw + b))
            # Clip to avoid log(0)
            eps = 1e-12
            cal = np.clip(cal, eps, 1.0 - eps)
            # Negative log-likelihood (binary cross-entropy)
            nll = -np.mean(
                outcomes * np.log(cal) + (1.0 - outcomes) * np.log(1.0 - cal)
            )
            return nll

        result = minimize(
            neg_log_likelihood,
            x0=np.array([0.0, 0.0]),
            method="Nelder-Mead",
            options={"maxiter": 10000, "xatol": 1e-8, "fatol": 1e-8},
        )
        self._platt_a = float(result.x[0])
        self._platt_b = float(result.x[1])
        logger.debug("Platt params: a=%.6f, b=%.6f", self._platt_a, self._platt_b)

    def _fit_isotonic(self, raw: np.ndarray, outcomes: np.ndarray) -> None:
        """Fit isotonic regression mapping."""
        self._isotonic = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        self._isotonic.fit(raw, outcomes)

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def calibrate(self, raw_probabilities: np.ndarray) -> np.ndarray:
        """Transform raw model probabilities to calibrated probabilities.

        Parameters
        ----------
        raw_probabilities : np.ndarray
            Model output probabilities, shape ``(n,)``.

        Returns
        -------
        np.ndarray
            Calibrated probabilities, shape ``(n,)``, values in [0, 1].

        Raises
        ------
        RuntimeError
            If ``fit()`` has not been called yet.
        """
        self._check_fitted()
        raw_probabilities = np.asarray(raw_probabilities, dtype=np.float64)

        if self.method == "platt":
            calibrated = 1.0 / (
                1.0 + np.exp(self._platt_a * raw_probabilities + self._platt_b)
            )
        else:
            calibrated = self._isotonic.predict(raw_probabilities)

        return np.clip(calibrated, 0.0, 1.0)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def calibration_error(
        self,
        probabilities: np.ndarray,
        outcomes: np.ndarray,
        n_bins: int = 10,
    ) -> float:
        """Compute Expected Calibration Error (ECE).

        ECE is the weighted average of ``|accuracy_k - confidence_k|`` across
        bins, where the weight is the fraction of samples in each bin.

        Parameters
        ----------
        probabilities : np.ndarray
            Predicted probabilities, shape ``(n,)``.
        outcomes : np.ndarray
            Binary ground-truth labels, shape ``(n,)``.
        n_bins : int
            Number of equal-width bins (default 10).

        Returns
        -------
        float
            ECE in [0, 1].  Lower is better.
        """
        probabilities = np.asarray(probabilities, dtype=np.float64)
        outcomes = np.asarray(outcomes, dtype=np.float64)
        n = len(probabilities)
        if n == 0:
            return 0.0

        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0

        for i in range(n_bins):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            if i < n_bins - 1:
                mask = (probabilities >= lo) & (probabilities < hi)
            else:
                # Last bin includes the right edge
                mask = (probabilities >= lo) & (probabilities <= hi)

            bin_count = mask.sum()
            if bin_count == 0:
                continue

            bin_accuracy = outcomes[mask].mean()
            bin_confidence = probabilities[mask].mean()
            ece += (bin_count / n) * abs(bin_accuracy - bin_confidence)

        return float(ece)

    def reliability_diagram_data(
        self,
        probabilities: np.ndarray,
        outcomes: np.ndarray,
        n_bins: int = 10,
    ) -> dict:
        """Compute data for a reliability (calibration) diagram.

        Parameters
        ----------
        probabilities : np.ndarray
            Predicted probabilities, shape ``(n,)``.
        outcomes : np.ndarray
            Binary ground-truth labels, shape ``(n,)``.
        n_bins : int
            Number of equal-width bins (default 10).

        Returns
        -------
        dict
            ``{'bin_centers': list, 'bin_accuracies': list,
            'bin_counts': list, 'ece': float}``
        """
        probabilities = np.asarray(probabilities, dtype=np.float64)
        outcomes = np.asarray(outcomes, dtype=np.float64)
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        bin_centers = []
        bin_accuracies = []
        bin_counts = []

        for i in range(n_bins):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            if i < n_bins - 1:
                mask = (probabilities >= lo) & (probabilities < hi)
            else:
                mask = (probabilities >= lo) & (probabilities <= hi)

            center = (lo + hi) / 2.0
            count = int(mask.sum())

            bin_centers.append(float(center))
            bin_counts.append(count)

            if count > 0:
                bin_accuracies.append(float(outcomes[mask].mean()))
            else:
                bin_accuracies.append(0.0)

        ece = self.calibration_error(probabilities, outcomes, n_bins=n_bins)

        return {
            "bin_centers": bin_centers,
            "bin_accuracies": bin_accuracies,
            "bin_counts": bin_counts,
            "ece": ece,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        """Save calibrator state to a joblib file.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to
            ``data/models/calibrator_{method}_latest.joblib``.
        """
        self._check_fitted()
        path = path or _default_save_path(self.method)
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        state = {
            "method": self.method,
            "platt_a": self._platt_a,
            "platt_b": self._platt_b,
            "isotonic": self._isotonic,
        }
        joblib.dump(state, path)
        logger.info("Calibrator saved to %s", path)

    def load(self, path: Optional[str] = None) -> None:
        """Load calibrator state from a joblib file.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to
            ``data/models/calibrator_{method}_latest.joblib``.
        """
        path = path or _default_save_path(self.method)
        state = joblib.load(path)

        self.method = state["method"]
        self._platt_a = state["platt_a"]
        self._platt_b = state["platt_b"]
        self._isotonic = state["isotonic"]
        self._fitted = True
        logger.info("Calibrator loaded from %s", path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_fitted(self) -> None:
        """Raise if the calibrator has not been fitted."""
        if not self._fitted:
            raise RuntimeError("Calibrator is not fitted. Call fit() or load() first.")
