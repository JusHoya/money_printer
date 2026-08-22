"""Tests for the FR-2.3 probability engine, and the PRD Phase 2 EC-4 evidence.

Exit criterion 4, verbatim:

    "Probability engine: for 10 recorded ladders, bracket probabilities sum to
    1.0 +/- 0.01 and are monotonically consistent with the calibrated CDF; a
    perturbation test (sigma doubled) measurably flattens the distribution."

The 10 ladders live in ``tests/fixtures/ladders_probability/recorded_ladders.json``
-- real Kalshi market metadata captured read-only from
``api.elections.kalshi.com``, so these tests are offline-deterministic and do not
depend on any concurrently-written artifact.

A note on what "verified" means here. Several tests below compute their expected
values from published standard-normal tables and from arithmetic written out in
the docstring, not from the engine. A test that only compares the code to
itself proves the code is self-consistent, which is exactly what every
invalidated review cycle on this project already was.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import os
from datetime import date

import pytest

from src.core.bracket_payoff import BracketSpec, yes_bounds
from src.calibration import probability_engine as pe
from src.calibration.probability_engine import (
    NX_WINDOW_REGIME,
    BracketProbabilities,
    NormalComponent,
    ProbabilityEngineError,
    bracket_probabilities_ensemble,
    bracket_probabilities_point,
    distribution_over_ladder,
    entropy_bits,
    integer_pmf,
    load_city_calibration,
    recompute_nx_window_regime,
    select_calibration,
    specs_from_markets,
    validate_ladder_partition,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(
    REPO_ROOT, "tests", "fixtures", "ladders_probability", "recorded_ladders.json"
)
CALIBRATION_DIR = os.path.join(REPO_ROOT, "data", "calibration")

CITIES = ("NY", "CHI", "LAX", "MIA")

#: Measured day-of sigma per city (workstream B's EC-3 table). Used as the
#: sigma grid for the sum-to-1 sweep so the sweep covers the real operating range.
MEASURED_DAY_OF_SIGMA = {"MIA": 1.7069, "LAX": 2.1409, "NY": 3.3736, "CHI": 3.9809}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
def _phi(z: float) -> float:
    """Standard-normal CDF, computed here so tests never call the engine's copy."""
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


@pytest.fixture(scope="module")
def ladders():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    assert len(blob["ladders"]) == 10, "EC-4 requires exactly 10 recorded ladders"
    return blob["ladders"]


@pytest.fixture(scope="module")
def calibrations():
    return {
        c: load_city_calibration(c, calibration_dir=CALIBRATION_DIR) for c in CITIES
    }


def _specs(ladder):
    return specs_from_markets(ladder["markets"])


def _ladder_center(specs):
    """Midpoint of the ladder's finite strikes -- a mu that is inside the ladder."""
    lows = [s.floor_strike for s in specs if s.floor_strike is not None]
    return (min(lows) + max(lows)) / 2.0


def _synthetic_calibration(
    *,
    bias_f,
    sigma_f,
    city="NY",
    source="gfs_mex",
    n=209,
    mean_spread_f=None,
    sufficient=True,
):
    """A minimal FR-2.2-shaped payload, for tests that need an exact mu and sigma.

    Deliberately hand-built rather than loaded: the hand-worked examples need a
    sigma that makes the z-scores land on published standard-normal table rows.
    """
    block = {"n": n, "sufficient": sufficient}
    if sufficient:
        block.update(
            {
                "bias_f": bias_f,
                "sigma_f": sigma_f,
                "mae_f": None,
                "spread_f_coverage": {
                    "rows_with_spread": 0,
                    "rows_without_spread": n,
                    "mean_spread_f": mean_spread_f,
                },
            }
        )
    return {
        "schema_version": 1,
        "city": city,
        "station": "KTEST",
        "source": source,
        "version": 1,
        "units": "degF",
        "min_bucket_n": 20,
        "sigma_sanity_bound_f": 4.0,
        "day_of": copy.deepcopy(block),
        "by_lead": {"day_of": copy.deepcopy(block)},
        "by_month_day_of": {},
        "by_season_day_of": {},
    }


#: The hand-worked ladder: the real KXHIGHNY-26JUL17 strike structure.
HAND_LADDER = [
    BracketSpec("T83", "less", None, 83.0),  # pays <= 82
    BracketSpec("B83.5", "between", 83.0, 84.0),
    BracketSpec("B85.5", "between", 85.0, 86.0),
    BracketSpec("B87.5", "between", 87.0, 88.0),
    BracketSpec("B89.5", "between", 89.0, 90.0),
    BracketSpec("T90", "greater", 90.0, None),  # pays >= 91
]


# ---------------------------------------------------------------------------
# 1. Hand-worked example (expected values from a published normal table)
# ---------------------------------------------------------------------------
def test_hand_worked_ladder_matches_a_published_normal_table():
    """Every bracket probability, derived by hand, asserted to 4 decimals.

    Inputs chosen so every bracket edge lands on a table row:

        raw forecast  = 87.0 degF
        bias_f        = +2.0 degF  (the guidance ran warm)
        mu            = 87.0 - 2.0 = 85.0     <- ERROR_CONVENTION
        sigma         = 2.0 degF

    The outcome is an integer, so a bracket [lo, hi] spans the real interval
    (lo - 0.5, hi + 0.5]. Bracket edges and their z = (edge - 85) / 2:

        82.5 -> z = -1.25      86.5 -> z = +0.75      90.5 -> z = +2.75
        84.5 -> z = -0.25      88.5 -> z = +1.75

    Standard-normal CDF, from a published 5-decimal table:

        Phi(-1.25) = 0.10565      Phi(+0.75) = 0.77337      Phi(+2.75) = 0.99702
        Phi(-0.25) = 0.40129      Phi(+1.75) = 0.95994

    Bracket probabilities, by hand:

        T83   (high <= 82)  = Phi(-1.25)               = 0.10565
        B83.5 (83..84)      = 0.40129 - 0.10565        = 0.29564
        B85.5 (85..86)      = 0.77337 - 0.40129        = 0.37208
        B87.5 (87..88)      = 0.95994 - 0.77337        = 0.18657
        B89.5 (89..90)      = 0.99702 - 0.95994        = 0.03708
        T90   (high >= 91)  = 1 - 0.99702              = 0.00298
                                                  sum  = 1.00000
    """
    expected = {
        "T83": 0.10565,
        "B83.5": 0.29564,
        "B85.5": 0.37208,
        "B87.5": 0.18657,
        "B89.5": 0.03708,
        "T90": 0.00298,
    }
    result = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=87.0,
        specs=HAND_LADDER,
        calibration=_synthetic_calibration(bias_f=2.0, sigma_f=2.0),
    )
    assert result.mu_f == pytest.approx(85.0, abs=1e-12)
    assert result.sigma_f == pytest.approx(2.0, abs=1e-12)
    for ticker, want in expected.items():
        assert result.probabilities[ticker] == pytest.approx(want, abs=1e-4), ticker
    assert math.fsum(expected.values()) == pytest.approx(1.0, abs=1e-5)
    assert math.fsum(result.probabilities.values()) == pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 2. Sign of the bias
# ---------------------------------------------------------------------------
def test_a_warm_bias_moves_mu_down_not_up():
    """Worked case: forecast 87 with a +2 degF warm bias must centre on 85, not 89.

    ``error_f = forecast - truth``, so ``bias_f = +2`` means the guidance came in
    2 degF above what settled. The corrected expectation is therefore 87 - 2 = 85.

    The consequence, spelled out because it is the whole trading edge: with
    mu = 85 the modal bracket is B85.5 (85..86). With the sign inverted
    (mu = 89) the modal bracket would be B89.5 (89..90) -- four degrees away, a
    different trade, and every number on the way there would still look
    plausible.
    """
    warm = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=87.0,
        specs=HAND_LADDER,
        calibration=_synthetic_calibration(bias_f=2.0, sigma_f=2.0),
    )
    assert warm.mu_f == pytest.approx(85.0)
    assert max(warm.probabilities, key=warm.probabilities.get) == "B85.5"

    # The inverted-sign world, constructed explicitly so the contrast is asserted
    # rather than described: a -2 bias (guidance ran cold) must centre on 89.
    cold = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=87.0,
        specs=HAND_LADDER,
        calibration=_synthetic_calibration(bias_f=-2.0, sigma_f=2.0),
    )
    assert cold.mu_f == pytest.approx(89.0)
    assert max(cold.probabilities, key=cold.probabilities.get) == "B89.5"

    assert warm.probabilities["B85.5"] > warm.probabilities["B89.5"]
    assert cold.probabilities["B89.5"] > cold.probabilities["B85.5"]


def test_bias_sign_matches_the_calibration_artifacts_stated_convention(calibrations):
    """The engine's arithmetic agrees with the string the artifact publishes."""
    payload = calibrations["NY"]
    assert payload["error_convention"] == (
        "error_f = forecast_high_f - truth_high_f (positive = forecast too warm)"
    )
    sel = select_calibration(payload, date(2026, 7, 17))
    result = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=85.0,
        specs=HAND_LADDER,
        calibration=payload,
    )
    assert result.mu_f == pytest.approx(85.0 - sel.bias_f, abs=1e-12)


# ---------------------------------------------------------------------------
# 3. Continuity correction / mass conservation
# ---------------------------------------------------------------------------
def test_pmf_over_integers_sums_to_one_minus_the_truncated_tail():
    comps = (NormalComponent("t", 1.0, 85.0, 3.0),)
    pmf, below, above = integer_pmf(comps)
    assert math.fsum(pmf.values()) + below + above == pytest.approx(1.0, abs=1e-15)
    assert math.fsum(pmf.values()) == pytest.approx(1.0 - (below + above), abs=1e-15)
    # 8 sigma of support leaves essentially nothing outside, but it is still counted.
    assert 0.0 <= below + above < 1e-12


def test_pmf_is_an_integer_interval_mass_not_a_density():
    """P(H = k) is the mass of (k-0.5, k+0.5], computed independently here.

    Also measures the error the continuity correction prevents. With mu = 85 and
    sigma = 3, bracket [85, 86] is worth::

        correct    = Phi((86.5-85)/3) - Phi((84.5-85)/3) = 0.691462 - 0.433816
                   = 0.257646
        no-correct = Phi((86.0-85)/3) - Phi((85.0-85)/3) = 0.630559 - 0.500000
                   = 0.130559

    a 12.7-point mispricing on a single bracket, in the same direction on every
    bracket in the ladder.
    """
    mu, sigma = 85.0, 3.0
    pmf, _, _ = integer_pmf((NormalComponent("t", 1.0, mu, sigma),))
    for k in (78, 84, 85, 86, 92):
        want = _phi((k + 0.5 - mu) / sigma) - _phi((k - 0.5 - mu) / sigma)
        assert pmf[k] == pytest.approx(want, abs=1e-15), k

    corrected = pmf[85] + pmf[86]
    uncorrected = _phi((86.0 - mu) / sigma) - _phi((85.0 - mu) / sigma)
    assert corrected == pytest.approx(0.257646, abs=1e-6)
    assert uncorrected == pytest.approx(0.130559, abs=1e-6)
    assert corrected - uncorrected == pytest.approx(0.127088, abs=1e-6)


def test_truncating_the_support_cannot_misattribute_mass_to_a_tail_contract():
    """A deliberately brutal truncation must not change a single probability.

    This is the mutation test for a real defect found in review: if the integer
    support stops short of the ladder's finite core, the analytic
    "mass above the support" is handed to the ``greater`` contract even though
    most of it actually belongs to the top ``between`` brackets. At the default
    8 sigma the residual is ~1e-15 and the bug is invisible; at 0.5 sigma it is
    worth ~0.3 of the distribution.

    Squeezing the support to 0.5 sigma must therefore reproduce the 8-sigma
    answer exactly, because the engine widens the support to span the ladder.
    """
    mu, sigma = 85.0, 2.0
    comps = (NormalComponent("t", 1.0, mu, sigma),)
    wide, _, _, _, _ = distribution_over_ladder(HAND_LADDER, comps, support_sigmas=8.0)
    tight, _, tail, uncovered, _ = distribution_over_ladder(
        HAND_LADDER, comps, support_sigmas=0.5
    )
    for ticker in wide:
        assert tight[ticker] == pytest.approx(wide[ticker], abs=1e-12), ticker
    assert math.fsum(tight.values()) == pytest.approx(1.0, abs=1e-12)
    assert uncovered == pytest.approx(0.0, abs=1e-12)

    # And the top contract is still worth its true 0.00298, not the ~0.3 that a
    # naive 0.5-sigma truncation would have dumped into it.
    assert tight["T90"] == pytest.approx(1.0 - _phi((90.5 - mu) / sigma), abs=1e-12)
    assert tight["T90"] == pytest.approx(0.00298, abs=1e-5)

    # The squeezed support is [81, 92] -- widened from [84, 86] to span the
    # ladder -- so 1.23% of the mass still sits outside it. That residual is not
    # zero and is not dropped: it lands below 82 and above 91, i.e. inside the
    # two open-ended contracts, which is the only place it can be attributed
    # without guessing.
    assert tail == pytest.approx(
        _phi((80.5 - mu) / sigma) + 1.0 - _phi((92.5 - mu) / sigma), abs=1e-12
    )
    assert tail == pytest.approx(0.012313, abs=1e-6)


def test_truncated_mass_is_absorbed_by_the_open_ended_contracts():
    """With no ladder to widen the support, the residual is still conserved.

    ``integer_pmf`` alone at 1 sigma over mu=85, sigma=2 gives the support
    [83, 87] (the sigma band is rounded outward to whole degrees), leaving
    Phi(-1.25) = 0.10565 below and the same above -- 21.1% of the distribution.
    The two numbers it returns account for that exactly, so nothing is silently
    dropped.
    """
    mu, sigma = 85.0, 2.0
    pmf, below, above = integer_pmf(
        (NormalComponent("t", 1.0, mu, sigma),), support_sigmas=1.0
    )
    assert (min(pmf), max(pmf)) == (83, 87)
    assert math.fsum(pmf.values()) + below + above == pytest.approx(1.0, abs=1e-15)
    assert below == pytest.approx(0.10565, abs=1e-5)
    assert above == pytest.approx(0.10565, abs=1e-5)
    assert below == pytest.approx(_phi((min(pmf) - 0.5 - mu) / sigma), abs=1e-15)
    assert above == pytest.approx(1.0 - _phi((max(pmf) + 0.5 - mu) / sigma), abs=1e-15)


def test_far_tail_probability_survives_cancellation():
    """A 6-sigma bracket must be a real number, not the residue of 1.0 - 1.0.

    P(H >= mu + 6 sigma) is ~9.87e-10. Computing it as Phi(hi) - Phi(lo) with
    both terms ~1.0 gives noise; the engine uses the survival function on that
    side. FR-3.1(a) sells exactly these brackets, so the digits matter.
    """
    mu, sigma = 85.0, 2.0
    specs = [
        BracketSpec("LOW", "less", None, mu - 20),
        BracketSpec("MID", "between", mu - 20, mu + 6 * sigma - 1),
        BracketSpec("FAR", "greater", mu + 6 * sigma - 1, None),  # pays >= mu+6sigma
    ]
    probs, _, _, uncovered, partition = distribution_over_ladder(
        specs, (NormalComponent("t", 1.0, mu, sigma),)
    )
    assert partition.complete
    expected = 1.0 - _phi(((mu + 6 * sigma) - 0.5 - mu) / sigma)
    assert probs["FAR"] == pytest.approx(expected, rel=1e-9)
    assert 1e-10 < probs["FAR"] < 1e-8
    assert uncovered == pytest.approx(0.0, abs=1e-15)


# ---------------------------------------------------------------------------
# 4. EC-4 sub-claim 1 -- sum to 1.0 +/- 0.01 over 10 recorded ladders
# ---------------------------------------------------------------------------
def test_ec4_the_ten_recorded_ladders_are_complete_partitions(ladders):
    """Sum-to-1 is only meaningful if the ladder tiles the integer line.

    A ladder with a hole would sum to less than 1 and the *data*, not the
    engine, would be at fault. This asserts the precondition explicitly so the
    next test cannot pass for the wrong reason.
    """
    for ladder in ladders:
        report = validate_ladder_partition(_specs(ladder))
        assert report.complete, f"{ladder['event_ticker']}: {report.issues}"
        assert report.n_brackets == 6
        assert report.has_low_tail and report.has_high_tail
        assert report.gaps == () and report.overlaps == ()
        assert report.covered_low is None and report.covered_high is None


@pytest.mark.parametrize("sigma_city", CITIES)
def test_ec4_bracket_probabilities_sum_to_one(ladders, calibrations, sigma_city):
    """EC-4 sub-claim 1, swept over 10 ladders x 7 mu offsets x 4 measured sigma.

    mu is walked from far below the ladder to far above it, so the sweep
    exercises the open-ended tail contracts absorbing essentially all the mass,
    not just the comfortable centred case.
    """
    sigma = MEASURED_DAY_OF_SIGMA[sigma_city]
    worst = 0.0
    for ladder in ladders:
        specs = _specs(ladder)
        centre = _ladder_center(specs)
        for offset in (-40, -8, -3, 0, 3, 8, 40):
            probs, _, _, uncovered, partition = distribution_over_ladder(
                specs, (NormalComponent("t", 1.0, centre + offset, sigma),)
            )
            total = math.fsum(probs.values())
            worst = max(worst, abs(1.0 - total))
            assert partition.complete
            assert total == pytest.approx(
                1.0, abs=0.01
            ), f"{ladder['event_ticker']} mu={centre + offset} sigma={sigma}"
            assert uncovered == pytest.approx(0.0, abs=1e-12)
    # The criterion allows 0.01; the engine is at machine epsilon because the
    # partition makes it an identity, not an approximation.
    assert worst < 1e-12, worst


def test_ec4_sum_to_one_end_to_end_through_the_public_api(ladders, calibrations):
    """The same claim through ``bracket_probabilities_point`` and the real files."""
    for ladder in ladders:
        specs = _specs(ladder)
        result = bracket_probabilities_point(
            city=ladder["city"],
            target_date=ladder["target_date"],
            forecast_high_f=_ladder_center(specs),
            specs=specs,
            calibration=calibrations[ladder["city"]],
            forecast_source="gfs_mex",
        )
        assert math.fsum(result.probabilities.values()) == pytest.approx(1.0, abs=0.01)
        assert result.uncovered_mass == pytest.approx(0.0, abs=1e-12)
        assert result.partition.complete
        assert result.calibration_source == "gfs_mex"
        assert result.mode == "point"


# ---------------------------------------------------------------------------
# 5. EC-4 sub-claim 2 -- monotone consistency with the calibrated CDF
# ---------------------------------------------------------------------------
def test_ec4_cumulative_bracket_sums_match_the_calibrated_cdf(ladders, calibrations):
    """Walking the ladder upward, the running total must equal F(bracket edge).

    ``F`` is recomputed here from :func:`math.erfc` with the same +/-0.5
    continuity convention -- an analytic reference, not the engine's own pmf sum.
    """
    worst = 0.0
    for ladder in ladders:
        specs = _specs(ladder)
        result = bracket_probabilities_point(
            city=ladder["city"],
            target_date=ladder["target_date"],
            forecast_high_f=_ladder_center(specs),
            specs=specs,
            calibration=calibrations[ladder["city"]],
        )
        mu, sigma = result.mu_f, result.sigma_f
        ordered = sorted(
            specs,
            key=lambda s: (
                -math.inf if math.isinf(yes_bounds(s)[0]) else yes_bounds(s)[0]
            ),
        )
        cumulative = 0.0
        for spec in ordered[:-1]:  # the top contract's edge is +inf
            cumulative += result.probabilities[spec.ticker]
            edge = yes_bounds(spec)[1]
            reference = _phi((edge + 0.5 - mu) / sigma)
            worst = max(worst, abs(cumulative - reference))
            assert cumulative == pytest.approx(
                reference, abs=1e-9
            ), f"{ladder['event_ticker']} at {spec.ticker} edge {edge}"
            # Monotone: a cumulative distribution never decreases.
            assert 0.0 <= cumulative <= 1.0
        # And the engine's own published CDF agrees with the same reference.
        assert result.cdf(mu) == pytest.approx(
            _phi((math.floor(mu) + 0.5 - mu) / sigma), abs=1e-12
        )
    assert worst < 1e-9, worst


def test_ec4_between_brackets_decay_monotonically_away_from_the_mode(
    ladders, calibrations
):
    """Probabilities fall as brackets move away from mu.

    Asserted on the equal-width ``between`` brackets only, and that restriction
    is deliberate rather than a convenience: the two end contracts are
    *unbounded*, so a ``less`` or ``greater`` contract legitimately holds more
    mass than the finite bracket beside it whenever mu sits near that end. A
    monotonicity claim over all six would be false for the data, not for the
    engine.
    """
    for ladder in ladders:
        specs = _specs(ladder)
        result = bracket_probabilities_point(
            city=ladder["city"],
            target_date=ladder["target_date"],
            forecast_high_f=_ladder_center(specs),
            specs=specs,
            calibration=calibrations[ladder["city"]],
        )
        between = sorted(
            (s for s in specs if s.strike_type == "between"),
            key=lambda s: s.floor_strike,
        )
        probs = [result.probabilities[s.ticker] for s in between]
        peak = max(range(len(probs)), key=probs.__getitem__)
        for i in range(peak):
            assert probs[i] < probs[i + 1], (
                ladder["event_ticker"],
                "rising limb",
                probs,
            )
        for i in range(peak, len(probs) - 1):
            assert probs[i] > probs[i + 1], (
                ladder["event_ticker"],
                "falling limb",
                probs,
            )
        # The mode must sit on the bracket containing mu.
        assert (
            between[peak].floor_strike - 0.5
            <= result.mu_f
            <= between[peak].cap_strike + 0.5
        )


# ---------------------------------------------------------------------------
# 6. EC-4 sub-claim 3 -- sigma doubled flattens the distribution
# ---------------------------------------------------------------------------
def _double_sigma(result: BracketProbabilities, specs):
    doubled = tuple(
        NormalComponent(c.label, c.weight, c.mu_f, c.sigma_f * 2.0)
        for c in result.components
    )
    return distribution_over_ladder(specs, doubled)


def test_ec4_doubling_sigma_measurably_flattens_the_distribution(ladders, calibrations):
    """Three metrics, all measured, all in the flattening direction.

    1. Shannon entropy over the ladder strictly increases.
    2. The largest *finite* bracket probability strictly decreases.
    3. The peak of the integer pmf strictly decreases (it approximately halves,
       as it must for a Gaussian whose sigma doubled).
    """
    for ladder in ladders:
        specs = _specs(ladder)
        base = bracket_probabilities_point(
            city=ladder["city"],
            target_date=ladder["target_date"],
            forecast_high_f=_ladder_center(specs),
            specs=specs,
            calibration=calibrations[ladder["city"]],
        )
        probs2, pmf2, _, _, _ = _double_sigma(base, specs)
        finite = {s.ticker for s in specs if s.strike_type == "between"}

        h1, h2 = base.entropy_bits(), entropy_bits(probs2.values())
        m1 = max(v for t, v in base.probabilities.items() if t in finite)
        m2 = max(v for t, v in probs2.items() if t in finite)
        peak1, peak2 = max(base.pmf.values()), max(pmf2.values())

        assert h2 > h1, (ladder["event_ticker"], h1, h2)
        assert m2 < m1, (ladder["event_ticker"], m1, m2)
        assert peak2 < peak1, (ladder["event_ticker"], peak1, peak2)
        assert peak2 == pytest.approx(peak1 / 2.0, rel=0.02)
        # Not a rounding wobble: the finite-bracket mode roughly halves too.
        assert m2 < 0.75 * m1
        assert math.fsum(probs2.values()) == pytest.approx(1.0, abs=1e-12)


def test_finite_bracket_maximum_reverses_when_mu_is_far_outside_the_ladder(
    ladders, calibrations
):
    """The finite-bracket criterion is conditional; entropy is not.

    The test above asserts that the largest *finite* bracket probability
    decreases under sigma-doubling, and it does -- but only because its ``mu``
    is the ladder centre. Move ``mu`` far below the ladder and the implication
    inverts: doubling sigma drags mass back *toward* brackets the forecast had
    abandoned, so the finite maximum **rises**. Measured here so the engine's
    docstring cannot claim more than the property holds, and so a future reader
    knows which of EC-4's three metrics is the unconditional one.

    Entropy is checked in the same loop and rises in every case, which is why
    EC-4 rests on it.
    """
    reversals = 0
    for ladder in ladders:
        specs = _specs(ladder)
        base = bracket_probabilities_point(
            city=ladder["city"],
            target_date=ladder["target_date"],
            forecast_high_f=_ladder_center(specs) - 25.0,
            specs=specs,
            calibration=calibrations[ladder["city"]],
        )
        probs2, _pmf2, _, _, _ = _double_sigma(base, specs)
        finite = {s.ticker for s in specs if s.strike_type == "between"}
        m1 = max(v for t, v in base.probabilities.items() if t in finite)
        m2 = max(v for t, v in probs2.items() if t in finite)
        if m2 > m1:
            reversals += 1
        # The metric EC-4 actually rests on is unaffected by where mu sits.
        assert entropy_bits(probs2.values()) > base.entropy_bits(), ladder[
            "event_ticker"
        ]
    assert reversals == len(ladders), (
        "the finite-bracket maximum was expected to reverse on every ladder with "
        f"mu 25 degF below the ladder centre; it reversed on {reversals}"
    )


def test_max_over_all_brackets_is_not_a_valid_flattening_metric(ladders, calibrations):
    """Documents, with numbers, why the "max bracket probability" metric is wrong.

    On KXHIGHNY-26JUL17 with the calibrated mu = 85.167 and sigma = 2.5988, the
    largest bracket probability *rises* from 0.29727 to 0.30395 when sigma is
    doubled -- because the unbounded ``T83`` contract (high <= 82) gains mass
    faster than the central bracket loses it, and as sigma grows without bound
    the two tail contracts converge on ~0.5 each.

    This test exists so that a future change cannot "fix" the flattening test by
    switching to that metric. The flattening claim is carried by entropy, by the
    finite-bracket maximum, and by the pmf peak -- see the test above.
    """
    ladder = next(ld for ld in ladders if ld["event_ticker"] == "KXHIGHNY-26JUL17")
    specs = _specs(ladder)
    base = bracket_probabilities_point(
        city="NY",
        target_date=ladder["target_date"],
        forecast_high_f=_ladder_center(specs),
        specs=specs,
        calibration=calibrations["NY"],
    )
    probs2, _, _, _, _ = _double_sigma(base, specs)
    assert base.mu_f == pytest.approx(85.1667, abs=1e-3)
    assert base.sigma_f == pytest.approx(2.5988, abs=1e-3)
    assert base.max_probability() == pytest.approx(0.29727, abs=1e-4)
    assert max(probs2.values()) == pytest.approx(0.30395, abs=1e-4)
    assert max(probs2.values()) > base.max_probability()  # the counterexample


# ---------------------------------------------------------------------------
# 7. Semantics are bracket_payoff's, not a second copy
# ---------------------------------------------------------------------------
def test_engine_asks_bracket_payoff_which_integers_pay(monkeypatch):
    """Mutation test: break ``settles_yes`` and the engine's answers must change.

    If this module carried its own between/greater/less inequality, swapping the
    authority out from under it would leave the probabilities untouched -- and
    the two copies would then be free to drift until settlement day.
    """
    specs = HAND_LADDER
    comps = (NormalComponent("t", 1.0, 85.0, 2.0),)
    honest, _, _, _, _ = distribution_over_ladder(specs, comps)

    def broken(spec, high):  # every contract pays nothing, whatever the outcome
        return False

    monkeypatch.setattr(pe, "settles_yes", broken)
    mutated, _, _, uncovered, _ = distribution_over_ladder(specs, comps)

    # The finite brackets collapse to zero, proving the engine really consults
    # the authority per integer. The two open-ended contracts keep only the
    # analytic truncation mass, which is attributed via yes_bounds and so is
    # ~0 at 8 sigma of support.
    assert all(v == pytest.approx(0.0, abs=1e-9) for v in mutated.values())
    assert honest != mutated
    assert honest["B85.5"] > 0.37 and mutated["B85.5"] == pytest.approx(0.0, abs=1e-12)
    assert uncovered == pytest.approx(1.0, abs=1e-9)


def test_module_contains_no_second_copy_of_the_settlement_rule():
    """AST guard: no ``strike_type`` comparison and no ticker-suffix inference.

    PRD Phase 1 exit criterion 2 in spirit -- direction must never be inferred
    from a ticker, and the inequality must live in exactly one module.
    """
    src = open(pe.__file__, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left = node.left
            if isinstance(left, ast.Attribute) and left.attr == "strike_type":
                offenders.append(f"line {node.lineno}: compares .strike_type")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("startswith", "endswith"):
                offenders.append(f"line {node.lineno}: {node.func.attr} on a name")
    assert not offenders, offenders
    # The one legitimate strike_type reference is in tests/reports, not logic.
    assert "STRIKE_TYPE_BETWEEN" not in src
    assert "STRIKE_TYPE_GREATER" not in src


# ---------------------------------------------------------------------------
# 8. Partition validation: gap, overlap, missing tail
# ---------------------------------------------------------------------------
def test_partition_detects_a_gap():
    specs = [
        BracketSpec("T80", "less", None, 80.0),  # <= 79
        BracketSpec("B80.5", "between", 80.0, 81.0),
        # 82..84 covered by nobody
        BracketSpec("B85.5", "between", 85.0, 86.0),
        BracketSpec("T86", "greater", 86.0, None),  # >= 87
    ]
    report = validate_ladder_partition(specs)
    assert not report.complete
    assert report.gaps == ((82, 84),)
    assert any("82..84" in i for i in report.issues)


def test_partition_detects_an_overlap():
    specs = [
        BracketSpec("T80", "less", None, 80.0),  # <= 79
        BracketSpec("B80.5", "between", 80.0, 84.0),
        BracketSpec("B83.5", "between", 83.0, 86.0),  # overlaps 83..84
        BracketSpec("T86", "greater", 86.0, None),  # >= 87
    ]
    report = validate_ladder_partition(specs)
    assert not report.complete
    assert report.overlaps
    a, b, lo, hi = report.overlaps[0]
    assert (a, b, lo) == ("B80.5", "B83.5", 83)


def test_partition_detects_a_missing_low_tail():
    specs = [
        BracketSpec("B80.5", "between", 80.0, 81.0),
        BracketSpec("B82.5", "between", 82.0, 83.0),
        BracketSpec("T83", "greater", 83.0, None),
    ]
    report = validate_ladder_partition(specs)
    assert not report.complete
    assert not report.has_low_tail and report.has_high_tail
    assert report.covered_low == 80 and report.covered_high is None
    assert any("low tail" in i for i in report.issues)


def test_partition_detects_a_missing_high_tail():
    specs = [
        BracketSpec("T80", "less", None, 80.0),
        BracketSpec("B80.5", "between", 80.0, 81.0),
    ]
    report = validate_ladder_partition(specs)
    assert not report.complete
    assert report.has_low_tail and not report.has_high_tail
    assert report.covered_low is None and report.covered_high == 81
    assert any("high tail" in i for i in report.issues)


def test_empty_ladder_is_incomplete_not_a_crash():
    report = validate_ladder_partition([])
    assert not report.complete and report.n_brackets == 0


def test_uncovered_mass_is_reported_and_never_renormalized():
    """A ladder with a hole must sum to < 1, with the deficit named.

    Renormalizing would hide the missing bracket and inflate every remaining
    probability -- turning a data defect into a confident-looking trade.
    """
    specs = [
        BracketSpec("T84", "less", None, 84.0),  # <= 83
        # 84..86 missing
        BracketSpec("B86.5", "between", 87.0, 88.0),
        BracketSpec("T88", "greater", 88.0, None),  # >= 89
    ]
    mu, sigma = 85.0, 2.0
    probs, pmf, _, uncovered, report = distribution_over_ladder(
        specs, (NormalComponent("t", 1.0, mu, sigma),)
    )
    assert not report.complete
    expected_gap = math.fsum(pmf[k] for k in (84, 85, 86))
    assert uncovered == pytest.approx(expected_gap, abs=1e-12)
    assert uncovered == pytest.approx(
        _phi((86.5 - mu) / sigma) - _phi((83.5 - mu) / sigma), abs=1e-12
    )
    assert math.fsum(probs.values()) == pytest.approx(1.0 - uncovered, abs=1e-12)
    assert math.fsum(probs.values()) < 0.5  # nothing was scaled back up to 1

    result = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=87.0,
        specs=specs,
        calibration=_synthetic_calibration(bias_f=2.0, sigma_f=2.0),
    )
    assert any("not a partition" in w for w in result.warnings)


def test_overlapping_ladder_reports_negative_uncovered_mass():
    """Two contracts covering the same integer sum to > 1; the sign says so."""
    specs = [
        BracketSpec("T84", "less", None, 84.0),
        BracketSpec("B83.5", "between", 83.0, 86.0),  # 83 double-covered
        BracketSpec("T86", "greater", 86.0, None),
    ]
    _, _, _, uncovered, report = distribution_over_ladder(
        specs, (NormalComponent("t", 1.0, 85.0, 2.0),)
    )
    assert not report.complete and report.overlaps
    assert uncovered < 0.0


# ---------------------------------------------------------------------------
# 9. Calibration selection: fallback is explicit, insufficiency is loud
# ---------------------------------------------------------------------------
def test_month_bucket_is_preferred_when_sufficient(calibrations):
    sel = select_calibration(calibrations["NY"], date(2026, 7, 17))
    assert sel.bucket == "by_month_day_of:2026-07"
    assert sel.fallback_chain == ()
    assert sel.n >= 20


def test_insufficient_month_falls_back_to_season_then_day_of_and_records_it():
    payload = _synthetic_calibration(bias_f=1.0, sigma_f=3.0)
    payload["by_month_day_of"] = {"2026-07": {"n": 4, "sufficient": False}}
    payload["by_season_day_of"] = {"JJA": {"n": 7, "sufficient": False}}
    sel = select_calibration(payload, date(2026, 7, 17))
    assert sel.bucket == "day_of"
    assert sel.fallback_chain == (
        "by_month_day_of:2026-07 (insufficient n=4)",
        "by_season_day_of:JJA (insufficient n=7)",
    )
    assert any("fallback taken" in w for w in sel.warnings)


def test_the_sufficient_flag_alone_is_enough_to_reject_a_bucket():
    """A block carrying ``sufficient: false`` is rejected even if it has stats.

    Found by mutation-testing this file: deleting the ``sufficient`` branch from
    ``_block_is_usable`` broke nothing, because the FR-2.2 writer omits
    ``bias_f``/``sigma_f`` from insufficient blocks and the *second* guard caught
    it. That is defence in depth, not a test. A hand-edited, stale, or
    schema-migrated payload can carry both the flag and the numbers, and the
    PRD's rule is to branch on the flag -- so the flag gets its own test.
    """
    payload = _synthetic_calibration(bias_f=1.0, sigma_f=3.0)
    payload["by_month_day_of"] = {
        "2026-07": {
            "n": 4,
            "sufficient": False,  # <- the only thing wrong with it
            "bias_f": 99.0,
            "sigma_f": 0.01,  # <- plausible-looking, unearned
            "spread_f_coverage": {"mean_spread_f": None},
        }
    }
    sel = select_calibration(payload, date(2026, 7, 17))
    assert sel.bucket == "day_of"
    assert sel.sigma_f == 3.0 and sel.bias_f == 1.0
    assert sel.fallback_chain == (
        "by_month_day_of:2026-07 (insufficient n=4)",
        "by_season_day_of:JJA (absent)",
    )
    # The unearned 0.01 sigma is nowhere in the answer.
    assert sel.sigma_f != 0.01 and sel.bias_f != 99.0


def test_insufficient_lead_bucket_with_stats_still_raises():
    payload = _synthetic_calibration(bias_f=1.0, sigma_f=3.0)
    payload["by_lead"]["lead_60_84"] = {
        "n": 3,
        "sufficient": False,
        "bias_f": 0.5,
        "sigma_f": 1.0,
        "spread_f_coverage": {"mean_spread_f": None},
    }
    with pytest.raises(ProbabilityEngineError, match="refusing to substitute"):
        select_calibration(payload, date(2026, 7, 17), lead_hours=72)


def test_all_buckets_insufficient_raises_rather_than_inventing_a_sigma():
    payload = _synthetic_calibration(bias_f=1.0, sigma_f=3.0, sufficient=False)
    with pytest.raises(ProbabilityEngineError, match="no usable calibration block"):
        select_calibration(payload, date(2026, 7, 17))


def test_insufficient_lead_bucket_refuses_to_borrow_the_day_of_block():
    payload = _synthetic_calibration(bias_f=1.0, sigma_f=3.0)
    payload["by_lead"]["lead_60_84"] = {"n": 3, "sufficient": False}
    with pytest.raises(ProbabilityEngineError, match="refusing to substitute"):
        select_calibration(payload, date(2026, 7, 17), lead_hours=72)


def test_null_published_spread_is_refused_loudly_when_required(calibrations):
    """`mean_spread_f: null` must never become 0.0.

    A spread of zero would size every position as though the forecast were
    certain. All four committed calibrations report null, so this fires on real
    data, not a contrived payload.
    """
    for city in CITIES:
        assert (
            calibrations[city]["day_of"]["spread_f_coverage"]["mean_spread_f"] is None
        )
        with pytest.raises(ProbabilityEngineError, match="mean_spread_f=null"):
            select_calibration(
                calibrations[city], date(2026, 7, 17), require_published_spread=True
            )
    sel = select_calibration(calibrations["NY"], date(2026, 7, 17))
    assert any("no published forecast spread" in w for w in sel.warnings)


def test_source_mismatch_is_warned(calibrations):
    result = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=85.0,
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
        forecast_source="gefs_ensemble",
    )
    assert any("SOURCE MISMATCH" in w for w in result.warnings)


def test_unknown_ticker_raises_instead_of_returning_zero(calibrations):
    result = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=85.0,
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
    )
    with pytest.raises(ProbabilityEngineError, match="never priced"):
        result.p_yes("KXHIGHNY-26JUL17-B99.5")


# ---------------------------------------------------------------------------
# 10. The N/X-window regime
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.path.exists(
        os.path.join(
            REPO_ROOT, "data", "forecast_archive", "forecast_series_gfs_mex.csv"
        )
    ),
    reason="forecast archive not present",
)
def test_regime_constants_match_a_fresh_recompute_from_the_archives():
    """The hard-coded regime table must stay derivable from the read-only inputs.

    Without this the constants are just numbers in a file that nothing checks.
    """
    fresh = recompute_nx_window_regime()
    assert set(fresh) == set(NX_WINDOW_REGIME)
    for city, measured in sorted(fresh.items()):
        stored = NX_WINDOW_REGIME[city]
        assert measured == stored, city


def test_regime_risk_is_attached_and_material_for_ny_and_chi(calibrations):
    for city in ("NY", "CHI"):
        result = bracket_probabilities_point(
            city=city,
            target_date="2026-07-17",
            forecast_high_f=85.0,
            specs=HAND_LADDER,
            calibration=calibrations[city],
        )
        assert result.regime_risk is not None
        assert result.regime_risk.material
        assert result.regime_risk.p_outside > 0.17
        assert any("outside" in w for w in result.warnings)
    for city in ("LAX", "MIA"):
        result = bracket_probabilities_point(
            city=city,
            target_date="2026-07-17",
            forecast_high_f=75.0,
            specs=HAND_LADDER,
            calibration=calibrations[city],
        )
        assert not result.regime_risk.material
        assert not result.regime_risk.outside_sufficient
        assert any("below the 20-day quorum" in w for w in result.warnings)


def test_mixture_widens_the_tails_and_still_sums_to_one(calibrations):
    single = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=85.0,
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
        regime="single",
    )
    mixed = bracket_probabilities_point(
        city="NY",
        target_date="2026-07-17",
        forecast_high_f=85.0,
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
        regime="mixture",
    )
    assert mixed.regime_model == "mixture_nx_window"
    assert len(mixed.components) == 2
    assert math.fsum(mixed.probabilities.values()) == pytest.approx(1.0, abs=1e-12)
    # The whole point: the far brackets FR-3.1(a) wants to sell get more mass.
    assert mixed.probabilities["T90"] > single.probabilities["T90"]
    assert mixed.sigma_f > single.sigma_f
    assert mixed.entropy_bits() > single.entropy_bits()


def test_mixture_is_refused_where_the_second_component_is_unmeasured(calibrations):
    for city in ("LAX", "MIA"):
        with pytest.raises(ProbabilityEngineError, match="below the .* quorum"):
            bracket_probabilities_point(
                city=city,
                target_date="2026-07-17",
                forecast_high_f=75.0,
                specs=HAND_LADDER,
                calibration=calibrations[city],
                regime="mixture",
            )
        degraded = bracket_probabilities_point(
            city=city,
            target_date="2026-07-17",
            forecast_high_f=75.0,
            specs=HAND_LADDER,
            calibration=calibrations[city],
            regime="mixture_if_available",
        )
        assert degraded.regime_model.startswith("single_normal (mixture unavailable")
        assert any("mixture unavailable" in w for w in degraded.warnings)
        assert len(degraded.components) == 1


# ---------------------------------------------------------------------------
# 11. Ensemble mode
# ---------------------------------------------------------------------------
def test_ensemble_uses_the_calibrated_sigma_not_the_member_spread(calibrations):
    """Two ensembles with the same mean but very different spread agree exactly.

    That is the documented, deliberate consequence of using the calibrated
    sigma: the member cloud's shape is discarded. The test pins it so nobody
    mistakes it for a bug -- and so a future change to use member spread has to
    delete an assertion rather than slip through.
    """
    tight = tuple(85.0 + 0.05 * i for i in range(-10, 11))
    wide = tuple(85.0 + 1.50 * i for i in range(-10, 11))
    common = dict(
        city="NY",
        target_date="2026-07-17",
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
    )
    a = bracket_probabilities_ensemble(members_f=tight, **common)
    b = bracket_probabilities_ensemble(members_f=wide, **common)

    assert a.mode == "ensemble" and b.mode == "ensemble"
    assert a.ensemble.n_members == 21 and b.ensemble.n_members == 21
    assert a.ensemble.mean_f == pytest.approx(b.ensemble.mean_f, abs=1e-9)
    assert a.ensemble.sigma_f < 1.0 < b.ensemble.sigma_f
    assert a.sigma_f == pytest.approx(b.sigma_f, abs=1e-12)
    for ticker in a.probabilities:
        assert a.probabilities[ticker] == pytest.approx(
            b.probabilities[ticker], abs=1e-12
        )
    assert any("dispersion is NOT used" in w for w in a.warnings)
    assert any("exceeds the calibrated sigma" in w for w in b.warnings)


def test_ensemble_and_point_agree_when_the_mean_is_the_point(calibrations):
    members = (83.0, 84.0, 85.0, 86.0, 87.0)
    common = dict(
        city="NY",
        target_date="2026-07-17",
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
    )
    ens = bracket_probabilities_ensemble(members_f=members, **common)
    pnt = bracket_probabilities_point(forecast_high_f=85.0, **common)
    for ticker in pnt.probabilities:
        assert ens.probabilities[ticker] == pytest.approx(
            pnt.probabilities[ticker], abs=1e-12
        )
    assert ens.mode != pnt.mode  # but the provenance stamp differs


def test_degenerate_ensemble_is_refused(calibrations):
    common = dict(
        city="NY",
        target_date="2026-07-17",
        specs=HAND_LADDER,
        calibration=calibrations["NY"],
    )
    with pytest.raises(ProbabilityEngineError, match="min_members"):
        bracket_probabilities_ensemble(members_f=(85.0,), **common)
    with pytest.raises(ProbabilityEngineError, match="min_members"):
        bracket_probabilities_ensemble(members_f=(), **common)
    with pytest.raises(ProbabilityEngineError, match="NaN"):
        bracket_probabilities_ensemble(members_f=(85.0, float("nan")), **common)


# ---------------------------------------------------------------------------
# 12. Serialization / reporting surface used by workstream E
# ---------------------------------------------------------------------------
def test_as_dict_carries_the_mode_bucket_and_every_caveat(calibrations, ladders):
    ladder = ladders[0]
    specs = _specs(ladder)
    result = bracket_probabilities_point(
        city=ladder["city"],
        target_date=ladder["target_date"],
        forecast_high_f=_ladder_center(specs),
        specs=specs,
        calibration=calibrations[ladder["city"]],
        forecast_source="gfs_mex",
    )
    blob = result.as_dict()
    assert blob["mode"] == "point"
    assert blob["regime_model"] == "single_normal"
    assert blob["calibration_bucket"].startswith("by_month_day_of:")
    assert blob["calibration_source"] == "gfs_mex"
    assert blob["sum_probabilities"] == pytest.approx(1.0, abs=1e-5)
    assert blob["partition"].startswith("complete:")
    assert blob["warnings"]
    assert json.loads(json.dumps(blob))  # report-serializable
