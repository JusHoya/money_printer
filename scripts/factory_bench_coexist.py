#!/usr/bin/env python3
"""Coexistence bench: mp-vllm token latency with the factory idle vs running (FR-F2.6).

Runs on the alcyone HOST (stdlib only -- the ``factory`` container has no
network, and this must talk to http://127.0.0.1:8000/v1). Design record:
``docs/factory/FACTORY_ARCHITECTURE.md`` section 7.2 ("records mp-vllm p50 token
latency for a fixed prompt with the factory idle vs running; acceptance:
<=10% change") and PRD_STRATEGY_FACTORY Phase F2 exit criteria.

    # before the run (factory idle)
    python3 scripts/factory_bench_coexist.py --label idle    --out reports/factory/<run_id>/bench.json
    # mid-run (factory evolving on the 16-core cpuset)
    python3 scripts/factory_bench_coexist.py --label running --out reports/factory/<run_id>/bench.json
    # verdict
    python3 scripts/factory_bench_coexist.py --compare        --out reports/factory/<run_id>/bench.json
    # factory throughput + host numbers (the run wrapper does this itself)
    python3 scripts/factory_bench_coexist.py --out ... --throughput-from data/factory/runs/<run_id>/status.json \\
        --wall-s 5400 --extra used_gib_peak=31

Each sample sends the SAME fixed prompt (``FIXED_PROMPT``) to
``<endpoint>/chat/completions`` with ``stream: true`` and measures, from the
SSE chunks, time-to-first-token (TTFT) and every inter-token gap. The report
under ``mp_vllm.<label>`` carries p50/p90 inter-token ms, p50 TTFT ms and
tokens/s over the N samples; ``--compare`` writes ``mp_vllm.compare`` with
``p50_change_pct = (running - idle) / idle * 100`` and ``pass = |change| <= 10``.

``bench.json`` is a tracked report file: it is merged, never clobbered, and
carries no wall-clock timestamp (sort_keys, indent 2, trailing newline).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL = "ykarout/Qwen3.5-9B-NVFP4"
THRESHOLD_PCT = 10.0

# Fixed prompt: deterministic, ~80 prompt tokens, asks for a long plain answer
# so max_tokens (not an early stop) bounds the sample. Do not edit between
# the idle and running samples of one run (its sha256 is recorded).
FIXED_PROMPT = (
    "You are a meticulous technical writer. In plain prose, without lists or "
    "headings, explain how a walk-forward validation with a two-day embargo "
    "differs from blocked k-fold cross-validation for a daily prediction-market "
    "strategy, why the embargo exists, and what a pooled out-of-sample mean over "
    "thirty-three validation dates does and does not tell an operator about "
    "live capital. Keep going until you are cut off."
)
SYSTEM_PROMPT = "Answer directly. Do not use markdown."


# ---------------------------------------------------------------------------
# JSON helpers (same conventions as src/factory/report.py: sorted, indented, newline)
# ---------------------------------------------------------------------------
def load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: str, obj: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, path)


def deep_merge(base: Dict[str, Any], add: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in add.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# statistics (no numpy on the host)
# ---------------------------------------------------------------------------
def percentile(xs: Sequence[float], p: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy 'linear'); None on empty input."""
    if not xs:
        return None
    s = sorted(float(x) for x in xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------
def sse_payloads(lines: Iterable[bytes]) -> Iterator[Any]:
    """Yield the JSON payload of every ``data:`` line; stop at ``data: [DONE]``.

    Non-data lines (comments, blank keep-alives, ``event:``) are skipped; a
    payload that is not JSON is skipped too (never raises mid-stream).
    """
    for raw in lines:
        line = raw.strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip().encode("utf-8")
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            return
        if not payload:
            continue
        try:
            yield json.loads(payload.decode("utf-8"))
        except ValueError:
            continue


def content_of(chunk: Any) -> str:
    """The text delta of one chat.completion.chunk ("" when it carries none)."""
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta") or {}
    if not isinstance(delta, dict):
        return ""
    # Thinking models stream their reasoning first: vLLM <= 0.10 under
    # ``reasoning_content``, vLLM 0.28 (alcyone, verified 2026-09-03) under
    # ``reasoning``. A chunk carrying any of the three is one generated token.
    for key in ("content", "reasoning_content", "reasoning"):
        text = delta.get(key)
        if isinstance(text, str) and text:
            return text
    return ""


def time_stream(lines: Iterable[bytes], t_send: float, clock: Callable[[], float]) -> Dict[str, Any]:
    """Consume an SSE stream, stamping each content chunk with ``clock()``.

    Returns ``{ttft_ms, inter_token_ms: [...], n_tokens, n_chars, gen_ms, total_ms,
    usage}``. A "token" is one chunk with a non-empty content delta (vLLM streams
    one token per chunk by default); ``usage`` is the final usage object if the
    server sent one (``stream_options.include_usage``).
    """
    stamps: List[float] = []
    n_chars = 0
    usage = None
    for chunk in sse_payloads(lines):
        text = content_of(chunk)
        if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        if text:
            stamps.append(clock())
            n_chars += len(text)
    t_end = clock()
    gaps = [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:])]
    return {
        "ttft_ms": (stamps[0] - t_send) * 1000.0 if stamps else None,
        "inter_token_ms": gaps,
        "n_tokens": len(stamps),
        "n_chars": n_chars,
        "gen_ms": (stamps[-1] - stamps[0]) * 1000.0 if len(stamps) > 1 else 0.0,
        "total_ms": (t_end - t_send) * 1000.0,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# one request
# ---------------------------------------------------------------------------
def request_body(model: str, max_tokens: int) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": FIXED_PROMPT},
        ],
        "max_tokens": int(max_tokens),
        "temperature": 0.0,
        "seed": 20260902,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def stream_once(endpoint: str, model: str, max_tokens: int, timeout: float = 120.0,
                clock: Callable[[], float] = time.perf_counter) -> Dict[str, Any]:
    url = endpoint.rstrip("/") + "/chat/completions"
    data = json.dumps(request_body(model, max_tokens)).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                 "Authorization": "Bearer " + os.getenv("MP_VLLM_API_KEY", "none")},
    )
    t_send = clock()
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback endpoint)
        return time_stream(resp, t_send, clock)


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def aggregate(samples: Sequence[Dict[str, Any]], *, label: str, endpoint: str, model: str,
              max_tokens: int) -> Dict[str, Any]:
    gaps: List[float] = [g for s in samples for g in s.get("inter_token_ms") or []]
    ttfts = [s["ttft_ms"] for s in samples if s.get("ttft_ms") is not None]
    tokens = sum(int(s.get("n_tokens") or 0) for s in samples)
    gen_s = sum(float(s.get("gen_ms") or 0.0) for s in samples) / 1000.0
    return {
        "label": label,
        "endpoint": endpoint,
        "model": model,
        "max_tokens": int(max_tokens),
        "prompt_sha256": hashlib.sha256(FIXED_PROMPT.encode("utf-8")).hexdigest(),
        "n": len(samples),
        "n_ok": len(ttfts),
        "p50_inter_token_ms": _r(percentile(gaps, 50)),
        "p90_inter_token_ms": _r(percentile(gaps, 90)),
        "p50_ttft_ms": _r(percentile(ttfts, 50)),
        "tokens_per_s": _r(tokens / gen_s, 1) if gen_s > 0 else None,
        "tokens_total": tokens,
        "gaps_total": len(gaps),
        "samples": [
            {"ttft_ms": _r(s.get("ttft_ms")), "n_tokens": s.get("n_tokens"),
             "p50_inter_token_ms": _r(percentile(s.get("inter_token_ms") or [], 50)),
             "total_ms": _r(s.get("total_ms"))}
            for s in samples
        ],
    }


def _r(x: Any, nd: int = 2) -> Optional[float]:
    try:
        return None if x is None else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def compare(idle: Optional[Dict[str, Any]], running: Optional[Dict[str, Any]],
            threshold_pct: float = THRESHOLD_PCT) -> Dict[str, Any]:
    """``p50_change_pct = (running - idle) / idle * 100``; ``pass = |change| <= threshold``.

    Missing or zero idle -> ``pass`` is None (not a pass), with a reason.
    """
    i = (idle or {}).get("p50_inter_token_ms")
    r = (running or {}).get("p50_inter_token_ms")
    out: Dict[str, Any] = {
        "metric": "p50_inter_token_ms",
        "idle": i,
        "running": r,
        "threshold_pct": float(threshold_pct),
        "p50_change_pct": None,
        "ttft_change_pct": None,
        "pass": None,
    }
    if i is None or r is None:
        out["reason"] = "missing idle or running sample"
        return out
    if float(i) <= 0:
        out["reason"] = "idle p50 is zero"
        return out
    change = (float(r) - float(i)) / float(i) * 100.0
    out["p50_change_pct"] = round(change, 2)
    out["pass"] = abs(change) <= float(threshold_pct)
    ti = (idle or {}).get("p50_ttft_ms")
    tr = (running or {}).get("p50_ttft_ms")
    if ti and tr and float(ti) > 0:
        out["ttft_change_pct"] = round((float(tr) - float(ti)) / float(ti) * 100.0, 2)
    return out


# ---------------------------------------------------------------------------
# factory throughput (EVOLVE writes evaluations + wall into status.json / run.json)
# ---------------------------------------------------------------------------
_EVAL_KEYS = ("evaluations", "n_evaluations", "evals")
_WALL_KEYS = ("wall_s", "wall_seconds", "elapsed_s", "elapsed_seconds", "wall")


def _first(doc: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        v = doc.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    thr = doc.get("throughput")
    if isinstance(thr, dict):
        for k in keys:
            v = thr.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def throughput_from(paths: Sequence[str], wall_s: Optional[float] = None) -> Dict[str, Any]:
    """Merge whatever exists: evaluations, wall seconds, state, gen/n_gens; tolerate absence."""
    out: Dict[str, Any] = {"sources": []}
    evals: Optional[float] = None
    wall: Optional[float] = None
    for p in paths:
        doc = load_json(p)
        if doc is None:
            out["sources"].append({"path": p, "found": False})
            continue
        out["sources"].append({"path": p, "found": True})
        if evals is None:
            evals = _first(doc, _EVAL_KEYS)
        if wall is None:
            wall = _first(doc, _WALL_KEYS)
        for k in ("state", "phase", "campaign", "gen", "n_gens", "n_phenotypes", "run_id"):
            if k in doc and k not in out:
                out[k] = doc[k]
        thr = doc.get("throughput")
        if isinstance(thr, dict) and "evals_per_s" in thr and "evals_per_s_reported" not in out:
            out["evals_per_s_reported"] = thr.get("evals_per_s")
    if wall_s is not None:
        wall = float(wall_s)
    out["evaluations"] = int(evals) if evals is not None else None
    out["wall_s"] = _r(wall, 1)
    out["evals_per_s"] = _r(evals / wall, 1) if (evals is not None and wall and wall > 0) else None
    return out


def parse_extra(items: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--extra wants key=value, got {it!r}")
        k, v = it.split("=", 1)
        k = k.strip()
        v = v.strip()
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_samples(endpoint: str, model: str, n: int, max_tokens: int, *, warmup: int = 1,
                timeout: float = 120.0, log: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    for i in range(max(0, warmup)):
        try:
            stream_once(endpoint, model, max_tokens, timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SystemExit(f"warmup request failed against {endpoint}: {e}")
    samples: List[Dict[str, Any]] = []
    for i in range(n):
        s = stream_once(endpoint, model, max_tokens, timeout=timeout)
        samples.append(s)
        log(f"sample {i + 1}/{n}: ttft {_r(s['ttft_ms'])} ms, {s['n_tokens']} tokens, "
            f"p50 gap {_r(percentile(s['inter_token_ms'], 50))} ms, total {_r(s['total_ms'])} ms")
    return samples


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory_bench_coexist.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--n", type=int, default=12, help="samples (>= 10 for the F2 record)")
    p.add_argument("--max-tokens", type=int, default=96)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--label", choices=("idle", "running"), default=None,
                   help="sample mp-vllm now and record under mp_vllm.<label>")
    p.add_argument("--out", required=True, help="bench.json to merge into (created if absent)")
    p.add_argument("--compare", action="store_true", help="write mp_vllm.compare from the idle/running samples")
    p.add_argument("--threshold-pct", type=float, default=THRESHOLD_PCT)
    p.add_argument("--throughput-from", action="append", default=[],
                   help="status.json / run.json of the run (evaluations + wall seconds; repeatable, absent files tolerated)")
    p.add_argument("--throughput-json", default=None, help="a JSON object merged verbatim under factory.throughput")
    p.add_argument("--wall-s", type=float, default=None, help="wall seconds measured by the wrapper")
    p.add_argument("--extra", action="append", default=[], help="key=value merged under factory.host (repeatable)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    doc = load_json(args.out) or {}
    doc.setdefault("kind", "coexistence_bench")
    doc.setdefault("mp_vllm", {})
    doc.setdefault("factory", {})
    did = False

    if args.label:
        if args.n < 1:
            raise SystemExit("--n must be >= 1")
        samples = run_samples(args.endpoint, args.model, args.n, args.max_tokens, warmup=args.warmup, timeout=args.timeout)
        doc["mp_vllm"][args.label] = aggregate(samples, label=args.label, endpoint=args.endpoint,
                                               model=args.model, max_tokens=args.max_tokens)
        a = doc["mp_vllm"][args.label]
        print(f"{args.label}: p50 inter-token {a['p50_inter_token_ms']} ms, p90 {a['p90_inter_token_ms']} ms, "
              f"p50 TTFT {a['p50_ttft_ms']} ms, {a['tokens_per_s']} tok/s over {a['n']} samples")
        did = True

    if args.throughput_from or args.wall_s is not None:
        thr = throughput_from(args.throughput_from, wall_s=args.wall_s)
        doc["factory"]["throughput"] = deep_merge(doc["factory"].get("throughput") or {}, thr)
        did = True
    if args.throughput_json:
        extra = load_json(args.throughput_json)
        if extra is None:
            raise SystemExit(f"--throughput-json {args.throughput_json}: not a JSON object")
        doc["factory"]["throughput"] = deep_merge(doc["factory"].get("throughput") or {}, extra)
        did = True
    if args.extra:
        doc["factory"]["host"] = deep_merge(doc["factory"].get("host") or {}, parse_extra(args.extra))
        did = True

    if args.compare:
        cmp_ = compare(doc["mp_vllm"].get("idle"), doc["mp_vllm"].get("running"), args.threshold_pct)
        doc["mp_vllm"]["compare"] = cmp_
        verdict = "PASS" if cmp_["pass"] else ("FAIL" if cmp_["pass"] is False else "INCOMPLETE")
        print(f"compare: idle {cmp_['idle']} ms -> running {cmp_['running']} ms = {cmp_['p50_change_pct']}% "
              f"(threshold {cmp_['threshold_pct']}%) {verdict}")
        did = True

    if not did:
        build_parser().error("nothing to do: give --label, --compare, --throughput-from/--throughput-json/--wall-s or --extra")
    write_json(args.out, doc)
    print(f"wrote {args.out}")
    if args.compare and doc["mp_vllm"]["compare"]["pass"] is False:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
