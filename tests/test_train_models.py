"""Tests for the training script (scripts/train_models.py).

Validates data loaders, feature engineering, walk-forward splitting,
and end-to-end training for BTC 15m, BTC hourly, and weather models.

Uses synthetic Parquet data to avoid dependency on live harvester runs.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fixtures: synthetic Parquet data that mimics harvester output
# ---------------------------------------------------------------------------


def _fake_btc_settlements(n=200, seed=42) -> pd.DataFrame:
    """Create a DataFrame matching the harvester's BTC Parquet schema."""
    rng = np.random.RandomState(seed)
    strikes = rng.choice([83000, 83500, 84000, 84500, 85000], n)
    spot = strikes + rng.randn(n) * 500
    results = np.where(spot >= strikes, "yes", "no")
    times = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")

    return pd.DataFrame(
        {
            "ticker": [f"KXBTC15M-26JAN01-T{s}" for s in strikes],
            "expiration_value": spot,
            "floor_strike": strikes.astype(float),
            "result": results,
            "yes_bid_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "yes_ask_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "no_bid_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "no_ask_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "last_price_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "volume_fp": rng.randint(10, 1000, n).astype(str),
            "open_interest_fp": rng.randint(0, 500, n).astype(str),
            "close_time": times.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "settled",
        }
    )


def _fake_weather_settlements(n=150, city_ticker="KXHIGHNY", seed=43) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    strikes = rng.choice([70, 75, 80, 85, 90], n)
    results = np.where(rng.rand(n) > 0.5, "yes", "no")
    times = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")

    return pd.DataFrame(
        {
            "ticker": [f"{city_ticker}-26JAN01-T{s}" for s in strikes],
            "result": results,
            "yes_bid_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "yes_ask_dollars": rng.uniform(0.01, 0.99, n).round(4).astype(str),
            "volume_fp": rng.randint(1, 200, n).astype(str),
            "close_time": times.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "settled",
        }
    )


@pytest.fixture()
def fake_data_dir(tmp_path, monkeypatch):
    """Write synthetic Parquet files and point the harvester at them."""
    import src.data.harvester as harvester_mod

    # BTC 15m
    btc_15m_dir = tmp_path / "btc_15m"
    btc_15m_dir.mkdir()
    _fake_btc_settlements(200, seed=42).to_parquet(btc_15m_dir / "2026-03-19.parquet")

    # BTC hourly (same schema, different data)
    btc_hr_dir = tmp_path / "btc_hourly"
    btc_hr_dir.mkdir()
    _fake_btc_settlements(300, seed=99).to_parquet(btc_hr_dir / "2026-03-19.parquet")

    # Weather — multiple cities
    for city, seed in [
        ("KXHIGHNY", 43),
        ("KXHIGHCHI", 44),
        ("KXHIGHLAX", 45),
        ("KXHIGHMIA", 46),
    ]:
        wx_dir = tmp_path / f"weather_{city}"
        wx_dir.mkdir()
        _fake_weather_settlements(100, city, seed).to_parquet(
            wx_dir / "2026-03-19.parquet"
        )

    # Also a combined "weather" dir (like the original NY-only harvest)
    wx_combined = tmp_path / "weather"
    wx_combined.mkdir()
    _fake_weather_settlements(80, "KXHIGHNY", 47).to_parquet(
        wx_combined / "2026-03-19.parquet"
    )

    monkeypatch.setattr(harvester_mod, "_HISTORICAL_DIR", tmp_path)

    # Also patch the train_models module's path resolution
    import scripts.train_models as tm  # noqa: F841

    def patched_load_weather():
        """Override historical_dir for weather scan."""
        from src.data.harvester import HistoricalHarvester

        frames = []
        for d in sorted(tmp_path.iterdir()):
            if not d.is_dir() or not d.name.startswith("weather"):
                continue
            df = HistoricalHarvester.load_from_parquet(d.name)
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        return tm._build_weather_features(df)

    return tmp_path


# ===========================================================================
# Data loader tests
# ===========================================================================


class TestLoadBtcTrainingData:
    def test_loads_btc_15m(self, fake_data_dir, monkeypatch):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")
        assert not data.empty
        assert "label" in data.columns
        assert "close" in data.columns
        assert "strike" in data.columns
        assert "price_distance" in data.columns

    def test_loads_btc_hourly(self, fake_data_dir, monkeypatch):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_hourly")
        assert not data.empty
        assert len(data) > 200  # We seeded 300 rows

    def test_btc_features_have_time_columns(self, fake_data_dir):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")
        assert "hour" in data.columns
        assert "day_of_week" in data.columns

    def test_btc_labels_are_binary(self, fake_data_dir):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")
        assert set(data["label"].unique()).issubset({0, 1})

    def test_missing_market_type_returns_empty(self, fake_data_dir):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("nonexistent_market")
        assert data.empty


class TestLoadWeatherTrainingData:
    def test_loads_multi_city_data(self, fake_data_dir, monkeypatch):
        import scripts.train_models as tm

        # Patch Path reference used inside load_weather_training_data
        monkeypatch.setattr(
            tm, "Path", lambda x: fake_data_dir if x == "data/historical" else Path(x)
        )
        data = tm.load_weather_training_data()
        assert not data.empty
        # Should have data from multiple cities
        cities = data["city"].unique()
        assert len(cities) >= 2

    def test_weather_has_city_code(self, fake_data_dir, monkeypatch):
        import scripts.train_models as tm

        monkeypatch.setattr(
            tm, "Path", lambda x: fake_data_dir if x == "data/historical" else Path(x)
        )
        data = tm.load_weather_training_data()
        assert "city_code" in data.columns
        assert data["city_code"].dtype in (np.int32, np.int64, int)

    def test_weather_labels_are_binary(self, fake_data_dir, monkeypatch):
        import scripts.train_models as tm

        monkeypatch.setattr(
            tm, "Path", lambda x: fake_data_dir if x == "data/historical" else Path(x)
        )
        data = tm.load_weather_training_data()
        assert set(data["label"].unique()).issubset({0, 1})


# ===========================================================================
# Walk-forward split
# ===========================================================================


class TestWalkForwardSplit:
    def test_split_sizes(self):
        import scripts.train_models as tm

        X = pd.DataFrame({"a": range(100)})
        y = np.zeros(100)
        X_tr, y_tr, X_val, y_val, X_te, y_te = tm.walk_forward_split(X, y)
        assert len(X_tr) == 70
        assert len(X_val) == 15
        assert len(X_te) == 15

    def test_split_no_overlap(self):
        import scripts.train_models as tm

        X = pd.DataFrame({"a": range(100)})
        y = np.arange(100)
        X_tr, y_tr, X_val, y_val, X_te, y_te = tm.walk_forward_split(X, y)
        # Last train < first val < last val < first test
        assert y_tr[-1] < y_val[0]
        assert y_val[-1] < y_te[0]

    def test_split_works_with_numpy(self):
        import scripts.train_models as tm

        X = np.random.randn(50, 3)
        y = np.random.randint(0, 2, 50)
        X_tr, y_tr, X_val, y_val, X_te, y_te = tm.walk_forward_split(X, y)
        assert len(X_tr) + len(X_val) + len(X_te) == 50


# ===========================================================================
# Feature engineering
# ===========================================================================


class TestBuildBtcFeatures:
    def test_output_columns(self):
        import scripts.train_models as tm

        raw = _fake_btc_settlements(50)
        out = tm._build_btc_features(raw)
        expected = {
            "close",
            "strike",
            "price_distance",
            "price_distance_pct",
            "volume",
            "open_interest",
            "bid",
            "ask",
            "spread",
            "mid_price",
            "label",
        }
        assert expected.issubset(set(out.columns))

    def test_price_distance_calculation(self):
        import scripts.train_models as tm

        raw = _fake_btc_settlements(20)
        out = tm._build_btc_features(raw)
        # price_distance = close - strike
        np.testing.assert_allclose(
            out["price_distance"].values,
            (out["close"] - out["strike"]).values,
            atol=1e-6,
        )

    def test_spread_non_negative(self):
        import scripts.train_models as tm

        raw = _fake_btc_settlements(50)
        out = tm._build_btc_features(raw)
        assert (out["spread"] >= 0).all()


# ===========================================================================
# End-to-end training (on synthetic data)
# ===========================================================================


class TestTrainXGBoost:
    def test_train_btc_15m_xgboost(self, fake_data_dir, tmp_path):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")
        save_path = str(tmp_path / "xgb_15m.joblib")
        metrics = tm.train_xgboost(data, save_path, "BTC 15m")
        assert os.path.exists(save_path)
        assert "test_auc" in metrics
        assert "test_accuracy" in metrics
        assert 0 <= metrics["test_auc"] <= 1

    def test_train_btc_hourly_xgboost(self, fake_data_dir, tmp_path):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_hourly")
        save_path = str(tmp_path / "xgb_hr.joblib")
        metrics = tm.train_xgboost(data, save_path, "BTC Hourly")
        assert os.path.exists(save_path)
        assert "test_auc" in metrics

    def test_train_weather_xgboost(self, fake_data_dir, tmp_path, monkeypatch):
        import scripts.train_models as tm

        monkeypatch.setattr(
            tm, "Path", lambda x: fake_data_dir if x == "data/historical" else Path(x)
        )
        data = tm.load_weather_training_data()
        save_path = str(tmp_path / "xgb_wx.joblib")
        metrics = tm.train_xgboost(data, save_path, "Weather")
        assert os.path.exists(save_path)
        assert "test_auc" in metrics


class TestTrainLSTM:
    def test_train_btc_15m_lstm(self, fake_data_dir, tmp_path):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")
        save_path = str(tmp_path / "lstm_15m.joblib")
        metrics = tm.train_lstm(data, save_path, "BTC 15m", seq_len=10, epochs=2)
        assert os.path.exists(save_path)
        assert "best_val_loss" in metrics or "final_train_loss" in metrics

    def test_train_btc_hourly_lstm(self, fake_data_dir, tmp_path):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_hourly")
        save_path = str(tmp_path / "lstm_hr.joblib")
        result = tm.train_lstm(data, save_path, "BTC Hourly", seq_len=10, epochs=2)
        assert os.path.exists(save_path)
        assert result is not None


class TestTrainCalibrator:
    def test_train_calibrator(self, fake_data_dir, tmp_path):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")

        # First train XGBoost (calibrator depends on it)
        xgb_path = str(tmp_path / "xgb_for_cal.joblib")
        tm.train_xgboost(data, xgb_path, "test")

        cal_path = str(tmp_path / "cal.joblib")
        result = tm.train_calibrator(data, xgb_path, cal_path)
        assert "ece" in result
        assert os.path.exists(cal_path)

    def test_calibrator_fails_without_xgboost(self, fake_data_dir, tmp_path):
        import scripts.train_models as tm

        data = tm.load_btc_training_data("btc_15m")
        result = tm.train_calibrator(
            data, "/nonexistent/xgb.joblib", str(tmp_path / "c.joblib")
        )
        assert "error" in result


# ===========================================================================
# CLI argument validation
# ===========================================================================


class TestCLIChoices:
    def test_all_models_listed(self):
        import scripts.train_models as tm

        assert "btc_15m" in tm.ALL_MODELS
        assert "btc_hourly" in tm.ALL_MODELS
        assert "weather" in tm.ALL_MODELS
        assert "calibrator" in tm.ALL_MODELS
