"""``src.factory.multiplicity``: RC p uniform on iid noise, SPA <= RC, Holm, DSR, KS, ledger_matrix."""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.factory import multiplicity as MP


# ---------------------------------------------------------------------------
# normal helpers
# ---------------------------------------------------------------------------
def test_norm_ppf_and_cdf_round_trip():
    assert abs(MP.norm_ppf(0.975) - 1.959963984540054) < 1e-12
    assert MP.norm_ppf(0.5) == 0.0
    assert abs(MP.norm_ppf(0.8413447460685429) - 1.0) < 1e-12
    for p in (1e-9, 0.001, 0.02, 0.3, 0.7, 0.99, 1 - 1e-9):
        assert abs(MP.norm_cdf(MP.norm_ppf(p)) - p) < 1e-13
    assert MP.norm_ppf(0.0) == -math.inf and MP.norm_ppf(1.0) == math.inf
    assert math.isnan(MP.norm_ppf(1.5))


# ---------------------------------------------------------------------------
# Reality Check: p uniform on iid noise
# ---------------------------------------------------------------------------
def test_rc_p_uniform_on_iid_noise():
    rng = np.random.default_rng(20260903)
    L, D, n_ledgers = 50, 40, 200
    p_vals = []
    p_spa = []
    for i in range(n_ledgers):
        M = rng.standard_normal((L, D))
        st = MP.row_stats(M)
        pick = int(np.argmax(st["t"]))  # the natural pick: the best in-sample phenotype
        rc = MP.reality_check(M, pick, n_boot=600, seed=1000 + i)
        p_vals.append(rc["p_rc"])
        p_spa.append(rc["p_spa"])
        assert rc["L"] == L and rc["D"] == D and rc["L_used"] == L
        assert rc["p_spa"] <= rc["p_rc"] + 1e-12
    ks = MP.ks_uniform(p_vals)
    share = float(np.mean(np.asarray(p_vals) < 0.10))
    assert ks["p"] > 0.01, (ks, share)
    assert 0.05 <= share <= 0.16, share


def test_rc_ragged_matrix_and_nan_handling():
    rng = np.random.default_rng(5)
    M = rng.standard_normal((20, 30))
    M[rng.random(M.shape) < 0.4] = np.nan  # phenotypes trade only on some dates
    M[3, :] = np.nan
    M[3, 0] = 0.5  # a single-date phenotype: no se -> excluded
    st = MP.row_stats(M)
    assert st["n"][3] == 1 and math.isnan(st["se"][3])
    pick = int(np.nanargmax(np.where(np.isfinite(st["t"]), st["t"], -np.inf)))
    rc = MP.reality_check(M, pick, n_boot=300, seed=2)
    assert rc["L_excluded"] >= 1 and 0.0 <= rc["p_rc"] <= 1.0 and rc["p_spa"] <= rc["p_rc"]
    rc3 = MP.reality_check(M, 3, n_boot=300, seed=2)
    assert math.isnan(rc3["p_rc"]) and math.isnan(rc3["p_spa"])
    # determinism
    assert MP.reality_check(M, pick, n_boot=300, seed=2) == rc


def test_spa_is_below_rc_when_poor_models_exist():
    rng = np.random.default_rng(9)
    M = rng.standard_normal((40, 30)) * 0.1
    M[:15] -= 0.5  # fifteen clearly poor phenotypes
    st = MP.row_stats(M)
    pick = int(np.argmax(st["t"]))
    rc = MP.reality_check(M, pick, n_boot=1000, seed=3)
    assert rc["p_spa"] <= rc["p_rc"]


# ---------------------------------------------------------------------------
# Holm
# ---------------------------------------------------------------------------
def test_holm_hand_computed():
    # m = 4: sorted 0.01, 0.02, 0.03, 0.04 -> adjusted 0.04, 0.06, 0.06, 0.06
    out = MP.holm({"a": 0.04, "b": 0.01, "c": 0.03, "d": 0.02}, alpha=0.05)
    assert out["b"]["rank"] == 1 and abs(out["b"]["p_adj"] - 0.04) < 1e-12 and out["b"]["reject"]
    assert out["d"]["rank"] == 2 and abs(out["d"]["p_adj"] - 0.06) < 1e-12 and not out["d"]["reject"]
    assert abs(out["c"]["p_adj"] - 0.06) < 1e-12 and abs(out["a"]["p_adj"] - 0.06) < 1e-12
    # monotone: adjusted values never decrease along the ordering
    ranks = sorted(out.values(), key=lambda v: v["rank"])
    assert all(ranks[i]["p_adj"] <= ranks[i + 1]["p_adj"] for i in range(len(ranks) - 1))
    # single hypothesis: unchanged; nan carried through
    one = MP.holm({"x": 0.03, "y": float("nan")})
    assert one["x"]["p_adj"] == 0.03 and one["x"]["reject"] and math.isnan(one["y"]["p_adj"]) and not one["y"]["reject"]
    assert list(one) == ["x", "y"]


# ---------------------------------------------------------------------------
# Deflated Sharpe
# ---------------------------------------------------------------------------
def test_dsr_reproduces_hand_computation():
    x = np.array([0.05, -0.02, 0.08, 0.01, -0.04, 0.06, 0.03, -0.01, 0.07, 0.02, 0.00, 0.04], dtype=np.float64)
    n = x.shape[0]
    sr = x.mean() / x.std(ddof=1)
    d = x - x.mean()
    m2, m3, m4 = (d ** 2).mean(), (d ** 3).mean(), (d ** 4).mean()
    skew, kurt = m3 / m2 ** 1.5, m4 / m2 ** 2
    N = 250
    v = 0.09
    g = MP.EULER_GAMMA
    emax = math.sqrt(v) * ((1 - g) * MP.norm_ppf(1 - 1 / N) + g * MP.norm_ppf(1 - 1 / (N * math.e)))
    denom = math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr * sr)
    dsr_hand = MP.norm_cdf((sr - emax) * math.sqrt(n - 1) / denom)
    psr_hand = MP.norm_cdf(sr * math.sqrt(n - 1) / denom)
    out = MP.deflated_sharpe(x, N, sr_var=v)
    assert abs(out["sr"] - sr) < 1e-12 and abs(out["skew"] - skew) < 1e-12 and abs(out["kurt"] - kurt) < 1e-12
    assert abs(out["expected_max_sr"] - emax) < 1e-12
    assert abs(out["dsr"] - dsr_hand) < 1e-12 and abs(out["psr"] - psr_hand) < 1e-12
    assert out["n"] == n and out["n_trials"] == N and out["sr_var_source"] == "explicit"
    # from a ledger SR distribution
    trials = np.array([0.1, -0.2, 0.3, 0.05, -0.1, 0.4])
    out2 = MP.deflated_sharpe(x, N, sr_trials=trials)
    assert abs(out2["sr_var_trials"] - trials.var(ddof=1)) < 1e-12 and out2["sr_var_source"] == "ledger_sr_distribution"
    # one trial: no deflation
    out1 = MP.deflated_sharpe(x, 1, sr_var=v)
    assert out1["expected_max_sr"] == 0.0 and abs(out1["dsr"] - out1["psr"]) < 1e-15
    assert math.isnan(MP.deflated_sharpe(np.array([1.0, 2.0]), 5)["dsr"])


def test_robust_variance():
    x = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 100.0])
    assert MP.robust_variance(x) < 0.1 < float(np.var(x, ddof=1))
    assert abs(MP.robust_variance([1.0, 2.0, 3.0]) - (1.4826 * 1.0) ** 2) < 1e-12
    assert math.isnan(MP.robust_variance([1.0]))


def test_sharpe_from_ledger_filters():
    sr = MP.sharpe_from_ledger([2.0, float("nan"), 1.0, 3.0], [4, 4, 1, 9], min_dates=2)
    assert np.allclose(sr, [1.0, 1.0])
    assert MP.sharpe_from_ledger([2.0, 3.0], [4, 9], min_dates=5).tolist() == [1.0]


# ---------------------------------------------------------------------------
# one-sided p, KS
# ---------------------------------------------------------------------------
def test_one_sided_p_matches_kernel_draws():
    from src.factory.fitness import bootstrap_draws

    v = np.array([0.02, -0.01, 0.03, 0.05, -0.02, 0.01, 0.04])
    draws = bootstrap_draws(v, n_boot=500, seed=11)
    assert MP.one_sided_p(v, n_boot=500, seed=11) == float(np.mean(draws <= 0))
    assert MP.one_sided_p(np.full(5, 0.1), n_boot=100, seed=1) == 0.0
    assert math.isnan(MP.one_sided_p(np.array([]), n_boot=100, seed=1))


def test_ks_uniform():
    rng = np.random.default_rng(3)
    u = rng.random(500)
    ks = MP.ks_uniform(u)
    assert ks["n"] == 500 and 0 < ks["stat"] < 0.1 and ks["p"] > 0.05
    lo = rng.random(500) * 0.3  # concentrated below 0.3
    assert MP.ks_uniform(lo)["p"] < 1e-6
    # hand check of D on a tiny sample: {0.1, 0.5, 0.9} -> D+ = max(1/3-0.1, 2/3-0.5, 1-0.9) = 0.2333.., D- = max(0.1-0, 0.5-1/3, 0.9-2/3) = 0.2333..
    ks3 = MP.ks_uniform([0.1, 0.5, 0.9])
    assert abs(ks3["stat"] - (1 / 3 - 0.1)) < 1e-12
    assert math.isnan(MP.ks_uniform([])["p"])


# ---------------------------------------------------------------------------
# ledger_matrix
# ---------------------------------------------------------------------------
def test_ledger_matrix_dedupes_and_maps_codes(tmp_path):
    from src.factory.ledger import Ledger

    class R:  # a FitnessResult-shaped stand-in
        def __init__(self, fit, ph, codes, pnl):
            self.fit, self.phenotype_hash, self.per_date_codes, self.per_date_pnl = fit, ph, codes, pnl
            self.constraint_reason = None if math.isfinite(fit) else "MIN_TRADES"
            self.trades, self.dates, self.cities = 5, len(codes), 3
            self.realized = self.realized_se = self.t_stat = self.boot_lo = self.boot_hi = self.worst_date_pnl = self.bss_trades = 0.0

    led = Ledger(tmp_path, "A")
    led.append_unscored(0, ['{"g":1}', '{"g":2}', '{"g":3}', '{"g":4}'])
    led.mark_scored(0, [
        R(0.1, "ph1", [0, 2], [0.5, -0.5]),
        R(-math.inf, "ph2", [1], [0.25]),        # killed but in the ledger: still a test
        R(0.2, "ph1", [0, 2], [9.0, 9.0]),       # duplicate phenotype: first copy wins
        None,                                      # no result: empty hash, skipped
    ])
    led.append_unscored(1, ['{"g":5}'])
    led.mark_scored(1, [R(0.3, "ph3", [2, 3], [1.0, 2.0])])
    worker_dates = ["2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21"]
    M, ids = MP.ledger_matrix(led, worker_dates)
    assert ids == ["ph1", "ph2", "ph3"] and M.shape == (3, 4)
    assert M[0, 0] == 0.5 and M[0, 2] == -0.5 and np.isnan(M[0, 1])
    assert M[1, 1] == 0.25 and M[2, 3] == 2.0
    # columns can be a superset / different order of the worker dates
    M2, ids2 = MP.ledger_matrix(led, ["2026-05-21", "2026-05-20", "2026-06-01"], code_dates=worker_dates)
    assert ids2 == ids and M2[2, 0] == 2.0 and M2[2, 1] == 1.0 and np.isnan(M2[2, 2])
    with pytest.raises(ValueError):
        MP.ledger_matrix(led, worker_dates, code_dates=worker_dates[:2])
