"""Tests for the mention base-rate strategy scaffold.

Two things are defended:

* **The kill switch is load-bearing inside the strategy itself.** With
  ``MENTION_TRADING_ENABLED=False`` (the shipped state) ``analyze`` emits
  nothing, whatever the edge — the scaffold cannot trade by being wired
  somewhere unexpected.
* **Observability** — every rejection path emits exactly one INFO line with a
  stable reason code and the measured value that failed (the gas-strategy
  contract).

The signal-shape tests monkeypatch the flag True; they are tests of the
scaffold's arithmetic, not a change of posture.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bots import mention_bot
from src.core.interfaces import MarketData
from src.strategies.mention_strategy import (
    MentionStrategy,
    split_mention_symbol,
)
from src.utils.logger import logger as mp_logger

SYMBOL = "KXTRUMPMENTION-26AUG27-FARM"


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
    return _reason_of(records).split(" reason=", 1)[1].split()[0]


def _market(symbol=SYMBOL, yes_bid=0.40, yes_ask=0.44, no_bid=None, no_ask=None):
    if no_bid is None and yes_ask is not None:
        no_bid = round(1.0 - yes_ask, 4)
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - yes_bid, 4)
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        price=0.42,
        volume=100,
        bid=yes_bid if yes_bid is not None else 0.0,
        ask=yes_ask if yes_ask is not None else 0.0,
        extra={"no_bid": no_bid, "no_ask": no_ask, "status": "active"},
    )


@pytest.fixture
def rates_path(tmp_path):
    path = tmp_path / "mention_base_rates.json"
    path.write_text(
        json.dumps(
            {
                "KXTRUMPMENTION": {"FARM": 0.65, "TARIFF": 0.42},
                "KXPRESMENTION": {"ECONOMY": 0.90},
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _strategy(rates_path, **kwargs):
    return MentionStrategy(base_rates_path=rates_path, **kwargs)


@pytest.fixture
def enabled(monkeypatch):
    """Flip the kill switch for scaffold-arithmetic tests only."""
    monkeypatch.setattr(mention_bot, "MENTION_TRADING_ENABLED", True)


# --------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------


def test_flag_is_false_and_analyze_emits_nothing(rates_path, records):
    """The shipped posture: a 25-point edge on a two-sided book emits nothing."""
    assert mention_bot.MENTION_TRADING_ENABLED is False
    strategy = _strategy(rates_path)
    assert strategy.analyze(_market()) == []
    assert _reason_code(records) == "MENTION_TRADING_DISABLED"


def test_flag_is_read_live_not_frozen_at_import(rates_path, monkeypatch):
    """analyze reads the bot module's flag at call time, so a monkeypatched
    True is honoured — the same mechanism a real flip would use."""
    strategy = _strategy(rates_path)
    assert strategy.analyze(_market()) == []
    monkeypatch.setattr(mention_bot, "MENTION_TRADING_ENABLED", True)
    assert len(strategy.analyze(_market())) == 1


# --------------------------------------------------------------------------
# Signal shape (flag monkeypatched True)
# --------------------------------------------------------------------------


def test_positive_edge_buys_yes_at_the_resting_bid(rates_path, enabled, records):
    # base_rate 0.65 vs mid 0.42 -> edge +0.23
    signals = _strategy(rates_path).analyze(_market())
    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == SYMBOL
    assert sig.side == "buy"
    assert sig.contract_side == "YES"
    assert sig.limit_price == pytest.approx(0.40)  # maker: rest at yes_bid
    assert sig.confidence == pytest.approx(0.65)
    assert sig.quantity == 5
    accepts = [r for r in records if "[Mention] ACCEPT" in r.getMessage()]
    assert len(accepts) == 1


def test_negative_edge_buys_no_at_the_no_bid(rates_path, enabled):
    # base_rate for ECONOMY is 0.90; make the market richer than that:
    # mid 0.975 -> edge -0.075... need <= -0.10: use bid 0.99? price must be <1.
    # Use TARIFF (0.42) against a rich market: bid 0.60, ask 0.64 -> mid 0.62,
    # edge -0.20 -> buy NO at no_bid = 1-0.64 = 0.36.
    market = _market(
        symbol="KXTRUMPMENTION-26AUG27-TARIFF", yes_bid=0.60, yes_ask=0.64
    )
    signals = _strategy(rates_path).analyze(market)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.contract_side == "NO"
    assert sig.side == "buy"
    assert sig.limit_price == pytest.approx(0.36)
    assert sig.confidence == pytest.approx(1.0 - 0.42)


def test_edge_below_min_is_rejected_with_the_measured_edge(
    rates_path, enabled, records
):
    # base_rate 0.42 vs mid 0.42 -> edge 0.0
    market = _market(symbol="KXTRUMPMENTION-26AUG27-TARIFF")
    assert _strategy(rates_path).analyze(market) == []
    message = _reason_of(records)
    assert "reason=MENTION_EDGE_BELOW_MIN" in message
    assert "edge=0" in message
    assert "min_edge=0.1" in message


def test_edge_threshold_either_side(rates_path, enabled):
    # Comfortably above the 0.10 threshold: mid 0.53 -> edge +0.12: emits.
    # (Deliberately not the exact boundary — binary floats make edge==0.10
    # an accident of representation, not a behavior worth pinning.)
    market = _market(yes_bid=0.50, yes_ask=0.56)
    assert len(_strategy(rates_path).analyze(market)) == 1
    # A hair inside the threshold: mid 0.555 -> edge +0.095: rejected.
    market = _market(yes_bid=0.535, yes_ask=0.575)
    assert _strategy(rates_path).analyze(market) == []


def test_one_sided_book_is_rejected(rates_path, enabled, records):
    market = _market(yes_bid=None, yes_ask=0.44)
    market.bid = 0.0  # Kalshi's absent-quote marker
    assert _strategy(rates_path).analyze(market) == []
    assert _reason_code(records) == "MENTION_BOOK_ONE_SIDED"


def test_unknown_word_is_rejected(rates_path, enabled, records):
    market = _market(symbol="KXTRUMPMENTION-26AUG27-BLIMP")
    assert _strategy(rates_path).analyze(market) == []
    message = _reason_of(records)
    assert "reason=MENTION_NO_BASE_RATE" in message
    assert "word=BLIMP" in message


def test_non_mention_symbol_is_rejected(rates_path, enabled, records):
    market = _market(symbol="KXHIGHNY-26AUG27-B82.5")
    assert _strategy(rates_path).analyze(market) == []
    assert _reason_code(records) == "MENTION_NOT_A_MENTION_MARKET"


def test_missing_base_rates_file_rejects_never_defaults(
    tmp_path, enabled, records
):
    strategy = _strategy(str(tmp_path / "absent.json"))
    assert strategy.analyze(_market()) == []
    assert _reason_code(records) == "MENTION_BASE_RATES_UNAVAILABLE"


# --------------------------------------------------------------------------
# Symbol parsing
# --------------------------------------------------------------------------


def test_split_mention_symbol():
    assert split_mention_symbol("KXLEAVITTMENTION-26AUG27-FARM") == (
        "KXLEAVITTMENTION",
        "FARM",
    )
    assert split_mention_symbol("kxtrumpmention-26aug27-tariff") == (
        "KXTRUMPMENTION",
        "TARIFF",
    )
    assert split_mention_symbol("KXHIGHNY-26AUG27-B82.5") is None
    assert split_mention_symbol("KXTRUMPMENTION") is None
    assert split_mention_symbol("") is None
    assert split_mention_symbol(None) is None
