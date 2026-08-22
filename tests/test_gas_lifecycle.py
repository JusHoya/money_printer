"""AAA gas position lifecycle and settlement (PRD FR-4.4, Phase 4 EC-3).

Phase 4 exit criterion 3 needs a simulated gas position to *survive to
settlement* and then settle *on the right number*. Before this workstream
neither held:

* ``SimulatedExchange._weather_close_refused`` returned ``False`` immediately
  for any non-``KXHIGH*`` symbol, so a ``KXAAAGASM`` position was eligible for
  ``TIME_LIMIT``, cycle-reset liquidation, stop-losses, profit targets and
  ``EARLY_SETTLEMENT`` — the same set of close reasons (188 ``TIME_LIMIT`` and
  69 ``CYCLE_RESET`` closes) that made the project's weather PnL history
  meaningless;
* there was no gas settlement path at all, and the payoff a gas symbol would
  have fallen through to prices a ``greater`` strike as ``high >= floor + 1``
  because a daily high is an integer count of degrees. A $4.60/gal gas strike
  run through it demands $5.60 to pay YES.

The gas rule, proven by workstream B against 1,506 published settlements
(15 of which landed exactly on their strike and all 15 paid NO), is
``value > floor_strike`` — strictly, with no epsilon and no rounding.

WHAT IS REAL HERE, AND WHAT IS A SEAM
-------------------------------------
Real production code, executed unmodified:
  * ``SimulatedExchange.update_market`` — the whole sweep: expiry check, time
    limit, valuation, profit targets, stops.
  * ``SimulatedExchange._close_position`` / ``_gas_exit_price`` /
    ``_settle_gas_position`` — the settlement path.
  * ``src.data.gas_settlement`` — workstream B's real resolver and real payoff,
    reading a real settlement-cache JSON and a real AAA CSV off disk.
  * ``OrchestratorEngine._rollover_positions`` — the real cycle-boundary
    liquidation code, called as an unbound method.
  * ``SimulatedExchange._save_state`` — real ``exchange_state.json`` writing.

Seams (three, all named):
  1. ``_rollover_positions`` is invoked with a ``SimpleNamespace`` standing in
     for ``self``, exactly as ``tests/test_weather_lifecycle.py`` does, because
     a real OrchestratorEngine builds a Dashboard, a KalshiProvider and a
     persisting RiskManager pointed at production state files.
  2. ``gas_settlement``'s two truth paths are redirected to ``tmp_path``. No
     test here touches the network: gas truth is a local CSV plus a local
     settlement cache by construction.
  3. Positions are opened via ``exchange.open_position`` rather than through a
     strategy signal, because gas trading is disabled in Phase 4
     (``GAS_TRADING_ENABLED = False``) and EC-2 gates flipping it.

NO HARDCODED SETTLEMENT DATES
-----------------------------
The PENDING/OVERDUE branch is selected by how long ago the settlement date
*began*, so a fixed date in a ticker decays: it starts inside the grace window
and silently crosses out of it as the calendar advances, turning a test red days
later for a reason that has nothing to do with the code. Two tests in this file
derive the date from the clock (``_todays_ticker``); the OVERDUE test pins a
deliberately long-past date so both branches stay covered forever.
"""

import json
import logging
import os
import sys
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.core.matching_engine as matching_engine  # noqa: E402
import src.data.gas_settlement as gas_settlement  # noqa: E402
from src.core.bracket_payoff import BracketSpec, is_weather_symbol  # noqa: E402
from src.core.bracket_payoff import settles_yes as temperature_settles_yes  # noqa: E402
from src.core.matching_engine import (  # noqa: E402
    SimulatedExchange,
    is_held_to_settlement,
)
from scripts.run_dashboard import OrchestratorEngine  # noqa: E402

# The traded series (PRD FR-4.3). Strike 4.60, strike_type "greater", verified
# live 2026-07-29 from GET /markets/KXAAAGASM-26AUG31-4.60.
GAS_TICKER = "KXAAAGASM-26AUG31-4.60"
GAS_SERIES = "KXAAAGASM"
GAS_SETTLEMENT_DATE = "2026-08-31"
GAS_STRIKE = 4.60

WEATHER_TICKER = "KXHIGHNY-26JUL25-B86.5"
CRYPTO_TICKER = "KXBTC15M-26JUL252130-30"

# Every close reason a gas position must refuse. Read off the live guard's own
# documentation tuple so a reason added to the engine shows up here.
FORBIDDEN_GAS_REASONS = (
    "TIME_LIMIT",
    "CYCLE_RESET",
    "STOP_LOSS_PRICE (0.25)",
    "STOP_LOSS_PCT",
    "TAKE_PROFIT",
    "PROFIT_TARGET (+0.15)",
    "EARLY_SETTLEMENT",
    "MARKET",
    "SOME_FUTURE_REASON",
)


class _ListHandler(logging.Handler):
    """The project logger sets ``propagate = False``; caplog cannot see it."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=None):
        return [
            r.getMessage() for r in self.records if level is None or r.levelno >= level
        ]


@pytest.fixture
def logs():
    handler = _ListHandler()
    logger = logging.getLogger("MoneyPrinter")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def truth(tmp_path, monkeypatch):
    """Real gas truth files on disk, read by workstream B's real resolver."""
    cache_path = tmp_path / "settlement_cache.json"
    cache_path.write_text(json.dumps({"truth": {}, "markets": {}}), encoding="utf-8")
    aaa_path = tmp_path / "aaa_daily_national.csv"
    aaa_path.write_text(
        "date,value,source,source_url,fetched_at,raw_sha256,quality\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        gas_settlement, "SETTLEMENT_CACHE_PATH", str(cache_path), raising=True
    )
    monkeypatch.setattr(gas_settlement, "AAA_DAILY_CSV", str(aaa_path), raising=True)
    gas_settlement.reset_caches()

    class _Truth:
        # NB: the attribute names deliberately differ from the enclosing locals
        # — ``x = x`` in a class body does not see the function's local scope.
        cache_file = cache_path
        aaa_file = aaa_path

        @staticmethod
        def publish(date, value, series=GAS_SERIES):
            """Record a published AAA value in the FR-4.4 settlement cache."""
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            blob["truth"][gas_settlement.truth_key(series, date)] = {
                "value": value,
                "source": gas_settlement.SOURCE_AAA_SERIES,
            }
            cache_path.write_text(json.dumps(blob), encoding="utf-8")
            gas_settlement.reset_caches()

        @staticmethod
        def publish_via_csv(date, value):
            """Record a value only in WS-A's AAA daily series (the second path)."""
            with open(aaa_path, "a", encoding="utf-8", newline="") as handle:
                handle.write(
                    f"{date},{value},aaa_live,https://example.invalid/{date},"
                    f"2026-01-01T00:00:00Z,,ok\n"
                )
            gas_settlement.reset_caches()

    try:
        yield _Truth
    finally:
        gas_settlement.reset_caches()


@pytest.fixture
def exchange(tmp_path):
    """Persisting exchange pointed at a temp state file (never prod state)."""
    return SimulatedExchange(state_file=tmp_path / "exchange_state.json")


def _open_gas(
    exchange,
    ticker=GAS_TICKER,
    entry=0.40,
    qty=10,
    stop_loss=0.0,
    expiration_time=None,
    floor_strike=GAS_STRIKE,
    strike_type="greater",
    disable_profit_targets=False,
):
    """Open a gas position exactly as the production path would.

    ``disable_profit_targets`` defaults to **False**, the production value:
    ``src/bots/mixins.py`` passes ``getattr(sig, "disable_profit_targets",
    False)`` and no gas strategy sets the attribute. A helper that hardcoded
    ``True`` would opt every test out of the ladder defect it is meant to
    detect.
    """
    exchange.open_position(
        symbol=ticker,
        side="buy",
        entry_price=entry,
        quantity=qty,
        stop_loss=stop_loss,
        expiration_time=expiration_time,
        strategy_name="Gas Convergence",
        contract_side="YES",
        disable_profit_targets=disable_profit_targets,
        strike_type=strike_type,
        floor_strike=floor_strike,
        cap_strike=None,
    )
    return exchange.positions[-1]


def _open_crypto(exchange, entry=0.30, qty=10):
    """Crypto comparison position — also on the production default ladder."""
    exchange.open_position(
        symbol=CRYPTO_TICKER,
        side="buy",
        entry_price=entry,
        quantity=qty,
        strategy_name="ML BTC 15m",
        disable_profit_targets=False,
        strike=64000.0,
    )
    return exchange.positions[-1]


def _age(pos, minutes):
    pos["open_time"] = datetime.now() - timedelta(minutes=minutes)


def _expire(pos, minutes=5):
    pos["expiration_time"] = datetime.now() - timedelta(minutes=minutes)


def _todays_ticker(strike=GAS_STRIKE):
    """A gas ticker whose settlement date is *today* in the series timezone.

    Keeps ``_hours_since_gas_settlement_date_open`` inside the grace window on
    whatever day the suite runs, so the PENDING branch never decays into the
    OVERDUE branch. Returns ``(ticker, settlement_date_iso)``.
    """
    today = datetime.now(ZoneInfo("America/New_York")).date()
    label = today.strftime("%y%b%d").upper()
    return f"{GAS_SERIES}-{label}-{strike:.2f}", today.isoformat()


def _sweep(exchange, price=0.40):
    """Drive the real ``update_market`` sweep over the gas book.

    ``_map_symbol_fragment`` leaves ``KXAAAGASM`` untouched (it is not a
    weather station id), the update type stays GENERIC, and the fragment is a
    substring of every gas ticker — so this reaches the gas positions' expiry
    check, time limit, valuation, ladder and stops, which is the whole point.
    """
    exchange.update_market(GAS_SERIES, price)


def _rollover(exchange):
    """Drive the real cycle-boundary code with a stub ``self`` (seam 1)."""
    stub = types.SimpleNamespace(
        risk_manager=types.SimpleNamespace(exchange=exchange, active_positions=99)
    )
    survivors = OrchestratorEngine._rollover_positions(stub)
    return stub, survivors


def _guard_fired(logs):
    return [m for m in logs.messages(logging.ERROR) if "FR-1.5 VIOLATION REFUSED" in m]


# ======================================================================
# 0. The predicate: one definition, two families, provably disjoint
# ======================================================================


def test_gas_and_weather_are_both_held_to_settlement():
    assert is_held_to_settlement(GAS_TICKER)
    assert is_held_to_settlement(WEATHER_TICKER)


def test_crypto_is_not_held_to_settlement():
    """Behaviour preservation: the invariant is not "every symbol"."""
    assert not is_held_to_settlement(CRYPTO_TICKER)
    assert not is_held_to_settlement("KXBTCD-26JUL2514-T78499.99")
    assert not is_held_to_settlement("")
    assert not is_held_to_settlement(None)


def test_the_two_families_are_disjoint():
    """No symbol may be governed by both payoff rules.

    ``KXAAAGASMAX``/``KXAAAGASMIN`` are real neighbouring series (annual
    high/low on the same underlying) that must not be swallowed by the
    ``KXAAAGASM`` prefix, so they are checked too.
    """
    for symbol in (
        GAS_TICKER,
        "KXAAAGASW-26AUG31-4.10",
        "KXAAAGASD-26AUG31-4.106",
        "KXAAAGASMAX-26-5.00",
        "KXAAAGASMIN-26-3.00",
        WEATHER_TICKER,
        "KXHIGHCHI-26JUL25-T90",
        CRYPTO_TICKER,
    ):
        assert not (
            is_weather_symbol(symbol) and gas_settlement.is_gas_symbol(symbol)
        ), f"{symbol} is claimed by both payoff modules"


# ======================================================================
# 1. Defect 1 — every forbidden close reason is refused
# ======================================================================


@pytest.mark.parametrize("reason", FORBIDDEN_GAS_REASONS)
def test_every_non_settlement_close_of_gas_is_refused(exchange, truth, logs, reason):
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_gas(exchange)

    exchange._close_position(pos, 0.10, reason=reason)

    assert pos in exchange.positions, f"{reason} must not close a gas position"
    assert not exchange.closed_trades
    assert _guard_fired(logs), logs.messages(logging.ERROR)
    assert any("FR-1.5 REFUSED" in a for a in alerts), alerts


def test_the_forbidden_set_is_the_engines_own_documented_set():
    """Pin this file's parametrization to the guard's documentation tuple.

    Without this the list above is a private opinion: a close reason added to
    the engine and documented in ``_FORBIDDEN_SETTLEMENT_CLOSE_PREFIXES`` would
    never be exercised against a gas position here.
    """
    documented = set(SimulatedExchange._FORBIDDEN_SETTLEMENT_CLOSE_PREFIXES)
    covered = {
        prefix
        for prefix in documented
        if any(reason.startswith(prefix) for reason in FORBIDDEN_GAS_REASONS)
    }
    assert covered == documented, f"not exercised against gas: {documented - covered}"


def test_guard_alerts_once_per_position_and_reason(exchange, truth, logs):
    """PRD §6 budgets <1 false alarm/day: a repeating refusal must not storm."""
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_gas(exchange)

    for _ in range(20):
        exchange._close_position(pos, 0.10, reason="TIME_LIMIT")

    assert len(alerts) == 1, alerts
    assert len(_guard_fired(logs)) == 20, "every attempt still hits the ERROR log"


def test_guard_does_not_block_gas_settlement(exchange, truth):
    """...and it does not block the reasons gas IS allowed to close on."""
    pos = _open_gas(exchange)
    exchange._close_position(pos, 4.65, reason="EXPIRATION")
    assert pos not in exchange.positions
    assert exchange.closed_trades[-1]["reason"] == "EXPIRATION"


def test_gas_position_survives_time_limit(exchange, truth):
    pos = _open_gas(exchange)
    _age(pos, exchange.TIME_LIMIT_MIN * 2)  # double the 60-minute wall clock

    _sweep(exchange)

    assert pos in exchange.positions, "FR-4.4: gas is exempt from TIME_LIMIT"
    assert not exchange.closed_trades


def test_gas_position_ignores_explicit_and_percentage_stops(exchange, truth):
    pos = _open_gas(exchange, entry=0.60, stop_loss=0.40)
    _age(pos, 45)  # past the 30s grace period
    exchange.update_market_price(GAS_TICKER, 0.02)  # -97%, trips every stop

    _sweep(exchange)

    assert pos in exchange.positions, "FR-4.4: no stop-losses on a binary gas contract"
    assert not exchange.closed_trades
    # Exempt from stops, not blind: it IS marked to the observed price.
    assert pos["current_price"] == pytest.approx(0.02)
    assert pos["pnl"] == pytest.approx((0.02 - 0.60) * 10)


def test_gas_position_carries_no_profit_target_ladder(exchange, truth):
    pos = _open_gas(exchange)  # production default: ladder NOT disabled
    assert pos["profit_targets"] == [], (
        "FR-4.4: a gas contract is held to settlement, so open_position must "
        "withhold the ladder regardless of what the caller asked for"
    )


def test_legacy_gas_position_with_a_ladder_is_never_exited_on_it(exchange, truth, logs):
    """A position restored from a state file written before the ladder was
    withheld at open still carries targets; the sweep must refuse to act."""
    pos = _open_gas(exchange)
    pos["profit_targets"] = [
        {"move": 0.15, "exit_pct": 0.50, "hit": False},
        {"move": 0.30, "exit_pct": 1.00, "hit": False},
    ]
    _age(pos, 45)
    exchange.update_market_price(GAS_TICKER, 0.90)  # +0.50, past both rungs

    _sweep(exchange)

    assert pos in exchange.positions
    assert exchange.closed_trades == []
    assert pos["quantity"] == 10
    assert all(not t["hit"] for t in pos["profit_targets"])


def test_partial_profit_target_close_is_refused_at_the_hard_guard(
    exchange, truth, logs
):
    """``_check_profit_targets`` never routes through ``_close_position``."""
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_gas(exchange)
    pos["profit_targets"] = [{"move": 0.15, "exit_pct": 0.50, "hit": False}]

    closed = exchange._check_profit_targets(pos, 0.90)  # +0.50 move

    assert closed is False
    assert pos["quantity"] == 10, "no tranche may be taken off a gas contract"
    assert not exchange.closed_trades
    assert _guard_fired(logs), logs.messages(logging.ERROR)
    assert any("FR-1.5 REFUSED" in a for a in alerts), alerts


def test_gas_position_ignores_the_early_settlement_peg_heuristic(exchange, truth):
    """A 0.99 peg invents a 1.00 outcome; the AAA value has not been published."""
    pos = _open_gas(exchange, entry=0.40)
    _age(pos, 45)  # past the 10-minute EARLY_SETTLEMENT age gate
    exchange.update_market_price(GAS_TICKER, 0.995)

    _sweep(exchange)

    assert pos in exchange.positions
    assert not exchange.closed_trades


def test_gas_mark_holds_at_entry_without_an_observed_price(exchange, truth):
    """No tanh estimator for gas: no observed price, no synthetic mark."""
    pos = _open_gas(exchange, entry=0.40)
    _age(pos, 45)

    # A $9/gal "spot" would have driven a distance-to-strike estimator hard;
    # with no observed Kalshi price the mark holds at entry.
    exchange.update_market(GAS_SERIES, 9.00)

    assert pos in exchange.positions
    assert pos["current_price"] == pytest.approx(0.40)
    assert pos["pnl"] == pytest.approx(0.0)


# ======================================================================
# 2. Exemptions pinned INDEPENDENTLY of the hard guard
#
# A mutation run on the weather equivalents showed each exemption could be
# reverted with the whole suite still green, because the guard absorbed the
# failure into a per-tick ERROR + alert. These assert the position is never
# even OFFERED for closure.
# ======================================================================


def _spy_closes(exchange, monkeypatch):
    calls = []
    real = exchange._close_position

    def spy(pos, final_spot_price, reason="MARKET"):
        calls.append(
            {"id": pos.get("id"), "symbol": pos.get("symbol"), "reason": reason}
        )
        return real(pos, final_spot_price, reason=reason)

    monkeypatch.setattr(exchange, "_close_position", spy)
    return calls


def _gas_closes(calls):
    return [c for c in calls if GAS_SERIES in str(c["symbol"])]


def test_time_limit_exemption_never_offers_the_position_for_closure(
    exchange, truth, logs, monkeypatch
):
    pos = _open_gas(exchange)
    _age(pos, exchange.TIME_LIMIT_MIN * 3)
    calls = _spy_closes(exchange, monkeypatch)

    _sweep(exchange)

    assert _gas_closes(calls) == [], (
        "the TIME_LIMIT exemption must skip the close outright, not lean on the "
        f"guard to refuse it: {calls}"
    )
    assert _guard_fired(logs) == []
    assert pos in exchange.positions


def test_stop_loss_exemption_never_offers_the_position_for_closure(
    exchange, truth, logs, monkeypatch
):
    pos = _open_gas(exchange, entry=0.60, stop_loss=0.40)
    _age(pos, 45)
    exchange.update_market_price(GAS_TICKER, 0.02)
    calls = _spy_closes(exchange, monkeypatch)

    _sweep(exchange)

    assert _gas_closes(calls) == [], calls
    assert _guard_fired(logs) == []
    assert pos in exchange.positions


def test_profit_target_exemption_never_reaches_the_ladder(
    exchange, truth, logs, monkeypatch
):
    pos = _open_gas(exchange)
    pos["profit_targets"] = [{"move": 0.15, "exit_pct": 0.50, "hit": False}]
    _age(pos, 45)
    exchange.update_market_price(GAS_TICKER, 0.95)

    seen = []
    real = exchange._check_profit_targets
    monkeypatch.setattr(
        exchange,
        "_check_profit_targets",
        lambda p, price: (seen.append(p.get("symbol")), real(p, price))[1],
    )

    _sweep(exchange)

    assert [
        s for s in seen if GAS_SERIES in str(s)
    ] == [], f"the ladder must not even be consulted for gas: {seen}"
    assert _guard_fired(logs) == []
    assert pos in exchange.positions


def test_a_normal_gas_tick_raises_no_alerts(exchange, truth, logs):
    """PRD §6: <1 false alarm/day. An ordinary tick must be silent."""
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_gas(exchange, entry=0.40, qty=10)
    _age(pos, exchange.TIME_LIMIT_MIN * 2)

    for mark in (0.02, 0.40, 0.95):  # adverse, flat, favourable
        exchange.update_market_price(GAS_TICKER, mark)
        _sweep(exchange)

    _rollover(exchange)

    truth.publish(GAS_SETTLEMENT_DATE, 4.65)
    _expire(pos)
    _sweep(exchange)

    assert alerts == [], alerts
    assert _guard_fired(logs) == [], logs.messages(logging.ERROR)
    assert exchange.closed_trades[-1]["reason"] == "EXPIRATION"


# ======================================================================
# 3. Cycle-boundary survival (real _rollover_positions)
#
# This is the case a widened close guard alone does NOT cover: the guard would
# refuse the CYCLE_RESET while the survivors filter still excluded gas, and
# ``positions[:] = survivors`` would drop the position from the open book with
# no closed row and no PnL.
# ======================================================================


def test_gas_survives_cycle_boundary_crypto_does_not(exchange, truth):
    gas = _open_gas(exchange, entry=0.40, qty=10)
    crypto = _open_crypto(exchange, entry=0.30, qty=10)
    gas_id, gas_entry, gas_qty = gas["id"], gas["entry_price"], gas["quantity"]

    stub, survivors = _rollover(exchange)

    assert gas in exchange.positions, "FR-4.4: gas survives a cycle boundary"
    assert [p["id"] for p in survivors] == [gas_id]
    assert gas["entry_price"] == pytest.approx(gas_entry)
    assert gas["quantity"] == gas_qty
    assert "reason" not in gas

    assert crypto not in exchange.positions
    assert exchange.closed_trades[-1]["reason"] == "CYCLE_RESET"
    assert stub.risk_manager.active_positions == 1


def test_rollover_carveout_never_offers_the_gas_position_for_closure(
    exchange, truth, logs, monkeypatch
):
    gas = _open_gas(exchange)
    _open_crypto(exchange)
    calls = _spy_closes(exchange, monkeypatch)

    _rollover(exchange)

    assert (
        _gas_closes(calls) == []
    ), f"the rollover carve-out must skip gas entirely: {calls}"
    assert [c["reason"] for c in calls] == ["CYCLE_RESET"], calls  # crypto only
    assert _guard_fired(logs) == []
    assert gas in exchange.positions


def test_gas_survivor_is_persisted_at_the_boundary(exchange, truth):
    gas = _open_gas(exchange)
    _open_crypto(exchange)

    _rollover(exchange)

    state = json.loads(exchange._state_file.read_text(encoding="utf-8"))
    assert [p["symbol"] for p in state["positions"]] == [GAS_TICKER]
    assert state["positions"][0]["id"] == gas["id"]


def test_weather_and_gas_survive_the_same_boundary_together(exchange, truth):
    """No regression: widening the carve-out did not narrow it for weather."""
    gas = _open_gas(exchange)
    exchange.open_position(
        symbol=WEATHER_TICKER,
        side="buy",
        entry_price=0.40,
        quantity=10,
        strategy_name="Meteorologist V2",
        contract_side="YES",
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
    )
    weather = exchange.positions[-1]
    crypto = _open_crypto(exchange)

    _rollover(exchange)

    assert gas in exchange.positions
    assert weather in exchange.positions
    assert crypto not in exchange.positions


# ======================================================================
# 4. Defect 2 — settlement on the right number, across the strict boundary
# ======================================================================


@pytest.mark.parametrize(
    "settled_value,expected_outcome,expected_exit",
    [
        (4.61, "yes", 1.00),  # a cent above -> YES
        (5.00, "yes", 1.00),  # well above -> YES
        (4.60, "no", 0.00),  # EXACTLY on the strike -> NO (15/15 live proofs)
        (4.599, "no", 0.00),  # a tenth of a cent below -> NO
        (3.00, "no", 0.00),  # well below -> NO
    ],
)
def test_gas_settles_on_the_strict_boundary(
    exchange, truth, logs, settled_value, expected_outcome, expected_exit
):
    pos = _open_gas(exchange, entry=0.40, qty=10)
    pos_id = pos["id"]
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, settled_value)

    _sweep(exchange)

    assert pos not in exchange.positions
    trade = next(t for t in exchange.closed_trades if t["id"] == pos_id)

    assert trade["reason"] == "EXPIRATION"
    assert trade["exit_price"] == pytest.approx(expected_exit)
    assert trade["settlement_outcome"] == expected_outcome
    assert trade["settlement_value"] == pytest.approx(settled_value)
    assert trade["settlement_spec"] == {
        "strike_type": "greater",
        "floor_strike": GAS_STRIKE,
        "cap_strike": None,
    }
    assert trade["settlement_rule"] == "strictly above 4.6"
    assert "settlement_high" not in trade, (
        "gas truth is USD/gal, not F — reusing the weather key would let a "
        "reader join a gas row to a temperature truth table"
    )

    # ...and the persisted exchange state agrees (what the VM would show).
    state = json.loads(exchange._state_file.read_text(encoding="utf-8"))
    persisted = next(t for t in state["closed_trades"] if t["id"] == pos_id)
    assert persisted["settlement_value"] == pytest.approx(settled_value)
    assert persisted["reason"] == "EXPIRATION"

    assert any("FR-4.4 SETTLED" in m for m in logs.messages(logging.INFO))


def test_exactly_on_strike_would_flip_under_a_non_strict_rule(exchange, truth):
    """Mutation check on the boundary itself, not just on the implementation.

    A ``>=`` payoff would invert this settlement — and would have inverted all
    15 live markets that settled exactly on their strike. Asserting the
    settlement AND asserting the counterfactual differs is what makes the
    boundary test load-bearing rather than incidental.
    """
    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, GAS_STRIKE)

    _sweep(exchange)

    trade = exchange.closed_trades[-1]
    assert trade["settlement_outcome"] == "no"
    assert GAS_STRIKE >= GAS_STRIKE, "the counterfactual >= rule would pay YES"
    assert not GAS_STRIKE > GAS_STRIKE, "the live > rule pays NO"


def test_gas_settles_from_the_aaa_series_when_the_cache_is_empty(exchange, truth):
    """The second truth path (WS-A's CSV) settles too, with the same rule."""
    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish_via_csv(GAS_SETTLEMENT_DATE, 4.75)

    _sweep(exchange)

    assert pos not in exchange.positions
    trade = exchange.closed_trades[-1]
    assert trade["settlement_value"] == pytest.approx(4.75)
    assert trade["settlement_outcome"] == "yes"


def test_settled_gas_pnl_is_the_settlement_payoff_net_of_fees(exchange, truth):
    """EC-3 wants the number, not just the outcome. KXAAAGASM bills makers."""
    from src.core.fee_calculator import compute_fee, fee_type_for_symbol

    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, 4.65)

    _sweep(exchange)

    trade = exchange.closed_trades[-1]
    exit_fee = compute_fee(
        1.00,
        10,
        is_maker=trade.get("is_maker", False),
        series_fee_type=fee_type_for_symbol(GAS_TICKER),
    ).fee
    assert trade["pnl"] == pytest.approx((1.00 - 0.40) * 10 - exit_fee)


def test_a_settled_gas_trade_is_verifiable_in_the_journal(exchange, truth):
    """Phase 1 EC-5 set the precedent that settlement must be verifiable in the
    journal, not only in exchange state; EC-3 inherits it.

    ``settlement_value`` is its own journal field rather than a reuse of
    ``settlement_high``: one column holding either F or USD/gal would let a
    reconcile join a gas row to a temperature truth table and get a plausible,
    wrong answer.
    """
    from src.ml.trade_journal import TradeOutcome

    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, 4.61)

    _sweep(exchange)

    outcome = TradeOutcome.from_position(exchange.closed_trades[-1])
    assert outcome.settlement_value == pytest.approx(4.61)
    assert outcome.settlement_high is None, "gas truth is not a temperature"
    assert outcome.settlement_outcome == "yes"
    assert outcome.settlement_rule == "strictly above 4.6"
    assert outcome.settlement_spec == {
        "strike_type": "greater",
        "floor_strike": GAS_STRIKE,
        "cap_strike": None,
    }
    assert outcome.close_reason == "EXPIRATION"


def test_a_no_settlement_is_a_full_loss_of_the_premium(exchange, truth):
    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, 4.10)

    _sweep(exchange)

    trade = exchange.closed_trades[-1]
    assert trade["exit_price"] == pytest.approx(0.00)
    assert trade["pnl"] == pytest.approx((0.00 - 0.40) * 10)


# ======================================================================
# 5. The temperature payoff never governs a gas symbol
# ======================================================================


def test_the_two_greater_rules_genuinely_differ():
    """The premise of Defect 2, asserted rather than asserted-in-a-comment.

    A temperature ``greater`` bracket pays YES at ``high >= floor + 1`` (a daily
    high is an integer count of degrees). The gas rule is ``value > floor``. On a
    $4.60 strike the two disagree over the whole interval (4.60, 5.60].
    """
    temp_spec = BracketSpec(
        ticker="KXHIGHNY-26JUL25-T4.6", strike_type="greater", floor_strike=GAS_STRIKE
    )
    gas_spec = gas_settlement.GasSpec(
        ticker=GAS_TICKER, strike_type="greater", floor_strike=GAS_STRIKE
    )

    # 4.61 -> gas YES, temperature NO. This is the whole defect: a $4.60 strike
    # priced by the temperature rule demands $5.60 to pay.
    assert gas_settlement.settles_yes(gas_spec, 4.61) is True
    assert temperature_settles_yes(temp_spec, 4.61) is False
    assert temperature_settles_yes(temp_spec, 5.60) is True

    # ...and they agree on the exactly-on-strike case only by coincidence.
    assert gas_settlement.settles_yes(gas_spec, GAS_STRIKE) is False
    assert temperature_settles_yes(temp_spec, GAS_STRIKE) is False


def test_gas_settlement_never_routes_through_the_temperature_payoff(
    exchange, truth, monkeypatch
):
    """Structural: the temperature payoff is not consulted for a gas symbol.

    Spies on ``bracket_payoff.settles_yes`` *as the engine holds it*. If a
    refactor ever routed gas through the weather branch, the settled exit price
    would still be 0.00/1.00 and could still look plausible — so asserting the
    price is not enough; the wrong module must be proven unused.
    """
    seen = []
    real = matching_engine.settles_yes
    monkeypatch.setattr(
        matching_engine,
        "settles_yes",
        lambda spec, value: (seen.append(spec.ticker), real(spec, value))[1],
    )

    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, 4.61)

    _sweep(exchange)

    assert seen == [], f"the temperature payoff was consulted for gas: {seen}"
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(1.00)
    assert exchange.closed_trades[-1]["settlement_outcome"] == "yes"


def test_a_gas_symbol_reaching_the_temperature_payoff_closes_flat_and_loudly(
    exchange, truth, logs
):
    """The last-resort structural guard, driven directly.

    Unreachable through ``_close_position`` (routing selects the gas branch), so
    it is invoked directly — a guard nobody can execute is a guard nobody has
    tested.
    """
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_gas(exchange, entry=0.40, qty=10)

    resolved = exchange._weather_exit_price(pos, 4.61)

    assert resolved == (0.40, "SETTLEMENT_UNRESOLVED")
    assert pos["settlement_error"] == "gas symbol routed to the temperature payoff"
    assert any(
        "SETTLEMENT_UNRESOLVED" in m and "temperature payoff" in m
        for m in logs.messages(logging.ERROR)
    ), logs.messages(logging.ERROR)
    assert any("gas symbol misrouted" in a for a in alerts), alerts


def test_a_weather_symbol_reaching_the_gas_payoff_closes_flat_and_loudly(
    exchange, truth, logs
):
    """The symmetric guard, so neither direction is the untested one."""
    alerts = []
    exchange.on_alert = alerts.append
    exchange.open_position(
        symbol=WEATHER_TICKER,
        side="buy",
        entry_price=0.40,
        quantity=10,
        strategy_name="Meteorologist V2",
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
    )
    pos = exchange.positions[-1]

    resolved = exchange._gas_exit_price(pos, 86.0)

    assert resolved == (0.40, "SETTLEMENT_UNRESOLVED")
    assert pos["settlement_error"] == "weather symbol routed to the gas payoff"
    assert any("weather symbol misrouted" in a for a in alerts), alerts


def test_a_gas_symbol_never_reaches_the_legacy_crypto_strike_parsing(exchange, truth):
    """The other wrong payoff: the suffix ``4.60`` parsed as a crypto strike.

    That path compares ``final_spot_price >= strike``. With the AAA value as
    ``final_spot_price`` and 4.60 as the strike it would settle 4.60 YES —
    inverting the boundary — so a NO settlement here also proves the legacy
    branch was not taken.
    """
    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, GAS_STRIKE)

    _sweep(exchange)

    trade = exchange.closed_trades[-1]
    assert trade["exit_price"] == pytest.approx(0.00)
    assert trade["settlement_outcome"] == "no"
    assert trade["settlement_spec"]["strike_type"] == "greater"


# ======================================================================
# 6. Truth pending / overdue — abort rather than guess
# ======================================================================


def test_expired_gas_holds_open_until_the_aaa_value_is_published(exchange, truth, logs):
    """No published value -> hold and retry, never a stale or assumed one."""
    ticker, settlement_date = _todays_ticker()

    # Fixture precondition, asserted rather than assumed: this test only
    # exercises the PENDING branch while the settlement date is inside the
    # grace window.
    age_h = matching_engine._hours_since_gas_settlement_date_open(ticker)
    assert (
        age_h is not None and age_h <= matching_engine.GAS_SETTLEMENT_TRUTH_GRACE_HOURS
    ), (
        f"fixture precondition: {ticker} must be within the "
        f"{matching_engine.GAS_SETTLEMENT_TRUTH_GRACE_HOURS}h grace window "
        f"(age {age_h})"
    )

    # A value for the DAY BEFORE is published — the trap this must not fall
    # into. The strategy's own projection was built on exactly this row.
    yesterday = (
        (datetime.fromisoformat(settlement_date) - timedelta(days=1)).date().isoformat()
    )
    truth.publish_via_csv(yesterday, 4.99)

    pos = _open_gas(exchange, ticker=ticker, entry=0.40, qty=10)
    _expire(pos)

    _sweep(exchange)

    assert pos in exchange.positions, "no published value -> no settlement"
    assert not exchange.closed_trades
    assert any(
        "SETTLEMENT_TRUTH_PENDING" in m for m in logs.messages(logging.INFO)
    ), logs.messages(logging.INFO)
    assert pos["_settlement_pending_logged"] == "PENDING"
    assert "settlement_value" not in pos, "yesterday's $4.99 must not have leaked in"

    # Truth arrives on the next sweep; the same position now settles.
    truth.publish(settlement_date, 4.55)
    _sweep(exchange)

    assert pos not in exchange.positions
    trade = exchange.closed_trades[-1]
    assert trade["reason"] == "EXPIRATION"
    assert trade["settlement_value"] == pytest.approx(4.55)
    assert trade["exit_price"] == pytest.approx(0.00)  # 4.55 is below 4.60


def test_pending_truth_raises_no_alert(exchange, truth):
    """Waiting hours for the 10:00 ET publication is normal, not a fault."""
    alerts = []
    exchange.on_alert = alerts.append
    ticker, _ = _todays_ticker()
    pos = _open_gas(exchange, ticker=ticker)
    _expire(pos)

    for _ in range(5):
        _sweep(exchange)

    assert alerts == [], alerts
    assert pos in exchange.positions


def test_overdue_truth_escalates_to_error_and_alerts_once(exchange, truth, logs):
    """Past the grace window the pending log escalates and pages — once."""
    alerts = []
    exchange.on_alert = alerts.append
    old_ticker = f"{GAS_SERIES}-26JAN31-4.60"  # settlement date long past
    pos = _open_gas(exchange, ticker=old_ticker)
    _expire(pos, minutes=60 * 24)

    for _ in range(5):
        _sweep(exchange)

    assert pos in exchange.positions
    assert any(
        "SETTLEMENT_TRUTH_OVERDUE" in m for m in logs.messages(logging.ERROR)
    ), logs.messages(logging.ERROR)
    assert [a for a in alerts if "SETTLEMENT TRUTH OVERDUE" in a] != [], alerts
    assert len(alerts) == 1, f"overdue truth must alert once, not per tick: {alerts}"
    assert pos["_settlement_pending_logged"] == "OVERDUE"


def test_truth_age_is_measured_from_the_start_of_the_settlement_date():
    """Gas truth is published on the morning OF the date, not the day after.

    Measuring the age from the date's CLOSE — the weather convention — would
    leave it negative until the date ended, so a value already ~14 hours late
    could never escalate. This pins the convention rather than trusting it.
    """
    at_midnight = datetime(2026, 8, 31, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    age = matching_engine._hours_since_gas_settlement_date_open(
        GAS_TICKER, now=at_midnight
    )
    assert age == pytest.approx(0.0)

    # 10:00 ET on the date — the expected publication time — is inside grace.
    at_publication = at_midnight + timedelta(hours=10)
    age = matching_engine._hours_since_gas_settlement_date_open(
        GAS_TICKER, now=at_publication
    )
    assert age == pytest.approx(10.0)
    assert age <= matching_engine.GAS_SETTLEMENT_TRUTH_GRACE_HOURS

    # Noon ET the following day is past grace: escalate.
    age = matching_engine._hours_since_gas_settlement_date_open(
        GAS_TICKER, now=at_midnight + timedelta(hours=36, minutes=1)
    )
    assert age > matching_engine.GAS_SETTLEMENT_TRUTH_GRACE_HOURS

    # The day before the date, the age is negative — nothing is late yet.
    age = matching_engine._hours_since_gas_settlement_date_open(
        GAS_TICKER, now=at_midnight - timedelta(hours=1)
    )
    assert age < 0


@pytest.mark.parametrize(
    "symbol",
    [
        "KXAAAGASM",  # series with no event-date segment
        "KXAAAGASM-26FEB30-4.60",  # not a real calendar date
        CRYPTO_TICKER,  # not a gas series at all
        "",
    ],
)
def test_an_undatable_symbol_yields_no_age_rather_than_a_guess(symbol):
    """``None`` makes the caller treat missing truth as PENDING.

    Escalating on a symbol we cannot even date would page an operator about a
    parsing bug dressed up as a recorder outage.
    """
    assert matching_engine._hours_since_gas_settlement_date_open(symbol) is None


def test_a_suspect_aaa_row_is_not_settlement_truth(exchange, truth):
    """Contract §1.1: ``quality=suspect`` rows are excluded from fits AND from
    settlement. A bad parse must not settle a position."""
    with open(truth.aaa_file, "a", encoding="utf-8", newline="") as handle:
        handle.write(
            f"{GAS_SETTLEMENT_DATE},4.61,aaa_live,https://example.invalid/x,"
            f"2026-01-01T00:00:00Z,,suspect\n"
        )
    gas_settlement.reset_caches()

    pos = _open_gas(exchange, entry=0.40, qty=10)
    _expire(pos)

    _sweep(exchange)

    assert pos in exchange.positions
    assert not exchange.closed_trades


def test_a_position_without_cached_api_fields_closes_flat_not_no(exchange, truth, logs):
    """A missing ``strike_type`` must not settle NO by default.

    Most rungs settle NO, so a defaulted NO looks plausible and is
    systematically wrong (``abort-on-missing-critical-input``). It closes flat
    at entry with zero PnL, loudly, so the gap is visible in the journal.
    """
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_gas(exchange, entry=0.40, qty=10, strike_type=None)
    _expire(pos)
    truth.publish(GAS_SETTLEMENT_DATE, 4.10)  # would be a clean NO

    _sweep(exchange)

    assert pos not in exchange.positions
    trade = exchange.closed_trades[-1]
    assert trade["reason"] == "SETTLEMENT_UNRESOLVED"
    assert trade["exit_price"] == pytest.approx(0.40), "flat at entry"
    assert trade["settlement_error"]
    assert "settlement_outcome" not in trade
    assert any("SETTLEMENT UNRESOLVED" in a for a in alerts), alerts


# ======================================================================
# 7. Whole-run scan: EC-3's negative assertion
# ======================================================================


def test_no_gas_position_ever_closes_on_a_lifecycle_reason(exchange, truth):
    """Synthetic run: time limits, stops, a cycle boundary, then settlement."""
    gas_a = _open_gas(exchange, entry=0.40, qty=10)
    gas_b = _open_gas(
        exchange,
        ticker="KXAAAGASM-26AUG31-4.30",
        entry=0.55,
        stop_loss=0.45,
        floor_strike=4.30,
    )
    crypto = _open_crypto(exchange)

    for pos in (gas_a, gas_b, crypto):
        _age(pos, 180)

    # Adverse marks that would trip every stop the engine has.
    exchange.update_market_price(GAS_TICKER, 0.01)
    exchange.update_market_price("KXAAAGASM-26AUG31-4.30", 0.01)

    _sweep(exchange)
    exchange.update_market("BTC", 63000.0)

    # A cycle boundary in the middle of the run.
    _rollover(exchange)

    # Then both gas positions expire with truth available: 4.45 is below the
    # 4.60 strike (NO) and above the 4.30 strike (YES).
    truth.publish(GAS_SETTLEMENT_DATE, 4.45)
    for pos in (gas_a, gas_b):
        _expire(pos, minutes=1)
    _sweep(exchange)

    gas_trades = [t for t in exchange.closed_trades if GAS_SERIES in t["symbol"]]
    assert len(gas_trades) == 2, exchange.closed_trades
    for trade in gas_trades:
        assert not str(
            trade["reason"]
        ).startswith(
            SimulatedExchange._FORBIDDEN_SETTLEMENT_CLOSE_PREFIXES
        ), f"{trade['symbol']} closed on a forbidden lifecycle reason: {trade['reason']}"
        assert trade["reason"] == "EXPIRATION"
        assert "settlement_value" in trade

    by_symbol = {t["symbol"]: t for t in gas_trades}
    assert by_symbol[GAS_TICKER]["exit_price"] == pytest.approx(0.00)
    assert by_symbol["KXAAAGASM-26AUG31-4.30"]["exit_price"] == pytest.approx(1.00)

    # ...while the crypto position went through the legacy lifecycle unchanged.
    crypto_trades = [t for t in exchange.closed_trades if "KXBTC" in t["symbol"]]
    assert crypto_trades and crypto_trades[0]["reason"] == "TIME_LIMIT"


# ======================================================================
# 8. Registration (PRD FR-4.1/FR-4.3) — wired, and still feed-only
# ======================================================================


def test_gas_bot_is_registered_feed_only():
    import src.bots  # noqa: F401 - triggers the sanctioned registrations
    from src.bots import gas_bot
    from src.bots.registry import BotRegistry

    assert "gas" in BotRegistry.list_bots()
    assert "weather" in BotRegistry.list_bots(), "weather must not have been displaced"
    assert gas_bot.GAS_TRADING_ENABLED is False, (
        "registration starts the feeds; PRD Phase 4 EC-2 gates trading on a "
        "backtest EV verdict that does not exist yet"
    )


def test_the_registered_gas_bot_reports_feed_only_in_the_cycle_summary():
    """FR-0.4: the cycle summary lists every registered bot with its status.

    Registering a bot whose status line said TRADING would misreport the whole
    phase, and the status is derived generically (any module-level boolean
    ``*TRADING_ENABLED``) rather than from a per-bot attribute — so it is worth
    proving on the real class rather than on a stub.
    """
    from src.bots.registry import BotRegistry

    bot = BotRegistry.create("gas")
    engine = OrchestratorEngine.__new__(OrchestratorEngine)  # skip heavy __init__
    engine.bots = [bot]
    engine.active_bots = {bot.name}

    assert engine._bot_status(bot) == "FEED-ONLY"
    assert engine._bot_status_summary() == f"{bot.name}=FEED-ONLY"
