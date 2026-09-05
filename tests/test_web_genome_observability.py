"""The web dashboard must show the genome and the true lifetime PnL (F3 red team, 2026-09-05).

Three defects this file pins, all found against the live maia sandbox:

1. **The genome was invisible.** F3 added 27,626 lines and none of them to
   ``src/web/``. No route read the genome's persisted state — the thing that
   decides whether it may emit at all — so "the genome stopped emitting", "a
   poll failure closed the day" and "the strategy was REFUSED at startup" were
   the same observation from outside. ``snapshot()["genome"]`` now carries it,
   including the refusal reason, bounded for a 1 Hz broadcast, and built by
   duck typing so ``tests/test_factory_isolation.py`` stays green.
2. **The reported PnL was not the paper account's.** ``portfolio.realized_pnl``
   is ``RiskManager.daily_pnl``, a per-cycle fragment the 4-hourly
   ``update_balance()`` zeroes; live on 2026-09-05 it read 0.0 against a
   journal summing to -13.34. ``cumulative_net_pnl`` is the engine's lifetime
   total, net of every fee.
3. **``mode`` lied by ambiguity.** It describes the Kalshi credential, not what
   any strategy is doing: 'paper' on maia while the genome ran in shadow.

The stubs here mimic ``GenomeStrategy``'s public surface (``state_dict``,
``spec``, ``stats``, ``state_path``, ``name``) rather than importing it: the
web process must never load ``src.factory``.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import deque
from datetime import datetime
from unittest.mock import MagicMock, PropertyMock

import pytest

GENOME_ID = "0c4b20502f2daf65"


# ---------------------------------------------------------------------------
# Stubs — the shapes state_manager duck-types against
# ---------------------------------------------------------------------------


class FakeSpec:
    """``src.factory.promoted.PromotedSpec``'s read surface."""

    def __init__(self, mode="shadow", registry_status="CLOSED"):
        self.genome_id = GENOME_ID
        self.family = "family_1"
        self.mode = mode
        self.registry_status = registry_status


class FakeGenomeStrategy:
    """``GenomeStrategy``'s read surface, with the same state-file shape."""

    def __init__(self, spec=None, state=None, raises=None):
        self.spec = spec or FakeSpec()
        self.name = f"Genome {GENOME_ID[:8]}"
        self.state_path = f"data/forecast_cache/genome_state_{GENOME_ID}.json"
        self.stats = {
            "analyze_calls": 412,
            "hours_evaluated": 9,
            "signals": 4,
            "rejects": 4,
            "poll_failures": 0,
        }
        self._state = state if state is not None else {
            "genome_id": GENOME_ID,
            "last_hour_epoch": {"NY": 1757088000, "MIA": 1757088000},
            "missed_days": [["NY", "2026-09-03"]],
            "traded": [["2026-09-05", "KXHIGHNY-26SEP05-B85"]],
        }
        self._raises = raises
        self.state_dict_calls = 0

    def state_dict(self):
        self.state_dict_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._state


class MutatingGenomeStrategy(FakeGenomeStrategy):
    """``state_dict()`` built exactly like the real one: it ITERATES sets that
    another thread mutates, which is what raises ``RuntimeError: Set changed
    size during iteration`` on the web thread."""

    def __init__(self):
        super().__init__()
        self.traded = {("2026-09-05", f"KXHIGHNY-26SEP05-B{n}") for n in range(60)}
        self.missed = {("NY", "2026-09-04")}
        self.last_hour = {"NY": 1757088000}

    def state_dict(self):
        self.state_dict_calls += 1
        return {
            "genome_id": GENOME_ID,
            "last_hour_epoch": {c: int(h) for c, h in sorted(self.last_hour.items())},
            "missed_days": sorted([list(k) for k in self.missed]),
            "traded": sorted([list(k) for k in self.traded]),
        }


def _make_bot(name, strategies=None, shadow=None, refused_reason=None):
    bot = MagicMock()
    type(bot).name = PropertyMock(return_value=name)
    bot.strategies = strategies if strategies is not None else {}
    # Assign real values: a bare MagicMock attribute is truthy and unserialisable.
    bot.genome_shadow = shadow
    bot.genome_spec = None
    if refused_reason is not None:
        bot.genome_refused_reason = refused_reason
    else:
        del bot.genome_refused_reason  # getattr(..., None) -> None, like a real bot
    return bot


@pytest.fixture
def orch():
    """An orchestrator whose weather bot carries no genome (the pre-F3 shape)."""
    o = MagicMock()
    o.bots = [_make_bot("Weather"), _make_bot("Gas")]
    o.active_bots = {"Weather", "Gas"}
    o.risk_manager = MagicMock()
    o.risk_manager.balance = 2874.12
    o.risk_manager.daily_pnl = -0.88
    o.risk_manager.unrealized_pnl = -52.5
    o.risk_manager.get_current_exposure.return_value = 72.5
    o.risk_manager.exchange.positions = []
    o.risk_manager.exchange.closed_trades = [{"pnl": -1.0}] * 6
    o.risk_manager.exchange.get_stats.return_value = {
        "realized": -0.88,
        "cumulative_net": -13.34,
        "cumulative_realized": -9.34,
        "cumulative_fees": 4.0,
    }
    o.dashboard = MagicMock()
    o.dashboard.start_time = datetime.now()
    o.dashboard.latest_prices = {}
    o.dashboard.alerts = deque()
    o.dashboard.logs = deque()
    o.dashboard.strategy_stats = {}
    o.dashboard.mascot = MagicMock()
    o.dashboard.mascot.state = "IDLE"
    o.dashboard.log_dir = None
    o.uptime_seconds = 125.0
    o.cycle_history = []
    o._training_diagnostics = {}
    o._training_history = []
    return o


def _sm(orchestrator):
    from src.web.state_manager import StateManager

    return StateManager(orchestrator)


# ---------------------------------------------------------------------------
# 1. The genome block
# ---------------------------------------------------------------------------


class TestGenomeBlock:
    def test_snapshot_has_a_genome_block_when_no_genome_is_loaded(self, orch):
        snap = _sm(orch).snapshot()
        assert "genome" in snap, "the snapshot must always answer 'is a genome running?'"
        assert snap["genome"]["present"] is False
        assert snap["genome"]["refused"] is False
        assert snap["genome"]["genome_id"] is None

    def test_loaded_genome_is_reported_with_id_mode_and_shadow_flag(self, orch):
        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy, "weather": object()}, shadow=True)
        g = _sm(orch).snapshot()["genome"]
        assert g["present"] is True
        assert g["genome_id"] == GENOME_ID
        assert g["spec_mode"] == "shadow"
        assert g["registry_status"] == "CLOSED"
        assert g["shadow"] is True
        assert g["execution_mode"] == "shadow"
        assert g["bot"] == "Weather"
        assert g["strategy"] == f"Genome {GENOME_ID[:8]}"
        assert g["state_path"].endswith(f"genome_state_{GENOME_ID}.json")
        assert g["stats"]["signals"] == 4
        assert g["stats"]["rejects"] == 4

    def test_genome_state_is_reported(self, orch):
        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        state = _sm(orch).snapshot()["genome"]["state"]
        assert state["genome_id"] == GENOME_ID
        assert state["last_hour_epoch"] == {"NY": 1757088000, "MIA": 1757088000}
        assert state["cities"] == 2
        assert state["traded"] == [["2026-09-05", "KXHIGHNY-26SEP05-B85"]]
        assert state["traded_total"] == 1
        assert state["missed_days"] == [["NY", "2026-09-03"]]

    def test_paper_mode_genome_reports_paper_execution(self, orch):
        strategy = FakeGenomeStrategy(spec=FakeSpec(mode="paper", registry_status="PROPOSED"))
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=False)
        g = _sm(orch).snapshot()["genome"]
        assert g["shadow"] is False
        assert g["execution_mode"] == "paper"
        assert g["spec_mode"] == "paper"

    def test_refusal_reason_is_surfaced(self, orch):
        """The failure mode that motivated this: the genome is REFUSED at
        startup, the bot silently runs V2, and the only trace is one log line
        that scrolls out of the 500-line tail within ~10 minutes."""
        orch.bots[0] = _make_bot(
            "Weather",
            shadow=False,
            refused_reason="GenomeSpecMismatch: calibration sha 3f2a != spec 91bc",
        )
        snap = _sm(orch).snapshot()
        g = snap["genome"]
        assert g["refused"] is True
        assert "calibration sha" in g["refused_reason"]
        assert g["present"] is False
        # A refused genome executes nothing — it must not read out as "paper".
        assert g["execution_mode"] is None
        assert snap["modes"]["genome"] == "refused"

    def test_refusal_reason_works_without_the_bot_attribute(self, orch):
        """``genome_refused_reason`` is set by the STRATEGY workstream; this
        module reads it with getattr so it works either way."""
        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        g = _sm(orch).snapshot()["genome"]
        assert g["refused"] is False
        assert g["refused_reason"] is None
        assert g["present"] is True

    def test_block_is_bounded_to_the_two_most_recent_days(self, orch):
        """It broadcasts at 1 Hz: only the recent days travel, with counts for
        the rest."""
        traded = [[f"2026-09-{d:02d}", f"KXHIGHNY-26SEP{d:02d}-B{n}"]
                  for d in (1, 2, 3, 4, 5) for n in range(40)]
        missed = [["NY", f"2026-09-{d:02d}"] for d in (1, 2, 3, 4, 5)]
        strategy = FakeGenomeStrategy(state={
            "genome_id": GENOME_ID,
            "last_hour_epoch": {"NY": 1757088000},
            "missed_days": missed,
            "traded": traded,
        })
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        state = _sm(orch).snapshot()["genome"]["state"]
        assert state["traded_total"] == 200
        assert len(state["traded"]) <= 100
        assert {row[0] for row in state["traded"]} == {"2026-09-04", "2026-09-05"}
        assert state["missed_days"] == [["NY", "2026-09-04"], ["NY", "2026-09-05"]]
        assert state["missed_days_total"] == 5
        assert state["truncated"] is True

    def test_block_stays_small_enough_to_broadcast_at_1hz(self, orch):
        """A month of accumulated state (the strategy prunes at 4096 traded
        entries) is a ~120 KB file; the block that ships every second off the
        back of it must stay a few KB."""
        cities = ("NY", "CHI", "LAX", "MIA")
        strategy = FakeGenomeStrategy(state={
            "genome_id": GENOME_ID,
            "last_hour_epoch": {c: 1757088000 for c in cities},
            "missed_days": [[c, f"2026-08-{d:02d}"] for d in range(1, 31) for c in cities],
            "traded": [[f"2026-08-{d:02d}", f"KXHIGH{c}-26AUG{d:02d}-B{85 + n}"]
                       for d in range(1, 31) for c in cities for n in range(24)],
        })
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        block = _sm(orch).snapshot()["genome"]
        raw = len(json.dumps(strategy._state))
        served = len(json.dumps(block))
        assert raw > 100_000, "the fixture should be a realistically large state file"
        assert served < 8192, f"genome block is {served} bytes — too fat for a 1 Hz broadcast"
        assert block["state"]["traded_total"] == 2880  # the count is not truncated

    def test_snapshot_stays_json_serialisable(self, orch):
        """Starlette serialises with allow_nan=False and no default= — anything
        exotic in the block 500s the whole endpoint."""
        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        snap = _sm(orch).snapshot()
        json.dumps({k: snap[k] for k in ("genome", "modes", "portfolio")}, allow_nan=False)


# ---------------------------------------------------------------------------
# 2. Concurrent mutation — the case that takes the endpoint down in production
# ---------------------------------------------------------------------------


class TestGenomeStateConcurrency:
    def test_state_read_failure_degrades_instead_of_killing_the_snapshot(self, orch):
        """The market thread mutates ``_traded`` while the web thread iterates
        it. A bare read raises and, uncaught, takes /api/status and every /ws
        client down with it."""
        strategy = FakeGenomeStrategy(
            raises=RuntimeError("Set changed size during iteration")
        )
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        snap = _sm(orch).snapshot()  # must not raise
        g = snap["genome"]
        assert g["present"] is True
        assert g["state"] is None
        assert "Set changed size during iteration" in g["state_error"]
        # Everything that does not come from the state file still reports.
        assert g["genome_id"] == GENOME_ID
        assert g["execution_mode"] == "shadow"
        json.dumps(g, allow_nan=False)

    def test_a_transient_mutation_error_is_retried(self, orch):
        class FlakyStrategy(FakeGenomeStrategy):
            def state_dict(self):
                self.state_dict_calls += 1
                if self.state_dict_calls == 1:
                    raise RuntimeError("Set changed size during iteration")
                return self._state

        strategy = FlakyStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        g = _sm(orch).snapshot()["genome"]
        assert strategy.state_dict_calls == 2
        assert g["state_error"] is None
        assert g["state"]["genome_id"] == GENOME_ID

    def test_snapshot_survives_a_thread_really_mutating_the_state(self, orch):
        """The real race, not a simulated one: the market thread adds and
        removes traded markets while the web thread iterates them inside
        ``state_dict()``.

        The interpreter switch interval is dropped for the duration so the
        collision is reliable rather than a once-a-week production surprise;
        without the guard in ``_genome_state`` this raises ``RuntimeError: Set
        changed size during iteration`` within a few dozen snapshots.
        """
        strategy = MutatingGenomeStrategy()
        strategy.traded = {("2026-09-05", f"KXHIGHNY-26SEP05-B{n}") for n in range(4000)}
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        sm = _sm(orch)
        stop = threading.Event()
        errors = []
        churn_errors = []

        def churn():
            n = 0
            while not stop.is_set():
                n += 1
                key = ("2026-09-05", f"KXHIGHNY-26SEP05-C{n % 500}")
                try:
                    strategy.traded.add(key)
                    strategy.missed.add(("NY", f"2026-09-0{n % 5 + 1}"))
                    if n % 2 == 0:
                        strategy.traded.discard(key)
                except Exception as exc:  # noqa: BLE001
                    churn_errors.append(exc)
                    return

        previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            for _ in range(200):
                try:
                    snap = sm.snapshot()
                except Exception as exc:  # noqa: BLE001 — the defect under test
                    errors.append(exc)
                    break
                assert snap["genome"]["present"] is True
        finally:
            stop.set()
            t.join(timeout=5)
            sys.setswitchinterval(previous_interval)
        assert not churn_errors, f"the churn thread itself failed: {churn_errors[0]!r}"
        assert not errors, f"snapshot() raised during concurrent mutation: {errors[0]!r}"


# ---------------------------------------------------------------------------
# 3. Lifetime PnL
# ---------------------------------------------------------------------------


class TestLifetimePnL:
    def test_cumulative_net_pnl_is_exposed_next_to_the_cycle_fragment(self, orch):
        pf = _sm(orch).snapshot()["portfolio"]
        # The per-cycle fragment keeps its historical name and value...
        assert pf["realized_pnl"] == -0.88
        assert pf["realized_pnl_cycle"] == -0.88
        # ...and the lifetime total, net of every fee, sits beside it.
        assert pf["cumulative_net_pnl"] == -13.34
        assert pf["cumulative_realized_pnl"] == -9.34
        assert pf["cumulative_fees"] == 4.0
        assert pf["closed_trades"] == 6

    def test_lifetime_fields_are_null_when_the_engine_cannot_answer(self, orch):
        orch.risk_manager.exchange.get_stats.side_effect = AttributeError("no exchange")
        del orch.risk_manager.exchange.closed_trades
        pf = _sm(orch).snapshot()["portfolio"]
        assert pf["cumulative_net_pnl"] is None
        assert pf["closed_trades"] is None
        assert pf["realized_pnl"] == -0.88  # the fragment still reports

    def test_nan_from_the_engine_never_reaches_the_serialiser(self, orch):
        orch.risk_manager.exchange.get_stats.return_value = {
            "cumulative_net": float("nan"),
            "cumulative_realized": None,
            "cumulative_fees": "not a number",
        }
        pf = _sm(orch).snapshot()["portfolio"]
        assert pf["cumulative_net_pnl"] is None
        json.dumps(pf, allow_nan=False)

    def test_portfolio_without_a_risk_manager_still_has_the_keys(self, orch):
        orch.risk_manager = None
        pf = _sm(orch).snapshot()["portfolio"]
        assert pf["cumulative_net_pnl"] is None
        assert pf["realized_pnl_cycle"] == 0.0


# ---------------------------------------------------------------------------
# 4. Mode disambiguation
# ---------------------------------------------------------------------------


class TestModeDisambiguation:
    def test_mode_keeps_its_value_and_gains_a_disambiguated_sibling(self, orch, monkeypatch):
        monkeypatch.delenv("KALSHI_API_URL", raising=False)
        orch.kalshi = MagicMock()
        orch.kalshi.read_only = True
        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        snap = _sm(orch).snapshot()
        assert snap["mode"] == "paper"  # unchanged: the front-end pill reads it
        assert snap["modes"]["capital"] == "paper"
        assert snap["modes"]["genome"] == "shadow"
        assert "capital=paper" in snap["modes"]["label"]
        assert "genome=shadow" in snap["modes"]["label"]

    def test_no_genome_reads_as_none_not_as_paper(self, orch):
        snap = _sm(orch).snapshot()
        assert snap["modes"]["genome"] == "none"


# ---------------------------------------------------------------------------
# 5. Routes
# ---------------------------------------------------------------------------


class TestGenomeRoutes:
    @pytest.fixture
    def client(self, orch):
        from fastapi.testclient import TestClient

        from src.web.server import create_app

        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        return TestClient(create_app(_sm(orch), orch))

    def test_status_serves_the_genome_block(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["genome"]["genome_id"] == GENOME_ID
        assert body["genome"]["execution_mode"] == "shadow"
        assert body["portfolio"]["cumulative_net_pnl"] == -13.34

    def test_genome_route_serves_the_block_alone(self, client):
        resp = client.get("/api/genome")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["genome"]["genome_id"] == GENOME_ID
        assert body["modes"]["genome"] == "shadow"
        assert "market_data" not in body  # the point of the route

    def test_genome_route_is_side_effect_free(self, orch):
        """/api/status appends an equity point on every call; a monitoring
        agent polling the genome must not write phantom rows into the chart."""
        strategy = FakeGenomeStrategy()
        orch.bots[0] = _make_bot("Weather", {"genome": strategy}, shadow=True)
        sm = _sm(orch)
        sm.snapshot()
        before = len(sm._pnl_history)
        for _ in range(5):
            sm.genome_snapshot()
        assert len(sm._pnl_history) == before


# ---------------------------------------------------------------------------
# 6. Isolation — the web layer may not import the factory
# ---------------------------------------------------------------------------


def test_bounding_matches_the_real_state_dict_shape():
    """The stubs above must not be the only thing the bounding logic is proved
    against: run the REAL ``GenomeStrategy.state_dict`` and bound its output.

    This pins the field order the truncation depends on — ``missed_days`` rows
    are ``[city, target_date]`` and ``traded`` rows are ``[target_date, symbol]``
    — so a change to either set in the STRATEGY workstream fails here instead
    of silently serving the wrong two days.
    """
    pytest.importorskip("numpy")
    gs = pytest.importorskip("src.strategies.genome_strategy")

    from src.web.state_manager import StateManager

    strategy = object.__new__(gs.GenomeStrategy)
    strategy.spec = FakeSpec()
    strategy._last_hour = {"NY": 1757088000, "MIA": 1757084400}
    # ``_missed_days`` is a MAPPING of (city, target_date) -> (lost_hour, cause), not a
    # set: the STRATEGY workstream added the cause so a closed city-day says WHY it
    # closed. This test is the tripwire that caught that change, which is its job.
    strategy._missed_days = {
        ("NY", f"2026-09-0{d}"): (1757088000, "poll_failure") for d in (2, 3, 4, 5)
    }
    strategy._traded = {(f"2026-09-0{d}", f"KXHIGHNY-26SEP0{d}-B85") for d in (2, 3, 4, 5)}

    doc = gs.GenomeStrategy.state_dict(strategy)
    bounded = StateManager._bound_state(doc)

    assert bounded["genome_id"] == GENOME_ID
    assert bounded["cities"] == 2
    assert bounded["missed_days"] == [["NY", "2026-09-04"], ["NY", "2026-09-05"]]
    assert bounded["missed_days_total"] == 4
    assert bounded["traded"] == [
        ["2026-09-04", "KXHIGHNY-26SEP04-B85"],
        ["2026-09-05", "KXHIGHNY-26SEP05-B85"],
    ]
    assert bounded["traded_total"] == 4
    assert bounded["truncated"] is True
    json.dumps(bounded, allow_nan=False)


def test_state_manager_imports_no_factory_module():
    """``tests/test_factory_isolation.py`` pins the runtime entry points'
    import graph; this pins the reason the genome block is duck-typed."""
    import ast
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "web", "state_manager.py",
    )
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = [
        m for m in imported
        if m.startswith("src.factory") or m.startswith("src.strategies")
    ]
    assert not forbidden, f"state_manager must reach the genome by duck typing: {forbidden}"
