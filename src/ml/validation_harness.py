from __future__ import annotations

import logging
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class TradingValidationHarness:
    """Statistical validation: is the model actually learning or getting lucky?

    Implements three tests:
    1. Monte Carlo permutation test (shuffle trade signs, compare Sharpe)
    2. Deflated Sharpe Ratio (correct for multiple strategy comparisons)
    3. Expanding-window walk-forward (multiple temporal splits)
    """

    # ------------------------------------------------------------------
    # 1. Monte Carlo permutation test
    # ------------------------------------------------------------------
    def monte_carlo_test(self, trade_pnls: List[float], n_perms: int = 10000) -> dict:
        """Permutation test: randomly flip trade signs, compute p-value.

        Returns dict with:
          - observed_sharpe: float
          - p_value: float (probability that result is due to chance)
          - n_perms: int
          - percentile: float (where observed falls in distribution)
        """
        pnls = np.asarray(trade_pnls, dtype=float)

        if len(pnls) < 2 or np.std(pnls) == 0:
            return {
                "observed_sharpe": 0.0,
                "p_value": 1.0,
                "n_perms": n_perms,
                "percentile": 0.0,
            }

        observed_sharpe = float(np.mean(pnls) / np.std(pnls))

        rng = np.random.default_rng(42)
        # Generate random sign flips: shape (n_perms, len(pnls))
        signs = rng.choice([-1, 1], size=(n_perms, len(pnls)))
        permuted = pnls * signs  # broadcast

        perm_means = permuted.mean(axis=1)
        perm_stds = permuted.std(axis=1)

        # Avoid division by zero for any permutation with constant values
        valid = perm_stds > 0
        perm_sharpes = np.full(n_perms, 0.0)
        perm_sharpes[valid] = perm_means[valid] / perm_stds[valid]

        p_value = float(np.mean(perm_sharpes >= observed_sharpe))
        percentile = float(np.mean(perm_sharpes < observed_sharpe) * 100)

        return {
            "observed_sharpe": observed_sharpe,
            "p_value": p_value,
            "n_perms": n_perms,
            "percentile": percentile,
        }

    # ------------------------------------------------------------------
    # 2. Deflated Sharpe Ratio
    # ------------------------------------------------------------------
    def deflated_sharpe(
        self,
        sharpe: float,
        n_trades: int,
        n_strategies_tested: int = 10,
    ) -> dict:
        """Correct Sharpe for multiple comparisons (Lopez de Prado DSR).

        Returns dict with:
          - raw_sharpe: float
          - deflated_sharpe: float
          - is_significant: bool
          - n_strategies_tested: int
        """
        if n_trades < 2 or n_strategies_tested < 1:
            return {
                "raw_sharpe": float(sharpe),
                "deflated_sharpe": 0.0,
                "is_significant": False,
                "n_strategies_tested": n_strategies_tested,
            }

        # Bailey & Lopez de Prado: expected maximum Sharpe from N independent
        # strategies that are truly random ~ sqrt(2 * ln(N)).
        expected_max = float(np.sqrt(2.0 * np.log(max(n_strategies_tested, 1))))

        # Standard error of the Sharpe estimate
        se_sharpe = float(np.sqrt((1.0 + 0.5 * sharpe**2) / n_trades))

        # Deflated Sharpe: how many SE above the expected-max-from-chance
        deflated = float((sharpe - expected_max) / se_sharpe) if se_sharpe > 0 else 0.0

        is_significant = deflated > 0

        return {
            "raw_sharpe": float(sharpe),
            "deflated_sharpe": deflated,
            "is_significant": is_significant,
            "n_strategies_tested": n_strategies_tested,
        }

    # ------------------------------------------------------------------
    # 3. Expanding-window walk-forward
    # ------------------------------------------------------------------
    def expanding_window_walkforward(
        self,
        features_df,
        labels,
        model_fn: Optional[Callable] = None,
        n_windows: int = 5,
    ) -> dict:
        """Multiple expanding-window walk-forward validations.

        Args:
            features_df: DataFrame with feat_* columns
            labels: Series of binary labels
            model_fn: callable that takes (X_train, y_train) and returns fitted
                      model with predict_proba method. If None, uses default
                      XGBoost.
            n_windows: number of expanding windows

        Returns dict with:
          - window_aucs: List[float]
          - window_accuracies: List[float]
          - mean_auc: float
          - std_auc: float
          - is_consistent: bool (std < 0.10)
        """
        from sklearn.metrics import roc_auc_score

        empty_result = {
            "window_aucs": [],
            "window_accuracies": [],
            "mean_auc": 0.0,
            "std_auc": 1.0,
            "is_consistent": False,
        }

        try:
            import pandas as pd

            features_df = pd.DataFrame(features_df)
            labels = np.asarray(labels)
        except Exception:
            logger.warning("Failed to convert inputs to DataFrame / array")
            return empty_result

        n = len(features_df)
        if n < 20 or len(labels) != n:
            logger.warning(
                "Not enough data for walk-forward (n=%d, labels=%d)",
                n,
                len(labels),
            )
            return empty_result

        feat_cols = [c for c in features_df.columns if c.startswith("feat_")]
        if not feat_cols:
            feat_cols = list(features_df.columns)

        if model_fn is None:
            model_fn = self._default_model_fn

        window_aucs: List[float] = []
        window_accuracies: List[float] = []

        for i in range(n_windows):
            # Training: first (40% + i*12%) of data
            train_frac = 0.40 + i * 0.12
            # Test: next 12%
            test_frac = 0.12

            train_end = int(n * train_frac)
            test_end = int(n * (train_frac + test_frac))

            if train_end < 10 or test_end > n or test_end <= train_end:
                continue

            X_train = features_df.iloc[:train_end][feat_cols]
            y_train = labels[:train_end]
            X_test = features_df.iloc[train_end:test_end][feat_cols]
            y_test = labels[train_end:test_end]

            # Skip windows where test or train has only one class
            if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
                continue

            if len(X_test) < 5:
                continue

            try:
                model = model_fn(X_train, y_train)
                probas = model.predict_proba(X_test)[:, 1]
                preds = (probas >= 0.5).astype(int)

                auc = float(roc_auc_score(y_test, probas))
                accuracy = float(np.mean(preds == y_test))

                window_aucs.append(auc)
                window_accuracies.append(accuracy)
            except Exception as exc:
                logger.warning("Window %d failed: %s", i, exc)
                continue

        if not window_aucs:
            return empty_result

        mean_auc = float(np.mean(window_aucs))
        std_auc = float(np.std(window_aucs))

        return {
            "window_aucs": window_aucs,
            "window_accuracies": window_accuracies,
            "mean_auc": mean_auc,
            "std_auc": std_auc,
            "is_consistent": std_auc < 0.10,
        }

    # ------------------------------------------------------------------
    # 4. Combined verdict
    # ------------------------------------------------------------------
    def verdict(
        self,
        trade_pnls: Optional[List[float]] = None,
        features_df=None,
        labels=None,
        n_strategies_tested: int = 10,
    ) -> dict:
        """Run all available tests and return combined verdict.

        Returns dict with:
          - verdict: 'LEARNING' / 'INCONCLUSIVE' / 'RANDOM'
          - details: dict of individual test results
          - summary: str (human-readable summary)
        """
        details: dict = {}
        mc_result = None
        dsr_result = None
        wf_result = None

        # --- Monte Carlo + Deflated Sharpe (need trade PnLs) ---
        if trade_pnls is not None and len(trade_pnls) >= 2:
            mc_result = self.monte_carlo_test(trade_pnls)
            details["monte_carlo"] = mc_result

            dsr_result = self.deflated_sharpe(
                sharpe=mc_result["observed_sharpe"],
                n_trades=len(trade_pnls),
                n_strategies_tested=n_strategies_tested,
            )
            details["deflated_sharpe"] = dsr_result

        # --- Walk-forward (need features + labels) ---
        if features_df is not None and labels is not None:
            wf_result = self.expanding_window_walkforward(features_df, labels)
            details["walk_forward"] = wf_result

        # --- Derive verdict ---
        p_value = mc_result["p_value"] if mc_result else None
        deflated_sig = dsr_result["is_significant"] if dsr_result else None
        mean_auc = wf_result["mean_auc"] if wf_result else None

        is_learning = True
        is_random = False

        # Check Monte Carlo
        if p_value is not None:
            if p_value >= 0.20:
                is_random = True
            if p_value >= 0.05:
                is_learning = False

        # Check Deflated Sharpe
        if deflated_sig is not None:
            if not deflated_sig:
                is_learning = False

        # Check walk-forward AUC
        if mean_auc is not None:
            if mean_auc < 0.52:
                is_random = True
            if mean_auc <= 0.55:
                is_learning = False

        if is_random:
            verdict_str = "RANDOM"
        elif is_learning:
            verdict_str = "LEARNING"
        else:
            verdict_str = "INCONCLUSIVE"

        # Build human-readable summary
        parts = []
        if p_value is not None:
            parts.append(f"MC p-value={p_value:.4f}")
        if dsr_result is not None:
            parts.append(
                f"DSR={'significant' if dsr_result['is_significant'] else 'not significant'}"
                f" ({dsr_result['deflated_sharpe']:.2f})"
            )
        if mean_auc is not None:
            parts.append(f"WF AUC={mean_auc:.3f}")
        summary = f"Verdict: {verdict_str}. " + ", ".join(parts)

        return {
            "verdict": verdict_str,
            "details": details,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _default_model_fn(X_train, y_train):
        """Default model factory: XGBClassifier with conservative settings."""
        from xgboost import XGBClassifier

        model = XGBClassifier(
            max_depth=3,
            n_estimators=80,
            learning_rate=0.05,
            use_label_encoder=False,
            eval_metric="logloss",
            verbosity=0,
        )
        model.fit(X_train, y_train)
        return model
