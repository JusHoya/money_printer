"""Tests for the shared BTC 15m feature builder.

Guards against train/inference feature-set drift.  The 16 features defined
here MUST match what ``btc_xgboost_latest.joblib`` was trained on — those
names live in ``data/models/btc_xgboost_feature_meta.json``.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.interfaces import MarketData
from src.ml.btc_features import BTC_FEATURE_NAMES, build_btc_sample_features


META_PATH = Path("data/models/btc_xgboost_feature_meta.json")


def test_feature_names_match_trained_model_metadata():
    """The 16 feature names must match the saved model's metadata file.

    This is the canonical anti-drift invariant: if anyone edits
    BTC_FEATURE_NAMES without retraining, this test fails.
    """
    if not META_PATH.exists():
        pytest.skip("feature meta file not present; skipping")
    meta = json.loads(META_PATH.read_text())
    expected = meta.get("feature_names") or []
    assert list(BTC_FEATURE_NAMES) == list(expected)


def test_build_returns_all_16_features():
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.55,
        tte_s=600.0,
        hour_of_day=14,
        minute_of_hour=17,
    )
    assert set(feats.keys()) == set(BTC_FEATURE_NAMES)
    assert len(feats) == 16


def test_core_distance_features():
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.55,
        tte_s=900.0,
        hour_of_day=10,
        minute_of_hour=5,
    )
    assert feats["feat_price_distance"] == pytest.approx(500.0)
    assert feats["feat_price_distance_pct"] == pytest.approx(500.0 / 84500.0)
    assert feats["feat_contract_price"] == 0.55
    assert feats["feat_time_to_expiry"] == 900.0
    assert feats["feat_normalized_tte"] == pytest.approx(1.0)
    assert feats["feat_hour_of_day"] == 10.0
    assert feats["feat_minute_in_interval"] == 5.0


def test_tanh_analytical_prob_matches_training_formula():
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.55,
        tte_s=900.0,
        hour_of_day=10,
        minute_of_hour=5,
    )
    # Training code: scale = max(0.3, tte/900) * 1000 ; tanh((spot-strike)/scale)
    expected = 0.5 + 0.5 * math.tanh(500.0 / 1000.0)
    assert feats["feat_tanh_prob"] == pytest.approx(expected)
    assert feats["feat_market_vs_analytical"] == pytest.approx(0.55 - expected)


def test_momentum_and_vol_zero_without_history():
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.55,
        tte_s=600.0,
        hour_of_day=14,
        minute_of_hour=17,
    )
    for name in (
        "feat_btc_return_1m",
        "feat_btc_return_5m",
        "feat_btc_return_10m",
        "feat_btc_vol_1m",
        "feat_btc_vol_5m",
        "feat_btc_range",
        "feat_contract_slope",
    ):
        assert feats[name] == 0.0


def test_momentum_features_with_history():
    """With spot history, returns and range should be non-zero and signed correctly."""
    now_ts = 1700_000_000.0
    # Rising prices over last 10 minutes
    spot_history = [(now_ts - 60 * m, 84000.0 + (10 - m) * 100.0) for m in range(11)]
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.55,
        tte_s=600.0,
        hour_of_day=14,
        minute_of_hour=17,
        spot_history=spot_history,
        now_ts=now_ts,
    )
    # Prices rose from 84000 to 85000 → positive 10m return, non-zero range
    assert feats["feat_btc_return_10m"] > 0
    assert feats["feat_btc_range"] > 0
    assert feats["feat_btc_return_5m"] > 0


def test_contract_slope_with_history():
    now_ts = 1700_000_000.0
    # Rising contract prices over last 2 minutes — oldest first (ts ascending).
    # Price rises from 0.40 to 0.62 over 12 samples spaced 10s apart.
    contract_history = [(now_ts - 10 * (11 - i), 0.40 + 0.02 * i) for i in range(12)]
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.60,
        tte_s=600.0,
        hour_of_day=14,
        minute_of_hour=17,
        contract_history=contract_history,
        now_ts=now_ts,
    )
    # slope should be positive (ascending values after time-sort)
    assert feats["feat_contract_slope"] > 0


def test_contract_slope_negative_with_falling_prices():
    now_ts = 1700_000_000.0
    # Falling contract prices over last 2 minutes — oldest first.
    contract_history = [(now_ts - 10 * (11 - i), 0.60 - 0.02 * i) for i in range(12)]
    feats = build_btc_sample_features(
        spot=85000.0,
        strike=84500.0,
        contract_price=0.38,
        tte_s=600.0,
        hour_of_day=14,
        minute_of_hour=17,
        contract_history=contract_history,
        now_ts=now_ts,
    )
    assert feats["feat_contract_slope"] < 0


def test_predict_btc_uses_xgboost_when_model_available():
    """Regression test for train/live feature mismatch.

    Before the fix: ``predict_btc`` fed RSI/MACD/Bollinger features (from
    ``FeaturePipeline``) to a model trained on price-distance/tanh features,
    which always triggered the bare ``except`` and fell through to the
    analytical tanh fallback.  This test guards that the model is actually
    invoked when available.
    """
    try:
        from src.ml.predictor import ModelPredictor
    except Exception:
        pytest.skip("predictor import failed")

    p = ModelPredictor(models_dir="data/models")
    if p._xgboost is None:
        pytest.skip("no trained xgboost model available")

    now = datetime.now(tz=timezone.utc)
    md = MarketData(
        symbol="KXBTC15M-26APR080000-T68500",
        timestamp=now,
        price=68420.0,
        volume=100,
        bid=0.54,
        ask=0.56,
        extra={
            "strike": 68500.0,
            "spot_price": 68420.0,
            "close_time": now + timedelta(minutes=10),
        },
    )
    res = p.predict_btc(md, strike=68500.0, time_to_expiry_s=600.0)

    assert 0.0 <= res["probability"] <= 1.0
    # Key assertion: the model was actually invoked, not silently bypassed.
    assert res.get("model_used", "").startswith(("xgboost", "ensemble", "lstm"))
    assert res["model_used"] != "analytical_fallback"
