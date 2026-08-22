"""The trade journal must record settlement provenance (Phase 1 exit 5).

Exit criterion 5 requires a settled weather position to be verified "in
exchange state **and journal**". Before this file the journal recorded only::

    {... "exit_price": 1.0, "close_reason": "EXPIRATION", "strike": null ...}

and silently dropped every field that says *what the position settled against*
and *under which rule*::

    settlement_high, settlement_spec, settlement_rule, settlement_outcome,
    strike_type, floor_strike, cap_strike

That is the join key FR-1.3 needs: without ``settlement_high`` on the row there
is no way to reconcile sim settlement against the IEM CLI daily high, and
without the bracket spec there is no way to recompute the outcome at all
(the ticker must never be re-parsed for direction — PRD FR-1.1).

Coverage:
1.  a settled weather position round-trips through the JSONL for all three
    ``strike_type`` values, with the daily high and the bracket spec intact;
2.  the row replays through ``bracket_payoff`` — outcome recomputable, and a
    truth mismatch is detectable (the FR-1.3 reconcile in miniature);
3.  backward compatibility: pre-Phase-1 rows (which lack every new field) still
    load, mixed old/new files load, and no absent strike is coerced to 0.0;
4.  a static contract check that the producer (``SimulatedExchange``) and the
    consumer (``TradeOutcome.from_position``) still agree on the key names;
5.  ``is_settlement_consistent`` routes each market kind through its OWN payoff
    rule -- gas through ``value > floor_strike`` (PRD FR-4.4), temperature
    through the bracket rule -- and neither can be re-crossed by a refactor.

Run: $env:PYTHONPATH="."; python -m pytest tests/test_journal_settlement_fields.py -v
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.bracket_payoff import (  # noqa: E402
    STRIKE_TYPE_BETWEEN,
    STRIKE_TYPE_GREATER,
    STRIKE_TYPE_LESS,
    BracketSpec,
    BracketSpecError,
    settles_yes,
)
from src.ml.trade_journal import TradeJournal, TradeOutcome  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MATCHING_ENGINE = os.path.join(REPO_ROOT, "src", "core", "matching_engine.py")

# The fields the 2026-07-25 red-team measured as DROPPED by the journal.
DROPPED_FIELDS = (
    "settlement_high",
    "settlement_spec",
    "settlement_rule",
    "settlement_outcome",
    "strike_type",
    "floor_strike",
    "cap_strike",
)


# ===========================================================================
# Fixtures: closed positions shaped exactly like SimulatedExchange's
# ===========================================================================

# Live-probed contracts (api.elections.kalshi.com, 2026-07-25) — the same
# three the FR-1.2 golden table pins.
SETTLED_CASES = [
    pytest.param(
        BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86.0, 87.0),
        86.0,
        "yes",
        id="between-B86.5-high86",
    ),
    pytest.param(
        BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87.0, None),
        87.0,
        "no",  # floor does NOT pay: "88 or above"
        id="greater-T87-high87",
    ),
    pytest.param(
        BracketSpec("KXHIGHNY-26JUL25-T80", STRIKE_TYPE_LESS, None, 80.0),
        74.0,
        "yes",  # "79 or below"
        id="less-T80-high74",
    ),
]


def _closed_position(spec: BracketSpec, high: float, strategy="Meteorologist V2"):
    """A closed weather position, keyed exactly as ``SimulatedExchange`` builds it.

    ``open_position`` caches the three bracket fields (FR-1.1); the settlement
    path (``_weather_exit_price``) stamps the four ``settlement_*`` keys.
    Producers of both are pinned by
    :func:`test_matching_engine_stamps_every_field_the_journal_reads`.
    """
    outcome_yes = settles_yes(spec, high)
    now = datetime(2026, 7, 26, 4, 59, 0)
    pos = {
        "id": 4211,
        "symbol": spec.ticker,
        "side": "buy",
        "contract_side": "YES",
        "entry_price": 0.42,
        "exit_price": 1.0 if outcome_yes else 0.0,
        "quantity": 50,
        "open_time": now - timedelta(hours=18),
        "close_time": now,
        "pnl": 29.0 if outcome_yes else -21.0,
        "reason": "EXPIRATION",
        "strategy_name": strategy,
        "strike": None,
        "ml_context": {"nws_forecast_high": 85.0},
    }
    # Cached at open (FR-1.1)
    pos.update(spec.as_dict())
    # Stamped at settlement (FR-1.2)
    pos["settlement_high"] = float(high)
    pos["settlement_spec"] = spec.as_dict()
    pos["settlement_rule"] = spec.describe()
    pos["settlement_outcome"] = "yes" if outcome_yes else "no"
    return pos


def _journal(tmp_path):
    return TradeJournal(path=str(tmp_path / "trade_journal.jsonl"))


def _rows(journal: TradeJournal):
    with io.open(journal.path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ===========================================================================
# 1. Settled weather positions round-trip with settlement provenance
# ===========================================================================


class TestSettledWeatherPositionRoundTrip:
    @pytest.mark.parametrize("spec,high,expected_outcome", SETTLED_CASES)
    def test_no_settlement_field_is_dropped(self, spec, high, expected_outcome):
        """The exact regression: none of the seven fields may vanish."""
        pos = _closed_position(spec, high)
        outcome = TradeOutcome.from_position(pos)

        dropped = [f for f in DROPPED_FIELDS if getattr(outcome, f, None) is None]
        # ``greater`` legitimately has no cap and ``less`` no floor — Kalshi
        # omits the irrelevant strike, and coercing it to 0.0 is forbidden.
        legitimately_absent = {
            STRIKE_TYPE_GREATER: {"cap_strike"},
            STRIKE_TYPE_LESS: {"floor_strike"},
            STRIKE_TYPE_BETWEEN: set(),
        }[spec.strike_type]
        assert (
            set(dropped) <= legitimately_absent
        ), f"{spec.ticker}: journal dropped {sorted(set(dropped) - legitimately_absent)}"

    @pytest.mark.parametrize("spec,high,expected_outcome", SETTLED_CASES)
    def test_written_jsonl_carries_high_and_spec(
        self, tmp_path, spec, high, expected_outcome
    ):
        journal = _journal(tmp_path)
        journal.record(TradeOutcome.from_position(_closed_position(spec, high)))

        (row,) = _rows(journal)
        assert row["settlement_high"] == pytest.approx(high)
        assert row["settlement_rule"] == spec.describe()
        assert row["settlement_outcome"] == expected_outcome
        assert row["settlement_spec"] == spec.as_dict()
        assert row["strike_type"] == spec.strike_type
        assert row["floor_strike"] == spec.floor_strike
        assert row["cap_strike"] == spec.cap_strike
        # Pre-existing columns are untouched.
        assert row["close_reason"] == "EXPIRATION"
        assert row["symbol"] == spec.ticker
        assert row["nws_forecast_high"] == 85.0

    @pytest.mark.parametrize("spec,high,expected_outcome", SETTLED_CASES)
    def test_loaded_outcome_replays_through_bracket_payoff(
        self, tmp_path, spec, high, expected_outcome
    ):
        """A journal row alone is enough to recompute the settlement."""
        journal = _journal(tmp_path)
        journal.record(TradeOutcome.from_position(_closed_position(spec, high)))

        (loaded,) = journal.load_all()
        rebuilt = loaded.bracket_spec()
        assert rebuilt.strike_type == spec.strike_type
        assert rebuilt.floor_strike == spec.floor_strike
        assert rebuilt.cap_strike == spec.cap_strike
        assert rebuilt.describe() == spec.describe()
        assert loaded.settlement_high == pytest.approx(high)
        assert loaded.is_settlement_consistent() is True

    def test_reconcile_against_external_truth_detects_a_mismatch(self, tmp_path):
        """FR-1.3 in miniature: journal row vs IEM CLI truth.

        This is the whole point of the added fields — the sim settled a
        position against 86F; if IEM's CLI high for that day is 88F the row
        must be flagged, and it can only be flagged because the high AND the
        bracket both live on the row.
        """
        spec = BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86.0, 87.0)
        journal = _journal(tmp_path)
        journal.record(TradeOutcome.from_position(_closed_position(spec, 86.0)))
        (loaded,) = journal.load_all()

        assert loaded.is_settlement_consistent(truth_high=86.0) is True
        assert loaded.is_settlement_consistent(truth_high=87.0) is True  # same bracket
        # IEM says 88F: the sim's "yes" is wrong, and the row proves it.
        assert loaded.is_settlement_consistent(truth_high=88.0) is False
        assert loaded.settlement_high != 88.0

    def test_unresolved_settlement_records_why(self, tmp_path):
        """A SETTLEMENT_UNRESOLVED close keeps its reason in the journal."""
        pos = {
            "symbol": "KXHIGHNY-26JUL25-B86.5",
            "strategy_name": "Meteorologist V2",
            "entry_price": 0.42,
            "exit_price": 0.42,
            "quantity": 50,
            "pnl": 0.0,
            "reason": "SETTLEMENT_UNRESOLVED",
            "settlement_error": "strike_type absent from API fields",
        }
        journal = _journal(tmp_path)
        journal.record(TradeOutcome.from_position(pos))
        (loaded,) = journal.load_all()

        assert loaded.close_reason == "SETTLEMENT_UNRESOLVED"
        assert "strike_type absent" in (loaded.settlement_error or "")
        assert loaded.settlement_high is None
        assert loaded.is_settlement_consistent() is False
        with pytest.raises(BracketSpecError):
            loaded.bracket_spec()


# ===========================================================================
# 2. Absent strikes are never coerced
# ===========================================================================


class TestMissingStrikesStayMissing:
    def test_greater_position_keeps_cap_none(self):
        spec = BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87.0, None)
        outcome = TradeOutcome.from_position(_closed_position(spec, 90.0))
        assert outcome.cap_strike is None, "None cap must not become 0.0"
        assert outcome.floor_strike == 87.0
        assert settles_yes(outcome.bracket_spec(), 90.0) is True

    def test_less_position_keeps_floor_none(self):
        spec = BracketSpec("KXHIGHNY-26JUL25-T80", STRIKE_TYPE_LESS, None, 80.0)
        outcome = TradeOutcome.from_position(_closed_position(spec, 74.0))
        assert outcome.floor_strike is None
        assert outcome.cap_strike == 80.0

    def test_bracket_fields_fall_back_to_the_open_time_cache(self):
        """A weather position closed WITHOUT settling still records its bracket.

        ``settlement_spec`` only exists once the position settles; the three
        FR-1.1 fields cached at open time must still reach the journal so an
        early close is attributable to a bracket.
        """
        pos = {
            "symbol": "KXHIGHCHI-26JUL25-T80",
            "strategy_name": "ML Weather",
            "entry_price": 0.30,
            "exit_price": 0.44,
            "quantity": 50,
            "pnl": 7.0,
            "reason": "TAKE_PROFIT",
            "strike_type": "less",
            "floor_strike": None,
            "cap_strike": 80.0,
        }
        outcome = TradeOutcome.from_position(pos)
        assert outcome.strike_type == "less"
        assert outcome.cap_strike == 80.0
        assert outcome.settlement_spec is None
        assert outcome.settlement_high is None


# ===========================================================================
# 3. Backward compatibility with existing data/trade_journal.jsonl rows
# ===========================================================================

# A verbatim pre-Phase-1 row shape (crypto era): none of the new fields,
# plus a key the dataclass has never had.
LEGACY_ROW = {
    "symbol": "KXBTCD-26MAY20-T104000",
    "strategy_name": "ML BTC 15m",
    "entry_time": "2026-05-20T14:02:11.100000",
    "exit_time": "2026-05-20T14:15:00",
    "entry_price": 0.37,
    "exit_price": 0.0,
    "quantity": 10.0,
    "side": "buy",
    "contract_side": "YES",
    "pnl": -3.7,
    "close_reason": "EXPIRATION",
    "model_probability": 0.51,
    "model_confidence": 0.62,
    "model_used": "lgbm_btc15m_v3",
    "strike": 104000.0,
    "tte_at_entry": 12.8,
    "btc_spot_at_entry": 103880.0,
    "prediction_correct": False,
    "edge_at_entry": 0.14,
    "legacy_only_column": "ignored",
}


class TestBackwardCompatibility:
    def test_old_format_row_still_loads(self, tmp_path):
        journal = _journal(tmp_path)
        with io.open(journal.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(LEGACY_ROW) + "\n")

        (loaded,) = TradeJournal(path=str(journal.path)).load_all()

        assert loaded.symbol == LEGACY_ROW["symbol"]
        assert loaded.pnl == pytest.approx(-3.7)
        assert loaded.strike == 104000.0
        # Every new field defaults cleanly — no KeyError, no crash.
        for field_name in DROPPED_FIELDS + ("settlement_error",):
            assert getattr(loaded, field_name) is None
        assert not hasattr(loaded, "legacy_only_column")

    def test_mixed_old_and_new_rows_load_together(self, tmp_path):
        spec = BracketSpec("KXHIGHNY-26JUL25-T80", STRIKE_TYPE_LESS, None, 80.0)
        journal = _journal(tmp_path)
        with io.open(journal.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(LEGACY_ROW) + "\n")
        journal = TradeJournal(path=str(journal.path))
        journal.record(TradeOutcome.from_position(_closed_position(spec, 74.0)))

        loaded = journal.load_all()
        assert len(loaded) == 2
        assert loaded[0].settlement_high is None  # legacy
        assert loaded[1].settlement_high == 74.0  # new
        assert journal.get_sample_count() == 2

    def test_existing_analytics_still_work_over_mixed_rows(self, tmp_path):
        """``analyze_losses``/``win_rate_by_feature`` must not care."""
        spec = BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87.0, None)
        journal = _journal(tmp_path)
        with io.open(journal.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(LEGACY_ROW) + "\n")
        journal = TradeJournal(path=str(journal.path))
        journal.record(TradeOutcome.from_position(_closed_position(spec, 87.0)))

        losses = journal.analyze_losses()
        assert losses["reason_breakdown"]["EXPIRATION"] == 2
        assert "by_confidence" in journal.win_rate_by_feature()

    def test_the_real_journal_on_disk_still_loads(self):
        """Guard against breaking the operator's existing file."""
        path = os.path.join(REPO_ROOT, "data", "trade_journal.jsonl")
        if not os.path.exists(path):
            pytest.skip("no data/trade_journal.jsonl in this checkout")
        with io.open(path, encoding="utf-8") as fh:
            total = sum(1 for line in fh if line.strip())
        loaded = TradeJournal(path=path).load_all()
        assert len(loaded) == total, (
            f"{total - len(loaded)} of {total} existing journal rows failed to "
            f"load after the settlement-field change"
        )


# ===========================================================================
# 4. Producer/consumer contract: the key names must not drift apart
# ===========================================================================


def _position_keys_assigned_in(path):
    """``{k}`` for every ``pos["k"] = ...`` / ``position["k"] = ...`` in a file."""
    tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id in ("pos", "position")
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                keys.add(target.slice.value)
    return keys


def test_matching_engine_stamps_every_field_the_journal_reads():
    """The exchange writes the keys ``from_position`` looks for.

    A rename on either side silently reopens the finding (the journal would
    go back to recording ``exit_price=1.0`` and nothing else), and no runtime
    test would catch it because ``dict.get`` returns None quietly.
    """
    assigned = _position_keys_assigned_in(MATCHING_ENGINE)
    required = {
        "settlement_high",
        "settlement_spec",
        "settlement_rule",
        "settlement_outcome",
        "settlement_error",
    }
    missing = required - assigned
    assert not missing, (
        f"src/core/matching_engine.py no longer stamps {sorted(missing)} onto a "
        f"settling position; TradeOutcome.from_position reads those keys"
    )


# ===========================================================================
# 5. is_settlement_consistent routes by market kind, never by unit-blind guess
# ===========================================================================
#
# The 2026-07-30 red-team finding (D8): the method was temperature-only, so a
# CORRECTLY settled gas row read as inconsistent both with and without an
# explicit truth --
#
#     settlement_value=4.75, floor_strike=4.60, outcome="yes"
#     is_settlement_consistent()     -> False   (truth: 4.75 > 4.60 -> yes)
#     is_settlement_consistent(4.75) -> False   (temperature "greater" wants
#                                                floor + 1, i.e. $5.60)
#
# No production caller existed at the time, which is exactly why it needed
# pinning: Phase 5's capital gate reads this method, and every good gas row
# would have tripped its phantom-PnL check.


def _gas_position(floor: float, value: float, ticker=None, strategy="Gas Convergence"):
    """A settled AAA gas position, keyed as the exchange builds one.

    Note ``settlement_value`` (USD/gal), never ``settlement_high`` (F) -- the
    contract keeps them in separate columns precisely so a reconcile cannot
    join a gas row to a temperature truth table.
    """
    from src.data.gas_settlement import GasSpec, expected_result

    spec = GasSpec(ticker or f"KXAAAGASM-26AUG31-{floor:.2f}", "greater", floor, None)
    outcome = expected_result(spec, value)
    now = datetime(2026, 8, 31, 14, 0, 0)
    pos = {
        "id": 7714,
        "symbol": spec.ticker,
        "side": "buy",
        "contract_side": "YES",
        "entry_price": 0.38,
        "exit_price": 1.0 if outcome == "yes" else 0.0,
        "quantity": 40,
        "open_time": now - timedelta(days=3),
        "close_time": now,
        "pnl": 24.8 if outcome == "yes" else -15.2,
        "reason": "EXPIRATION",
        "strategy_name": strategy,
        "strike": None,
    }
    pos.update(spec.as_dict())
    pos["settlement_value"] = float(value)
    pos["settlement_spec"] = spec.as_dict()
    pos["settlement_rule"] = spec.describe()
    pos["settlement_outcome"] = outcome
    return pos


class TestSettlementConsistencyRoutesByMarketKind:
    # (floor, settled value, expected outcome). The 4.60/4.75 row is the
    # red-team's verbatim repro; the two boundary rows are the whole ballgame
    # of FR-4.4 -- settle == strike pays NO, strictly.
    GAS_CASES = [
        pytest.param(4.60, 4.75, "yes", id="above-strike"),
        pytest.param(4.60, 4.60, "no", id="exactly-on-strike-pays-NO"),
        pytest.param(4.60, 4.599, "no", id="a-tenth-of-a-cent-below"),
        pytest.param(3.89, 3.847, "no", id="live-26JUN30-3.89"),
    ]

    @pytest.mark.parametrize("floor,value,expected", GAS_CASES)
    def test_a_correctly_settled_gas_row_reads_consistent(self, floor, value, expected):
        outcome = TradeOutcome.from_position(_gas_position(floor, value))
        assert outcome.settlement_outcome == expected
        assert outcome.settlement_value == pytest.approx(value)
        assert outcome.settlement_high is None, "a gas row must not carry a F high"
        assert outcome.is_settlement_consistent() is True
        assert outcome.is_settlement_consistent(value) is True
        assert outcome.is_settlement_consistent(truth_value=value) is True

    @pytest.mark.parametrize("floor,value,expected", GAS_CASES)
    def test_the_gas_row_never_routes_through_the_temperature_rule(
        self, floor, value, expected, monkeypatch
    ):
        """Make the temperature payoff explode; the gas row must still settle.

        This is the wire-crossing guard. ``bracket_payoff.settles_yes`` is the
        function the defect routed through, and the gas path must not touch it
        even though a gas ``greater`` spec is *parseable* by it.
        """
        import src.core.bracket_payoff as bp

        def _forbidden(*_a, **_k):
            raise AssertionError(
                "a gas row was routed through bracket_payoff.settles_yes -- the "
                "temperature 'greater' rule wants floor + 1, i.e. $5.60 for a "
                "$4.60 strike"
            )

        monkeypatch.setattr(bp, "settles_yes", _forbidden)
        outcome = TradeOutcome.from_position(_gas_position(floor, value))
        assert outcome.is_settlement_consistent() is True

    def test_a_wrongly_settled_gas_row_reads_inconsistent(self):
        """The gate must be able to fail: flip the recorded outcome."""
        pos = _gas_position(4.60, 4.75)
        pos["settlement_outcome"] = "no"  # truth says 4.75 > 4.60 -> yes
        outcome = TradeOutcome.from_position(pos)
        assert outcome.is_settlement_consistent() is False

    def test_external_gas_truth_disagreeing_with_the_sim_is_detected(self):
        """FR-4.4 in miniature: journal row vs the published AAA value.

        The sim settled on 4.75 and paid YES. If AAA's published value for that
        date is 4.55 the row is wrong, and the row alone proves it.
        """
        outcome = TradeOutcome.from_position(_gas_position(4.60, 4.75))
        assert outcome.is_settlement_consistent(truth_value=4.75) is True
        assert outcome.is_settlement_consistent(truth_value=4.61) is True  # same side
        assert outcome.is_settlement_consistent(truth_value=4.60) is False  # tie -> NO
        assert outcome.is_settlement_consistent(truth_value=4.55) is False

    def test_the_weather_control_still_routes_through_the_bracket_rule(self):
        """A temperature row must be unaffected -- and must not touch gas."""
        spec = BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86.0, 87.0)
        outcome = TradeOutcome.from_position(_closed_position(spec, 86.0))
        assert outcome.is_gas() is False
        assert outcome.is_settlement_consistent() is True
        assert outcome.is_settlement_consistent(86.0) is True
        assert outcome.is_settlement_consistent(truth_high=88.0) is False

    def test_the_weather_row_never_routes_through_the_gas_rule(self, monkeypatch):
        """The mirror guard: a bracket row must not reach the gas payoff.

        Without it, a "unify the two payoffs" refactor could settle every
        weather row under ``value > floor_strike`` -- which for the T87
        ``greater`` bracket would pay YES on a high of 87F, inverting the live
        result the FR-1.2 golden table pins.
        """
        import src.data.gas_settlement as gs

        def _forbidden(*_a, **_k):
            raise AssertionError(
                "a temperature row was routed through gas_settlement.settles_yes"
            )

        monkeypatch.setattr(gs, "settles_yes", _forbidden)
        monkeypatch.setattr(gs, "expected_result", _forbidden)
        spec = BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87.0, None)
        outcome = TradeOutcome.from_position(_closed_position(spec, 87.0))
        assert outcome.settlement_outcome == "no"  # "88 or above"
        assert outcome.is_settlement_consistent() is True

    def test_the_two_rules_genuinely_disagree_on_the_same_numbers(self):
        """Prove the routing matters rather than asserting it does.

        The same ``greater`` spec at floor 4.60 and the same value 4.75 give
        opposite answers under the two modules. If this ever stops being true
        the wire-crossing guards above become vacuous, so it is asserted
        directly.
        """
        from src.core.bracket_payoff import parse_bracket_spec
        from src.core.bracket_payoff import settles_yes as temperature_settles_yes
        from src.data.gas_settlement import parse_gas_spec
        from src.data.gas_settlement import settles_yes as gas_settles_yes

        fields = {"strike_type": "greater", "floor_strike": 4.60, "cap_strike": None}
        assert gas_settles_yes(parse_gas_spec("KXAAAGASM-26AUG31-4.60", fields), 4.75)
        assert not temperature_settles_yes(
            parse_bracket_spec("KXAAAGASM-26AUG31-4.60", fields), 4.75
        )
        # ...and the temperature rule needs floor + 1 to flip, i.e. $5.60/gal.
        assert temperature_settles_yes(
            parse_bracket_spec("KXAAAGASM-26AUG31-4.60", fields), 5.60
        )

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            pytest.param(
                {"truth_high": 4.75}, "temperature", id="gas-given-truth_high"
            ),
            pytest.param(
                {"truth": 4.75, "truth_value": 4.75}, "at most one", id="two-truths"
            ),
        ],
    )
    def test_a_unit_crossed_argument_raises_rather_than_answering(self, kwargs, needle):
        outcome = TradeOutcome.from_position(_gas_position(4.60, 4.75))
        with pytest.raises(ValueError, match=needle):
            outcome.is_settlement_consistent(**kwargs)

    def test_a_temperature_row_given_the_gas_argument_raises(self):
        spec = BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86.0, 87.0)
        outcome = TradeOutcome.from_position(_closed_position(spec, 86.0))
        with pytest.raises(ValueError, match="USD/gal"):
            outcome.is_settlement_consistent(truth_value=86.0)

    def test_a_row_carrying_both_settlement_columns_is_ambiguous_and_raises(self):
        """ "Exactly one of the two is populated" is enforced, not just documented."""
        pos = _gas_position(4.60, 4.75)
        pos["settlement_high"] = 86.0  # units now ambiguous
        outcome = TradeOutcome.from_position(pos)
        with pytest.raises(ValueError, match="BOTH"):
            outcome.is_settlement_consistent()

    def test_an_unsettled_gas_row_is_not_consistent_and_says_why(self):
        from src.data.gas_settlement import GasSpecError

        pos = {
            "symbol": "KXAAAGASM-26AUG31-4.60",
            "strategy_name": "Gas Convergence",
            "entry_price": 0.38,
            "exit_price": 0.38,
            "quantity": 40,
            "pnl": 0.0,
            "reason": "SETTLEMENT_UNRESOLVED",
            "settlement_error": "no AAA national average published yet",
        }
        outcome = TradeOutcome.from_position(pos)
        assert outcome.is_gas() is True
        assert outcome.settlement_value is None
        assert outcome.is_settlement_consistent() is False
        with pytest.raises(GasSpecError):
            outcome.gas_spec()

    def test_the_gas_series_registry_is_what_decides_the_route(self):
        """``KXAAAGASMAX`` is a DIFFERENT (annual) series and must not be gas.

        The discriminator is the registry's longest-prefix match, not a
        ``startswith("KXAAAGAS")``, so a neighbouring series cannot be settled
        under a rule that was never verified against it.
        """
        assert TradeOutcome("KXAAAGASM-26AUG31-4.60", "s").is_gas() is True
        assert TradeOutcome("KXAAAGASD-26JUL28-4.10", "s").is_gas() is True
        assert TradeOutcome("KXAAAGASW-26JUL27-4.11", "s").is_gas() is True
        assert TradeOutcome("KXAAAGASMAX-26-5.00", "s").is_gas() is False
        assert TradeOutcome("KXHIGHNY-26JUL25-T87", "s").is_gas() is False
        assert TradeOutcome("KXBTCD-26MAY20-T104000", "s").is_gas() is False


def test_from_position_reads_every_stamped_settlement_key():
    """Symmetry check: whatever the engine stamps, the journal must read."""
    journal_src = io.open(
        os.path.join(REPO_ROOT, "src", "ml", "trade_journal.py"), encoding="utf-8"
    ).read()
    engine_settlement_keys = {
        k
        for k in _position_keys_assigned_in(MATCHING_ENGINE)
        if k.startswith("settlement_")
    }
    # ``_settlement_pending_logged`` is runtime bookkeeping, not provenance.
    engine_settlement_keys -= {"_settlement_pending_logged"}
    unread = [k for k in sorted(engine_settlement_keys) if f'"{k}"' not in journal_src]
    assert not unread, (
        f"SimulatedExchange stamps {unread} on a settling position but "
        f"TradeOutcome.from_position never reads them — Phase 1 exit criterion "
        f"5 requires settlement to be verifiable in the journal, not just in "
        f"exchange state"
    )
