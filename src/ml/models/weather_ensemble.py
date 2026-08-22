"""
Sprint 2 Task 2.6 — Weather ensemble model.

Combines NWS and HRRR forecast sources, learns per-station/month bias
corrections, and identifies Kalshi bracket mispricings.
"""

import os
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from scipy.stats import norm

from src.utils.logger import logger

# Default persistence path (relative to repo root)
_DEFAULT_MODEL_PATH = os.path.join("data", "models", "weather_ensemble_latest.joblib")


class WeatherEnsembleModel:
    """Ensemble temperature predictor that fuses NWS and HRRR forecasts.

    When trained on historical data the model uses LightGBM (or XGBoost)
    regression plus per-station/month bias tables.  Without training it
    falls back to a fixed weighted average (0.6 * HRRR + 0.4 * NWS) and
    default uncertainty.
    """

    def __init__(self, model_path: str = None):
        self.model_path = model_path or _DEFAULT_MODEL_PATH
        self.model = None  # Regression model, set after train() or load()
        self._is_trained = False

        # Learned bias corrections: {(station_id, month): bias_value}
        self._bias_table: Dict[Tuple[str, int], float] = {}

        # Learned global uncertainty (std of residuals) — used if no
        # per-station estimate is available.
        self._global_uncertainty: float = 2.0  # degrees F default

        # Per-station uncertainty: {station_id: std}
        self._station_uncertainty: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, historical_data: pd.DataFrame) -> dict:
        """Train the ensemble temperature model.

        Parameters
        ----------
        historical_data : pd.DataFrame
            Required columns: nws_forecast_high, hrrr_model_high,
            actual_high, station_id, month, kalshi_bracket_lower,
            kalshi_bracket_upper, bracket_price.

        Returns
        -------
        dict
            Training metrics including RMSE, MAE, bias table size, and
            feature importances.
        """
        required_cols = [
            "nws_forecast_high",
            "hrrr_model_high",
            "actual_high",
            "station_id",
            "month",
            "kalshi_bracket_lower",
            "kalshi_bracket_upper",
            "bracket_price",
        ]
        missing = [c for c in required_cols if c not in historical_data.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        df = historical_data.dropna(
            subset=["nws_forecast_high", "hrrr_model_high", "actual_high"]
        ).copy()
        if len(df) < 10:
            raise ValueError(f"Need at least 10 rows to train, got {len(df)}")

        # ---- Learn per-station/month bias corrections ----
        self._bias_table = {}
        for (station, month), group in df.groupby(["station_id", "month"]):
            # bias = mean(predicted_blend - actual)
            blend = 0.6 * group["hrrr_model_high"] + 0.4 * group["nws_forecast_high"]
            bias = float((blend - group["actual_high"]).mean())
            self._bias_table[(station, int(month))] = bias

        # Per-station residual uncertainty
        self._station_uncertainty = {}
        for station, group in df.groupby("station_id"):
            blend = 0.6 * group["hrrr_model_high"] + 0.4 * group["nws_forecast_high"]
            residuals = group["actual_high"] - blend
            self._station_uncertainty[station] = (
                float(residuals.std()) if len(group) > 2 else 2.0
            )

        # Global uncertainty (residual std across entire dataset)
        blend_all = 0.6 * df["hrrr_model_high"] + 0.4 * df["nws_forecast_high"]
        self._global_uncertainty = float((df["actual_high"] - blend_all).std())

        # ---- Train regression model ----
        # Features: forecasts, station (encoded), month, spread between sources
        df["forecast_spread"] = df["hrrr_model_high"] - df["nws_forecast_high"]
        df["station_encoded"] = df["station_id"].astype("category").cat.codes

        feature_cols = [
            "nws_forecast_high",
            "hrrr_model_high",
            "forecast_spread",
            "station_encoded",
            "month",
        ]
        X = df[feature_cols]
        y = df["actual_high"]

        try:
            from lightgbm import LGBMRegressor

            self.model = LGBMRegressor(
                n_estimators=150,
                learning_rate=0.05,
                max_depth=5,
                num_leaves=31,
                min_child_samples=5,
                subsample=0.8,
                colsample_bytree=0.8,
                verbose=-1,
            )
        except ImportError:
            logger.warning(
                "[WeatherEnsemble] lightgbm not installed, trying xgboost..."
            )
            try:
                from xgboost import XGBRegressor

                self.model = XGBRegressor(
                    n_estimators=150,
                    learning_rate=0.05,
                    max_depth=5,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    verbosity=0,
                )
            except ImportError:
                logger.error(
                    "[WeatherEnsemble] Neither lightgbm nor xgboost installed — cannot train."
                )
                raise

        self.model.fit(X, y)
        self._is_trained = True

        # Store category mapping so we can encode stations at predict time
        self._station_cat_map: Dict[str, int] = dict(
            zip(
                df["station_id"].astype("category").cat.categories,
                range(len(df["station_id"].astype("category").cat.categories)),
            )
        )

        # Metrics
        preds = self.model.predict(X)
        residuals = y.values - preds
        rmse = float(np.sqrt(np.mean(residuals**2)))
        mae = float(np.mean(np.abs(residuals)))
        importances = dict(
            zip(feature_cols, map(float, self.model.feature_importances_))
        )

        logger.info(
            f"[WeatherEnsemble] Trained on {len(df)} rows | RMSE={rmse:.2f}F "
            f"MAE={mae:.2f}F | Bias entries={len(self._bias_table)}"
        )
        return {
            "rmse": rmse,
            "mae": mae,
            "n_samples": len(df),
            "bias_table_size": len(self._bias_table),
            "global_uncertainty": self._global_uncertainty,
            "feature_importances": importances,
        }

    # ------------------------------------------------------------------
    # Temperature prediction
    # ------------------------------------------------------------------

    def predict_temperature(
        self,
        nws_forecast: float,
        hrrr_forecast: float,
        station_id: str,
        month: int = None,
    ) -> dict:
        """Predict the actual daily high temperature.

        Combines NWS and HRRR forecasts with learned bias corrections.
        Falls back to a simple weighted average when untrained.

        Returns
        -------
        dict
            Keys: predicted_high, confidence_interval (low, high),
            bias_correction.
        """
        if self._is_trained and self.model is not None:
            return self._predict_ml(nws_forecast, hrrr_forecast, station_id, month)

        return self._predict_fallback(nws_forecast, hrrr_forecast, station_id, month)

    def _predict_ml(
        self,
        nws_forecast: float,
        hrrr_forecast: float,
        station_id: str,
        month: Optional[int],
    ) -> dict:
        """Model-driven temperature prediction."""
        import datetime as _dt

        if month is None:
            month = _dt.datetime.now().month

        spread = hrrr_forecast - nws_forecast
        station_code = self._station_cat_map.get(station_id, -1)

        features = pd.DataFrame(
            [[nws_forecast, hrrr_forecast, spread, station_code, month]],
            columns=[
                "nws_forecast_high",
                "hrrr_model_high",
                "forecast_spread",
                "station_encoded",
                "month",
            ],
        )
        predicted = float(self.model.predict(features)[0])

        # Bias correction (informational — the model already accounts for it,
        # but we report what the correction would have been)
        bias = self._bias_table.get((station_id, month), 0.0)

        # Uncertainty
        uncertainty = self._station_uncertainty.get(
            station_id, self._global_uncertainty
        )

        logger.info(
            f"[WeatherEnsemble] ML predict: {predicted:.1f}F "
            f"(NWS={nws_forecast:.1f}, HRRR={hrrr_forecast:.1f}, "
            f"bias={bias:+.1f}, station={station_id})"
        )
        return {
            "predicted_high": round(predicted, 1),
            "confidence_interval": (
                round(predicted - 1.96 * uncertainty, 1),
                round(predicted + 1.96 * uncertainty, 1),
            ),
            "bias_correction": round(bias, 2),
        }

    def _predict_fallback(
        self,
        nws_forecast: float,
        hrrr_forecast: float,
        station_id: str,
        month: Optional[int],
    ) -> dict:
        """Simple weighted average fallback (no trained model)."""
        import datetime as _dt

        if month is None:
            month = _dt.datetime.now().month

        blend = 0.6 * hrrr_forecast + 0.4 * nws_forecast

        # Apply bias correction if we have one (could be loaded from a
        # previous training run)
        bias = self._bias_table.get((station_id, month), 0.0)
        predicted = blend - bias

        uncertainty = self._station_uncertainty.get(
            station_id, self._global_uncertainty
        )

        logger.info(
            f"[WeatherEnsemble] Fallback predict: {predicted:.1f}F "
            f"(0.6*HRRR + 0.4*NWS = {blend:.1f}, bias={bias:+.1f})"
        )
        return {
            "predicted_high": round(predicted, 1),
            "confidence_interval": (
                round(predicted - 1.96 * uncertainty, 1),
                round(predicted + 1.96 * uncertainty, 1),
            ),
            "bias_correction": round(bias, 2),
        }

    # ------------------------------------------------------------------
    # Bracket probability
    # ------------------------------------------------------------------

    def predict_bracket_probability(
        self,
        predicted_temp: float,
        bracket_lower: float,
        bracket_upper: float,
        uncertainty: float = 2.0,
    ) -> float:
        """Probability that the actual high falls within [bracket_lower, bracket_upper].

        Uses a normal distribution centred on *predicted_temp* with the
        given standard deviation (*uncertainty*).

        Returns a probability in [0, 1].
        """
        sigma = max(uncertainty, 0.01)
        p_upper = norm.cdf(bracket_upper, loc=predicted_temp, scale=sigma)
        p_lower = norm.cdf(bracket_lower, loc=predicted_temp, scale=sigma)
        probability = float(p_upper - p_lower)
        return max(0.0, min(1.0, probability))

    # ------------------------------------------------------------------
    # Mispricing detection
    # ------------------------------------------------------------------

    def find_mispricings(
        self,
        predictions: dict,
        kalshi_prices: dict,
        min_edge: float = 0.08,
    ) -> List[dict]:
        """Compare model bracket probabilities against Kalshi market prices.

        Parameters
        ----------
        predictions : dict
            Keyed by bracket string (e.g. ``"80-85"``), values are dicts
            with at least ``predicted_high`` and an optional ``uncertainty``.
            Or a single prediction dict with ``predicted_high`` applied
            across all brackets.
        kalshi_prices : dict
            Keyed by bracket string, values are floats (market price 0-1).
        min_edge : float
            Minimum absolute edge to include in results.

        Returns
        -------
        list of dict
            Each dict has: bracket, model_prob, market_price, edge, side.
        """
        mispricings: List[dict] = []

        # Support a single prediction applied to all brackets
        single_pred = None
        if "predicted_high" in predictions:
            single_pred = predictions

        for bracket_key, market_price in kalshi_prices.items():
            # Parse bracket bounds (e.g. "80-85" or "T80" / "B85")
            lower, upper = self._parse_bracket(bracket_key)
            if lower is None or upper is None:
                continue

            # Get prediction for this bracket
            if single_pred:
                pred = single_pred
            else:
                pred = predictions.get(bracket_key, {})
                if "predicted_high" not in pred:
                    continue

            predicted_high = pred["predicted_high"]
            uncertainty = pred.get("uncertainty", self._global_uncertainty)

            model_prob = self.predict_bracket_probability(
                predicted_high, lower, upper, uncertainty
            )
            edge = model_prob - market_price

            if abs(edge) >= min_edge:
                side = "buy" if edge > 0 else "sell"
                mispricings.append(
                    {
                        "bracket": bracket_key,
                        "model_prob": round(model_prob, 4),
                        "market_price": round(market_price, 4),
                        "edge": round(edge, 4),
                        "side": side,
                    }
                )

        # Sort by absolute edge descending (best opportunities first)
        mispricings.sort(key=lambda x: abs(x["edge"]), reverse=True)

        if mispricings:
            logger.info(
                f"[WeatherEnsemble] Found {len(mispricings)} mispricings "
                f"(min_edge={min_edge:.2f}). Best: {mispricings[0]}"
            )
        return mispricings

    @staticmethod
    def _parse_bracket(bracket_key: str) -> Tuple[Optional[float], Optional[float]]:
        """Parse a bracket key into (lower, upper) bounds.

        Supported formats:
        - ``"80-85"``  -> (80.0, 85.0)
        - ``"80.5-85"`` -> (80.5, 85.0)
        """
        try:
            parts = bracket_key.split("-")
            if len(parts) == 2:
                return float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            pass
        return None, None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str = None) -> None:
        """Persist the trained model and bias tables to disk."""
        save_path = path or self.model_path
        if self.model is None and not self._bias_table:
            logger.warning("[WeatherEnsemble] Nothing to save (no trained state).")
            return

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        payload = {
            "model": self.model,
            "is_trained": self._is_trained,
            "bias_table": self._bias_table,
            "global_uncertainty": self._global_uncertainty,
            "station_uncertainty": self._station_uncertainty,
            "station_cat_map": getattr(self, "_station_cat_map", {}),
        }
        joblib.dump(payload, save_path)
        logger.info(f"[WeatherEnsemble] Model saved to {save_path}")

    def load(self, path: str = None) -> None:
        """Load a previously saved model from disk."""
        load_path = path or self.model_path
        if not os.path.exists(load_path):
            logger.warning(f"[WeatherEnsemble] Model file not found: {load_path}")
            return

        payload = joblib.load(load_path)
        self.model = payload.get("model")
        self._is_trained = payload.get("is_trained", self.model is not None)
        self._bias_table = payload.get("bias_table", {})
        self._global_uncertainty = payload.get("global_uncertainty", 2.0)
        self._station_uncertainty = payload.get("station_uncertainty", {})
        self._station_cat_map = payload.get("station_cat_map", {})
        logger.info(
            f"[WeatherEnsemble] Model loaded from {load_path} "
            f"(trained={self._is_trained}, bias_entries={len(self._bias_table)})"
        )
