"""Lag/drift projection of the AAA national average gas price (PRD FR-4.2).

`KXAAAGASM` settles on the **AAA national average retail price for regular
gasoline published on one specific calendar date** — not a monthly average.
Trading closes at 23:59 ET the evening *before* that value publishes
(``close_time`` 2026-08-31T03:59Z for the 2026-08-31 settlement), so the final
decision is always made on a projection and never on the settled value. That one
fact sets every design constraint in this module.

WHAT THIS MODULE PROMISES
-------------------------
1. **No lookahead, structurally.** :func:`project` clamps the series to
   ``as_of`` at its own first statement and every internal helper refuses a
   series that is not clamped (:class:`GasLookaheadError`). See
   `NO-LOOKAHEAD ENFORCEMENT`_ below.
2. **Abort, never default.** Insufficient, stale, gappy or implausible input
   raises :class:`GasDataUnavailable`. A last-observed-value fallback here would
   be a plausible number that sizes a real trade, which is exactly what
   ``abort-on-missing-critical-input`` forbids.
3. **Strictly greater.** :func:`prob_above` and :func:`settles_yes_gas`
   implement ``value > floor_strike``. A settle exactly equal to the strike
   pays NO.

THE MODEL
---------
Retail gasoline is a sticky, strongly autocorrelated series that follows
wholesale (RBOB) with a multi-day pass-through lag. The projection is therefore
a **direct h-step-ahead drift regression** on the daily AAA series with the
lagged wholesale drift as a covariate::

    A(t + h) - A(t)  =  b0
                      + b1 * (A(t) - A(t - k)) / k          # AAA momentum
                      + b2 * (R(t - L) - R(t - L - k)) / k   # lagged RBOB drift
                      + eps

    point = A(as_of) + prediction at t = as_of

* ``h`` = ``lead_days`` = ``target_date - as_of``. One horizon is fitted per
  call — the horizon actually being asked about — so there is no bank of
  per-horizon coefficients to drift out of sync, and no redundant fitting.
* ``k`` = :attr:`ProjectionConfig.momentum_days` (default 7).
* ``L`` = the **fitted** pass-through lag, selected by maximising the Pearson
  correlation between the daily AAA change and the RBOB daily change lagged
  ``L`` days, over ``L`` in ``0..lag_max``. It is a fitted parameter, not an
  assumed constant, and it is published in
  :attr:`GasProjection.model_version` (``lagdrift_v1+rbobL7``) so every
  downstream artifact records which lag produced it.
* Regressors are standardised before the fit purely for conditioning; the
  reported coefficients are never exposed, only the prediction.

``sigma`` is the OLS **prediction** standard error at the requested point::

    sigma = s * sqrt(1 + x0' (X'X)^-1 x0),   s^2 = SSE / (n - p)

so it widens naturally with lead time (longer horizons have larger residuals)
and widens again when today's momentum/wholesale state sits far from the
training centroid — the situation in which the fit deserves least trust. It is
**not** a ``sqrt(h)`` scaling of a single day-ahead error; nothing here assumes
a variance law.

Honest limitation: the training windows overlap (``t`` and ``t+1`` share
``h-1`` days), so the residuals are autocorrelated. The residual *variance*
estimate stays consistent, but the ``(X'X)^-1`` parameter-uncertainty inflation
is optimistic under overlap, and the ``n - p`` degrees-of-freedom correction is
optimistic too. The authoritative check on ``sigma`` is therefore the held-out
month-end MAE that Phase 4 exit criterion 2 requires of the backtest artifact,
not this in-sample number.

EIA WEEKLY
----------
The EIA weekly retail series is loaded and, at the most recent common date,
checked against AAA for level consistency (a large divergence is logged, not
silenced). It is **not** a regressor by default: it measures the same
underlying quantity as AAA at one seventh the frequency, so its trailing drift
is near-collinear with the AAA momentum term and adding it mostly inflates the
parameter-uncertainty term without adding information. Set
:attr:`ProjectionConfig.use_eia_covariate` to include it explicitly.

.. _NO-LOOKAHEAD ENFORCEMENT:

NO-LOOKAHEAD ENFORCEMENT
------------------------
Three independent mechanisms, because a backtest that peeks is worth less than
no backtest at all (contract §0.2):

1. **Boundary clamp.** :func:`project` rejects ``as_of >= target_date`` and then
   immediately rebinds its series to ``series.observed_through(as_of)``. The
   caller's unclamped object is never referenced again.
2. **Typed guard.** :meth:`GasSeries.observed_through` stamps
   :attr:`GasSeries.clamped_to`. Every helper that touches data calls
   :func:`_require_clamped`, which raises :class:`GasLookaheadError` unless the
   stamp equals the requested ``as_of`` **and** a re-scan finds no row dated
   after it. A hand-built series with a forged stamp still fails the re-scan.
3. **Hash witness.** :attr:`GasProjection.inputs_hash` covers exactly the rows
   used. Appending any future row leaves the hash — and therefore the whole
   projection — bit-identical, which
   ``tests/test_gas_projection.py::test_future_rows_cannot_influence_projection``
   asserts directly.

DO NOT SETTLE GAS THROUGH ``src.core.bracket_payoff``
-----------------------------------------------------
``bracket_payoff`` is the single source of truth for *daily-high temperature*
brackets, where the outcome is a whole degree and ``strike_type='greater'``
means ``high >= floor + 1``. Gas settles on a continuous price to three
decimals and ``strike_type='greater'`` means ``value > floor`` — a $4.60 strike
pays YES at $4.601, not at $5.60. Both series report the identical
``strike_type`` string for two different rules, so the gas payoff lives here, in
:func:`settles_yes_gas`, and ``tests/test_gas_projection.py`` pins the
difference so nobody wires one into the other.
"""

from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Model family. The published :attr:`GasProjection.model_version` appends the
#: fitted covariate state: ``lagdrift_v1+rbobL7`` or ``lagdrift_v1+nocovar``.
MODEL_FAMILY = "lagdrift_v1"

#: AAA publishes the national average to three decimals (contract §1), so the
#: settlement value lives on a $0.001 grid. Strict-greater is implemented
#: against that grid rather than being waved away as measure-zero.
AAA_TICK = 0.001

#: 97.5th percentile of the standard normal, for the 95% CI.
_Z_95 = 1.959963984540054

_SQRT2 = math.sqrt(2.0)

QUALITY_OK = "ok"
QUALITY_SUSPECT = "suspect"

_AAA_FILENAME = "aaa_daily_national.csv"
_RBOB_FILENAME = "rbob_daily.csv"
_EIA_FILENAME = "eia_weekly_regular.csv"


class GasDataUnavailable(RuntimeError):
    """Input is insufficient, stale, gappy or implausible for a projection.

    Callers must treat this as a hard abort. There is deliberately no
    "last observed value" fallback: a plausible default here would be handed
    straight to position sizing.
    """


class GasLookaheadError(RuntimeError):
    """An internal helper was handed data that is not clamped to ``as_of``.

    This is a programming error, not a data condition — it means a code path
    bypassed :meth:`GasSeries.observed_through` and could see the future.
    """


# ---------------------------------------------------------------------------
# Series containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GasObservation:
    """One dated value from one of the persisted series (contract §1)."""

    date: date
    value: float
    quality: str = QUALITY_OK
    source: str = ""


@dataclass(frozen=True)
class GasSeries:
    """The three persisted series, optionally clamped to an ``as_of`` date.

    ``clamped_to`` is the no-lookahead stamp. A series built by
    :meth:`from_rows` or :meth:`from_csv_dir` is **unclamped**
    (``clamped_to is None``) and cannot be fitted on; :func:`project` clamps it.
    """

    aaa: Tuple[GasObservation, ...] = ()
    rbob: Tuple[GasObservation, ...] = ()
    eia_weekly: Tuple[GasObservation, ...] = ()
    clamped_to: Optional[date] = None

    # -- construction ----------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        aaa: Iterable[GasObservation] = (),
        rbob: Iterable[GasObservation] = (),
        eia_weekly: Iterable[GasObservation] = (),
    ) -> "GasSeries":
        """Build an unclamped series, each component sorted ascending by date."""
        key = lambda o: o.date  # noqa: E731
        return cls(
            aaa=tuple(sorted(aaa, key=key)),
            rbob=tuple(sorted(rbob, key=key)),
            eia_weekly=tuple(sorted(eia_weekly, key=key)),
            clamped_to=None,
        )

    @classmethod
    def from_csv_dir(cls, directory) -> "GasSeries":
        """Load the contract §1 CSVs from ``data/gas_truth/``-shaped directory.

        ``aaa_daily_national.csv`` is required; ``rbob_daily.csv`` and
        ``eia_weekly_regular.csv`` are optional and absence is logged, not
        defaulted. Reads the schema declared in the data contract rather than
        importing another workstream's provider module, so this loader does not
        depend on that module existing.

        :raises GasDataUnavailable: if the AAA file is missing or unparseable.
        """
        root = Path(directory)
        aaa_path = root / _AAA_FILENAME
        if not aaa_path.is_file():
            raise GasDataUnavailable(
                f"{aaa_path} not found; the AAA daily series is the projection's "
                f"critical input and there is no substitute for it"
            )
        aaa = _read_csv_series(aaa_path, "date", with_quality=True)
        if not aaa:
            raise GasDataUnavailable(f"{aaa_path} contains no usable rows")

        rbob: Tuple[GasObservation, ...] = ()
        rbob_path = root / _RBOB_FILENAME
        if rbob_path.is_file():
            rbob = _read_csv_series(rbob_path, "date", with_quality=False)
        else:
            logger.info(
                "[GasProjection] %s absent; projecting without the lagged "
                "wholesale covariate",
                rbob_path,
            )

        eia: Tuple[GasObservation, ...] = ()
        eia_path = root / _EIA_FILENAME
        if eia_path.is_file():
            eia = _read_csv_series(eia_path, "week_ending", with_quality=False)
        else:
            logger.info("[GasProjection] %s absent", eia_path)

        return cls.from_rows(aaa=aaa, rbob=rbob, eia_weekly=eia)

    # -- clamping --------------------------------------------------------

    def observed_through(self, as_of: date) -> "GasSeries":
        """Return a copy holding only rows dated ``<= as_of``, stamped as such.

        This is the only sanctioned way to prepare a series for fitting.
        """
        as_of = _coerce_date(as_of, "as_of")
        return GasSeries(
            aaa=tuple(o for o in self.aaa if o.date <= as_of),
            rbob=tuple(o for o in self.rbob if o.date <= as_of),
            eia_weekly=tuple(o for o in self.eia_weekly if o.date <= as_of),
            clamped_to=as_of,
        )


@dataclass(frozen=True)
class ProjectionConfig:
    """Gates and hyper-parameters. Every threshold is an abort, not a clip."""

    #: FR-4.2 requires >= 12 months of backfilled history.
    min_history_days: int = 365
    #: Beyond this lead the direct regression has no useful training support.
    max_lead_days: int = 45
    #: ``as_of`` must be an actually observed AAA date, so the grid never
    #: forward-extrapolates its own anchor value.
    require_observed_as_of: bool = True
    #: Longest run of consecutive missing AAA days that may be interpolated.
    max_gap_days: int = 10
    #: Wayback coverage is ~24/30 days per month, i.e. ~20% filled.
    max_interpolated_fraction: float = 0.35
    momentum_days: int = 7
    lag_max: int = 14
    min_lag_pairs: int = 120
    min_lag_corr: float = 0.05
    min_train_pairs: int = 60
    #: Newest raw RBOB observation may be this stale relative to ``as_of``
    #: before the covariate is dropped (forward-filling a stale wholesale
    #: print into the prediction row would be a silent default).
    max_rbob_staleness_days: int = 5
    use_eia_covariate: bool = False
    eia_momentum_days: int = 14
    #: Logged (not aborted) when AAA and EIA levels disagree by more than this
    #: at their most recent common date. The two series measure slightly
    #: different baskets, so a small spread is expected.
    max_aaa_eia_divergence: float = 0.30
    #: Conservative floor on the reported sigma: a fit that claims sub-half-cent
    #: certainty two weeks out is not to be believed. Binding is logged.
    min_sigma: float = 0.005
    #: Contract §1 plausibility band. A point outside it means the fit or the
    #: data is broken, so abort rather than publish it.
    plausible_low: float = 1.00
    plausible_high: float = 9.00
    include_suspect: bool = False


DEFAULT_PROJECTION_CONFIG = ProjectionConfig()


@dataclass(frozen=True)
class GasProjection:
    """Projected AAA national average for one settlement date (contract §2)."""

    target_date: date  # the settlement date being projected
    as_of: date  # last observation used; MUST be < target_date
    point: float  # projected AAA national average, USD/gal
    sigma: float  # 1-sigma of the projection error, USD/gal
    ci_low: float  # 95% CI
    ci_high: float
    lead_days: int  # target_date - as_of
    n_train: int  # observations in the fit
    n_interpolated: int  # AAA days filled in memory to build the grid
    model_version: str  # e.g. "lagdrift_v1+rbobL7"
    inputs_hash: str  # SHA-256 over the exact input rows used


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def project(
    as_of: date,
    target_date: date,
    series: GasSeries,
    config: ProjectionConfig = DEFAULT_PROJECTION_CONFIG,
) -> GasProjection:
    """Project the AAA national average on ``target_date``.

    Uses only rows dated ``<= as_of``. Raises :class:`GasDataUnavailable`
    rather than returning a default.

    :param as_of: last observation date the projection may see. Must be an
        observed AAA date under the default config.
    :param target_date: the settlement date (the AAA publication date the
        contract resolves against).
    :param series: the loaded series; clamped internally, never mutated.
    :param config: gates and hyper-parameters.
    """
    as_of = _coerce_date(as_of, "as_of")
    target_date = _coerce_date(target_date, "target_date")

    # (1) Boundary: refuse a non-future target before touching any data. An
    #     as_of at or after the target is the definition of lookahead here.
    if as_of >= target_date:
        raise GasDataUnavailable(
            f"as_of {as_of} is not before target_date {target_date}; a "
            f"projection that may see the settlement date is worthless "
            f"(trading closes the evening before the value publishes)"
        )
    if not isinstance(series, GasSeries):
        raise GasDataUnavailable(
            f"series must be a GasSeries, got {type(series).__name__}"
        )

    lead_days = (target_date - as_of).days
    if lead_days > config.max_lead_days:
        raise GasDataUnavailable(
            f"lead of {lead_days}d exceeds max_lead_days "
            f"{config.max_lead_days}d; the direct regression has no usable "
            f"training support that far out"
        )

    # (2) Clamp. Everything below reads `observed` only; `series` is dead here.
    observed = series.observed_through(as_of)
    del series

    return _project_clamped(as_of, target_date, lead_days, observed, config)


def prob_above(proj: GasProjection, floor_strike: float) -> float:
    """``P(settle > floor_strike)``.

    STRICTLY greater, and monotonically decreasing in ``floor_strike``.

    AAA publishes to three decimals, so "strictly greater than K" is
    "at least K + $0.001" on the published grid. The half-tick continuity
    correction implements that explicitly::

        P = 1 - Phi((K + AAA_TICK/2 - point) / sigma)

    The correction is worth about 0.02pt at a $0.05 sigma — immaterial to
    sizing, but it makes the strict inequality visible in the code rather than
    resting on the "a continuous distribution has no atoms" hand-wave. The
    settlement rule itself is strict (contract §0.1) and :func:`settles_yes_gas`
    is where an actual outcome is judged.
    """
    if not isinstance(proj, GasProjection):
        raise TypeError(f"proj must be a GasProjection, got {type(proj).__name__}")
    strike = _finite_float(floor_strike, "floor_strike")
    sigma = float(proj.sigma)
    if not math.isfinite(sigma) or sigma <= 0.0:
        # A zero/NaN sigma would make this a step function or a NaN; either
        # would be handed to an EV gate. project() cannot emit one, so a
        # caller must have built the dataclass by hand.
        raise ValueError(
            f"projection sigma must be finite and positive, got {proj.sigma!r}"
        )
    z = (strike + AAA_TICK / 2.0 - float(proj.point)) / sigma
    return _norm_sf(z)


def settles_yes_gas(value: float, floor_strike: float) -> bool:
    """True iff a ``strike_type='greater'`` gas market settles YES.

    ``value > floor_strike``, strictly. A settle exactly equal to the strike
    pays NO (contract §0.1). This is the gas payoff; it is deliberately NOT
    ``src.core.bracket_payoff.settles_yes``, whose ``greater`` rule is the
    whole-degree temperature rule ``high >= floor + 1``.
    """
    v = _finite_float(value, "value")
    k = _finite_float(floor_strike, "floor_strike")
    return v > k


def bracket_pmf(proj: GasProjection, strikes: Sequence[float]) -> List[float]:
    """Implied probability mass over a ``greater``-ladder's outcome bands.

    ``strikes`` must be strictly ascending. Returns ``len(strikes) + 1``
    probabilities: ``P(v <= s0)``, then each ``P(s_{i-1} < v <= s_i)``, then
    ``P(v > s_last)``. Built as differences of :func:`prob_above` so the result
    sums to 1 by construction rather than by renormalisation, and so it inherits
    the strict-greater convention exactly.
    """
    ordered = [_finite_float(s, "strike") for s in strikes]
    if any(b <= a for a, b in zip(ordered, ordered[1:])):
        raise ValueError(f"strikes must be strictly ascending, got {ordered}")
    tails = [prob_above(proj, s) for s in ordered]
    out = [1.0 - tails[0]]
    out.extend(tails[i] - tails[i + 1] for i in range(len(tails) - 1))
    out.append(tails[-1])
    return out


def fitted_lag_from_model_version(model_version: str) -> Optional[int]:
    """Recover the fitted RBOB lag from a published ``model_version``.

    ``"lagdrift_v1+rbobL7"`` -> ``7``; ``"lagdrift_v1+nocovar"`` -> ``None``.
    """
    text = str(model_version or "")
    marker = "+rbobL"
    if marker not in text:
        return None
    try:
        return int(text.split(marker, 1)[1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def _require_clamped(observed: GasSeries, as_of: date) -> None:
    """Structural no-lookahead guard for every helper that touches data.

    Checks the stamp AND re-scans the rows, so a hand-built series carrying a
    forged ``clamped_to`` is still rejected.
    """
    if not isinstance(observed, GasSeries):
        raise GasLookaheadError(
            f"expected a clamped GasSeries, got {type(observed).__name__}"
        )
    if observed.clamped_to != as_of:
        raise GasLookaheadError(
            f"series is clamped_to={observed.clamped_to!r}, expected {as_of!r}; "
            f"call GasSeries.observed_through(as_of) before fitting"
        )
    for name, rows in (
        ("aaa", observed.aaa),
        ("rbob", observed.rbob),
        ("eia_weekly", observed.eia_weekly),
    ):
        for obs in rows:
            if obs.date > as_of:
                raise GasLookaheadError(
                    f"{name} row dated {obs.date} is after as_of {as_of} despite "
                    f"the clamp stamp; refusing to fit on future data"
                )


def _project_clamped(
    as_of: date,
    target_date: date,
    lead_days: int,
    observed: GasSeries,
    config: ProjectionConfig,
) -> GasProjection:
    _require_clamped(observed, as_of)

    # --- AAA: the critical input -------------------------------------
    aaa_map = _observation_map(observed.aaa, "aaa", config.include_suspect)
    if not aaa_map:
        raise GasDataUnavailable(
            f"no usable AAA observations at or before {as_of} "
            f"(quality filter: include_suspect={config.include_suspect})"
        )
    aaa_first = min(aaa_map)
    aaa_last = max(aaa_map)

    if config.require_observed_as_of and aaa_last != as_of:
        raise GasDataUnavailable(
            f"as_of {as_of} is not an observed AAA date (newest observation is "
            f"{aaa_last}, {(as_of - aaa_last).days}d earlier). The projection "
            f"anchors on A(as_of); extrapolating that anchor forward would be "
            f"the silent last-observed-value default this module refuses"
        )

    history_days = (aaa_last - aaa_first).days + 1
    if history_days < config.min_history_days:
        raise GasDataUnavailable(
            f"AAA history spans {history_days}d ({aaa_first}..{aaa_last}), "
            f"below the FR-4.2 minimum of {config.min_history_days}d"
        )

    aaa_grid, aaa_dates, n_interpolated, max_gap = _dense_grid(
        aaa_map, aaa_first, aaa_last, method="linear"
    )
    if max_gap > config.max_gap_days:
        raise GasDataUnavailable(
            f"AAA series has a {max_gap}-day gap, above max_gap_days "
            f"{config.max_gap_days}; interpolating across it would invent the "
            f"very movement the model is trying to measure"
        )
    filled_fraction = n_interpolated / float(len(aaa_grid))
    if filled_fraction > config.max_interpolated_fraction:
        raise GasDataUnavailable(
            f"{n_interpolated}/{len(aaa_grid)} AAA days ({filled_fraction:.1%}) "
            f"are interpolated, above max_interpolated_fraction "
            f"{config.max_interpolated_fraction:.0%}"
        )

    # --- RBOB: lagged covariate, dropped loudly if unusable -----------
    rbob_map = _observation_map(observed.rbob, "rbob", config.include_suspect)
    rbob_lookup, rbob_reason = _rbob_lookup(rbob_map, as_of, config)

    fitted_lag: Optional[int] = None
    if rbob_lookup is not None:
        fitted_lag, lag_corr, lag_pairs = _fit_pass_through_lag(
            aaa_grid, aaa_dates, rbob_lookup, config
        )
        if fitted_lag is None:
            rbob_reason = (
                f"no lag in 0..{config.lag_max} reached min_lag_corr "
                f"{config.min_lag_corr} (best {lag_corr:.3f} on {lag_pairs} pairs)"
            )
            rbob_lookup = None
        else:
            logger.info(
                "[GasProjection] fitted RBOB pass-through lag L=%dd "
                "(corr=%.3f on %d daily pairs)",
                fitted_lag,
                lag_corr,
                lag_pairs,
            )
    if rbob_lookup is None:
        logger.warning(
            "[GasProjection] projecting WITHOUT the lagged wholesale covariate: "
            "%s. model_version records this as +nocovar.",
            rbob_reason,
        )

    # --- EIA: level cross-check, optional covariate -------------------
    eia_map = _observation_map(
        observed.eia_weekly, "eia_weekly", config.include_suspect
    )
    _check_aaa_eia_levels(aaa_map, eia_map, config)
    eia_lookup = (
        _step_lookup(eia_map) if (config.use_eia_covariate and eia_map) else None
    )
    if config.use_eia_covariate and eia_lookup is None:
        logger.warning(
            "[GasProjection] use_eia_covariate is set but no EIA weekly rows "
            "are available at or before %s; fitting without it",
            as_of,
        )

    # --- design matrix -------------------------------------------------
    design = _build_design(
        aaa_grid,
        aaa_dates,
        lead_days,
        fitted_lag,
        rbob_lookup,
        eia_lookup,
        config,
    )
    n_train = design.n_train
    if n_train < config.min_train_pairs:
        raise GasDataUnavailable(
            f"only {n_train} usable (t, t+{lead_days}) training pairs, below "
            f"min_train_pairs {config.min_train_pairs}"
        )

    delta, sigma = _ols_predict(design, config)

    point = float(aaa_grid[-1] + delta)
    if not math.isfinite(point):
        raise GasDataUnavailable("projected point is not finite")
    if not (config.plausible_low <= point <= config.plausible_high):
        raise GasDataUnavailable(
            f"projected point ${point:.3f}/gal is outside the plausibility band "
            f"[${config.plausible_low:.2f}, ${config.plausible_high:.2f}]; the "
            f"fit or the input is broken and this number must not size a trade"
        )

    if sigma < config.min_sigma:
        logger.info(
            "[GasProjection] sigma floor binding: fitted %.5f -> %.5f "
            "(min_sigma); a sub-half-cent claim %dd out is not credible",
            sigma,
            config.min_sigma,
            lead_days,
        )
        sigma = config.min_sigma

    model_version = MODEL_FAMILY + (
        f"+rbobL{fitted_lag}" if fitted_lag is not None else "+nocovar"
    )
    if config.use_eia_covariate and eia_lookup is not None:
        model_version += "+eia"

    inputs_hash = _inputs_hash(aaa_map, rbob_map, eia_map)

    proj = GasProjection(
        target_date=target_date,
        as_of=as_of,
        point=point,
        sigma=float(sigma),
        ci_low=float(point - _Z_95 * sigma),
        ci_high=float(point + _Z_95 * sigma),
        lead_days=lead_days,
        n_train=n_train,
        n_interpolated=n_interpolated,
        model_version=model_version,
        inputs_hash=inputs_hash,
    )
    logger.info(
        "[GasProjection] %s target=%s as_of=%s lead=%dd point=%.3f sigma=%.4f "
        "ci=[%.3f, %.3f] n_train=%d n_interp=%d hash=%s",
        proj.model_version,
        proj.target_date,
        proj.as_of,
        proj.lead_days,
        proj.point,
        proj.sigma,
        proj.ci_low,
        proj.ci_high,
        proj.n_train,
        proj.n_interpolated,
        proj.inputs_hash[:12],
    )
    return proj


@dataclass(frozen=True)
class _Design:
    """Standardised design matrix plus the prediction row."""

    X: np.ndarray  # (n, p) with intercept in column 0
    y: np.ndarray  # (n,)
    x0: np.ndarray  # (p,) standardised prediction row
    n_train: int
    names: Tuple[str, ...]


def _build_design(
    aaa_grid: np.ndarray,
    aaa_dates: Sequence[date],
    lead_days: int,
    fitted_lag: Optional[int],
    rbob_lookup,
    eia_lookup,
    config: ProjectionConfig,
) -> _Design:
    """Assemble the direct h-step regression, then standardise the regressors.

    Row ``t`` uses only data dated ``<= aaa_dates[t]`` on the predictor side and
    ``aaa_dates[t + lead_days]`` on the target side; the prediction row is
    ``t = len(grid) - 1`` (``as_of``), so it too reads only observed history.
    """
    k = int(config.momentum_days)
    n_grid = len(aaa_grid)
    ek = int(config.eia_momentum_days)

    def momentum(t: int) -> Optional[float]:
        if t - k < 0:
            return None
        return float(aaa_grid[t] - aaa_grid[t - k]) / k

    def rbob_drift(t: int) -> Optional[float]:
        if fitted_lag is None or rbob_lookup is None:
            return None
        d = aaa_dates[t] - timedelta(days=fitted_lag)
        near = rbob_lookup(d)
        far = rbob_lookup(d - timedelta(days=k))
        if near is None or far is None:
            return None
        return (near - far) / k

    def eia_drift(t: int) -> Optional[float]:
        if eia_lookup is None:
            return None
        d = aaa_dates[t]
        near = eia_lookup(d)
        far = eia_lookup(d - timedelta(days=ek))
        if near is None or far is None:
            return None
        return (near - far) / ek

    builders = [("aaa_momentum", momentum)]
    if fitted_lag is not None and rbob_lookup is not None:
        builders.append((f"rbob_drift_L{fitted_lag}", rbob_drift))
    if eia_lookup is not None:
        builders.append(("eia_drift", eia_drift))

    names = tuple(name for name, _ in builders)

    # Prediction row first: if the current state is incomplete there is nothing
    # to project from, and it is better to say so than to fit and then fail.
    t0 = n_grid - 1
    x0_raw: List[float] = []
    for name, fn in builders:
        value = fn(t0)
        if value is None:
            raise GasDataUnavailable(
                f"predictor {name!r} is unavailable at as_of {aaa_dates[t0]}; "
                f"refusing to substitute a default for it"
            )
        x0_raw.append(value)

    rows: List[List[float]] = []
    targets: List[float] = []
    for t in range(k, n_grid - lead_days):
        values = [fn(t) for _, fn in builders]
        if any(v is None for v in values):
            continue
        rows.append([float(v) for v in values])  # type: ignore[arg-type]
        targets.append(float(aaa_grid[t + lead_days] - aaa_grid[t]))

    if not rows:
        raise GasDataUnavailable(
            f"no usable (t, t+{lead_days}) training pairs could be built"
        )

    raw = np.asarray(rows, dtype=float)
    y = np.asarray(targets, dtype=float)
    x0 = np.asarray(x0_raw, dtype=float)

    # Standardise for conditioning only: the fit is equivariant, but a raw
    # design whose columns are ~1e-3 next to an intercept of 1 has a condition
    # number in the millions and makes the degeneracy gate meaningless.
    mean = raw.mean(axis=0)
    sd = raw.std(axis=0, ddof=0)
    keep = sd > 0.0
    if not keep.any():
        raise GasDataUnavailable(
            "every regressor is constant across the training window; the fit "
            "would be an intercept-only mean and carries no information"
        )
    if not keep.all():
        dropped = [names[i] for i in range(len(names)) if not keep[i]]
        logger.warning(
            "[GasProjection] dropping constant regressor(s) %s", ", ".join(dropped)
        )
        raw = raw[:, keep]
        x0 = x0[keep]
        mean = mean[keep]
        sd = sd[keep]
        names = tuple(names[i] for i in range(len(keep)) if keep[i])

    z = (raw - mean) / sd
    z0 = (x0 - mean) / sd

    X = np.column_stack([np.ones(len(z)), z])
    x0_full = np.concatenate([[1.0], z0])
    return _Design(X=X, y=y, x0=x0_full, n_train=len(y), names=names)


def _ols_predict(design: _Design, config: ProjectionConfig) -> Tuple[float, float]:
    """Return ``(predicted change, prediction standard error)``.

    ``sigma`` is the textbook OLS prediction standard error, which is the
    residual scale inflated by the parameter uncertainty at the prediction
    point. Both terms matter: the first says how well the model fits history at
    this horizon, the second says how unusual today's state is relative to that
    history.
    """
    X, y, x0 = design.X, design.y, design.x0
    n, p = X.shape
    if n <= p:
        raise GasDataUnavailable(
            f"{n} training rows for {p} parameters leaves no residual degrees "
            f"of freedom"
        )

    xtx = X.T @ X
    cond = float(np.linalg.cond(xtx))
    if not math.isfinite(cond) or cond > 1e10:
        raise GasDataUnavailable(
            f"design matrix is near-singular (cond={cond:.3g}); the regressors "
            f"are collinear and the coefficients are not identified"
        )

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    sse = float(resid @ resid)
    s2 = sse / (n - p)
    if not math.isfinite(s2) or s2 < 0.0:
        raise GasDataUnavailable(f"residual variance is not usable ({s2!r})")

    leverage = float(x0 @ np.linalg.solve(xtx, x0))
    var_pred = s2 * (1.0 + max(0.0, leverage))
    if not math.isfinite(var_pred) or var_pred <= 0.0:
        raise GasDataUnavailable(
            f"prediction variance is not usable ({var_pred!r}); the fit is "
            f"degenerate and must not produce a confidence interval"
        )

    return float(x0 @ beta), math.sqrt(var_pred)


def _fit_pass_through_lag(
    aaa_grid: np.ndarray,
    aaa_dates: Sequence[date],
    rbob_lookup,
    config: ProjectionConfig,
) -> Tuple[Optional[int], float, int]:
    """Select the wholesale-to-retail pass-through lag by daily-change correlation.

    Returns ``(lag or None, best correlation, pairs at the best lag)``. Ties go
    to the smallest lag so the choice is deterministic.
    """
    d_aaa = np.diff(aaa_grid)
    best_lag: Optional[int] = None
    best_corr = float("-inf")
    best_pairs = 0

    for lag in range(0, int(config.lag_max) + 1):
        xs: List[float] = []
        ys: List[float] = []
        for i in range(1, len(aaa_grid)):
            d = aaa_dates[i] - timedelta(days=lag)
            near = rbob_lookup(d)
            far = rbob_lookup(d - timedelta(days=1))
            if near is None or far is None:
                continue
            xs.append(near - far)
            ys.append(float(d_aaa[i - 1]))
        if len(xs) < config.min_lag_pairs:
            continue
        a = np.asarray(xs, dtype=float)
        b = np.asarray(ys, dtype=float)
        if a.std(ddof=0) <= 0.0 or b.std(ddof=0) <= 0.0:
            continue
        corr = float(np.corrcoef(a, b)[0, 1])
        if not math.isfinite(corr):
            continue
        if corr > best_corr:
            best_corr, best_lag, best_pairs = corr, lag, len(xs)

    if best_lag is None or best_corr < config.min_lag_corr:
        return None, (best_corr if math.isfinite(best_corr) else 0.0), best_pairs
    return best_lag, best_corr, best_pairs


# ---------------------------------------------------------------------------
# Series plumbing
# ---------------------------------------------------------------------------


def _observation_map(
    rows: Sequence[GasObservation], label: str, include_suspect: bool
) -> Dict[date, float]:
    """Quality-filtered ``{date: value}``, aborting on contradictory duplicates."""
    out: Dict[date, float] = {}
    for obs in rows:
        quality = (obs.quality or QUALITY_OK).strip().lower()
        if quality != QUALITY_OK and not include_suspect:
            continue
        value = float(obs.value)
        if not math.isfinite(value):
            raise GasDataUnavailable(
                f"{label} row {obs.date} has a non-finite value {obs.value!r}"
            )
        prior = out.get(obs.date)
        if prior is not None and abs(prior - value) > 1e-9:
            raise GasDataUnavailable(
                f"{label} has two different values for {obs.date} "
                f"({prior} and {value}); the input is ambiguous and a "
                f"silent pick would be arbitrary"
            )
        out[obs.date] = value
    return out


def _dense_grid(
    values: Dict[date, float], start: date, end: date, method: str
) -> Tuple[np.ndarray, List[date], int, int]:
    """Regular daily grid over ``[start, end]``.

    ``method="linear"`` interpolates interior gaps; ``method="ffill"`` carries
    the last observation forward (correct for a wholesale spot price on a
    non-trading day, which has no value to interpolate *to*).

    Both endpoints must be observed for ``linear`` — :func:`_project_clamped`
    guarantees that for AAA — so no interior gap is ever extrapolated.
    Returns ``(values, dates, n_filled, longest_missing_run)``.
    """
    n_days = (end - start).days + 1
    dates = [start + timedelta(days=i) for i in range(n_days)]
    present = [values.get(d) for d in dates]

    if method == "linear":
        if present[0] is None or present[-1] is None:
            raise GasDataUnavailable(
                "linear grid requires observed endpoints; got "
                f"start={start} present={present[0] is not None}, "
                f"end={end} present={present[-1] is not None}"
            )
    elif method == "ffill":
        if present[0] is None:
            raise GasDataUnavailable(
                f"forward-fill grid requires an observed first day ({start})"
            )
    else:  # pragma: no cover - programming error
        raise ValueError(f"unknown grid method {method!r}")

    out = [0.0] * n_days
    n_filled = 0
    longest_gap = 0
    i = 0
    while i < n_days:
        if present[i] is not None:
            out[i] = float(present[i])
            i += 1
            continue
        j = i
        while j < n_days and present[j] is None:
            j += 1
        run = j - i
        longest_gap = max(longest_gap, run)
        left = out[i - 1]
        if method == "linear":
            right = float(present[j])
            step = (right - left) / (run + 1)
            for m in range(run):
                out[i + m] = left + step * (m + 1)
        else:
            for m in range(run):
                out[i + m] = left
        n_filled += run
        i = j

    return np.asarray(out, dtype=float), dates, n_filled, longest_gap


def _rbob_lookup(rbob_map: Dict[date, float], as_of: date, config: ProjectionConfig):
    """``(lookup_fn, reason_if_unusable)`` for the RBOB covariate.

    The lookup is a forward-filled step function over the observed RBOB dates:
    ``lookup(d)`` returns the most recent print dated ``<= d``, or ``None``
    before the series starts. Forward fill (not interpolation) is the right
    treatment for a spot price on a weekend or holiday.
    """
    if not rbob_map:
        return None, "no RBOB rows available"
    newest = max(rbob_map)
    staleness = (as_of - newest).days
    if staleness > config.max_rbob_staleness_days:
        return None, (
            f"newest RBOB print is {newest} ({staleness}d before as_of {as_of}), "
            f"beyond max_rbob_staleness_days {config.max_rbob_staleness_days}"
        )
    return _step_lookup(rbob_map), ""


def _step_lookup(values: Dict[date, float]):
    """Forward-filled step lookup: most recent value dated ``<= d``, else None."""
    keys = sorted(values)
    ordinals = [d.toordinal() for d in keys]
    vals = [values[d] for d in keys]

    def lookup(d: date) -> Optional[float]:
        target = d.toordinal()
        lo, hi = 0, len(ordinals) - 1
        if target < ordinals[0]:
            return None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ordinals[mid] <= target:
                lo = mid
            else:
                hi = mid - 1
        return vals[lo]

    return lookup


def _check_aaa_eia_levels(
    aaa_map: Dict[date, float],
    eia_map: Dict[date, float],
    config: ProjectionConfig,
) -> None:
    """Log (never silence) a large AAA-vs-EIA level divergence.

    AAA quotes regular retail, EIA quotes all-formulations retail, so a few
    cents of spread is structural. A large divergence means one of the two
    scrapes is reading the wrong number, which is worth an operator's attention
    but is not grounds to abort a projection built on AAA alone.
    """
    if not eia_map or not aaa_map:
        return
    newest_eia = max(eia_map)
    aaa_on_or_before = [d for d in aaa_map if d <= newest_eia]
    if not aaa_on_or_before:
        return
    d_aaa = max(aaa_on_or_before)
    gap = abs(aaa_map[d_aaa] - eia_map[newest_eia])
    if gap > config.max_aaa_eia_divergence:
        logger.warning(
            "[GasProjection] AAA (%s $%.3f) and EIA weekly (%s $%.3f) differ by "
            "$%.3f, above max_aaa_eia_divergence $%.2f — check both scrapes",
            d_aaa,
            aaa_map[d_aaa],
            newest_eia,
            eia_map[newest_eia],
            gap,
            config.max_aaa_eia_divergence,
        )


def _inputs_hash(
    aaa_map: Dict[date, float],
    rbob_map: Dict[date, float],
    eia_map: Dict[date, float],
) -> str:
    """SHA-256 over exactly the input rows the fit could see.

    Deterministic, order-independent (each series is emitted sorted) and
    covering only the clamped, quality-filtered rows. That makes the hash a
    witness for the no-lookahead property: appending a future row must leave it
    unchanged.
    """
    digest = hashlib.sha256()
    for label, table in (("AAA", aaa_map), ("RBOB", rbob_map), ("EIA", eia_map)):
        digest.update(f"[{label}]\n".encode("utf-8"))
        for d in sorted(table):
            digest.update(f"{d.isoformat()}|{table[d]:.6f}\n".encode("utf-8"))
    return digest.hexdigest()


def _read_csv_series(path: Path, date_column: str, with_quality: bool):
    """Read a contract §1 CSV into ``GasObservation`` rows."""
    rows: List[GasObservation] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or date_column not in reader.fieldnames:
            raise GasDataUnavailable(
                f"{path} is missing the {date_column!r} column "
                f"(header: {reader.fieldnames})"
            )
        for line_no, row in enumerate(reader, start=2):
            raw_date = (row.get(date_column) or "").strip()
            raw_value = (row.get("value") or "").strip()
            if not raw_date or not raw_value:
                continue
            try:
                parsed = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise GasDataUnavailable(
                    f"{path}:{line_no} has an unparseable {date_column} "
                    f"{raw_date!r}"
                ) from exc
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise GasDataUnavailable(
                    f"{path}:{line_no} has an unparseable value {raw_value!r}"
                ) from exc
            quality = (
                (row.get("quality") or QUALITY_OK).strip().lower()
                if with_quality
                else QUALITY_OK
            )
            rows.append(
                GasObservation(
                    date=parsed,
                    value=value,
                    quality=quality or QUALITY_OK,
                    source=(row.get("source") or "").strip(),
                )
            )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _norm_sf(z: float) -> float:
    """``P(Z > z)`` for a standard normal, stable in both tails."""
    return 0.5 * math.erfc(z / _SQRT2)


def _finite_float(value, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number, got {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return out


def _coerce_date(value, label: str) -> date:
    """Accept a ``date``, a ``datetime`` or an ISO string; reject anything else."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise GasDataUnavailable(f"{label}={value!r} is not an ISO date") from exc
    raise GasDataUnavailable(
        f"{label} must be a date, got {type(value).__name__}: {value!r}"
    )


__all__ = [
    "AAA_TICK",
    "DEFAULT_PROJECTION_CONFIG",
    "GasDataUnavailable",
    "GasLookaheadError",
    "GasObservation",
    "GasProjection",
    "GasSeries",
    "MODEL_FAMILY",
    "ProjectionConfig",
    "bracket_pmf",
    "fitted_lag_from_model_version",
    "prob_above",
    "project",
    "settles_yes_gas",
]
