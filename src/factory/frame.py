"""Frame hardening: evaluator opportunity frame -> slim visible/hidden numpy ``Frame``.

Implements FACTORY_ARCHITECTURE section 4.2 (items 1-9) and PRD FR-F1.1 on
top of the **unchanged** ``ev_analysis`` opportunity frame that
``lanes/weather.py`` builds. Lab-only (pandas); the numpy-only consumers are
``columns.py`` / ``features.py`` / ``genome.py``.

Three frames (``FrameSet``):

* ``parity``   -- Phase-2 convention: pinned inputs (``configs/factory/parity_inputs.json``),
  availability lag 0, no truth filter, no sigma cap, evaluator ``executable``.
  Exists only so the fitness kernel can be proven identical to
  ``ev_analysis.evaluate_shape`` (181 trades / +0.0636).
* ``search``   -- current inputs, lag 240 min, truth filter, ``sigma_f <= 4``,
  ``executable &= sandbox_admissible``, cutoff asserted. What evolution sees.
* ``gefs_twin`` -- the ``search`` hardening on the GEFS source; ``search.twin_index``
  maps each search row to its twin row (``-1`` when absent) for the ex-ante
  disqualifier.

Availability lag (section 4.2 item 3, PRD A11)
-----------------------------------------------
The evaluator joins the latest vintage with ``init_ts <= ts_utc`` (lag 0).
MOS MEX bulletins issue ~4 h after init, so the search frame must use, at
each snapshot, the latest vintage with ``init_ts + lag <= ts_utc``. This is
done **at the vintage join**, not by dropping rows afterwards:
``lanes.weather.build_opportunities`` shifts the archive's ``init_ts`` key
forward by the lag before calling ``ev_analysis.forecast_vintage_table``
(the true ``init_time_utc`` string is carried through unchanged, so
``lead_hours`` and the probability table are the archive's own), rebuilds
the probability table for exactly those vintages, and then builds the
opportunity frame. ``from_opportunity_frame`` re-asserts
``init + lag <= ts`` on every row and aborts on any violation -- which after
the shifted join can only mean a bug. Snapshots whose only vintages are
inside the lag window get no vintage and drop out of the inner join, the
same way the evaluator drops snapshots with no vintage at lag 0; the counts
(``lag_snapshots_revintaged``, ``lag_snapshots_dropped``) are recorded in
provenance.

Provenance
----------
sha256 of every ladder CSV, the forecast archive, the truth files, every
file in the calibration directory (asserted *not* loaded by the walk-forward
chain, hashed for the record), the fee regime, the ``EVConfig`` fields, the
git rev (**abort if empty**), ``deploy/spark/requirements-lab.lock``, the
availability lag and the filter counts. Path separators are normalised to
``/`` before anything is hashed or written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.factory import fees as fees_mod
from src.factory import features as feat
from src.factory.columns import (
    CITY_LABELS,
    DIRECTION_LABELS,
    HIDDEN_COLUMNS,
    HIDDEN_DTYPES,
    MODE_LABELS,
    RESULT_LABELS,
    STRIKE_TYPE_LABELS,
    VISIBLE_COLUMNS,
    VISIBLE_DTYPES,
    Frame,
)

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
PARITY_INPUTS_PATH = os.path.join(REPO_ROOT, "configs", "factory", "parity_inputs.json")
DEFAULT_PARITY_DEST = os.path.join(REPO_ROOT, "data", "factory", "parity_inputs")
LAB_LOCK_PATH = os.path.join(REPO_ROOT, "deploy", "spark", "requirements-lab.lock")
#: PRD A3: development data ends here until the registry carries RATIFIED <date>.
FACTORY_DATA_CUTOFF = "2026-07-25"
#: Section 4.2 item 3 default (MOS MEX issues ~4 h after init).
FORECAST_AVAILABILITY_LAG_MIN = 240

#: Columns ``from_opportunity_frame`` reads from the evaluator frame.
REQUIRED_OPP_COLUMNS: Tuple[str, ...] = (
    "series", "city", "target_date", "market_ticker", "ts_utc", "minutes_to_close",
    "strike_type", "floor_strike", "cap_strike", "yes_bid", "yes_ask", "no_bid",
    "no_ask", "last", "price_mean", "yes_bid_low", "yes_ask_high", "volume",
    "open_interest", "result", "expiration_value", "cli_high",
    "payoff_matches_kalshi", "truth_agrees", "init_time_utc", "lead_hours",
    "mu_f", "sigma_f", "p_yes", "midpoint_f", "distance_f", "edge_distance_f",
    "fwd_min_ask", "fwd_max_bid", "maker_yes_fill", "maker_no_fill", "settles_yes",
    "direction", "mode", "quote", "p_win", "won", "price_paid", "executable",
    "fee_per_contract", "ev_per_contract", "realized_per_contract",
)


class FrameAbort(RuntimeError):
    """A hardening assert failed; the reason is logged and carried in the message."""


@dataclass(frozen=True)
class FrameConfig:
    """Everything ``WeatherLane.build_frames`` needs; hashed into provenance."""

    cutoff: str = FACTORY_DATA_CUTOFF
    availability_lag_min: int = FORECAST_AVAILABILITY_LAG_MIN
    sigma_cap: float = 4.0
    contracts: int = 20
    adverse_fill: float = 0.01
    embargo_days: int = 1
    source: str = "gfs_mex"
    parity_inputs: str = PARITY_INPUTS_PATH
    parity_dest: str = DEFAULT_PARITY_DEST
    fee_regime_path: str = fees_mod.DEFAULT_REGIME_PATH
    ladder_root: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("parity_inputs", "parity_dest", "fee_regime_path", "ladder_root"):
            if d[k]:
                d[k] = _relpath(d[k])
        return d


@dataclass
class FrameSet:
    parity: Frame
    search: Frame
    gefs_twin: Frame
    provenance: Dict[str, Any] = field(default_factory=dict)

    def frames(self) -> Dict[str, Frame]:
        return {"parity": self.parity, "search": self.search, "gefs_twin": self.gefs_twin}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _abort(reason: str) -> None:
    logger.error("frame abort: %s", reason)
    raise FrameAbort(reason)


def _relpath(path: str) -> str:
    """Repo-relative, ``/``-separated path (or the absolute one, normalised)."""
    p = os.path.abspath(str(path))
    try:
        rel = os.path.relpath(p, REPO_ROOT)
        if not rel.startswith(".."):
            p = rel
    except ValueError:
        pass
    return p.replace(os.sep, "/").replace("\\", "/")


def sha256_file(path: str) -> str:
    return fees_mod.sha256_file(path)


def _epoch_seconds(values: Any) -> np.ndarray:
    """tz-aware/naive/ISO-string timestamps -> int64 epoch seconds (UTC)."""
    ts = pd.to_datetime(pd.Series(values), utc=True)
    naive = ts.dt.tz_convert(None)
    return naive.to_numpy().astype("datetime64[s]").astype(np.int64)


def _tri(values: Any) -> np.ndarray:
    """object column of True/False/None/NaN -> int16 1/0/-1."""
    s = pd.Series(values)
    isna = s.isna().to_numpy()
    raw = np.where(isna, False, s.to_numpy(dtype=object))
    truthy = np.asarray([bool(v) for v in raw], dtype=bool)
    out = np.where(isna, -1, np.where(truthy, 1, 0)).astype(np.int16)
    return out


def _bool(values: Any) -> np.ndarray:
    """object/bool column with possible NaN -> bool (NaN -> False)."""
    return _tri(values) == 1


def _codes(values: Any, labels: Sequence[str]) -> np.ndarray:
    arr = pd.Series(values).astype(str).to_numpy()
    lut = {lab: i for i, lab in enumerate(labels)}
    return np.asarray([lut.get(v, -1) for v in arr], dtype=np.int16)


def _f64(values: Any) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=np.float64)


def git_rev(repo_root: str = REPO_ROOT) -> str:
    """``git rev-parse HEAD`` (+``"+dirty"`` when tracked files differ from HEAD);
    falls back to reading ``.git``; ``""`` when unknown. ``MP_GIT_REV`` wins so
    provenance and the registry (``registry.git_rev``) can never disagree."""
    env = os.getenv("MP_GIT_REV", "").strip()
    if env:
        return env
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            from src.factory.registry import git_dirty_suffix

            return r.stdout.strip() + git_dirty_suffix(repo_root)
    except (OSError, subprocess.SubprocessError):
        pass
    # No git binary (container): read .git/HEAD by hand (worktrees use a gitdir file).
    try:
        git_path = os.path.join(repo_root, ".git")
        if os.path.isfile(git_path):
            with open(git_path, "r", encoding="utf-8") as fh:
                line = fh.read().strip()
            if line.startswith("gitdir:"):
                git_path = os.path.normpath(os.path.join(repo_root, line[7:].strip()))
        with open(os.path.join(git_path, "HEAD"), "r", encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            common = git_path
            cd = os.path.join(git_path, "commondir")
            if os.path.isfile(cd):
                with open(cd, "r", encoding="utf-8") as fh:
                    common = os.path.normpath(os.path.join(git_path, fh.read().strip()))
            ref_path = os.path.join(common, ref)
            if os.path.isfile(ref_path):
                with open(ref_path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            packed = os.path.join(common, "packed-refs")
            if os.path.isfile(packed):
                with open(packed, "r", encoding="utf-8") as fh:
                    for ln in fh:
                        parts = ln.strip().split(" ", 1)
                        if len(parts) == 2 and parts[1] == ref:
                            return parts[0]
            return ""
        return head
    except OSError:
        return ""


_LADDER_HASH_CACHE: Dict[str, Dict[str, str]] = {}


def hash_ladder_root(root: str) -> Dict[str, str]:
    """``{relpath: sha256}`` of every ``*.csv`` (and manifest.json) under a ladder root."""
    key = os.path.normcase(os.path.abspath(root))
    cached = _LADDER_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    out: Dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if fn.endswith(".csv") or fn == "manifest.json":
                p = os.path.join(dirpath, fn)
                out[_relpath(p)] = sha256_file(p)
    out = dict(sorted(out.items()))
    _LADDER_HASH_CACHE[key] = out
    return out


def _hash_dir(path: str, suffixes: Tuple[str, ...] = (".json",)) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.isdir(path):
        return out
    for fn in sorted(os.listdir(path)):
        if fn.endswith(suffixes):
            p = os.path.join(path, fn)
            if os.path.isfile(p):
                out[_relpath(p)] = sha256_file(p)
    return out


# ---------------------------------------------------------------------------
# the hardening
# ---------------------------------------------------------------------------
def from_opportunity_frame(
    opp: pd.DataFrame,
    *,
    name: str,
    availability_lag_min: int = 0,
    truth_filter: bool = False,
    sigma_cap: Optional[float] = None,
    fold_sandbox_admissible: bool = False,
    cutoff: Optional[str] = None,
    fee_regime: Optional[fees_mod.FeeRegime] = None,
    adverse_fill: float = 0.01,
    contracts: int = 20,
) -> Frame:
    """Apply section 4.2 to an evaluator opportunity frame and encode it per ``columns.py``.

    Aborts (``FrameAbort``, logged) on: a ``target_date`` past ``cutoff``; any
    ``payoff_matches_kalshi == False`` market (always, filter or not); any row
    with ``init_time_utc + availability_lag_min > ts_utc``; an empty git rev.
    """
    missing = [c for c in REQUIRED_OPP_COLUMNS if c not in opp.columns]
    if missing:
        _abort(f"{name}: opportunity frame lacks columns {missing}")
    prov: Dict[str, Any] = {
        "frame": name,
        "availability_lag_min": int(availability_lag_min),
        "truth_filter": bool(truth_filter),
        "sigma_cap": None if sigma_cap is None else float(sigma_cap),
        "fold_sandbox_admissible": bool(fold_sandbox_admissible),
        "cutoff": cutoff,
        "adverse_fill": float(adverse_fill),
        "contracts": int(contracts),
        "rows_in": int(len(opp)),
        "markets_in": int(opp["market_ticker"].nunique()),
    }
    attrs = dict(getattr(opp, "attrs", {}) or {})

    # 1. cutoff assert ------------------------------------------------------
    tdates = opp["target_date"].astype(str).str.slice(0, 10)
    if cutoff is not None and len(tdates):
        latest = str(tdates.max())
        if latest > str(cutoff):
            n_late = int((tdates > str(cutoff)).sum())
            _abort(
                f"{name}: {n_late} row(s) carry target_date > cutoff {cutoff} "
                f"(latest {latest}); PRD_STRATEGY_FACTORY A3 -- sealed data may not enter a frame"
            )

    # 2. truth filter (market level) ---------------------------------------
    result_l = opp["result"].astype(str).str.lower().to_numpy()
    settled = np.isin(result_l, list(RESULT_LABELS))
    payoff_tri = _tri(opp["payoff_matches_kalshi"])
    truth_tri = _tri(opp["truth_agrees"])
    tickers = opp["market_ticker"].astype(str).to_numpy()

    def _n_markets(mask: np.ndarray) -> int:
        return int(np.unique(tickers[mask]).shape[0]) if mask.any() else 0

    if (payoff_tri == 0).any():
        bad = sorted(set(tickers[payoff_tri == 0]))
        _abort(
            f"{name}: {len(bad)} market(s) have payoff_matches_kalshi == False "
            f"(e.g. {bad[:3]}); the recorded result does not reproduce the payoff, refusing "
            "to score them (section 4.2 item 2)"
        )
    prov["markets_result_unsettled"] = _n_markets(~settled)
    prov["markets_truth_disagree"] = _n_markets(truth_tri == 0)
    prov["markets_truth_none"] = _n_markets(truth_tri == -1)
    prov["markets_payoff_none"] = _n_markets(payoff_tri == -1)
    keep = np.ones(len(opp), dtype=bool)
    if truth_filter:
        keep = settled & (payoff_tri != 0) & (truth_tri != 0)
        prov["dropped_result_unsettled"] = _n_markets(~settled)
        prov["dropped_payoff_mismatch"] = _n_markets(payoff_tri == 0)
        prov["dropped_truth_disagree"] = _n_markets(truth_tri == 0)
        prov["dropped_truth_rows"] = int((~keep).sum())
        prov["kept_truth_none"] = _n_markets(keep & (truth_tri == -1))
        prov["kept_payoff_none"] = _n_markets(keep & (payoff_tri == -1))

    # 3. no-lookahead assert ------------------------------------------------
    ts_epoch = _epoch_seconds(opp["ts_utc"])
    init_epoch = _epoch_seconds(opp["init_time_utc"])
    lag_s = int(availability_lag_min) * 60
    viol = (init_epoch + lag_s) > ts_epoch
    if viol.any():
        i = int(np.flatnonzero(viol)[0])
        _abort(
            f"{name}: {int(viol.sum())} row(s) violate init_time_utc + {availability_lag_min} min "
            f"<= ts_utc (e.g. {tickers[i]} init={opp['init_time_utc'].iloc[i]} "
            f"ts={opp['ts_utc'].iloc[i]}); section 4.2 item 3 / PRD A11"
        )
    prov["lookahead_violations"] = 0
    prov["min_availability_slack_min"] = (
        float((ts_epoch - init_epoch - lag_s).min() / 60.0) if len(opp) else None
    )

    # 4. sigma cap ----------------------------------------------------------
    sigma = _f64(opp["sigma_f"])
    if sigma_cap is not None:
        ok = sigma <= float(sigma_cap)
        prov["dropped_sigma_rows"] = int((keep & ~ok).sum())
        prov["dropped_sigma_markets"] = _n_markets(keep & ~ok)
        keep &= ok

    sub = opp.loc[keep]
    idx = np.flatnonzero(keep)
    n = int(len(sub))
    prov["rows_kept"] = n
    if n == 0:
        _abort(f"{name}: no rows survive the hardening")

    # 5-7. encode -------------------------------------------------------------
    city_code = _codes(sub["city"], CITY_LABELS)
    if (city_code < 0).any():
        _abort(f"{name}: unknown city label(s) {sorted(set(sub['city'].astype(str)[city_code < 0]))}")
    dates = np.asarray(sorted(set(tdates.to_numpy()[idx])), dtype=str)
    date_lut = {d: i for i, d in enumerate(dates)}
    target_date_code = np.asarray([date_lut[d] for d in tdates.to_numpy()[idx]], dtype=np.int16)
    markets = np.asarray(sorted(set(tickers[idx])), dtype=str)
    market_lut = {m: i for i, m in enumerate(markets)}
    market_code = np.asarray([market_lut[m] for m in tickers[idx]], dtype=np.int32)
    direction_code = _codes(sub["direction"], DIRECTION_LABELS)
    mode_code = _codes(sub["mode"], MODE_LABELS)
    if (direction_code < 0).any() or (mode_code < 0).any():
        _abort(f"{name}: unknown direction/mode label")

    yes_bid = _f64(sub["yes_bid"])
    yes_ask = _f64(sub["yes_ask"])
    quote_ev = _f64(sub["quote"])
    quote_f = feat.quote(yes_bid, yes_ask, direction_code, mode_code)
    if not _nan_equal(quote_f, quote_ev):
        _abort(f"{name}: features.quote disagrees with the evaluator's quote column")
    price_ev = _f64(sub["price_paid"])
    price_f = feat.price_paid(quote_f, adverse_fill)
    if not _nan_equal(price_f, price_ev):
        _abort(
            f"{name}: features.price_paid(quote, {adverse_fill}) disagrees with the "
            "evaluator's price_paid (adverse_fill mismatch?)"
        )
    p_win = _f64(sub["p_win"])
    minutes = _f64(sub["minutes_to_close"])
    distance = _f64(sub["distance_f"])
    lead_hours = _f64(sub["lead_hours"])
    ts_sub = ts_epoch[idx]

    sandbox_ok = feat.sandbox_admissible(p_win, price_f)
    executable = _bool(sub["executable"])
    prov["executable_evaluator"] = int(executable.sum())
    prov["sandbox_admissible_rows"] = int(sandbox_ok.sum())
    if fold_sandbox_admissible:
        executable = executable & sandbox_ok
    prov["executable_rows"] = int(executable.sum())

    regime = fee_regime if fee_regime is not None else fees_mod.load_regime()
    series = sub["series"].astype(str).to_numpy()
    is_maker = mode_code == MODE_LABELS.index("maker")
    fee = fees_mod.fee_per_contract(price_f, ts_sub, series, contracts, is_maker, regime=regime)
    fee_ev = _f64(sub["fee_per_contract"])
    both = ~np.isnan(fee) & ~np.isnan(fee_ev)
    prov["fee_regime_vs_evaluator"] = {
        "nan_pattern_equal": bool(np.array_equal(np.isnan(fee), np.isnan(fee_ev))),
        "rows_differing": int((fee[both] != fee_ev[both]).sum()),
        "max_abs_diff": float(np.abs(fee[both] - fee_ev[both]).max()) if both.any() else 0.0,
    }

    won = _bool(sub["won"])
    realized = won.astype(np.float64) - price_f - fee
    realized = np.where(executable, realized, np.nan)

    visible: Dict[str, np.ndarray] = {
        "city_code": city_code,
        "target_date_code": target_date_code,
        "market_code": market_code,
        "ts_utc": ts_sub.astype(np.int64),
        "minutes_to_close": minutes,
        "window_code": feat.window_code(minutes),
        "direction_code": direction_code,
        "mode_code": mode_code,
        "band_code": feat.band_code(distance),
        "lead_bucket_code": feat.lead_bucket_code(lead_hours),
        "lead_hours": lead_hours,
        "p_yes": _f64(sub["p_yes"]),
        "p_win": p_win,
        "mu_f": _f64(sub["mu_f"]),
        "sigma_f": sigma[idx],
        "midpoint_f": _f64(sub["midpoint_f"]),
        "distance_f": distance,
        "edge_distance_f": _f64(sub["edge_distance_f"]),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": _f64(sub["no_bid"]),
        "no_ask": _f64(sub["no_ask"]),
        "last": _f64(sub["last"]),
        "price_mean": _f64(sub["price_mean"]),
        "volume": _f64(sub["volume"]),
        "open_interest": _f64(sub["open_interest"]),
        "quote": quote_f,
        "price_paid": price_f,
        "fee_per_contract": fee,
        "executable": executable,
        "sandbox_admissible": sandbox_ok,
        "floor_strike": _f64(sub["floor_strike"]),
        "cap_strike": _f64(sub["cap_strike"]),
        "strike_type_code": _codes(sub["strike_type"], STRIKE_TYPE_LABELS),
    }
    hidden: Dict[str, np.ndarray] = {
        "won": won,
        "realized_per_contract": realized,
        "result_code": _codes(sub["result"].astype(str).str.lower(), RESULT_LABELS),
        "settles_yes": _bool(sub["settles_yes"]),
        "expiration_value": _f64(sub["expiration_value"]),
        "cli_high": _f64(sub["cli_high"]),
        "truth_agrees": truth_tri[idx],
        "payoff_matches_kalshi": payoff_tri[idx],
        "maker_yes_fill": _bool(sub["maker_yes_fill"]),
        "maker_no_fill": _bool(sub["maker_no_fill"]),
        "fwd_min_ask": _f64(sub["fwd_min_ask"]),
        "fwd_max_bid": _f64(sub["fwd_max_bid"]),
        "yes_bid_low": _f64(sub["yes_bid_low"]),
        "yes_ask_high": _f64(sub["yes_ask_high"]),
        "ev_per_contract": _f64(sub["ev_per_contract"]),
    }
    if (hidden["result_code"] < 0).any() and truth_filter:
        _abort(f"{name}: unsettled result survived the truth filter")

    # sort by (market_code, ts_utc, direction_code, mode_code), stable ---------
    order = np.lexsort((mode_code, direction_code, ts_sub, market_code))
    for d in (visible, hidden):
        for k, v in d.items():
            d[k] = np.ascontiguousarray(v[order]).astype(np.dtype(
                VISIBLE_DTYPES.get(k) or HIDDEN_DTYPES[k]
            ), copy=False)
    block_starts = _block_starts(visible["market_code"], len(markets))

    # provenance --------------------------------------------------------------
    prov.update(_input_provenance(attrs, regime))
    rev = git_rev()
    if not rev:
        _abort(f"{name}: git rev is empty (root-container trap; section 4.2 item 9)")
    prov["git_rev"] = rev
    prov["lab_lock_sha256"] = sha256_file(LAB_LOCK_PATH) if os.path.exists(LAB_LOCK_PATH) else None
    prov["n_rows"] = n
    prov["n_dates"] = int(len(dates))
    prov["n_markets"] = int(len(markets))
    prov["dates"] = [str(dates[0]), str(dates[-1])]

    frame = Frame(
        name=name, visible=visible, hidden=hidden, dates=dates, markets=markets,
        block_starts=block_starts, provenance=prov,
    )
    frame.validate()
    frame.provenance["frame_sha256"] = frame_sha256(frame)
    logger.info(
        "frame %s: rows=%d dates=%d markets=%d executable=%d sha=%s",
        name, n, len(dates), len(markets), int(executable.sum()),
        frame.provenance["frame_sha256"][:12],
    )
    return frame


def _nan_equal(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(np.array_equal(a, b, equal_nan=True))


def _block_starts(market_code: np.ndarray, n_markets: int) -> np.ndarray:
    counts = np.bincount(market_code, minlength=n_markets)
    return np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)


def _input_provenance(attrs: Mapping[str, Any], regime: fees_mod.FeeRegime) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "source": attrs.get("source"),
        "ev_config": attrs.get("ev_config"),
        "embargo_days": attrs.get("embargo_days"),
        "fee_regime": {"path": _relpath(regime.path), "sha256": regime.sha256},
        "opportunity_attrs": {
            k: v for k, v in attrs.items()
            if k.startswith("lag_") or k in ("vintage_rows", "probability_rows")
        },
    }
    root = attrs.get("ladder_root")
    if root:
        hashes = hash_ladder_root(root)
        out["ladder_root"] = _relpath(root)
        out["ladder_files"] = hashes
        out["ladder_files_sha256"] = _sha256_of_mapping(hashes)
    fc = attrs.get("forecast_csv")
    if fc and os.path.exists(fc):
        out["forecast_csv"] = {"path": _relpath(fc), "sha256": sha256_file(fc)}
    truth = attrs.get("truth_files") or {}
    out["truth_files"] = {
        c: {"path": _relpath(p), "sha256": sha256_file(p)} for c, p in truth.items()
        if os.path.exists(p)
    }
    cal = attrs.get("calibration_dir")
    if cal:
        out["calibration_dir"] = {
            "path": _relpath(cal),
            "files": _hash_dir(cal),
            "loaded_by_walk_forward": False,
        }
    return out


def _sha256_of_mapping(m: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(m):
        h.update(k.encode("utf-8"))
        h.update(b"\0")
        h.update(m[k].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# gefs twin
# ---------------------------------------------------------------------------
def build_gefs_twin(
    search: Frame,
    opp_gefs: pd.DataFrame,
    *,
    availability_lag_min: int = FORECAST_AVAILABILITY_LAG_MIN,
    truth_filter: bool = True,
    sigma_cap: Optional[float] = 4.0,
    fold_sandbox_admissible: bool = True,
    cutoff: Optional[str] = FACTORY_DATA_CUTOFF,
    fee_regime: Optional[fees_mod.FeeRegime] = None,
    adverse_fill: float = 0.01,
    contracts: int = 20,
) -> Frame:
    """The GEFS twin of ``search`` (same hardening); sets ``search.twin_index``.

    ``twin_index[r]`` is the twin row with the same ``(market_ticker, ts_utc,
    direction, mode)`` as search row ``r``, or ``-1`` when the twin has no
    such row (no GEFS vintage, sigma over the cap, ...).
    """
    twin = from_opportunity_frame(
        opp_gefs, name="gefs_twin", availability_lag_min=availability_lag_min,
        truth_filter=truth_filter, sigma_cap=sigma_cap,
        fold_sandbox_admissible=fold_sandbox_admissible, cutoff=cutoff,
        fee_regime=fee_regime, adverse_fill=adverse_fill, contracts=contracts,
    )
    search.twin_index = twin_index(search, twin)
    search.provenance["twin"] = {
        "frame_sha256": twin.provenance.get("frame_sha256"),
        "rows_with_twin": int((search.twin_index >= 0).sum()),
        "rows": search.n_rows,
    }
    search.validate()
    return twin


def _key_frame(frame: Frame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": frame.markets[frame.visible["market_code"]],
            "ts": frame.visible["ts_utc"],
            "d": frame.visible["direction_code"],
            "m": frame.visible["mode_code"],
        }
    )


def twin_index(search: Frame, twin: Frame) -> np.ndarray:
    left = _key_frame(search)
    right = _key_frame(twin)
    right["twin_row"] = np.arange(twin.n_rows, dtype=np.int64)
    if right.duplicated(["ticker", "ts", "d", "m"]).any():
        _abort("gefs_twin: duplicate (ticker, ts, direction, mode) keys")
    merged = left.merge(right, on=["ticker", "ts", "d", "m"], how="left", sort=False)
    if len(merged) != len(left):
        _abort("gefs_twin: key join changed the row count")
    return merged["twin_row"].fillna(-1).to_numpy(dtype=np.int64)


# ---------------------------------------------------------------------------
# hashing / save / load
# ---------------------------------------------------------------------------
def frame_sha256(frame: Frame) -> str:
    """Path-independent content hash: canonical column order bytes (little-endian)."""
    h = hashlib.sha256()
    h.update(b"dates\0" + "\n".join(map(str, frame.dates)).encode("utf-8") + b"\0")
    h.update(b"markets\0" + "\n".join(map(str, frame.markets)).encode("utf-8") + b"\0")
    for group, names in (("visible", VISIBLE_COLUMNS), ("hidden", HIDDEN_COLUMNS)):
        src = frame.visible if group == "visible" else frame.hidden
        for name in names:
            a = np.ascontiguousarray(src[name])
            if a.dtype.byteorder == ">":
                a = a.astype(a.dtype.newbyteorder("<"))
            h.update(f"{group}/{name}/{a.dtype.str}\0".encode("ascii"))
            h.update(a.tobytes())
    return h.hexdigest()


def _structured(cols: Mapping[str, np.ndarray], names: Sequence[str], dtypes: Mapping[str, str]) -> np.ndarray:
    dt = np.dtype([(n, np.dtype(dtypes[n])) for n in names])
    out = np.empty(int(next(iter(cols.values())).shape[0]) if cols else 0, dtype=dt)
    for n in names:
        out[n] = cols[n]
    return out


def save(frame: Frame, directory: str) -> str:
    """Write ``visible.npy``/``hidden.npy`` (structured), json sidecars and ``frame.sha256``."""
    os.makedirs(directory, exist_ok=True)
    np.save(os.path.join(directory, "visible.npy"), _structured(frame.visible, VISIBLE_COLUMNS, VISIBLE_DTYPES))
    np.save(os.path.join(directory, "hidden.npy"), _structured(frame.hidden, HIDDEN_COLUMNS, HIDDEN_DTYPES))
    np.save(os.path.join(directory, "block_starts.npy"), frame.block_starts)
    if frame.twin_index is not None:
        np.save(os.path.join(directory, "twin_index.npy"), frame.twin_index)
    with open(os.path.join(directory, "columns.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"name": frame.name, "visible": dict(VISIBLE_DTYPES), "hidden": dict(HIDDEN_DTYPES)},
            fh, indent=1,
        )
    with open(os.path.join(directory, "dates.json"), "w", encoding="utf-8") as fh:
        json.dump([str(d) for d in frame.dates], fh)
    with open(os.path.join(directory, "markets.json"), "w", encoding="utf-8") as fh:
        json.dump([str(m) for m in frame.markets], fh)
    sha = frame_sha256(frame)
    prov = dict(frame.provenance)
    prov["frame_sha256"] = sha
    with open(os.path.join(directory, "provenance.json"), "w", encoding="utf-8") as fh:
        json.dump(prov, fh, indent=1, sort_keys=True, default=str)
    with open(os.path.join(directory, "frame.sha256"), "w", encoding="ascii") as fh:
        fh.write(f"{sha}  {frame.name}\n")
    return sha


def load(directory: str) -> Frame:
    """Inverse of :func:`save`; verifies ``frame.sha256``."""
    with open(os.path.join(directory, "columns.json"), "r", encoding="utf-8") as fh:
        cols = json.load(fh)
    if list(cols["visible"]) != list(VISIBLE_COLUMNS) or list(cols["hidden"]) != list(HIDDEN_COLUMNS):
        raise FrameAbort(f"{directory}: column contract differs from columns.py")
    vis = np.load(os.path.join(directory, "visible.npy"))
    hid = np.load(os.path.join(directory, "hidden.npy"))
    visible = {n: np.ascontiguousarray(vis[n]) for n in VISIBLE_COLUMNS}
    hidden = {n: np.ascontiguousarray(hid[n]) for n in HIDDEN_COLUMNS}
    with open(os.path.join(directory, "dates.json"), "r", encoding="utf-8") as fh:
        dates = np.asarray(json.load(fh), dtype=str)
    with open(os.path.join(directory, "markets.json"), "r", encoding="utf-8") as fh:
        markets = np.asarray(json.load(fh), dtype=str)
    with open(os.path.join(directory, "provenance.json"), "r", encoding="utf-8") as fh:
        prov = json.load(fh)
    twin = None
    tp = os.path.join(directory, "twin_index.npy")
    if os.path.exists(tp):
        twin = np.load(tp)
    frame = Frame(
        name=cols["name"], visible=visible, hidden=hidden, dates=dates, markets=markets,
        block_starts=_block_starts(visible["market_code"], len(markets)),
        provenance=prov, twin_index=twin,
    )
    frame.validate()
    with open(os.path.join(directory, "frame.sha256"), "r", encoding="ascii") as fh:
        want = fh.read().split()[0]
    got = frame_sha256(frame)
    if got != want:
        raise FrameAbort(f"{directory}: frame.sha256 {want[:12]} != content {got[:12]}")
    return frame


def save_frameset(fs: FrameSet, directory: str) -> Dict[str, str]:
    shas = {name: save(fr, os.path.join(directory, name)) for name, fr in fs.frames().items()}
    with open(os.path.join(directory, "provenance.json"), "w", encoding="utf-8") as fh:
        json.dump({**fs.provenance, "frames": shas}, fh, indent=1, sort_keys=True, default=str)
    return shas


# ---------------------------------------------------------------------------
# parity inputs
# ---------------------------------------------------------------------------
def load_parity_pin(path: str = PARITY_INPUTS_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        pin = json.load(fh)
    commit = str(pin.get("commit", ""))
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise FrameAbort(f"{path}: commit must be a full 40-char sha, got {commit!r}")
    for f in pin.get("files", []):
        if len(str(f.get("sha256", ""))) != 64:
            raise FrameAbort(f"{path}: {f.get('path')} needs a full sha256")
    return pin


def materialise_parity_inputs(
    dest: str = DEFAULT_PARITY_DEST, pin_path: str = PARITY_INPUTS_PATH
) -> Tuple[str, str]:
    """Write the pinned forecast-archive / truth blobs; return ``(forecast_archive_dir, truth_dir)``.

    Each file is ``git show <commit>:<path>`` (cwd = repo root), written under
    ``dest/<parent dir name>/<basename>`` and verified against the pinned
    sha256. A file already present with the right hash is reused without git.
    Aborts with a clear reason when git is unavailable or a hash mismatches.
    """
    pin = load_parity_pin(pin_path)
    commit = pin["commit"]
    fa_dir = os.path.join(dest, "forecast_archive")
    truth_dir = os.path.join(dest, "weather_truth")
    os.makedirs(fa_dir, exist_ok=True)
    os.makedirs(truth_dir, exist_ok=True)
    for entry in pin["files"]:
        rel = str(entry["path"]).replace("\\", "/")
        want = str(entry["sha256"])
        parent = rel.split("/")[-2] if "/" in rel else ""
        sub = {"forecast_archive": fa_dir, "weather_truth": truth_dir}.get(parent)
        if sub is None:
            _abort(f"parity pin: {rel} is neither a forecast_archive nor a weather_truth file")
        target = os.path.join(sub, os.path.basename(rel))
        if os.path.exists(target) and sha256_file(target) == want:
            continue
        try:
            r = subprocess.run(
                ["git", "show", f"{commit}:{rel}"], cwd=REPO_ROOT, capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _abort(f"parity pin: git unavailable ({exc}); cannot materialise {rel}@{commit[:12]}")
        if r.returncode != 0:
            _abort(
                f"parity pin: git show {commit[:12]}:{rel} failed: "
                f"{r.stderr.decode('utf-8', 'replace').strip()}"
            )
        got = hashlib.sha256(r.stdout).hexdigest()
        if got != want:
            _abort(f"parity pin: {rel}@{commit[:12]} sha256 {got[:12]} != pinned {want[:12]}")
        with open(target, "wb") as fh:
            fh.write(r.stdout)
        if sha256_file(target) != want:
            _abort(f"parity pin: {target} did not round-trip its sha256")
    return fa_dir, truth_dir


__all__ = [
    "DEFAULT_PARITY_DEST",
    "FACTORY_DATA_CUTOFF",
    "FORECAST_AVAILABILITY_LAG_MIN",
    "FrameAbort",
    "FrameConfig",
    "FrameSet",
    "LAB_LOCK_PATH",
    "PARITY_INPUTS_PATH",
    "REQUIRED_OPP_COLUMNS",
    "build_gefs_twin",
    "frame_sha256",
    "from_opportunity_frame",
    "git_rev",
    "hash_ladder_root",
    "load",
    "load_parity_pin",
    "materialise_parity_inputs",
    "save",
    "save_frameset",
    "twin_index",
]
