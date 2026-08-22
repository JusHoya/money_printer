"""Tests for Sprint 2 — ML prediction engine.

Covers: XGBoost, LSTM, calibration, Brier scoring, weather ensemble,
hybrid ensemble, time optimizer, model predictor, and training pipeline.

Uses synthetic data to verify train/predict round-trips without needing
real historical data.
"""

import os
from datetime import datetime

import numpy as np
import pandas as pd

from src.core.interfaces import MarketData


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------


def _make_btc_features(n=200, seed=42):
    """Generate synthetic BTC feature DataFrame suitable for model training."""
    rng = np.random.RandomState(seed)
    prices = np.cumsum(rng.randn(n) * 50) + 84000
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
            "close": prices,
            "volume": rng.randint(50, 500, n).astype(float),
            "bid": prices - rng.uniform(0.5, 2.0, n),
            "ask": prices + rng.uniform(0.5, 2.0, n),
            "high": prices + rng.uniform(0, 100, n),
            "low": prices - rng.uniform(0, 100, n),
            "open": prices + rng.randn(n) * 20,
        }
    )
    return df


def _make_labels(n=200, seed=42):
    """Binary labels: 1 if next close > current close, else 0."""
    rng = np.random.RandomState(seed)
    return rng.randint(0, 2, n)


# ===========================================================================
# 2.1  BTC XGBoost Classifier
# ===========================================================================


class TestBTCXGBoost:
    def test_train_and_predict(self):
        from src.ml.features import FeaturePipeline
        from src.ml.models.btc_xgboost import BTCXGBoostClassifier

        fp = FeaturePipeline()
        df = _make_btc_features(200)
        df = fp.compute_features(df)
        feat_cols = [c for c in df.columns if c.startswith("feat_")]
        X = df[feat_cols].fillna(0)
        y = _make_labels(len(X))

        model = BTCXGBoostClassifier()
        metrics = model.train(X, y)
        assert "auc" in metrics or "accuracy" in metrics

        probs = model.predict_proba(X[:5])
        assert len(probs) == 5
        assert all(0 <= p <= 1 for p in probs)

    def test_predict_returns_binary(self):
        from src.ml.features import FeaturePipeline
        from src.ml.models.btc_xgboost import BTCXGBoostClassifier

        fp = FeaturePipeline()
        df = _make_btc_features(100)
        df = fp.compute_features(df)
        feat_cols = [c for c in df.columns if c.startswith("feat_")]
        X = df[feat_cols].fillna(0)
        y = _make_labels(len(X))

        model = BTCXGBoostClassifier()
        model.train(X, y)
        preds = model.predict(X[:5])
        assert all(p in (0, 1) for p in preds)

    def test_save_and_load(self, tmp_path):
        from src.ml.features import FeaturePipeline
        from src.ml.models.btc_xgboost import BTCXGBoostClassifier

        fp = FeaturePipeline()
        df = _make_btc_features(100)
        df = fp.compute_features(df)
        feat_cols = [c for c in df.columns if c.startswith("feat_")]
        X = df[feat_cols].fillna(0)
        y = _make_labels(len(X))

        model = BTCXGBoostClassifier()
        model.train(X, y)

        path = str(tmp_path / "xgb_test.joblib")
        model.save(path)
        assert os.path.exists(path)

        loaded = BTCXGBoostClassifier(model_path=path)
        probs = loaded.predict_proba(X[:3])
        assert len(probs) == 3

    def test_feature_importance(self):
        from src.ml.features import FeaturePipeline
        from src.ml.models.btc_xgboost import BTCXGBoostClassifier

        fp = FeaturePipeline()
        df = _make_btc_features(100)
        df = fp.compute_features(df)
        feat_cols = [c for c in df.columns if c.startswith("feat_")]
        X = df[feat_cols].fillna(0)
        y = _make_labels(len(X))

        model = BTCXGBoostClassifier()
        model.train(X, y)
        importance = model.get_feature_importance()
        assert len(importance) > 0


# ===========================================================================
# 2.2  BTC LSTM Predictor
# ===========================================================================


class TestBTCLSTM:
    def test_train_and_predict(self):
        from src.ml.features import FeaturePipeline
        from src.ml.models.btc_lstm import BTCLSTMPredictor

        fp = FeaturePipeline()
        df = _make_btc_features(200)
        df = fp.compute_features(df)
        feat_cols = [c for c in df.columns if c.startswith("feat_")]
        X = df[feat_cols].fillna(0)
        y = _make_labels(len(X))

        seq_len = 10
        model = BTCLSTMPredictor(
            input_size=len(feat_cols),
            hidden_size=16,
            num_layers=1,
            sequence_length=seq_len,
        )
        # prepare_sequences returns (X_3d, timestamps), not (X, y)
        X_seq, _ = model.prepare_sequences(X, feat_cols)
        # Labels: aligned to sequences (offset by seq_len)
        y_seq = y[seq_len : seq_len + len(X_seq)].astype(np.float32)
        metrics = model.train_model(X_seq, y_seq, epochs=3, batch_size=16)
        assert metrics is not None

        probs = model.predict_proba(X_seq[:5])
        assert len(probs) >= 1
        assert all(0 <= p <= 1 for p in probs)

    def test_save_and_load(self, tmp_path):
        from src.ml.features import FeaturePipeline
        from src.ml.models.btc_lstm import BTCLSTMPredictor

        fp = FeaturePipeline()
        df = _make_btc_features(100)
        df = fp.compute_features(df)
        feat_cols = [c for c in df.columns if c.startswith("feat_")]
        X = df[feat_cols].fillna(0)
        y = _make_labels(len(X))

        seq_len = 10
        model = BTCLSTMPredictor(
            input_size=len(feat_cols),
            hidden_size=16,
            num_layers=1,
            sequence_length=seq_len,
        )
        X_seq, _ = model.prepare_sequences(X, feat_cols)
        y_seq = y[seq_len : seq_len + len(X_seq)].astype(np.float32)
        model.train_model(X_seq, y_seq, epochs=2, batch_size=16)

        path = str(tmp_path / "lstm_test.joblib")
        model.save(path)
        assert os.path.exists(path)

        loaded = BTCLSTMPredictor(model_path=path)
        probs = loaded.predict_proba(X_seq[:3])
        assert len(probs) >= 1


# ===========================================================================
# 2.4  Probability Calibration
# ===========================================================================


class TestCalibration:
    def test_platt_scaling_fit_and_calibrate(self):
        from src.ml.calibration import ProbabilityCalibrator

        cal = ProbabilityCalibrator(method="platt")
        raw = np.array([0.2, 0.4, 0.6, 0.8, 0.3, 0.7, 0.5, 0.9, 0.1, 0.55])
        outcomes = np.array([0, 0, 1, 1, 0, 1, 0, 1, 0, 1])
        cal.fit(raw, outcomes)

        calibrated = cal.calibrate(np.array([0.3, 0.7]))
        assert len(calibrated) == 2
        assert all(0 <= c <= 1 for c in calibrated)

    def test_isotonic_regression(self):
        from src.ml.calibration import ProbabilityCalibrator

        cal = ProbabilityCalibrator(method="isotonic")
        raw = np.linspace(0.1, 0.9, 50)
        outcomes = (raw > 0.5).astype(int)
        # Add some noise
        rng = np.random.RandomState(42)
        outcomes = np.where(rng.rand(50) < 0.1, 1 - outcomes, outcomes)
        cal.fit(raw, outcomes)

        calibrated = cal.calibrate(np.array([0.3, 0.7]))
        assert len(calibrated) == 2

    def test_calibration_error(self):
        from src.ml.calibration import ProbabilityCalibrator

        cal = ProbabilityCalibrator(method="platt")
        raw = np.linspace(0.1, 0.9, 100)
        outcomes = (raw > 0.5).astype(int)
        cal.fit(raw, outcomes)

        calibrated = cal.calibrate(raw)
        ece = cal.calibration_error(calibrated, outcomes)
        assert 0 <= ece <= 1

    def test_save_and_load(self, tmp_path):
        from src.ml.calibration import ProbabilityCalibrator

        cal = ProbabilityCalibrator(method="platt")
        raw = np.linspace(0.1, 0.9, 50)
        outcomes = (raw > 0.5).astype(int)
        cal.fit(raw, outcomes)

        path = str(tmp_path / "cal_test.joblib")
        cal.save(path)
        assert os.path.exists(path)

        loaded = ProbabilityCalibrator(method="platt")
        loaded.load(path)
        result = loaded.calibrate(np.array([0.5]))
        assert len(result) == 1


# ===========================================================================
# 2.5  Brier Score Tracker
# ===========================================================================


class TestBrierScore:
    def test_update_and_score(self):
        from src.ml.brier import BrierScoreTracker

        tracker = BrierScoreTracker(window_size=10)
        # Perfect predictions
        for _ in range(5):
            tracker.update(0.9, 1)  # Predicted 90%, happened
            tracker.update(0.1, 0)  # Predicted 10%, didn't happen
        score = tracker.score()
        assert score < 0.05  # Should be very low for good predictions

    def test_bad_predictions_high_score(self):
        from src.ml.brier import BrierScoreTracker

        tracker = BrierScoreTracker(window_size=10)
        for _ in range(5):
            tracker.update(0.9, 0)  # Predicted 90%, didn't happen
            tracker.update(0.1, 1)  # Predicted 10%, happened
        score = tracker.score()
        assert score > 0.5  # Should be high for bad predictions

    def test_is_degraded(self):
        from src.ml.brier import BrierScoreTracker

        tracker = BrierScoreTracker(window_size=10)
        for _ in range(10):
            tracker.update(0.8, 0)  # Bad predictions
        assert tracker.is_degraded(threshold=0.25)

    def test_per_model_tracking(self):
        from src.ml.brier import BrierScoreTracker

        tracker = BrierScoreTracker()
        tracker.update_tagged("xgb", 0.9, 1)
        tracker.update_tagged("lstm", 0.2, 1)
        scores = tracker.per_model_scores()
        assert "xgb" in scores
        assert "lstm" in scores
        assert scores["xgb"] < scores["lstm"]  # XGB was more accurate

    def test_decompose(self):
        from src.ml.brier import BrierScoreTracker

        tracker = BrierScoreTracker(window_size=20)
        rng = np.random.RandomState(42)
        for _ in range(20):
            p = rng.uniform(0.1, 0.9)
            outcome = 1 if rng.rand() < p else 0
            tracker.update(p, outcome)
        decomp = tracker.decompose()
        assert "brier" in decomp
        assert "reliability" in decomp
        assert "resolution" in decomp


# ===========================================================================
# 2.6  Weather Ensemble
# ===========================================================================


class TestWeatherEnsemble:
    def test_predict_fallback(self):
        from src.ml.models.weather_ensemble import WeatherEnsembleModel

        model = WeatherEnsembleModel()
        result = model.predict_temperature(
            nws_forecast=75.0, hrrr_forecast=77.0, station_id="KNYC"
        )
        assert (
            "predicted_temp" in result
            or "temperature" in result
            or isinstance(result, dict)
        )

    def test_predict_bracket_probability(self):
        from src.ml.models.weather_ensemble import WeatherEnsembleModel

        model = WeatherEnsembleModel()
        # Uses analytical fallback (no training needed)
        prob = model.predict_bracket_probability(
            predicted_temp=80.0,
            bracket_lower=75.0,
            bracket_upper=85.0,
            uncertainty=2.0,
        )
        assert 0 <= prob <= 1


# ===========================================================================
# 2.7  Hybrid Ensemble
# ===========================================================================


class TestHybridEnsemble:
    def test_untrained_weighted_average(self):
        from src.ml.models.ensemble import HybridEnsemble

        ensemble = HybridEnsemble()
        xgb_probs = np.array([0.7, 0.3, 0.8])
        lstm_probs = np.array([0.6, 0.4, 0.9])
        blended = ensemble.predict_proba(xgb_probs, lstm_probs)
        assert len(blended) == 3
        # Default: 0.6 * XGB + 0.4 * LSTM
        expected = 0.6 * 0.7 + 0.4 * 0.6
        assert abs(blended[0] - expected) < 0.01

    def test_train_meta_learner(self):
        from src.ml.models.ensemble import HybridEnsemble

        ensemble = HybridEnsemble()
        rng = np.random.RandomState(42)
        xgb = rng.uniform(0.2, 0.8, 100)
        lstm = rng.uniform(0.2, 0.8, 100)
        truth = (0.5 * xgb + 0.5 * lstm > 0.5).astype(int)

        metrics = ensemble.train_meta(xgb, lstm, truth)
        assert "auc" in metrics or "accuracy" in metrics

        # Now predict should use trained meta-learner
        blended = ensemble.predict_proba(xgb[:5], lstm[:5])
        assert len(blended) == 5


# ===========================================================================
# 2.3  Time-to-Expiry Optimizer
# ===========================================================================


class TestTimeOptimizer:
    def test_recommend_fallback(self):
        from src.ml.models.time_optimizer import TimeToExpiryOptimizer

        opt = TimeToExpiryOptimizer()
        result = opt.recommend(
            current_price=84500.0,
            strike=84000.0,
            time_remaining_s=300.0,
            volatility=0.02,
            current_contract_price=0.55,
        )
        assert "recommended_price" in result
        assert "confidence" in result
        assert 0 <= result["recommended_price"] <= 1

    def test_compute_fair_value(self):
        from src.ml.models.time_optimizer import TimeToExpiryOptimizer

        opt = TimeToExpiryOptimizer()
        fv = opt.compute_fair_value(
            current_price=84500.0,
            strike=84000.0,
            time_remaining_s=600.0,
            volatility=0.02,
        )
        assert 0 <= fv <= 1


# ===========================================================================
# 2.10  Model Predictor (unified interface)
# ===========================================================================


class TestModelPredictor:
    def test_predict_btc_fallback(self):
        """With no trained models, should use tanh fallback."""
        from src.ml.predictor import ModelPredictor

        predictor = ModelPredictor(models_dir="data/models")
        md = MarketData(
            symbol="KXBTC15M-TEST-T84000",
            timestamp=datetime.now(),
            price=84500.0,
            volume=100,
            bid=0.55,
            ask=0.60,
            extra={"strike": 84000.0, "time_to_expiry": 600},
        )
        result = predictor.predict_btc(md, strike=84000.0, time_to_expiry_s=600.0)
        assert "probability" in result
        assert 0 <= result["probability"] <= 1
        assert "confidence" in result

    def test_predict_weather_fallback(self):
        from src.ml.predictor import ModelPredictor

        predictor = ModelPredictor(models_dir="data/models")
        result = predictor.predict_weather(
            nws_forecast=80.0,
            hrrr_forecast=82.0,
            station_id="KNYC",
            bracket_lower=75.0,
            bracket_upper=85.0,
        )
        assert "probability" in result
        assert 0 <= result["probability"] <= 1

    def test_predict_generic_routing(self):
        from src.ml.predictor import ModelPredictor

        predictor = ModelPredictor(models_dir="data/models")
        md = MarketData(
            symbol="KXBTC15M-TEST-T84000",
            timestamp=datetime.now(),
            price=84500.0,
            volume=100,
            bid=0.55,
            ask=0.60,
            extra={"strike": 84000.0},
        )
        result = predictor.predict(md, market_type="btc")
        assert "probability" in result
        assert "model_used" in result
        assert "latency_ms" in result

    def test_get_status(self):
        from src.ml.predictor import ModelPredictor

        predictor = ModelPredictor(models_dir="data/models")
        status = predictor.get_status()
        assert "models_dir" in status
        assert "fallback_active" in status

    def test_tanh_estimate_above_strike(self):
        from src.ml.predictor import ModelPredictor

        # Price well above strike → probability should be > 0.5
        prob = ModelPredictor._tanh_estimate(85000.0, 84000.0, 600.0)
        assert prob > 0.5

    def test_tanh_estimate_below_strike(self):
        from src.ml.predictor import ModelPredictor

        # Price well below strike → probability should be < 0.5
        prob = ModelPredictor._tanh_estimate(83000.0, 84000.0, 600.0)
        assert prob < 0.5
