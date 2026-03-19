"""Unified model serving interface.

Single entry point for all ML predictions.  Loads trained models from
disk and exposes market-type-specific prediction methods.

Sprint 2, Task 2.10 of Money Printer V2.
"""

import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.core.interfaces import MarketData
from src.ml.features import FeaturePipeline

logger = logging.getLogger(__name__)


class ModelPredictor:
    """Unified prediction interface for all Money Printer ML models.

    Auto-loads the latest trained models from *models_dir* and exposes
    ``predict()``, ``predict_btc()``, and ``predict_weather()`` methods.
    Falls back to analytical (tanh-based) estimators when ML models are
    not available.
    """

    def __init__(self, models_dir: str = "data/models") -> None:
        self._models_dir = Path(models_dir)
        self._feature_pipeline = FeaturePipeline()

        # Model slots — populated by load_models()
        self._xgboost = None
        self._lstm = None
        self._ensemble = None
        self._time_optimizer = None
        self._weather_ensemble = None
        self._calibrator = None

        # Track model metadata
        self._model_status: Dict[str, dict] = {}

        # Attempt to load any existing models
        self.load_models()

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def load_models(self) -> None:
        """Reload all models from disk.

        Each model is loaded independently — a failure in one does not
        prevent the others from loading.
        """
        self._model_status = {}

        # XGBoost
        self._xgboost = self._try_load_model(
            "btc_xgboost",
            "src.ml.models.btc_xgboost",
            "BTCXGBoostClassifier",
            "btc_xgboost_latest.joblib",
        )

        # LSTM
        self._lstm = self._try_load_model(
            "btc_lstm",
            "src.ml.models.btc_lstm",
            "BTCLSTMPredictor",
            "btc_lstm_latest.joblib",
        )

        # Ensemble
        self._ensemble = self._try_load_model(
            "ensemble",
            "src.ml.models.ensemble",
            "HybridEnsemble",
            "ensemble_latest.joblib",
        )

        # Time optimizer
        self._time_optimizer = self._try_load_model(
            "time_optimizer",
            "src.ml.models.time_optimizer",
            "TimeToExpiryOptimizer",
            "time_optimizer_latest.joblib",
        )

        # Weather ensemble
        self._weather_ensemble = self._try_load_model(
            "weather_ensemble",
            "src.ml.models.weather_ensemble",
            "WeatherEnsembleModel",
            "weather_ensemble_latest.joblib",
        )

        # Calibrator
        self._calibrator = self._try_load_model(
            "calibrator",
            "src.ml.calibration",
            "ProbabilityCalibrator",
            "calibrator_latest.joblib",
        )

        loaded = [k for k, v in self._model_status.items() if v.get("loaded")]
        if loaded:
            logger.info("Loaded models: %s", ", ".join(loaded))
        else:
            logger.info(
                "No trained models found in %s — will use analytical fallbacks",
                self._models_dir,
            )

    def _try_load_model(
        self,
        name: str,
        module_path: str,
        class_name: str,
        filename: str,
    ):
        """Attempt to import and load a single model from disk.

        Returns the model instance on success, or None on failure.
        Updates ``_model_status`` either way.
        """
        model_file = self._models_dir / filename
        try:
            import importlib

            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)

            if model_file.exists():
                instance = cls(model_path=str(model_file))
                mtime = datetime.fromtimestamp(
                    model_file.stat().st_mtime, tz=timezone.utc
                )
                self._model_status[name] = {
                    "loaded": True,
                    "path": str(model_file),
                    "last_modified": mtime.isoformat(),
                }
                logger.debug("Loaded %s from %s", name, model_file)
                return instance

            # Class importable but no saved model on disk
            self._model_status[name] = {
                "loaded": False,
                "reason": "no saved model file",
            }
            return None

        except ImportError:
            self._model_status[name] = {
                "loaded": False,
                "reason": f"module {module_path} not available",
            }
            return None
        except Exception as exc:
            self._model_status[name] = {
                "loaded": False,
                "reason": str(exc),
            }
            logger.warning("Failed to load %s: %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # Generic prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        market_data: MarketData,
        time_to_expiry: Optional[float] = None,
        market_type: str = "btc",
    ) -> dict:
        """Main prediction interface.

        Takes raw MarketData + context and returns a prediction dict.
        Internally computes features, runs through the appropriate model
        pipeline, and calibrates probabilities.

        Parameters
        ----------
        market_data : MarketData
            Current market snapshot.
        time_to_expiry : float, optional
            Seconds until contract expiry.
        market_type : str
            ``"btc"`` or ``"weather"``.

        Returns
        -------
        dict
            ``probability``, ``confidence``, ``recommended_price``,
            ``model_used``, ``features_computed``.
        """
        t0 = time.perf_counter()

        # Build a single-row DataFrame for feature computation
        record = {
            "timestamp": market_data.timestamp,
            "close": market_data.price,
            "volume": market_data.volume,
            "bid": market_data.bid,
            "ask": market_data.ask,
        }
        if market_data.extra:
            for key in ("high", "low", "open", "expiry"):
                if key in market_data.extra:
                    record[key] = market_data.extra[key]

        df = pd.DataFrame([record])

        # Compute features
        try:
            df = self._feature_pipeline.compute_features(df)
            feature_cols = [c for c in df.columns if c.startswith("feat_")]
            features_computed = len(feature_cols)
        except Exception as exc:
            logger.warning("Feature computation failed: %s", exc)
            feature_cols = []
            features_computed = 0

        # Route to market-specific prediction
        if market_type == "btc":
            strike = (market_data.extra or {}).get("strike", market_data.price)
            tte = time_to_expiry or (market_data.extra or {}).get(
                "time_to_expiry", 900.0
            )
            result = self._predict_btc_internal(df, feature_cols, strike, tte)
        elif market_type == "weather":
            result = self._predict_weather_fallback(market_data)
        else:
            result = self._predict_analytical_fallback(market_data)

        elapsed_ms = (time.perf_counter() - t0) * 1000

        return {
            "probability": result["probability"],
            "confidence": result["confidence"],
            "recommended_price": result["recommended_price"],
            "model_used": result.get("model_used", "analytical_fallback"),
            "features_computed": features_computed,
            "latency_ms": round(elapsed_ms, 2),
        }

    # ------------------------------------------------------------------
    # BTC-specific prediction
    # ------------------------------------------------------------------

    def predict_btc(
        self,
        market_data: MarketData,
        strike: float,
        time_to_expiry_s: float,
    ) -> dict:
        """BTC-specific prediction using ensemble (XGBoost + LSTM).

        Parameters
        ----------
        market_data : MarketData
            Current BTC market snapshot.
        strike : float
            Contract strike price.
        time_to_expiry_s : float
            Seconds until contract expiry.

        Returns
        -------
        dict
            ``probability``, ``confidence``, ``fair_value``,
            ``recommended_price``, ``edge``.
        """
        # Build feature DataFrame
        record = {
            "timestamp": market_data.timestamp,
            "close": market_data.price,
            "volume": market_data.volume,
            "bid": market_data.bid,
            "ask": market_data.ask,
        }
        if market_data.extra:
            for key in ("high", "low", "open", "expiry"):
                if key in market_data.extra:
                    record[key] = market_data.extra[key]

        df = pd.DataFrame([record])
        try:
            df = self._feature_pipeline.compute_features(df)
            feature_cols = [c for c in df.columns if c.startswith("feat_")]
        except Exception:
            feature_cols = []

        result = self._predict_btc_internal(df, feature_cols, strike, time_to_expiry_s)

        # Compute edge relative to current market mid
        mid = (market_data.bid + market_data.ask) / 2.0 if market_data.ask > 0 else 0.5
        edge = result["fair_value"] - mid

        return {
            "probability": result["probability"],
            "confidence": result["confidence"],
            "fair_value": result["fair_value"],
            "recommended_price": result["recommended_price"],
            "edge": round(edge, 4),
        }

    def _predict_btc_internal(
        self,
        df: pd.DataFrame,
        feature_cols: List[str],
        strike: float,
        time_to_expiry_s: float,
    ) -> dict:
        """Internal BTC prediction logic.

        Tries ML models first, then falls back to analytical tanh
        estimation.
        """
        xgb_prob = None
        lstm_prob = None
        model_used = "analytical_fallback"

        # Try XGBoost
        if self._xgboost is not None and feature_cols:
            try:
                X = df[feature_cols].fillna(0.0)
                xgb_prob = float(self._xgboost.predict_proba(X)[0])
                model_used = "xgboost"
            except Exception as exc:
                logger.debug("XGBoost prediction failed: %s", exc)

        # Try LSTM
        if self._lstm is not None and feature_cols:
            try:
                X = df[feature_cols].fillna(0.0)
                lstm_prob = float(self._lstm.predict_proba(X)[0])
                model_used = "lstm" if xgb_prob is None else model_used
            except Exception as exc:
                logger.debug("LSTM prediction failed: %s", exc)

        # Ensemble if both available
        if xgb_prob is not None and lstm_prob is not None:
            if self._ensemble is not None:
                try:
                    ensemble_prob = float(
                        self._ensemble.predict_proba(
                            np.array([xgb_prob]),
                            np.array([lstm_prob]),
                        )[0]
                    )
                    probability = ensemble_prob
                    model_used = "ensemble"
                except Exception as exc:
                    logger.debug("Ensemble prediction failed: %s", exc)
                    probability = 0.6 * xgb_prob + 0.4 * lstm_prob
                    model_used = "xgboost+lstm_avg"
            else:
                probability = 0.6 * xgb_prob + 0.4 * lstm_prob
                model_used = "xgboost+lstm_avg"
        elif xgb_prob is not None:
            probability = xgb_prob
        elif lstm_prob is not None:
            probability = lstm_prob
        else:
            # Analytical fallback: tanh-based estimation
            current_price = float(df["close"].iloc[0]) if "close" in df.columns else 0.0
            probability = self._tanh_estimate(current_price, strike, time_to_expiry_s)

        # Calibrate if available
        if self._calibrator is not None:
            try:
                probability = float(
                    self._calibrator.calibrate(np.array([probability]))[0]
                )
                model_used += "+calibrated"
            except Exception:
                pass

        # Confidence based on distance from 0.5 and model quality
        confidence = self._compute_confidence(probability, model_used, time_to_expiry_s)

        # Fair value and recommended price
        fair_value = round(probability, 4)
        spread_buffer = 0.02
        recommended_price = round(
            fair_value - spread_buffer
            if probability > 0.5
            else fair_value + spread_buffer,
            4,
        )

        return {
            "probability": round(probability, 4),
            "confidence": round(confidence, 4),
            "fair_value": fair_value,
            "recommended_price": recommended_price,
            "model_used": model_used,
        }

    # ------------------------------------------------------------------
    # Weather-specific prediction
    # ------------------------------------------------------------------

    def predict_weather(
        self,
        nws_forecast: float,
        hrrr_forecast: float,
        station_id: str,
        bracket_lower: float,
        bracket_upper: float,
    ) -> dict:
        """Weather-specific prediction using weather ensemble.

        Parameters
        ----------
        nws_forecast : float
            NWS temperature forecast (Fahrenheit).
        hrrr_forecast : float
            HRRR model temperature forecast (Fahrenheit).
        station_id : str
            Weather station identifier.
        bracket_lower : float
            Lower bound of the temperature bracket.
        bracket_upper : float
            Upper bound of the temperature bracket.

        Returns
        -------
        dict
            ``probability``, ``confidence``, ``bracket``.
        """
        bracket_label = f"{bracket_lower}-{bracket_upper}F"

        # Try ML weather ensemble
        if self._weather_ensemble is not None:
            try:
                result = self._weather_ensemble.predict(
                    nws_forecast=nws_forecast,
                    hrrr_forecast=hrrr_forecast,
                    station_id=station_id,
                    bracket_lower=bracket_lower,
                    bracket_upper=bracket_upper,
                )
                return {
                    "probability": round(float(result.get("probability", 0.5)), 4),
                    "confidence": round(float(result.get("confidence", 0.5)), 4),
                    "bracket": bracket_label,
                }
            except Exception as exc:
                logger.debug("Weather ensemble prediction failed: %s", exc)

        # Analytical fallback
        blended_forecast = 0.6 * nws_forecast + 0.4 * hrrr_forecast
        bracket_mid = (bracket_lower + bracket_upper) / 2.0
        bracket_width = bracket_upper - bracket_lower

        # Simple Gaussian-ish probability based on distance from bracket
        if bracket_width > 0:
            z = (blended_forecast - bracket_mid) / (bracket_width / 2.0)
            # Probability of being IN the bracket
            probability = max(0.05, min(0.95, math.exp(-0.5 * z * z)))
        else:
            probability = 0.5

        # Confidence: higher when forecasts agree
        forecast_spread = abs(nws_forecast - hrrr_forecast)
        confidence = max(0.2, 1.0 - forecast_spread / 10.0)

        return {
            "probability": round(probability, 4),
            "confidence": round(confidence, 4),
            "bracket": bracket_label,
        }

    def _predict_weather_fallback(self, market_data: MarketData) -> dict:
        """Weather fallback when called through generic predict()."""
        extra = market_data.extra or {}
        nws = extra.get("nws_forecast", extra.get("forecast", market_data.price))
        hrrr = extra.get("hrrr_forecast", nws)
        bracket_lower = extra.get("bracket_lower", nws - 5)
        bracket_upper = extra.get("bracket_upper", nws + 5)

        result = self.predict_weather(
            nws_forecast=nws,
            hrrr_forecast=hrrr,
            station_id=extra.get("station_id", "unknown"),
            bracket_lower=bracket_lower,
            bracket_upper=bracket_upper,
        )
        return {
            "probability": result["probability"],
            "confidence": result["confidence"],
            "recommended_price": round(result["probability"], 4),
            "model_used": "weather_analytical",
        }

    # ------------------------------------------------------------------
    # Analytical fallbacks
    # ------------------------------------------------------------------

    @staticmethod
    def _tanh_estimate(
        current_price: float,
        strike: float,
        time_to_expiry_s: float,
    ) -> float:
        """Tanh-based probability estimate for BTC contracts.

        Uses ``tanh(diff / scale)`` with scale=1000 as established in
        the existing trading system.
        """
        if current_price <= 0:
            return 0.5
        diff = current_price - strike
        scale = 1000.0

        # Adjust scale by time-to-expiry: wider scale when more time left
        if time_to_expiry_s > 0:
            time_factor = max(0.3, min(2.0, time_to_expiry_s / 900.0))
            scale *= time_factor

        raw = 0.5 + 0.5 * math.tanh(diff / scale)
        return max(0.01, min(0.99, raw))

    @staticmethod
    def _predict_analytical_fallback(market_data: MarketData) -> dict:
        """Generic analytical fallback for unknown market types."""
        return {
            "probability": 0.5,
            "confidence": 0.1,
            "recommended_price": 0.50,
            "model_used": "analytical_fallback",
        }

    @staticmethod
    def _compute_confidence(
        probability: float,
        model_used: str,
        time_to_expiry_s: float,
    ) -> float:
        """Compute confidence score based on prediction strength and model.

        Higher confidence when:
        - Probability is far from 0.5 (strong signal)
        - A trained ML model was used
        - More time to expiry (more data to work with)
        """
        # Base: distance from 0.5 (0.0 to 0.5 range, scaled to 0-1)
        signal_strength = abs(probability - 0.5) * 2.0

        # Model quality bonus
        model_bonuses = {
            "ensemble": 0.15,
            "xgboost+lstm_avg": 0.10,
            "xgboost": 0.08,
            "lstm": 0.05,
            "analytical_fallback": 0.0,
        }
        # Strip "+calibrated" suffix for lookup
        base_model = model_used.replace("+calibrated", "")
        bonus = model_bonuses.get(base_model, 0.0)

        # Calibration bonus
        if "+calibrated" in model_used:
            bonus += 0.05

        confidence = signal_strength * 0.7 + bonus + 0.1
        return max(0.1, min(0.95, confidence))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return status of all model slots.

        Returns
        -------
        dict
            Per-model load status, last training date, etc.
        """
        return {
            "models_dir": str(self._models_dir),
            "models": dict(self._model_status),
            "available_models": [
                k for k, v in self._model_status.items() if v.get("loaded")
            ],
            "fallback_active": not any(
                v.get("loaded") for v in self._model_status.values()
            ),
        }
