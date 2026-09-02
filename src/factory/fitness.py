"""Fitness kernel: ``score()`` reproduces ``ev_analysis.evaluate_shape`` (FR-F1.3).

Settlement-true, date-clustered realized PnL per contract of a genome's
trades on a ``columns.Frame``: first masked EXECUTABLE snapshot per market,
per-``target_date`` clustering, the identical 4000-draw seeded bootstrap,
``fit = boot_lo`` (PRD_STRATEGY_FACTORY section 4 A2), and the hard
constraints of ``docs/factory/FACTORY_ARCHITECTURE.md`` section 5 step 6
(violation -> ``fit = -inf`` with a reason code).

``modelled_ev`` (hidden ``ev_per_contract``) is reported as a DIAGNOSTIC only
and is never an input to ``fit`` (architecture section 11, "Modelled-EV trap").

numpy-only except ``score_reference`` (pandas, parity tests only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from src.factory import genome as G
from src.factory.columns import Frame

DEFAULT_N_BOOT = 4000
DEFAULT_SEED = 20260726

# hard-constraint thresholds (architecture section 5, step 6)
MIN_DATE_FRACTION = 0.6
MIN_TRADES = 40
MIN_CITIES = 3
WORST_DATE_MIN = -0.50
MAX_CLAUSES = 8
BSS_TRADES_MIN = -0.05
BSS_MIN_TWO_SIDED = 10

REASON_NO_TRADES = "NO_TRADES"
REASON_MIN_DATES = "MIN_DATES"
REASON_MIN_TRADES = "MIN_TRADES"
REASON_MIN_CITIES = "MIN_CITIES"
REASON_WORST_DATE = "WORST_DATE"
REASON_MAX_CLAUSES = "MAX_CLAUSES"
REASON_GEFS_TWIN = "GEFS_TWIN"
REASON_BSS = "BSS"

#: the ``ev_analysis.ShapeResult`` fields, in its order
SHAPE_RESULT_FIELDS = (
    "label",
    "trades",
    "markets",
    "city_days",
    "dates",
    "fill_opportunity_rate",
    "modelled_ev",
    "realized",
    "realized_se",
    "t_stat",
    "boot_lo",
    "boot_hi",
    "win_rate",
    "mean_price_paid",
    "mean_fee",
    "mean_model_p_yes",
    "mean_market_yes_ask",
    "realized_yes_rate",
    "losing_dates",
    "worst_date_pnl",
)

NAN = float("nan")
NEG_INF = float("-inf")


@dataclass
class FitnessResult:
    """Every ``ShapeResult`` field plus the factory's fitness/constraint fields."""

    label: str = ""
    trades: int = 0
    markets: int = 0
    city_days: int = 0
    dates: int = 0
    fill_opportunity_rate: float = NAN
    modelled_ev: float = NAN  # diagnostic only -- never an input to fit
    realized: float = NAN
    realized_se: float = NAN
    t_stat: float = NAN
    boot_lo: float = NAN
    boot_hi: float = NAN
    win_rate: float = NAN
    mean_price_paid: float = NAN
    mean_fee: float = NAN
    mean_model_p_yes: float = NAN
    mean_market_yes_ask: float = NAN
    realized_yes_rate: float = NAN
    losing_dates: int = 0
    worst_date_pnl: float = NAN
    # factory fields
    fit: float = NEG_INF
    constraint_reason: Optional[str] = None
    cities: int = 0
    bss_trades: float = NAN
    gefs_twin_realized: float = NAN
    n_dates_in_mask: int = 0
    n_active_clauses: Optional[int] = None
    per_date_pnl: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    per_date_codes: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))
    trade_rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    phenotype_hash: str = ""

    def shape_dict(self) -> Dict[str, Any]:
        """The ``ShapeResult``-shaped subset (for parity comparison / reports)."""
        return {k: getattr(self, k) for k in SHAPE_RESULT_FIELDS}

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe dict (arrays -> lists, -inf/nan preserved as floats)."""
        out: Dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, np.ndarray):
                v = v.tolist()
            elif isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, (np.floating,)):
                v = float(v)
            out[f.name] = v
        return out

    @property
    def passed(self) -> bool:
        return self.constraint_reason is None and math.isfinite(self.fit)


def _empty_result(label: str, n_dates_in_mask: int, n_active: Optional[int], n_cand: int) -> FitnessResult:
    return FitnessResult(
        label=label,
        fit=NEG_INF,
        constraint_reason=REASON_NO_TRADES,
        n_dates_in_mask=n_dates_in_mask,
        n_active_clauses=n_active,
        fill_opportunity_rate=(0.0 if n_cand > 0 else NAN),
        phenotype_hash=G.phenotype_hash_from_codes(np.zeros(0, dtype=np.int64)),
    )


def date_row_mask(F: Frame, date_codes: Sequence[int]) -> np.ndarray:
    """Row mask selecting the rows whose ``target_date_code`` is in ``date_codes``."""
    keep = np.zeros(F.n_dates, dtype=bool)
    keep[np.asarray(list(date_codes), dtype=np.int64)] = True
    return keep[F.visible["target_date_code"]]


def _n_dates_in(F: Frame, date_mask: Optional[np.ndarray]) -> int:
    if date_mask is None:
        return F.n_dates
    tdc = F.visible["target_date_code"][date_mask]
    if tdc.size == 0:
        return 0
    return int(np.count_nonzero(np.bincount(tdc, minlength=F.n_dates)))


def group_mean_kahan(codes: np.ndarray, values: np.ndarray, n_groups: int):
    """Per-group mean with pandas' compensated (Kahan) summation, in row order.

    ``pandas.core.groupby`` ``group_mean`` accumulates each group with
    ``y = v - comp; t = sum + y; comp = (t - sum) - y; sum = t`` over the rows
    in frame order and divides by the count. Plain ``np.bincount`` sums differ
    at the 1e-17 level, which flips the sign of a date whose mean is exactly
    zero (``losing_dates``), so the kernel replicates the compensated sum
    vectorised across groups (one step per within-group position).

    Returns ``(group_codes, means)`` for the groups with >= 1 row, in ascending
    code order (== ``groupby(sort=True)`` order for sorted date labels).
    """
    codes = np.asarray(codes, dtype=np.int64)
    values = np.asarray(values, dtype=np.float64)
    counts = np.bincount(codes, minlength=n_groups)
    present = np.flatnonzero(counts > 0)
    if present.size == 0:
        return present.astype(np.int16), np.zeros(0, dtype=np.float64)
    order = np.argsort(codes, kind="stable")
    c = codes[order]
    v = values[order]
    starts = np.cumsum(counts) - counts
    pos = np.arange(c.shape[0]) - starts[c]
    k_max = int(counts.max())
    mat = np.zeros((n_groups, k_max), dtype=np.float64)
    valid = np.zeros((n_groups, k_max), dtype=bool)
    mat[c, pos] = v
    valid[c, pos] = True
    sumx = np.zeros(n_groups, dtype=np.float64)
    comp = np.zeros(n_groups, dtype=np.float64)
    for k in range(k_max):
        ok = valid[:, k]
        y = mat[:, k] - comp
        t = sumx + y
        comp_new = (t - sumx) - y
        comp_new[comp_new != comp_new] = 0.0  # pandas GH#50367 nan guard
        sumx = np.where(ok, t, sumx)
        comp = np.where(ok, comp_new, comp)
    means = sumx[present] / counts[present]
    return present.astype(np.int16), means


def bootstrap_draws(values: np.ndarray, n_boot: int = DEFAULT_N_BOOT, seed: int = DEFAULT_SEED) -> np.ndarray:
    """EXACTLY the evaluator's bootstrap: seeded ``default_rng``, ``integers(0, n, (n_boot, n))``."""
    n = values.shape[0]
    rng = np.random.default_rng(seed)
    return values[rng.integers(0, n, size=(n_boot, n))].mean(axis=1)


def score(
    F: Frame,
    mask: np.ndarray,
    *,
    date_mask: Optional[np.ndarray] = None,
    label: str = "",
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    constraints: bool = True,
    twin: Optional[Frame] = None,
    genome: Optional[G.Genome] = None,
    n_active_clauses: Optional[int] = None,
) -> FitnessResult:
    """Score a row mask on ``F``; reproduces ``evaluate_shape`` field for field.

    ``mask`` is the genome's ``to_mask`` (or any bool row mask); ``date_mask``
    restricts the rows (the fold); ``twin`` is the gefs twin frame reached
    through ``F.twin_index`` (GEFS_TWIN constraint skipped when None).
    ``genome`` (or ``n_active_clauses``) enables the MAX_CLAUSES constraint.

    No masked executable row -> ``trades=0, fit=-inf, reason NO_TRADES`` (the
    evaluator's ``None``).
    """
    vis = F.visible
    hid = F.hidden
    if n_active_clauses is None and genome is not None:
        n_active_clauses = G.n_active_clauses(genome)
    n_dates_in_mask = _n_dates_in(F, date_mask)

    cand = np.asarray(mask, dtype=bool)
    if date_mask is not None:
        cand = np.logical_and(cand, date_mask)
    n_cand = int(np.count_nonzero(cand))
    if n_cand == 0:
        return _empty_result(label, n_dates_in_mask, n_active_clauses, 0)
    M = np.logical_and(cand, vis["executable"])
    n_exec = int(np.count_nonzero(M))
    if n_exec == 0:
        return _empty_result(label, n_dates_in_mask, n_active_clauses, n_cand)

    rows = G.first_true_per_block(M, F.block_starts)
    n_trades = int(rows.shape[0])

    realized = hid["realized_per_contract"][rows]
    tdc = vis["target_date_code"][rows]
    n_all_dates = F.n_dates
    per_date_codes, values = group_mean_kahan(tdc, realized, n_all_dates)
    n = int(values.shape[0])

    mean = float(values.mean())
    se = float(values.std(ddof=1) / math.sqrt(n)) if n > 1 else NAN
    t = float(mean / se) if (se == se and se > 0) else NAN
    draws = bootstrap_draws(values, n_boot=n_boot, seed=seed)
    lo, hi = (float(x) for x in np.percentile(draws, [2.5, 97.5]))

    won = hid["won"][rows]
    win_rate = float(won.mean())
    first_dir = int(vis["direction_code"][rows[0]])
    realized_yes_rate = float(1.0 - win_rate) if first_dir == 1 else win_rate

    city = vis["city_code"][rows]
    city_days = int(np.unique(city.astype(np.int64) * (n_all_dates + 1) + tdc.astype(np.int64)).shape[0])
    cities = int(np.unique(city).shape[0])

    res = FitnessResult(
        label=label,
        trades=n_trades,
        markets=int(np.unique(vis["market_code"][rows]).shape[0]),
        city_days=city_days,
        dates=n,
        fill_opportunity_rate=float(n_exec / n_cand),
        modelled_ev=float(hid["ev_per_contract"][rows].mean()),
        realized=mean,
        realized_se=se,
        t_stat=t,
        boot_lo=lo,
        boot_hi=hi,
        win_rate=win_rate,
        mean_price_paid=float(vis["price_paid"][rows].mean()),
        mean_fee=float(vis["fee_per_contract"][rows].mean()),
        mean_model_p_yes=float(vis["p_yes"][rows].mean()),
        mean_market_yes_ask=float(vis["yes_ask"][rows].mean()),
        realized_yes_rate=realized_yes_rate,
        losing_dates=int(np.count_nonzero(values < 0)),
        worst_date_pnl=float(values.min()),
        fit=lo,
        constraint_reason=None,
        cities=cities,
        n_dates_in_mask=n_dates_in_mask,
        n_active_clauses=n_active_clauses,
        per_date_pnl=values.astype(np.float64),
        per_date_codes=per_date_codes,
        trade_rows=rows,
        phenotype_hash=G.phenotype_hash_from_codes(vis["market_code"][rows]),
    )

    # Brier skill vs the market on the genome's two-sided trades
    res.bss_trades = bss_on_rows(F, rows)

    # gefs twin realized on the same trade rows
    if twin is not None and F.twin_index is not None:
        ti = F.twin_index[rows]
        ok = ti >= 0
        if np.any(ok):
            tr = twin.hidden["realized_per_contract"][ti[ok]]
            tr = tr[np.isfinite(tr)]
            res.gefs_twin_realized = float(tr.mean()) if tr.size else NAN

    if constraints:
        reason = check_constraints(res)
        if reason is not None:
            res.constraint_reason = reason
            res.fit = NEG_INF
    return res


def check_constraints(res: FitnessResult) -> Optional[str]:
    """First violated hard-constraint code, or None.

    Order: NO_TRADES, MIN_TRADES, MIN_DATES, MIN_CITIES, WORST_DATE,
    MAX_CLAUSES, GEFS_TWIN, BSS (the count constraints first, so a 4-trade
    shape reports MIN_TRADES).
    """
    if res.trades <= 0:
        return REASON_NO_TRADES
    if res.trades < MIN_TRADES:
        return REASON_MIN_TRADES
    if res.dates < MIN_DATE_FRACTION * res.n_dates_in_mask:
        return REASON_MIN_DATES
    if res.cities < MIN_CITIES:
        return REASON_MIN_CITIES
    if not (res.worst_date_pnl >= WORST_DATE_MIN):
        return REASON_WORST_DATE
    if res.n_active_clauses is not None and res.n_active_clauses > MAX_CLAUSES:
        return REASON_MAX_CLAUSES
    if res.gefs_twin_realized == res.gefs_twin_realized and res.gefs_twin_realized < 0:
        return REASON_GEFS_TWIN
    if res.bss_trades == res.bss_trades and res.bss_trades < BSS_TRADES_MIN:
        return REASON_BSS
    return None


def bss_on_rows(F: Frame, rows: np.ndarray, min_rows: int = BSS_MIN_TWO_SIDED) -> float:
    """``1 - Brier(p_win) / Brier(p_mkt)`` over the two-sided rows among ``rows``.

    ``p_mkt = (yes_bid + yes_ask) / 2`` mapped to the traded side (``1 - mid``
    for NO); outcome = hidden ``won``. NaN (skip) when fewer than ``min_rows``
    two-sided rows.
    """
    vis = F.visible
    yb = vis["yes_bid"][rows]
    ya = vis["yes_ask"][rows]
    two = (yb > 0.0) & (ya < 1.0)
    if int(np.count_nonzero(two)) < min_rows:
        return NAN
    r = rows[two]
    mid = (yb[two] + ya[two]) / 2.0
    is_no = vis["direction_code"][r] == 1
    p_mkt = np.where(is_no, 1.0 - mid, mid)
    p_win = vis["p_win"][r]
    y = F.hidden["won"][r].astype(np.float64)
    b_model = float(np.mean((p_win - y) ** 2))
    b_mkt = float(np.mean((p_mkt - y) ** 2))
    if b_mkt <= 0:
        return NAN
    return float(1.0 - b_model / b_mkt)


def frame_bss_vs_market(
    F: Frame, *, n_boot: int = DEFAULT_N_BOOT, seed: int = DEFAULT_SEED
) -> Dict[str, Any]:
    """Brier skill of the calibration ``p_yes`` vs the market mid on ALL two-sided rows.

    Counts each snapshot once (direction buy_yes, mode taker rows); outcome =
    hidden ``settles_yes``; date-clustered bootstrap CI (resample dates with
    replacement, recompute the pooled BSS).
    """
    vis = F.visible
    sel = (
        (vis["direction_code"] == 0)
        & (vis["mode_code"] == 0)
        & (vis["yes_bid"] > 0.0)
        & (vis["yes_ask"] < 1.0)
    )
    rows = np.flatnonzero(sel)
    if rows.size == 0:
        return {"bss": NAN, "ci_lo": NAN, "ci_hi": NAN, "n_rows": 0, "n_dates": 0}
    p = vis["p_yes"][rows]
    mid = (vis["yes_bid"][rows] + vis["yes_ask"][rows]) / 2.0
    y = F.hidden["settles_yes"][rows].astype(np.float64)
    s_model = (p - y) ** 2
    s_mkt = (mid - y) ** 2
    tdc = vis["target_date_code"][rows]
    n_all = F.n_dates
    sm_d = np.bincount(tdc, weights=s_model, minlength=n_all)
    sk_d = np.bincount(tdc, weights=s_mkt, minlength=n_all)
    present = np.flatnonzero(np.bincount(tdc, minlength=n_all) > 0)
    sm_d = sm_d[present]
    sk_d = sk_d[present]
    n = int(present.shape[0])
    bss = float(1.0 - sm_d.sum() / sk_d.sum())
    rng = np.random.default_rng(seed)
    draws_idx = rng.integers(0, n, size=(n_boot, n))
    w = np.zeros((n_boot, n), dtype=np.float64)
    for b in range(n_boot):
        w[b] = np.bincount(draws_idx[b], minlength=n)
    num = w @ sm_d
    den = w @ sk_d
    boot = 1.0 - num / den
    lo, hi = (float(x) for x in np.percentile(boot, [2.5, 97.5]))
    return {
        "bss": bss,
        "ci_lo": lo,
        "ci_hi": hi,
        "n_rows": int(rows.shape[0]),
        "n_dates": n,
        "brier_model": float(sm_d.sum() / rows.shape[0]),
        "brier_market": float(sk_d.sum() / rows.shape[0]),
    }


# ---------------------------------------------------------------------------
# Reference (pandas) side -- parity tests only
# ---------------------------------------------------------------------------


def score_reference(opp: Any, mask: Any, label: str = "", **kwargs: Any) -> Any:
    """Thin wrapper over ``ev_analysis.evaluate_shape`` (returns ``ShapeResult`` or None)."""
    import src.backtest.ev_analysis as ev  # pandas; lab only

    return ev.evaluate_shape(opp, mask, label, **kwargs)


def _leaf_equal(a: Any, b: Any, tol: float) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if isinstance(a, (bool, np.bool_)) or isinstance(b, (bool, np.bool_)):
        return bool(a) == bool(b)
    fa, fb = float(a), float(b)
    if math.isnan(fa) and math.isnan(fb):
        return True
    if math.isinf(fa) or math.isinf(fb):
        return fa == fb
    return abs(fa - fb) <= tol


def compare(fr: FitnessResult, sr: Any, tol: float = 1e-9, fields_: Sequence[str] = SHAPE_RESULT_FIELDS) -> List[str]:
    """Names of the ``ShapeResult`` fields where ``fr`` and ``sr`` differ (empty == parity).

    ``sr`` None (the evaluator's "no trades") is parity iff ``fr.trades == 0``
    and ``fr.fit == -inf``.
    """
    if sr is None:
        return [] if (fr.trades == 0 and fr.fit == NEG_INF) else ["trades"]
    diffs: List[str] = []
    for k in fields_:
        a = getattr(fr, k)
        b = sr[k] if isinstance(sr, dict) else getattr(sr, k)
        if not _leaf_equal(a, b, tol):
            diffs.append(k)
    return diffs


__all__ = [
    "FitnessResult",
    "score",
    "check_constraints",
    "bss_on_rows",
    "frame_bss_vs_market",
    "bootstrap_draws",
    "group_mean_kahan",
    "date_row_mask",
    "score_reference",
    "compare",
    "SHAPE_RESULT_FIELDS",
]
