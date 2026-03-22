"""
Train BTC 15-minute contract model from live dashboard CSV data.

Usage:
    $env:PYTHONPATH = "."; python scripts/train_from_csv.py
    $env:PYTHONPATH = "."; python scripts/train_from_csv.py --data-dir logs/_archive/NewTrainData
    $env:PYTHONPATH = "."; python scripts/train_from_csv.py --dry-run
"""

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score
from xgboost import XGBClassifier

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.ml.calibration import ProbabilityCalibrator

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("train_csv")

# --------------------------------------------------------------------------- #
# 1. Parse strikes from log files
# --------------------------------------------------------------------------- #

_EVAL_RE = re.compile(r"Evaluating (KXBTC15M-\S+)\s*\|.*?strike=([\d.]+)")


def extract_strikes_from_logs(log_paths: List[str]) -> Dict[str, float]:
    """Parse 'Evaluating KXBTC15M-... | strike=X' from money_printer logs."""
    strikes: Dict[str, float] = {}
    for path in log_paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _EVAL_RE.search(line)
                if m:
                    symbol = m.group(1)
                    strike = float(m.group(2))
                    if symbol not in strikes:
                        strikes[symbol] = strike
    log.info("Parsed %d unique strikes from %d log files", len(strikes), len(log_paths))
    return strikes


# --------------------------------------------------------------------------- #
# 2. Parse expiry from ticker symbol
# --------------------------------------------------------------------------- #

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def parse_expiry(symbol: str) -> Optional[datetime]:
    """Parse 'KXBTC15M-26MAR201715-15' → datetime(2026, 3, 20, 17, 15)."""
    # Strip display suffix like ' (15m)'
    clean = symbol.split(" ")[0]
    parts = clean.split("-")
    if len(parts) < 2:
        return None
    try:
        date_time = parts[1]  # e.g. '26MAR201715'
        yy = int(date_time[:2])
        mon_str = date_time[2:5]
        dd = int(date_time[5:7])
        hh = int(date_time[7:9])
        mm = int(date_time[9:11])
        month = _MONTHS.get(mon_str.upper())
        if month is None:
            return None
        return datetime(2000 + yy, month, dd, hh, mm, 0)
    except (ValueError, IndexError):
        return None


def canon_symbol(symbol: str) -> str:
    """Strip display suffix: 'KXBTC15M-26MAR201715-15 (15m)' → 'KXBTC15M-26MAR201715-15'."""
    return symbol.split(" ")[0]


# --------------------------------------------------------------------------- #
# 3. Load CSV data
# --------------------------------------------------------------------------- #


def load_session(
    data_csv: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load one data CSV; return (btc_df, contract_df)."""
    rows = []
    with open(data_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r["Type"] == "MARKET_DATA":
                rows.append(r)

    btc_rows = []
    contract_rows = []
    for r in rows:
        ts = pd.Timestamp(r["Timestamp"])
        sym = r["Symbol"]
        price = float(r["Price"])
        if "BTC-USD" in sym and "Coinbase" in sym:
            btc_rows.append({"timestamp": ts, "btc_price": price})
        elif "KXBTC15M" in sym and "(15m)" in sym:
            contract_rows.append(
                {
                    "timestamp": ts,
                    "symbol": canon_symbol(sym),
                    "contract_price": price,
                }
            )

    btc_df = pd.DataFrame(btc_rows)
    contract_df = pd.DataFrame(contract_rows)
    return btc_df, contract_df


# --------------------------------------------------------------------------- #
# 4. Compute ground-truth labels
# --------------------------------------------------------------------------- #


def compute_labels_from_terminal_price(
    contract_df: pd.DataFrame,
) -> Dict[str, int]:
    """Infer labels from terminal contract price (>0.90 → YES, <0.10 → NO)."""
    labels: Dict[str, int] = {}
    if contract_df.empty:
        return labels
    for sym, grp in contract_df.groupby("symbol"):
        last_price = grp["contract_price"].iloc[-1]
        if last_price > 0.90:
            labels[sym] = 1
        elif last_price < 0.10:
            labels[sym] = 0
        # else: ambiguous, skip

    log.info(
        "Labels (terminal price): %d contracts (%d YES, %d NO)",
        len(labels),
        sum(labels.values()),
        len(labels) - sum(labels.values()),
    )
    return labels


def infer_strikes(
    btc_df: pd.DataFrame,
    contract_df: pd.DataFrame,
    log_strikes: Dict[str, float],
) -> Dict[str, float]:
    """Get strikes: prefer log-parsed, fall back to inferring from price ≈ 0.50 crossover."""
    strikes: Dict[str, float] = dict(log_strikes)
    if btc_df.empty or contract_df.empty:
        return strikes

    btc_sorted = btc_df.sort_values("timestamp")

    for sym, grp in contract_df.groupby("symbol"):
        if sym in strikes:
            continue  # already have from logs

        # Find observation closest to 0.50 price
        grp = grp.sort_values("timestamp")
        dist_to_half = (grp["contract_price"] - 0.50).abs()
        best_idx = dist_to_half.idxmin()
        best_price = grp.loc[best_idx, "contract_price"]

        # Only use if reasonably close to 0.50 (within 0.30)
        if abs(best_price - 0.50) > 0.30:
            continue

        best_ts = grp.loc[best_idx, "timestamp"]
        # Find nearest BTC price
        btc_mask = (btc_sorted["timestamp"] >= best_ts - pd.Timedelta(seconds=30)) & (
            btc_sorted["timestamp"] <= best_ts + pd.Timedelta(seconds=30)
        )
        btc_near = btc_sorted.loc[btc_mask]
        if btc_near.empty:
            continue

        try:
            cidx = (btc_near["timestamp"] - best_ts).abs().idxmin()
            strikes[sym] = btc_near.loc[cidx, "btc_price"]
        except Exception:
            strikes[sym] = btc_near.iloc[0]["btc_price"]

    log.info(
        "Strikes: %d from logs, %d inferred, %d total",
        len(log_strikes),
        len(strikes) - len(log_strikes),
        len(strikes),
    )
    return strikes


# --------------------------------------------------------------------------- #
# 5. Build feature samples
# --------------------------------------------------------------------------- #


def build_features(
    btc_df: pd.DataFrame,
    contract_df: pd.DataFrame,
    strikes: Dict[str, float],
    labels: Dict[str, int],
    sample_interval_s: int = 30,
) -> pd.DataFrame:
    """Build feature matrix: multiple samples per contract at regular intervals."""
    if btc_df.empty or contract_df.empty:
        return pd.DataFrame()

    btc_df = btc_df.sort_values("timestamp").reset_index(drop=True)
    contract_df = contract_df.sort_values("timestamp").reset_index(drop=True)

    samples = []
    for sym, label in labels.items():
        strike = strikes.get(sym, 0)
        if strike <= 0:
            continue
        expiry = parse_expiry(sym)
        if expiry is None:
            continue
        expiry_ts = pd.Timestamp(expiry)

        # Get contract observations for this symbol
        cdf = contract_df[contract_df["symbol"] == sym].copy()
        if cdf.empty:
            continue

        # Sample window: from first observation to 60s before expiry
        t_start = cdf["timestamp"].iloc[0]
        t_end = expiry_ts - pd.Timedelta(seconds=60)
        if t_end <= t_start:
            continue

        # Generate sample times
        t = t_start
        while t <= t_end:
            # Find nearest BTC price (within 30s)
            btc_mask = (btc_df["timestamp"] >= t - pd.Timedelta(seconds=30)) & (
                btc_df["timestamp"] <= t + pd.Timedelta(seconds=30)
            )
            btc_near = btc_df.loc[btc_mask]
            if btc_near.empty:
                t += pd.Timedelta(seconds=sample_interval_s)
                continue
            # Pick closest
            try:
                idx = (btc_near["timestamp"] - t).abs().idxmin()
                btc_price = btc_near.loc[idx, "btc_price"]
            except Exception:
                btc_price = btc_near.iloc[0]["btc_price"]

            # Find nearest contract price (within 30s)
            c_mask = (cdf["timestamp"] >= t - pd.Timedelta(seconds=30)) & (
                cdf["timestamp"] <= t + pd.Timedelta(seconds=30)
            )
            c_near = cdf.loc[c_mask]
            if c_near.empty:
                t += pd.Timedelta(seconds=sample_interval_s)
                continue
            try:
                cidx = (c_near["timestamp"] - t).abs().idxmin()
                contract_price = c_near.loc[cidx, "contract_price"]
            except Exception:
                contract_price = c_near.iloc[0]["contract_price"]

            # Time to expiry
            tte_s = max(0, (expiry_ts - t).total_seconds())

            # BTC price history for momentum/volatility
            lookback = btc_df[
                (btc_df["timestamp"] >= t - pd.Timedelta(minutes=10))
                & (btc_df["timestamp"] <= t)
            ]["btc_price"]

            # Features
            price_dist = btc_price - strike
            price_dist_pct = price_dist / strike if strike > 0 else 0
            normalized_tte = tte_s / 900.0

            # Tanh analytical estimate for comparison
            scale = max(0.3, normalized_tte) * 1000
            tanh_prob = 0.5 + 0.5 * math.tanh(price_dist / scale)

            # Momentum features
            ret_1m = ret_5m = ret_10m = 0.0
            vol_1m = vol_5m = 0.0
            btc_range = 0.0

            if len(lookback) >= 2:
                prices_arr = lookback.values
                btc_range = float(prices_arr.max() - prices_arr.min())
                log_returns = np.diff(np.log(prices_arr))
                if len(log_returns) > 0:
                    vol_5m = float(np.std(log_returns))

                # 1-min return
                btc_1m = btc_df[
                    (btc_df["timestamp"] >= t - pd.Timedelta(minutes=1))
                    & (btc_df["timestamp"] <= t)
                ]["btc_price"]
                if len(btc_1m) >= 2:
                    ret_1m = float((btc_1m.iloc[-1] - btc_1m.iloc[0]) / btc_1m.iloc[0])
                    lr_1m = np.diff(np.log(btc_1m.values))
                    vol_1m = float(np.std(lr_1m)) if len(lr_1m) > 0 else 0.0

                # 5-min return
                btc_5m = btc_df[
                    (btc_df["timestamp"] >= t - pd.Timedelta(minutes=5))
                    & (btc_df["timestamp"] <= t)
                ]["btc_price"]
                if len(btc_5m) >= 2:
                    ret_5m = float((btc_5m.iloc[-1] - btc_5m.iloc[0]) / btc_5m.iloc[0])

                # 10-min return
                if len(lookback) >= 2:
                    ret_10m = float((prices_arr[-1] - prices_arr[0]) / prices_arr[0])

            # Contract price trajectory
            c_recent = cdf[
                (cdf["timestamp"] >= t - pd.Timedelta(minutes=2))
                & (cdf["timestamp"] <= t)
            ]["contract_price"]
            contract_slope = 0.0
            if len(c_recent) >= 2:
                x = np.arange(len(c_recent), dtype=float)
                try:
                    slope, _ = np.polyfit(x, c_recent.values, 1)
                    contract_slope = float(slope)
                except Exception:
                    pass

            sample = {
                "timestamp": t,
                "symbol": sym,
                "label": label,
                # Core features
                "feat_price_distance": price_dist,
                "feat_price_distance_pct": price_dist_pct,
                "feat_contract_price": contract_price,
                "feat_tanh_prob": tanh_prob,
                "feat_market_vs_analytical": contract_price - tanh_prob,
                "feat_time_to_expiry": tte_s,
                "feat_normalized_tte": normalized_tte,
                # Momentum
                "feat_btc_return_1m": ret_1m,
                "feat_btc_return_5m": ret_5m,
                "feat_btc_return_10m": ret_10m,
                # Volatility
                "feat_btc_vol_1m": vol_1m,
                "feat_btc_vol_5m": vol_5m,
                "feat_btc_range": btc_range,
                # Contract microstructure
                "feat_contract_slope": contract_slope,
                # Time features
                "feat_hour_of_day": t.hour,
                "feat_minute_in_interval": t.minute % 15,
            }
            samples.append(sample)
            t += pd.Timedelta(seconds=sample_interval_s)

    df = pd.DataFrame(samples)
    if not df.empty:
        log.info(
            "Built %d samples from %d contracts (%.1f samples/contract)",
            len(df),
            len(labels),
            len(df) / max(1, len(labels)),
        )
    return df


# --------------------------------------------------------------------------- #
# 6. Walk-forward split (temporal, no shuffle)
# --------------------------------------------------------------------------- #


def walk_forward_split(
    df: pd.DataFrame, train_pct: float = 0.6, val_pct: float = 0.2
) -> Tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series, pd.DataFrame, pd.Series]:
    """Temporal train/val/test split."""
    df = df.sort_values("timestamp").reset_index(drop=True)
    n = len(df)
    train_end = int(n * train_pct)
    val_end = int(n * (train_pct + val_pct))

    feat_cols = [c for c in df.columns if c.startswith("feat_")]

    X_train = df.iloc[:train_end][feat_cols]
    y_train = df.iloc[:train_end]["label"]
    X_val = df.iloc[train_end:val_end][feat_cols]
    y_val = df.iloc[train_end:val_end]["label"]
    X_test = df.iloc[val_end:][feat_cols]
    y_test = df.iloc[val_end:]["label"]

    return X_train, y_train, X_val, y_val, X_test, y_test


# --------------------------------------------------------------------------- #
# 7. Training
# --------------------------------------------------------------------------- #


def train_xgboost(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
) -> XGBClassifier:
    """Train conservative XGBoost for small dataset."""
    model = XGBClassifier(
        max_depth=4,
        n_estimators=100,
        learning_rate=0.05,
        objective="binary:logistic",
        eval_metric="auc",
        early_stopping_rounds=15,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )
    return model


# --------------------------------------------------------------------------- #
# 8. Main
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(description="Train BTC 15m model from CSV data")
    parser.add_argument(
        "--data-dir",
        default="logs/_archive",
        help="Root dir to scan recursively for data_*.csv files",
    )
    parser.add_argument("--model-dir", default="data/models")
    parser.add_argument("--sample-interval", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    model_dir = Path(args.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    # Discover files recursively across all subdirectories
    data_csvs = sorted(data_dir.rglob("data_*.csv"))
    log_files = sorted(data_dir.rglob("money_printer_*.log"))

    if not data_csvs:
        log.error("No data_*.csv files found in %s", data_dir)
        return

    log.info(
        "Found %d data CSVs, %d log files across %s",
        len(data_csvs),
        len(log_files),
        data_dir,
    )

    # Parse strikes from any available logs
    log_strikes = extract_strikes_from_logs([str(p) for p in log_files])

    # Load all sessions
    all_btc = []
    all_contract = []

    for csv_path in data_csvs:
        btc_df, contract_df = load_session(str(csv_path))
        if not btc_df.empty:
            all_btc.append(btc_df)
        if not contract_df.empty:
            all_contract.append(contract_df)

    if not all_btc or not all_contract:
        log.error("No BTC or contract data found")
        return

    btc_df = (
        pd.concat(all_btc, ignore_index=True)
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    contract_df = (
        pd.concat(all_contract, ignore_index=True)
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    unique_contracts = contract_df["symbol"].unique()
    log.info(
        "Combined: %d BTC rows, %d contract rows, %d unique contracts",
        len(btc_df),
        len(contract_df),
        len(unique_contracts),
    )

    # Compute labels from terminal contract prices (no logs needed)
    labels = compute_labels_from_terminal_price(contract_df)
    if not labels:
        log.error("No contracts could be labeled.")
        return

    # Infer strikes: log-based where available, price-crossover elsewhere
    all_strikes = infer_strikes(btc_df, contract_df, log_strikes)

    if args.dry_run:
        log.info("Dry run complete. %d labelable contracts.", len(labels))
        for sym, lbl in sorted(labels.items()):
            strike = all_strikes.get(sym, 0)
            log.info("  %s strike=%.0f → %s", sym, strike, "YES" if lbl else "NO")
        return

    # Build features
    log.info("Building features (interval=%ds)...", args.sample_interval)
    df = build_features(btc_df, contract_df, all_strikes, labels, args.sample_interval)

    if df.empty or len(df) < 10:
        log.error("Not enough samples (%d). Need more data.", len(df))
        return

    feat_cols = [c for c in df.columns if c.startswith("feat_")]
    log.info("Features: %s", feat_cols)

    # Split
    X_train, y_train, X_val, y_val, X_test, y_test = walk_forward_split(df)
    log.info(
        "Split: train=%d, val=%d, test=%d | Label dist: train=%.1f%% YES, test=%.1f%% YES",
        len(X_train),
        len(X_val),
        len(X_test),
        y_train.mean() * 100,
        y_test.mean() * 100 if len(y_test) > 0 else 0,
    )

    # Train
    log.info("Training XGBoost...")
    model = train_xgboost(X_train, y_train, X_val, y_val)

    # Evaluate
    results = {}
    for name, X, y in [
        ("train", X_train, y_train),
        ("val", X_val, y_val),
        ("test", X_test, y_test),
    ]:
        if len(X) == 0:
            continue
        proba = model.predict_proba(X)[:, 1]
        preds = (proba >= 0.5).astype(int)
        try:
            auc = roc_auc_score(y, proba)
        except ValueError:
            auc = 0.0
        acc = accuracy_score(y, preds)
        results[name] = {"auc": auc, "accuracy": acc, "n": len(X)}
        log.info("  %s — AUC: %.4f | Accuracy: %.4f | N: %d", name, auc, acc, len(X))

    # Calibrate
    log.info("Calibrating probabilities...")
    cal = ProbabilityCalibrator(method="platt")
    test_proba = (
        model.predict_proba(X_test)[:, 1]
        if len(X_test) > 0
        else model.predict_proba(X_val)[:, 1]
    )
    test_y = y_test if len(y_test) > 0 else y_val
    try:
        cal.fit(test_proba, test_y.values)
        calibrated = cal.calibrate(test_proba)
        ece = cal.calibration_error(calibrated, test_y.values)
        log.info("  ECE (calibrated): %.4f", ece)
    except Exception as e:
        log.warning("Calibration failed: %s", e)

    # Feature importance
    log.info("\nFeature Importance (top 10):")
    importance = model.feature_importances_
    feat_imp = sorted(zip(feat_cols, importance), key=lambda x: -x[1])
    for fname, imp in feat_imp[:10]:
        bar = "#" * int(imp * 50)
        log.info("  %-30s %.4f %s", fname, imp, bar)

    # Save model
    model_path = str(model_dir / "btc_xgboost_latest.joblib")
    joblib.dump({"model": model, "feature_names": feat_cols}, model_path)
    log.info("Model saved to %s", model_path)

    # Save calibrator
    cal_path = str(model_dir / "calibrator_latest.joblib")
    try:
        cal.save(cal_path)
        log.info("Calibrator saved to %s", cal_path)
    except Exception:
        pass

    # Save feature metadata
    meta_path = str(model_dir / "btc_xgboost_feature_meta.json")
    with open(meta_path, "w") as f:
        json.dump(
            {
                "feature_names": feat_cols,
                "training_samples": len(df),
                "contracts_labeled": len(labels),
                "label_distribution": {
                    "yes": int(sum(labels.values())),
                    "no": len(labels) - int(sum(labels.values())),
                },
                "results": results,
            },
            f,
            indent=2,
        )
    log.info("Feature metadata saved to %s", meta_path)

    # Summary
    print("\n" + "=" * 60)
    print("  TRAINING COMPLETE")
    print("=" * 60)
    print(
        f"  Contracts: {len(labels)} ({sum(labels.values())} YES / {len(labels) - sum(labels.values())} NO)"
    )
    print(f"  Samples:   {len(df)} ({args.sample_interval}s interval)")
    print(f"  Features:  {len(feat_cols)}")
    for name, r in results.items():
        print(f"  {name:>5}: AUC={r['auc']:.4f}  Acc={r['accuracy']:.4f}  N={r['n']}")
    print(f"\n  Model:      {model_path}")
    print(f"  Calibrator: {cal_path}")
    print(f"  Metadata:   {meta_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
