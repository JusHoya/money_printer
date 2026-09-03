"""F2 EVOLVE: determinism, resume, synthetic-edge recovery, niching, picker, worker isolation, tripwire.

Everything runs on a small synthetic 69-date FrameSet (``tests.factory_testkit
.synthetic_frame`` relabelled onto the development calendar, realized PnL
clipped to +-0.45 so the WORST_DATE constraint is satisfiable). Budgets are
tiny (population 24, generations 4, n_boot 200); pool tests use 2 workers.

    python -m pytest tests/test_factory_evolve.py -v
"""
from __future__ import annotations

import datetime
import json
import multiprocessing as mp
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pytest

from src.factory import evolve
from src.factory import fitness as FT
from src.factory import folds
from src.factory import frame as frame_mod
from src.factory import genome as G
from src.factory import guards
from src.factory import ledger as ledger_mod
from src.factory import procedure
from tests import factory_testkit as K

CFG = evolve.EvolveConfig(population=24, generations=4, n_boot=200, workers=1)
CFG_POOL = evolve.EvolveConfig(population=24, generations=4, n_boot=200, workers=2)
MASTER = 20260902
CONFIG = {"run_id": "t", "lock_sha256": "l" * 64, "git_rev": "d" * 40, "_config_sha256": "c" * 64}


# ---------------------------------------------------------------------------
# synthetic FrameSet on the development calendar
# ---------------------------------------------------------------------------
def make_fs(seed: int = 3, n_markets: int = 552) -> frame_mod.FrameSet:
    s = K.synthetic_frame(n_markets=n_markets, n_snapshots=3, n_dates=69, seed=seed, name="search")
    s.dates = np.asarray(folds.DEV_DATES, dtype=str)
    s.hidden["realized_per_contract"] = np.clip(s.hidden["realized_per_contract"], -0.45, 0.45)
    s.provenance["source"] = "gfs_mex"
    t = K.copy_frame(s, name="gefs_twin")
    s.twin_index = np.arange(s.n_rows, dtype=np.int64)
    p = K.copy_frame(s, name="parity")
    p.twin_index = None
    return frame_mod.FrameSet(parity=p, search=s, gefs_twin=t, provenance={"synthetic": True})


@pytest.fixture(scope="module")
def fs() -> frame_mod.FrameSet:
    return make_fs()


def _run(fs, run_dir: Path, *, cfg=CFG, campaigns=("A",), blocked_folds=False, resume=False, on_generation=None,
         config=None, master_seed=MASTER) -> procedure.ProcedureResult:
    return procedure.run_procedure(
        fs, dict(config or CONFIG), run_dir, campaigns=campaigns, blocked_folds=blocked_folds, cfg=cfg,
        master_seed=master_seed, frame_dir=None, resume=resume, log=lambda s: None, on_generation=on_generation,
    )


def _parquet_bytes(run_dir: Path) -> Dict[str, bytes]:
    return {p.relative_to(run_dir).as_posix(): p.read_bytes() for p in sorted(run_dir.rglob("gen_*.parquet"))}


def _pick_ids(res: procedure.ProcedureResult) -> Dict[str, Optional[str]]:
    return {k: p.genome_id for k, p in {**res.picks, **res.folds}.items()}


# ---------------------------------------------------------------------------
# seed_for
# ---------------------------------------------------------------------------
def test_seed_for_matches_contract_formula():
    import hashlib

    for ms, camp, gen in ((20260902, "A", 0), (1, "ALL69", 17), (7, "F3", 59)):
        want = int.from_bytes(hashlib.sha256(f"{ms}:{camp}:{gen}".encode()).digest()[:8], "little")
        assert evolve.seed_for(ms, camp, gen) == want
    assert evolve.seed_for(1, "A", 0) != evolve.seed_for(1, "A", 1) != evolve.seed_for(1, "B", 1)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_same_seed_byte_identical_parquet_and_pool_matches_inprocess(fs, tmp_path):
    r1 = _run(fs, tmp_path / "r1", campaigns=("A", "B"))
    r2 = _run(fs, tmp_path / "r2", campaigns=("A", "B"))
    b1, b2 = _parquet_bytes(tmp_path / "r1"), _parquet_bytes(tmp_path / "r2")
    assert set(b1) == set(b2) and len(b1) == 2 * CFG.generations
    for k in b1:
        assert b1[k] == b2[k], k
    assert _pick_ids(r1) == _pick_ids(r2)
    assert (tmp_path / "r1" / "oos" / "pooled.json").read_bytes() == (tmp_path / "r2" / "oos" / "pooled.json").read_bytes()
    assert (tmp_path / "r1" / "picks.json").read_bytes() == (tmp_path / "r2" / "picks.json").read_bytes()
    # a 2-worker pool (ordered imap, spawn or fork) reproduces the in-process bytes
    r3 = _run(fs, tmp_path / "r3", cfg=CFG_POOL, campaigns=("A", "B"))
    b3 = _parquet_bytes(tmp_path / "r3")
    for k in b1:
        assert b1[k] == b3[k], k
    assert _pick_ids(r1) == _pick_ids(r3)
    # a different master seed is a different search
    r4 = _run(fs, tmp_path / "r4", campaigns=("A",), master_seed=MASTER + 1)
    assert _parquet_bytes(tmp_path / "r4")["ledger/A/gen_000.parquet"] != b1["ledger/A/gen_000.parquet"]
    assert r4.evaluations == CFG.population * CFG.generations


def test_generation_is_pure_function_of_previous_parquet(fs, tmp_path):
    """Breeding from the ledger rows (resume path) == breeding from the in-memory population."""
    _run(fs, tmp_path / "r", campaigns=("A",))
    led = ledger_mod.Ledger(tmp_path / "r", "A")
    camp = folds.campaigns([str(d) for d in fs.search.dates])["A"]
    search, _ = folds.strip_to_campaign(fs.search, fs.gefs_twin, camp)
    for gen in range(1, CFG.generations):
        pop = evolve.individuals_from_ledger(led.read_gen(gen - 1).to_pylist(), search)
        rng = np.random.default_rng(evolve.seed_for(MASTER, "A", gen))
        genomes = evolve.breed(pop, CFG, rng, n_markets=search.n_markets)
        want = led.read_gen(gen).column("genome_json").to_pylist()
        assert [ledger_mod.genome_json(g) for g in genomes] == want, gen


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------
def test_resume_after_abort_reproduces_uninterrupted_run(fs, tmp_path):
    cfg = evolve.EvolveConfig(population=24, generations=6, n_boot=200, workers=1)
    ref = _run(fs, tmp_path / "ref", cfg=cfg, campaigns=("A", "B"))
    ref_bytes = _parquet_bytes(tmp_path / "ref")

    class Abort(RuntimeError):
        pass

    def killer(campaign, gen, info):
        if campaign == "B" and gen == 2:  # B's gen 2 is fully SCORED on disk when this fires
            raise Abort("simulated kill -9 after generation 2")

    with pytest.raises(Abort):
        _run(fs, tmp_path / "cut", cfg=cfg, campaigns=("A", "B"), on_generation=killer)
    st = json.loads((tmp_path / "cut" / "status.json").read_text())
    assert st["state"] == "FAILED" and st["campaign"] == "B" and st["gen"] == 2
    assert not (tmp_path / "cut" / "oos" / "pooled.json").exists()
    # campaign A was picked + validated before the kill; B has gens 0..2 only
    assert ledger_mod.Ledger(tmp_path / "cut", "B").generations() == [0, 1, 2]
    with pytest.raises(procedure.ProcedureError, match="never overwritten"):
        _run(fs, tmp_path / "cut", cfg=cfg, campaigns=("A", "B"))
    with pytest.raises(evolve.EvolveError, match="resume=True"):  # the campaign level refuses too
        evolve.run_campaign(fs, folds.campaigns([str(d) for d in fs.search.dates])["B"], cfg, tmp_path / "cut",
                            master_seed=MASTER, log=lambda s: None)
    res = _run(fs, tmp_path / "cut", cfg=cfg, campaigns=("A", "B"), resume=True)
    cut_bytes = _parquet_bytes(tmp_path / "cut")
    assert set(cut_bytes) == set(ref_bytes)
    for k in ref_bytes:
        assert cut_bytes[k] == ref_bytes[k], k
    assert _pick_ids(res) == _pick_ids(ref)
    assert (tmp_path / "cut" / "picks.json").read_bytes() == (tmp_path / "ref" / "picks.json").read_bytes()
    assert (tmp_path / "cut" / "oos" / "pooled.json").read_bytes() == (tmp_path / "ref" / "oos" / "pooled.json").read_bytes()
    st = json.loads((tmp_path / "cut" / "status.json").read_text())
    assert st["state"] == "DONE" and st["evaluations"] == res.evaluations == ref.evaluations


def test_resume_recomputes_unscored_generation_byte_identically(fs, tmp_path):
    cfg = evolve.EvolveConfig(population=24, generations=5, n_boot=200, workers=1)
    ref = _run(fs, tmp_path / "ref", cfg=cfg, campaigns=("A",))
    ref_bytes = _parquet_bytes(tmp_path / "ref")
    # crash between append_unscored(3) and mark_scored(3): gen 3 left UNSCORED, 4 never written
    cut = tmp_path / "cut"
    shutil.copytree(tmp_path / "ref", cut)
    led = ledger_mod.Ledger(cut, "A")
    g3 = led.read_gen(3).column("genome_json").to_pylist()
    led.gen_path(3).unlink()
    led.gen_path(4).unlink()
    led.append_unscored(3, g3)
    assert len(led.unscored()) == cfg.population
    (cut / "picks.json").unlink()
    shutil.rmtree(cut / "oos")
    res = _run(fs, cut, cfg=cfg, campaigns=("A",), resume=True)
    cut_bytes = _parquet_bytes(cut)
    for k in ref_bytes:
        assert cut_bytes[k] == ref_bytes[k], k
    assert _pick_ids(res) == _pick_ids(ref)
    assert not led.unscored()


def test_resume_is_noop_when_done_and_refuses_other_frame_or_lock(fs, tmp_path):
    _run(fs, tmp_path / "r", campaigns=("A",))
    before = _parquet_bytes(tmp_path / "r")
    calls: List[Tuple[str, int]] = []
    res = _run(fs, tmp_path / "r", campaigns=("A",), resume=True, on_generation=lambda c, g, i: calls.append((c, g)))
    assert calls == [] and _parquet_bytes(tmp_path / "r") == before
    assert res.evaluations == CFG.population * CFG.generations
    with pytest.raises(procedure.ProcedureError, match="lock_sha256"):
        _run(fs, tmp_path / "r", campaigns=("A",), resume=True, config={**CONFIG, "lock_sha256": "x" * 64})
    with pytest.raises(procedure.ProcedureError, match="frame sha256"):
        _run(make_fs(seed=4), tmp_path / "r", campaigns=("A",), resume=True)
    with pytest.raises(procedure.ProcedureError, match="never overwritten"):
        _run(fs, tmp_path / "r", campaigns=("A",))


# ---------------------------------------------------------------------------
# picks.json checkpoint before validation
# ---------------------------------------------------------------------------
def test_picks_checkpointed_with_validation_null_before_validation_scored(fs, tmp_path, monkeypatch):
    seen: List[dict] = []
    real = procedure.score_on_dates

    def spy(*a, **k):
        seen.append(json.loads((tmp_path / "r" / "picks.json").read_text()))
        raise RuntimeError("stop before validation")

    monkeypatch.setattr(procedure, "score_on_dates", spy)
    with pytest.raises(RuntimeError, match="stop before validation"):
        _run(fs, tmp_path / "r", campaigns=("A",))
    assert len(seen) == 1
    ck = seen[0]["A"]
    assert ck["validation"] is None and ck["validation_done"] is False and ck["genome_json"]
    assert ck["in_sample"]["fit"] is not None and "trade_rows" not in ck["in_sample"]
    assert "per_date_pnl" in ck["in_sample"] and "per_date_codes" in ck["in_sample"]
    monkeypatch.setattr(procedure, "score_on_dates", real)
    res = _run(fs, tmp_path / "r", campaigns=("A",), resume=True)
    final = json.loads((tmp_path / "r" / "picks.json").read_text())["A"]
    assert final["validation_done"] is True and final["validation"]["trades"] == res.picks["A"].validation.trades
    assert final["genome_id"] == ck["genome_id"]


# ---------------------------------------------------------------------------
# synthetic-edge recovery
# ---------------------------------------------------------------------------
PLANTED = G.Genome.from_values(direction="buy_no", mode="taker", bands=("3-4F", "4-5F", "5F+"), sigma_cap=4.0,
                               windows=G.ALL_WINDOWS, lead_buckets=G.ALL_LEADS)


def _plant(fs: frame_mod.FrameSet, rule: G.Genome, seed: int = 11) -> Tuple[frame_mod.FrameSet, np.ndarray]:
    """Give every buy_no row of the rule's markets a +5c..+30c edge (won w.p. 0.85), consistently in the twin."""
    rng = np.random.default_rng(seed)
    s = K.copy_frame(fs.search, name="search")
    s.provenance = dict(fs.search.provenance)
    codes = np.unique(evolve.trade_markets(rule, s))
    rows = np.flatnonzero(np.isin(s.visible["market_code"], codes) & (s.visible["direction_code"] == 1))
    won = rng.random(rows.size) < 0.85
    s.hidden["won"][rows] = won
    s.hidden["realized_per_contract"][rows] = np.where(won, 0.30, -0.20)
    s.visible["p_win"][rows] = 0.85
    t = K.copy_frame(s, name="gefs_twin")
    t.twin_index = None
    s.twin_index = np.arange(s.n_rows, dtype=np.int64)
    return frame_mod.FrameSet(parity=fs.parity, search=s, gefs_twin=t, provenance=dict(fs.provenance)), codes


def _jaccard(a: np.ndarray, b: np.ndarray) -> float:
    A, B = set(a.tolist()), set(b.tolist())
    return len(A & B) / len(A | B) if (A | B) else 1.0


def test_synthetic_edge_recovery(fs, tmp_path):
    planted_fs, planted_codes = _plant(fs, PLANTED)
    camp = folds.campaigns([str(d) for d in planted_fs.search.dates])["A"]
    search, twin = folds.strip_to_campaign(planted_fs.search, planted_fs.gefs_twin, camp)
    planted_score = evolve.score_genome(PLANTED, search, twin, n_boot=200, seed=FT.DEFAULT_SEED)
    assert planted_score.feasible and planted_score.fit > 0.05, planted_score
    cfg = evolve.EvolveConfig(population=40, generations=6, n_boot=200, workers=1)
    res = _run(planted_fs, tmp_path / "r", cfg=cfg, campaigns=("A",))
    p = res.picks["A"]
    assert p.genome is not None and p.in_sample is not None and p.in_sample.passed
    found = evolve.trade_markets(p.genome, search)
    jac = _jaccard(found, np.unique(planted_score.trade_markets))
    assert jac >= 0.5 or p.in_sample.fit >= planted_score.fit, (jac, p.in_sample.fit, planted_score.fit)
    # the edge carries into the validation block the search never saw
    assert p.validation is not None and p.validation.realized > 0


# ---------------------------------------------------------------------------
# niching, duplicate kills, ranking
# ---------------------------------------------------------------------------
def _ind(idx: int, fit: float, markets: List[int], status: str = "SCORED", trades: int = 50, n_clauses: int = 1) -> evolve.Individual:
    g = G.SEEDS["nofilter_no"].replace(sigma_cap=4.0)
    return evolve.Individual(idx=idx, genome=g, genome_json="{}", genome_id=f"id{idx:02d}", status=status, fit=fit,
                             reason="" if status == "SCORED" else "MIN_TRADES", trades=trades, phenotype_hash=f"h{idx}",
                             n_clauses=n_clauses, trade_markets=np.asarray(markets, dtype=np.int32))


def test_niche_removes_jaccard_duplicates_keeping_the_fitter():
    base = list(range(20))
    a = _ind(0, 0.05, base)  # best
    b = _ind(1, 0.04, base[:19] + [99])  # Jaccard 19/21 = 0.905 > 0.90 -> removed
    c = _ind(2, 0.03, base[:18] + [98, 97])  # 18/22 = 0.82 -> kept
    d = _ind(3, 0.02, base)  # exact duplicate of a -> removed
    e = _ind(4, float("-inf"), [], status="KILLED", trades=0)  # empty set
    f = _ind(5, float("-inf"), [], status="KILLED", trades=0)  # empty vs empty = 1.0 -> removed
    ranked = evolve.rank([f, e, d, c, b, a])
    assert [i.idx for i in ranked] == [0, 1, 2, 3, 4, 5]
    kept = evolve.niche(ranked, 0.90, n_markets=100)
    assert [i.idx for i in kept] == [0, 2, 4]
    jm = evolve.jaccard_matrix([a.trade_markets, b.trade_markets, e.trade_markets], 100)
    assert abs(jm[0, 1] - 19 / 21) < 1e-9 and jm[0, 0] == 1.0 and jm[2, 2] == 1.0 and jm[0, 2] == 0.0


def test_breed_composition_and_elites_come_from_niched_pool():
    cfg = evolve.EvolveConfig(population=20, generations=2, n_boot=100, workers=1)  # n_elite = n_immigrants = 1
    pop = [_ind(i, 0.10 - i * 0.01, list(range(i, i + 30))) for i in range(10)]
    dup = _ind(10, 0.09, list(range(0, 30)))  # same set as the best, ranked second by fit
    rng = np.random.default_rng(0)
    out = evolve.breed(pop + [dup], cfg, rng, n_markets=100)
    assert len(out) == cfg.population
    assert np.array_equal(out[0].genes, pop[0].genome.genes)  # the elite is the top of the pool
    # gene-identical rows are redrawn (best effort, REDRAW attempts): a pool of one gene family still diversifies
    assert len({g.genes.tobytes() for g in out}) >= 0.75 * len(out)
    assert all(g.is_searchable() and g.name == "" for g in out)


def test_duplicate_phenotype_kill_and_illegal_code():
    r1 = evolve.Scored(fit=0.1, constraint_reason=None, phenotype_hash="x", trades=50)
    r2 = evolve.Scored(fit=0.1, constraint_reason=None, phenotype_hash="x", trades=50)
    r3 = evolve.Scored(fit=float("-inf"), constraint_reason="MIN_TRADES", phenotype_hash="x", trades=3)
    out = evolve.apply_duplicate_kills([r1, r2, r3])
    assert out[0].constraint_reason is None and out[1].constraint_reason == evolve.KILL_DUPLICATE_PHENOTYPE
    assert out[2].constraint_reason == "MIN_TRADES"
    assert set(evolve.KILL_CODES) >= {"NO_TRADES", "BSS", "DUPLICATE_PHENOTYPE", "ILLEGAL"}
    F = K.synthetic_frame(n_markets=8, n_snapshots=2, n_dates=4, seed=1)
    maker = G.SEEDS["salvage_5f"]  # mode=maker: legal to encode, not searchable
    r = evolve.score_genome(maker, F, None, n_boot=50, seed=1)
    assert r.constraint_reason == evolve.KILL_ILLEGAL and not r.feasible


def test_ledger_rows_carry_kill_codes_and_parent_date_codes(fs, tmp_path):
    _run(fs, tmp_path / "r", campaigns=("A",))
    led = ledger_mod.Ledger(tmp_path / "r", "A")
    table = led.read_all()
    reasons = set(table.column("reason").to_pylist())
    assert reasons <= set(evolve.KILL_CODES) | {""}
    statuses = set(table.column("status").to_pylist())
    assert statuses <= {"SCORED", "KILLED"} and "SCORED" in statuses
    camp = folds.campaigns([str(d) for d in fs.search.dates])["A"]
    search_set = set(camp.search_dates)
    for codes in table.column("per_date_codes").to_pylist():
        for c in codes:
            assert str(fs.search.dates[c]) in search_set  # parent-frame codes, inside the search window only
    for i, idx in enumerate(table.column("idx").to_pylist()[: CFG.population]):
        assert idx == i  # rows sorted by idx


# ---------------------------------------------------------------------------
# picker
# ---------------------------------------------------------------------------
def _row(gen: int, idx: int, fit: float, *, genes: Optional[G.Genome] = None, status: str = "SCORED", reason: str = "") -> dict:
    g = genes or G.SEEDS["nofilter_no"].replace(sigma_cap=4.0)
    gj = ledger_mod.genome_json(g)
    return {
        "row_id": f"A/g{gen:03d}/{idx:05d}", "campaign": "A", "gen": gen, "idx": idx, "genome_id": ledger_mod.genome_id(gj),
        "genome_json": gj, "status": status, "fitness": fit, "reason": reason, "trades": 50, "dates": 20, "cities": 4,
        "realized": 0.05, "realized_se": 0.01, "t_stat": 5.0, "boot_lo": fit, "boot_hi": 0.1, "worst_date_pnl": -0.1,
        "bss_trades": 0.0, "phenotype_hash": f"ph{gen}{idx}", "per_date_pnl": [0.05], "per_date_codes": [0],
    }


def test_picker_max_boot_lo_ties_fewer_clauses_then_genome_id_and_fallbacks():
    cfg = evolve.EvolveConfig(population=20, generations=2, n_boot=100, workers=1)
    two = G.SEEDS["nofilter_no"].replace(sigma_cap=4.0)  # bands + sigma_cap + windows = 3 clauses
    one = two.replace(bands=G.ALL_BANDS, windows=G.ALL_WINDOWS)  # sigma_cap only = 1 clause
    assert G.n_active_clauses(one) < G.n_active_clauses(two)
    rows = [_row(0, i, 0.01 * i) for i in range(20)]  # gen 0: fit up to 0.19
    rows += [_row(1, 0, 0.05, genes=two), _row(1, 1, 0.05, genes=one), _row(1, 2, 0.04)]
    rows += [_row(1, i, float("-inf"), status="KILLED", reason="MIN_TRADES") for i in range(3, 20)]
    p = evolve.pick(rows, cfg)
    # final generation only (gen 1); elites = top 5% of 20 rows = 1 row -> max(1, ...) with ties by boot_lo
    assert p.picked_gen == 1 and p.reason is None
    # n_elite = max(1, round(0.05*20)) = 1: the ranked first row by fitness is idx 0 (fit 0.05, idx tie-break)
    assert p.genome_id == rows[20]["genome_id"] and p.n_candidates == 1
    # widen the elite fraction: both 0.05 rows are elites -> fewer clauses wins the tie
    cfg2 = evolve.EvolveConfig(population=20, generations=2, n_boot=100, workers=1, elite_frac=0.10)
    p2 = evolve.pick(rows, cfg2)
    assert p2.genome_id == rows[21]["genome_id"] and p2.n_candidates == 2
    # identical genes + fit among the elites -> genome_id lexical (deterministic)
    rows2 = [_row(0, 0, 0.05, genes=one), _row(0, 1, 0.05, genes=one)]
    rows2[1]["genome_id"] = "0000000000000000"
    cfg3 = evolve.EvolveConfig(population=20, generations=2, n_boot=100, workers=1, elite_frac=1.0)
    assert evolve.pick(rows2, cfg3).genome_id == "0000000000000000"
    assert evolve.pick(rows2, cfg2).genome_id == rows2[0]["genome_id"]  # one elite: the lower idx
    # no feasible elite in the final gen -> best-fit feasible row of the whole ledger
    rows3 = [_row(0, 0, 0.02), _row(0, 1, 0.07), _row(1, 0, float("-inf"), status="KILLED", reason="BSS")]
    p3 = evolve.pick(rows3, cfg)
    assert p3.reason == evolve.PICK_REASON_FALLBACK and p3.picked_gen == 0 and p3.genome_id == rows3[1]["genome_id"]
    # nothing feasible anywhere
    p4 = evolve.pick([_row(0, 0, float("-inf"), status="KILLED", reason="NO_TRADES")], cfg)
    assert p4.genome is None and p4.reason == evolve.PICK_REASON_NO_FEASIBLE
    assert evolve.pick([], cfg).reason == evolve.PICK_REASON_NO_FEASIBLE


# ---------------------------------------------------------------------------
# workers never see validation / embargo rows
# ---------------------------------------------------------------------------
def test_workers_never_see_validation_dates(fs, tmp_path, monkeypatch):
    handed: Dict[str, Tuple[str, ...]] = {}
    orig_init = evolve.Evaluator.__init__

    def spy_init(self, search, twin, cfg, *, spawn_dir=None):
        orig_init(self, search, twin, cfg, spawn_dir=spawn_dir)
        handed[search.provenance.get("campaign")] = tuple(str(d) for d in search.dates)
        if twin is not None:
            assert tuple(str(d) for d in twin.dates) == handed[search.provenance.get("campaign")]

    monkeypatch.setattr(evolve.Evaluator, "__init__", spy_init)
    on_disk: Dict[str, Tuple[str, ...]] = {}
    seen_info: Dict[str, Tuple[str, ...]] = {}

    def cb(campaign, gen, info):
        seen_info[campaign] = tuple(info["worker_dates"])
        dj = tmp_path / "r" / "frames" / campaign / "search" / "dates.json"
        if dj.exists():  # spawn: what the workers frame.load()
            on_disk[campaign] = tuple(json.loads(dj.read_text()))

    _run(fs, tmp_path / "r", cfg=CFG_POOL, campaigns=("A", "B", "C"), blocked_folds=True, on_generation=cb)
    camps = folds.campaigns([str(d) for d in fs.search.dates])
    camps.update(procedure.fold_campaigns([str(d) for d in fs.search.dates]))
    for name, camp in camps.items():
        if name == "ALL69":
            continue
        assert name in handed and name in seen_info
        for dates in (handed[name], seen_info[name]) + ((on_disk[name],) if name in on_disk else ()):
            assert set(dates) == set(camp.search_dates), name
            assert not set(dates) & set(camp.validation_dates), name
            assert not set(dates) & set(camp.embargo_dates), name
    if mp.get_all_start_methods() == ["spawn"]:
        assert on_disk  # the spawn path really wrote (and the workers loaded) stripped frames
    assert not (tmp_path / "r" / "frames").exists() or not any((tmp_path / "r" / "frames").iterdir())
    fp = json.loads((tmp_path / "r" / "oos" / "folds_pooled.json").read_text())
    assert fp["label"] == procedure.FOLDS_LABEL and fp["n_calendar_dates"] == 69


# ---------------------------------------------------------------------------
# pooled OOS
# ---------------------------------------------------------------------------
def test_pooled_oos_is_33_calendar_dates_no_zero_fill(fs, tmp_path):
    res = _run(fs, tmp_path / "r", campaigns=("A", "B", "C", "ALL69"))
    po = json.loads((tmp_path / "r" / "oos" / "pooled.json").read_text())
    assert po["n_calendar_dates"] == 33 and po["campaigns"] == ["A", "B", "C"]
    assert 0 < po["n_dates"] <= 33 and len(po["per_date"]) == po["n_dates"]
    assert all(r["trades"] >= 1 for r in po["per_date"])  # no zero-fill
    dates = [r["date"] for r in po["per_date"]]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    camps = folds.campaigns([str(d) for d in fs.search.dates])
    for r in po["per_date"]:
        assert r["date"] in camps[r["campaign"]].validation_dates
    v = np.asarray([r["pnl"] for r in po["per_date"]])
    st = procedure.pooled_stats(v, n_boot=200)
    assert po["mean"] == st["mean"] == pytest.approx(float(v.mean()))
    assert po["boot_lo"] == st["boot_lo"] and po["se"] == st["se"]
    w = np.asarray([r["trades"] for r in po["per_date"]], dtype=float)
    assert po["trade_weighted_mean"] == pytest.approx(float((v * w).sum() / w.sum()))
    assert "mean of per-date mean" in po["mean_definition"]
    # ALL69 has no validation block
    assert res.picks["ALL69"].validation is None
    assert json.loads((tmp_path / "r" / "picks.json").read_text())["ALL69"]["n_validation_dates"] == 0


def test_score_on_dates_matches_fitness_on_date_mask(fs):
    camp = folds.campaigns([str(d) for d in fs.search.dates])["A"]
    g = G.SEEDS["nofilter_no"].replace(sigma_cap=4.0)
    a = procedure.score_on_dates(fs.search, fs.gefs_twin, g, camp.validation_dates, n_boot=200, seed=1)
    b = FT.score(fs.search, G.to_mask(g, fs.search), date_mask=folds.date_mask(fs.search, camp.validation_dates),
                 twin=fs.gefs_twin, genome=g, n_boot=200, seed=1, constraints=False)
    assert FT.compare(a, b.shape_dict(), tol=0.0) == []
    assert set(str(fs.search.dates[c]) for c in a.per_date_codes) <= set(camp.validation_dates)


# ---------------------------------------------------------------------------
# artefacts are timestamp-free
# ---------------------------------------------------------------------------
TIMESTAMP_KEYS = {"ts", "timestamp", "generated_at", "created_at", "updated_at", "as_of", "now", "time", "wall_clock", "elapsed_s"}
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "/" + str(k), k, v
            yield from _walk(v, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def test_run_artefacts_are_timestamp_free_and_status_shape(fs, tmp_path):
    mirror = tmp_path / "reports" / "t" / "status.json"
    latest = tmp_path / "reports" / "latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"board": "gen0_x/board.md", "summary": "gen0_x/summary.json"}))
    cfg_doc = {**CONFIG, "_status_mirror": str(mirror), "_latest_json": str(latest)}
    _run(fs, tmp_path / "r", campaigns=("A",), config=cfg_doc)
    for name in ("status.json", "picks.json", "run.json", "folds.json", "oos/pooled.json"):
        doc = json.loads((tmp_path / "r" / name).read_text())
        for path, key, value in _walk(doc):
            assert key not in TIMESTAMP_KEYS, (name, path)
            if isinstance(value, str):
                assert not ISO_DATETIME.search(value), (name, path, value)
        assert (tmp_path / "r" / name).read_bytes().endswith(b"\n")
    st = json.loads((tmp_path / "r" / "status.json").read_text())
    assert set(st) >= {"run_id", "state", "phase", "campaign", "gen", "n_gens", "best_fit", "n_phenotypes",
                       "evaluations", "picks_done", "controls_done"}
    assert st["state"] == "DONE" and st["picks_done"] == ["A"] and st["controls_done"] == {}
    assert mirror.read_bytes() == (tmp_path / "r" / "status.json").read_bytes()
    lj = json.loads(latest.read_text())
    assert lj["active_run"] == "t" and lj["status"] == "t/status.json" and lj["board"] == "gen0_x/board.md"
    rj = json.loads((tmp_path / "r" / "run.json").read_text())
    assert rj["kind"] == "run" and rj["frames"]["search"] == frame_mod.frame_sha256(fs.search)
    assert rj["budget"]["population"] == CFG.population and rj["master_seed"] == MASTER and rj["lock_sha256"] == "l" * 64


# ---------------------------------------------------------------------------
# tripwire (FR-F2.5)
# ---------------------------------------------------------------------------
GUARDED_SRC = (
    "import datetime, time\n"
    "def now(): return datetime.datetime.now()\n"
    "def utcnow(): return datetime.datetime.utcnow()\n"
    "def today(): return datetime.datetime.today()\n"
    "def clock(): return time.time()\n"
    "def mono(): return time.monotonic()\n"
    "def ns(): return time.time_ns()\n"
)


def _compiled_as(filename: str) -> dict:
    ns: dict = {}
    exec(compile(GUARDED_SRC, filename, "exec"), ns)
    return ns


def _call_now_from_genome_file() -> str:
    """Runs INSIDE a pool worker: a datetime.now() whose code object lives in genome.py."""
    return str(_compiled_as(G.__file__)["now"]())


def _call_now_from_strategies() -> str:
    return str(_compiled_as("src/strategies/weather_strategy.py")["now"]())


def _call_now_from_elsewhere() -> str:
    return str(_compiled_as("src/factory/ledger.py")["now"]())


def _pd_now_from_features() -> str:
    import os

    ns: dict = {}
    exec(compile("import pandas as pd\ndef f(): return pd.Timestamp.now()", os.path.join("src", "factory", "features.py"), "exec"), ns)
    return str(ns["f"]())


def test_tripwire_in_process():
    guards.uninstall()
    try:
        guards.install()
        guards.install()  # idempotent
        assert guards.installed()
        ns = _compiled_as(G.__file__)
        for fn in ("now", "utcnow", "today", "clock", "mono", "ns"):
            with pytest.raises(guards.WallClockError, match="genome.py"):
                ns[fn]()
        with pytest.raises(guards.WallClockError, match="strategies"):
            _call_now_from_strategies()
        with pytest.raises(guards.WallClockError, match="features.py"):
            _pd_now_from_features()
        # everyone else keeps working, and the datetime type still behaves
        assert _call_now_from_elsewhere()
        import time

        assert time.time() > 0 and datetime.datetime.now().year >= 2026
        assert isinstance(datetime.datetime(2026, 1, 1), datetime.datetime)
        import pandas as pd

        assert isinstance(pd.Timestamp("2026-01-01"), datetime.datetime)
        assert issubclass(pd.Timestamp, datetime.datetime)
        assert datetime.datetime.fromisoformat("2026-01-01T00:00").year == 2026
    finally:
        guards.uninstall()
    assert not guards.installed()
    assert _compiled_as(G.__file__)["now"]().year >= 2026  # restored


def test_tripwire_raises_inside_worker_and_parent_sees_it():
    ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    with ctx.Pool(1, initializer=evolve._init_worker, initargs=(None, None, 50, 1)) as pool:
        with pytest.raises(guards.WallClockError, match="genome.py"):
            pool.apply(_call_now_from_genome_file)
        with pytest.raises(guards.WallClockError, match="strategies"):
            pool.apply(_call_now_from_strategies)
        assert pool.apply(_call_now_from_elsewhere)  # other callers are fine inside the worker
    assert not guards.installed() or mp.get_all_start_methods()[0] == "fork"  # parent untouched under spawn
