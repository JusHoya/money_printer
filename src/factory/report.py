"""Gen-0 report rendering: ``summary.{json,md}``, ``board.md``, ``status.json``, ``latest.json``.

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 1.1 (report.py),
7.3 (``status.json`` timestamp-free), 8 (storage layout), 10 (board columns);
PRD_STRATEGY_FACTORY section 6 (honesty is the interface; board columns; quiet
progress).

Everything here tolerates missing keys (rendered as an em dash): the gen-0
summary is produced by ``src.factory.gen0`` and its exact contents may grow.
No wall-clock value is written into ``summary.json`` / ``board.md`` /
``status.json`` -- the Hermes cron byte-hashes ``board.md`` and must stay
silent when nothing changed.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

DASH = "—"
SEED_ORDER = (
    "fr31a_taker",
    "fr31b",
    "nofilter_no",
    "salvage_5f",
    "mlweather_fallback",
    "fr31a_gefs",
    "far_yes_taker",
)
FR31A_REFERENCE = {"trades": 181, "dates": 65, "realized": 0.0636}
TIMESTAMP_KEY_HINTS = ("time", "date_", "_at", "ts", "stamp", "when", "generated", "created", "updated")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _norm_paths(obj: Any) -> Any:
    """Normalise path separators in every string leaf (byte-identical reports across hosts)."""
    if isinstance(obj, dict):
        return {k: _norm_paths(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm_paths(v) for v in obj]
    if isinstance(obj, str) and "\\" in obj:
        return obj.replace("\\", "/")
    return obj


def _json_safe(obj: Any) -> Any:
    """NaN / inf -> None; numpy scalars -> python; sets -> sorted lists."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_json_safe(v) for v in obj)
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            obj = obj.item()
        except Exception:  # pragma: no cover - exotic numpy objects
            return str(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _fmt(v: Any, nd: int = 4, signed: bool = True) -> str:
    if v is None:
        return DASH
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return "-inf" if v < 0 else "+inf"
        return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if v else DASH
    return str(v)


def _ci(row: Any) -> str:
    lo, hi = _g(row, "boot_lo"), _g(row, "boot_hi")
    if lo is None or hi is None:
        return DASH
    return f"[{_fmt(lo)}, {_fmt(hi)}]"


def _row_cell(row: Any) -> str:
    """``realized [lo, hi] / trades t / dates d`` or a reason code."""
    if not isinstance(row, dict):
        return DASH
    reason = row.get("constraint_reason")
    fit = row.get("fit")
    # -inf in memory; None after the summary.json round-trip (write_json maps -inf -> null)
    if (isinstance(fit, float) and not math.isfinite(fit)) or (fit is None and reason):
        return f"KILLED:{reason or '?'}"
    return f"{_fmt(row.get('realized'))} {_ci(row)} n={_fmt(row.get('trades'))} d={_fmt(row.get('dates'))}"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_json_safe(_norm_paths(obj)), sort_keys=True, indent=2)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    os.replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# headline extraction (shared by summary.md, board, status.json, Hermes)
# ---------------------------------------------------------------------------
def seed_names(summary: Dict[str, Any]) -> List[str]:
    seeds = _g(summary, "seeds", default={}) or {}
    ordered = [s for s in SEED_ORDER if s in seeds]
    return ordered + sorted(s for s in seeds if s not in ordered)


def parity_check(summary: Dict[str, Any]) -> Dict[str, Any]:
    """The fr31a parity line: does the kernel reproduce 181 / 65 / +0.0636 on the parity frame?"""
    seed = _g(summary, "seeds", "fr31a_taker", default={}) or {}
    row = seed.get("parity_full") or {}
    ref = seed.get("reference") or {}
    matches = ref.get("matches_1e9")
    if matches is None and row:
        try:
            matches = (
                int(row.get("trades", -1)) == FR31A_REFERENCE["trades"]
                and int(row.get("dates", -1)) == FR31A_REFERENCE["dates"]
                and abs(float(row.get("realized", 9)) - FR31A_REFERENCE["realized"]) < 5e-5
            )
        except (TypeError, ValueError):
            matches = None
    return {
        "expected": dict(FR31A_REFERENCE),
        "trades": row.get("trades"),
        "dates": row.get("dates"),
        "realized": row.get("realized"),
        "boot_lo": row.get("boot_lo"),
        "boot_hi": row.get("boot_hi"),
        "matches_1e9": matches,
        "fields_differing": ref.get("fields_differing", []),
        "label": ref.get("label"),
    }


def headline(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, timestamp-free numbers for latest.json / status.json / Hermes."""
    seeds = _g(summary, "seeds", default={}) or {}

    def _seed(name: str) -> Dict[str, Any]:
        s = seeds.get(name) or {}
        out: Dict[str, Any] = {}
        for k in ("parity_full", "search_full"):
            r = s.get(k) or {}
            out[k] = {f: r.get(f) for f in ("trades", "dates", "realized", "boot_lo", "boot_hi", "fit", "constraint_reason")}
        camps = s.get("campaigns") or {}
        out["validation"] = {
            c: {f: (camps.get(c, {}).get("validation") or {}).get(f) for f in ("trades", "dates", "realized", "boot_lo", "boot_hi")}
            for c in ("A", "B", "C")
            if c in camps
        }
        out["phenotype_hash"] = s.get("phenotype_hash")
        out["notes"] = s.get("notes")
        return out

    return {
        "run_id": summary.get("run_id"),
        "kind": summary.get("kind"),
        "family": summary.get("family"),
        "registry_status": _g(summary, "registry_line", "status"),
        "git_rev": summary.get("git_rev"),
        "parity_check": parity_check(summary),
        "seeds": {n: _seed(n) for n in seed_names(summary)},
        "brier_skill_vs_market": summary.get("brier_skill_vs_market"),
        "throughput": summary.get("throughput"),
        "n_phenotypes": len({(seeds[s] or {}).get("phenotype_hash") for s in seeds if (seeds[s] or {}).get("phenotype_hash")}),
    }


def render_status_json(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Timestamp-free progress document (section 7.3)."""
    h = headline(summary)
    fr = summary.get("frame") or {}
    return {
        "run_id": h["run_id"],
        "kind": h["kind"],
        "family": h["family"],
        "registry_status": h["registry_status"],
        "git_rev": h["git_rev"],
        "frame": {k: fr.get(k) for k in ("parity_sha256", "search_sha256", "gefs_twin_sha256", "parity_rows", "search_rows")},
        "parity_matches_1e9": h["parity_check"]["matches_1e9"],
        "seeds_scored": list(h["seeds"]),
        "n_phenotypes": h["n_phenotypes"],
        "campaigns": ["A", "B", "C", "ALL69"],
        "phase": "F1",
        "evolution": "not started (F2)",
        "controls": "n/a (F2)",
    }


def _bss_entries(summary: Dict[str, Any]) -> List[Any]:
    """``[(frame_name, bss_dict)]``: gen0 writes ``{"parity": {...}, "search": {...}}``; a flat dict is one entry."""
    bss = summary.get("brier_skill_vs_market") or {}
    if not isinstance(bss, dict):
        return [("frame", {})]
    if "bss" in bss or not bss:
        return [("frame", bss)]
    return [(str(k), v if isinstance(v, dict) else {}) for k, v in bss.items()]


# ---------------------------------------------------------------------------
# summary.md
# ---------------------------------------------------------------------------
def render_summary_md(summary: Dict[str, Any]) -> str:
    fam = summary.get("family", DASH)
    reg = summary.get("registry_line") or {}
    pc = parity_check(summary)
    lines: List[str] = []
    lines.append(f"# Factory gen-0 report -- `{summary.get('run_id', DASH)}`")
    lines.append("")
    lines.append(f"**Family** `{fam}` -- registry status **{reg.get('status', 'UNREGISTERED')}** "
                 f"(picker `{reg.get('picker', DASH)}`, config sha `{str(reg.get('config_sha256', DASH))[:12]}`, "
                 f"cutoff {reg.get('cutoff', DASH)})")
    lines.append("")
    m = pc["matches_1e9"]
    verdict = "yes" if m else ("no" if m is False else DASH)
    lines.append(
        f"**Parity check** fr31a_taker on the parity frame: expected 181 / 65 / +0.0636; "
        f"kernel {_fmt(pc['trades'])} / {_fmt(pc['dates'])} / {_fmt(pc['realized'])} "
        f"boot [{_fmt(pc['boot_lo'])}, {_fmt(pc['boot_hi'])}] -- matches within 1e-9: **{verdict}**"
        + (f" (differing: {', '.join(pc['fields_differing'])})" if pc["fields_differing"] else "")
    )
    lines.append("")
    lines.append("Frame: parity `{}` ({} rows) / search `{}` ({} rows) / gefs twin `{}`".format(
        str(_g(summary, "frame", "parity_sha256", default=DASH))[:12],
        _fmt(_g(summary, "frame", "parity_rows")),
        str(_g(summary, "frame", "search_sha256", default=DASH))[:12],
        _fmt(_g(summary, "frame", "search_rows")),
        str(_g(summary, "frame", "gefs_twin_sha256", default=DASH))[:12],
    ))
    lines.append("")
    lines.append("## Seeds (realized c/contract, date-bootstrap 95% CI, trades, dates)")
    lines.append("")
    lines.append("| seed | parity (69d) | search (69d) | A validation | B validation | C validation |")
    lines.append("|---|---|---|---|---|---|")
    seeds = summary.get("seeds") or {}
    for name in seed_names(summary):
        s = seeds.get(name) or {}
        camps = s.get("campaigns") or {}
        cells = [
            _row_cell(s.get("parity_full")),
            _row_cell(s.get("search_full")),
            _row_cell(_g(camps, "A", "validation")),
            _row_cell(_g(camps, "B", "validation")),
            _row_cell(_g(camps, "C", "validation")),
        ]
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    ml = seeds.get("mlweather_fallback")
    lines.append("")
    if ml:
        lines.append(f"`mlweather_fallback` (what maia trades today): {ml.get('notes') or 'no note'}")
    else:
        lines.append(f"`mlweather_fallback` row: {DASH} (absent from this summary)")
    lines.append("")
    for frame_name, bss in _bss_entries(summary):
        lines.append(
            f"**Frame-level Brier skill vs market mid** ({frame_name} frame, all two-sided rows): BSS {_fmt(bss.get('bss'))} "
            f"CI [{_fmt(bss.get('ci_lo'))}, {_fmt(bss.get('ci_hi'))}] over {_fmt(bss.get('n_rows'))} rows / "
            f"{_fmt(bss.get('n_dates'))} dates (date-clustered)"
        )
    tp = summary.get("throughput")
    if tp:
        lines.append("")
        lines.append(
            f"**Throughput** {_fmt(tp.get('evals_per_s'), 1, signed=False)} evals/s on {_fmt(tp.get('workers'))} workers, "
            f"peak RSS {_fmt(tp.get('peak_rss_mb'), 0, signed=False)} MB, host `{tp.get('host', DASH)}`"
        )
    lines.append("")
    lines.append("Evolution, RC/SPA, Holm, controls: F2 -- not part of gen-0. No genome is proposed by this report.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# board.md
# ---------------------------------------------------------------------------
BOARD_COLUMNS = (
    "lane", "status", "family", "pick", "pooled OOS lo..hi", "dates", "trades", "p_RC",
    "Holm p", "vs no-filter", "vs fr31a", "N_phenotypes", "controls", "coverage units / next-data ETA",
)


def _coverage_lanes(coverage: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Tolerate ``{"lanes": {...}}``, ``{"lanes": [...]}`` or a bare ``{lane: {...}}``."""
    if not coverage:
        return {}
    lanes = coverage.get("lanes", coverage)
    if isinstance(lanes, list):
        return {str(x.get("lane") or x.get("name")): x for x in lanes if isinstance(x, dict)}
    if isinstance(lanes, dict):
        return {str(k): (v if isinstance(v, dict) else {"status": v}) for k, v in lanes.items()}
    return {}


def _lane_status(info: Dict[str, Any]) -> str:
    # coverage.py writes the lane readiness under "state"; accept both spellings
    # (the first Discord board rendered gas/mention as "—" because of this).
    st = str(info.get("status") or info.get("state") or DASH)
    n = info.get("n_units", info.get("independent_units", info.get("units")))
    if st.upper().startswith("NOT_PROMOTABLE") and n is not None and "(" not in st:
        return f"{st}({n})"
    return st


def _lane_units(info: Dict[str, Any]) -> str:
    n = info.get("n_units", info.get("independent_units", info.get("units")))
    unit = info.get("independent_unit", info.get("unit", ""))
    eta = info.get("next_data_eta", info.get("eta", info.get("next_data")))
    floor = info.get("search_floor", 40)
    left = f"{n} {unit}".strip() if n is not None else DASH
    if n is not None and floor:
        left += f" / floor {floor}"
    return f"{left}; ETA {eta if eta else DASH}"


def render_board(summary: Optional[Dict[str, Any]], coverage: Optional[Dict[str, Any]], paper: Optional[Dict[str, Any]] = None) -> str:
    summary = summary or {}
    seeds = summary.get("seeds") or {}
    lanes = _coverage_lanes(coverage)
    weather_lane = summary.get("lane") or "weather"
    if weather_lane not in lanes:
        lanes = dict(lanes)
        lanes.setdefault(weather_lane, {"status": "READY" if summary else DASH})
    order = [weather_lane] + [l for l in ("gas", "mention", "tweets", "crypto_annual") if l in lanes]
    order += sorted(l for l in lanes if l not in order)

    fr = _g(seeds, "fr31a_taker", "parity_full") or {}
    nf = _g(seeds, "nofilter_no", "parity_full") or {}
    def _with_verdict(row: Dict[str, Any]) -> str:
        # A KILLED seed's realized number is a diagnostic, never a headline:
        # carry the constraint verdict into the board cell (red team, 2026-09-02).
        reason = row.get("constraint_reason")
        return f"{_fmt(row.get('realized'))}" + (f" KILLED:{reason}" if reason else "")

    vs_nofilter = (
        f"{_with_verdict(fr)} vs {_with_verdict(nf)} (parity)"
        if fr.get("realized") is not None and nf.get("realized") is not None else "n/a (F2)"
    )
    pooled = summary.get("pooled_oos") or {}
    pooled_cell = (
        f"{_fmt(pooled.get('boot_lo'))}..{_fmt(pooled.get('boot_hi'))}" if pooled.get("boot_lo") is not None else "n/a (F2)"
    )
    n_ph = headline(summary)["n_phenotypes"] if summary else 0

    rows: List[List[str]] = []
    for lane in order:
        info = lanes.get(lane, {})
        if lane == weather_lane and summary:
            rows.append([
                lane, _lane_status(info) if (info.get("status") or info.get("state")) else "READY", str(summary.get("family", DASH)),
                f"gen0 seeds only ({summary.get('run_id', DASH)})", pooled_cell,
                _fmt(pooled.get("dates")) if pooled else "n/a (F2)",
                _fmt(pooled.get("trades")) if pooled else "n/a (F2)",
                "n/a (F2)", "n/a (F2)", vs_nofilter, "fr31a IS the seed", str(n_ph), "n/a (F2)", _lane_units(info),
            ])
        else:
            rows.append([lane, _lane_status(info), DASH, DASH, DASH, DASH, DASH, DASH, DASH, DASH, DASH, DASH, DASH, _lane_units(info)])
    p = paper or {}
    rows.append([
        "PAPER", p.get("status", "n/a (F3)"), p.get("family", DASH), p.get("genome_id", DASH),
        p.get("sandbox_c_per_contract", "n/a (F3)"), _fmt(p.get("settled_target_dates")) if p else "n/a (F3)",
        _fmt(p.get("settled_trades")) if p else "n/a (F3)", DASH, DASH, DASH,
        p.get("prediction_c_per_contract", "n/a (F3)"), DASH, DASH, p.get("note", "sandbox closed_trades vs factory prediction"),
    ])

    out = ["# Factory board", ""]
    out.append(f"run `{summary.get('run_id', DASH)}` ({summary.get('kind', DASH)}) -- registry {(_g(summary, 'registry_line', 'status') or 'UNREGISTERED')}")
    out.append("")
    out.append("| " + " | ".join(BOARD_COLUMNS) + " |")
    out.append("|" + "---|" * len(BOARD_COLUMNS))
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    out.append("")
    pc = parity_check(summary) if summary else {"matches_1e9": None}
    m = pc.get("matches_1e9")
    out.append(f"parity fr31a 181/65/+0.0636: {'yes' if m else ('no' if m is False else DASH)}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_gen0_report(summary: Dict[str, Any], out_dir: Union[str, Path], *, reports_root: Optional[Path] = None,
                      coverage: Optional[Dict[str, Any]] = None, paper: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    """Write summary.json / summary.md / board.md / status.json into ``out_dir`` and ``latest.json`` beside it.

    ``reports_root`` defaults to ``out_dir.parent`` (i.e. ``reports/factory``); ``coverage``
    defaults to ``<reports_root>/coverage.json`` when present.
    """
    # Resolve both so a relative ``--out`` still yields reports-root-relative pointers in latest.json.
    out_dir = Path(out_dir).resolve()
    reports_root = Path(reports_root).resolve() if reports_root else out_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    if coverage is None:
        coverage = _load_json(reports_root / "coverage.json")

    paths = {
        "summary_json": out_dir / "summary.json",
        "summary_md": out_dir / "summary.md",
        "board_md": out_dir / "board.md",
        "status_json": out_dir / "status.json",
        "latest_json": reports_root / "latest.json",
    }
    write_json(paths["summary_json"], summary)
    write_text(paths["summary_md"], render_summary_md(summary))
    write_text(paths["board_md"], render_board(summary, coverage, paper))
    write_json(paths["status_json"], render_status_json(summary))

    def _rel(p: Path) -> str:
        try:
            return p.relative_to(reports_root).as_posix()
        except ValueError:
            return p.as_posix()

    write_json(paths["latest_json"], {
        "run_id": summary.get("run_id"),
        "kind": summary.get("kind"),
        "family": summary.get("family"),
        "summary": _rel(paths["summary_json"]),
        "summary_md": _rel(paths["summary_md"]),
        "board": _rel(paths["board_md"]),
        "status": _rel(paths["status_json"]),
        "headline": headline(summary),
    })
    return paths
