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
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score
from xgboost import XGBClassifier

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.ml.btc_features import build_btc_sample_features
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


def parse_expiry(symbol: str, to_utc: bool = True) -> Optional[datetime]:
    """Parse 'KXBTC15M-26MAR201715-15' → datetime in UTC.

    Kalshi BTC 15-min symbols encode expiry in **Eastern Time**.
    CSV timestamps from the VM are UTC, so we must convert.

    Parameters
    ----------
    to_utc : bool
        If True (default), convert the parsed ET datetime to UTC.
        Set False only for unit tests comparing raw parsed values.
    """
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

        naive = datetime(2000 + yy, month, dd, hh, mm, 0)

        if not to_utc:
            return naive

        # Convert Eastern Time → UTC
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

        et = ZoneInfo("America/New_York")
        utc = ZoneInfo("UTC")
        aware = naive.replace(tzinfo=et)
        return aware.astimezone(utc).replace(tzinfo=None)

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
        # 2026-06-10 ML-label fix: a terminal price pinned at ~1.0 is a
        # cleared/locked-book artifact, not a real YES. This fast path has no
        # API to resolve it, so we skip it (drop) rather than mislabel YES.
        if 0.90 < last_price < _LOCKED_BOOK_HI:
            labels[sym] = 1
        elif last_price < 0.10:
            labels[sym] = 0
        # else: ambiguous (or locked book), skip

    log.info(
        "Labels (terminal price): %d contracts (%d YES, %d NO)",
        len(labels),
        sum(labels.values()),
        len(labels) - sum(labels.values()),
    )
    return labels


# 2026-06-10 ML-label fix: a contract whose final observed price is pinned at
# ~1.0 is almost always a cleared/locked book (the YES book empties at close ->
# bid=0, ask=1.0 -> the harvester logged 1.0), NOT a settled-YES print. The
# artifact can persist for several final samples, so terminal price is
# unreliable here and a penultimate-price heuristic cannot recover it. We treat
# such terminals as suspect and defer to the actual Kalshi settlement result.
# Verified: removes 74/74 false-YES on the Jun 6-10 production sample
# (contract-level label error 31.5% -> 0%).
_LOCKED_BOOK_HI = 0.995


# Settlement result cache file (persists across training runs)
_SETTLEMENT_CACHE_PATH = os.path.join("data", "models", "settlement_cache.json")


def _load_settlement_cache() -> Dict[str, int]:
    """Load cached settlement results from disk."""
    try:
        if os.path.exists(_SETTLEMENT_CACHE_PATH):
            with open(_SETTLEMENT_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        log.warning("Could not load settlement cache: %s", exc)
    return {}


def _save_settlement_cache(cache: Dict[str, int]) -> None:
    """Persist settlement cache to disk."""
    try:
        os.makedirs(os.path.dirname(_SETTLEMENT_CACHE_PATH), exist_ok=True)
        with open(_SETTLEMENT_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as exc:
        log.warning("Could not save settlement cache: %s", exc)


def compute_labels_with_settlement(
    contract_df: pd.DataFrame,
    kalshi_provider=None,
) -> Dict[str, int]:
    """Label contracts using terminal price first, then Kalshi API for ambiguous ones.

    For contracts whose last observed price is clearly settled (>0.90 or <0.10),
    uses the fast terminal-price heuristic.  For ambiguous contracts (price between
    0.10 and 0.90 -- typically from auto-cycles that ended before settlement),
    queries the Kalshi API to check if the contract has since settled and uses
    the definitive settlement result.

    Parameters
    ----------
    contract_df : pd.DataFrame
        Contract price observations with columns ``symbol`` and ``contract_price``.
    kalshi_provider : KalshiProvider, optional
        Authenticated Kalshi API client.  If ``None``, falls back to terminal-price
        only (identical to ``compute_labels_from_terminal_price``).

    Returns
    -------
    dict[str, int]
        Mapping of contract symbol to label (1 = YES, 0 = NO).
    """
    # Step 1: Terminal-price heuristic (fast path)
    labels: Dict[str, int] = {}
    ambiguous: List[str] = []

    if contract_df.empty:
        return labels

    # 2026-06-10 ML-label fix: a contract whose final observed price is pinned
    # at ~1.0 is almost always a cleared/locked book (YES book empties at close
    # -> bid=0, ask=1.0 -> harvester logged 1.0), NOT a settled-YES print.
    # Terminal price is unreliable here (the artifact can persist for several
    # final samples), so we defer such contracts to the actual Kalshi settlement
    # result instead of trusting the 1.0. Verified: removes 74/74 false-YES on
    # the Jun 6-10 production sample, label error 31.5% -> 0%.
    locked_book = 0
    for sym, grp in contract_df.groupby("symbol"):
        last_price = grp["contract_price"].iloc[-1]
        if last_price >= _LOCKED_BOOK_HI:  # locked/cleared book — DO NOT trust as YES
            ambiguous.append(sym)  # force settlement-result resolution
            locked_book += 1
        elif last_price > 0.85:  # genuine YES still trading below lock
            labels[sym] = 1
        elif last_price < 0.15:
            labels[sym] = 0
        else:
            ambiguous.append(sym)

    log.info(
        "Labels (terminal price): %d resolved, %d ambiguous (%.1f%%) "
        "[%d withheld by locked-book guard]",
        len(labels),
        len(ambiguous),
        100 * len(ambiguous) / max(1, len(labels) + len(ambiguous)),
        locked_book,
    )

    if not ambiguous:
        return labels

    # Step 2: Check settlement cache for previously resolved contracts
    cache = _load_settlement_cache()
    still_ambiguous = []
    for sym in ambiguous:
        if sym in cache:
            labels[sym] = cache[sym]
        else:
            still_ambiguous.append(sym)

    if cache and len(still_ambiguous) < len(ambiguous):
        log.info(
            "Settlement cache: resolved %d/%d ambiguous contracts",
            len(ambiguous) - len(still_ambiguous),
            len(ambiguous),
        )

    if not still_ambiguous or kalshi_provider is None:
        if still_ambiguous and kalshi_provider is None:
            log.warning(
                "No Kalshi API available — %d ambiguous contracts remain unlabeled",
                len(still_ambiguous),
            )
        _log_label_summary(labels)
        return labels

    # Step 3: Query Kalshi API for remaining ambiguous contracts
    api_resolved = 0
    api_errors = 0
    api_unsettled = 0
    min_request_interval = 0.12  # Stay under 10 req/s rate limit

    log.info("Querying Kalshi API for %d ambiguous contracts...", len(still_ambiguous))

    for sym in still_ambiguous:
        try:
            # Rate limiting
            time.sleep(min_request_interval)

            # Fetch raw market data (includes status and result fields)
            raw = kalshi_provider._fetch_market_raw(sym, kalshi_provider.PUBLIC_API_URL)
            if not raw:
                api_errors += 1
                continue

            status = (raw.get("status") or "").lower()
            result = (raw.get("result") or "").lower()

            if status in ("settled", "finalized", "closed") and result in ("yes", "no"):
                label = 1 if result == "yes" else 0
                labels[sym] = label
                cache[sym] = label
                api_resolved += 1
                log.debug("API settled %s → %s", sym, result.upper())
            else:
                # Contract hasn't settled yet — skip it
                api_unsettled += 1
                log.debug(
                    "API: %s not settled (status=%s, result=%s)", sym, status, result
                )

        except Exception as exc:
            api_errors += 1
            log.warning("API error for %s: %s", sym, exc)

    # Save updated cache
    if api_resolved > 0:
        _save_settlement_cache(cache)

    log.info(
        "Kalshi API: %d resolved, %d unsettled, %d errors (of %d queried)",
        api_resolved,
        api_unsettled,
        api_errors,
        len(still_ambiguous),
    )

    _log_label_summary(labels)
    return labels


def _log_label_summary(labels: Dict[str, int]) -> None:
    """Log label count summary."""
    yes_count = sum(labels.values())
    no_count = len(labels) - yes_count
    log.info(
        "Labels (final): %d contracts (%d YES, %d NO)",
        len(labels),
        yes_count,
        no_count,
    )


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
    expiry_buffer_s: int = 15,
) -> pd.DataFrame:
    """Build feature matrix: multiple samples per contract at regular intervals.

    Parameters
    ----------
    expiry_buffer_s : int
        Stop sampling this many seconds before expiry.  Contracts observed
        only within this buffer are skipped (outcome already decided).
        Default 15s (was 60s, which discarded 53% of contracts).
    """
    if btc_df.empty or contract_df.empty:
        return pd.DataFrame()

    btc_df = btc_df.sort_values("timestamp").reset_index(drop=True)
    contract_df = contract_df.sort_values("timestamp").reset_index(drop=True)

    samples = []
    skip_no_strike = skip_no_expiry = skip_no_obs = skip_short = 0
    for sym, label in labels.items():
        strike = strikes.get(sym, 0)
        if strike <= 0:
            skip_no_strike += 1
            continue
        expiry = parse_expiry(sym)
        if expiry is None:
            skip_no_expiry += 1
            continue
        expiry_ts = pd.Timestamp(expiry)

        # Get contract observations for this symbol
        cdf = contract_df[contract_df["symbol"] == sym].copy()
        if cdf.empty:
            skip_no_obs += 1
            continue

        # Sample window: from first observation to expiry_buffer_s before expiry
        t_start = cdf["timestamp"].iloc[0]
        t_end = expiry_ts - pd.Timedelta(seconds=expiry_buffer_s)
        if t_end <= t_start:
            skip_short += 1
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

            # BTC spot history for momentum/volatility features (last 10 minutes).
            # Convert to (epoch_s, price) tuples so the shared builder gets the
            # same representation as the live path.
            lookback_df = btc_df[
                (btc_df["timestamp"] >= t - pd.Timedelta(minutes=10))
                & (btc_df["timestamp"] <= t)
            ]
            spot_history = [
                (ts.timestamp(), float(p))
                for ts, p in zip(lookback_df["timestamp"], lookback_df["btc_price"])
            ]

            # Contract price trajectory (last 2 minutes) for slope feature.
            c_recent_df = cdf[
                (cdf["timestamp"] >= t - pd.Timedelta(minutes=2))
                & (cdf["timestamp"] <= t)
            ]
            contract_history = [
                (ts.timestamp(), float(p))
                for ts, p in zip(
                    c_recent_df["timestamp"], c_recent_df["contract_price"]
                )
            ]

            # Delegate all 16 feature values to the shared builder — same
            # code path live inference uses, so train/serve can't drift.
            feats = build_btc_sample_features(
                spot=btc_price,
                strike=strike,
                contract_price=contract_price,
                tte_s=tte_s,
                hour_of_day=t.hour,
                minute_of_hour=t.minute,
                spot_history=spot_history,
                contract_history=contract_history,
                now_ts=t.timestamp(),
            )
            sample = {"timestamp": t, "symbol": sym, "label": label, **feats}
            samples.append(sample)
            t += pd.Timedelta(seconds=sample_interval_s)

    df = pd.DataFrame(samples)
    used = len(labels) - skip_no_strike - skip_no_expiry - skip_no_obs - skip_short
    if not df.empty:
        log.info(
            "Built %d samples from %d/%d contracts (%.1f samples/contract) | "
            "skipped: %d no_strike, %d short_window, %d no_obs, %d no_expiry",
            len(df),
            used,
            len(labels),
            len(df) / max(1, used),
            skip_no_strike,
            skip_short,
            skip_no_obs,
            skip_no_expiry,
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


def compute_outcome_weights(
    df: pd.DataFrame,
    journal_outcomes: list,
    default_weight: float = 1.0,
    wrong_weight: float = 1.5,
    wrong_confident_weight: float = 2.0,
    correct_weight: float = 1.2,
    max_weight: float = 3.0,
) -> np.ndarray:
    """Assign sample weights based on trade outcome feedback.

    Matches training samples to trade journal outcomes by symbol overlap.
    Weights:
      - Default: 1.0 (no matching outcome)
      - Model was CORRECT and profitable: 1.2 (reinforce)
      - Model was WRONG: 1.5 (focus on failures)
      - Model was WRONG with HIGH confidence (>0.7): 2.0 (punish overconfidence)
      - Capped at max_weight to prevent overfitting
    """
    weights = np.full(len(df), default_weight)
    if not journal_outcomes or "symbol" not in df.columns:
        return weights

    # Build outcome lookup: symbol -> (prediction_correct, model_confidence)
    outcome_map = {}
    for o in journal_outcomes:
        sym = getattr(o, "symbol", None) or (
            o.get("symbol") if isinstance(o, dict) else None
        )
        if not sym:
            continue
        correct = getattr(o, "prediction_correct", None)
        if correct is None and isinstance(o, dict):
            correct = o.get("prediction_correct")
        conf = getattr(o, "model_confidence", None)
        if conf is None and isinstance(o, dict):
            conf = o.get("model_confidence")
        outcome_map[sym] = (correct, conf)

    if not outcome_map:
        return weights

    for i, row in df.iterrows():
        sym = row.get("symbol", "")
        if sym not in outcome_map:
            continue
        correct, conf = outcome_map[sym]
        if correct is True:
            weights[i] = min(correct_weight, max_weight)
        elif correct is False:
            if conf is not None and conf > 0.7:
                weights[i] = min(wrong_confident_weight, max_weight)
            else:
                weights[i] = min(wrong_weight, max_weight)

    weighted = int((weights != default_weight).sum())
    if weighted:
        log.info(
            "Outcome weights: %d/%d samples weighted (avg=%.2f)",
            weighted,
            len(weights),
            weights.mean(),
        )
    return weights


def train_xgboost(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sample_weight: Optional[np.ndarray] = None,
) -> XGBClassifier:
    """Train conservative XGBoost for small dataset."""
    model = XGBClassifier(
        max_depth=3,
        n_estimators=100,
        learning_rate=0.05,
        objective="binary:logistic",
        eval_metric="auc",
        early_stopping_rounds=15,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_weight=5,
        reg_alpha=0.1,
        gamma=0.1,
    )
    fit_kwargs = {
        "eval_set": [(X_val, y_val)],
        "verbose": False,
    }
    if sample_weight is not None:
        fit_kwargs["sample_weight"] = sample_weight
    model.fit(X_train, y_train, **fit_kwargs)
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
    parser.add_argument(
        "--use-api",
        action="store_true",
        help="Query Kalshi API to resolve ambiguous contract labels via settlement data",
    )
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

    # Deduplicate by filename — same CSV can appear in multiple cycle dirs
    seen_names = set()
    unique_csvs = []
    for csv_path in data_csvs:
        if csv_path.name not in seen_names:
            seen_names.add(csv_path.name)
            unique_csvs.append(csv_path)
    skipped = len(data_csvs) - len(unique_csvs)

    log.info(
        "Found %d data CSVs (%d unique, %d duplicates skipped), %d log files across %s",
        len(data_csvs),
        len(unique_csvs),
        skipped,
        len(log_files),
        data_dir,
    )

    # Parse strikes from any available logs
    log_strikes = extract_strikes_from_logs([str(p) for p in log_files])

    # Load all sessions
    all_btc = []
    all_contract = []

    for csv_path in unique_csvs:
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
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    contract_df = (
        pd.concat(all_contract, ignore_index=True)
        .drop_duplicates(subset=["timestamp", "symbol"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    unique_contracts = contract_df["symbol"].unique()
    log.info(
        "Combined: %d BTC rows, %d contract rows, %d unique contracts (from %d unique CSVs)",
        len(btc_df),
        len(contract_df),
        len(unique_contracts),
        len(unique_csvs),
    )

    # Compute labels — use Kalshi API for ambiguous contracts if --use-api
    kalshi = None
    if args.use_api:
        try:
            from src.data.kalshi_provider import KalshiProvider

            k_id = os.getenv("KALSHI_KEY_ID")
            k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
            if k_id and k_key:
                kalshi = KalshiProvider(k_id, k_key, read_only=True)
                log.info("Kalshi API initialized for settlement lookups")
            else:
                log.warning(
                    "--use-api: KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set"
                )
        except Exception as exc:
            log.warning("Could not initialize Kalshi API: %s", exc)

    labels = compute_labels_with_settlement(contract_df, kalshi_provider=kalshi)
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
