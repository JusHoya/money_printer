"""Tests for the FR-4.3 gas convergence strategy and the Phase 4 gas bot.

Two things are being defended here.

**Exit criterion 3** — "entries occur only within the configured final-N-day
window at >= 8pt model-market divergence". Both halves are asserted directly,
including the boundaries of the window and of the divergence threshold.

**Observability** — every rejection path emits exactly one INFO line carrying a
stable reason code *and* the measured value that failed. A gate whose reject
condition is universally true would otherwise kill all throughput silently,
which this project has already shipped once.

The market fixtures use quotes and timestamps taken verbatim from the live
``KXAAAGASM`` ladder probed 2026-07-29, so the settlement-date arithmetic is
tested against the real field values rather than an invented shape.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.core.fee_calculator import (
    FEE_TYPE_WITH_MAKER_FEES,
    compute_fee,
    fee_type_for_symbol,
)
from src.core.interfaces import MarketData
from src.data.aaa_provider import REASON_SCRAPE_FAILED_TODAY, SignalGate
from src.models.gas_projection import GasObservation, GasSeries, prob_above, project
from src.strategies import gas_convergence as gc
from src.strategies.gas_convergence import (
    ENTRY_MODE_MAKER,
    GasConvergenceStrategy,
    resolve_settlement_date,
)
from src.utils.logger import logger as mp_logger

UTC = ZoneInfo("UTC")

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

START = date(2025, 2, 1)
N_DAYS = 560
TRUE_LAG = 6
AS_OF = START + timedelta(days=N_DAYS - 1)  # newest AAA observation
TODAY = AS_OF  # data is same-day fresh unless a test says otherwise
SETTLEMENT = TODAY + timedelta(days=14)
SYMBOL = "KXAAAGASM-26AUG31-4.30"


def _synthetic(n_days=N_DAYS, lag=TRUE_LAG, seed=11):
    """Same generator as tests/test_gas_projection.py.

    Deliberately duplicated rather than imported: these two test modules stay
    independently runnable, and a shared fixture module is not in this
    workstream's file ownership.
    """
    rng = np.random.default_rng(seed)
    dates = [START + timedelta(days=i) for i in range(n_days)]
    d_rbob = rng.normal(0.0, 0.035, size=n_days)
    rbob = 2.20 + np.cumsum(d_rbob)
    aaa = np.empty(n_days)
    aaa[0] = 4.10
    for i in range(1, n_days):
        driver = d_rbob[i - lag] if i - lag >= 1 else 0.0
        aaa[i] = aaa[i - 1] + 0.35 * driver + rng.normal(0.0, 0.004)
    return dates, aaa, rbob


@pytest.fixture(scope="module")
def series() -> GasSeries:
    dates, aaa, rbob = _synthetic()
    return GasSeries.from_rows(
        aaa=[GasObservation(date=d, value=float(v)) for d, v in zip(dates, aaa)],
        rbob=[GasObservation(date=d, value=float(v)) for d, v in zip(dates, rbob)],
    )


@pytest.fixture(scope="module")
def model_p_yes(series) -> float:
    """The model's P(YES) for the 4.30 strike at the fixture's 14-day lead."""
    return prob_above(project(AS_OF, SETTLEMENT, series), 4.30)


def _market(
    symbol=SYMBOL,
    yes_bid=0.40,
    yes_ask=0.44,
    no_bid=None,
    no_ask=None,
    last=0.42,
    floor_strike=4.30,
    strike_type="greater",
    close_time=None,
    volume=1455,
    **extra_overrides,
) -> MarketData:
    """One bracket, shaped exactly as ``KalshiProvider._parse_market_data`` emits."""
    if no_bid is None and yes_ask is not None:
        no_bid = round(1.0 - yes_ask, 4)
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - yes_bid, 4)
    if close_time is None:
        # 23:59 ET the evening before settlement, i.e. what Kalshi publishes.
        close_time = (
            (
                datetime.combine(
                    SETTLEMENT - timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=gc.MARKET_TZ,
                )
                + timedelta(hours=23, minutes=59)
            )
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    extra = {
        "status": "active",
        "close_time": close_time,
        "source": "ladder",
        "no_bid": no_bid,
        "no_ask": no_ask,
        "strike": floor_strike,
        "strike_type": strike_type,
        "floor_strike": floor_strike,
        "cap_strike": None,
        "yes_sub_title": "above $4.30",
        "sub_title": None,
    }
    extra.update(extra_overrides)
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        price=last if last is not None else 0.0,
        volume=volume,
        bid=yes_bid if yes_bid is not None else 0.0,
        ask=yes_ask if yes_ask is not None else 0.0,
        extra=extra,
    )


def _strategy(series, **kwargs) -> GasConvergenceStrategy:
    kwargs.setdefault("clock", lambda: TODAY)
    kwargs.setdefault("series", series)
    # FR-4.1's AAA scrape gate is exercised deliberately in TestScrapeGate below
    # and end-to-end in tests/test_aaa_provider.py. Everywhere else it is held
    # open EXPLICITLY, for two reasons: a test that means to measure the
    # divergence gate must not silently be measuring the data gate instead, and
    # no test in this module may read the real ``data/gas_truth/`` directory,
    # whose contents change daily.
    kwargs.setdefault("gate", lambda: SignalGate(True))
    kwargs.setdefault("gate_cache_seconds", 0.0)
    return GasConvergenceStrategy(**kwargs)


@pytest.fixture
def records():
    """Collect records from the shared MoneyPrinter logger (propagate=False)."""
    collected = []

    class _Collector(logging.Handler):
        def emit(self, record):
            collected.append(record)

    handler = _Collector(level=logging.DEBUG)
    mp_logger.addHandler(handler)
    yield collected
    mp_logger.removeHandler(handler)


def _rejections(records):
    return [r for r in records if "[Risk] REJECT" in r.getMessage()]


def _reason_of(records) -> str:
    lines = _rejections(records)
    assert len(lines) == 1, f"expected exactly one rejection, got {len(lines)}: {lines}"
    message = lines[0].getMessage()
    assert lines[0].levelno == logging.INFO, "rejections must be visible at INFO"
    return message


def _reason_code(records) -> str:
    """The ``reason=`` token exactly.

    Substring-matching ``"reason=X" in message`` is not enough once a rejection
    also carries a ``gate_reason=`` field: ``reason=GAS_SCRAPE_GATE_ERROR``
    matches inside ``gate_reason=GAS_SCRAPE_GATE_ERROR``, so a test asserting the
    wrong top-level code passes anyway.
    """
    return _reason_of(records).split(" reason=", 1)[1].split()[0]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_accepts_a_divergent_bracket_inside_the_window(series, model_p_yes, records):
    signals = _strategy(series).analyze(_market())
    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == SYMBOL
    assert sig.side == "buy"
    assert sig.contract_side == "YES"  # model 0.52 > market mid 0.42
    assert sig.limit_price == pytest.approx(0.44)  # executable ask, not the bid
    assert sig.confidence == pytest.approx(model_p_yes, abs=1e-9)
    assert sig.quantity == 5
    accepts = [r for r in records if "[Gas] ACCEPT" in r.getMessage()]
    assert len(accepts) == 1
    text = accepts[0].getMessage()
    for token in ("ev_taker=", "ev_maker=", "divergence=", "fee_type=", "lead=14d"):
        assert token in text, text


def test_signal_carries_bracket_semantics_and_projection_provenance(series):
    sig = _strategy(series).analyze(_market())[0]
    assert sig.strike_type == "greater"
    assert sig.floor_strike == pytest.approx(4.30)
    assert sig.cap_strike is None
    assert sig.gas_model_version == f"lagdrift_v1+rbobL{TRUE_LAG}"
    assert len(sig.gas_inputs_hash) == 64
    assert sig.gas_projection_sigma > 0
    # Expiration is 10:00 ET on the settlement date (Kalshi's own
    # expected_expiration_time for every live gas event).
    assert sig.expiration_time.astimezone(gc.MARKET_TZ).date() == SETTLEMENT
    assert sig.expiration_time.astimezone(gc.MARKET_TZ).hour == 10


def test_buys_no_when_the_market_prices_above_the_model(series, model_p_yes):
    sig = _strategy(series).analyze(_market(yes_bid=0.59, yes_ask=0.62, last=0.60))[0]
    assert sig.contract_side == "NO"
    assert sig.limit_price == pytest.approx(1.0 - 0.59)  # no_ask
    assert sig.confidence == pytest.approx(1.0 - model_p_yes, abs=1e-9)


def test_entry_mode_maker_emits_the_resting_price(series):
    sig = _strategy(series, entry_mode=ENTRY_MODE_MAKER).analyze(_market())[0]
    assert sig.limit_price == pytest.approx(0.40)  # joins the bid


# --------------------------------------------------------------------------
# Exit criterion 3a: the final-N-day window
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lead,accepted",
    [(-1, False), (0, False), (1, True), (14, True), (15, False), (30, False)],
)
def test_entries_only_inside_the_final_window(series, records, lead, accepted):
    settlement = TODAY + timedelta(days=lead)
    close = (
        (
            datetime.combine(
                settlement - timedelta(days=1), datetime.min.time(), tzinfo=gc.MARKET_TZ
            )
            + timedelta(hours=23, minutes=59)
        )
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    signals = _strategy(series).analyze(_market(close_time=close))
    assert bool(signals) is accepted
    if not accepted:
        message = _reason_of(records)
        assert "reason=GAS_OUTSIDE_FINAL_WINDOW" in message
        assert f"lead_days={lead}" in message
        assert "window_days=14" in message


def test_window_length_is_configurable(series):
    settlement = TODAY + timedelta(days=20)
    close = (
        (
            datetime.combine(
                settlement - timedelta(days=1), datetime.min.time(), tzinfo=gc.MARKET_TZ
            )
            + timedelta(hours=23, minutes=59)
        )
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    assert not _strategy(series).analyze(_market(close_time=close))
    assert _strategy(series, final_window_days=21).analyze(_market(close_time=close))


# --------------------------------------------------------------------------
# Exit criterion 3b: the 8pt divergence threshold
# --------------------------------------------------------------------------


def test_divergence_threshold_is_exactly_eight_points(series, model_p_yes, records):
    """Just inside the threshold rejects; just outside it accepts."""
    strategy = _strategy(series)

    # Mid placed so |model - mid| is a hair under 8pt.
    near_mid = model_p_yes - 0.0799
    market = _market(yes_bid=near_mid - 0.01, yes_ask=near_mid + 0.01, last=near_mid)
    assert not strategy.analyze(market)
    message = _reason_of(records)
    assert "reason=GAS_DIVERGENCE_BELOW_MIN" in message
    assert "divergence=0.0799" in message
    assert "min_divergence=0.08" in message

    far_mid = model_p_yes - 0.0801
    assert strategy.analyze(
        _market(yes_bid=far_mid - 0.01, yes_ask=far_mid + 0.01, last=far_mid)
    )


def test_min_divergence_is_configurable(series, model_p_yes):
    mid = model_p_yes - 0.05
    market = _market(yes_bid=mid - 0.01, yes_ask=mid + 0.01, last=mid)
    assert not _strategy(series).analyze(market)
    assert _strategy(series, min_divergence=0.04).analyze(market)


def test_quantity_is_never_scaled_by_the_divergence(series):
    """FR-4.3 "sized small": a fixed base quantity, whatever the edge."""
    strategy = _strategy(series)
    small_edge = strategy.analyze(_market(yes_bid=0.40, yes_ask=0.44, last=0.42))[0]
    big_edge = strategy.analyze(
        _market(symbol="KXAAAGASM-26AUG31-4.31", yes_bid=0.20, yes_ask=0.24, last=0.22)
    )[0]
    assert big_edge.confidence >= small_edge.confidence
    assert small_edge.quantity == big_edge.quantity == strategy.base_quantity


def test_one_fit_serves_the_whole_ladder(series, monkeypatch):
    """Every bracket in an event shares one regression, not one each."""
    calls = []
    real = gc.project

    def counting(*args, **kwargs):
        calls.append(args[:2])
        return real(*args, **kwargs)

    monkeypatch.setattr(gc, "project", counting)
    strategy = _strategy(series)
    for i in range(6):
        strategy.analyze(_market(symbol=f"KXAAAGASM-26AUG31-4.3{i}"))
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Fees: the gas maker schedule must be load-bearing
# --------------------------------------------------------------------------


def test_the_gas_series_is_billed_for_resting_liquidity():
    """Pinned against the live metadata probed 2026-07-29."""
    gas_fee_type = fee_type_for_symbol(SYMBOL)
    assert gas_fee_type == FEE_TYPE_WITH_MAKER_FEES
    assert compute_fee(0.40, 5, is_maker=True, series_fee_type=gas_fee_type).fee > 0


def test_ev_threads_the_symbol_so_the_maker_fee_is_charged(series):
    """A symbol-less (or weather) call would price the maker leg as free."""
    strategy = _strategy(series)
    gas = strategy._ev(SYMBOL, 0.52, 0.40, 5, is_maker=True)
    weather = strategy._ev("KXHIGHNY-26JUL29-B85.5", 0.52, 0.40, 5, is_maker=True)
    free = 0.52 - 0.40
    assert weather == pytest.approx(free)  # standard schedule: no maker fee
    assert gas < weather  # gas pays for resting liquidity
    assert gas == pytest.approx(free - 0.03 / 5)


def test_ev_uses_the_order_total_not_a_per_contract_fee_at_c_equals_one(series):
    """The fee is rounded up on the order total, so it must be sized correctly."""
    strategy = _strategy(series)
    ev = strategy._ev(SYMBOL, 0.52, 0.44, 20, is_maker=False)
    gas_fee_type = fee_type_for_symbol(SYMBOL)
    total = compute_fee(0.44, 20, is_maker=False, series_fee_type=gas_fee_type).fee
    per_c1 = compute_fee(0.44, 1, is_maker=False, series_fee_type=gas_fee_type).fee
    assert ev == pytest.approx(0.52 - 0.44 - total / 20)
    assert total / 20 < per_c1, "per-contract-at-C=1 overstates the cost"


def test_fees_can_turn_a_divergent_bracket_into_a_rejection(
    series, model_p_yes, records
):
    """A wide book: 11pt divergence vs the mid, negative EV at the ask.

    This is also why the EV gate takes the minimum of both legs. Resting at the
    0.30 bid would show a large positive maker EV, but that fill is a fantasy;
    the executable price is the 0.52 ask, and after the taker fee the trade
    loses money.
    """
    strategy = _strategy(series)
    market = _market(yes_bid=0.30, yes_ask=0.52, last=0.41)
    assert prob_above(project(AS_OF, SETTLEMENT, series), 4.30) == pytest.approx(
        model_p_yes
    )
    assert abs(model_p_yes - 0.41) >= 0.08  # divergence gate would pass
    assert not strategy.analyze(market)
    message = _reason_of(records)
    assert "reason=GAS_EV_NOT_POSITIVE" in message
    assert "ev_taker=-" in message
    assert "ev_maker=0." in message  # the fantasy leg, logged for contrast
    assert "fee_type=quadratic_with_maker_fees" in message


# --------------------------------------------------------------------------
# Data freshness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("age,accepted", [(0, True), (2, True), (3, False), (9, False)])
def test_stale_aaa_data_blocks_signals(series, records, age, accepted):
    strategy = _strategy(series, clock=lambda: AS_OF + timedelta(days=age))
    settlement = AS_OF + timedelta(days=age + 14)
    close = (
        (
            datetime.combine(
                settlement - timedelta(days=1), datetime.min.time(), tzinfo=gc.MARKET_TZ
            )
            + timedelta(hours=23, minutes=59)
        )
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    signals = strategy.analyze(_market(close_time=close))
    assert bool(signals) is accepted
    if not accepted:
        message = _reason_of(records)
        assert "reason=GAS_DATA_STALE" in message
        assert f"staleness_days={age}" in message
        assert "max_age_days=2" in message


def test_projection_failure_is_reported_not_defaulted(records):
    """An abort from the model becomes a logged rejection, never a fallback."""
    thin = GasSeries.from_rows(
        aaa=[
            GasObservation(date=AS_OF - timedelta(days=i), value=4.10)
            for i in range(30)
        ]
    )
    assert not _strategy(thin).analyze(_market())
    message = _reason_of(records)
    assert "reason=GAS_PROJECTION_UNAVAILABLE" in message
    assert "below the FR-4.2 minimum" in message


def test_no_series_rejects_every_market(records):
    assert not _strategy(None).analyze(_market())
    assert "reason=GAS_SERIES_UNAVAILABLE" in _reason_of(records)


# --------------------------------------------------------------------------
# FR-4.1 / EC-1: the AAA scrape gate, consumed by the signal path
# --------------------------------------------------------------------------


def _blocked_gate(
    reason: str = REASON_SCRAPE_FAILED_TODAY,
    detail: str = "1 scrape failure(s) recorded for 2026-08-14 and no admissible "
    "aaa_live row persisted for that day",
):
    return lambda: SignalGate(False, reason, detail)


class TestScrapeGate:
    """Phase 4 EC-1's second half — "zero signals that day" — enforced where a
    gas signal is actually created.

    Before this wiring existed the gate was implemented in the provider and
    consumed by nobody: ``git grep signal_gate -- src/ scripts/`` matched only
    ``aaa_provider.py`` itself, so a failed scrape alerted and then the strategy
    traded the day anyway on the freshness check alone.
    """

    def test_a_blocked_gate_produces_zero_signals(self, series, records):
        strategy = _strategy(series, gate=_blocked_gate())
        assert strategy.analyze(_market()) == []
        assert _reason_code(records) == "GAS_SCRAPE_GATE_BLOCKED"
        message = _reason_of(records)
        assert f"gate_reason={REASON_SCRAPE_FAILED_TODAY}" in message
        # the measured value that failed rides along, per FR-0.4
        assert "no admissible aaa_live row" in message

    def test_the_same_market_is_accepted_when_the_gate_allows(self, series):
        """Attribution: the block above is the gate's doing and nothing else."""
        assert len(_strategy(series).analyze(_market())) == 1

    def test_the_gate_is_consulted_before_any_projection_is_fitted(
        self, series, monkeypatch
    ):
        """A blocked day does not get a model fitted first."""
        fits = []

        def _spy(*args, **kwargs):
            fits.append(args)
            raise AssertionError("project() must not run on a blocked day")

        monkeypatch.setattr(gc, "project", _spy)
        assert _strategy(series, gate=_blocked_gate()).analyze(_market()) == []
        assert fits == []

    def test_a_raising_gate_blocks_rather_than_falling_through(self, series, records):
        """Fail closed: an unreadable failure record is not a licence to trade."""

        def boom():
            raise OSError("scrape_failures.json unreadable")

        assert _strategy(series, gate=boom).analyze(_market()) == []
        assert _reason_code(records) == "GAS_SCRAPE_GATE_ERROR"
        assert "OSError" in _reason_of(records)

    def test_the_default_gate_is_the_real_provider_not_a_permissive_stub(
        self, series, monkeypatch
    ):
        """The SHIPPED configuration must be the gated one.

        ``GasBot`` constructs ``GasConvergenceStrategy()`` with no arguments, so
        ``gate=None`` has to mean "use the real AAA provider gate". If it ever
        means "no gate", EC-1's second half is unenforced in production while
        every test in this module still passes.
        """
        calls = []

        def _fake_default(*, max_age_days):
            calls.append(max_age_days)
            return SignalGate(False, REASON_SCRAPE_FAILED_TODAY, "stub")

        monkeypatch.setattr(gc, "default_signal_gate", _fake_default)
        strategy = GasConvergenceStrategy(
            series=series, clock=lambda: TODAY, gate_cache_seconds=0.0
        )
        assert strategy.analyze(_market()) == []
        # and the freshness limit is threaded, not duplicated
        assert calls == [strategy.max_data_age_days]

    def test_the_shipped_default_gate_reads_the_real_provider(self, monkeypatch):
        """``default_signal_gate`` goes through ``AAAProvider.signal_gate``."""
        seen = {}

        class _Spy:
            def signal_gate(self, **kwargs):
                seen.update(kwargs)
                return SignalGate(True)

        monkeypatch.setattr(gc, "_DEFAULT_GATE_PROVIDER", _Spy())
        assert gc.default_signal_gate(max_age_days=7).allow is True
        assert seen == {"max_age_days": 7}

    def test_one_verdict_serves_a_whole_ladder_pass(self, series):
        """A 40-bracket ladder must not re-read the series 40 times."""
        calls = []

        def gate():
            calls.append(1)
            return SignalGate(True)

        strategy = _strategy(series, gate=gate, gate_cache_seconds=60.0)
        for _ in range(4):
            strategy.analyze(_market())
        assert len(calls) == 1

    def test_a_series_reload_re_asks_the_gate(self, series):
        """The recorder is a different process, so the verdict cannot be cached
        for the day: a reload must re-read it."""
        calls = []

        def gate():
            calls.append(1)
            return SignalGate(True)

        strategy = _strategy(series, gate=gate, gate_cache_seconds=60.0)
        strategy.analyze(_market())
        strategy.set_series(series)
        strategy.analyze(_market())
        assert len(calls) == 2


# --------------------------------------------------------------------------
# Bracket semantics come from API fields
# --------------------------------------------------------------------------


def test_missing_strike_type_is_rejected_not_inferred(series, records):
    assert not _strategy(series).analyze(_market(strike_type=None))
    message = _reason_of(records)
    assert "reason=GAS_BRACKET_SPEC_UNAVAILABLE" in message
    assert "strike_type absent" in message


def test_missing_floor_strike_is_rejected(series, records):
    assert not _strategy(series).analyze(_market(floor_strike=None))
    assert "floor_strike absent" in _reason_of(records)


def test_unsupported_strike_type_is_rejected(series, records):
    assert not _strategy(series).analyze(
        _market(strike_type="between", cap_strike=4.40)
    )
    message = _reason_of(records)
    assert "reason=GAS_UNSUPPORTED_STRIKE_TYPE" in message
    assert "strike_type=between" in message


def test_non_gas_series_is_rejected(series, records):
    assert not _strategy(series).analyze(_market(symbol="KXHIGHNY-26JUL29-B85.5"))
    message = _reason_of(records)
    assert "reason=GAS_UNSUPPORTED_SERIES" in message
    assert "series=KXHIGHNY" in message


def test_absent_extra_fields_are_rejected(series, records):
    market = _market()
    market.extra = None
    assert not _strategy(series).analyze(market)
    assert "reason=GAS_BRACKET_SPEC_UNAVAILABLE" in _reason_of(records)


# --------------------------------------------------------------------------
# Settlement date: API fields only, ET arithmetic
# --------------------------------------------------------------------------


def test_settlement_date_from_close_time_is_the_next_et_day():
    """Verbatim live values: KXAAAGASM-26AUG31 closes 23:59 ET on Aug 30."""
    settlement, source = resolve_settlement_date(
        "KXAAAGASM-26AUG31-4.60", {"close_time": "2026-08-31T03:59:00Z"}
    )
    assert settlement == date(2026, 8, 31)
    assert source == "close_time"


def test_settlement_date_prefers_expected_expiration_time():
    settlement, source = resolve_settlement_date(
        "KXAAAGASM-26AUG31-4.60",
        {
            "close_time": "2026-08-31T03:59:00Z",
            "expected_expiration_time": "2026-08-31T14:00:00Z",
        },
    )
    assert settlement == date(2026, 8, 31)
    assert source == "expected_expiration_time"


def test_settlement_date_accepts_an_explicit_override():
    settlement, source = resolve_settlement_date(
        "KXAAAGASM-26AUG31-4.60", {"settlement_date": "2026-08-31"}
    )
    assert settlement == date(2026, 8, 31)
    assert source == "settlement_date"


@pytest.mark.parametrize(
    "close_time,expected",
    [
        ("2026-07-31T03:59:00Z", date(2026, 7, 31)),  # live KXAAAGASM-26JUL31
        ("2026-08-03T03:59:00Z", date(2026, 8, 3)),  # live KXAAAGASW-26AUG03
    ],
)
def test_settlement_date_matches_every_live_event(close_time, expected):
    resolved, _ = resolve_settlement_date("KXAAAGASM-X", {"close_time": close_time})
    assert resolved == expected


def test_a_utc_naive_reading_would_be_off_by_a_day():
    """The reason the conversion is ET: 03:59Z is still the previous ET day."""
    close = datetime.fromisoformat("2026-08-31T03:59:00+00:00")
    assert close.date() == date(2026, 8, 31)  # naive UTC reading
    assert close.astimezone(gc.MARKET_TZ).date() == date(2026, 8, 30)  # ET truth
    assert resolve_settlement_date("KXAAAGASM-X", {"close_time": close})[0] == date(
        2026, 8, 31
    )


def test_missing_settlement_date_is_rejected(series, records):
    market = _market()
    market.extra["close_time"] = None
    assert not _strategy(series).analyze(market)
    message = _reason_of(records)
    assert "reason=GAS_SETTLEMENT_DATE_UNAVAILABLE" in message
    assert "refusing to infer the settlement date from the ticker" in message


def test_default_clock_is_the_et_calendar_date():
    assert gc.MARKET_TZ.key == "America/New_York"
    assert GasConvergenceStrategy().clock is gc._today_et
    assert gc._today_et() == datetime.now(gc.MARKET_TZ).date()


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


def test_near_resolved_markets_are_skipped(series, records):
    assert not _strategy(series).analyze(_market(yes_bid=0.98, yes_ask=0.99, last=0.98))
    message = _reason_of(records)
    assert "reason=GAS_NEAR_RESOLVED" in message
    assert "yes_bid=0.98" in message


def test_unquoted_market_is_skipped(series, records):
    assert not _strategy(series).analyze(
        _market(yes_bid=0.0, yes_ask=0.0, no_bid=0.0, no_ask=0.0, last=0.0)
    )
    message = _reason_of(records)
    assert "reason=GAS_NO_USABLE_QUOTE" in message


def test_one_sided_book_names_its_price_source(series, records):
    """A divergence measured off a one-sided book is labelled as such."""
    strategy = _strategy(series)
    signals = strategy.analyze(_market(yes_bid=0.40, yes_ask=None, last=None))
    # No YES ask means no executable YES entry: rejected, not guessed at.
    assert not signals
    message = _reason_of(records)
    assert "reason=GAS_NO_USABLE_QUOTE" in message
    assert "both fee legs" in message


def test_constructor_validates_its_arguments():
    with pytest.raises(ValueError, match="entry_mode"):
        GasConvergenceStrategy(entry_mode="cross")
    with pytest.raises(ValueError, match="base_quantity"):
        GasConvergenceStrategy(base_quantity=0)


def test_strategy_name_is_stable(series):
    assert _strategy(series).name() == "Gas Convergence"


# --------------------------------------------------------------------------
# Rejection observability, as a set
# --------------------------------------------------------------------------


def test_every_rejection_path_logs_a_reason_code_and_a_measured_value(series, records):
    """One INFO line per rejection, each with at least one measured k=v pair."""
    cases = [
        _market(symbol="KXHIGHNY-26JUL29-B85.5"),
        _market(strike_type=None),
        _market(strike_type="less", floor_strike=None, cap_strike=4.4),
        _market(yes_bid=0.98, yes_ask=0.99, last=0.98),
        _market(yes_bid=0.0, yes_ask=0.0, no_bid=0.0, no_ask=0.0, last=0.0),
        _market(yes_bid=0.49, yes_ask=0.51, last=0.50),
        _market(yes_bid=0.30, yes_ask=0.52, last=0.41),
    ]
    strategy = _strategy(series)
    seen = set()
    for market in cases:
        records.clear()
        assert not strategy.analyze(market)
        message = _reason_of(records)
        reason = message.split("reason=", 1)[1].split()[0]
        seen.add(reason)
        tail = message.split(f"reason={reason}", 1)[1].strip()
        assert "=" in tail, f"{reason} logged no measured value: {message}"

    # The two scrape-gate codes cannot be induced by market shape — they come
    # from the AAA provider's verdict — so they are driven through their own
    # strategies rather than quietly left out of this enumeration.
    def _raise():
        raise OSError("scrape_failures.json unreadable")

    for gate in (_blocked_gate(), _raise):
        records.clear()
        assert not _strategy(series, gate=gate).analyze(_market())
        message = _reason_of(records)
        reason = message.split("reason=", 1)[1].split()[0]
        seen.add(reason)
        tail = message.split(f"reason={reason}", 1)[1].strip()
        assert "=" in tail, f"{reason} logged no measured value: {message}"

    assert seen == {
        "GAS_UNSUPPORTED_SERIES",
        "GAS_BRACKET_SPEC_UNAVAILABLE",
        "GAS_UNSUPPORTED_STRIKE_TYPE",
        "GAS_NEAR_RESOLVED",
        "GAS_NO_USABLE_QUOTE",
        "GAS_DIVERGENCE_BELOW_MIN",
        "GAS_EV_NOT_POSITIVE",
        "GAS_SCRAPE_GATE_BLOCKED",
        "GAS_SCRAPE_GATE_ERROR",
    }


# --------------------------------------------------------------------------
# The bot
# --------------------------------------------------------------------------


class _FakeKalshi:
    def __init__(self, ladder):
        self._ladder = ladder
        self.ladder_calls = []
        self.orderbook_calls = []

    def fetch_market_ladder(self, series_ticker, **kwargs):
        self.ladder_calls.append(series_ticker)
        return list(self._ladder) if series_ticker == "KXAAAGASM" else []

    def fetch_orderbook(self, symbol, depth=3):
        self.orderbook_calls.append(symbol)
        return {"yes": [[0.40, 10]], "no": [[0.58, 10]]}


class _FakeDashboard:
    def __init__(self):
        self.prices = {}
        self.depths = []

    def update_price(self, name, price, **kwargs):
        self.prices[name] = (price, kwargs)

    def record_depth(self, symbol, book, **kwargs):
        self.depths.append((symbol, kwargs))


class _FakeExchange:
    def __init__(self):
        self.marks = {}

    def update_market_price(self, symbol, price):
        self.marks[symbol] = price


class _FakeRiskManager:
    def __init__(self):
        self.exchange = _FakeExchange()
        self.data = {}
        self.orders = []

    def update_market_data(self, symbol, price):
        self.data[symbol] = price

    def check_order(self, *args, **kwargs):  # pragma: no cover - must not be hit
        self.orders.append((args, kwargs))
        return False, "TEST"


@pytest.fixture
def bot(monkeypatch):
    from src.bots import gas_bot as gb

    monkeypatch.setattr(gb.time, "sleep", lambda *_: None)
    instance = gb.GasBot()
    instance.SERIES = ("KXAAAGASM",)
    return instance


def test_gas_trading_is_disabled_by_default():
    """PRD Phase 4 EC-2 gates paper trading on a backtest verdict that does not
    exist yet, so the flag cannot be True."""
    from src.bots import gas_bot

    assert gas_bot.GAS_TRADING_ENABLED is False


def test_gas_bot_is_registered_and_still_feed_only():
    """Phase 4 wiring: the bot IS registered, and registration is not a verdict.

    Was ``test_gas_bot_is_not_registered_yet``, which pinned the pre-wiring
    state while ``registry.py``/``src/bots/__init__.py`` were still unwired.
    Phase 4 registered the bot feed-only (see ``src/bots/__init__.py`` and
    ``tests/test_lean_config_2026_06_03.py``), so the assertion is inverted --
    and paired with the kill switch, because the whole point of registering a
    feed-only bot is that harvesting starts while trading does not.
    """
    import src.bots  # noqa: F401 - triggers the sanctioned registrations
    from src.bots import gas_bot
    from src.bots.registry import BotRegistry

    assert "gas" in BotRegistry.list_bots()
    assert isinstance(BotRegistry.create("gas"), gas_bot.GasBot)
    assert gas_bot.GAS_TRADING_ENABLED is False


def test_feed_only_tick_harvests_the_ladder_and_emits_no_signals(bot, records):
    ladder = [
        _market(symbol="KXAAAGASM-26AUG31-4.30"),
        _market(symbol="KXAAAGASM-26AUG31-4.40", yes_bid=0.18, yes_ask=0.22, last=0.20),
    ]
    kalshi = _FakeKalshi(ladder)
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    assert bot.tick(risk, dashboard) == []

    assert kalshi.ladder_calls == ["KXAAAGASM"]
    assert "KXAAAGASM-26AUG31-4.30 (Market)" in dashboard.prices
    recorded = dashboard.prices["KXAAAGASM-26AUG31-4.30 (Market)"][1]
    assert recorded["strike_type"] == "greater"
    assert recorded["floor_strike"] == pytest.approx(4.30)
    assert risk.data["KXAAAGASM-26AUG31-4.40"] == pytest.approx(0.18)
    assert risk.exchange.marks["KXAAAGASM-26AUG31-4.40"] == pytest.approx(0.18)
    assert risk.orders == [], "feed-only must never reach the risk manager"

    feed_lines = [r for r in records if "FEED-ONLY" in r.getMessage()]
    assert feed_lines, "silence must be explained (PRD §6)"
    assert "0 signals emitted" in feed_lines[-1].getMessage()


def test_bot_reports_a_missing_truth_series_without_crashing(bot, tmp_path, records):
    bot.truth_dir = tmp_path  # empty: WS-A has not landed the CSVs
    bot.setup(_FakeKalshi([]))
    assert bot.strategies["gas_convergence"].series is None
    warnings = [
        r
        for r in records
        if r.levelno >= logging.WARNING and "truth series unavailable" in r.getMessage()
    ]
    assert warnings


def test_hourly_depth_snapshot_is_not_per_tick(bot):
    ladder = [_market(symbol="KXAAAGASM-26AUG31-4.30")]
    kalshi = _FakeKalshi(ladder)
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()
    bot.setup(kalshi)

    bot.tick(risk, dashboard)
    assert len(kalshi.orderbook_calls) == 1  # first tick records a baseline
    bot.tick(risk, dashboard)
    assert len(kalshi.orderbook_calls) == 1  # second tick must not re-snapshot
    assert dashboard.depths[0][1]["strike_type"] == "greater"


def test_bot_survives_a_provider_exception(bot, records):
    class _Broken:
        def fetch_market_ladder(self, *_args, **_kwargs):
            raise RuntimeError("kalshi 502")

    bot.setup(_Broken())
    assert bot.tick(_FakeRiskManager(), _FakeDashboard()) == []
    assert any("Market Fetch Fail" in r.getMessage() for r in records)
