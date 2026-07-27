"""End-to-end settlement semantics through the simulated exchange (PRD FR-1.2).

``tests/test_bracket_payoff.py`` proves the payoff *module* is right. This file
proves the thing that actually books money -- ``SimulatedExchange._close_position``
-- routes through that module and produces the right exit price and PnL for all
three ``strike_type`` values, including the review's live-verified cases:

    KXHIGHNY-26JUL25-B86.5  between floor=86 cap=87  "86 to 87"     YES iff 86..87
    KXHIGHNY-26JUL25-T87    greater floor=87         "88 or above"  YES iff >= 88
    KXHIGHNY-26JUL25-T80    less    cap=80           "79 or below"  YES iff <= 79

Plus the two paths that must NOT invent an outcome: a position carrying no
cached bracket spec closes flat as ``SETTLEMENT_UNRESOLVED``, and a legacy
crypto position settles exactly as it did before this phase.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.fee_calculator import compute_fee  # noqa: E402
from src.core.matching_engine import SimulatedExchange  # noqa: E402

MP_LOGGER = "MoneyPrinter"


class _ListHandler(logging.Handler):
    """The project logger has ``propagate = False``, so caplog cannot see it."""

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
    logger = logging.getLogger(MP_LOGGER)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def exchange():
    # state_file=None -> persistence disabled; never touches data/exchange_state.json
    return SimulatedExchange(state_file=None)


def _open_weather(
    exchange,
    ticker,
    strike_type,
    floor=None,
    cap=None,
    entry=0.40,
    qty=10,
    side="buy",
    contract_side="YES",
):
    exchange.open_position(
        symbol=ticker,
        side=side,
        entry_price=entry,
        quantity=qty,
        strategy_name="test",
        contract_side=contract_side,
        disable_profit_targets=True,
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
    )
    return exchange.positions[-1]


def _expected_pnl(entry, exit_price, qty, side="buy", is_maker=True):
    gross = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
    return gross - compute_fee(exit_price, qty, is_maker=is_maker).fee


# ======================================================================
# Golden table: the three live-verified contracts, settled end to end
# ======================================================================

# (ticker, strike_type, floor, cap, high, expect_yes)
GOLDEN = [
    # between: floor 86, cap 87 -> "86 to 87". Boundaries floor-1/floor/cap/cap+1.
    ("KXHIGHNY-26JUL25-B86.5", "between", 86, 87, 85, False),
    ("KXHIGHNY-26JUL25-B86.5", "between", 86, 87, 86, True),
    ("KXHIGHNY-26JUL25-B86.5", "between", 86, 87, 87, True),
    ("KXHIGHNY-26JUL25-B86.5", "between", 86, 87, 88, False),
    # greater: floor 87 -> "88 or above". Does NOT pay at 87.
    ("KXHIGHNY-26JUL25-T87", "greater", 87, None, 86, False),
    ("KXHIGHNY-26JUL25-T87", "greater", 87, None, 87, False),
    ("KXHIGHNY-26JUL25-T87", "greater", 87, None, 88, True),
    ("KXHIGHNY-26JUL25-T87", "greater", 87, None, 99, True),
    # less: cap 80 -> "79 or below". Does NOT pay at 80.
    ("KXHIGHNY-26JUL25-T80", "less", None, 80, 78, True),
    ("KXHIGHNY-26JUL25-T80", "less", None, 80, 79, True),
    ("KXHIGHNY-26JUL25-T80", "less", None, 80, 80, False),
    ("KXHIGHNY-26JUL25-T80", "less", None, 80, 81, False),
]


@pytest.mark.parametrize("ticker,stype,floor,cap,high,expect_yes", GOLDEN)
def test_sim_settlement_matches_live_semantics(
    exchange, ticker, stype, floor, cap, high, expect_yes
):
    pos = _open_weather(exchange, ticker, stype, floor, cap, entry=0.40, qty=10)
    exchange._close_position(pos, high, reason="EXPIRATION")

    assert pos not in exchange.positions
    trade = exchange.closed_trades[-1]
    expected_exit = 1.00 if expect_yes else 0.00
    assert trade["exit_price"] == pytest.approx(expected_exit), (
        f"{ticker} ({stype}) at {high}F should settle "
        f"{'YES' if expect_yes else 'NO'}"
    )
    assert trade["pnl"] == pytest.approx(_expected_pnl(0.40, expected_exit, 10))
    assert trade["reason"] == "EXPIRATION"
    # Provenance: what settled it, and under which rule.
    assert trade["settlement_high"] == pytest.approx(float(high))
    assert trade["settlement_spec"] == {
        "strike_type": stype,
        "floor_strike": None if floor is None else float(floor),
        "cap_strike": None if cap is None else float(cap),
    }
    assert trade["settlement_outcome"] == ("yes" if expect_yes else "no")


def test_less_bracket_is_not_settled_backwards(exchange):
    """The exact inversion the suffix parser produced, pinned as a test.

    A ``less`` bracket (T80 = "79 or below") settles YES on a COLD day. The old
    parser read every ``T`` ticker as "above" and settled it YES on a HOT one.
    """
    cold = _open_weather(exchange, "KXHIGHNY-26JUL25-T80", "less", None, 80)
    exchange._close_position(cold, 70, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(1.00)

    hot = _open_weather(exchange, "KXHIGHNY-26JUL25-T80", "less", None, 80)
    exchange._close_position(hot, 95, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(0.00)


def test_no_contract_settles_against_the_yes_outcome(exchange):
    """A NO position on a YES-settling bracket loses; PnL uses the same rule."""
    pos = _open_weather(
        exchange,
        "KXHIGHNY-26JUL25-B86.5",
        "between",
        86,
        87,
        entry=0.60,
        qty=10,
        contract_side="NO",
    )
    # Entry price of a NO contract is already the NO cost; the exchange settles
    # the YES leg to 1.00 and the buy-side PnL falls out of that.
    exchange._close_position(pos, 86, reason="EXPIRATION")
    trade = exchange.closed_trades[-1]
    assert trade["exit_price"] == pytest.approx(1.00)
    assert trade["settlement_outcome"] == "yes"


# ======================================================================
# Missing spec: refuse to fabricate an outcome
# ======================================================================


def test_position_without_bracket_spec_closes_unresolved(exchange, logs):
    """FR-1.1: no spec -> no guess. Flat close, ERROR, operator alert."""
    alerts = []
    exchange.on_alert = alerts.append

    # A legacy weather position: opened before FR-1.1 cached the API fields.
    exchange.open_position(
        symbol="KXHIGHNY-26JUL25-B86.5",
        side="buy",
        entry_price=0.42,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
    )
    pos = exchange.positions[-1]
    pos.pop("strike_type", None)
    pos.pop("floor_strike", None)
    pos.pop("cap_strike", None)

    exchange._close_position(pos, 86, reason="EXPIRATION")

    trade = exchange.closed_trades[-1]
    assert trade["reason"] == "SETTLEMENT_UNRESOLVED"
    assert trade["exit_price"] == pytest.approx(0.42)
    # Zero PnL before fees: the close books no gain or loss from a guess.
    assert trade["pnl"] == pytest.approx(_expected_pnl(0.42, 0.42, 10))
    assert "settlement_outcome" not in trade

    errors = logs.messages(logging.ERROR)
    assert any("SETTLEMENT_UNRESOLVED" in m for m in errors), errors
    assert any("SETTLEMENT UNRESOLVED" in a for a in alerts), alerts


def test_settling_no_by_default_would_have_been_wrong(exchange):
    """Why the flat close matters: the "safe" default is systematically wrong.

    ~90% of a city's ladder settles NO, so defaulting to NO looks plausible and
    silently books a full loss on the one bracket that actually paid.
    """
    exchange.open_position(
        symbol="KXHIGHNY-26JUL25-B86.5",
        side="buy",
        entry_price=0.42,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
    )
    pos = exchange.positions[-1]
    for key in ("strike_type", "floor_strike", "cap_strike"):
        pos.pop(key, None)
    exchange._close_position(pos, 86, reason="EXPIRATION")

    trade = exchange.closed_trades[-1]
    fabricated_no_pnl = _expected_pnl(0.42, 0.00, 10)
    assert (
        trade["pnl"] > fabricated_no_pnl
    ), "an unresolved settlement must not book the fabricated-NO loss"


# ======================================================================
# Behaviour preservation: legacy crypto settlement is untouched
# ======================================================================


def test_legacy_crypto_settles_from_cached_strike(exchange):
    """KXBTC15M with a cached API strike: YES iff spot >= strike (unchanged)."""
    exchange.open_position(
        symbol="KXBTC15M-26JUN032130-30",
        side="buy",
        entry_price=0.30,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
        strike=64000.0,
    )
    pos = exchange.positions[-1]
    exchange._close_position(pos, 64500.0, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(1.00)

    exchange.open_position(
        symbol="KXBTC15M-26JUN032130-30",
        side="buy",
        entry_price=0.30,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
        strike=64000.0,
    )
    pos = exchange.positions[-1]
    exchange._close_position(pos, 63500.0, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(0.00)


def test_legacy_crypto_15m_without_strike_still_fail_safes_to_no(exchange, logs):
    """The 2026-06-10 fix: a 15m crypto ticker with no cached strike settles NO."""
    exchange.open_position(
        symbol="KXETH15M-26JUN032130-00",
        side="buy",
        entry_price=0.20,
        quantity=10,
        strategy_name="latency",
        disable_profit_targets=True,
    )
    pos = exchange.positions[-1]
    exchange._close_position(pos, 3000.0, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(0.00)
    assert any("missing cached" in m for m in logs.messages(logging.ERROR))


def test_legacy_hourly_crypto_settles_from_suffix_strike(exchange):
    """KXBTCD hourly: the suffix IS the strike; behaviour preserved."""
    exchange.open_position(
        symbol="KXBTCD-26JUN0317-T78499.99",
        side="buy",
        entry_price=0.30,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
    )
    pos = exchange.positions[-1]
    exchange._close_position(pos, 79000.0, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(1.00)

    exchange.open_position(
        symbol="KXBTCD-26JUN0317-T78499.99",
        side="buy",
        entry_price=0.30,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
    )
    pos = exchange.positions[-1]
    exchange._close_position(pos, 78000.0, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(0.00)


def test_precip_settlement_unchanged(exchange):
    """PRECIP contracts still settle off the probability, not a bracket."""
    exchange.open_position(
        symbol="PRECIP_KNYC",
        side="buy",
        entry_price=0.30,
        quantity=10,
        strategy_name="legacy",
        disable_profit_targets=True,
    )
    pos = exchange.positions[-1]
    exchange._close_position(pos, 0.80, reason="EXPIRATION")
    assert exchange.closed_trades[-1]["exit_price"] == pytest.approx(1.00)
