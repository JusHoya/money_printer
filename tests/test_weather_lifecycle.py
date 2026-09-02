"""Weather position lifecycle (PRD FR-1.5, Phase 1 exit criterion 5).

FR-1.5 verbatim: *"Weather positions are exempt from ``TIME_LIMIT_MIN`` and
cycle-reset liquidation; they persist across cycles and settle via FR-1.2. No
stop-losses on binary weather positions."*

Exit criterion 5: *"a sim weather position opened before a cycle boundary
survives the boundary and settles at expiry with the FR-1.2 payoff (verified in
exchange state and journal); no TIME_LIMIT or CYCLE_RESET close reason appears
on any weather position."*

WHAT IS REAL HERE, AND WHAT IS A SEAM
-------------------------------------
Real production code, executed unmodified:
  * ``SimulatedExchange.update_market`` -- the whole sweep: expiry check, time
    limit, valuation, profit targets, stops.
  * ``SimulatedExchange._close_position`` / ``_weather_exit_price`` /
    ``_settle_weather_position`` -- the settlement path.
  * ``src.core.weather_settlement.resolve_settlement_high`` -- the real
    resolver, reading a real settlement-cache JSON file off disk.
  * ``src.core.bracket_payoff`` -- the real payoff module.
  * ``OrchestratorEngine._rollover_positions`` -- the real cycle-boundary
    liquidation code, called as an unbound method.
  * ``SimulatedExchange._save_state`` -- real ``exchange_state.json`` writing.

Seams (three, all named):
  1. ``_rollover_positions`` is invoked with a ``SimpleNamespace`` standing in
     for ``self``, carrying only ``risk_manager.exchange`` and
     ``risk_manager.active_positions``. Constructing a real OrchestratorEngine
     would build a Dashboard, a KalshiProvider and a persisting RiskManager
     pointed at production state files.
  2. The settlement-truth cache path is redirected to ``tmp_path`` and the IEM
     provider is replaced with an offline stub, so the tests never hit the
     network. The *resolver logic* is untouched.
  3. Positions are opened via ``exchange.open_position`` rather than through a
     strategy signal, since weather trading was disabled in Phase 1
     (``WEATHER_TRADING_ENABLED = False``). The FR-F0.1 section at the bottom
     is the exception: it drives the real strategy -> ``SignalProcessorMixin``
     -> ``RiskManager`` -> exchange chain, because the defect it pins (no
     weather signal ever carried an ``expiration_time``, so the EXPIRATION
     CHECK never reached ``_settle_weather_position``) lived in that chain.
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

import src.core.weather_settlement as weather_settlement  # noqa: E402
from src.core.matching_engine import SimulatedExchange  # noqa: E402
from scripts.run_dashboard import OrchestratorEngine  # noqa: E402

WEATHER_TICKER = "KXHIGHNY-26JUL25-B86.5"  # between, floor 86 cap 87 -> "86 to 87"
WEATHER_STATION_DAY = "KNYC|2026-07-25"
CRYPTO_TICKER = "KXBTC15M-26JUL252130-30"

FORBIDDEN_WEATHER_REASONS = ("TIME_LIMIT", "CYCLE_RESET", "STOP_LOSS")


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


class _OfflineProvider:
    """Stands in for IEMCLIProvider so no test ever reaches the network."""

    def __init__(self):
        self.calls = []

    def fetch_daily_high(self, station, date, **kwargs):
        self.calls.append((station, date))
        return None


@pytest.fixture
def truth(tmp_path, monkeypatch):
    """A real settlement cache on disk, read by the real resolver."""
    cache_path = tmp_path / "settlement_cache.json"
    cache_path.write_text(json.dumps({"truth": {}, "markets": {}}), encoding="utf-8")
    monkeypatch.setattr(
        weather_settlement, "SETTLEMENT_CACHE_PATH", str(cache_path), raising=True
    )
    weather_settlement.reset_caches()
    offline = _OfflineProvider()
    weather_settlement._provider = offline

    class _Truth:
        path = cache_path
        provider = offline

        @staticmethod
        def publish(key, high):
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            blob["truth"][key] = {"high": high, "source": "iem_cli"}
            cache_path.write_text(json.dumps(blob), encoding="utf-8")
            weather_settlement._miss_log.clear()

    try:
        yield _Truth
    finally:
        weather_settlement.reset_caches()


@pytest.fixture
def exchange(tmp_path):
    """Persisting exchange pointed at a temp state file (never prod state)."""
    return SimulatedExchange(state_file=tmp_path / "exchange_state.json")


#: ``_open_weather`` default: the settlement day is still running. The tests
#: in the lifecycle sections below exercise the *pre-expiry* sweep (stops,
#: time limit, marks, ladder) on a ticker dated 2026-07-25; they used to rely
#: on ``expiration_time=None`` to keep that sweep away from the expiry branch,
#: which was the FR-F0.1 defect itself -- since the fix the exchange backfills
#: a missing weather expiration with the settlement-day close, and 2026-07-25
#: closed long ago. Tests that want expiry set ``pos["expiration_time"]`` by
#: hand, exactly as before.
_DAY_STILL_RUNNING = object()


def _open_weather(
    exchange,
    ticker=WEATHER_TICKER,
    entry=0.40,
    qty=10,
    stop_loss=0.0,
    expiration_time=_DAY_STILL_RUNNING,
    disable_profit_targets=False,
):
    """Open a weather position exactly as the production path would.

    ``disable_profit_targets`` defaults to **False**, the production value:
    ``src/bots/mixins.py`` passes ``getattr(sig, "disable_profit_targets",
    False)`` and no weather strategy sets the attribute. These helpers used to
    hardcode ``True``, which opted every test out of the profit-target ladder —
    the exact defect the ladder had (a weather position fully closed on two
    PROFIT_TARGET tranches, never reaching settlement) was invisible as a
    result. A test that opts out of the defect cannot detect it.
    """
    if expiration_time is _DAY_STILL_RUNNING:
        expiration_time = datetime.now().astimezone() + timedelta(days=1)
    exchange.open_position(
        symbol=ticker,
        side="buy",
        entry_price=entry,
        quantity=qty,
        stop_loss=stop_loss,
        expiration_time=expiration_time,
        strategy_name="Meteorologist V2",
        contract_side="YES",
        disable_profit_targets=disable_profit_targets,
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
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


# ======================================================================
# 1. TIME_LIMIT_MIN exemption
# ======================================================================


def test_weather_position_survives_time_limit(exchange, truth):
    pos = _open_weather(exchange)
    _age(pos, exchange.TIME_LIMIT_MIN * 2)  # double the 60-minute wall clock

    exchange.update_market("TEMP_KNYC", 84.0)

    assert pos in exchange.positions, "FR-1.5: weather is exempt from TIME_LIMIT"
    assert not exchange.closed_trades


def test_crypto_position_still_hits_time_limit(exchange, truth):
    """Behaviour preservation: the exemption is weather-only."""
    pos = _open_crypto(exchange)
    _age(pos, exchange.TIME_LIMIT_MIN * 2)

    exchange.update_market("BTC", 64000.0)

    assert pos not in exchange.positions
    assert exchange.closed_trades[-1]["reason"] == "TIME_LIMIT"


# ======================================================================
# 2. Stop-loss exemption
# ======================================================================


def test_weather_position_ignores_explicit_stop_loss(exchange, truth):
    pos = _open_weather(exchange, entry=0.60, stop_loss=0.40)
    _age(pos, 45)  # past the 30s grace period, inside TIME_LIMIT_MIN
    # A large adverse move: the market reprices the contract to a penny.
    exchange.update_market_price(WEATHER_TICKER, 0.02)

    exchange.update_market("TEMP_KNYC", 70.0)

    assert pos in exchange.positions, "FR-1.5: no stop-losses on binary weather"
    assert not exchange.closed_trades
    # It IS marked to the observed market price -- exempt from stops, not blind.
    assert pos["current_price"] == pytest.approx(0.02)
    assert pos["pnl"] == pytest.approx((0.02 - 0.60) * 10)


def test_weather_position_ignores_percentage_stop(exchange, truth):
    """The STOP_LOSS_PCT fallback fires with no explicit stop; not for weather."""
    pos = _open_weather(exchange, entry=0.60, stop_loss=0.0)
    _age(pos, 45)
    exchange.update_market_price(WEATHER_TICKER, 0.10)  # -83%, far past 15%

    exchange.update_market("TEMP_KNYC", 70.0)

    assert pos in exchange.positions
    assert not exchange.closed_trades


def test_weather_mark_holds_at_entry_without_an_observed_price(exchange, truth):
    """B3: no tanh estimator for weather -- no observed price, no synthetic mark."""
    pos = _open_weather(exchange, entry=0.40)
    _age(pos, 45)

    # A blistering 105F would have driven the tanh estimate toward 0.99 for the
    # old KXHIGH scale=10.0 path; with no observed Kalshi price the mark holds.
    exchange.update_market("TEMP_KNYC", 105.0)

    assert pos in exchange.positions
    assert pos["current_price"] == pytest.approx(0.40)
    assert pos["pnl"] == pytest.approx(0.0)


# ======================================================================
# 3. Cycle-boundary survival (real _rollover_positions)
# ======================================================================


def _rollover(exchange):
    """Drive the real cycle-boundary code with a stub ``self`` (seam 1)."""
    stub = types.SimpleNamespace(
        risk_manager=types.SimpleNamespace(exchange=exchange, active_positions=99)
    )
    survivors = OrchestratorEngine._rollover_positions(stub)
    return stub, survivors


def test_weather_survives_cycle_boundary_crypto_does_not(exchange, truth):
    weather = _open_weather(exchange, entry=0.40, qty=10)
    crypto = _open_crypto(exchange, entry=0.30, qty=10)
    weather_id, weather_entry, weather_qty = (
        weather["id"],
        weather["entry_price"],
        weather["quantity"],
    )

    stub, survivors = _rollover(exchange)

    # Weather survives, intact.
    assert weather in exchange.positions
    assert [p["id"] for p in survivors] == [weather_id]
    assert weather["id"] == weather_id
    assert weather["entry_price"] == pytest.approx(weather_entry)
    assert weather["quantity"] == weather_qty
    assert "reason" not in weather

    # Crypto is liquidated with CYCLE_RESET, exactly as before.
    assert crypto not in exchange.positions
    closed = exchange.closed_trades[-1]
    assert closed["symbol"] == CRYPTO_TICKER
    assert closed["reason"] == "CYCLE_RESET"

    # active_positions agrees with the surviving book.
    assert stub.risk_manager.active_positions == 1


def test_next_id_does_not_collide_with_a_survivor(exchange, truth):
    weather = _open_weather(exchange)
    _open_crypto(exchange)
    survivor_id = weather["id"]

    _rollover(exchange)

    assert exchange._next_id > survivor_id
    # And the next position actually opened gets a fresh id.
    new_pos = _open_crypto(exchange)
    assert new_pos["id"] != survivor_id
    assert len({p["id"] for p in exchange.positions}) == len(exchange.positions)


def test_survivor_is_persisted_at_the_boundary(exchange, truth):
    weather = _open_weather(exchange)
    _open_crypto(exchange)

    _rollover(exchange)

    state = json.loads(exchange._state_file.read_text(encoding="utf-8"))
    open_symbols = [p["symbol"] for p in state["positions"]]
    assert open_symbols == [WEATHER_TICKER]
    assert state["positions"][0]["id"] == weather["id"]


# ======================================================================
# 4. Settle the survivor at expiry through the FR-1.2 payoff
# ======================================================================


def test_survivor_settles_at_expiry_through_fr12_payoff(exchange, truth, logs):
    """Exit criterion 5, end to end: survive the boundary, then settle."""
    weather = _open_weather(exchange, entry=0.40, qty=10)
    _open_crypto(exchange)
    _rollover(exchange)
    survivor_id = weather["id"]

    # The contract expires; truth is published (86F -> inside the 86-87 band).
    weather["expiration_time"] = datetime.now() - timedelta(minutes=5)
    truth.publish(WEATHER_STATION_DAY, 86)

    exchange.update_market("TEMP_KNYC", 86.0)

    assert weather not in exchange.positions
    trade = next(t for t in exchange.closed_trades if t["id"] == survivor_id)

    # Settlement reason, not a lifecycle reason.
    assert trade["reason"] == "EXPIRATION"
    assert not trade["reason"].startswith(FORBIDDEN_WEATHER_REASONS)

    # FR-1.2 payoff: YES -> 1.00.
    assert trade["exit_price"] == pytest.approx(1.00)
    from src.core.fee_calculator import compute_fee

    expected = (1.00 - 0.40) * 10 - compute_fee(1.00, 10, is_maker=True).fee
    assert trade["pnl"] == pytest.approx(expected)

    # The record carries the settlement high and the bracket spec used.
    assert trade["settlement_high"] == pytest.approx(86.0)
    assert trade["settlement_spec"] == {
        "strike_type": "between",
        "floor_strike": 86.0,
        "cap_strike": 87.0,
    }
    assert trade["settlement_rule"] == "86 to 87"
    assert trade["settlement_outcome"] == "yes"

    # ...and so does the persisted exchange state (what the VM would show).
    state = json.loads(exchange._state_file.read_text(encoding="utf-8"))
    persisted = next(t for t in state["closed_trades"] if t["id"] == survivor_id)
    assert persisted["settlement_high"] == 86.0
    assert persisted["settlement_spec"]["strike_type"] == "between"
    assert persisted["reason"] == "EXPIRATION"

    assert any("FR-1.2 SETTLED" in m for m in logs.messages(logging.INFO))


def _live_settlement_day():
    """Ticker and cache key for a settlement day still running at the station.

    The PENDING/OVERDUE branch is chosen by how long ago the settlement day
    *closed* (:data:`SETTLEMENT_TRUTH_GRACE_HOURS`, 36 h), so a hardcoded date
    in the ticker decays: it starts inside the window and silently crosses out
    of it as the calendar advances, turning this test red days later for a
    reason that has nothing to do with the code. The module-level
    ``WEATHER_TICKER`` is fixed at 2026-07-25 and did exactly that.

    Deriving the day from the station's own clock keeps
    ``hours_since_settlement_day_close`` negative, so the PENDING branch is
    pinned on whatever day the suite runs.
    ``test_overdue_truth_escalates_to_error`` pins the OVERDUE branch with a
    deliberately long-past date, so both sides stay covered.
    """
    today = datetime.now(ZoneInfo("America/New_York")).date()
    ticker = f"KXHIGHNY-{today.strftime('%y%b%d').upper()}-B86.5"
    return ticker, f"KNYC|{today.isoformat()}"


def test_expired_weather_holds_open_until_truth_is_published(exchange, truth, logs):
    """Unpublished CLI product -> hold and retry, never a guessed outcome."""
    ticker, station_day = _live_settlement_day()

    # Fixture precondition: this test only exercises the PENDING branch while
    # the settlement day is inside the grace window. Assert it rather than
    # assume it, so a regression in the age helper surfaces here as a clear
    # precondition failure instead of as a confusing missing-log assertion.
    age_h = weather_settlement.hours_since_settlement_day_close(ticker)
    assert (
        age_h is not None and age_h <= weather_settlement.SETTLEMENT_TRUTH_GRACE_HOURS
    ), (
        f"fixture precondition: {ticker} must be within the "
        f"{weather_settlement.SETTLEMENT_TRUTH_GRACE_HOURS}h grace window "
        f"(age {age_h})"
    )

    weather = _open_weather(exchange, ticker=ticker, entry=0.40, qty=10)
    weather["expiration_time"] = datetime.now() - timedelta(minutes=5)

    exchange.update_market("TEMP_KNYC", 92.0)  # observed max is NOT truth

    assert weather in exchange.positions, "no truth -> no settlement"
    assert not exchange.closed_trades
    assert any(
        "SETTLEMENT_TRUTH_PENDING" in m for m in logs.messages(logging.INFO)
    ), logs.messages(logging.INFO)
    # The observed 92F must not have leaked in as a substitute for the CLI high.
    assert "settlement_high" not in weather

    # Truth arrives on the next sweep; the same position now settles. The key
    # must match the ticker's own settlement day, not the module constant.
    truth.publish(station_day, 92)
    exchange.update_market("TEMP_KNYC", 92.0)

    assert weather not in exchange.positions
    trade = exchange.closed_trades[-1]
    assert trade["reason"] == "EXPIRATION"
    assert trade["exit_price"] == pytest.approx(0.00)  # 92 is above the 86-87 band
    assert trade["settlement_high"] == pytest.approx(92.0)


def test_overdue_truth_escalates_to_error(exchange, truth, logs):
    """Past the grace window the pending log escalates and alerts."""
    alerts = []
    exchange.on_alert = alerts.append
    old_ticker = "KXHIGHNY-26JAN02-B86.5"  # settlement day long past
    exchange.open_position(
        symbol=old_ticker,
        side="buy",
        entry_price=0.40,
        quantity=10,
        strategy_name="Meteorologist V2",
        disable_profit_targets=True,
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
        expiration_time=datetime.now() - timedelta(days=1),
    )
    pos = exchange.positions[-1]

    exchange.update_market("TEMP_KNYC", 40.0)

    assert pos in exchange.positions
    assert any(
        "SETTLEMENT_TRUTH_OVERDUE" in m for m in logs.messages(logging.ERROR)
    ), logs.messages(logging.ERROR)
    assert any("SETTLEMENT TRUTH OVERDUE" in a for a in alerts), alerts


# ======================================================================
# 5. The hard invariant guard
# ======================================================================


@pytest.mark.parametrize(
    "reason", ["TIME_LIMIT", "CYCLE_RESET", "STOP_LOSS_PRICE (0.25)", "STOP_LOSS_PCT"]
)
def test_lifecycle_close_of_a_weather_position_is_refused(
    exchange, truth, logs, reason
):
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_weather(exchange)

    exchange._close_position(pos, 0.10, reason=reason)

    assert pos in exchange.positions, f"{reason} must not close a weather position"
    assert not exchange.closed_trades
    assert any(
        "FR-1.5 VIOLATION REFUSED" in m for m in logs.messages(logging.ERROR)
    ), logs.messages(logging.ERROR)
    assert any("FR-1.5 REFUSED" in a for a in alerts), alerts


def test_guard_does_not_block_crypto_lifecycle_closes(exchange, truth):
    """The guard is weather-only: crypto still closes on every legacy reason."""
    pos = _open_crypto(exchange)
    exchange._close_position(pos, 0.20, reason="TIME_LIMIT")
    assert pos not in exchange.positions
    assert exchange.closed_trades[-1]["reason"] == "TIME_LIMIT"


def test_guard_does_not_block_weather_settlement(exchange, truth):
    """...and it does not block the reasons weather IS allowed to close on."""
    pos = _open_weather(exchange)
    exchange._close_position(pos, 86, reason="EXPIRATION")
    assert pos not in exchange.positions
    assert exchange.closed_trades[-1]["reason"] == "EXPIRATION"


# ======================================================================
# 6. Whole-run scan: exit criterion 5's negative assertion
# ======================================================================


def test_no_weather_position_ever_closes_on_a_lifecycle_reason(exchange, truth):
    """Synthetic run: time limits, stops, a cycle boundary, then settlement."""
    weather_a = _open_weather(exchange, entry=0.40, qty=10)
    weather_b = _open_weather(
        exchange, ticker="KXHIGHCHI-26JUL25-B86.5", entry=0.55, stop_loss=0.45
    )
    crypto = _open_crypto(exchange)

    # Age everything well past the time limit and the grace period.
    for pos in (weather_a, weather_b, crypto):
        _age(pos, 180)

    # Adverse marks that would trip every stop the engine has.
    exchange.update_market_price(WEATHER_TICKER, 0.01)
    exchange.update_market_price("KXHIGHCHI-26JUL25-B86.5", 0.01)

    exchange.update_market("TEMP_KNYC", 70.0)
    exchange.update_market("TEMP_KMDW", 70.0)  # exercises the new KMDW mapping
    exchange.update_market("BTC", 63000.0)

    # A cycle boundary in the middle of the run.
    _rollover(exchange)

    # Then both weather positions expire with truth available.
    truth.publish(WEATHER_STATION_DAY, 86)
    truth.publish("KMDW|2026-07-25", 90)
    for pos in (weather_a, weather_b):
        pos["expiration_time"] = datetime.now() - timedelta(minutes=1)
    exchange.update_market("TEMP_KNYC", 86.0)
    exchange.update_market("TEMP_KMDW", 90.0)

    weather_trades = [t for t in exchange.closed_trades if "KXHIGH" in t["symbol"]]
    assert len(weather_trades) == 2, exchange.closed_trades
    for trade in weather_trades:
        assert not str(trade["reason"]).startswith(FORBIDDEN_WEATHER_REASONS), (
            f"{trade['symbol']} closed on a forbidden lifecycle reason: "
            f"{trade['reason']}"
        )
        assert trade["reason"] == "EXPIRATION"
        assert "settlement_high" in trade

    # NY high 86 -> inside the band -> YES; CHI high 90 -> above it -> NO.
    by_symbol = {t["symbol"]: t for t in weather_trades}
    assert by_symbol[WEATHER_TICKER]["exit_price"] == pytest.approx(1.00)
    assert by_symbol["KXHIGHCHI-26JUL25-B86.5"]["exit_price"] == pytest.approx(0.00)

    # ...while the crypto position went through the legacy lifecycle unchanged.
    crypto_trades = [t for t in exchange.closed_trades if "KXBTC" in t["symbol"]]
    assert crypto_trades and crypto_trades[0]["reason"] == "TIME_LIMIT"


def _spy_closes(exchange, monkeypatch):
    """Record every ``_close_position`` call, then delegate to the real one.

    Lets a test assert a position was never even OFFERED for closure, which is
    what pins an exemption *independently of the hard guard*. Without this the
    guard silently absorbs a reverted exemption and no test fails.
    """
    calls = []
    real = exchange._close_position

    def spy(pos, final_spot_price, reason="MARKET"):
        calls.append(
            {"id": pos.get("id"), "symbol": pos.get("symbol"), "reason": reason}
        )
        return real(pos, final_spot_price, reason=reason)

    monkeypatch.setattr(exchange, "_close_position", spy)
    return calls


def _weather_closes(calls):
    return [c for c in calls if "KXHIGH" in str(c["symbol"])]


def _guard_fired(logs):
    return [m for m in logs.messages(logging.ERROR) if "FR-1.5 VIOLATION REFUSED" in m]


# ======================================================================
# 7. Profit-target ladder exemption (FINDING 1)
#
# The production signal path leaves disable_profit_targets at its default
# False, so a weather position used to be handed the {+0.15, +0.30} ladder and
# fully exited on it at an invented mark -- never reaching settlement, never
# carrying a settlement_high. FR-1.5 requires the opposite.
# ======================================================================


def test_production_default_weather_position_carries_no_ladder(exchange, truth):
    pos = _open_weather(exchange)  # production default: ladder NOT disabled
    assert pos["profit_targets"] == [], (
        "FR-1.5: a weather bracket is held to settlement, so open_position must "
        "withhold the ladder regardless of what the caller asked for"
    )


def test_crypto_still_gets_the_ladder(exchange, truth):
    """Behaviour preservation: the ladder is withheld for weather only."""
    pos = _open_crypto(exchange)
    assert [t["move"] for t in pos["profit_targets"]] == [0.15, 0.30]


def test_weather_holds_through_a_favourable_mark_then_settles(exchange, truth):
    """The verifier's exact repro, end to end, on production defaults."""
    pos = _open_weather(exchange, entry=0.40, qty=5)
    _age(pos, 45)

    # Observed Kalshi mark 0.72 vs entry 0.40 = +0.32, past BOTH ladder rungs.
    exchange.update_market_price(WEATHER_TICKER, 0.72)
    exchange.update_market("TEMP_KNYC", 88.0)

    assert pos in exchange.positions, "a +0.32 move must not exit a weather bracket"
    assert exchange.closed_trades == []
    assert pos["quantity"] == 5, "no partial profit-target tranche either"
    assert pos["current_price"] == pytest.approx(0.72)  # marked, not exited

    # It settles at expiry through FR-1.2, with a settlement_high.
    pos["expiration_time"] = datetime.now() - timedelta(minutes=5)
    truth.publish(WEATHER_STATION_DAY, 86)
    exchange.update_market("TEMP_KNYC", 86.0)

    trade = exchange.closed_trades[-1]
    assert trade["reason"] == "EXPIRATION"
    assert trade["exit_price"] == pytest.approx(1.00)
    assert trade["settlement_high"] == pytest.approx(86.0)
    assert trade["quantity"] == 5, "the whole position settled, not a remainder"


def test_crypto_still_exits_on_the_ladder(exchange, truth):
    """The ladder itself is untouched for the families that should use it."""
    pos = _open_crypto(exchange, entry=0.30, qty=10)
    _age(pos, 5)
    exchange.update_market_price(CRYPTO_TICKER, 0.50)  # +0.20 -> first rung

    exchange.update_market("BTC", 64000.0)

    assert exchange.closed_trades, "crypto must still take profit targets"
    assert exchange.closed_trades[-1]["reason"].startswith("PROFIT_TARGET")


def test_legacy_weather_position_with_a_ladder_is_never_exited_on_it(
    exchange, truth, logs
):
    """A position restored from a state file written before the fix.

    open_position no longer attaches the ladder, but ``exchange_state.json``
    on the VM still holds weather positions that carry one, so the runtime
    sweep must refuse to act on it.
    """
    pos = _open_weather(exchange)
    pos["profit_targets"] = [  # exactly what a pre-fix state file contains
        {"move": 0.15, "exit_pct": 0.50, "hit": False},
        {"move": 0.30, "exit_pct": 1.00, "hit": False},
    ]
    _age(pos, 45)
    exchange.update_market_price(WEATHER_TICKER, 0.90)  # +0.50, past both rungs

    exchange.update_market("TEMP_KNYC", 88.0)

    assert pos in exchange.positions
    assert exchange.closed_trades == []
    assert pos["quantity"] == 10
    assert all(not t["hit"] for t in pos["profit_targets"])


# ======================================================================
# 8. Guard completeness (FINDING 2)
#
# The guard is a whitelist: only settlement reasons may close a weather
# position. The prefix tuple is documentation, and the source walk below
# proves the documentation is complete.
# ======================================================================


@pytest.mark.parametrize(
    "reason",
    [
        "TIME_LIMIT",
        "CYCLE_RESET",
        "STOP_LOSS_PRICE (0.25)",
        "STOP_LOSS_PCT",
        "TAKE_PROFIT",
        "PROFIT_TARGET (+0.15)",
        "EARLY_SETTLEMENT",
        "MARKET",
        "SOME_FUTURE_REASON",
    ],
)
def test_every_non_settlement_close_of_weather_is_refused(
    exchange, truth, logs, reason
):
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_weather(exchange)

    exchange._close_position(pos, 0.10, reason=reason)

    assert pos in exchange.positions, f"{reason} must not close a weather position"
    assert not exchange.closed_trades
    assert _guard_fired(logs), logs.messages(logging.ERROR)
    assert any("FR-1.5 REFUSED" in a for a in alerts), alerts


def test_partial_profit_target_close_is_refused_at_the_hard_guard(
    exchange, truth, logs
):
    """_check_profit_targets never routes through _close_position, so it needs
    its own guard -- this is the path the verifier drove to a full exit."""
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_weather(exchange)
    pos["profit_targets"] = [{"move": 0.15, "exit_pct": 0.50, "hit": False}]

    closed = exchange._check_profit_targets(pos, 0.90)  # +0.50 move

    assert closed is False
    assert pos["quantity"] == 10, "no tranche may be taken off a weather bracket"
    assert not exchange.closed_trades
    assert _guard_fired(logs), logs.messages(logging.ERROR)
    assert any("FR-1.5 REFUSED" in a for a in alerts), alerts


def test_guard_alerts_once_per_position_and_reason(exchange, truth, logs):
    """PRD §6 budgets <1 false alarm/day: a repeating refusal must not storm."""
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_weather(exchange)

    for _ in range(20):
        exchange._close_position(pos, 0.10, reason="TIME_LIMIT")

    assert len(alerts) == 1, alerts
    assert len(_guard_fired(logs)) == 20, "every attempt still hits the ERROR log"


def _reason_literals_in(path):
    """Every literal close reason written in a source file."""
    import re

    text = path.read_text(encoding="utf-8")
    found = set()
    for pattern in (
        r'reason\s*=\s*f?"([^"]+)"',
        r'\["reason"\]\s*=\s*f?"([^"]+)"',
    ):
        for raw in re.findall(pattern, text):
            # f-string placeholders: keep the literal prefix only, e.g.
            # 'STOP_LOSS_PRICE ({pos[...]})' -> 'STOP_LOSS_PRICE ('
            found.add(raw.split("{")[0].strip())
    return found


def test_every_close_reason_in_the_sources_is_classified(exchange):
    """Provable completeness: no reason string can silently bypass the guard.

    Walks the production sources for literal ``reason=`` strings and asserts
    each one is either a declared settlement reason (allowed on weather) or is
    actually refused by the live guard. A new reason added anywhere in these
    files fails here unless it is deliberately classified.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    sources = [
        repo / "src" / "core" / "matching_engine.py",
        repo / "scripts" / "run_dashboard.py",
        repo / "src" / "backtest" / "engine.py",
    ]
    literals = set()
    for src in sources:
        assert src.exists(), src
        literals |= _reason_literals_in(src)

    # Sanity: the walk actually finds the reasons we know exist.
    assert {"TIME_LIMIT", "CYCLE_RESET", "EXPIRATION", "TAKE_PROFIT"} <= literals

    settlement = set(SimulatedExchange._WEATHER_SETTLEMENT_CLOSE_REASONS)
    probe = {"symbol": WEATHER_TICKER, "id": -1}
    unclassified = []
    for literal in sorted(literals):
        refused = exchange._weather_close_refused(dict(probe), literal, "scan")
        documented = literal.startswith(
            SimulatedExchange._WEATHER_FORBIDDEN_CLOSE_PREFIXES
        )
        if literal in settlement:
            assert not refused, f"{literal} is a settlement reason but was refused"
            continue
        if not (refused and documented):
            unclassified.append((literal, refused, documented))
    assert not unclassified, (
        "close reasons neither whitelisted as settlement nor refused+documented "
        f"by _WEATHER_FORBIDDEN_CLOSE_PREFIXES: {unclassified}"
    )


# ======================================================================
# 9. Position-id allocation (FINDING 3)
#
# The trade journal, the reconciler and exit criterion 5's own verification
# join a position to its closed row by id. A reissued id merges two trades in
# the evidence. FR-1.5 makes reissue the NORMAL case: a high-id weather
# survivor pins _next_id above a dense closed list, which is exactly the
# condition that defeats the legacy len(open)+len(closed)+1 floor.
# ======================================================================


def test_allocated_id_never_collides_with_a_closed_trade(exchange, truth):
    weather = _open_weather(exchange)  # id 1, will survive
    for _ in range(4):  # ids 2..5, all closed
        crypto = _open_crypto(exchange)
        exchange._close_position(crypto, 0.20, reason="TIME_LIMIT")

    fresh = _open_crypto(exchange)

    closed_ids = {t["id"] for t in exchange.closed_trades}
    assert (
        fresh["id"] not in closed_ids
    ), f"reissued id {fresh['id']}; closed ids are {sorted(closed_ids)}"
    assert fresh["id"] != weather["id"]


def test_next_id_is_persisted_and_survives_a_restart(exchange, truth, tmp_path):
    _open_weather(exchange)
    crypto = _open_crypto(exchange)
    exchange._close_position(crypto, 0.20, reason="TIME_LIMIT")

    state = json.loads(exchange._state_file.read_text(encoding="utf-8"))
    assert "next_position_id" in state, "the id floor must be part of the payload"
    assert state["next_position_id"] == exchange._next_id

    restored = SimulatedExchange(state_file=exchange._state_file)
    assert restored._next_id >= exchange._next_id


def test_restart_over_a_dense_closed_list_does_not_reissue_ids(tmp_path):
    """The verifier's exact repro: open [5, 900], closed [1,2,3,4,901,902,903]."""
    state_file = tmp_path / "exchange_state.json"

    def _row(pid, symbol, **extra):
        row = {
            "id": pid,
            "symbol": symbol,
            "side": "buy",
            "entry_price": 0.40,
            "current_price": 0.40,
            "quantity": 5,
            "original_quantity": 5,
            "open_time": datetime.now().isoformat(),
            "pnl": 0.0,
            "stop_loss": 0.0,
            "trailing_rules": None,
            "trailing_activated": False,
            "expiration_time": None,
            "strategy_name": "legacy",
            "last_market_price": 0,
            "contract_side": "YES",
            "strike": None,
            "strike_type": "between",
            "floor_strike": 86,
            "cap_strike": 87,
            "profit_targets": [],
            "entry_fee": 0.0,
            "is_maker": True,
        }
        row.update(extra)
        return row

    payload = {
        "schema_version": 1,
        "saved_at": datetime.now().isoformat(),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_fees_paid": 0.0,
        "maker_fills": 0,
        "taker_fills": 0,
        "cumulative_realized_pnl": 0.0,
        "cumulative_fees_paid": 0.0,
        "cumulative_entry_fees": 0.0,
        # NOTE: no "next_position_id" -- a legacy file, the worst case.
        "positions": [_row(5, CRYPTO_TICKER), _row(900, WEATHER_TICKER)],
        "closed_trades": [
            _row(pid, CRYPTO_TICKER, pnl=0.0, exit_price=0.5, reason="TIME_LIMIT")
            for pid in (1, 2, 3, 4, 901, 902, 903)
        ],
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    restored = SimulatedExchange(state_file=state_file)
    assert sorted(p["id"] for p in restored.positions) == [5, 900]

    restored.open_position(
        symbol=CRYPTO_TICKER,
        side="buy",
        entry_price=0.30,
        quantity=1,
        strategy_name="post-restart",
        strike=64000.0,
    )
    new_id = restored.positions[-1]["id"]

    closed_ids = {t["id"] for t in restored.closed_trades}
    open_ids = {p["id"] for p in restored.positions[:-1]}
    assert new_id not in closed_ids, (
        f"post-restart id {new_id} collides with a closed trade "
        f"({sorted(closed_ids)})"
    )
    assert new_id not in open_ids
    assert new_id > 903, "ids must resume above the whole known history"


def test_pre_existing_duplicate_ids_load_without_crashing_and_are_logged(
    tmp_path, logs
):
    """The VM state already has 158 duplicate ids among 1583 closed rows."""
    state_file = tmp_path / "exchange_state.json"
    row = {
        "id": 7,
        "symbol": CRYPTO_TICKER,
        "side": "buy",
        "entry_price": 0.40,
        "current_price": 0.40,
        "quantity": 5,
        "original_quantity": 5,
        "open_time": datetime.now().isoformat(),
        "pnl": 0.0,
        "stop_loss": 0.0,
        "trailing_rules": None,
        "trailing_activated": False,
        "expiration_time": None,
        "strategy_name": "legacy",
        "last_market_price": 0,
        "contract_side": "YES",
        "strike": 64000.0,
        "profit_targets": [],
        "entry_fee": 0.0,
        "is_maker": True,
        "exit_price": 0.5,
        "reason": "TIME_LIMIT",
    }
    state_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "saved_at": datetime.now().isoformat(),
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_fees_paid": 0.0,
                "maker_fills": 0,
                "taker_fills": 0,
                "cumulative_realized_pnl": 0.0,
                "cumulative_fees_paid": 0.0,
                "cumulative_entry_fees": 0.0,
                "positions": [],
                "closed_trades": [dict(row), dict(row), dict(row, id=8)],
            }
        ),
        encoding="utf-8",
    )

    restored = SimulatedExchange(state_file=state_file)

    assert len(restored.closed_trades) == 3, "history is preserved, not repaired"
    assert any(
        "POSITION ID DUPLICATES" in m for m in logs.messages(logging.WARNING)
    ), logs.messages(logging.WARNING)

    restored.open_position(
        symbol=CRYPTO_TICKER,
        side="buy",
        entry_price=0.30,
        quantity=1,
        strategy_name="post-restart",
        strike=64000.0,
    )
    assert restored.positions[-1]["id"] > 8


# ======================================================================
# 10. Each FR-1.5 defence, pinned INDEPENDENTLY of the hard guard (FINDING 4)
#
# A mutation run showed the time-limit exemption, the stop-loss exemption and
# the rollover carve-out could each be reverted with 326 tests still green:
# the guard absorbed the failure. These tests assert the position is never even
# OFFERED for closure, so reverting an exemption fails a test instead of
# silently degrading into a per-tick ERROR + alert storm.
# ======================================================================


def test_time_limit_exemption_never_offers_the_position_for_closure(
    exchange, truth, logs, monkeypatch
):
    pos = _open_weather(exchange)
    _age(pos, exchange.TIME_LIMIT_MIN * 3)
    calls = _spy_closes(exchange, monkeypatch)

    exchange.update_market("TEMP_KNYC", 84.0)

    assert _weather_closes(calls) == [], (
        "the TIME_LIMIT exemption must skip the close outright, not lean on "
        f"the guard to refuse it: {calls}"
    )
    assert _guard_fired(logs) == []
    assert pos in exchange.positions


def test_stop_loss_exemption_never_offers_the_position_for_closure(
    exchange, truth, logs, monkeypatch
):
    pos = _open_weather(exchange, entry=0.60, stop_loss=0.40)
    _age(pos, 45)
    exchange.update_market_price(WEATHER_TICKER, 0.02)  # would trip every stop
    calls = _spy_closes(exchange, monkeypatch)

    exchange.update_market("TEMP_KNYC", 70.0)

    assert _weather_closes(calls) == [], calls
    assert _guard_fired(logs) == []
    assert pos in exchange.positions


def test_profit_target_exemption_never_reaches_the_ladder(
    exchange, truth, logs, monkeypatch
):
    """Defence 2 of 3: the call site skips weather before the ladder runs."""
    pos = _open_weather(exchange)
    pos["profit_targets"] = [{"move": 0.15, "exit_pct": 0.50, "hit": False}]
    _age(pos, 45)
    exchange.update_market_price(WEATHER_TICKER, 0.95)

    seen = []
    real = exchange._check_profit_targets
    monkeypatch.setattr(
        exchange,
        "_check_profit_targets",
        lambda p, price: (seen.append(p.get("symbol")), real(p, price))[1],
    )

    exchange.update_market("TEMP_KNYC", 88.0)

    assert [
        s for s in seen if "KXHIGH" in str(s)
    ] == [], f"the ladder must not even be consulted for weather: {seen}"
    assert _guard_fired(logs) == []
    assert pos in exchange.positions


def test_rollover_carveout_never_offers_the_position_for_closure(
    exchange, truth, logs, monkeypatch
):
    weather = _open_weather(exchange)
    _open_crypto(exchange)
    calls = _spy_closes(exchange, monkeypatch)

    _rollover(exchange)

    assert (
        _weather_closes(calls) == []
    ), f"the rollover carve-out must skip weather entirely: {calls}"
    assert [c["reason"] for c in calls] == ["CYCLE_RESET"], calls  # crypto only
    assert _guard_fired(logs) == []
    assert weather in exchange.positions


def test_a_normal_weather_tick_raises_no_fr15_alerts(exchange, truth, logs):
    """PRD §6: <1 false alarm/day. An ordinary tick must be silent."""
    alerts = []
    exchange.on_alert = alerts.append
    pos = _open_weather(exchange, entry=0.40, qty=10)
    _age(pos, exchange.TIME_LIMIT_MIN * 2)

    for mark in (0.02, 0.40, 0.95):  # adverse, flat, favourable
        exchange.update_market_price(WEATHER_TICKER, mark)
        exchange.update_market("TEMP_KNYC", 84.0)

    _rollover(exchange)

    truth.publish(WEATHER_STATION_DAY, 86)
    pos["expiration_time"] = datetime.now() - timedelta(minutes=1)
    exchange.update_market("TEMP_KNYC", 86.0)

    assert alerts == [], alerts
    assert _guard_fired(logs) == [], logs.messages(logging.ERROR)
    assert exchange.closed_trades[-1]["reason"] == "EXPIRATION"


def test_kmdw_maps_to_chicago_positions(exchange, truth):
    """The bot emits TEMP_KMDW; the old symbol_map had no KMDW entry at all."""
    pos = _open_weather(exchange, ticker="KXHIGHCHI-26JUL25-B86.5", entry=0.40)
    _age(pos, 45)
    exchange.update_market_price("KXHIGHCHI-26JUL25-B86.5", 0.75)

    exchange.update_market("TEMP_KMDW", 88.0)

    assert pos["current_price"] == pytest.approx(
        0.75
    ), "TEMP_KMDW must route to the KXHIGHCHI position"


# ======================================================================
# 11. PRD FR-F0.1: every weather signal carries a tz-aware expiration_time
# ======================================================================
#
# Confirmed defect (2026-09-02): ``pos["expiration_time"]`` was set only from
# ``sig.expiration_time`` and no weather strategy ever set it, so the
# EXPIRATION CHECK in ``update_market`` never ran for a weather position and
# ``_settle_weather_position`` was unreachable from the live path. maia's
# three 2026-09-01 positions were still open 27 h after their settlement day
# closed. The stamp now lives at the strategy return boundary
# (``bracket_payoff.attach_spec_to_signals``) with backfills in
# ``open_position`` and ``_load_state``, all reading one helper:
# ``weather_settlement.settlement_close_for``.

import src.core.matching_engine as matching_engine  # noqa: E402
import src.strategies.weather_strategy as weather_strategy  # noqa: E402
from src.bots.mixins import SignalProcessorMixin  # noqa: E402
from src.core.bracket_payoff import attach_spec_to_signals  # noqa: E402
from src.core.interfaces import MarketData, TradeSignal  # noqa: E402
from src.core.risk_manager import RiskManager  # noqa: E402
from src.core.weather_settlement import settlement_close_for  # noqa: E402
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2  # noqa: E402

ET = ZoneInfo("America/New_York")

# The three positions open on maia (opened 2026-09-01 13:50 ET, id 1-3).
MAIA_POSITIONS = [
    # (symbol, strike_type, floor, cap, expected settlement close)
    (
        "KXHIGHNY-26SEP01-T83",
        "greater",
        83,
        None,
        datetime(2026, 9, 2, 0, 0, tzinfo=ZoneInfo("America/New_York")),
    ),
    (
        "KXHIGHLAX-26SEP01-B76.5",
        "between",
        76,
        77,
        datetime(2026, 9, 2, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
    ),
    (
        "KXHIGHMIA-26SEP01-B89.5",
        "between",
        89,
        90,
        datetime(2026, 9, 2, 0, 0, tzinfo=ZoneInfo("America/New_York")),
    ),
]


class _Clock(datetime):
    """A ``datetime`` whose ``now()`` is pinned; everything else is inherited.

    Substituted for a module's ``datetime`` name so ``isinstance`` checks,
    ``fromisoformat`` and arithmetic in that module keep working unchanged.
    """

    _now = None

    @classmethod
    def now(cls, tz=None):
        """Aware in ``tz`` when asked; otherwise naive host-local, like the real one."""
        current = cls._now
        if tz is not None:
            return current.astimezone(tz)
        return current.astimezone().replace(tzinfo=None)

    @classmethod
    def set(cls, when):
        cls._now = when


class _Dashboard:
    """The two calls ``_process_signals`` makes on the dashboard."""

    def __init__(self):
        self.signals = []

    def log(self, *_args, **_kwargs):
        pass

    def record_signal(self, sig, status=None, strategy_name=None):
        self.signals.append((sig, status, strategy_name))


class _Bot(SignalProcessorMixin):
    """The mixin as WeatherBot uses it; no provider, no tick loop."""


def _won_market(symbol, strike_type, floor, cap, observed_max):
    """A market the V2 WINNER GUARD emits on: a one-sided bracket already met.

    Shaped as ``kalshi_provider._parse_market_data`` + the weather bot's
    observation enrichment emit it. The guard path is the one deterministic
    emit in ``_analyze`` (no forecast model, no velocity window), so it is the
    right way to get a *real* strategy signal into the chain.
    """
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(),
        price=0.60,
        volume=100,
        bid=0.55,
        ask=0.60,
        extra={
            "status": "active",
            "source": "live_metar",
            "metar_age_seconds": 30,
            "no_bid": 0.40,
            "no_ask": 0.45,
            "strike": floor,
            "strike_type": strike_type,
            "floor_strike": floor,
            "cap_strike": cap,
            "temperature_f": observed_max,
            "max_temp_today_f": observed_max,
            "forecast": [{"isDaytime": True, "temperature": observed_max}],
        },
    )


def _settle_spy(exchange, monkeypatch):
    calls = []
    real = exchange._settle_weather_position

    def spy(pos):
        calls.append(pos["symbol"])
        return real(pos)

    monkeypatch.setattr(exchange, "_settle_weather_position", spy)
    return calls


def test_signal_path_stamps_settlement_close_then_settles(truth, monkeypatch, logs):
    """Strategy -> mixin -> risk manager -> exchange, nothing set by hand.

    The stamp must come out of the real strategy's return boundary and survive
    the whole executed-signal chain onto the position; then, once the clock
    passes it, the position must leave the book through
    ``_settle_weather_position`` -- and only once truth is published.
    """
    symbol, strike_type, floor, cap, expected_close = MAIA_POSITIONS[0]
    assert expected_close == settlement_close_for(symbol)

    # 13:50 ET on the settlement day: inside the 10:00-13:59 trading window,
    # the observed max (85F) is already above the `greater` floor (83 -> YES
    # at >= 84), so the WINNER GUARD emits a BUY YES at the ask.
    _Clock.set(datetime(2026, 9, 1, 13, 50, tzinfo=ET))
    monkeypatch.setattr(weather_strategy, "datetime", _Clock)
    signals = WeatherArbitrageStrategyV2().analyze(
        _won_market(symbol, strike_type, floor, cap, observed_max=85.0)
    )
    assert signals, "fixture precondition: the WINNER GUARD must emit here"
    for sig in signals:
        assert sig.expiration_time is not None, "FR-F0.1: unstamped weather signal"
        assert sig.expiration_time.tzinfo is not None, "FR-F0.1: naive expiration"
        assert sig.expiration_time == expected_close

    # The real gauntlet: slot check, Kelly, EV gate, check_order, record_execution.
    risk = RiskManager(starting_balance=3000.0)
    exchange = risk.exchange
    dashboard = _Dashboard()
    assert _Bot()._process_signals(signals, "Meteorologist V2", risk, dashboard)
    assert [s for s in dashboard.signals if s[1] == "EXECUTED"]

    (pos,) = exchange.positions
    assert pos["symbol"] == symbol
    assert pos["expiration_time"] == expected_close
    assert pos["expiration_time"].tzinfo is not None
    assert pos["expiration_time"].utcoffset() == expected_close.utcoffset()

    # Before the close the sweep must not touch the settlement branch.
    settle_calls = _settle_spy(exchange, monkeypatch)
    monkeypatch.setattr(matching_engine, "datetime", _Clock)
    _Clock.set(expected_close - timedelta(minutes=1))
    exchange.update_market("TEMP_KNYC", 85.0)
    assert pos in exchange.positions
    assert settle_calls == []

    # The clock passes the close; truth is not published -> hold, via the
    # settlement helper, never a lifecycle close.
    _Clock.set(expected_close + timedelta(minutes=1))
    exchange.update_market("TEMP_KNYC", 85.0)
    assert pos in exchange.positions
    assert settle_calls == [symbol]
    assert not exchange.closed_trades

    # Truth arrives (85F >= 84 -> YES); the same sweep now settles it.
    truth.publish("KNYC|2026-09-01", 85)
    exchange.update_market("TEMP_KNYC", 85.0)
    assert pos not in exchange.positions
    assert settle_calls == [symbol, symbol]
    (trade,) = exchange.closed_trades
    assert trade["id"] == pos["id"]
    assert trade["reason"] == "EXPIRATION"
    assert not trade["reason"].startswith(FORBIDDEN_WEATHER_REASONS)
    assert trade["exit_price"] == pytest.approx(1.00)
    assert trade["settlement_high"] == pytest.approx(85.0)
    assert trade["settlement_outcome"] == "yes"
    assert any("FR-1.2 SETTLED" in m for m in logs.messages(logging.INFO))


def test_caller_supplied_expiration_is_never_overridden(exchange, tmp_path):
    """Signal, open_position and _load_state all keep an explicit value."""
    symbol = MAIA_POSITIONS[0][0]
    custom = datetime(2026, 9, 1, 23, 0, tzinfo=ZoneInfo("UTC"))
    assert custom != settlement_close_for(symbol)

    # Signal boundary.
    sig = TradeSignal(
        symbol=symbol, side="buy", quantity=1, limit_price=0.5, expiration_time=custom
    )
    (sig,) = attach_spec_to_signals([sig], _won_market(symbol, "greater", 83, None, 85))
    assert sig.expiration_time == custom

    # Exchange open.
    pos = _open_weather(exchange, ticker=symbol, expiration_time=custom)
    assert pos["expiration_time"] == custom

    # Restart: the persisted ISO string carries its offset and comes back
    # tz-aware and equal, with no backfill on top of it.
    exchange._save_state()
    restored = SimulatedExchange(state_file=exchange._state_file)
    (again,) = restored.positions
    assert again["expiration_time"] == custom
    assert again["expiration_time"].tzinfo is not None


def _legacy_row(pid, symbol, strike_type, floor, cap, **extra):
    """A position row as maia's pre-FR-F0.1 exchange_state.json holds it."""
    row = {
        "id": pid,
        "symbol": symbol,
        "side": "buy",
        "entry_price": 0.40,
        "current_price": 0.40,
        "quantity": 5,
        "original_quantity": 5,
        "open_time": "2026-09-01T17:50:00",
        "pnl": 0.0,
        "stop_loss": 0.0,
        "trailing_rules": None,
        "trailing_activated": False,
        "expiration_time": None,
        "strategy_name": "Meteorologist V2",
        "last_market_price": 0,
        "contract_side": "YES",
        "strike": floor,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "profit_targets": [],
        "entry_fee": 0.0,
        "is_maker": False,
        "fill_type": "taker_assumed",
    }
    row.update(extra)
    return row


def _legacy_state(positions):
    return {
        "schema_version": 1,
        "saved_at": "2026-09-01T17:50:00",
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_fees_paid": 0.0,
        "maker_fills": 0,
        "taker_fills": len(positions),
        "cumulative_realized_pnl": 0.0,
        "cumulative_fees_paid": 0.0,
        "cumulative_entry_fees": 0.0,
        "next_position_id": len(positions) + 1,
        "positions": positions,
        "closed_trades": [],
    }


def test_load_state_backfills_the_maia_positions(tmp_path, logs):
    """maia's exchange_state.json: three weather rows with no expiration."""
    state_file = tmp_path / "exchange_state.json"
    rows = [
        _legacy_row(i, sym, st, lo, hi)
        for i, (sym, st, lo, hi, _close) in enumerate(MAIA_POSITIONS, start=1)
    ]
    state_file.write_text(json.dumps(_legacy_state(rows)), encoding="utf-8")

    restored = SimulatedExchange(state_file=state_file)

    assert [p["id"] for p in restored.positions] == [1, 2, 3]
    for pos, (sym, _st, _lo, _hi, expected_close) in zip(
        restored.positions, MAIA_POSITIONS
    ):
        assert pos["symbol"] == sym
        exp = pos["expiration_time"]
        assert exp is not None, f"{sym}: not backfilled"
        assert exp.tzinfo is not None, f"{sym}: backfilled naive"
        assert exp == expected_close
        # Same instant in the station's own zone, not a UTC-flavoured midnight.
        assert exp.utcoffset() == expected_close.utcoffset()
    # LAX closes three hours after NY/MIA in absolute terms.
    ny, lax, mia = (p["expiration_time"] for p in restored.positions)
    assert lax - ny == timedelta(hours=3)
    assert mia == ny

    # One INFO line per repaired position.
    backfill_lines = [
        m for m in logs.messages(logging.INFO) if "BACKFILL expiration_time" in m
    ]
    assert len(backfill_lines) == 3
    for sym, *_rest in MAIA_POSITIONS:
        assert any(sym in m for m in backfill_lines), sym

    # Round trip: the repaired state was persisted with offsets, and a second
    # restart reads it back tz-aware and equal without repairing again.
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk["positions"][0]["expiration_time"] == "2026-09-02T00:00:00-04:00"
    assert on_disk["positions"][1]["expiration_time"] == "2026-09-02T00:00:00-07:00"
    logs.records.clear()
    again = SimulatedExchange(state_file=state_file)
    assert [p["expiration_time"] for p in again.positions] == [
        close for *_r, close in MAIA_POSITIONS
    ]
    assert all(p["expiration_time"].tzinfo is not None for p in again.positions)
    assert not [
        m for m in logs.messages(logging.INFO) if "BACKFILL expiration_time" in m
    ]


def test_backfilled_maia_position_settles_once_truth_is_published(
    tmp_path, truth, monkeypatch
):
    """What the exit criterion asks of maia: the restored row leaves the book."""
    state_file = tmp_path / "exchange_state.json"
    sym, st, lo, hi, close = MAIA_POSITIONS[0]
    state_file.write_text(
        json.dumps(_legacy_state([_legacy_row(1, sym, st, lo, hi)])), encoding="utf-8"
    )
    restored = SimulatedExchange(state_file=state_file)
    (pos,) = restored.positions
    settle_calls = _settle_spy(restored, monkeypatch)

    monkeypatch.setattr(matching_engine, "datetime", _Clock)
    _Clock.set(close + timedelta(hours=6))  # the morning after; CLI not out yet
    restored.update_market("TEMP_KNYC", 80.0)
    assert pos in restored.positions
    assert settle_calls == [sym]

    truth.publish("KNYC|2026-09-01", 83)  # 83 is NOT >= 84 -> NO
    restored.update_market("TEMP_KNYC", 80.0)
    assert pos not in restored.positions
    (trade,) = restored.closed_trades
    assert trade["reason"] == "EXPIRATION"
    assert trade["exit_price"] == pytest.approx(0.00)
    assert trade["settlement_outcome"] == "no"


def test_non_weather_symbol_gets_no_backfill(exchange, tmp_path):
    """Crypto rows and signals are untouched on every one of the three paths."""
    # Signal boundary (a non-bracket market, as the crypto_annual feed emits).
    market = MarketData(
        symbol=CRYPTO_TICKER,
        timestamp=datetime.now(),
        price=0.4,
        volume=1,
        bid=0.39,
        ask=0.41,
        extra={"source": "live", "strike": 64000.0},
    )
    sig = TradeSignal(symbol=CRYPTO_TICKER, side="buy", quantity=1, limit_price=0.4)
    (sig,) = attach_spec_to_signals([sig], market)
    assert sig.expiration_time is None

    # Exchange open.
    pos = _open_crypto(exchange)
    assert pos["expiration_time"] is None

    # Restart over a legacy file.
    exchange._save_state()
    restored = SimulatedExchange(state_file=exchange._state_file)
    (again,) = restored.positions
    assert again["symbol"] == CRYPTO_TICKER
    assert again["expiration_time"] is None
    assert settlement_close_for(CRYPTO_TICKER) is None


def test_open_position_backfills_a_weather_position_opened_without_one(exchange):
    """The exchange-level backstop for any caller that bypasses the stamp."""
    sym, _st, _lo, _hi, close = MAIA_POSITIONS[1]  # LAX: a different zone
    pos = _open_weather(exchange, ticker=sym, expiration_time=None)
    assert pos["expiration_time"] == close
    assert pos["expiration_time"].tzinfo is not None
    assert pos["expiration_time"].utcoffset() == timedelta(hours=-7)
