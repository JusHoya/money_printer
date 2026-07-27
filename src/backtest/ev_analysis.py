"""Expected-value machinery for the PRD Phase 2 go/no-go (FR-2.4, EC-5).

This module turns three recorded artifacts into an EV verdict:

* **the tape** -- ``data/ladders/<SERIES>/<date>.csv``, Kalshi's own hourly
  quote history for every bracket of every city-day (workstream C);
* **the forecast** -- ``data/forecast_archive/forecast_series_<source>.csv``,
  every archived model run with the UTC instant it was issued (workstream B);
* **the calibration** -- ``data/calibration/<CITY>_<source>_v<N>.json``, the
  measured forecast bias and error sigma (workstream B), consumed through
  workstream D's probability engine.

Five properties are load-bearing, because four prior review cycles in this
project reported numbers that were not what they claimed to be:

1. **No EV is ever quoted for a fill the tape says was unavailable.**
   Every candidate trade names the *specific* side of the book it must hit
   (:func:`quote_for_shape`), and a row where that side is absent is counted as
   an unavailable opportunity, never as a fill at some other price. The
   fill-opportunity rate travels beside every EV number and is never averaged
   into it.

2. **No lookahead in the forecast.** A ladder snapshot at ``ts_utc`` may only
   use a model run whose ``init_time_utc <= ts_utc``
   (:func:`select_forecast_vintage`). The archive carries 15 runs per target
   date spanning eight days of lead; using the day-of run on a snapshot taken
   the previous afternoon would be reading tomorrow's forecast.

3. **No lookahead in the calibration.** :class:`WalkForwardCalibrator` refits
   bias and sigma for each target date from paired days strictly earlier than
   that date (plus an optional embargo), so the numbers that price a ladder
   were never fitted on that ladder's outcome. The in-sample path is retained
   for comparison only -- the gap between the two is a reported quantity.

4. **Fees come from :mod:`src.core.fee_calculator`,** which was corrected this
   sprint against the published schedule (maker ``$0.00`` on ``KXHIGH*``, taker
   ``ceil_to_cent(0.07*C*P*(1-P))``, no settlement fee). This module adds only
   the per-contract division, because the fee rounds on the *order total* and a
   C=1 model overstates far-bracket taker cost roughly threefold.

5. **The 1c adverse-fill allowance EC-5 mandates is applied to the price, not
   to the answer.** ``price_paid = quote + 0.01``, and the fee is charged on
   the price actually paid. On a 1c far bracket that allowance is 100% of the
   premium, which is the point of requiring it.

Contract conventions
--------------------
Everything about *which way a bracket pays* comes from
:mod:`src.core.bracket_payoff` via the API's ``strike_type`` / ``floor_strike``
/ ``cap_strike``. No ticker string is parsed here (PRD FR-1.1).

The NO book is the exact identity ``no_bid = 1 - yes_ask``,
``no_ask = 1 - yes_bid``; nothing is modelled or interpolated. An empty book
arrives from Kalshi as the sentinel ``yes_bid = 0.0`` / ``yes_ask = 1.0``, and
those are treated as *absent*, never as a tradeable price of zero or one.

Source parameterization
-----------------------
:class:`CalibrationSource` names the forecast CSV, the calibration directory,
the source label and the version. Adding a second calibrated source (e.g. a
GEFS calibration) is one more :class:`CalibrationSource` instance, not a
rewrite; :func:`available_sources` discovers what is on disk.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import numpy as np
import pandas as pd

from src.calibration import forecast_calibration as fcal
from src.calibration.probability_engine import (
    NX_WINDOW_REGIME,
    ProbabilityEngineError,
    REGIME_MIXTURE,
    REGIME_MIXTURE_IF_AVAILABLE,
    REGIME_SINGLE,
    bracket_probabilities_point,
    normal_cdf,
)
from src.core.bracket_payoff import BracketSpec, parse_bracket_spec, yes_bounds
from src.core.fee_calculator import (
    EXIT_SETTLEMENT,
    FEE_TYPE_STANDARD,
    ev_after_fees,
    maker_fee,
    taker_fee,
)
from src.data.kalshi_history import LADDER_DIR

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
FORECAST_ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "forecast_archive")
CALIBRATION_DIR = os.path.join(REPO_ROOT, "data", "calibration")
WEATHER_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "weather_truth")

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------
DIRECTION_YES = "buy_yes"
DIRECTION_NO = "buy_no"
DIRECTIONS = (DIRECTION_YES, DIRECTION_NO)

MODE_MAKER = "maker"
MODE_TAKER = "taker"
MODES = (MODE_MAKER, MODE_TAKER)

CALIB_IN_SAMPLE = "in_sample"
CALIB_WALK_FORWARD = "walk_forward"

#: EC-5's "1c adverse-fill allowance", in dollars.
ADVERSE_FILL_DOLLARS = 0.01

#: Kalshi's price grid is whole cents in [0.01, 0.99]; 1.00 is not orderable.
MAX_ORDERABLE_PRICE = 0.99

#: Bracket-distance band edges in degF, on ``|midpoint - calibrated median|``.
#: The final band is open-ended.
BAND_EDGES: Tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
BAND_LABELS: Tuple[str, ...] = ("0-1F", "1-2F", "2-3F", "3-4F", "4-5F", "5F+")

#: Time-to-close windows, in minutes, ordered far-from-close first.
TIME_WINDOWS: Tuple[Tuple[str, float, float], ...] = (
    (">=24h", 24 * 60, math.inf),
    ("12-24h", 12 * 60, 24 * 60),
    ("6-12h", 6 * 60, 12 * 60),
    ("3-6h", 3 * 60, 6 * 60),
    ("1-3h", 60, 3 * 60),
    ("<1h", 0.0, 60),
)

CITY_STATION = {"NY": "KNYC", "CHI": "KMDW", "LAX": "KLAX", "MIA": "KMIA"}
CITY_TZ = {
    "NY": "America/New_York",
    "CHI": "America/Chicago",
    "LAX": "America/Los_Angeles",
    "MIA": "America/New_York",
}


class EVAnalysisError(RuntimeError):
    """Raised when an EV cannot be computed honestly rather than approximately."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationSource:
    """One calibrated forecast source: its archive, its artifacts, its label.

    A second source (workstream F's GEFS calibration, say) is a second instance
    of this class -- the harness below takes it as a parameter everywhere.
    """

    name: str
    version: int = 1
    forecast_csv: Optional[str] = None
    calibration_dir: str = CALIBRATION_DIR

    def resolved_forecast_csv(self) -> str:
        if self.forecast_csv:
            return self.forecast_csv
        return os.path.join(FORECAST_ARCHIVE_DIR, f"forecast_series_{self.name}.csv")

    def calibration_path(self, city: str) -> str:
        return os.path.join(
            self.calibration_dir,
            fcal.calibration_filename(city.upper(), self.name, self.version),
        )

    def is_available(self, cities: Sequence[str]) -> bool:
        if not os.path.exists(self.resolved_forecast_csv()):
            return False
        return all(os.path.exists(self.calibration_path(c)) for c in cities)


GFS_MEX = CalibrationSource(name="gfs_mex", version=1)
GEFS = CalibrationSource(name="gefs", version=1)
CANDIDATE_SOURCES: Tuple[CalibrationSource, ...] = (GFS_MEX, GEFS)


def available_sources(
    cities: Sequence[str] = tuple(CITY_STATION),
    candidates: Sequence[CalibrationSource] = CANDIDATE_SOURCES,
) -> List[CalibrationSource]:
    """Every candidate source whose archive *and* all four artifacts exist.

    Workstream F may land a second calibration mid-sprint; discovering it here
    means the go/no-go reports under both sources without a code change, and
    reports its absence explicitly when it has not landed.
    """
    return [s for s in candidates if s.is_available(cities)]


@dataclass(frozen=True)
class EVConfig:
    """Everything that moves an EV number, in one auditable object."""

    contracts: int = 20
    adverse_fill_dollars: float = ADVERSE_FILL_DOLLARS
    series_fee_type: str = FEE_TYPE_STANDARD
    regime: str = REGIME_SINGLE
    #: Refit the calibration per target date on strictly earlier truth.
    calibration_mode: str = CALIB_WALK_FORWARD
    #: Extra days of truth withheld beyond "strictly before the target date".
    #: 1 = the default no-lookahead rule; 2 also withholds the day whose CLI
    #: report publishes the morning the ladder is already open.
    embargo_days: int = 1
    band_edges: Tuple[float, ...] = BAND_EDGES
    support_sigmas: float = 8.0

    def label(self) -> str:
        return (
            f"C={self.contracts} adverse={self.adverse_fill_dollars:.2f} "
            f"regime={self.regime} calib={self.calibration_mode}"
            f"(embargo={self.embargo_days}d)"
        )


# --------------------------------------------------------------------------
# Bracket geometry
# --------------------------------------------------------------------------
def ladder_core_width_f(specs: Sequence[BracketSpec]) -> float:
    """Width in degF of the ladder's finite ``between`` brackets.

    Every recorded KXHIGH ladder is 2 degF-wide ``between`` brackets flanked by
    one ``less`` and one ``greater`` (workstream C, 276/276 city-days). The
    width is measured rather than assumed so a ladder that changes shape does
    not silently reuse a stale constant.
    """
    widths = sorted(
        {
            float(s.cap_strike) - float(s.floor_strike) + 1.0
            for s in specs
            if s.strike_type == "between"
        }
    )
    if not widths:
        raise EVAnalysisError("ladder has no finite bracket; cannot measure its width")
    return widths[len(widths) // 2]


def bracket_midpoint_f(spec: BracketSpec, core_width_f: float) -> float:
    """Representative temperature of a bracket, in degF.

    ``between`` brackets use the midpoint of their inclusive integer YES range.
    The two open-ended tail contracts have no midpoint, so they are represented
    as a *virtual bracket of the same width as the ladder's finite core*,
    starting at their one real edge:

    * ``greater`` (YES for high >= L)  ->  ``L + (w - 1) / 2``
    * ``less``    (YES for high <= H)  ->  ``H - (w - 1) / 2``

    On the standard 2 degF ladder that puts T90 (>=91) at 91.5 and T83 (<=82)
    at 81.5 -- i.e. the tails are treated as if they were one more 2 degF
    bracket, which is the least generous reading available: pushing them
    further out would inflate their band distance and make far-bracket EV look
    better than it is.
    """
    lo, hi = yes_bounds(spec)
    half = (float(core_width_f) - 1.0) / 2.0
    if math.isinf(lo) and math.isinf(hi):
        raise EVAnalysisError(f"{spec.ticker}: bracket is unbounded on both sides")
    if math.isinf(lo):
        return float(hi) - half
    if math.isinf(hi):
        return float(lo) + half
    return (float(lo) + float(hi)) / 2.0


def bracket_edge_distance_f(spec: BracketSpec, median_f: float) -> float:
    """Distance from ``median_f`` to the nearest degF the bracket pays on.

    Reported beside the midpoint distance so a red-team can see both readings
    of "how far out is this bracket"; ``0.0`` means the calibrated median falls
    inside the bracket.
    """
    lo, hi = yes_bounds(spec)
    if median_f < lo:
        return float(lo) - float(median_f)
    if median_f > hi:
        return float(median_f) - float(hi)
    return 0.0


def band_label(distance_f: float, edges: Sequence[float] = BAND_EDGES) -> str:
    """Band name for a bracket-distance, e.g. ``2-3F``. Final band is open."""
    if distance_f is None or (isinstance(distance_f, float) and math.isnan(distance_f)):
        return "unknown"
    for i in range(len(edges) - 1, 0, -1):
        if distance_f >= edges[i]:
            return (
                f"{edges[i]:g}F+"
                if i == len(edges) - 1
                else f"{edges[i]:g}-{edges[i + 1]:g}F"
            )
    return f"{edges[0]:g}-{edges[1]:g}F"


def time_window_label(minutes_to_close: float) -> str:
    for name, lo, hi in TIME_WINDOWS:
        if lo <= minutes_to_close < hi:
            return name
    return "post_close"


# --------------------------------------------------------------------------
# Prices, fills, fees, EV
# --------------------------------------------------------------------------
def quote_for_shape(
    yes_bid: float,
    yes_ask: float,
    direction: str,
    mode: str,
) -> Optional[float]:
    """The price this trade shape must actually hit, or ``None`` if absent.

    ``yes_bid == 0`` and ``yes_ask == 1`` are Kalshi's empty-book sentinels and
    return ``None`` -- an EV computed against them would be an EV against a
    quote that did not exist.

    * taker buy YES -- lifts the YES offer, pays ``yes_ask``
    * taker buy NO  -- lifts the NO offer, pays ``no_ask = 1 - yes_bid``
    * maker buy YES -- joins the YES bid, pays ``yes_bid``
    * maker buy NO  -- joins the NO bid, pays ``no_bid = 1 - yes_ask``

    On a book whose median spread is exactly one tick there is no price
    *inside* the spread to rest at, so "maker" here means joining the best bid
    on the side being bought. That is the only maker price the recorded tape
    can support.
    """
    has_bid = yes_bid is not None and not _isnan(yes_bid) and yes_bid > 0.0
    has_ask = yes_ask is not None and not _isnan(yes_ask) and yes_ask < 1.0
    if direction == DIRECTION_YES:
        if mode == MODE_TAKER:
            return float(yes_ask) if has_ask else None
        return float(yes_bid) if has_bid else None
    if direction == DIRECTION_NO:
        if mode == MODE_TAKER:
            return (1.0 - float(yes_bid)) if has_bid else None
        return (1.0 - float(yes_ask)) if has_ask else None
    raise EVAnalysisError(f"unknown direction {direction!r}")


def _isnan(x: Any) -> bool:
    try:
        return bool(math.isnan(float(x)))
    except (TypeError, ValueError):
        return True


def adverse_fill_price(
    quote: float, allowance: float = ADVERSE_FILL_DOLLARS
) -> Optional[float]:
    """``quote + allowance``, or ``None`` when that leaves the orderable grid.

    A quote at 0.99 plus a 1c allowance is 1.00, which Kalshi does not accept
    and which can never be +EV anyway. Returning ``None`` marks the trade
    unexecutable rather than booking a guaranteed loss as an opportunity.
    """
    price = round(float(quote) + float(allowance), 10)
    if price > MAX_ORDERABLE_PRICE + 1e-12 or price <= 0.0:
        return None
    return price


def fee_per_contract(
    price: float,
    contracts: int,
    is_maker: bool,
    series_fee_type: str = FEE_TYPE_STANDARD,
) -> float:
    """Entry fee per contract, from :mod:`src.core.fee_calculator`.

    Kalshi rounds the fee up to the cent on the **order total**, so the
    per-contract cost falls with size. This function divides; it does not
    reimplement the schedule.
    """
    if contracts <= 0:
        raise EVAnalysisError(f"contracts must be positive, got {contracts}")
    total = (
        maker_fee(price, contracts, series_fee_type)
        if is_maker
        else taker_fee(price, contracts)
    )
    return total / float(contracts)


@dataclass(frozen=True)
class EVResult:
    """One trade's modelled EV, with every intermediate kept."""

    p_win: float
    quote: float
    price_paid: float
    fee_total: float
    fee_per_contract: float
    contracts: int
    is_maker: bool
    ev_per_contract: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "p_win": self.p_win,
            "quote": self.quote,
            "price_paid": self.price_paid,
            "fee_total": self.fee_total,
            "fee_per_contract": self.fee_per_contract,
            "contracts": self.contracts,
            "is_maker": self.is_maker,
            "ev_per_contract": self.ev_per_contract,
        }


def ev_for_trade(
    p_win: float,
    quote: float,
    *,
    contracts: int = 1,
    is_maker: bool = True,
    series_fee_type: str = FEE_TYPE_STANDARD,
    adverse_fill: float = ADVERSE_FILL_DOLLARS,
) -> Optional[EVResult]:
    """EV per contract for one binary bought and held to settlement.

    ``EV = P(win) * $1.00 - price_paid - entry_fee_per_contract``

    One fee leg, not two: Kalshi charges no settlement fee and PRD FR-1.5 holds
    every weather position to expiry. ``price_paid`` already carries EC-5's
    adverse-fill allowance. Returns ``None`` when the allowance pushes the
    price off the orderable grid.
    """
    price_paid = adverse_fill_price(quote, adverse_fill)
    if price_paid is None:
        return None
    total = (
        maker_fee(price_paid, contracts, series_fee_type)
        if is_maker
        else taker_fee(price_paid, contracts)
    )
    per = total / float(contracts)
    return EVResult(
        p_win=float(p_win),
        quote=float(quote),
        price_paid=price_paid,
        fee_total=total,
        fee_per_contract=per,
        contracts=int(contracts),
        is_maker=bool(is_maker),
        ev_per_contract=float(p_win) - price_paid - per,
    )


def ev_matches_fee_calculator(
    p_win: float,
    quote: float,
    *,
    is_maker: bool = True,
    series_fee_type: str = FEE_TYPE_STANDARD,
    adverse_fill: float = ADVERSE_FILL_DOLLARS,
) -> bool:
    """At C=1, agree exactly with :func:`fee_calculator.ev_after_fees`.

    The size-aware path above divides a total fee; at one contract there is
    nothing to divide, so the two must coincide. Used as a cross-check so the
    per-contract division cannot silently drift from the reserved fee module.
    """
    result = ev_for_trade(
        p_win,
        quote,
        contracts=1,
        is_maker=is_maker,
        series_fee_type=series_fee_type,
        adverse_fill=adverse_fill,
    )
    if result is None:
        return True
    reference = ev_after_fees(
        p_win, result.price_paid, 1, is_maker, series_fee_type, EXIT_SETTLEMENT
    )
    return abs(result.ev_per_contract - reference) < 1e-12


# --------------------------------------------------------------------------
# Maker fill model
# --------------------------------------------------------------------------
def _forward_extreme(values: pd.Series, kind: str) -> pd.Series:
    """Extreme of ``values`` over the *strictly later* rows of one market.

    ``kind="min"`` returns, at each position, the minimum of every later
    non-null value; ``kind="max"`` the maximum. Positions with no later
    non-null value at all return ``NaN``.

    The empty-book sentinels arrive here as ``NaN``, and pandas'
    ``cummin``/``cummax`` emit ``NaN`` at every ``NaN`` *input* position. Doing
    the accumulation on the raw column therefore blanks a snapshot whenever the
    single immediately-following snapshot had no quote, even though a later one
    did -- which under-counts maker fills. The NaNs are accordingly replaced by
    the neutral extreme (``+inf`` for a running minimum, ``-inf`` for a running
    maximum) *before* accumulating, and the neutral value is mapped back to
    ``NaN`` afterwards so "no future quote exists" stays distinguishable from
    "a future quote exists and it is this".
    """
    neutral = math.inf if kind == "min" else -math.inf
    shifted = values[::-1].shift(1).fillna(neutral)
    acc = shifted.cummin() if kind == "min" else shifted.cummax()
    return acc.replace([math.inf, -math.inf], np.nan)[::-1]


def add_maker_fill_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Mark, per snapshot, whether a resting order would ever have filled.

    PRD FR-3.3 ties maker fill probability to "observed trade flow / quote
    traversal". With hourly candles the observable traversal is the *quote*:

    * a resting **YES bid** at ``L = yes_bid(t)`` fills if some later snapshot
      before close shows ``yes_ask(t') <= L`` -- somebody is offering at or
      below the price you are bidding, so they would have hit you;
    * a resting **NO bid** at ``1 - yes_ask(t)`` (equivalently a YES offer at
      ``yes_ask(t)``) fills if some later snapshot shows
      ``yes_bid(t') >= yes_ask(t)``.

    "Some later snapshot" means **any** later snapshot before close, not the
    next one: an empty book at ``t+1`` does not cancel a traversal at ``t+3``.
    See :func:`_forward_extreme` for why that requires care.

    Both are lower bounds on true fill probability -- a traversal that opened
    and closed inside one hour is invisible at this resolution -- and both are
    upper bounds on *durable* fills, since queue position is unobservable. The
    direction of the error is stated in the report rather than corrected for.

    Adds ``maker_yes_fill`` and ``maker_no_fill`` (bool) plus the forward
    extremes they were derived from.
    """
    out = df.sort_values(["market_ticker", "ts_utc"], kind="mergesort").copy()
    ask = out["yes_ask"].where(out["yes_ask"] < 1.0)
    bid = out["yes_bid"].where(out["yes_bid"] > 0.0)

    # Strictly-later forward extremes, per market.
    out["fwd_min_ask"] = ask.groupby(out["market_ticker"], sort=False).transform(
        lambda s: _forward_extreme(s, "min")
    )
    out["fwd_max_bid"] = bid.groupby(out["market_ticker"], sort=False).transform(
        lambda s: _forward_extreme(s, "max")
    )
    eps = 1e-9
    out["maker_yes_fill"] = (
        (out["yes_bid"] > 0.0)
        & out["fwd_min_ask"].notna()
        & (out["fwd_min_ask"] <= out["yes_bid"] + eps)
    )
    out["maker_no_fill"] = (
        (out["yes_ask"] < 1.0)
        & out["fwd_max_bid"].notna()
        & (out["fwd_max_bid"] >= out["yes_ask"] - eps)
    )
    return out


# --------------------------------------------------------------------------
# Forecast archive
# --------------------------------------------------------------------------
def load_forecast_archive(source: CalibrationSource) -> pd.DataFrame:
    """The normalized forecast series, with a tz-aware ``init_ts``."""
    path = source.resolved_forecast_csv()
    if not os.path.exists(path):
        raise EVAnalysisError(
            f"no forecast archive for source {source.name!r} at {path}"
        )
    df = pd.read_csv(path)
    required = {
        "city",
        "station",
        "target_date",
        "init_time_utc",
        "lead_hours",
        "source",
        "forecast_high_f",
    }
    missing = required - set(df.columns)
    if missing:
        raise EVAnalysisError(f"{path}: missing columns {sorted(missing)}")
    df["init_ts"] = pd.to_datetime(df["init_time_utc"], utc=True)
    df["lead_hours"] = df["lead_hours"].astype(int)
    df["forecast_high_f"] = df["forecast_high_f"].astype(float)
    return df.sort_values(["city", "target_date", "init_ts"], kind="mergesort")


def select_forecast_vintage(
    archive: pd.DataFrame,
    city: str,
    target_date: str,
    as_of: pd.Timestamp,
) -> Optional[Mapping[str, Any]]:
    """The most recent run for ``(city, target_date)`` issued at or before ``as_of``.

    This is the no-lookahead gate. A snapshot taken the afternoon before the
    target day cannot see the 00Z run that is issued that evening, and this
    function will not hand it one.
    """
    sub = archive[
        (archive["city"] == city)
        & (archive["target_date"] == target_date)
        & (archive["init_ts"] <= as_of)
    ]
    if sub.empty:
        return None
    return sub.iloc[-1].to_dict()


def _asof_backward(
    left: pd.DataFrame,
    right: pd.DataFrame,
    left_on: str,
    right_on: str,
) -> pd.DataFrame:
    """``merge_asof(..., direction="backward", allow_exact_matches=True)``.

    Written out with :meth:`Series.searchsorted` rather than delegating to
    :func:`pandas.merge_asof`, which faults (Windows access violation inside
    ``concatenate_managers``/``is_monotonic_increasing``) on this project's
    pinned pandas 2.3.3 / numpy 2.4.0 / CPython 3.14 stack after the first
    successful call in a process. The defect is in the library's native join
    path, not in the data: the same inputs succeed on the first call and fault
    on the second, and the report needs three such joins. This implementation
    touches only pure-Python and numpy indexing, and
    ``tests/test_ev_analysis.py`` pins it against ``merge_asof`` itself on a
    fixture with unmatched, exact and interior keys.

    Semantics, exactly: every left row takes the last right row whose
    ``right_on`` is <= its ``left_on``; a left row with no such right row keeps
    ``NaN`` in every right column. Both frames are sorted on their key first,
    as ``merge_asof`` requires.
    """
    left = left.sort_values(left_on, kind="mergesort").reset_index(drop=True)
    right = right.sort_values(right_on, kind="mergesort").reset_index(drop=True)
    out = left.copy()
    if right.empty:
        for col in right.columns:
            out[col] = np.nan
        return out
    pos = np.asarray(right[right_on].searchsorted(left[left_on], side="right")) - 1
    matched = pos >= 0
    picked = right.iloc[np.where(matched, pos, 0)].reset_index(drop=True)
    for col in right.columns:
        series = picked[col]
        if not matched.all():
            # Only mask when something is actually unmatched: an unconditional
            # ``where`` would upcast an int column to float even with nothing
            # to replace, and merge_asof does not.
            series = series.where(pd.Series(matched, index=series.index))
        out[col] = series
    return out


def forecast_vintage_table(
    ladders: pd.DataFrame,
    archive: pd.DataFrame,
) -> pd.DataFrame:
    """For every distinct ``(city, target_date, ts_utc)``, the usable vintage.

    Returned columns: ``city``, ``target_date``, ``ts_utc``, ``init_time_utc``,
    ``lead_hours``, ``forecast_high_f``. Rows with no usable vintage are
    omitted and their count is recoverable by joining back.
    """
    snapshots = (
        ladders[["city", "target_date", "ts_utc"]]
        .drop_duplicates()
        .sort_values(["city", "target_date", "ts_utc"], kind="mergesort")
    )
    frames: List[pd.DataFrame] = []
    for (city, tdate), grp in snapshots.groupby(["city", "target_date"], sort=True):
        runs = archive[(archive["city"] == city) & (archive["target_date"] == tdate)]
        if runs.empty:
            continue
        merged = _asof_backward(
            grp,
            runs[["init_ts", "init_time_utc", "lead_hours", "forecast_high_f"]],
            left_on="ts_utc",
            right_on="init_ts",
        )
        frames.append(merged)
    if not frames:
        raise EVAnalysisError("no forecast vintage could be matched to any snapshot")
    out = pd.concat(frames, ignore_index=True)
    return out.dropna(subset=["forecast_high_f"])


# --------------------------------------------------------------------------
# Walk-forward calibration
# --------------------------------------------------------------------------
class WalkForwardCalibrator:
    """Refit a city's calibration using only truth from before a target date.

    The committed ``<CITY>_<source>_v1.json`` artifacts were fitted on
    2025-12-28..2026-07-24 while the recorded ladders run 2026-05-18..2026-07-25
    -- they overlap almost entirely, so pricing a ladder with them scores the
    trade with a calibration that already saw its outcome. This class rebuilds
    the same payload, through workstream B's own
    :func:`forecast_calibration.build_city_calibration`, from paired days whose
    ``target_date`` is at least ``embargo_days`` before the date being priced.

    ``embargo_days=1`` is the literal "strictly before that date" rule.
    ``embargo_days=2`` additionally withholds the day whose CLI report is only
    published on the morning the ladder is already trading.
    """

    def __init__(
        self,
        source: CalibrationSource,
        cities: Sequence[str] = tuple(CITY_STATION),
        embargo_days: int = 1,
        min_paired_days: int = 60,
    ) -> None:
        self.source = source
        self.cities = tuple(c.upper() for c in cities)
        self.embargo_days = int(embargo_days)
        self.min_paired_days = int(min_paired_days)
        self._paired: Dict[str, List[fcal.PairedDay]] = {}
        self._drops: Dict[str, Mapping[str, int]] = {}
        self._rows_for_city: Dict[str, int] = {}
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._fingerprints: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        rows = fcal.load_forecast_series(self.source.resolved_forecast_csv())
        self._forecast_fingerprint = fcal.content_fingerprint(
            rows,
            (
                "city",
                "station",
                "target_date",
                "init_time_utc",
                "lead_hours",
                "source",
                "forecast_high_f",
                "spread_f",
            ),
        )
        for city in self.cities:
            station = CITY_STATION[city]
            truth = fcal.load_truth(station, WEATHER_TRUTH_DIR)
            city_rows = [r for r in rows if r["city"] == city]
            paired, drops = fcal.pair_city(city_rows, station, truth)
            self._paired[city] = paired
            self._drops[city] = drops
            self._rows_for_city[city] = len(city_rows)
            self._fingerprints[city] = fcal.content_fingerprint(
                [
                    {"station": station, "date": d, "high": h}
                    for d, h in sorted(truth.items())
                ],
                ("station", "date", "high"),
            )

    def cutoff_for(self, target_date: str) -> str:
        """Latest ``target_date`` of truth admissible when pricing ``target_date``."""
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        return (d - timedelta(days=self.embargo_days)).isoformat()

    def calibration_as_of(self, city: str, target_date: str) -> Dict[str, Any]:
        city = city.upper()
        key = (city, target_date)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        cutoff = self.cutoff_for(target_date)
        subset = [p for p in self._paired[city] if p.target_date <= cutoff]
        day_of = {
            p.target_date
            for p in subset
            if fcal.bucket_for_lead(p.lead_hours) == "day_of"
        }
        if len(day_of) < self.min_paired_days:
            raise EVAnalysisError(
                f"{city} @ {target_date}: only {len(day_of)} day-of paired days "
                f"available strictly before the cutoff {cutoff}; FR-2.2 requires "
                f">= {self.min_paired_days}. Refusing to price this date rather "
                f"than fitting a thin calibration and calling it walk-forward."
            )
        payload = fcal.build_city_calibration(
            city=city,
            station=CITY_STATION[city],
            timezone_name=CITY_TZ[city],
            source=self.source.name,
            version=self.source.version,
            paired=subset,
            drops=self._drops[city],
            forecast_rows_for_city=self._rows_for_city[city],
            forecast_fingerprint=self._forecast_fingerprint,
            truth_fingerprint=self._fingerprints[city],
        )
        payload = fcal.finalize(payload)
        self._cache[key] = payload
        return payload

    def paired_days(self, city: str, bucket: str = "day_of") -> List[fcal.PairedDay]:
        return [
            p
            for p in self._paired[city.upper()]
            if fcal.bucket_for_lead(p.lead_hours) == bucket
        ]


class InSampleCalibrator:
    """The committed artifacts, unchanged. Comparison baseline only."""

    def __init__(
        self, source: CalibrationSource, cities: Sequence[str] = tuple(CITY_STATION)
    ):
        self.source = source
        self._payloads = {
            c.upper(): fcal.load_calibration(source.calibration_path(c)) for c in cities
        }

    def calibration_as_of(self, city: str, target_date: str) -> Dict[str, Any]:
        return self._payloads[city.upper()]


# --------------------------------------------------------------------------
# Probability table
# --------------------------------------------------------------------------
def specs_for_city_day(
    ladders: pd.DataFrame,
) -> Dict[Tuple[str, str], List[BracketSpec]]:
    """The 6 :class:`BracketSpec` of every ``(city, target_date)`` ladder.

    Built only from the API's ``strike_type`` / ``floor_strike`` /
    ``cap_strike`` through :func:`parse_bracket_spec`, which raises rather than
    guessing (PRD FR-1.1).
    """
    cols = [
        "city",
        "target_date",
        "market_ticker",
        "strike_type",
        "floor_strike",
        "cap_strike",
    ]
    uniq = ladders[cols].drop_duplicates().sort_values(cols, kind="mergesort")
    out: Dict[Tuple[str, str], List[BracketSpec]] = {}
    for row in uniq.itertuples(index=False):
        extra = {
            "strike_type": row.strike_type,
            "floor_strike": None
            if pd.isna(row.floor_strike)
            else float(row.floor_strike),
            "cap_strike": None if pd.isna(row.cap_strike) else float(row.cap_strike),
        }
        out.setdefault((row.city, row.target_date), []).append(
            parse_bracket_spec(row.market_ticker, extra)
        )
    return out


def build_probability_table(
    ladders: pd.DataFrame,
    vintages: pd.DataFrame,
    calibrator: Any,
    source: CalibrationSource,
    config: EVConfig,
    day_of_only: bool = False,
) -> pd.DataFrame:
    """P(YES), mu and sigma for every (city, date, forecast vintage, bracket).

    One probability-engine call per distinct ``(city, target_date,
    init_time_utc)``; the ladder tape carries at most two usable vintages per
    city-day (the 12Z run of the previous day and the 00Z run of the target
    day), so this is a few hundred calls, not tens of thousands.

    ``day_of_only`` restricts to the day-of lead bucket. The mixture regime's
    components come from the *day-of* N/X-window split, so applying it to a
    16-hour-lead forecast would price a longer-lead forecast with day-of regime
    parameters; ``build_probability_table`` refuses that combination rather
    than producing a plausible-looking number.
    """
    if (
        config.regime in (REGIME_MIXTURE, REGIME_MIXTURE_IF_AVAILABLE)
        and not day_of_only
    ):
        raise EVAnalysisError(
            "the N/X-window mixture is parameterized on the day-of regime split; "
            "pass day_of_only=True so it is not applied to a longer-lead forecast"
        )
    specs_by_day = specs_for_city_day(ladders)
    keys = (
        vintages[
            ["city", "target_date", "init_time_utc", "lead_hours", "forecast_high_f"]
        ]
        .drop_duplicates()
        .sort_values(["city", "target_date", "init_time_utc"], kind="mergesort")
    )
    records: List[Dict[str, Any]] = []
    failures: List[str] = []
    for row in keys.itertuples(index=False):
        lead = int(row.lead_hours)
        bucket = fcal.bucket_for_lead(lead)
        if day_of_only and bucket != "day_of":
            continue
        specs = specs_by_day.get((row.city, row.target_date))
        if not specs:
            continue
        width = ladder_core_width_f(specs)
        try:
            calibration = calibrator.calibration_as_of(row.city, row.target_date)
            result = bracket_probabilities_point(
                city=row.city,
                target_date=row.target_date,
                forecast_high_f=float(row.forecast_high_f),
                specs=specs,
                calibration=calibration,
                lead_hours=None if bucket == "day_of" else lead,
                regime=config.regime,
                forecast_source=source.name,
                support_sigmas=config.support_sigmas,
            )
        except (ProbabilityEngineError, EVAnalysisError, fcal.CalibrationError) as exc:
            failures.append(f"{row.city} {row.target_date} lead={lead}: {exc}")
            continue
        median_f = result.mu_f
        for spec in specs:
            records.append(
                {
                    "city": row.city,
                    "target_date": row.target_date,
                    "init_time_utc": row.init_time_utc,
                    "lead_hours": lead,
                    "lead_bucket": bucket,
                    "market_ticker": spec.ticker,
                    "forecast_high_f": float(row.forecast_high_f),
                    "mu_f": median_f,
                    "sigma_f": result.sigma_f,
                    "calibration_bucket": result.calibration_bucket,
                    "regime_model": result.regime_model,
                    "p_yes": result.p_yes(spec.ticker),
                    "midpoint_f": bracket_midpoint_f(spec, width),
                    "edge_distance_f": bracket_edge_distance_f(spec, median_f),
                    "uncovered_mass": result.uncovered_mass,
                }
            )
    out = pd.DataFrame.from_records(records)
    if out.empty:
        raise EVAnalysisError(
            "no bracket probabilities could be computed; failures: "
            + "; ".join(failures[:5])
        )
    out["distance_f"] = (out["midpoint_f"] - out["mu_f"]).abs()
    out["band"] = out["distance_f"].map(lambda d: band_label(d, config.band_edges))
    out.attrs["failures"] = failures
    return out


# --------------------------------------------------------------------------
# Opportunity frame
# --------------------------------------------------------------------------
def build_opportunity_frame(
    ladders: pd.DataFrame,
    probabilities: pd.DataFrame,
    vintages: pd.DataFrame,
    config: EVConfig,
) -> pd.DataFrame:
    """One row per (snapshot x bracket x direction x maker/taker) candidate.

    Carries, for every candidate: the band, the time-to-close window, the
    quote the shape must hit (``NaN`` when that side of the book was empty),
    whether it was available at all, the modelled win probability, the price
    after the adverse-fill allowance, the fee, the modelled EV, and the
    realized settlement PnL for the same trade.
    """
    tape = ladders.merge(
        vintages[
            [
                "city",
                "target_date",
                "ts_utc",
                "init_time_utc",
                "lead_hours",
                "forecast_high_f",
            ]
        ],
        on=["city", "target_date", "ts_utc"],
        how="inner",
        validate="many_to_one",
    )
    tape = tape.merge(
        probabilities[
            [
                "city",
                "target_date",
                "init_time_utc",
                "market_ticker",
                "mu_f",
                "sigma_f",
                "p_yes",
                "midpoint_f",
                "distance_f",
                "edge_distance_f",
                "band",
                "lead_bucket",
                "calibration_bucket",
                "regime_model",
            ]
        ],
        on=["city", "target_date", "init_time_utc", "market_ticker"],
        how="inner",
        validate="many_to_one",
    )
    tape = tape[tape["minutes_to_close"] > 0].copy()
    tape = add_maker_fill_flags(tape)
    tape["window"] = tape["minutes_to_close"].map(time_window_label)
    tape["settles_yes"] = tape["result"].astype(str).str.lower().eq("yes")

    frames: List[pd.DataFrame] = []
    for direction in DIRECTIONS:
        for mode in MODES:
            part = tape.copy()
            part["direction"] = direction
            part["mode"] = mode
            is_maker = mode == MODE_MAKER
            if direction == DIRECTION_YES:
                quote = (
                    part["yes_ask"].where(part["yes_ask"] < 1.0)
                    if not is_maker
                    else part["yes_bid"].where(part["yes_bid"] > 0.0)
                )
                p_win = part["p_yes"]
                won = part["settles_yes"]
            else:
                quote = (
                    (1.0 - part["yes_bid"]).where(part["yes_bid"] > 0.0)
                    if not is_maker
                    else (1.0 - part["yes_ask"]).where(part["yes_ask"] < 1.0)
                )
                p_win = 1.0 - part["p_yes"]
                won = ~part["settles_yes"]
            part["quote"] = quote
            part["p_win"] = p_win
            part["won"] = won
            part["quote_present"] = quote.notna()
            if is_maker:
                fillable = (
                    part["maker_yes_fill"]
                    if direction == DIRECTION_YES
                    else part["maker_no_fill"]
                )
            else:
                fillable = part["quote_present"]
            part["fillable"] = part["quote_present"] & fillable.fillna(False)
            frames.append(part)

    opp = pd.concat(frames, ignore_index=True)
    opp["price_paid"] = (opp["quote"] + config.adverse_fill_dollars).round(10)
    opp.loc[opp["price_paid"] > MAX_ORDERABLE_PRICE + 1e-12, "price_paid"] = np.nan
    opp["executable"] = opp["fillable"] & opp["price_paid"].notna()

    opp["is_maker"] = opp["mode"].eq(MODE_MAKER)
    opp["fee_per_contract"] = _vector_fee(
        opp["price_paid"], config.contracts, opp["is_maker"], config.series_fee_type
    )
    opp["ev_per_contract"] = opp["p_win"] - opp["price_paid"] - opp["fee_per_contract"]
    opp["realized_per_contract"] = (
        opp["won"].astype(float) - opp["price_paid"] - opp["fee_per_contract"]
    )
    opp.loc[~opp["executable"], ["ev_per_contract", "realized_per_contract"]] = np.nan
    return opp


def _vector_fee(
    prices: pd.Series,
    contracts: int,
    is_maker: pd.Series,
    series_fee_type: str,
) -> pd.Series:
    """Fee per contract for a whole column, memoized on (price, maker)."""
    cache: Dict[Tuple[float, bool], float] = {}
    out = np.full(len(prices), np.nan)
    price_vals = prices.to_numpy()
    maker_vals = is_maker.to_numpy()
    for i in range(len(price_vals)):
        p = price_vals[i]
        if p != p:  # NaN
            continue
        key = (round(float(p), 4), bool(maker_vals[i]))
        val = cache.get(key)
        if val is None:
            val = fee_per_contract(key[0], contracts, key[1], series_fee_type)
            cache[key] = val
        out[i] = val
    return pd.Series(out, index=prices.index)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def aggregate_bands(
    opportunities: pd.DataFrame,
    group_cols: Sequence[str] = ("direction", "mode", "band"),
) -> pd.DataFrame:
    """Per-cell EV summary with the fill-opportunity rate beside every number.

    ``n_candidates`` counts every snapshot the shape could have been attempted
    on; ``n_executable`` counts those where the required side of the book was
    present **and** (for maker) a later traversal would have filled the resting
    order. Every price/EV statistic is computed on the executable subset only:
    an EV averaged over snapshots where the quote did not exist is fiction.
    """
    cols = list(group_cols)
    grouped = opportunities.groupby(cols, dropna=False, sort=True)
    rows: List[Dict[str, Any]] = []
    for key, g in grouped:
        key_t = key if isinstance(key, tuple) else (key,)
        ex = g[g["executable"]]
        rec: Dict[str, Any] = dict(zip(cols, key_t))
        rec["n_candidates"] = int(len(g))
        rec["n_quote_present"] = int(g["quote_present"].sum())
        rec["n_executable"] = int(len(ex))
        rec["quote_rate"] = float(g["quote_present"].mean()) if len(g) else float("nan")
        rec["fill_rate"] = float(len(ex) / len(g)) if len(g) else float("nan")
        if ex.empty:
            for c in (
                "mean_p_win",
                "mean_quote",
                "mean_price_paid",
                "mean_fee",
                "ev_per_contract",
                "realized_per_contract",
                "realized_se",
                "n_city_days",
                "ev_total_dollars",
            ):
                rec[c] = float("nan")
            rows.append(rec)
            continue
        rec["mean_p_win"] = float(ex["p_win"].mean())
        rec["mean_quote"] = float(ex["quote"].mean())
        rec["mean_price_paid"] = float(ex["price_paid"].mean())
        rec["mean_fee"] = float(ex["fee_per_contract"].mean())
        rec["ev_per_contract"] = float(ex["ev_per_contract"].mean())
        rec["realized_per_contract"] = float(ex["realized_per_contract"].mean())
        day_means = ex.groupby(["city", "target_date"])["realized_per_contract"].mean()
        rec["n_city_days"] = int(len(day_means))
        rec["realized_se"] = (
            float(day_means.std(ddof=1) / math.sqrt(len(day_means)))
            if len(day_means) > 1
            else float("nan")
        )
        rec["ev_total_dollars"] = float(ex["ev_per_contract"].sum())
        rows.append(rec)
    out = pd.DataFrame(rows)
    return out.sort_values(cols, kind="mergesort").reset_index(drop=True)


def positive_ev_shapes(
    bands: pd.DataFrame,
    min_executable: int = 30,
    min_city_days: int = 10,
) -> pd.DataFrame:
    """Cells whose modelled EV is > 0 on a non-trivial number of real fills.

    FR-2.4 admits a trade shape on modelled EV alone. The two sample floors are
    this module's own addition: a positive EV measured on a handful of
    snapshots from three days is not evidence of a trade shape, and Phase 3
    would be sized from it.
    """
    if bands.empty:
        return bands
    mask = (
        (bands["ev_per_contract"] > 0)
        & (bands["n_executable"] >= min_executable)
        & (bands["n_city_days"] >= min_city_days)
    )
    return bands[mask].sort_values("ev_per_contract", ascending=False)


# --------------------------------------------------------------------------
# Named trade shapes (PRD FR-3.1)
# --------------------------------------------------------------------------
#: FR-3.1(a) defaults, quoted from the PRD: "model P(YES) <= market ask - 8pt,
#: bracket >= 4 degF from calibrated forecast median".
FR31A_MARGIN = 0.08
FR31A_DISTANCE_F = 4.0
#: FR-3.1(b): "model P >= 0.95" in the settlement-station afternoon.
FR31B_P_MIN = 0.95

#: Windows in which the day-of forecast is not yet stale. The archived MOS
#: source publishes no same-day update, so inside ~12h to close the model is
#: frozen while the market keeps learning; a band computed from that frozen
#: median stops meaning "far from the forecast".
TRADEABLE_WINDOWS: Tuple[str, ...] = (">=24h", "12-24h")


def fr31a_mask(
    df: pd.DataFrame,
    margin: float = FR31A_MARGIN,
    distance_f: float = FR31A_DISTANCE_F,
    windows: Sequence[str] = TRADEABLE_WINDOWS,
) -> pd.Series:
    """The far-bracket NO shape exactly as PRD FR-3.1(a) specifies it.

    ``market ask`` is the real YES offer; a row whose ask is the empty-book
    sentinel 1.0 would satisfy ``p_yes <= 1.0 - margin`` almost always, so the
    ask must be present for the rule to fire at all.
    """
    return (
        df["direction"].eq(DIRECTION_NO)
        & df["window"].isin(list(windows))
        & (df["yes_ask"] < 1.0)
        & (df["p_yes"] <= df["yes_ask"] - float(margin))
        & (df["edge_distance_f"] >= float(distance_f))
    )


def fr31b_mask(
    df: pd.DataFrame,
    p_min: float = FR31B_P_MIN,
    windows: Sequence[str] = ("6-12h", "3-6h", "1-3h", "<1h"),
) -> pd.Series:
    """The lock-in shape of PRD FR-3.1(b): buy a near-certain outcome late."""
    return (
        df["direction"].eq(DIRECTION_YES)
        & df["window"].isin(list(windows))
        & (df["p_win"] >= float(p_min))
    )


@dataclass(frozen=True)
class ShapeResult:
    """Settlement-true outcome of one named trade shape, clustered by date."""

    label: str
    trades: int
    markets: int
    city_days: int
    dates: int
    fill_opportunity_rate: float
    modelled_ev: float
    realized: float
    realized_se: float
    t_stat: float
    boot_lo: float
    boot_hi: float
    win_rate: float
    mean_price_paid: float
    mean_fee: float
    mean_model_p_yes: float
    mean_market_yes_ask: float
    realized_yes_rate: float
    losing_dates: int
    worst_date_pnl: float

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def evaluate_shape(
    opportunities: pd.DataFrame,
    mask: pd.Series,
    label: str,
    *,
    max_entries_per_market: int = 1,
    cluster: str = "target_date",
    n_boot: int = 4000,
    seed: int = 20260726,
) -> Optional[ShapeResult]:
    """Realized, settlement-true PnL of a trade shape, with clustered inference.

    Every hourly snapshot of one market is the *same* bet on the same outcome,
    and the six brackets of a city-day are one joint outcome, so a naive
    per-row standard error would be meaningless. Entries are therefore capped
    per market (PRD FR-3.4 allows at most 3/market/day) and the inference is a
    non-parametric bootstrap over whole **dates** -- the coarsest cluster, which
    also absorbs the correlation between four cities under one weather pattern.

    ``fill_opportunity_rate`` is the share of snapshots where the rule fired
    *and* the required side of the book was actually there.
    """
    candidates = opportunities[mask.reindex(opportunities.index, fill_value=False)]
    if candidates.empty:
        return None
    executable = candidates[candidates["executable"]]
    if executable.empty:
        return None
    trades = (
        executable.sort_values(["market_ticker", "ts_utc"], kind="mergesort")
        .groupby("market_ticker", sort=False)
        .head(max_entries_per_market)
    )
    by_cluster = trades.groupby(cluster)["realized_per_contract"].mean()
    n = len(by_cluster)
    values = by_cluster.to_numpy()
    mean = float(values.mean())
    se = float(values.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
    rng = np.random.default_rng(seed)
    draws = values[rng.integers(0, n, size=(n_boot, n))].mean(axis=1)
    lo, hi = (float(x) for x in np.percentile(draws, [2.5, 97.5]))
    return ShapeResult(
        label=label,
        trades=int(len(trades)),
        markets=int(trades["market_ticker"].nunique()),
        city_days=int(trades.groupby(["city", "target_date"]).ngroups),
        dates=n,
        fill_opportunity_rate=float(len(executable) / len(candidates)),
        modelled_ev=float(trades["ev_per_contract"].mean()),
        realized=mean,
        realized_se=se,
        t_stat=float(mean / se) if se == se and se > 0 else float("nan"),
        boot_lo=lo,
        boot_hi=hi,
        win_rate=float(trades["won"].mean()),
        mean_price_paid=float(trades["price_paid"].mean()),
        mean_fee=float(trades["fee_per_contract"].mean()),
        mean_model_p_yes=float(trades["p_yes"].mean()),
        mean_market_yes_ask=float(trades["yes_ask"].mean()),
        realized_yes_rate=float(1.0 - trades["won"].mean())
        if trades["direction"].iloc[0] == DIRECTION_NO
        else float(trades["won"].mean()),
        losing_dates=int((by_cluster < 0).sum()),
        worst_date_pnl=float(by_cluster.min()),
    )


# --------------------------------------------------------------------------
# Tail calibration
# --------------------------------------------------------------------------
def gaussian_tail_probability(
    threshold: float,
    bias: float,
    sigma: float,
    side: str = "abs",
) -> float:
    """Modelled P(error beyond ``threshold``) for an integer-valued error.

    The forecast and the CLI truth are both whole degrees, so the error is an
    integer and the same continuity correction the probability engine uses on
    the pmf applies here: ``P(E >= t) = 1 - Phi((t - 0.5 - bias)/sigma)``.
    Comparing an integer sample against an uncorrected continuous tail would
    manufacture a miscalibration that is an artifact of the comparison.
    """
    if sigma <= 0:
        raise EVAnalysisError("sigma must be positive")
    hi = 1.0 - normal_cdf((threshold - 0.5 - bias) / sigma)
    lo = normal_cdf((-threshold + 0.5 - bias) / sigma)
    if side == "high":
        return hi
    if side == "low":
        return lo
    if side == "abs":
        return hi + lo
    raise EVAnalysisError(f"unknown side {side!r}")


def mixture_tail_probability(
    threshold: float,
    city: str,
    side: str = "abs",
) -> Optional[float]:
    """Same, under workstream D's measured two-component N/X-window mixture.

    Returns ``None`` for a city whose outside-window component is below the
    20-day quorum (LAX, MIA: n=2). Workstream D refuses to model those and so
    does this.
    """
    risk = NX_WINDOW_REGIME.get(city.upper())
    if risk is None or not risk.outside_sufficient:
        return None
    p = risk.p_outside
    inside = gaussian_tail_probability(
        threshold, float(risk.bias_inside_f), float(risk.sigma_inside_f), side
    )
    outside = gaussian_tail_probability(
        threshold, float(risk.bias_outside_f), float(risk.sigma_outside_f), side
    )
    return (1.0 - p) * inside + p * outside


def tail_calibration_table(
    calibrator: WalkForwardCalibrator,
    cities: Sequence[str] = tuple(CITY_STATION),
    thresholds: Sequence[float] = (3, 4, 5, 6, 8),
    source: CalibrationSource = GFS_MEX,
) -> pd.DataFrame:
    """Realized vs modelled frequency of large forecast errors, per city.

    Every far-bracket EV in this report is a bet on the modelled tail being
    right; workstream D reported (gap 1) that normality was never tested. This
    is that test, on the day-of residuals, against the same calibrated bias and
    sigma the engine uses, and against the mixture where one exists.

    Columns: city, threshold, side, n, observed count and frequency, the
    single-Gaussian and mixture predictions, their ratios, and an exact
    two-sided binomial p-value against the Gaussian prediction.
    """
    from scipy import stats as _stats  # local: scipy is only needed for the p-value

    rows: List[Dict[str, Any]] = []
    for city in cities:
        city = city.upper()
        payload = fcal.load_calibration(source.calibration_path(city))
        block = payload["day_of"]
        bias, sigma = float(block["bias_f"]), float(block["sigma_f"])
        errors = [p.error_f for p in calibrator.paired_days(city, "day_of")]
        n = len(errors)
        for t in thresholds:
            for side, predicate in (
                ("abs", lambda e, t=t: abs(e) >= t),
                ("high", lambda e, t=t: e >= t),
                ("low", lambda e, t=t: e <= -t),
            ):
                obs = sum(1 for e in errors if predicate(e))
                pred = gaussian_tail_probability(t, bias, sigma, side)
                mix = mixture_tail_probability(t, city, side)
                pval = float(
                    _stats.binomtest(obs, n, min(max(pred, 1e-12), 1 - 1e-12)).pvalue
                )
                rows.append(
                    {
                        "city": city,
                        "threshold_f": float(t),
                        "side": side,
                        "n": n,
                        "observed": obs,
                        "observed_freq": obs / n if n else float("nan"),
                        "gaussian_freq": pred,
                        "gaussian_expected": pred * n,
                        "ratio_obs_over_gaussian": (obs / n) / pred
                        if pred > 0
                        else float("inf"),
                        "mixture_freq": mix,
                        "ratio_obs_over_mixture": ((obs / n) / mix) if mix else None,
                        "binom_p": pval,
                    }
                )
    return pd.DataFrame(rows)


def standardized_residual_table(
    calibrator: WalkForwardCalibrator,
    cities: Sequence[str] = tuple(CITY_STATION),
    thresholds: Sequence[float] = (1.0, 1.5, 2.0, 2.5, 3.0),
) -> pd.DataFrame:
    """Walk-forward z-scores of every priced day, pooled, versus N(0,1).

    ``z = (error - bias_walkforward) / sigma_walkforward`` where the bias and
    sigma are the ones the engine would actually have selected on that date
    (month -> season -> day-of chain, fitted only on earlier truth). This tests
    the *deployed* pipeline's tails rather than a single annual block's.
    """
    from src.calibration.probability_engine import select_calibration

    rows: List[Dict[str, Any]] = []
    for city in cities:
        city = city.upper()
        for p in calibrator.paired_days(city, "day_of"):
            try:
                payload = calibrator.calibration_as_of(city, p.target_date)
                sel = select_calibration(
                    payload,
                    datetime.strptime(p.target_date, "%Y-%m-%d").date(),
                    lead_hours=None,
                )
            except (EVAnalysisError, ProbabilityEngineError, fcal.CalibrationError):
                continue
            rows.append(
                {
                    "city": city,
                    "target_date": p.target_date,
                    "error_f": p.error_f,
                    "bias_f": sel.bias_f,
                    "sigma_f": sel.sigma_f,
                    "bucket": sel.bucket,
                    "z": (p.error_f - sel.bias_f) / sel.sigma_f,
                }
            )
    z = pd.DataFrame(rows)
    if z.empty:
        return z
    summary: List[Dict[str, Any]] = []
    for t in thresholds:
        obs = int((z["z"].abs() >= t).sum())
        pred = 2.0 * (1.0 - normal_cdf(t))
        summary.append(
            {
                "threshold_z": t,
                "n": int(len(z)),
                "observed": obs,
                "observed_freq": obs / len(z),
                "normal_freq": pred,
                "ratio": (obs / len(z)) / pred if pred > 0 else float("inf"),
            }
        )
    out = pd.DataFrame(summary)
    out.attrs["residuals"] = z
    return out


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def provenance(source: CalibrationSource, cities: Sequence[str]) -> Dict[str, Any]:
    """Exact inputs, with content hashes, for the artifact's provenance block."""
    out: Dict[str, Any] = {
        "source": source.name,
        "version": source.version,
        "forecast_csv": os.path.relpath(source.resolved_forecast_csv(), REPO_ROOT),
        "forecast_csv_sha256": sha256_file(source.resolved_forecast_csv()),
        "ladder_root": os.path.relpath(LADDER_DIR, REPO_ROOT),
        "calibrations": {},
        "truth": {},
    }
    for city in cities:
        path = source.calibration_path(city)
        payload = fcal.load_calibration(path)
        out["calibrations"][city.upper()] = {
            "path": os.path.relpath(path, REPO_ROOT),
            "file_sha256": sha256_file(path),
            "content_hash": payload.get("content_hash"),
            "day_of_bias_f": payload["day_of"].get("bias_f"),
            "day_of_sigma_f": payload["day_of"].get("sigma_f"),
            "day_of_n": payload["day_of"].get("n"),
            "first_target_date": payload["coverage"].get("first_target_date"),
            "last_target_date": payload["coverage"].get("last_target_date"),
        }
        tpath = fcal.truth_csv_path(CITY_STATION[city.upper()], WEATHER_TRUTH_DIR)
        out["truth"][city.upper()] = {
            "path": os.path.relpath(tpath, REPO_ROOT),
            "file_sha256": sha256_file(tpath),
        }
    return out


def contamination_window(
    source: CalibrationSource, ladders: pd.DataFrame, cities: Sequence[str]
) -> Dict[str, Any]:
    """How much of the traded tape the in-sample calibration already saw."""
    first_ladder, last_ladder = (
        ladders["target_date"].min(),
        ladders["target_date"].max(),
    )
    spans = []
    for city in cities:
        payload = fcal.load_calibration(source.calibration_path(city))
        spans.append(
            (
                payload["coverage"]["first_target_date"],
                payload["coverage"]["last_target_date"],
            )
        )
    calib_first = min(s[0] for s in spans)
    calib_last = max(s[1] for s in spans)
    ladder_dates = sorted(set(ladders["target_date"]))
    overlap = [d for d in ladder_dates if calib_first <= d <= calib_last]
    return {
        "ladder_first": first_ladder,
        "ladder_last": last_ladder,
        "calibration_first": calib_first,
        "calibration_last": calib_last,
        "ladder_dates": len(ladder_dates),
        "ladder_dates_inside_calibration_window": len(overlap),
        "overlap_fraction": len(overlap) / len(ladder_dates) if ladder_dates else 0.0,
    }
