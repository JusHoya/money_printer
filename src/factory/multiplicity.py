"""Multiplicity statistics for the strategy factory (PRD_STRATEGY_FACTORY FR-F2.3).

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 6.3. numpy-only;
every random draw is seeded; every formula is written out here so a red team
can recompute each number from the ledger and the frame alone.

Inputs and conventions
----------------------
* A **ledger matrix** ``M`` is ``L x D`` float64: row ``l`` is one distinct
  phenotype (the multiplicity unit -- the set of markets a genome trades; a
  genome that was killed by a hard constraint is still a row because it was a
  test), column ``d`` is one search-window date, ``M[l, d]`` is the realized
  PnL per contract of phenotype ``l`` on date ``d`` (the per-date cluster mean
  the fitness kernel stores as ``per_date_pnl``) and **NaN where the phenotype
  had no trade on that date**. Phenotypes only have PnL on the dates they
  traded, so the matrix is ragged by construction.
* Per-phenotype statistics use the phenotype's OWN traded dates only:
  ``n_l = #finite``, ``mean_l = nanmean``, ``se_l = nanstd(ddof=1)/sqrt(n_l)``,
  ``t_l = mean_l / se_l``. Rows with ``n_l < 2`` have no standard error and are
  excluded from the resampled maximum (counted in ``L_excluded``).

White Reality Check (Reality Check for data snooping, White 2000)
-------------------------------------------------------------------
``B`` date resamples: ``idx_b ~ Uniform{0..D-1}^D`` with replacement from
``np.random.default_rng(seed)``. In resample ``b`` every phenotype's mean is
recomputed over the resampled dates on which it traded (a date drawn ``w``
times counts ``w`` times; a phenotype with no traded date in the resample is
skipped for that ``b``):

    mean*_{b,l} = sum_d w_{b,d} M[l,d] 1{traded} / sum_d w_{b,d} 1{traded}

The centred statistic is studentised by the RESAMPLE's own standard error
(bootstrap-t / percentile-t): with ``c_{b,l} = sum_d w_{b,d} 1{traded}`` and
``se*_{b,l} = sqrt( var_{ddof=1}(resampled values of l) / c_{b,l} )``,

    Z_{b,l} = (mean*_{b,l} - mean_l) / se*_{b,l} ,   T_b = max_l Z_{b,l}

and ``p_RC(pick) = (1/B) sum_b 1{T_b >= t_pick}`` where ``t_pick`` is the
pick's observed ``mean_pick / se_pick``. This is the least-favourable null
(every phenotype's true mean is zero). It is a continuous p with no
``1/(K+1)`` floor; its resolution is ``1/B``. The resample's own ``se*`` (not
the original ``se_l``) is used because the observed statistic is a MAX over
many t-statistics: a max of Student-t variates (n_l - 1 degrees of freedom)
is heavier-tailed than a max of normals, and dividing by the fixed ``se_l``
would make the bootstrap max normal-tailed and the p anti-conservative
(~17% of picks below p = 0.10 on iid noise with L = 50, D = 40; the
bootstrap-t restores ~10%). A resample in which a phenotype has fewer than
two traded dates, or zero variance, contributes nothing for that phenotype.

Hansen SPA (Superior Predictive Ability, Hansen 2005), studentised
--------------------------------------------------------------------
Same resamples, but "poor" phenotypes are NOT recentred to zero: with

    g_l = mean_l  if  t_l >= -sqrt(2 log log n_l)   (not poor)
    g_l = 0       otherwise                          (poor: keeps its negative mean)

    Z^SPA_{b,l} = (mean*_{b,l} - g_l) / se*_{b,l} ,   T^SPA_b = max_l Z^SPA_{b,l}

and ``p_SPA(pick) = (1/B) sum_b 1{T^SPA_b >= t_pick}``. ``log log n`` is
floored at 0 (``n_l <= 2``: threshold 0). Hansen's statistic carries a
``max(., 0)`` floor because his observed statistic is ``max(T, 0)``; here the
observed statistic is the pick's own ``t``, so the floor is omitted -- with
it, any pick with ``t <= 0`` would get ``p_SPA = 1`` regardless of the ledger.
Because ``g_l <= mean_l`` only differs from ``mean_l`` when ``mean_l < 0``,
``Z^SPA <= Z^RC`` term by term and therefore ``p_SPA <= p_RC`` always.

Holm (1979) step-down
---------------------
Sort the ``m`` p-values ascending, ``p_(1) <= ... <= p_(m)``; the adjusted
value is ``p~_(i) = max_{j <= i} min(1, (m - j + 1) p_(j))`` and hypothesis
``i`` is rejected iff ``p~_(i) <= alpha`` (equivalently: reject sequentially
while ``p_(j) <= alpha / (m - j + 1)``, stop at the first failure).

Deflated Sharpe ratio (Bailey & Lopez de Prado 2014), on the validation DATE series
--------------------------------------------------------------------------------------
Non-annualised, per-date: ``SR = mean(x) / std(x, ddof=1)`` over the ``n``
validation dates; skewness ``gamma3 = m3 / m2^1.5`` and (non-excess) kurtosis
``gamma4 = m4 / m2^2`` from the same series (population moments). The
probabilistic Sharpe ratio against a benchmark ``SR*`` is

    PSR(SR*) = Phi( (SR - SR*) sqrt(n - 1) / sqrt(1 - gamma3 SR + (gamma4 - 1)/4 SR^2) )

and the deflated Sharpe ratio is ``DSR = PSR(E[max SR])`` with the expected
maximum Sharpe of ``N`` independent trials under the null

    E[max SR] = sqrt(V[SR]) * ( (1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)) )

``gamma = 0.5772156649...`` (Euler-Mascheroni). ``N`` = the number of distinct
phenotypes in the ledger (``n_trials``). ``V[SR]`` is the variance of the
Sharpe ratio ACROSS trials and is estimated from the ledger's own SR
distribution: ``sr_trials[l] = t_l / sqrt(n_l)`` for every ledger row with
``n_l >= 2``, ``V[SR] = var(sr_trials, ddof=1)`` (``sr_var`` may be passed
explicitly instead). When neither is available the fallback is the sampling
variance of a single SR estimate, ``(1 - gamma3 SR + (gamma4 - 1)/4 SR^2)/(n - 1)``
(Lo 2002 / Mertens 2002), and ``sr_var_source`` says so. ``N == 1`` gives
``E[max SR] = 0`` and ``DSR == PSR(0)``.

One-sided bootstrap p
---------------------
``one_sided_p(v) = (1/B) sum_b 1{ mean(v[idx_b]) <= 0 }`` with the SAME draw
the fitness kernel makes (``fitness.bootstrap_draws``: ``default_rng(seed)
.integers(0, n, size=(B, n))``), so it is the exact tail companion of the
reported ``boot_lo``. Plain share (resolution ``1/B``; ``0.0`` means
``< 1/B``).

One-sample Kolmogorov-Smirnov against Uniform(0, 1)
----------------------------------------------------
``D = max( max_i (i/n - x_(i)), max_i (x_(i) - (i-1)/n) )`` on the sorted
sample; asymptotic p from the Kolmogorov distribution with Stephens' finite-n
correction, ``lambda = (sqrt(n) + 0.12 + 0.11/sqrt(n)) D``,
``p = 2 sum_{k>=1} (-1)^(k-1) exp(-2 k^2 lambda^2)`` (clipped to [0, 1]).

Normal distribution helpers are exact-to-double: ``Phi`` via ``math.erf``,
``Phi^-1`` via Acklam's rational approximation refined by one Newton step.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

EULER_GAMMA = 0.5772156649015329
DEFAULT_N_BOOT = 4000
DEFAULT_SEED = 20260726
_MIN_DATES = 2  # a standard error needs two dates

__all__ = [
    "EULER_GAMMA",
    "deflated_sharpe",
    "holm",
    "ks_uniform",
    "ledger_matrix",
    "norm_cdf",
    "norm_ppf",
    "one_sided_p",
    "reality_check",
    "robust_variance",
    "row_stats",
    "sharpe_from_ledger",
]


# ---------------------------------------------------------------------------
# normal distribution
# ---------------------------------------------------------------------------
def norm_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf`` (exact to double precision)."""
    x = float(x)
    if math.isnan(x):
        return math.nan
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Acklam (2003) coefficients, relative error < 1.15e-9 before refinement.
_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
      3.754408661907416e00)


def norm_ppf(p: float) -> float:
    """Standard normal quantile: Acklam's approximation plus one Newton step."""
    p = float(p)
    if math.isnan(p) or p < 0.0 or p > 1.0:
        return math.nan
    if p == 0.0:
        return -math.inf
    if p == 1.0:
        return math.inf
    plow = 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= 1.0 - plow:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / (
            ((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    # one Newton-Raphson refinement on Phi(x) - p
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(0.5 * x * x)
    x = x - u / (1.0 + 0.5 * x * u)
    return x


# ---------------------------------------------------------------------------
# ledger matrix statistics
# ---------------------------------------------------------------------------
def row_stats(matrix: np.ndarray) -> Dict[str, np.ndarray]:
    """Per-row ``n``, ``mean``, ``se``, ``t`` over the finite entries of a ragged matrix."""
    M = np.asarray(matrix, dtype=np.float64)
    if M.ndim != 2:
        raise ValueError(f"ledger matrix must be 2-D, got shape {M.shape}")
    traded = np.isfinite(M)
    n = traded.sum(axis=1).astype(np.int64)
    M0 = np.where(traded, M, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n > 0, M0.sum(axis=1) / np.maximum(n, 1), np.nan)
        dev = np.where(traded, M0 - mean[:, None], 0.0)
        var = np.where(n > 1, (dev * dev).sum(axis=1) / np.maximum(n - 1, 1), np.nan)
        se = np.sqrt(var) / np.sqrt(np.maximum(n, 1))
        t = np.where((n > 1) & (se > 0), mean / se, np.nan)
    return {"n": n, "mean": mean, "se": se, "t": t}


def reality_check(
    per_date_matrix: np.ndarray,
    pick_index: int,
    *,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    chunk: int = 256,
) -> Dict[str, Any]:
    """White RC and Hansen SPA p-values of ``pick_index`` over all phenotypes (module docstring).

    Returns ``{"p_rc", "p_spa", "L", "D", "L_used", "L_excluded", "t_pick",
    "n_pick", "n_boot", "seed"}``. ``p_rc``/``p_spa`` are NaN when the pick
    itself has no standard error (fewer than two traded dates).
    """
    M = np.asarray(per_date_matrix, dtype=np.float64)
    if M.ndim != 2:
        raise ValueError(f"ledger matrix must be 2-D, got shape {M.shape}")
    L, D = M.shape
    if not (0 <= int(pick_index) < L):
        raise IndexError(f"pick_index {pick_index} outside 0..{L - 1}")
    st = row_stats(M)
    n, mean, se, t = st["n"], st["mean"], st["se"], st["t"]
    usable = (n >= _MIN_DATES) & np.isfinite(se) & (se > 0)
    t_pick = float(t[pick_index]) if usable[pick_index] else math.nan
    out: Dict[str, Any] = {
        "p_rc": math.nan,
        "p_spa": math.nan,
        "L": int(L),
        "D": int(D),
        "L_used": int(usable.sum()),
        "L_excluded": int(L - usable.sum()),
        "t_pick": t_pick,
        "n_pick": int(n[pick_index]),
        "n_boot": int(n_boot),
        "seed": int(seed),
    }
    if D == 0 or not usable.any() or not math.isfinite(t_pick) or n_boot <= 0:
        return out

    Mu = M[usable]
    traded = np.isfinite(Mu)
    M0 = np.where(traded, Mu, 0.0)
    M0sq = M0 * M0
    T1 = traded.astype(np.float64)
    mean_u = mean[usable]
    n_u = n[usable].astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        loglog = np.log(np.log(n_u))
    thresh = -np.sqrt(2.0 * np.maximum(loglog, 0.0))
    poor = t[usable] < thresh
    g = np.where(poor, 0.0, mean_u)  # SPA recentring: poor models keep their (negative) mean

    rng = np.random.default_rng(int(seed))
    idx = rng.integers(0, D, size=(int(n_boot), D))
    n_ge_rc = 0
    n_ge_spa = 0
    for start in range(0, int(n_boot), int(chunk)):
        block = idx[start:start + int(chunk)]
        b = block.shape[0]
        W = np.zeros((b, D), dtype=np.float64)
        rows = np.repeat(np.arange(b), D)
        np.add.at(W, (rows, block.ravel()), 1.0)
        S = W @ M0.T  # (b, L_used) weighted sums
        S2 = W @ M0sq.T  # weighted sums of squares
        C = W @ T1.T  # (b, L_used) weighted counts
        with np.errstate(invalid="ignore", divide="ignore"):
            Cs = np.where(C > 0, C, 1.0)
            mstar = np.where(C > 0, S / Cs, np.nan)
            var_star = np.where(C > 1, (S2 / Cs - mstar * mstar) * (Cs / np.maximum(Cs - 1.0, 1.0)), np.nan)
            se_star = np.sqrt(np.where(var_star > 0, var_star, np.nan) / Cs)
            z_rc = (mstar - mean_u[None, :]) / se_star
            z_spa = (mstar - g[None, :]) / se_star
        z_rc = np.where(np.isfinite(z_rc), z_rc, -np.inf)
        z_spa = np.where(np.isfinite(z_spa), z_spa, -np.inf)
        n_ge_rc += int(np.count_nonzero(z_rc.max(axis=1) >= t_pick))
        n_ge_spa += int(np.count_nonzero(z_spa.max(axis=1) >= t_pick))
    out["p_rc"] = n_ge_rc / float(n_boot)
    out["p_spa"] = n_ge_spa / float(n_boot)
    return out


# ---------------------------------------------------------------------------
# Holm
# ---------------------------------------------------------------------------
def holm(pvals: Dict[str, float], alpha: float = 0.05) -> Dict[str, Dict[str, Any]]:
    """Holm step-down adjustment; ``{name: {"p", "p_adj", "reject", "rank"}}``.

    NaN p-values are carried through unadjusted (``p_adj`` NaN, ``reject``
    False) and do not count toward ``m``.
    """
    items = [(k, float(v)) for k, v in pvals.items()]
    finite = [(k, v) for k, v in items if math.isfinite(v)]
    m = len(finite)
    order = sorted(finite, key=lambda kv: (kv[1], kv[0]))
    out: Dict[str, Dict[str, Any]] = {}
    running = 0.0
    for i, (k, p) in enumerate(order):
        adj = min(1.0, (m - i) * p)
        running = max(running, adj)
        out[k] = {"p": p, "p_adj": running, "reject": bool(running <= alpha), "rank": i + 1}
    for k, v in items:
        if not math.isfinite(v):
            out[k] = {"p": v, "p_adj": math.nan, "reject": False, "rank": None}
    return {k: out[k] for k, _ in items}


# ---------------------------------------------------------------------------
# Deflated Sharpe
# ---------------------------------------------------------------------------
def _moments(x: np.ndarray) -> Tuple[float, float, float, float]:
    """(mean, std_ddof1, skew, kurt) with population central moments for skew/kurt."""
    n = x.shape[0]
    mu = float(x.mean())
    d = x - mu
    m2 = float((d * d).mean())
    m3 = float((d ** 3).mean())
    m4 = float((d ** 4).mean())
    std = float(x.std(ddof=1)) if n > 1 else math.nan
    skew = m3 / m2 ** 1.5 if m2 > 0 else math.nan
    kurt = m4 / m2 ** 2 if m2 > 0 else math.nan
    return mu, std, skew, kurt


def sharpe_from_ledger(t_stat: Sequence[float], dates: Sequence[int], *, min_dates: int = _MIN_DATES) -> np.ndarray:
    """Per-row non-annualised SR ``t / sqrt(n)`` for rows with ``n >= min_dates`` and finite ``t``.

    The report passes ``min_dates = ceil(0.6 * D)`` (the MIN_DATES hard
    constraint) so the cross-trial SR variance is estimated over the
    phenotypes that actually competed for the pick, not over two-date flukes.
    """
    t = np.asarray(t_stat, dtype=np.float64)
    n = np.asarray(dates, dtype=np.float64)
    ok = np.isfinite(t) & (n >= max(int(min_dates), _MIN_DATES))
    return t[ok] / np.sqrt(n[ok])


def robust_variance(values: Sequence[float]) -> float:
    """``(1.4826 * MAD)^2`` -- a normal-consistent variance immune to a few wild trials.

    A ledger holds phenotypes whose per-date PnL is nearly constant (e.g. a
    far-bracket NO that always settles), whose SR is enormous; the plain
    cross-trial variance is dominated by them and inflates ``E[max SR]``. The
    report carries both estimates (``clustered_dsr`` and ``clustered_dsr.robust``).
    """
    x = np.asarray([float(v) for v in values], dtype=np.float64)
    x = x[np.isfinite(x)]
    if x.shape[0] < 2:
        return math.nan
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    return (1.4826 * mad) ** 2


def deflated_sharpe(
    date_series: np.ndarray,
    n_trials: int,
    *,
    sr_benchmark: float = 0.0,
    sr_trials: Optional[np.ndarray] = None,
    sr_var: Optional[float] = None,
) -> Dict[str, Any]:
    """Bailey & Lopez de Prado DSR on a per-date PnL series (module docstring)."""
    x = np.asarray(date_series, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = int(x.shape[0])
    N = max(int(n_trials), 1)
    out: Dict[str, Any] = {
        "sr": math.nan, "dsr": math.nan, "psr": math.nan, "expected_max_sr": math.nan,
        "skew": math.nan, "kurt": math.nan, "n": n, "n_trials": N,
        "sr_benchmark": float(sr_benchmark), "sr_var_trials": math.nan, "sr_var_source": None,
    }
    if n < 3:
        return out
    mu, std, skew, kurt = _moments(x)
    if not (std > 0) or not math.isfinite(skew) or not math.isfinite(kurt):
        return out
    sr = mu / std
    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not (denom_sq > 0):
        out.update({"sr": sr, "skew": skew, "kurt": kurt})
        return out
    denom = math.sqrt(denom_sq)

    if sr_var is not None:
        v = float(sr_var)
        src = "explicit"
    elif sr_trials is not None and np.asarray(sr_trials).size >= 2:
        st = np.asarray(sr_trials, dtype=np.float64)
        st = st[np.isfinite(st)]
        v = float(st.var(ddof=1)) if st.size >= 2 else math.nan
        src = "ledger_sr_distribution"
    else:
        v = denom_sq / (n - 1)
        src = "single_estimate_sampling_variance"
    if not math.isfinite(v):
        v = denom_sq / (n - 1)
        src = "single_estimate_sampling_variance"

    if N > 1:
        emax = math.sqrt(max(v, 0.0)) * (
            (1.0 - EULER_GAMMA) * norm_ppf(1.0 - 1.0 / N) + EULER_GAMMA * norm_ppf(1.0 - 1.0 / (N * math.e))
        )
    else:
        emax = 0.0

    def _psr(sr_star: float) -> float:
        return norm_cdf((sr - sr_star) * math.sqrt(n - 1.0) / denom)

    out.update({
        "sr": sr,
        "dsr": _psr(emax),
        "psr": _psr(float(sr_benchmark)),
        "expected_max_sr": emax,
        "skew": skew,
        "kurt": kurt,
        "sr_var_trials": v,
        "sr_var_source": src,
    })
    return out


# ---------------------------------------------------------------------------
# one-sided bootstrap p and KS
# ---------------------------------------------------------------------------
def one_sided_p(per_date_pnl: np.ndarray, *, n_boot: int = DEFAULT_N_BOOT, seed: int = DEFAULT_SEED) -> float:
    """Date-bootstrap ``P(mean <= 0)`` with the fitness kernel's exact draw."""
    from src.factory.fitness import bootstrap_draws

    v = np.asarray(per_date_pnl, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.shape[0] == 0:
        return math.nan
    draws = bootstrap_draws(v, n_boot=int(n_boot), seed=int(seed))
    return float(np.count_nonzero(draws <= 0.0) / draws.shape[0])


def _kolmogorov_sf(lam: float, terms: int = 100) -> float:
    if lam <= 0:
        return 1.0
    k = np.arange(1, terms + 1, dtype=np.float64)
    s = 2.0 * np.sum(((-1.0) ** (k - 1)) * np.exp(-2.0 * k * k * lam * lam))
    return float(min(1.0, max(0.0, s)))


def ks_uniform(values: Sequence[float]) -> Dict[str, Any]:
    """One-sample KS statistic vs Uniform(0,1) and its asymptotic p (module docstring)."""
    x = np.asarray([float(v) for v in values], dtype=np.float64)
    x = x[np.isfinite(x)]
    n = int(x.shape[0])
    if n == 0:
        return {"stat": math.nan, "p": math.nan, "n": 0}
    xs = np.sort(x)
    i = np.arange(1, n + 1, dtype=np.float64)
    d_plus = float(np.max(i / n - xs))
    d_minus = float(np.max(xs - (i - 1.0) / n))
    d = max(d_plus, d_minus, 0.0)
    sn = math.sqrt(n)
    lam = (sn + 0.12 + 0.11 / sn) * d
    return {"stat": d, "p": _kolmogorov_sf(lam), "n": n}


# ---------------------------------------------------------------------------
# ledger -> matrix
# ---------------------------------------------------------------------------
def ledger_matrix(
    ledger: Any,
    dates: Sequence[str],
    *,
    code_dates: Optional[Sequence[str]] = None,
) -> Tuple[np.ndarray, List[str]]:
    """``(L x D matrix, phenotype_hashes)`` from a ``Ledger`` (or an arrow table / list of rows).

    Rows are deduplicated by ``phenotype_hash`` keeping the FIRST occurrence
    (ledger order: generation, then idx); rows with an empty hash (UNSCORED,
    or killed before any trade) are skipped. ``per_date_codes`` index
    ``code_dates`` (default: ``dates``) -- the worker frame's date list, i.e.
    the campaign's search dates that exist in the frame -- and are mapped onto
    the columns ``dates``; a code whose date is not in ``dates`` is dropped.
    """
    if hasattr(ledger, "read_all"):
        table = ledger.read_all()
        rows = table.to_pylist()
    elif hasattr(ledger, "to_pylist"):
        rows = ledger.to_pylist()
    else:
        rows = list(ledger)
    cols = [str(d) for d in dates]
    col_of = {d: i for i, d in enumerate(cols)}
    cd = [str(d) for d in (code_dates if code_dates is not None else cols)]
    seen: Dict[str, int] = {}
    ids: List[str] = []
    entries: List[Tuple[int, List[int], List[float]]] = []
    for r in rows:
        ph = str(r.get("phenotype_hash") or "")
        if not ph or ph in seen:
            continue
        codes = [int(c) for c in (r.get("per_date_codes") or [])]
        pnl = [float(v) for v in (r.get("per_date_pnl") or [])]
        if len(codes) != len(pnl):
            raise ValueError(f"ledger row {r.get('row_id')}: per_date_codes/per_date_pnl length mismatch")
        for c in codes:
            if c < 0 or c >= len(cd):
                raise ValueError(
                    f"ledger row {r.get('row_id')}: per_date_code {c} outside the {len(cd)} worker dates"
                )
        seen[ph] = len(ids)
        ids.append(ph)
        entries.append((len(ids) - 1, codes, pnl))
    M = np.full((len(ids), len(cols)), np.nan, dtype=np.float64)
    for l, codes, pnl in entries:
        for c, v in zip(codes, pnl):
            j = col_of.get(cd[c])
            if j is not None:
                M[l, j] = v
    return M, ids
