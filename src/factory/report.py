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
import time
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
    # On Windows a concurrent reader (dashboard poller, monitor) holding the
    # target open makes os.replace fail with a share violation; retry briefly
    # (red team F2 D1). Linux renames are unaffected.
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


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


# ===========================================================================
# F2 family report (STATS workstream): build_family_summary, verdict, renderers
# ===========================================================================
# Every number below is recomputed from ledger + frames + picks.json + the
# controls summary (FACTORY_ROADMAP section F2 exit bullets 5-6); nothing is
# copied from an in-memory procedure result, so ``factory.py report`` on a
# finished run dir is byte-identical to the report written at the end of the
# run. Formulas live in ``src/factory/multiplicity.py`` (RC/SPA/Holm/DSR/KS)
# and ``src/factory/controls.py`` (pooled statistics, validation scoring).
FAMILY_CAMPAIGNS = ("A", "B", "C", "ALL69")
POOLED_CAMPAIGNS = ("A", "B", "C")
ADVERSE_SENSITIVITY = (0.02, 0.03)
BOARD_MAX_CHARS = 1900
#: The verdict conditions (FACTORY_ARCHITECTURE section 6.3 promotion + section 5 step 8 gates)
VERDICT_CONDITIONS = (
    "headline_picks_present",
    "pooled_boot_lo_gt0",
    "holm_p_lt_alpha",
    "p_rc_all69_lt_threshold",
    "beats_every_control",
    "paired_vs_nofilter_lo_gt0",
    "sign_survives_2c",
    "sign_survives_3c",
    "sign_survives_embargo2",
    "bss_trades_ge0",
    "point_estimate_ge_4c",
    "cities_ge3",
)


def _sign(x: Any) -> Optional[str]:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return "+" if x > 0 else ("-" if x < 0 else "0")


def _fit_row(res: Any) -> Dict[str, Any]:
    """FitnessResult -> JSON row without the array fields (like gen0._row)."""
    d = res.as_dict()
    for k in ("per_date_pnl", "per_date_codes", "trade_rows"):
        d.pop(k, None)
    d["passed"] = bool(res.passed)
    return d


def _stats_only(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: p.get(k) for k in ("n_dates", "mean", "se", "t_stat", "boot_lo", "boot_hi", "trades")}


def _adverse_frame(F: Any, adverse: float) -> Any:
    """Copy of ``F`` with ``price_paid = quote + adverse`` (NaN past 0.99 -> not executable) and realized recomputed.

    ``fee_per_contract`` is held at the frame's 1c-fill value (the fee moves by
    < 0.1c across a 2c price shift; the sensitivity is about the fill, not the
    fee schedule).
    """
    import numpy as np

    from src.factory import features as feat
    from src.factory.columns import Frame

    v = dict(F.visible)
    price = feat.price_paid(F.visible["quote"], float(adverse))
    ex = F.visible["executable"] & np.isfinite(price)
    v["price_paid"] = price.astype(np.float64)
    v["executable"] = ex.astype(bool)
    h = dict(F.hidden)
    h["realized_per_contract"] = np.where(ex, F.hidden["won"].astype(np.float64) - price - F.visible["fee_per_contract"], np.nan).astype(np.float64)
    out = Frame(name=F.name, visible=v, hidden=h, dates=F.dates, markets=F.markets, block_starts=F.block_starts,
                provenance=dict(F.provenance), twin_index=F.twin_index)
    out.validate()
    return out


def _tail_ratio(fs: Any, dates: Sequence[str]) -> Dict[str, Any]:
    """Observed vs Gaussian-expected share of |z| >= 2.5 (z = (high - mu_last)/sigma_last) over the city-days of ``dates``.

    Informational (R3 #4 asks for the walk-forward tail model's ratio); on ~130
    validation city-days the expected count is ~1.6, so it is reported, never gated.
    """
    import numpy as np

    from src.factory import folds as _folds
    from src.factory import multiplicity as MP
    from src.factory import null as NULL

    F = fs.search
    dm = _folds.date_mask(F, dates)
    inv, ukeys, mu_last = NULL._city_day_table(F)
    v = F.visible
    nd = F.n_dates
    high = NULL.city_day_high(F)
    ts = v["ts_utc"]
    z_vals = []
    for k in range(ukeys.shape[0]):
        rows = np.flatnonzero(inv == k)
        if not dm[rows[0]]:
            continue
        r = rows[np.argmax(ts[rows])]
        h = high[r]
        sg = v["sigma_f"][r]
        if np.isfinite(h) and np.isfinite(sg) and sg > 0 and np.isfinite(mu_last[k]):
            z_vals.append(float((h - mu_last[k]) / sg))
    z = np.asarray(z_vals, dtype=np.float64)
    n = int(z.shape[0])
    expected = 2.0 * (1.0 - MP.norm_cdf(2.5))
    obs = int(np.count_nonzero(np.abs(z) >= 2.5)) if n else 0
    share = obs / n if n else math.nan
    return {"n_city_days": n, "n_abs_z_ge_2p5": obs, "observed_share": share, "expected_share": expected,
            "ratio": (share / expected) if n else math.nan, "expected_count": expected * n,
            "gate_applicable": False, "note": "informational: R3 #4 tail ratio on the last-vintage Gaussian; not part of the verdict"}


def _registry_entries(path: Optional[Path]) -> List[Dict[str, Any]]:
    if not path or not Path(path).exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                ln = json.loads(raw)
                ln.pop("ts", None)
                out.append(ln)
    return out


def build_family_summary(run_dir: Union[str, Path], fs: Any, config: Dict[str, Any], registry_line: Optional[Dict[str, Any]], *,
                         n_boot: int = 4000, seed: Optional[int] = None, coverage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The family ``summary.json`` with every field of FACTORY_ROADMAP section F2 exit bullet 5 (module block comment)."""
    import numpy as np

    from src.factory import controls as CT
    from src.factory import fitness
    from src.factory import folds as _folds
    from src.factory import gen0
    from src.factory import genome as G
    from src.factory import multiplicity as MP
    from src.factory.ledger import Ledger

    run_dir = Path(run_dir)
    seed = fitness.DEFAULT_SEED if seed is None else int(seed)
    run_json = CT.load_json(run_dir / "run.json") or {}
    status = CT.load_json(run_dir / "status.json") or {}
    picks = CT.load_picks(run_dir)
    pooled_disk = CT.load_json(run_dir / "oos" / "pooled.json") or {}
    controls = CT.load_json(run_dir / "controls" / "summary.json")
    family = str(config.get("family") or (registry_line or {}).get("family") or "")
    thresholds = dict(config.get("thresholds") or {})
    alpha = float(thresholds.get("holm_alpha", 0.05))
    F = fs.search
    camps = _folds.campaigns([str(d) for d in F.dates])
    frames_dir = run_json.get("frames_dir") or config.get("frames_dir")

    # -- per-campaign picks: decoded genes, in-sample (re-scored on the search window), validation ---
    pick_rows: Dict[str, Any] = {}
    genomes: Dict[str, G.Genome] = {}
    per_date: List[Dict[str, Any]] = []
    val_results: Dict[str, Any] = {}
    ledgers: Dict[str, Any] = {}
    n_phen: Dict[str, int] = {}
    mult: Dict[str, Any] = {}
    sr_trials: List[float] = []
    all_hashes = set()
    picks_missing: Dict[str, Any] = {}
    for c in FAMILY_CAMPAIGNS:
        p = picks.get(c)
        camp = camps.get(c)
        if camp is None or not CT.pick_present(p):
            picks_missing[c] = (p or {}).get("reason") or "MISSING"
            pick_rows[c] = {"missing": True, "reason": picks_missing[c], "n_candidates": (p or {}).get("n_candidates")}
            continue
        g = CT.pick_genome(p)
        genomes[c] = g
        mask = G.to_mask(g, F)
        dm_s = _folds.date_mask(F, camp.search_dates)
        r_is = fitness.score(F, mask, date_mask=dm_s, constraints=True, twin=fs.gefs_twin, genome=g, n_boot=n_boot, seed=seed, label=f"{c}/in_sample")
        is_disk = p.get("in_sample") or {}
        row: Dict[str, Any] = {
            "genome_json": p.get("genome_json"),
            "genes": g.to_json().get("genes"),
            "describe": g.describe(),
            "genome_id": p.get("genome_id"),
            "phenotype_hash": p.get("phenotype_hash"),
            "picked_gen": p.get("picked_gen"),
            "n_candidates": p.get("n_candidates"),
            "n_active_clauses": G.n_active_clauses(g),
            "in_sample": _fit_row(r_is),
            "in_sample_boot_lo_picks_json": is_disk.get("boot_lo"),
            "in_sample_matches_picks_json": (
                None if is_disk.get("boot_lo") is None or r_is.boot_lo != r_is.boot_lo
                else bool(abs(float(is_disk["boot_lo"]) - float(r_is.boot_lo)) < 1e-12)
            ),
            "validation": None,
            "phenotype_hash_search_full": G.phenotype_hash(g, F),
        }
        if camp.validation_dates:
            r_v = CT.score_validation(fs, g, camp, n_boot=n_boot, seed=seed)
            val_results[c] = r_v
            row["validation"] = _fit_row(r_v)
            row["validation"]["bss_trades"] = r_v.bss_trades
            if r_v.trades:
                per_date.extend(CT.per_date_rows(F, r_v, c))
        pick_rows[c] = row
        # ledger: distinct phenotypes, RC/SPA, SR distribution
        led = Ledger(run_dir, c)
        table = led.read_all()
        ledgers[c] = table
        hashes = {h for h in table.column("phenotype_hash").to_pylist() if h}
        n_phen[c] = len(hashes)
        if c in POOLED_CAMPAIGNS:
            all_hashes |= hashes
            min_d = int(math.ceil(fitness.MIN_DATE_FRACTION * len(camp.search_dates)))
            sr_trials.extend(float(x) for x in MP.sharpe_from_ledger(table.column("t_stat").to_pylist(), table.column("dates").to_pylist(), min_dates=min_d))
        M, ids = MP.ledger_matrix(table, camp.search_dates, code_dates=CT.ledger_code_dates(F))
        ph = str(p.get("phenotype_hash") or "")
        if ph in ids:
            rc = MP.reality_check(M, ids.index(ph), n_boot=n_boot, seed=seed)
        else:
            rc = {"p_rc": math.nan, "p_spa": math.nan, "L": int(M.shape[0]), "D": int(M.shape[1]), "t_pick": math.nan}
        rc["pick_in_ledger"] = ph in ids
        rc["n_phenotypes"] = n_phen[c]
        rc["n_ledger_rows"] = int(table.num_rows)
        mult[c] = rc

    # -- pooled OOS ------------------------------------------------------------
    pooled = CT.pooled_stats([r["pnl"] for r in per_date], n_boot=n_boot, seed=seed)
    pooled["trades"] = int(sum(r["trades"] for r in per_date))
    pooled["per_date"] = per_date
    pooled["one_sided_p"] = MP.one_sided_p([r["pnl"] for r in per_date], n_boot=n_boot, seed=seed)
    pooled["mean_procedure"] = pooled_disk.get("mean")
    pooled["matches_procedure"] = (
        None if pooled_disk.get("mean") is None or pooled["mean"] != pooled["mean"]
        else bool(abs(float(pooled_disk["mean"]) - float(pooled["mean"])) < 1e-12)
    )
    pooled_dates = sorted({r["date"] for r in per_date})
    pooled_cities = set()
    for c, r_v in val_results.items():
        if r_v is not None and r_v.trades:
            pooled_cities |= set(int(x) for x in F.visible["city_code"][np.asarray(r_v.trade_rows, dtype=np.int64)].tolist())
    pooled["cities"] = len(pooled_cities)
    pooled["picks_missing"] = {c: r for c, r in picks_missing.items() if c in POOLED_CAMPAIGNS}
    pooled["n_calendar_dates"] = int(sum(len(camps[c].validation_dates) for c in POOLED_CAMPAIGNS if c in camps))

    # -- Holm across registry entries -------------------------------------------
    reg_path = config.get("registry_path")
    entries = _registry_entries(Path(reg_path)) if reg_path else []
    holm_inputs: Dict[str, float] = {}
    no_p: List[str] = []
    for ln in entries:
        fam = str(ln.get("family"))
        if ln.get("event") == "family" and fam not in holm_inputs and fam != family:
            holm_inputs[fam] = math.nan
        if ln.get("event") == "transition" and fam != family:
            pv = (ln.get("evidence") or {}).get("pooled_one_sided_p")
            if pv is not None:
                holm_inputs[fam] = float(pv)
    no_p = [f for f, v in holm_inputs.items() if v != v]
    holm_inputs = {f: v for f, v in holm_inputs.items() if v == v}
    holm_inputs[family] = pooled["one_sided_p"]
    holm_adj = MP.holm(holm_inputs, alpha=alpha)
    holm_block = {"alpha": alpha, "inputs": holm_inputs, "adjusted": holm_adj, "families_without_p": no_p,
                  "this_family": holm_adj.get(family), "m": len(holm_inputs)}

    # -- clustered DSR on the pooled validation date series -------------------------
    dsr = MP.deflated_sharpe(np.asarray([r["pnl"] for r in per_date], dtype=np.float64), max(len(all_hashes), 1),
                             sr_trials=np.asarray(sr_trials, dtype=np.float64))
    dsr["n_trials_definition"] = "distinct phenotype hashes across the A, B, C ledgers"
    dsr["clustering"] = "target_date (the series is the per-date cluster mean)"
    dsr["n_sr_trials"] = len(sr_trials)
    robust = MP.deflated_sharpe(np.asarray([r["pnl"] for r in per_date], dtype=np.float64), max(len(all_hashes), 1),
                                sr_var=MP.robust_variance(sr_trials))
    dsr["robust"] = {k: robust.get(k) for k in ("dsr", "expected_max_sr", "sr_var_trials")}
    dsr["robust"]["sr_var_source"] = "ledger_sr_distribution_mad"

    # -- paired vs no-filter -------------------------------------------------------
    base = G.SEEDS["nofilter_no"]
    paired_per: Dict[str, Any] = {}
    diffs: List[float] = []
    for c in POOLED_CAMPAIGNS:
        r_v = val_results.get(c)
        camp = camps.get(c)
        if r_v is None or camp is None or not r_v.trades:
            paired_per[c] = None
            continue
        r_b = CT.score_validation(fs, base, camp, n_boot=n_boot, seed=seed)
        b_map = {int(k): float(v) for k, v in zip(r_b.per_date_codes, r_b.per_date_pnl)} if r_b is not None and r_b.trades else {}
        d = [float(v) - b_map.get(int(k), 0.0) for k, v in zip(r_v.per_date_codes, r_v.per_date_pnl)]
        diffs.extend(d)
        paired_per[c] = dict(CT.pooled_stats(d, n_boot=n_boot, seed=seed), baseline_trades=int(r_b.trades) if r_b else 0,
                             baseline_realized=(r_b.realized if r_b and r_b.trades else None))
    paired = {"baseline": "nofilter_no", "rule": "per-date (pick_k - baseline_k) on the dates the pick traded; baseline 0 where it did not trade",
              "pooled": CT.pooled_stats(diffs, n_boot=n_boot, seed=seed), "per_campaign": paired_per}

    # -- sensitivity: 2c / 3c adverse fill, embargo 2 --------------------------------------
    sens: Dict[str, Any] = {}
    for adv in ADVERSE_SENSITIVITY:
        Fa = _adverse_frame(F, adv)
        vals: List[float] = []
        per_c: Dict[str, Any] = {}
        for c in POOLED_CAMPAIGNS:
            g = genomes.get(c)
            camp = camps.get(c)
            if g is None or camp is None or not camp.validation_dates:
                continue
            r = fitness.score(Fa, G.to_mask(g, Fa), date_mask=_folds.date_mask(Fa, camp.validation_dates), constraints=False, n_boot=n_boot, seed=seed)
            per_c[c] = {"trades": int(r.trades), "dates": int(r.dates), "realized": r.realized if r.trades else None}
            if r.trades:
                vals.extend(float(x) for x in r.per_date_pnl)
        st = CT.pooled_stats(vals, n_boot=n_boot, seed=seed)
        sens[f"adverse_{adv:.2f}"] = dict(_stats_only(dict(st, trades=sum(x["trades"] for x in per_c.values()))), sign=_sign(st["mean"]),
                                          per_campaign=per_c, price_rule=f"price_paid = quote + {adv:.2f}; fee held at the frame's value")
    sens["embargo_2"] = dict(_stats_only(pooled), sign=_sign(pooled["mean"]),
                             note="embargo_days = 2 IS the campaign calendar default (section 6.1: two embargo dates before every validation block); the headline pooled OOS already satisfies it")

    # -- BSS_trades per pick on validation ------------------------------------------
    bss = {c: (val_results[c].bss_trades if val_results.get(c) is not None else None) for c in POOLED_CAMPAIGNS}
    rows_all = np.concatenate([np.asarray(val_results[c].trade_rows, dtype=np.int64) for c in POOLED_CAMPAIGNS if val_results.get(c) is not None and val_results[c].trades]) if val_results else np.zeros(0, dtype=np.int64)
    bss["pooled"] = fitness.bss_on_rows(F, rows_all) if rows_all.size else math.nan
    bss["pooled_n_trades"] = int(rows_all.shape[0])

    # -- phenotype Jaccard between the picks (market sets on the full search frame) ---------------
    sets: Dict[str, set] = {}
    for c, g in genomes.items():
        m = G.to_mask(g, F) & F.visible["executable"]
        rows = G.first_true_per_block(m, F.block_starts)
        sets[c] = set(F.markets[F.visible["market_code"][rows]].tolist())
    jac: Dict[str, Any] = {"markets_per_pick": {c: len(s) for c, s in sets.items()}, "pairs": {}}
    names = [c for c in FAMILY_CAMPAIGNS if c in sets]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            u = sets[a] | sets[b]
            jac["pairs"][f"{a}/{b}"] = (len(sets[a] & sets[b]) / len(u)) if u else math.nan

    # -- blocked 5-fold diagnostic ----------------------------------------------------
    folds_pooled = CT.load_json(run_dir / "oos" / "folds_pooled.json")
    fold_picks = {k: {"genome_id": v.get("genome_id"), "phenotype_hash": v.get("phenotype_hash"),
                      "in_sample_boot_lo": (v.get("in_sample") or {}).get("boot_lo"),
                      "validation": {kk: (v.get("validation") or {}).get(kk) for kk in ("trades", "dates", "realized", "boot_lo", "boot_hi")}}
                  for k, v in picks.items() if k.startswith("F") and isinstance(v, dict)}
    blocked = {"label": "in-sample blocks postdate the held block", "available": folds_pooled is not None,
               "pooled": _stats_only(folds_pooled) if folds_pooled else None, "picks": fold_picks,
               "purge_3_sensitivity": (folds_pooled or {}).get("purge_3") if folds_pooled else None}

    # -- finalists (ALL69 ledger, picker order) ----------------------------------------
    finalists: List[Dict[str, Any]] = []
    if "ALL69" in ledgers:
        rows = [r for r in ledgers["ALL69"].to_pylist() if r.get("status") == "SCORED" and r.get("boot_lo") == r.get("boot_lo")]
        seen = set()
        ordered = sorted(rows, key=lambda r: (-float(r["boot_lo"]), int(json.loads(r["genome_json"]).get("n_active_clauses", 99)), str(r["genome_id"])))
        for r in ordered:
            if r["phenotype_hash"] in seen:
                continue
            seen.add(r["phenotype_hash"])
            finalists.append({"rank": len(finalists) + 1, "genome_id": r["genome_id"], "genome_json": r["genome_json"],
                              "phenotype_hash": r["phenotype_hash"], "boot_lo": r["boot_lo"], "realized": r["realized"],
                              "trades": r["trades"], "dates": r["dates"], "gen": r["gen"],
                              "n_active_clauses": json.loads(r["genome_json"]).get("n_active_clauses")})
            if len(finalists) == 3:
                break
    if pick_rows.get("ALL69") and not pick_rows["ALL69"].get("missing") and finalists and finalists[0]["genome_id"] != pick_rows["ALL69"]["genome_id"]:
        finalists.insert(0, {"rank": 0, "genome_id": pick_rows["ALL69"]["genome_id"], "genome_json": pick_rows["ALL69"]["genome_json"],
                             "phenotype_hash": pick_rows["ALL69"]["phenotype_hash"], "note": "the ALL69 pick (picks.json) differs from the ledger order"})

    summary: Dict[str, Any] = {
        "run_id": run_json.get("run_id") or run_dir.name,
        "kind": "family",
        "phase": "F2",
        "family": family,
        "lane": config.get("lane", "weather"),
        "source": config.get("source"),
        "mode": config.get("mode"),
        "gene_spec_version": G.GENE_SPEC_VERSION,
        "git_rev": run_json.get("git_rev"),
        "lock_sha256": run_json.get("lock_sha256"),
        "config_sha256": (registry_line or {}).get("config_sha256") or run_json.get("config_sha256") or config.get("_config_sha256"),
        "master_seed": run_json.get("master_seed") or (config.get("budget") or {}).get("master_seed"),
        "budget": run_json.get("budget") or config.get("budget"),
        "registry_line": {k: v for k, v in (registry_line or {}).items() if k != "ts"},
        "thresholds": thresholds,
        "frame": gen0._frames_summary(fs, Path(frames_dir) if frames_dir else None, config),
        "campaigns": gen0._campaign_block(camps),
        "picks": pick_rows,
        "picks_missing": picks_missing,
        "pooled_oos": pooled,
        "multiplicity": mult,
        "holm": holm_block,
        "clustered_dsr": dsr,
        "n_phenotypes": dict(n_phen, total=int(sum(n_phen.values())), distinct_abc=len(all_hashes)),
        # From the ledger alone (the report must be recomputable from ledger +
        # frame); status.json's counter is a cross-check, not a source.
        "evaluations": int(sum(t.num_rows for t in ledgers.values())),
        "evaluations_status_json": status.get("evaluations"),
        "paired_vs_nofilter": paired,
        "sensitivity": sens,
        "bss_trades": bss,
        "phenotype_jaccard": jac,
        "tail_ratio": _tail_ratio(fs, pooled_dates) if pooled_dates else None,
        "controls": controls,
        "blocked_folds": blocked,
        "finalists": finalists,
        "status": {k: status.get(k) for k in ("state", "phase", "evaluations", "n_phenotypes", "picks_done", "controls_done")},
        "bootstrap": {"n_boot": int(n_boot), "seed": seed},
    }
    summary["verdict"] = evaluate_verdict(summary)
    return summary


def evaluate_verdict(summary: Dict[str, Any]) -> Dict[str, Any]:
    """PROPOSED iff every section 6.3 promotion condition and section 5.8 gate holds; else CLOSED with the failures listed."""
    th = summary.get("thresholds") or {}
    pooled = summary.get("pooled_oos") or {}
    holm_this = _g(summary, "holm", "this_family") or {}
    p_rc_all = _g(summary, "multiplicity", "ALL69", "p_rc")
    controls = summary.get("controls") or {}
    ctrl_means = [m for k in ("snapshot", "residual") for m in ((controls.get(k) or {}).get("pooled_means") or []) if m is not None and m == m]
    controls_complete = bool(controls) and all(
        (controls.get(k) or {}).get("n_done") == (controls.get(k) or {}).get("n") for k in ("snapshot", "residual", "planted") if k in controls
    )
    mean = pooled.get("mean")

    def _gt(x: Any, y: float) -> Optional[bool]:
        return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else bool(x > y)

    def _ge(x: Any, y: float) -> Optional[bool]:
        return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else bool(x >= y)

    def _lt(x: Any, y: float) -> Optional[bool]:
        return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else bool(x < y)

    conditions: Dict[str, Optional[bool]] = {
        "headline_picks_present": not (summary.get("picks_missing") or {}),
        "pooled_boot_lo_gt0": _gt(pooled.get("boot_lo"), float(th.get("pooled_boot_lo_gt", 0.0))),
        "holm_p_lt_alpha": _lt(holm_this.get("p_adj"), float(th.get("holm_alpha", 0.05))),
        "p_rc_all69_lt_threshold": _lt(p_rc_all, float(th.get("p_rc_all69_lt", 0.10))),
        "beats_every_control": (bool(mean is not None and mean == mean and ctrl_means and mean > max(ctrl_means)) if controls_complete and ctrl_means else None),
        "paired_vs_nofilter_lo_gt0": _gt(_g(summary, "paired_vs_nofilter", "pooled", "boot_lo"), 0.0),
        "sign_survives_2c": (_g(summary, "sensitivity", "adverse_0.02", "sign") == "+") if _g(summary, "sensitivity", "adverse_0.02", "sign") else None,
        "sign_survives_3c": (_g(summary, "sensitivity", "adverse_0.03", "sign") == "+") if _g(summary, "sensitivity", "adverse_0.03", "sign") else None,
        "sign_survives_embargo2": (_g(summary, "sensitivity", "embargo_2", "sign") == "+") if _g(summary, "sensitivity", "embargo_2", "sign") else None,
        "bss_trades_ge0": _ge(_g(summary, "bss_trades", "pooled"), 0.0),
        "point_estimate_ge_4c": _ge(mean, 0.04),
        "cities_ge3": _ge(pooled.get("cities"), 3),
    }
    failing = [k for k in VERDICT_CONDITIONS if conditions.get(k) is not True]
    status = "PROPOSED" if not failing else "CLOSED"
    planted = (controls.get("planted") or {}) if controls else {}
    return {
        "status": status,
        "conditions": conditions,
        "failing": failing,
        "controls_complete": controls_complete,
        "planted_pass": planted.get("pass"),
        "snapshot_pass": bool((controls.get("snapshot") or {}).get("pass_boot_lo") and (controls.get("snapshot") or {}).get("pass_ks")) if controls else None,
        "residual_real_rank": (controls.get("residual") or {}).get("real_rank") if controls else None,
        "rule": "PROPOSED iff every headline campaign (A/B/C/ALL69) has a pick, pooled boot_lo > 0, Holm p < alpha, p_RC(ALL69) < threshold, beats every control pooled validation, and every section 5.8 gate; otherwise CLOSED (section 6.3)",
    }


def verdict(summary: Dict[str, Any]) -> str:
    """``"PROPOSED"`` or ``"CLOSED"`` for a family summary (recomputed, never read from the summary)."""
    return str(evaluate_verdict(summary)["status"])


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------
def _p(v: Any, nd: int = 3) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return DASH
    return f"{v:.{nd}f}"


def render_family_md(summary: Dict[str, Any]) -> str:
    v = summary.get("verdict") or {}
    pooled = summary.get("pooled_oos") or {}
    lines: List[str] = []
    lines.append(f"# Factory family report -- `{summary.get('run_id', DASH)}`")
    lines.append("")
    lines.append(f"## VERDICT: **{v.get('status', DASH)}**")
    if v.get("failing"):
        lines.append(f"failing conditions: {', '.join(v['failing'])}")
    lines.append("")
    reg = summary.get("registry_line") or {}
    lines.append(f"**Family** `{summary.get('family', DASH)}` -- registry status **{reg.get('status', 'UNREGISTERED')}** "
                 f"(config sha `{str(summary.get('config_sha256', DASH))[:12]}`, git `{str(summary.get('git_rev', DASH))[:12]}`, "
                 f"master seed {summary.get('master_seed', DASH)}, evaluations {summary.get('evaluations', DASH)})")
    lines.append("")
    lines.append("## Picks (pre-registered picker: max search-window boot_lo among constraint-satisfying elites; ties -> fewer clauses)")
    lines.append("")
    lines.append("| campaign | genome | phenotype | gen | in-sample realized [boot CI] n/d | validation realized [boot CI] n/d | BSS_trades (val) | N phenotypes | p_RC | p_SPA |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for c in FAMILY_CAMPAIGNS:
        p = (summary.get("picks") or {}).get(c)
        if not p:
            lines.append(f"| {c} | {DASH} | | | | | | | | |")
            continue
        m = (summary.get("multiplicity") or {}).get(c) or {}
        val = p.get("validation")
        lines.append(f"| {c} | `{p.get('genome_id')}` {p.get('describe', '')} | `{str(p.get('phenotype_hash', ''))[:12]}` | {p.get('picked_gen', DASH)} | "
                     f"{_row_cell(p.get('in_sample'))} | {_row_cell(val) if val else 'none (deployment genome)'} | "
                     f"{_p((val or {}).get('bss_trades'))} | {(summary.get('n_phenotypes') or {}).get(c, DASH)} | {_p(m.get('p_rc'))} | {_p(m.get('p_spa'))} |")
    lines.append("")
    lines.append(f"## Pooled OOS ({pooled.get('n_dates', DASH)} validation dates, {pooled.get('trades', DASH)} trades, {pooled.get('cities', DASH)} cities)")
    lines.append("")
    lines.append(f"mean {_fmt(pooled.get('mean'))} se {_fmt(pooled.get('se'))} t {_fmt(pooled.get('t_stat'), 2)} boot 95% [{_fmt(pooled.get('boot_lo'))}, {_fmt(pooled.get('boot_hi'))}] "
                 f"one-sided p {_p(pooled.get('one_sided_p'), 4)} (matches procedure: {pooled.get('matches_procedure')})")
    h = summary.get("holm") or {}
    ht = h.get("this_family") or {}
    lines.append(f"Holm across {h.get('m', DASH)} registry entr{'y' if h.get('m') == 1 else 'ies'} (alpha {h.get('alpha')}): p_adj {_p(ht.get('p_adj'), 4)} reject {ht.get('reject')}"
                 + (f"; families without a recorded p: {', '.join(h.get('families_without_p'))}" if h.get("families_without_p") else ""))
    d = summary.get("clustered_dsr") or {}
    lines.append(f"Clustered DSR: SR {_p(d.get('sr'))} DSR {_p(d.get('dsr'))} PSR(0) {_p(d.get('psr'))} E[max SR] {_p(d.get('expected_max_sr'))} "
                 f"(N_trials {d.get('n_trials')} phenotypes, skew {_p(d.get('skew'), 2)} kurt {_p(d.get('kurt'), 2)}, V[SR] {_p(d.get('sr_var_trials'), 4)} from {d.get('sr_var_source')}); "
                 f"MAD-robust V[SR] {_p(_g(d, 'robust', 'sr_var_trials'), 4)} -> E[max SR] {_p(_g(d, 'robust', 'expected_max_sr'))} DSR {_p(_g(d, 'robust', 'dsr'))}")
    lines.append("")
    lines.append("## Gates (section 5.8)")
    lines.append("")
    pv = _g(summary, "paired_vs_nofilter", "pooled") or {}
    lines.append(f"- paired vs `nofilter_no`: mean {_fmt(pv.get('mean'))} se {_fmt(pv.get('se'))} t {_fmt(pv.get('t_stat'), 2)} boot lo {_fmt(pv.get('boot_lo'))} (n {pv.get('n_dates', DASH)})")
    s = summary.get("sensitivity") or {}
    lines.append(f"- sign at +2c: {(s.get('adverse_0.02') or {}).get('sign', DASH)} ({_fmt((s.get('adverse_0.02') or {}).get('mean'))}); "
                 f"+3c: {(s.get('adverse_0.03') or {}).get('sign', DASH)} ({_fmt((s.get('adverse_0.03') or {}).get('mean'))}); "
                 f"embargo 2: {(s.get('embargo_2') or {}).get('sign', DASH)} -- {(s.get('embargo_2') or {}).get('note', '')}")
    b = summary.get("bss_trades") or {}
    lines.append(f"- BSS_trades on pooled validation trades: {_p(b.get('pooled'))} (A {_p(b.get('A'))} / B {_p(b.get('B'))} / C {_p(b.get('C'))})")
    tr = summary.get("tail_ratio") or {}
    lines.append(f"- tail ratio |z|>=2.5 (informational): {_p(tr.get('ratio'), 2)} ({tr.get('n_abs_z_ge_2p5', DASH)} of {tr.get('n_city_days', DASH)} city-days; expected {_p(tr.get('expected_count'), 1)})")
    j = summary.get("phenotype_jaccard") or {}
    lines.append(f"- phenotype Jaccard: " + ", ".join(f"{k} {_p(x, 2)}" for k, x in (j.get("pairs") or {}).items()))
    lines.append("")
    lines.append("## Controls")
    lines.append("")
    ctl = summary.get("controls")
    if not ctl:
        lines.append("controls/summary.json absent -- run `factory.py controls <run_id>`")
    else:
        sn = ctl.get("snapshot") or {}
        rs = ctl.get("residual") or {}
        pl = ctl.get("planted") or {}
        lines.append(f"- snapshot-efficient null: {sn.get('n_done', 0)}/{sn.get('n', DASH)} replicates, pooled boot_lo > 0 in {sn.get('n_boot_lo_gt0', DASH)} "
                     f"(<= 1 required: {sn.get('pass_boot_lo')}); KS of the picks' p_RC vs U(0,1): D {_p((sn.get('ks_p_rc') or {}).get('stat'))} p {_p((sn.get('ks_p_rc') or {}).get('p'))} "
                     f"(> 0.05 required: {sn.get('pass_ks')}); real pooled mean rank {sn.get('real_rank', DASH)} of {sn.get('n_done', DASH)}")
        lines.append(f"- residual-shuffle null: {rs.get('n_done', 0)}/{rs.get('n', DASH)} replicates, p95 of pooled means {_fmt(rs.get('p95'))}; "
                     f"real pick {_fmt(ctl.get('real_pooled_mean'))} rank {rs.get('real_rank', DASH)} of {rs.get('n_done', DASH)} (exceeds p95: {rs.get('real_exceeds_p95')})")
        lines.append(f"- planted edge (+{pl.get('edge', DASH)}): recovered pick pooled on planted {_fmt((pl.get('pick_pooled_on_planted') or {}).get('mean'))} "
                     f"vs original {_fmt((pl.get('pick_pooled_on_original') or {}).get('mean'))}; captured {_fmt(pl.get('captured'))} "
                     f"ratio {_p(pl.get('capture_ratio'), 2)} (>= 0.8: {pl.get('pass')}); the rule's own validation delta {_fmt(pl.get('rule_pooled_validation_delta'))}")
    lines.append("")
    bf = summary.get("blocked_folds") or {}
    lines.append(f"## Blocked 5-fold diagnostic ({bf.get('label')})")
    lines.append("")
    if bf.get("available"):
        bp = bf.get("pooled") or {}
        lines.append(f"pooled {_fmt(bp.get('mean'))} [{_fmt(bp.get('boot_lo'))}, {_fmt(bp.get('boot_hi'))}] n {bp.get('n_dates', DASH)}; folds: " + ", ".join(bf.get("picks") or []))
    else:
        lines.append("not run (never headline)")
    lines.append("")
    lines.append("## Verdict conditions")
    lines.append("")
    for k in VERDICT_CONDITIONS:
        val = (v.get("conditions") or {}).get(k)
        lines.append(f"- {k}: {'PASS' if val is True else ('FAIL' if val is False else 'n/a')}")
    lines.append("")
    lines.append(f"**{v.get('status', DASH)}** -- {v.get('rule', '')}")
    return "\n".join(lines) + "\n"


def render_family_board(summary: Dict[str, Any], coverage: Optional[Dict[str, Any]] = None) -> str:
    """<= 1,900 chars, timestamp-free; the verdict and the residual-null rank first."""
    v = summary.get("verdict") or {}
    pooled = summary.get("pooled_oos") or {}
    ctl = summary.get("controls") or {}
    sn = ctl.get("snapshot") or {}
    rs = ctl.get("residual") or {}
    pl = ctl.get("planted") or {}
    m = summary.get("multiplicity") or {}
    h = (summary.get("holm") or {}).get("this_family") or {}
    nph = summary.get("n_phenotypes") or {}
    status_word = str(v.get("status", DASH))
    out: List[str] = ["# Factory board", ""]
    out.append(f"## VERDICT: {status_word} -- `{summary.get('family', DASH)}` run `{summary.get('run_id', DASH)}`")
    if v.get("failing"):
        out.append(f"failing: {', '.join(v['failing'])}")
    out.append(f"pooled OOS {_fmt(pooled.get('mean'))} [{_fmt(pooled.get('boot_lo'))}, {_fmt(pooled.get('boot_hi'))}] t {_fmt(pooled.get('t_stat'), 2)} "
               f"n {pooled.get('n_dates', DASH)}d/{pooled.get('trades', DASH)}t | p_RC A/B/C/ALL69 "
               + "/".join(_p((m.get(c) or {}).get("p_rc"), 2) for c in FAMILY_CAMPAIGNS)
               + f" | Holm p {_p(h.get('p_adj'), 3)} | DSR {_p(_g(summary, 'clustered_dsr', 'dsr'), 2)} | N_phen {nph.get('total', DASH)}")
    if ctl:
        out.append(f"RESIDUAL-NULL RANK {rs.get('real_rank', DASH)}/{rs.get('n_done', DASH)} (p95 {_fmt(rs.get('p95'))}) | "
                   f"snapshot boot_lo>0 {sn.get('n_boot_lo_gt0', DASH)}/{sn.get('n_done', DASH)} KS p {_p((sn.get('ks_p_rc') or {}).get('p'), 2)} rank {sn.get('real_rank', DASH)} | "
                   f"planted capture {_p(pl.get('capture_ratio'), 2)} {'PASS' if pl.get('pass') else 'FAIL'}")
    else:
        out.append("controls: not run")
    out.append("")
    cols = ("lane", "status", "family", "pick", "pooled OOS lo..hi", "dates", "trades", "p_RC", "Holm p", "vs no-filter", "N_phen", "controls", "units / ETA")
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "---|" * len(cols))
    lanes = _coverage_lanes(coverage)
    weather = summary.get("lane") or "weather"
    pk = (summary.get("picks") or {}).get("ALL69") or {}
    pv = _g(summary, "paired_vs_nofilter", "pooled") or {}
    info = lanes.get(weather, {})
    ctrl_cell = (f"snap {sn.get('n_boot_lo_gt0', DASH)}/{sn.get('n_done', DASH)} res#{rs.get('real_rank', DASH)} plant {_p(pl.get('capture_ratio'), 2)}") if ctl else "none"
    out.append("| " + " | ".join([
        weather, _lane_status(info) if (info.get("status") or info.get("state")) else "READY", str(summary.get("family", DASH)),
        f"{status_word} `{str(pk.get('genome_id', DASH))[:8]}`", f"{_fmt(pooled.get('boot_lo'))}..{_fmt(pooled.get('boot_hi'))}",
        str(pooled.get("n_dates", DASH)), str(pooled.get("trades", DASH)), _p((m.get("ALL69") or {}).get("p_rc"), 2), _p(h.get("p_adj"), 3),
        f"{_fmt(pv.get('mean'))} lo {_fmt(pv.get('boot_lo'))}", str(nph.get("total", DASH)), ctrl_cell, _lane_units(info),
    ]) + " |")
    for lane in [l for l in ("gas", "mention", "tweets", "crypto_annual") if l in lanes] + sorted(l for l in lanes if l not in (weather, "gas", "mention", "tweets", "crypto_annual")):
        li = lanes.get(lane, {})
        out.append("| " + " | ".join([lane, _lane_status(li)] + [DASH] * 10 + [_lane_units(li)]) + " |")
    text = "\n".join(out) + "\n"
    if len(text) > BOARD_MAX_CHARS:
        # drop the lane table rows first, then truncate hard: the verdict lines must survive
        head = "\n".join(out[: out.index("") + 1 if "" in out else len(out)]) + "\n"
        text = head if len(head) <= BOARD_MAX_CHARS else head[: BOARD_MAX_CHARS - 1] + "\n"
    return text


def write_oos_csv(summary: Dict[str, Any], path: Path) -> Path:
    rows = _g(summary, "pooled_oos", "per_date") or []
    lines = ["date,campaign,pnl,trades"] + [f"{r['date']},{r['campaign']},{r['pnl']!r},{r['trades']}" for r in rows]
    write_text(Path(path), "\n".join(lines) + "\n")
    return Path(path)


def write_finalists(summary: Dict[str, Any], path: Path) -> Path:
    write_json(Path(path), {"run_id": summary.get("run_id"), "family": summary.get("family"),
                            "picker": "max_boot_lo_ties_fewer_clauses (ALL69 ledger, distinct phenotypes)",
                            "all69_pick": (summary.get("picks") or {}).get("ALL69"), "finalists": summary.get("finalists") or []})
    return Path(path)


def write_family_report(summary: Dict[str, Any], out_dir: Union[str, Path], *, reports_root: Optional[Path] = None,
                        coverage: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    """summary.json / summary.md / board.md / oos_by_date.csv / finalists.json / status.json + latest.json (``run``, ``board``)."""
    out_dir = Path(out_dir).resolve()
    reports_root = Path(reports_root).resolve() if reports_root else out_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    if coverage is None:
        coverage = _load_json(reports_root / "coverage.json")
    paths = {
        "summary_json": out_dir / "summary.json",
        "summary_md": out_dir / "summary.md",
        "board_md": out_dir / "board.md",
        "oos_csv": out_dir / "oos_by_date.csv",
        "finalists_json": out_dir / "finalists.json",
        "status_json": out_dir / "status.json",
        "latest_json": reports_root / "latest.json",
    }
    write_json(paths["summary_json"], summary)
    write_text(paths["summary_md"], render_family_md(summary))
    write_text(paths["board_md"], render_family_board(summary, coverage))
    write_oos_csv(summary, paths["oos_csv"])
    write_finalists(summary, paths["finalists_json"])
    v = summary.get("verdict") or {}
    pooled = summary.get("pooled_oos") or {}
    write_json(paths["status_json"], {
        "run_id": summary.get("run_id"), "kind": "family", "family": summary.get("family"), "phase": "F2",
        "state": "REPORTED", "verdict": v.get("status"), "failing": v.get("failing"),
        "pooled_oos": _stats_only(pooled), "p_rc_all69": _g(summary, "multiplicity", "ALL69", "p_rc"),
        "holm_p": _g(summary, "holm", "this_family", "p_adj"), "n_phenotypes": summary.get("n_phenotypes"),
        "evaluations": summary.get("evaluations"), "controls_done": {k: (summary.get("controls") or {}).get(k, {}).get("n_done") for k in ("snapshot", "residual", "planted")} if summary.get("controls") else None,
        "registry_status": _g(summary, "registry_line", "status"), "git_rev": summary.get("git_rev"),
    })

    def _rel(p: Path) -> str:
        try:
            return p.relative_to(reports_root).as_posix()
        except ValueError:
            return p.as_posix()

    latest = _load_json(paths["latest_json"]) or {}
    latest.update({
        "run": summary.get("run_id"),
        "board": _rel(paths["board_md"]),
        "family": summary.get("family"),
        "family_summary": _rel(paths["summary_json"]),
        "family_status": _rel(paths["status_json"]),
        "verdict": v.get("status"),
    })
    write_json(paths["latest_json"], latest)
    return paths
