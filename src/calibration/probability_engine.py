"""Calibrated forecast -> P(bracket) for a whole Kalshi ladder (PRD FR-2.3).

Turns a forecast (a single guidance value, or an ensemble's members) plus a
:mod:`src.calibration.forecast_calibration` artifact into a probability for
**every** bracket in a city's daily-high ladder. Phase 2 exit criterion 4 is the
acceptance test: 10 recorded ladders sum to 1.0 +/- 0.01, the running cumulative
matches the calibrated CDF at every bracket boundary, and doubling sigma
measurably flattens the distribution.

"Flattens" is carried by **Shannon entropy over the ladder**, which rises under
sigma-doubling in every configuration probed and is the metric EC-4 rests on.
The two secondary criteria the EC-4 test also asserts -- the peak of the integer
pmf, and the largest *finite* bracket probability -- are not equivalent to it,
and the second one holds only while ``mu`` sits inside the ladder. Placed far
outside it, doubling sigma moves mass back *toward* the finite brackets the
forecast had abandoned, so the largest finite bracket probability **rises**: at
``mu`` 25 degF below the ladder centre it reverses on all ten recorded EC-4
ladders, and it already reverses 10 degF outside. See
``tests/test_probability_engine.py`` for both the in-ladder assertion and the
pinned counterexample.

THE OUTCOME VARIABLE IS A DISCRETE INTEGER
------------------------------------------
Kalshi settles daily-high markets on the NWS Climatological Report maximum,
which is published as a whole degree Fahrenheit, and every bracket edge is an
integer. So this module builds a **probability mass function over integer
highs**, never a continuous density sampled at bracket edges::

    P(H = k) = Phi((k + 0.5 - mu) / sigma) - Phi((k - 0.5 - mu) / sigma)

The +/-0.5 continuity correction is not cosmetic. Without it a 2-degree bracket
[85, 86] would be priced as the 1-degree-wide interval (85, 86] and every
bracket in the ladder would be systematically mispriced by roughly half a
bracket width -- the kind of error that looks like a small calibration offset
and is actually a structural bias in the same direction on every trade.

CONTRACT SEMANTICS ARE NOT RE-IMPLEMENTED HERE
----------------------------------------------
Which integer outcomes pay YES is asked of :mod:`src.core.bracket_payoff`, the
Phase 1 single source of truth, one integer at a time via
:func:`~src.core.bracket_payoff.settles_yes`. This module contains **no**
``between``/``greater``/``less`` inequality of its own and never looks at a
ticker suffix letter. A second copy of the settlement rule is precisely the
defect Phase 1 existed to delete; two copies drift, and the drift is invisible
until settlement day. :func:`~src.core.bracket_payoff.yes_bounds` is consulted
only to discover *which end of a contract is unbounded*, so the truncated
support's residual tail mass can be attributed to the right contract.

MASS IS CONSERVED, NEVER RENORMALIZED
-------------------------------------
The integer support is truncated at ``mu +/- support_sigmas * sigma`` (clipped
to a physical range). The mass outside that window is **not dropped**: it is
computed analytically and added to whichever contract is open in that direction
(:attr:`BracketProbabilities.pmf_tail_mass` reports it). Whatever is still not
covered by any bracket -- a gap in the ladder, a missing tail contract, a ladder
that simply is not a partition -- is reported as
:attr:`BracketProbabilities.uncovered_mass` and left uncovered. Renormalizing
would hide the missing bracket and inflate every remaining probability, turning
a data defect into a confident-looking trade.

Sum-to-1 is therefore **structural, not luck**: it holds exactly when the ladder
is a partition of the integer line, and :func:`validate_ladder_partition` is the
function that proves it is.

THE SIGN OF THE BIAS
--------------------
Workstream B's convention, repeated here because inverting it does not degrade
the edge, it *reverses* it while every downstream number still looks plausible::

    error_f  = forecast_high_f - truth_high_f     (positive = forecast too warm)
    mu       = forecast_high_f - bias_f           (debiased central estimate)

A calibration with ``bias_f = +2.0`` says the guidance ran 2 degF warm, so the
corrected expectation is 2 degF *cooler* than the raw forecast.

THE N/X WINDOW REGIME (workstream B's top modeling risk, handled explicitly)
----------------------------------------------------------------------------
The MOS ``N/X`` daytime maximum covers roughly 0700-1900 LST. Kalshi settles on
the CLI midnight-to-midnight maximum. On days when the calendar-day maximum is
set outside the guidance window -- a warm front at 2 AM ahead of a cold push --
the "forecast error" is not a forecast bust at all, and it is large and cold-
biased. Measured on the same 209 day-of paired days the calibration was built
from (see :data:`NX_WINDOW_REGIME` and :func:`recompute_nx_window_regime`):
NY and CHI set ~18% of their maxima outside the window, with roughly double the
sigma and a ~2-2.5 degF cold bias; LAX and MIA essentially never do.

Averaging those two populations into one Gaussian produces a distribution that
is calibrated on average and confidently wrong in the tails -- exactly the far
brackets FR-3.1(a) intends to sell. So this module does not average them:

* ``regime="single"`` (default) uses the single calibrated Gaussian **and always
  attaches a populated** :class:`RegimeRisk` **plus a warning**, so the strategy
  layer can refuse or discount far-bracket trades on cities where the regime is
  material.
* ``regime="mixture"`` builds an explicit two-component mixture and refuses
  outright for a city whose outside-window sample is below the calibrator's
  20-day quorum (LAX n=2, MIA n=2). ``regime="mixture_if_available"`` degrades
  to single for those cities, but records the degradation in ``warnings`` and in
  ``regime_model``. There is no silent path.

WHAT THE ENSEMBLE MODE DISCARDS (stated up front)
-------------------------------------------------
:func:`bracket_probabilities_ensemble` uses the **debiased member mean** as
``mu`` and the **calibrated** sigma as the dispersion. The raw member spread is
measured and reported as a diagnostic but is *not* used to size the
distribution, because no relationship between published spread and realized
error has been measured for these stations: workstream B's backfill carries
``spread_f`` blank on all 13,020 rows and every calibration block reports
``mean_spread_f: null``. This module therefore refuses to convert a null spread
into 0.0 (see ``require_published_spread``) -- a spread of zero would size every
position as though the forecast were certain.

The cost of that choice, stated plainly: all shape information in the member
cloud is discarded -- skew, multimodality, and any day-to-day variation in
forecast confidence. A kernel-smoothed variant would preserve it, but the
multimodality of a raw (under-dispersed) ensemble is itself unvalidated here, so
preserving it would be preserving noise with no evidence it is signal. The
prerequisite for revisiting this is a measured spread-skill relationship: a
calibration rebuilt on a source that publishes a spread, showing that
high-spread days really do have larger realized error.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.core.bracket_payoff import (
    BracketSpec,
    parse_bracket_spec,
    settles_yes,
    yes_bounds,
)
from src.calibration.forecast_calibration import (
    CALIBRATION_DIR,
    DAY_OF_BUCKET,
    FORECAST_ARCHIVE_DIR,
    LEAD_BUCKETS,
    MIN_BUCKET_N,
    WEATHER_TRUTH_DIR,
    bucket_for_lead,
    calibration_filename,
    load_calibration,
)

# Imported rather than re-declared on purpose: if this module kept its own
# month -> season map and the calibrator's drifted, the engine would read a
# season block the calibrator never built from those months, and nothing would
# raise.
from src.calibration.forecast_calibration import _SEASONS as _SEASON_BY_MONTH

logger = logging.getLogger(__name__)

_SQRT2 = math.sqrt(2.0)

#: Physical clamp on the integer support. Not a forecast bound -- a guard so a
#: pathological sigma cannot ask for a million-element pmf. US records are
#: roughly -80 degF (Prospect Creek, AK) and 134 degF (Death Valley); these
#: bounds are wider than any of the four cities can produce, and any mass
#: clipped by them is still conserved via the open-ended tail contracts.
PHYSICAL_MIN_F = -60
PHYSICAL_MAX_F = 135

#: Half-widths of the integer support, in sigma. 8 sigma leaves ~1e-15 residual
#: for a single Gaussian; the residual is reported, not dropped.
DEFAULT_SUPPORT_SIGMAS = 8.0

#: The guidance window the MOS ``N/X`` daytime maximum covers, in local standard
#: time. Used only to classify observed CLI maxima, never to gate trading.
NX_WINDOW_LST = (7.0, 19.0)

MODE_POINT = "point"
MODE_ENSEMBLE = "ensemble"

REGIME_SINGLE = "single"
REGIME_MIXTURE = "mixture"
REGIME_MIXTURE_IF_AVAILABLE = "mixture_if_available"

_REGIME_MODEL_SINGLE = "single_normal"
_REGIME_MODEL_MIXTURE = "mixture_nx_window"


class ProbabilityEngineError(RuntimeError):
    """The distribution could not be built from the inputs as given.

    Always a hard abort. Every condition that raises here is one where the
    alternative is a plausible-looking number nobody measured: a calibration
    bucket with no statistics, a null spread treated as certainty, a mixture
    requested for a city whose second component has n=2.
    """


# ---------------------------------------------------------------------------
# Normal primitives (stdlib only, so no library version bump can move a number)
# ---------------------------------------------------------------------------
def normal_cdf(z: float) -> float:
    """``P(Z <= z)`` for a standard normal, via :func:`math.erfc`."""
    return 0.5 * math.erfc(-float(z) / _SQRT2)


def normal_sf(z: float) -> float:
    """``P(Z > z)``. Kept separate so far-tail differences do not cancel."""
    return 0.5 * math.erfc(float(z) / _SQRT2)


def _interval_mass(z_lo: float, z_hi: float) -> float:
    """``P(z_lo < Z <= z_hi)``, evaluated on whichever side avoids cancellation.

    ``Phi(hi) - Phi(lo)`` with both arguments deep in the upper tail subtracts
    two numbers that are each ~1.0 and loses every significant digit. Using the
    survival function on that side keeps the result accurate to full precision,
    which is what makes the far brackets -- the ones FR-3.1(a) wants to sell --
    meaningful rather than float noise.
    """
    if z_hi <= z_lo:
        return 0.0
    if z_lo >= 0.0:
        return normal_sf(z_lo) - normal_sf(z_hi)
    return normal_cdf(z_hi) - normal_cdf(z_lo)


def entropy_bits(values: Iterable[float]) -> float:
    """Shannon entropy in bits of a (sub-)probability vector.

    Zero-mass entries contribute nothing (``0 log 0 == 0``). Used as the
    perturbation metric for exit criterion 4: a flatter distribution has
    strictly higher entropy, which "the numbers changed" does not establish.
    """
    total = 0.0
    for p in values:
        p = float(p)
        if p > 0.0:
            total -= p * math.log2(p)
    return total


# ---------------------------------------------------------------------------
# Ladder partition
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PartitionReport:
    """Whether a list of brackets tiles the integer line without gap or overlap.

    A complete Kalshi daily-high ladder is exactly that: one ``less`` contract
    covering everything at or below some integer, contiguous ``between``
    contracts, and one ``greater`` contract covering everything above. When the
    partition is complete, ``sum(P(bracket)) == 1`` is an identity rather than a
    coincidence -- which is why the FR-2.3 sum-to-1 assertion is only meaningful
    alongside ``complete is True``. A ladder with a hole sums to less than 1 and
    the missing bracket, not the engine, is at fault.
    """

    complete: bool
    n_brackets: int
    has_low_tail: bool
    has_high_tail: bool
    #: Lowest covered integer; ``None`` means unbounded below (a ``less`` exists).
    covered_low: Optional[int]
    #: Highest covered integer; ``None`` means unbounded above.
    covered_high: Optional[int]
    #: Inclusive integer ranges covered by no bracket, between the extremes.
    gaps: Tuple[Tuple[int, int], ...]
    #: ``(ticker_a, ticker_b, lo, hi)`` for each overlapping integer range.
    overlaps: Tuple[Tuple[str, str, int, int], ...]
    issues: Tuple[str, ...]

    def describe(self) -> str:
        lo = "-inf" if self.covered_low is None else str(self.covered_low)
        hi = "+inf" if self.covered_high is None else str(self.covered_high)
        verdict = "complete" if self.complete else "INCOMPLETE"
        return f"{verdict}: {self.n_brackets} brackets covering [{lo}, {hi}]"


def _integer_bounds(spec: BracketSpec) -> Tuple[Optional[int], Optional[int]]:
    """The inclusive integer interval that pays YES, ``None`` for an open end.

    Delegates to :func:`~src.core.bracket_payoff.yes_bounds`; the only local
    work is converting the float bounds to the integer lattice the outcome
    actually lives on.
    """
    lo, hi = yes_bounds(spec)
    ilo = None if math.isinf(lo) else int(math.ceil(lo))
    ihi = None if math.isinf(hi) else int(math.floor(hi))
    return ilo, ihi


def validate_ladder_partition(specs: Sequence[BracketSpec]) -> PartitionReport:
    """Detect gaps, overlaps, and missing tails in a ladder.

    Returns a report rather than raising: an incomplete ladder is a fact about
    the recorded data that the caller may want to trade around (or refuse), and
    the uncovered mass is quantified in :class:`BracketProbabilities` either way.
    """
    issues: List[str] = []
    if not specs:
        return PartitionReport(
            complete=False,
            n_brackets=0,
            has_low_tail=False,
            has_high_tail=False,
            covered_low=None,
            covered_high=None,
            gaps=(),
            overlaps=(),
            issues=("ladder is empty",),
        )

    intervals: List[Tuple[Optional[int], Optional[int], str]] = []
    for spec in specs:
        ilo, ihi = _integer_bounds(spec)
        if ilo is not None and ihi is not None and ihi < ilo:
            issues.append(f"{spec.ticker}: covers no integer outcome ({ilo}..{ihi})")
            continue
        intervals.append((ilo, ihi, spec.ticker))

    low_tails = [t for lo, _, t in intervals if lo is None]
    high_tails = [t for _, hi, t in intervals if hi is None]
    if not low_tails:
        issues.append("no contract covers the low tail (nothing pays below the ladder)")
    elif len(low_tails) > 1:
        issues.append(f"{len(low_tails)} contracts cover the low tail: {low_tails}")
    if not high_tails:
        issues.append(
            "no contract covers the high tail (nothing pays above the ladder)"
        )
    elif len(high_tails) > 1:
        issues.append(f"{len(high_tails)} contracts cover the high tail: {high_tails}")

    # Sort on the integer lattice with the open ends pushed to the extremes.
    ordered = sorted(
        intervals,
        key=lambda iv: (
            -math.inf if iv[0] is None else iv[0],
            math.inf if iv[1] is None else iv[1],
        ),
    )

    gaps: List[Tuple[int, int]] = []
    overlaps: List[Tuple[str, str, int, int]] = []
    prev_hi: Optional[int] = None
    prev_ticker = ""
    prev_open_high = False
    for lo, hi, ticker in ordered:
        if prev_open_high:
            # A previous contract already ran to +inf; anything after it overlaps.
            o_lo = -PHYSICAL_MAX_F if lo is None else lo
            overlaps.append((prev_ticker, ticker, int(o_lo), int(o_lo)))
        elif prev_hi is not None and lo is not None:
            if lo <= prev_hi:
                overlaps.append(
                    (
                        prev_ticker,
                        ticker,
                        lo,
                        min(prev_hi, PHYSICAL_MAX_F if hi is None else hi),
                    )
                )
            elif lo > prev_hi + 1:
                gaps.append((prev_hi + 1, lo - 1))
        if hi is None:
            prev_open_high = True
        else:
            prev_hi = hi if prev_hi is None else max(prev_hi, hi)
        prev_ticker = ticker

    for g_lo, g_hi in gaps:
        issues.append(f"gap: no contract covers {g_lo}..{g_hi}")
    for a, b, o_lo, o_hi in overlaps:
        issues.append(f"overlap: {a} and {b} both cover {o_lo}..{o_hi}")

    finite_los = [lo for lo, _, _ in intervals if lo is not None]
    finite_his = [hi for _, hi, _ in intervals if hi is not None]
    covered_low = None if low_tails else (min(finite_los) if finite_los else None)
    covered_high = None if high_tails else (max(finite_his) if finite_his else None)

    complete = (
        len(low_tails) == 1
        and len(high_tails) == 1
        and not gaps
        and not overlaps
        and not issues
    )
    return PartitionReport(
        complete=complete,
        n_brackets=len(specs),
        has_low_tail=bool(low_tails),
        has_high_tail=bool(high_tails),
        covered_low=covered_low,
        covered_high=covered_high,
        gaps=tuple(gaps),
        overlaps=tuple(overlaps),
        issues=tuple(issues),
    )


def specs_from_markets(markets: Sequence[Mapping[str, Any]]) -> List[BracketSpec]:
    """Build :class:`BracketSpec` objects from raw Kalshi market dicts.

    Every field comes from the API payload through
    :func:`~src.core.bracket_payoff.parse_bracket_spec`, which raises rather
    than guessing when ``strike_type`` is absent.
    """
    out: List[BracketSpec] = []
    for market in markets:
        ticker = market.get("ticker") or market.get("symbol") or "<unknown>"
        out.append(parse_bracket_spec(str(ticker), market))
    return out


# ---------------------------------------------------------------------------
# Calibration selection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationSelection:
    """Which calibration block was used, and how it was arrived at."""

    bucket: str
    n: int
    bias_f: float
    sigma_f: float
    mae_f: Optional[float]
    #: Every block that was consulted and rejected, in order, with the reason.
    fallback_chain: Tuple[str, ...]
    mean_spread_f: Optional[float]
    warnings: Tuple[str, ...]


def _block_is_usable(block: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(block, Mapping):
        return False
    if not block.get("sufficient"):
        return False
    return block.get("bias_f") is not None and block.get("sigma_f") is not None


def _season_of(target: date) -> str:
    return _SEASON_BY_MONTH[target.month]


def select_calibration(
    payload: Mapping[str, Any],
    target_date: date,
    *,
    lead_hours: Optional[int] = None,
    require_published_spread: bool = False,
) -> CalibrationSelection:
    """Pick the calibration block for a target date, with an explicit fallback.

    Day-of resolution order is ``by_month_day_of`` -> ``by_season_day_of`` ->
    ``day_of``. Each step branches on the block's own ``sufficient`` flag -- an
    undersized bucket carries no statistics at all in the FR-2.2 schema, so
    reading ``sigma_f`` off it would read ``None``. Every descent is logged at
    WARNING and recorded in ``fallback_chain``; nothing falls back silently.

    A non-day-of lead bucket that is insufficient **raises**. Substituting the
    day-of block for a 5-day lead would swap a ~3 degF sigma in for one that is
    materially larger, and would size positions as if a 5-day forecast were a
    same-day one.
    """
    chain: List[str] = []
    warnings: List[str] = []

    if lead_hours is not None:
        lead_bucket = bucket_for_lead(int(lead_hours))
        if lead_bucket is None:
            raise ProbabilityEngineError(
                f"lead_hours={lead_hours} falls in no calibrated lead bucket"
            )
        if lead_bucket != DAY_OF_BUCKET:
            block = (payload.get("by_lead") or {}).get(lead_bucket)
            if not _block_is_usable(block):
                n = (block or {}).get("n", 0)
                raise ProbabilityEngineError(
                    f"{payload.get('city')}: lead bucket {lead_bucket!r} is "
                    f"insufficient (n={n} < {payload.get('min_bucket_n', MIN_BUCKET_N)}); "
                    f"refusing to substitute the day-of block for a "
                    f"{lead_hours}h lead"
                )
            return _selection_from(
                payload,
                f"by_lead:{lead_bucket}",
                block,
                chain,
                warnings,
                require_published_spread,
            )

    month_key = f"{target_date.year:04d}-{target_date.month:02d}"
    candidates = (
        (
            f"by_month_day_of:{month_key}",
            (payload.get("by_month_day_of") or {}).get(month_key),
        ),
        (
            f"by_season_day_of:{_season_of(target_date)}",
            (payload.get("by_season_day_of") or {}).get(_season_of(target_date)),
        ),
        ("day_of", payload.get("day_of")),
    )
    for name, block in candidates:
        if _block_is_usable(block):
            return _selection_from(
                payload, name, block, chain, warnings, require_published_spread
            )
        if block is None:
            reason = "absent"
        elif not block.get("sufficient"):
            reason = f"insufficient n={block.get('n', 0)}"
        else:
            reason = "no bias_f/sigma_f"
        chain.append(f"{name} ({reason})")
        logger.warning(
            "calibration fallback for %s %s: %s -> next",
            payload.get("city"),
            target_date.isoformat(),
            chain[-1],
        )

    raise ProbabilityEngineError(
        f"{payload.get('city')}: no usable calibration block for "
        f"{target_date.isoformat()}; consulted {chain}"
    )


def _selection_from(
    payload: Mapping[str, Any],
    name: str,
    block: Mapping[str, Any],
    chain: List[str],
    warnings: List[str],
    require_published_spread: bool,
) -> CalibrationSelection:
    spread_cov = block.get("spread_f_coverage") or {}
    mean_spread = spread_cov.get("mean_spread_f")
    if mean_spread is None:
        if require_published_spread:
            raise ProbabilityEngineError(
                f"{payload.get('city')} {name}: the calibration reports "
                f"mean_spread_f=null (source {payload.get('source')!r} publishes no "
                f"spread on any of its {spread_cov.get('rows_without_spread', '?')} "
                f"rows). Refusing require_published_spread=True: treating a null "
                f"spread as 0.0 would size every position as though the forecast "
                f"were certain."
            )
        warnings.append(
            f"{name}: no published forecast spread (mean_spread_f=null); sigma is "
            f"the calibrated error sigma only, identical for every day in the bucket"
        )
    if chain:
        warnings.append(f"calibration fallback taken: {' -> '.join(chain)} -> {name}")

    sigma = float(block["sigma_f"])
    if sigma <= 0.0:
        raise ProbabilityEngineError(f"{payload.get('city')} {name}: sigma_f={sigma}")
    bound = payload.get("sigma_sanity_bound_f")
    if bound is not None and sigma > float(bound):
        warnings.append(
            f"{name}: sigma_f={sigma:.4f} exceeds the calibration's own sanity "
            f"bound of {float(bound):.2f} degF (PRD Phase 2 EC-3)"
        )

    return CalibrationSelection(
        bucket=name,
        n=int(block.get("n", 0)),
        bias_f=float(block["bias_f"]),
        sigma_f=sigma,
        mae_f=block.get("mae_f"),
        fallback_chain=tuple(chain),
        mean_spread_f=mean_spread,
        warnings=tuple(warnings),
    )


def load_city_calibration(
    city: str,
    *,
    source: str = "gfs_mex",
    version: int = 1,
    calibration_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Load ``<CITY>_<SOURCE>_v<N>.json`` through workstream B's validator.

    :func:`~src.calibration.forecast_calibration.load_calibration` checks the
    ``schema_version`` and re-verifies the ``content_hash``, so a hand-edited or
    truncated artifact fails here rather than producing quiet nonsense.
    """
    path = os.path.join(
        calibration_dir or CALIBRATION_DIR,
        calibration_filename(city.upper(), source, version),
    )
    return load_calibration(path)


# ---------------------------------------------------------------------------
# The N/X-window regime
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegimeRisk:
    """Measured split of day-of forecast error by *when* the CLI max was set.

    ``inside`` = the CLI maximum's clock time fell inside
    :data:`NX_WINDOW_LST`, i.e. the window MOS ``N/X`` guidance actually covers.
    ``outside`` = it did not, so the settled value is a quantity the guidance
    never forecast.

    ``outside_sufficient`` is the same 20-day quorum the calibrator applies. A
    city below it has a *known but unquantified* risk: the frequency is
    measured, the error distribution is not, and no mixture may be built from
    it.
    """

    city: str
    station: str
    window_lst: Tuple[float, float]
    n_inside: int
    n_outside: int
    n_unknown_time: int
    p_outside: float
    bias_inside_f: Optional[float]
    sigma_inside_f: Optional[float]
    bias_outside_f: Optional[float]
    sigma_outside_f: Optional[float]
    outside_sufficient: bool

    @property
    def material(self) -> bool:
        """True when the regime is frequent enough to move a far-bracket price."""
        return self.p_outside >= 0.05

    def caveat(self) -> str:
        if self.outside_sufficient:
            return (
                f"{self.city}: {self.p_outside * 100:.1f}% of day-of samples "
                f"(n={self.n_outside}) set the CLI maximum outside "
                f"{self.window_lst[0]:.0f}-{self.window_lst[1]:.0f} LST, where the "
                f"error is bias {self.bias_outside_f:+.2f} / sigma "
                f"{self.sigma_outside_f:.2f} degF vs bias {self.bias_inside_f:+.2f} / "
                f"sigma {self.sigma_inside_f:.2f} inside. The single-Gaussian tails "
                f"understate that regime; discount far-bracket sells accordingly "
                f"or use regime='mixture'."
            )
        return (
            f"{self.city}: {self.p_outside * 100:.1f}% of day-of samples "
            f"(n={self.n_outside}) set the CLI maximum outside "
            f"{self.window_lst[0]:.0f}-{self.window_lst[1]:.0f} LST. That sample is "
            f"below the {MIN_BUCKET_N}-day quorum, so the regime's frequency is "
            f"measured but its error distribution is NOT; no mixture may be built "
            f"from it."
        )


#: Measured on ``data/forecast_archive/forecast_series_gfs_mex.csv`` (day-of
#: rows, lead in [-24, 12)) joined to ``data/weather_truth/cli_daily_high_*.csv``
#: on 2026-07-26 -- the identical 208-209 paired days the published calibrations
#: were built from. :func:`recompute_nx_window_regime` regenerates this table
#: from those read-only archives and ``tests/test_probability_engine.py`` asserts
#: the constants below still match, so they cannot drift away from the data.
NX_WINDOW_REGIME: Dict[str, RegimeRisk] = {
    "NY": RegimeRisk(
        city="NY",
        station="KNYC",
        window_lst=NX_WINDOW_LST,
        n_inside=172,
        n_outside=37,
        n_unknown_time=0,
        p_outside=37 / 209,
        bias_inside_f=0.4767,
        sigma_inside_f=2.8846,
        bias_outside_f=-1.8919,
        sigma_outside_f=4.6355,
        outside_sufficient=True,
    ),
    "CHI": RegimeRisk(
        city="CHI",
        station="KMDW",
        window_lst=NX_WINDOW_LST,
        n_inside=171,
        n_outside=38,
        n_unknown_time=0,
        p_outside=38 / 209,
        bias_inside_f=0.7076,
        sigma_inside_f=2.9279,
        bias_outside_f=-2.4474,
        sigma_outside_f=6.4292,
        outside_sufficient=True,
    ),
    "LAX": RegimeRisk(
        city="LAX",
        station="KLAX",
        window_lst=NX_WINDOW_LST,
        n_inside=206,
        n_outside=2,
        n_unknown_time=1,
        p_outside=2 / 208,
        bias_inside_f=-0.3641,
        sigma_inside_f=2.1410,
        bias_outside_f=None,
        sigma_outside_f=None,
        outside_sufficient=False,
    ),
    "MIA": RegimeRisk(
        city="MIA",
        station="KMIA",
        window_lst=NX_WINDOW_LST,
        n_inside=196,
        n_outside=2,
        n_unknown_time=10,
        p_outside=2 / 198,
        bias_inside_f=-0.2092,
        sigma_inside_f=1.6524,
        bias_outside_f=None,
        sigma_outside_f=None,
        outside_sufficient=False,
    ),
}

_CLOCK_RE = re.compile(r"^(\d{1,2})(\d{2})\s*(AM|PM)$")


def _parse_cli_clock(raw: Any) -> Optional[float]:
    """``"1159 PM"`` -> ``23.983`` local hours. ``None`` when unparseable.

    An unparseable time is counted (``n_unknown_time``) and excluded, never
    defaulted into either regime.
    """
    m = _CLOCK_RE.match(str(raw or "").strip().upper())
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3) == "PM":
        hour += 12
    return hour + int(m.group(2)) / 60.0


def _mean(xs: Sequence[float]) -> Optional[float]:
    return math.fsum(xs) / len(xs) if xs else None


def _stdev_sample(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    mu = _mean(xs)
    return math.sqrt(math.fsum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def recompute_nx_window_regime(
    *,
    forecast_csv: Optional[str] = None,
    truth_dir: Optional[str] = None,
    source: str = "gfs_mex",
    min_n: int = MIN_BUCKET_N,
) -> Dict[str, RegimeRisk]:
    """Rebuild :data:`NX_WINDOW_REGIME` from the read-only archives.

    Exists so the hard-coded table above is *derivable* rather than asserted:
    the test suite calls this and compares. Reads
    ``data/forecast_archive/forecast_series_<source>.csv`` and
    ``data/weather_truth/cli_daily_high_<STATION>.csv``; both are treated as
    read-only inputs and neither is written.
    """
    path = forecast_csv or os.path.join(
        FORECAST_ARCHIVE_DIR, f"forecast_series_{source}.csv"
    )
    if not os.path.exists(path):
        raise ProbabilityEngineError(f"no forecast series at {path}")

    # The day-of band is read from the calibrator's own LEAD_BUCKETS rather than
    # written down again here, so a change to its edges cannot leave this
    # recompute measuring a different sample than the calibration it checks.
    day_lo, day_hi = next(
        (lo, hi) for name, lo, hi in LEAD_BUCKETS if name == DAY_OF_BUCKET
    )
    day_of: Dict[str, Dict[str, float]] = {}
    stations: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["source"] != source:
                continue
            lead = int(row["lead_hours"])
            if not (day_lo <= lead < day_hi):
                continue
            city = row["city"]
            stations[city] = row["station"]
            day_of.setdefault(city, {})[row["target_date"]] = float(
                row["forecast_high_f"]
            )

    out: Dict[str, RegimeRisk] = {}
    for city in sorted(day_of):
        station = stations[city]
        tpath = os.path.join(
            truth_dir or WEATHER_TRUTH_DIR, f"cli_daily_high_{station}.csv"
        )
        if not os.path.exists(tpath):
            raise ProbabilityEngineError(f"no CLI truth file at {tpath}")
        truth: Dict[str, Tuple[Optional[int], Optional[float]]] = {}
        with open(tpath, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                high = (row.get("high") or "").strip()
                truth[row["date"]] = (
                    int(high) if high else None,
                    _parse_cli_clock(row.get("high_time")),
                )

        inside: List[float] = []
        outside: List[float] = []
        unknown = 0
        for target, forecast in sorted(day_of[city].items()):
            pair = truth.get(target)
            if pair is None or pair[0] is None:
                continue
            error = forecast - pair[0]  # ERROR_CONVENTION: positive = too warm
            when = pair[1]
            if when is None:
                unknown += 1
                continue
            (inside if NX_WINDOW_LST[0] <= when < NX_WINDOW_LST[1] else outside).append(
                error
            )

        n_in, n_out = len(inside), len(outside)
        sufficient = n_out >= min_n
        out[city] = RegimeRisk(
            city=city,
            station=station,
            window_lst=NX_WINDOW_LST,
            n_inside=n_in,
            n_outside=n_out,
            n_unknown_time=unknown,
            p_outside=(n_out / (n_in + n_out)) if (n_in + n_out) else 0.0,
            bias_inside_f=_round(_mean(inside)),
            sigma_inside_f=_round(_stdev_sample(inside)),
            bias_outside_f=_round(_mean(outside)) if sufficient else None,
            sigma_outside_f=_round(_stdev_sample(outside)) if sufficient else None,
            outside_sufficient=sufficient,
        )
    return out


def _round(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------------------
# The distribution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NormalComponent:
    """One Gaussian in the (possibly one-element) mixture over the daily high."""

    label: str
    weight: float
    mu_f: float
    sigma_f: float


@dataclass(frozen=True)
class EnsembleDiagnostics:
    """What the member cloud looked like -- measured, reported, and not used.

    ``sigma_f`` here is the raw member spread. It does **not** size the
    distribution (see the module docstring): a raw ensemble is under-dispersed,
    and no spread-skill relationship has been measured for these stations.
    """

    n_members: int
    mean_f: float
    median_f: float
    min_f: float
    max_f: float
    sigma_f: Optional[float]


@dataclass(frozen=True)
class BracketProbabilities:
    """A full distribution over one city's ladder for one target date.

    ``probabilities`` maps market ticker -> P(YES). ``pmf`` maps integer daily
    high -> mass over the truncated support. The two mass accounts are distinct
    and both are reported:

    * :attr:`pmf_tail_mass` -- mass outside the truncated integer support. It is
      *not* lost: it is added to whichever contract is open in that direction,
      so ``sum(pmf.values()) + pmf_tail_mass == 1``.
    * :attr:`uncovered_mass` -- ``1 - sum(probabilities.values())``. Zero for a
      complete ladder. Positive means the ladder has a gap or is missing a tail
      contract; negative means two contracts overlap. Never renormalized away.
    """

    city: str
    target_date: date
    mu_f: float
    sigma_f: float
    mode: str
    regime_model: str
    calibration_bucket: str
    calibration_source: str
    probabilities: Dict[str, float]
    pmf: Dict[int, float]
    pmf_tail_mass: float
    uncovered_mass: float
    partition: PartitionReport
    components: Tuple[NormalComponent, ...]
    calibration: CalibrationSelection
    regime_risk: Optional[RegimeRisk]
    ensemble: Optional[EnsembleDiagnostics]
    warnings: Tuple[str, ...]

    # -- convenience ------------------------------------------------------
    def p_yes(self, ticker: str) -> float:
        """P(YES) for one ticker. Raises on an unknown ticker (never 0.0)."""
        try:
            return self.probabilities[ticker]
        except KeyError:
            raise ProbabilityEngineError(
                f"{ticker} is not in this ladder; refusing to return 0.0 for a "
                f"contract that was never priced"
            ) from None

    def cdf(self, high_f: float) -> float:
        """``P(daily high <= high_f)`` under this distribution.

        The discrete CDF of the mixture, evaluated in closed form (not by
        summing the truncated pmf), so it stays exact in the far tails. Uses the
        same +/-0.5 continuity convention as the pmf: ``cdf(k)`` includes the
        whole integer ``k``.
        """
        edge = math.floor(float(high_f)) + 0.5
        return sum(
            c.weight * normal_cdf((edge - c.mu_f) / c.sigma_f) for c in self.components
        )

    def max_probability(self) -> float:
        return max(self.probabilities.values()) if self.probabilities else 0.0

    def entropy_bits(self) -> float:
        """Shannon entropy of the bracket distribution, in bits."""
        return entropy_bits(self.probabilities.values())

    def as_dict(self, *, nd: int = 6) -> Dict[str, Any]:
        """Report-friendly rendering. Rounding happens here and nowhere else."""
        return {
            "city": self.city,
            "target_date": self.target_date.isoformat(),
            "mu_f": round(self.mu_f, nd),
            "sigma_f": round(self.sigma_f, nd),
            "mode": self.mode,
            "regime_model": self.regime_model,
            "calibration_bucket": self.calibration_bucket,
            "calibration_source": self.calibration_source,
            "probabilities": {k: round(v, nd) for k, v in self.probabilities.items()},
            "sum_probabilities": round(math.fsum(self.probabilities.values()), nd),
            "uncovered_mass": round(self.uncovered_mass, nd),
            "pmf_tail_mass": round(self.pmf_tail_mass, nd),
            "partition": self.partition.describe(),
            "partition_issues": list(self.partition.issues),
            "components": [
                {
                    "label": c.label,
                    "weight": round(c.weight, nd),
                    "mu_f": round(c.mu_f, nd),
                    "sigma_f": round(c.sigma_f, nd),
                }
                for c in self.components
            ],
            "entropy_bits": round(self.entropy_bits(), nd),
            "max_probability": round(self.max_probability(), nd),
            "warnings": list(self.warnings),
        }


def integer_pmf(
    components: Sequence[NormalComponent],
    *,
    support_sigmas: float = DEFAULT_SUPPORT_SIGMAS,
    must_cover: Optional[Tuple[int, int]] = None,
) -> Tuple[Dict[int, float], float, float]:
    """PMF over integer daily highs, plus the truncated mass on each side.

    Returns ``(pmf, mass_below, mass_above)`` where ``mass_below`` is the exact
    analytic mass strictly below the support and ``mass_above`` the mass strictly
    above, so ``sum(pmf) + mass_below + mass_above == 1`` to float precision.
    Both are attributed to the open-ended contracts by the caller rather than
    being discarded.

    ``must_cover`` widens the support so it spans a given inclusive integer
    range, and callers pricing a ladder **must** pass the ladder's finite core.
    Without it the truncated mass is not safely attributable: a support that
    stops short of the top ``between`` bracket would sweep that bracket's mass
    into ``mass_above`` and hand it to the ``greater`` contract, which is a
    silent, entirely plausible-looking mispricing of the exact far bracket
    FR-3.1(a) wants to sell.
    """
    if not components:
        raise ProbabilityEngineError("no mixture components")
    lo = min(c.mu_f - support_sigmas * c.sigma_f for c in components)
    hi = max(c.mu_f + support_sigmas * c.sigma_f for c in components)
    k_lo = max(PHYSICAL_MIN_F, int(math.floor(lo)))
    k_hi = min(PHYSICAL_MAX_F, int(math.ceil(hi)))
    if must_cover is not None:
        k_lo = min(k_lo, int(must_cover[0]))
        k_hi = max(k_hi, int(must_cover[1]))
    if k_hi < k_lo:
        raise ProbabilityEngineError(
            f"empty integer support [{k_lo}, {k_hi}] for mu in "
            f"[{lo:.2f}, {hi:.2f}]; is the forecast physical?"
        )

    pmf: Dict[int, float] = {}
    for k in range(k_lo, k_hi + 1):
        mass = 0.0
        for c in components:
            z_lo = (k - 0.5 - c.mu_f) / c.sigma_f
            z_hi = (k + 0.5 - c.mu_f) / c.sigma_f
            mass += c.weight * _interval_mass(z_lo, z_hi)
        pmf[k] = mass

    mass_below = math.fsum(
        c.weight * normal_cdf((k_lo - 0.5 - c.mu_f) / c.sigma_f) for c in components
    )
    mass_above = math.fsum(
        c.weight * normal_sf((k_hi + 0.5 - c.mu_f) / c.sigma_f) for c in components
    )
    return pmf, mass_below, mass_above


def distribution_over_ladder(
    specs: Sequence[BracketSpec],
    components: Sequence[NormalComponent],
    *,
    support_sigmas: float = DEFAULT_SUPPORT_SIGMAS,
) -> Tuple[Dict[str, float], Dict[int, float], float, float, PartitionReport]:
    """Core engine: mixture + ladder -> ``P(YES)`` per ticker.

    For each bracket, the integers that pay YES are enumerated by asking
    :func:`~src.core.bracket_payoff.settles_yes` about every integer in the
    support -- this module never encodes a ``between``/``greater``/``less``
    inequality of its own. The residual truncated mass is then added to whichever
    contracts are open in that direction, discovered via
    :func:`~src.core.bracket_payoff.yes_bounds`.

    Returns ``(probabilities, pmf, pmf_tail_mass, uncovered_mass, partition)``.
    """
    partition = validate_ladder_partition(specs)

    # The support must span every finite bracket bound, or the truncated mass
    # would be attributed to the wrong contract. See ``integer_pmf``'s
    # ``must_cover``. The +/-1 covers the off-by-one on the one-sided types.
    finite: List[int] = []
    for spec in specs:
        ilo, ihi = _integer_bounds(spec)
        finite.extend(b for b in (ilo, ihi) if b is not None)
    must_cover = (min(finite) - 1, max(finite) + 1) if finite else None

    pmf, mass_below, mass_above = integer_pmf(
        components, support_sigmas=support_sigmas, must_cover=must_cover
    )
    support = sorted(pmf)

    probabilities: Dict[str, float] = {}
    for spec in specs:
        core = math.fsum(pmf[k] for k in support if settles_yes(spec, k))
        lo, hi = yes_bounds(spec)
        if math.isinf(lo):
            core += mass_below
        if math.isinf(hi):
            core += mass_above
        probabilities[spec.ticker] = min(1.0, max(0.0, core))

    uncovered = 1.0 - math.fsum(probabilities.values())
    return probabilities, pmf, mass_below + mass_above, uncovered, partition


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _build_components(
    mu_debiased: float,
    selection: CalibrationSelection,
    regime: str,
    regime_risk: Optional[RegimeRisk],
    raw_forecast_f: float,
    warnings: List[str],
) -> Tuple[Tuple[NormalComponent, ...], str]:
    """Single Gaussian, or the explicit two-component N/X-window mixture."""
    if regime == REGIME_SINGLE:
        return (
            (NormalComponent("calibrated", 1.0, mu_debiased, selection.sigma_f),),
            _REGIME_MODEL_SINGLE,
        )

    if regime not in (REGIME_MIXTURE, REGIME_MIXTURE_IF_AVAILABLE):
        raise ProbabilityEngineError(
            f"unknown regime {regime!r}; expected one of "
            f"{(REGIME_SINGLE, REGIME_MIXTURE, REGIME_MIXTURE_IF_AVAILABLE)}"
        )

    unavailable: Optional[str] = None
    if regime_risk is None:
        unavailable = "no measured N/X-window regime for this city"
    elif not regime_risk.outside_sufficient:
        unavailable = (
            f"outside-window sample n={regime_risk.n_outside} is below the "
            f"{MIN_BUCKET_N}-day quorum, so its error distribution is unmeasured"
        )
    if unavailable:
        if regime == REGIME_MIXTURE:
            raise ProbabilityEngineError(
                f"regime='mixture' requested but {unavailable}. Pass "
                f"regime='mixture_if_available' to accept the single-Gaussian "
                f"fallback with the degradation recorded, or regime='single'."
            )
        warnings.append(f"mixture unavailable, using single Gaussian: {unavailable}")
        return (
            (NormalComponent("calibrated", 1.0, mu_debiased, selection.sigma_f),),
            f"{_REGIME_MODEL_SINGLE} (mixture unavailable: {unavailable})",
        )

    assert regime_risk is not None  # narrowed above
    p_out = regime_risk.p_outside
    warnings.append(
        f"regime='mixture': {p_out * 100:.1f}% weight on the outside-window "
        f"component (bias {regime_risk.bias_outside_f:+.2f}, sigma "
        f"{regime_risk.sigma_outside_f:.2f} degF, n={regime_risk.n_outside}). Its "
        f"bias and sigma come from the regime table, NOT from the "
        f"{selection.bucket} calibration block, so this distribution mixes two "
        f"differently-bucketed samples."
    )
    return (
        (
            NormalComponent(
                "inside_nx_window",
                1.0 - p_out,
                raw_forecast_f - float(regime_risk.bias_inside_f),
                float(regime_risk.sigma_inside_f),
            ),
            NormalComponent(
                "outside_nx_window",
                p_out,
                raw_forecast_f - float(regime_risk.bias_outside_f),
                float(regime_risk.sigma_outside_f),
            ),
        ),
        _REGIME_MODEL_MIXTURE,
    )


def _finish(
    *,
    city: str,
    target_date: date,
    raw_forecast_f: float,
    selection: CalibrationSelection,
    specs: Sequence[BracketSpec],
    mode: str,
    regime: str,
    payload: Mapping[str, Any],
    forecast_source: Optional[str],
    support_sigmas: float,
    ensemble: Optional[EnsembleDiagnostics],
    extra_warnings: Sequence[str] = (),
) -> BracketProbabilities:
    warnings: List[str] = list(selection.warnings) + list(extra_warnings)

    calibration_source = str(payload.get("source", "?"))
    if forecast_source is not None and forecast_source != calibration_source:
        warnings.append(
            f"SOURCE MISMATCH: forecast source is {forecast_source!r} but the "
            f"calibration was built from {calibration_source!r}. bias_f and "
            f"sigma_f describe {calibration_source!r}'s errors; applying them to "
            f"another model's output is unvalidated."
        )

    # The N/X-window caveat is attached unconditionally, not only when a mixture
    # is refused. A single-Gaussian distribution for NY or CHI is understating
    # its own tails ~18% of the time, and a consumer that never sees that fact
    # is the failure mode workstream B flagged.
    regime_risk = NX_WINDOW_REGIME.get(city.upper())
    if regime_risk is None:
        warnings.append(
            f"no measured N/X-window regime for city {city!r}; the settlement-"
            f"window risk is unquantified here"
        )
    elif regime == REGIME_SINGLE:
        warnings.append(regime_risk.caveat())

    mu = raw_forecast_f - selection.bias_f  # ERROR_CONVENTION, see module docstring
    components, regime_model = _build_components(
        mu, selection, regime, regime_risk, raw_forecast_f, warnings
    )

    probabilities, pmf, tail, uncovered, partition = distribution_over_ladder(
        specs, components, support_sigmas=support_sigmas
    )

    if not partition.complete:
        warnings.append(
            f"ladder is not a partition ({'; '.join(partition.issues)}); "
            f"{uncovered:+.6f} of the mass is unaccounted for and has NOT been "
            f"renormalized away"
        )

    # Effective sigma of the mixture, for reporting. For a single component this
    # is exactly sigma_f; for a mixture it is the law-of-total-variance sigma,
    # which is the honest scalar summary of a distribution that is not Gaussian.
    mu_mix = math.fsum(c.weight * c.mu_f for c in components)
    var_mix = math.fsum(
        c.weight * (c.sigma_f**2 + (c.mu_f - mu_mix) ** 2) for c in components
    )

    return BracketProbabilities(
        city=city.upper(),
        target_date=target_date,
        mu_f=mu_mix,
        sigma_f=math.sqrt(var_mix),
        mode=mode,
        regime_model=regime_model,
        calibration_bucket=selection.bucket,
        calibration_source=calibration_source,
        probabilities=probabilities,
        pmf=pmf,
        pmf_tail_mass=tail,
        uncovered_mass=uncovered,
        partition=partition,
        components=components,
        calibration=selection,
        regime_risk=regime_risk,
        ensemble=ensemble,
        warnings=tuple(warnings),
    )


def bracket_probabilities_point(
    *,
    city: str,
    target_date: Any,
    forecast_high_f: float,
    specs: Sequence[BracketSpec],
    calibration: Mapping[str, Any],
    lead_hours: Optional[int] = None,
    regime: str = REGIME_SINGLE,
    forecast_source: Optional[str] = None,
    require_published_spread: bool = False,
    support_sigmas: float = DEFAULT_SUPPORT_SIGMAS,
) -> BracketProbabilities:
    """P(YES) for every bracket, from a single forecast high (the MOS/NWS path).

    ``mu = forecast_high_f - bias_f`` and ``sigma = sigma_f``, both taken from
    the calibration block chosen by :func:`select_calibration`.

    :param calibration: a payload from :func:`load_city_calibration`.
    :param lead_hours: forecast lead. ``None`` selects the day-of chain.
    :param regime: ``"single"``, ``"mixture"``, or ``"mixture_if_available"``.
    :param forecast_source: the source label of ``forecast_high_f``. Supplying
        it enables the source-mismatch warning; omitting it disables that check.
    """
    target = _coerce_date(target_date)
    selection = select_calibration(
        calibration,
        target,
        lead_hours=lead_hours,
        require_published_spread=require_published_spread,
    )
    return _finish(
        city=city,
        target_date=target,
        raw_forecast_f=float(forecast_high_f),
        selection=selection,
        specs=specs,
        mode=MODE_POINT,
        regime=regime,
        payload=calibration,
        forecast_source=forecast_source,
        support_sigmas=support_sigmas,
        ensemble=None,
    )


def bracket_probabilities_ensemble(
    *,
    city: str,
    target_date: Any,
    members_f: Sequence[float],
    specs: Sequence[BracketSpec],
    calibration: Mapping[str, Any],
    lead_hours: Optional[int] = None,
    regime: str = REGIME_SINGLE,
    forecast_source: Optional[str] = None,
    require_published_spread: bool = False,
    support_sigmas: float = DEFAULT_SUPPORT_SIGMAS,
    min_members: int = 2,
) -> BracketProbabilities:
    """P(YES) for every bracket, from an ensemble's member daily-highs.

    ``mu = mean(members) - bias_f``; ``sigma`` is the **calibrated** sigma, not
    the member spread. See the module docstring for why, and for what that
    discards. The raw member statistics are returned in
    :attr:`BracketProbabilities.ensemble` as a diagnostic.

    An empty or single-member ensemble raises: a "mean" of one member is a point
    forecast wearing an ensemble's name, and should go through
    :func:`bracket_probabilities_point` where that is visible in ``mode``.
    """
    values = [float(v) for v in members_f]
    if len(values) < int(min_members):
        raise ProbabilityEngineError(
            f"{city}: {len(values)} ensemble member(s) < min_members="
            f"{min_members}; refusing to build a distribution from an ensemble "
            f"that is not one"
        )
    if any(math.isnan(v) or math.isinf(v) for v in values):
        raise ProbabilityEngineError(f"{city}: ensemble members contain NaN/inf")

    target = _coerce_date(target_date)
    selection = select_calibration(
        calibration,
        target,
        lead_hours=lead_hours,
        require_published_spread=require_published_spread,
    )

    ordered = sorted(values)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else 0.5 * (ordered[n // 2 - 1] + ordered[n // 2])
    member_sigma = _stdev_sample(ordered)
    diagnostics = EnsembleDiagnostics(
        n_members=n,
        mean_f=_mean(ordered),
        median_f=median,
        min_f=ordered[0],
        max_f=ordered[-1],
        sigma_f=member_sigma,
    )

    extra = [
        f"ensemble dispersion is NOT used: member spread {member_sigma:.3f} degF "
        f"(n={n}) is reported as a diagnostic only; sigma comes from the "
        f"{selection.bucket} calibration block ({selection.sigma_f:.4f} degF)."
        if member_sigma is not None
        else f"ensemble member spread undefined (n={n})",
    ]
    if member_sigma is not None and member_sigma > selection.sigma_f:
        extra.append(
            f"member spread {member_sigma:.3f} exceeds the calibrated sigma "
            f"{selection.sigma_f:.4f}: this day looks less certain than the "
            f"bucket average, and the calibrated sigma may understate it."
        )

    return _finish(
        city=city,
        target_date=target,
        raw_forecast_f=diagnostics.mean_f,
        selection=selection,
        specs=specs,
        mode=MODE_ENSEMBLE,
        regime=regime,
        payload=calibration,
        forecast_source=forecast_source,
        support_sigmas=support_sigmas,
        ensemble=diagnostics,
        extra_warnings=extra,
    )


__all__ = [
    "PHYSICAL_MIN_F",
    "PHYSICAL_MAX_F",
    "DEFAULT_SUPPORT_SIGMAS",
    "NX_WINDOW_LST",
    "NX_WINDOW_REGIME",
    "MODE_POINT",
    "MODE_ENSEMBLE",
    "REGIME_SINGLE",
    "REGIME_MIXTURE",
    "REGIME_MIXTURE_IF_AVAILABLE",
    "ProbabilityEngineError",
    "PartitionReport",
    "CalibrationSelection",
    "RegimeRisk",
    "NormalComponent",
    "EnsembleDiagnostics",
    "BracketProbabilities",
    "normal_cdf",
    "normal_sf",
    "entropy_bits",
    "validate_ladder_partition",
    "specs_from_markets",
    "select_calibration",
    "load_city_calibration",
    "recompute_nx_window_regime",
    "integer_pmf",
    "distribution_over_ladder",
    "bracket_probabilities_point",
    "bracket_probabilities_ensemble",
]
