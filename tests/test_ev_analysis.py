"""Tests for the Phase 2 go/no-go EV machinery (`src/backtest/ev_analysis.py`).

Run this file alone -- the full suite is prohibited on this machine::

    $env:PYTHONPATH = "."
    python -m pytest tests/test_ev_analysis.py -v

Everything here is offline. The handful of tests that read the committed
calibration / ladder artifacts skip (rather than fail) when those are absent, so
a clean checkout still runs green; the arithmetic tests never skip.

The load-bearing test is
:func:`test_hand_worked_ev_matches_a_value_computed_by_hand_in_this_docstring`:
its expected number is derived in the docstring from a published normal table
and is **not** produced by calling the code under test. Phase 2 exit criterion 5
requires a red-team to recompute a band's EV from raw inputs and match within
rounding; that test is that requirement, executable.
"""

from __future__ import annotations

import json
import math
import os

import pandas as pd
import pytest

from src.backtest import ev_analysis as ev
from src.core.bracket_payoff import BracketSpec
from src.core.fee_calculator import (
    EXIT_SETTLEMENT,
    FEE_TYPE_STANDARD,
    FEE_TYPE_WITH_MAKER_FEES,
    ev_after_fees,
    taker_fee,
)

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "ev_analysis"
)


# ==========================================================================
# The hand-worked EV -- EC-5's "recompute one band's EV and match within rounding"
# ==========================================================================
def test_hand_worked_ev_matches_a_value_computed_by_hand_in_this_docstring():
    r"""Buy NO on a 2-degF bracket, held to settlement. Worked by hand below.

    **Inputs** (all chosen so every intermediate lands on a published table row):

    ==================================  ===============
    calibrated median  mu                85.0 degF
    calibrated sigma                      2.0 degF
    bracket (``between``)                 floor 83, cap 84  -> pays YES for a
                                          whole-degree high of 83 or 84
    market YES bid                        0.38
    order size C                          10 contracts
    adverse-fill allowance (EC-5)         $0.01
    ==================================  ===============

    **Step 1 -- P(YES).** The settled high is a whole degree, so the bracket's
    probability is an interval mass with a continuity correction::

        z_hi = (84 + 0.5 - 85) / 2 = -0.25
        z_lo = (83 - 0.5 - 85) / 2 = -1.25

    From a published 5-decimal standard normal table:

        Phi(-0.25) = 0.40129
        Phi(-1.25) = 0.10565

        P(YES) = 0.40129 - 0.10565 = 0.29564
        P(NO)  = 1 - 0.29564       = 0.70436

    **Step 2 -- the price this shape must hit.** Buying NO as a taker lifts the
    NO offer, which is the exact complement of the YES bid::

        no_ask = 1 - yes_bid = 1 - 0.38 = 0.62

    **Step 3 -- EC-5's 1c adverse-fill allowance**::

        price_paid = 0.62 + 0.01 = 0.63

    **Step 4 -- fee.** Taker, and Kalshi rounds up to the cent on the *order
    total*; settlement is free and PRD FR-1.5 holds to expiry, so ONE leg::

        raw       = 0.07 * 10 * 0.63 * (1 - 0.63)
                  = 0.07 * 10 * 0.63 * 0.37
                  = 0.16317 dollars
        order fee = ceil to the cent          = $0.17
        per contract = 0.17 / 10              = $0.017

    **Step 5 -- EV per contract**::

        EV = P(NO) - price_paid - fee_per_contract
           = 0.70436 - 0.63 - 0.017
           = 0.05736 dollars   =   +5.736 cents per contract

    **The maker variant** pays no fee at all on ``KXHIGH*``::

        EV = 0.70436 - 0.63 - 0.000 = 0.07436   =   +7.436 cents

    **The C = 1 taker variant** shows how brutally the whole-cent rounding
    bites a single contract::

        raw = 0.07 * 1 * 0.63 * 0.37 = 0.016317  ->  ceil = $0.02
        EV  = 0.70436 - 0.63 - 0.02  = 0.05436   =   +5.436 cents
    """

    # --- Step 1, recomputed here from math.erfc, NOT from the engine ---
    def phi(z: float) -> float:
        return 0.5 * math.erfc(-z / math.sqrt(2.0))

    p_yes = phi(-0.25) - phi(-1.25)
    assert p_yes == pytest.approx(0.29564, abs=5e-6)  # the table value
    p_no = 1.0 - p_yes
    assert p_no == pytest.approx(0.70436, abs=5e-6)

    # --- Step 2: the NO offer is the exact complement of the YES bid ---
    quote = ev.quote_for_shape(
        yes_bid=0.38, yes_ask=0.41, direction=ev.DIRECTION_NO, mode=ev.MODE_TAKER
    )
    assert quote == pytest.approx(0.62)

    # --- Steps 3-5, through the code under test ---
    taker = ev.ev_for_trade(p_no, quote, contracts=10, is_maker=False)
    assert taker.price_paid == pytest.approx(0.63)
    assert taker.fee_total == pytest.approx(0.17)
    assert taker.fee_per_contract == pytest.approx(0.017)
    assert taker.ev_per_contract == pytest.approx(0.05736, abs=1e-5)

    maker = ev.ev_for_trade(p_no, quote, contracts=10, is_maker=True)
    assert maker.fee_total == 0.0
    assert maker.ev_per_contract == pytest.approx(0.07436, abs=1e-5)

    one = ev.ev_for_trade(p_no, quote, contracts=1, is_maker=False)
    assert one.fee_per_contract == pytest.approx(0.02)
    assert one.ev_per_contract == pytest.approx(0.05436, abs=1e-5)


def test_the_probability_engine_reproduces_the_hand_worked_p_yes():
    """The same bracket through workstream D's engine must equal the table value.

    If this drifts from 0.29564 the hand-worked test above is no longer a check
    on the pipeline, only on this module's arithmetic.
    """
    from src.calibration.probability_engine import (
        distribution_over_ladder,
        NormalComponent,
    )

    specs = [
        BracketSpec("LOW", "less", cap_strike=83.0),
        BracketSpec("MID", "between", floor_strike=83.0, cap_strike=84.0),
        BracketSpec("HIGH", "greater", floor_strike=84.0),
    ]
    probs, _pmf, _tail, uncovered, partition = distribution_over_ladder(
        specs, (NormalComponent("calibrated", 1.0, 85.0, 2.0),)
    )
    assert partition.complete
    assert abs(uncovered) < 1e-12
    assert probs["MID"] == pytest.approx(0.29564, abs=5e-6)


def test_ev_agrees_with_the_reserved_fee_module_at_one_contract():
    """At C=1 there is nothing to divide, so we must equal ``ev_after_fees``.

    This module only adds a per-contract division to the corrected fee model;
    the division must not be a place where the two silently diverge.
    """
    for price in (0.01, 0.05, 0.10, 0.25, 0.50, 0.63, 0.90, 0.98):
        for is_maker in (True, False):
            for fee_type in (FEE_TYPE_STANDARD, FEE_TYPE_WITH_MAKER_FEES):
                assert ev.ev_matches_fee_calculator(
                    0.5, price, is_maker=is_maker, series_fee_type=fee_type
                )
    # ... and explicitly, once, in the open.
    got = ev.ev_for_trade(0.70436, 0.62, contracts=1, is_maker=False)
    want = ev_after_fees(0.70436, 0.63, 1, False, FEE_TYPE_STANDARD, EXIT_SETTLEMENT)
    assert got.ev_per_contract == pytest.approx(want, abs=1e-12)


def test_settlement_exit_charges_one_leg_not_a_round_trip():
    """PRD FR-1.5 holds weather to expiry and Kalshi charges no settlement fee."""
    round_trip = ev_after_fees(0.70436, 0.63, 1, False, FEE_TYPE_STANDARD, "trade_out")
    settlement = ev_after_fees(
        0.70436, 0.63, 1, False, FEE_TYPE_STANDARD, EXIT_SETTLEMENT
    )
    assert settlement - round_trip == pytest.approx(taker_fee(0.63, 1), abs=1e-12)
    assert ev.ev_for_trade(
        0.70436, 0.62, contracts=1, is_maker=False
    ).ev_per_contract == pytest.approx(settlement, abs=1e-12)


def test_fee_per_contract_falls_with_order_size_because_kalshi_rounds_the_total():
    """A C=1 model overstates far-bracket taker cost roughly threefold."""
    at_1 = ev.fee_per_contract(0.05, 1, is_maker=False)
    at_20 = ev.fee_per_contract(0.05, 20, is_maker=False)
    assert at_1 == pytest.approx(0.01)  # ceil(0.07*1*0.05*0.95=0.003325) -> $0.01
    assert at_20 == pytest.approx(0.07 / 20)  # ceil(0.0665) -> $0.07, /20
    assert at_20 < at_1 / 2


def test_maker_fee_is_zero_on_weather_and_nonzero_on_a_maker_fee_series():
    assert ev.fee_per_contract(0.10, 1, is_maker=True) == 0.0
    assert (
        ev.fee_per_contract(
            0.10, 1, is_maker=True, series_fee_type=FEE_TYPE_WITH_MAKER_FEES
        )
        > 0.0
    )


# ==========================================================================
# Availability -- never quote an EV for a fill the tape says was unavailable
# ==========================================================================
@pytest.mark.parametrize(
    "yes_bid,yes_ask,direction,mode,expected",
    [
        # a real two-sided book
        (0.30, 0.32, ev.DIRECTION_YES, ev.MODE_TAKER, 0.32),
        (0.30, 0.32, ev.DIRECTION_YES, ev.MODE_MAKER, 0.30),
        (0.30, 0.32, ev.DIRECTION_NO, ev.MODE_TAKER, 0.70),
        (0.30, 0.32, ev.DIRECTION_NO, ev.MODE_MAKER, 0.68),
        # empty bid sentinel: no NO offer exists, and no YES bid to join
        (0.00, 0.01, ev.DIRECTION_NO, ev.MODE_TAKER, None),
        (0.00, 0.01, ev.DIRECTION_YES, ev.MODE_MAKER, None),
        (0.00, 0.01, ev.DIRECTION_YES, ev.MODE_TAKER, 0.01),
        # empty ask sentinel: no YES offer exists, and no NO bid to join
        (0.99, 1.00, ev.DIRECTION_YES, ev.MODE_TAKER, None),
        (0.99, 1.00, ev.DIRECTION_NO, ev.MODE_MAKER, None),
        (0.99, 1.00, ev.DIRECTION_NO, ev.MODE_TAKER, 0.01),
    ],
)
def test_empty_book_sentinels_are_absent_never_a_price(
    yes_bid, yes_ask, direction, mode, expected
):
    """``yes_bid == 0`` / ``yes_ask == 1`` mean 'no quote', not 'free' / 'certain'.

    This is the single most dangerous coercion available in this dataset: a
    far bracket with no bid would otherwise book as a NO purchase at $0.00.
    """
    got = ev.quote_for_shape(yes_bid, yes_ask, direction, mode)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_adverse_fill_off_the_orderable_grid_is_unexecutable_not_a_loss():
    """0.99 + 1c = 1.00, which Kalshi will not accept. Refuse, do not book it."""
    assert ev.adverse_fill_price(0.98) == pytest.approx(0.99)
    assert ev.adverse_fill_price(0.99) is None
    assert ev.ev_for_trade(0.999, 0.99, contracts=1, is_maker=False) is None


def test_opportunity_frame_never_prices_a_missing_side():
    """Rows whose required side is absent carry no EV and no realized PnL.

    Exercises the real :func:`build_opportunity_frame`, not a test-local copy,
    so a regression that leaves an EV on an unfilled row is caught here.
    """
    opp = _real_opportunity_frame()
    missing = opp[~opp["quote_present"]]
    assert len(missing) > 0, "fixture must contain a one-sided book"
    assert missing["ev_per_contract"].isna().all()
    assert missing["realized_per_contract"].isna().all()
    assert missing["executable"].eq(False).all()

    unfilled = opp[~opp["executable"]]
    assert len(unfilled) > len(missing), "fixture must contain an unfilled maker order"
    assert unfilled["ev_per_contract"].isna().all()
    assert unfilled["realized_per_contract"].isna().all()

    # ...and the rows that DID fill must carry a finite EV, or the assertions
    # above would pass on an all-NaN column.
    filled = opp[opp["executable"]]
    assert len(filled) > 0
    assert filled["ev_per_contract"].notna().all()
    assert filled["realized_per_contract"].notna().all()


def test_opportunity_frame_post_close_snapshots_are_dropped():
    """A snapshot after the market closed is not a trading opportunity."""
    opp = _real_opportunity_frame()
    assert (opp["minutes_to_close"] > 0).all()


def test_aggregate_bands_reports_the_fill_rate_and_excludes_unfilled_rows():
    opp = _real_opportunity_frame()
    bands = ev.aggregate_bands(opp)
    for _, row in bands.iterrows():
        assert row["n_executable"] <= row["n_candidates"]
        assert 0.0 <= row["fill_rate"] <= 1.0
        if row["n_executable"] == 0:
            assert math.isnan(row["ev_per_contract"])
    # EV statistics must equal the mean over executable rows only.
    filled = bands[
        (bands["direction"] == ev.DIRECTION_NO)
        & (bands["mode"] == ev.MODE_TAKER)
        & (bands["n_executable"] > 0)
    ]
    assert not filled.empty
    cell = filled.iloc[0]
    sub = opp[
        (opp["direction"] == ev.DIRECTION_NO)
        & (opp["mode"] == ev.MODE_TAKER)
        & (opp["band"] == cell["band"])
        & opp["executable"]
    ]
    assert cell["ev_per_contract"] == pytest.approx(sub["ev_per_contract"].mean())


# ==========================================================================
# Maker fill model
# ==========================================================================
def test_maker_fill_requires_a_later_quote_traversal():
    """A resting bid fills only if the ask later comes down to it -- and the
    traversal must be in the *future*, never in the same or an earlier snapshot.
    """
    df = pd.DataFrame(
        {
            "market_ticker": ["M"] * 4,
            "ts_utc": pd.to_datetime(
                [
                    "2026-06-01T00:00Z",
                    "2026-06-01T01:00Z",
                    "2026-06-01T02:00Z",
                    "2026-06-01T03:00Z",
                ],
                utc=True,
            ),
            "yes_bid": [0.30, 0.20, 0.10, 0.05],
            "yes_ask": [0.32, 0.22, 0.12, 0.07],
        }
    )
    out = ev.add_maker_fill_flags(df).sort_values("ts_utc")
    # t0: bid 0.30, later asks 0.22/0.12/0.07 all <= 0.30 -> fills
    assert bool(out.iloc[0]["maker_yes_fill"])
    # t3 is last: nothing later -> cannot fill
    assert not bool(out.iloc[3]["maker_yes_fill"])
    # NO side needs the market to rally into the offer; this tape only falls.
    assert not out["maker_no_fill"].any()

    rising = df.copy()
    rising["yes_bid"] = [0.05, 0.10, 0.20, 0.30]
    rising["yes_ask"] = [0.07, 0.12, 0.22, 0.32]
    up = ev.add_maker_fill_flags(rising).sort_values("ts_utc")
    assert bool(up.iloc[0]["maker_no_fill"])  # offer 0.07, later bid 0.10 >= 0.07
    assert not bool(up.iloc[3]["maker_no_fill"])
    assert not up["maker_yes_fill"].any()


def test_maker_fill_traversal_must_be_strictly_later_not_the_same_snapshot():
    """A resting order cannot be filled by the quote that defined it.

    In the recorded tape ``yes_ask`` is strictly above ``yes_bid`` on all 38,200
    two-sided rows, so a same-snapshot self-fill never shows up in production
    data and a defect here would be invisible on the real archive. A locked book
    (bid == ask) makes the requirement load-bearing, which is the only way to
    pin it.
    """
    locked = pd.DataFrame(
        {
            "market_ticker": ["M", "M"],
            "ts_utc": pd.to_datetime(
                ["2026-06-01T00:00Z", "2026-06-01T01:00Z"], utc=True
            ),
            "yes_bid": [0.40, 0.05],
            "yes_ask": [0.40, 0.90],
        }
    )
    out = ev.add_maker_fill_flags(locked).sort_values("ts_utc")
    # t0's own ask equals its own bid; only a LATER ask <= 0.40 may fill it, and
    # the only later ask is 0.90.
    assert not bool(out.iloc[0]["maker_yes_fill"])
    # symmetric on the NO side: t0's own bid equals its own offer.
    assert not bool(out.iloc[0]["maker_no_fill"])


def test_an_empty_book_at_t_plus_1_does_not_cancel_a_traversal_at_t_plus_3():
    """The forward extreme must skip empty snapshots, not be poisoned by them.

    The docstring promises "*some* later snapshot before close"; the original
    implementation accumulated the forward extreme with pandas ``cummin`` /
    ``cummax`` over a column carrying ``NaN`` at every empty-book snapshot, and
    those emit ``NaN`` at each ``NaN`` *input* position. The forward extreme at
    ``t`` was therefore blank whenever the single **immediately next** snapshot
    had no quote, even when a later one crossed the limit -- silently
    under-counting maker fills (1,110 of 27,262 fillable ``buy_no`` snapshots,
    +4.2%, on the recorded tape).

    Both tapes below place the empty book at ``t+1`` and the traversal at
    ``t+3``. Against the old behaviour every assertion in this test fails.
    """
    ts = pd.to_datetime(
        [
            "2026-06-01T00:00Z",
            "2026-06-01T01:00Z",
            "2026-06-01T02:00Z",
            "2026-06-01T03:00Z",
        ],
        utc=True,
    )
    # NO side: resting offer at 0.40; t+1 has no bid (sentinel 0.0), t+3 rallies
    # through it.
    no_side = pd.DataFrame(
        {
            "market_ticker": ["M"] * 4,
            "ts_utc": ts,
            "yes_bid": [0.38, 0.00, 0.00, 0.45],
            "yes_ask": [0.40, 1.00, 1.00, 0.47],
        }
    )
    out = ev.add_maker_fill_flags(no_side).sort_values("ts_utc")
    assert out.iloc[0]["fwd_max_bid"] == pytest.approx(0.45)
    assert bool(out.iloc[0]["maker_no_fill"])

    # YES side: resting bid at 0.38; t+1 has no ask, t+3 offers below it.
    yes_side = pd.DataFrame(
        {
            "market_ticker": ["M"] * 4,
            "ts_utc": ts,
            "yes_bid": [0.38, 0.00, 0.00, 0.30],
            "yes_ask": [0.40, 1.00, 1.00, 0.32],
        }
    )
    out = ev.add_maker_fill_flags(yes_side).sort_values("ts_utc")
    assert out.iloc[0]["fwd_min_ask"] == pytest.approx(0.32)
    assert bool(out.iloc[0]["maker_yes_fill"])

    # And "no later quote at all" still reads as NaN, not as a neutral extreme.
    assert math.isnan(float(out.iloc[3]["fwd_min_ask"]))
    assert math.isnan(float(out.iloc[3]["fwd_max_bid"]))
    assert not bool(out.iloc[3]["maker_yes_fill"])
    assert not bool(out.iloc[3]["maker_no_fill"])


def test_forward_extreme_equals_an_independent_traversal_on_a_gappy_tape():
    """Cross-check the vectorized extreme against a plain loop.

    The loop is the definition: for each row, scan every strictly later row of
    the same market and take the extreme of the non-sentinel quotes. No pandas
    cumulative is involved, so the two implementations share no failure mode.
    """
    rng = __import__("random").Random(20260726)
    n = 60
    bids, asks = [], []
    for _ in range(n):
        if rng.random() < 0.35:  # empty book
            bids.append(0.0)
            asks.append(1.0)
        else:
            b = round(rng.uniform(0.02, 0.90), 2)
            bids.append(b)
            asks.append(round(min(b + rng.choice([0.01, 0.02, 0.05]), 0.99), 2))
    df = pd.DataFrame(
        {
            "market_ticker": ["M"] * n,
            "ts_utc": pd.date_range("2026-06-01", periods=n, freq="h", tz="UTC"),
            "yes_bid": bids,
            "yes_ask": asks,
        }
    )
    out = ev.add_maker_fill_flags(df).sort_values("ts_utc").reset_index(drop=True)
    for i in range(n):
        later_asks = [asks[j] for j in range(i + 1, n) if asks[j] < 1.0]
        later_bids = [bids[j] for j in range(i + 1, n) if bids[j] > 0.0]
        got_min = float(out.iloc[i]["fwd_min_ask"])
        got_max = float(out.iloc[i]["fwd_max_bid"])
        if later_asks:
            assert got_min == pytest.approx(min(later_asks)), i
        else:
            assert math.isnan(got_min), i
        if later_bids:
            assert got_max == pytest.approx(max(later_bids)), i
        else:
            assert math.isnan(got_max), i
        assert bool(out.iloc[i]["maker_yes_fill"]) == bool(
            bids[i] > 0.0 and later_asks and min(later_asks) <= bids[i] + 1e-9
        ), i
        assert bool(out.iloc[i]["maker_no_fill"]) == bool(
            asks[i] < 1.0 and later_bids and max(later_bids) >= asks[i] - 1e-9
        ), i


def test_maker_fill_does_not_leak_across_markets():
    """One market's traversal must never fill another market's resting order."""
    df = pd.DataFrame(
        {
            "market_ticker": ["A", "A", "B", "B"],
            "ts_utc": pd.to_datetime(
                [
                    "2026-06-01T00:00Z",
                    "2026-06-01T01:00Z",
                    "2026-06-01T00:00Z",
                    "2026-06-01T01:00Z",
                ],
                utc=True,
            ),
            "yes_bid": [0.50, 0.50, 0.10, 0.10],
            "yes_ask": [0.52, 0.52, 0.12, 0.12],
        }
    )
    out = ev.add_maker_fill_flags(df)
    # B's cheap ask (0.12) must not fill A's 0.50 bid.
    assert not out[out["market_ticker"] == "A"]["maker_yes_fill"].any()


# ==========================================================================
# Bracket geometry
# ==========================================================================
def test_open_ended_brackets_get_a_same_width_virtual_midpoint():
    """T90 (>=91) -> 91.5 and T83 (<=82) -> 81.5 on a 2 degF ladder.

    Pushing the tails further out would inflate their band distance and make
    far-bracket EV look better than it is, so the convention is deliberately
    the least generous one available.
    """
    between = BracketSpec("B85.5", "between", floor_strike=85.0, cap_strike=86.0)
    greater = BracketSpec("T90", "greater", floor_strike=90.0)
    less = BracketSpec("T83", "less", cap_strike=83.0)
    width = ev.ladder_core_width_f([between])
    assert width == 2.0
    assert ev.bracket_midpoint_f(between, width) == pytest.approx(85.5)
    assert ev.bracket_midpoint_f(greater, width) == pytest.approx(91.5)
    assert ev.bracket_midpoint_f(less, width) == pytest.approx(81.5)


def test_edge_distance_is_zero_inside_the_bracket_and_positive_outside():
    spec = BracketSpec("B85.5", "between", floor_strike=85.0, cap_strike=86.0)
    assert ev.bracket_edge_distance_f(spec, 85.4) == 0.0
    assert ev.bracket_edge_distance_f(spec, 88.0) == pytest.approx(2.0)
    assert ev.bracket_edge_distance_f(spec, 82.0) == pytest.approx(3.0)


@pytest.mark.parametrize(
    "distance,label",
    [
        (0.0, "0-1F"),
        (0.99, "0-1F"),
        (1.0, "1-2F"),
        (3.99, "3-4F"),
        (4.0, "4-5F"),
        (5.0, "5F+"),
        (17.3, "5F+"),
    ],
)
def test_band_edges_are_half_open_and_the_last_band_is_open(distance, label):
    assert ev.band_label(distance) == label


def test_ladder_width_is_measured_not_assumed():
    with pytest.raises(ev.EVAnalysisError):
        ev.ladder_core_width_f([BracketSpec("T90", "greater", floor_strike=90.0)])


# ==========================================================================
# No lookahead
# ==========================================================================
def test_forecast_vintage_never_uses_a_run_issued_after_the_snapshot():
    """The single most valuable number in a backtest is one you could not have had."""
    archive = pd.DataFrame(
        {
            "city": ["NY"] * 3,
            "target_date": ["2026-07-17"] * 3,
            "init_time_utc": [
                "2026-07-15T12:00:00Z",
                "2026-07-16T12:00:00Z",
                "2026-07-17T00:00:00Z",
            ],
            "lead_hours": [40, 16, 4],
            "forecast_high_f": [87.0, 86.0, 90.0],
        }
    )
    archive["init_ts"] = pd.to_datetime(archive["init_time_utc"], utc=True)

    def pick(ts: str):
        return ev.select_forecast_vintage(
            archive, "NY", "2026-07-17", pd.Timestamp(ts, tz="UTC")
        )

    assert pick("2026-07-16T18:00Z")["forecast_high_f"] == 86.0  # not the 90 to come
    assert pick("2026-07-17T00:00Z")["forecast_high_f"] == 90.0  # exact match is usable
    assert pick("2026-07-16T00:00Z")["forecast_high_f"] == 87.0
    assert pick("2026-07-15T00:00Z") is None  # nothing issued yet


def test_forecast_vintage_table_matches_backward_only():
    ladders = pd.DataFrame(
        {
            "city": ["NY", "NY"],
            "target_date": ["2026-07-17", "2026-07-17"],
            "ts_utc": pd.to_datetime(
                ["2026-07-16T18:00Z", "2026-07-17T03:00Z"], utc=True
            ),
        }
    )
    archive = pd.DataFrame(
        {
            "city": ["NY", "NY"],
            "target_date": ["2026-07-17", "2026-07-17"],
            "init_time_utc": ["2026-07-16T12:00:00Z", "2026-07-17T00:00:00Z"],
            "lead_hours": [16, 4],
            "forecast_high_f": [86.0, 90.0],
        }
    )
    archive["init_ts"] = pd.to_datetime(archive["init_time_utc"], utc=True)
    out = ev.forecast_vintage_table(ladders, archive).sort_values("ts_utc")
    assert list(out["forecast_high_f"]) == [86.0, 90.0]
    assert (out["init_ts"] <= out["ts_utc"]).all()


def test_asof_backward_reproduces_merge_asof_including_dtypes():
    """The hand-written backward join must be pandas' own, exactly.

    ``pandas.merge_asof`` faults (Windows access violation in its native join
    path) on this project's pinned pandas/numpy/CPython after the first call in
    a process, and the report needs three vintage joins. The join is therefore
    written out in :func:`ev._asof_backward`, and this test pins it against the
    library function on a fixture carrying every case that matters: a left key
    before any right key (unmatched), an exact tie, interior keys, keys after
    the last right key, unsorted input on both sides, and duplicate right keys.

    ``check_dtype=True`` is load-bearing: an unconditional mask would upcast
    ``lead_hours`` from int64 to float64 and every downstream count would then
    render as a float.
    """
    left = pd.DataFrame(
        {
            "city": ["NY"] * 6,
            "ts_utc": pd.to_datetime(
                [
                    "2026-07-17T03:00Z",
                    "2026-07-16T18:00Z",
                    "2026-07-16T12:00Z",
                    "2026-07-15T00:00Z",
                    "2026-07-18T09:00Z",
                    "2026-07-17T00:00Z",
                ],
                utc=True,
            ),
        }
    )
    right = pd.DataFrame(
        {
            "init_ts": pd.to_datetime(
                [
                    "2026-07-17T00:00Z",
                    "2026-07-16T12:00Z",
                    "2026-07-16T12:00Z",
                    "2026-07-18T00:00Z",
                ],
                utc=True,
            ),
            "init_time_utc": ["c", "a", "b", "d"],
            "lead_hours": [4, 16, 16, 9],
            "forecast_high_f": [90.0, 86.0, 87.0, 91.0],
        }
    )
    expected = pd.merge_asof(
        left.sort_values("ts_utc", kind="mergesort").reset_index(drop=True),
        right.sort_values("init_ts", kind="mergesort").reset_index(drop=True),
        left_on="ts_utc",
        right_on="init_ts",
        direction="backward",
        allow_exact_matches=True,
    )
    got = ev._asof_backward(left, right, left_on="ts_utc", right_on="init_ts")
    pd.testing.assert_frame_equal(expected, got, check_dtype=True, check_exact=True)

    # The unmatched row is the earliest one, and it really is unmatched.
    assert math.isnan(float(got.iloc[0]["forecast_high_f"]))
    # An all-matched frame keeps the integer dtype merge_asof would keep.
    all_matched = ev._asof_backward(
        left[left["ts_utc"] >= "2026-07-16T12:00Z"],
        right,
        left_on="ts_utc",
        right_on="init_ts",
    )
    assert all_matched["lead_hours"].dtype == right["lead_hours"].dtype


# ==========================================================================
# Tail probability
# ==========================================================================
def test_gaussian_tail_uses_the_same_continuity_correction_as_the_pmf():
    r"""Errors are whole degrees, so the tail must be corrected like the pmf.

    With bias 0 and sigma 2, P(|E| >= 4) for an integer-valued E is

        P(E >= 4) = 1 - Phi((4 - 0.5)/2) = 1 - Phi(1.75) = 1 - 0.95994 = 0.04006
        P(E <= -4) =     Phi((-4 + 0.5)/2) =    Phi(-1.75)              = 0.04006
        total                                                           = 0.08012

    (Phi(1.75) = 0.95994 from a published 5-decimal table.) An *uncorrected*
    continuous tail would be 2*(1 - Phi(2)) = 0.04550 -- 43% smaller, and the
    whole far-bracket verdict lives in exactly this number.
    """
    got = ev.gaussian_tail_probability(4.0, bias=0.0, sigma=2.0, side="abs")
    assert got == pytest.approx(0.08012, abs=5e-5)
    assert ev.gaussian_tail_probability(4.0, 0.0, 2.0, "high") == pytest.approx(
        0.04006, abs=5e-5
    )
    assert ev.gaussian_tail_probability(4.0, 0.0, 2.0, "low") == pytest.approx(
        0.04006, abs=5e-5
    )
    uncorrected = 2.0 * (1.0 - ev.normal_cdf(2.0))
    assert uncorrected < got * 0.6


def test_a_warm_bias_shifts_the_tail_asymmetrically():
    hi = ev.gaussian_tail_probability(4.0, bias=1.0, sigma=2.0, side="high")
    lo = ev.gaussian_tail_probability(4.0, bias=1.0, sigma=2.0, side="low")
    assert hi > lo


def test_mixture_tail_is_refused_where_the_second_component_is_unmeasured():
    """LAX and MIA have n=2 outside the N/X window. Refuse, do not extrapolate."""
    assert ev.mixture_tail_probability(5.0, "LAX") is None
    assert ev.mixture_tail_probability(5.0, "MIA") is None
    for city in ("NY", "CHI"):
        mix = ev.mixture_tail_probability(5.0, city)
        assert mix is not None and 0.0 < mix < 1.0


def test_mixture_regime_is_refused_on_a_non_day_of_lead():
    """The mixture's components are the *day-of* regime split (workstream D
    section 6). Applying them to a 16-hour-lead forecast would price a longer
    lead with day-of parameters, so the builder refuses the combination.
    """
    cfg = ev.EVConfig(regime=ev.REGIME_MIXTURE)
    with pytest.raises(ev.EVAnalysisError):
        ev.build_probability_table(
            pd.DataFrame(), pd.DataFrame(), None, ev.GFS_MEX, cfg, day_of_only=False
        )


# ==========================================================================
# Shape evaluation
# ==========================================================================
def test_fr31a_mask_is_the_prd_rule_and_needs_a_real_ask():
    """A sentinel ask of 1.00 would satisfy ``p_yes <= 1.00 - 0.08`` almost
    always; the rule must require the ask to exist.
    """
    df = pd.DataFrame(
        {
            "direction": [ev.DIRECTION_NO] * 4,
            "window": [">=24h"] * 4,
            "yes_ask": [0.20, 1.00, 0.20, 0.20],
            "p_yes": [0.05, 0.05, 0.15, 0.05],
            "edge_distance_f": [5.0, 5.0, 5.0, 2.0],
        }
    )
    got = list(ev.fr31a_mask(df))
    assert got == [True, False, False, False]


def test_evaluate_shape_clusters_on_dates_not_rows():
    """Twenty snapshots of one market on one date are one bet, not twenty."""
    opp = _toy_opportunity_frame()
    mask = opp["direction"].eq(ev.DIRECTION_NO) & opp["mode"].eq(ev.MODE_TAKER)
    res = ev.evaluate_shape(opp, mask, "toy", max_entries_per_market=1)
    assert res is not None
    assert res.trades == res.markets  # one entry per market
    assert res.dates <= res.trades
    assert 0.0 <= res.fill_opportunity_rate <= 1.0


def test_evaluate_shape_returns_none_rather_than_a_number_on_no_fills():
    opp = _toy_opportunity_frame()
    nothing = pd.Series(False, index=opp.index)
    assert ev.evaluate_shape(opp, nothing, "empty") is None


# ==========================================================================
# Walk-forward calibration
# ==========================================================================
def _artifacts_present() -> bool:
    return ev.GFS_MEX.is_available(tuple(ev.CITY_STATION)) and os.path.isdir(
        os.path.join(ev.REPO_ROOT, "data", "weather_truth")
    )


requires_artifacts = pytest.mark.skipif(
    not _artifacts_present(),
    reason="calibration / forecast / truth artifacts not on disk",
)


def test_walk_forward_cutoff_is_strictly_before_the_priced_date():
    cal = ev.WalkForwardCalibrator.__new__(ev.WalkForwardCalibrator)
    cal.embargo_days = 1
    assert cal.cutoff_for("2026-07-17") == "2026-07-16"
    cal.embargo_days = 2
    assert cal.cutoff_for("2026-07-17") == "2026-07-15"


@requires_artifacts
def test_walk_forward_calibration_never_sees_the_date_it_prices():
    """The contamination fix, asserted rather than described.

    The committed artifacts were fitted through 2026-07-24 while the ladders run
    to 2026-07-25, so an in-sample score reads the outcome it is predicting.
    """
    wf = ev.WalkForwardCalibrator(ev.GFS_MEX, ("NY",), embargo_days=1)
    for target in ("2026-05-18", "2026-06-15", "2026-07-25"):
        payload = wf.calibration_as_of("NY", target)
        assert payload["coverage"]["last_target_date"] < target
        assert payload["coverage"]["day_of_paired_days"] >= 60


@requires_artifacts
def test_walk_forward_and_in_sample_are_actually_different_calibrations():
    """If they agreed, the walk-forward harness would be decorative."""
    wf = ev.WalkForwardCalibrator(ev.GFS_MEX, ("NY",), embargo_days=1)
    ins = ev.InSampleCalibrator(ev.GFS_MEX, ("NY",))
    a = wf.calibration_as_of("NY", "2026-06-01")["day_of"]
    b = ins.calibration_as_of("NY", "2026-06-01")["day_of"]
    assert a["n"] < b["n"]
    assert (a["bias_f"], a["sigma_f"]) != (b["bias_f"], b["sigma_f"])


@requires_artifacts
def test_walk_forward_refuses_a_date_with_too_little_prior_truth():
    wf = ev.WalkForwardCalibrator(
        ev.GFS_MEX, ("NY",), embargo_days=1, min_paired_days=60
    )
    with pytest.raises(ev.EVAnalysisError):
        wf.calibration_as_of("NY", "2026-01-05")


@requires_artifacts
def test_available_sources_discovers_what_is_on_disk_and_nothing_else():
    names = [s.name for s in ev.available_sources()]
    assert "gfs_mex" in names
    for s in ev.available_sources():
        assert s.is_available(tuple(ev.CITY_STATION))


# ==========================================================================
# The committed worked example -- the red-team's recompute, pinned
# ==========================================================================
@pytest.mark.skipif(
    not os.path.exists(os.path.join(FIXTURE_DIR, "worked_example.json")),
    reason="worked-example fixture not generated yet",
)
def test_the_reports_worked_example_recomputes_from_its_own_raw_inputs():
    """Recompute the artifact's §5 example from the raw values it publishes.

    Nothing here reads the report's *answer*; every intermediate is rebuilt from
    the raw inputs, which is exactly what EC-5 asks a red-team to do.
    """
    with open(os.path.join(FIXTURE_DIR, "worked_example.json"), encoding="utf-8") as fh:
        x = json.load(fh)

    mu = x["forecast_high_f"] - x["bias_f"]
    assert mu == pytest.approx(x["mu_f"], abs=1e-6)

    sigma = x["sigma_f"]
    z_hi = (x["cap_strike"] + 0.5 - mu) / sigma
    z_lo = (x["floor_strike"] - 0.5 - mu) / sigma
    phi = lambda z: 0.5 * math.erfc(-z / math.sqrt(2.0))  # noqa: E731
    p_yes = phi(z_hi) - phi(z_lo)
    assert p_yes == pytest.approx(x["p_yes"], abs=1e-9)

    quote = 1.0 - x["yes_bid"]
    assert quote == pytest.approx(x["no_ask"], abs=1e-9)

    result = ev.ev_for_trade(
        1.0 - p_yes,
        quote,
        contracts=x["contracts"],
        is_maker=False,
        adverse_fill=x["adverse_fill"],
    )
    assert result.price_paid == pytest.approx(x["price_paid"], abs=1e-9)
    assert result.fee_total == pytest.approx(x["fee_total"], abs=1e-9)
    assert result.ev_per_contract == pytest.approx(x["ev_per_contract"], abs=1e-6)

    settled = x["expiration_value"]
    won = not (x["floor_strike"] <= settled <= x["cap_strike"])
    assert won == x["won"]
    realized = (1.0 if won else 0.0) - result.price_paid - result.fee_per_contract
    assert realized == pytest.approx(x["realized_per_contract"], abs=1e-6)


# ==========================================================================
# Fixtures
# ==========================================================================
def _real_opportunity_frame() -> pd.DataFrame:
    """Drive the production :func:`build_opportunity_frame` on a synthetic tape.

    Two markets over two dates. ``A`` has a two-sided book that falls (so a
    resting YES bid fills and a resting NO bid does not); ``B`` has **no bid at
    all**, which is the recorded archive's dominant far-bracket state and the
    case an EV model must not silently price. One snapshot per date is placed
    after the close to exercise the post-close drop.
    """
    lad_rows, prob_rows, vin_rows = [], [], []
    for day, (date_str, settled) in enumerate(
        [("2026-06-01", "no"), ("2026-06-02", "yes")]
    ):
        init = f"{date_str}T00:00:00Z"
        vin_rows += [
            {
                "city": "NY",
                "target_date": date_str,
                "ts_utc": pd.Timestamp(f"{date_str}T0{h}:00Z", tz="UTC"),
                "init_time_utc": init,
                "lead_hours": 4,
                "forecast_high_f": 90.0,
            }
            for h in range(4)
        ]
        prob_rows += [
            {
                "city": "NY",
                "target_date": date_str,
                "init_time_utc": init,
                "market_ticker": f"M{day}A",
                "mu_f": 90.0,
                "sigma_f": 3.0,
                "p_yes": 0.10,
                "midpoint_f": 83.5,
                "distance_f": 6.5,
                "edge_distance_f": 6.0,
                "band": "5F+",
                "lead_bucket": "day_of",
                "calibration_bucket": "day_of",
                "regime_model": "single_normal",
            },
            {
                "city": "NY",
                "target_date": date_str,
                "init_time_utc": init,
                "market_ticker": f"M{day}B",
                "mu_f": 90.0,
                "sigma_f": 3.0,
                "p_yes": 0.03,
                "midpoint_f": 91.5,
                "distance_f": 1.5,
                "edge_distance_f": 1.0,
                "band": "1-2F",
                "lead_bucket": "day_of",
                "calibration_bucket": "day_of",
                "regime_model": "single_normal",
            },
        ]
        for h in range(4):
            common = {
                "city": "NY",
                "target_date": date_str,
                "ts_utc": pd.Timestamp(f"{date_str}T0{h}:00Z", tz="UTC"),
                # the 4th snapshot is past the close and must be dropped
                "minutes_to_close": 1500 - h * 600 if h < 3 else -30.0,
                "result": settled,
                "expiration_value": 84.0 if settled == "yes" else 90.0,
            }
            lad_rows.append(
                {
                    **common,
                    "market_ticker": f"M{day}A",
                    "strike_type": "between",
                    "floor_strike": 83.0,
                    "cap_strike": 84.0,
                    "yes_sub_title": "83 to 84",
                    "yes_bid": 0.20 - 0.05 * h,
                    "yes_ask": 0.22 - 0.05 * h,
                }
            )
            lad_rows.append(
                {
                    **common,
                    "market_ticker": f"M{day}B",
                    "strike_type": "greater",
                    "floor_strike": 90.0,
                    "cap_strike": float("nan"),
                    "yes_sub_title": "91 or above",
                    "yes_bid": 0.0,
                    "yes_ask": 0.01,  # one-sided: no bid ever
                }
            )
    ladders = pd.DataFrame(lad_rows)
    ladders["no_bid"] = 1.0 - ladders["yes_ask"]
    ladders["no_ask"] = 1.0 - ladders["yes_bid"]
    ladders["has_quote"] = (ladders["yes_bid"] > 0) & (ladders["yes_ask"] < 1)
    return ev.build_opportunity_frame(
        ladders,
        pd.DataFrame(prob_rows),
        pd.DataFrame(vin_rows),
        ev.EVConfig(contracts=10),
    )


def _toy_opportunity_frame() -> pd.DataFrame:
    """A small end-to-end opportunity frame with deliberate one-sided books.

    Two markets over two dates. One market's book is two-sided; the other has
    no bid at all, which is the recorded tape's dominant far-bracket state and
    the case an EV model must not silently price.
    """
    rows = []
    for day, (date_str, settled) in enumerate(
        [("2026-06-01", "no"), ("2026-06-02", "yes")]
    ):
        for hour in range(3):
            rows.append(
                {
                    "city": "NY",
                    "target_date": date_str,
                    "market_ticker": f"M{day}A",
                    "ts_utc": pd.Timestamp(f"{date_str}T0{hour}:00Z"),
                    "minutes_to_close": 1500 - hour * 60,
                    "strike_type": "between",
                    "floor_strike": 83.0,
                    "cap_strike": 84.0,
                    "yes_sub_title": "83 to 84",
                    "yes_bid": 0.20 - 0.01 * hour,
                    "yes_ask": 0.22 - 0.01 * hour,
                    "result": settled,
                    "expiration_value": 84.0 if settled == "yes" else 90.0,
                    "p_yes": 0.10,
                    "mu_f": 90.0,
                    "sigma_f": 3.0,
                    "midpoint_f": 83.5,
                    "distance_f": 6.5,
                    "edge_distance_f": 6.0,
                    "band": "5F+",
                    "lead_bucket": "day_of",
                    "calibration_bucket": "day_of",
                    "regime_model": "single_normal",
                }
            )
            rows.append(
                {
                    "city": "NY",
                    "target_date": date_str,
                    "market_ticker": f"M{day}B",
                    "ts_utc": pd.Timestamp(f"{date_str}T0{hour}:00Z"),
                    "minutes_to_close": 1500 - hour * 60,
                    "strike_type": "greater",
                    "floor_strike": 90.0,
                    "cap_strike": float("nan"),
                    "yes_sub_title": "91 or above",
                    "yes_bid": 0.0,
                    "yes_ask": 0.01,  # one-sided: no bid at all
                    "result": "no",
                    "expiration_value": 84.0 if settled == "yes" else 90.0,
                    "p_yes": 0.03,
                    "mu_f": 90.0,
                    "sigma_f": 3.0,
                    "midpoint_f": 91.5,
                    "distance_f": 1.5,
                    "edge_distance_f": 1.0,
                    "band": "1-2F",
                    "lead_bucket": "day_of",
                    "calibration_bucket": "day_of",
                    "regime_model": "single_normal",
                }
            )
    tape = pd.DataFrame(rows)
    tape["ts_utc"] = pd.to_datetime(tape["ts_utc"], utc=True)
    tape["no_bid"] = 1.0 - tape["yes_ask"]
    tape["no_ask"] = 1.0 - tape["yes_bid"]
    tape["has_quote"] = (tape["yes_bid"] > 0) & (tape["yes_ask"] < 1)
    tape["init_time_utc"] = "2026-06-01T00:00:00Z"
    tape["lead_hours"] = 4
    tape["forecast_high_f"] = 90.0
    tape["window"] = tape["minutes_to_close"].map(ev.time_window_label)
    tape["settles_yes"] = tape["result"].eq("yes")
    tape = ev.add_maker_fill_flags(tape)

    cfg = ev.EVConfig(contracts=10)
    frames = []
    for direction in ev.DIRECTIONS:
        for mode in ev.MODES:
            part = tape.copy()
            part["direction"] = direction
            part["mode"] = mode
            is_maker = mode == ev.MODE_MAKER
            if direction == ev.DIRECTION_YES:
                quote = (
                    part["yes_bid"].where(part["yes_bid"] > 0)
                    if is_maker
                    else part["yes_ask"].where(part["yes_ask"] < 1.0)
                )
                part["p_win"] = part["p_yes"]
                part["won"] = part["settles_yes"]
            else:
                quote = (
                    (1.0 - part["yes_ask"]).where(part["yes_ask"] < 1.0)
                    if is_maker
                    else (1.0 - part["yes_bid"]).where(part["yes_bid"] > 0)
                )
                part["p_win"] = 1.0 - part["p_yes"]
                part["won"] = ~part["settles_yes"]
            part["quote"] = quote
            part["quote_present"] = quote.notna()
            fillable = (
                (
                    part["maker_yes_fill"]
                    if direction == ev.DIRECTION_YES
                    else part["maker_no_fill"]
                )
                if is_maker
                else part["quote_present"]
            )
            part["fillable"] = part["quote_present"] & fillable.fillna(False)
            frames.append(part)
    opp = pd.concat(frames, ignore_index=True)
    opp["price_paid"] = (opp["quote"] + cfg.adverse_fill_dollars).round(10)
    opp.loc[opp["price_paid"] > ev.MAX_ORDERABLE_PRICE + 1e-12, "price_paid"] = float(
        "nan"
    )
    opp["executable"] = opp["fillable"] & opp["price_paid"].notna()
    opp["is_maker"] = opp["mode"].eq(ev.MODE_MAKER)
    opp["fee_per_contract"] = [
        float("nan")
        if pd.isna(p)
        else ev.fee_per_contract(p, cfg.contracts, m, cfg.series_fee_type)
        for p, m in zip(opp["price_paid"], opp["is_maker"])
    ]
    opp["ev_per_contract"] = opp["p_win"] - opp["price_paid"] - opp["fee_per_contract"]
    opp["realized_per_contract"] = (
        opp["won"].astype(float) - opp["price_paid"] - opp["fee_per_contract"]
    )
    opp.loc[~opp["executable"], ["ev_per_contract", "realized_per_contract"]] = float(
        "nan"
    )
    return opp
