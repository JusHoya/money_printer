"""Generation 0: score the pre-registered seeds, settlement-true, on every frame and campaign.

PRD_STRATEGY_FACTORY FR-F1.4-F1.6 and the Phase F1 exit criteria; design record
``docs/factory/FACTORY_ARCHITECTURE.md`` sections 5 (fitness), 6.1 (campaigns),
7.3 (timestamp-free artefacts) and 8 (storage layout).

``run_gen0(config, out_dir) -> summary`` is called by ``scripts/factory.py gen0``
(see that module's docstring for the ``config`` keys). Order of operations,
which is the point of the module:

1. frames: ``frame.load`` the frozen ``parity/``, ``search/``, ``gefs_twin/``
   under ``config["frames_dir"]`` (the layout ``freeze-frame`` writes), else
   build them with ``WeatherLane().build_frames`` and save them the same way;
2. the registry family line is written (or, on an idempotent rerun, reused
   and its ``config_sha256`` re-checked) BEFORE any ``fitness.score`` call;
3. every seed in ``genome.SEEDS`` is scored: ``parity_full`` (parity frame,
   constraints on), ``search_full`` (search frame with the gefs twin,
   constraints on; ``fr31a_gefs`` is scored on the gefs twin frame itself),
   and per campaign A/B/C/ALL69 a ``search`` row (constraints on) and a
   ``validation`` row (constraints OFF -- a report row, never a selection);
4. the four Phase-2 taker shapes are compared leaf by leaf, at 1e-9, with
   ``reports/phase2/ws_e_go_no_go_data_2026-07-26.json``;
5. the frame-level Brier skill vs the market mid is computed on the parity
   and search frames with a date-clustered CI.

``summary.json`` carries no wall-clock value (section 7.3): the registry line
is copied WITHOUT its ``ts``; ``run_id`` is whatever the CLI passed in. The
per-date PnL vectors go to ``seed_date_pnl.json`` beside the summary, not
into the summary itself. ``throughput`` is left ``None`` -- the CLI's
``--bench`` fills it before ``report.write_gen0_report``.

Every ``score`` call goes through the module attribute ``fitness.score`` so a
test can monkeypatch it (registry-before-score proof).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.factory import fitness
from src.factory import folds
from src.factory import frame as frame_mod
from src.factory import genome as G
from src.factory.columns import Frame
from src.factory.registry import FAMILY_F1, Registry, RegistryError
from src.factory.report import write_json

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE2_REFERENCE = REPO_ROOT / "reports" / "phase2" / "ws_e_go_no_go_data_2026-07-26.json"
FRAMES_ROOT = REPO_ROOT / "data" / "factory" / "frames"
REFERENCE_TOL = 1e-9
#: ``FitnessResult`` fields kept out of the summary rows (they go to seed_date_pnl.json).
ARRAY_FIELDS = ("per_date_pnl", "per_date_codes", "trade_rows")
CONSTRAINT_ORDER = (
    fitness.REASON_NO_TRADES,
    fitness.REASON_MIN_TRADES,
    fitness.REASON_MIN_DATES,
    fitness.REASON_MIN_CITIES,
    fitness.REASON_WORST_DATE,
    fitness.REASON_MAX_CLAUSES,
    fitness.REASON_GEFS_TWIN,
    fitness.REASON_BSS,
)
CONSTRAINT_THRESHOLDS = {
    "min_trades": fitness.MIN_TRADES,
    "min_date_fraction": fitness.MIN_DATE_FRACTION,
    "min_cities": fitness.MIN_CITIES,
    "worst_date_min": fitness.WORST_DATE_MIN,
    "max_clauses": fitness.MAX_CLAUSES,
    "gefs_twin_min": 0.0,
    "bss_trades_min": fitness.BSS_TRADES_MIN,
    "bss_min_two_sided": fitness.BSS_MIN_TWO_SIDED,
    "n_boot": fitness.DEFAULT_N_BOOT,
    "bootstrap_seed": fitness.DEFAULT_SEED,
}


class Gen0Error(RuntimeError):
    """A gen-0 precondition failed (loud, per PRD section 6)."""


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------
def load_frameset(frames_dir: Path) -> frame_mod.FrameSet:
    """Inverse of the ``freeze-frame`` layout: ``parity/``, ``search/``, ``gefs_twin/`` + ``provenance.json``."""
    frames_dir = Path(frames_dir)
    for sub in ("parity", "search"):
        if not (frames_dir / sub).is_dir():
            raise Gen0Error(f"frames dir {frames_dir} has no {sub}/ (run `factory.py freeze-frame`)")
    parity = frame_mod.load(str(frames_dir / "parity"))
    search = frame_mod.load(str(frames_dir / "search"))
    twin = frame_mod.load(str(frames_dir / "gefs_twin")) if (frames_dir / "gefs_twin").is_dir() else None
    prov: Dict[str, Any] = {}
    pj = frames_dir / "provenance.json"
    if pj.exists():
        with open(pj, "r", encoding="utf-8") as fh:
            prov = json.load(fh)
    return frame_mod.FrameSet(parity=parity, search=search, gefs_twin=twin, provenance=prov)


def save_frameset_like_freeze(fs: frame_mod.FrameSet, out_root: Path, lane: str, cutoff: str) -> Tuple[Path, Dict[str, Any]]:
    """Write ``<out_root>/<lane>_<cutoff>_<search sha12>/`` exactly as ``factory.py freeze-frame`` does."""
    shas = {
        "parity": frame_mod.frame_sha256(fs.parity),
        "search": frame_mod.frame_sha256(fs.search),
        "gefs_twin": frame_mod.frame_sha256(fs.gefs_twin) if fs.gefs_twin is not None else None,
    }
    out_dir = Path(out_root) / f"{lane}_{cutoff}_{shas['search'][:12]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_mod.save(fs.parity, str(out_dir / "parity"))
    frame_mod.save(fs.search, str(out_dir / "search"))
    if fs.gefs_twin is not None:
        frame_mod.save(fs.gefs_twin, str(out_dir / "gefs_twin"))
    write_json(out_dir / "provenance.json", dict(fs.provenance or {}))
    (out_dir / "frame.sha256").write_text(
        "".join(f"{v}  {k}\n" for k, v in shas.items() if v), encoding="utf-8"
    )
    return out_dir, shas


def _build_frames(config: Dict[str, Any]) -> Tuple[frame_mod.FrameSet, Path]:
    from src.factory.lanes.weather import WeatherLane

    lane = str(config.get("lane", "weather"))
    if lane != "weather":
        raise Gen0Error(f"lane {lane!r} is not READY in F1 (only weather builds frames)")
    fcfg = dict(config.get("frame") or {})
    kwargs = {k: fcfg[k] for k in ("cutoff", "availability_lag_min", "sigma_cap", "contracts", "adverse_fill", "embargo_days") if k in fcfg}
    kwargs["source"] = str(config.get("source", "gfs_mex"))
    fc = frame_mod.FrameConfig(**kwargs)
    logger.info("gen0: building %s frames with %s", lane, fc.as_dict())
    fs = WeatherLane().build_frames(fc)
    repo_root = Path(config.get("repo_root") or REPO_ROOT)
    out_dir, _ = save_frameset_like_freeze(fs, repo_root / "data" / "factory" / "frames", lane, fc.cutoff)
    return fs, out_dir


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def _config_sha256(config: Dict[str, Any]) -> str:
    sha = config.get("_config_sha256")
    if sha:
        return str(sha)
    path = config.get("_config_path")
    if path and os.path.exists(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read())
        return h.hexdigest()
    # No file: hash the family-defining keys canonically (tests / programmatic configs).
    keys = ("family", "lane", "source", "mode", "gene_spec_version", "grouping_unit", "family_cap",
            "frame", "campaigns", "picker", "thresholds", "budget", "seeds")
    doc = {k: config.get(k) for k in keys}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def refuse_overwrite(out_dir: Path, run_id: str, reports_root: Optional[Path] = None, *, force: bool = False) -> None:
    """Refuse to overwrite a same-day gen-0 report unless ``force`` (F1 red-team carry-over).

    ``scripts/factory.py gen0`` defaults ``run_id`` to ``gen0_<today>``, so a
    second invocation on the same day would silently replace
    ``reports/factory/<run_id>/summary.json`` and repoint ``latest.json`` at the
    new numbers with no trace of the first run. Raises ``Gen0Error`` when
    ``<out_dir>/summary.json`` exists, or when ``<reports_root>/latest.json``
    already names this ``run_id``, unless ``force=True`` (``--force``).
    """
    if force:
        return
    out_dir = Path(out_dir)
    summary = out_dir / "summary.json"
    if summary.exists():
        raise Gen0Error(
            f"{summary} exists; refusing to overwrite the gen-0 report for {run_id!r} "
            "(pass --run-id for a new run, or --force to replace it on purpose)"
        )
    root = Path(reports_root) if reports_root else out_dir.parent
    latest = root / "latest.json"
    if latest.exists():
        try:
            with open(latest, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = {}
        if isinstance(doc, dict) and doc.get("run_id") == run_id:
            raise Gen0Error(
                f"{latest} already points at run {run_id!r}; refusing to overwrite it "
                "(pass --run-id for a new run, or --force to replace it on purpose)"
            )


def ensure_family_line(config: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Write the family line (or reuse the existing one) and return it WITHOUT its ``ts``.

    A rerun with the same config reuses the line; a different ``config_sha256``
    under the same family name aborts (thresholds are pre-committed -- a new
    config is a new family name, never an edit).
    """
    family = str(config.get("family") or FAMILY_F1)
    registry_path = config.get("registry_path") or (Path(config.get("repo_root") or REPO_ROOT) / "reports" / "factory" / "registry.jsonl")
    reg = Registry(registry_path, repo_root=repo_root)
    sha = _config_sha256(config)
    line = reg.family_line(family)
    if line is None:
        fcfg = dict(config.get("frame") or {})
        line = reg.write_family_line(
            family,
            lane=str(config.get("lane", "weather")),
            source=str(config.get("source", "gfs_mex")),
            mode=str(config.get("mode", "taker")),
            gene_spec_version=int(config.get("gene_spec_version", G.GENE_SPEC_VERSION)),
            config_sha256=sha,
            budget=dict(config.get("budget") or {}),
            picker=str(config.get("picker", "")),
            thresholds=dict(config.get("thresholds") or {}),
            cutoff=str(fcfg.get("cutoff", frame_mod.FACTORY_DATA_CUTOFF)),
            grouping_unit=str(config.get("grouping_unit", "target_date")),
            family_cap=int(config.get("family_cap", 6)),
            notes=f"gen0 {config.get('run_id', '')}".strip(),
        )
        logger.info("gen0: wrote registry family line for %s (config %s)", family, sha[:12])
    elif str(line.get("config_sha256")) != sha:
        raise RegistryError(
            f"family {family!r} is registered with config_sha256 {str(line.get('config_sha256'))[:12]} "
            f"but this config hashes to {sha[:12]}; a changed config is a NEW family name (registry {reg.path})"
        )
    out = {k: v for k, v in line.items() if k != "ts"}
    out["status"] = reg.status(family)
    out["registry_path"] = _rel(reg.path, config)
    return out


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def _row(res: fitness.FitnessResult) -> Dict[str, Any]:
    d = res.as_dict()
    for k in ARRAY_FIELDS:
        d.pop(k, None)
    d["passed"] = bool(res.passed)
    return d


def _dates_of(F: Frame, res: fitness.FitnessResult) -> Dict[str, Any]:
    codes = np.asarray(res.per_date_codes, dtype=np.int64)
    return {
        "dates": [str(F.dates[c]) for c in codes],
        "per_date_pnl": [float(x) for x in np.asarray(res.per_date_pnl, dtype=np.float64)],
        "trade_markets": [str(F.markets[m]) for m in F.visible["market_code"][np.asarray(res.trade_rows, dtype=np.int64)]],
    }


def _score(F: Frame, mask: np.ndarray, **kw: Any) -> fitness.FitnessResult:
    return fitness.score(F, mask, **kw)  # module attribute lookup: monkeypatchable


def _score_seed(
    name: str,
    g: G.Genome,
    parity: Frame,
    search: Frame,
    twin: Optional[Frame],
    camps: Dict[str, folds.Campaign],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(summary entry, per-date vectors) for one seed."""
    notes: List[str] = [g.notes] if g.notes else []
    entry: Dict[str, Any] = {
        "genome": g.to_json(),
        "source": g.source,
        "searchable": bool(g.is_searchable()),
        "n_active_clauses": G.n_active_clauses(g),
    }
    vectors: Dict[str, Any] = {}

    # parity (Phase-2 convention; the 1e-9 proof lives here)
    mask_p = G.to_mask(g, parity)
    r_par = _score(parity, mask_p, constraints=True, genome=g, label=name)
    entry["parity_full"] = _row(r_par)
    vectors["parity_full"] = _dates_of(parity, r_par)
    if name == "fr31a_gefs":
        notes.append("parity_full is the parity score of the same genes on the gfs_mex parity frame "
                     "(== fr31a_taker); search_full and the campaigns are scored on the gefs twin frame.")

    # search frame (or the gefs twin for fr31a_gefs)
    if name == "fr31a_gefs":
        target, target_twin, frame_name = twin, None, "gefs_twin"
    else:
        target, target_twin, frame_name = search, twin, "search"
    entry["frame_scored"] = frame_name
    if target is None:
        notes.append("gefs twin frame absent: search_full and campaigns not scored")
        entry["search_full"] = None
        entry["campaigns"] = {}
        entry["phenotype_hash"] = None
    else:
        mask_s = G.to_mask(g, target)
        r_s = _score(target, mask_s, constraints=True, twin=target_twin, genome=g, label=name)
        entry["search_full"] = _row(r_s)
        entry["phenotype_hash"] = r_s.phenotype_hash
        entry["phenotype_hash_frame"] = frame_name
        vectors["search_full"] = _dates_of(target, r_s)
        per_camp: Dict[str, Any] = {}
        vec_camp: Dict[str, Any] = {}
        for cname, camp in camps.items():
            dm_s = folds.date_mask(target, camp.search_dates)
            r_cs = _score(target, mask_s, date_mask=dm_s, constraints=True, twin=target_twin, genome=g,
                          label=f"{name}/{cname}/search")
            crow: Dict[str, Any] = {
                "search": _row(r_cs),
                "n_search_dates": len(camp.search_dates),
                "n_validation_dates": len(camp.validation_dates),
                "n_embargo_dates": len(camp.embargo_dates),
            }
            cvec: Dict[str, Any] = {"search": _dates_of(target, r_cs)}
            if camp.validation_dates:
                dm_v = folds.date_mask(target, camp.validation_dates)
                r_cv = _score(target, mask_s, date_mask=dm_v, constraints=False, twin=target_twin, genome=g,
                              label=f"{name}/{cname}/validation")
                crow["validation"] = _row(r_cv)
                cvec["validation"] = _dates_of(target, r_cv)
            else:
                crow["validation"] = None
            per_camp[cname] = crow
            vec_camp[cname] = cvec
        entry["campaigns"] = per_camp
        vectors["campaigns"] = vec_camp
    entry["notes"] = " ".join(notes)
    return entry, vectors


# ---------------------------------------------------------------------------
# Phase-2 reference comparison
# ---------------------------------------------------------------------------
def load_phase2_shapes(path: Path = PHASE2_REFERENCE) -> Dict[str, Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {str(s["label"]): s for s in data.get("shapes", [])}


def compare_reference(row: Dict[str, Any], ref: Dict[str, Any], label: str, tol: float = REFERENCE_TOL) -> Dict[str, Any]:
    """Every leaf of the reference shape vs the parity row (``label`` compared to the seed's expected label)."""
    diffs: List[str] = []
    compared: List[str] = []
    for k, v in sorted(ref.items()):
        if k == "label":
            if str(v) != label:
                diffs.append(k)
            compared.append(k)
            continue
        if isinstance(v, (dict, list)):
            continue
        if k not in row:
            diffs.append(k)
            continue
        compared.append(k)
        if not fitness._leaf_equal(row[k], v, tol):
            diffs.append(k)
    return {
        "label": label,
        "path": _relpath_str(PHASE2_REFERENCE),
        "tolerance": tol,
        "fields_compared": compared,
        "fields_differing": diffs,
        "matches_1e9": len(diffs) == 0,
        "reference": {k: v for k, v in ref.items() if not isinstance(v, (dict, list))},
    }


# ---------------------------------------------------------------------------
# frame block
# ---------------------------------------------------------------------------
_PROV_DROP = ("ladder_files",)  # hundreds of per-file hashes; the aggregate sha stays


def _frame_block(name: str, F: Optional[Frame]) -> Optional[Dict[str, Any]]:
    if F is None:
        return None
    prov = {k: v for k, v in (F.provenance or {}).items() if k not in _PROV_DROP}
    return {
        "sha256": prov.get("frame_sha256") or frame_mod.frame_sha256(F),
        "rows": F.n_rows,
        "dates": F.n_dates,
        "markets": F.n_markets,
        "executable_rows": int(np.count_nonzero(F.visible["executable"])),
        "date_range": [str(F.dates[0]), str(F.dates[-1])] if F.n_dates else None,
        "provenance": prov,
    }


def _frames_summary(fs: frame_mod.FrameSet, frames_dir: Optional[Path], config: Dict[str, Any]) -> Dict[str, Any]:
    search = fs.search
    sp = search.provenance or {}
    twin_cov = None
    if search.twin_index is not None and search.n_rows:
        twin_cov = float(np.count_nonzero(search.twin_index >= 0) / search.n_rows)
    block = {
        "frames_dir": _rel(frames_dir, config) if frames_dir else None,
        "parity_sha256": (fs.parity.provenance or {}).get("frame_sha256") or frame_mod.frame_sha256(fs.parity),
        "search_sha256": sp.get("frame_sha256") or frame_mod.frame_sha256(search),
        "gefs_twin_sha256": ((fs.gefs_twin.provenance or {}).get("frame_sha256") or frame_mod.frame_sha256(fs.gefs_twin))
        if fs.gefs_twin is not None else None,
        "parity_rows": fs.parity.n_rows,
        "search_rows": search.n_rows,
        "gefs_twin_rows": fs.gefs_twin.n_rows if fs.gefs_twin is not None else 0,
        "search_executable_rows": int(np.count_nonzero(search.visible["executable"])),
        "twin_coverage": twin_cov,
        "availability_lag_min": sp.get("availability_lag_min"),
        "sigma_cap": sp.get("sigma_cap"),
        "cutoff": sp.get("cutoff"),
        "kept_truth_none": sp.get("kept_truth_none"),
        "kept_payoff_none": sp.get("kept_payoff_none"),
        "dropped_result_unsettled": sp.get("dropped_result_unsettled"),
        "dropped_payoff_mismatch": sp.get("dropped_payoff_mismatch"),
        "dropped_truth_disagree": sp.get("dropped_truth_disagree"),
        "dropped_truth_rows": sp.get("dropped_truth_rows"),
        "dropped_sigma_rows": sp.get("dropped_sigma_rows"),
        "dropped_sigma_markets": sp.get("dropped_sigma_markets"),
        "lookahead_violations": sp.get("lookahead_violations"),
        "frameset_provenance": {k: v for k, v in (fs.provenance or {}).items() if k not in _PROV_DROP},
        "frames": {
            "parity": _frame_block("parity", fs.parity),
            "search": _frame_block("search", search),
            "gefs_twin": _frame_block("gefs_twin", fs.gefs_twin),
        },
    }
    return block


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _relpath_str(p: Any) -> str:
    try:
        return Path(p).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return str(p).replace("\\", "/")


def _rel(p: Any, config: Dict[str, Any]) -> str:
    root = Path(config.get("repo_root") or REPO_ROOT)
    try:
        return Path(p).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return str(p).replace("\\", "/")


def _campaign_block(camps: Dict[str, folds.Campaign]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, c in camps.items():
        out[name] = {
            "search": [c.search_dates[0], c.search_dates[-1]] if c.search_dates else None,
            "n_search": len(c.search_dates),
            "embargo": list(c.embargo_dates),
            "validation": [c.validation_dates[0], c.validation_dates[-1]] if c.validation_dates else None,
            "n_validation": len(c.validation_dates),
        }
    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def run_gen0(config: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    """Score gen-0 (module docstring); returns the summary dict the CLI hands to ``report.write_gen0_report``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(config.get("repo_root") or REPO_ROOT)
    family = str(config.get("family") or FAMILY_F1)
    lane = str(config.get("lane", "weather"))

    # 1. frames -------------------------------------------------------------
    frames_dir = Path(config["frames_dir"]) if config.get("frames_dir") else None
    if frames_dir is not None:
        fs = load_frameset(frames_dir)
    else:
        fs = None  # built after the registry line (a build is ~12 s; the line costs nothing)

    # 2. registry line BEFORE any result ------------------------------------
    registry_line = ensure_family_line(config, repo_root=repo_root)

    if fs is None:
        fs, frames_dir = _build_frames(config)
    parity, search, twin = fs.parity, fs.search, fs.gefs_twin

    # 3. campaigns on the search frame's dates ------------------------------
    camps = folds.campaigns([str(d) for d in search.dates])
    wanted = list(config.get("campaigns") or list(camps))
    unknown = [c for c in wanted if c not in camps]
    if unknown:
        raise Gen0Error(f"config names unknown campaign(s) {unknown}; have {sorted(camps)}")
    camps = {c: camps[c] for c in wanted}

    # 4. seeds ---------------------------------------------------------------
    seed_names = list(config.get("seeds") or list(G.SEEDS))
    missing = [s for s in seed_names if s not in G.SEEDS]
    if missing:
        raise Gen0Error(f"config names unknown seed(s) {missing}; genome.SEEDS has {sorted(G.SEEDS)}")
    for s in G.SEEDS:
        if s not in seed_names:
            seed_names.append(s)  # every seed is scored, config order first
    refs: Dict[str, Dict[str, Any]] = {}
    if PHASE2_REFERENCE.exists():
        refs = load_phase2_shapes(PHASE2_REFERENCE)
    seeds: Dict[str, Any] = {}
    vectors: Dict[str, Any] = {}
    for name in seed_names:
        g = G.SEEDS[name]
        entry, vec = _score_seed(name, g, parity, search, twin, camps)
        label = G.PHASE2_SHAPE_LABELS.get(name)
        if label is not None and label in refs:
            entry["reference"] = compare_reference(entry["parity_full"], refs[label], label)
        elif label is not None:
            entry["reference"] = {"label": label, "matches_1e9": None, "fields_differing": [],
                                  "note": f"reference shape {label!r} not found in {_relpath_str(PHASE2_REFERENCE)}"}
        else:
            entry["reference"] = None
        seeds[name] = entry
        vectors[name] = vec
        pf, sf = entry["parity_full"], entry.get("search_full") or {}
        logger.info(
            "gen0 %-18s parity %4d/%2d %+.4f [%+.4f,%+.4f] %s | %s %4s/%2s %s %s",
            name, pf["trades"], pf["dates"], pf["realized"] if pf["realized"] == pf["realized"] else float("nan"),
            pf["boot_lo"], pf["boot_hi"], pf["constraint_reason"] or "ok",
            entry["frame_scored"], sf.get("trades"), sf.get("dates"),
            f"{sf['realized']:+.4f}" if sf and sf.get("realized") == sf.get("realized") else "nan",
            sf.get("constraint_reason") or ("ok" if sf else "n/a"),
        )

    # 5. frame-level Brier skill vs market ----------------------------------
    bss = {
        "parity": fitness.frame_bss_vs_market(parity),
        "search": fitness.frame_bss_vs_market(search),
    }

    # 6. summary -----------------------------------------------------------------
    summary: Dict[str, Any] = {
        "run_id": config.get("run_id"),
        "kind": "gen0",
        "family": family,
        "lane": lane,
        "source": config.get("source"),
        "mode": config.get("mode"),
        "git_rev": config.get("git_rev"),
        "lock_sha256": config.get("lock_sha256"),
        "fee_regime_sha256": config.get("fee_regime_sha256"),
        "gene_spec_version": G.GENE_SPEC_VERSION,
        "config_sha256": registry_line.get("config_sha256"),
        "config_path": _rel(config["_config_path"], config) if config.get("_config_path") else None,
        "registry_line": registry_line,
        "frame": _frames_summary(fs, frames_dir, config),
        "campaigns": _campaign_block(camps),
        "seeds": seeds,
        "seed_notes": dict(G.seed_notes),
        "constraint_order": list(CONSTRAINT_ORDER),
        "constraint_thresholds": dict(CONSTRAINT_THRESHOLDS),
        "brier_skill_vs_market": bss,
        "throughput": None,
        "workers": config.get("workers"),
        "files": {"seed_date_pnl": "seed_date_pnl.json"},
        "phase_note": "F1 gen-0: seeds only; no genome is proposed. Evolution/RC/Holm/controls are F2.",
    }
    write_json(out_dir / "seed_date_pnl.json", {"run_id": config.get("run_id"), "seeds": vectors})
    return summary


__all__ = [
    "CONSTRAINT_ORDER",
    "CONSTRAINT_THRESHOLDS",
    "Gen0Error",
    "compare_reference",
    "ensure_family_line",
    "load_frameset",
    "load_phase2_shapes",
    "refuse_overwrite",
    "run_gen0",
    "save_frameset_like_freeze",
]
