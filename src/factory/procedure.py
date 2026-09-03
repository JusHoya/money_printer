"""The full pre-registered procedure: campaigns, picks, ONE validation score, pooled OOS (FR-F2.2).

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 6.1 (anchored
walk-forward campaigns A/B/C/ALL69 with a 2-day embargo -- the headline), 6.2
(blocked 5-fold with a 2-day purge -- diagnostic only, label "in-sample blocks
postdate the held block"), 7.3 (run.json, resume refusal on a different frame
or lock hash), 8 (run directory layout).

Order of operations per campaign (``run_procedure``):

1. ``evolve.run_campaign`` -- the frames are stripped to the search window
   BEFORE the pool exists; workers never see validation or embargo rows.
2. ``evolve.pick`` over the campaign ledger -> ``picks.json`` is written with
   ``validation = null`` (checkpoint) BEFORE any validation row is read.
3. The pick is scored ONCE on the validation block by the main process
   (``score_on_dates`` on the FULL search frame + twin, constraints off) and
   ``picks.json`` is rewritten. A campaign whose pick is already validated in
   ``picks.json`` is skipped on resume.

Pooled OOS = the per-date PnLs of the A/B/C validation blocks (12 + 12 + 9
= 33 calendar dates; a validation date on which the pick did not trade
contributes NOTHING -- no zero-fill -- and ``n_dates`` counts the dates
actually traded). ``mean`` is the mean of the per-date mean realized PnL per
contract (the ``fitness.score`` / ``evaluate_shape`` convention: four cities
under one synoptic pattern are one draw), ``se`` clusters by date, the CI is
the seeded ``fitness.bootstrap_draws`` over dates. The trade-weighted mean is
reported beside it as ``trade_weighted_mean``.

The ledger ``per_date_codes`` written by ``evolve`` index the FULL search
frame's ``Frame.dates``; ``folds.json`` carries that date list as ``dates``.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from src.factory import evolve
from src.factory import fitness
from src.factory import folds
from src.factory import genome as G
from src.factory.columns import Frame
from src.factory.evolve import EvolveConfig, Pick  # re-exported
from src.factory.report import write_json

HEADLINE_CAMPAIGNS: Tuple[str, ...] = ("A", "B", "C")
DEFAULT_CAMPAIGNS: Tuple[str, ...] = ("A", "B", "C", "ALL69")
FOLD_K = 5
FOLD_PURGE_DAYS = 2
FOLDS_LABEL = "in-sample blocks postdate the held block"
#: FitnessResult array fields dropped from picks.json (per_date_* stay)
DROP_ARRAYS = ("trade_rows",)


class ProcedureError(RuntimeError):
    """A procedure precondition failed (loud, PRD_STRATEGY_FACTORY section 6)."""


@dataclass
class ProcedureResult:
    run_dir: Path
    picks: Dict[str, Pick]
    pooled: Dict[str, Any]
    folds: Dict[str, Pick] = field(default_factory=dict)
    folds_pooled: Optional[Dict[str, Any]] = None
    evaluations: int = 0
    n_phenotypes: Dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0  # wall time of THIS call (printed, never written to run artefacts)
    scored_now: int = 0  # evaluations performed by THIS call (``evaluations`` counts the rows on disk)
    score_seconds: float = 0.0  # pure scoring wall time of this call (throughput print only)


# ---------------------------------------------------------------------------
# calendar helpers
# ---------------------------------------------------------------------------
def validation_dates(campaign: folds.Campaign) -> Tuple[str, ...]:
    return tuple(campaign.validation_dates)


def pooled_validation_dates(camps: Dict[str, folds.Campaign]) -> List[str]:
    """The pooled OOS calendar: A(12) + B(12) + C(9) = 33 dates on the full development window."""
    out: List[str] = []
    for name in HEADLINE_CAMPAIGNS:
        if name in camps:
            out.extend(camps[name].validation_dates)
    return out


def fold_campaigns(frame_dates: Sequence[str], k: int = FOLD_K, purge_days: int = FOLD_PURGE_DAYS) -> Dict[str, folds.Campaign]:
    """Blocked k-fold as campaigns ``F1..Fk``: search = train, embargo = purge, validation = held."""
    out: Dict[str, folds.Campaign] = {}
    for f in folds.blocked_kfold(frame_dates, k=k, purge_days=purge_days):
        name = f"F{f.index + 1}"
        out[name] = folds.Campaign(name, tuple(f.train), tuple(f.purge), tuple(f.held))
    return out


# ---------------------------------------------------------------------------
# scoring helpers (main process only)
# ---------------------------------------------------------------------------
def score_on_dates(
    F: Frame,
    twin: Optional[Frame],
    genome: G.Genome,
    dates: Sequence[str],
    *,
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
    constraints: bool = False,
    label: str = "",
) -> fitness.FitnessResult:
    """Score ``genome`` on the rows of ``F`` whose target date is in ``dates`` (main process only)."""
    dm = folds.date_mask(F, dates)
    return fitness.score(F, G.to_mask(genome, F), date_mask=dm, twin=twin, genome=genome, n_boot=n_boot,
                         seed=seed, constraints=constraints, label=label)


def pooled_stats(per_date_pnl: np.ndarray, *, n_boot: int = fitness.DEFAULT_N_BOOT,
                 seed: int = fitness.DEFAULT_SEED) -> Dict[str, Any]:
    """Date-clustered statistics of a per-date PnL vector (architecture section 5 step 5)."""
    v = np.asarray(per_date_pnl, dtype=np.float64)
    n = int(v.shape[0])
    if n == 0:
        return {"n_dates": 0, "mean": None, "se": None, "t_stat": None, "boot_lo": None, "boot_hi": None}
    mean = float(v.mean())
    se = float(v.std(ddof=1) / math.sqrt(n)) if n > 1 else None
    t = float(mean / se) if (se is not None and se > 0) else None
    draws = fitness.bootstrap_draws(v, n_boot=n_boot, seed=seed)
    lo, hi = (float(x) for x in np.percentile(draws, [2.5, 97.5]))
    return {"n_dates": n, "mean": mean, "se": se, "t_stat": t, "boot_lo": lo, "boot_hi": hi}


def per_date_trades(F: Frame, res: fitness.FitnessResult) -> List[int]:
    """Trades per traded date (aligned with ``res.per_date_codes``), from ``trade_rows``."""
    codes = np.asarray(res.per_date_codes, dtype=np.int64)
    if codes.size == 0:
        return []
    rows = np.asarray(res.trade_rows, dtype=np.int64)
    counts = np.bincount(F.visible["target_date_code"][rows], minlength=F.n_dates)
    return [int(counts[c]) for c in codes]


def per_date_rows(F: Frame, res: fitness.FitnessResult, campaign: str,
                  trades: Optional[Sequence[int]] = None) -> List[Dict[str, Any]]:
    """``[{date, campaign, pnl, trades}]`` for the dates the result traded (no zero-fill).

    ``trades`` (from ``picks.json``) replaces the ``trade_rows`` count when the
    result was restored from disk and carries no ``trade_rows``.
    """
    codes = np.asarray(res.per_date_codes, dtype=np.int64)
    if codes.size == 0:
        return []
    counts = list(trades) if trades is not None else per_date_trades(F, res)
    if len(counts) != codes.size:
        raise ProcedureError(f"{campaign}: per_date_trades has {len(counts)} entries for {codes.size} dates")
    return [
        {"date": str(F.dates[c]), "campaign": campaign, "pnl": float(p), "trades": int(t)}
        for c, p, t in zip(codes, np.asarray(res.per_date_pnl, dtype=np.float64), counts)
    ]


def pool_per_date(rows: Sequence[Dict[str, Any]], *, n_boot: int, seed: int, label: Optional[str] = None,
                  n_calendar_dates: Optional[int] = None) -> Dict[str, Any]:
    """The ``oos/pooled.json`` document from per-date rows."""
    rows = sorted(rows, key=lambda r: (r["date"], r["campaign"]))
    v = np.asarray([r["pnl"] for r in rows], dtype=np.float64)
    w = np.asarray([r["trades"] for r in rows], dtype=np.float64)
    stats = pooled_stats(v, n_boot=n_boot, seed=seed)
    doc: Dict[str, Any] = {
        "per_date": list(rows),
        "n_trades": int(w.sum()),
        "trade_weighted_mean": float((v * w).sum() / w.sum()) if w.sum() > 0 else None,
        "mean_definition": (
            "mean of per-date mean realized PnL per contract (fitness.score / evaluate_shape "
            "convention); se = std(ddof=1)/sqrt(n_dates); boot CI = seeded fitness.bootstrap_draws "
            "over dates; dates with no trade are absent (no zero-fill)"
        ),
        "n_boot": int(n_boot),
        "bootstrap_seed": int(seed),
        "campaigns": sorted({r["campaign"] for r in rows}),
    }
    doc.update(stats)
    if n_calendar_dates is not None:
        doc["n_calendar_dates"] = int(n_calendar_dates)
    if label is not None:
        doc["label"] = label
    return doc


# ---------------------------------------------------------------------------
# picks.json
# ---------------------------------------------------------------------------
def _result_dict(res: Optional[fitness.FitnessResult], F: Optional[Frame] = None) -> Optional[Dict[str, Any]]:
    if res is None:
        return None
    d = res.as_dict()
    for k in DROP_ARRAYS:
        d.pop(k, None)
    if F is not None:
        d["per_date_dates"] = [str(F.dates[int(c)]) for c in np.asarray(res.per_date_codes, dtype=np.int64)]
        if np.asarray(res.trade_rows).size or not np.asarray(res.per_date_codes).size:
            d["per_date_trades"] = per_date_trades(F, res)
    d["passed"] = bool(res.passed)
    return d


def pick_to_dict(p: Pick, *, F: Optional[Frame] = None, n_validation_dates: int = 0, validation_done: bool = False) -> Dict[str, Any]:
    return {
        "campaign": p.campaign,
        "genome_json": p.genome_json,
        "genome_id": p.genome_id,
        "phenotype_hash": p.phenotype_hash,
        "picked_gen": p.picked_gen,
        "in_sample": _result_dict(p.in_sample, F),
        "validation": _result_dict(p.validation, F),
        "n_candidates": int(p.n_candidates),
        "reason": p.reason,
        "n_validation_dates": int(n_validation_dates),
        "validation_done": bool(validation_done),
    }


def pick_from_dict(d: Dict[str, Any]) -> Pick:
    """Inverse of :func:`pick_to_dict` (arrays restored; ``trade_rows`` absent)."""

    def _res(x: Optional[Dict[str, Any]]) -> Optional[fitness.FitnessResult]:
        if x is None:
            return None
        kw = {k: v for k, v in x.items() if k in fitness.FitnessResult.__dataclass_fields__}
        for k in ("per_date_pnl", "per_date_codes", "trade_rows"):
            if k in kw and kw[k] is not None:
                kw[k] = np.asarray(kw[k])
            else:
                kw.pop(k, None)
        for k, v in list(kw.items()):
            if v is None and k in ("fit", "realized", "realized_se", "t_stat", "boot_lo", "boot_hi",
                                   "worst_date_pnl", "bss_trades", "gefs_twin_realized", "modelled_ev",
                                   "fill_opportunity_rate", "win_rate", "mean_price_paid", "mean_fee",
                                   "mean_model_p_yes", "mean_market_yes_ask", "realized_yes_rate"):
                kw[k] = fitness.NEG_INF if k == "fit" else fitness.NAN
        return fitness.FitnessResult(**kw)

    g = G.Genome.from_json(d["genome_json"]) if d.get("genome_json") else None
    val = d.get("validation")
    return Pick(
        campaign=str(d["campaign"]),
        genome=g,
        genome_json=d.get("genome_json"),
        genome_id=d.get("genome_id"),
        phenotype_hash=d.get("phenotype_hash"),
        picked_gen=d.get("picked_gen"),
        in_sample=_res(d.get("in_sample")),
        validation=_res(val),
        n_candidates=int(d.get("n_candidates") or 0),
        reason=d.get("reason"),
        validation_per_date_trades=(list(val["per_date_trades"]) if val and val.get("per_date_trades") is not None else None),
    )


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# run.json
# ---------------------------------------------------------------------------
def _frame_shas(fs: Any) -> Dict[str, Optional[str]]:
    from src.factory import frame as frame_mod

    out: Dict[str, Optional[str]] = {}
    for name in ("parity", "search", "gefs_twin"):
        fr = getattr(fs, name, None)
        out[name] = frame_mod.frame_sha256(fr) if fr is not None else None
    return out


def write_or_check_run_json(run_dir: Path, fs: Any, config: Dict[str, Any], cfg: EvolveConfig, *, master_seed: int,
                            campaigns: Sequence[str], blocked_folds: bool, frame_dir: Optional[str],
                            resume: bool) -> Dict[str, Any]:
    """Write ``run.json`` (kind="run") or, on resume, refuse a different frame / lock hash."""
    path = run_dir / "run.json"
    shas = _frame_shas(fs)
    doc: Dict[str, Any] = {
        "run_id": str(config.get("run_id") or run_dir.name),
        "kind": str(config.get("kind") or "run"),
        "frames_dir": (str(frame_dir).replace("\\", "/") if frame_dir else None),
        "frames": shas,
        "config_sha256": config.get("_config_sha256") or config.get("config_sha256"),
        "config_path": config.get("_config_path") or config.get("config_path"),
        "lock_sha256": config.get("lock_sha256"),
        "lock_file": config.get("lock_file"),
        "git_rev": config.get("git_rev"),
        "fee_regime_sha256": config.get("fee_regime_sha256"),
        "family": config.get("family"),
        "master_seed": int(master_seed),
        "budget": cfg.as_dict(),
        "campaigns": list(campaigns),
        "blocked_folds": bool(blocked_folds),
        "fold_k": FOLD_K,
        "fold_purge_days": FOLD_PURGE_DAYS,
        "versions": config.get("versions"),
        "host": config.get("host"),
    }
    existing = _load_json(path)
    if existing is not None:
        if not resume:
            raise ProcedureError(f"{path} exists; a run_id is never overwritten (use resume)")
        for key in ("search", "gefs_twin", "parity"):
            if existing.get("frames", {}).get(key) != shas.get(key):
                raise ProcedureError(
                    f"resume refused: {key} frame sha256 {str(existing.get('frames', {}).get(key))[:12]} in "
                    f"{path.name} != {str(shas.get(key))[:12]} now"
                )
        if existing.get("lock_sha256") != doc["lock_sha256"]:
            raise ProcedureError(
                f"resume refused: lock_sha256 {str(existing.get('lock_sha256'))[:12]} in {path.name} != "
                f"{str(doc['lock_sha256'])[:12]} now"
            )
        if int(existing.get("master_seed", master_seed)) != int(master_seed):
            raise ProcedureError("resume refused: master_seed differs from run.json")
        # workers / chunksize never change a result (ordered imap); everything else does
        strip = ("workers", "chunksize")
        old_b = {k: v for k, v in (existing.get("budget") or {}).items() if k not in strip}
        new_b = {k: v for k, v in doc["budget"].items() if k not in strip}
        if old_b != new_b:
            raise ProcedureError(f"resume refused: budget differs from run.json ({old_b} vs {new_b})")
        return existing
    write_json(path, doc)
    return doc


def _latest_json_update(path: Path, run_id: str) -> None:
    """``reports/factory/latest.json``: add ``active_run`` + ``status`` (keeps every other key)."""
    doc = _load_json(path) or {}
    doc["active_run"] = run_id
    doc["status"] = f"{run_id}/status.json"
    write_json(path, doc)


# ---------------------------------------------------------------------------
# the procedure
# ---------------------------------------------------------------------------
def run_procedure(
    fs: Any,
    config: Dict[str, Any],
    run_dir: Union[str, Path],
    *,
    campaigns: Sequence[str] = DEFAULT_CAMPAIGNS,
    blocked_folds: bool = False,
    cfg: EvolveConfig,
    master_seed: int,
    frame_dir: Optional[str] = None,
    resume: bool = False,
    log: Callable[[str], Any] = print,
    on_generation: Optional[Callable[[str, int, Dict[str, Any]], Any]] = None,
) -> ProcedureResult:
    """Run (or resume) the full procedure into ``run_dir`` (module docstring).

    ``config`` is the family YAML dict plus the CLI's runtime keys (``run_id``,
    ``git_rev``, ``lock_sha256``, ``_config_sha256`` ...). Two private keys
    drive reporting side effects and are set by the CLI only:
    ``_status_mirror`` (path of the tracked ``status.json`` copy) and
    ``_latest_json`` (``reports/factory/latest.json`` to point at this run).
    Control replicates (STATS) call this with neither.
    """
    t0 = time.perf_counter()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(config.get("run_id") or run_dir.name)
    campaigns = tuple(campaigns)

    frame_dates = [str(d) for d in fs.search.dates]
    all_camps = folds.campaigns(frame_dates)
    unknown = [c for c in campaigns if c not in all_camps]
    if unknown:
        raise ProcedureError(f"unknown campaign(s) {unknown}; have {sorted(all_camps)}")
    camps: Dict[str, folds.Campaign] = {c: all_camps[c] for c in campaigns}
    fcamps: Dict[str, folds.Campaign] = fold_campaigns(frame_dates) if blocked_folds else {}

    run_doc = write_or_check_run_json(run_dir, fs, config, cfg, master_seed=master_seed, campaigns=campaigns,
                                      blocked_folds=blocked_folds, frame_dir=frame_dir, resume=resume)
    write_json(run_dir / "folds.json", {
        "campaigns": {k: v.as_dict() for k, v in camps.items()},
        "blocked_folds": {k: v.as_dict() for k, v in fcamps.items()},
        "fold_k": FOLD_K,
        "fold_purge_days": FOLD_PURGE_DAYS,
        "folds_label": FOLDS_LABEL,
        "dates": frame_dates,
        "pooled_validation_dates": pooled_validation_dates(camps),
        "ledger_per_date_codes": "index into this file's 'dates' (the full search frame's Frame.dates)",
    })

    status_mirror = Path(config["_status_mirror"]) if config.get("_status_mirror") else None
    if config.get("_latest_json"):
        _latest_json_update(Path(config["_latest_json"]), run_id)
    status_path = run_dir / "status.json"
    status: Dict[str, Any] = _load_json(status_path) if resume else None  # type: ignore[assignment]
    if not status:
        status = {"run_id": run_id, "picks_done": [], "controls_done": {}, "evaluations": 0}
    status.update({"run_id": run_id, "state": evolve.STATE_RUNNING, "phase": "start", "campaign": None,
                   "gen": None, "n_gens": int(cfg.generations)})
    status.setdefault("best_fit", None)
    status.setdefault("n_phenotypes", 0)
    evolve.write_status(status_path, status, status_mirror)

    picks_path = run_dir / "picks.json"
    picks_doc: Dict[str, Any] = (_load_json(picks_path) or {}) if resume else {}
    if picks_doc and not resume:
        raise ProcedureError(f"{picks_path} exists; pass resume=True")

    picks: Dict[str, Pick] = {}
    fpicks: Dict[str, Pick] = {}
    evaluations = 0
    scored_now = 0
    score_seconds = 0.0
    n_phenotypes: Dict[str, int] = {}
    search, twin = fs.search, fs.gefs_twin

    def _write_picks() -> None:
        write_json(picks_path, picks_doc)

    try:
        for name, camp in list(camps.items()) + list(fcamps.items()):
            target = fpicks if name in fcamps else picks
            n_val = len(camp.validation_dates)
            done = picks_doc.get(name)
            if done and done.get("validation_done"):
                target[name] = pick_from_dict(done)
                led = evolve.ledger_mod.Ledger(run_dir, name)
                summ = led.summary()
                evaluations += int(summ["n_rows"])
                n_phenotypes[name] = int(summ["n_phenotypes"])
                if name not in status["picks_done"]:
                    status["picks_done"].append(name)
                log(f"[{name}] already picked and validated; skipping")
                continue

            # 1. evolve (workers see the stripped frame only)
            cres = evolve.run_campaign(fs, camp, cfg, run_dir, master_seed=master_seed, frame_dir=frame_dir,
                                       log=log, on_generation=on_generation, resume=resume, status=status,
                                       status_mirror=status_mirror)
            evaluations += cres.evaluations
            scored_now += cres.scored_now
            score_seconds += cres.score_seconds
            n_phenotypes[name] = cres.n_phenotypes

            # 2. pick + checkpoint BEFORE any validation row is read
            status.update({"phase": "pick", "campaign": name})
            evolve.write_status(status_path, status, status_mirror)
            p = evolve.pick(cres.ledger, cfg, campaign=name)
            if not p.empty:
                # the parent's own re-score on the campaign frame is the full in-sample record (and a check)
                assert p.genome is not None
                r_in = evolve.score_genome(p.genome, cres.search, cres.twin, n_boot=cfg.n_boot, seed=cfg.boot_seed)
                if p.in_sample is not None and not (math.isclose(r_in.fit, p.in_sample.fit, rel_tol=0, abs_tol=0)):
                    raise ProcedureError(f"[{name}] ledger fit {p.in_sample.fit} != re-scored fit {r_in.fit}")
                full = fitness.score(cres.search, G.to_mask(p.genome, cres.search), twin=cres.twin, genome=p.genome,
                                     n_boot=cfg.n_boot, seed=cfg.boot_seed, constraints=True, label=f"{name}/in_sample")
                # per-date codes of the campaign frame -> the full frame's calendar
                parent = [str(d) for d in search.dates]
                cmap = np.asarray([parent.index(str(d)) for d in cres.search.dates], dtype=np.int64)
                full.per_date_codes = cmap[np.asarray(full.per_date_codes, dtype=np.int64)].astype(np.int16)
                p.in_sample = full
            picks_doc[name] = pick_to_dict(p, F=search, n_validation_dates=n_val, validation_done=False)
            _write_picks()
            log(f"[{name}] pick: {p.genome_id} gen {p.picked_gen} fit {None if p.in_sample is None else round(p.in_sample.fit, 5)} "
                f"candidates {p.n_candidates}{' ' + p.reason if p.reason else ''}")

            # 3. validation: ONCE, main process, full frame restricted to the validation dates
            status.update({"phase": "validate"})
            evolve.write_status(status_path, status, status_mirror)
            if not p.empty and n_val:
                assert p.genome is not None
                p.validation = score_on_dates(search, twin, p.genome, camp.validation_dates, n_boot=cfg.n_boot,
                                              seed=cfg.boot_seed, constraints=False, label=f"{name}/validation")
                log(f"[{name}] validation: trades {p.validation.trades} dates {p.validation.dates}/{n_val} "
                    f"realized {p.validation.realized:+.4f} [{p.validation.boot_lo:+.4f}, {p.validation.boot_hi:+.4f}]")
            picks_doc[name] = pick_to_dict(p, F=search, n_validation_dates=n_val, validation_done=True)
            _write_picks()
            target[name] = p
            if name not in status["picks_done"]:
                status["picks_done"].append(name)
            status["evaluations"] = int(evaluations)
            evolve.write_status(status_path, status, status_mirror)

        # pooled OOS over the headline campaigns
        status.update({"phase": "pooled", "campaign": None})
        rows: List[Dict[str, Any]] = []
        missing: List[str] = []
        for name in HEADLINE_CAMPAIGNS:
            p = picks.get(name)
            if p is None or p.validation is None:
                if name in camps:
                    missing.append(name)
                continue
            rows.extend(per_date_rows(search, p.validation, name, trades=p.validation_per_date_trades))
        pooled = pool_per_date(rows, n_boot=cfg.n_boot, seed=cfg.boot_seed,
                               n_calendar_dates=len(pooled_validation_dates(camps)))
        pooled["picks_missing"] = missing
        pooled["picks"] = {n: (picks[n].genome_id if n in picks else None) for n in HEADLINE_CAMPAIGNS if n in camps}
        write_json(run_dir / "oos" / "pooled.json", pooled)

        folds_pooled: Optional[Dict[str, Any]] = None
        if fcamps:
            frows: List[Dict[str, Any]] = []
            fmissing: List[str] = []
            for name in fcamps:
                p = fpicks.get(name)
                if p is None or p.validation is None:
                    fmissing.append(name)
                    continue
                frows.extend(per_date_rows(search, p.validation, name, trades=p.validation_per_date_trades))
            folds_pooled = pool_per_date(frows, n_boot=cfg.n_boot, seed=cfg.boot_seed, label=FOLDS_LABEL,
                                         n_calendar_dates=len(frame_dates))
            folds_pooled["picks_missing"] = fmissing
            folds_pooled["picks"] = {n: (fpicks[n].genome_id if n in fpicks else None) for n in fcamps}
            write_json(run_dir / "oos" / "folds_pooled.json", folds_pooled)

        status.update({"state": evolve.STATE_DONE, "phase": "done", "campaign": None, "evaluations": int(evaluations),
                       "n_phenotypes": int(sum(n_phenotypes.values()))})
        evolve.write_status(status_path, status, status_mirror)
    except BaseException:
        status.update({"state": evolve.STATE_FAILED})
        try:
            evolve.write_status(status_path, status, status_mirror)
        except Exception:  # pragma: no cover - never mask the original error
            pass
        raise

    frames_dir = run_dir / "frames"  # spawn-only scratch; every campaign removed its own subdir
    if frames_dir.is_dir() and not any(frames_dir.iterdir()):
        frames_dir.rmdir()
    elapsed = time.perf_counter() - t0
    log(f"procedure {run_id}: {evaluations} evaluations on disk; pooled OOS n_dates {pooled['n_dates']} "
        f"mean {pooled['mean']} boot_lo {pooled['boot_lo']}")
    return ProcedureResult(run_dir=run_dir, picks=picks, pooled=pooled, folds=fpicks, folds_pooled=folds_pooled,
                           evaluations=evaluations, n_phenotypes=n_phenotypes, elapsed_s=elapsed,
                           scored_now=scored_now, score_seconds=score_seconds)


__all__ = [
    "DEFAULT_CAMPAIGNS",
    "FOLDS_LABEL",
    "HEADLINE_CAMPAIGNS",
    "EvolveConfig",
    "Pick",
    "ProcedureError",
    "ProcedureResult",
    "fold_campaigns",
    "per_date_rows",
    "pick_from_dict",
    "pick_to_dict",
    "pool_per_date",
    "pooled_stats",
    "pooled_validation_dates",
    "run_procedure",
    "score_on_dates",
    "validation_dates",
    "write_or_check_run_json",
]
