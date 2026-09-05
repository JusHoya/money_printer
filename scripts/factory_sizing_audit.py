"""What folding the runtime's cold-start sizing law into admissibility would do.

Option 5 groundwork from ``reports/factory/sizing_cold_start_2026-09-05.md`` --
the *diagnostic*, not the re-search.  The frame builder already folds one of the
sandbox's two admission gates into ``executable`` (``sandbox_admissible``, the
mixin's EV gate, which passed 130/130 of the deployed genome's trades and never
binds) and models the other -- ``RiskManager.calculate_kelly_size > 0`` -- not at
all.  A v2 family would fold the sizing law in and re-search.  Before spending a
freeze, a search, a parity run and a gate registration on that, this answers the
question it turns on: **what is left, and what does it realize?**

Two shapes matter and the memo distinguishes them:

* ``as_deployed_*`` -- the genome's *already chosen* trade rows (one per market,
  the first masked executable candle) split by whether the runtime could size
  them.  This is what the sandbox is executing today, because
  ``genome_strategy`` burns a market on EMIT, not on fill (memo section 5a).
* ``v2_repick_*`` -- the first candle per market that is executable **and**
  sizable.  This is what a v2 frame whose ``sandbox_admissible`` carried the
  sizing law would actually search over, and it is strictly larger.

Every shape is scored by the factory's own ``fitness.score`` with
``constraints=False``, so ``realized_per_contract`` is the **date-clustered**
mean over ``target_date`` units that HANDOFF section 3 rule 3 requires, with the
same bootstrap the search used.

Read-only.  This script loads a frozen frame and builds row masks; it never
writes a frame, never mutates a column, and in particular never touches
``executable`` or ``sandbox_admissible``.  Changing those is a v2 family's job,
with its own freeze, search, parity run and gate registration.

Usage
-----
::

    export PYTHONPATH=.
    # one promoted genome (or a SEEDS name)
    python scripts/factory_sizing_audit.py --frames data/factory/frames/weather_2026-07-25_bfcf94654a3a \\
        --genome 0c4b20502f2daf65
    python scripts/factory_sizing_audit.py --frames <DIR> --seed fr31a_taker
    # every promoted spec on disk
    python scripts/factory_sizing_audit.py --frames <DIR> --all-promoted
    # the whole executable universe, no genome mask
    python scripts/factory_sizing_audit.py --frames <DIR> --universe --direction buy_no --mode taker
    # add --json PATH to save the numbers
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.factory import columns as C  # noqa: E402
from src.factory import fitness  # noqa: E402
from src.factory import genome as G  # noqa: E402
from src.factory import sizing as S  # noqa: E402

FRAMES_ROOT = REPO_ROOT / "data" / "factory" / "frames"


def _die(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    print(f"factory_sizing_audit: ABORT: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# masks (read-only over the frame)
# ---------------------------------------------------------------------------
def frame_sizable_mask(F: Any) -> np.ndarray:
    """Rows of ``F`` the runtime would give a non-zero Kelly size at cold start.

    A fresh boolean array; ``F`` is not touched.  The ticker is threaded per row
    so the fee is the series' own, exactly as ``calculate_kelly_size`` does it.
    """
    vis = F.visible
    tickers = np.asarray(F.markets)[vis["market_code"]]
    return S.sizable_mask(vis["price_paid"], vis["p_win"], symbols=tickers)


def _shape(F: Any, mask: np.ndarray, label: str, *, n_boot: int, seed: int) -> Dict[str, Any]:
    """One scored shape as a JSON-ready row (date-clustered realized, per HANDOFF 3.3)."""
    res = fitness.score(F, mask, constraints=False, label=label, n_boot=n_boot, seed=seed)
    rows = res.trade_rows
    per_trade = (
        float(np.mean(F.hidden["realized_per_contract"][rows])) if rows.shape[0] else float("nan")
    )

    def _f(x: float) -> Optional[float]:
        return None if (x is None or not math.isfinite(float(x))) else float(x)

    return {
        "shape": label,
        "candidate_rows": int(np.count_nonzero(mask)),
        "trades": int(res.trades),
        "markets": int(res.markets),
        "dates": int(res.dates),
        "cities": int(res.cities),
        "realized_per_contract": _f(res.realized),
        "realized_se": _f(res.realized_se),
        "t_stat": _f(res.t_stat),
        "boot_lo": _f(res.boot_lo),
        "boot_hi": _f(res.boot_hi),
        "realized_per_trade": _f(per_trade),
        "win_rate": _f(res.win_rate),
        "mean_price_paid": _f(res.mean_price_paid),
    }


def audit_genome_shapes(
    F: Any, genome: Any, *, n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED
) -> Dict[str, Any]:
    """The five shapes for one genome on one frozen frame.  Does not modify ``F``."""
    sizable = frame_sizable_mask(F)
    mask = np.asarray(G.to_mask(genome, F), dtype=bool)
    rows = S.genome_trade_rows(F, genome)

    # "as deployed": the genome's already-chosen rows, split by sizability.
    kept = np.zeros(F.n_rows, dtype=bool)
    drop = np.zeros(F.n_rows, dtype=bool)
    if rows.shape[0]:
        keep_sel = sizable[rows]
        kept[rows[keep_sel]] = True
        drop[rows[~keep_sel]] = True

    audit = S.audit_genome(F, genome, label=str(getattr(genome, "name", "genome")))
    shapes = [
        _shape(F, mask, "registered", n_boot=n_boot, seed=seed),
        _shape(F, kept, "as_deployed_sizable", n_boot=n_boot, seed=seed),
        _shape(F, drop, "as_deployed_unsizable", n_boot=n_boot, seed=seed),
        _shape(F, mask & sizable, "v2_repick_sizable", n_boot=n_boot, seed=seed),
        _shape(F, mask & ~sizable, "v2_repick_unsizable", n_boot=n_boot, seed=seed),
    ]
    return {
        "frame": {"name": F.name, "n_rows": int(F.n_rows), "n_markets": int(F.n_markets),
                  "n_dates": int(F.n_dates), "sha256": str(F.provenance.get("frame_sha256", ""))},
        "genome": {"name": str(getattr(genome, "name", "genome")),
                   "source": str(getattr(genome, "source", "")),
                   "direction": C.DIRECTION_LABELS[int(genome.direction)],
                   "mode": C.MODE_LABELS[int(genome.mode)],
                   "describe": genome.describe() if hasattr(genome, "describe") else ""},
        "cold_start": audit.as_dict(),
        "shapes": shapes,
    }


def audit_universe_shapes(
    F: Any, *, direction: str = "buy_no", mode: str = "taker",
    n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED,
) -> Dict[str, Any]:
    """The same split over a whole ``(direction, mode)`` slice, with no genome mask."""
    dcode = C.code_for(C.DIRECTION_LABELS, direction)
    mcode = C.code_for(C.MODE_LABELS, mode)
    if dcode < 0 or mcode < 0:
        raise ValueError(f"unknown direction/mode {direction!r}/{mode!r}")
    vis = F.visible
    slice_mask = (vis["direction_code"] == dcode) & (vis["mode_code"] == mcode)
    sizable = frame_sizable_mask(F)
    shapes = [
        _shape(F, slice_mask, "universe", n_boot=n_boot, seed=seed),
        _shape(F, slice_mask & sizable, "v2_repick_sizable", n_boot=n_boot, seed=seed),
        _shape(F, slice_mask & ~sizable, "v2_repick_unsizable", n_boot=n_boot, seed=seed),
    ]
    exec_slice = slice_mask & vis["executable"]
    return {
        "frame": {"name": F.name, "n_rows": int(F.n_rows), "n_markets": int(F.n_markets),
                  "n_dates": int(F.n_dates), "sha256": str(F.provenance.get("frame_sha256", ""))},
        "universe": {
            "direction": direction, "mode": mode,
            "slice_rows": int(np.count_nonzero(slice_mask)),
            "executable_rows": int(np.count_nonzero(exec_slice)),
            "executable_rows_sizable": int(np.count_nonzero(exec_slice & sizable)),
            "sizable_row_fraction": (
                float(np.count_nonzero(exec_slice & sizable) / np.count_nonzero(exec_slice))
                if np.count_nonzero(exec_slice) else float("nan")
            ),
        },
        "shapes": shapes,
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
_COLS = ("shape", "candidate_rows", "trades", "dates", "realized_per_contract",
         "realized_per_trade", "t_stat", "boot_lo", "boot_hi", "win_rate", "mean_price_paid")


def render(doc: Dict[str, Any]) -> str:
    out: List[str] = []
    fr = doc["frame"]
    out.append(f"frame {fr['name']} ({str(fr['sha256'])[:12]}): {fr['n_rows']} rows, "
               f"{fr['n_markets']} markets, {fr['n_dates']} dates")
    if "genome" in doc:
        g = doc["genome"]
        out.append(f"genome {g['name']} [{g['source']}] {g['direction']}/{g['mode']}")
        cs = doc["cold_start"]
        out.append(
            f"cold-start sizing: {cs['n_sizable']}/{cs['n_trades']} trades sizable "
            f"({cs['sizable_fraction']:.1%}), {cs['n_dates_sizable']}/{cs['n_dates']} target_date units; "
            f"median price_paid {cs['median_price']:.3f} vs ceiling {cs['median_ceiling']:.3f} "
            f"-> promotion guard {'PASS' if cs['ok'] else 'REFUSE'} at {cs['min_sizable_fraction']:.0%}"
        )
    if "universe" in doc:
        u = doc["universe"]
        out.append(f"universe {u['direction']}/{u['mode']}: {u['executable_rows']} executable rows, "
                   f"{u['executable_rows_sizable']} sizable ({u['sizable_row_fraction']:.1%})")
    head = (
        f"{'shape':22s} {'rows':>7s} {'trades':>7s} {'dates':>6s} {'realiz/ct':>10s} "
        f"{'/trade':>9s} {'t':>7s} {'boot_lo':>9s} {'boot_hi':>9s} {'winrate':>8s} {'meanpx':>7s}"
    )
    out.append(head)
    out.append("-" * len(head))
    for s in doc["shapes"]:
        def g(k: str) -> str:
            v = s.get(k)
            return "     n/a" if v is None else f"{float(v):+.5f}" if k.startswith(("realiz", "boot")) else f"{float(v):.4f}"
        out.append(
            f"{s['shape']:22s} {s['candidate_rows']:7d} {s['trades']:7d} {s['dates']:6d} "
            f"{g('realized_per_contract'):>10s} {g('realized_per_trade'):>9s} "
            f"{'    n/a' if s['t_stat'] is None else format(s['t_stat'], '+7.2f')} "
            f"{g('boot_lo'):>9s} {g('boot_hi'):>9s} "
            f"{'     n/a' if s['win_rate'] is None else format(s['win_rate'], '8.4f')} "
            f"{'    n/a' if s['mean_price_paid'] is None else format(s['mean_price_paid'], '7.4f')}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _latest_frames_dir() -> Optional[Path]:
    if not FRAMES_ROOT.exists():
        return None
    dirs = sorted((d for d in FRAMES_ROOT.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


def _load_frame(frames_dir: Path, source: str):
    from src.factory import frame as FR

    sub = "gefs_twin" if source == "gefs" else "search"
    path = frames_dir / sub
    if not path.exists():
        _die(f"{path} does not exist (a {source!r} genome needs the {sub!r} frame)")
    return FR.load(str(path))


def _genomes(args: argparse.Namespace) -> List[Any]:
    from src.factory import promoted as P

    if args.seed:
        if args.seed not in G.SEEDS:
            _die(f"unknown seed {args.seed!r}; have {sorted(G.SEEDS)}")
        return [G.SEEDS[args.seed]]
    if args.all_promoted:
        d = Path(args.promoted_dir) if args.promoted_dir else Path(P.PROMOTED_DIR)
        specs = sorted(d.glob("*.json"))
        if not specs:
            _die(f"no promoted specs under {d}")
        return [P.load_promoted(str(p)).genome() for p in specs]
    spec = P.load_promoted(args.genome, directory=str(args.promoted_dir or P.PROMOTED_DIR))
    return [spec.genome()]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="factory_sizing_audit.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--frames", default=None, help="frozen frame dir (default newest under data/factory/frames)")
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--genome", default=None, help="promoted genome id or spec path")
    sel.add_argument("--seed", default=None, help="a src.factory.genome.SEEDS name")
    sel.add_argument("--all-promoted", action="store_true", help="every spec in configs/factory/promoted/")
    sel.add_argument("--universe", action="store_true", help="no genome: a whole (direction, mode) slice")
    ap.add_argument("--direction", default="buy_no", choices=C.DIRECTION_LABELS, help="--universe only")
    ap.add_argument("--mode", default="taker", choices=C.MODE_LABELS, help="--universe only")
    ap.add_argument("--source", default="gfs_mex", choices=("gfs_mex", "gefs"), help="--universe frame (default gfs_mex/search)")
    ap.add_argument("--promoted-dir", default=None, help=f"default {Path('configs/factory/promoted')}")
    ap.add_argument("--n-boot", type=int, default=fitness.DEFAULT_N_BOOT,
                    help=f"bootstrap draws (default {fitness.DEFAULT_N_BOOT}, the search's)")
    ap.add_argument("--seed-rng", type=int, default=fitness.DEFAULT_SEED,
                    help=f"bootstrap seed (default {fitness.DEFAULT_SEED}, fitness.DEFAULT_SEED -- so the "
                         "intervals here are the ones the search itself would print)")
    ap.add_argument("--json", default=None, help="also write the numbers to this path")
    args = ap.parse_args(argv)

    frames_dir = Path(args.frames) if args.frames else _latest_frames_dir()
    if frames_dir is None or not frames_dir.exists():
        _die("no frozen frames found; pass --frames DIR")

    docs: List[Dict[str, Any]] = []
    if args.universe:
        F = _load_frame(frames_dir, args.source)
        docs.append(audit_universe_shapes(F, direction=args.direction, mode=args.mode,
                                          n_boot=args.n_boot, seed=args.seed_rng))
    else:
        by_source: Dict[str, Any] = {}
        for g in _genomes(args):
            src = str(getattr(g, "source", "gfs_mex"))
            if src not in by_source:
                by_source[src] = _load_frame(frames_dir, src)
            docs.append(audit_genome_shapes(by_source[src], g, n_boot=args.n_boot, seed=args.seed_rng))

    for d in docs:
        print(render(d))
        print()

    if args.json:
        out = {"frames_dir": str(frames_dir), "finding_report": S.FINDING_REPORT,
               "min_sizable_trade_fraction": S.MIN_SIZABLE_TRADE_FRACTION,
               "audits": docs}
        if len(docs) == 1:
            out.update(docs[0])
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"wrote {os.path.relpath(path, REPO_ROOT) if str(path).startswith(str(REPO_ROOT)) else path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
