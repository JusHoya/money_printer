"""Shared BTC 15m feature builder — used by BOTH training and live inference.

This module defines the canonical 16-feature schema that the
``btc_xgboost_latest.joblib`` model expects.  Historically, training built
these features inline in ``scripts/train_from_csv.py`` while live inference
went through ``FeaturePipeline`` (RSI/MACD/Bollinger), producing a completely
different feature set and silently falling back to the analytical tanh
estimator.  Extracting the per-sample computation here guarantees the train
and live paths can never drift again.

Feature list (order matches ``data/models/btc_xgboost_feature_meta.json``):

1.  ``feat_price_distance``          — spot − strike
2.  ``feat_price_distance_pct``      — (spot − strike) / strike
3.  ``feat_contract_price``          — current contract mid/ask price
4.  ``feat_tanh_prob``               — analytical tanh estimate (reference)
5.  ``feat_market_vs_analytical``    — contract_price − tanh_prob
6.  ``feat_time_to_expiry``          — seconds to expiry
7.  ``feat_normalized_tte``          — tte / 900 (one 15m interval)
8.  ``feat_btc_return_1m``           — 1-minute spot return
9.  ``feat_btc_return_5m``           — 5-minute spot return
10. ``feat_btc_return_10m``          — 10-minute spot return
11. ``feat_btc_vol_1m``              — std of 1-minute log returns
12. ``feat_btc_vol_5m``              — std of 5+ minute log returns
13. ``feat_btc_range``               — max − min over lookback window
14. ``feat_contract_slope``          — linear slope of recent contract prices
15. ``feat_hour_of_day``             — sample timestamp hour (UTC)
16. ``feat_minute_in_interval``      — minute % 15
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Canonical feature order — do NOT reorder without retraining the model.
BTC_FEATURE_NAMES: List[str] = [
    "feat_price_distance",
    "feat_price_distance_pct",
    "feat_contract_price",
    "feat_tanh_prob",
    "feat_market_vs_analytical",
    "feat_time_to_expiry",
    "feat_normalized_tte",
    "feat_btc_return_1m",
    "feat_btc_return_5m",
    "feat_btc_return_10m",
    "feat_btc_vol_1m",
    "feat_btc_vol_5m",
    "feat_btc_range",
    "feat_contract_slope",
    "feat_hour_of_day",
    "feat_minute_in_interval",
]


def _tanh_prob(spot: float, strike: float, tte_s: float) -> float:
    """Analytical tanh estimate matching the one used in training.

    Mirrors the scale logic in ``train_from_csv.build_features``:
    ``scale = max(0.3, tte/900) * 1000``.
    """
    if strike <= 0:
        return 0.5
    normalized_tte = tte_s / 900.0
    scale = max(0.3, normalized_tte) * 1000.0
    return 0.5 + 0.5 * math.tanh((spot - strike) / scale)


def _compute_momentum_vol(
    spot_history: Optional[Sequence[Tuple[float, float]]],
    now_ts: Optional[float],
) -> Dict[str, float]:
    """Compute 1m/5m/10m returns, 1m/5m vol, and range over the lookback window.

    Parameters
    ----------
    spot_history : sequence of ``(epoch_seconds, price)`` tuples, oldest first
        History of BTC spot observations.  May be empty or ``None``.
    now_ts : float
        Epoch-seconds of the sample (inclusive upper bound).  Only history
        points with ``ts <= now_ts`` are considered.

    Returns
    -------
    dict
        ``ret_1m``, ``ret_5m``, ``ret_10m``, ``vol_1m``, ``vol_5m``, ``btc_range``.
        Any window without at least 2 observations returns 0.0 for that field,
        matching the training-time behaviour (skip_short guards).
    """
    zero = {
        "ret_1m": 0.0,
        "ret_5m": 0.0,
        "ret_10m": 0.0,
        "vol_1m": 0.0,
        "vol_5m": 0.0,
        "btc_range": 0.0,
    }
    if not spot_history or now_ts is None:
        return zero

    # Filter and sort
    filtered = [(ts, p) for (ts, p) in spot_history if ts <= now_ts and p > 0]
    if len(filtered) < 2:
        return zero
    filtered.sort(key=lambda x: x[0])

    def _window(seconds: float) -> List[float]:
        lo = now_ts - seconds
        return [p for (ts, p) in filtered if ts >= lo]

    out = dict(zero)

    # 10-minute lookback window drives ret_10m, vol_5m, and btc_range
    win_10m = _window(600.0)
    if len(win_10m) >= 2:
        prices_arr = np.asarray(win_10m, dtype=float)
        out["btc_range"] = float(prices_arr.max() - prices_arr.min())
        log_returns = np.diff(np.log(prices_arr))
        if len(log_returns) > 0:
            out["vol_5m"] = float(np.std(log_returns))
        out["ret_10m"] = float((prices_arr[-1] - prices_arr[0]) / prices_arr[0])

    # 1-minute window
    win_1m = _window(60.0)
    if len(win_1m) >= 2:
        arr = np.asarray(win_1m, dtype=float)
        out["ret_1m"] = float((arr[-1] - arr[0]) / arr[0])
        lr = np.diff(np.log(arr))
        out["vol_1m"] = float(np.std(lr)) if len(lr) > 0 else 0.0

    # 5-minute window
    win_5m = _window(300.0)
    if len(win_5m) >= 2:
        arr = np.asarray(win_5m, dtype=float)
        out["ret_5m"] = float((arr[-1] - arr[0]) / arr[0])

    return out


def _compute_contract_slope(
    contract_history: Optional[Sequence[Tuple[float, float]]],
    now_ts: Optional[float],
) -> float:
    """Linear slope of the last ~2 minutes of contract prices.

    Mirrors the training-time behaviour — ``np.polyfit`` over observations
    within ``[now-120s, now]``.  Returns 0.0 when fewer than 2 observations.
    """
    if not contract_history or now_ts is None:
        return 0.0
    lo = now_ts - 120.0
    filtered = sorted(
        [(ts, p) for (ts, p) in contract_history if lo <= ts <= now_ts and p > 0],
        key=lambda x: x[0],
    )
    if len(filtered) < 2:
        return 0.0
    prices = [p for (_, p) in filtered]
    x = np.arange(len(prices), dtype=float)
    try:
        slope, _ = np.polyfit(x, np.asarray(prices, dtype=float), 1)
        return float(slope)
    except Exception:
        return 0.0


def build_btc_sample_features(
    spot: float,
    strike: float,
    contract_price: float,
    tte_s: float,
    hour_of_day: int,
    minute_of_hour: int,
    spot_history: Optional[Sequence[Tuple[float, float]]] = None,
    contract_history: Optional[Sequence[Tuple[float, float]]] = None,
    now_ts: Optional[float] = None,
) -> Dict[str, float]:
    """Build a single-row feature dict matching the training schema.

    Parameters
    ----------
    spot : float
        Current BTC spot price (USD).
    strike : float
        Contract strike price (USD).
    contract_price : float
        Current Kalshi contract price in [0, 1] — typically the mid of the
        two-sided market, or the ask for YES / (1-bid) for NO.
    tte_s : float
        Seconds remaining until contract expiry.
    hour_of_day : int
        Sample timestamp hour (0–23).
    minute_of_hour : int
        Sample timestamp minute (0–59).  Used to compute ``minute_in_interval``.
    spot_history : sequence of ``(epoch_seconds, price)``, optional
        Historical BTC spot prices for momentum / volatility features.  When
        missing or too sparse, those features fall back to 0.0, matching the
        training-time ``lookback.empty`` path.
    contract_history : sequence of ``(epoch_seconds, price)``, optional
        Historical contract prices for the slope feature.  When missing or
        too sparse, slope falls back to 0.0.
    now_ts : float, optional
        Epoch-seconds of the sample.  Required if any history list is
        provided; otherwise ignored.

    Returns
    -------
    dict
        16 ``feat_*`` keys, in the canonical order defined by
        :data:`BTC_FEATURE_NAMES`.
    """
    # Core distance / TTE features
    price_dist = float(spot) - float(strike)
    price_dist_pct = (price_dist / strike) if strike > 0 else 0.0
    normalized_tte = tte_s / 900.0
    tanh_prob = _tanh_prob(spot, strike, tte_s)
    market_vs_analytical = float(contract_price) - tanh_prob

    mom = _compute_momentum_vol(spot_history, now_ts)
    contract_slope = _compute_contract_slope(contract_history, now_ts)

    return {
        "feat_price_distance": float(price_dist),
        "feat_price_distance_pct": float(price_dist_pct),
        "feat_contract_price": float(contract_price),
        "feat_tanh_prob": float(tanh_prob),
        "feat_market_vs_analytical": float(market_vs_analytical),
        "feat_time_to_expiry": float(tte_s),
        "feat_normalized_tte": float(normalized_tte),
        "feat_btc_return_1m": float(mom["ret_1m"]),
        "feat_btc_return_5m": float(mom["ret_5m"]),
        "feat_btc_return_10m": float(mom["ret_10m"]),
        "feat_btc_vol_1m": float(mom["vol_1m"]),
        "feat_btc_vol_5m": float(mom["vol_5m"]),
        "feat_btc_range": float(mom["btc_range"]),
        "feat_contract_slope": float(contract_slope),
        "feat_hour_of_day": float(hour_of_day),
        "feat_minute_in_interval": float(minute_of_hour % 15),
    }
