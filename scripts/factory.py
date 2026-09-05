#!/usr/bin/env python
"""Strategy-factory CLI (PRD_STRATEGY_FACTORY FR-F1.6; FACTORY_ARCHITECTURE section 1.3).

    python scripts/factory.py freeze-frame [--cutoff 2026-07-25 ...]
    python scripts/factory.py gen0 [--frames DIR] [--out reports/factory/gen0_<date>] [--workers N] [--bench]
    python scripts/factory.py board | coverage | status
    python scripts/factory.py run [--config Y] [--frames DIR] [--run-id ID] [--workers N] [--population N]
                                  [--generations N] [--master-seed S] [--campaigns A,B,C,ALL69]
                                  [--blocked-folds|--no-blocked-folds] [--out DIR]
    python scripts/factory.py resume <run_id> [--workers N] [--out DIR]
    python scripts/factory.py controls|report|holdout|score|promote   -> exit 2 (F2 STATS / F4)

Runs inside the ``factory`` compose service on alcyone (network_mode: none;
``/app`` read-only except ``data/factory`` and ``reports/factory``):

    docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory \
        python scripts/factory.py freeze-frame

Heavy modules (``src.factory.frame``, ``lanes``, ``gen0``, pandas/pyarrow) are
imported lazily inside the subcommands so ``--help`` never touches them.

``run.json`` (written by ``freeze-frame`` and ``gen0``): run_id, git_rev
(non-empty or abort), lock_sha256 (``deploy/spark/requirements-lab.lock``),
frame sha256s, config, fee_regime_sha256, python/numpy/pandas versions, host uid.

``run`` (F2, FR-F2.1/F2.2): ``src.factory.procedure.run_procedure`` writes
``data/factory/runs/<run_id>/{run.json,folds.json,status.json,picks.json,
ledger/,oos/}``; the tracked ``reports/factory/<run_id>/`` gets ``status.json``
(mirrored every generation) and a copy of ``run.json``;
``reports/factory/latest.json`` gains ``active_run`` + ``status``. A run_id is
NEVER overwritten -- ``resume <run_id>`` continues from the last fully scored
generation (byte-identical to an uninterrupted run) and is a no-op on a DONE run.

Calling convention for the integration agent's ``src.factory.gen0.run_gen0``:
``run_gen0(config: dict, out_dir: Path) -> summary dict`` where ``config`` is
the family YAML as a dict plus ``frames_dir`` (str | None), ``workers`` (int),
``bench`` (bool), ``run_id``, ``git_rev``, ``lock_sha256``, ``fee_regime_sha256``
and ``repo_root``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FAMILY_CONFIG = REPO_ROOT / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml"
LOCK_FILE = REPO_ROOT / "deploy" / "spark" / "requirements-lab.lock"
FEE_REGIME = REPO_ROOT / "configs" / "fees" / "fee_regime.csv"
FRAMES_ROOT = REPO_ROOT / "data" / "factory" / "frames"
RUNS_ROOT = REPO_ROOT / "data" / "factory" / "runs"
REPORTS_ROOT = REPO_ROOT / "reports" / "factory"
NOT_IMPLEMENTED = ("holdout", "score", "promote")  # run/resume: F2 EVOLVE; controls/report: F2 STATS (below)


# ---------------------------------------------------------------------------
# helpers (no heavy imports)
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> Optional[str]:
    """CRLF-normalised sha256 (see ``src.factory.fees.sha256_file``); None if unreadable."""
    try:
        from src.factory.fees import sha256_file as _norm_sha

        return _norm_sha(str(path))
    except OSError:
        return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"factory: ABORT: {msg}", file=sys.stderr)
    sys.exit(code)


def _git_rev_or_die() -> str:
    from src.factory.registry import git_rev

    rev = git_rev(REPO_ROOT)
    if not rev:
        _die("git rev is empty (set MP_GIT_REV or run from a git checkout)")
    return rev


def _versions() -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"python": platform.python_version()}
    for mod in ("numpy", "pandas", "pyarrow"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:
            out[mod] = None
    return out


def _uid() -> Optional[int]:
    return os.getuid() if hasattr(os, "getuid") else None


def load_family_config(path: Path) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["_config_path"] = str(path)
    cfg["_config_sha256"] = sha256_file(path)
    return cfg


def write_run_json(path: Path, *, run_id: str, kind: str, frame_shas: Dict[str, Any], config: Dict[str, Any],
                   extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from src.factory.report import write_json

    doc = {
        "run_id": run_id,
        "kind": kind,
        "git_rev": _git_rev_or_die(),
        "lock_sha256": sha256_file(LOCK_FILE),
        "lock_file": LOCK_FILE.relative_to(REPO_ROOT).as_posix(),
        "frames": frame_shas,
        "config": config,
        "fee_regime_sha256": sha256_file(FEE_REGIME),
        "versions": _versions(),
        "host": {"node": platform.node(), "uid": _uid(), "platform": platform.platform()},
    }
    if extra:
        doc.update(extra)
    if doc["lock_sha256"] is None:
        _die(f"lock file missing: {LOCK_FILE}")
    write_json(path, doc)
    return doc


def _today() -> str:
    return _dt.date.today().isoformat()


def _frame_config_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "cutoff": args.cutoff,
        "availability_lag_min": args.lag,
        "sigma_cap": args.sigma_cap,
        "contracts": args.contracts,
        "adverse_fill": args.adverse_fill,
        "embargo_days": args.embargo_days,
        "source": args.source,
    }


def _latest_frames_dir(lane: str) -> Optional[Path]:
    if not FRAMES_ROOT.exists():
        return None
    cands = sorted(p for p in FRAMES_ROOT.iterdir() if p.is_dir() and p.name.startswith(f"{lane}_"))
    return cands[-1] if cands else None


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_freeze_frame(args: argparse.Namespace) -> int:
    from src.factory import frame as frame_mod
    from src.factory.lanes.weather import WeatherLane
    from src.factory.report import write_json

    if args.lane != "weather":
        _die(f"lane {args.lane!r} is not READY in F1 (only weather builds frames)")
    kwargs = _frame_config_kwargs(args)
    cfg = frame_mod.FrameConfig(**kwargs)
    print(f"freeze-frame: building {args.lane} frames with {kwargs}")
    try:
        fs = WeatherLane().build_frames(cfg)
    except frame_mod.FrameAbort as e:
        _die(f"frame build refused: {e}")
    shas = {
        "parity": frame_mod.frame_sha256(fs.parity),
        "search": frame_mod.frame_sha256(fs.search),
        "gefs_twin": frame_mod.frame_sha256(fs.gefs_twin) if fs.gefs_twin is not None else None,
    }
    out_root = Path(args.out_root) if args.out_root else FRAMES_ROOT
    out_dir = out_root / f"{args.lane}_{args.cutoff}_{shas['search'][:12]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_mod.save(fs.parity, out_dir / "parity")
    frame_mod.save(fs.search, out_dir / "search")
    if fs.gefs_twin is not None:
        frame_mod.save(fs.gefs_twin, out_dir / "gefs_twin")
    prov = dict(fs.provenance or {})
    write_json(out_dir / "provenance.json", prov)
    (out_dir / "frame.sha256").write_text(
        "".join(f"{v}  {k}\n" for k, v in shas.items() if v), encoding="utf-8"
    )
    run_id = f"freeze_{args.lane}_{args.cutoff}_{shas['search'][:12]}"
    write_run_json(out_dir / "run.json", run_id=run_id, kind="freeze-frame", frame_shas=shas, config=kwargs)
    print(f"freeze-frame: wrote {out_dir}")
    print("provenance summary:")
    for k in sorted(prov):
        v = prov[k]
        if isinstance(v, (dict, list)):
            v = f"<{type(v).__name__} len={len(v)}>"
        print(f"  {k}: {v}")
    print(f"rows: parity={fs.parity.n_rows} search={fs.search.n_rows} "
          f"gefs_twin={fs.gefs_twin.n_rows if fs.gefs_twin is not None else 0}")
    for k, v in shas.items():
        print(f"sha256 {k}: {v}")
    return 0


def cmd_gen0(args: argparse.Namespace) -> int:
    from src.factory import report as report_mod
    from src.factory.gen0 import Gen0Error, refuse_overwrite, run_gen0

    cfg_path = Path(args.config) if args.config else DEFAULT_FAMILY_CONFIG
    if not cfg_path.exists():
        _die(f"family config missing: {cfg_path}")
    config = load_family_config(cfg_path)
    lane = str(config.get("lane", "weather"))
    frames_dir = Path(args.frames) if args.frames else _latest_frames_dir(lane)
    if frames_dir is None or not frames_dir.exists():
        _die("no frozen frames found; run `factory.py freeze-frame` first or pass --frames DIR")
    run_id = args.run_id or f"gen0_{_today()}"
    out_dir = Path(args.out) if args.out else REPORTS_ROOT / run_id
    # Same-day rerun guard: never replace an existing summary.json / latest.json
    # pointer for this run_id without --force (src.factory.gen0.refuse_overwrite).
    try:
        refuse_overwrite(out_dir, run_id, REPORTS_ROOT, force=bool(args.force))
    except Gen0Error as e:
        _die(str(e))
    out_dir.mkdir(parents=True, exist_ok=True)

    frame_shas: Dict[str, Any] = {}
    sha_file = frames_dir / "frame.sha256"
    if sha_file.exists():
        for line in sha_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                frame_shas[parts[1]] = parts[0]
    git_rev = _git_rev_or_die()
    lock_sha = sha256_file(LOCK_FILE)
    config.update({
        "frames_dir": str(frames_dir),
        "workers": int(args.workers),
        "bench": bool(args.bench),
        "run_id": run_id,
        "git_rev": git_rev,
        "lock_sha256": lock_sha,
        "fee_regime_sha256": sha256_file(FEE_REGIME),
        "repo_root": str(REPO_ROOT),
        "registry_path": str(REPORTS_ROOT / "registry.jsonl"),
    })
    run_json_dir = RUNS_ROOT / run_id
    run_json_dir.mkdir(parents=True, exist_ok=True)
    write_run_json(run_json_dir / "run.json", run_id=run_id, kind="gen0", frame_shas=frame_shas,
                   config={k: v for k, v in config.items() if not k.startswith("_")},
                   extra={"frames_dir": str(frames_dir), "out_dir": str(out_dir)})

    summary = run_gen0(config, out_dir)
    if not isinstance(summary, dict):
        _die("run_gen0 did not return a summary dict")
    # The tracked report carries its own copy of run.json (data/factory is
    # ignored) so the git rev / lock hash a red team audits travel with it.
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_json_dir / "run.json", out_dir / "run.json")
    summary.setdefault("run_id", run_id)
    summary.setdefault("kind", "gen0")
    summary.setdefault("family", config.get("family"))
    summary.setdefault("git_rev", git_rev)
    summary.setdefault("lock_sha256", lock_sha)
    if args.bench and not summary.get("throughput"):
        from src.factory import bench as bench_mod
        from src.factory import frame as frame_mod

        search = frame_mod.load(frames_dir / "search")
        summary["throughput"] = bench_mod.bench_throughput(
            search, n=args.bench_n, workers=int(args.workers), frame_dir=str(frames_dir / "search")
        )
    paths = report_mod.write_gen0_report(summary, out_dir, reports_root=REPORTS_ROOT)
    print(f"gen0: wrote {paths['summary_json']}")
    print(report_mod.render_summary_md(summary))
    return 0


# --- F2 EVOLVE: run / resume (begin) ---------------------------------------
def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _frame_shas_from_dir(frames_dir: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sha_file = frames_dir / "frame.sha256"
    if sha_file.exists():
        for line in sha_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[1]] = parts[0]
    return out


def _runtime_config(config: Dict[str, Any], *, run_id: str, frames_dir: Path, out_dir: Path, workers: int) -> None:
    """The runtime keys ``run_procedure`` records in run.json + the two private reporting keys."""
    lock_sha = sha256_file(LOCK_FILE)
    if lock_sha is None:
        _die(f"lock file missing: {LOCK_FILE}")
    config.update({
        "run_id": run_id,
        "kind": "run",
        "frames_dir": str(frames_dir),
        "workers": int(workers),
        "git_rev": _git_rev_or_die(),
        "lock_sha256": lock_sha,
        "lock_file": LOCK_FILE.relative_to(REPO_ROOT).as_posix(),
        "fee_regime_sha256": sha256_file(FEE_REGIME),
        "repo_root": str(REPO_ROOT),
        "registry_path": str(REPORTS_ROOT / "registry.jsonl"),
        "versions": _versions(),
        "host": {"node": platform.node(), "uid": _uid(), "platform": platform.platform()},
        "_status_mirror": str(out_dir / "status.json"),
        "_latest_json": str(REPORTS_ROOT / "latest.json"),
    })


def _print_procedure_result(res: Any, *, workers: int) -> None:
    def _fmt(v: Any) -> str:
        return "n/a" if v is None else f"{v:+.4f}"

    print("picks:")
    for name, p in list(res.picks.items()) + list(res.folds.items()):
        if p.genome is None:
            print(f"  {name:6s} none ({p.reason})")
            continue
        ins = p.in_sample
        val = p.validation
        line = f"  {name:6s} {p.genome_id} gen {p.picked_gen:3d} fit {_fmt(None if ins is None else ins.fit)}"
        if ins is not None:
            line += f" trades {ins.trades} dates {ins.dates}"
        if val is not None:
            line += f" | validation {_fmt(val.realized)} [{_fmt(val.boot_lo)}, {_fmt(val.boot_hi)}] trades {val.trades} dates {val.dates}"
        if p.reason:
            line += f" ({p.reason})"
        print(line)
    po = res.pooled
    print(f"pooled OOS: n_dates {po.get('n_dates')} mean {_fmt(po.get('mean'))} se {_fmt(po.get('se'))} "
          f"t {_fmt(po.get('t_stat'))} boot [{_fmt(po.get('boot_lo'))}, {_fmt(po.get('boot_hi'))}] "
          f"trade-weighted {_fmt(po.get('trade_weighted_mean'))} trades {po.get('n_trades')}")
    if res.folds_pooled:
        fp = res.folds_pooled
        print(f"blocked-5-fold ({fp.get('label')}): n_dates {fp.get('n_dates')} mean {_fmt(fp.get('mean'))} "
              f"boot [{_fmt(fp.get('boot_lo'))}, {_fmt(fp.get('boot_hi'))}]")
    if res.elapsed_s > 0:
        rate = f"{res.scored_now / res.score_seconds:.0f} evals/s" if res.score_seconds > 0 else "n/a"
        print(f"evaluations {res.evaluations} on disk ({res.scored_now} scored now in {res.score_seconds:.1f} s of "
              f"pure scoring = {rate}); this call {res.elapsed_s:.1f} s wall, workers {workers}")


def cmd_run(args: argparse.Namespace) -> int:
    from src.factory import procedure as proc_mod
    from src.factory.evolve import EvolveConfig
    from src.factory.gen0 import load_frameset

    cfg_path = Path(args.config) if args.config else DEFAULT_FAMILY_CONFIG
    if not cfg_path.exists():
        _die(f"family config missing: {cfg_path}")
    config = load_family_config(cfg_path)
    lane = str(config.get("lane", "weather"))
    frames_dir = Path(args.frames) if args.frames else _latest_frames_dir(lane)
    if frames_dir is None or not frames_dir.exists():
        _die("no frozen frames found; run `factory.py freeze-frame` first or pass --frames DIR")
    run_id = args.run_id or f"run_{_today_utc()}"
    run_dir = RUNS_ROOT / run_id
    if run_dir.exists():
        _die(f"run {run_id!r} already exists at {run_dir}; a run_id is never overwritten "
             f"(continue it with `factory.py resume {run_id}` or choose another --run-id)")
    out_dir = Path(args.out) if args.out else REPORTS_ROOT / run_id
    budget = dict(config.get("budget") or {})
    cfg = EvolveConfig(
        population=int(args.population or budget.get("population", 400)),
        generations=int(args.generations or budget.get("generations", 60)),
        workers=int(args.workers or budget.get("workers", 16)),
        n_boot=int(budget.get("bootstrap_draws", 4000)),
    )
    master_seed = int(args.master_seed if args.master_seed is not None else budget.get("master_seed", 20260902))
    campaigns = tuple(c.strip() for c in args.campaigns.split(",") if c.strip()) if args.campaigns else tuple(
        config.get("campaigns") or proc_mod.DEFAULT_CAMPAIGNS)
    _runtime_config(config, run_id=run_id, frames_dir=frames_dir, out_dir=out_dir, workers=cfg.workers)
    fs = load_frameset(frames_dir)
    want = _frame_shas_from_dir(frames_dir)
    if want.get("search") and want["search"] != (fs.search.provenance or {}).get("frame_sha256", want["search"]):
        _die(f"{frames_dir}/frame.sha256 disagrees with search/provenance.json")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run {run_id}: frames {frames_dir.name} population {cfg.population} generations {cfg.generations} "
          f"workers {cfg.workers} n_boot {cfg.n_boot} master_seed {master_seed} campaigns {list(campaigns)} "
          f"blocked_folds {bool(args.blocked_folds)}")
    res = proc_mod.run_procedure(
        fs, config, run_dir, campaigns=campaigns, blocked_folds=bool(args.blocked_folds), cfg=cfg,
        master_seed=master_seed, frame_dir=str(frames_dir), resume=False, log=print,
    )
    shutil.copyfile(run_dir / "run.json", out_dir / "run.json")
    _print_procedure_result(res, workers=cfg.workers)
    print(f"run {run_id}: DONE -> {run_dir} (status mirrored to {out_dir / 'status.json'})")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from src.factory import procedure as proc_mod
    from src.factory.evolve import EvolveConfig
    from src.factory.gen0 import load_frameset

    run_id = str(args.run_id)
    run_dir = RUNS_ROOT / run_id
    run_json = run_dir / "run.json"
    if not run_json.exists():
        _die(f"no run.json for {run_id!r} under {run_dir}; nothing to resume")
    doc = json.loads(run_json.read_text(encoding="utf-8"))
    if doc.get("kind") != "run":
        _die(f"{run_json} is kind={doc.get('kind')!r}, not a `run`")
    status_path = run_dir / "status.json"
    if status_path.exists():
        st = json.loads(status_path.read_text(encoding="utf-8"))
        if st.get("state") == "DONE":
            print(f"resume {run_id}: already DONE (evaluations {st.get('evaluations')}, picks {st.get('picks_done')}); nothing to do")
            return 0
    cfg_path = Path(doc["config_path"]) if doc.get("config_path") else DEFAULT_FAMILY_CONFIG
    if not cfg_path.exists():
        _die(f"family config missing: {cfg_path}")
    config = load_family_config(cfg_path)
    if doc.get("config_sha256") and config.get("_config_sha256") != doc.get("config_sha256"):
        _die(f"config {cfg_path.name} changed since the run started ({str(doc.get('config_sha256'))[:12]} -> "
             f"{str(config.get('_config_sha256'))[:12]}); a run is never resumed on another config")
    frames_dir = Path(doc["frames_dir"]) if doc.get("frames_dir") else None
    if frames_dir is None or not frames_dir.exists():
        _die(f"frames dir {frames_dir} from run.json no longer exists")
    budget = dict(doc.get("budget") or {})
    if args.workers:
        budget["workers"] = int(args.workers)
    cfg = EvolveConfig(**budget)
    out_dir = Path(args.out) if args.out else REPORTS_ROOT / run_id
    _runtime_config(config, run_id=run_id, frames_dir=frames_dir, out_dir=out_dir, workers=cfg.workers)
    fs = load_frameset(frames_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"resume {run_id}: frames {frames_dir.name} budget {budget} master_seed {doc.get('master_seed')}")
    res = proc_mod.run_procedure(
        fs, config, run_dir, campaigns=tuple(doc.get("campaigns") or proc_mod.DEFAULT_CAMPAIGNS),
        blocked_folds=bool(doc.get("blocked_folds")), cfg=cfg, master_seed=int(doc["master_seed"]),
        frame_dir=str(frames_dir), resume=True, log=print,
    )
    shutil.copyfile(run_dir / "run.json", out_dir / "run.json")
    _print_procedure_result(res, workers=cfg.workers)
    print(f"resume {run_id}: DONE")
    return 0


# --- F2 EVOLVE: run / resume (end) -----------------------------------------


def cmd_board(args: argparse.Namespace) -> int:
    from src.factory import report as report_mod

    latest = report_mod._load_json(REPORTS_ROOT / "latest.json")
    summary = None
    if latest and latest.get("summary"):
        summary = report_mod._load_json(REPORTS_ROOT / latest["summary"])
    coverage = report_mod._load_json(REPORTS_ROOT / "coverage.json")
    if summary is None and coverage is None:
        print("board: no reports/factory/latest.json or coverage.json yet", file=sys.stderr)
        return 1
    print(report_mod.render_board(summary, coverage, None), end="")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from src.factory.coverage import write_coverage

    path = Path(args.out) if args.out else REPORTS_ROOT / "coverage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    result = write_coverage(path)
    print(f"coverage: wrote {path}")
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = REPORTS_ROOT / "latest.json"
    if not path.exists():
        print(f"status: {path} does not exist (no run yet)", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_not_implemented(args: argparse.Namespace) -> int:
    print(f"factory {args.command}: F2+/F4 — not implemented in F1", file=sys.stderr)
    return 2


# ===========================================================================
# F2 STATS workstream: `controls <run_id>` and `report <run_id>`
# ===========================================================================
def _load_run(run_id: str, config_path: Optional[str], frames: Optional[str]):
    """(run_dir, run.json, config dict, FrameSet) for a finished/running `factory.py run` directory."""
    from src.factory.gen0 import load_frameset

    run_dir = RUNS_ROOT / run_id
    if not run_dir.is_dir():
        _die(f"run dir missing: {run_dir}")
    run_json: Dict[str, Any] = {}
    rj = run_dir / "run.json"
    if rj.exists():
        run_json = json.loads(rj.read_text(encoding="utf-8"))
    cfg_path = Path(config_path) if config_path else DEFAULT_FAMILY_CONFIG
    if not cfg_path.exists():
        _die(f"family config missing: {cfg_path}")
    config = load_family_config(cfg_path)
    want = run_json.get("config_sha256")
    if want and config.get("_config_sha256") and str(want) != str(config["_config_sha256"]):
        _die(f"run {run_id} was made with config sha {str(want)[:12]}, but {cfg_path.name} hashes to {str(config['_config_sha256'])[:12]}")
    frames_dir = Path(frames) if frames else (Path(run_json["frames_dir"]) if run_json.get("frames_dir") else _latest_frames_dir(str(config.get("lane", "weather"))))
    if frames_dir is None or not Path(frames_dir).exists():
        _die("frames dir not found; pass --frames DIR")
    fs = load_frameset(Path(frames_dir))
    frame_shas = run_json.get("frames") or {}
    got = fs.search.provenance.get("frame_sha256")
    if frame_shas.get("search") and got and frame_shas["search"] != got:
        _die(f"search frame sha {str(got)[:12]} differs from run.json's {str(frame_shas['search'])[:12]}")
    config.update({
        "frames_dir": str(frames_dir),
        "run_id": run_id,
        "repo_root": str(REPO_ROOT),
        "registry_path": str(REPORTS_ROOT / "registry.jsonl"),
    })
    return run_dir, run_json, config, fs


def _evolve_cfg(config: Dict[str, Any], run_json: Dict[str, Any], workers: Optional[int]):
    try:
        from src.factory.evolve import EvolveConfig
    except ImportError as exc:  # EVOLVE workstream not merged yet
        _die(f"src.factory.evolve is unavailable ({exc}); controls need the F2 EVOLVE modules")
    budget = dict(run_json.get("budget") or config.get("budget") or {})
    kw: Dict[str, Any] = {}
    if "population" in budget:
        kw["population"] = int(budget["population"])
    if "generations" in budget:
        kw["generations"] = int(budget["generations"])
    if "bootstrap_draws" in budget:
        kw["n_boot"] = int(budget["bootstrap_draws"])
    kw["workers"] = int(workers) if workers else int(budget.get("workers", 16))
    return EvolveConfig(**kw)


def cmd_controls(args: argparse.Namespace) -> int:
    from src.factory import controls as controls_mod

    run_dir, run_json, config, fs = _load_run(args.run_id, args.config, args.frames)
    if not (run_dir / "oos" / "pooled.json").exists():
        _die(f"{run_dir / 'oos' / 'pooled.json'} missing: the real run must finish before its controls")
    master_seed = int(run_json.get("master_seed") or (config.get("budget") or {}).get("master_seed") or 0)
    cfg = _evolve_cfg(config, run_json, args.workers)
    kinds = tuple(k.strip() for k in str(args.kinds).split(",") if k.strip())
    print(f"controls: run {args.run_id} kinds={kinds} snapshot x{args.n_snapshot} residual x{args.n_residual} planted x1 "
          f"master_seed={master_seed} workers={cfg.workers}")
    summary = controls_mod.run_controls(
        fs, config, run_dir, None, cfg=cfg, master_seed=master_seed, n_snapshot=int(args.n_snapshot),
        n_residual=int(args.n_residual), kinds=kinds, log=print,
        # the tracked mirror the Hermes monitor cron hashes (FR-F2.6 "≥3 posts during the run")
        status_mirror=REPORTS_ROOT / args.run_id / "status.json",
    )
    for kind in kinds:
        blk = summary.get(kind) or {}
        if kind == "snapshot":
            print(f"snapshot: boot_lo>0 in {blk.get('n_boot_lo_gt0')}/{blk.get('n_done')}; KS p {blk.get('ks_p_rc', {}).get('p')}; real rank {blk.get('real_rank')}")
        elif kind == "residual":
            print(f"residual: p95 {blk.get('p95')}; real pooled mean {summary.get('real_pooled_mean')} rank {blk.get('real_rank')}/{blk.get('n_done')}")
        else:
            print(f"planted: captured {blk.get('captured')} ratio {blk.get('capture_ratio')} pass {blk.get('pass')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from src.factory import report as report_mod
    from src.factory.registry import Registry, RegistryError, TERMINAL

    run_dir, run_json, config, fs = _load_run(args.run_id, args.config, args.frames)
    family = str(config.get("family"))
    reg = Registry(REPORTS_ROOT / "registry.jsonl", repo_root=REPO_ROOT)
    line = reg.family_line(family)
    if line is None:
        _die(f"family {family!r} has no registry line; the run must have been registered before it started")
    registry_line = {k: v for k, v in line.items() if k != "ts"}
    registry_line["status"] = reg.status(family)
    sens_fs = None
    sens_dir = getattr(args, "sensitivity_frames", None)
    if sens_dir:
        from src.factory.gen0 import load_frameset

        sd = Path(sens_dir)
        if not sd.exists():
            _die(f"--sensitivity-frames {sd} does not exist (freeze one with `factory.py freeze-frame --embargo-days 2 --out-root DIR`)")
        sens_fs = load_frameset(sd)
        emb = (sens_fs.provenance.get("config") or {}).get("embargo_days") if isinstance(sens_fs.provenance, dict) else None
        if emb is not None and int(emb) != 2:
            _die(f"--sensitivity-frames {sd} was frozen with embargo_days={emb}, want 2")
    summary = report_mod.build_family_summary(run_dir, fs, config, registry_line, sensitivity_fs=sens_fs,
                                              sensitivity_frames_dir=str(sens_dir) if sens_dir else None)
    verdict = summary["verdict"]["status"]

    # registry transition FIRST, so the written summary carries the post-transition
    # registry status and a rerun of `report` is byte-identical (idempotent).
    current = reg.status(family)
    if args.no_transition:
        print(f"report: verdict {verdict}; registry left at {current} (--no-transition)")
    elif current == verdict:
        print(f"report: verdict {verdict}; registry already {current} (idempotent)")
    elif current in TERMINAL:
        print(f"report: verdict {verdict} but family is {current}; no further transitions are possible", file=sys.stderr)
    else:
        pooled = summary.get("pooled_oos") or {}
        evidence = {
            "run_id": args.run_id,
            "pooled_mean": pooled.get("mean"), "pooled_boot_lo": pooled.get("boot_lo"), "pooled_boot_hi": pooled.get("boot_hi"),
            "pooled_n_dates": pooled.get("n_dates"), "pooled_one_sided_p": pooled.get("one_sided_p"),
            "holm_p_adj": report_mod._g(summary, "holm", "this_family", "p_adj"),
            "p_rc_all69": report_mod._g(summary, "multiplicity", "ALL69", "p_rc"),
            "failing": summary["verdict"].get("failing"),
            "controls_complete": summary["verdict"].get("controls_complete"),
        }
        try:
            reg.transition(family, verdict, genome_id=report_mod._g(summary, "picks", "ALL69", "genome_id"), evidence=evidence)
            print(f"report: registry {family}: {current} -> {verdict}")
        except RegistryError as exc:
            print(f"report: registry transition refused: {exc}", file=sys.stderr)
    summary["registry_line"]["status"] = reg.status(family)

    out_dir = Path(args.out) if args.out else REPORTS_ROOT / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("run.json", "bench.json"):
        src = run_dir / name
        if src.exists():
            shutil.copyfile(src, out_dir / name)
    # the report's status.json extends the run's progress document (never replaces its keys)
    base_status = json.loads((run_dir / "status.json").read_text(encoding="utf-8")) if (run_dir / "status.json").exists() else {}
    paths = report_mod.write_family_report(summary, out_dir, reports_root=REPORTS_ROOT)
    merged = dict(base_status)
    merged.update(json.loads(paths["status_json"].read_text(encoding="utf-8")))
    if base_status.get("state"):
        merged["state"] = base_status["state"]
        merged["reported"] = True
    report_mod.write_json(paths["status_json"], merged)
    print(f"report: wrote {paths['summary_json']}")
    print(report_mod.render_family_md(summary))
    return 0
# ===========================================================================
# end F2 STATS block
# ===========================================================================


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    ff = sub.add_parser("freeze-frame", help="build + save parity/search/gefs_twin frames under data/factory/frames/")
    ff.add_argument("--lane", default="weather")
    ff.add_argument("--cutoff", default="2026-07-25")
    ff.add_argument("--lag", type=int, default=240, help="availability lag, minutes")
    ff.add_argument("--sigma-cap", type=float, default=4.0)
    ff.add_argument("--contracts", type=int, default=20)
    ff.add_argument("--adverse-fill", type=float, default=0.01)
    ff.add_argument("--embargo-days", type=int, default=1)
    ff.add_argument("--source", default="gfs_mex")
    ff.add_argument("--out-root", default=None, help=f"default {FRAMES_ROOT}")
    ff.set_defaults(func=cmd_freeze_frame)

    g0 = sub.add_parser("gen0", help="score the gen-0 seeds and write reports/factory/<run_id>/")
    g0.add_argument("--frames", default=None, help="frozen frame dir (default: newest under data/factory/frames)")
    g0.add_argument("--out", default=None, help="default reports/factory/gen0_<date>")
    g0.add_argument("--config", default=None, help=f"family YAML (default {DEFAULT_FAMILY_CONFIG.name})")
    g0.add_argument("--run-id", default=None)
    g0.add_argument("--workers", type=int, default=16)
    g0.add_argument("--bench", action="store_true", help="measure evals/s with a fork pool")
    g0.add_argument("--bench-n", type=int, default=2000)
    g0.add_argument("--force", action="store_true",
                    help="overwrite an existing reports/factory/<run_id>/summary.json and the latest.json pointer")
    g0.set_defaults(func=cmd_gen0)

    b = sub.add_parser("board", help="print board.md rendered from the latest summary + coverage")
    b.set_defaults(func=cmd_board)

    c = sub.add_parser("coverage", help="regenerate reports/factory/coverage.json")
    c.add_argument("--out", default=None)
    c.set_defaults(func=cmd_coverage)

    s = sub.add_parser("status", help="print reports/factory/latest.json")
    s.set_defaults(func=cmd_status)

    # --- F2 EVOLVE: run / resume parser entries (begin) ---
    r = sub.add_parser("run", help="evolve campaigns A/B/C/ALL69 (+ blocked 5-fold) into data/factory/runs/<run_id>/")
    r.add_argument("--config", default=None, help=f"family YAML (default {DEFAULT_FAMILY_CONFIG.name})")
    r.add_argument("--frames", default=None, help="frozen frame dir (default: newest under data/factory/frames)")
    r.add_argument("--run-id", default=None, help="default run_<UTC date>; an existing run_id is refused")
    r.add_argument("--workers", type=int, default=None, help="default budget.workers (16)")
    r.add_argument("--population", type=int, default=None, help="default budget.population (400)")
    r.add_argument("--generations", type=int, default=None, help="default budget.generations (60)")
    r.add_argument("--master-seed", type=int, default=None, help="default budget.master_seed (20260902)")
    r.add_argument("--campaigns", default=None, help="comma list, default the config's (A,B,C,ALL69)")
    r.add_argument("--blocked-folds", dest="blocked_folds", action="store_true", default=True,
                   help="also run the blocked 5-fold diagnostic F1..F5 (default)")
    r.add_argument("--no-blocked-folds", dest="blocked_folds", action="store_false")
    r.add_argument("--out", default=None, help="tracked report dir (default reports/factory/<run_id>)")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="continue a run from its last fully scored generation (no-op when DONE)")
    rs.add_argument("run_id")
    rs.add_argument("--workers", type=int, default=None, help="override the worker count (results do not depend on it)")
    rs.add_argument("--out", default=None, help="tracked report dir (default reports/factory/<run_id>)")
    rs.set_defaults(func=cmd_resume)
    # --- F2 EVOLVE: run / resume parser entries (end) ---

    # ---- F2 STATS block: controls / report ---------------------------------
    ct = sub.add_parser("controls", help="run the 41 control replicates of a finished run (resumable) and write controls/summary.json")
    ct.add_argument("run_id")
    ct.add_argument("--kinds", default="snapshot,residual,planted", help="comma list of snapshot,residual,planted")
    ct.add_argument("--n-snapshot", type=int, default=20)
    ct.add_argument("--n-residual", type=int, default=20)
    ct.add_argument("--workers", type=int, default=None, help="override the budget's worker count")
    ct.add_argument("--config", default=None, help=f"family YAML (default {DEFAULT_FAMILY_CONFIG.name})")
    ct.add_argument("--frames", default=None, help="frozen frame dir (default: run.json's frames_dir)")
    ct.set_defaults(func=cmd_controls)

    rp = sub.add_parser("report", help="rebuild reports/factory/<run_id>/ from ledger + frames + picks + controls; transition the registry")
    rp.add_argument("run_id")
    rp.add_argument("--out", default=None, help="default reports/factory/<run_id>")
    rp.add_argument("--config", default=None, help=f"family YAML (default {DEFAULT_FAMILY_CONFIG.name})")
    rp.add_argument("--frames", default=None, help="frozen frame dir (default: run.json's frames_dir)")
    rp.add_argument("--no-transition", action="store_true", help="do not write the PROPOSED/CLOSED registry line")
    rp.add_argument("--sensitivity-frames", default=None,
                    help="embargo-2 frozen frame dir (`freeze-frame --embargo-days 2 --out-root DIR`) for sensitivity.embargo_2; "
                         "absent -> the condition is recorded as not applicable")
    rp.set_defaults(func=cmd_report)
    # ---- end F2 STATS block --------------------------------------------------

    for name in NOT_IMPLEMENTED:
        ni = sub.add_parser(name, help="F4 — not implemented yet")
        ni.set_defaults(func=cmd_not_implemented)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    # The remaining stubs accept (and ignore) any arguments so `factory.py report --x`
    # exits 2 with the not-implemented message instead of an argparse error.
    args, extra = parser.parse_known_args(argv)
    if extra and args.command not in NOT_IMPLEMENTED:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
