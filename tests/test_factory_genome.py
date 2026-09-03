"""GENE_SPEC v1 genome tests (PRD_STRATEGY_FACTORY FR-F1.2 / Phase F1 exit criteria).

Spec sanity, seeds, operators, ``to_mask`` row purity, row-permutation
invariance, hidden-column refusal at construction, truth perturbation.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from src.factory import columns as C
from src.factory import fitness as FT
from src.factory import genome as G
from tests import factory_testkit as K

SEED_NAMES = ("fr31a_taker", "fr31b", "nofilter_no", "far_yes_taker", "salvage_5f", "mlweather_fallback", "fr31a_gefs")


@pytest.fixture(scope="module")
def synth():
    return K.synthetic_frame(n_markets=24, n_snapshots=6, n_dates=6, seed=3)


@pytest.fixture(scope="module")
def pinned_frame(tmp_path_factory):
    dirs = K.pinned_dirs(tmp_path_factory)
    opp = K.build_opp(True, dirs)
    return K.opp_to_frame(opp, name="parity")


# ---------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------


def test_spec_has_13_genes_with_exact_domains():
    assert G.GENE_SPEC_VERSION == 1
    assert G.N_GENES == 13
    assert G.GENE_NAMES == (
        "direction", "mode", "windows", "bands", "p_win_lo", "p_win_hi", "far_margin",
        "quote_lo", "quote_hi", "sigma_cap", "lead_buckets", "edge_distance_lo", "entries_per_market",
    )
    s = {g.name: g for g in G.GENE_SPEC}
    assert s["direction"].domain == ("buy_yes", "buy_no")
    assert s["mode"].domain == ("taker", "maker") and s["mode"].search_codes() == (0,)
    assert s["windows"].domain == C.WINDOW_LABELS and s["windows"].n_bits == 6
    assert s["bands"].domain == C.BAND_LABELS and s["bands"].n_bits == 6
    assert s["p_win_lo"].domain == (None, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
    assert s["p_win_hi"].domain == (None, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0)
    assert s["far_margin"].domain == (None, 0.0, 0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.14, 0.16, 0.18, 0.2)
    assert s["quote_lo"].domain == (None, 0.02, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
    assert len(s["quote_lo"].domain) <= 15
    assert s["quote_hi"].domain == (None, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98)
    assert s["sigma_cap"].domain == (None, 2.0, 2.5, 3.0, 3.5, 4.0)
    assert s["sigma_cap"].off_allowed and not s["sigma_cap"].off_in_search
    assert s["sigma_cap"].search_codes() == (1, 2, 3, 4, 5)
    assert s["lead_buckets"].domain == ("short", "medium", "long")
    assert s["edge_distance_lo"].domain == (None, 1, 2, 3, 4, 5, 6)
    assert s["entries_per_market"].domain == (1,) and s["entries_per_market"].frozen
    for spec in G.GENE_SPEC:
        assert spec.kind in (G.KIND_CATEGORICAL, G.KIND_SUBSET, G.KIND_ORDINAL)


def test_seeds_exist_legal_and_roundtrip():
    assert tuple(G.SEEDS) == SEED_NAMES
    for name in SEED_NAMES:
        g = G.SEEDS[name]
        assert g.name == name
        assert g.notes == G.seed_notes[name] and g.notes
        assert g.genes.dtype == np.int16 and g.genes.shape == (13,)
        assert not g.genes.flags.writeable
        assert G.is_legal(g), name
        # encode/decode round trip
        enc = G.encode(G.decode(g.genes))
        assert np.array_equal(enc, g.genes)
        assert G.Genome(enc, source=g.source) == g
        # JSON round trip (both dict and string)
        j = g.to_json()
        assert j["gene_spec_version"] == 1 and j["name"] == name and j["source"] == g.source
        g2 = G.Genome.from_json(j)
        assert g2 == g and g2.name == name and g2.notes == g.notes and g2.source == g.source
        g3 = G.Genome.from_json(json.dumps(j))
        assert g3 == g
        assert json.loads(g.to_json_str())["encoding"] == [int(x) for x in g.genes]
    assert G.SEEDS["salvage_5f"].is_searchable() is False  # maker
    for name in SEED_NAMES:
        if name != "salvage_5f":
            # sigma_cap OFF is encoding-only: seeds are legal, not searchable
            assert G.SEEDS[name].value("sigma_cap") is G.OFF
            assert G.SEEDS[name].is_searchable() is False


def test_seed_gene_values_exact():
    v = G.SEEDS["fr31a_taker"].values()
    assert v == {
        "direction": "buy_no", "mode": "taker", "windows": (">=24h", "12-24h"), "bands": C.BAND_LABELS,
        "p_win_lo": None, "p_win_hi": None, "far_margin": 0.08, "quote_lo": None, "quote_hi": None,
        "sigma_cap": None, "lead_buckets": C.LEAD_BUCKET_LABELS, "edge_distance_lo": 4, "entries_per_market": 1,
    }
    v = G.SEEDS["fr31b"].values()
    assert (v["direction"], v["mode"], v["windows"], v["p_win_lo"]) == ("buy_yes", "taker", ("6-12h", "3-6h", "1-3h", "<1h"), 0.95)
    assert v["bands"] == C.BAND_LABELS and v["far_margin"] is None and v["edge_distance_lo"] is None
    v = G.SEEDS["nofilter_no"].values()
    assert (v["direction"], v["mode"], v["windows"], v["bands"]) == ("buy_no", "taker", (">=24h", "12-24h"), ("4-5F", "5F+"))
    assert all(v[k] is None for k in ("p_win_lo", "p_win_hi", "far_margin", "quote_lo", "quote_hi", "sigma_cap", "edge_distance_lo"))
    v = G.SEEDS["far_yes_taker"].values()
    assert (v["direction"], v["mode"], v["windows"], v["bands"]) == ("buy_yes", "taker", (">=24h", "12-24h"), ("4-5F", "5F+"))
    v = G.SEEDS["salvage_5f"].values()
    assert (v["direction"], v["mode"], v["windows"], v["bands"]) == ("buy_no", "maker", C.WINDOW_LABELS, ("5F+",))
    v = G.SEEDS["mlweather_fallback"].values()
    assert (v["direction"], v["mode"]) == ("buy_no", "taker")
    assert v["windows"] == (">=24h", "12-24h", "6-12h")
    assert v["bands"] == ("1-2F", "2-3F", "3-4F", "4-5F", "5F+")
    assert v["quote_hi"] == 0.85 and v["p_win_lo"] is None and v["far_margin"] is None
    assert "APPROXIMATION" in G.SEEDS["mlweather_fallback"].notes
    gefs = G.SEEDS["fr31a_gefs"]
    assert np.array_equal(gefs.genes, G.SEEDS["fr31a_taker"].genes) and gefs.source == "gefs"
    assert gefs != G.SEEDS["fr31a_taker"]  # source is part of identity


def test_n_active_clauses():
    assert G.n_active_clauses(G.SEEDS["fr31a_taker"]) == 3  # windows, far_margin, edge_distance
    assert G.n_active_clauses(G.SEEDS["fr31b"]) == 2
    assert G.n_active_clauses(G.SEEDS["nofilter_no"]) == 2
    assert G.n_active_clauses(G.SEEDS["salvage_5f"]) == 1
    assert G.n_active_clauses(G.SEEDS["mlweather_fallback"]) == 3
    full = G.Genome.from_values(direction="buy_no", mode="taker")
    assert G.n_active_clauses(full) == 0 and full.value("windows") == C.WINDOW_LABELS


def test_encode_rejects_bad_values():
    with pytest.raises(ValueError):
        G.Genome.from_values(sigma_cap=9.9)
    with pytest.raises(ValueError):
        G.Genome(np.zeros(13, dtype=np.int16))  # windows bitmask 0 = empty subset
    with pytest.raises(ValueError):
        G.Genome(np.zeros(12, dtype=np.int16))
    with pytest.raises(KeyError):
        G.encode({"cities": ("NY",)})
    with pytest.raises(ValueError):
        G.Genome.from_json({"gene_spec_version": 2, "genes": {}})


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------


def test_random_1000_legal_and_operators_keep_legality():
    rng = np.random.default_rng(20260902)
    pop = [G.Genome.random(rng) for _ in range(1000)]
    for g in pop:
        assert G.is_legal(g) and G.is_searchable(g)
        assert g.value("mode") == "taker" and g.value("sigma_cap") is not None
    modes = {g.value("mode") for g in pop}
    assert modes == {"taker"}
    assert {g.value("direction") for g in pop} == {"buy_yes", "buy_no"}
    changed = 0
    for i in range(1000):
        a, b = pop[i], pop[(i * 7 + 1) % 1000]
        m = G.mutate(a, rng)
        assert G.is_legal(m) and G.is_searchable(m)
        changed += int(m != a)
        c = G.crossover(a, b, rng)
        assert G.is_legal(c) and G.is_searchable(c)
        for k in range(G.N_GENES):
            assert c.genes[k] in (a.genes[k], b.genes[k]) or G.GENE_NAMES[k] in ("p_win_hi", "quote_hi")
        cm = G.repair(G.mutate(c, rng), rng)
        assert G.is_legal(cm) and G.is_searchable(cm)
    assert 300 < changed < 1000  # per-gene rate 1/L: most children differ somewhere


def test_repair_fixes_each_violation():
    rng = np.random.default_rng(5)
    bad = np.array(G.SEEDS["fr31a_taker"].genes, dtype=np.int16)
    bad[G.GENE_INDEX["mode"]] = 1  # maker
    bad[G.GENE_INDEX["p_win_lo"]] = 10  # 0.95
    bad[G.GENE_INDEX["p_win_hi"]] = 1  # 0.60 < lo
    bad[G.GENE_INDEX["quote_lo"]] = 11  # 0.50
    bad[G.GENE_INDEX["quote_hi"]] = 1  # 0.10 < lo
    g = G.Genome(bad)
    assert G.is_legal(g) is False and G.is_searchable(g) is False
    r = G.repair(g, rng)
    assert G.is_legal(r) and G.is_searchable(r)
    assert r.value("mode") == "taker" and r.value("sigma_cap") is not None
    v = r.values()
    assert v["p_win_hi"] is None or v["p_win_hi"] >= v["p_win_lo"]
    assert v["quote_hi"] is None or v["quote_hi"] >= v["quote_lo"]
    # subsets never empty after mutation
    one_bit = G.Genome.from_values(windows=("<1h",), bands=("5F+",), lead_buckets=("long",), sigma_cap=3.0)
    for _ in range(300):
        m = G.mutate(one_bit, rng)
        assert all(len(m.value(k)) >= 1 for k in ("windows", "bands", "lead_buckets"))


# ---------------------------------------------------------------------------
# to_mask: purity, permutation, hidden columns
# ---------------------------------------------------------------------------


def _sample_rows(F: C.Frame, rng: np.random.Generator, n: int = 400) -> np.ndarray:
    return np.unique(np.concatenate([rng.integers(0, F.n_rows, size=n), [0, F.n_rows - 1]]))


def test_to_mask_frame_equals_row_view_synthetic(synth):
    rng = np.random.default_rng(11)
    genomes = list(G.SEEDS.values()) + [G.Genome.random(rng) for _ in range(60)]
    rows = _sample_rows(synth, rng, 300)
    for g in genomes:
        m = G.to_mask(g, synth)
        assert m.dtype == bool and m.shape == (synth.n_rows,)
        for i in rows:
            r = G.to_mask(g, C.row_view(synth, int(i)))
            assert isinstance(r, np.bool_)
            assert bool(r) == bool(m[i]), (g, i)
        # VisibleOnly-wrapped row and a plain dict give the same answer
        i = int(rows[0])
        assert bool(G.to_mask(g, C.VisibleOnly(C.row_view(synth, i)))) == bool(m[i])


def test_to_mask_frame_equals_row_view_pinned(pinned_frame):
    F = pinned_frame
    rng = np.random.default_rng(12)
    rows = _sample_rows(F, rng, 500)
    for name, g in G.SEEDS.items():
        m = G.to_mask(g, F)
        assert m.any(), name
        trade_rows = FT.score(F, m, constraints=False).trade_rows
        check = np.unique(np.concatenate([rows, trade_rows[:50]]))
        for i in check:
            assert bool(G.to_mask(g, C.row_view(F, int(i)))) == bool(m[i]), (name, i)


def test_row_permutation_invariance(synth):
    rng = np.random.default_rng(21)
    perm = rng.permutation(synth.n_rows)
    F2, new_index = K.permute_rows(synth, perm)
    genomes = list(G.SEEDS.values()) + [G.Genome.random(rng) for _ in range(30)]
    for g in genomes:
        m1 = G.to_mask(g, synth)
        m2 = G.to_mask(g, F2)
        assert np.array_equal(m2[new_index], m1)
        r1 = FT.score(synth, m1, constraints=False)
        r2 = FT.score(F2, m2, constraints=False)
        assert r1.trades == r2.trades and r1.dates == r2.dates
        assert np.array_equal(np.sort(new_index[r1.trade_rows]), np.sort(r2.trade_rows))
        assert r1.phenotype_hash == r2.phenotype_hash
        if r1.trades:
            assert abs(r1.realized - r2.realized) < 1e-12 and abs(r1.boot_lo - r2.boot_lo) < 1e-12


def test_hidden_column_reference_fails_at_construction(synth):
    for hidden in C.HIDDEN_COLUMNS:
        with pytest.raises(C.HiddenColumnError):
            G.Predicate(hidden, "ge", 0.0)
        with pytest.raises(C.HiddenColumnError):
            G.Predicate("p_yes", "le_diff", 0.0, other=hidden)
    with pytest.raises(KeyError):
        G.Predicate("not_a_column", "eq", 1)
    with pytest.raises(C.HiddenColumnError):
        synth.col("won")
    with pytest.raises(C.HiddenColumnError):
        C.VisibleOnly(C.row_view(synth, 0))["realized_per_contract"]
    # a genome whose predicate tuple is tampered with still cannot construct a hidden predicate
    g = G.SEEDS["fr31a_taker"]
    assert all(p.column in C.VISIBLE_DTYPES and (p.other is None or p.other in C.VISIBLE_DTYPES) for p in g.predicates)
    row = C.row_view(synth, 0)
    assert set(row) == set(C.VISIBLE_COLUMNS) and not (set(row) & set(C.HIDDEN_COLUMNS))
    # every genome predicate evaluates on the visible-only row (no hidden name reachable)
    assert isinstance(G.to_mask(g, row), np.bool_)


def test_truth_perturbation_leaves_trade_sets_unchanged(pinned_frame):
    F = pinned_frame
    rng = np.random.default_rng(99)
    F2 = K.copy_frame(F, name="perturbed")
    perm = rng.permutation(F.n_rows)
    for col in ("won", "realized_per_contract", "settles_yes", "ev_per_contract", "result_code"):
        F2.hidden[col] = F.hidden[col][perm]
    for name, g in G.SEEDS.items():
        r1 = FT.score(F, G.to_mask(g, F), constraints=False)
        r2 = FT.score(F2, G.to_mask(g, F2), constraints=False)
        assert np.array_equal(r1.trade_rows, r2.trade_rows), name
        assert r1.trades == r2.trades and r1.dates == r2.dates and r1.markets == r2.markets
        assert r1.phenotype_hash == r2.phenotype_hash == G.phenotype_hash(g, F) == G.phenotype_hash(g, F2)
        assert r1.fill_opportunity_rate == r2.fill_opportunity_rate
        assert r1.mean_price_paid == r2.mean_price_paid and r1.mean_market_yes_ask == r2.mean_market_yes_ask
    # the perturbation did change the outcome column the scorer reads (sanity)
    r_a = FT.score(F, G.to_mask(G.SEEDS["nofilter_no"], F), constraints=False)
    r_b = FT.score(F2, G.to_mask(G.SEEDS["nofilter_no"], F2), constraints=False)
    assert r_a.realized != r_b.realized


def test_phenotype_hash_properties(synth):
    rng = np.random.default_rng(4)
    g = G.SEEDS["nofilter_no"]
    h = G.phenotype_hash(g, synth)
    assert len(h) == 40 and h == G.phenotype_hash(g, synth)
    F2 = K.copy_frame(synth)
    perm = rng.permutation(synth.n_rows)
    F2.hidden["won"] = synth.hidden["won"][perm]
    F2.hidden["realized_per_contract"] = synth.hidden["realized_per_contract"][perm]
    assert G.phenotype_hash(g, F2) == h
    assert FT.score(synth, G.to_mask(g, synth), constraints=False).phenotype_hash == h
    # a date mask restricting to no dates hashes the empty set
    empty = np.zeros(synth.n_rows, dtype=bool)
    assert G.phenotype_hash(g, synth, date_mask=empty) == G.phenotype_hash_from_tickers(np.zeros(0, dtype=str))
    # the hash is a function of the traded market SET only: equal sets <-> equal hashes
    for other in (G.SEEDS["far_yes_taker"], G.SEEDS["nofilter_no"].replace(bands=("5F+",)), G.SEEDS["fr31a_taker"]):
        s_g = set(synth.visible["market_code"][FT.score(synth, G.to_mask(g, synth), constraints=False).trade_rows].tolist())
        s_o = set(synth.visible["market_code"][FT.score(synth, G.to_mask(other, synth), constraints=False).trade_rows].tolist())
        assert (G.phenotype_hash(other, synth) == h) == (s_g == s_o)


def test_first_true_per_block():
    bs = np.array([0, 3, 3, 7, 9])
    M = np.array([0, 1, 1, 0, 0, 1, 1, 0, 1], dtype=bool)
    assert G.first_true_per_block(M, bs).tolist() == [1, 5, 8]
    assert G.first_true_per_block(np.zeros(9, dtype=bool), bs).size == 0
