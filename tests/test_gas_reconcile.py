"""Tests for the AAA gas settlement reconcile job (PRD FR-4.4).

Phase 4 exit criterion 3, second half: *"settlement reconcile vs the published
AAA value shows 0 mismatches."* This file drives ``scripts/reconcile_gas.py``
offline -- no network, no writes outside ``tmp_path`` -- and its centre of
gravity is the same as ``tests/test_weather_reconcile.py``: proving the job
cannot report success while checking nothing.

WHAT IS ASSERTED
----------------
1. Each of the four legs (semantics / truth / outcome / sim) detects its own
   class of failure, and each explained category stays explained.
2. Every coverage floor can actually fail, demonstrated one at a time. A gate
   that has never been shown to fail is not evidence, so every floor here is
   driven red on purpose and then satisfied.
3. The committed live monthly ladders reconcile clean end to end against an AAA
   series that carries the correct values, and go red when one value is
   perturbed by a tenth of a cent.

THE AAA CSVs BUILT IN THESE TESTS ARE SYNTHETIC STAND-INS
--------------------------------------------------------
Workstream A owns the real ``data/gas_truth/aaa_daily_national.csv``. The files
written here live in ``tmp_path``, carry ``source=test_fixture``, and exist only
to exercise the plumbing; nothing here claims to be a harvested AAA value. The
numbers used for the clean run are taken from the committed ladders' own
``expiration_value``, which makes the *truth leg* of that particular test
tautological by construction -- so it is the perturbation test immediately
after it, not the clean run, that shows the leg can fail.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "gas")
MONTHLY_FIXTURE = os.path.join(FIXTURE_DIR, "kxaaagasm_settled_ladders.json")
TIES_FIXTURE = os.path.join(FIXTURE_DIR, "gas_boundary_ties.json")
#: The one settled ladder on which Kalshi's settlement input disagrees with our
#: AAA record. Committed verbatim as the evidence for the registered exception.
WEEKLY_JUL13_FIXTURE = os.path.join(FIXTURE_DIR, "kxaaagasw_26jul13_ladder.json")


def _load_reconcile_gas():
    """scripts/ is not a package, so load the job by path (as it loads its own)."""
    path = os.path.join(REPO_ROOT, "scripts", "reconcile_gas.py")
    spec = importlib.util.spec_from_file_location("_reconcile_gas", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RG = _load_reconcile_gas()


def _markets(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)["markets"]


MONTHLY_MARKETS = _markets(MONTHLY_FIXTURE)
TIE_MARKETS = _markets(TIES_FIXTURE)


def _ladder(event_ticker):
    return [m for m in MONTHLY_MARKETS if m["event_ticker"] == event_ticker]


JUL13_WEEKLY = _markets(WEEKLY_JUL13_FIXTURE)

JUN30 = _ladder("KXAAAGASM-26JUN30")
MAY31 = _ladder("KXAAAGASM-26MAY31")
JUN30_VALUE = 3.847
MAY31_VALUE = 4.336

#: The registered disagreement, both numbers. Kalshi settled 2026-07-13 on
#: 3.876; our AAA row for that date is 3.872.
JUL13_KALSHI_VALUE = 3.876
JUL13_OUR_VALUE = 3.872
JUL13_KEY = ("KXAAAGASW", "2026-07-13")

#: Today, for the retention/age classification. Pinned so nothing in this file
#: changes meaning with the wall clock.
AS_OF = "2026-07-30"

AAA_HEADER = "date,value,source,source_url,fetched_at,raw_sha256,quality"


def _write_aaa(tmp_path, values, *, name="aaa_daily_national.csv"):
    """A synthetic stand-in for workstream A's series. See the module docstring."""
    lines = [AAA_HEADER]
    for date, value in sorted(values.items()):
        lines.append(
            f"{date},{value},test_fixture,"
            f"https://example.invalid/synthetic-stand-in,"
            f"2026-07-29T00:00:00Z,,ok"
        )
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ======================================================================
# classify_market -- one leg at a time
# ======================================================================
def test_a_clean_settled_market_matches():
    market = next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.89")
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "MATCH"
    assert row["explained"] is True
    assert row["semantics_checked"] is True
    assert row["expected_result"] == "no"
    assert row["kalshi_result"] == "no"


def test_every_committed_monthly_market_matches_against_the_right_value():
    for market in JUN30:
        row = RG.classify_market(market, JUN30_VALUE)
        assert row["category"] == "MATCH", (market["ticker"], row["detail"])
    for market in MAY31:
        row = RG.classify_market(market, MAY31_VALUE)
        assert row["category"] == "MATCH", (market["ticker"], row["detail"])


def test_missing_aaa_value_is_explained_but_the_semantics_leg_still_ran():
    """Absent truth must not silently look like a verified clean market."""
    market = next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.89")
    row = RG.classify_market(market, None)
    assert row["category"] == "NO_AAA_VALUE"
    assert row["explained"] is True
    assert row["semantics_checked"] is True  # checked vs Kalshi's own input
    assert row["expected_result"] is None  # but NOT verified vs our authority


def test_an_unsettled_market_is_explained():
    market = {
        "ticker": "KXAAAGASM-26AUG31-4.60",
        "status": "active",
        "result": "",
        "strike_type": "greater",
        "floor_strike": 4.60,
    }
    row = RG.classify_market(market, 4.10)
    assert row["category"] == "NOT_SETTLED"
    assert row["explained"] is True
    assert row["semantics_checked"] is False


def test_a_terminal_market_with_no_result_is_explained_but_distinct():
    market = {
        "ticker": "KXAAAGASM-26JUN30-3.89",
        "status": "finalized",
        "result": "",
        "strike_type": "greater",
        "floor_strike": 3.89,
    }
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "NO_RESULT"
    assert row["explained"] is True


def test_a_voided_market_is_explained():
    market = dict(JUN30[0], result="void")
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "VOIDED"
    assert row["explained"] is True


@pytest.mark.parametrize(
    "mutation,label",
    [
        pytest.param({"strike_type": None}, "strike_type-None", id="strike_type-None"),
        pytest.param({"strike_type": "less"}, "unverified", id="strike_type-less"),
        pytest.param({"floor_strike": None}, "floor-missing", id="floor-missing"),
        pytest.param({"floor_strike": "cheap"}, "floor-text", id="floor-not-numeric"),
    ],
)
def test_unusable_semantics_are_a_spec_error_never_a_guess(mutation, label):
    market = dict(JUN30[0], **mutation)
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "SPEC_ERROR", label
    assert row["explained"] is False


def test_a_non_numeric_expiration_value_is_a_spec_error():
    market = dict(JUN30[0], expiration_value="three-eighty-four")
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "SPEC_ERROR"
    assert row["explained"] is False


def test_semantics_mismatch_fires_when_our_payoff_disagrees_with_the_exchange():
    """Kalshi settled 3.847 and published NO at strike 3.89. Flip the result."""
    market = dict(
        next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.89"),
        result="yes",
    )
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "SEMANTICS_MISMATCH"
    assert row["explained"] is False
    assert "3.847" in row["detail"]


def test_semantics_leg_catches_the_exact_boundary_from_live_data():
    """The tie markets are the leg's sharpest test: settle == strike -> NO."""
    for market in TIE_MARKETS:
        assert (
            RG.classify_market(market, float(market["expiration_value"]))["category"]
            == "MATCH"
        ), market["ticker"]
        flipped = dict(market, result="yes")
        assert (
            RG.classify_market(flipped, float(market["expiration_value"]))["category"]
            == "SEMANTICS_MISMATCH"
        ), market["ticker"]


def test_truth_mismatch_fires_when_our_aaa_value_drifts_from_kalshis():
    market = next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.00")
    row = RG.classify_market(market, JUN30_VALUE + 0.010)
    assert row["category"] == "TRUTH_MISMATCH"
    assert row["explained"] is False


def test_truth_tolerance_is_tighter_than_the_finest_strike_spacing():
    """A tolerance wider than a strike step could hide a real inversion.

    The finest observed strike spacing is 0.002; the default tolerance must be
    well inside that (``guard-tighter-than-the-gate-it-feeds``).
    """
    assert RG.DEFAULT_TRUTH_TOLERANCE < 0.002 / 2
    market = next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.00")
    # Inside the tolerance: a match. (Deliberately not *exactly* on it -- a
    # float comparison sitting on its own threshold is a coin flip.)
    assert (
        RG.classify_market(market, JUN30_VALUE + RG.DEFAULT_TRUTH_TOLERANCE * 0.5)[
            "category"
        ]
        == "MATCH"
    )
    # Beyond it: a mismatch.
    assert (
        RG.classify_market(market, JUN30_VALUE + RG.DEFAULT_TRUTH_TOLERANCE * 3)[
            "category"
        ]
        == "TRUTH_MISMATCH"
    )
    # A whole tenth of a cent -- the smallest error AAA's own precision can
    # express -- must always be caught.
    assert (
        RG.classify_market(market, round(JUN30_VALUE + 0.001, 3))["category"]
        == "TRUTH_MISMATCH"
    )


def test_result_mismatch_fires_when_only_our_payoff_disagrees():
    """A market with no ``expiration_value`` skips the truth leg but not the outcome leg."""
    market = {
        "ticker": "KXAAAGASM-26JUN30-3.89",
        "status": "finalized",
        "result": "yes",
        "strike_type": "greater",
        "floor_strike": 3.89,
    }
    row = RG.classify_market(market, JUN30_VALUE)
    assert row["category"] == "RESULT_MISMATCH"
    assert row["explained"] is False
    assert row["semantics_checked"] is False


def test_sim_mismatch_fires_when_the_simulator_recorded_the_other_outcome():
    market = next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.89")
    row = RG.classify_market(
        market,
        JUN30_VALUE,
        {"sim_result": "yes", "strategy": "GasConvergence", "pnl": 12.0},
    )
    assert row["category"] == "SIM_MISMATCH"
    assert row["explained"] is False
    assert "GasConvergence" in row["detail"]


def test_an_agreeing_sim_record_still_matches():
    market = next(m for m in JUN30 if m["ticker"] == "KXAAAGASM-26JUN30-3.89")
    row = RG.classify_market(market, JUN30_VALUE, {"sim_result": "no"})
    assert row["category"] == "MATCH"


def test_every_category_is_declared_explained_or_unexplained():
    """A new category that is in neither set would default to unexplained silently."""
    seen = RG.EXPLAINED_CATEGORIES | RG.UNEXPLAINED_CATEGORIES
    assert not RG.EXPLAINED_CATEGORIES & RG.UNEXPLAINED_CATEGORIES
    for category in (
        "NO_AAA_VALUE",
        "NOT_SETTLED",
        "VOIDED",
        "NO_RESULT",
        "NO_EVENT",
        "NO_MARKETS",
        "SPEC_ERROR",
        "SEMANTICS_MISMATCH",
        "TRUTH_MISMATCH",
        "RESULT_MISMATCH",
        "SIM_MISMATCH",
        "LADDER_CONTRADICTION",
        "PIN_MISMATCH",
    ):
        assert category in seen, category


# ======================================================================
# reconcile_period -- ladder-level behaviour
# ======================================================================
def test_a_clean_period_pins_the_interval_and_counts_all_three_legs():
    period = RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, JUN30_VALUE)
    assert period["unexplained"] == 0
    assert period["markets_checked"] == len(JUN30) == 33
    assert period["semantics_verified"] == 33
    assert period["verified"] == 33
    assert period["matched"] == 33
    pinned = period["pinned"]
    assert pinned["value_low_exclusive"] == pytest.approx(3.84)
    assert pinned["value_high_inclusive"] == pytest.approx(3.85)
    assert pinned["interval_width"] == pytest.approx(0.01, abs=1e-9)


def test_no_event_is_explained_and_produces_exactly_one_row():
    """A recent date with no Kalshi event. ``as_of`` is pinned deliberately.

    The classification depends on the settlement date's AGE (see
    ``PRUNED`` below), so leaving ``as_of`` to the wall clock would silently
    change this test's meaning as the months pass.
    """
    period = RG.reconcile_period(
        "KXAAAGASM", "2026-06-30", None, None, as_of="2026-07-30"
    )
    assert [r["category"] for r in period["rows"]] == ["NO_EVENT"]
    assert period["explained"] == 1
    assert period["unexplained"] == 0
    assert period["markets_checked"] == 0
    # A missing event inside the retention window is a real problem, so the
    # period stays IN scope for the per-period coverage floors.
    assert period["coverage_excluded"] is None


def test_an_empty_ladder_is_UNEXPLAINED_not_a_quiet_period():
    """The trap: an event that exists but returns no markets.

    Before this row existed the job reported "0 unexplained mismatches
    (0 markets checked)" forever after a renamed event ticker.
    """
    period = RG.reconcile_period(
        "KXAAAGASM", "2026-06-30", [], JUN30_VALUE, as_of="2026-07-30"
    )
    assert [r["category"] for r in period["rows"]] == ["NO_MARKETS"]
    assert period["unexplained"] == 1
    assert period["coverage_excluded"] is None


def test_a_non_monotonic_ladder_is_a_contradiction_row():
    ladder = [
        {
            "ticker": "KXAAAGASM-26JUN30-4.00",
            "event_ticker": "KXAAAGASM-26JUN30",
            "status": "finalized",
            "strike_type": "greater",
            "floor_strike": 4.00,
            "result": "yes",
            "expiration_value": "4.010",
        },
        {
            "ticker": "KXAAAGASM-26JUN30-3.90",
            "event_ticker": "KXAAAGASM-26JUN30",
            "status": "finalized",
            "strike_type": "greater",
            "floor_strike": 3.90,
            "result": "no",
            "expiration_value": "4.010",
        },
    ]
    period = RG.reconcile_period("KXAAAGASM", "2026-06-30", ladder, None)
    categories = [r["category"] for r in period["rows"]]
    assert "LADDER_CONTRADICTION" in categories
    assert period["unexplained"] >= 1
    assert period["pinned"] is None


def test_pin_mismatch_fires_when_our_value_sits_outside_the_ladders_bracket():
    """A ladder-level check no per-market comparison can express.

    Strip ``expiration_value`` so the truth leg cannot fire, then hand the job
    an AAA value outside the interval the results bracket. Without the pin
    check the per-market rows alone would report this as ordinary result
    mismatches without ever naming the interval.
    """
    ladder = [dict(m) for m in JUN30]
    for market in ladder:
        market.pop("expiration_value", None)
    period = RG.reconcile_period("KXAAAGASM", "2026-06-30", ladder, 4.500)
    categories = [r["category"] for r in period["rows"]]
    assert "PIN_MISMATCH" in categories
    detail = next(r for r in period["rows"] if r["category"] == "PIN_MISMATCH")[
        "detail"
    ]
    assert "3.840" in detail and "3.850" in detail


def test_a_value_inside_the_bracket_produces_no_pin_mismatch():
    ladder = [dict(m) for m in JUN30]
    for market in ladder:
        market.pop("expiration_value", None)
    period = RG.reconcile_period("KXAAAGASM", "2026-06-30", ladder, 3.845)
    assert [r["category"] for r in period["rows"]].count("PIN_MISMATCH") == 0
    assert period["unexplained"] == 0


# ======================================================================
# COVERAGE FLOOR -- every floor is shown to fail, one at a time
# ======================================================================
def _summary(periods, dates, series):
    return {
        "dates": list(dates),
        "series": list(series),
        "periods": periods,
        "totals": RG.aggregate(periods),
    }


def test_coverage_passes_on_a_real_clean_run():
    periods = [RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, JUN30_VALUE)]
    coverage = RG.evaluate_coverage(_summary(periods, ["2026-06-30"], ["KXAAAGASM"]))
    assert coverage["ok"] is True, coverage["failures"]
    assert coverage["markets_checked"] == 33
    assert coverage["semantics_verified"] == 33
    assert coverage["verified"] == 33


def test_markets_floor_fails_on_a_total_outage():
    """Every period NO_EVENT: zero unexplained mismatches, and worthless."""
    periods = [
        RG.reconcile_period("KXAAAGASM", "2026-05-31", None, None),
        RG.reconcile_period("KXAAAGASM", "2026-06-30", None, None),
    ]
    summary = _summary(periods, ["2026-05-31", "2026-06-30"], ["KXAAAGASM"])
    assert summary["totals"]["unexplained"] == 0  # <-- the trap
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is False
    assert any("markets were fetched" in f for f in coverage["failures"])


def test_semantics_floor_fails_when_nothing_in_the_run_was_settled():
    """Reconciling only future periods must not pass."""
    ladder = [
        {
            "ticker": f"KXAAAGASM-26AUG31-{4.0 + i / 100:.2f}",
            "event_ticker": "KXAAAGASM-26AUG31",
            "status": "active",
            "result": "",
            "strike_type": "greater",
            "floor_strike": 4.0 + i / 100,
        }
        for i in range(26)
    ]
    periods = [RG.reconcile_period("KXAAAGASM", "2026-08-31", ladder, None)]
    summary = _summary(periods, ["2026-08-31"], ["KXAAAGASM"])
    assert summary["totals"]["unexplained"] == 0  # <-- the trap
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is False
    assert any("settlement input" in f for f in coverage["failures"])
    # The markets floor is satisfied, so this really is the semantics floor.
    assert not any("markets were fetched" in f for f in coverage["failures"])


def test_verified_floor_fails_when_the_aaa_series_is_missing():
    """The exit-criterion-3 leg. A clean report without AAA truth is vacuous."""
    periods = [RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, None)]
    summary = _summary(periods, ["2026-06-30"], ["KXAAAGASM"])
    assert summary["totals"]["unexplained"] == 0  # <-- the trap
    assert summary["totals"]["semantics_verified"] == 33  # this leg DID run
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is False
    assert any("OUR AAA value" in f for f in coverage["failures"])
    assert any("aaa_daily_national.csv" in f for f in coverage["failures"])


def test_floors_scale_with_the_number_of_periods_requested():
    """A thin single period cannot carry a run that asked for four.

    The floors are aggregates on purpose (one market legitimately unsettled is
    ordinary latency, not a fault), so what scaling buys is this: the same
    ladder that satisfies a one-period run fails a four-period run.
    """
    thin = JUN30[:10]
    one_period = _summary(
        [RG.reconcile_period("KXAAAGASM", "2026-06-30", thin, JUN30_VALUE)],
        ["2026-06-30"],
        ["KXAAAGASM"],
    )
    assert RG.evaluate_coverage(one_period)["ok"] is True

    four_periods = _summary(
        [
            RG.reconcile_period("KXAAAGASM", "2026-06-30", thin, JUN30_VALUE),
            RG.reconcile_period("KXAAAGASM", "2026-05-31", None, None),
            RG.reconcile_period("KXAAAGASD", "2026-06-30", None, None),
            RG.reconcile_period("KXAAAGASD", "2026-05-31", None, None),
        ],
        ["2026-05-31", "2026-06-30"],
        ["KXAAAGASM", "KXAAAGASD"],
    )
    coverage = RG.evaluate_coverage(four_periods)
    assert coverage["periods"] == 4
    assert coverage["markets_floor"] == 4 * RG.DEFAULT_MIN_MARKETS_PER_PERIOD
    assert coverage["verified_floor"] == 4
    assert coverage["ok"] is False
    assert any("markets were fetched" in f for f in coverage["failures"])


def test_the_report_states_the_coverage_numbers_and_the_verdict():
    periods = [RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, None)]
    summary = _summary(periods, ["2026-06-30"], ["KXAAAGASM"])
    summary["generated_at"] = "2026-07-29T00:00:00Z"
    summary["sim_records"] = 0
    summary["aaa_rows_loaded"] = 0
    coverage = RG.evaluate_coverage(summary)
    text = RG.format_report(summary, threshold=0, coverage=coverage)
    assert "Coverage floor (a run that checks nothing must not pass)" in text
    assert "COVERAGE            : FAILED" in text
    assert "Semantics verified : 33" in text
    assert "Outcomes verified  : 0" in text
    assert "semantics verified  :   33" in text  # the coverage block
    assert "outcomes verified   :    0" in text
    # Explained rows are listed, never dropped.
    assert "NO_AAA_VALUE" in text
    # The sim leg says plainly that it had nothing to check.
    assert "NOTHING TO CHECK" in text


def test_the_report_states_the_pinned_interval():
    periods = [RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, JUN30_VALUE)]
    summary = _summary(periods, ["2026-06-30"], ["KXAAAGASM"])
    summary["generated_at"] = "2026-07-29T00:00:00Z"
    summary["sim_records"] = 0
    summary["aaa_rows_loaded"] = 1
    text = RG.format_report(
        summary, threshold=0, coverage=RG.evaluate_coverage(summary)
    )
    assert "(3.840, 3.850]" in text
    assert "COVERAGE            : OK" in text


# ======================================================================
# End to end through reconcile_dates and main()
# ======================================================================
def _fake_fetcher(mapping):
    def fetch(series_ticker, date):
        return mapping.get((series_ticker.upper(), date), None)

    return fetch


def test_reconcile_dates_is_clean_on_the_live_ladders(tmp_path):
    from src.data.gas_settlement import load_aaa_series

    aaa_path = _write_aaa(
        tmp_path, {"2026-06-30": JUN30_VALUE, "2026-05-31": MAY31_VALUE}
    )
    summary = RG.reconcile_dates(
        ["2026-05-31", "2026-06-30"],
        ["KXAAAGASM"],
        market_fetcher=_fake_fetcher(
            {
                ("KXAAAGASM", "2026-06-30"): JUN30,
                ("KXAAAGASM", "2026-05-31"): MAY31,
            }
        ),
        aaa_series=load_aaa_series(aaa_path),
        sim_outcomes={},
        cache={"truth": {}, "markets": {}},
    )
    totals = summary["totals"]
    assert totals["unexplained"] == 0
    assert totals["markets_checked"] == 74
    assert totals["verified"] == 74
    assert totals["semantics_verified"] == 74
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is True, coverage["failures"]
    # Both month-ends were recorded with their pinned interval.
    truth = summary["cache"]["truth"]
    assert set(truth) == {"KXAAAGASM|2026-05-31", "KXAAAGASM|2026-06-30"}
    assert truth["KXAAAGASM|2026-06-30"]["pinned"]["interval_width"] == pytest.approx(
        0.01, abs=1e-9
    )
    assert summary["cache"]["markets"]["KXAAAGASM-26JUN30-3.89"]["result"] == "no"


def test_a_single_perturbed_aaa_value_turns_the_run_red(tmp_path):
    """Prove the clean run above is not clean by construction."""
    from src.data.gas_settlement import load_aaa_series

    aaa_path = _write_aaa(
        tmp_path,
        {"2026-06-30": round(JUN30_VALUE + 0.010, 3), "2026-05-31": MAY31_VALUE},
    )
    summary = RG.reconcile_dates(
        ["2026-05-31", "2026-06-30"],
        ["KXAAAGASM"],
        market_fetcher=_fake_fetcher(
            {
                ("KXAAAGASM", "2026-06-30"): JUN30,
                ("KXAAAGASM", "2026-05-31"): MAY31,
            }
        ),
        aaa_series=load_aaa_series(aaa_path),
        sim_outcomes={},
        cache={"truth": {}, "markets": {}},
    )
    totals = summary["totals"]
    assert totals["unexplained"] > 0
    assert totals["by_category"].get("TRUTH_MISMATCH", 0) == 33


def test_the_reconcile_never_validates_against_its_own_cache(tmp_path):
    """The cache is this job's OUTPUT, so it must not be read back as truth.

    A previous version of this job read the settlement cache before the AAA
    series. Two consequences, both bad: a run validated against the number it
    had recorded itself, and a later correction to
    ``aaa_daily_national.csv`` was permanently shadowed -- the job stayed green
    on the stale value. This test hands the job a *correct* cache and a *wrong*
    CSV and requires it to go red on the CSV.
    """
    from src.data.gas_settlement import load_aaa_series

    aaa_path = _write_aaa(tmp_path, {"2026-06-30": 4.500})
    cache = {
        "truth": {
            "KXAAAGASM|2026-06-30": {
                "value": JUN30_VALUE,
                "source": "a-previous-run-of-this-very-job",
                "source_url": "",
            }
        },
        "markets": {},
    }
    summary = RG.reconcile_dates(
        ["2026-06-30"],
        ["KXAAAGASM"],
        market_fetcher=_fake_fetcher({("KXAAAGASM", "2026-06-30"): JUN30}),
        aaa_series=load_aaa_series(aaa_path),
        sim_outcomes={},
        cache=cache,
    )
    assert summary["totals"]["unexplained"] > 0
    assert summary["periods"][0]["aaa_value"] == pytest.approx(4.500)
    assert summary["periods"][0]["truth_source"] == "test_fixture"


def test_the_runtime_resolver_does_read_the_cache_first(tmp_path):
    """...while the runtime settlement resolver reads the recorder's cache first.

    That is the weather split: the reconcile job writes the cache, the runtime
    reads it, and neither validates against its own output.
    """
    import json as _json

    from src.data.gas_settlement import load_aaa_series, resolve_settlement_value

    aaa_path = _write_aaa(tmp_path, {"2026-06-30": 4.500})
    cache_path = tmp_path / "settlement_cache.json"
    cache_path.write_text(
        _json.dumps(
            {
                "truth": {
                    "KXAAAGASM|2026-06-30": {"value": JUN30_VALUE, "source": "recorder"}
                },
                "markets": {},
            }
        ),
        encoding="utf-8",
    )
    value = resolve_settlement_value(
        "KXAAAGASM-26JUN30-3.89",
        cache_path=str(cache_path),
        aaa_series=load_aaa_series(aaa_path),
    )
    assert value == pytest.approx(JUN30_VALUE)


def test_a_mixed_cadence_run_does_not_reconcile_off_cadence_pairs(tmp_path):
    """Monthly plus daily in one run must not ask the monthly series for a Tuesday.

    Two things go wrong without ``per_series_dates``: an explained NO_EVENT row
    for every off-cadence pair (report noise), and a period count taken from the
    cross product rather than from the periods that exist -- which scales every
    floor to a denominator the run was never going to fill and fails a healthy
    run. The live 2-monthly + 2-daily run reported "floor 64 over 8 period(s)"
    when there were four periods.
    """
    from src.data.gas_settlement import load_aaa_series

    aaa_path = _write_aaa(tmp_path, {"2026-06-30": JUN30_VALUE})
    per_series = {"KXAAAGASM": ["2026-06-30"], "KXAAAGASD": ["2026-07-28"]}
    summary = RG.reconcile_dates(
        ["2026-06-30", "2026-07-28"],
        ["KXAAAGASM", "KXAAAGASD"],
        market_fetcher=_fake_fetcher({("KXAAAGASM", "2026-06-30"): JUN30}),
        aaa_series=load_aaa_series(aaa_path),
        sim_outcomes={},
        cache={"truth": {}, "markets": {}},
        per_series_dates=per_series,
    )
    reconciled = {(p["series_ticker"], p["date"]) for p in summary["periods"]}
    assert reconciled == {("KXAAAGASM", "2026-06-30"), ("KXAAAGASD", "2026-07-28")}
    # 2 real periods, not the 4 of the cross product.
    coverage = RG.evaluate_coverage(summary)
    assert coverage["periods"] == 2
    assert coverage["markets_floor"] == 2 * RG.DEFAULT_MIN_MARKETS_PER_PERIOD


def test_the_cross_product_period_count_would_fail_a_healthy_mixed_run():
    """Same reconciled periods, two denominators, opposite verdicts.

    Nine markets per period clears the 8/period floor for the two periods that
    exist, and misses the floor for the four the cross product imagines. The
    only difference between the two evaluations below is whether
    ``per_series_dates`` is present, so this isolates the denominator.
    """
    periods = [
        RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30[:9], JUN30_VALUE),
        RG.reconcile_period("KXAAAGASD", "2026-07-28", JUN30[:9], JUN30_VALUE),
    ]
    exact = _summary(periods, ["2026-06-30", "2026-07-28"], ["KXAAAGASM", "KXAAAGASD"])
    exact["per_series_dates"] = {
        "KXAAAGASM": ["2026-06-30"],
        "KXAAAGASD": ["2026-07-28"],
    }
    cross = _summary(periods, ["2026-06-30", "2026-07-28"], ["KXAAAGASM", "KXAAAGASD"])

    exact_coverage = RG.evaluate_coverage(exact)
    cross_coverage = RG.evaluate_coverage(cross)
    assert exact_coverage["periods"] == 2
    assert cross_coverage["periods"] == 4
    assert exact_coverage["ok"] is True, exact_coverage["failures"]
    assert cross_coverage["ok"] is False  # the spurious failure being removed


def test_main_exits_0_on_a_clean_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        RG, "fetch_event_markets", _fake_fetcher({("KXAAAGASM", "2026-06-30"): JUN30})
    )
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    aaa_path = _write_aaa(tmp_path, {"2026-06-30": JUN30_VALUE})
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--periods",
            "1",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            aaa_path,
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == 0
    written = os.listdir(tmp_path / "reports")
    assert "reconcile_gas_2026-06-30.txt" in written
    assert "reconcile_gas_2026-06-30.json" in written


def test_main_exits_1_on_an_unexplained_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        RG, "fetch_event_markets", _fake_fetcher({("KXAAAGASM", "2026-06-30"): JUN30})
    )
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    aaa_path = _write_aaa(tmp_path, {"2026-06-30": 4.500})
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            aaa_path,
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == 1


def test_main_exits_3_when_the_run_verified_nothing(tmp_path, monkeypatch):
    """A run that checks nothing must not pass -- exercised through the CLI."""
    monkeypatch.setattr(RG, "fetch_event_markets", lambda series, date: None)
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            str(tmp_path / "absent.csv"),
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == RG.EXIT_COVERAGE == 3


def test_main_exits_3_when_the_ladder_is_there_but_the_aaa_series_is_not(
    tmp_path, monkeypatch
):
    """The realistic Phase 4 failure: Kalshi fine, AAA authority absent."""
    monkeypatch.setattr(
        RG, "fetch_event_markets", _fake_fetcher({("KXAAAGASM", "2026-06-30"): JUN30})
    )
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            str(tmp_path / "absent.csv"),
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == 3
    text = (tmp_path / "reports" / "reconcile_gas_2026-06-30.txt").read_text(
        encoding="utf-8"
    )
    assert "COVERAGE            : FAILED" in text
    assert "0 unexplained" not in text.split("UNEXPLAINED")[0]


def test_main_rejects_an_unknown_series():
    assert RG.main(["--series", "KXNOTAGASSERIES"]) == 2


def test_main_rejects_a_nonsense_period_count():
    assert RG.main(["--series", "KXAAAGASM", "--periods", "0"]) == 2


# ======================================================================
# D1 (a): the standing cron must cover every series that is harvested
# ======================================================================
#
# The 2026-07-30 red-team finding: the standing cron line read
# ``--series KXAAAGASM,KXAAAGASD``, which excludes ``KXAAAGASW`` -- a series
# ``gas_bot.GAS_SERIES`` actually harvests, and the only one carrying a truth
# disagreement. "0 mismatches" was therefore a property of the chosen scope.


def _docstring_cron_series():
    """The series list the module docstring's cron line actually passes.

    Audited from the docstring text, not from the constant: the operator copies
    the cron line, so the line is the gate (``grep-the-gate-not-the-claim``).
    """
    text = RG.__doc__ or ""
    assert "\nCRON " in text, "the module docstring no longer carries a CRON section"
    cron_block = text.split("\nCRON ", 1)[1].split("\nExit codes", 1)[0]
    assert "--series " in cron_block, (
        "the CRON section no longer passes --series, so the standing job would "
        "reconcile only the CLI default"
    )
    tail = cron_block.split("--series ", 1)[1]
    return tuple(s.strip().upper() for s in tail.split()[0].split(",") if s.strip())


def test_the_standing_cron_covers_every_series_the_bot_harvests():
    from src.bots.gas_bot import GAS_SERIES as HARVESTED

    cron = set(_docstring_cron_series())
    assert cron == set(RG.STANDING_CRON_SERIES), (
        f"the docstring cron line passes {sorted(cron)} but STANDING_CRON_SERIES "
        f"is {sorted(RG.STANDING_CRON_SERIES)} -- the constant nobody passes is "
        f"not the cron line"
    )
    missing = set(HARVESTED) - cron
    assert not missing, (
        f"the standing reconcile cron omits {sorted(missing)}, which gas_bot "
        f"harvests. A reconcile that omits a harvested series is not a "
        f"reconcile: '0 mismatches' would be a property of the chosen scope."
    )


def test_the_standing_cron_covers_every_series_this_job_governs():
    """Not just the harvested ones: all three settle on the same AAA number."""
    from src.data.gas_settlement import GAS_SERIES

    assert set(_docstring_cron_series()) == set(GAS_SERIES)


def test_every_cron_series_is_actually_accepted_by_the_cli():
    """A cron line naming a series the parser rejects would exit 2 nightly."""
    from src.data.gas_settlement import get_series

    for series in _docstring_cron_series():
        assert get_series(series).series_ticker == series


# ======================================================================
# D1 (b): the isolated truth disagreement is a registered, dated exception
#         -- NOT a widened tolerance
# ======================================================================


def test_the_truth_tolerance_was_not_widened_to_absorb_the_disagreement():
    """The whole point. $0.0040 is 8x the tolerance; hiding it would blind the leg."""
    assert RG.DEFAULT_TRUTH_TOLERANCE == 0.0005
    gap = abs(JUL13_KALSHI_VALUE - JUL13_OUR_VALUE)
    assert gap == pytest.approx(0.004, abs=1e-9)
    assert gap > 8 * RG.DEFAULT_TRUTH_TOLERANCE - 1e-12
    # A tolerance wide enough to swallow it would also swallow two full steps of
    # the finest observed ladder (0.002 spacing).
    assert gap > 2 * 0.002 - 1e-12


def test_the_exception_register_holds_exactly_the_documented_entries():
    """Adding an entry requires editing this test -- a deliberate act.

    The register is the one place a disagreement can be marked explained, so its
    contents are pinned rather than merely reviewed. Each entry must also carry
    the evidence that justifies it.
    """
    assert set(RG.TRUTH_EXCEPTIONS) == {JUL13_KEY}
    entry = RG.TRUTH_EXCEPTIONS[JUL13_KEY]
    assert entry["rule"] == "AAA_INTRADAY_REVISION"
    assert entry["registered_on"] == "2026-07-30"
    assert entry["kalshi_expiration_value"] == pytest.approx(JUL13_KALSHI_VALUE)
    assert entry["our_aaa_value"] == pytest.approx(JUL13_OUR_VALUE)
    assert len(entry["evidence"]) > 200, "an exception without evidence is a waiver"
    for rel in entry["evidence_paths"]:
        assert os.path.exists(os.path.join(REPO_ROOT, rel)), rel


def test_the_committed_ladder_is_the_evidence_the_register_describes():
    """Audit the fixture against the register's claims rather than trusting them."""
    assert len(JUL13_WEEKLY) == 20
    assert {m["event_ticker"] for m in JUL13_WEEKLY} == {"KXAAAGASW-26JUL13"}
    settled_on = {float(m["expiration_value"]) for m in JUL13_WEEKLY}
    assert len(settled_on) == 1
    assert settled_on.pop() == pytest.approx(JUL13_KALSHI_VALUE)
    # The ladder brackets the settle to (3.860, 3.880] -- and BOTH values sit
    # strictly inside it, which is why no published result can distinguish them.
    from src.data.gas_settlement import pin_truth_from_ladder

    pinned = pin_truth_from_ladder(JUL13_WEEKLY)
    assert pinned.low_exclusive == pytest.approx(3.86)
    assert pinned.high_inclusive == pytest.approx(3.88)
    assert pinned.contains(JUL13_OUR_VALUE) and pinned.contains(JUL13_KALSHI_VALUE)


def test_the_registered_exception_applies_to_the_real_ladder():
    """Every market is explained, and every one is still VERIFIED vs our value."""
    period = RG.reconcile_period(
        "KXAAAGASW", "2026-07-13", JUL13_WEEKLY, JUL13_OUR_VALUE, as_of=AS_OF
    )
    assert period["unexplained"] == 0
    assert period["truth_exceptions"] == 20
    assert {r["category"] for r in period["rows"]} == {"TRUTH_EXCEPTION"}
    assert all(r["explained"] for r in period["rows"])
    # The exit-criterion-3 leg was NOT skipped: every outcome was recomputed
    # from OUR AAA value and agreed with Kalshi's published result.
    assert period["verified"] == 20
    assert period["semantics_verified"] == 20
    for row in period["rows"]:
        assert row["truth_exception"] == "AAA_INTRADAY_REVISION"
        assert row["expected_result"] == row["kalshi_result"]


def test_the_same_disagreement_on_an_UNREGISTERED_date_is_a_breach():
    """The exception is pinned to one date; it does not generalise.

    This is the guard against the tempting-but-wrong version of this fix -- a
    rule like "if it matches our previous-day value, explain it" -- which would
    absorb a systematic one-day shift silently.
    """
    ladder = [
        dict(
            m,
            ticker=m["ticker"].replace("26JUL13", "26JUL20"),
            event_ticker="KXAAAGASW-26JUL20",
        )
        for m in JUL13_WEEKLY
    ]
    period = RG.reconcile_period(
        "KXAAAGASW", "2026-07-20", ladder, JUL13_OUR_VALUE, as_of=AS_OF
    )
    assert period["truth_exceptions"] == 0
    assert period["unexplained"] == 20
    assert {r["category"] for r in period["rows"]} == {"TRUTH_MISMATCH"}


@pytest.mark.parametrize(
    "kalshi_value,our_value,needle",
    [
        pytest.param(3.900, JUL13_OUR_VALUE, "pinned to Kalshi's", id="kalshi-moved"),
        pytest.param(JUL13_KALSHI_VALUE, 3.800, "pinned to our", id="ours-moved"),
    ],
)
def test_the_exception_stops_applying_when_either_number_moves(
    kalshi_value, our_value, needle
):
    """Condition 2 and condition 3. A future shift cannot inherit the exception."""
    market = dict(JUL13_WEEKLY[0], expiration_value=f"{kalshi_value:.4f}")
    row = RG.classify_market(market, our_value)
    assert row["category"] == "TRUTH_MISMATCH"
    assert row["explained"] is False
    assert needle in row["detail"]
    assert row["truth_exception"] is None


def test_the_exception_does_not_cover_a_difference_that_changes_an_OUTCOME():
    """Condition 4, recomputed live: immateriality is measured, not asserted.

    Synthesise a strike between the two values (3.874) on the registered date.
    Kalshi's 3.876 pays YES there and our 3.872 pays NO, so the disagreement is
    material for that market -- and the registered date must not license it.
    """
    market = dict(
        JUL13_WEEKLY[0],
        ticker="KXAAAGASW-26JUL13-3.874",
        floor_strike=3.874,
        result="yes",  # what Kalshi would publish on 3.876
    )
    row = RG.classify_market(market, JUL13_OUR_VALUE)
    assert row["category"] == "TRUTH_MISMATCH"
    assert row["explained"] is False
    assert "immaterial" in row["detail"]
    assert "NO on 3.872" in row["detail"]


def test_an_excepted_market_with_a_wrong_OUTCOME_still_breaches():
    """The outcome leg runs after the exception, so a real error still fires."""
    market = dict(JUL13_WEEKLY[0], result="no")  # strike 3.62; 3.872 > 3.62 -> yes
    row = RG.classify_market(market, JUL13_OUR_VALUE)
    # The semantics leg catches it first (Kalshi's own value disagrees too),
    # which is the right order -- but either way it is UNEXPLAINED, not excepted.
    assert row["explained"] is False
    assert row["category"] in ("SEMANTICS_MISMATCH", "RESULT_MISMATCH")
    assert row["truth_exception"] is None


def test_an_excepted_market_whose_own_payoff_is_wrong_breaches_as_RESULT_MISMATCH():
    """Strip ``expiration_value`` so only the outcome leg can speak.

    Without ``expiration_value`` the truth leg cannot fire at all, so this
    isolates the claim that being on a registered date never short-circuits the
    exit-criterion-3 comparison.
    """
    market = dict(JUL13_WEEKLY[0], result="no")
    market.pop("expiration_value", None)
    row = RG.classify_market(market, JUL13_OUR_VALUE)
    assert row["category"] == "RESULT_MISMATCH"
    assert row["explained"] is False


def test_a_systematic_shift_across_the_window_cannot_hide_behind_the_exception():
    """The failure mode the register must not create.

    Present the SAME $0.004 disagreement on four consecutive weekly periods,
    each with a real, parseable event-date label. The registered date is
    explained; the other three are not, so the run is red -- which is how a
    systematic shift announces itself rather than being absorbed.

    The labels must be genuine (``26JUL06``, not ``26XXX01``): an unparseable
    segment makes ``registered_truth_exception`` return None for a reason that
    has nothing to do with the date not being registered, which would make this
    test pass for the wrong reason.
    """
    periods = []
    for date, label in [
        ("2026-07-13", "26JUL13"),
        ("2026-07-06", "26JUL06"),
        ("2026-06-29", "26JUN29"),
        ("2026-06-22", "26JUN22"),
    ]:
        ladder = [
            dict(
                m,
                ticker=m["ticker"].replace("26JUL13", label),
                event_ticker=f"KXAAAGASW-{label}",
            )
            for m in JUL13_WEEKLY
        ]
        # Every ticker still carries a date segment the register can look up.
        from src.data.gas_settlement import settlement_date_for

        assert settlement_date_for(ladder[0]["ticker"]).isoformat() == date
        periods.append(
            RG.reconcile_period("KXAAAGASW", date, ladder, JUL13_OUR_VALUE, as_of=AS_OF)
        )
    totals = RG.aggregate(periods)
    assert totals["truth_exceptions"] == 20  # only the registered date
    assert totals["by_category"]["TRUTH_MISMATCH"] == 60  # the other three
    assert totals["unexplained"] == 60


def test_TRUTH_EXCEPTION_is_reported_by_name_with_its_evidence():
    """An exception summarised as a plain MATCH is one nobody revisits."""
    periods = [
        RG.reconcile_period(
            "KXAAAGASW", "2026-07-13", JUL13_WEEKLY, JUL13_OUR_VALUE, as_of=AS_OF
        )
    ]
    summary = _summary(periods, ["2026-07-13"], ["KXAAAGASW"])
    summary["generated_at"] = "2026-07-30T00:00:00Z"
    summary["sim_records"] = 0
    summary["aaa_rows_loaded"] = 1
    text = RG.format_report(
        summary, threshold=0, coverage=RG.evaluate_coverage(summary)
    )
    assert "Truth exceptions   : 20" in text
    assert "REGISTERED TRUTH EXCEPTIONS APPLIED" in text
    assert "AAA_INTRADAY_REVISION" in text
    assert "tolerance was NOT widened" in text
    assert "evidence: tests/fixtures/gas/kalshi_pinned_truth.csv" in text
    assert "TRUTH_EXCEPTION" in text
    assert "COVERAGE            : OK" in text


# ======================================================================
# D1 (c): Kalshi's settled-market retention is an EXPLAINED condition
# ======================================================================


def test_an_old_empty_ladder_is_PRUNED_not_an_unexplained_failure():
    """--periods 3 on the monthly series hits Kalshi's retention, not a bug.

    2026-04-30 is 91 days before 2026-07-30 and its ladder comes back empty.
    Paging a cron about the exchange's retention policy is noise.
    """
    period = RG.reconcile_period("KXAAAGASM", "2026-04-30", [], 4.300, as_of=AS_OF)
    (row,) = period["rows"]
    assert row["category"] == "PRUNED"
    assert row["explained"] is True
    assert period["unexplained"] == 0
    assert period["age_days"] == 91
    assert period["coverage_excluded"] == "PRUNED"
    assert "retention" in row["detail"]


def test_a_recent_empty_ladder_is_still_an_unexplained_NO_MARKETS():
    """The guard stays live where it matters: inside the retention window."""
    period = RG.reconcile_period(
        "KXAAAGASM", "2026-06-30", [], JUN30_VALUE, as_of=AS_OF
    )
    (row,) = period["rows"]
    assert row["category"] == "NO_MARKETS"
    assert row["explained"] is False
    assert period["unexplained"] == 1
    assert period["coverage_excluded"] is None


def test_an_old_missing_event_is_also_PRUNED():
    period = RG.reconcile_period("KXAAAGASM", "2026-01-31", None, None, as_of=AS_OF)
    (row,) = period["rows"]
    assert row["category"] == "PRUNED"
    assert row["explained"] is True
    assert period["coverage_excluded"] == "PRUNED"


@pytest.mark.parametrize(
    "age,expected",
    [
        pytest.param(60, "NO_MARKETS", id="exactly-at-the-horizon-stays-unexplained"),
        pytest.param(61, "PRUNED", id="one-day-past-the-horizon"),
    ],
)
def test_the_retention_horizon_boundary_is_exact(age, expected):
    """The horizon is evidence-bounded: [59, 90) days observed live.

    60 is the conservative choice, so a period exactly 60 days old -- which we
    have SEEN Kalshi still serve -- must not be explained away.
    """
    from datetime import date, timedelta

    day = (date.fromisoformat(AS_OF) - timedelta(days=age)).isoformat()
    period = RG.reconcile_period("KXAAAGASD", day, [], 4.100, as_of=AS_OF)
    assert period["rows"][0]["category"] == expected
    assert RG.RETENTION_DAYS == 60


def test_the_retention_horizon_is_configurable_and_still_bites():
    """A shorter horizon must not silently explain everything either."""
    period = RG.reconcile_period(
        "KXAAAGASM", "2026-06-30", [], JUN30_VALUE, as_of=AS_OF, retention_days=10
    )
    assert period["rows"][0]["category"] == "PRUNED"


def test_a_pruned_period_is_excluded_from_the_per_period_floor_and_counted():
    periods = [
        RG.reconcile_period("KXAAAGASM", "2026-04-30", [], 4.300, as_of=AS_OF),
        RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, JUN30_VALUE, as_of=AS_OF),
    ]
    summary = _summary(periods, ["2026-04-30", "2026-06-30"], ["KXAAAGASM"])
    coverage = RG.evaluate_coverage(summary)
    assert coverage["periods_excluded"] == 1
    assert coverage["periods_evaluated"] == 1
    assert coverage["ok"] is True, coverage["failures"]
    excluded = coverage["excluded"][0]
    assert (excluded["date"], excluded["excluded"]) == ("2026-04-30", "PRUNED")


def test_a_run_where_EVERY_period_is_pruned_fails_rather_than_passing_vacuously():
    """Exclusions must not become a way to pass while verifying nothing."""
    periods = [
        RG.reconcile_period("KXAAAGASM", "2026-01-31", [], None, as_of=AS_OF),
        RG.reconcile_period("KXAAAGASM", "2026-02-28", [], None, as_of=AS_OF),
    ]
    summary = _summary(periods, ["2026-01-31", "2026-02-28"], ["KXAAAGASM"])
    assert summary["totals"]["unexplained"] == 0  # <-- the trap
    coverage = RG.evaluate_coverage(summary)
    assert coverage["periods_excluded"] == 2
    assert coverage["periods_evaluated"] == 0
    assert coverage["ok"] is False
    assert any(
        "excluded from" in f and "verified nothing" in f for f in coverage["failures"]
    )


def test_the_report_prints_the_exclusion_count_with_its_rationale():
    """A widening carve-out must be a rising number, not a silence."""
    periods = [
        RG.reconcile_period("KXAAAGASM", "2026-04-30", [], 4.300, as_of=AS_OF),
        RG.reconcile_period("KXAAAGASM", "2026-06-30", JUN30, JUN30_VALUE, as_of=AS_OF),
    ]
    summary = _summary(periods, ["2026-04-30", "2026-06-30"], ["KXAAAGASM"])
    summary["generated_at"] = "2026-07-30T00:00:00Z"
    summary["sim_records"] = 0
    summary["aaa_rows_loaded"] = 2
    text = RG.format_report(
        summary, threshold=0, coverage=RG.evaluate_coverage(summary)
    )
    assert "periods EXCLUDED    : 1" in text
    assert "EXCLUDED (PRUNED)" in text
    assert "PRUNED" in text.split("Non-match categories")[1]


# ======================================================================
# D3: the coverage floor is PER PERIOD, so partial AAA loss is visible
# ======================================================================
#
# The 2026-07-30 red-team finding, verbatim: the exact standing cron with AAA
# degraded to 1 of 4 periods reported
#
#     AAA rows loaded : 1   Markets checked : 108   Outcomes verified : 33 (floor 4)
#     COVERAGE : OK   UNEXPLAINED : 0   EXITCODE=0
#
# Three of four periods verified NOTHING against the settlement authority.


def _four_period_run(covered):
    """The standing cron's four periods, with AAA covering only ``covered``."""
    plan = [
        ("KXAAAGASM", "2026-05-31", MAY31, MAY31_VALUE),
        ("KXAAAGASM", "2026-06-30", JUN30, JUN30_VALUE),
        ("KXAAAGASD", "2026-07-28", JUN30, JUN30_VALUE),
        ("KXAAAGASD", "2026-07-29", MAY31, MAY31_VALUE),
    ]
    periods = [
        RG.reconcile_period(
            series,
            date,
            ladder,
            value if date in covered else None,
            as_of=AS_OF,
        )
        for series, date, ladder, value in plan
    ]
    summary = _summary(
        periods,
        ["2026-05-31", "2026-06-30", "2026-07-28", "2026-07-29"],
        ["KXAAAGASM", "KXAAAGASD"],
    )
    summary["per_series_dates"] = {
        "KXAAAGASM": ["2026-05-31", "2026-06-30"],
        "KXAAAGASD": ["2026-07-28", "2026-07-29"],
    }
    return summary


def test_the_aggregate_floor_alone_passed_the_red_teams_degraded_run():
    """Establish that the aggregate really is blind before fixing it.

    33 outcomes verified against an aggregate floor of 4 is a pass, and it was
    the pass that let three empty periods through. Asserted so the per-period
    result below cannot be mistaken for a change in the aggregate.
    """
    summary = _four_period_run({"2026-06-30"})
    totals = summary["totals"]
    assert totals["unexplained"] == 0
    assert totals["verified"] == 33
    coverage = RG.evaluate_coverage(summary)
    assert coverage["verified"] >= coverage["verified_floor"]  # aggregate: PASS
    assert coverage["markets_checked"] >= coverage["markets_floor"]
    assert coverage["semantics_verified"] >= coverage["semantics_floor"]


def test_partial_aaa_loss_fails_the_per_period_floor():
    """The fix: a period that verified nothing cannot be averaged away."""
    summary = _four_period_run({"2026-06-30"})
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is False
    assert coverage["periods_evaluated"] == 4
    assert coverage["periods_failing"] == 3
    failed = {(e["date"], e["verified"]) for e in coverage["per_period"] if not e["ok"]}
    assert failed == {("2026-05-31", 0), ("2026-07-28", 0), ("2026-07-29", 0)}
    for date in ("2026-05-31", "2026-07-28", "2026-07-29"):
        assert any(
            date in f and "verified NOTHING against the settlement authority" in f
            for f in coverage["failures"]
        ), date


def test_the_wider_daily_case_fails_too():
    """17 daily periods, AAA covering 1: 321 of 338 markets never compared."""
    covered = "2026-07-29"
    periods = [
        RG.reconcile_period(
            "KXAAAGASD",
            f"2026-07-{day:02d}",
            JUN30[:20],
            JUN30_VALUE if f"2026-07-{day:02d}" == covered else None,
            as_of=AS_OF,
        )
        for day in range(13, 30)
    ]
    summary = _summary(periods, [p["date"] for p in periods], ["KXAAAGASD"])
    assert summary["totals"]["unexplained"] == 0  # <-- the trap
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is False
    assert coverage["periods_failing"] == 16


def test_a_fully_covered_four_period_run_still_passes():
    """The floor must not red a healthy run -- the other half of the gate."""
    summary = _four_period_run({"2026-05-31", "2026-06-30", "2026-07-28", "2026-07-29"})
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is True, coverage["failures"]
    assert coverage["periods_failing"] == 0
    assert all(e["verified"] > 0 for e in coverage["per_period"])


def test_total_aaa_absence_still_fails_with_the_aggregate_message():
    """Keep the existing total-absence behaviour, unchanged."""
    summary = _four_period_run(set())
    coverage = RG.evaluate_coverage(summary)
    assert coverage["ok"] is False
    assert any("OUR AAA value" in f for f in coverage["failures"])
    assert any("aaa_daily_national.csv" in f for f in coverage["failures"])


def test_one_market_of_latency_inside_a_period_is_still_ordinary():
    """Per-period floors are counts, not ratios.

    A single unsettled contract in an otherwise complete ladder must stay
    ordinary latency -- otherwise the fix trades a false negative for a nightly
    false positive.
    """
    ladder = [dict(m) for m in JUN30]
    ladder[0] = dict(ladder[0], status="active", result="")
    period = RG.reconcile_period(
        "KXAAAGASM", "2026-06-30", ladder, JUN30_VALUE, as_of=AS_OF
    )
    summary = _summary(periods=[period], dates=["2026-06-30"], series=["KXAAAGASM"])
    coverage = RG.evaluate_coverage(summary)
    assert period["verified"] == 32
    assert coverage["ok"] is True, coverage["failures"]


def test_the_report_prints_every_periods_verified_count_and_floor():
    """ "Print the per-period verified count and its floor on every run."" """
    summary = _four_period_run({"2026-06-30"})
    summary["generated_at"] = "2026-07-30T00:00:00Z"
    summary["sim_records"] = 0
    summary["aaa_rows_loaded"] = 1
    coverage = RG.evaluate_coverage(summary)
    text = RG.format_report(summary, threshold=0, coverage=coverage)
    assert "Per-period coverage floor" in text
    assert "COVERAGE            : FAILED" in text
    assert "periods evaluated   : 4  (3 failing)" in text
    for date in ("2026-05-31", "2026-06-30", "2026-07-28", "2026-07-29"):
        assert date in text
    # Each row shows observed/floor for all three legs, pass or fail.
    assert "   0/1   " in text  # a period that verified nothing
    assert "  33/1   " in text  # the one that did
    assert "FAILED" in text and "OK" in text


def test_the_per_period_floor_is_configurable_and_reported():
    summary = _four_period_run({"2026-05-31", "2026-06-30", "2026-07-28", "2026-07-29"})
    coverage = RG.evaluate_coverage(summary, min_verified_per_period=40)
    assert coverage["min_verified_per_period"] == 40
    assert coverage["ok"] is False
    # 41 markets on MAY31 clears 40; the 33-market JUN30 ladder does not.
    assert {e["date"] for e in coverage["per_period"] if not e["ok"]} == {
        "2026-06-30",
        "2026-07-28",
    }


def test_main_exits_3_on_a_partially_degraded_aaa_series(tmp_path, monkeypatch):
    """End to end through the CLI: the red-team's scenario now exits 3."""
    monkeypatch.setattr(
        RG,
        "fetch_event_markets",
        _fake_fetcher(
            {("KXAAAGASM", "2026-06-30"): JUN30, ("KXAAAGASM", "2026-05-31"): MAY31}
        ),
    )
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    aaa_path = _write_aaa(tmp_path, {"2026-06-30": JUN30_VALUE})  # 05-31 missing
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--periods",
            "2",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            aaa_path,
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == RG.EXIT_COVERAGE == 3
    text = (tmp_path / "reports" / "reconcile_gas_2026-06-30.txt").read_text(
        encoding="utf-8"
    )
    assert "COVERAGE            : FAILED" in text
    assert "2026-05-31" in text and "verified NOTHING" in text


def test_main_still_exits_0_when_every_period_is_covered(tmp_path, monkeypatch):
    monkeypatch.setattr(
        RG,
        "fetch_event_markets",
        _fake_fetcher(
            {("KXAAAGASM", "2026-06-30"): JUN30, ("KXAAAGASM", "2026-05-31"): MAY31}
        ),
    )
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    aaa_path = _write_aaa(
        tmp_path, {"2026-06-30": JUN30_VALUE, "2026-05-31": MAY31_VALUE}
    )
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--periods",
            "2",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            aaa_path,
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == 0


# ======================================================================
# D3 (cont.): the two named exemptions, and the suspect-row policy
# ======================================================================
#
# A per-period floor that fires on the AAA authority's OWN declared behaviour is
# a floor somebody switches off. Two conditions are therefore exempted by name
# and counted on every run -- and each is proved unable to carry a whole run.


def _write_aaa_rows(tmp_path, rows, *, name="aaa.csv"):
    """AAA CSV from ``{date: (value, quality)}``. Synthetic stand-in, see above."""
    lines = [AAA_HEADER]
    for date, (value, quality) in sorted(rows.items()):
        lines.append(
            f"{date},{value},test_fixture,"
            f"https://example.invalid/synthetic-stand-in,"
            f"2026-07-29T00:00:00Z,,{quality}"
        )
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _dense_daily(first_day, last_day, value=JUN30_VALUE, *, skip=()):
    """A contiguous run of July AAA rows. The value matches the JUN30 ladder's
    own ``expiration_value`` so the truth leg is satisfied and these tests
    isolate the COVERAGE behaviour rather than re-testing the truth leg."""
    return {
        f"2026-07-{d:02d}": (value, "ok")
        for d in range(first_day, last_day + 1)
        if f"2026-07-{d:02d}" not in skip
    }


class TestIsolatedAaaGapExemption:
    def test_a_bracketed_gap_exempts_only_the_outcome_floor(self, tmp_path):
        """Contract §1.1: a missing day is a missing row, by design.

        2026-07-20 has no AAA row but 07-17..07-19 and 07-21..07-23 all do, so
        the harvest demonstrably covers the neighbourhood.
        """
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, _dense_daily(15, 25, skip=("2026-07-20",))),
            include_suspect=True,
        )
        summary = RG.reconcile_dates(
            ["2026-07-19", "2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher(
                {
                    ("KXAAAGASD", "2026-07-19"): JUN30,
                    ("KXAAAGASD", "2026-07-20"): JUN30,
                }
            ),
            aaa_series=aaa,
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
        )
        gap = next(p for p in summary["periods"] if p["date"] == "2026-07-20")
        assert gap["verified"] == 0
        assert gap["verified_exempt"] == "AAA_GAP"
        coverage = RG.evaluate_coverage(summary)
        assert coverage["ok"] is True, coverage["failures"]
        assert coverage["periods_verified_exempt"] == 1
        # The other two floors were NOT waived for the exempt period.
        entry = next(e for e in coverage["per_period"] if e["date"] == "2026-07-20")
        assert entry["markets_checked"] >= entry["markets_floor"]
        assert entry["semantics_verified"] >= entry["semantics_floor"]

    def test_an_exempt_period_whose_LADDER_is_missing_still_fails(self, tmp_path):
        """The exemption covers the outcome floor only, never the markets floor."""
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, _dense_daily(15, 25, skip=("2026-07-20",))),
            include_suspect=True,
        )
        summary = RG.reconcile_dates(
            ["2026-07-19", "2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher({("KXAAAGASD", "2026-07-19"): JUN30}),
            aaa_series=aaa,
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
        )
        coverage = RG.evaluate_coverage(summary)
        assert coverage["ok"] is False
        assert any(
            "2026-07-20" in f and "ladder was not read" in f
            for f in coverage["failures"]
        )

    def test_the_red_teams_degraded_series_is_NOT_exempt(self, tmp_path):
        """The discriminator that matters: a degraded harvest brackets nothing.

        The 1-of-4 CSV has a single row on 2026-06-30, which is nowhere near
        2026-05-31 / 2026-07-28 / 2026-07-29 -- so those periods stay in scope
        and the run stays red. This is what stops the exemption from reopening D3.
        """
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, {"2026-06-30": (JUN30_VALUE, "ok")}),
            include_suspect=True,
        )
        summary = RG.reconcile_dates(
            ["2026-05-31", "2026-06-30", "2026-07-28", "2026-07-29"],
            ["KXAAAGASM"],
            market_fetcher=_fake_fetcher(
                {
                    ("KXAAAGASM", "2026-05-31"): MAY31,
                    ("KXAAAGASM", "2026-06-30"): JUN30,
                    ("KXAAAGASM", "2026-07-28"): JUN30,
                    ("KXAAAGASM", "2026-07-29"): JUN30,
                }
            ),
            aaa_series=aaa,
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
        )
        coverage = RG.evaluate_coverage(summary)
        assert coverage["periods_verified_exempt"] == 0
        assert coverage["ok"] is False
        assert coverage["periods_failing"] == 3

    def test_a_harvester_that_STOPPED_is_not_exempt(self, tmp_path):
        """One-sided coverage must not qualify: nothing follows the gap."""
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, _dense_daily(15, 22)), include_suspect=True
        )
        assert RG.aaa_gap_is_bracketed("2026-07-23", aaa) is False
        assert RG.aaa_gap_is_bracketed("2026-07-20", aaa) is True

    @pytest.mark.parametrize(
        "hole,exempt_days",
        [
            # W = 3. L <= W: the whole hole is exempt.
            pytest.param((11, 11), {11}, id="L1-all-exempt"),
            pytest.param((11, 13), {11, 12, 13}, id="L3-all-exempt"),
            # W < L < 2W: only the inner days -- an edge day's far side is more
            # than W away.
            pytest.param((11, 14), {12, 13}, id="L4-inner-only"),
            pytest.param((11, 15), {13}, id="L5-inner-only"),
            # L >= 2W: no day of the hole is exempt.
            pytest.param((11, 16), set(), id="L6-none-exempt"),
            pytest.param((11, 17), set(), id="L7-none-exempt"),
            pytest.param((6, 19), set(), id="L14-none-exempt"),
        ],
    )
    def test_the_exemption_is_bounded_by_the_size_of_the_hole(
        self, tmp_path, hole, exempt_days
    ):
        """The bound is on gap SIZE, not "no AAA row is fine".

        A one-to-three-day hole is entirely explained; a week-long hole is
        entirely unexplained. This is what keeps the exemption from becoming a
        blanket waiver, so the whole table is pinned rather than one example.
        """
        from src.data.gas_settlement import load_aaa_series

        first, last = hole
        rows = _dense_daily(1, first - 1)
        rows.update(_dense_daily(last + 1, 25))
        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, rows, name=f"hole_{first}_{last}.csv"),
            include_suspect=True,
        )
        observed = {
            day
            for day in range(first, last + 1)
            if RG.aaa_gap_is_bracketed(f"2026-07-{day:02d}", aaa)
        }
        assert observed == exempt_days

    def test_a_run_where_EVERY_period_is_exempt_fails(self, tmp_path):
        """An exemption is not a pass."""
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(
                tmp_path, _dense_daily(15, 25, skip=("2026-07-20", "2026-07-21"))
            ),
            include_suspect=True,
        )
        summary = RG.reconcile_dates(
            ["2026-07-20", "2026-07-21"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher(
                {
                    ("KXAAAGASD", "2026-07-20"): JUN30,
                    ("KXAAAGASD", "2026-07-21"): JUN30,
                }
            ),
            aaa_series=aaa,
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
        )
        assert summary["totals"]["unexplained"] == 0  # <-- the trap
        coverage = RG.evaluate_coverage(summary)
        assert coverage["periods_verified_exempt"] == 2
        assert coverage["ok"] is False
        assert any("An exemption is not a pass" in f for f in coverage["failures"])

    def test_the_window_is_configurable_and_zero_exempts_nothing(self, tmp_path):
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, _dense_daily(15, 25, skip=("2026-07-20",))),
            include_suspect=True,
        )
        assert RG.aaa_gap_is_bracketed("2026-07-20", aaa, window_days=0) is False
        summary = RG.reconcile_dates(
            ["2026-07-19", "2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher(
                {
                    ("KXAAAGASD", "2026-07-19"): JUN30,
                    ("KXAAAGASD", "2026-07-20"): JUN30,
                }
            ),
            aaa_series=aaa,
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
            aaa_gap_window_days=0,
        )
        assert RG.evaluate_coverage(summary)["ok"] is False

    def test_the_report_states_the_exemption_count_and_its_rationale(self, tmp_path):
        from src.data.gas_settlement import load_aaa_series

        aaa = load_aaa_series(
            _write_aaa_rows(tmp_path, _dense_daily(15, 25, skip=("2026-07-20",))),
            include_suspect=True,
        )
        summary = RG.reconcile_dates(
            ["2026-07-19", "2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher(
                {
                    ("KXAAAGASD", "2026-07-19"): JUN30,
                    ("KXAAAGASD", "2026-07-20"): JUN30,
                }
            ),
            aaa_series=aaa,
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
        )
        text = RG.format_report(
            summary, threshold=0, coverage=RG.evaluate_coverage(summary)
        )
        assert "outcome floor EXEMPT: 1" in text
        assert "OK (outcome floor EXEMPT: AAA_GAP)" in text
        assert "2026-07-20 KXAAAGASD: outcome floor exempt (AAA_GAP)" in text


class TestSuspectRowsAreVerifiedButNeverCached:
    """A ``quality=suspect`` row is evidence for a reconcile and poison for a cache."""

    def _run(self, tmp_path, quality):
        from src.data.gas_settlement import load_aaa_series

        rows = _dense_daily(15, 25)
        rows["2026-07-20"] = (JUN30_VALUE, quality)
        cache = {"truth": {}, "markets": {}}
        summary = RG.reconcile_dates(
            ["2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher({("KXAAAGASD", "2026-07-20"): JUN30}),
            aaa_series=load_aaa_series(
                _write_aaa_rows(tmp_path, rows, name=f"aaa_{quality}.csv"),
                include_suspect=True,
            ),
            sim_outcomes={},
            cache=cache,
            as_of=AS_OF,
        )
        return summary, cache

    def test_a_suspect_row_is_still_compared_against_the_authority(self, tmp_path):
        """Excluding it would mean the authority was never consulted for the date."""
        summary, _ = self._run(tmp_path, "suspect")
        period = summary["periods"][0]
        assert period["aaa_value"] == pytest.approx(JUN30_VALUE)
        assert period["truth_quality"] == "suspect"
        assert period["verified"] == 33
        assert period["unexplained"] == 0
        assert RG.evaluate_coverage(summary)["ok"] is True

    def test_a_suspect_value_never_reaches_the_settlement_cache(self, tmp_path):
        """The runtime is cache-first and its CSV fallback excludes suspect rows.

        Caching one would settle a position on a value the runtime's own policy
        rejects -- a suspect row would take effect only via this job's output.
        """
        _, cache = self._run(tmp_path, "suspect")
        entry = cache["truth"]["KXAAAGASD|2026-07-20"]
        assert entry["value"] is None
        assert entry["pinned"]["value_high_inclusive"] == pytest.approx(3.85)

    def test_an_ok_value_does_reach_the_cache(self, tmp_path):
        """The control: only the suspect flag withholds the write."""
        _, cache = self._run(tmp_path, "ok")
        assert cache["truth"]["KXAAAGASD|2026-07-20"]["value"] == pytest.approx(
            JUN30_VALUE
        )

    def test_a_suspect_value_cannot_null_a_prior_good_cache_entry(self, tmp_path):
        """D10 and the suspect policy must compose, not collide."""
        from src.data.gas_settlement import load_aaa_series, record_truth

        cache = {"truth": {}, "markets": {}}
        record_truth(cache, "KXAAAGASD", "2026-07-20", 4.321, source="aaa_live")
        rows = _dense_daily(15, 25)
        rows["2026-07-20"] = (JUN30_VALUE, "suspect")
        RG.reconcile_dates(
            ["2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher({("KXAAAGASD", "2026-07-20"): JUN30}),
            aaa_series=load_aaa_series(
                _write_aaa_rows(tmp_path, rows), include_suspect=True
            ),
            sim_outcomes={},
            cache=cache,
            as_of=AS_OF,
        )
        assert cache["truth"]["KXAAAGASD|2026-07-20"]["value"] == pytest.approx(4.321)

    def test_a_suspect_row_that_DISAGREES_with_kalshi_still_breaches(self, tmp_path):
        """Including suspect rows adds verification, not leniency."""
        summary, _ = self._run(tmp_path, "ok")  # baseline: agrees
        assert summary["totals"]["unexplained"] == 0

        from src.data.gas_settlement import load_aaa_series

        rows = _dense_daily(15, 25)
        rows["2026-07-20"] = (4.500, "suspect")
        bad = RG.reconcile_dates(
            ["2026-07-20"],
            ["KXAAAGASD"],
            market_fetcher=_fake_fetcher({("KXAAAGASD", "2026-07-20"): JUN30}),
            aaa_series=load_aaa_series(
                _write_aaa_rows(tmp_path, rows, name="aaa_bad.csv"),
                include_suspect=True,
            ),
            sim_outcomes={},
            cache={"truth": {}, "markets": {}},
            as_of=AS_OF,
        )
        assert bad["totals"]["by_category"]["TRUTH_MISMATCH"] == 33

    def test_the_report_marks_a_suspect_source_with_a_question_mark(self, tmp_path):
        summary, _ = self._run(tmp_path, "suspect")
        summary["generated_at"] = "2026-07-30T00:00:00Z"
        text = RG.format_report(
            summary, threshold=0, coverage=RG.evaluate_coverage(summary)
        )
        assert "test_fixture?" in text
        assert "AAA row quality=suspect" in text


def test_the_cli_loads_suspect_rows_so_the_authority_is_consulted(
    tmp_path, monkeypatch
):
    """End to end: a suspect-only date must not read as NO_AAA_VALUE."""
    monkeypatch.setattr(
        RG, "fetch_event_markets", _fake_fetcher({("KXAAAGASM", "2026-06-30"): JUN30})
    )
    monkeypatch.setattr(RG, "load_sim_outcomes", lambda: {})
    monkeypatch.setattr(RG, "save_settlement_cache", lambda *a, **k: None)
    aaa_path = _write_aaa_rows(tmp_path, {"2026-06-30": (JUN30_VALUE, "suspect")})
    code = RG.main(
        [
            "--date",
            "2026-07-15",
            "--series",
            "KXAAAGASM",
            "--aaa-csv",
            aaa_path,
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-discord",
            "--quiet",
        ]
    )
    assert code == 0
    text = (tmp_path / "reports" / "reconcile_gas_2026-06-30.txt").read_text(
        encoding="utf-8"
    )
    assert "Outcomes verified  : 33" in text
    assert "test_fixture?" in text


# ======================================================================
# D10: the recorder can only ever IMPROVE the settlement cache
# ======================================================================
#
# The 2026-07-30 red-team finding: ``record_truth`` fired whenever
# ``value is not None or pinned``, so a degraded run wrote ``value: null`` over
# 19 previously-correct entries. Fail-safe only by accident --
# ``_value_from_cache`` returns None on a null -- but the runtime resolver is
# CACHE-FIRST, so a bad reconcile run demoted the settlement path's primary
# source to the CSV fallback.


def _cache_with(entries):
    from src.data.gas_settlement import record_truth

    cache = {"truth": {}, "markets": {}}
    for (series, date), value in entries.items():
        record_truth(
            cache,
            series,
            date,
            value,
            source="aaa_wayback",
            source_url="https://example.invalid/good",
        )
    return cache


def test_record_truth_never_overwrites_a_good_value_with_a_null():
    from src.data.gas_settlement import record_truth

    cache = _cache_with({("KXAAAGASM", "2026-06-30"): JUN30_VALUE})
    before = dict(cache["truth"]["KXAAAGASM|2026-06-30"])

    record_truth(cache, "KXAAAGASM", "2026-06-30", None, source="kalshi_settlement")

    after = cache["truth"]["KXAAAGASM|2026-06-30"]
    assert after["value"] == pytest.approx(JUN30_VALUE)
    # The provenance of the run that ESTABLISHED the value is kept, not restamped.
    assert after["source"] == before["source"]
    assert after["source_url"] == before["source_url"]
    assert after["recorded_at"] == before["recorded_at"]


def test_a_null_write_still_refreshes_the_kalshi_derived_pinned_interval():
    """Improving is allowed; the pin comes from Kalshi alone, not from AAA."""
    from src.data.gas_settlement import pin_truth_from_ladder, record_truth

    cache = _cache_with({("KXAAAGASM", "2026-06-30"): JUN30_VALUE})
    record_truth(
        cache,
        "KXAAAGASM",
        "2026-06-30",
        None,
        source="kalshi_settlement",
        pinned=pin_truth_from_ladder(JUN30),
    )
    entry = cache["truth"]["KXAAAGASM|2026-06-30"]
    assert entry["value"] == pytest.approx(JUN30_VALUE)
    assert entry["pinned"]["value_high_inclusive"] == pytest.approx(3.85)


def test_a_retained_value_is_marked_so_a_stale_retention_is_visible():
    """The honest cost of improve-only: a WITHDRAWN row cannot be purged here.

    A withdrawn row and a degraded harvest both look like "no row for this
    date", and retaining is the safe side of that ambiguity. The marker makes a
    retained value auditable, and a fresh good write clears it.
    """
    from src.data.gas_settlement import record_truth

    cache = _cache_with({("KXAAAGASM", "2026-06-30"): JUN30_VALUE})
    assert "value_retained" not in cache["truth"]["KXAAAGASM|2026-06-30"]

    record_truth(cache, "KXAAAGASM", "2026-06-30", None, source="kalshi_settlement")
    assert cache["truth"]["KXAAAGASM|2026-06-30"]["value_retained"] is True

    record_truth(cache, "KXAAAGASM", "2026-06-30", JUN30_VALUE, source="aaa_live")
    assert "value_retained" not in cache["truth"]["KXAAAGASM|2026-06-30"]


def test_a_corrected_value_still_propagates():
    """Only nulls are refused. "First write wins" would shadow a correction."""
    from src.data.gas_settlement import record_truth

    cache = _cache_with({("KXAAAGASM", "2026-06-30"): 4.500})
    record_truth(cache, "KXAAAGASM", "2026-06-30", JUN30_VALUE, source="aaa_live")
    entry = cache["truth"]["KXAAAGASM|2026-06-30"]
    assert entry["value"] == pytest.approx(JUN30_VALUE)
    assert entry["source"] == "aaa_live"


def test_a_null_is_still_written_when_there_is_nothing_to_keep():
    """An absence must stay visible; the pinned interval is still worth caching."""
    from src.data.gas_settlement import record_truth

    cache = {"truth": {}, "markets": {}}
    record_truth(cache, "KXAAAGASM", "2026-06-30", None, source="kalshi_settlement")
    assert cache["truth"]["KXAAAGASM|2026-06-30"]["value"] is None


@pytest.mark.parametrize(
    "prior",
    [None, "", "not-a-number", float("nan")],
    ids=["none", "empty", "text", "nan"],
)
def test_an_unusable_prior_value_is_not_treated_as_good(prior):
    """A null must not be blocked by junk that only looks like a number."""
    from src.data.gas_settlement import record_truth

    cache = {
        "truth": {"KXAAAGASM|2026-06-30": {"value": prior, "source": "junk"}},
        "markets": {},
    }
    record_truth(cache, "KXAAAGASM", "2026-06-30", None, source="kalshi_settlement")
    entry = cache["truth"]["KXAAAGASM|2026-06-30"]
    assert entry["value"] is None
    assert entry["source"] == "kalshi_settlement"


def test_a_degraded_reconcile_run_leaves_prior_good_entries_intact(tmp_path):
    """The red-team's scenario at the level it actually happened.

    A healthy run establishes two month-ends; a degraded run that can only
    resolve one of them must not null the other.
    """
    from src.data.gas_settlement import load_aaa_series

    fetcher = _fake_fetcher(
        {("KXAAAGASM", "2026-06-30"): JUN30, ("KXAAAGASM", "2026-05-31"): MAY31}
    )
    healthy = RG.reconcile_dates(
        ["2026-05-31", "2026-06-30"],
        ["KXAAAGASM"],
        market_fetcher=fetcher,
        aaa_series=load_aaa_series(
            _write_aaa(tmp_path, {"2026-06-30": JUN30_VALUE, "2026-05-31": MAY31_VALUE})
        ),
        sim_outcomes={},
        cache={"truth": {}, "markets": {}},
        as_of=AS_OF,
    )
    cache = healthy["cache"]
    assert {k: v["value"] for k, v in cache["truth"].items()} == {
        "KXAAAGASM|2026-05-31": pytest.approx(MAY31_VALUE),
        "KXAAAGASM|2026-06-30": pytest.approx(JUN30_VALUE),
    }

    degraded = RG.reconcile_dates(
        ["2026-05-31", "2026-06-30"],
        ["KXAAAGASM"],
        market_fetcher=fetcher,
        aaa_series=load_aaa_series(
            _write_aaa(tmp_path, {"2026-06-30": JUN30_VALUE}, name="degraded.csv")
        ),
        sim_outcomes={},
        cache=cache,
        as_of=AS_OF,
    )
    values = {k: v["value"] for k, v in degraded["cache"]["truth"].items()}
    assert values["KXAAAGASM|2026-05-31"] == pytest.approx(MAY31_VALUE), (
        "the degraded run nulled a previously-correct cache entry; the runtime "
        "resolver is cache-first, so that demotes the settlement path's "
        "primary source"
    )
    assert values["KXAAAGASM|2026-06-30"] == pytest.approx(JUN30_VALUE)
    # ...and the run is still honest about having verified nothing for 05-31.
    assert RG.evaluate_coverage(degraded)["ok"] is False


def test_the_runtime_resolver_keeps_reading_the_retained_value(tmp_path):
    """The reason D10 matters: the resolver is cache-first.

    With the value retained the resolver answers from the cache. This is the
    property a null destroyed -- it fell through to the CSV, which is the
    fallback, not the primary source.
    """
    import json as _json

    from src.data.gas_settlement import (
        record_truth,
        reset_caches,
        resolve_settlement_value,
    )

    cache = _cache_with({("KXAAAGASM", "2026-06-30"): JUN30_VALUE})
    record_truth(cache, "KXAAAGASM", "2026-06-30", None, source="kalshi_settlement")
    cache_path = tmp_path / "settlement_cache.json"
    cache_path.write_text(_json.dumps(cache), encoding="utf-8")

    reset_caches()
    try:
        value = resolve_settlement_value(
            "KXAAAGASM-26JUN30-3.89", cache_path=str(cache_path), aaa_series={}
        )
    finally:
        reset_caches()
    assert value == pytest.approx(JUN30_VALUE)


# ======================================================================
# --harvest-truth
# ======================================================================
def test_harvest_truth_writes_the_pinned_series_and_manifest(tmp_path):
    def fetcher(series_ticker):
        if series_ticker == "KXAAAGASM":
            return MONTHLY_MARKETS
        return []

    paths = RG.harvest_pinned_truth(
        ["KXAAAGASM", "KXAAAGASD", "KXAAAGASW"],
        fetcher=fetcher,
        fixture_dir=str(tmp_path),
    )
    rows = RG.load_pinned_truth(paths["csv"])
    assert len(rows) == 2
    assert {r["settlement_date"] for r in rows} == {"2026-05-31", "2026-06-30"}
    manifest = paths["manifest_blob"]
    assert manifest["rows"] == 2
    assert manifest["month_end_dates"] == ["2026-05-31", "2026-06-30"]
    assert manifest["by_period_kind"]["monthly"]["max_interval_width"] == pytest.approx(
        0.01, abs=1e-9
    )
    assert "kalshi" in manifest["authority"].lower()
    assert "No AAA-sourced series was consulted" in manifest["authority"]
    # The hash must describe the bytes actually written.
    import hashlib

    with open(paths["csv"], "rb") as handle:
        raw = handle.read().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == manifest["content_sha256"]


def test_harvest_truth_counts_the_boundary_ties_it_saw(tmp_path):
    def fetcher(series_ticker):
        return TIE_MARKETS if series_ticker == "KXAAAGASD" else []

    paths = RG.harvest_pinned_truth(
        ["KXAAAGASD"], fetcher=fetcher, fixture_dir=str(tmp_path)
    )
    manifest = paths["manifest_blob"]
    # The stub hands the whole tie fixture to one series, so the count is the
    # fixture size: what is being asserted is that the harvest counts ties at
    # all, and that a one-sided ladder does not fake an interval width.
    assert manifest["boundary_tie_markets"] == len(TIE_MARKETS) == 15
    assert manifest["by_period_kind"]["daily"]["max_interval_width"] is None
    assert manifest["by_period_kind"]["daily"]["one_sided"] == manifest["rows"]


def test_harvest_truth_records_that_it_pinned_nothing_rather_than_claiming_success(
    tmp_path,
):
    paths = RG.harvest_pinned_truth(
        ["KXAAAGASM"], fetcher=lambda s: [], fixture_dir=str(tmp_path)
    )
    manifest = paths["manifest_blob"]
    assert manifest["rows"] == 0
    assert manifest["month_end_dates"] == []
    assert "n/a" in manifest["retention_note"]


def test_load_pinned_truth_rejects_a_schema_break(tmp_path):
    from src.data.gas_settlement import GasTruthError

    bad = tmp_path / "kalshi_pinned_truth.csv"
    bad.write_text("settlement_date,series\n2026-06-30,KXAAAGASM\n", encoding="utf-8")
    with pytest.raises(GasTruthError):
        RG.load_pinned_truth(str(bad))
