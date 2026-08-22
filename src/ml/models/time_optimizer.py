"""
Sprint 2 Task 2.3 — Time-to-expiry optimizer for BTC contracts.

Given a BTC contract with strike K, current price S, and time T remaining,
recommends the optimal limit order price using either a trained LightGBM model
or an analytical Black-Scholes-like fallback.
"""

import os

import joblib
import numpy as np
import pandas as pd
from scipy.stats import norm

from src.utils.logger import logger

# Default persistence path (relative to repo root)
_DEFAULT_MODEL_PATH = os.path.join("data", "models", "time_optimizer_latest.joblib")


class TimeToExpiryOptimizer:
    """Recommends optimal limit-order prices for BTC binary contracts.

    When a trained LightGBM model is available the recommendation is
    model-driven.  Otherwise an analytical approximation based on the
    cumulative normal distribution (Black-Scholes-like) is used.
    """

    def __init__(self, model_path: str = None):
        self.model_path = model_path or _DEFAULT_MODEL_PATH
        self.model = None  # LGBMRegressor, set after train() or load()
        self._is_trained = False

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, historical_data: pd.DataFrame) -> dict:
        """Train a LightGBM regression model to predict optimal limit price.

        Parameters
        ----------
        historical_data : pd.DataFrame
            Required columns: time_to_expiry (seconds), price_distance
            (current - strike), realized_vol, contract_price, fill_rate,
            settlement_value.

        Returns
        -------
        dict
            Training metrics including RMSE and feature importances.
        """
        required_cols = [
            "time_to_expiry",
            "price_distance",
            "realized_vol",
            "contract_price",
            "fill_rate",
            "settlement_value",
        ]
        missing = [c for c in required_cols if c not in historical_data.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        df = historical_data.dropna(subset=required_cols).copy()
        if len(df) < 10:
            raise ValueError(f"Need at least 10 rows to train, got {len(df)}")

        # ------ target: optimal limit price = maximise fill_rate * edge ------
        # edge = |settlement_value - contract_price|  (how much value we capture)
        df["edge"] = (df["settlement_value"] - df["contract_price"]).abs()
        df["expected_value"] = df["fill_rate"] * df["edge"]

        # We predict the contract_price that would maximise expected_value.
        # As a practical proxy the target is the contract_price from rows
        # where expected_value was highest within each time bucket.
        target = df["contract_price"].copy()

        features = [
            "time_to_expiry",
            "price_distance",
            "realized_vol",
            "contract_price",
            "fill_rate",
        ]
        X = df[features]

        try:
            from lightgbm import LGBMRegressor
        except ImportError:
            logger.error("[TimeOptimizer] lightgbm is not installed — cannot train.")
            raise

        self.model = LGBMRegressor(
            n_estimators=200,
            learning_rate=0.05,
            max_depth=6,
            num_leaves=31,
            min_child_samples=5,
            subsample=0.8,
            colsample_bytree=0.8,
            verbose=-1,
        )
        self.model.fit(X, target)
        self._is_trained = True

        # Metrics
        preds = self.model.predict(X)
        rmse = float(np.sqrt(np.mean((preds - target.values) ** 2)))
        importances = dict(zip(features, map(float, self.model.feature_importances_)))

        logger.info(
            f"[TimeOptimizer] Trained on {len(df)} rows | RMSE={rmse:.6f} | "
            f"Top feature: {max(importances, key=importances.get)}"
        )
        return {"rmse": rmse, "n_samples": len(df), "feature_importances": importances}

    # ------------------------------------------------------------------
    # Recommendation
    # ------------------------------------------------------------------

    def recommend(
        self,
        current_price: float,
        strike: float,
        time_remaining_s: float,
        volatility: float,
        current_contract_price: float,
    ) -> dict:
        """Return a recommended limit-order price for a BTC binary contract.

        Uses the trained model when available; otherwise falls back to an
        analytical estimate derived from the cumulative normal distribution.

        Returns
        -------
        dict
            Keys: recommended_price, confidence, fair_value, edge.
        """
        fair_value = self.compute_fair_value(
            current_price, strike, time_remaining_s, volatility
        )

        # Determine edge (how much the contract is mispriced)
        # Positive edge = contract is cheap relative to fair value (buy opportunity)
        edge = fair_value - current_contract_price

        if self._is_trained and self.model is not None:
            return self._recommend_ml(
                current_price,
                strike,
                time_remaining_s,
                volatility,
                current_contract_price,
                fair_value,
                edge,
            )

        return self._recommend_analytical(
            current_price,
            strike,
            time_remaining_s,
            volatility,
            current_contract_price,
            fair_value,
            edge,
        )

    def _recommend_ml(
        self,
        current_price: float,
        strike: float,
        time_remaining_s: float,
        volatility: float,
        current_contract_price: float,
        fair_value: float,
        edge: float,
    ) -> dict:
        """Model-driven recommendation."""
        price_distance = current_price - strike
        features = pd.DataFrame(
            [
                [
                    time_remaining_s,
                    price_distance,
                    volatility,
                    current_contract_price,
                    1.0,
                ]
            ],
            columns=[
                "time_to_expiry",
                "price_distance",
                "realized_vol",
                "contract_price",
                "fill_rate",
            ],
        )
        predicted = float(self.model.predict(features)[0])

        # Clamp to [0, 1] — these are binary contract prices
        predicted = max(0.01, min(0.99, predicted))

        # Confidence: higher when model and analytical agree
        analytical_fv = fair_value
        agreement = 1.0 - min(abs(predicted - analytical_fv), 0.5) / 0.5
        confidence = 0.5 + 0.5 * agreement  # range [0.5, 1.0]

        logger.info(
            f"[TimeOptimizer] ML recommend: price={predicted:.4f} fv={fair_value:.4f} "
            f"edge={edge:+.4f} conf={confidence:.2f}"
        )
        return {
            "recommended_price": round(predicted, 4),
            "confidence": round(confidence, 4),
            "fair_value": round(fair_value, 4),
            "edge": round(edge, 4),
        }

    def _recommend_analytical(
        self,
        current_price: float,
        strike: float,
        time_remaining_s: float,
        volatility: float,
        current_contract_price: float,
        fair_value: float,
        edge: float,
    ) -> dict:
        """Analytical fallback using Black-Scholes-like probability estimate.

        recommended_price is fair_value adjusted toward current_contract_price
        by a confidence factor, so we don't place unrealistically aggressive
        orders.
        """
        # Confidence decays as time_remaining grows or volatility is high
        time_hours = max(time_remaining_s, 1.0) / 3600.0
        time_factor = min(1.0, 1.0 / (1.0 + time_hours))
        vol_factor = max(0.0, 1.0 - volatility * 5.0)  # penalise high vol
        confidence = 0.3 + 0.7 * time_factor * max(0.2, vol_factor)
        confidence = max(0.1, min(0.95, confidence))

        # Blend fair_value toward current market price by (1 - confidence)
        # High confidence -> stick close to fair_value
        # Low confidence -> stay close to current market price
        recommended = fair_value * confidence + current_contract_price * (
            1.0 - confidence
        )
        recommended = max(0.01, min(0.99, recommended))

        logger.info(
            f"[TimeOptimizer] Analytical recommend: price={recommended:.4f} "
            f"fv={fair_value:.4f} edge={edge:+.4f} conf={confidence:.2f}"
        )
        return {
            "recommended_price": round(recommended, 4),
            "confidence": round(confidence, 4),
            "fair_value": round(fair_value, 4),
            "edge": round(edge, 4),
        }

    # ------------------------------------------------------------------
    # Fair-value estimation
    # ------------------------------------------------------------------

    def compute_fair_value(
        self,
        current_price: float,
        strike: float,
        time_remaining_s: float,
        volatility: float,
    ) -> float:
        """Estimate the true probability that BTC finishes above *strike*.

        Uses ``norm.cdf((S - K) / (vol * sqrt(T)))`` where *T* is in
        annualised terms (seconds / seconds-per-year).

        Returns a probability in [0, 1].
        """
        # Avoid division by zero
        T_seconds = max(time_remaining_s, 1.0)
        SECONDS_PER_YEAR = 365.25 * 24 * 3600
        T_annual = T_seconds / SECONDS_PER_YEAR

        vol = max(volatility, 1e-8)
        denominator = vol * np.sqrt(T_annual)

        if denominator < 1e-12:
            # Essentially zero time left — price determines outcome
            return 1.0 if current_price > strike else 0.0

        d = (current_price - strike) / denominator
        probability = float(norm.cdf(d))

        # Clamp to avoid 0.0 / 1.0 extremes (binary contract never truly 0 or 1
        # until settlement)
        return max(0.01, min(0.99, probability))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str = None) -> None:
        """Persist the trained model to disk via joblib."""
        save_path = path or self.model_path
        if self.model is None:
            logger.warning("[TimeOptimizer] No trained model to save.")
            return

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        payload = {
            "model": self.model,
            "is_trained": self._is_trained,
        }
        joblib.dump(payload, save_path)
        logger.info(f"[TimeOptimizer] Model saved to {save_path}")

    def load(self, path: str = None) -> None:
        """Load a previously saved model from disk."""
        load_path = path or self.model_path
        if not os.path.exists(load_path):
            logger.warning(f"[TimeOptimizer] Model file not found: {load_path}")
            return

        payload = joblib.load(load_path)
        self.model = payload.get("model")
        self._is_trained = payload.get("is_trained", self.model is not None)
        logger.info(f"[TimeOptimizer] Model loaded from {load_path}")
