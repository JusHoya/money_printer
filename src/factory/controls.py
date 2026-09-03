"""Control runs: snapshot-efficient x20, residual-shuffle x20, planted-edge x1 (FR-F2.4).

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 6.4 and the
F2 contract. Each replicate is the FULL procedure (campaigns A, B, C; same
population, generations and master seed) run by ``procedure.run_procedure``
on a transformed ``FrameSet`` from ``src.factory.null`` in
``<run_dir>/controls/<kind>/<k>/`` (the run-directory layout of the contract).
Replicates whose ``oos/pooled.json`` exists are skipped (resumable), and every
per-replicate number is recomputed FROM DISK (``picks.json``, ``oos/pooled.json``,
``ledger/``) plus the deterministic control frame, never from the in-memory
result, so ``controls/summary.json`` is reproducible after a crash.

Seeds: ``null.control_seed(master_seed, kind, k)``; the procedure itself
still runs with ``master_seed`` (same search, different truth), which is what
makes a control a control.

``controls/summary.json`` (timestamp-free)::

    snapshot: {n, replicates: [{k, seed, pooled, p_rc_per_campaign, p_spa_per_campaign,
               boot_lo_gt0, picks}], pooled_means, n_boot_lo_gt0, ks_p_rc: {stat, p, n},
               real_pooled_mean, real_rank, pass_boot_lo (<= 1 of 20), pass_ks (p > 0.05)}
    residual: {n, replicates: [...], pooled_means, p95, real_pooled_mean, real_rank,
               real_exceeds_p95}
    planted:  {rule, edge, seed, info, picks: {A: {...}}, pick_pooled_on_planted,
               pick_pooled_on_original, captured, capture_ratio, pass (>= 0.8),
               rule_pooled_validation_delta}

``real_rank`` = ``1 + #{replicates whose pooled mean > the real run's}`` (1 = the
real run beats every replicate). Pooled statistics use ``pooled_stats`` --
``fitness.bootstrap_draws`` with the kernel's seed, identical to
``procedure.pooled_stats``.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from src.factory import fitness
from src.factory import folds
from src.factory import genome as G
from src.factory import multiplicity as MP
from src.factory import null as NULL
from src.factory.columns import Frame
from src.factory.frame import FrameSet
from src.factory.ledger import Ledger
from src.factory.report import write_json

KINDS = NULL.KINDS
CONTROL_CAMPAIGNS = ("A", "B", "C")
POOLED_CAMPAIGNS = ("A", "B", "C")

__all__ = [
    "CONTROL_CAMPAIGNS",
    "KINDS",
    "load_json",
    "load_picks",
    "pick_genome",
    "pick_p_rc",
    "pooled_stats",
    "pooled_validation",
    "replicate_dir",
    "replicate_summary",
    "run_controls",
    "score_validation",
    "summarise_controls",
    "worker_dates",
]


# ---------------------------------------------------------------------------
# disk helpers
# ---------------------------------------------------------------------------
def load_json(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_picks(run_dir: Union[str, Path]) -> Dict[str, Any]:
    picks = load_json(Path(run_dir) / "picks.json")
    if picks is None:
        raise FileNotFoundError(f"{Path(run_dir) / 'picks.json'} is missing")
    return picks


def pick_genome(pick: Dict[str, Any]) -> G.Genome:
    gj = pick.get("genome_json")
    if isinstance(gj, str):
        return G.Genome.from_json(gj)
    return G.Genome.from_json(dict(gj))


def replicate_dir(run_dir: Union[str, Path], kind: str, k: int) -> Path:
    return Path(run_dir) / "controls" / str(kind) / str(int(k))


def _update_status(run_dir: Path, **fields: Any) -> None:
    p = run_dir / "status.json"
    doc = load_json(p) or {"run_id": run_dir.name, "state": "RUNNING"}
    doc.update(fields)
    write_json(p, doc)


# ---------------------------------------------------------------------------
# scoring helpers (main process; the SAME arithmetic as the procedure)
# ---------------------------------------------------------------------------
def pooled_stats(per_date_pnl: Sequence[float], *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED) -> Dict[str, Any]:
    """mean / se / t / boot CI of a per-date series (``fitness.bootstrap_draws``, kernel seed)."""
    v = np.asarray([float(x) for x in per_date_pnl], dtype=np.float64)
    v = v[np.isfinite(v)]
    n = int(v.shape[0])
    out: Dict[str, Any] = {"n_dates": n, "mean": math.nan, "se": math.nan, "t_stat": math.nan, "boot_lo": math.nan, "boot_hi": math.nan}
    if n == 0:
        return out
    mean = float(v.mean())
    se = float(v.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan
    t = float(mean / se) if (se == se and se > 0) else math.nan
    draws = fitness.bootstrap_draws(v, n_boot=int(n_boot), seed=int(seed))
    lo, hi = (float(x) for x in np.percentile(draws, [2.5, 97.5]))
    out.update({"mean": mean, "se": se, "t_stat": t, "boot_lo": lo, "boot_hi": hi})
    return out


def worker_dates(F: Frame, campaign: folds.Campaign) -> List[str]:
    """The dates a campaign's worker frame carries (what the ledger's ``per_date_codes`` index)."""
    keep = set(campaign.search_dates)
    return [str(d) for d in F.dates if str(d) in keep]


def score_validation(fs: FrameSet, genome: G.Genome, campaign: folds.Campaign, *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED) -> Optional[fitness.FitnessResult]:
    """Score ``genome`` once on the campaign's validation dates of the FULL search frame (constraints off)."""
    if not campaign.validation_dates:
        return None
    F = fs.search
    dm = folds.date_mask(F, campaign.validation_dates)
    return fitness.score(F, G.to_mask(genome, F), date_mask=dm, constraints=False, twin=fs.gefs_twin, genome=genome,
                         n_boot=n_boot, seed=seed, label=f"{genome.name}/{campaign.name}/validation")


def per_date_rows(F: Frame, res: fitness.FitnessResult, campaign: str) -> List[Dict[str, Any]]:
    """``[{date, campaign, pnl, trades}]`` from a validation ``FitnessResult``."""
    codes = np.asarray(res.per_date_codes, dtype=np.int64)
    tdc = F.visible["target_date_code"][np.asarray(res.trade_rows, dtype=np.int64)]
    counts = np.bincount(tdc, minlength=F.n_dates) if tdc.size else np.zeros(F.n_dates, dtype=np.int64)
    return [
        {"date": str(F.dates[c]), "campaign": campaign, "pnl": float(p), "trades": int(counts[c])}
        for c, p in zip(codes, np.asarray(res.per_date_pnl, dtype=np.float64))
    ]


def pooled_validation(fs: FrameSet, genomes: Dict[str, G.Genome], campaigns: Sequence[str] = POOLED_CAMPAIGNS, *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED) -> Tuple[Dict[str, Any], Dict[str, Optional[fitness.FitnessResult]]]:
    """Pool the validation-date PnLs of ``genomes[c]`` over ``campaigns`` (33 dates on the full frame)."""
    camps = folds.campaigns([str(d) for d in fs.search.dates])
    per_date: List[Dict[str, Any]] = []
    results: Dict[str, Optional[fitness.FitnessResult]] = {}
    for c in campaigns:
        g = genomes.get(c)
        if g is None or c not in camps:
            results[c] = None
            continue
        r = score_validation(fs, g, camps[c], n_boot=n_boot, seed=seed)
        results[c] = r
        if r is not None and r.trades:
            per_date.extend(per_date_rows(fs.search, r, c))
    stats = pooled_stats([row["pnl"] for row in per_date], n_boot=n_boot, seed=seed)
    stats["per_date"] = per_date
    stats["trades"] = int(sum(row["trades"] for row in per_date))
    return stats, results


def pick_p_rc(run_dir: Union[str, Path], campaign: folds.Campaign, F: Frame, phenotype_hash: str, *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED) -> Dict[str, Any]:
    """RC/SPA of the campaign pick over every distinct phenotype in the campaign's ledger."""
    led = Ledger(run_dir, campaign.name)
    M, ids = MP.ledger_matrix(led, campaign.search_dates, code_dates=worker_dates(F, campaign))
    out: Dict[str, Any] = {"p_rc": math.nan, "p_spa": math.nan, "L": int(M.shape[0]), "D": int(M.shape[1]), "n_phenotypes": len(ids), "pick_in_ledger": False}
    if phenotype_hash in ids:
        rc = MP.reality_check(M, ids.index(phenotype_hash), n_boot=n_boot, seed=seed)
        out.update(rc)
        out["pick_in_ledger"] = True
    return out


# ---------------------------------------------------------------------------
# per-replicate summary (from disk)
# ---------------------------------------------------------------------------
def replicate_summary(rep_dir: Path, fs_k: FrameSet, campaigns: Sequence[str] = CONTROL_CAMPAIGNS, *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED) -> Dict[str, Any]:
    """Pooled validation (re-scored on the control frame), per-campaign p_RC/SPA and the picks."""
    picks = load_picks(rep_dir)
    camps = folds.campaigns([str(d) for d in fs_k.search.dates])
    genomes: Dict[str, G.Genome] = {}
    picks_out: Dict[str, Any] = {}
    for c in campaigns:
        p = picks.get(c)
        if not p:
            continue
        g = pick_genome(p)
        genomes[c] = g
        picks_out[c] = {
            "genome_id": p.get("genome_id"),
            "genome_json": p.get("genome_json"),
            "phenotype_hash": p.get("phenotype_hash"),
            "picked_gen": p.get("picked_gen"),
            "n_candidates": p.get("n_candidates"),
            "in_sample_boot_lo": (p.get("in_sample") or {}).get("boot_lo"),
        }
    pooled, _ = pooled_validation(fs_k, genomes, campaigns, n_boot=n_boot, seed=seed)
    on_disk = load_json(rep_dir / "oos" / "pooled.json") or {}
    pooled_disk_mean = on_disk.get("mean")
    p_rc: Dict[str, Any] = {}
    p_spa: Dict[str, Any] = {}
    n_ph: Dict[str, int] = {}
    for c in campaigns:
        if c not in genomes:
            continue
        rc = pick_p_rc(rep_dir, camps[c], fs_k.search, str(picks_out[c]["phenotype_hash"] or ""), n_boot=n_boot, seed=seed)
        p_rc[c] = rc["p_rc"]
        p_spa[c] = rc["p_spa"]
        n_ph[c] = rc["n_phenotypes"]
    lo = pooled.get("boot_lo")
    return {
        "pooled": {k: pooled[k] for k in ("n_dates", "mean", "se", "t_stat", "boot_lo", "boot_hi", "trades")},
        "pooled_mean_procedure": pooled_disk_mean,
        "pooled_matches_procedure": (
            None if pooled_disk_mean is None or pooled["mean"] != pooled["mean"]
            else bool(abs(float(pooled_disk_mean) - float(pooled["mean"])) < 1e-12)
        ),
        "p_rc_per_campaign": p_rc,
        "p_spa_per_campaign": p_spa,
        "n_phenotypes": n_ph,
        "boot_lo_gt0": bool(lo is not None and lo == lo and lo > 0.0),
        "picks": picks_out,
    }


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------
def _default_run_procedure() -> Callable[..., Any]:
    from src.factory import procedure  # EVOLVE workstream; resolved lazily

    return procedure.run_procedure


def _rank(real: float, others: Sequence[float]) -> Optional[int]:
    vals = [float(x) for x in others if x == x]
    if real != real or not vals:
        return None
    return 1 + sum(1 for x in vals if x > real)


def run_controls(
    fs: FrameSet,
    config: Dict[str, Any],
    run_dir: Union[str, Path],
    real: Any,
    *,
    cfg: Any,
    master_seed: int,
    n_snapshot: int = 20,
    n_residual: int = 20,
    kinds: Sequence[str] = KINDS,
    log: Callable[[str], None] = print,
    run_procedure: Optional[Callable[..., Any]] = None,
    campaigns: Sequence[str] = CONTROL_CAMPAIGNS,
    edge: float = 0.05,
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
) -> Dict[str, Any]:
    """Run every replicate of every kind (resumable) and write ``controls/summary.json``.

    ``real``: the real run's ``ProcedureResult`` (or anything with ``.pooled``
    / a dict with ``"pooled"``), or ``None`` to read ``<run_dir>/oos/pooled.json``.
    ``run_procedure`` defaults to ``src.factory.procedure.run_procedure`` and is
    called as ``run_procedure(fs_k, config, rep_dir, campaigns=campaigns,
    blocked_folds=False, cfg=cfg, master_seed=master_seed, frame_dir=None,
    resume=True, log=log)``.
    """
    run_dir = Path(run_dir)
    rp = run_procedure or _default_run_procedure()
    plan = {"snapshot": int(n_snapshot), "residual": int(n_residual), "planted": 1}
    for kind in kinds:
        if kind not in KINDS:
            raise ValueError(f"unknown control kind {kind!r}; want {KINDS}")
        n = plan[kind]
        for k in range(n):
            rep = replicate_dir(run_dir, kind, k)
            s = NULL.control_seed(master_seed, kind, k)
            if (rep / "oos" / "pooled.json").exists():
                log(f"controls: {kind}/{k} already done (oos/pooled.json exists); skipping")
                continue
            log(f"controls: {kind}/{k} seed={s}: building control frames")
            kw = {"edge": edge} if kind == "planted" else {}
            fs_k, info = NULL.make_control_frames(fs, kind, s, **kw)
            rep.mkdir(parents=True, exist_ok=True)
            write_json(rep / "control.json", {"kind": kind, "k": k, "seed": s, "info": info})
            log(f"controls: {kind}/{k}: running the procedure on campaigns {list(campaigns)}")
            rp(fs_k, config, rep, campaigns=tuple(campaigns), blocked_folds=False, cfg=cfg,
               master_seed=master_seed, frame_dir=None, resume=True, log=log)
            done = {kk: sum(1 for j in range(plan[kk]) if (replicate_dir(run_dir, kk, j) / "oos" / "pooled.json").exists()) for kk in kinds}
            _update_status(run_dir, phase="controls", controls_done=done)
    real_mean = _real_pooled_mean(real, run_dir)
    summary = summarise_controls(
        run_dir, fs, real_mean, kinds=kinds, n_snapshot=n_snapshot, n_residual=n_residual,
        master_seed=master_seed, campaigns=campaigns, edge=edge, n_boot=n_boot, seed=seed, log=log,
    )
    write_json(run_dir / "controls" / "summary.json", summary)
    log(f"controls: wrote {run_dir / 'controls' / 'summary.json'}")
    return summary


def _real_pooled_mean(real: Any, run_dir: Path) -> float:
    pooled = None
    if real is None:
        pooled = load_json(run_dir / "oos" / "pooled.json")
    elif isinstance(real, dict):
        pooled = real.get("pooled", real)
    else:
        pooled = getattr(real, "pooled", None)
    if not pooled or pooled.get("mean") is None:
        return math.nan
    return float(pooled["mean"])


def summarise_controls(
    run_dir: Union[str, Path],
    fs: FrameSet,
    real_pooled_mean: float,
    *,
    kinds: Sequence[str] = KINDS,
    n_snapshot: int = 20,
    n_residual: int = 20,
    master_seed: int,
    campaigns: Sequence[str] = CONTROL_CAMPAIGNS,
    edge: float = 0.05,
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Build the ``controls/summary.json`` document from the replicate directories on disk."""
    run_dir = Path(run_dir)
    plan = {"snapshot": int(n_snapshot), "residual": int(n_residual), "planted": 1}
    out: Dict[str, Any] = {
        "campaigns": list(campaigns),
        "master_seed": int(master_seed),
        "real_pooled_mean": real_pooled_mean,
        "n_boot": int(n_boot),
        "bootstrap_seed": int(seed),
        "kinds": list(kinds),
    }
    for kind in kinds:
        reps: List[Dict[str, Any]] = []
        missing: List[int] = []
        for k in range(plan[kind]):
            rep = replicate_dir(run_dir, kind, k)
            if not (rep / "oos" / "pooled.json").exists():
                missing.append(k)
                continue
            s = NULL.control_seed(master_seed, kind, k)
            kw = {"edge": edge} if kind == "planted" else {}
            fs_k, info = NULL.make_control_frames(fs, kind, s, **kw)
            rs = replicate_summary(rep, fs_k, campaigns, n_boot=n_boot, seed=seed)
            rs.update({"k": k, "seed": s})
            if kind == "planted":
                rs["info"] = info
                rs["fs_k"] = fs_k  # stripped below
            reps.append(rs)
            log(f"controls: summarised {kind}/{k}: pooled mean {rs['pooled']['mean']:+.4f} boot_lo {rs['pooled']['boot_lo']:+.4f}")
        means = [r["pooled"]["mean"] for r in reps]
        block: Dict[str, Any] = {"n": plan[kind], "n_done": len(reps), "missing": missing, "replicates": [], "pooled_means": means}
        if kind == "snapshot":
            pvals = [p for r in reps for p in r["p_rc_per_campaign"].values() if p == p]
            ks = MP.ks_uniform(pvals)
            n_gt0 = sum(1 for r in reps if r["boot_lo_gt0"])
            block.update({
                "n_boot_lo_gt0": n_gt0,
                "ks_p_rc": ks,
                "p_rc_values": pvals,
                "real_rank": _rank(real_pooled_mean, means),
                "pass_boot_lo": bool(len(reps) == plan[kind] and n_gt0 <= 1),
                "pass_ks": bool(ks["p"] == ks["p"] and ks["p"] > 0.05),
            })
        elif kind == "residual":
            finite = [m for m in means if m == m]
            p95 = float(np.percentile(finite, 95)) if finite else math.nan
            block.update({
                "p95": p95,
                "real_rank": _rank(real_pooled_mean, means),
                "real_exceeds_p95": bool(real_pooled_mean == real_pooled_mean and p95 == p95 and real_pooled_mean > p95),
            })
        elif kind == "planted" and reps:
            r0 = reps[0]
            fs_k = r0.pop("fs_k")
            genomes = {c: pick_genome({"genome_json": p["genome_json"]}) for c, p in r0["picks"].items()}
            on_planted, _ = pooled_validation(fs_k, genomes, campaigns, n_boot=n_boot, seed=seed)
            on_original, _ = pooled_validation(fs, genomes, campaigns, n_boot=n_boot, seed=seed)
            captured = (float(on_planted["mean"]) - float(on_original["mean"])) if (on_planted["n_dates"] and on_original["n_dates"]) else math.nan
            ratio = captured / float(edge) if captured == captured and edge else math.nan
            block.update({
                "rule": r0["info"].get("rule"),
                "edge": float(edge),
                "seed": r0["seed"],
                "info": {k: v for k, v in r0["info"].items() if k != "strata"},
                "picks": r0["picks"],
                "pick_pooled_on_planted": {k: on_planted[k] for k in ("n_dates", "mean", "se", "t_stat", "boot_lo", "boot_hi", "trades")},
                "pick_pooled_on_original": {k: on_original[k] for k in ("n_dates", "mean", "se", "t_stat", "boot_lo", "boot_hi", "trades")},
                "captured": captured,
                "capture_ratio": ratio,
                "pass": bool(ratio == ratio and ratio >= 0.8),
                "rule_pooled_validation_delta": r0["info"].get("rule_pooled_validation_delta"),
            })
        for r in reps:
            r.pop("fs_k", None)
            r.pop("info", None)
        block["replicates"] = reps
        out[kind] = block
    return out
