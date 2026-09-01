"""Tests for the feed-only crypto annual harvester bot and the fee-multiplier
plumbing its docstring is a witness for.

Defended here:

* the kill switch ships ``False``, there is no strategy behind it, and the
  tick emits zero signals;
* the harvest covers both annual ladders (``KXBTCY``, ``KXETHY``) with depth
  hourly-only and capped at 30;
* ``SERIES_FEE_MULTIPLIER`` keeps ``KXBTCY``/``KXETHY`` at the conservative
  1.0 (the live API's ``fee_multiplier == 0`` is unverified by a trade), and
  the multiplier wiring leaves DEFAULT fee behavior byte-identical — deleting
  either property is a governance change, and it fails here.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.fee_calculator import (
    FEE_TYPE_WITH_MAKER_FEES,
    SERIES_FEE_MULTIPLIER,
    compute_fee,
    fee_multiplier_for_series,
    fee_multiplier_for_symbol,
    maker_fee,
    taker_fee,
)
from src.core.interfaces import MarketData


def _market(symbol, yes_bid=0.10, yes_ask=0.14, last=0.12, volume=25):
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        price=last,
        volume=volume,
        bid=yes_bid,
        ask=yes_ask,
        extra={
            "status": "active",
            "no_bid": round(1.0 - yes_ask, 4),
            "no_ask": round(1.0 - yes_bid, 4),
            "strike_type": "between",
            "floor_strike": 105000.0,
            "cap_strike": 109999.99,
            "yes_sub_title": "$105,000 to $109,999.99",
            "close_time": "2027-01-01T15:00:00Z",
        },
    )


class _FakeKalshi:
    def __init__(self, ladders):
        self._ladders = ladders
        self.ladder_calls = []
        self.orderbook_calls = []

    def fetch_market_ladder(self, series_ticker, **kwargs):
        self.ladder_calls.append(series_ticker)
        result = self._ladders.get(series_ticker, [])
        if isinstance(result, Exception):
            raise result
        return list(result)

    def fetch_orderbook(self, symbol, depth=3):
        self.orderbook_calls.append(symbol)
        return {"yes": [[0.10, 50]], "no": [[0.85, 50]]}


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
        return False


@pytest.fixture
def bot(monkeypatch):
    from src.bots import crypto_annual_bot as cab

    monkeypatch.setattr(cab.time, "sleep", lambda *_: None)
    return cab.CryptoAnnualBot()


# --------------------------------------------------------------------------
# Posture
# --------------------------------------------------------------------------


def test_crypto_annual_trading_is_disabled_by_default():
    from src.bots import crypto_annual_bot

    assert crypto_annual_bot.CRYPTO_ANNUAL_TRADING_ENABLED is False


def test_crypto_annual_bot_is_registered_and_still_feed_only():
    import src.bots  # noqa: F401 - triggers the sanctioned registrations
    from src.bots import crypto_annual_bot
    from src.bots.registry import BotRegistry

    assert "crypto_annual" in BotRegistry.list_bots()
    assert BotRegistry.create("crypto_annual").name == "CryptoAnnual"
    assert crypto_annual_bot.CRYPTO_ANNUAL_TRADING_ENABLED is False


def test_series_are_the_two_annual_ladders(bot):
    assert bot.get_symbols() == ["KXBTCY", "KXETHY"]


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------


def test_feed_only_tick_harvests_both_series_and_emits_no_signals(bot):
    kalshi = _FakeKalshi(
        {
            "KXBTCY": [_market("KXBTCY-27DEC31-B105000")],
            "KXETHY": [_market("KXETHY-27DEC31-B3750", yes_bid=0.22, yes_ask=0.26)],
        }
    )
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    assert bot.tick(risk, dashboard) == []

    assert kalshi.ladder_calls == ["KXBTCY", "KXETHY"]
    assert "KXBTCY-27DEC31-B105000 (Market)" in dashboard.prices
    assert "KXETHY-27DEC31-B3750 (Market)" in dashboard.prices
    _, kwargs = dashboard.prices["KXBTCY-27DEC31-B105000 (Market)"]
    assert kwargs["floor_strike"] == pytest.approx(105000.0)
    assert risk.exchange.marks["KXETHY-27DEC31-B3750"] == pytest.approx(0.22)
    # FEED-ONLY: the risk gate was never even consulted.
    assert risk.orders == []


def test_depth_snapshot_is_hourly_and_capped_at_30(bot):
    ladder = [_market(f"KXBTCY-27DEC31-B{100 + 5 * i}000") for i in range(35)]
    kalshi = _FakeKalshi({"KXBTCY": ladder, "KXETHY": []})
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    bot.tick(risk, dashboard)

    assert bot.MAX_DEPTH_MARKETS_PER_SERIES == 30
    assert len(kalshi.orderbook_calls) == 30
    assert len(dashboard.depths) == 30

    # An immediate second tick is inside the hourly window: no book calls.
    bot.tick(risk, dashboard)
    assert len(kalshi.orderbook_calls) == 30


def test_one_failing_series_does_not_take_down_the_tick(bot):
    kalshi = _FakeKalshi(
        {
            "KXBTCY": RuntimeError("api down"),
            "KXETHY": [_market("KXETHY-27DEC31-B3750")],
        }
    )
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    assert bot.tick(risk, dashboard) == []
    assert "KXETHY-27DEC31-B3750 (Market)" in dashboard.prices


# --------------------------------------------------------------------------
# Fee multiplier plumbing (src/core/fee_calculator.py)
# --------------------------------------------------------------------------


def test_annual_ladder_entries_stay_conservative_at_1():
    """The live API reports fee_multiplier=0 for KXBTCY/KXETHY. Unverified by
    a trade, so the map must keep both at 1.0 — lowering an entry without the
    demo-API fill receipt is the optimistic-EV failure mode behind both HALTs.
    """
    assert SERIES_FEE_MULTIPLIER["KXBTCY"] == 1.0
    assert SERIES_FEE_MULTIPLIER["KXETHY"] == 1.0
    assert fee_multiplier_for_series("KXBTCY") == 1.0
    assert fee_multiplier_for_symbol("KXETHY-27DEC31-B3750") == 1.0


def test_unknown_series_defaults_to_multiplier_1():
    assert fee_multiplier_for_series("KXHIGHNY") == 1.0
    assert fee_multiplier_for_series("") == 1.0
    assert fee_multiplier_for_symbol("KXAAAGASM-26AUG-B3.25") == 1.0


def test_default_fee_behavior_is_unchanged():
    """The multiplier parameter must be a no-op at its default: these are the
    published-schedule values the Phase 2 21-row table check pinned."""
    assert taker_fee(0.10, 100) == pytest.approx(0.63)
    assert taker_fee(0.50, 1) == pytest.approx(0.02)
    assert maker_fee(0.50, 1) == 0.0  # standard schedule: no maker fee
    assert maker_fee(0.50, 1, FEE_TYPE_WITH_MAKER_FEES) == pytest.approx(0.01)
    # Explicit multiplier=1.0 is byte-identical to omitting it.
    assert compute_fee(0.37, 7, is_maker=False) == compute_fee(
        0.37, 7, is_maker=False, fee_multiplier=1.0
    )
    assert compute_fee(
        0.37, 7, is_maker=True, series_fee_type=FEE_TYPE_WITH_MAKER_FEES
    ) == compute_fee(
        0.37,
        7,
        is_maker=True,
        series_fee_type=FEE_TYPE_WITH_MAKER_FEES,
        fee_multiplier=1.0,
    )


def test_multiplier_scales_both_rate_formulas():
    # Taker at P=0.50, C=100: 0.07*100*0.25 = $1.75; halved -> ceil(0.875) = 0.88.
    assert taker_fee(0.50, 100) == pytest.approx(1.75)
    assert taker_fee(0.50, 100, fee_multiplier=0.5) == pytest.approx(0.88)
    assert taker_fee(0.50, 100, fee_multiplier=0.0) == 0.0
    # Maker on a maker-fee series at P=0.50, C=100: 0.0175*100*0.25 = $0.4375
    # -> $0.44; doubled -> $0.88.
    assert maker_fee(0.50, 100, FEE_TYPE_WITH_MAKER_FEES) == pytest.approx(0.44)
    assert maker_fee(
        0.50, 100, FEE_TYPE_WITH_MAKER_FEES, fee_multiplier=2.0
    ) == pytest.approx(0.88)
