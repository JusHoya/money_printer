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
    consumer (``TradeOutcome.from_position``) still agree on the key names.

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
