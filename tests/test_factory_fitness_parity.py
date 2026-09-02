"""Fitness kernel parity with ``ev_analysis.evaluate_shape`` (FR-F1.3, Phase F1 exit criteria).

Real data: the Phase-2 parity frame with the forecast archive and truth files
pinned to commit 48618cf (skips if git cannot produce them). Module-scoped
fixtures; the 1,000-genome sweep is the slow part (pandas side, ~30-60 s).
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest

from src.factory import fitness as FT
from src.factory import genome as G
from tests import factory_testkit as K

REF_FR31A = {
    "trades": 181, "dates": 65, "realized": 0.06362903846153846, "realized_se": 0.024798421096283527,
    "t_stat": 2.5658503908167916, "boot_lo": 0.01220677083333332, "boot_hi": 0.1086289246794872,
}
TOL = 1e-9


@pytest.fixture(scope="module")
def opp(tmp_path_factory):
    dirs = K.pinned_dirs(tmp_path_factory)
    return K.build_opp(True, dirs)


@pytest.fixture(scope="module")
def F(opp):
    return K.opp_to_frame(opp, name="parity")


@pytest.fixture(scope="module")
def reference_shapes():
    with open(K.REFERENCE_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    return {s["label"]: s for s in data["shapes"]}


def _fmt(r: FT.FitnessResult) -> str:
    return (
        f"trades={r.trades} dates={r.dates} realized={r.realized:+.6f} se={r.realized_se:.6f} "
        f"t={r.t_stat:+.4f} boot=[{r.boot_lo:+.6f},{r.boot_hi:+.6f}] fit={r.fit} reason={r.constraint_reason}"
    )


# ---------------------------------------------------------------------------
# (a) the four Phase-2 taker shapes and all seven seeds
# ---------------------------------------------------------------------------


def test_fr31a_taker_headline_numbers(F, opp):
    g = G.SEEDS["fr31a_taker"]
    r = FT.score(F, G.to_mask(g, F), label="fr31a_taker", constraints=False)
    print("\nfr31a_taker:", _fmt(r))
    for k, v in REF_FR31A.items():
        assert abs(getattr(r, k) - v) <= TOL, (k, getattr(r, k), v)
    assert r.trades == 181 and r.dates == 65 and r.cities == 4
    assert round(r.realized, 4) == 0.0636 and round(r.realized_se, 4) == 0.0248 and round(r.t_stat, 2) == 2.57
    assert round(r.boot_lo, 4) == 0.0122 and round(r.boot_hi, 4) == 0.1086
    assert r.fit == r.boot_lo


def test_phase2_shapes_match_reference_json_and_evaluate_shape(F, opp, reference_shapes):
    masks = K.phase2_masks(opp)
    for seed_name, label in G.PHASE2_SHAPE_LABELS.items():
        g = G.SEEDS[seed_name]
        pm = K.to_pandas_mask(g, opp)
        # the genome's predicate list reproduces the go_no_go mask row-for-row
        assert np.array_equal(pm.to_numpy(), masks[seed_name].to_numpy()), seed_name
        r = FT.score(F, G.to_mask(g, F), label=label, constraints=False)
        sr = FT.score_reference(opp, masks[seed_name], label)
        assert sr is not None
        assert FT.compare(r, sr, tol=TOL) == [], seed_name
        ref = reference_shapes[label]
        for k, v in ref.items():
            if isinstance(v, str):
                assert getattr(r, k) == v
            elif v is None or (isinstance(v, float) and math.isnan(v)):
                assert math.isnan(getattr(r, k)), (seed_name, k)
            else:
                assert abs(float(getattr(r, k)) - float(v)) <= TOL, (seed_name, k, getattr(r, k), v)
        print(f"\n{seed_name:16s} {label}: {_fmt(r)}")


def test_all_seven_seeds_parity(F, opp):
    for name, g in G.SEEDS.items():
        pm = K.to_pandas_mask(g, opp)
        r = FT.score(F, G.to_mask(g, F), label=name, constraints=False)
        sr = FT.score_reference(opp, pm, name)
        assert FT.compare(r, sr, tol=TOL) == [], name
        assert r.phenotype_hash == G.phenotype_hash(g, F)
        print(f"\n{name:20s} {_fmt(r)} cities={r.cities} bss_trades={r.bss_trades:+.4f} clauses={G.n_active_clauses(g)}")
    a = FT.score(F, G.to_mask(G.SEEDS["fr31a_taker"], F), constraints=False)
    b = FT.score(F, G.to_mask(G.SEEDS["fr31a_gefs"], F), constraints=False)
    assert np.array_equal(a.trade_rows, b.trade_rows)  # same genes, gfs_mex frame


def test_nofilter_no_reproduces_baseline(F):
    r = FT.score(F, G.to_mask(G.SEEDS["nofilter_no"], F), constraints=False)
    assert r.trades == 664 and r.dates == 69
    assert abs(r.realized - 0.020873737564770182) <= TOL
    assert round(r.realized, 4) == 0.0209


def test_mlweather_fallback_has_a_number_at_or_below_baseline(F):
    r = FT.score(F, G.to_mask(G.SEEDS["mlweather_fallback"], F), constraints=False)
    base = FT.score(F, G.to_mask(G.SEEDS["nofilter_no"], F), constraints=False)
    print("\nmlweather_fallback:", _fmt(r))
    assert r.trades > 0 and math.isfinite(r.realized)
    assert r.realized <= base.realized


# ---------------------------------------------------------------------------
# (b) 1,000 random genomes vs evaluate_shape (including None <-> -inf)
# ---------------------------------------------------------------------------


def test_1000_random_genomes_agree_with_evaluate_shape(F, opp):
    rng = np.random.default_rng(20260726)
    genomes = [G.Genome.random(rng) for _ in range(1000)]
    n_none = n_traded = 0
    t_kernel = t_pandas = 0.0
    for i, g in enumerate(genomes):
        t0 = time.perf_counter()
        r = FT.score(F, G.to_mask(g, F), label=f"g{i}", constraints=False)
        t_kernel += time.perf_counter() - t0
        t0 = time.perf_counter()
        sr = FT.score_reference(opp, K.to_pandas_mask(g, opp), f"g{i}")
        t_pandas += time.perf_counter() - t0
        if sr is None:
            n_none += 1
            assert r.trades == 0 and r.fit == float("-inf") and r.constraint_reason == FT.REASON_NO_TRADES, i
            continue
        n_traded += 1
        assert r.trades == sr.trades and r.dates == sr.dates, i
        assert abs(r.realized - sr.realized) <= TOL, i
        assert abs(r.boot_lo - sr.boot_lo) <= TOL, i
        assert FT.compare(r, sr, tol=TOL) == [], (i, FT.compare(r, sr, tol=TOL))
        assert r.fit == r.boot_lo
    print(
        f"\n1000 random genomes: {n_none} no-trade (None <-> -inf), {n_traded} traded; "
        f"kernel {t_kernel * 1000 / 1000:.2f} ms/genome, pandas {t_pandas * 1000 / 1000:.1f} ms/genome"
    )
    assert n_none + n_traded == 1000


# ---------------------------------------------------------------------------
# (c) constraints
# ---------------------------------------------------------------------------


def test_constraints_fr31b_min_trades(F):
    g = G.SEEDS["fr31b"]
    r = FT.score(F, G.to_mask(g, F), label="fr31b", genome=g)
    assert r.trades == 4
    assert r.fit == float("-inf") and r.constraint_reason == FT.REASON_MIN_TRADES
    # stats are still reported after a violation
    assert abs(r.realized - 0.08187500000000002) <= TOL and r.dates == 4


def test_constraints_reported_for_every_seed(F):
    out = {}
    for name, g in G.SEEDS.items():
        r = FT.score(F, G.to_mask(g, F), label=name, genome=g)
        out[name] = (r.constraint_reason, r.fit)
        print(f"\n{name:20s} reason={r.constraint_reason} fit={r.fit} worst={r.worst_date_pnl:+.4f} bss={r.bss_trades:+.4f} dates={r.dates}/{r.n_dates_in_mask}")
    # fr31a's worst date is -0.744 (reference JSON) -> WORST_DATE on the parity frame
    assert out["fr31a_taker"][0] == FT.REASON_WORST_DATE and out["fr31a_taker"][1] == float("-inf")
    assert out["fr31b"][0] == FT.REASON_MIN_TRADES


def test_constraint_codes_synthetic():
    base = FT.FitnessResult(trades=100, dates=60, n_dates_in_mask=69, cities=4, worst_date_pnl=-0.1, boot_lo=0.01, fit=0.01)
    assert FT.check_constraints(base) is None
    r = FT.FitnessResult(**{**base.__dict__, "dates": 41})
    assert FT.check_constraints(r) == FT.REASON_MIN_DATES
    r = FT.FitnessResult(**{**base.__dict__, "trades": 39})
    assert FT.check_constraints(r) == FT.REASON_MIN_TRADES
    r = FT.FitnessResult(**{**base.__dict__, "cities": 2})
    assert FT.check_constraints(r) == FT.REASON_MIN_CITIES
    r = FT.FitnessResult(**{**base.__dict__, "worst_date_pnl": -0.51})
    assert FT.check_constraints(r) == FT.REASON_WORST_DATE
    r = FT.FitnessResult(**{**base.__dict__, "n_active_clauses": 9})
    assert FT.check_constraints(r) == FT.REASON_MAX_CLAUSES
    r = FT.FitnessResult(**{**base.__dict__, "gefs_twin_realized": -0.01})
    assert FT.check_constraints(r) == FT.REASON_GEFS_TWIN
    r = FT.FitnessResult(**{**base.__dict__, "bss_trades": -0.06})
    assert FT.check_constraints(r) == FT.REASON_BSS
    r = FT.FitnessResult(**{**base.__dict__, "bss_trades": float("nan"), "gefs_twin_realized": float("nan")})
    assert FT.check_constraints(r) is None  # skipped when not computable


def test_max_clauses_constraint_via_genome():
    F = K.synthetic_frame(n_markets=40, n_snapshots=8, n_dates=5, seed=8)
    g = G.Genome.from_values(
        direction="buy_no", mode="taker", windows=(">=24h", "12-24h", "6-12h", "3-6h", "1-3h"),
        bands=("0-1F", "1-2F", "2-3F", "3-4F", "4-5F"), p_win_lo=0.5, p_win_hi=1.0, far_margin=0.0,
        quote_lo=0.02, quote_hi=0.98, sigma_cap=4.0, lead_buckets=("short", "medium"), edge_distance_lo=1,
    )
    assert G.n_active_clauses(g) == 10
    r = FT.score(F, G.to_mask(g, F), genome=g)
    if r.trades:
        assert r.constraint_reason in (
            FT.REASON_MAX_CLAUSES, FT.REASON_MIN_DATES, FT.REASON_MIN_TRADES, FT.REASON_MIN_CITIES, FT.REASON_WORST_DATE,
        )
        # with the earlier constraints satisfied by construction, MAX_CLAUSES fires
        loose = FT.FitnessResult(trades=100, dates=60, n_dates_in_mask=69, cities=4, worst_date_pnl=-0.1, n_active_clauses=10)
        assert FT.check_constraints(loose) == FT.REASON_MAX_CLAUSES


# ---------------------------------------------------------------------------
# (d) no executable market -> NO_TRADES
# ---------------------------------------------------------------------------


def test_no_executable_rows_gives_no_trades():
    F = K.synthetic_frame(n_markets=10, n_snapshots=4, n_dates=3, seed=1, executable=False)
    assert not F.visible["executable"].any()
    for g in list(G.SEEDS.values())[:3] + [G.Genome.from_values(direction="buy_no", mode="taker", sigma_cap=4.0)]:
        m = G.to_mask(g, F)
        r = FT.score(F, m, genome=g)
        assert r.trades == 0 and r.fit == float("-inf") and r.constraint_reason == FT.REASON_NO_TRADES
        assert math.isnan(r.realized) and math.isnan(r.boot_lo) and r.trade_rows.size == 0
        r2 = FT.score(F, m, constraints=False)
        assert r2.trades == 0 and r2.fit == float("-inf") and r2.constraint_reason == FT.REASON_NO_TRADES
    # all-False mask on a normal frame
    F2 = K.synthetic_frame(n_markets=6, n_snapshots=3, n_dates=2, seed=2)
    r = FT.score(F2, np.zeros(F2.n_rows, dtype=bool))
    assert r.trades == 0 and r.fit == float("-inf") and r.constraint_reason == FT.REASON_NO_TRADES
    assert math.isnan(r.fill_opportunity_rate)


# ---------------------------------------------------------------------------
# (e) frame-level Brier skill vs market
# ---------------------------------------------------------------------------


def test_frame_bss_vs_market(F):
    out = FT.frame_bss_vs_market(F)
    print("\nframe BSS vs market:", out)
    assert all(math.isfinite(out[k]) for k in ("bss", "ci_lo", "ci_hi"))
    assert out["ci_lo"] <= out["bss"] <= out["ci_hi"]
    assert out["n_rows"] > 1000 and out["n_dates"] == 69
    # deterministic
    assert FT.frame_bss_vs_market(F) == out


# ---------------------------------------------------------------------------
# date masks (folds) and the gefs twin path
# ---------------------------------------------------------------------------


def test_date_mask_matches_evaluate_shape_on_filtered_frame(F, opp):
    codes = np.arange(0, F.n_dates, 3)
    dm = FT.date_row_mask(F, codes)
    keep_dates = set(F.dates[codes].tolist())
    sub = opp[opp["target_date"].astype(str).isin(keep_dates)].copy()
    for name in ("fr31a_taker", "nofilter_no", "mlweather_fallback"):
        g = G.SEEDS[name]
        r = FT.score(F, G.to_mask(g, F), date_mask=dm, label=name, constraints=False)
        sr = FT.score_reference(sub, K.to_pandas_mask(g, sub), name)
        assert FT.compare(r, sr, tol=TOL) == [], name
        assert r.n_dates_in_mask == len(codes)
        assert set(F.dates[r.per_date_codes].tolist()) <= keep_dates


def test_gefs_twin_constraint_uses_twin_index(F):
    # a twin that is F itself with realized negated: mean twin realized < 0 -> GEFS_TWIN
    twin = K.copy_frame(F, name="gefs_twin")
    twin.hidden["realized_per_contract"] = -F.hidden["realized_per_contract"]
    F2 = K.copy_frame(F)
    F2.twin_index = np.arange(F.n_rows, dtype=np.int64)
    g = G.SEEDS["nofilter_no"]
    r = FT.score(F2, G.to_mask(g, F2), genome=g, twin=twin)
    pooled = float(F.hidden["realized_per_contract"][r.trade_rows].mean())  # per-trade mean on the twin rows
    assert abs(r.gefs_twin_realized + pooled) < 1e-12
    assert r.constraint_reason == FT.REASON_GEFS_TWIN
    r_ok = FT.score(F2, G.to_mask(g, F2), genome=g, twin=F2)
    assert abs(r_ok.gefs_twin_realized - pooled) < 1e-12 and r_ok.constraint_reason != FT.REASON_GEFS_TWIN
    # no twin -> constraint skipped
    r_none = FT.score(F2, G.to_mask(g, F2), genome=g)
    assert math.isnan(r_none.gefs_twin_realized) and r_none.constraint_reason != FT.REASON_GEFS_TWIN
