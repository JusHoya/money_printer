"""Shared metric computation for backtesting and reporting.

Sprint 5 — centralized performance metrics used by the engine,
report generator, and sandbox scorecard.
"""

import math
from typing import Dict, List, Tuple

import numpy as np


def compute_win_rate(trades: List[dict]) -> float:
    """Fraction of trades with positive PnL."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return wins / len(trades)


def compute_total_pnl(trades: List[dict]) -> float:
    return sum(t.get("pnl", 0) for t in trades)


def compute_total_fees(trades: List[dict]) -> float:
    return sum(t.get("fee", 0) for t in trades)


def compute_sharpe_ratio(
    returns: List[float], risk_free_rate: float = 0.0, annualize: float = 252.0
) -> float:
    """Annualized Sharpe ratio from per-trade returns.

    Returns 0.0 if insufficient data or zero volatility.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    excess = arr - risk_free_rate / annualize
    std = np.std(excess, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(annualize))


def compute_max_drawdown(equity_curve: List[float]) -> Tuple[float, int, int]:
    """Maximum drawdown as a fraction of peak equity.

    Returns (max_dd_pct, peak_idx, trough_idx).
    """
    if len(equity_curve) < 2:
        return 0.0, 0, 0

    peak = equity_curve[0]
    peak_idx = 0
    max_dd = 0.0
    dd_peak_idx = 0
    dd_trough_idx = 0

    for i, val in enumerate(equity_curve):
        if val > peak:
            peak = val
            peak_idx = i
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            dd_peak_idx = peak_idx
            dd_trough_idx = i

    return max_dd, dd_peak_idx, dd_trough_idx


def compute_brier_score(predictions: List[Tuple[float, int]]) -> float:
    """Brier score: mean((predicted - actual)^2). Lower is better."""
    if not predictions:
        return 1.0
    return float(np.mean([(p - a) ** 2 for p, a in predictions]))


def compute_profit_factor(trades: List[dict]) -> float:
    """Sum of gross wins / abs(sum of gross losses). >1 is profitable."""
    wins = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
    losses = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def validate_criteria(results: dict) -> Dict[str, bool]:
    """Check the 5 mandatory success criteria from PRD Section 7.

    Returns a dict of {criterion: passed} bools.
    """
    return {
        "win_rate_85": results.get("win_rate", 0) >= 0.85,
        "per_strategy_75": all(
            v.get("win_rate", 0) >= 0.75
            for v in results.get("per_strategy", {}).values()
        ),
        "positive_pnl": results.get("net_pnl", 0) > 0,
        "max_drawdown_15": results.get("max_drawdown_pct", 1.0) < 0.15,
        "brier_below_20": results.get("brier_score", 1.0) < 0.20,
    }
