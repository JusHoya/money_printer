"""Tests for the FR-4.2 AAA gas projection model.

The properties under test are the ones that would cost money if they were wrong:

* a future row cannot influence a projection (contract §0.2 lookahead);
* ``prob_above`` is strictly greater and strictly monotone (contract §0.1);
* insufficient or stale input aborts instead of producing a plausible default;
* doubling sigma measurably flattens the bracket distribution (the Phase 2
  perturbation check, reused);
* the gas ``greater`` rule is NOT the temperature ``greater`` rule.

The synthetic fixture generates AAA as a pass-through of lagged RBOB daily
changes with a known lag, so the lag-selection step has a ground truth to be
scored against rather than merely "a number came out".
"""

import math
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from src.models.gas_projection import (
    AAA_TICK,
    GasDataUnavailable,
    GasLookaheadError,
    GasObservation,
    GasProjection,
    GasSeries,
    ProjectionConfig,
    _project_clamped,
    _require_clamped,
    bracket_pmf,
    fitted_lag_from_model_version,
    prob_above,
    project,
    settles_yes_gas,
)

# --------------------------------------------------------------------------
# Fixture: AAA follows RBOB with a known lag
# --------------------------------------------------------------------------

START = date(2025, 2, 1)
N_DAYS = 560
TRUE_LAG = 6
ANCHOR = 4.10


def _synthetic(
    n_days: int = N_DAYS,
    lag: int = TRUE_LAG,
    seed: int = 11,
    pass_through: float = 0.35,
    noise: float = 0.004,
):
    """Deterministic (dates, aaa, rbob) arrays. AAA follows RBOB with ``lag``."""
    rng = np.random.default_rng(seed)
    dates = [START + timedelta(days=i) for i in range(n_days)]
    d_rbob = rng.normal(0.0, 0.035, size=n_days)
    rbob = 2.20 + np.cumsum(d_rbob)
    aaa = np.empty(n_days)
    aaa[0] = ANCHOR
    for i in range(1, n_days):
        driver = d_rbob[i - lag] if i - lag >= 1 else 0.0
        aaa[i] = aaa[i - 1] + pass_through * driver + rng.normal(0.0, noise)
    return dates, aaa, rbob


def _series(
    drop=(),
    with_rbob: bool = True,
    rbob_through=None,
    suspect=(),
    extra_aaa=(),
    extra_rbob=(),
    **kwargs,
) -> GasSeries:
    """Build a :class:`GasSeries` from the synthetic fixture.

    ``drop`` removes AAA dates entirely (a missing row, per contract §1.1).
    ``suspect`` marks AAA dates ``quality=suspect``.
    """
    dates, aaa, rbob = _synthetic(**kwargs)
    drop_set, suspect_set = set(drop), set(suspect)
    aaa_rows = [
        GasObservation(
            date=d,
            value=float(v),
            quality="suspect" if d in suspect_set else "ok",
            source="aaa_wayback",
        )
        for d, v in zip(dates, aaa)
        if d not in drop_set
    ]
    aaa_rows.extend(extra_aaa)
    rbob_rows = []
    if with_rbob:
        rbob_rows = [
            GasObservation(date=d, value=float(v), source="eia_bulk")
            for d, v in zip(dates, rbob)
            if rbob_through is None or d <= rbob_through
        ]
        rbob_rows.extend(extra_rbob)
    return GasSeries.from_rows(aaa=aaa_rows, rbob=rbob_rows)


AS_OF = START + timedelta(days=N_DAYS - 1)
TARGET = AS_OF + timedelta(days=14)


@pytest.fixture(scope="module")
def series() -> GasSeries:
    return _series()


@pytest.fixture(scope="module")
def proj(series) -> GasProjection:
    return project(AS_OF, TARGET, series)


# --------------------------------------------------------------------------
# Contract §2 shape
# --------------------------------------------------------------------------


def test_projection_populates_every_contract_field(proj):
    assert proj.target_date == TARGET
    assert proj.as_of == AS_OF
    assert proj.as_of < proj.target_date
    assert proj.lead_days == 14
    assert 1.0 < proj.point < 9.0
    assert proj.sigma > 0.0
    assert proj.ci_low < proj.point < proj.ci_high
    # 95% CI is +/- 1.96 sigma about the point.
    assert proj.ci_high - proj.point == pytest.approx(1.959963984540054 * proj.sigma)
    assert proj.n_train >= 60
    assert proj.n_interpolated == 0
    assert proj.model_version.startswith("lagdrift_v1")
    assert len(proj.inputs_hash) == 64


def test_projection_is_deterministic(series):
    assert project(AS_OF, TARGET, series) == project(AS_OF, TARGET, series)


def test_sigma_widens_with_lead_time(series):
    sigmas = [
        project(AS_OF, AS_OF + timedelta(days=h), series).sigma for h in (1, 7, 14, 28)
    ]
    assert sigmas == sorted(sigmas), sigmas
    assert sigmas[-1] > 2 * sigmas[0]


# --------------------------------------------------------------------------
# The fitted lag
# --------------------------------------------------------------------------


def test_fitted_lag_recovers_the_generating_lag(proj):
    """The pass-through lag is fitted, and on data with a known lag it finds it."""
    assert fitted_lag_from_model_version(proj.model_version) == TRUE_LAG
    assert proj.model_version == f"lagdrift_v1+rbobL{TRUE_LAG}"


@pytest.mark.parametrize("true_lag", [0, 3, 9])
def test_fitted_lag_tracks_the_fixture_across_lags(true_lag):
    s = _series(lag=true_lag, seed=23)
    p = project(AS_OF, TARGET, s)
    assert fitted_lag_from_model_version(p.model_version) == true_lag


def test_fitted_lag_survives_a_business_day_only_rbob_series():
    """The real RBOB series has no weekend rows; the fixture must not depend on
    a covariate observed every calendar day.

    Measured on the live file 2026-07-29: ``data/gas_truth/rbob_daily.csv`` holds
    390 rows spanning 2025-01-02..2026-07-27, i.e. trading days only. Weekends
    are carried forward (a spot price has no weekend value to interpolate *to*),
    so Monday's daily change spans the weekend. The lag is still recovered.
    """
    dates, aaa, rbob = _synthetic()
    weekdays_only = GasSeries.from_rows(
        aaa=[GasObservation(date=d, value=float(v)) for d, v in zip(dates, aaa)],
        rbob=[
            GasObservation(date=d, value=float(v))
            for d, v in zip(dates, rbob)
            if d.weekday() < 5
        ],
    )
    p = project(AS_OF, TARGET, weekdays_only)
    assert fitted_lag_from_model_version(p.model_version) == TRUE_LAG


def test_absent_rbob_degrades_to_a_documented_nocovar_model(series):
    p = project(AS_OF, TARGET, _series(with_rbob=False))
    assert p.model_version == "lagdrift_v1+nocovar"
    assert fitted_lag_from_model_version(p.model_version) is None
    assert p.sigma > 0.0


def test_stale_rbob_drops_the_covariate_rather_than_forward_filling_it():
    """A stale wholesale print must not be carried into the prediction row."""
    stale = _series(rbob_through=AS_OF - timedelta(days=12))
    p = project(AS_OF, TARGET, stale)
    assert p.model_version == "lagdrift_v1+nocovar"


# --------------------------------------------------------------------------
# No lookahead
# --------------------------------------------------------------------------


def test_future_rows_cannot_influence_projection(series, proj):
    """The hash witness: appending future rows changes nothing at all."""
    poisoned = _series(
        extra_aaa=[
            GasObservation(date=AS_OF + timedelta(days=d), value=9.99)
            for d in (1, 3, 14)
        ],
        extra_rbob=[
            GasObservation(date=AS_OF + timedelta(days=d), value=99.0)
            for d in (1, 3, 14)
        ],
    )
    after = project(AS_OF, TARGET, poisoned)
    assert after == proj
    assert after.inputs_hash == proj.inputs_hash


def test_target_date_value_is_never_read(series):
    """The settlement day's own value cannot reach the projection.

    Trading closes the evening before publication, so a projection that moves
    when the target date's value changes is invalid by construction.
    """
    target_known = _series(
        extra_aaa=[GasObservation(date=TARGET, value=8.88)],
    )
    assert project(AS_OF, TARGET, target_known) == project(AS_OF, TARGET, series)


def test_as_of_at_or_after_target_aborts(series):
    with pytest.raises(GasDataUnavailable, match="not before target_date"):
        project(TARGET, TARGET, series)
    with pytest.raises(GasDataUnavailable, match="not before target_date"):
        project(TARGET + timedelta(days=1), TARGET, series)


def test_unclamped_series_is_refused_by_the_internal_guard(series):
    with pytest.raises(GasLookaheadError, match="clamped_to=None"):
        _require_clamped(series, AS_OF)
    with pytest.raises(GasLookaheadError, match="clamped_to=None"):
        _project_clamped(AS_OF, TARGET, 14, series, ProjectionConfig())


def test_forged_clamp_stamp_is_caught_by_the_rescan(series):
    """A stamp is not trusted on its own; the rows are re-scanned."""
    forged = GasSeries(
        aaa=series.aaa + (GasObservation(date=AS_OF + timedelta(days=2), value=9.99),),
        rbob=series.rbob,
        clamped_to=AS_OF,
    )
    with pytest.raises(GasLookaheadError, match="despite"):
        _require_clamped(forged, AS_OF)
    with pytest.raises(GasLookaheadError, match="despite"):
        _project_clamped(AS_OF, TARGET, 14, forged, ProjectionConfig())


def test_clamp_excludes_the_boundary_correctly(series):
    clamped = series.observed_through(AS_OF - timedelta(days=30))
    assert clamped.clamped_to == AS_OF - timedelta(days=30)
    assert max(o.date for o in clamped.aaa) == AS_OF - timedelta(days=30)
    _require_clamped(clamped, AS_OF - timedelta(days=30))


# --------------------------------------------------------------------------
# prob_above: strictly greater, strictly monotone
# --------------------------------------------------------------------------


def test_prob_above_implements_strictly_greater(proj):
    """Strict-greater is implemented on the published $0.001 grid, not assumed."""
    strike = round(proj.point, 2)
    strict = prob_above(proj, strike)
    non_strict = 0.5 * math.erfc(((strike - proj.point) / proj.sigma) / math.sqrt(2.0))
    assert strict < non_strict
    # The gap is exactly the half-tick continuity correction.
    expected = 0.5 * math.erfc(
        ((strike + AAA_TICK / 2.0 - proj.point) / proj.sigma) / math.sqrt(2.0)
    )
    assert strict == pytest.approx(expected, abs=1e-15)


def test_settles_yes_gas_pays_no_at_the_strike():
    assert settles_yes_gas(4.601, 4.60) is True
    assert settles_yes_gas(4.60, 4.60) is False
    assert settles_yes_gas(4.599, 4.60) is False


def test_prob_above_is_strictly_monotonically_decreasing(proj):
    strikes = [
        proj.point - 5 * proj.sigma + i * (10 * proj.sigma / 400) for i in range(401)
    ]
    probs = [prob_above(proj, s) for s in strikes]
    assert all(b < a for a, b in zip(probs, probs[1:])), "not strictly decreasing"


def test_prob_above_is_bounded_and_saturates(proj):
    assert prob_above(proj, proj.point - 50 * proj.sigma) == pytest.approx(1.0)
    assert prob_above(proj, proj.point + 50 * proj.sigma) == pytest.approx(0.0)
    for k in (0.0, 2.0, 4.0, 6.0, 20.0):
        assert 0.0 <= prob_above(proj, k) <= 1.0


def test_prob_above_refuses_a_degenerate_sigma(proj):
    for bad in (0.0, -0.01, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="sigma"):
            prob_above(replace(proj, sigma=bad), 4.30)


def test_prob_above_refuses_a_non_numeric_strike(proj):
    for bad in (None, "high", float("nan"), True):
        with pytest.raises((ValueError, TypeError)):
            prob_above(proj, bad)


# --------------------------------------------------------------------------
# Perturbation: doubling sigma flattens the distribution
# --------------------------------------------------------------------------


def _entropy(pmf):
    return -math.fsum(p * math.log(p) for p in pmf if p > 0.0)


def test_doubling_sigma_flattens_the_bracket_distribution(proj):
    """PRD Phase 2 EC-4's perturbation check, applied to the gas ladder.

    "Flattens" is carried by Shannon entropy over the ladder, the same metric
    the weather probability engine's EC-4 rests on. The ladder is centred on the
    projection so the entropy comparison is made where the mass is; placed far
    outside the ladder, doubling sigma moves mass back toward the finite bands
    and the largest-band criterion reverses (documented in
    ``src/calibration/probability_engine.py``).
    """
    strikes = [round(proj.point + i * 0.05, 3) for i in range(-4, 5)]
    tight = bracket_pmf(proj, strikes)
    wide = bracket_pmf(replace(proj, sigma=2.0 * proj.sigma), strikes)

    assert math.fsum(tight) == pytest.approx(1.0, abs=1e-12)
    assert math.fsum(wide) == pytest.approx(1.0, abs=1e-12)
    assert _entropy(wide) > _entropy(tight)
    # The peak band must lose mass to its neighbours.
    assert max(wide) < max(tight)


def test_doubling_sigma_pulls_out_of_the_money_tails_toward_a_half(proj):
    doubled = replace(proj, sigma=2.0 * proj.sigma)
    for offset in (-3.0, -2.0, 2.0, 3.0):
        strike = proj.point + offset * proj.sigma
        tight = prob_above(proj, strike)
        wide = prob_above(doubled, strike)
        assert abs(wide - 0.5) < abs(tight - 0.5), (strike, tight, wide)


def test_bracket_pmf_requires_ascending_strikes(proj):
    with pytest.raises(ValueError, match="ascending"):
        bracket_pmf(proj, [4.30, 4.20])
    with pytest.raises(ValueError, match="ascending"):
        bracket_pmf(proj, [4.30, 4.30])


# --------------------------------------------------------------------------
# Abort, never default
# --------------------------------------------------------------------------


def test_short_history_aborts_rather_than_fitting_on_what_it_has():
    short_as_of = START + timedelta(days=200)
    s = _series().observed_through(short_as_of)
    s = GasSeries.from_rows(aaa=s.aaa, rbob=s.rbob)  # drop the clamp stamp
    with pytest.raises(GasDataUnavailable, match="below the FR-4.2 minimum"):
        project(short_as_of, short_as_of + timedelta(days=14), s)


def test_as_of_later_than_the_newest_observation_aborts(series):
    """No last-observed-value anchor: an as_of past the data is an abort."""
    with pytest.raises(GasDataUnavailable, match="not an observed AAA date"):
        project(AS_OF + timedelta(days=2), TARGET + timedelta(days=2), series)


def test_a_long_gap_aborts_rather_than_being_interpolated_across():
    gap = [AS_OF - timedelta(days=200 - i) for i in range(12)]
    with pytest.raises(GasDataUnavailable, match="12-day gap"):
        project(AS_OF, TARGET, _series(drop=gap))


def test_too_much_interpolation_aborts():
    every_other = [START + timedelta(days=i) for i in range(1, N_DAYS - 1, 2)]
    with pytest.raises(GasDataUnavailable, match="max_interpolated_fraction"):
        project(AS_OF, TARGET, _series(drop=every_other))


def test_empty_series_aborts():
    with pytest.raises(GasDataUnavailable, match="no usable AAA observations"):
        project(AS_OF, TARGET, GasSeries.from_rows())


def test_lead_beyond_max_aborts(series):
    with pytest.raises(GasDataUnavailable, match="exceeds max_lead_days"):
        project(AS_OF, AS_OF + timedelta(days=60), series)


def test_implausible_point_aborts(series):
    """A point outside the plausibility band must never reach position sizing."""
    tight = ProjectionConfig(plausible_low=1.00, plausible_high=3.00)
    with pytest.raises(GasDataUnavailable, match="plausibility band"):
        project(AS_OF, TARGET, series, config=tight)


def test_contradictory_duplicate_rows_abort():
    dup = _series(
        extra_aaa=[GasObservation(date=START + timedelta(days=100), value=1.23)]
    )
    with pytest.raises(GasDataUnavailable, match="two different values"):
        project(AS_OF, TARGET, dup)


def test_non_finite_value_aborts():
    bad = _series(
        extra_aaa=[GasObservation(date=AS_OF - timedelta(days=1), value=float("nan"))]
    )
    with pytest.raises(GasDataUnavailable, match="non-finite"):
        project(AS_OF, TARGET, bad)


def test_project_rejects_a_non_series_argument():
    with pytest.raises(GasDataUnavailable, match="must be a GasSeries"):
        project(AS_OF, TARGET, [1, 2, 3])


def test_suspect_rows_are_excluded_from_the_fit(series, proj):
    """A ``quality=suspect`` row is dropped, so its day becomes interpolated."""
    marked = _series(suspect=[START + timedelta(days=100)])
    p = project(AS_OF, TARGET, marked)
    assert p.n_interpolated == proj.n_interpolated + 1
    assert p.inputs_hash != proj.inputs_hash

    included = ProjectionConfig(include_suspect=True)
    p2 = project(AS_OF, TARGET, marked, config=included)
    assert p2.n_interpolated == proj.n_interpolated


def test_missing_rows_are_counted_as_interpolated(proj):
    holes = [AS_OF - timedelta(days=100 + i) for i in range(3)]
    p = project(AS_OF, TARGET, _series(drop=holes))
    assert p.n_interpolated == 3


# --------------------------------------------------------------------------
# The gas rule is not the temperature rule
# --------------------------------------------------------------------------


def test_gas_greater_is_not_the_temperature_greater_rule():
    """``strike_type='greater'`` means two different things on two series.

    ``bracket_payoff`` is the whole-degree daily-high rule (``high >= floor+1``);
    gas is continuous and strict (``value > floor``). A $4.60 gas strike pays YES
    at $4.601. Routing gas through ``bracket_payoff`` would demand $5.60.
    """
    from src.core.bracket_payoff import BracketSpec, settles_yes, yes_bounds

    spec = BracketSpec(
        ticker="KXAAAGASM-26AUG31-4.60", strike_type="greater", floor_strike=4.60
    )
    assert yes_bounds(spec) == (5.60, math.inf)
    assert settles_yes(spec, 4.601) is False  # temperature rule: needs 5.60
    assert settles_yes_gas(4.601, 4.60) is True  # gas rule: strictly greater
    assert settles_yes(spec, 5.60) is True
    assert settles_yes_gas(5.60, 4.60) is True


# --------------------------------------------------------------------------
# Contract §1 CSV loader
# --------------------------------------------------------------------------


def _write_csvs(tmp_path, aaa_rows, rbob_rows=None, eia_rows=None):
    aaa = tmp_path / "aaa_daily_national.csv"
    aaa.write_text(
        "date,value,source,source_url,fetched_at,raw_sha256,quality\n"
        + "".join(
            f"{d.isoformat()},{v:.3f},aaa_wayback,https://web.archive.org/x,"
            f"2026-07-29T00:00:00Z,{'0' * 64},{q}\n"
            for d, v, q in aaa_rows
        ),
        encoding="utf-8",
        newline="",
    )
    if rbob_rows is not None:
        (tmp_path / "rbob_daily.csv").write_text(
            "date,value,source,source_url,fetched_at\n"
            + "".join(
                f"{d.isoformat()},{v:.4f},eia_bulk,https://eia.gov/x,"
                f"2026-07-29T00:00:00Z\n"
                for d, v in rbob_rows
            ),
            encoding="utf-8",
            newline="",
        )
    if eia_rows is not None:
        (tmp_path / "eia_weekly_regular.csv").write_text(
            "week_ending,value,source,source_url,fetched_at\n"
            + "".join(
                f"{d.isoformat()},{v:.3f},eia_bulk,https://eia.gov/y,"
                f"2026-07-29T00:00:00Z\n"
                for d, v in eia_rows
            ),
            encoding="utf-8",
            newline="",
        )
    return tmp_path


def test_from_csv_dir_reads_the_contract_schema_and_projects(tmp_path):
    dates, aaa, rbob = _synthetic()
    _write_csvs(
        tmp_path,
        [(d, v, "ok") for d, v in zip(dates, aaa)],
        [(d, v) for d, v in zip(dates, rbob)],
        [(d, float(v)) for d, v in zip(dates[::7], aaa[::7])],
    )
    loaded = GasSeries.from_csv_dir(tmp_path)
    assert len(loaded.aaa) == N_DAYS
    assert len(loaded.rbob) == N_DAYS
    assert loaded.eia_weekly
    assert loaded.clamped_to is None
    p = project(AS_OF, TARGET, loaded)
    assert fitted_lag_from_model_version(p.model_version) == TRUE_LAG


def test_from_csv_dir_requires_the_aaa_file(tmp_path):
    with pytest.raises(GasDataUnavailable, match="aaa_daily_national.csv not found"):
        GasSeries.from_csv_dir(tmp_path)


def test_from_csv_dir_survives_missing_optional_covariates(tmp_path):
    dates, aaa, _ = _synthetic()
    _write_csvs(tmp_path, [(d, v, "ok") for d, v in zip(dates, aaa)])
    loaded = GasSeries.from_csv_dir(tmp_path)
    assert loaded.rbob == ()
    assert loaded.eia_weekly == ()
    assert project(AS_OF, TARGET, loaded).model_version == "lagdrift_v1+nocovar"


def test_from_csv_dir_rejects_an_unparseable_date(tmp_path):
    (tmp_path / "aaa_daily_national.csv").write_text(
        "date,value,source,source_url,fetched_at,raw_sha256,quality\n"
        "not-a-date,4.100,aaa_live,https://x,2026-07-29T00:00:00Z,,ok\n",
        encoding="utf-8",
        newline="",
    )
    with pytest.raises(GasDataUnavailable, match="unparseable date"):
        GasSeries.from_csv_dir(tmp_path)


# --------------------------------------------------------------------------
# Optional EIA covariate
# --------------------------------------------------------------------------


def test_eia_covariate_is_off_by_default_and_recorded_when_enabled():
    dates, aaa, rbob = _synthetic()
    eia = [GasObservation(date=d, value=float(v)) for d, v in zip(dates[::7], aaa[::7])]
    s = GasSeries.from_rows(
        aaa=[GasObservation(date=d, value=float(v)) for d, v in zip(dates, aaa)],
        rbob=[GasObservation(date=d, value=float(v)) for d, v in zip(dates, rbob)],
        eia_weekly=eia,
    )
    default = project(AS_OF, TARGET, s)
    assert "+eia" not in default.model_version

    enabled = project(AS_OF, TARGET, s, config=ProjectionConfig(use_eia_covariate=True))
    assert enabled.model_version.endswith("+eia")
    assert enabled.n_train >= 60
