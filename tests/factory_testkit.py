"""Test kit for the factory genome/fitness workstream (NOT a test module).

* ``pinned_dirs``: the forecast archive and the four truth files at commit
  ``48618cf`` (the Phase-2 reference inputs) via ``git show`` -- skips when
  git cannot produce them.
* ``build_opp``: the evaluator's pandas opportunity frame (module-cached).
* ``opp_to_frame``: a MINIMAL, INDEPENDENT pandas -> ``columns.Frame``
  converter (deliberately not ``src.factory.frame``; the integration step
  cross-checks the two).
* ``to_pandas_mask``: the genome's predicate list evaluated over the pandas
  frame, so the pandas/evaluator side of a parity test never touches numpy
  frame code.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import pytest

from src.factory import columns as C
from src.factory import genome as G

PIN_COMMIT = "48618cf"
PIN_FILES = (
    "data/forecast_archive/forecast_series_gfs_mex.csv",
    "data/weather_truth/cli_daily_high_KNYC.csv",
    "data/weather_truth/cli_daily_high_KMDW.csv",
    "data/weather_truth/cli_daily_high_KLAX.csv",
    "data/weather_truth/cli_daily_high_KMIA.csv",
)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE_JSON = os.path.join(REPO_ROOT, "reports", "phase2", "ws_e_go_no_go_data_2026-07-26.json")

_OPP_CACHE: Dict[bool, pd.DataFrame] = {}
_PIN_CACHE: Dict[str, Tuple[str, str]] = {}


def pinned_dirs(tmp_path_factory) -> Tuple[str, str]:
    """(forecast_archive_dir, weather_truth_dir) pinned to ``PIN_COMMIT``; skips on failure."""
    if "dirs" in _PIN_CACHE:
        return _PIN_CACHE["dirs"]
    base = tmp_path_factory.mktemp("pin48618cf")
    fa = base / "forecast_archive"
    wt = base / "weather_truth"
    fa.mkdir()
    wt.mkdir()
    for rel in PIN_FILES:
        try:
            out = subprocess.run(
                ["git", "show", f"{PIN_COMMIT}:{rel}"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
                timeout=120,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            pytest.skip(f"cannot pin {rel} at {PIN_COMMIT}: {exc}")
        target = (fa if rel.startswith("data/forecast_archive") else wt) / os.path.basename(rel)
        target.write_bytes(out)
    _PIN_CACHE["dirs"] = (str(fa), str(wt))
    return _PIN_CACHE["dirs"]


def build_opp(pinned: bool = False, dirs: Optional[Tuple[str, str]] = None) -> pd.DataFrame:
    """The Phase-2 walk-forward opportunity frame (C=20, +1c adverse fill, embargo 1)."""
    if pinned in _OPP_CACHE:
        return _OPP_CACHE[pinned]
    logging.getLogger("src.calibration.probability_engine").setLevel(logging.ERROR)
    import src.backtest.ev_analysis as ev

    saved = (ev.FORECAST_ARCHIVE_DIR, ev.WEATHER_TRUTH_DIR)
    try:
        if pinned:
            if dirs is None:
                raise ValueError("pinned=True needs dirs=pinned_dirs(...)")
            ev.FORECAST_ARCHIVE_DIR, ev.WEATHER_TRUTH_DIR = dirs
        ladders = ev.load_search_ladders()
        archive = ev.load_forecast_archive(ev.GFS_MEX)
        vintages = ev.forecast_vintage_table(ladders, archive)
        cfg = ev.EVConfig(
            calibration_mode=ev.CALIB_WALK_FORWARD,
            contracts=20,
            adverse_fill_dollars=0.01,
            embargo_days=1,
        )
        wf = ev.WalkForwardCalibrator(ev.GFS_MEX, ("NY", "CHI", "LAX", "MIA"), embargo_days=1)
        probs = ev.build_probability_table(ladders, vintages, wf, ev.GFS_MEX, cfg)
        opp = ev.build_opportunity_frame(ladders, probs, vintages, cfg)
    finally:
        ev.FORECAST_ARCHIVE_DIR, ev.WEATHER_TRUTH_DIR = saved
    _OPP_CACHE[pinned] = opp
    return opp


# ---------------------------------------------------------------------------
# pandas -> Frame (independent converter)
# ---------------------------------------------------------------------------


def _tri(v: Any) -> int:
    """True -> 1, False -> 0, anything else (None/NaN) -> -1."""
    if v is True or (isinstance(v, (np.bool_,)) and bool(v)):
        return 1
    if v is False or (isinstance(v, (np.bool_,)) and not bool(v)):
        return 0
    return -1


def _sandbox_admissible(p_win: np.ndarray, price_paid: np.ndarray) -> np.ndarray:
    from src.core.fee_calculator import trade_is_profitable

    out = np.zeros(p_win.shape[0], dtype=bool)
    cache: Dict[Tuple[float, float], bool] = {}
    for i in range(p_win.shape[0]):
        pp = price_paid[i]
        pw = p_win[i]
        if not (pp == pp) or not (pw == pw):
            continue
        key = (float(pw), float(pp))
        r = cache.get(key)
        if r is None:
            r = bool(trade_is_profitable(float(pw), float(pp), 1, False))
            cache[key] = r
        out[i] = r
    return out


def opp_to_frame(opp: pd.DataFrame, name: str = "parity") -> C.Frame:
    """Encode every visible/hidden column of ``columns.py`` from the evaluator frame."""
    import src.backtest.ev_analysis as ev

    n = len(opp)
    dates = np.array(sorted(opp["target_date"].astype(str).unique()), dtype=str)
    markets = np.array(sorted(opp["market_ticker"].astype(str).unique()), dtype=str)
    date_code = pd.Categorical(opp["target_date"].astype(str), categories=list(dates)).codes
    market_code = pd.Categorical(opp["market_ticker"].astype(str), categories=list(markets)).codes
    ts = (
        opp["ts_utc"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy().astype("datetime64[s]").astype("int64")
    )
    win_idx = {lab: i for i, lab in enumerate(C.WINDOW_LABELS)}
    band_idx = {lab: i for i, lab in enumerate(C.BAND_LABELS)}
    window_code = np.array([win_idx.get(w, -1) for w in opp["minutes_to_close"].map(ev.time_window_label)])
    band_code = np.array([band_idx.get(b, -1) for b in opp["band"]])
    lead_code = np.array([C.lead_bucket_code(x) for x in opp["lead_hours"].to_numpy()])
    dir_code = np.array([C.code_for(C.DIRECTION_LABELS, d) for d in opp["direction"]])
    mode_code = np.array([C.code_for(C.MODE_LABELS, m) for m in opp["mode"]])
    city_code = np.array([C.code_for(C.CITY_LABELS, c) for c in opp["city"]])
    strike_code = np.array([C.code_for(C.STRIKE_TYPE_LABELS, s) for s in opp["strike_type"]])
    result_code = np.array(
        [C.code_for(C.RESULT_LABELS, str(r).lower()) if isinstance(r, str) else -1 for r in opp["result"]]
    )

    f64 = lambda col: opp[col].to_numpy(dtype="float64", na_value=np.nan)  # noqa: E731
    b = lambda col: opp[col].fillna(False).astype(bool).to_numpy()  # noqa: E731

    visible: Dict[str, np.ndarray] = {
        "city_code": city_code,
        "target_date_code": date_code,
        "market_code": market_code,
        "ts_utc": ts,
        "minutes_to_close": f64("minutes_to_close"),
        "window_code": window_code,
        "direction_code": dir_code,
        "mode_code": mode_code,
        "band_code": band_code,
        "lead_bucket_code": lead_code,
        "lead_hours": f64("lead_hours"),
        "p_yes": f64("p_yes"),
        "p_win": f64("p_win"),
        "mu_f": f64("mu_f"),
        "sigma_f": f64("sigma_f"),
        "midpoint_f": f64("midpoint_f"),
        "distance_f": f64("distance_f"),
        "edge_distance_f": f64("edge_distance_f"),
        "yes_bid": f64("yes_bid"),
        "yes_ask": f64("yes_ask"),
        "no_bid": f64("no_bid"),
        "no_ask": f64("no_ask"),
        "last": f64("last"),
        "price_mean": f64("price_mean"),
        "volume": f64("volume"),
        "open_interest": f64("open_interest"),
        "quote": f64("quote"),
        "price_paid": f64("price_paid"),
        "fee_per_contract": f64("fee_per_contract"),
        "executable": b("executable"),
        "sandbox_admissible": _sandbox_admissible(f64("p_win"), f64("price_paid")),
        "floor_strike": f64("floor_strike"),
        "cap_strike": f64("cap_strike"),
        "strike_type_code": strike_code,
    }
    hidden: Dict[str, np.ndarray] = {
        "won": b("won"),
        "realized_per_contract": f64("realized_per_contract"),
        "result_code": result_code,
        "settles_yes": b("settles_yes"),
        "expiration_value": f64("expiration_value"),
        "cli_high": f64("cli_high"),
        "truth_agrees": np.array([_tri(v) for v in opp["truth_agrees"]]),
        "payoff_matches_kalshi": np.array([_tri(v) for v in opp["payoff_matches_kalshi"]]),
        "maker_yes_fill": b("maker_yes_fill"),
        "maker_no_fill": b("maker_no_fill"),
        "fwd_min_ask": f64("fwd_min_ask"),
        "fwd_max_bid": f64("fwd_max_bid"),
        "yes_bid_low": f64("yes_bid_low"),
        "yes_ask_high": f64("yes_ask_high"),
        "ev_per_contract": f64("ev_per_contract"),
    }
    order = np.lexsort((ts, market_code))  # stable: ties keep the evaluator's row order
    visible = {k: np.ascontiguousarray(v[order]).astype(C.VISIBLE_DTYPES[k]) for k, v in visible.items()}
    hidden = {k: np.ascontiguousarray(v[order]).astype(C.HIDDEN_DTYPES[k]) for k, v in hidden.items()}
    mc = visible["market_code"]
    block_starts = np.searchsorted(mc, np.arange(len(markets) + 1)).astype(np.int64)
    frame = C.Frame(
        name=name,
        visible=visible,
        hidden=hidden,
        dates=dates,
        markets=markets,
        block_starts=block_starts,
        provenance={"converter": "tests.factory_testkit.opp_to_frame", "n_rows": n},
    )
    frame.validate()
    return frame


# ---------------------------------------------------------------------------
# genome predicates over the pandas frame
# ---------------------------------------------------------------------------

_CODE_COLS = {
    "direction_code": ("direction", C.DIRECTION_LABELS),
    "mode_code": ("mode", C.MODE_LABELS),
    "window_code": ("window", C.WINDOW_LABELS),
    "band_code": ("band", C.BAND_LABELS),
}


def to_pandas_mask(g: G.Genome, opp: pd.DataFrame) -> pd.Series:
    """Evaluate ``g.predicates`` over the evaluator's pandas frame (string labels)."""
    if "_lead_bucket_code" not in opp.columns:
        opp["_lead_bucket_code"] = np.array([C.lead_bucket_code(x) for x in opp["lead_hours"].to_numpy()])
    m = pd.Series(True, index=opp.index)
    for p in g.predicates:
        if p.op == "eq":
            col, labels = _CODE_COLS[p.column]
            term = opp[col].eq(labels[int(p.value)])
        elif p.op == "in":
            if p.column == "lead_bucket_code":
                allowed = [i for i in range(p.n_labels) if (int(p.value) >> i) & 1]
                term = opp["_lead_bucket_code"].isin(allowed)
            else:
                col, labels = _CODE_COLS[p.column]
                allowed_l = [labels[i] for i in range(p.n_labels) if (int(p.value) >> i) & 1]
                term = opp[col].isin(allowed_l)
        elif p.op == "ge":
            term = opp[p.column] >= p.value
        elif p.op == "le":
            term = opp[p.column] <= p.value
        elif p.op == "gt":
            term = opp[p.column] > p.value
        elif p.op == "lt":
            term = opp[p.column] < p.value
        elif p.op == "le_diff":
            term = opp[p.column] <= opp[p.other] - p.value
        elif p.op == "ge_sum":
            term = opp[p.column] >= opp[p.other] + p.value
        else:
            raise ValueError(p.op)
        m &= term.fillna(False)
    return m


def phase2_masks(opp: pd.DataFrame) -> Dict[str, pd.Series]:
    """The four Phase-2 taker shapes exactly as scripts/go_no_go.py:443-480 builds them."""
    import src.backtest.ev_analysis as ev

    taker = opp["mode"].eq(ev.MODE_TAKER)
    win = opp["window"].isin(list(ev.TRADEABLE_WINDOWS))
    far = opp["band"].isin(["4-5F", "5F+"])
    return {
        "fr31a_taker": ev.fr31a_mask(opp) & taker,
        "fr31b": ev.fr31b_mask(opp) & taker,
        "far_yes_taker": opp["direction"].eq(ev.DIRECTION_YES) & taker & win & far,
        "nofilter_no": opp["direction"].eq(ev.DIRECTION_NO) & taker & win & far,
    }


# ---------------------------------------------------------------------------
# synthetic frames
# ---------------------------------------------------------------------------


def synthetic_frame(
    n_markets: int = 12,
    n_snapshots: int = 5,
    n_dates: int = 4,
    seed: int = 7,
    name: str = "synthetic",
    executable: Optional[bool] = None,
) -> C.Frame:
    """A small random Frame with all four direction/mode rows per snapshot."""
    rng = np.random.default_rng(seed)
    rows = []
    dates = np.array([f"2026-06-{d + 1:02d}" for d in range(n_dates)], dtype=str)
    markets = np.array([f"KXHIGHNY-26JUN{(i % n_dates) + 1:02d}-T{70 + i}" for i in range(n_markets)], dtype=str)
    markets = np.array(sorted(markets), dtype=str)
    for mi in range(n_markets):
        d = mi % n_dates
        city = mi % 4
        mu = 75.0 + rng.normal(0, 3)
        mid = 70.0 + mi
        for si in range(n_snapshots):
            ts = 1_750_000_000 + d * 86400 + si * 3600
            mtc = float(rng.integers(30, 3000))
            yes_bid = float(rng.integers(0, 60)) / 100.0
            yes_ask = min(1.0, yes_bid + float(rng.integers(1, 40)) / 100.0)
            p_yes = float(np.clip(rng.random(), 0.01, 0.99))
            sigma = float(rng.choice([1.5, 2.5, 3.5, 4.5]))
            lead = float(rng.choice([4, 7, 16, 19]))
            settles = bool(rng.random() < 0.3)
            for dcode, direction in enumerate(C.DIRECTION_LABELS):
                for mcode, mode in enumerate(C.MODE_LABELS):
                    is_maker = mcode == 1
                    if direction == "buy_yes":
                        q = (yes_ask if yes_ask < 1.0 else np.nan) if not is_maker else (yes_bid if yes_bid > 0 else np.nan)
                        p_win, won = p_yes, settles
                    else:
                        q = ((1 - yes_bid) if yes_bid > 0 else np.nan) if not is_maker else ((1 - yes_ask) if yes_ask < 1.0 else np.nan)
                        p_win, won = 1 - p_yes, not settles
                    pp = round(q + 0.01, 10) if q == q else np.nan
                    if pp == pp and pp > 0.99:
                        pp = np.nan
                    fee = 0.0 if is_maker else (0.01 if pp == pp else np.nan)
                    ex = (pp == pp) if executable is None else bool(executable)
                    realized = (float(won) - pp - fee) if ex else np.nan
                    rows.append(
                        dict(
                            city_code=city, target_date_code=d, market_code=mi, ts_utc=ts,
                            minutes_to_close=mtc, window_code=C.code_for(C.WINDOW_LABELS, _window(mtc)),
                            direction_code=dcode, mode_code=mcode,
                            band_code=min(5, int(abs(mid - mu))), lead_bucket_code=C.lead_bucket_code(lead),
                            lead_hours=lead, p_yes=p_yes, p_win=p_win, mu_f=mu, sigma_f=sigma, midpoint_f=mid,
                            distance_f=abs(mid - mu), edge_distance_f=max(0.0, abs(mid - mu) - 0.5),
                            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=1 - yes_ask, no_ask=1 - yes_bid,
                            last=yes_bid, price_mean=(yes_bid + yes_ask) / 2, volume=10.0, open_interest=5.0,
                            quote=q, price_paid=pp, fee_per_contract=fee, executable=ex,
                            sandbox_admissible=ex, floor_strike=mid - 0.5, cap_strike=mid + 0.5, strike_type_code=0,
                            won=won, realized_per_contract=realized, result_code=int(settles), settles_yes=settles,
                            expiration_value=float(settles), cli_high=mu, truth_agrees=1, payoff_matches_kalshi=1,
                            maker_yes_fill=True, maker_no_fill=True, fwd_min_ask=yes_ask, fwd_max_bid=yes_bid,
                            yes_bid_low=yes_bid, yes_ask_high=yes_ask, ev_per_contract=(p_win - pp - fee) if ex else np.nan,
                        )
                    )
    df = pd.DataFrame(rows)
    order = np.lexsort((df["ts_utc"].to_numpy(), df["market_code"].to_numpy()))
    df = df.iloc[order].reset_index(drop=True)
    visible = {k: df[k].to_numpy().astype(dt) for k, dt in C.VISIBLE_DTYPES.items()}
    hidden = {k: df[k].to_numpy().astype(dt) for k, dt in C.HIDDEN_DTYPES.items()}
    mc = visible["market_code"]
    block_starts = np.searchsorted(mc, np.arange(n_markets + 1)).astype(np.int64)
    fr = C.Frame(name=name, visible=visible, hidden=hidden, dates=dates, markets=markets, block_starts=block_starts)
    fr.validate()
    return fr


def _window(minutes_to_close: float) -> str:
    import src.backtest.ev_analysis as ev

    return ev.time_window_label(minutes_to_close)


def permute_rows(F: C.Frame, perm: np.ndarray, name: str = "permuted") -> Tuple[C.Frame, np.ndarray]:
    """Rows reordered by ``perm`` then re-sorted to a valid Frame; returns (frame, new_index_of_old_row)."""
    vis = {k: v[perm] for k, v in F.visible.items()}
    hid = {k: v[perm] for k, v in F.hidden.items()}
    order = np.lexsort((vis["ts_utc"], vis["market_code"]))
    vis = {k: np.ascontiguousarray(v[order]) for k, v in vis.items()}
    hid = {k: np.ascontiguousarray(v[order]) for k, v in hid.items()}
    # old row r sits at position perm[order][j] == r  ->  new_index[r] = j
    new_index = np.empty(F.n_rows, dtype=np.int64)
    new_index[perm[order]] = np.arange(F.n_rows)
    mc = vis["market_code"]
    block_starts = np.searchsorted(mc, np.arange(F.n_markets + 1)).astype(np.int64)
    fr = C.Frame(name=name, visible=vis, hidden=hid, dates=F.dates, markets=F.markets, block_starts=block_starts)
    fr.validate()
    return fr, new_index


def copy_frame(F: C.Frame, name: Optional[str] = None) -> C.Frame:
    fr = C.Frame(
        name=name or F.name,
        visible={k: v.copy() for k, v in F.visible.items()},
        hidden={k: v.copy() for k, v in F.hidden.items()},
        dates=F.dates.copy(),
        markets=F.markets.copy(),
        block_starts=F.block_starts.copy(),
        provenance=dict(F.provenance),
        twin_index=None if F.twin_index is None else F.twin_index.copy(),
    )
    return fr
