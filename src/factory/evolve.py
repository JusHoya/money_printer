"""Evolutionary search over GENE_SPEC v1 genomes (PRD_STRATEGY_FACTORY FR-F2.1).

Design record: ``docs/factory/FACTORY_ROADMAP.md`` section F2 bullet 1 and
``docs/factory/FACTORY_ARCHITECTURE.md`` sections 5 (fitness), 6.1 (campaign
frames), 7.1 (process model), 7.3 (reproducibility, resumability).

Algorithm (one campaign)
------------------------
* Population ``mu`` (400). Generation 0 = the searchable gen-0 seeds
  (``genome.SEEDS`` with ``mode == taker`` and the campaign's ``source``;
  ``sigma_cap`` OFF is replaced by the frame's cap 4.0, which is a no-op
  predicate on the search frame, so the seed's phenotype is unchanged) followed
  by ``Genome.random`` draws. Every genome enters the population nameless
  (``genome_id`` depends on genes + source only).
* Generation ``N + 1`` is a PURE FUNCTION of ``gen_N.parquet`` and
  ``seed_for(master_seed, campaign, N + 1)``:

  1. rank the rows: SCORED rows by fitness (desc), ties fewer clauses then
     idx; KILLED rows after them by ``trades`` (desc) then idx;
  2. phenotype niching: walk the ranked list and drop any row whose trade
     market-set has Jaccard > ``niche_jaccard`` (0.90) with an already kept
     row -- the breeding pool;
  3. elites = the first ``round(elite_frac * mu)`` pool members with finite
     fitness, copied unchanged; immigrants = ``round(immigrant_frac * mu)``
     fresh ``Genome.random`` draws; children fill the rest: two tournaments of
     size ``tournament`` over the pool (lower rank wins), uniform crossover,
     per-gene mutation at 1/L with legality repair (``genome.crossover`` /
     ``genome.mutate`` / ``genome.repair``). A child/immigrant whose genes
     already appear in the new generation is redrawn up to ``REDRAW`` times.

* Write-then-evaluate: ``Ledger.append_unscored(N, genomes)`` BEFORE the
  pool scores anything, ``Ledger.mark_scored(N, results)`` after -- both
  atomic; rows sorted by ``idx``. ``status.json`` (timestamp-free) after
  every generation.
* Kill codes in the ledger ``reason`` column: the fitness constraint codes,
  ``DUPLICATE_PHENOTYPE`` (a later row of the same generation with the same
  phenotype hash as an earlier constraint-satisfying row) and ``ILLEGAL``
  (a genome that is not searchable -- can only come from a hand-edited
  ledger).
* Resume: the restart point is the last generation whose rows are all
  SCORED/KILLED; a generation file with UNSCORED rows is recomputed from the
  previous generation and reproduces byte-identically (the previous
  generation's trade sets, needed for niching, are recomputed in the parent
  from the genomes and the frame -- scoring is deterministic).

Process model
-------------
Workers score ``fitness.score(F, to_mask(g, F), twin=..., genome=g, n_boot)``
and return a compact :class:`Scored` (no ``trade_rows``; the trade market
codes travel for niching, the per-date vectors for the ledger). Results are
collected with ordered ``imap`` (deterministic). ``guards.install()`` runs in
every worker initializer. Frames are stripped to the campaign's search
window with ``folds.strip_to_campaign`` BEFORE the pool is created:

* fork (alcyone): the stripped frames sit in the module globals ``_FRAME`` /
  ``_TWIN`` and are inherited copy-on-write;
* spawn (Windows): the stripped frames are saved with ``frame.save`` under
  ``<run_dir>/frames/<campaign>/{search,gefs_twin}`` and every worker loads
  them in its initializer. The original ``frame_dir`` is NEVER loaded by a
  worker (it holds the validation rows); it is provenance only.

The ledger's ``per_date_codes`` are indices into the PARENT search frame's
``Frame.dates`` (the campaign frame's ``provenance["parent_dates"]``), not the
stripped frame's, so vectors from different campaigns align on one calendar.
"""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing as mp
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

from src.factory import fitness
from src.factory import folds
from src.factory import genome as G
from src.factory import guards
from src.factory import ledger as ledger_mod
from src.factory.columns import Frame
from src.factory.report import write_json

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
KILL_DUPLICATE_PHENOTYPE = "DUPLICATE_PHENOTYPE"
KILL_ILLEGAL = "ILLEGAL"
#: every reason code that can appear in the ledger ``reason`` column
KILL_CODES: Tuple[str, ...] = (
    fitness.REASON_NO_TRADES,
    fitness.REASON_MIN_TRADES,
    fitness.REASON_MIN_DATES,
    fitness.REASON_MIN_CITIES,
    fitness.REASON_WORST_DATE,
    fitness.REASON_MAX_CLAUSES,
    fitness.REASON_GEFS_TWIN,
    fitness.REASON_BSS,
    KILL_DUPLICATE_PHENOTYPE,
    KILL_ILLEGAL,
)
REDRAW = 8  # attempts to avoid a gene-identical child/immigrant within a generation
STATE_RUNNING = "RUNNING"
STATE_DONE = "DONE"
STATE_FAILED = "FAILED"


class EvolveError(RuntimeError):
    """A campaign precondition failed (loud, PRD_STRATEGY_FACTORY section 6)."""


def seed_for(master_seed: int, campaign: str, gen: int) -> int:
    """Per-generation RNG seed: first 8 bytes (little-endian) of sha256(``"{master}:{campaign}:{gen}"``)."""
    payload = f"{int(master_seed)}:{campaign}:{int(gen)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvolveConfig:
    population: int = 400
    generations: int = 60
    tournament: int = 4
    elite_frac: float = 0.05
    immigrant_frac: float = 0.05
    niche_jaccard: float = 0.90
    n_boot: int = 4000
    workers: int = 16
    boot_seed: int = fitness.DEFAULT_SEED
    chunksize: int = 0  # 0 -> population // (4 * workers), at least 1

    def __post_init__(self) -> None:
        if self.population < 2:
            raise ValueError("population must be >= 2")
        if self.generations < 1:
            raise ValueError("generations must be >= 1")
        if self.tournament < 1:
            raise ValueError("tournament must be >= 1")

    @property
    def n_elite(self) -> int:
        return int(round(self.elite_frac * self.population))

    @property
    def n_immigrants(self) -> int:
        return int(round(self.immigrant_frac * self.population))

    def effective_chunksize(self) -> int:
        if self.chunksize > 0:
            return int(self.chunksize)
        return max(1, self.population // (4 * max(1, self.workers)))

    def as_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ---------------------------------------------------------------------------
# worker side
# ---------------------------------------------------------------------------
@dataclass
class Scored:
    """Compact worker result: ``FitnessResult`` minus ``trade_rows`` plus the trade market codes.

    ``per_date_codes`` are indices into the SCORING frame's dates when the
    worker returns them; :func:`_realign` maps them to the parent frame before
    they reach the ledger. ``trade_markets`` are the scoring frame's dense
    ``market_code`` values (one per trade).
    """

    fit: float = fitness.NEG_INF
    constraint_reason: Optional[str] = None
    trades: int = 0
    markets: int = 0
    dates: int = 0
    cities: int = 0
    city_days: int = 0
    realized: float = fitness.NAN
    realized_se: float = fitness.NAN
    t_stat: float = fitness.NAN
    boot_lo: float = fitness.NAN
    boot_hi: float = fitness.NAN
    win_rate: float = fitness.NAN
    worst_date_pnl: float = fitness.NAN
    losing_dates: int = 0
    bss_trades: float = fitness.NAN
    gefs_twin_realized: float = fitness.NAN
    modelled_ev: float = fitness.NAN
    fill_opportunity_rate: float = fitness.NAN
    mean_price_paid: float = fitness.NAN
    mean_fee: float = fitness.NAN
    n_dates_in_mask: int = 0
    n_active_clauses: Optional[int] = None
    phenotype_hash: str = ""
    per_date_pnl: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    per_date_codes: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))
    trade_markets: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))

    @property
    def feasible(self) -> bool:
        return self.constraint_reason is None and math.isfinite(self.fit)

    @classmethod
    def from_result(cls, r: fitness.FitnessResult, F: Frame) -> "Scored":
        rows = np.asarray(r.trade_rows, dtype=np.int64)
        return cls(
            fit=float(r.fit),
            constraint_reason=r.constraint_reason,
            trades=int(r.trades),
            markets=int(r.markets),
            dates=int(r.dates),
            cities=int(r.cities),
            city_days=int(r.city_days),
            realized=float(r.realized),
            realized_se=float(r.realized_se),
            t_stat=float(r.t_stat),
            boot_lo=float(r.boot_lo),
            boot_hi=float(r.boot_hi),
            win_rate=float(r.win_rate),
            worst_date_pnl=float(r.worst_date_pnl),
            losing_dates=int(r.losing_dates),
            bss_trades=float(r.bss_trades),
            gefs_twin_realized=float(r.gefs_twin_realized),
            modelled_ev=float(r.modelled_ev),
            fill_opportunity_rate=float(r.fill_opportunity_rate),
            mean_price_paid=float(r.mean_price_paid),
            mean_fee=float(r.mean_fee),
            n_dates_in_mask=int(r.n_dates_in_mask),
            n_active_clauses=r.n_active_clauses,
            phenotype_hash=str(r.phenotype_hash),
            per_date_pnl=np.asarray(r.per_date_pnl, dtype=np.float64),
            per_date_codes=np.asarray(r.per_date_codes, dtype=np.int16),
            trade_markets=np.asarray(F.visible["market_code"][rows], dtype=np.int32) if rows.size else np.zeros(0, dtype=np.int32),
        )

    def as_row(self) -> Dict[str, Any]:
        """JSON-safe dict (arrays -> lists)."""
        out: Dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, np.ndarray):
                v = v.tolist()
            elif isinstance(v, np.generic):
                v = v.item()
            out[f.name] = v
        return out


_FRAME: Optional[Frame] = None
_TWIN: Optional[Frame] = None
_N_BOOT: int = fitness.DEFAULT_N_BOOT
_SEED: int = fitness.DEFAULT_SEED


def _init_worker(search_dir: Optional[str], twin_dir: Optional[str], n_boot: int, seed: int) -> None:
    """Pool initializer: tripwire first, then the campaign frames (spawn loads from disk)."""
    global _FRAME, _TWIN, _N_BOOT, _SEED
    guards.install()
    _N_BOOT = int(n_boot)
    _SEED = int(seed)
    if _FRAME is None and search_dir:
        from src.factory import frame as frame_mod

        _FRAME = frame_mod.load(search_dir)
        _TWIN = frame_mod.load(twin_dir) if twin_dir else None


def score_genome(g: G.Genome, F: Frame, twin: Optional[Frame], *, n_boot: int, seed: int) -> Scored:
    """THE scoring path: ``fitness.score(F, to_mask(g, F), twin, genome=g)`` -> :class:`Scored`."""
    if not G.is_searchable(g):
        return Scored(constraint_reason=KILL_ILLEGAL, n_active_clauses=G.n_active_clauses(g))
    mask = G.to_mask(g, F)
    res = fitness.score(F, mask, twin=twin, genome=g, n_boot=n_boot, seed=seed, constraints=True)
    return Scored.from_result(res, F)


def _score_json(gj: str) -> Scored:
    if _FRAME is None:
        raise EvolveError("worker has no frame (initializer not run)")
    return score_genome(G.Genome.from_json(gj), _FRAME, _TWIN, n_boot=_N_BOOT, seed=_SEED)


def trade_markets(g: G.Genome, F: Frame) -> np.ndarray:
    """Dense market codes of the genome's trades on ``F`` (the fitness kernel's trade set)."""
    M = G.to_mask(g, F)
    np.logical_and(M, F.visible["executable"], out=M)
    rows = G.first_true_per_block(M, F.block_starts)
    return np.asarray(F.visible["market_code"][rows], dtype=np.int32)


# ---------------------------------------------------------------------------
# evaluator (pool owner)
# ---------------------------------------------------------------------------
class Evaluator:
    """Owns the worker pool for ONE campaign; frames are fixed for its lifetime."""

    def __init__(self, search: Frame, twin: Optional[Frame], cfg: EvolveConfig, *, spawn_dir: Optional[Path] = None):
        self.search = search
        self.twin = twin
        self.cfg = cfg
        self.spawn_dir = spawn_dir
        self.pool = None
        self.ctx = None
        self.start_method: Optional[str] = None
        self.evaluations = 0
        self.score_s = 0.0

    def __enter__(self) -> "Evaluator":
        global _FRAME, _TWIN, _N_BOOT, _SEED
        _FRAME, _TWIN = self.search, self.twin
        _N_BOOT, _SEED = int(self.cfg.n_boot), int(self.cfg.boot_seed)
        if self.cfg.workers <= 1:
            self.start_method = "inprocess"
            _init_worker(None, None, self.cfg.n_boot, self.cfg.boot_seed)
            return self
        methods = mp.get_all_start_methods()
        if "fork" in methods:
            self.ctx = mp.get_context("fork")
            initargs: Tuple[Any, ...] = (None, None, self.cfg.n_boot, self.cfg.boot_seed)
        else:
            if self.spawn_dir is None:
                raise EvolveError("spawn start method needs spawn_dir to hand the stripped frames to workers")
            from src.factory import frame as frame_mod

            sdir = self.spawn_dir / "search"
            tdir = self.spawn_dir / "gefs_twin"
            frame_mod.save(self.search, str(sdir))
            if self.twin is not None:
                frame_mod.save(self.twin, str(tdir))
            self.ctx = mp.get_context("spawn")
            initargs = (str(sdir), str(tdir) if self.twin is not None else None, self.cfg.n_boot, self.cfg.boot_seed)
        self.start_method = self.ctx.get_start_method()
        self.pool = self.ctx.Pool(processes=int(self.cfg.workers), initializer=_init_worker, initargs=initargs)
        return self

    def __exit__(self, *exc: Any) -> None:
        global _FRAME, _TWIN
        if self.pool is not None:
            self.pool.terminate()
            self.pool.join()
            self.pool = None
        _FRAME, _TWIN = None, None
        if self.spawn_dir is not None and self.spawn_dir.exists():
            shutil.rmtree(self.spawn_dir, ignore_errors=True)

    def score(self, genomes: Sequence[G.Genome]) -> List[Scored]:
        """Score in order (ordered ``imap``); results align with ``genomes``."""
        gjs = [ledger_mod.genome_json(g) for g in genomes]
        t0 = time.perf_counter()
        if self.pool is None:
            out = [_score_json(gj) for gj in gjs]
        else:
            out = list(self.pool.imap(_score_json, gjs, chunksize=self.cfg.effective_chunksize()))
        self.score_s += time.perf_counter() - t0  # pure scoring wall time (throughput report only)
        self.evaluations += len(out)
        return out


# ---------------------------------------------------------------------------
# population bookkeeping (parent side)
# ---------------------------------------------------------------------------
@dataclass
class Individual:
    """One ledger row of a generation, as the breeder sees it."""

    idx: int
    genome: G.Genome
    genome_json: str
    genome_id: str
    status: str
    fit: float
    reason: str
    trades: int
    phenotype_hash: str
    n_clauses: int
    trade_markets: np.ndarray  # dense market codes on the campaign frame

    @property
    def scored(self) -> bool:
        return self.status == ledger_mod.STATUS_SCORED and math.isfinite(self.fit)


def _nameless(g: G.Genome, source: str) -> G.Genome:
    return G.Genome(g.genes, name="", notes="", source=source)


def initial_population(cfg: EvolveConfig, rng: np.random.Generator, *, source: str = "gfs_mex") -> List[G.Genome]:
    """Generation 0: searchable seeds (sigma_cap OFF -> 4.0, nameless) then ``Genome.random`` fills."""
    out: List[G.Genome] = []
    seen: set = set()
    for s in G.SEEDS.values():
        if s.source != source or s.mode != 0:
            continue
        g = s.replace(sigma_cap=4.0) if s.value("sigma_cap") is G.OFF else s
        g = _nameless(g, source)
        if not G.is_searchable(g):
            continue
        key = g.genes.tobytes()
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
        if len(out) >= cfg.population:
            break
    while len(out) < cfg.population:
        g = _fresh(rng, source, seen)
        out.append(g)
    return out


def _fresh(rng: np.random.Generator, source: str, seen: set) -> G.Genome:
    g = G.Genome.random(rng)
    if g.source != source:
        g = g.with_meta(source=source)
    for _ in range(REDRAW):
        if g.genes.tobytes() not in seen:
            break
        g = G.Genome.random(rng)
        if g.source != source:
            g = g.with_meta(source=source)
    seen.add(g.genes.tobytes())
    return g


def rank(population: Sequence[Individual]) -> List[Individual]:
    """SCORED by fitness desc (ties: fewer clauses, idx); then KILLED by trades desc, idx."""

    def key(ind: Individual) -> Tuple[Any, ...]:
        if ind.scored:
            return (0, -ind.fit, ind.n_clauses, ind.idx)
        return (1, -int(ind.trades), ind.n_clauses, ind.idx)

    return sorted(population, key=key)


def jaccard_matrix(sets: Sequence[np.ndarray], n_markets: int) -> np.ndarray:
    """Pairwise Jaccard of market-code sets (empty vs empty = 1.0)."""
    n = len(sets)
    if n == 0:
        return np.zeros((0, 0), dtype=np.float64)
    B = np.zeros((n, max(1, n_markets)), dtype=np.float64)  # exact counts (float32 is off at 1e-8)
    for i, s in enumerate(sets):
        if s.size:
            B[i, np.asarray(s, dtype=np.int64)] = 1.0
    inter = B @ B.T
    size = B.sum(axis=1)
    union = size[:, None] + size[None, :] - inter
    with np.errstate(invalid="ignore", divide="ignore"):
        jac = np.where(union > 0, inter / np.where(union > 0, union, 1.0), 1.0)
    return jac.astype(np.float64)


def niche(ranked: Sequence[Individual], threshold: float, n_markets: int) -> List[Individual]:
    """Greedy phenotype niching over a ranked list: drop rows whose trade set has Jaccard > ``threshold`` with a kept row."""
    n = len(ranked)
    if n == 0:
        return []
    jac = jaccard_matrix([ind.trade_markets for ind in ranked], n_markets)
    kept: List[int] = []
    for i in range(n):
        if kept and float(jac[i, kept].max()) > threshold:
            continue
        kept.append(i)
    return [ranked[i] for i in kept]


def _tournament(rng: np.random.Generator, n_pool: int, k: int) -> int:
    draws = rng.integers(0, n_pool, size=k)
    return int(draws.min())  # the pool is ranked: lower index = better


def breed(population: Sequence[Individual], cfg: EvolveConfig, rng: np.random.Generator, *, n_markets: int,
          source: str = "gfs_mex") -> List[G.Genome]:
    """Next generation from a fully scored population (pure function of population + rng)."""
    ranked = rank(population)
    pool = niche(ranked, cfg.niche_jaccard, n_markets)
    if not pool:
        raise EvolveError("empty breeding pool")
    out: List[G.Genome] = []
    seen: set = set()
    for ind in pool:
        if len(out) >= cfg.n_elite:
            break
        if not ind.scored:
            break
        g = _nameless(ind.genome, source)
        key = g.genes.tobytes()
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    n_children = cfg.population - len(out) - cfg.n_immigrants
    n_pool = len(pool)
    for _ in range(max(0, n_children)):
        child = None
        for _attempt in range(REDRAW + 1):
            a = pool[_tournament(rng, n_pool, cfg.tournament)]
            b = pool[_tournament(rng, n_pool, cfg.tournament)]
            child = G.mutate(G.crossover(a.genome, b.genome, rng), rng)
            child = _nameless(child, source)
            if child.genes.tobytes() not in seen:
                break
        assert child is not None
        seen.add(child.genes.tobytes())
        out.append(child)
    while len(out) < cfg.population:
        out.append(_fresh(rng, source, seen))
    return out[: cfg.population]


def apply_duplicate_kills(results: Sequence[Scored]) -> List[Scored]:
    """Within one generation, a later constraint-satisfying row that repeats an earlier row's phenotype is DUPLICATE_PHENOTYPE."""
    seen: set = set()
    out: List[Scored] = []
    for r in results:
        if r.constraint_reason is None:
            if r.phenotype_hash in seen:
                r = Scored(**{**{f.name: getattr(r, f.name) for f in fields(r)}, "constraint_reason": KILL_DUPLICATE_PHENOTYPE})
            else:
                seen.add(r.phenotype_hash)
        out.append(r)
    return out


def _realign(results: Sequence[Scored], date_map: np.ndarray) -> List[Scored]:
    """Map ``per_date_codes`` from the campaign frame's dates to the parent frame's dates."""
    out: List[Scored] = []
    for r in results:
        if r.per_date_codes.size:
            codes = date_map[np.asarray(r.per_date_codes, dtype=np.int64)].astype(np.int16)
            r = Scored(**{**{f.name: getattr(r, f.name) for f in fields(r)}, "per_date_codes": codes})
        out.append(r)
    return out


def _individuals_from_results(genomes: Sequence[G.Genome], results: Sequence[Scored]) -> List[Individual]:
    out: List[Individual] = []
    for i, (g, r) in enumerate(zip(genomes, results)):
        gj = ledger_mod.genome_json(g)
        killed = (r.constraint_reason is not None) or not math.isfinite(r.fit)
        out.append(
            Individual(
                idx=i,
                genome=g,
                genome_json=gj,
                genome_id=ledger_mod.genome_id(gj),
                status=ledger_mod.STATUS_KILLED if killed else ledger_mod.STATUS_SCORED,
                fit=float(r.fit) if not killed else fitness.NEG_INF,
                reason=str(r.constraint_reason or ""),
                trades=int(r.trades),
                phenotype_hash=str(r.phenotype_hash),
                n_clauses=int(r.n_active_clauses) if r.n_active_clauses is not None else G.n_active_clauses(g),
                trade_markets=np.asarray(r.trade_markets, dtype=np.int32),
            )
        )
    return out


def individuals_from_ledger(rows: Sequence[Dict[str, Any]], F: Frame) -> List[Individual]:
    """Rebuild a generation's population from its ledger rows (trade sets recomputed on ``F``)."""
    out: List[Individual] = []
    for row in sorted(rows, key=lambda r: int(r["idx"])):
        g = G.Genome.from_json(row["genome_json"])
        fit = row.get("fitness")
        fit = float(fit) if fit is not None else fitness.NEG_INF
        status = str(row.get("status"))
        if status == ledger_mod.STATUS_UNSCORED:
            raise EvolveError(f"{row.get('row_id')}: UNSCORED row cannot seed a generation")
        out.append(
            Individual(
                idx=int(row["idx"]),
                genome=g,
                genome_json=str(row["genome_json"]),
                genome_id=str(row["genome_id"]),
                status=status,
                fit=fit if status == ledger_mod.STATUS_SCORED else fitness.NEG_INF,
                reason=str(row.get("reason") or ""),
                trades=int(row.get("trades") or 0),
                phenotype_hash=str(row.get("phenotype_hash") or ""),
                n_clauses=G.n_active_clauses(g),
                trade_markets=trade_markets(g, F) if G.is_searchable(g) else np.zeros(0, dtype=np.int32),
            )
        )
    return out


# ---------------------------------------------------------------------------
# status.json
# ---------------------------------------------------------------------------
def write_status(path: Path, doc: Dict[str, Any], mirror: Optional[Path] = None) -> None:
    """Timestamp-free ``status.json`` (+ mirror copy)."""
    write_json(path, doc)
    if mirror is not None:
        write_json(mirror, doc)


# ---------------------------------------------------------------------------
# campaign
# ---------------------------------------------------------------------------
@dataclass
class CampaignResult:
    campaign: str
    ledger: ledger_mod.Ledger
    n_generations: int
    evaluations: int
    n_phenotypes: int
    best_fit: Optional[float]
    worker_dates: Tuple[str, ...]
    start_method: Optional[str]
    resumed_from: Optional[int]
    search: Frame  # the campaign (stripped) search frame -- for the parent's own re-scores
    twin: Optional[Frame]
    scored_now: int = 0  # evaluations performed by THIS call (evaluations counts the rows on disk)
    score_seconds: float = 0.0  # pure scoring wall time of this call (throughput print only)


def _complete_generations(ledger: ledger_mod.Ledger) -> Tuple[int, List[int]]:
    """(last generation with every row SCORED/KILLED, contiguous from 0; generations with UNSCORED rows)."""
    gens = ledger.generations()
    last = -1
    unscored: List[int] = []
    for g in gens:
        st = set(ledger.read_gen(g).column("status").to_pylist())
        if ledger_mod.STATUS_UNSCORED in st:
            unscored.append(g)
            continue
        if g == last + 1:
            last = g
        else:
            raise EvolveError(f"{ledger.campaign}: ledger has a gap before gen_{g:03d} (last complete {last})")
    return last, unscored


def run_campaign(
    fs: Any,
    campaign: folds.Campaign,
    cfg: EvolveConfig,
    run_dir: Union[str, Path],
    *,
    master_seed: int,
    frame_dir: Optional[str] = None,
    log: Callable[[str], Any] = print,
    on_generation: Optional[Callable[[str, int, Dict[str, Any]], Any]] = None,
    resume: bool = False,
    status: Optional[Dict[str, Any]] = None,
    status_mirror: Optional[Path] = None,
) -> CampaignResult:
    """Evolve one campaign to ``cfg.generations`` generations (module docstring).

    ``fs`` is a ``frame.FrameSet`` (``search`` + optional ``gefs_twin``); the
    frames are stripped to ``campaign.worker_dates`` here, before any worker
    exists. ``status`` is the run-level status document to update in place
    (a fresh one is made when None); ``frame_dir`` is provenance only.
    """
    run_dir = Path(run_dir)
    name = campaign.name
    source = str(getattr(fs.search, "provenance", {}).get("source") or "gfs_mex")
    search, twin = folds.strip_to_campaign(fs.search, fs.gefs_twin, campaign)
    worker_dates = tuple(str(d) for d in search.dates)
    forbidden = set(campaign.stripped_dates) & set(worker_dates)
    if forbidden:
        raise EvolveError(f"{name}: stripped frame still holds {sorted(forbidden)[:3]}")
    parent_dates = [str(d) for d in fs.search.dates]
    date_map = np.asarray([parent_dates.index(d) for d in worker_dates], dtype=np.int64)

    ledger = ledger_mod.Ledger(run_dir, name)
    status_path = run_dir / "status.json"
    doc: Dict[str, Any] = status if status is not None else {"run_id": run_dir.name}
    doc.setdefault("picks_done", [])
    doc.setdefault("controls_done", {})
    doc.setdefault("evaluations", 0)
    doc.update({"state": STATE_RUNNING, "phase": "evolve", "campaign": name, "n_gens": int(cfg.generations)})

    # -- resume point ----------------------------------------------------------
    last_complete, unscored = _complete_generations(ledger)
    if not resume and (last_complete >= 0 or unscored):
        raise EvolveError(f"{name}: ledger already has generations (pass resume=True)")
    for g in unscored:
        if g != last_complete + 1:
            ledger.gen_path(g).unlink()  # orphaned UNSCORED file beyond the restart point
    resumed_from: Optional[int] = last_complete if resume and last_complete >= 0 else None
    phenotypes: set = set()
    evaluations_here = 0
    best_fit: Optional[float] = None
    if last_complete >= 0:
        table = ledger.read_all()
        phenotypes = {h for h in table.column("phenotype_hash").to_pylist() if h}
        evaluations_here = int(table.num_rows)
        fits = [f for f in table.column("fitness").to_pylist() if f is not None and math.isfinite(f)]
        best_fit = max(fits) if fits else None
        population = individuals_from_ledger(ledger.read_gen(last_complete).to_pylist(), search)
        log(f"[{name}] resume from gen {last_complete} ({evaluations_here} evaluations on disk)")
    else:
        population = []

    n_gens = int(cfg.generations)
    if last_complete >= n_gens - 1:
        log(f"[{name}] complete ({n_gens} generations)")
        return CampaignResult(name, ledger, n_gens, evaluations_here, len(phenotypes), best_fit, worker_dates,
                              None, resumed_from, search, twin)

    spawn_dir = run_dir / "frames" / name
    with Evaluator(search, twin, cfg, spawn_dir=spawn_dir) as ev:
        for gen in range(last_complete + 1, n_gens):
            rng = np.random.default_rng(seed_for(master_seed, name, gen))
            if gen == 0:
                genomes = initial_population(cfg, rng, source=source)
            else:
                genomes = breed(population, cfg, rng, n_markets=search.n_markets, source=source)
            assert len(genomes) == cfg.population
            # write-then-evaluate
            ledger.append_unscored(gen, genomes)
            results = apply_duplicate_kills(ev.score(genomes))
            ledger.mark_scored(gen, _realign(results, date_map))
            population = _individuals_from_results(genomes, results)
            evaluations_here += len(results)
            doc["evaluations"] = int(doc.get("evaluations", 0)) + len(results)
            for r in results:
                if r.phenotype_hash:
                    phenotypes.add(r.phenotype_hash)
                if r.feasible and (best_fit is None or r.fit > best_fit):
                    best_fit = float(r.fit)
            n_scored = sum(1 for r in results if r.feasible)
            doc.update({"gen": gen, "best_fit": best_fit, "n_phenotypes": len(phenotypes)})
            write_status(status_path, doc, status_mirror)
            info = {
                "campaign": name,
                "gen": gen,
                "n_scored": n_scored,
                "n_killed": len(results) - n_scored,
                "best_fit": best_fit,
                "n_phenotypes": len(phenotypes),
                "evaluations": evaluations_here,
                "worker_dates": worker_dates,
                "start_method": ev.start_method,
            }
            log(f"[{name}] gen {gen:3d}/{n_gens}: scored {n_scored:3d} killed {len(results) - n_scored:3d} "
                f"best_fit {best_fit if best_fit is None else round(best_fit, 5)} phenotypes {len(phenotypes)}")
            if on_generation is not None:
                on_generation(name, gen, info)
        start_method = ev.start_method
        scored_now, score_s = ev.evaluations, ev.score_s
    return CampaignResult(name, ledger, n_gens, evaluations_here, len(phenotypes), best_fit, worker_dates,
                          start_method, resumed_from, search, twin, scored_now=scored_now, score_seconds=score_s)


# ---------------------------------------------------------------------------
# picker (pre-registered: max boot_lo among constraint-satisfying elites)
# ---------------------------------------------------------------------------
PICK_REASON_NO_FEASIBLE = "NO_FEASIBLE"
PICK_REASON_FALLBACK = "FALLBACK_WHOLE_LEDGER"


@dataclass
class Pick:
    """The picker's choice for one campaign (``validation`` filled by the main process later)."""

    campaign: str
    genome: Optional[G.Genome]
    genome_json: Optional[str]
    genome_id: Optional[str]
    phenotype_hash: Optional[str]
    picked_gen: Optional[int]
    in_sample: Optional[fitness.FitnessResult]
    validation: Optional[fitness.FitnessResult] = None
    n_candidates: int = 0
    reason: Optional[str] = None  # None | FALLBACK_WHOLE_LEDGER | NO_FEASIBLE
    #: trades per validation date (aligned with ``validation.per_date_codes``); restored from
    #: picks.json on resume, where ``trade_rows`` is not persisted
    validation_per_date_trades: Optional[List[int]] = None

    @property
    def empty(self) -> bool:
        return self.genome is None


def _feasible(row: Dict[str, Any]) -> bool:
    f = row.get("fitness")
    return (
        str(row.get("status")) == ledger_mod.STATUS_SCORED
        and not row.get("reason")
        and f is not None
        and math.isfinite(float(f))
    )


def _clauses(row: Dict[str, Any]) -> int:
    """``n_active_clauses`` from the canonical genome JSON (no Genome construction)."""
    try:
        return int(json.loads(row["genome_json"])["n_active_clauses"])
    except (KeyError, TypeError, ValueError):
        return G.n_active_clauses(G.Genome.from_json(row["genome_json"]))


def _pick_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    """Sort key: higher boot_lo first; ties -> fewer clauses; then genome_id lexical."""
    return (-float(row["boot_lo"]), _clauses(row), str(row["genome_id"]))


def _fit_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    return (-float(row["fitness"]), _clauses(row), str(row["genome_id"]))


def _result_from_row(row: Dict[str, Any]) -> fitness.FitnessResult:
    return fitness.FitnessResult(
        label=str(row.get("row_id") or ""),
        trades=int(row.get("trades") or 0),
        dates=int(row.get("dates") or 0),
        realized=float(row.get("realized")),
        realized_se=float(row.get("realized_se")),
        t_stat=float(row.get("t_stat")),
        boot_lo=float(row.get("boot_lo")),
        boot_hi=float(row.get("boot_hi")),
        worst_date_pnl=float(row.get("worst_date_pnl")),
        fit=float(row.get("fitness")),
        constraint_reason=None,
        cities=int(row.get("cities") or 0),
        bss_trades=float(row.get("bss_trades")),
        per_date_pnl=np.asarray(row.get("per_date_pnl") or [], dtype=np.float64),
        per_date_codes=np.asarray(row.get("per_date_codes") or [], dtype=np.int16),
        phenotype_hash=str(row.get("phenotype_hash") or ""),
    )


def pick(ledger_or_rows: Union[ledger_mod.Ledger, Sequence[Dict[str, Any]]], cfg: EvolveConfig, *,
         campaign: Optional[str] = None) -> Pick:
    """Pre-registered picker over a campaign ledger (architecture section 6.1).

    Elites = the top ``elite_frac`` of the FINAL generation by fitness
    (``max(1, round(elite_frac * n_rows))`` rows); among the constraint-
    satisfying elites choose the highest search-window ``boot_lo``; ties ->
    fewer active clauses -> ``genome_id`` lexical. If no elite satisfies the
    constraints, fall back to the best-fitness constraint-satisfying row of
    the WHOLE ledger (``reason = FALLBACK_WHOLE_LEDGER``); if there is none,
    ``genome is None`` with ``reason = NO_FEASIBLE``.
    """
    if isinstance(ledger_or_rows, ledger_mod.Ledger):
        rows = ledger_or_rows.read_all().to_pylist()
        campaign = campaign or ledger_or_rows.campaign
    else:
        rows = list(ledger_or_rows)
    campaign = campaign or (str(rows[0]["campaign"]) if rows else "")
    if not rows:
        return Pick(campaign, None, None, None, None, None, None, None, 0, PICK_REASON_NO_FEASIBLE)
    final_gen = max(int(r["gen"]) for r in rows)
    final = [r for r in rows if int(r["gen"]) == final_gen]
    n_elite = max(1, int(round(cfg.elite_frac * len(final))))
    ranked = sorted(final, key=lambda r: (
        0 if _feasible(r) else 1,
        -float(r["fitness"]) if _feasible(r) else 0.0,
        int(r["idx"]),
    ))
    elites = [r for r in ranked[:n_elite] if _feasible(r)]
    reason: Optional[str] = None
    if elites:
        chosen = sorted(elites, key=_pick_key)[0]
        n_cand = len(elites)
    else:
        feasible = [r for r in rows if _feasible(r)]
        if not feasible:
            return Pick(campaign, None, None, None, None, None, None, None, 0, PICK_REASON_NO_FEASIBLE)
        chosen = sorted(feasible, key=_fit_key)[0]
        n_cand = len(feasible)
        reason = PICK_REASON_FALLBACK
    g = G.Genome.from_json(chosen["genome_json"])
    return Pick(
        campaign=campaign,
        genome=g,
        genome_json=str(chosen["genome_json"]),
        genome_id=str(chosen["genome_id"]),
        phenotype_hash=str(chosen["phenotype_hash"]),
        picked_gen=int(chosen["gen"]),
        in_sample=_result_from_row(chosen),
        validation=None,
        n_candidates=n_cand,
        reason=reason,
    )


__all__ = [
    "KILL_CODES",
    "KILL_DUPLICATE_PHENOTYPE",
    "KILL_ILLEGAL",
    "PICK_REASON_FALLBACK",
    "PICK_REASON_NO_FEASIBLE",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_RUNNING",
    "CampaignResult",
    "Evaluator",
    "EvolveConfig",
    "EvolveError",
    "Individual",
    "Pick",
    "Scored",
    "apply_duplicate_kills",
    "breed",
    "individuals_from_ledger",
    "initial_population",
    "jaccard_matrix",
    "niche",
    "pick",
    "rank",
    "run_campaign",
    "score_genome",
    "seed_for",
    "trade_markets",
    "write_status",
]
