"""Throughput benchmark for the fitness kernel (``scripts/factory.py gen0 --bench``).

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 7.1 (fork pool
of 16 workers inheriting the slim arrays copy-on-write, ``os.sched_setaffinity``
per worker, ``imap_unordered(chunksize=64)``) and 7.2 (>= 3,000 genome
evaluations/s on 16 workers is the F1 exit criterion; peak RSS < 8 GB).

Process model
-------------
* Linux: ``multiprocessing.get_context("fork")``; the frame is placed in the
  module global ``_FRAME`` BEFORE the pool starts so every worker inherits it
  without pickling.
* Windows / no fork: ``spawn`` fallback; workers receive the frame DIRECTORY
  and ``frame.load`` it in the initializer (needs ``frame_dir``).
* Peak RSS: ``resource.getrusage`` (self + children) on Linux, ``psutil`` for
  the parent elsewhere, else ``None``.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np

_FRAME = None  # set by the parent before forking; loaded per worker under spawn
_CPUS: List[int] = []


# ---------------------------------------------------------------------------
# worker side
# ---------------------------------------------------------------------------
def _pin_affinity() -> None:
    if not hasattr(os, "sched_setaffinity"):
        return
    try:
        allowed = sorted(os.sched_getaffinity(0))
        if not allowed:
            return
        # Round-robin over the container's allowed cpus by the worker's ordinal
        # (multiprocessing names workers ForkPoolWorker-<n>).
        import multiprocessing as mp

        name = mp.current_process().name
        ordinal = int(name.rsplit("-", 1)[-1]) if "-" in name and name.rsplit("-", 1)[-1].isdigit() else 0
        os.sched_setaffinity(0, {allowed[ordinal % len(allowed)]})
    except (OSError, ValueError):
        pass


def _init_worker(frame_dir: Optional[str]) -> None:
    global _FRAME
    _pin_affinity()
    if _FRAME is None and frame_dir:
        from src.factory import frame as _frame

        _FRAME = _frame.load(frame_dir)


def _score_json(gj: str) -> float:
    from src.factory import fitness as _fitness
    from src.factory import genome as _genome

    from_json = getattr(_genome, "from_json", None) or _genome.Genome.from_json
    g = from_json(gj)
    mask = _genome.to_mask(g, _FRAME)
    r = _fitness.score(_FRAME, mask, constraints=True)
    return float(getattr(r, "fit", float("nan")))


# ---------------------------------------------------------------------------
# parent side
# ---------------------------------------------------------------------------
def _random_genomes(n: int, seed: int) -> List[str]:
    """``n`` random legal genomes as JSON strings, using whichever generator genome.py exposes."""
    from src.factory import genome as _genome

    rng = np.random.default_rng(seed)
    make = None
    for name in ("random_genome", "random"):
        fn = getattr(_genome, name, None)
        if callable(fn):
            make = fn
            break
    if make is None:
        cls = getattr(_genome, "Genome", None)
        fn = getattr(cls, "random", None) if cls is not None else None
        if callable(fn):
            make = fn
    out: List[str] = []
    if make is not None:
        for _ in range(n):
            out.append(_json(_genome, make(rng)))
        return out
    seeds = list(getattr(_genome, "SEEDS", {}).values())
    if not seeds:
        raise RuntimeError("genome.py exposes neither a random generator nor SEEDS")
    mutate = getattr(_genome, "mutate", None)
    for i in range(n):
        g = seeds[i % len(seeds)]
        if callable(mutate):
            g = mutate(g, rng)
        out.append(_json(_genome, g))
    return out


def _json(mod: Any, g: Any) -> str:
    out = g.to_json() if hasattr(g, "to_json") else mod.to_json(g)
    if isinstance(out, (dict, list)):
        out = json.dumps(out, sort_keys=True, separators=(",", ":"))
    return str(out)


def peak_rss_mb() -> Optional[float]:
    try:
        import resource  # POSIX only

        self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        kids_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        if sys.platform == "darwin":  # bytes there
            return round(max(self_kb, kids_kb) / 1e6, 1)
        return round(max(self_kb, kids_kb) / 1024.0, 1)
    except ImportError:
        pass
    try:
        import psutil

        return round(psutil.Process().memory_info().peak_wset / 1e6, 1) if hasattr(
            psutil.Process().memory_info(), "peak_wset"
        ) else round(psutil.Process().memory_info().rss / 1e6, 1)
    except Exception:
        return None


def bench_throughput(frame, n: int = 2000, workers: int = 16, *, frame_dir: Optional[str] = None,
                     seed: int = 0, chunksize: int = 64) -> Dict[str, Any]:
    """Score ``n`` random genomes on ``frame`` with a ``workers``-process pool.

    Returns ``{evals_per_s, workers, peak_rss_mb, host, n, elapsed_s, start_method}``.
    """
    global _FRAME
    import multiprocessing as mp

    genomes = _random_genomes(n, seed)
    methods = mp.get_all_start_methods()
    if "fork" in methods:
        ctx = mp.get_context("fork")
        _FRAME = frame
        init_arg: Optional[str] = None
    else:
        if not frame_dir and workers > 1:
            raise RuntimeError("spawn fallback needs frame_dir so workers can frame.load() it")
        ctx = mp.get_context("spawn")
        _FRAME = frame  # the workers=1 path scores in-process
        init_arg = str(frame_dir) if frame_dir else None

    t0 = time.perf_counter()
    if workers <= 1:
        _init_worker(None)
        for gj in genomes:
            _score_json(gj)
    else:
        with ctx.Pool(processes=workers, initializer=_init_worker, initargs=(init_arg,)) as pool:
            for _ in pool.imap_unordered(_score_json, genomes, chunksize=chunksize):
                pass
    elapsed = time.perf_counter() - t0
    return {
        "evals_per_s": round(n / elapsed, 1) if elapsed > 0 else None,
        "workers": int(workers),
        "peak_rss_mb": peak_rss_mb(),
        "host": platform.node(),
        "n": int(n),
        "elapsed_s": round(elapsed, 3),
        "start_method": ctx.get_start_method(),
    }
