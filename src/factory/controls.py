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

    snapshot: {n, replicates: [{k, seed, pooled, p_rc_per_campaign (feasible set),
               p_rc_all_per_campaign, p_spa_per_campaign, L_feasible, paired_delta, boot_lo_gt0,
               picks}], pooled_means, n_boot_lo_gt0, ks_p_rc: {stat, p, n} (feasible p),
               ks_p_rc_all, real_pooled_mean, real_rank, pass_boot_lo (<= 1 of 20), pass_ks (p > 0.05)}
    residual: {n, replicates: [...], pooled_means (= raw_means, diagnostic), p95, real_rank,
               real_exceeds_p95, paired_deltas, paired_p95, real_paired_delta, real_paired_rank,
               real_exceeds_paired_p95, statistic, note}          -- section 6.4a (2026-09-03)
    planted:  {rule, edge, seed, info, picks: {A: {...}}, pick_pooled_on_planted,
               pick_pooled_on_original, captured, capture_ratio, pass (>= 0.8),
               rule_pooled_validation_delta, rule_capture_ratio, pick_validation_trades,
               pick_flipped_trades, pick_rule_overlap, note}
    real_paired_delta, real_paired: the real picks' paired delta vs nofilter_no on the real frame

``real_rank`` = ``1 + #{replicates whose pooled mean > the real run's}`` (1 = the
real run beats every replicate); ``real_paired_rank`` the same on the paired
deltas. ``p_rc`` is the FEASIBLE-set Reality Check (``multiplicity.pick_multiplicity``:
dates >= ceil(0.6 D) and trades >= 40, the picker's admissible set); ``p_rc_all``
competes against every ledger phenotype (p ~ 1 for every pick: no power).
Pooled statistics use ``pooled_stats`` -- ``fitness.bootstrap_draws`` with the
kernel's seed, identical to ``procedure.pooled_stats``.
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
    "pick_present",
    "ledger_code_dates",
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


def pick_present(pick: Optional[Dict[str, Any]]) -> bool:
    """True when a picks.json entry carries a genome (NO_FEASIBLE picks have ``genome_json: null``)."""
    return bool(pick) and bool(pick.get("genome_json"))


def pick_genome(pick: Dict[str, Any]) -> G.Genome:
    gj = pick.get("genome_json")
    if not gj:
        raise ValueError(f"pick has no genome (reason={pick.get('reason')!r}); guard with pick_present()")
    if isinstance(gj, str):
        return G.Genome.from_json(gj)
    return G.Genome.from_json(dict(gj))


def replicate_dir(run_dir: Union[str, Path], kind: str, k: int) -> Path:
    return Path(run_dir) / "controls" / str(kind) / str(int(k))


def _update_status(run_dir: Path, mirror: Optional[Path] = None, **fields: Any) -> None:
    """Timestamp-free status.json update (+ the tracked mirror the Hermes monitor hashes).

    Without the mirror the monitor cron saw the same bytes for the whole controls
    phase and posted nothing (alcyone run_2026-09-03): every replicate must change it.
    """
    p = run_dir / "status.json"
    doc = load_json(p) or {"run_id": run_dir.name, "state": "RUNNING"}
    doc.update(fields)
    write_json(p, doc)
    if mirror is not None:
        write_json(Path(mirror), doc)


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
    """The dates a campaign's worker frame carries (search window only)."""
    keep = set(campaign.search_dates)
    return [str(d) for d in F.dates if str(d) in keep]


def ledger_code_dates(F: Frame) -> List[str]:
    """What the ledger's ``per_date_codes`` index: the FULL search frame's ``Frame.dates``
    (``evolve`` remaps worker-frame codes to the parent frame before the ledger write;
    ``folds.json["dates"]`` lists the same calendar)."""
    return [str(d) for d in F.dates]


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
    """RC/SPA of the campaign pick: headline on the FEASIBLE competition set, ``*_all`` over every phenotype.

    Feasible = distinct ledger phenotypes with ``dates >= ceil(0.6 * |search dates|)``
    and ``trades >= MIN_TRADES`` (the picker's admissible set; ``multiplicity``
    module docstring, 2026-09-03 amendment). ``n_phenotypes`` counts every
    distinct phenotype (the multiplicity the ledger records).
    """
    led = Ledger(run_dir, campaign.name)
    M, ids, meta = MP.ledger_matrix(led, campaign.search_dates, code_dates=ledger_code_dates(F), with_meta=True)
    feas = MP.feasible_mask(meta["dates"], meta["trades"], len(campaign.search_dates),
                            min_date_fraction=fitness.MIN_DATE_FRACTION, min_trades=fitness.MIN_TRADES)
    out: Dict[str, Any] = {"p_rc": math.nan, "p_spa": math.nan, "p_rc_all": math.nan, "p_spa_all": math.nan,
                           "L": int(M.shape[0]), "L_all": int(M.shape[0]), "L_feasible": int(np.count_nonzero(feas)),
                           "D": int(M.shape[1]), "n_phenotypes": len(ids), "pick_in_ledger": False}
    if phenotype_hash in ids:
        rc = MP.pick_multiplicity(M, ids.index(phenotype_hash), feas, n_boot=n_boot, seed=seed)
        out.update(rc)
        out["pick_in_ledger"] = True
    return out


def paired_vs_baseline(
    fs_k: FrameSet,
    genomes: Dict[str, G.Genome],
    campaigns: Sequence[str] = POOLED_CAMPAIGNS,
    *,
    baseline: Optional[G.Genome] = None,
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
) -> Dict[str, Any]:
    """Per-date paired difference (pick_k - baseline_k) on the validation dates the pick traded, pooled.

    The baseline (default the ``nofilter_no`` seed) is scored on the SAME frame
    -- for a control replicate that is the transformed frame, so the market-vs-
    truth inflation of the residual-shuffle null cancels in the difference
    (FACTORY_ARCHITECTURE section 6.4a). Baseline contributes 0 on a date it did
    not trade. The report's ``paired_vs_nofilter`` block uses this function.
    """
    base = baseline if baseline is not None else G.SEEDS["nofilter_no"]
    camps = folds.campaigns([str(d) for d in fs_k.search.dates])
    per_campaign: Dict[str, Any] = {}
    diffs: List[float] = []
    base_means: List[float] = []
    for c in campaigns:
        g = genomes.get(c)
        camp = camps.get(c)
        if g is None or camp is None or not camp.validation_dates:
            per_campaign[c] = None
            continue
        r_v = score_validation(fs_k, g, camp, n_boot=n_boot, seed=seed)
        if r_v is None or not r_v.trades:
            per_campaign[c] = None
            continue
        r_b = score_validation(fs_k, base, camp, n_boot=n_boot, seed=seed)
        b_map = {int(k): float(v) for k, v in zip(r_b.per_date_codes, r_b.per_date_pnl)} if r_b is not None and r_b.trades else {}
        d = [float(v) - b_map.get(int(k), 0.0) for k, v in zip(r_v.per_date_codes, r_v.per_date_pnl)]
        diffs.extend(d)
        if r_b is not None and r_b.trades:
            base_means.extend(float(x) for x in r_b.per_date_pnl)
        per_campaign[c] = dict(pooled_stats(d, n_boot=n_boot, seed=seed), baseline_trades=int(r_b.trades) if r_b else 0,
                               baseline_realized=(r_b.realized if r_b and r_b.trades else None))
    return {
        "baseline": base.name or "nofilter_no",
        "rule": "per-date (pick_k - baseline_k) on the dates the pick traded; baseline 0 where it did not trade",
        "pooled": pooled_stats(diffs, n_boot=n_boot, seed=seed),
        "per_campaign": per_campaign,
        "baseline_pooled_mean": (float(np.mean(base_means)) if base_means else math.nan),
    }


# ---------------------------------------------------------------------------
# per-replicate summary (from disk)
# ---------------------------------------------------------------------------
def replicate_summary(rep_dir: Path, fs_k: FrameSet, campaigns: Sequence[str] = CONTROL_CAMPAIGNS, *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED) -> Dict[str, Any]:
    """Pooled validation (re-scored on the control frame), per-campaign p_RC/SPA and the picks."""
    picks = load_picks(rep_dir)
    camps = folds.campaigns([str(d) for d in fs_k.search.dates])
    genomes: Dict[str, G.Genome] = {}
    picks_out: Dict[str, Any] = {}
    picks_missing: Dict[str, Any] = {}
    for c in campaigns:
        p = picks.get(c)
        if not pick_present(p):
            picks_missing[c] = (p or {}).get("reason") or "MISSING"
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
    p_rc_all: Dict[str, Any] = {}
    n_ph: Dict[str, int] = {}
    l_feas: Dict[str, int] = {}
    for c in campaigns:
        if c not in genomes:
            continue
        rc = pick_p_rc(rep_dir, camps[c], fs_k.search, str(picks_out[c]["phenotype_hash"] or ""), n_boot=n_boot, seed=seed)
        p_rc[c] = rc["p_rc"]
        p_spa[c] = rc["p_spa"]
        p_rc_all[c] = rc["p_rc_all"]
        n_ph[c] = rc["n_phenotypes"]
        l_feas[c] = rc["L_feasible"]
    paired = paired_vs_baseline(fs_k, genomes, campaigns, n_boot=n_boot, seed=seed)
    lo = pooled.get("boot_lo")
    return {
        "paired_delta": paired["pooled"].get("mean"),
        "paired": {"pooled": paired["pooled"], "baseline_pooled_mean": paired["baseline_pooled_mean"], "baseline": paired["baseline"]},
        "p_rc_all_per_campaign": p_rc_all,
        "L_feasible": l_feas,
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
        "picks_missing": picks_missing,
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
    status_mirror: Optional[Union[str, Path]] = None,
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
            _update_status(run_dir, status_mirror, phase="controls", control=f"{kind}/{k}")
            rp(fs_k, config, rep, campaigns=tuple(campaigns), blocked_folds=False, cfg=cfg,
               master_seed=master_seed, frame_dir=None, resume=True, log=log)
            done = {kk: sum(1 for j in range(plan[kk]) if (replicate_dir(run_dir, kk, j) / "oos" / "pooled.json").exists()) for kk in kinds}
            _update_status(run_dir, status_mirror, phase="controls", control=None, controls_done=done)
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
    real_picks = load_picks(run_dir)
    real_genomes = {c: pick_genome(real_picks[c]) for c in campaigns if pick_present(real_picks.get(c))}
    real_paired = paired_vs_baseline(fs, real_genomes, campaigns, n_boot=n_boot, seed=seed) if real_genomes else None
    real_paired_delta = float(real_paired["pooled"]["mean"]) if real_paired else math.nan
    out: Dict[str, Any] = {
        "campaigns": list(campaigns),
        "master_seed": int(master_seed),
        "real_pooled_mean": real_pooled_mean,
        "real_paired_delta": real_paired_delta,
        "real_paired": ({"pooled": real_paired["pooled"], "baseline_pooled_mean": real_paired["baseline_pooled_mean"],
                         "baseline": real_paired["baseline"]} if real_paired else None),
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
            pvals_all = [p for r in reps for p in r["p_rc_all_per_campaign"].values() if p == p]
            ks = MP.ks_uniform(pvals)
            ks_all = MP.ks_uniform(pvals_all)
            n_gt0 = sum(1 for r in reps if r["boot_lo_gt0"])
            block.update({
                "n_boot_lo_gt0": n_gt0,
                "ks_p_rc": ks,
                "p_rc_values": pvals,
                "ks_p_rc_all": ks_all,
                "p_rc_all_values": pvals_all,
                "p_rc_definition": "feasible competition set (multiplicity.pick_multiplicity); *_all = every ledger phenotype",
                "real_rank": _rank(real_pooled_mean, means),
                "pass_boot_lo": bool(len(reps) == plan[kind] and n_gt0 <= 1),
                "pass_ks": bool(ks["p"] == ks["p"] and ks["p"] > 0.05),
            })
        elif kind == "residual":
            finite = [m for m in means if m == m]
            p95 = float(np.percentile(finite, 95)) if finite else math.nan
            deltas = [r.get("paired_delta") if r.get("paired_delta") is not None else math.nan for r in reps]
            dfin = [d for d in deltas if d == d]
            dp95 = float(np.percentile(dfin, 95)) if dfin else math.nan
            block.update({
                "raw_means": means,
                "p95": p95,
                "real_rank": _rank(real_pooled_mean, means),
                "real_exceeds_p95": bool(real_pooled_mean == real_pooled_mean and p95 == p95 and real_pooled_mean > p95),
                "paired_deltas": deltas,
                "paired_p95": dp95,
                "real_paired_delta": real_paired_delta,
                "real_paired_rank": _rank(real_paired_delta, deltas),
                "real_exceeds_paired_p95": bool(real_paired_delta == real_paired_delta and dp95 == dp95 and real_paired_delta > dp95),
                "statistic": "paired: pooled(pick_k) - pooled(nofilter_no on the same shuffled-truth frame), per-date on the pick's validation dates",
                "note": ("raw pooled means are a diagnostic only: the residual shuffle keeps the market's late-day quotes, "
                         "which already embed the observed high, so after re-settling on shifted truth every rule that fades a "
                         "confident late quote wins (buy_no 3-6h longshot +0.64/contract under the null vs -0.05 real; "
                         "fr31a +0.12 vs +0.07). The paired difference against the no-filter baseline under the SAME truth "
                         "absorbs that inflation (FACTORY_ARCHITECTURE section 6.4a, 2026-09-03 amendment)."),
            })
        elif kind == "planted" and reps:
            r0 = reps[0]
            fs_k = r0.pop("fs_k")
            genomes = {c: pick_genome({"genome_json": p["genome_json"]}) for c, p in r0["picks"].items() if p.get("genome_json")}
            on_planted, res_planted = pooled_validation(fs_k, genomes, campaigns, n_boot=n_boot, seed=seed)
            on_original, _ = pooled_validation(fs, genomes, campaigns, n_boot=n_boot, seed=seed)
            captured = (float(on_planted["mean"]) - float(on_original["mean"])) if (on_planted["n_dates"] and on_original["n_dates"]) else math.nan
            ratio = captured / float(edge) if captured == captured and edge else math.nan
            # disclosure (red team F2 S3): how many of the picks' validation trades the planting touched,
            # and how much of their trade set lies inside the planted rule region
            flipped = np.flatnonzero(fs_k.search.hidden["won"] != fs.search.hidden["won"]) if fs_k.search.n_rows == fs.search.n_rows else np.zeros(0, dtype=np.int64)
            flipped_set = set(int(x) for x in flipped.tolist())
            rule_json = r0["info"].get("rule")
            try:
                rule_g = G.Genome.from_json(rule_json) if rule_json else NULL.PLANTED_RULE
            except Exception:
                rule_g = NULL.PLANTED_RULE
            region = G.to_mask(rule_g, fs.search) & fs.search.visible["executable"]
            n_tr = 0
            n_flip = 0
            n_in = 0
            for c, r in res_planted.items():
                if r is None or not r.trades:
                    continue
                tr = np.asarray(r.trade_rows, dtype=np.int64)
                n_tr += int(tr.shape[0])
                n_flip += int(sum(1 for x in tr.tolist() if int(x) in flipped_set))
                n_in += int(np.count_nonzero(region[tr]))
            rule_delta = r0["info"].get("rule_pooled_validation_delta")
            block.update({
                "pick_validation_trades": n_tr,
                "pick_flipped_trades": n_flip,
                "pick_rule_overlap": (n_in / n_tr) if n_tr else math.nan,
                "rule_capture_ratio": (float(rule_delta) / float(edge)) if (rule_delta is not None and edge) else math.nan,
                "n_rows_flipped": int(flipped.shape[0]),
                "note": ("capture_ratio (the section 6.4 gate) is pick-level: (picks pooled on the planted frame - picks pooled on the "
                         "original frame) / edge. With ~48 pick validation trades each flipped trade moves it by ~0.36, so it has "
                         "one-trade granularity and a ~78% pass rate across re-plantings of the same picks (red team F2). "
                         "rule_capture_ratio is the planted rule's own validation delta / edge (deterministic to rounding); "
                         "pick_rule_overlap says how much of the picks' trade set lies inside the planted region."),
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
