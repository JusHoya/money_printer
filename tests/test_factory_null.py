"""``src.factory.null`` control-frame properties on a synthetic FrameSet and (when present) the frozen real frame."""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from src.factory import fitness as FT
from src.factory import folds
from src.factory import genome as G
from src.factory import null as NULL
from tests import factory_stats_testkit as SK

CITIES = 4


@pytest.fixture(scope="module")
def fs_syn():
    return SK.synthetic_dev_frameset(n_per_city_date=2, n_snapshots=5, seed=11)


@pytest.fixture(scope="module")
def fs_real():
    t0 = time.time()
    fs = SK.load_real_frameset()
    if fs is None:
        pytest.skip("frozen real frame not on this machine")
    if time.time() - t0 > 10:
        pytest.skip("real frame loads too slowly for the suite")
    return fs


def _visible_identical(a, b):
    return all(np.array_equal(a.visible[k], b.visible[k], equal_nan=True) for k in a.visible)


def _hidden_consistent(F):
    """The frame's own formulas hold on every row."""
    v, h = F.visible, F.hidden
    assert np.array_equal(h["won"], h["settles_yes"] == (v["direction_code"] == 0))
    assert np.array_equal(h["result_code"], h["settles_yes"].astype(np.int16))
    r = h["won"].astype(np.float64) - v["price_paid"] - v["fee_per_contract"]
    ex = v["executable"]
    assert np.array_equal(r[ex], h["realized_per_contract"][ex])
    assert np.all(np.isnan(h["realized_per_contract"][~ex]))
    F.validate()


# ---------------------------------------------------------------------------
# payoff mirror
# ---------------------------------------------------------------------------
def test_payoff_matches_bracket_payoff():
    from src.core.bracket_payoff import BracketSpec, settles_yes

    rng = np.random.default_rng(1)
    for _ in range(300):
        st = int(rng.integers(0, 3))
        fl = float(rng.integers(60, 100))
        cp = fl + 1.0
        high = float(rng.integers(55, 105))
        spec = BracketSpec(ticker="KXHIGHNY-TEST", strike_type=("between", "less", "greater")[st],
                           floor_strike=fl if st != 1 else None, cap_strike=cp if st != 2 else None)
        want = settles_yes(spec, high)
        got = NULL.payoff_settles_yes(np.array([fl if st != 1 else np.nan]), np.array([cp if st != 2 else np.nan]), np.array([st]), np.array([high]))[0]
        assert bool(got) == bool(want), (st, fl, cp, high)


def test_control_seed_is_stable():
    assert NULL.control_seed(20260902, "snapshot", 0) == NULL.control_seed(20260902, "snapshot", 0)
    assert NULL.control_seed(20260902, "snapshot", 0) != NULL.control_seed(20260902, "snapshot", 1)
    assert NULL.control_seed(20260902, "snapshot", 0) != NULL.control_seed(20260902, "residual", 0)


# ---------------------------------------------------------------------------
# snapshot-efficient null
# ---------------------------------------------------------------------------
def _check_snapshot(fs, seed):
    out = NULL.snapshot_efficient(fs, seed)
    S, T = out.search, out.gefs_twin
    assert out.parity is fs.parity
    # visible columns bit-identical to the two-sided rows of the source
    keep = NULL.two_sided(fs.search)
    for k in fs.search.visible:
        if k in ("market_code", "target_date_code"):
            continue  # re-densified by strip_rows
        assert np.array_equal(fs.search.visible[k][keep], S.visible[k], equal_nan=True), k
    assert np.all(NULL.two_sided(S)) and np.all(NULL.two_sided(T))
    assert S.n_rows == int(keep.sum())
    _hidden_consistent(S)
    _hidden_consistent(T)
    # matched twin rows share the draw
    ok = S.twin_index >= 0
    assert np.array_equal(T.hidden["won"][S.twin_index[ok]], S.hidden["won"][ok])
    # every row's expected realized is -(half spread + adverse + fee): the realised mean matches within 4 se
    p = NULL.market_side_prob(S)
    ex = S.visible["executable"]
    expected = (p - S.visible["price_paid"] - S.visible["fee_per_contract"])[ex]
    got = S.hidden["realized_per_contract"][ex]
    se = math.sqrt(float((p[ex] * (1 - p[ex])).sum())) / ex.sum()
    assert abs(float(got.mean()) - float(expected.mean())) < 4 * se + 1e-9
    # and per genome (a few random ones plus the seeds): realized mean ~ expected mean on its trade rows
    rng = np.random.default_rng(seed)
    gens = [G.SEEDS["nofilter_no"], G.SEEDS["far_yes_taker"], G.SEEDS["mlweather_fallback"]] + [G.Genome.random(rng) for _ in range(6)]
    checked = 0
    for g in gens:
        r = FT.score(S, G.to_mask(g, S), constraints=False)
        if r.trades < 30:
            continue
        rows = r.trade_rows
        exp = float((p[rows] - S.visible["price_paid"][rows] - S.visible["fee_per_contract"][rows]).mean())
        se_g = math.sqrt(float((p[rows] * (1 - p[rows])).sum())) / rows.shape[0]
        assert abs(r.realized - exp) < 4 * se_g + 1e-9, (g.describe(), r.realized, exp, se_g)
        assert exp < 0  # no rule has edge under this null
        checked += 1
    assert checked >= 3
    # determinism
    out2 = NULL.snapshot_efficient(fs, seed)
    assert np.array_equal(out2.search.hidden["won"], S.hidden["won"])
    assert out.provenance["control"]["kind"] == "snapshot"
    return out


def test_snapshot_efficient_synthetic(fs_syn):
    _check_snapshot(fs_syn, 7)


def test_snapshot_efficient_real(fs_real):
    out = _check_snapshot(fs_real, 20260902)
    assert out.search.n_rows == 125792 and out.provenance["control"]["search_rows_dropped"] == 90844


# ---------------------------------------------------------------------------
# residual-shuffle null
# ---------------------------------------------------------------------------
def _check_residual(fs, seed):
    out = NULL.residual_shuffle(fs, seed)
    S, T = out.search, out.gefs_twin
    assert out.parity is fs.parity
    assert _visible_identical(fs.search, S) and _visible_identical(fs.gefs_twin, T)
    assert all(S.visible[k] is fs.search.visible[k] for k in S.visible)  # shared, never written
    _hidden_consistent(S)
    _hidden_consistent(T)
    # per-city multiset of whole-degree residuals preserved; shifted, not identity
    inv, uk, mu = NULL._city_day_table(fs.search)
    base = np.floor(mu + 0.5)
    h0, h1 = NULL.city_day_high(fs.search), NULL.city_day_high(S)
    moved = 0
    for c in range(CITIES):
        sel = np.flatnonzero(uk // fs.search.n_dates == c)
        r0 = sorted(float(h0[inv == k][0] - base[k]) for k in sel if np.isfinite(h0[inv == k][0]))
        r1 = sorted(float(h1[inv == k][0] - base[k]) for k in sel if np.isfinite(h1[inv == k][0]))
        assert r0 == r1, c
        moved += sum(1 for k in sel if h0[inv == k][0] != h1[inv == k][0])
    assert moved > 0
    # the new high is constant within a city-day and (when the source highs are whole degrees, as
    # Kalshi settles them) stays on the whole-degree grid; the twin shares it
    integral_source = all(float(x) == math.floor(float(x)) for x in h0 if np.isfinite(x))
    for k in range(uk.shape[0]):
        vals = np.unique(h1[inv == k])
        assert vals.shape[0] == 1
        if integral_source:
            assert float(vals[0]) == math.floor(float(vals[0]))
    tiT = fs.search.twin_index
    ok = tiT >= 0
    assert np.array_equal(T.hidden["settles_yes"][tiT[ok]], S.hidden["settles_yes"][ok])
    # settlement recomputed from the new high with the payoff rule
    v = S.visible
    assert np.array_equal(S.hidden["settles_yes"], NULL.payoff_settles_yes(v["floor_strike"], v["cap_strike"], v["strike_type_code"], h1))
    out2 = NULL.residual_shuffle(fs, seed)
    assert np.array_equal(out2.search.hidden["cli_high"], S.hidden["cli_high"], equal_nan=True)
    return out


def test_residual_shuffle_synthetic(fs_syn):
    _check_residual(fs_syn, 3)


def test_residual_shuffle_real(fs_real):
    out = _check_residual(fs_real, 20260902)
    assert out.provenance["control"]["city_days_shifted"] == 253


# ---------------------------------------------------------------------------
# planted edge
# ---------------------------------------------------------------------------
def _check_planted(fs, seed, rule=NULL.PLANTED_RULE, edge=0.05):
    out, info = NULL.planted_edge(fs, seed, rule=rule, edge=edge)
    S, T = out.search, out.gefs_twin
    assert out.parity is fs.parity
    assert _visible_identical(fs.search, S) and all(S.visible[k] is fs.search.visible[k] for k in S.visible)
    _hidden_consistent(S)
    _hidden_consistent(T)
    v = fs.search.visible
    mask = G.to_mask(rule, fs.search)
    rows = np.flatnonzero(mask & v["executable"] & np.isfinite(fs.search.hidden["realized_per_contract"]))
    before = fs.search.hidden["realized_per_contract"]
    after = S.hidden["realized_per_contract"]
    delta = float(np.mean(after[rows] - before[rows]))
    assert abs(delta - edge) <= 0.002, (delta, info["n_rule_rows"], info["n_flipped"])
    assert info["n_flipped"] == round(edge * info["n_rule_rows"])
    # nothing else changed: every non-flipped row is bit-identical in every hidden column
    flipped = np.flatnonzero(S.hidden["won"] != fs.search.hidden["won"])
    assert flipped.shape[0] == info["n_flipped"] and set(flipped.tolist()) <= set(rows.tolist())
    keep = np.ones(fs.search.n_rows, dtype=bool)
    keep[flipped] = False
    for k in fs.search.hidden:
        assert np.array_equal(fs.search.hidden[k][keep], S.hidden[k][keep], equal_nan=True), k
    assert np.all(after[flipped] - before[flipped] == 1.0)
    # twin matched rows flipped identically
    ti = fs.search.twin_index
    tf = ti[flipped]
    tf = tf[tf >= 0]
    assert np.all(T.hidden["won"][tf]) and info["twin_rows_flipped"] == tf.shape[0]
    # the rule's own validation delta is close to the edge in every block, and in the pooled 33 dates
    for c in ("A", "B", "C"):
        w = info["windows"][c]
        if w["validation"] and w["validation"]["n_trade_rows"] >= 20:
            assert abs(w["validation"]["delta_trade_rows"] - edge) < 0.02, (c, w["validation"])
    if info["rule_pooled_validation_delta"] is not None:
        assert abs(info["rule_pooled_validation_delta"] - edge) < 0.02
    return out, info


def test_planted_edge_synthetic(fs_syn):
    # the real PLANTED_RULE may select few rows on a synthetic frame; use a broad 3-clause rule there
    rule = G.Genome.from_values(name="syn", direction="buy_no", mode="taker", windows=(">=24h", "12-24h", "6-12h"),
                                bands=("2-3F", "3-4F", "4-5F", "5F+"), sigma_cap=4.0, lead_buckets=("short", "medium", "long"))
    out, info = _check_planted(fs_syn, 5, rule=rule)
    assert info["n_rule_rows"] >= 250


def test_planted_edge_real(fs_real):
    out, info = _check_planted(fs_real, 20260902)
    assert info["n_rule_rows"] == 3455 and info["n_trade_rows"] == 392
    camps = folds.campaigns([str(d) for d in fs_real.search.dates])
    # PLANTED_RULE: >= 40 trades on every campaign search window and trades in every validation block
    for c, camp in camps.items():
        w = info["windows"][c]
        assert w["search"]["rule_trades"] >= 40, c
        if camp.validation_dates:
            assert w["validation"]["rule_trades"] > 0, c
    assert G.n_active_clauses(NULL.PLANTED_RULE) == 3 and NULL.PLANTED_RULE.is_searchable()


def test_make_control_frames_dispatch(fs_syn):
    for kind in NULL.KINDS:
        out, info = NULL.make_control_frames(fs_syn, kind, 1)
        assert info["kind"] == kind and out.provenance["control"]["kind"] == kind
    with pytest.raises(ValueError):
        NULL.make_control_frames(fs_syn, "bogus", 1)
