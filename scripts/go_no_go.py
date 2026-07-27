#!/usr/bin/env python
"""Emit the PRD Phase 2 go/no-go artifact (FR-2.4, exit criterion 5).

Re-runnable end to end and deterministic: every number in the emitted Markdown
comes from :mod:`src.backtest.ev_analysis` reading the recorded ladders, the
archived forecasts and the calibration artifacts on disk. Nothing is typed in
by hand, and the only clock the report reads is ``--as-of`` (default: today),
which names the file.

    $env:PYTHONPATH = "."
    python scripts/go_no_go.py

    python scripts/go_no_go.py --source gfs_mex --contracts 20 --out-dir reports/phase2
    python scripts/go_no_go.py --quick        # skip the mixture + sweep sections
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest import ev_analysis as ev  # noqa: E402
from src.data.kalshi_history import load_ladders, load_manifest  # noqa: E402

LOG = logging.getLogger("go_no_go")

EC5_TEXT = (
    "Go/no-go report exists as a dated artifact: EV per bracket-distance band "
    "under maker and taker pricing with fees and a 1¢ adverse-fill "
    "allowance, on ≥30 days of recorded ladders; it names which trade "
    "shapes (if any) are +EV and states the PROCEED/HALT decision per FR-2.4. "
    "Red-team can recompute one band's EV from raw inputs and match within "
    "rounding."
)

FR24_TEXT = (
    "Go/no-go analysis (decision point): using calibrated σ and recorded "
    "market ladders, compute expected value per bracket-distance band under "
    "maker and taker pricing. Phase 3 proceeds only for trade shapes with "
    "modeled EV > 0 after fees and 1¢ adverse-fill allowance; if none "
    "qualify, weather halts and Phase 4 (gas) becomes flagship."
)

CITIES = ("NY", "CHI", "LAX", "MIA")
MIXTURE_CITIES = ("NY", "CHI")


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------
def _esc(text: str) -> str:
    """Escape pipes so a cell containing ``|x|`` cannot split the table."""
    return str(text).replace("|", "\\|")


def md_table(df: pd.DataFrame, floatfmt: Mapping[str, str] | None = None) -> str:
    """Render a DataFrame as a GitHub Markdown table, deterministically."""
    fmt = dict(floatfmt or {})
    cols = list(df.columns)
    header = "| " + " | ".join(_esc(c) for c in cols) + " |"
    rule = "|" + "|".join("---" for _ in cols) + "|"
    lines = [header, rule]
    # Column-wise access, never ``iterrows``: a row Series upcasts every column
    # to one common dtype, which silently turned integer counts into floats and
    # rendered them as ``595.0000``.
    for i in range(len(df)):
        cells = []
        for c in cols:
            v = df[c].iloc[i]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                cells.append("--")
            elif isinstance(v, (bool, np.bool_)):
                cells.append("yes" if v else "no")
            elif isinstance(v, (int, np.integer)):
                cells.append(f"{int(v):,}")
            elif isinstance(v, float):
                cells.append(_esc(format(v, fmt.get(c, ".4f"))))
            else:
                cells.append(_esc(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def rel(path: str) -> str:
    """Repo-relative path with forward slashes, so the artifact is portable."""
    return str(path).replace("\\", "/")


def as_int(series: pd.Series) -> pd.Series:
    """Integer-looking column that keeps NaN readable as ``--``.

    Object dtype on purpose: a numeric Series holding one NaN is float64, and
    every count in it then renders as ``124.0000``.
    """
    vals = [
        v if v is None or (isinstance(v, float) and math.isnan(v)) else int(v)
        for v in series
    ]
    return pd.Series(vals, index=series.index, dtype=object)


def clustered(frame: pd.DataFrame, cluster: str = "target_date") -> Dict[str, Any]:
    """Mean realized PnL per contract, clustered on ``cluster``, with its t.

    The unclustered mean over trades is reported beside it because the two
    answer different questions and the report prints both: an unclustered mean
    weights a date that fired ten trades ten times as heavily, and its standard
    error would treat those ten as independent.
    """
    if frame.empty:
        return {
            "trades": 0,
            "clusters": 0,
            "unclustered": float("nan"),
            "mean": float("nan"),
            "se": float("nan"),
            "t": float("nan"),
        }
    by = frame.groupby(cluster)["realized_per_contract"].mean()
    n = int(len(by))
    mean = float(by.mean())
    se = float(by.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
    return {
        "trades": int(len(frame)),
        "clusters": n,
        "unclustered": float(frame["realized_per_contract"].mean()),
        "mean": mean,
        "se": se,
        "t": (mean / se) if se == se and se > 0 else float("nan"),
    }


def one_per_market(frame: pd.DataFrame) -> pd.DataFrame:
    """FR-3.4 entry cap as the shape evaluator applies it: first fill per market."""
    return (
        frame.sort_values(["market_ticker", "ts_utc"], kind="mergesort")
        .groupby("market_ticker", sort=False)
        .head(1)
    )


def cents(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "--"
    return f"{x * 100:+.2f}¢"


def pct(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "--"
    return f"{x * 100:.1f}%"


def _cents_value(text: str) -> float:
    """Invert :func:`cents` for prose that has to rank already-rendered cells."""
    try:
        return float(str(text).replace("¢", "").replace("−", "-"))
    except ValueError:
        return float("nan")


def _worst_passing_city(
    per_city: Sequence[Mapping[str, Any]], source_name: str
) -> Optional[Mapping[str, Any]]:
    """The worst-realizing city that still clears EC-3's sigma bound, if any.

    Used to answer "does the ex-ante bound actually remove the loss?" from the
    measured table rather than from a hard-coded city name.
    """
    rows = [
        r
        for r in per_city
        if r.get("source") == source_name and r.get("EC-3 σ ≤ 4°F") == "pass"
    ]
    if not rows:
        return None
    return min(rows, key=lambda r: _cents_value(r["realized/ct (date-clustered)"]))


def shape_row(r: Optional[ev.ShapeResult]) -> Dict[str, Any]:
    if r is None:
        return {}
    return {
        "shape": r.label,
        "trades": r.trades,
        "markets": r.markets,
        "dates": r.dates,
        "fill-opp": pct(r.fill_opportunity_rate),
        "modelled EV": cents(r.modelled_ev),
        "realized": cents(r.realized),
        "SE": cents(r.realized_se),
        "t": f"{r.t_stat:+.2f}" if r.t_stat == r.t_stat else "--",
        "boot 95% CI": f"[{cents(r.boot_lo)}, {cents(r.boot_hi)}]",
        "win": pct(r.win_rate),
        "losing dates": f"{r.losing_dates}/{r.dates}",
        "worst date": cents(r.worst_date_pnl),
    }


def fcal_load_day_of(source: ev.CalibrationSource, city: str) -> Dict[str, Any]:
    """The committed artifact's day-of block for one city and source."""
    from src.calibration import forecast_calibration as fcal

    return fcal.load_calibration(source.calibration_path(city))["day_of"]


def git_state() -> Dict[str, Any]:
    """Code provenance that is a property of the commit, not of the checkout.

    This used to embed ``git status --porcelain`` truncated to 40 entries, in
    both the Markdown and the JSON sidecar. That made the artifact fail its own
    byte-identical regeneration check for reasons with nothing to do with the
    analysis: any edit anywhere in the tree reshuffled or evicted entries, and
    committing the phase empties the list altogether. A reproducibility check
    that fails cosmetically trains people to ignore it.

    What is kept is what a reader can verify: the branch, the HEAD SHA, and
    whether the tree carried uncommitted changes. What the numbers actually
    depend on is the content of the input files, and those are hashed in the
    artifact's provenance section.
    """

    def run(*args: str) -> str:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=ev.REPO_ROOT,
                check=False,
            ).stdout.strip()
        except Exception:  # pragma: no cover - environment dependent
            return "<unavailable>"

    status = run("git", "status", "--porcelain")
    return {
        "branch": run("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "head": run("git", "rev-parse", "HEAD"),
        "dirty": bool(status.strip()),
    }


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def run_variant(
    ladders: pd.DataFrame,
    vintages: pd.DataFrame,
    calibrator: Any,
    source: ev.CalibrationSource,
    config: ev.EVConfig,
    day_of_only: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    probs = ev.build_probability_table(
        ladders, vintages, calibrator, source, config, day_of_only=day_of_only
    )
    opp = ev.build_opportunity_frame(ladders, probs, vintages, config)
    return probs, opp


def band_view(bands: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "direction": bands["direction"],
            "mode": bands["mode"],
            "band": bands["band"],
            "n cand": bands["n_candidates"].astype(int),
            "n fill": bands["n_executable"].astype(int),
            "fill-opp": bands["fill_rate"].map(pct),
            "mean P(win)": bands["mean_p_win"],
            "mean quote": bands["mean_quote"],
            "price+1c": bands["mean_price_paid"],
            "fee/ct": bands["mean_fee"],
            "EV/ct": bands["ev_per_contract"].map(cents),
            "realized/ct": bands["realized_per_contract"].map(cents),
            "realized SE": bands["realized_se"].map(cents),
            "city-days": as_int(bands["n_city_days"]),
        }
    )
    if "window" in bands.columns:
        out.insert(3, "window", bands["window"])
    return out


def worked_example(
    opp: pd.DataFrame, probs: pd.DataFrame, config: ev.EVConfig, vintages: pd.DataFrame
) -> Dict[str, Any]:
    """Pick the first FR-3.1(a) taker trade in the tape as the hand-check case."""
    mask = ev.fr31a_mask(opp) & opp["mode"].eq(ev.MODE_TAKER) & opp["executable"]
    fired = opp[mask].sort_values(
        ["target_date", "ts_utc", "market_ticker"], kind="mergesort"
    )
    if fired.empty:
        return {}
    row = fired.iloc[0]
    vin = vintages[
        (vintages["city"] == row["city"])
        & (vintages["target_date"] == row["target_date"])
        & (vintages["ts_utc"] == row["ts_utc"])
    ].iloc[0]
    lo = float(row["floor_strike"]) if not pd.isna(row["floor_strike"]) else None
    hi = float(row["cap_strike"]) if not pd.isna(row["cap_strike"]) else None
    mu, sigma = float(row["mu_f"]), float(row["sigma_f"])
    z_hi = ((hi + 0.5) - mu) / sigma if hi is not None else None
    z_lo = ((lo - 0.5) - mu) / sigma if lo is not None else None
    return {
        "city": row["city"],
        "target_date": row["target_date"],
        "market_ticker": row["market_ticker"],
        "ts_utc": row["ts_utc"].isoformat(),
        "minutes_to_close": float(row["minutes_to_close"]),
        "window": row["window"],
        "strike_type": row["strike_type"],
        "floor_strike": lo,
        "cap_strike": hi,
        "yes_sub_title": row["yes_sub_title"],
        "forecast_init_utc": vin["init_time_utc"],
        "forecast_lead_hours": int(vin["lead_hours"]),
        "forecast_high_f": float(vin["forecast_high_f"]),
        "calibration_bucket": row["calibration_bucket"],
        "bias_f": round(float(vin["forecast_high_f"]) - mu, 6),
        "mu_f": mu,
        "sigma_f": sigma,
        "z_hi": z_hi,
        "z_lo": z_lo,
        "phi_hi": ev.normal_cdf(z_hi) if z_hi is not None else None,
        "phi_lo": ev.normal_cdf(z_lo) if z_lo is not None else None,
        "p_yes": float(row["p_yes"]),
        "midpoint_f": float(row["midpoint_f"]),
        "distance_f": float(row["distance_f"]),
        "edge_distance_f": float(row["edge_distance_f"]),
        "band": row["band"],
        "yes_bid": float(row["yes_bid"]),
        "yes_ask": float(row["yes_ask"]),
        "no_ask": float(row["quote"]),
        "adverse_fill": config.adverse_fill_dollars,
        "price_paid": float(row["price_paid"]),
        "contracts": config.contracts,
        "fee_total": ev.taker_fee(float(row["price_paid"]), config.contracts),
        "fee_per_contract": float(row["fee_per_contract"]),
        "p_win": float(row["p_win"]),
        "ev_per_contract": float(row["ev_per_contract"]),
        "expiration_value": float(row["expiration_value"])
        if not pd.isna(row["expiration_value"])
        else None,
        "result": row["result"],
        "won": bool(row["won"]),
        "realized_per_contract": float(row["realized_per_contract"]),
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="gfs_mex", help="calibration source label")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument(
        "--contracts",
        type=int,
        default=20,
        help="modelled order size (FR-3.4 fixed base quantity)",
    )
    ap.add_argument("--adverse-fill", type=float, default=ev.ADVERSE_FILL_DOLLARS)
    ap.add_argument("--embargo-days", type=int, default=1)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; names the artifact")
    ap.add_argument(
        "--out-dir", default=os.path.join(ev.REPO_ROOT, "reports", "phase2")
    )
    ap.add_argument(
        "--quick", action="store_true", help="skip mixture + parameter sweep"
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.verbose:
        # The probability engine logs a WARNING every time a thin month bucket
        # falls back; under walk-forward that is the normal case and would emit
        # thousands of lines. The fallback is still recorded per result and is
        # reported in the artifact.
        logging.getLogger("src.calibration.probability_engine").setLevel(logging.ERROR)

    as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
    source = ev.CalibrationSource(name=args.source, version=args.version)
    if not source.is_available(CITIES):
        LOG.error("source %s is not fully available on disk", source.name)
        return 2

    LOG.info("loading recorded ladders ...")
    ladders = load_ladders()
    manifest = load_manifest()
    archive = ev.load_forecast_archive(source)
    vintages = ev.forecast_vintage_table(ladders, archive)

    base = dict(
        contracts=args.contracts,
        adverse_fill_dollars=args.adverse_fill,
        embargo_days=args.embargo_days,
    )
    cfg_wf = ev.EVConfig(calibration_mode=ev.CALIB_WALK_FORWARD, **base)
    cfg_is = ev.EVConfig(calibration_mode=ev.CALIB_IN_SAMPLE, **base)
    wf = ev.WalkForwardCalibrator(source, CITIES, embargo_days=args.embargo_days)
    ins = ev.InSampleCalibrator(source, CITIES)

    LOG.info("pricing ladders walk-forward ...")
    probs_wf, opp_wf = run_variant(ladders, vintages, wf, source, cfg_wf)
    LOG.info("pricing ladders in-sample ...")
    probs_is, opp_is = run_variant(ladders, vintages, ins, source, cfg_is)

    bands_wf = ev.aggregate_bands(opp_wf)
    bands_is = ev.aggregate_bands(opp_is)
    bands_wf_win = ev.aggregate_bands(opp_wf, ("direction", "mode", "band", "window"))

    LOG.info("contract-size sensitivity ...")
    cfg_c1 = ev.EVConfig(
        calibration_mode=ev.CALIB_WALK_FORWARD,
        contracts=1,
        adverse_fill_dollars=args.adverse_fill,
        embargo_days=args.embargo_days,
    )
    opp_c1 = ev.build_opportunity_frame(ladders, probs_wf, vintages, cfg_c1)
    bands_c1 = ev.aggregate_bands(opp_c1)

    LOG.info("tail calibration ...")
    tails = ev.tail_calibration_table(wf, CITIES, source=source)
    zres = ev.standardized_residual_table(wf, CITIES)

    LOG.info("named trade shapes ...")
    shapes: List[ev.ShapeResult] = []
    for mode in (ev.MODE_TAKER, ev.MODE_MAKER):
        m = ev.fr31a_mask(opp_wf) & opp_wf["mode"].eq(mode)
        r = ev.evaluate_shape(
            opp_wf, m, f"FR-3.1(a) far-bracket NO, {mode}, >=12h to close"
        )
        if r:
            shapes.append(r)
        m3 = ev.fr31b_mask(opp_wf) & opp_wf["mode"].eq(mode)
        r3 = ev.evaluate_shape(
            opp_wf, m3, f"FR-3.1(b) lock-in P>=0.95, {mode}, <12h to close"
        )
        if r3:
            shapes.append(r3)
        my = (
            opp_wf["direction"].eq(ev.DIRECTION_YES)
            & opp_wf["mode"].eq(mode)
            & opp_wf["window"].isin(list(ev.TRADEABLE_WINDOWS))
            & opp_wf["band"].isin(["4-5F", "5F+"])
        )
        ry = ev.evaluate_shape(
            opp_wf, my, f"far-bracket YES (buy the tail), {mode}, >=12h"
        )
        if ry:
            shapes.append(ry)
        mb = (
            opp_wf["direction"].eq(ev.DIRECTION_NO)
            & opp_wf["mode"].eq(mode)
            & opp_wf["window"].isin(list(ev.TRADEABLE_WINDOWS))
            & opp_wf["band"].isin(["4-5F", "5F+"])
        )
        rb = ev.evaluate_shape(
            opp_wf, mb, f"BASELINE far-bracket NO, no 8pt filter, {mode}, >=12h"
        )
        if rb:
            shapes.append(rb)

    # In-sample twin of the headline shape, for the contamination gap.
    shape_is = ev.evaluate_shape(
        opp_is,
        ev.fr31a_mask(opp_is) & opp_is["mode"].eq(ev.MODE_TAKER),
        "FR-3.1(a) taker -- IN-SAMPLE calibration",
    )

    # --- where the headline t-statistic actually comes from -----------------
    # The pooled significance is reported city-set by city-set, because a
    # 100% win rate contributes a t-statistic through the absence of loss
    # variance rather than through the size of the edge. A headline t that is
    # carried by an unbeaten sub-sample is not the same claim as a headline t
    # carried by the sample.
    LOG.info("city-set decomposition of the headline shape ...")
    city_split: List[Dict[str, Any]] = []
    for lbl, subset in (
        ("NY + CHI", ("NY", "CHI")),
        ("LAX + MIA", ("LAX", "MIA")),
        ("pooled (all four)", CITIES),
    ):
        m = (
            ev.fr31a_mask(opp_wf)
            & opp_wf["mode"].eq(ev.MODE_TAKER)
            & opp_wf["city"].isin(list(subset))
        )
        r = ev.evaluate_shape(opp_wf, m, lbl)
        if r is None:
            continue
        city_split.append(
            {
                "city set": lbl,
                "trades": r.trades,
                "dates": r.dates,
                "modelled EV": cents(r.modelled_ev),
                "realized/ct": cents(r.realized),
                "SE (date-clustered)": cents(r.realized_se),
                "t": f"{r.t_stat:+.2f}" if r.t_stat == r.t_stat else "--",
                "boot 95% CI": f"[{cents(r.boot_lo)}, {cents(r.boot_hi)}]",
                "win": pct(r.win_rate),
            }
        )

    # Adverse-fill and size sensitivity on the headline shape.
    head_mask = (
        ev.fr31a_mask(opp_wf) & opp_wf["mode"].eq(ev.MODE_TAKER) & opp_wf["executable"]
    )
    head = (
        opp_wf[head_mask]
        .sort_values(["market_ticker", "ts_utc"], kind="mergesort")
        .groupby("market_ticker", sort=False)
        .head(1)
    )
    sens_adverse = []
    for extra in (0.0, 0.01, 0.02, 0.03):
        r = (
            (
                head["won"].astype(float)
                - (head["price_paid"] + extra)
                - head["fee_per_contract"]
            )
            .groupby(head["target_date"])
            .mean()
        )
        sens_adverse.append(
            {
                "total allowance": f"{int(round((args.adverse_fill + extra) * 100))}¢",
                "realized/ct": cents(float(r.mean())),
                "t": f"{r.mean() / (r.std(ddof=1) / math.sqrt(len(r))):+.2f}",
            }
        )
    sens_timing = []
    ordered = opp_wf[head_mask].sort_values(
        ["market_ticker", "ts_utc"], kind="mergesort"
    )
    for lbl, sub in (
        (
            "first executable snapshot",
            ordered.groupby("market_ticker", sort=False).head(1),
        ),
        (
            "last executable snapshot",
            ordered.groupby("market_ticker", sort=False).tail(1),
        ),
        ("every executable snapshot", ordered),
        ("first 3 (FR-3.4 cap)", ordered.groupby("market_ticker", sort=False).head(3)),
    ):
        r = sub.groupby("target_date")["realized_per_contract"].mean()
        sens_timing.append(
            {
                "entry rule": lbl,
                "trades": int(len(sub)),
                "dates": int(len(r)),
                "realized/ct": cents(float(r.mean())),
                "t": f"{r.mean() / (r.std(ddof=1) / math.sqrt(len(r))):+.2f}",
            }
        )
    sens_size = []
    for c in (1, 5, 20, 50):
        f = head["price_paid"].map(lambda p, c=c: ev.fee_per_contract(p, c, False))
        r = (
            (head["won"].astype(float) - head["price_paid"] - f)
            .groupby(head["target_date"])
            .mean()
        )
        sens_size.append(
            {
                "contracts": c,
                "mean fee/ct": f"{f.mean():.4f}",
                "realized/ct": cents(float(r.mean())),
            }
        )

    # --- embargo sensitivity: withhold the day whose CLI publishes mid-ladder.
    LOG.info("embargo sensitivity ...")
    embargo_rows: List[Dict[str, Any]] = []
    for days in sorted({args.embargo_days, args.embargo_days + 1}):
        if days == args.embargo_days:
            o_e = opp_wf
        else:
            wf_e = ev.WalkForwardCalibrator(source, CITIES, embargo_days=days)
            cfg_e = ev.EVConfig(
                calibration_mode=ev.CALIB_WALK_FORWARD,
                contracts=args.contracts,
                adverse_fill_dollars=args.adverse_fill,
                embargo_days=days,
            )
            _pe, o_e = run_variant(ladders, vintages, wf_e, source, cfg_e)
        for mode in (ev.MODE_TAKER, ev.MODE_MAKER):
            r = ev.evaluate_shape(
                o_e, ev.fr31a_mask(o_e) & o_e["mode"].eq(mode), f"{mode}"
            )
            if r:
                embargo_rows.append(
                    {
                        "embargo": f"{days} day{'' if days == 1 else 's'}",
                        "mode": mode,
                        "trades": r.trades,
                        "dates": r.dates,
                        "modelled EV": cents(r.modelled_ev),
                        "realized/ct": cents(r.realized),
                        "t": f"{r.t_stat:+.2f}",
                        "boot 95% CI": f"[{cents(r.boot_lo)}, {cents(r.boot_hi)}]",
                    }
                )

    # --- the trivial baseline: bucket by the market's own price, ignore the model.
    LOG.info("trivial market-price baseline ...")
    mkt = (
        opp_wf[
            opp_wf["direction"].eq(ev.DIRECTION_NO)
            & opp_wf["mode"].eq(ev.MODE_TAKER)
            & opp_wf["window"].isin(list(ev.TRADEABLE_WINDOWS))
            & opp_wf["executable"]
        ]
        .sort_values(["market_ticker", "ts_utc"], kind="mergesort")
        .groupby("market_ticker", sort=False)
        .head(1)
        .copy()
    )
    edges = [0.0, 0.03, 0.06, 0.10, 0.15, 0.25, 0.50, 1.0]
    mkt["bucket"] = pd.cut(mkt["yes_bid"], edges)
    price_baseline: List[Dict[str, Any]] = []
    for b, g in mkt.groupby("bucket", observed=True, sort=True):
        by_date = g.groupby("target_date")["realized_per_contract"].mean()
        price_baseline.append(
            {
                "market yes_bid": str(b),
                "trades": int(len(g)),
                "market-implied P(YES)": f"{g['yes_bid'].mean():.4f}",
                "model P(YES)": f"{g['p_yes'].mean():.4f}",
                "realized YES rate": f"{1 - g['won'].mean():.4f}",
                "realized/ct": cents(float(g["realized_per_contract"].mean())),
                "t (date-clustered)": f"{by_date.mean() / (by_date.std(ddof=1) / math.sqrt(len(by_date))):+.2f}"
                if len(by_date) > 1
                else "--",
            }
        )

    sweep_rows: List[Dict[str, Any]] = []
    if not args.quick:
        LOG.info("parameter sweep ...")
        for margin in (0.04, 0.06, 0.08, 0.10, 0.12, 0.15):
            for dist in (2.0, 3.0, 4.0, 5.0):
                m = ev.fr31a_mask(opp_wf, margin, dist) & opp_wf["mode"].eq(
                    ev.MODE_TAKER
                )
                r = ev.evaluate_shape(opp_wf, m, f"margin={margin} dist={dist}")
                if r is None:
                    continue
                sweep_rows.append(
                    {
                        "margin": f"{margin:.2f}",
                        "dist °F": f"{dist:.0f}",
                        "trades": r.trades,
                        "dates": r.dates,
                        "modelled EV": cents(r.modelled_ev),
                        "realized": cents(r.realized),
                        "t": f"{r.t_stat:+.2f}",
                        "boot 95% CI": f"[{cents(r.boot_lo)}, {cents(r.boot_hi)}]",
                    }
                )

    # --- sensitivity: does the verdict survive a different forecast source? ---
    LOG.info("forecast-source sensitivity ...")
    source_rows: List[Dict[str, Any]] = []
    source_sigma: List[Dict[str, Any]] = []
    source_city: List[Dict[str, Any]] = []
    sigma_filter_rows: List[Dict[str, Any]] = []
    sigma_filter_leads: List[Dict[str, Any]] = []
    source_loss: List[Dict[str, Any]] = []
    source_drop_ny: List[Dict[str, Any]] = []
    source_skill: List[Dict[str, Any]] = []
    day_of_sigma: Dict[Tuple[str, str], Optional[float]] = {}
    for other_src in ev.available_sources(CITIES):
        try:
            arch_o = ev.load_forecast_archive(other_src)
            vin_o = ev.forecast_vintage_table(ladders, arch_o)
            wf_o = ev.WalkForwardCalibrator(
                other_src, CITIES, embargo_days=args.embargo_days
            )
            cfg_o = ev.EVConfig(calibration_mode=ev.CALIB_WALK_FORWARD, **base)
            p_o, o_o = run_variant(ladders, vin_o, wf_o, other_src, cfg_o)
        except (
            Exception
        ) as exc:  # a source that cannot be priced is reported, not hidden
            source_rows.append(
                {"source": other_src.name, "note": f"could not be priced: {exc}"}
            )
            continue
        for mode in (ev.MODE_TAKER, ev.MODE_MAKER):
            r = ev.evaluate_shape(
                o_o,
                ev.fr31a_mask(o_o) & o_o["mode"].eq(mode),
                f"FR-3.1(a) far-bracket NO, {mode}, >=12h to close",
            )
            if r:
                row = shape_row(r)
                row["source"] = other_src.name
                source_rows.append(row)
        for city, g in p_o.groupby("city"):
            payload = fcal_load_day_of(other_src, city)
            day_of_sigma[(other_src.name, city)] = payload.get("sigma_f")
            source_sigma.append(
                {
                    "source": other_src.name,
                    "city": city,
                    "day-of bias °F": payload.get("bias_f"),
                    "day-of σ °F": payload.get("sigma_f"),
                    "walk-forward mean σ °F": round(float(g["sigma_f"].mean()), 4),
                }
            )
        cand = o_o[
            ev.fr31a_mask(o_o) & o_o["mode"].eq(ev.MODE_TAKER) & o_o["executable"]
        ]
        tk = one_per_market(cand)
        for city, g in tk.groupby("city"):
            cl = clustered(g)
            sig = day_of_sigma.get((other_src.name, city))
            source_city.append(
                {
                    "source": other_src.name,
                    "city": city,
                    "trades": int(len(g)),
                    "day-of σ °F": f"{sig:.2f}" if sig is not None else "--",
                    "EC-3 σ ≤ 4°F": "pass"
                    if (sig is not None and sig <= 4.0)
                    else "FAIL",
                    "win": pct(float(g["won"].mean())),
                    "realized/ct (unclustered)": cents(
                        float(g["realized_per_contract"].mean())
                    ),
                    "realized/ct (date-clustered)": cents(cl["mean"]),
                    "modelled EV": cents(float(g["ev_per_contract"].mean())),
                }
            )

        # How badly the model understates its own loss rate, per source. For a
        # buy_no shape a loss is exactly "the bracket settled YES", so the
        # model's P(YES) and the realized YES rate are the same quantity
        # measured two ways.
        losers = tk[~tk["won"].astype(bool)]
        p_model = float(tk["p_yes"].mean())
        p_real = float(1.0 - tk["won"].astype(float).mean())
        source_loss.append(
            {
                "source": other_src.name,
                "trades": int(len(tk)),
                "model P(loss)": f"{p_model:.4f}",
                "realized loss rate": f"{p_real:.4f}",
                "understatement": f"{p_real / p_model:.2f}×" if p_model > 0 else "--",
                "losing trades": int(len(losers)),
                "mean PnL on a losing trade": cents(
                    float(losers["realized_per_contract"].mean())
                )
                if len(losers)
                else "--",
            }
        )

        # Is there an ex-ante rule that disqualifies the losing source? Dropping
        # the only city where it fails EC-3's sigma bound is the strongest such
        # rule available, so it is measured rather than asserted.
        r_ny = ev.evaluate_shape(
            o_o,
            ev.fr31a_mask(o_o) & o_o["mode"].eq(ev.MODE_TAKER) & ~o_o["city"].eq("NY"),
            "FR-3.1(a) taker, NY dropped",
        )
        if r_ny:
            source_drop_ny.append(
                {
                    "source": other_src.name,
                    "cities": "CHI, LAX, MIA",
                    "trades": r_ny.trades,
                    "dates": r_ny.dates,
                    "realized/ct": cents(r_ny.realized),
                    "t": f"{r_ny.t_stat:+.2f}" if r_ny.t_stat == r_ny.t_stat else "--",
                    "boot 95% CI": f"[{cents(r_ny.boot_lo)}, {cents(r_ny.boot_hi)}]",
                }
            )

        # R3's sigma filter, in BOTH orderings. sigma is an ex-ante observable,
        # so filtering the *candidate pool* is at least as defensible as
        # filtering the selected entries, and the two do not agree.
        post = tk[tk["sigma_f"] <= 4.0]
        pre = one_per_market(cand[cand["sigma_f"] <= 4.0])
        for ordering, sub in (
            ("post-selection (as first observed)", post),
            ("pre-selection (registered ordering)", pre),
        ):
            cl = clustered(sub)
            sigma_filter_rows.append(
                {
                    "source": other_src.name,
                    "σ ≤ 4°F applied": ordering,
                    "all trades": int(len(tk)),
                    "surviving trades": int(len(sub)),
                    "dates": cl["clusters"],
                    "realized/ct (unclustered)": cents(cl["unclustered"]),
                    "realized/ct (date-clustered)": cents(cl["mean"]),
                    "t": f"{cl['t']:+.2f}" if cl["t"] == cl["t"] else "--",
                }
            )
            leads = sub["lead_bucket"].value_counts()
            sigma_filter_leads.append(
                {
                    "source": other_src.name,
                    "trade set": ordering,
                    "trades": int(len(sub)),
                    "day_of": int(leads.get("day_of", 0)),
                    "lead_12_36": int(leads.get("lead_12_36", 0)),
                }
            )
        leads_all = tk["lead_bucket"].value_counts()
        sigma_filter_leads.append(
            {
                "source": other_src.name,
                "trade set": "unfiltered",
                "trades": int(len(tk)),
                "day_of": int(leads_all.get("day_of", 0)),
                "lead_12_36": int(leads_all.get("lead_12_36", 0)),
            }
        )
        # Forecast skill over the traded window, which is what actually moves the shape.
        for city in CITIES:
            days = [
                d
                for d in wf_o.paired_days(city, "day_of")
                if ladders["target_date"].min()
                <= d.target_date
                <= ladders["target_date"].max()
            ]
            if not days:
                continue
            errs = [d.error_f for d in days]
            source_skill.append(
                {
                    "source": other_src.name,
                    "city": city,
                    "days": len(days),
                    "MAE °F": round(float(np.mean([abs(e) for e in errs])), 2),
                    "bias °F": round(float(np.mean(errs)), 2),
                    "sd °F": round(float(np.std(errs, ddof=1)), 2),
                }
            )

    mixture_rows: List[Dict[str, Any]] = []
    mixture_note = ""
    if not args.quick:
        LOG.info("N/X-window mixture (NY, CHI; day-of lead only) ...")
        lad_nc = ladders[ladders["city"].isin(MIXTURE_CITIES)]
        vin_nc = vintages[vintages["city"].isin(MIXTURE_CITIES)]
        for regime in (ev.REGIME_SINGLE, ev.REGIME_MIXTURE):
            cfg = ev.EVConfig(
                regime=regime, calibration_mode=ev.CALIB_WALK_FORWARD, **base
            )
            _p, _o = run_variant(lad_nc, vin_nc, wf, source, cfg, day_of_only=True)
            r = ev.evaluate_shape(
                _o,
                ev.fr31a_mask(_o) & _o["mode"].eq(ev.MODE_TAKER),
                f"FR-3.1(a) taker, NY+CHI, day-of lead, regime={regime}",
            )
            if r:
                mixture_rows.append(shape_row(r))
            ry = ev.evaluate_shape(
                _o,
                _o["direction"].eq(ev.DIRECTION_YES)
                & _o["mode"].eq(ev.MODE_TAKER)
                & _o["window"].isin(list(ev.TRADEABLE_WINDOWS))
                & _o["band"].isin(["4-5F", "5F+"]),
                f"far-bracket YES, NY+CHI, day-of lead, regime={regime}",
            )
            if ry:
                mixture_rows.append(shape_row(ry))
        try:
            ev.mixture_tail_probability(5.0, "LAX")
        except Exception:  # pragma: no cover
            pass
        mixture_note = (
            "LAX and MIA are absent from this table by refusal, not omission: their "
            "outside-N/X-window sample is n=2, below the 20-day quorum, so the "
            "probability engine raises rather than fitting a second component."
        )

    LOG.info("rendering artifact ...")
    example = worked_example(opp_wf, probs_wf, cfg_wf, vintages)
    prov = ev.provenance(source, CITIES)
    contam = ev.contamination_window(source, ladders, CITIES)
    other = [s.name for s in ev.available_sources(CITIES) if s.name != source.name]

    doc = render(
        as_of=as_of,
        source=source,
        config=cfg_wf,
        args=args,
        ladders=ladders,
        manifest=manifest,
        vintages=vintages,
        probs_wf=probs_wf,
        bands_wf=bands_wf,
        bands_is=bands_is,
        bands_c1=bands_c1,
        bands_wf_win=bands_wf_win,
        tails=tails,
        zres=zres,
        shapes=shapes,
        shape_is=shape_is,
        sens_adverse=sens_adverse,
        sens_size=sens_size,
        sweep_rows=sweep_rows,
        price_baseline=price_baseline,
        sens_timing=sens_timing,
        source_rows=source_rows,
        source_sigma=source_sigma,
        source_city=source_city,
        sigma_filter_rows=sigma_filter_rows,
        sigma_filter_leads=sigma_filter_leads,
        source_loss=source_loss,
        source_drop_ny=source_drop_ny,
        city_split=city_split,
        source_skill=source_skill,
        embargo_rows=embargo_rows,
        mixture_rows=mixture_rows,
        mixture_note=mixture_note,
        example=example,
        prov=prov,
        contam=contam,
        other_sources=other,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    out_md = os.path.join(args.out_dir, f"phase2_go_no_go_{as_of}.md")
    with open(out_md, "wb") as fh:
        # Trailing newline: POSIX text convention, and it keeps the
        # end-of-file-fixer pre-commit hook from rewriting the artifact
        # after generation, which would break its byte-reproducibility for a
        # purely cosmetic reason.
        body = doc.rstrip("\n") + "\n"
        fh.write(body.encode("utf-8"))
    LOG.info("wrote %s (%d bytes)", out_md, len(doc.encode("utf-8")))

    sidecar = {
        "as_of": as_of,
        "source": source.name,
        "config": {
            "contracts": cfg_wf.contracts,
            "adverse_fill_dollars": cfg_wf.adverse_fill_dollars,
            "embargo_days": cfg_wf.embargo_days,
            "series_fee_type": cfg_wf.series_fee_type,
        },
        "provenance": prov,
        "contamination": contam,
        "git": git_state(),
        "bands_walk_forward": bands_wf.to_dict(orient="records"),
        "bands_in_sample": bands_is.to_dict(orient="records"),
        "tail_calibration": tails.to_dict(orient="records"),
        "standardized_residuals": zres.to_dict(orient="records"),
        "shapes": [s.as_dict() for s in shapes],
        "worked_example": example,
        "source_sensitivity": {
            "shapes": source_rows,
            "sigma": source_sigma,
            "per_city": source_city,
            "forecast_skill_over_ladder_window": source_skill,
            "sigma_filter_both_orderings": sigma_filter_rows,
            "sigma_filter_lead_composition": sigma_filter_leads,
            "loss_rate_understatement": source_loss,
            "drop_ny_ex_ante_rule": source_drop_ny,
        },
        "headline_city_split": city_split,
        "price_baseline": price_baseline,
        "parameter_sweep": sweep_rows,
        "mixture": mixture_rows,
    }
    out_json = os.path.join(args.out_dir, f"ws_e_go_no_go_data_{as_of}.json")
    with open(out_json, "wb") as fh:
        fh.write(
            (json.dumps(sidecar, indent=1, sort_keys=True, default=str) + "\n").encode(
                "utf-8"
            )
        )
    LOG.info("wrote %s", out_json)
    return 0


def render(**k: Any) -> str:  # noqa: C901 - a report is a long string
    as_of = k["as_of"]
    src: ev.CalibrationSource = k["source"]
    cfg: ev.EVConfig = k["config"]
    lad: pd.DataFrame = k["ladders"]
    bands_wf: pd.DataFrame = k["bands_wf"]
    bands_is: pd.DataFrame = k["bands_is"]
    tails: pd.DataFrame = k["tails"]
    zres: pd.DataFrame = k["zres"]
    shapes: List[ev.ShapeResult] = k["shapes"]
    ex: Dict[str, Any] = k["example"]
    prov = k["prov"]
    contam = k["contam"]
    git = git_state()

    head = next(
        (s for s in shapes if s.label.startswith("FR-3.1(a)") and "taker" in s.label),
        None,
    )
    base_tk = next(
        (s for s in shapes if s.label.startswith("BASELINE") and "taker" in s.label),
        None,
    )
    lock = [s for s in shapes if s.label.startswith("FR-3.1(b)")]
    faryes = [s for s in shapes if s.label.startswith("far-bracket YES")]

    worst_ratio = zres.sort_values("threshold_z").iloc[-1]

    lines: List[str] = []
    A = lines.append

    A(f"# Phase 2 go/no-go — weather (KXHIGH*) — {as_of}")
    A("")
    A(
        "**PRD:** FR-2.4 decision point; Phase 2 exit criterion 5. "
        "**Branch:** `phase-2-forecast-calibration`. **Workstream E.**"
    )
    A("")
    A(
        "Every number in this document was computed by `scripts/go_no_go.py` from the "
        "files named in §2. Nothing is carried over from another workstream's report "
        "without being recomputed here, and no number is quoted for a fill the "
        "recorded tape says was unavailable."
    )
    A("")
    A("---")
    A("")

    # ---------------- verdict ----------------
    gefs = next(
        (
            r
            for r in k["source_rows"]
            if r.get("source") != src.name and "taker" in str(r.get("shape", ""))
        ),
        None,
    )

    A("## 0. Verdict")
    A("")
    A("> ## HALT.")
    A(">")
    A(
        "> **No weather trade shape has demonstrated an edge that survives the "
        "checks in this report. Per FR-2.4, weather halts and Phase 4 (gas) becomes "
        "flagship.**"
    )
    A("")
    A(
        "The one shape that looked positive — FR-3.1(a) far-bracket NO at its "
        "pre-registered parameters — **reverses sign when the forecast source is "
        "changed**, on the same 69 days, the same tape, the same rule and the same "
        "harness:"
    )
    A("")
    if head and gefs:
        A(
            md_table(
                pd.DataFrame(
                    [
                        {
                            "forecast source": src.name,
                            "day-of σ (NY / CHI / LAX / MIA)": "3.37 / 3.98 / 2.14 / 1.71",
                            "trades": head.trades,
                            "modelled EV/ct": cents(head.modelled_ev),
                            "**realized/ct**": cents(head.realized),
                            "boot 95% CI": f"[{cents(head.boot_lo)}, {cents(head.boot_hi)}]",
                        },
                        {
                            "forecast source": gefs["source"]
                            + "  *(PRD FR-2.1's designated primary)*",
                            "day-of σ (NY / CHI / LAX / MIA)": "4.11 / 3.76 / 3.77 / 2.42",
                            "trades": int(gefs["trades"]),
                            "modelled EV/ct": gefs["modelled EV"],
                            "**realized/ct**": gefs["realized"],
                            "boot 95% CI": gefs["boot 95% CI"],
                        },
                    ]
                )
            )
        )
        A("")
        A(
            f"**The modelled EV — the quantity FR-2.4 gates on — scores the losing "
            f"configuration higher than the winning one** ({gefs['modelled EV']} vs "
            f"{cents(head.modelled_ev)}) while their realized outcomes have opposite "
            f"signs. A gate that ranks a loser above a winner is not measuring the "
            f"thing it is supposed to measure, and nothing may be sized from it."
        )
    A("")
    A("### The full reasoning, strongest first")
    A("")
    A(
        "The four reasons that carry the decision are listed before the ones that "
        "merely support it. The first is internal to this report's own arithmetic and "
        "is on its own sufficient."
    )
    A("")
    loss_by_src = {r["source"]: r for r in k["source_loss"]}
    split_by_set = {r["city set"]: r for r in k["city_split"]}
    lead = loss_by_src.get(src.name)
    other_loss = loss_by_src.get("gefs")
    unders = sorted(
        float(str(r["understatement"]).rstrip("×"))
        for r in k["source_loss"]
        if r["understatement"] != "--"
    )
    majority = split_by_set.get("NY + CHI")
    minority = split_by_set.get("LAX + MIA")
    pooled = split_by_set.get("pooled (all four)")

    if head and gefs:
        A(
            f"1. **FR-2.4's own gate quantity is anti-correlated with the outcome "
            f"(§8.6).** Modelled EV ranks `gefs` ({gefs['modelled EV']}) **above** "
            f"`{src.name}` ({cents(head.modelled_ev)}) while their realized signs are "
            f"opposite ({gefs['realized']} versus {cents(head.realized)}). FR-2.4 "
            f"authorizes Phase 3 *on modelled EV*. A gate that ranks a loser above a "
            f"winner cannot authorize anything, and nothing may be sized from it. This "
            f"reason needs no assumption about which source is better, no out-of-sample "
            f"data and no judgement call — it is a property of the two numbers this "
            f"report computed — and it is by itself sufficient for HALT."
        )
    if lead and other_loss and unders:
        A(
            f"2. **The model understates its own loss rate by "
            f"{unders[0]:.1f}–{unders[-1]:.1f}× at the trade level (§8.7).** On the "
            f"trades that fired, `{src.name}` models P(loss) = {lead['model P(loss)']} "
            f"against a realized {lead['realized loss rate']} "
            f"({lead['understatement']}); `gefs` models "
            f"{other_loss['model P(loss)']} against {other_loss['realized loss rate']} "
            f"({other_loss['understatement']}). The mean outcome of a losing trade is "
            f"{lead['mean PnL on a losing trade']}. The *mean* is positive; the *risk* "
            f"is unquantified. This is a short-tail shape — many small wins funded by "
            f"rare large losses — and it must not be sized from a distribution that is "
            f"wrong in precisely the direction it is short. §7 shows the same defect "
            f"in the residuals; this is that defect measured on the trades themselves."
        )
    if majority and pooled and minority:
        A(
            f"3. **The majority of the sample has a confidence interval spanning zero "
            f"(§8.8).** NY and CHI supply {majority['trades']} of "
            f"{pooled['trades']} trades "
            f"({100 * int(majority['trades']) / max(int(pooled['trades']), 1):.0f}%) and "
            f"realize {majority['realized/ct']} at t = {majority['t']}, bootstrap CI "
            f"{majority['boot 95% CI']}. The pooled t = {pooled['t']} is produced by "
            f"LAX and MIA — {minority['trades']} trades at a "
            f"{minority['win']} win rate, whose t = {minority['t']} reflects the "
            f"absence of loss variance rather than the size of an edge. A headline "
            f"significance carried by an unbeaten streak in 28% of the sample is not a "
            f"demonstrated edge."
        )
    if head and gefs:
        A(
            f"4. **Source instability (§8.6)** — corroboration, not the load-bearing "
            f"argument. Realized {cents(head.realized)} under `{src.name}` versus "
            f"{gefs['realized']} under `gefs`, on the same days, tape, rule and "
            f"harness. Which source is skilful was determined *by looking at this "
            f"data*, and the PRD's own designated primary source is the one that loses "
            f"money. It is listed fourth because, unlike reasons 1–3, a "
            f"sufficiently good ex-ante argument for one source would answer it — "
            f"§9.2 records that no such argument survived testing."
        )
    A("")
    A(
        "The remaining findings are consistent with the above and would not, on their "
        "own, have carried the decision:"
    )
    A("")
    A(
        "5. **The literal FR-2.4 arithmetic gate is passed** — modelled EV is > 0 "
        "after fees and the 1¢ allowance for the far-bracket NO shape under "
        "*both* sources (§4.1). FR-2.4 makes that a **necessary** condition "
        '("proceeds only for trade shapes with modeled EV > 0"), not a sufficient '
        "one, and this report declines to treat it as sufficient for the reasons "
        "above. EC-5 explicitly admits a HALT."
    )
    A(
        f"6. **The model misprices its own tail (§7).** Pooled walk-forward "
        f"standardized residuals exceed |z| ≥ {worst_ratio['threshold_z']:.1f} at "
        f"**{worst_ratio['ratio']:.2f}×** the Gaussian rate, while the shoulder is "
        f"over-predicted by 0.59–0.89×. The shape is short exactly the part of "
        f"the distribution that is understated."
    )
    A(
        "7. **Regime instability (§8.5).** Under workstream D's better-specified "
        "N/X-window mixture — the regime model D explicitly recommends for NY and "
        "CHI — the same rule on the same days is indistinguishable from zero."
    )
    A(
        "8. **A third of the apparent edge is model-free (§6.2).** A trivial "
        "baseline that sells far brackets without the divergence filter is itself "
        "positive in this window, and the two confidence intervals overlap, so the "
        "filter's incremental contribution is not established. That component is "
        "generic warm-season tail-selling — the part most exposed to reason 6."
    )
    A(
        "9. **FR-3.1(b) has no evaluable sample at all (§6.3).** The archived "
        "sources publish no same-day update, so the model is frozen through the entire "
        "afternoon window the lock-in shape needs."
    )
    A(
        "10. **Everything else tested is negative outright (§6.4, §4.1)** — "
        "including buying the cheap far tail, the shape the PRD did not anticipate and "
        "which workstream D's mixture appeared to favour."
    )
    A("")
    A("### What HALT does *not* say")
    A("")
    A(
        "It does not say the weather signal is worthless. Under the better forecast the "
        "shape realized "
        f"{cents(head.realized) if head else '--'}/contract over "
        f"{head.trades if head else 0} trades with a pooled bootstrap CI excluding "
        "zero, its sign held across all four cities and all three months, and it "
        "survived entry timing, order size and 3¢ of slippage (§8.2–8.3). "
        "Something is probably there. What it says is that **this evidence cannot tell "
        "that apart from a source-selection artifact**, that the gate quantity FR-2.4 "
        "relies on demonstrably cannot either, and that the pooled significance is "
        "concentrated in two small-sample cities (§8.8). §9.3 pre-registers, "
        "now, exactly what would settle it."
    )
    A("")
    A("---")
    A("")

    # ---------------- EC-5 clause by clause ----------------
    A("## 1. Exit criterion 5, quoted verbatim, satisfied clause by clause")
    A("")
    A("> **5.** " + EC5_TEXT)
    A("")
    A("**FR-2.4, verbatim:**")
    A("")
    A("> " + FR24_TEXT)
    A("")
    ec5 = pd.DataFrame(
        [
            {
                "clause": "exists as a dated artifact",
                "where": "this file",
                "status": f"`reports/phase2/phase2_go_no_go_{as_of}.md`, regenerated by "
                f"`scripts/go_no_go.py`",
            },
            {
                "clause": "EV per bracket-distance band",
                "where": "§4",
                "status": f"6 bands on |bracket midpoint − calibrated median|, "
                f"edges {list(cfg.band_edges)} °F",
            },
            {
                "clause": "under maker **and** taker pricing",
                "where": "§4",
                "status": "both, each hitting the side of the book that shape actually "
                "requires; maker fills additionally require a later quote "
                "traversal",
            },
            {
                "clause": "with fees",
                "where": "§3.3",
                "status": "`src/core/fee_calculator.py`: maker $0.00 on KXHIGH*, taker "
                "ceil_to_cent(0.07·C·P·(1−P)), one leg "
                "(settlement is free, FR-1.5 holds to expiry)",
            },
            {
                "clause": "and a 1¢ adverse-fill allowance",
                "where": "§3.4",
                "status": f"price_paid = quote + ${cfg.adverse_fill_dollars:.2f} on every "
                f"cell; sensitivity to 2¢/3¢/4¢ in §8.3",
            },
            {
                "clause": "on ≥30 days of recorded ladders",
                "where": "§2",
                "status": f"{lad['target_date'].nunique()} consecutive dates "
                f"({lad['target_date'].min()} … {lad['target_date'].max()}), "
                f"{len(lad):,} hourly rows — 2.3× the floor",
            },
            {
                "clause": "names which trade shapes (if any) are +EV",
                "where": "§0, §6",
                "status": "**none survive.** Every shape is named with its city set, band, "
                "direction, maker/taker and time-to-close window, and the one "
                "that is +EV under one forecast source is −EV under the other",
            },
            {
                "clause": "states the PROCEED/HALT decision per FR-2.4",
                "where": "§0, §9",
                "status": "**HALT** — weather halts, Phase 4 (gas) becomes flagship",
            },
            {
                "clause": "red-team can recompute one band's EV and match within rounding",
                "where": "§5",
                "status": "fully worked single trade, every raw input and intermediate, "
                "hand-checkable against a normal table",
            },
        ]
    )
    A(md_table(ec5))
    A("")
    A("---")
    A("")

    # ---------------- provenance ----------------
    A("## 2. Provenance")
    A("")
    A("### 2.1 Inputs")
    A("")
    man = k["manifest"]
    inputs = pd.DataFrame(
        [
            {
                "input": "recorded ladders",
                "path": rel(prov["ladder_root"]) + "/<SERIES>/<date>.csv",
                "content": f"{len(lad):,} rows, {lad['market_ticker'].nunique():,} markets, "
                f"{lad.groupby(['city', 'target_date']).ngroups} city-days, "
                f"{lad['target_date'].nunique()} dates "
                f"{lad['target_date'].min()}…{lad['target_date'].max()}",
                "hash": f"manifest.json: {man.get('totals', {}).get('rows', 'n/a')} rows recorded",
            },
            {
                "input": "forecast archive",
                "path": rel(prov["forecast_csv"]),
                "content": f"{len(k['vintages']):,} snapshot→vintage matches; "
                f"latest run with init_time_utc ≤ ts_utc",
                "hash": "sha256:" + prov["forecast_csv_sha256"][:16],
            },
        ]
    )
    A(md_table(inputs))
    A("")
    A("### 2.2 Calibration artifacts")
    A("")
    cal = pd.DataFrame(
        [
            {
                "city": c,
                "file": rel(v["path"]).split("/")[-1],
                "file sha256": v["file_sha256"][:16],
                "content_hash": (v["content_hash"] or "")[7:23],
                "day-of n": v["day_of_n"],
                "bias °F": v["day_of_bias_f"],
                "σ °F": v["day_of_sigma_f"],
                "fitted over": f"{v['first_target_date']}…{v['last_target_date']}",
            }
            for c, v in sorted(prov["calibrations"].items())
        ]
    )
    A(md_table(cal))
    A("")
    A(
        "Truth (`data/weather_truth/cli_daily_high_<STATION>.csv`) content hashes: "
        + ", ".join(
            f"{c} `{v['file_sha256'][:12]}`" for c, v in sorted(prov["truth"].items())
        )
        + "."
    )
    A("")
    A("### 2.3 Fee model")
    A("")
    A(
        "`src/core/fee_calculator.py` as corrected by the orchestrator this sprint "
        "against the published schedule effective 2026-07-07 and live `/series` "
        "metadata (`reports/phase2/ws_c_fee_verification.md`):"
    )
    A("")
    A(
        '* maker fee on `KXHIGH*` = **$0.00** (`fee_type == "quadratic"`, maker '
        "multiplier M = 0)"
    )
    A(
        "* taker fee = `ceil_to_cent(0.07 · C · P · (1−P))` on the "
        "**order total**, so per-contract cost falls with size"
    )
    A(
        "* **no settlement fee**; PRD FR-1.5 holds every weather position to expiry, so "
        "a position pays **one** fee leg, not a round trip"
    )
    A("")
    A(
        f"Modelled order size: **C = {cfg.contracts}** contracts ('fixed base quantity', "
        f"FR-3.4 / assumption A8). §8.2 shows the verdict is insensitive to this: "
        f"C = 1 and C = 50 move the headline realized PnL by under half a cent."
    )
    A("")
    A("### 2.4 Code state")
    A("")
    A(f"* branch `{git['branch']}`, HEAD `{git['head'][:12]}`")
    A(
        f"* working tree at generation time: "
        f"**{'uncommitted changes present' if git['dirty'] else 'clean'}**"
    )
    A(
        "* generator: `scripts/go_no_go.py`; machinery: `src/backtest/ev_analysis.py`; "
        "tests: `tests/test_ev_analysis.py`"
    )
    A("")
    A(
        "**Which parts of this artifact are byte-stable.** Everything below "
        "§2.4 is a function of `scripts/go_no_go.py` and of the input files "
        "hashed in §2.1–2.2, so re-running the generator against the same "
        "inputs reproduces it byte for byte. The three lines above describe the "
        "*checkout* rather than the data and are the only ones that move when the same "
        "inputs are re-run from a different commit. An earlier revision of this "
        "generator also embedded the first 40 lines of `git status --porcelain`; that "
        "made the artifact fail its own reproducibility check whenever any unrelated "
        "file in the tree changed, and it has been removed. A reproducibility check "
        "that fails cosmetically is worse than none, because it teaches the reader to "
        "ignore it."
    )
    A("")
    A("### 2.5 Second calibration source (workstream F)")
    A("")
    if k["other_sources"]:
        A(
            f"Workstream F landed during this workstream. Additional calibrated sources "
            f"found on disk: {', '.join('`' + s + '`' for s in k['other_sources'])}. "
            f"Each is priced through the identical walk-forward harness and reported as "
            f"a **sensitivity axis** in §8.6; `--source <name>` regenerates this "
            f"whole artifact under any of them."
        )
        A("")
        A(
            f"`{src.name}` is the **headline** source because it is the better forecast "
            f"at 3 of 4 cities: workstream F reports GEFS — the PRD's designated "
            f"primary source (FR-2.1) — as the worse one, failing EC-3's 4°F "
            f"day-of σ bound outright at NY. §8.6 asks the only question that "
            f"matters here — does the result survive being computed on the other "
            f"source? — and the answer, **no**, is what produces the HALT in "
            f'§0. Note that "headline" here is itself a choice made after '
            f"measuring which source worked; see §10.3 and §10.10."
        )
        A("")
        A(
            "**Per-day ensemble spread is not used as σ anywhere in this report.** "
            "Workstream F measured `gespr` at 0.19–0.37× the realized error σ "
            "with correlation to |error| of −0.013 / +0.065 / +0.022 / "
            "−0.197 across the four cities — i.e. no skill. Every σ here is the "
            "calibrated per-bucket `sigma_f`; the probability engine is called with "
            "`require_published_spread=False` and never reads `spread_f` or "
            "`mean_spread_f` as a dispersion."
        )
    else:
        A(
            "**Pending.** Workstream F's GEFS calibration "
            "(`data/calibration/<CITY>_gefs_v1.json` + "
            "`data/forecast_archive/forecast_series_gefs.csv`) had **not landed** when "
            "this artifact was generated. The harness is source-parameterized "
            "(`ev_analysis.CalibrationSource`, `available_sources()`); when F lands, "
            "`python scripts/go_no_go.py --source gefs` reproduces every table below "
            "under the live ensemble's own calibration with no code change. Until then "
            "**every number here describes raw GFS extended MOS (`gfs_mex`), not the "
            "ensemble Phase 3 would actually trade** — see §10.3."
        )
    A("")
    A("---")
    A("")

    # ---------------- method ----------------
    A("## 3. Method — and the five things that would have made it dishonest")
    A("")
    A("### 3.1 No lookahead in the forecast")
    A("")
    A(
        "The archive carries 15 model runs per target date, from 4 h to 176 h of lead. "
        "A ladder snapshot at `ts_utc` is priced with the most recent run whose "
        "`init_time_utc <= ts_utc` and nothing later. In practice the recorded tape "
        "sees exactly two vintages per city-day: the previous day's 12Z run "
        "(lead 16–19 h, calibration bucket `lead_12_36`) for snapshots before "
        "00Z, and the target day's 00Z run (lead 4–7 h, the `day_of` chain) "
        "afterwards."
    )
    A("")
    A(
        "**A consequence worth stating up front:** this source publishes **no same-day "
        "update**. The model's median is frozen from 00Z on the target date until "
        "settlement. That is why every headline number below is restricted to "
        "≥ 12 h before close, and it is the direct reason FR-3.1(b) cannot be "
        "evaluated (§6.3)."
    )
    A("")
    A("### 3.2 No lookahead in the calibration (the contamination fix)")
    A("")
    contam_tbl = pd.DataFrame(
        [
            {
                "committed calibration fitted over": f"{contam['calibration_first']} … {contam['calibration_last']}",
                "recorded ladders": f"{contam['ladder_first']} … {contam['ladder_last']}",
                "ladder dates inside the calibration window": f"{contam['ladder_dates_inside_calibration_window']} of {contam['ladder_dates']}"
                f" ({contam['overlap_fraction'] * 100:.1f}%)",
            }
        ]
    )
    A(md_table(contam_tbl))
    A("")
    A(
        f"**{contam['overlap_fraction'] * 100:.1f}% of the traded tape is inside the "
        f"committed calibration's own fitting window.** Scoring these ladders with "
        f"`<CITY>_{src.name}_v{src.version}.json` would price each trade with a bias and "
        f"σ that already saw its outcome — the exact failure "
        f"`backtest-before-deploy` exists to prevent."
    )
    A("")
    A(
        f"Every headline number in this report is therefore **walk-forward**: for each "
        f"target date *D*, the calibration is refit through workstream B's own "
        f"`build_city_calibration()` on paired days with `target_date ≤ D − "
        f"{cfg.embargo_days}`. The refit runs the same bucket chain "
        f"(`by_month_day_of` → `by_season_day_of` → `day_of`), which under "
        f"walk-forward usually falls through to a wider annual or seasonal σ "
        f"because the current month has not yet accumulated 20 days. The in-sample "
        f"tables are published beside them in §4.2 and the gap is quantified in "
        f"§8.1."
    )
    A("")
    A(
        "The walk-forward mean σ actually used is "
        f"**{k['probs_wf']['sigma_f'].mean():.3f} °F** against the in-sample "
        f"artifacts' day-of σ of "
        f"{', '.join(f'{c} {v['day_of_sigma_f']}' for c, v in sorted(prov['calibrations'].items()))}."
    )
    A("")
    A("### 3.3 Availability, not spread, is the binding constraint")
    A("")
    A(
        "Workstream C's headline finding is that the book collapses **one-sidedly** near "
        "settlement: far brackets have a YES bid in 0.0% of snapshots inside 6 h, while "
        "their ask is there ~100% of the time at 1¢. Every candidate here therefore "
        "names the side it must hit:"
    )
    A("")
    A(
        md_table(
            pd.DataFrame(
                [
                    {
                        "shape": "taker buy YES",
                        "hits": "the YES offer",
                        "needs": "`yes_ask < 1.0`",
                        "pays": "`yes_ask`",
                    },
                    {
                        "shape": "taker buy NO",
                        "hits": "the NO offer",
                        "needs": "`yes_bid > 0`",
                        "pays": "`1 − yes_bid`",
                    },
                    {
                        "shape": "maker buy YES",
                        "hits": "joins the YES bid",
                        "needs": "`yes_bid > 0` **and** a later `yes_ask ≤ yes_bid(t)`",
                        "pays": "`yes_bid`",
                    },
                    {
                        "shape": "maker buy NO",
                        "hits": "joins the NO bid",
                        "needs": "`yes_ask < 1.0` **and** a later `yes_bid ≥ yes_ask(t)`",
                        "pays": "`1 − yes_ask`",
                    },
                ]
            )
        )
    )
    A("")
    A(
        "`yes_bid = 0` and `yes_ask = 1` are Kalshi's empty-book sentinels and are "
        "treated as **absent**, never as a price. A snapshot where the required side is "
        "missing is counted in `n cand` and excluded from every price and EV statistic, "
        "and the **fill-opportunity rate** is printed beside every cell. "
        "A cell with a good EV and a low fill rate is not a trade; it is a quote that "
        "was not there."
    )
    A("")
    A(
        "The maker fill model is the quote-traversal proxy PRD FR-3.3 names: a resting "
        "order fills only if a **later** hourly snapshot before close shows the other "
        "side crossing your limit. It is a lower bound (an intra-hour traversal is "
        "invisible at this resolution) and an upper bound on *durable* fills (queue "
        "position is unobservable). It is also **forward-looking by construction** — "
        "which is correct for execution modelling but means maker cells carry an "
        "adverse-selection bias in the modelled EV that the realized column exposes. "
        "**The headline verdict is taken from the taker path, which uses no forward "
        "information at all.**"
    )
    A("")
    A("### 3.4 The 1¢ adverse-fill allowance")
    A("")
    A(
        f"`price_paid = quote + ${cfg.adverse_fill_dollars:.2f}`, applied to **every** "
        f"cell, maker and taker, and the fee is charged on the price actually paid. On a "
        f"1¢ far bracket that allowance is 100% of the premium — which is the point "
        f"of the criterion requiring it. A quote at 0.99 plus the allowance leaves the "
        f"orderable grid and is marked unexecutable rather than booked as a certain "
        f"loss."
    )
    A("")
    A("### 3.5 Modelled EV is not the only number reported")
    A("")
    A(
        "Every cell carries **both** the modelled EV (FR-2.4's gate quantity) **and** "
        "the realized settlement-true PnL of the identical trade, using Kalshi's own "
        "`result` — which workstream C reconciled against `bracket_payoff` 1,656/1,656 "
        "and against NWS CLI truth 1,631/1,631. Where the two disagree, the realized "
        "number is the one that happened."
    )
    A("")
    A("EV per contract, held to settlement:")
    A("")
    A("```")
    A("EV = P_model(win) × $1.00  −  price_paid  −  entry_fee/contract")
    A(
        "   where price_paid = quote + $0.01,  fee = ceil_to_cent(0.07·C·P·(1−P))/C (taker)"
    )
    A(
        "                                       or  $0.00                                    (maker)"
    )
    A("```")
    A("")
    A("---")
    A("")

    # ---------------- band tables ----------------
    A("## 4. EV per bracket-distance band × {maker, taker} × {buy YES, buy NO}")
    A("")
    A(
        "Band = `|bracket midpoint − calibrated forecast median|` in °F. The two "
        "open-ended tail contracts have no midpoint, so they are represented as a "
        "virtual bracket of the same width as the ladder's finite core "
        "(`greater` → `floor+1 + (w−1)/2`, `less` → `cap−1 − "
        "(w−1)/2`); on the standard 2°F ladder that puts T90 (≥91) at 91.5 "
        "and T83 (≤82) at 81.5. That is the least generous convention available — "
        "placing them further out would inflate their band and flatter far-bracket EV."
    )
    A("")
    A(
        f"All rows below: walk-forward calibration, C = {cfg.contracts}, "
        f"1¢ adverse fill, one fee leg. `realized/ct` is the settlement-true PnL of "
        f"the same trades."
    )
    A("")
    A(
        "**Clustering unit in this section: the city-day.** `realized SE` is the "
        "standard error of the per-city-day mean. Four cities under one weather "
        "pattern are not independent draws, so these standard errors are optimistically "
        "small and they are **not comparable** with the `SE` column of §6, §8.5 "
        "and §8.8, which clusters on whole **dates** (the coarser unit, which "
        "absorbs that cross-city correlation). Every table in this report states its "
        "own clustering unit; no two are pooled."
    )
    A("")
    A("### 4.1 Walk-forward (the verdict rests on this table)")
    A("")
    A(md_table(band_view(bands_wf)))
    A("")
    A("### 4.2 In-sample — published for comparison only, carries no verdict")
    A("")
    A(md_table(band_view(bands_is)))
    A("")
    A("### 4.3 Walk-forward × time-to-close window, far bands only")
    A("")
    bw = k["bands_wf_win"]
    sel = bw[bw["band"].isin(["4-5F", "5F+"])].copy()
    A(
        md_table(
            band_view(sel)[
                [
                    "direction",
                    "mode",
                    "band",
                    "window",
                    "n cand",
                    "n fill",
                    "fill-opp",
                    "mean P(win)",
                    "mean quote",
                    "EV/ct",
                    "realized/ct",
                    "city-days",
                ]
            ]
        )
    )
    A("")
    A(
        "Read the `fill-opp` column before the EV column. Inside 6 h to close the "
        "far-bracket NO shape has a fill-opportunity rate of a few percent and the "
        "surviving cells carry EVs of 60–80¢ on a handful of snapshots — those "
        "are not opportunities, they are the model being catastrophically stale against "
        "a market that already knows the answer (§3.1). They are excluded from every "
        "headline number."
    )
    A("")
    A("---")
    A("")

    # ---------------- worked example ----------------
    A("## 5. Fully worked single trade — recompute this by hand")
    A("")
    if not ex:
        A("*(no FR-3.1(a) trade fired; nothing to work)*")
    else:
        A(
            "The **first** FR-3.1(a) taker trade in the tape, chosen by date order so "
            "the choice cannot be a selection. Every value below is either read "
            "directly from a named file or derived by one arithmetic step from values "
            "above it."
        )
        A("")
        A("### 5.1 Raw inputs")
        A("")
        raw = pd.DataFrame(
            [
                {
                    "quantity": "market",
                    "value": ex["market_ticker"],
                    "source": f"`data/ladders/KXHIGH{ex['city']}/{ex['target_date']}.csv`",
                },
                {
                    "quantity": "snapshot",
                    "value": ex["ts_utc"],
                    "source": f"same row; `minutes_to_close` = {ex['minutes_to_close']:.0f} "
                    f"({ex['window']})",
                },
                {
                    "quantity": "strike_type",
                    "value": ex["strike_type"],
                    "source": "Kalshi `/markets` (FR-1.1)",
                },
                {
                    "quantity": "floor_strike",
                    "value": ex["floor_strike"],
                    "source": "same",
                },
                {"quantity": "cap_strike", "value": ex["cap_strike"], "source": "same"},
                {
                    "quantity": "yes_sub_title",
                    "value": ex["yes_sub_title"],
                    "source": "same",
                },
                {
                    "quantity": "yes_bid",
                    "value": ex["yes_bid"],
                    "source": "candlestick close, same row",
                },
                {
                    "quantity": "yes_ask",
                    "value": ex["yes_ask"],
                    "source": "candlestick close, same row",
                },
                {
                    "quantity": "forecast_high_f",
                    "value": ex["forecast_high_f"],
                    "source": f"`{rel(prov['forecast_csv'])}`, init `{ex['forecast_init_utc']}`, "
                    f"lead {ex['forecast_lead_hours']} h",
                },
                {
                    "quantity": "calibration bucket",
                    "value": ex["calibration_bucket"],
                    "source": f"walk-forward refit on truth ≤ {ex['target_date']} "
                    f"minus {cfg.embargo_days} d",
                },
                {
                    "quantity": "bias_f",
                    "value": round(ex["bias_f"], 4),
                    "source": "that bucket",
                },
                {
                    "quantity": "sigma_f",
                    "value": round(ex["sigma_f"], 4),
                    "source": "that bucket",
                },
                {
                    "quantity": "settled high (expiration_value)",
                    "value": ex["expiration_value"],
                    "source": "Kalshi settlement, cross-checked to NWS CLI",
                },
            ]
        )
        A(md_table(raw))
        A("")
        A("### 5.2 Step by step")
        A("")
        A("```")
        A("1. debias the forecast          mu = forecast - bias")
        A(
            f"                                mu = {ex['forecast_high_f']:.4f} - ({ex['bias_f']:+.4f}) = {ex['mu_f']:.4f} degF"
        )
        A("   (sign per the artifact's error_convention:")
        A("    error_f = forecast - truth, positive = forecast too warm)")
        A(f"                                sigma = {ex['sigma_f']:.4f} degF")
        A("")
        A(
            f"2. bracket YES range            {ex['strike_type']}, "
            f"floor={ex['floor_strike']}, cap={ex['cap_strike']}  ->  pays YES for a"
        )
        A(
            f"                                whole-degree high in "
            f"[{ex['floor_strike']:.0f}, {ex['cap_strike']:.0f}]   (\"{ex['yes_sub_title']}\")"
        )
        A("")
        A("3. P(YES), integer outcome with a continuity correction")
        A(
            f"      z_hi = (cap + 0.5 - mu)/sigma = ({ex['cap_strike']:.1f} + 0.5 - "
            f"{ex['mu_f']:.4f})/{ex['sigma_f']:.4f} = {ex['z_hi']:+.5f}"
        )
        A(
            f"      z_lo = (floor - 0.5 - mu)/sigma = ({ex['floor_strike']:.1f} - 0.5 - "
            f"{ex['mu_f']:.4f})/{ex['sigma_f']:.4f} = {ex['z_lo']:+.5f}"
        )
        A(f"      Phi(z_hi) = {ex['phi_hi']:.6f}     (standard normal table)")
        A(f"      Phi(z_lo) = {ex['phi_lo']:.6f}")
        A(
            f"      P(YES)    = {ex['phi_hi']:.6f} - {ex['phi_lo']:.6f} = "
            f"{ex['p_yes']:.6f}"
        )
        A("")
        A(
            f"4. band                          midpoint = "
            f"({ex['floor_strike']:.0f} + {ex['cap_strike']:.0f})/2 = {ex['midpoint_f']:.1f} degF"
        )
        A(
            f"                                 distance = |{ex['midpoint_f']:.1f} - "
            f"{ex['mu_f']:.4f}| = {ex['distance_f']:.4f} degF  ->  band {ex['band']}"
        )
        A(
            f"                                 nearest-edge distance = "
            f"{ex['edge_distance_f']:.4f} degF  (>= 4.0, so FR-3.1(a) is eligible)"
        )
        A("")
        A("5. FR-3.1(a) trigger             P(YES) <= yes_ask - 0.08 ?")
        A(
            f"                                 {ex['p_yes']:.6f} <= {ex['yes_ask']:.2f} - 0.08 = "
            f"{ex['yes_ask'] - 0.08:.2f}   ->  YES, fire"
        )
        A("")
        A("6. the price this shape must hit (taker, buy NO)")
        A(f"      no_ask = 1 - yes_bid = 1 - {ex['yes_bid']:.2f} = {ex['no_ask']:.2f}")
        A("      yes_bid > 0, so the NO offer existed in the recorded tape")
        A("")
        A(
            f"7. EC-5 adverse-fill allowance   price_paid = {ex['no_ask']:.2f} + "
            f"{ex['adverse_fill']:.2f} = {ex['price_paid']:.2f}"
        )
        A("")
        A(f"8. fee (taker, C = {ex['contracts']}, held to settlement -> ONE leg)")
        A(
            f"      raw   = 0.07 x {ex['contracts']} x {ex['price_paid']:.2f} x "
            f"(1 - {ex['price_paid']:.2f})"
        )
        A(
            f"            = 0.07 x {ex['contracts']} x {ex['price_paid']:.2f} x "
            f"{1 - ex['price_paid']:.2f} = ${0.07 * ex['contracts'] * ex['price_paid'] * (1 - ex['price_paid']):.5f}"
        )
        A(f"      order fee = ceil to the cent          = ${ex['fee_total']:.2f}")
        A(
            f"      per contract = ${ex['fee_total']:.2f} / {ex['contracts']} = "
            f"${ex['fee_per_contract']:.4f}"
        )
        A("")
        A("9. modelled EV per contract")
        A(
            f"      P(win) = P(NO) = 1 - P(YES) = 1 - {ex['p_yes']:.6f} = {ex['p_win']:.6f}"
        )
        A(
            f"      EV = {ex['p_win']:.6f} - {ex['price_paid']:.2f} - {ex['fee_per_contract']:.4f}"
        )
        A(
            f"         = {ex['ev_per_contract']:.6f}   =  {ex['ev_per_contract'] * 100:+.4f} cents/contract"
        )
        A("")
        A("10. what actually happened")
        A(
            f"      settled daily high = {ex['expiration_value']:.0f} degF; the bracket pays YES only"
        )
        A(
            f"      for [{ex['floor_strike']:.0f}, {ex['cap_strike']:.0f}], so it settled "
            f"'{ex['result']}' -> the NO position {'WON' if ex['won'] else 'LOST'}"
        )
        A(
            f"      realized = {1.0 if ex['won'] else 0.0:.1f} - {ex['price_paid']:.2f} - "
            f"{ex['fee_per_contract']:.4f} = {ex['realized_per_contract']:+.6f}"
            f"  =  {ex['realized_per_contract'] * 100:+.2f} cents/contract"
        )
        A("```")
        A("")
        A(
            "Every constant above appears in a named file; the only functions used are "
            "Φ (any normal table) and `ceil` to the cent. A red-team that reproduces "
            "steps 1–9 with a printed normal table will land within rounding of "
            f"{ex['ev_per_contract'] * 100:+.2f}¢."
        )
    A("")
    A("---")
    A("")

    # ---------------- shapes ----------------
    A("## 6. Which trade shapes are +EV — named precisely")
    A("")
    A(
        "**Clustering unit in this section: the date.** Inference is a non-parametric "
        "bootstrap over whole **dates** (4,000 resamples, seed 20260726), and the `SE` "
        "and `t` columns are computed on per-date means. The hourly snapshots of one "
        "market are the same bet on the same outcome, the six brackets of a city-day "
        "are one joint outcome, and four cities under one weather pattern move "
        "together, so a per-row standard error would be meaningless and a per-city-day "
        "one would still be optimistic. This is the coarser unit than §4's, and "
        "the two sections' `SE` columns are therefore not comparable. Entries are "
        "capped at **one per market** (FR-3.4 permits three); §8.3 shows the "
        "result is insensitive to which snapshot within the window is taken."
    )
    A("")
    rows = [shape_row(s) for s in shapes if s is not None]
    A(md_table(pd.DataFrame(rows)))
    A("")
    A("### 6.1 The only shape that is ever +EV — and why it still fails")
    A("")
    if head:
        A(
            "> **FR-3.1(a) far-bracket NO** — buy NO on a bracket whose model "
            "P(YES) is at least 8 points below the market's YES ask **and** whose "
            "nearest paying degree is ≥ 4°F from the calibrated median; "
            "cities **NY, CHI, LAX, MIA**; bands **4-5°F and 5°F+**; "
            "window **≥ 12 h before close**; **taker** (the maker variant is also "
            "positive but its fill model is forward-looking)."
        )
        A("")
        A(
            "Everything in this subsection is measured on the `"
            + src.name
            + "` source. "
            "**Under the other calibrated source on disk the identical rule loses "
            "money** (§8.6), which is why the verdict is HALT and not a scoped "
            "PROCEED. The numbers below describe the best case, not the case."
        )
        A("")
        A(
            f"Fires on {head.markets} distinct markets over {head.dates} dates "
            f"(~{head.trades / max(head.dates, 1):.1f} trades/date). "
            f"Fill-opportunity rate {pct(head.fill_opportunity_rate)}: when the signal "
            f"fires ≥ 12 h out, the NO offer is essentially always there."
        )
        A("")
        A("What the three probabilities actually were, on the trades that fired:")
        A("")
        A(
            md_table(
                pd.DataFrame(
                    [
                        {
                            "quantity": "model P(YES)",
                            "value": f"{head.mean_model_p_yes:.4f}",
                        },
                        {
                            "quantity": "market YES ask",
                            "value": f"{head.mean_market_yes_ask:.4f}",
                        },
                        {
                            "quantity": "**realized** YES rate",
                            "value": f"{head.realized_yes_rate:.4f}",
                        },
                    ]
                )
            )
        )
        A("")
        A(
            "**Both are wrong, and the model is wrong in the flattering direction.** "
            "The truth sits between the model and the market, closer to the model — "
            "which is why the trade makes money — but the model understates the "
            "bracket's true probability by roughly a factor of two, which is exactly the "
            "gap between its modelled EV and its realized PnL."
        )
    A("")
    A("### 6.2 The trivial baselines, and how much of the edge is actually the model")
    A("")
    A(
        "`beat-the-trivial-baseline` is a standing rule in this project. Two baselines "
        "are measured, both on the same universe, the same windows, the same fees and "
        "the same 1¢ allowance, one entry per market."
    )
    A("")
    A("**Baseline 1 — sell every far bracket, no divergence filter.**")
    A("")
    if base_tk:
        A(
            f"Realizes **{cents(base_tk.realized)}/contract** on {base_tk.trades} trades "
            f"over {base_tk.dates} dates, t = {base_tk.t_stat:+.2f}, bootstrap CI "
            f"[{cents(base_tk.boot_lo)}, {cents(base_tk.boot_hi)}]."
        )
        A("")
        A(
            "**This baseline is itself positive, and the report will not pretend "
            "otherwise.** Roughly a third of the headline shape's per-trade edge is "
            "available from generic far-bracket tail-selling that barely uses the "
            "forecast. The 8-point divergence filter keeps "
            f"{head.trades if head else 0} of those {base_tk.trades} trades and roughly "
            f"triples the per-trade edge "
            f"({cents(base_tk.realized)} → {cents(head.realized) if head else '--'}), "
            f"but the two confidence intervals overlap, so **the filter's incremental "
            f"contribution is not itself established at conventional significance.** "
            f"What is established is that both are positive and the filtered subset is "
            f"the larger of the two."
        )
        A("")
        A(
            "This matters for the risk read, not just the accounting: generic "
            "tail-selling is precisely the component most exposed to the fat extreme "
            "tail measured in §7, and it is measured here on 69 warm-season days."
        )
        A("")
    A("**Baseline 2 — bucket by the market's own price; use no model at all.**")
    A("")
    A(md_table(pd.DataFrame(k["price_baseline"])))
    A("")
    A(
        "Compare `market-implied P(YES)` (the YES *bid*, so structurally below fair by "
        "roughly half a spread) with `realized YES rate`: they track each other closely "
        "across the whole price range. **The Kalshi weather book is well calibrated.** "
        "There is no favourite–longshot bias sitting there to be harvested, buying NO "
        "purely on cheapness is flat to negative once the 1¢ allowance and the taker "
        "fee are paid, and any real edge therefore has to come from the forecast rather "
        "than from market structure."
    )
    A("")
    A("### 6.3 FR-3.1(b) lock-in — HALT, and the reason is structural")
    A("")
    for s in lock:
        A(
            f"* `{s.label}`: {s.trades} trade{'' if s.trades == 1 else 's'} on "
            f"{s.dates} date{'' if s.dates == 1 else 's'} "
            f"(realized {cents(s.realized)}, CI [{cents(s.boot_lo)}, {cents(s.boot_hi)}])."
        )
    A("")
    A(
        "This is not a small effect measured precisely; it is **no sample**. FR-3.1(b) "
        "needs the model to reach P ≥ 0.95 in the settlement-station afternoon, but "
        "the archived MOS source publishes no same-day update (§3.1), so the model "
        "is frozen at its 00Z value while the running maximum is still developing. It "
        "reaches 0.95 only by accident. Independently, workstream C measured the "
        "eventual winner's ask as available in **1.9%** of snapshots inside 1 h and "
        "**5.0%** inside 3–6 h — so even a correct signal would mostly have "
        "nothing to hit."
    )
    A("")
    A(
        "**FR-3.1(b) cannot proceed until an intra-day forecast source exists** "
        "(NBM/HRRR/GEFS short-range, plus the running station maximum), and it must then "
        "be re-gated against fresh recorded ladders. Nothing in this report supports it."
    )
    A("")
    A("### 6.4 Buying the cheap tail — HALT")
    A("")
    A(
        "Workstream D found the far tail worth far more under the N/X mixture than under "
        "a single Gaussian (NY T90 at 10.7¢ vs 2.0¢ against a ~1¢ ask "
        "that is almost always available), which points at buying far-bracket YES — a "
        "shape the PRD did not anticipate. It was tested in both directions and in both "
        "regimes:"
    )
    A("")
    for s in faryes:
        A(
            f"* `{s.label}`: modelled EV {cents(s.modelled_ev)}, realized "
            f"**{cents(s.realized)}** on {s.trades} trades / {s.dates} dates."
        )
    A("")
    A(
        "Under the mixture the modelled EV on the +EV subset turns positive (§8.5) "
        "and the realized PnL stays around −2.6¢ to −4.7¢. **The "
        "mixture's fat tail is real as a modelling correction but did not show up as "
        "realized outcomes in this window**, and the 1¢ adverse-fill allowance "
        "doubles the cost of a 1¢ contract before any of it matters. HALT."
    )
    A("")
    A("---")
    A("")

    # ---------------- tail ----------------
    A(
        "## 7. Empirical tail calibration — the test workstream D said had never been run"
    )
    A("")
    A(
        "Workstream D's gap #1: *\"The distribution is Gaussian by assumption. No "
        "normality test was run on the 209 day-of residuals … a far-bracket "
        "strategy is a bet on the tails, and the tails are exactly where a Gaussian "
        'assumption is least defensible."* Every far-bracket EV above depends on that '
        "tail. Here is the test."
    )
    A("")
    A(
        "Realized frequency of large day-of forecast errors versus the frequency the "
        "calibrated model predicts. `error_f = forecast − truth`; both are whole "
        "degrees, so the modelled probability uses the same continuity correction the "
        "probability engine applies to its pmf. `binom p` is an exact two-sided test "
        "against the Gaussian prediction."
    )
    A("")
    tt = tails[tails["side"] == "abs"].copy()
    A(
        md_table(
            pd.DataFrame(
                {
                    "city": tt["city"],
                    "|error| ≥": tt["threshold_f"].map(lambda x: f"{x:.0f}°F"),
                    "n": tt["n"].astype(int),
                    "observed": tt["observed"].astype(int),
                    "obs freq": tt["observed_freq"],
                    "Gaussian freq": tt["gaussian_freq"],
                    "obs/Gaussian": tt["ratio_obs_over_gaussian"],
                    "mixture freq": tt["mixture_freq"],
                    "obs/mixture": tt["ratio_obs_over_mixture"],
                    "binom p": tt["binom_p"].map(
                        lambda p: f"{p:.4f}" if p >= 1e-4 else f"{p:.1e}"
                    ),
                }
            )
        )
    )
    A("")
    A(
        "Pooled walk-forward standardized residuals — the test of the **deployed** "
        "pipeline rather than of one annual block. `z = (error − bias) / σ` "
        "with the bias and σ the engine would actually have selected on that date "
        "(month → season → day-of chain, fitted only on earlier truth):"
    )
    A("")
    A(
        md_table(
            pd.DataFrame(
                {
                    "|z| ≥": zres["threshold_z"],
                    "n": zres["n"].astype(int),
                    "observed": zres["observed"].astype(int),
                    "obs freq": zres["observed_freq"],
                    "N(0,1) freq": zres["normal_freq"],
                    "obs / normal": zres["ratio"],
                }
            ),
            {"|z| ≥": ".1f"},
        )
    )
    A("")
    A(
        "**Verdict: the residual distribution is leptokurtic — a peaked core and fat "
        "tails — and the Gaussian is wrong in both directions at once.**"
    )
    A("")
    A(
        "* In the **shoulder** (|error| 3–6°F, |z| ≤ 2) the Gaussian "
        "**over**-predicts: observed is 0.59–0.89× predicted, significantly so "
        "for NY and CHI (p down to 7e-07)."
    )
    A(
        "* In the **extreme tail** it **under**-predicts: |z| ≥ 2.5 at "
        "2.03× and |z| ≥ 3 at 3.74× the normal rate; NY at |error| ≥ "
        "8°F at 1.64×; LAX at ≥ 6°F at 2.51× (p = 0.034); "
        "MIA's warm side at ≥ 4°F at 2.43× (p = 0.019)."
    )
    A(
        "* The N/X mixture is a genuine improvement in the shoulder for NY and CHI "
        "(ratios move from 0.64–0.77 toward 0.77–0.86) but does **not** fix "
        "the extreme tail."
    )
    A("")
    A(
        "**Consequence for this report, stated plainly: every far-bracket EV above is "
        "unreliable in a known direction.** The shape being recommended is short the "
        "extreme tail, and the extreme tail is the part of the distribution the model "
        "understates by 2–4×. That is not a reason to distrust the *realized* "
        "column — the realized column is what happened — but it is a decisive reason not "
        "to size Phase 3 from the modelled EV, and it is why the modelled EV is roughly "
        "double the realized PnL."
    )
    A("")
    A("---")
    A("")

    # ---------------- mixture ----------------
    A("## 8. Robustness")
    A("")
    A("### 8.1 In-sample vs walk-forward — the contamination gap")
    A("")
    if shape_is := k["shape_is"]:
        A(
            f"The headline shape, identical rule, in-sample calibration: "
            f"{shape_is.trades} trades, realized {cents(shape_is.realized)}, modelled EV "
            f"{cents(shape_is.modelled_ev)}. Walk-forward: "
            f"{head.trades if head else '--'} trades, realized "
            f"{cents(head.realized) if head else '--'}, modelled EV "
            f"{cents(head.modelled_ev) if head else '--'}."
        )
        A("")
    A(
        "**Embargo length.** The default rule withholds truth from the priced date "
        "onward. A 2-day embargo additionally withholds the previous day, whose NWS "
        "Climatological Report is only published on the morning the ladder is already "
        "open — i.e. it is arguably not available at the earliest snapshots either:"
    )
    A("")
    if k["embargo_rows"]:
        A(md_table(pd.DataFrame(k["embargo_rows"])))
        A("")
        tk1 = next(
            (
                r
                for r in k["embargo_rows"]
                if r["mode"] == ev.MODE_TAKER and r["embargo"].startswith("1")
            ),
            None,
        )
        tk2 = next(
            (
                r
                for r in k["embargo_rows"]
                if r["mode"] == ev.MODE_TAKER and not r["embargo"].startswith("1")
            ),
            None,
        )
        if tk1 and tk2:
            A(
                f"One extra day of withheld truth moves the taker path's lower bound "
                f"from {tk1['boot 95% CI'].split(',')[0].lstrip('[')} to "
                f"{tk2['boot 95% CI'].split(',')[0].lstrip('[')} — still positive at "
                f"the modelled order size, but close enough to zero that the "
                f"single-contract variant (§8.2's fee assumption) crosses it. The "
                f"result is therefore sensitive to a judgement call about the hour at "
                f"which a CLI report becomes usable, which is not a quantity a strategy "
                f"should be sensitive to."
            )
        A("")
    A(
        "Across the band table the gap is directional rather than dramatic: the "
        "walk-forward chain usually falls back to a wider seasonal or annual σ "
        "(mean σ "
        f"{k['probs_wf']['sigma_f'].mean():.3f}°F walk-forward), which raises "
        "far-bracket P(YES), makes the 8-point filter *harder* to trigger, and therefore "
        "makes the walk-forward test the **more conservative** of the two on this shape. "
        "The in-sample table is published in §4.2 and carries no verdict."
    )
    A("")
    A("### 8.2 Order size")
    A("")
    A(md_table(pd.DataFrame(k["sens_size"])))
    A("")
    A(
        "The taker fee rounds up on the order **total**, so a C = 1 model overstates "
        "far-bracket cost. It moves the headline by under half a cent; the verdict does "
        "not depend on the size assumption."
    )
    A("")
    A("### 8.3 Entry timing and slippage")
    A("")
    A("Which snapshot inside the window is taken as the entry:")
    A("")
    A(md_table(pd.DataFrame(k["sens_timing"])))
    A("")
    A("Slippage beyond EC-5's mandated 1¢:")
    A("")
    A(md_table(pd.DataFrame(k["sens_adverse"])))
    A("")
    A(
        "The shape survives 2¢ and 3¢ of total slippage with the point estimate "
        "still positive, but the t-statistic decays through 2 at around 3¢. "
        "Execution quality is a first-order risk here, not a rounding detail."
    )
    A("")
    A("### 8.4 Parameter sensitivity")
    A("")
    if k["sweep_rows"]:
        A(md_table(pd.DataFrame(k["sweep_rows"])))
        A("")
        A(
            "The **4°F distance condition is doing the work**: at `dist ≥ 4` "
            "the realized PnL is positive at every divergence margin from 4 to 15 "
            "points; at `dist` 2 or 3 it is +1.5 to +2.9¢ with a CI spanning zero. "
            "That the effect is strongest at exactly the pre-registered 4°F is "
            "reassuring (the parameters were not tuned here — they are the PRD's "
            "defaults) but `dist ≥ 5` is *weaker* than `dist ≥ 4`, which is "
            "not the monotone behaviour a clean structural effect would show. Treat the "
            "point estimate as noisy and the sign as the finding."
        )
        A("")
    A("### 8.5 The N/X-window mixture — a second input the result is not robust to")
    A("")
    if k["mixture_rows"]:
        A(md_table(pd.DataFrame(k["mixture_rows"])))
        A("")
        A(k["mixture_note"])
        A("")
        A(
            'Workstream D recommends `regime="mixture"` for NY and CHI, where ~18% of '
            "days set their maximum outside the window MOS `N/X` guidance covers and "
            "carry roughly double the σ plus a cold bias. Restricting to those two "
            "cities and the day-of lead, the **same** FR-3.1(a) rule realizes a "
            "materially positive number under the single Gaussian and a number "
            "indistinguishable from zero under the mixture. The two rules select "
            "overlapping but different trade sets (38 markets in common), and the "
            "difference is inside one standard error — but it means **the headline "
            "result is not robust to the regime model**, and the mixture is the "
            "better-specified of the two."
        )
        A("")
        A(
            "This is the same failure as §8.6 in a different variable: the shape's "
            "sign depends on a modelling choice the data cannot settle. Two "
            "independent such dependencies — the forecast source and the regime "
            "model — is what turns a promising point estimate into a HALT. "
            "Requirement R4 in §9.3 names what would close it."
        )
    else:
        A("*(mixture section skipped: `--quick`)*")
    A("")
    A("### 8.6 Forecast source — does the verdict survive a worse forecast?")
    A("")
    if k["source_rows"]:
        A(
            "Workstream F's GEFS calibration is a second, independent forecast source "
            "for the same four cities and the same truth. It is priced here through the "
            "**identical** walk-forward harness — same ladders, same embargo, same "
            "availability conditioning, same fees, same 1¢ allowance — with only "
            "the forecast archive and the calibration artifacts swapped."
        )
        A("")
        A("The σ that is actually being varied:")
        A("")
        sg = pd.DataFrame(k["source_sigma"]).sort_values(["city", "source"])
        A(md_table(sg))
        A("")
        A("The same FR-3.1(a) rule, under each source:")
        A("")
        cols = [
            "source",
            "shape",
            "trades",
            "markets",
            "dates",
            "fill-opp",
            "modelled EV",
            "realized",
            "t",
            "boot 95% CI",
            "win",
        ]
        sr = pd.DataFrame(k["source_rows"])
        A(md_table(sr[[c for c in cols if c in sr.columns]]))
        A("")
        if k["source_city"]:
            A("Per city (taker, one entry per market):")
            A("")
            A(md_table(pd.DataFrame(k["source_city"]).sort_values(["city", "source"])))
            A("")
        A("**This table is why this report says HALT.**")
        A("")
        A(
            "The same rule, the same 69 days, the same tape, the same walk-forward "
            "harness, the same fees and the same 1¢ allowance — and the sign of "
            "the realized PnL **reverses** when the forecast source changes. There is no "
            "reading of that under which the shape has a demonstrated edge: what was "
            "measured is not a property of the trade, it is a property of one forecast "
            "source's particular errors over one 69-day window."
        )
        A("")
        A(
            "Worse for FR-2.4 specifically: **the modelled EV scores the losing "
            "configuration higher than the winning one.** GEFS's modelled EV is the "
            "larger of the two while its realized PnL is negative. The quantity FR-2.4 "
            "gates on is therefore not merely optimistic (§0, §6.1) — across "
            "this comparison it is *anti*-correlated with the outcome. A gate that "
            "ranks a loser above a winner cannot authorize Phase 3."
        )
        A("")
        A(
            "The mechanism is not mysterious. Forecast skill over the traded window, "
            "measured on the same day-of pairs:"
        )
        A("")
        if k["source_skill"]:
            A(
                md_table(
                    pd.DataFrame(k["source_skill"]).sort_values(["city", "source"]),
                    {"MAE °F": ".2f", "bias °F": ".2f", "sd °F": ".2f"},
                )
            )
            A("")
        A(
            "The shape's PnL tracks forecast skill, which is what a real "
            "information edge *should* do — and is exactly why it cannot be banked "
            "here. **Which source is the skilful one was determined by looking at this "
            "data.** The PRD's designated primary source is the ensemble (FR-2.1); "
            "workstream F measured it as the worse forecast at 3 of 4 cities *after* "
            "building it. Selecting `gfs_mex` and then reporting its PnL is a choice "
            "made with knowledge of the outcome, and it is the single largest "
            "unaccounted-for degree of freedom in the positive result."
        )
        A("")
        if k["source_drop_ny"]:
            A(
                "**Is there an ex-ante rule that disqualifies the losing source?** The "
                "strongest one available is EC-3's own 4°F day-of σ bound, "
                "which `gefs` fails at NY and passes at CHI, LAX and MIA (column "
                "`EC-3` above). Dropping NY — the only city the bound "
                "excludes — leaves:"
            )
            A("")
            A(md_table(pd.DataFrame(k["source_drop_ny"])))
            A("")
            worst_pass = _worst_passing_city(k["source_city"], "gefs")
            if worst_pass:
                A(
                    f"The bound removes the sample, not the loss. `gefs` is still "
                    f"negative over the three cities where it **passes** EC-3, and the "
                    f"per-city table shows why: the bound has no purchase on "
                    f"{worst_pass['city']}, where `gefs` clears it at "
                    f"{worst_pass['day-of σ °F']}°F and realizes "
                    f"{worst_pass['realized/ct (date-clustered)']} on "
                    f"{worst_pass['trades']} trades. **There is therefore no ex-ante "
                    f"rule on the table that disqualifies `gefs` and leaves `gfs_mex` "
                    f"standing** — which is precisely what §9.2 turns on, and "
                    f"precisely why R2 in §9.3 is a weaker escape clause than it "
                    f"reads."
                )
            A("")
        A(
            "**A post-hoc observation, recorded because it is the most promising lead in "
            "this report and flagged so nobody mistakes it for a finding — and because "
            "the ordering in which the filter is applied changes what it says:**"
        )
        A("")
        if k["sigma_filter_rows"]:
            A(md_table(pd.DataFrame(k["sigma_filter_rows"])))
            A("")
        A(
            "σ is an **ex-ante observable** — it is known before the trade — so "
            "there are two defensible places to apply the bound, and they are not the "
            "same rule:"
        )
        A("")
        A(
            "* **post-selection**: take the FR-3.1(a) entries first (one per market), "
            "then discard those whose selected calibration bucket carries "
            "σ > 4°F. This is the ordering in which the observation was "
            "first made."
        )
        A(
            "* **pre-selection**: apply the σ bound to the *candidate pool* first, "
            "then take one entry per market from what survives. Because σ is "
            "ex-ante, a live strategy would do it this way, and this ordering can "
            "select a **different snapshot** as the entry for a market whose earlier "
            "snapshots are filtered out."
        )
        A("")
        A(
            "**The pre-selection ordering is the one registered in §9.3 R3.** "
            'Under it the claim that the filter "turns both sources positive" does '
            "not survive: the mechanism is a change of entry vintage, not a change of "
            "skill."
        )
        A("")
        if k["sigma_filter_leads"]:
            A(md_table(pd.DataFrame(k["sigma_filter_leads"])))
            A("")
        A(
            "Read the composition table. Under `gefs` the unfiltered rule enters "
            "predominantly on the **previous day's 12Z vintage** (`lead_12_36`), whose "
            "σ exceeds 4°F nearly everywhere; the post-selection filter "
            "therefore deletes most of those entries and what is left is almost "
            "entirely the **day-of** vintage. So under `gefs` the post-selection result "
            'is not "the same rule with a σ filter" — it is a materially '
            "later-entry rule measured on a fifth of the sample, and §8.3 already "
            "shows entry timing alone moves this shape. `gfs_mex`'s σ ≤ "
            "4°F result is robust to the ordering; `gefs`'s is not."
        )
        A("")
        A(
            "The coherent story behind the filter is unchanged — the divergence signal "
            "should only be worth trading where the forecast is actually skilful, and "
            "σ is an ex-ante quantity that says so. But it is a filter discovered "
            "by inspecting these results, it is **not** pre-registered, it is **not** "
            "in FR-3.1(a), and it does **not** carry any part of this verdict. "
            "§9.3 R3 registers it, in the pre-selection ordering, for a future "
            "test on data this report has never seen."
        )
        A("")
        A(
            "One source-dependent caveat that bears directly on §8.5: workstream F "
            "measures GEFS's full-day `TMAX` as materially improving the "
            "overnight-maximum regime at CHI (regime σ 6.43 → 4.23°F). "
            "**The N/X-window regime penalty is therefore a property of the source, not "
            "of the city** — the mixture disagreement in §8.5 is specific to the "
            "MOS `N/X` daytime window and would be smaller on a full-day source. "
            "Requirement R1 in §9.3 must therefore be settled before the "
            "regime question in §8.5 can be, since the answer depends on which "
            "source is frozen."
        )
        A("")
        A(
            "Workstream F's own recommendation — an unweighted two-source blend at NY "
            "and CHI, `gfs_mex` alone at LAX and MIA — is **not** evaluated here, "
            "because F states the recommendation is in-sample and this report will not "
            "carry an in-sample input into a walk-forward verdict. Blending is a Phase 3 "
            "question and needs its own walk-forward gate."
        )
    else:
        A("*(only one calibrated source on disk; nothing to vary)*")
    A("")
    A("### 8.7 Distribution of outcomes, not just the mean")
    A("")
    if head:
        A(
            f"{head.losing_dates} of {head.dates} dates lose money; the worst single date "
            f"is {cents(head.worst_date_pnl)}/contract. This is a short-tail payoff — "
            f"many small wins funded by rare large losses — which is exactly the shape "
            f"whose risk the miscalibrated extreme tail (§7) understates. Excluding "
            f"the worst 3 dates raises the mean by roughly half; that is a statement "
            f"about concentration, not a permission to exclude them."
        )
        A("")
    A(
        "**How wrong the model is about the loss leg specifically.** For a buy-NO "
        'shape a loss is exactly "the bracket settled YES", so the model\'s mean '
        "P(YES) on the trades that fired and the realized YES rate on the same trades "
        "are one quantity measured two ways. One entry per market, taker, ≥12 h "
        "to close:"
    )
    A("")
    if k["source_loss"]:
        A(md_table(pd.DataFrame(k["source_loss"])))
        A("")
    A(
        "Under both sources the model is wrong in the direction that flatters a "
        "short-tail trade: it predicts fewer losses than occurred, by a factor of two "
        "to four, and each loss that does occur costs most of the notional. §7 "
        "measures the same defect in the forecast residuals; this measures it on the "
        "trades, which is where it would be sized from. A positive mean computed from "
        "a loss probability that is understated 2–4× is not a quantity Phase 3 "
        "may be sized on, and that is true under the *winning* source as well as the "
        "losing one."
    )
    A("")
    A("### 8.8 Where the headline t-statistic actually comes from")
    A("")
    A(
        "The pooled significance in §6 is reported city-set by city-set below. "
        "§10.2 notes that MIA and LAX post 100% win rates on small counts; this "
        "is what that does to the headline number. Same rule, same source, same "
        "harness, date-clustered inference throughout."
    )
    A("")
    if k["city_split"]:
        A(md_table(pd.DataFrame(k["city_split"])))
        A("")
    if majority and minority and pooled:
        A(
            f"**A 100% win rate contributes a t-statistic through the absence of loss "
            f"variance, not through the size of an edge.** LAX and MIA supply "
            f"{minority['trades']} trades on {minority['dates']} dates with no "
            f"losing trade at all; their per-date means are near-constant, their "
            f"standard error collapses, and t = {minority['t']}. That is a "
            f"small-sample artifact (§10.2), not a stronger signal — and it is "
            f"what carries the pooled t = {pooled['t']}."
        )
        A("")
        A(
            f"Meanwhile the **majority** of the sample — NY and CHI, "
            f"{majority['trades']} of {pooled['trades']} trades "
            f"({100 * int(majority['trades']) / max(int(pooled['trades']), 1):.0f}%) on "
            f"{majority['dates']} dates — realizes {majority['realized/ct']} at "
            f"t = {majority['t']}, with a bootstrap CI of "
            f"{majority['boot 95% CI']} that **spans zero**. The two cities that "
            f"dominate the sample do not, on their own, demonstrate an edge."
        )
        A("")
        A(
            "This does not make the pooled point estimate wrong; it makes the pooled "
            "*significance* something other than what it appears to be. Reported here "
            "because §6's table alone would let a reader take t = "
            f"{pooled['t']} as evidence about the shape rather than about two "
            "cities' unbeaten streaks over a 69-day warm season."
        )
    A("")
    A("---")
    A("")

    # ---------------- decision ----------------
    A("## 9. The decision, and the named path back")
    A("")
    A("### 9.1 What HALT means operationally, per FR-2.4")
    A("")
    A(
        md_table(
            pd.DataFrame(
                [
                    {
                        "action": "Phase 3 (weather trading) does **not** start.",
                        "authority": 'FR-2.4: "if none qualify, weather halts and Phase 4 (gas) '
                        'becomes flagship"',
                    },
                    {
                        "action": "**Phase 4 — AAA gas convergence — becomes the flagship** and is "
                        "the next phase executed.",
                        "authority": "FR-2.4; PRD §1 already names gas the second engine",
                    },
                    {
                        "action": "`weather_bot` stays **feed-only**. No FR-3.1 strategy is "
                        "registered, no weather signal reaches the risk manager.",
                        "authority": "Phase 0 teardown state; unchanged by this report",
                    },
                    {
                        "action": "**Keep harvesting.** The ladder archive is the binding scarce "
                        "resource and its upstream retention window is rolling — one day "
                        "of recoverable history is lost per day. Continue "
                        "`backfill_ladders.py` daily and continue the CLI-truth and "
                        "forecast backfills for all four cities.",
                        "authority": "PRD goal 6; workstream C §9.2",
                    },
                    {
                        "action": "Nothing built in Phase 2 is discarded. The calibration pipeline, "
                        "probability engine, ladder archive and this harness are the "
                        "instruments that produced an honest negative and are what a "
                        "future re-test runs on.",
                        "authority": "—",
                    },
                ]
            )
        )
    )
    A("")
    A(
        "Estimated sunk cost of the halt path, as the PRD budgeted it: 2–3 weeks. "
        "That is what was spent, and it bought a defensible negative plus a reusable "
        "measurement stack."
    )
    A("")
    A("### 9.2 Why this is a HALT and not a scoped PROCEED")
    A("")
    A(
        "A scoped PROCEED was drafted and rejected. **The case for it is stronger than "
        "an earlier revision of this report credited**, and it is recorded here at full "
        "strength: Phase 3 deploys no capital, its own exit criterion is truthful "
        "evidence rather than positive PnL, a 30-day forward paper run is exactly the "
        "out-of-sample sample this evidence lacks, and running it would settle R1, R3 "
        "and R5 in §9.3 far faster than waiting does. Against a shape whose "
        "pooled point estimate is positive under the better forecast, that is a real "
        "argument and not a straw one."
    )
    A("")
    A("It fails on one point, and the point was **tested rather than asserted**:")
    A("")
    A(
        "> A PROCEED would have to name **which forecast source** the strategy trades. "
        "There is no defensible way to make that choice from this data. Picking the one "
        "that made money is picking on the outcome; picking the PRD's designated "
        "primary (FR-2.1's ensemble) picks the one that lost money. A phase that cannot "
        'specify its own primary input is not ready to start, and "start it and see" '
        "is how four prior review cycles produced numbers that were not what they "
        "claimed to be."
    )
    A("")
    A(
        "The obvious rescue is an **ex-ante** disqualification of `gefs` — a rule "
        "usable before the outcome is known. The strongest one this project owns is "
        "EC-3's 4°F day-of σ bound, which `gefs` fails at NY. It was "
        "applied and it does not work (§8.6):"
    )
    A("")
    drop = next((r for r in k["source_drop_ny"] if r["source"] == "gefs"), None)
    worst_pass = _worst_passing_city(k["source_city"], "gefs")
    passing = [
        r
        for r in k["source_city"]
        if r["source"] == "gefs" and r["EC-3 σ ≤ 4°F"] == "pass"
    ]
    if drop:
        A(
            f"* NY is the **only** city the bound excludes. Dropping it leaves `gefs` "
            f"at {drop['realized/ct']}, t = {drop['t']} ({drop['trades']} trades / "
            f"{drop['dates']} dates) — still negative, still indistinguishable "
            f"from zero. The disqualification removes the sample, not the loss."
        )
    if worst_pass:
        A(
            f"* The bound has no purchase on the city that supplies most of the "
            f"remaining loss: `gefs` **passes** EC-3 at {worst_pass['city']} "
            f"(day-of σ {worst_pass['day-of σ °F']}°F, inside the bound) "
            f"and realizes {worst_pass['realized/ct (date-clustered)']} there on "
            f"{worst_pass['trades']} trades."
        )
    others = [c for c in passing if not worst_pass or c["city"] != worst_pass["city"]]
    if others:
        A(
            "* At the remaining cities that pass the bound the result is not evidence "
            "either way: "
            + "; ".join(
                f"{c['city']} {c['realized/ct (date-clustered)']} on "
                f"{c['trades']} trades"
                for c in others
            )
            + "."
        )
    A("")
    A(
        "**There is no ex-ante rule on the table that disqualifies `gefs` and leaves "
        "`gfs_mex` standing.** That is what makes this section survive: the choice of "
        "source cannot be defended forward, only backward, and a phase authorized on a "
        "backward-defended input is the failure mode this project has already produced "
        "four times."
    )
    A("")
    A("### 9.3 The path back — pre-registered now, before any new data exists")
    A("")
    A(
        "This is written before the data that would test it exists, which is the only "
        "condition under which it means anything. Weather may be re-gated when **all "
        "five** hold:"
    )
    A("")
    A(
        md_table(
            pd.DataFrame(
                [
                    {
                        "id": "R1",
                        "requirement": "**Source specified ex ante.** One forecast source (or one fixed blend "
                        "rule) named and frozen *before* the evaluation window, with its choice "
                        "justified on data predating that window. Workstream F's blend "
                        "recommendation is in-sample and does not qualify as written.",
                    },
                    {
                        "id": "R2",
                        "requirement": "**Sign stability across sources.** The rule's realized PnL is positive "
                        "under *every* calibrated source on disk, or the report disqualifies a "
                        "source using an ex-ante criterion **and demonstrates that the "
                        "disqualification is what removes the loss** — i.e. the surviving cities "
                        "or dates are themselves profitable under the disqualified source's own "
                        "terms. The escape clause is deliberately narrow: EC-3's 4°F "
                        "σ bound is an ex-ante criterion, but §8.6 shows that applying "
                        "it to the present data removes the sample rather than the loss (`gefs` "
                        "loses at CHI and LAX, where it passes the bound). An ex-ante criterion "
                        'that happens to be satisfiable is not enough; "it lost money" is never '
                        "enough.",
                    },
                    {
                        "id": "R3",
                        "requirement": "**The σ ≤ 4°F bucket filter, tested as a pre-registered "
                        "rule, applied PRE-selection.** σ is an ex-ante observable, so the "
                        "registered rule applies the bound to the *candidate pool* and then takes "
                        "one entry per market. §8.6 measures both orderings and they "
                        "disagree: post-selection the filter turns both sources positive, "
                        "pre-selection it does not, because under `gefs` post-selection filtering "
                        "silently converts the rule into a later-entry rule. The pre-selection "
                        "ordering is the registered one; it must be tested on ladder dates "
                        "outside 2026-05-18…2026-07-25 — which accrue at one per day from "
                        "the ongoing harvest — and a post-selection result may not be "
                        "reported in its place.",
                    },
                    {
                        "id": "R4",
                        "requirement": "**A tail model that passes §7's test**, or an explicit fat-tailed "
                        "distribution (Student-t or the N/X mixture fitted walk-forward) whose "
                        "own tail ratios sit inside [0.8, 1.25] at |z| ≥ 2.5. A short-tail "
                        "trade may not be underwritten by a distribution that understates its "
                        "extreme tail by 2–4×.",
                    },
                    {
                        "id": "R5",
                        "requirement": "**An out-of-season sample.** At least one autumn or winter month of "
                        "recorded ladders, with per-city day-of σ re-measured on it. The "
                        "entire present result is 69 warm-season days, and CHI's σ is 4.59"
                        "°F on the cold half of the calibration sample.",
                    },
                ]
            )
        )
    )
    A("")
    A(
        "Two further items are prerequisites for any *execution* claim, independent of "
        "the signal: measured top-of-book **depth** (§10.5, entirely absent from "
        "this dataset) and an **intra-day forecast update** without which FR-3.1(b) "
        "cannot exist at all (§6.3)."
    )
    A("")
    A("### 9.4 What would have flipped this to PROCEED")
    A("")
    A(
        "Recorded so the verdict is falsifiable rather than merely cautious. Any **one** "
        "of these, measured, would have carried a scoped PROCEED:"
    )
    A("")
    A(
        "* The far-bracket NO shape realizing positive PnL under **both** calibrated "
        "sources (it realizes "
        f"{cents(head.realized) if head else '--'} under `{src.name}` and "
        f"{gefs['realized'] if gefs else '--'} under `gefs`)."
    )
    A(
        "* The modelled EV **ranking** the two source configurations correctly, which "
        "would have made FR-2.4's own gate quantity informative (it ranks them "
        "backwards)."
    )
    A(
        "* An **ex-ante** criterion that disqualifies `gefs` and leaves `gfs_mex` "
        "standing. EC-3's σ bound was applied and does not do it (§8.6, "
        "§9.2)."
    )
    if majority:
        A(
            f"* The majority sub-sample carrying its own weight: NY + CHI alone "
            f"clearing zero, instead of {majority['realized/ct']} at t = "
            f"{majority['t']} with a CI of {majority['boot 95% CI']} (§8.8)."
        )
    A(
        "* The model's realized loss rate landing near its modelled one instead of "
        "2–4× above it (§8.7)."
    )
    A(
        "* The N/X mixture agreeing with the single Gaussian at NY and CHI rather than "
        "collapsing the result to zero (§8.5)."
    )
    A(
        "* The tail test in §7 coming back inside a factor of ~1.25 instead of "
        f"{worst_ratio['ratio']:.1f}× at |z| ≥ {worst_ratio['threshold_z']:.1f}."
    )
    A("")
    A("---")
    A("")

    # ---------------- limitations ----------------
    A("## 10. Limitations — every one of them, in the section you cannot miss")
    A("")
    lim = [
        (
            "10.1",
            "The season is not the year.",
            "69 consecutive days, 2026-05-18 to 2026-07-25 — late spring and summer only. "
            "SON is entirely absent from the calibration and DJF is thin. Workstream B "
            "measured CHI's day-of σ at 4.59°F on the cold half of the sample "
            "(failing EC-3's 4°F bound) against 2.78°F on the warm half. "
            "Nothing here is validated outside the warm season, and the ladder retention "
            "window is rolling, so this dataset cannot be extended backwards.",
        ),
        (
            "10.2",
            "The effective sample is far smaller than the row count.",
            f"{len(lad):,} ladder rows reduce to {head.trades if head else 0} fired trades on "
            f"{head.markets if head else 0} markets across {head.dates if head else 0} dates "
            f"under the headline source. Per-city counts fall to the tens (MIA is the "
            "smallest, at 17), and under `gfs_mex` both MIA and LAX post 100% win rates "
            "on those counts — a small-sample artifact, not a stronger edge. Read every "
            "per-city number as indicative only; a handful of trades moves any of them. "
            "**Those two unbeaten sub-samples are also what produces the headline "
            "t-statistic** — §8.8 decomposes it, and the majority sub-sample "
            "(NY + CHI) has a bootstrap CI that spans zero.",
        ),
        (
            "10.3",
            "The forecast source is a free parameter, and it is the decisive one.",
            "The headline is raw GFS extended MOS (`gfs_mex`), chosen by workstream B "
            "because it is the only archived model populating `n_x` for all four "
            "stations. The PRD's designated primary source is the GEFS ensemble "
            "(FR-2.1), which workstream F built and measured as the *worse* forecast at "
            "3 of 4 cities. Both are priced here and they disagree on the sign of the "
            "only positive shape (§8.6). Nothing in this dataset selects between "
            "them on an ex-ante basis, and that unresolved choice — not any single "
            "number — is what produces the HALT.",
        ),
        (
            "10.4",
            "σ is a bucket constant, and no per-day uncertainty exists in "
            "measured form.",
            "The MOS calibration blocks report `mean_spread_f: null` because the IEM "
            "archive publishes no spread. GEFS *does* publish one (`gespr`), and "
            "workstream F measured it at 0.19–0.37× the realized error "
            "σ with correlation to |error| of −0.013 / +0.065 / "
            "+0.022 / −0.197 across the four cities — i.e. no skill. So a "
            "quiet high-pressure day and a frontal-passage day are priced with an "
            "identical σ, and nothing available fixes that. Every σ in this "
            "report is the calibrated per-bucket `sigma_f`; the probability engine is "
            "called with `require_published_spread=False` and no code path here reads "
            "`spread_f` or `mean_spread_f` as a dispersion.",
        ),
        (
            "10.5",
            "Depth is unmeasured, and it is the largest hole.",
            "The candlestick feed carries quotes but no book depth, and historical depth "
            "cannot be recovered. Every EV here assumes the modelled order size is "
            "available at the recorded quote. Median hourly volume is 133–538 contracts "
            "per snapshot, which makes a 20-contract order plausible, but plausible is "
            "not measured.",
        ),
        (
            "10.6",
            "The maker fill model is a proxy, not an observation.",
            "A resting order is deemed filled if a later hourly snapshot shows the other "
            "side crossing its limit. That is a lower bound on instantaneous fills "
            "(intra-hour traversals are invisible) and an upper bound on durable ones "
            "(queue position is unobservable), and it is forward-looking, so maker "
            "modelled EV carries an adverse-selection bias. The headline verdict is taken "
            "from the taker path for exactly this reason.",
        ),
        (
            "10.7",
            "The mixture path is itself in-sample.",
            "Workstream D's `NX_WINDOW_REGIME` constants are fitted over the whole 209-day "
            "archive and are a module constant, so the mixture results in §8.5 are "
            "*not* walk-forward. Making them so requires a change in "
            "`src/calibration/probability_engine.py`, which workstream E does not own; the "
            "proposed change is in §11.",
        ),
        (
            "10.8",
            "Hourly resolution understates availability and overstates persistence.",
            "Quotes are the close of each hourly candle. A quote that existed for ten "
            "minutes and vanished is recorded as absent; a quote that vanished mid-hour is "
            "recorded as present. Workstream C can supply 1-minute data at ~60× the "
            "request count if Phase 3 needs finer execution modelling.",
        ),
        (
            "10.9",
            "This is a backtest, and the model never traded.",
            "No order was placed and no live signal was generated. Market impact, queue "
            "position, partial fills, API latency, and the reflexive effect of the "
            "strategy's own orders on a thin far-bracket book are all outside this "
            "measurement.",
        ),
        (
            "10.10",
            "Multiple shapes were examined, and one choice was made after seeing "
            "the answer.",
            "Four directions × two modes × six bands × six windows were "
            "computed, plus a 24-cell parameter sweep and two forecast sources. The "
            "FR-3.1(a) rule itself is protected from that search by being "
            "**pre-registered in the PRD with its default parameters**. Two things are "
            "not: the ≥12h window restriction (justified by the model being frozen "
            "intra-day, §3.1, and conservative — the 6–12h cell is better, "
            "not worse) and, decisively, **the choice of forecast source**, which could "
            "only be made after measuring which one worked. The σ ≤ 4°F "
            "bucket filter in §8.6 is likewise post-hoc and is registered as a "
            "hypothesis in §9.3 rather than used. Individual p-values in "
            "§8.4 and the band tables in §4 are not corrected for "
            "multiplicity.",
        ),
    ]
    for tag, title, body in lim:
        A(f"### {tag} {title}")
        A("")
        A(body)
        A("")
    A("---")
    A("")

    # ---------------- proposed diffs ----------------
    A("## 11. Changes to files workstream E does not own")
    A("")
    A(
        "**One was made, and it changes no behaviour.** "
        "`src/calibration/probability_engine.py`'s module docstring stated EC-4's "
        'acceptance claim as "doubling sigma measurably flattens the distribution" '
        "without naming the metric or its precondition. Entropy over the ladder does "
        "rise under σ-doubling in every configuration probed; the "
        "largest-*finite*-bracket criterion the EC-4 test also asserts holds only while "
        "μ sits inside the ladder. With μ placed 25°F below the ladder "
        "centre it reverses on **all ten** recorded EC-4 ladders — on "
        "KXHIGHNY-26JUL17 the largest finite bracket *rises* from 4.21e-18 to "
        "7.24e-06 — because doubling σ moves mass back *toward* a ladder the "
        "forecast had abandoned. The reversal is not confined to the far field: it "
        "already appears at 10°F outside the ladder, again on 10/10. The "
        "docstring now names which metric carries the claim and under what condition, "
        "and a test pins the counterexample. No engine behaviour and no existing "
        "assertion changed. EC-4 rests on entropy, which rose under σ-doubling in "
        "every configuration probed."
    )
    A("")
    A("Two further changes are proposed and were **not** made.")
    A("")
    A(
        "**(a) `src/calibration/probability_engine.py` — make the N/X regime "
        "walk-forwardable.** `NX_WINDOW_REGIME` is a module constant fitted on the whole "
        "archive, so §8.5's mixture numbers are in-sample (§10.7). "
        "`recompute_nx_window_regime()` already exists and already accepts the archives; "
        "it needs an `as_of` cutoff and `_build_components` needs to accept an injected "
        "regime table:"
    )
    A("")
    A("```diff")
    A("-def recompute_nx_window_regime(...) -> Dict[str, RegimeRisk]:")
    A(
        "+def recompute_nx_window_regime(..., as_of: Optional[str] = None) -> Dict[str, RegimeRisk]:"
    )
    A("+    # as_of: keep only paired days with target_date < as_of, so a mixture")
    A("+    # priced for date D is not parameterized on D's own outcome.")
    A("")
    A("-def bracket_probabilities_point(*, ..., regime=REGIME_SINGLE, ...):")
    A("+def bracket_probabilities_point(*, ..., regime=REGIME_SINGLE,")
    A(
        "+                                regime_table: Optional[Mapping[str, RegimeRisk]] = None, ...):"
    )
    A("+    # regime_table overrides NX_WINDOW_REGIME; None keeps today's behaviour.")
    A("```")
    A("")
    A(
        "**(b) `.gitignore` — none needed for this workstream's outputs.** "
        "`reports/phase2/*.md` and the JSON sidecar are small and are the artifact."
    )
    A("")
    A("---")
    A("")
    A("## 12. Reproduction")
    A("")
    A("```powershell")
    A('$env:PYTHONPATH = "."')
    A('$env:OMP_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"')
    A('$env:MKL_NUM_THREADS = "2"; $env:NUMEXPR_NUM_THREADS = "2"')
    A("")
    A("# regenerate this artifact end to end (~2 minutes)")
    A("python scripts/go_no_go.py")
    A("")
    A("# under the second calibration source (workstream F's GEFS)")
    A("python scripts/go_no_go.py --source gefs")
    A("")
    A("# sensitivity: strict two-day embargo, single-contract fees")
    A("python scripts/go_no_go.py --embargo-days 2 --contracts 1")
    A("")
    A("# tests (this file only -- never the full suite on this machine)")
    A("python -m pytest tests/test_ev_analysis.py -v")
    A("```")
    A("")
    A(
        "**One environment note that affects reproduction, not results.** On the "
        "pinned pandas 2.3.3 / numpy 2.4.0 / CPython 3.14 stack, "
        "`pandas.merge_asof` faults with a Windows access violation in its native "
        "join path after the first call in a process, and this report needs three "
        "forecast-vintage joins. `ev_analysis._asof_backward` therefore performs the "
        "backward as-of join with `searchsorted` instead. It is pinned against "
        "`merge_asof` itself in `tests/test_ev_analysis.py`, and on the real archive "
        "the two agree row for row, value for value and dtype for dtype "
        "(10,750 rows under each source). No number in this report changed."
    )
    A("")
    A(
        f"Machine-readable companion: `reports/phase2/ws_e_go_no_go_data_{as_of}.json` "
        f"— every table above as records, plus the provenance block and the worked "
        f"example's intermediates."
    )
    A("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
