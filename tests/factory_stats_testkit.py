"""Test kit for the F2 STATS workstream (NOT a test module).

* ``synthetic_dev_frameset``: a synthetic ``FrameSet`` whose 69 dates are the
  real development calendar (``folds.DEV_DATES``) so the campaign calendar,
  the null transforms and the report run on it unchanged.
* ``fake_run_procedure``: a stand-in for ``src.factory.procedure.run_procedure``
  with the contract's signature that writes a REAL ledger (``ledger.Ledger``,
  write-then-evaluate), ``picks.json``, ``oos/pooled.json`` and
  ``status.json`` in the contract's layout and returns a ProcedureResult-shaped
  object. It scores a fixed population (the seeds plus seeded random genomes)
  over two "generations" with ``fitness.score`` on the campaign-stripped
  frames and applies the pre-registered picker (max ``boot_lo`` among
  constraint-satisfying rows; ties -> fewer clauses; then genome_id). When no
  row satisfies the constraints (small synthetic frames) it falls back to the
  best finite ``boot_lo`` -- a fake-only convenience, flagged in picks.json.
* ``write_fake_run``: ``run.json`` + ``folds.json`` as EVOLVE's ``run`` writes
  them, then ``fake_run_procedure`` on campaigns A/B/C/ALL69.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from src.factory import controls as CT
from src.factory import fitness as FT
from src.factory import folds
from src.factory import genome as G
from src.factory.frame import FrameSet, frame_sha256
from src.factory.ledger import Ledger, genome_id
from src.factory.report import write_json
from tests import factory_testkit as K

REPO = Path(__file__).resolve().parents[1]
REAL_FRAMES = REPO.parent.parent.parent / "data" / "factory" / "frames" / "weather_2026-07-25_bfcf94654a3a"
if not REAL_FRAMES.exists():
    REAL_FRAMES = REPO / "data" / "factory" / "frames" / "weather_2026-07-25_bfcf94654a3a"
if not REAL_FRAMES.exists():
    _alt = Path("W:/Hoya_Space/Projects/money_printer/data/factory/frames/weather_2026-07-25_bfcf94654a3a")
    if _alt.exists():
        REAL_FRAMES = _alt


def _stamp(fr):
    fr.provenance = dict(fr.provenance or {})
    fr.provenance["frame_sha256"] = frame_sha256(fr)
    return fr


def synthetic_dev_frameset(n_per_city_date: int = 1, n_snapshots: int = 4, seed: int = 3) -> FrameSet:
    """A synthetic FrameSet over the 69 development dates (4 cities x ``n_per_city_date`` markets per date)."""
    n_dates = len(folds.DEV_DATES)
    n_markets = n_dates * 4 * int(n_per_city_date)
    search = K.synthetic_frame(n_markets=n_markets, n_snapshots=n_snapshots, n_dates=n_dates, seed=seed, name="search")
    search.dates = np.asarray(folds.DEV_DATES, dtype=str)
    parity = K.copy_frame(search, name="parity")
    twin = K.copy_frame(search, name="gefs_twin")
    search.twin_index = np.arange(search.n_rows, dtype=np.int64)
    for fr in (parity, search, twin):
        fr.validate()
        _stamp(fr)
    return FrameSet(parity=parity, search=search, gefs_twin=twin, provenance={"lane": "weather", "synthetic": True})


def _pick_key(row: Dict[str, Any]):
    return (-float(row["boot_lo"]), int(row["n_active_clauses"]), str(row["genome_id"]))


def fake_run_procedure(
    fs: FrameSet,
    config: Dict[str, Any],
    run_dir: Path,
    *,
    campaigns: Sequence[str] = ("A", "B", "C", "ALL69"),
    blocked_folds: bool = False,
    cfg: Any = None,
    master_seed: int = 0,
    frame_dir: Optional[str] = None,
    resume: bool = False,
    log=print,
    n_random: int = 10,
    generations: int = 2,
):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    camps = folds.campaigns([str(d) for d in fs.search.dates])
    rng = np.random.default_rng(int(master_seed))
    seeds = [G.SEEDS[n] for n in ("nofilter_no", "fr31a_taker", "mlweather_fallback", "far_yes_taker")]
    picks: Dict[str, Any] = {}
    picks_ns: Dict[str, Any] = {}
    per_date: List[Dict[str, Any]] = []
    evaluations = 0
    n_phen: Dict[str, int] = {}
    for c in campaigns:
        camp = camps[c]
        s, t = folds.strip_to_campaign(fs.search, fs.gefs_twin, camp)
        led = Ledger(run_dir, c)
        if resume and led.generations():
            gens_done = led.generations()
        else:
            gens_done = []
        rows: List[Dict[str, Any]] = []
        for gen in range(int(generations)):
            pop = (seeds if gen == 0 else []) + [G.Genome.random(rng) for _ in range(int(n_random))]
            if gen in gens_done:
                tbl = led.read_gen(gen).to_pylist()
                for r in tbl:
                    rows.append(dict(r, gen=gen))
                continue
            led.append_unscored(gen, pop)
            results = [FT.score(s, G.to_mask(g, s), constraints=True, twin=t, genome=g, label=g.name) for g in pop]
            # like evolve: ledger per_date_codes index the PARENT (full search) frame's dates
            parent_idx = {str(d): i for i, d in enumerate(fs.search.dates)}
            cmap = np.asarray([parent_idx[str(d)] for d in s.dates], dtype=np.int64)
            for r in results:
                if np.asarray(r.per_date_codes).size:
                    r.per_date_codes = cmap[np.asarray(r.per_date_codes, dtype=np.int64)].astype(np.int16)
            led.mark_scored(gen, results)
            evaluations += len(pop)
            for i, (g, r) in enumerate(zip(pop, results)):
                d = r.as_dict()
                d.pop("trade_rows", None)
                rows.append({
                    "gen": gen, "idx": i, "genome": g, "genome_json": g.to_json_str(),
                    "genome_id": genome_id(g.to_json_str()),
                    "phenotype_hash": r.phenotype_hash, "boot_lo": r.boot_lo, "passed": r.passed,
                    "n_active_clauses": G.n_active_clauses(g), "in_sample": d,
                })
        cands = [r for r in rows if r.get("passed") and "genome" in r]
        fallback = False
        if not cands:
            fallback = True
            cands = [r for r in rows if "genome" in r and r["boot_lo"] == r["boot_lo"] and r["in_sample"]["trades"] > 0]
        if not cands:
            raise RuntimeError(f"fake procedure: no scoreable genome on campaign {c}")
        best = sorted(cands, key=_pick_key)[0]
        g = best["genome"]
        val = CT.score_validation(fs, g, camp)
        val_d = None
        if val is not None:
            val_d = val.as_dict()
            val_d.pop("trade_rows", None)
            if val.trades:
                per_date.extend(CT.per_date_rows(fs.search, val, c))
        n_phen[c] = len(led.phenotypes())
        picks[c] = {
            "genome_json": best["genome_json"], "genome_id": best["genome_id"], "phenotype_hash": best["phenotype_hash"],
            "picked_gen": int(best["gen"]), "in_sample": best["in_sample"], "validation": val_d,
            "n_candidates": len(cands), "fake_fallback_pick": fallback,
        }
        picks_ns[c] = SimpleNamespace(campaign=c, genome=g, genome_json=best["genome_json"], genome_id=best["genome_id"],
                                      phenotype_hash=best["phenotype_hash"], picked_gen=int(best["gen"]),
                                      in_sample=best["in_sample"], validation=val, n_candidates=len(cands))
        write_json(run_dir / "picks.json", picks)
        log(f"fake procedure: {c}: pick {best['genome_id']} boot_lo {best['boot_lo']:+.4f} fallback={fallback}")
    pooled = CT.pooled_stats([r["pnl"] for r in per_date])
    pooled["per_date"] = per_date
    write_json(run_dir / "oos" / "pooled.json", pooled)
    write_json(run_dir / "status.json", {
        "run_id": run_dir.name, "state": "DONE", "phase": "done", "campaign": None, "gen": int(generations) - 1,
        "n_gens": int(generations), "best_fit": None, "n_phenotypes": sum(n_phen.values()),
        "evaluations": evaluations, "picks_done": list(campaigns), "controls_done": {},
    })
    return SimpleNamespace(run_dir=run_dir, picks=picks_ns, pooled=pooled, folds={}, folds_pooled=None,
                           evaluations=evaluations, n_phenotypes=n_phen)


def write_fake_run(fs: FrameSet, run_dir: Path, config: Dict[str, Any], *, master_seed: int = 20260902, frames_dir: Optional[Path] = None, **kw) -> Any:
    """``run.json`` + ``folds.json`` like EVOLVE's ``run``, then the fake procedure on A/B/C/ALL69."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    camps = folds.campaigns([str(d) for d in fs.search.dates])
    write_json(run_dir / "run.json", {
        "run_id": run_dir.name, "kind": "run", "frames_dir": str(frames_dir) if frames_dir else None,
        "frames": {"parity": fs.parity.provenance.get("frame_sha256"), "search": fs.search.provenance.get("frame_sha256"),
                   "gefs_twin": fs.gefs_twin.provenance.get("frame_sha256") if fs.gefs_twin is not None else None},
        "config_sha256": config.get("_config_sha256"), "lock_sha256": "l" * 64, "git_rev": "deadbeef" * 5,
        "master_seed": int(master_seed), "budget": dict(config.get("budget") or {}),
    })
    write_json(run_dir / "folds.json", {c: camps[c].as_dict() for c in camps})
    return fake_run_procedure(fs, config, run_dir, master_seed=master_seed, **kw)


def base_config(tmp_path: Path) -> Dict[str, Any]:
    import yaml

    p = REPO / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["_config_path"] = str(p)
    cfg["_config_sha256"] = "c" * 64
    cfg["repo_root"] = str(tmp_path)
    cfg["registry_path"] = str(tmp_path / "reports" / "factory" / "registry.jsonl")
    return cfg


def load_real_frameset():
    """The frozen real FrameSet or None when it is not on this machine."""
    if not REAL_FRAMES.exists():
        return None
    from src.factory.gen0 import load_frameset

    return load_frameset(REAL_FRAMES)
