"""FR-5.2 fill-configuration switch and evidence trail (F4 blocker 2, 2026-09-05).

PRD FR-5.2 says the promotion gate is evaluated only on runs with realistic
fills. Before this sprint that requirement was unsatisfiable, in both directions:

* nothing in the runtime could TURN THE MODEL ON. ``SimulatedExchange`` takes
  ``realistic_fills`` (matching_engine.py:446) but ``RiskManager.__init__``
  constructs it without the argument (risk_manager.py:137) and both files are
  protected (tests/test_protected_files.py), so no production call site existed;
* nothing could EVIDENCE it either. ``_save_state`` never serialises the flag,
  so ``gate.py``'s ``_state_realistic_fills`` returned ``None`` forever and the
  terminal gate refused every record it was handed.

Both halves live in ``scripts/run_dashboard.py`` (unprotected): a DEFAULT-OFF
``MP_REALISTIC_FILLS`` switch applied to the already-constructed exchange, and
an append-only ``data/fill_config.jsonl`` recording the exchange's EFFECTIVE
configuration, which ``scripts/gate.py`` joins to the paper record by time.

What these tests pin, in order:

1. the mechanism the switch depends on -- the engine reads ``realistic_fills``
   at FILL TIME, so a post-construction assignment is honoured. If a future
   engine change caches the flag at ``__init__`` the switch silently stops
   working, and this is the test that would catch it;
2. the switch is OFF unless asked, and unrecognised values are OFF;
3. the record reports the EXCHANGE, never the environment;
4. the gate's reader: run windows, staleness, the grace cap, and the fact that
   no absence or damage can produce a ``True``;
5. the writer and the reader agree on one schema (round trip).
"""
from __future__ import annotations

import importlib.util
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("mp_gate", REPO_ROOT / "scripts" / "gate.py")
gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(gate)

from scripts.run_dashboard import (  # noqa: E402
    append_fill_config_record,
    apply_fill_config,
    fill_config_record,
    fill_config_snapshot,
    realistic_fills_from_env,
)
from src.core.matching_engine import SimulatedExchange  # noqa: E402

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _record(**over):
    """A minimal well-formed fill-config record."""
    base = {
        "record": "fill_config",
        "schema_version": 1,
        "run_id": "run-a",
        "event": "heartbeat",
        "run_started_utc": _iso(T0),
        "observed_utc": _iso(T0),
        "heartbeat_sec": 300.0,
        "realistic_fills": True,
    }
    base.update(over)
    return base


def _log(tmp_path: Path, *records, name="fill_config.jsonl") -> str:
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )
    return str(path)


def _trade(entry: datetime, symbol="KXHIGHNY-26JUN01-B84.5"):
    return {"symbol": symbol, "entry_time": _iso(entry)}


# ---------------------------------------------------------------------------
# 1. The mechanism: the engine reads the flag at fill time, not at __init__
# ---------------------------------------------------------------------------
def test_realistic_fills_assigned_after_construction_is_honoured_at_fill_time():
    """The switch sets a PUBLIC attribute on an exchange RiskManager already built.

    That is only sound because ``penny_floor_fill_probability``
    (matching_engine.py:851) and the early-out in ``open_position``
    (matching_engine.py:972) both read ``self.realistic_fills`` when the fill
    happens. This test fails if that ever becomes an ``__init__``-time decision.
    """
    ex = SimulatedExchange()  # exactly how RiskManager builds it: no fill args
    assert ex.realistic_fills is False
    assert ex.penny_floor_fill_probability(0.02, "buy", "YES") == 1.0
    for _ in range(10):
        ex.open_position("TESTSYM", "buy", 0.02, 1, strategy_name="T")
    assert ex.penny_floor_requested == 0, "the model must be inert while OFF"
    assert len(ex.positions) == 10

    ex.realistic_fills = True  # <- post-construction, what apply_fill_config does
    ex.penny_fill_prob = 0.5
    ex._fill_rng = random.Random(1)
    assert ex.penny_floor_fill_probability(0.02, "buy", "YES") < 1.0
    for _ in range(12):
        ex.open_position("TESTSYM", "buy", 0.02, 1, strategy_name="T")
    assert ex.penny_floor_requested == 12, "the penny-floor gate must have run"
    assert ex.penny_floor_skipped > 0, "some penny-floor orders must fail to fill"
    assert len(ex.positions) == 10 + (12 - ex.penny_floor_skipped)


# ---------------------------------------------------------------------------
# 2. The switch is OFF unless asked
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on", "y"])
def test_env_switch_on(raw):
    assert realistic_fills_from_env({"MP_REALISTIC_FILLS": raw}) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  "])
def test_env_switch_off(raw):
    assert realistic_fills_from_env({"MP_REALISTIC_FILLS": raw}) is False


def test_env_switch_defaults_off_and_junk_is_off():
    assert realistic_fills_from_env({}) is False
    assert realistic_fills_from_env({"MP_REALISTIC_FILLS": "maybe"}) is False


def test_apply_fill_config_is_a_no_op_with_no_environment():
    """Deploying this branch with nothing set must not change a single fill."""
    ex = SimulatedExchange()
    before = (ex.realistic_fills, ex.penny_fill_prob, ex.PENNY_FLOOR_LO, ex.PENNY_FLOOR_HI)
    applied = apply_fill_config(ex, {})
    assert (ex.realistic_fills, ex.penny_fill_prob, ex.PENNY_FLOOR_LO, ex.PENNY_FLOOR_HI) == before
    assert applied["realistic_fills"] is False
    assert applied["fill_rng_seed"] is None


def test_apply_fill_config_opt_in_sets_the_effective_configuration():
    ex = SimulatedExchange()
    applied = apply_fill_config(
        ex,
        {"MP_REALISTIC_FILLS": "1", "MP_PENNY_FILL_PROB": "0.25", "MP_FILL_RNG_SEED": "7"},
    )
    assert ex.realistic_fills is True
    assert ex.penny_fill_prob == 0.25
    assert applied["realistic_fills"] is True
    assert applied["penny_fill_prob"] == 0.25
    assert applied["fill_rng_seed"] == 7
    # the seed really seeded the exchange's own stream, not the global one
    assert ex._fill_rng.random() == random.Random(7).random()


def test_apply_fill_config_keeps_bad_numbers_out_of_the_engine():
    ex = SimulatedExchange()
    default_prob = ex.penny_fill_prob
    apply_fill_config(
        ex,
        {"MP_REALISTIC_FILLS": "1", "MP_PENNY_FILL_PROB": "lots", "MP_FILL_RNG_SEED": "x"},
    )
    assert ex.realistic_fills is True
    assert ex.penny_fill_prob == default_prob


# ---------------------------------------------------------------------------
# 3. The record describes the EXCHANGE, not the environment
# ---------------------------------------------------------------------------
def test_record_reports_the_exchange_even_when_the_environment_disagrees():
    """A mis-parsed or overridden variable must not be able to claim a true run."""
    ex = SimulatedExchange()
    apply_fill_config(ex, {"MP_REALISTIC_FILLS": "1"})
    ex.realistic_fills = False  # something later turned it back off
    rec = fill_config_record(ex, run_id="r", run_started_utc=_iso(T0), event="heartbeat")
    assert rec["realistic_fills"] is False
    assert fill_config_snapshot(ex)["realistic_fills"] is False


def test_append_fill_config_record_creates_the_directory_and_appends():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "nested" / "fill_config.jsonl")
        ex = SimulatedExchange()
        for event in ("start", "heartbeat", "stop"):
            assert append_fill_config_record(
                path, fill_config_record(ex, run_id="r", run_started_utc=_iso(T0), event=event)
            )
        lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(row)["event"] for row in lines] == ["start", "heartbeat", "stop"]


def test_append_fill_config_record_never_raises_on_a_bad_path():
    ex = SimulatedExchange()
    rec = fill_config_record(ex, run_id="r", run_started_utc=_iso(T0), event="start")
    assert append_fill_config_record("", rec) is False


# ---------------------------------------------------------------------------
# 4. The gate's reader
# ---------------------------------------------------------------------------
def test_loader_drops_junk_without_letting_it_through(tmp_path):
    path = tmp_path / "fill_config.jsonl"
    path.write_text(
        json.dumps(_record()) + "\n"
        + "{not json\n"
        + json.dumps({"record": "something_else"}) + "\n"
        + json.dumps(_record(schema_version=99)) + "\n"
        + "\n",
        encoding="utf-8",
    )
    records, problems = gate.load_fill_config_records(str(path))
    assert len(records) == 1
    assert problems == {
        "unreadable_lines": 1,
        "foreign_records": 1,
        "unsupported_schema": 1,
    }


def test_loader_on_a_missing_file_is_empty_not_an_error():
    records, problems = gate.load_fill_config_records("nope/does/not/exist.jsonl")
    assert records == [] and problems["unreadable_lines"] == 0


def test_run_window_closes_exactly_on_a_clean_stop():
    stop = T0 + timedelta(hours=2)
    runs, problems = gate.fill_config_runs(
        [
            _record(event="start", observed_utc=_iso(T0)),
            _record(event="heartbeat", observed_utc=_iso(T0 + timedelta(hours=1))),
            _record(event="stop", observed_utc=_iso(stop)),
        ]
    )
    assert problems == {"malformed_records": 0, "future_dated_records": 0}
    assert len(runs) == 1 and runs[0]["evidencing"] is True
    assert runs[0]["window_start"] == T0
    assert runs[0]["window_end"] == stop, "a clean stop bounds the window exactly"


def test_a_crashed_run_stops_evidencing_after_its_declared_grace():
    last = T0 + timedelta(hours=1)
    runs, _ = gate.fill_config_runs(
        [_record(event="start", observed_utc=_iso(T0)),
         _record(event="heartbeat", observed_utc=_iso(last))]
    )
    assert runs[0]["window_end"] == last + timedelta(seconds=300.0)
    assert runs[0]["stopped"] is None


def test_a_record_cannot_claim_an_unbounded_grace_for_itself():
    """The FORWARD edge. One of three bounds, not the whole defence.

    Capping the grace stops a record buying itself an arbitrary future; it never
    bounded the window's OTHER edges, which is how a single forged line covered
    a whole paper record (see ``test_a_claimed_run_start_cannot_reach_back_past
    _the_capped_grace``, ``test_a_run_needs_a_start_and_a_strictly_later_record
    _to_evidence_anything`` and ``test_a_future_dated_record_is_not_a_stamp``).
    """
    runs, _ = gate.fill_config_runs([_record(heartbeat_sec=10**9)])
    assert runs[0]["grace_sec"] == gate.FILL_CONFIG_MAX_GRACE_S
    runs, _ = gate.fill_config_runs([_record(heartbeat_sec="banana")])
    assert runs[0]["grace_sec"] == gate.FILL_CONFIG_DEFAULT_GRACE_S


def test_a_claimed_run_start_cannot_reach_back_past_the_capped_grace():
    """The backward edge, which the F4 review found unbounded.

    ``run_started_utc`` is attacker-supplied text like every other field, and it
    was taken verbatim. A real start record stamps ``observed_utc`` within
    milliseconds of the instant it declares, so clamping the claim to the run's
    own first stamp minus its capped grace is invisible to a genuine run and
    stops a fabricated one from reaching backwards over trades it never made.
    """
    runs, _ = gate.fill_config_runs(
        [
            _record(event="start", run_started_utc=_iso(datetime(1970, 1, 1, tzinfo=timezone.utc)),
                    observed_utc=_iso(T0)),
            _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=1))),
        ]
    )
    assert runs[0]["start_clamped"] is True
    assert runs[0]["window_start"] == T0 - timedelta(seconds=300.0)
    # a genuine run declares a start it actually stamped: nothing moves
    runs, _ = gate.fill_config_runs(
        [
            _record(event="start", observed_utc=_iso(T0)),
            _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=1))),
        ]
    )
    assert runs[0]["start_clamped"] is False and runs[0]["window_start"] == T0


def test_a_run_needs_a_start_and_a_strictly_later_record_to_evidence_anything():
    """One line describes an INSTANT. It must never be able to cover a record."""
    lone, _ = gate.fill_config_runs([_record(event="start", observed_utc=_iso(T0))])
    assert lone[0]["evidencing"] is False
    assert "strictly later record" in lone[0]["not_evidencing"]
    # two lines stamped at the same instant are still one instant
    same, _ = gate.fill_config_runs(
        [_record(event="start", observed_utc=_iso(T0)),
         _record(event="stop", observed_utc=_iso(T0))]
    )
    assert same[0]["evidencing"] is False
    # a later record with no start anchors nothing
    headless, _ = gate.fill_config_runs(
        [_record(event="heartbeat", observed_utc=_iso(T0)),
         _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=1)))]
    )
    assert headless[0]["evidencing"] is False
    assert "no start record" in headless[0]["not_evidencing"]
    # and a non-evidencing run covers nothing, however wide its window
    ok, _ = gate.fill_config_runs(
        [_record(event="start", observed_utc=_iso(T0)),
         _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=1)))]
    )
    assert ok[0]["evidencing"] is True


def test_a_non_evidencing_run_covers_no_fill(tmp_path):
    path = _log(tmp_path, _record(event="start", observed_utc=_iso(T0),
                                  run_started_utc=_iso(T0 - timedelta(days=400))))
    detail = gate.resolve_fill_config(path, [_trade(T0)])
    assert detail["value"] is None
    assert detail["runs_evidencing"] == 0 and len(detail["runs"]) == 1
    assert detail["fills_uncovered"] == 1
    assert "no evidencing run" in detail["note"]


def test_a_future_dated_record_is_not_a_stamp(tmp_path):
    """A record asserts a LIVE process wrote it, so it cannot post-date now."""
    now = T0
    ahead = now + timedelta(seconds=gate.FILL_CONFIG_FUTURE_TOLERANCE_S + 60)
    runs, problems = gate.fill_config_runs(
        [_record(event="start", observed_utc=_iso(now - timedelta(hours=1))),
         _record(event="stop", observed_utc=_iso(ahead))],
        now=now,
    )
    assert problems["future_dated_records"] == 1
    assert problems["malformed_records"] == 1  # dropped, never merged into a window
    assert runs[0]["records"] == 1 and runs[0]["evidencing"] is False
    # inside the clock-skew tolerance a slightly-ahead stamp is still honoured
    runs, problems = gate.fill_config_runs(
        [_record(event="start", observed_utc=_iso(now - timedelta(hours=1))),
         _record(event="stop", observed_utc=_iso(now + timedelta(seconds=60)))],
        now=now,
    )
    assert problems["future_dated_records"] == 0 and runs[0]["evidencing"] is True


def test_the_verdict_reports_future_dated_drops_to_the_operator(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0)),
        _record(event="stop", observed_utc=_iso(datetime(2099, 1, 1, tzinfo=timezone.utc))),
    )
    detail = gate.resolve_fill_config(path, [_trade(T0 + timedelta(hours=1))], now=T0)
    assert detail["value"] is None and detail["future_dated_records"] == 1
    assert "future-dated" in detail["note"]


def test_records_that_cannot_place_or_interpret_themselves_are_malformed():
    runs, problems = gate.fill_config_runs(
        [
            _record(run_id=None),
            _record(observed_utc="not a time"),
            _record(realistic_fills="true"),  # a string is not a recorded boolean
            _record(realistic_fills=1),
        ]
    )
    assert runs == [] and problems["malformed_records"] == 4


def test_no_log_answers_unknown_never_yes(tmp_path):
    detail = gate.resolve_fill_config(str(tmp_path / "absent.jsonl"), [_trade(T0)])
    assert detail["value"] is None and detail["exists"] is False


def test_a_covering_true_run_evidences_the_fills(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0)),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=4))),
    )
    detail = gate.resolve_fill_config(
        path, [_trade(T0 + timedelta(hours=1)), _trade(T0 + timedelta(hours=3))]
    )
    assert detail["value"] is True
    assert detail["fills_covered"] == 2 and detail["fills_uncovered"] == 0
    assert detail["sha256"] and len(detail["sha256"]) == 64


def test_a_stale_log_refuses_rather_than_covering_later_fills(tmp_path):
    """The record must describe the run that made the trades, not any run."""
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0)),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=1))),
    )
    detail = gate.resolve_fill_config(path, [_trade(T0 + timedelta(days=3))])
    assert detail["value"] is None
    assert detail["fills_uncovered"] == 1
    assert detail["uncovered_examples"][0]["reason"] == "outside every recorded run window"


def test_a_fill_before_the_run_started_is_not_covered(tmp_path):
    path = _log(tmp_path, _record(event="start", observed_utc=_iso(T0)))
    detail = gate.resolve_fill_config(path, [_trade(T0 - timedelta(hours=1))])
    assert detail["value"] is None and detail["fills_uncovered"] == 1


def test_a_false_run_is_a_definite_false_not_an_unknown(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0), realistic_fills=False),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=2)), realistic_fills=False),
    )
    detail = gate.resolve_fill_config(path, [_trade(T0 + timedelta(hours=1))])
    assert detail["value"] is False


def test_one_uncovered_fill_spoils_a_log_that_covers_the_rest(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0)),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=2))),
    )
    detail = gate.resolve_fill_config(
        path, [_trade(T0 + timedelta(hours=1)), _trade(T0 + timedelta(days=9))]
    )
    assert detail["value"] is None
    assert detail["fills_covered"] == 1 and detail["fills_uncovered"] == 1


def test_overlapping_runs_that_disagree_are_unknown(tmp_path):
    path = _log(
        tmp_path,
        _record(run_id="a", event="start", observed_utc=_iso(T0)),
        _record(run_id="a", event="stop", observed_utc=_iso(T0 + timedelta(hours=4))),
        _record(run_id="b", event="start", observed_utc=_iso(T0), realistic_fills=False),
        _record(run_id="b", event="stop", observed_utc=_iso(T0 + timedelta(hours=4)),
                realistic_fills=False),
    )
    detail = gate.resolve_fill_config(path, [_trade(T0 + timedelta(hours=1))])
    assert detail["value"] is None and detail["fills_conflicting"] == 1


def test_a_fill_without_an_entry_time_cannot_be_attributed(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0)),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=4))),
    )
    detail = gate.resolve_fill_config(path, [{"symbol": "KXHIGHNY-26JUN01-B84.5"}])
    assert detail["value"] is None and detail["fills_without_entry_time"] == 1


def test_a_log_with_no_fills_to_attribute_evidences_nothing(tmp_path):
    path = _log(tmp_path, _record(event="start", observed_utc=_iso(T0)))
    assert gate.resolve_fill_config(path, [])["value"] is None


# ---------------------------------------------------------------------------
# 5. Precedence and contradiction
# ---------------------------------------------------------------------------
def test_operator_assertion_answers_only_when_nothing_else_does(tmp_path):
    ev = gate.resolve_realistic_fills(
        operator_assertion=True, state_path=None, fill_config_path=None, trades=[_trade(T0)]
    )
    assert ev["value"] is True and ev["source"] == "operator_assertion"


def test_the_log_outranks_the_operator_and_agreement_is_fine(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0)),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=2))),
    )
    ev = gate.resolve_realistic_fills(
        operator_assertion=True,
        state_path=None,
        fill_config_path=path,
        trades=[_trade(T0 + timedelta(hours=1))],
    )
    assert ev["value"] is True and ev["source"] == "fill_config_log"
    assert ev["contradiction"] is None


def test_an_operator_assertion_cannot_overrule_the_recorded_run(tmp_path):
    path = _log(
        tmp_path,
        _record(event="start", observed_utc=_iso(T0), realistic_fills=False),
        _record(event="stop", observed_utc=_iso(T0 + timedelta(hours=2)), realistic_fills=False),
    )
    ev = gate.resolve_realistic_fills(
        operator_assertion=True,
        state_path=None,
        fill_config_path=path,
        trades=[_trade(T0 + timedelta(hours=1))],
    )
    assert ev["value"] is None and ev["source"] is None
    assert "CONTRADICT" in ev["contradiction"]


def test_the_exchange_state_outranks_everything_when_it_ever_answers(tmp_path):
    state = tmp_path / "exchange_state.json"
    state.write_text(json.dumps({"realistic_fills": True, "closed_trades": []}), encoding="utf-8")
    ev = gate.resolve_realistic_fills(
        operator_assertion=True, state_path=str(state), fill_config_path=None, trades=[_trade(T0)]
    )
    assert ev["value"] is True and ev["source"] == "exchange_state"


# ---------------------------------------------------------------------------
# 6. Writer and reader share one schema
# ---------------------------------------------------------------------------
def test_the_orchestrators_own_records_are_readable_by_the_gate(tmp_path):
    """Round trip through the real writer -- no hand-built dicts."""
    path = str(tmp_path / "fill_config.jsonl")
    ex = SimulatedExchange()
    apply_fill_config(ex, {"MP_REALISTIC_FILLS": "1", "MP_FILL_RNG_SEED": "3"})
    start = T0
    stop = T0 + timedelta(hours=3)
    for event, when in (("start", start), ("book_change", T0 + timedelta(hours=1)), ("stop", stop)):
        append_fill_config_record(
            path,
            fill_config_record(
                ex, run_id="round-trip", run_started_utc=_iso(start), event=event,
                observed_utc=_iso(when),
            ),
        )
    detail = gate.resolve_fill_config(path, [_trade(T0 + timedelta(hours=2))])
    assert detail["value"] is True, detail["note"]
    assert detail["records"] == 3 and detail["malformed_records"] == 0
    run = detail["runs"][0]
    assert run["closed_cleanly"] is True
    assert run["window_start"] == _iso(start) and run["window_end"] == _iso(stop)


def test_an_off_run_round_trips_as_a_definite_false(tmp_path):
    path = str(tmp_path / "fill_config.jsonl")
    ex = SimulatedExchange()
    apply_fill_config(ex, {})  # the deployed default
    for event, when in (("start", T0), ("stop", T0 + timedelta(hours=2))):
        append_fill_config_record(
            path,
            fill_config_record(
                ex, run_id="off-run", run_started_utc=_iso(T0), event=event,
                observed_utc=_iso(when),
            ),
        )
    detail = gate.resolve_fill_config(path, [_trade(T0 + timedelta(hours=1))])
    assert detail["value"] is False
