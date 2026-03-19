"""Train ML models from harvested settlement data.

Bridges the gap between harvested Parquet settlement data and the
training pipeline which expects feature DataFrames.

Usage:
    python scripts/train_models.py
    python scripts/train_models.py --model btc_xgboost
    python scripts/train_models.py --model all
"""

import argparse
import logging
import os
import re
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_btc_training_data() -> pd.DataFrame:
    """Convert BTC settlement Parquet into a training DataFrame.

    Each row is a settled 15-min contract. We extract features that
    are available from the settlement snapshot and generate binary
    labels from the ``result`` column.
    """
    from src.data.harvester import HistoricalHarvester

    df = HistoricalHarvester.load_from_parquet("btc_15m")
    if df.empty:
        logger.error("No BTC Parquet data found in data/historical/btc_15m/")
        return pd.DataFrame()

    logger.info("Loaded %d BTC settlement records", len(df))

    # Extract strike from ticker
    def parse_strike(ticker):
        try:
            parts = str(ticker).split("-")
            return float(re.sub(r"[A-Za-z]", "", parts[-1]))
        except Exception:
            return np.nan

    df["strike"] = df["ticker"].apply(parse_strike)

    # Parse numeric fields
    for col in [
        "yes_bid_dollars",
        "yes_ask_dollars",
        "no_bid_dollars",
        "no_ask_dollars",
        "last_price_dollars",
        "volume_fp",
        "open_interest_fp",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows without usable data
    df = df.dropna(subset=["strike", "expiration_value"])
    df["expiration_value"] = pd.to_numeric(df["expiration_value"], errors="coerce")

    # Feature engineering from settlement data
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

    # Time features from close_time
    if "close_time" in df.columns:
        ct = pd.to_datetime(df["close_time"], errors="coerce", utc=True)
        out["hour"] = ct.dt.hour
        out["minute"] = ct.dt.minute
        out["day_of_week"] = ct.dt.dayofweek
        out["timestamp"] = ct

    # Label: did YES win?
    out["label"] = (df["result"].str.lower() == "yes").astype(int)

    out = out.dropna(subset=["close", "strike", "label"])
    logger.info(
        "Prepared %d BTC training rows with %d features", len(out), len(out.columns) - 1
    )
    return out


def load_weather_training_data() -> pd.DataFrame:
    """Convert weather settlement Parquet into a training DataFrame."""
    from src.data.harvester import HistoricalHarvester

    df = HistoricalHarvester.load_from_parquet("weather")
    if df.empty:
        logger.error("No weather Parquet data found")
        return pd.DataFrame()

    logger.info("Loaded %d weather settlement records", len(df))

    def parse_strike(ticker):
        try:
            parts = str(ticker).split("-")
            return float(re.sub(r"[A-Za-z]", "", parts[-1]))
        except Exception:
            return np.nan

    df["strike"] = df["ticker"].apply(parse_strike)
    for col in ["yes_bid_dollars", "yes_ask_dollars", "volume_fp", "expiration_value"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["strike"])

    out = pd.DataFrame()
    out["strike"] = df["strike"].astype(float)
    out["bid"] = df.get("yes_bid_dollars", pd.Series(0.5)).fillna(0.5).astype(float)
    out["ask"] = df.get("yes_ask_dollars", pd.Series(0.5)).fillna(0.5).astype(float)
    out["volume"] = df.get("volume_fp", pd.Series(0)).fillna(0).astype(float)
    out["label"] = (df["result"].str.lower() == "yes").astype(int)

    # Extract city from ticker
    out["city"] = df["ticker"].apply(
        lambda t: "NY"
        if "NY" in str(t)
        else "LA"
        if "LA" in str(t)
        else "CH"
        if "CH" in str(t)
        else "MI"
        if "MI" in str(t)
        else "OTHER"
    )

    out = out.dropna(subset=["strike", "label"])
    logger.info("Prepared %d weather training rows", len(out))
    return out


def train_btc_xgboost(data: pd.DataFrame) -> dict:
    """Train BTC XGBoost on settlement features."""
    from src.ml.models.btc_xgboost import BTCXGBoostClassifier

    # Use available numeric features
    feat_cols = [c for c in data.columns if c not in ("label", "timestamp", "city")]
    X = data[feat_cols].fillna(0)
    y = data["label"].values

    # Walk-forward split (70/15/15 by time)
    n = len(X)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)

    X_train, y_train = X.iloc[:train_end], y[:train_end]
    X_val, y_val = X.iloc[train_end:val_end], y[train_end:val_end]
    X_test, y_test = X.iloc[val_end:], y[val_end:]

    model = BTCXGBoostClassifier()
    metrics = model.train(X_train, y_train, val_X=X_val, val_y=y_val)

    # Test evaluation
    test_probs = model.predict_proba(X_test)
    from sklearn.metrics import roc_auc_score, accuracy_score

    test_auc = roc_auc_score(y_test, test_probs)
    test_acc = accuracy_score(y_test, (test_probs > 0.5).astype(int))
    metrics["test_auc"] = round(test_auc, 4)
    metrics["test_accuracy"] = round(test_acc, 4)

    # Save
    model.save("data/models/btc_xgboost_latest.joblib")
    logger.info("XGBoost saved. Test AUC=%.4f Accuracy=%.4f", test_auc, test_acc)
    return metrics


def train_btc_lstm(data: pd.DataFrame) -> dict:
    """Train BTC LSTM on settlement features."""
    from src.ml.models.btc_lstm import BTCLSTMPredictor

    feat_cols = [c for c in data.columns if c not in ("label", "timestamp", "city")]
    X = data[feat_cols].fillna(0)
    y = data["label"].values.astype(np.float32)

    seq_len = 20
    model = BTCLSTMPredictor(
        input_size=len(feat_cols), hidden_size=32, num_layers=1, sequence_length=seq_len
    )

    # Prepare sequences
    X_seq, _ = model.prepare_sequences(X, feat_cols)
    y_seq = y[seq_len : seq_len + len(X_seq)]

    # Walk-forward split
    n = len(X_seq)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)

    metrics = model.train_model(
        X_seq[:train_end],
        y_seq[:train_end],
        val_X=X_seq[train_end:val_end],
        val_y=y_seq[train_end:val_end],
        epochs=20,
        batch_size=64,
    )

    model.save("data/models/btc_lstm_latest.joblib")
    logger.info("LSTM saved. Metrics: %s", metrics)
    return metrics


def train_calibrator(data: pd.DataFrame) -> dict:
    """Train probability calibrator on XGBoost outputs."""
    from src.ml.models.btc_xgboost import BTCXGBoostClassifier
    from src.ml.calibration import ProbabilityCalibrator

    model_path = "data/models/btc_xgboost_latest.joblib"
    if not os.path.exists(model_path):
        return {"error": "XGBoost model not found — train it first"}

    xgb = BTCXGBoostClassifier(model_path=model_path)
    feat_cols = [c for c in data.columns if c not in ("label", "timestamp", "city")]
    X = data[feat_cols].fillna(0)
    y = data["label"].values

    # Use last 30% for calibration
    n = len(X)
    cal_start = int(n * 0.70)
    raw_probs = xgb.predict_proba(X.iloc[cal_start:])
    outcomes = y[cal_start:]

    cal = ProbabilityCalibrator(method="platt")
    cal.fit(raw_probs, outcomes)
    ece = cal.calibration_error(cal.calibrate(raw_probs), outcomes)

    cal.save("data/models/calibrator_latest.joblib")
    logger.info("Calibrator saved. ECE=%.4f", ece)
    return {"ece": round(ece, 4)}


def main():
    parser = argparse.ArgumentParser(description="Train Money Printer ML models")
    parser.add_argument(
        "--model",
        default="all",
        help="Model to train: all, btc_xgboost, btc_lstm, calibrator",
    )
    args = parser.parse_args()

    os.makedirs("data/models", exist_ok=True)

    btc_data = load_btc_training_data()
    if btc_data.empty:
        logger.error("No training data — run the harvester first")
        return

    results = {}

    if args.model in ("all", "btc_xgboost"):
        logger.info("=== Training BTC XGBoost ===")
        results["btc_xgboost"] = train_btc_xgboost(btc_data)

    if args.model in ("all", "btc_lstm"):
        logger.info("=== Training BTC LSTM ===")
        results["btc_lstm"] = train_btc_lstm(btc_data)

    if args.model in ("all", "calibrator"):
        logger.info("=== Training Calibrator ===")
        results["calibrator"] = train_calibrator(btc_data)

    print("\n=== TRAINING RESULTS ===")
    for name, res in results.items():
        print(f"  {name}: {res}")


if __name__ == "__main__":
    main()
