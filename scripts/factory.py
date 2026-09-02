#!/usr/bin/env python
"""Strategy-factory CLI (PRD_STRATEGY_FACTORY FR-F1.6; FACTORY_ARCHITECTURE section 1.3).

    python scripts/factory.py freeze-frame [--cutoff 2026-07-25 ...]
    python scripts/factory.py gen0 [--frames DIR] [--out reports/factory/gen0_<date>] [--workers N] [--bench]
    python scripts/factory.py board | coverage | status
    python scripts/factory.py run|resume|controls|report|holdout|score|promote   -> exit 2 (F2+/F4)

Runs inside the ``factory`` compose service on alcyone (network_mode: none;
``/app`` read-only except ``data/factory`` and ``reports/factory``):

    docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory \
        python scripts/factory.py freeze-frame

Heavy modules (``src.factory.frame``, ``lanes``, ``gen0``, pandas/pyarrow) are
imported lazily inside the subcommands so ``--help`` never touches them.

``run.json`` (written by ``freeze-frame`` and ``gen0``): run_id, git_rev
(non-empty or abort), lock_sha256 (``deploy/spark/requirements-lab.lock``),
frame sha256s, config, fee_regime_sha256, python/numpy/pandas versions, host uid.

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
NOT_IMPLEMENTED = ("run", "resume", "controls", "report", "holdout", "score", "promote")


# ---------------------------------------------------------------------------
# helpers (no heavy imports)
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
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
    from src.factory.gen0 import run_gen0

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
    g0.set_defaults(func=cmd_gen0)

    b = sub.add_parser("board", help="print board.md rendered from the latest summary + coverage")
    b.set_defaults(func=cmd_board)

    c = sub.add_parser("coverage", help="regenerate reports/factory/coverage.json")
    c.add_argument("--out", default=None)
    c.set_defaults(func=cmd_coverage)

    s = sub.add_parser("status", help="print reports/factory/latest.json")
    s.set_defaults(func=cmd_status)

    for name in NOT_IMPLEMENTED:
        ni = sub.add_parser(name, help="F2+/F4 — not implemented in F1")
        ni.set_defaults(func=cmd_not_implemented)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    # The F2+/F4 stubs accept (and ignore) any arguments so `factory.py run --config x`
    # exits 2 with the not-implemented message instead of an argparse error.
    args, extra = parser.parse_known_args(argv)
    if extra and args.command not in NOT_IMPLEMENTED:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
