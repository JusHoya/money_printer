"""Train ML models from harvested settlement data.

Bridges the gap between harvested Parquet settlement data and the
training pipeline which expects feature DataFrames.

Supports BTC 15m, BTC hourly, and multi-city weather models.

Usage:
    python scripts/train_models.py                    # Train everything
    python scripts/train_models.py --model btc_15m    # BTC 15m only
    python scripts/train_models.py --model btc_hourly # BTC hourly only
    python scripts/train_models.py --model weather     # Weather only
    python scripts/train_models.py --model calibrator  # Calibrator only
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_NON_FEATURE_COLS = ("label", "timestamp", "city")


# ======================================================================
# Data loaders
# ======================================================================


def _parse_strike(ticker: str) -> float:
    """Extract numeric strike from a Kalshi ticker string."""
    try:
        parts = str(ticker).split("-")
        return float(re.sub(r"[A-Za-z]", "", parts[-1]))
    except Exception:
        return np.nan


def _parse_numeric_columns(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """Convert string dollar/volume columns to float."""
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _build_btc_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build training features from a raw BTC settlement DataFrame.

    Works identically for 15m and hourly data — same column structure.
    """
    df["strike"] = df["ticker"].apply(_parse_strike)
    df = _parse_numeric_columns(
        df,
        [
            "yes_bid_dollars",
            "yes_ask_dollars",
            "no_bid_dollars",
            "no_ask_dollars",
            "last_price_dollars",
            "volume_fp",
            "open_interest_fp",
        ],
    )
    df = df.dropna(subset=["strike", "expiration_value"])
    df["expiration_value"] = pd.to_numeric(df["expiration_value"], errors="coerce")

    out = pd.DataFrame()
    out["close"] = df["expiration_value"].astype(float)
    out["strike"] = df["strike"].astype(float)
    out["price_distance"] = out["close"] - out["strike"]
    out["price_distance_pct"] = out["price_distance"] / out["strike"].clip(lower=1)
    out["volume"] = df.get("volume_fp", pd.Series(0)).fillna(0).astype(float)
    out["open_interest"] = (
        df.get("open_interest_fp", pd.Series(0)).fillna(0).astype(float)
    )
    out["bid"] = df.get("yes_bid_dollars", pd.Series(0.5)).fillna(0.5).astype(float)
    out["ask"] = df.get("yes_ask_dollars", pd.Series(0.5)).fillna(0.5).astype(float)
    out["spread"] = (out["ask"] - out["bid"]).clip(lower=0)
    out["mid_price"] = (out["bid"] + out["ask"]) / 2

    if "close_time" in df.columns:
        ct = pd.to_datetime(df["close_time"], errors="coerce", utc=True)
        out["hour"] = ct.dt.hour
        out["minute"] = ct.dt.minute
        out["day_of_week"] = ct.dt.dayofweek
        out["timestamp"] = ct

    out["label"] = (df["result"].str.lower() == "yes").astype(int)
    out = out.dropna(subset=["close", "strike", "label"])
    return out


def load_btc_training_data(market_type: str = "btc_15m") -> pd.DataFrame:
    """Load BTC settlement data from Parquet and build features.

    Parameters
    ----------
    market_type : str
        ``"btc_15m"`` or ``"btc_hourly"``
    """
    from src.data.harvester import HistoricalHarvester

    df = HistoricalHarvester.load_from_parquet(market_type)
    if df.empty:
        logger.error("No Parquet data found in data/historical/%s/", market_type)
        return pd.DataFrame()

    logger.info("Loaded %d %s settlement records", len(df), market_type)
    out = _build_btc_features(df)
    logger.info(
        "Prepared %d %s training rows with %d features",
        len(out),
        market_type,
        len([c for c in out.columns if c not in _NON_FEATURE_COLS]),
    )
    return out


def load_weather_training_data() -> pd.DataFrame:
    """Load ALL weather city data from Parquet and build features.

    Scans ``data/historical/`` for directories starting with ``weather``
    and concatenates all Parquet files with a ``city`` column.
    """
    from src.data.harvester import HistoricalHarvester

    historical_dir = Path("data/historical")
    frames = []

    # Find all weather directories (weather/, weather_KXHIGHCHI/, etc.)
    for d in sorted(historical_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("weather"):
            continue
        df = HistoricalHarvester.load_from_parquet(d.name)
        if not df.empty:
            logger.info("  %s: %d records", d.name, len(df))
            frames.append(df)

    if not frames:
        logger.error("No weather Parquet data found")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    logger.info(
        "Combined %d weather settlement records from %d sources", len(df), len(frames)
    )

    # Build features
    df["strike"] = df["ticker"].apply(_parse_strike)
    df = _parse_numeric_columns(
        df, ["yes_bid_dollars", "yes_ask_dollars", "volume_fp", "expiration_value"]
    )
    df = df.dropna(subset=["strike"])

    out = pd.DataFrame()
    out["strike"] = df["strike"].astype(float)
    out["bid"] = df.get("yes_bid_dollars", pd.Series(0.5)).fillna(0.5).astype(float)
    out["ask"] = df.get("yes_ask_dollars", pd.Series(0.5)).fillna(0.5).astype(float)
    out["spread"] = (out["ask"] - out["bid"]).clip(lower=0)
    out["volume"] = df.get("volume_fp", pd.Series(0)).fillna(0).astype(float)
    out["label"] = (df["result"].str.lower() == "yes").astype(int)

    # City extraction from ticker
    city_map = {
        "NY": "NY",
        "LAX": "LA",
        "LA": "LA",
        "CHI": "CH",
        "CH": "CH",
        "MIA": "MI",
        "MI": "MI",
        "DFW": "DFW",
    }

    def _extract_city(ticker):
        t = str(ticker).upper()
        for key, val in city_map.items():
            if key in t:
                return val
        return "OTHER"

    out["city"] = df["ticker"].apply(_extract_city)

    # Encode city as numeric for XGBoost
    city_codes = {"NY": 0, "LA": 1, "CH": 2, "MI": 3, "DFW": 4, "OTHER": 5}
    out["city_code"] = out["city"].map(city_codes).fillna(5).astype(int)

    if "close_time" in df.columns:
        ct = pd.to_datetime(df["close_time"], errors="coerce", utc=True)
        out["hour"] = ct.dt.hour
        out["day_of_week"] = ct.dt.dayofweek
        out["month"] = ct.dt.month
        out["timestamp"] = ct

    out = out.dropna(subset=["strike", "label"])
    logger.info(
        "Prepared %d weather training rows (%s)",
        len(out),
        out["city"].value_counts().to_dict(),
    )
    return out


# ======================================================================
# Training functions
# ======================================================================


def walk_forward_split(X, y, train_pct=0.70, val_pct=0.15):
    """Temporal walk-forward split (no lookahead bias).

    Returns (X_train, y_train, X_val, y_val, X_test, y_test).
    """
    n = len(X)
    train_end = int(n * train_pct)
    val_end = int(n * (train_pct + val_pct))

    if hasattr(X, "iloc"):
        return (
            X.iloc[:train_end],
            y[:train_end],
            X.iloc[train_end:val_end],
            y[train_end:val_end],
            X.iloc[val_end:],
            y[val_end:],
        )
    return (
        X[:train_end],
        y[:train_end],
        X[train_end:val_end],
        y[train_end:val_end],
        X[val_end:],
        y[val_end:],
    )


def train_xgboost(data: pd.DataFrame, save_path: str, label: str = "") -> dict:
    """Train XGBoost on any settlement feature DataFrame."""
    from sklearn.metrics import accuracy_score, roc_auc_score

    from src.ml.models.btc_xgboost import BTCXGBoostClassifier

    feat_cols = [c for c in data.columns if c not in _NON_FEATURE_COLS]
    X = data[feat_cols].fillna(0)
    y = data["label"].values

    X_train, y_train, X_val, y_val, X_test, y_test = walk_forward_split(X, y)

    model = BTCXGBoostClassifier()
    metrics = model.train(X_train, y_train, val_X=X_val, val_y=y_val)

    test_probs = model.predict_proba(X_test)
    test_auc = roc_auc_score(y_test, test_probs)
    test_acc = accuracy_score(y_test, (test_probs > 0.5).astype(int))
    metrics["test_auc"] = round(test_auc, 4)
    metrics["test_accuracy"] = round(test_acc, 4)
    metrics["test_size"] = len(y_test)

    model.save(save_path)
    logger.info(
        "%s XGBoost saved to %s. Test AUC=%.4f Accuracy=%.4f",
        label,
        save_path,
        test_auc,
        test_acc,
    )
    return metrics


def train_lstm(
    data: pd.DataFrame,
    save_path: str,
    label: str = "",
    seq_len: int = 20,
    epochs: int = 20,
) -> dict:
    """Train LSTM on any settlement feature DataFrame."""
    from src.ml.models.btc_lstm import BTCLSTMPredictor

    feat_cols = [c for c in data.columns if c not in _NON_FEATURE_COLS]
    X = data[feat_cols].fillna(0)
    y = data["label"].values.astype(np.float32)

    model = BTCLSTMPredictor(
        input_size=len(feat_cols),
        hidden_size=32,
        num_layers=1,
        sequence_length=seq_len,
    )

    X_seq, _ = model.prepare_sequences(X, feat_cols)
    y_seq = y[seq_len : seq_len + len(X_seq)]

    X_train, y_train, X_val, y_val, X_test, y_test = walk_forward_split(X_seq, y_seq)

    metrics = model.train_model(
        X_train,
        y_train,
        val_X=X_val,
        val_y=y_val,
        epochs=epochs,
        batch_size=64,
    )

    model.save(save_path)
    logger.info("%s LSTM saved to %s. Metrics: %s", label, save_path, metrics)
    return metrics


def train_calibrator(data: pd.DataFrame, xgb_path: str, save_path: str) -> dict:
    """Train probability calibrator on XGBoost outputs."""
    from src.ml.calibration import ProbabilityCalibrator
    from src.ml.models.btc_xgboost import BTCXGBoostClassifier

    if not os.path.exists(xgb_path):
        return {"error": f"XGBoost model not found at {xgb_path}"}

    xgb = BTCXGBoostClassifier(model_path=xgb_path)
    feat_cols = [c for c in data.columns if c not in _NON_FEATURE_COLS]
    X = data[feat_cols].fillna(0)
    y = data["label"].values

    # Use last 30% for calibration
    cal_start = int(len(X) * 0.70)
    raw_probs = xgb.predict_proba(X.iloc[cal_start:])
    outcomes = y[cal_start:]

    cal = ProbabilityCalibrator(method="platt")
    cal.fit(raw_probs, outcomes)
    ece = cal.calibration_error(cal.calibrate(raw_probs), outcomes)

    cal.save(save_path)
    logger.info("Calibrator saved to %s. ECE=%.4f", save_path, ece)
    return {"ece": round(ece, 4), "samples": len(outcomes)}


# ======================================================================
# Main
# ======================================================================


ALL_MODELS = [
    "btc_15m",
    "btc_hourly",
    "weather",
    "calibrator",
]


def main():
    parser = argparse.ArgumentParser(description="Train Money Printer ML models")
    parser.add_argument(
        "--model",
        default="all",
        choices=["all"] + ALL_MODELS,
        help="Model group to train (default: all)",
    )
    args = parser.parse_args()

    os.makedirs("data/models", exist_ok=True)
    results = {}

    # --- BTC 15m ---
    if args.model in ("all", "btc_15m"):
        data_15m = load_btc_training_data("btc_15m")
        if not data_15m.empty:
            logger.info("=== Training BTC 15m XGBoost ===")
            results["btc_15m_xgboost"] = train_xgboost(
                data_15m, "data/models/btc_xgboost_latest.joblib", "BTC 15m"
            )
            logger.info("=== Training BTC 15m LSTM ===")
            results["btc_15m_lstm"] = train_lstm(
                data_15m, "data/models/btc_lstm_latest.joblib", "BTC 15m"
            )
        else:
            results["btc_15m"] = {"error": "No data"}

    # --- BTC Hourly ---
    if args.model in ("all", "btc_hourly"):
        data_hr = load_btc_training_data("btc_hourly")
        if not data_hr.empty:
            logger.info("=== Training BTC Hourly XGBoost ===")
            results["btc_hourly_xgboost"] = train_xgboost(
                data_hr, "data/models/btc_hourly_xgboost_latest.joblib", "BTC Hourly"
            )
            logger.info("=== Training BTC Hourly LSTM ===")
            results["btc_hourly_lstm"] = train_lstm(
                data_hr,
                "data/models/btc_hourly_lstm_latest.joblib",
                "BTC Hourly",
                seq_len=30,  # Longer sequences for hourly data
            )
        else:
            results["btc_hourly"] = {"error": "No data"}

    # --- Weather (multi-city) ---
    if args.model in ("all", "weather"):
        data_wx = load_weather_training_data()
        if not data_wx.empty:
            logger.info("=== Training Weather XGBoost (multi-city) ===")
            results["weather_xgboost"] = train_xgboost(
                data_wx, "data/models/weather_xgboost_latest.joblib", "Weather"
            )
        else:
            results["weather"] = {"error": "No data"}

    # --- Calibrator (uses BTC 15m XGBoost) ---
    if args.model in ("all", "calibrator"):
        data_15m = load_btc_training_data("btc_15m")
        if not data_15m.empty:
            logger.info("=== Training Calibrator ===")
            results["calibrator"] = train_calibrator(
                data_15m,
                "data/models/btc_xgboost_latest.joblib",
                "data/models/calibrator_latest.joblib",
            )
        else:
            results["calibrator"] = {"error": "No BTC 15m data for calibration"}

    print("\n=== TRAINING RESULTS ===")
    for name, res in results.items():
        print(f"  {name}: {res}")


if __name__ == "__main__":
    main()
