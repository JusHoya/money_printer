"""Tests for the Phase 4 gas backtest harness (WS-D, exit criterion 2).

These tests exist to attack the harness, not to demonstrate it. A backtest that
peeks, that prices a quote the book never showed, that re-implements a gate it
claims to be replaying, or that reports a trade-level standard error as though it
were an independent sample, produces a number worse than no number at all. Each
group below is one of those failure modes made to fail.

Nothing here hits the network. Most tests build a small synthetic series and a
hand-made tape row; the tests in ``TestArtifactIntegrity`` and
``TestArtifactTellsOneStory`` read the committed tape and the committed dated
report instead, and **treat their absence as a failure rather than a skip** --
those files are this workstream's deliverables, and a test that skips when the
evidence is missing is a green that cannot fail. Exactly one skip remains in this
file and it is justified inline.
"""

from __future__ import annotations

import csv
import glob
import math
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts import gas_backtest as gb  # noqa: E402
from src.core.fee_calculator import (  # noqa: E402
    FEE_TYPE_STANDARD,
    FEE_TYPE_WITH_MAKER_FEES,
    KNOWN_MAKER_FEE_SERIES,
    compute_fee,
    fee_type_for_symbol,
)
from src.models.gas_projection import (  # noqa: E402
    GasObservation,
    GasSeries,
    ProjectionConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _synthetic_aaa(start: date, n_days: int, base: float = 3.50) -> list:
    """A smooth, strictly plausible AAA series long enough to fit on."""
    rows = []
    for i in range(n_days):
        value = base + 0.30 * math.sin(i / 40.0) + 0.0004 * i
        rows.append(
            GasObservation(date=start + timedelta(days=i), value=round(value, 3))
        )
    return rows


def _synthetic_rbob(start: date, n_days: int) -> list:
    rows = []
    for i in range(n_days):
        value = 2.20 + 0.35 * math.sin((i - 5) / 38.0) + 0.0003 * i
        rows.append(
            GasObservation(date=start + timedelta(days=i), value=round(value, 3))
        )
    return rows


def _series(start: date = date(2024, 1, 1), n_days: int = 500) -> GasSeries:
    return GasSeries.from_rows(
        aaa=_synthetic_aaa(start, n_days),
        rbob=_synthetic_rbob(start - timedelta(days=30), n_days + 30),
    )


def _tape_row(**kw) -> gb.TapeRow:
    """A tape row with sane defaults; override only what a test cares about."""
    defaults = dict(
        series="KXAAAGASM",
        event_ticker="KXAAAGASM-26JUN30",
        ticker="KXAAAGASM-26JUN30-3.82",
        floor_strike=3.82,
        strike_type="greater",
        status="finalized",
        result="yes",
        expiration_value=3.847,
        close_time="2026-06-30T03:59:00Z",
        expected_expiration_time="2026-06-30T14:00:00Z",
        settlement_date=date(2026, 6, 30),
        end_ts=int(datetime(2026, 6, 24, 22, 0, tzinfo=timezone.utc).timestamp()),
        et_date=date(2026, 6, 24),
        et_hour=18,
        yes_bid=0.70,
        yes_ask=0.78,
        yes_bid_high=0.72,
        yes_ask_low=0.76,
        last=0.74,
        volume_fp=100.0,
        open_interest_fp=500.0,
    )
    defaults.update(kw)
    return gb.TapeRow(**defaults)


# ===========================================================================
# 1. Lookahead
# ===========================================================================


class TestNoLookahead:
    """The single defect that would make the artifact worthless."""

    def test_appending_future_rows_cannot_change_a_decision(self):
        """A row published after the decision date must not move any number.

        This is the mutation test for the whole harness: the same replay is run
        against a series that has been extended with wildly different future
        values. If any priced cell moves, the clamp leaks.
        """
        base = _series()
        newest = max(o.date for o in base.aaa)
        et_date = newest  # decide on the newest observed day
        settlement = et_date + timedelta(days=10)
        row = _tape_row(
            settlement_date=settlement,
            et_date=et_date,
            end_ts=int(
                datetime(
                    et_date.year, et_date.month, et_date.day, 22, tzinfo=timezone.utc
                ).timestamp()
            ),
            close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
            expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
            floor_strike=round(base.aaa[-1].value, 2),
            result="",
            expiration_value=None,
        )
        spec = _spec_from_series(base)
        run_a = gb.simulate_ev(
            spec,
            [row],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )

        # Poison the future: absurd values on every day after the decision.
        poisoned = GasSeries.from_rows(
            aaa=list(base.aaa)
            + [
                GasObservation(date=et_date + timedelta(days=i), value=8.50)
                for i in range(1, 30)
            ],
            rbob=list(base.rbob)
            + [
                GasObservation(date=et_date + timedelta(days=i), value=7.00)
                for i in range(1, 30)
            ],
        )
        run_b = gb.simulate_ev(
            _spec_from_series(poisoned),
            [row],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )

        assert run_a.cells, "the control run produced no cells to compare"
        assert len(run_a.cells) == len(run_b.cells)
        for a, b in zip(run_a.cells, run_b.cells):
            assert a.point == pytest.approx(b.point, abs=1e-12)
            assert a.sigma == pytest.approx(b.sigma, abs=1e-12)
            assert a.p_yes == pytest.approx(b.p_yes, abs=1e-12)
            assert a.inputs_hash == b.inputs_hash

    def test_walk_forward_mae_never_fits_on_or_after_its_target(self):
        spec = _spec_from_series(_series())
        counter = gb.FitCounter()
        table = {o.date: o.value for o in spec.series.aaa}
        targets = gb.observed_aaa_dates(spec)[-20:]
        rows = gb.walk_forward_mae(
            spec,
            targets,
            (1, 7, 14),
            ProjectionConfig(min_history_days=365),
            counter,
            lambda d: table.get(d),
            "aaa",
        )
        assert rows, "no MAE rows were produced, so this test proves nothing"
        for r in rows:
            assert r.as_of < r.target_date
            assert r.realized_lead >= r.nominal_lead
            assert r.realized_lead == (r.target_date - r.as_of).days

    def test_decision_snapshot_never_after_its_own_hour(self):
        rows = [
            _tape_row(et_hour=h, end_ts=1_780_000_000 + h * 3600) for h in range(24)
        ]
        picked = gb._decision_rows(rows, hour_et=18)
        assert len(picked) == 1
        assert picked[0].et_hour == 18


# ===========================================================================
# 2. Quote availability — never price a fill the book did not offer
# ===========================================================================


class TestQuoteHandling:
    def test_empty_book_sentinels_are_absent_not_prices(self):
        assert gb._candle_price({"close_dollars": "0.0000"}) is None
        assert gb._candle_price({"close_dollars": "1.0000"}) is None
        assert gb._candle_price({"close_dollars": "0.0100"}) == pytest.approx(0.01)
        assert gb._candle_price({}) is None
        assert gb._candle_price(None) is None
        assert gb._candle_price({"close_dollars": ""}) is None

    def test_missing_side_is_counted_as_a_candidate_but_never_priced(self):
        """A one-sided book must inflate ``n_cand`` and not ``n_exec``.

        The failure this guards is the one that makes a backtest look good: drop
        the unfillable rows silently and the executable fraction reads 100%.
        """
        spec = _spec_from_series(_series())
        newest = max(o.date for o in spec.series.aaa)
        settlement = newest + timedelta(days=8)
        common = dict(
            settlement_date=settlement,
            et_date=newest,
            end_ts=int(
                datetime(
                    newest.year, newest.month, newest.day, 22, tzinfo=timezone.utc
                ).timestamp()
            ),
            close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
            expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
            floor_strike=round(spec.series.aaa[-1].value, 2),
            result="",
            expiration_value=None,
        )
        no_ask = _tape_row(
            ticker="KXAAAGASM-26JUN30-A",
            yes_ask=None,
            yes_ask_low=None,
            last=0.70,
            **common,
        )
        run = gb.simulate_ev(
            spec,
            [no_ask],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )
        yes_taker = [c for c in run.cells if c.side == "YES" and c.mode == "taker"]
        assert yes_taker, "expected a YES taker candidate row"
        assert yes_taker[0].executable is False
        assert yes_taker[0].ev is None
        assert yes_taker[0].realized is None
        qa = gb.quote_availability(run.cells)
        assert qa["YES_taker"]["n_cand"] == 1
        assert qa["YES_taker"]["n_exec"] == 0
        assert qa["YES_taker"]["frac"] == 0.0

    def test_price_at_or_above_one_after_allowance_is_unexecutable(self):
        """A 0.99 quote plus the 1c allowance leaves the orderable grid."""
        spec = _spec_from_series(_series())
        newest = max(o.date for o in spec.series.aaa)
        settlement = newest + timedelta(days=5)
        row = _tape_row(
            yes_bid=0.995,
            yes_ask=0.998,
            yes_bid_high=0.995,
            yes_ask_low=0.998,
            last=0.995,
            settlement_date=settlement,
            et_date=newest,
            end_ts=int(
                datetime(
                    newest.year, newest.month, newest.day, 22, tzinfo=timezone.utc
                ).timestamp()
            ),
            close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
            expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
            result="",
            expiration_value=None,
        )
        run = gb.simulate_ev(
            spec,
            [row],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )
        # The loop below is vacuous unless cells exist AND at least one was
        # actually pushed off the orderable grid by the allowance, so both are
        # asserted first. An earlier revision asserted only the loop and would
        # have passed on zero cells.
        assert run.cells, "no cells priced; the guard below would be vacuous"
        rejected = [
            c
            for c in run.cells
            if c.quote is not None and c.quote + gb.ADVERSE_FILL_ALLOWANCE >= 1.0
        ]
        assert rejected, (
            "no candidate was near enough to $1.00 for the allowance to matter; "
            "this fixture no longer tests what it claims"
        )
        for cell in rejected:
            assert cell.executable is False
            assert cell.price_paid is None
            assert cell.ev is None
        for cell in run.cells:
            if cell.price_paid is not None:
                assert cell.price_paid < 1.0

    def test_reference_price_matches_the_strategys_own_rule(self):
        """The report's reference price must be the strategy's, not a variant.

        Compared against ``gas_convergence._reference_price`` over a grid rather
        than restated, because a silently divergent mid would move every
        divergence number in the artifact.
        """
        from src.strategies.gas_convergence import _reference_price

        compared: list = []
        grid = (None, 0.0, 0.01, 0.4, 0.6, 0.99, 1.0)
        for bid in grid:
            for ask in grid:
                for last in grid:
                    row = _tape_row(
                        yes_bid=bid if (bid and 0 < bid < 1) else None,
                        yes_ask=ask if (ask and 0 < ask < 1) else None,
                        last=last if (last and 0 < last < 1) else None,
                    )
                    mine = gb._reference(row)
                    theirs = _reference_price(row.yes_bid, row.yes_ask, row.last)
                    assert mine == theirs
                    if mine is not None:
                        compared.append(mine)
        assert len(compared) > 50, (
            f"only {len(compared)} grid points produced a price; the parity "
            f"assertion was mostly comparing None to None"
        )


# ===========================================================================
# 3. Fees — the 25% trap
# ===========================================================================


class TestFees:
    def test_only_the_monthly_gas_series_bills_makers(self):
        assert fee_type_for_symbol("KXAAAGASM-26AUG31-4.60") == (
            FEE_TYPE_WITH_MAKER_FEES
        )
        assert fee_type_for_symbol("KXAAAGASW-26AUG03-4.10") == FEE_TYPE_STANDARD
        assert fee_type_for_symbol("KXAAAGASD-26JUL30-4.10") == FEE_TYPE_STANDARD
        assert KNOWN_MAKER_FEE_SERIES == frozenset({"KXAAAGASM"})

    @pytest.mark.parametrize("price", [0.05, 0.10, 0.25, 0.50, 0.75, 0.90])
    def test_at_one_contract_scaling_taker_by_25_percent_understates_the_maker_fee(
        self, price
    ):
        """The PRD's "25% of taker" shortcut is wrong at FR-4.3's order size.

        Both legs are ceil'd to the cent independently, so at C = 1 the maker fee
        is one cent whenever it is charged at all, and a quarter of the taker fee
        never reaches it. This is the concrete direction of the error — the
        shortcut makes resting liquidity look cheaper than it is — which is why
        the report never derives one fee from the other.
        """
        ft = FEE_TYPE_WITH_MAKER_FEES
        taker = compute_fee(price, 1, is_maker=False, series_fee_type=ft).fee
        maker = compute_fee(price, 1, is_maker=True, series_fee_type=ft).fee
        assert taker > 0 and maker > 0
        assert maker > 0.25 * taker + 1e-9
        assert maker / taker >= 0.5 - 1e-9

    def test_the_charged_ratio_is_not_a_constant_25_percent(self):
        """It happens to equal 25% at some (price, size) points and not others.

        Recorded explicitly so nobody "simplifies" the fee path back to a scale
        factor on the strength of a lucky spot check. The assertion is that the
        set of charged ratios over a realistic grid is not the single value 0.25.
        """
        ft = FEE_TYPE_WITH_MAKER_FEES
        ratios = set()
        for price in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90):
            for contracts in (1, 5, 20):
                taker = compute_fee(
                    price, contracts, is_maker=False, series_fee_type=ft
                ).fee
                maker = compute_fee(
                    price, contracts, is_maker=True, series_fee_type=ft
                ).fee
                assert taker > 0
                ratios.add(round(maker / taker, 4))
        assert len(ratios) > 1
        assert max(ratios) >= 0.5
        assert 0.25 in ratios, (
            "the grid should contain at least one point where the charged ratio "
            "does equal the rate ratio, so the report's own count of such points "
            "is checkable"
        )

    def test_ev_matches_the_strategys_own_fee_path(self):
        """``EV`` in the report equals ``GasConvergenceStrategy._ev`` exactly."""
        from src.strategies.gas_convergence import GasConvergenceStrategy

        strat = GasConvergenceStrategy(series=None)
        symbol = "KXAAAGASM-26JUN30-3.82"
        prices = (0.07, 0.31, 0.58, 0.86)
        assert prices, "no prices to compare"
        for price in prices:
            for maker in (False, True):
                fee_total = compute_fee(
                    price,
                    5,
                    is_maker=maker,
                    series_fee_type=fee_type_for_symbol(symbol),
                ).fee
                mine = 0.60 - price - fee_total / 5.0
                theirs = strat._ev(symbol, 0.60, price, 5, is_maker=maker)
                assert mine == pytest.approx(theirs, abs=1e-12)

    def test_weekly_series_maker_fee_is_free_and_is_not_borrowed_from_monthly(self):
        assert (
            compute_fee(
                0.40,
                5,
                is_maker=True,
                series_fee_type=fee_type_for_symbol("KXAAAGASW-26AUG03-4.10"),
            ).fee
            == 0.0
        )
        assert (
            compute_fee(
                0.40,
                5,
                is_maker=True,
                series_fee_type=fee_type_for_symbol("KXAAAGASM-26AUG31-4.60"),
            ).fee
            > 0.0
        )


# ===========================================================================
# 4. Settlement semantics
# ===========================================================================


class TestSettlement:
    def test_strict_greater_at_the_boundary(self):
        from src.models.gas_projection import settles_yes_gas

        assert settles_yes_gas(3.821, 3.82) is True
        assert settles_yes_gas(3.820, 3.82) is False
        assert settles_yes_gas(3.819, 3.82) is False

    def test_reconcile_flags_a_disagreement_instead_of_smoothing_it(self):
        reconcile = {}
        # Kalshi says NO but the published value is above the strike.
        row = _tape_row(result="no", expiration_value=3.900, floor_strike=3.82)
        assert gb._settled_yes(row, reconcile) is False
        assert reconcile.get("MISMATCH") == 1
        assert reconcile.get("match", 0) == 0

    def test_unsettled_market_yields_no_realized_pnl(self):
        reconcile = {}
        row = _tape_row(result="", expiration_value=None)
        assert gb._settled_yes(row, reconcile) is None
        assert reconcile.get("unsettled") == 1

    def test_settlement_date_comes_from_api_fields_not_the_ticker(self):
        from src.strategies.gas_convergence import resolve_settlement_date

        d, src = resolve_settlement_date(
            "KXAAAGASM-26AUG31-4.60",
            {"expected_expiration_time": "2026-08-31T14:00:00Z"},
        )
        assert (d, src) == (date(2026, 8, 31), "expected_expiration_time")
        d, src = resolve_settlement_date(
            "KXAAAGASM-26AUG31-4.60", {"close_time": "2026-08-31T03:59:00Z"}
        )
        assert (d, src) == (date(2026, 8, 31), "close_time")
        with pytest.raises(ValueError):
            resolve_settlement_date("KXAAAGASM-26AUG31-4.60", {})


# ===========================================================================
# 5. Statistics — the clustering unit
# ===========================================================================


class TestClustering:
    def test_event_clustering_is_wider_than_trade_clustering_when_correlated(self):
        """The whole point of clustering: correlated rows must widen the interval.

        Two settlements, each with 50 identical outcomes. The trade-level SE is
        near zero by construction; the event-level SE is the real spread. A
        harness that reported the former as the headline would claim
        significance it has not got, which is what this test forbids.
        """
        cells = []
        for settlement, realized in (
            (date(2026, 6, 30), +0.20),
            (date(2026, 7, 31), -0.20),
        ):
            for i in range(50):
                cells.append(
                    _cell(settlement_date=settlement, realized=realized, ev=0.25)
                )
        clustered = gb.cluster_by_event(cells)
        assert clustered["n_events"] == 2
        assert clustered["n_trades"] == 100
        assert clustered["event_mean"] == pytest.approx(0.0)
        trade_se = statistics.stdev([c.realized for c in cells]) / math.sqrt(100)
        assert clustered["event_se"] > trade_se
        # And with a single settlement there is no interval to quote at all.
        one = gb.cluster_by_event(cells[:50])
        assert one["n_events"] == 1
        assert one["event_se"] is None
        assert one["ci_low"] is None

    def test_modelled_ev_outside_the_realized_interval_is_detected(self):
        cells = []
        for i, settlement in enumerate(
            [date(2026, 6, 1) + timedelta(days=7 * i) for i in range(9)]
        ):
            for _ in range(20):
                cells.append(
                    _cell(
                        settlement_date=settlement,
                        realized=-0.03 + 0.01 * (i % 3),
                        ev=0.23,
                        accepted=True,
                    )
                )
        summary = gb.accepted_summary(cells, "taker")
        cluster = summary["cluster"]
        assert cluster["n_events"] == 9
        assert summary["ev"] == pytest.approx(0.23)
        assert cluster["ci_high"] < summary["ev"]
        assert gb._inside(summary["ev"], cluster) == "**NO**"
        assert summary["ev_vs_realized_t"] > 3.0

    def test_skill_vs_market_prefers_the_better_forecaster(self):
        """A market that is right and a model that is wrong must score that way."""
        cells = []
        for i in range(6):
            settlement = date(2026, 6, 1) + timedelta(days=7 * i)
            for won in (True, False):
                cells.append(
                    _cell(
                        settlement_date=settlement,
                        p_yes=0.50,  # uninformative model
                        market_price=0.95 if won else 0.05,  # informed market
                        won=won,
                        realized=0.0,
                        ev=0.0,
                    )
                )
        skill = gb.skill_vs_market(cells)
        assert skill["brier_market"] < skill["brier_model"]
        assert skill["diff"] > 0
        assert skill["events_model_better"] == 0

    def test_calibration_table_reports_distinct_settlements(self):
        cells = [
            _cell(settlement_date=date(2026, 6, 30), p_yes=0.55, won=False),
            _cell(settlement_date=date(2026, 6, 30), p_yes=0.56, won=False),
            _cell(settlement_date=date(2026, 7, 31), p_yes=0.57, won=True),
        ]
        table = gb.calibration_table(cells)
        row = [r for r in table if r["decile"] == "0.5-0.6"][0]
        assert row["n"] == 3
        assert row["n_events"] == 2
        assert row["realized"] == pytest.approx(1 / 3)


def _cell(**kw) -> gb.EvCell:
    defaults = dict(
        ticker="KXAAAGASM-26JUN30-3.82",
        series="KXAAAGASM",
        event_ticker="KXAAAGASM-26JUN30",
        settlement_date=date(2026, 6, 30),
        et_date=date(2026, 6, 24),
        lead_days=6,
        floor_strike=3.82,
        point=3.90,
        sigma=0.07,
        p_yes=0.85,
        market_price=0.74,
        price_source="mid",
        divergence=0.11,
        band="0-1c",
        side="YES",
        mode="taker",
        quote=0.78,
        price_paid=0.79,
        fee_per_ct=0.012,
        ev=0.05,
        ev_no_allowance=0.06,
        realized=0.198,
        won=True,
        executable=True,
        maker_filled=None,
        accepted=True,
        reject_reason=None,
        n_train=377,
        model_version="lagdrift_v1+rbobL2",
        inputs_hash="0" * 64,
        volume_fp=100.0,
        open_interest_fp=500.0,
        spread=0.08,
    )
    defaults.update(kw)
    return gb.EvCell(**defaults)


# ===========================================================================
# 6. The replay drives the real strategy
# ===========================================================================


class TestReplayUsesTheRealStrategy:
    def test_rejection_capture_is_attached_to_the_logger_the_strategy_uses(self):
        """The shared logger sets ``propagate=False``.

        A capture handler on the root logger sees nothing, and every reason-code
        count in the artifact would silently read zero — the exact
        ``make-silent-rejections-observable`` failure this project has shipped
        before. This test fails if the handler is ever moved back to root.
        """
        spec = _spec_from_series(_series())
        newest = max(o.date for o in spec.series.aaa)
        settlement = newest + timedelta(days=6)
        # A strike far below the projection: the model will price it ~1.0 while
        # the book sits mid, guaranteeing a divergence decision and, with a
        # near-resolved book, a logged rejection.
        row = _tape_row(
            floor_strike=1.50,
            yes_bid=0.99,
            yes_ask=0.995,
            yes_bid_high=0.99,
            yes_ask_low=0.995,
            last=0.99,
            settlement_date=settlement,
            et_date=newest,
            end_ts=int(
                datetime(
                    newest.year, newest.month, newest.day, 22, tzinfo=timezone.utc
                ).timestamp()
            ),
            close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
            expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
            result="",
            expiration_value=None,
        )
        run = gb.simulate_ev(
            spec,
            [row],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )
        assert run.rejections, (
            "no rejection reason codes were captured; the capture handler is "
            "probably attached to a logger the strategy does not write to"
        )
        assert all(code.startswith("GAS_") for code in run.rejections)

    def test_window_gate_blocks_outside_and_admits_inside(self):
        """Both directions.

        The negative half alone would pass on a harness that admits nothing at
        all — the failure mode this project has already shipped once, when a
        near-ATM proximity filter rejected every contract for weeks.
        """
        spec = _spec_from_series(_series())
        newest = max(o.date for o in spec.series.aaa)

        def priced(lead_days: int):
            settlement = newest + timedelta(days=lead_days)
            row = _tape_row(
                settlement_date=settlement,
                et_date=newest,
                end_ts=int(
                    datetime(
                        newest.year, newest.month, newest.day, 22, tzinfo=timezone.utc
                    ).timestamp()
                ),
                close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
                expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
                floor_strike=round(spec.series.aaa[-1].value, 2),
                result="",
                expiration_value=None,
            )
            return gb.simulate_ev(
                spec,
                [row],
                config=ProjectionConfig(min_history_days=365),
                quantity=5,
                hour_et=18,
                window_days=14,
                series_filter="KXAAAGASM",
            )

        outside = priced(40)
        assert outside.cells == []
        assert outside.n_snapshots == 0

        inside = priced(9)
        assert inside.n_snapshots == 1, (
            "the window gate rejected an in-window snapshot too, so the negative "
            "assertion above proves nothing"
        )
        assert inside.cells

    def test_stale_data_dates_are_excluded_and_counted(self):
        base = _series()
        # Decide 10 days after the newest AAA row: the freshness gate must bite.
        newest = max(o.date for o in base.aaa)
        et_date = newest + timedelta(days=10)
        settlement = et_date + timedelta(days=5)
        row = _tape_row(
            settlement_date=settlement,
            et_date=et_date,
            close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
            expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
            result="",
            expiration_value=None,
        )
        run = gb.simulate_ev(
            _spec_from_series(base),
            [row],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )
        assert run.stale_decision_dates == 1
        assert run.n_decision_dates == 0
        assert run.cells == []

        # Positive control: the same shape one day stale must be admitted, or
        # the assertions above would also hold for a harness that admits nothing.
        fresh_date = newest + timedelta(days=1)
        fresh_settlement = fresh_date + timedelta(days=5)
        fresh = gb.simulate_ev(
            _spec_from_series(base),
            [
                _tape_row(
                    settlement_date=fresh_settlement,
                    et_date=fresh_date,
                    end_ts=int(
                        datetime(
                            fresh_date.year,
                            fresh_date.month,
                            fresh_date.day,
                            22,
                            tzinfo=timezone.utc,
                        ).timestamp()
                    ),
                    close_time=(fresh_settlement - timedelta(days=1)).isoformat()
                    + "T03:59:00Z",
                    expected_expiration_time=fresh_settlement.isoformat()
                    + "T14:00:00Z",
                    floor_strike=round(base.aaa[-1].value, 2),
                    result="",
                    expiration_value=None,
                )
            ],
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )
        assert fresh.stale_decision_dates == 0
        assert fresh.n_decision_dates == 1
        assert fresh.cells, "a 1-day-old series was rejected as stale"

    def test_projection_is_fitted_once_per_as_of_and_settlement(self):
        """A 20-strike ladder on one date must cost one regression, not twenty."""
        spec = _spec_from_series(_series())
        newest = max(o.date for o in spec.series.aaa)
        settlement = newest + timedelta(days=7)
        rows = [
            _tape_row(
                ticker=f"KXAAAGASM-26JUN30-{3.50 + 0.01 * i:.2f}",
                floor_strike=round(3.50 + 0.01 * i, 2),
                settlement_date=settlement,
                et_date=newest,
                end_ts=int(
                    datetime(
                        newest.year, newest.month, newest.day, 22, tzinfo=timezone.utc
                    ).timestamp()
                ),
                close_time=(settlement - timedelta(days=1)).isoformat() + "T03:59:00Z",
                expected_expiration_time=settlement.isoformat() + "T14:00:00Z",
                result="",
                expiration_value=None,
            )
            for i in range(20)
        ]
        run = gb.simulate_ev(
            spec,
            rows,
            config=ProjectionConfig(min_history_days=365),
            quantity=5,
            hour_et=18,
            window_days=14,
            series_filter="KXAAAGASM",
        )
        assert run.n_markets == 20
        assert run.fits == 1

    def test_maker_fill_requires_a_later_traversal(self):
        early = _tape_row(end_ts=1_000, yes_bid=0.40, yes_ask=0.60, yes_ask_low=0.60)
        never = {early.ticker: [early]}
        assert gb._maker_fill(never, early, "YES", 0.40) is False
        later = _tape_row(end_ts=2_000, yes_ask=0.45, yes_ask_low=0.38)
        crossed = {early.ticker: [early, later]}
        assert gb._maker_fill(crossed, early, "YES", 0.40) is True


def _spec_from_series(series: GasSeries) -> gb.SeriesSpec:
    dates = sorted({o.date for o in series.aaa})
    return gb.SeriesSpec(
        label="test",
        series=series,
        aaa_first=dates[0],
        aaa_last=dates[-1],
        aaa_rows=len(dates),
        aaa_suspect=0,
        aaa_missing_days=(dates[-1] - dates[0]).days + 1 - len(dates),
        rbob_label="synthetic",
        rbob_series_id="synthetic",
        rbob_first=min((o.date for o in series.rbob), default=None),
        rbob_last=max((o.date for o in series.rbob), default=None),
        rbob_rows=len(series.rbob),
        eia_rows=len(series.eia_weekly),
        include_suspect=False,
    )


# ===========================================================================
# 7. Band assignment and aggregation arithmetic
# ===========================================================================


class TestAggregation:
    @pytest.mark.parametrize(
        "distance,expected",
        [
            (0.000, "0-1c"),
            (0.009, "0-1c"),
            (0.010, "1-2c"),
            (0.029, "2-3c"),
            (0.030, "3-5c"),
            (0.049, "3-5c"),
            (0.050, "5-8c"),
            (0.079, "5-8c"),
            (0.080, "8c+"),
            (1.000, "8c+"),
        ],
    )
    def test_band_edges_are_half_open_and_cover_everything(self, distance, expected):
        assert gb._band_label(distance) == expected

    def test_band_table_excludes_unexecutable_cells_from_price_stats(self):
        cells = [
            _cell(band="0-1c", executable=True, price_paid=0.50, ev=0.05, realized=0.5),
            _cell(
                band="0-1c",
                executable=False,
                quote=None,
                price_paid=None,
                fee_per_ct=None,
                ev=None,
                realized=None,
                won=None,
            ),
        ]
        rows = gb.band_table(cells, "YES", "taker")
        assert len(rows) == 1
        row = rows[0]
        assert row["n_cand"] == 2
        assert row["n_exec"] == 1
        assert row["exec_frac"] == 0.5
        assert row["mean_paid"] == pytest.approx(0.50)

    def test_mae_stats_bias_and_mae_are_distinct(self):
        rows = [
            gb.MaeRow(
                target_date=date(2026, 6, 30),
                nominal_lead=7,
                as_of=date(2026, 6, 23),
                realized_lead=7,
                point=3.90,
                sigma=0.08,
                truth=3.85,
                error=+0.05,
                n_train=300,
                model_version="v",
                truth_channel="aaa",
                inputs_hash="x",
            ),
            gb.MaeRow(
                target_date=date(2026, 7, 31),
                nominal_lead=7,
                as_of=date(2026, 7, 24),
                realized_lead=7,
                point=3.80,
                sigma=0.08,
                truth=3.85,
                error=-0.05,
                n_train=300,
                model_version="v",
                truth_channel="aaa",
                inputs_hash="x",
            ),
        ]
        stats = gb.mae_stats(rows)
        assert stats["mae"] == pytest.approx(0.05)
        assert stats["bias"] == pytest.approx(0.0)


# ===========================================================================
# 8. The committed artifact matches the code that claims to have produced it
# ===========================================================================


class TestArtifactIntegrity:
    """The tape is committed evidence, so its absence is a failure not a skip.

    ``reports/phase4/gas_quote_tape.csv`` is the only historical gas quote
    surface that exists anywhere — Kalshi prunes settled markets after about two
    months, so it is not re-fetchable for the events it covers. A test that
    quietly skips when it is gone would let the evidence disappear silently.
    """

    def test_the_quote_tape_evidence_is_present(self):
        required = (gb.TAPE_PATH, gb.TAPE_MANIFEST_PATH)
        assert len(required) == 2
        for path in required:
            assert os.path.isfile(path), (
                f"{path} is missing. This is committed evidence covering settled "
                f"events Kalshi no longer serves; it cannot simply be re-fetched. "
                f"`python scripts/gas_backtest.py fetch-tape` only recovers what "
                f"the API still returns today."
            )

    def test_tape_manifest_hash_matches_the_tape_on_disk(self):
        manifest = gb._read_json(gb.TAPE_MANIFEST_PATH)
        assert manifest["content_hash"] == gb._file_sha256(gb.TAPE_PATH), (
            "the tape's on-disk bytes do not match the hash its manifest "
            "publishes. If the tape was not regenerated, the likely cause is "
            "line endings: this repo is developed with core.autocrlf=true, the "
            "generator writes LF, and `reports/phase4/**` must therefore be "
            "pinned `text eol=lf` in .gitattributes exactly as "
            "`reports/phase2/*` already is. .gitattributes is an "
            "orchestrator-owned file; see this workstream's report."
        )

    def test_tape_rows_never_carry_a_sentinel_as_a_price(self):
        checked = 0
        rows = 0
        with open(gb.TAPE_PATH, "r", encoding="utf-8", newline="") as handle:
            for i, row in enumerate(csv.DictReader(handle)):
                rows += 1
                for column in ("yes_bid", "yes_ask", "yes_bid_high", "yes_ask_low"):
                    raw = row[column]
                    if raw == "":
                        continue
                    value = float(raw)
                    assert 0.0 < value < 1.0, f"row {i} {column}={raw}"
                    checked += 1
        # Without this the test passes on an empty tape, or on a tape whose every
        # quote column is blank -- a green that proves nothing.
        assert rows > 1000, f"tape has only {rows} rows; expected the full history"
        assert checked > 1000, (
            f"only {checked} quote values were non-blank; the assertion above "
            f"never really ran"
        )

    def test_no_test_in_this_file_can_skip_or_pass_vacuously(self):
        """Enforce, for every future test here, what had to be audited by hand.

        Two failure shapes, both of which this file has shipped:

        * a ``pytest.skip`` guarded on something the run itself produces — a
          green that cannot fail. One of those hid a JSON block I had reported as
          delivered when only the source had it;
        * a test whose every assertion sits inside a ``for`` over a collection
          that may be empty, so it passes when the thing it examines is absent.

        A literal-iterable loop is fine, but it must be preceded by an assertion
        outside the loop — usually that the iterable is non-empty — so the intent
        is stated rather than inferred.
        """
        import ast

        tree = ast.parse(open(__file__, encoding="utf-8").read())

        def asserts(node):
            return [n for n in ast.walk(node) if isinstance(n, ast.Assert)]

        skipping, vacuous = [], []
        checked = 0
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                if not fn.name.startswith("test_"):
                    continue
                checked += 1
                name = f"{cls.name}::{fn.name}"
                if fn.name == "test_no_test_in_this_file_can_skip_or_pass_vacuously":
                    continue
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "skip"
                    ):
                        skipping.append(name)
                loops = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
                nested = {
                    id(a)
                    for lp in loops
                    for a in ast.walk(lp)
                    if isinstance(a, ast.Assert)
                }
                all_a = asserts(fn)
                outside = [a for a in all_a if id(a) not in nested]
                if not all_a or not outside:
                    vacuous.append(name)

        assert checked > 40, (
            f"the AST walk found only {checked} tests; it is probably not "
            f"matching and this meta-test is itself vacuous"
        )
        assert not skipping, (
            f"pytest.skip in {sorted(set(skipping))}. Every input these tests "
            f"read is produced by `gas_backtest.py run` or committed alongside "
            f"it, so absence is a defect, not a reason to skip."
        )
        assert not vacuous, (
            f"every assertion is inside a loop in {sorted(set(vacuous))}, so the "
            f"test passes if that loop never runs. Add an assertion outside it."
        )

    def test_every_tape_market_is_strike_type_greater(self):
        """PRD FR-1.1: read semantics from the API, never from the suffix."""
        seen = set()
        with open(gb.TAPE_PATH, "r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                seen.add(row["strike_type"])
        assert seen == {"greater"}, seen


# ===========================================================================
# 9. The deferral register, and the artifact not arguing with itself
# ===========================================================================


class TestAaaVsKalshiCrossCheck:
    """The two-channel integrity check, and proof that it can fail.

    ``mutation-test-your-acceptance-gates``: a green gate proves nothing until it
    has been shown to go red. This check previously lived in the artifact as the
    hardcoded sentence "77/77 AAA rows fall inside the Kalshi-pinned interval",
    quoted from a message — which could not fail, and did go stale the moment a
    row was removed from the AAA series.
    """

    def test_the_committed_inputs_agree_across_both_channels(self):
        xc = gb.aaa_vs_kalshi_crosscheck()
        assert xc.pinned_rows > 50, (
            f"only {xc.pinned_rows} pinned rows; the fixture looks truncated and "
            f"the assertions below would be weak"
        )
        assert xc.rows_with_aaa > 50
        assert xc.outside == 0, f"values outside the interval: {xc.outside_detail}"
        assert xc.neither == 0, f"unattributable settlements: {xc.neither_detail}"
        assert xc.containment_ok
        assert xc.attribution_ok
        assert xc.max_deviation is not None and xc.max_deviation < 0.01, (
            f"max deviation ${xc.max_deviation} is a cent or more, which is the "
            f"width of a whole strike on this ladder"
        )

    def test_a_day_shifted_series_shows_up_as_previous_day_matches(self, tmp_path):
        """The ET-attribution column must actually detect a shift.

        Shifting every AAA row one day later should move the mass from the
        same-day column into the previous-day column. If it does not, the column
        is decorative and could not have told us the attribution is sound.
        """
        real = gb.aaa_vs_kalshi_crosscheck()
        assert real.same_day > real.prev_day, "precondition: real data is same-day"
        rows = self._read_real_aaa()

        # Re-dating every row one day EARLIER makes our d-1 slot hold what the
        # exchange calls day d, which is exactly the previous-day signature.
        earlier = self._write_aaa(
            tmp_path / "earlier", [(d - timedelta(days=1), v, q) for d, v, q in rows]
        )
        shifted = gb.aaa_vs_kalshi_crosscheck(gas_dir=str(earlier))
        assert shifted.prev_day > shifted.same_day, (
            f"after re-dating every row a day earlier the previous-day column "
            f"holds {shifted.prev_day} and same-day still holds "
            f"{shifted.same_day}; the column does not measure what it claims"
        )
        # Most of the mass must move, not merely a plurality. An exact count is
        # not assertable: re-dating also changes which pinned dates have a row at
        # the series boundaries, so a handful legitimately fall out.
        assert shifted.prev_day >= 0.75 * real.same_day, (
            f"only {shifted.prev_day} of {real.same_day} same-day matches "
            f"reappeared as previous-day matches"
        )
        # The containment check must independently notice the same shift.
        assert shifted.outside > 0
        assert shifted.containment_ok is False

        # And a shift the previous-day column cannot absorb must land in
        # `neither` rather than being quietly counted as agreement.
        later = self._write_aaa(
            tmp_path / "later", [(d + timedelta(days=2), v, q) for d, v, q in rows]
        )
        unattributable = gb.aaa_vs_kalshi_crosscheck(gas_dir=str(later))
        assert unattributable.neither > real.neither, (
            "a two-day shift produced no unattributable settlements, so the "
            "`neither` column cannot detect a misalignment either"
        )
        assert unattributable.attribution_ok is False

    def test_a_poisoned_value_is_caught_as_outside_the_interval(self, tmp_path):
        """Perturb one value on a pinned date; containment must go red."""
        rows = self._read_real_aaa()
        pinned = gb.load_pinned_truth()
        target = next(
            r.settlement_date
            for r in pinned
            if any(d == r.settlement_date for d, _v, _q in rows)
        )
        poisoned = [(d, (v + 1.0 if d == target else v), q) for d, v, q in rows]
        path = self._write_aaa(tmp_path, poisoned)
        xc = gb.aaa_vs_kalshi_crosscheck(gas_dir=str(path))
        assert xc.outside >= 1, "a $1.00 error on a pinned date was not detected"
        assert xc.containment_ok is False
        assert any(target.isoformat() in d for d in xc.outside_detail)

    def test_a_removed_row_is_reported_not_silently_dropped(self, tmp_path):
        rows = self._read_real_aaa()
        pinned = gb.load_pinned_truth()
        present = {d for d, _v, _q in rows}
        target = next(r.settlement_date for r in pinned if r.settlement_date in present)
        path = self._write_aaa(tmp_path, [(d, v, q) for d, v, q in rows if d != target])
        xc = gb.aaa_vs_kalshi_crosscheck(gas_dir=str(path))
        real = gb.aaa_vs_kalshi_crosscheck()
        assert xc.no_aaa_row > real.no_aaa_row
        assert target.isoformat() in xc.no_row_dates
        # A missing row must not be counted as agreement.
        assert xc.rows_with_aaa < real.rows_with_aaa

    @staticmethod
    def _read_real_aaa():
        out = []
        path = os.path.join(gb.GAS_TRUTH_DIR, "aaa_daily_national.csv")
        with open(path, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if not (row.get("date") or "").strip():
                    continue
                out.append(
                    (
                        date.fromisoformat(row["date"]),
                        float(row["value"]),
                        (row.get("quality") or "ok"),
                    )
                )
        return out

    @staticmethod
    def _write_aaa(tmp_path, rows):
        d = tmp_path / "gas_truth"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "aaa_daily_national.csv"
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            w = csv.writer(handle, lineterminator="\n")
            w.writerow(
                [
                    "date",
                    "value",
                    "source",
                    "source_url",
                    "fetched_at",
                    "raw_sha256",
                    "quality",
                ]
            )
            for dt, v, q in sorted(rows):
                w.writerow([dt.isoformat(), f"{v:.3f}", "t", "t", "t", "t", q])
        return d


class TestDeferralRegister:
    """The register must close items, and cross-references must never drift.

    The failure this guards is real and already happened once: WS-A extended the
    AAA backfill from 14 months to 4.5 years, the computed tables updated, and
    three blocks of hardcoded prose did not — the header still said the backfill
    was in flight, §9 asked for an extension that had landed, and §10 still
    listed a satisfied clause as an open deferral. A register that under-reports
    completion is wrong in the direction that matters, because a reader
    reconciling it against §1 cannot tell which section is authoritative.
    """

    def test_a_satisfied_clause_is_recorded_closed_not_deleted(self):
        reg = gb.DeferralRegister()
        reg.close("k", "an item", "the evidence that closed it")
        assert reg.n_closed == 1
        assert reg.n_open == 0
        md = reg.markdown()
        assert "**CLOSED**" in md
        assert "the evidence that closed it" in md
        assert "an item" in md, "a closed item must stay visible, not vanish"

    def test_cross_references_resolve_through_the_register(self):
        reg = gb.DeferralRegister()
        reg.add("first", "i1", "r1", "c1")
        reg.add("second", "i2", "r2", "c2")
        assert reg.ref("first") == "§10.1"
        assert reg.ref("second") == "§10.2"
        # An unregistered key degrades to the section, never to a wrong number.
        assert reg.ref("nope") == "§10"

    def test_numbering_shifts_and_references_follow(self):
        """Dropping an earlier item must not leave a later reference stale."""
        with_first = gb.DeferralRegister()
        with_first.add("a", "i", "r", "c")
        with_first.add("b", "i", "r", "c")
        without_first = gb.DeferralRegister()
        without_first.add("b", "i", "r", "c")
        assert with_first.ref("b") == "§10.2"
        assert without_first.ref("b") == "§10.1"

    def test_open_and_closed_counts_match_the_rendered_rows(self):
        reg = gb.DeferralRegister()
        reg.close("a", "closed item", "evidence")
        reg.add("b", "open item", "reason", "remedy")
        reg.add("c", "another open", "reason", "remedy")
        md = reg.markdown()
        assert md.count("**CLOSED**") == reg.n_closed == 1
        assert md.count("| OPEN |") == reg.n_open == 2

    @pytest.mark.parametrize(
        "month_ends,expect_open",
        [
            (0, True),
            (gb.REQUIRED_MONTH_ENDS - 1, True),
            (gb.REQUIRED_MONTH_ENDS, False),
            (gb.REQUIRED_MONTH_ENDS + 31, False),
        ],
    )
    def test_month_end_item_opens_and_closes_on_the_measured_count(
        self, month_ends, expect_open
    ):
        """The switch is the data, not a hardcoded belief about the data."""
        head = _stub_axis_result(month_ends)
        reg = gb._build_register(
            head,
            {"inputs": {"tape": {}}},
            month_ends,
            month_ends >= gb.REQUIRED_MONTH_ENDS,
        )
        assert reg.status("month_ends") is expect_open
        md = reg.markdown()
        if expect_open:
            assert "a longer Wayback backfill" in md
        else:
            assert (
                "a longer Wayback backfill" not in md
            ), "a remedy for an already-satisfied clause is a stale demand"
            assert f"{month_ends} month-ends held out" in md

    def test_the_freshness_gate_the_report_describes_is_the_one_it_applies(self):
        """The prose, the replay's pre-filter and the live strategy agree.

        Restating ``2`` in three places is how a report ends up describing a gate
        the bot no longer applies, so the constant is read from the strategy.
        """
        from src.strategies.gas_convergence import GasConvergenceStrategy

        assert (
            gb.MAX_DATA_AGE_DAYS
            == GasConvergenceStrategy(series=None).max_data_age_days
        )

    def test_items_the_backfill_cannot_fix_stay_open_regardless(self):
        """Kalshi's ~2-month pruning is not addressed by more AAA history."""
        spans = (0, gb.REQUIRED_MONTH_ENDS * 100)
        assert spans, "no month-end counts to exercise"
        for month_ends in spans:
            reg = gb._build_register(
                _stub_axis_result(month_ends),
                {"inputs": {"tape": {}}},
                month_ends,
                month_ends >= gb.REQUIRED_MONTH_ENDS,
            )
            assert reg.status("monthly_events") is True
            assert reg.status("intra_hour") is True
            assert reg.status("rbob_exante") is True


def _stub_axis_result(month_ends: int) -> gb.AxisResult:
    """Minimal AxisResult for register tests — no fitting, no tape."""
    series = _series(n_days=400)
    spec = _spec_from_series(series)
    empty = gb.EvRun()
    return gb.AxisResult(
        mae_fits=0,
        mae_aborts={},
        axis=gb.HEADLINE_AXIS,
        spec=spec,
        config=ProjectionConfig(),
        mae_rows=[],
        mae_by_lead={},
        mae_overall={},
        daily_rows=[],
        daily_by_lead={},
        daily_overall={},
        month_ends_held_out=month_ends,
        monthly=empty,
        weekly=empty,
        accepted_taker=gb.accepted_summary([], "taker"),
        accepted_maker=gb.accepted_summary([], "maker"),
    )


class TestArtifactTellsOneStory:
    """Extract the same claim from every section and diff them.

    ``one-decision-record-for-cross-document-state``: when several sections each
    publish the same shared state they drift apart without any single edit being
    wrong, so a separate pass has to pull the claim out of each and compare.
    """

    @staticmethod
    def _artifact() -> str:
        """The committed artifact. Absence is a failure, never a skip.

        The dated report is this workstream's deliverable and the evidence the
        phase criterion rests on. A test that skips when it is missing is a green
        that cannot fail, and this file already shipped one of those.
        """
        matches = sorted(glob.glob(os.path.join(gb.PHASE4_DIR, "phase4_backtest_*.md")))
        assert matches, (
            "no phase4_backtest_<date>.md in reports/phase4/. This artifact is "
            "the deliverable, not an optional input: run "
            "`python scripts/gas_backtest.py run`."
        )
        return open(matches[-1], encoding="utf-8").read()

    def test_month_end_clause_status_agrees_across_every_section(self):
        text = self._artifact()
        m = re.search(r"\*\*Month-ends held out: (\d+)\.\*\*", text)
        assert m, "§3.1 does not publish a month-end count"
        count = int(m.group(1))
        met = count >= gb.REQUIRED_MONTH_ENDS

        banner = re.search(
            r"That span yields \*\*(\d+) held-out month-ends\*\*.*?"
            r"the clause is \*\*(MET|NOT MET)\*\*",
            text,
            re.S,
        )
        assert banner, "the header banner does not publish the clause status"
        assert int(banner.group(1)) == count
        assert (banner.group(2) == "MET") is met

        row = re.search(
            r"\| month-end MAE on >=\d+ held-out month-ends \| §3\.1 \| ([^|]+)\|",
            text,
        )
        assert row, "§1 has no month-end clause row"
        cell = row.group(1)
        assert f"**{count} month-ends**" in cell
        assert ("**MET**" in cell) is met
        assert ("**NOT MET**" in cell) is not met

        assert f"The criterion asks for >= {gb.REQUIRED_MONTH_ENDS}" in text

    def test_the_register_does_not_contradict_section_1(self):
        text = self._artifact()
        count = int(re.search(r"\*\*Month-ends held out: (\d+)\.\*\*", text).group(1))
        met = count >= gb.REQUIRED_MONTH_ENDS
        # Find the register row for the month-end item.
        rows = [
            line
            for line in text.splitlines()
            if line.startswith("| 10.") and "held-out month-ends" in line
        ]
        assert (
            len(rows) == 1
        ), f"expected exactly one month-end register row, got {rows}"
        if met:
            assert (
                "**CLOSED**" in rows[0]
            ), "§1 says the clause is MET but the register still lists it OPEN"
        else:
            assert "| OPEN |" in rows[0]

    def test_the_backfill_demand_appears_exactly_when_it_is_warranted(self):
        """Bidirectional: absent when the clause is met, present when it is not.

        Checking only the "absent when met" direction would leave the guard
        untested on a short backfill, and a guard tested in one direction only
        may be blocking everything. So this asserts both, and it does not skip.
        """
        text = self._artifact()
        count = int(re.search(r"\*\*Month-ends held out: (\d+)\.\*\*", text).group(1))
        met = count >= gb.REQUIRED_MONTH_ENDS
        demand = "a longer Wayback backfill"
        if met:
            assert demand not in text, (
                f"{count} month-ends satisfies the clause, but the artifact still "
                f"asks for a longer backfill"
            )
        else:
            assert demand in text, (
                f"only {count} month-ends, so §9/§10 must still ask for a longer "
                f"backfill — silently dropping the ask would under-report the gap"
            )

        # The four literal strings that drifted when WS-A extended the backfill.
        # Each was true of an earlier input and false of this one; none may
        # survive a regeneration that satisfied the clause.
        stale = (
            "was still running when this was generated",
            "would make this table meaningful",
            "this artifact must be regenerated on it",
        )
        checked = 0
        for phrase in stale:
            checked += 1
            assert phrase not in text, f"stale prose survived regeneration: {phrase!r}"
        assert checked == len(stale)

    def test_the_json_companion_publishes_the_same_claim_as_the_markdown(self):
        """Two files publishing one shared state is exactly how drift starts.

        The markdown is the artifact of record and the JSON is what a red-team
        greps. If they can disagree, one of them is wrong and nothing says which.

        **This test must never skip.** Both files are written by the same run, so
        a missing file or a missing key is a defect in the generator, not an
        absent optional input. An earlier revision guarded the block with
        ``pytest.skip("JSON predates the month_ends/deferrals block")``, which
        made it a green that could not fail — and it stayed green while I reported
        the JSON block as delivered when only the source had it. The guard is
        gone deliberately; do not restore it.
        """
        import json as _json

        md_matches = sorted(
            glob.glob(os.path.join(gb.PHASE4_DIR, "phase4_backtest_*.md"))
        )
        js_matches = sorted(
            glob.glob(os.path.join(gb.PHASE4_DIR, "phase4_backtest_data_*.json"))
        )
        assert md_matches, "no markdown artifact; run `gas_backtest.py run`"
        assert js_matches, "no JSON companion; run `gas_backtest.py run`"
        text = open(md_matches[-1], encoding="utf-8").read()
        data = _json.load(open(js_matches[-1], encoding="utf-8"))
        for key in ("month_ends", "deferrals"):
            assert key in data, (
                f"the JSON companion has no {key!r} block. The markdown renders "
                f"this claim, so the JSON must serialise the same object — see "
                f"`RenderedClaims` and `_cmd_run`. Missing keys are a generator "
                f"defect, not a reason to skip."
            )

        count = int(re.search(r"\*\*Month-ends held out: (\d+)\.\*\*", text).group(1))
        assert data["month_ends"]["held_out"] == count
        assert data["month_ends"]["required"] == gb.REQUIRED_MONTH_ENDS
        assert data["month_ends"]["clause_met"] is (count >= gb.REQUIRED_MONTH_ENDS)

        claim = re.search(r"\*\*(\d+) open, (\d+) closed\.\*\*", text)
        assert claim, "§10 does not publish its open/closed counts"
        assert data["deferrals"]["n_open"] == int(claim.group(1))
        assert data["deferrals"]["n_closed"] == int(claim.group(2))
        assert data["deferrals"]["items"], "the register serialised zero items"
        for item in data["deferrals"]["items"]:
            row = [
                line
                for line in text.splitlines()
                if line.startswith(f"| {item['n']} |")
            ]
            assert len(row) == 1, f"no unique §{item['n']} row in the markdown"
            expected = "**CLOSED**" if item["status"] == "closed" else "| OPEN |"
            assert expected in row[0], (
                f"§{item['n']} is {item['status']} in the JSON but the markdown "
                f"row says otherwise: {row[0][:120]}"
            )

    def test_the_crosscheck_is_measured_by_the_run_not_quoted_from_a_message(self):
        """Lock out the reintroduction of a hardcoded cross-validation figure.

        §3.3.1 once read "The orchestrator cross-validated the two: **77/77** AAA
        rows fall inside the Kalshi-pinned interval". That number came from a
        message, could not fail, and went stale the moment the `2026-07-28` row
        was removed. The artifact must now render what the run measured, and the
        rendered figures must equal a fresh recompute.
        """
        text = self._artifact()
        for banned in (
            "The orchestrator cross-validated",
            "77/77",
            "the deviation the orchestrator measured",
        ):
            assert banned not in text, (
                f"a quoted cross-validation figure is back in the artifact: "
                f"{banned!r}. §3.3.1 must render `aaa_vs_kalshi_crosscheck()`."
            )
        xc = gb.aaa_vs_kalshi_crosscheck()
        assert f"**{xc.inside} of {xc.rows_with_aaa}**" in text, (
            "the artifact does not render the containment figures this run " "measures"
        )
        assert f"**${xc.max_deviation:.4f}**" in text
        assert f"**{xc.same_day}**" in text
        assert "attributed to the run" not in text  # no hand-waving substitute

    def test_the_json_companion_carries_the_measured_crosscheck(self):
        import json as _json

        matches = sorted(
            glob.glob(os.path.join(gb.PHASE4_DIR, "phase4_backtest_data_*.json"))
        )
        assert matches, "no JSON companion; run `gas_backtest.py run`"
        data = _json.load(open(matches[-1], encoding="utf-8"))
        assert (
            "aaa_vs_kalshi_crosscheck" in data
        ), "the JSON companion has no aaa_vs_kalshi_crosscheck block"
        block = data["aaa_vs_kalshi_crosscheck"]
        xc = gb.aaa_vs_kalshi_crosscheck()
        for field_name in (
            "inside",
            "outside",
            "no_aaa_row",
            "same_day",
            "prev_day",
            "neither",
        ):
            assert block[field_name] == getattr(xc, field_name), (
                f"JSON {field_name}={block[field_name]} but a fresh recompute "
                f"gives {getattr(xc, field_name)}"
            )

    def test_register_open_closed_counts_match_its_own_rows(self):
        text = self._artifact()
        claim = re.search(r"\*\*(\d+) open, (\d+) closed\.\*\*", text)
        assert claim, "§10 does not publish its own open/closed counts"
        rows = [line for line in text.splitlines() if line.startswith("| 10.")]
        assert rows, "the register rendered no rows at all"
        assert sum(1 for r in rows if "| OPEN |" in r) == int(claim.group(1))
        assert sum(1 for r in rows if "**CLOSED**" in r) == int(claim.group(2))
        assert int(claim.group(1)) + int(claim.group(2)) == len(rows)

    def test_every_internal_section_reference_resolves(self):
        """No §N.M citation may point at a heading the artifact does not have.

        Citations written ``contract §1.1`` are references into
        ``docs/phase4_data_contract.md`` and are deliberately excluded — the
        prefix is what makes them unambiguous, so the test requires it rather
        than guessing.
        """
        text = self._artifact()
        headings = set()
        for line in text.splitlines():
            h = re.match(r"#{2,3} (\d+(?:\.\d+)?)\.? ", line)
            if h:
                headings.add(h.group(1))
        # The register renders its numbers in a table column, not a heading.
        headings.update(
            re.match(r"\| (10\.\d+) \|", line).group(1)
            for line in text.splitlines()
            if re.match(r"\| 10\.\d+ \|", line)
        )
        internal = re.findall(r"(?<!contract )§(\d+(?:\.\d+)?)", text)
        assert len(internal) > 20, (
            f"only {len(internal)} internal citations found; the regex is "
            f"probably not matching and this test is vacuous"
        )
        dangling = {c for c in internal if c not in headings}
        assert not dangling, (
            f"citations with no target in this artifact: {sorted(dangling)}. "
            f"A reference into the data contract must be written `contract §N`."
        )

    def test_the_data_span_is_described_consistently(self):
        """The banner, §2.1 and §3.1 must agree on the AAA span and row count."""
        text = self._artifact()
        banner = re.search(
            r"AAA daily national average `(\d{4}-\d\d-\d\d)` \.\. "
            r"`(\d{4}-\d\d-\d\d)` — (\d+) days, [\d.]+ yr — (\d+) usable rows "
            r"\((\d+) `suspect` excluded, (\d+) missing",
            text,
        )
        assert banner, "the header banner does not publish the span"
        first, last, days, rows, suspect, missing = banner.groups()
        table = re.search(
            r"\| AAA daily national \| `data/gas_truth/aaa_daily_national\.csv` \| "
            r"(\d+) usable \| (\d{4}-\d\d-\d\d) \.\. (\d{4}-\d\d-\d\d) \((\d+) d\) \| "
            r"(\d+) rows flagged",
            text,
        )
        assert table, "§2.1 does not publish the span in the expected shape"
        assert table.group(1) == rows
        assert table.group(2) == first
        assert table.group(3) == last
        assert table.group(4) == days
        assert table.group(5) == suspect
        assert f"{missing} calendar days inside the span have no row" in text

        # Anywhere the prose names the AAA start date, it must name this one.
        # This is the assertion that would have caught the real drift: §9 once
        # said "AAA starting <old date>" while §2.1 already showed the new span.
        mentions = 0
        for phrase in (
            r"the series starts (\d{4}-\d\d-\d\d)",
            r"AAA (?:currently )?starts (\d{4}-\d\d-\d\d)",
            r"AAA starting (\d{4}-\d\d-\d\d)",
            r"AAA series starts (\d{4}-\d\d-\d\d)",
            r"backfill starts (\d{4}-\d\d-\d\d)",
        ):
            for found in re.findall(phrase, text):
                mentions += 1
                assert (
                    found == first
                ), f"prose names AAA start {found} but the input starts {first}"
        assert mentions, (
            "no prose in the artifact names the AAA start date, so this test "
            "cannot catch the drift it exists to catch"
        )
