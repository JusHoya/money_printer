"""Tests for the feed-only mention harvester bot.

Defended here:

* the kill switch ships ``False`` and the tick emits zero signals while it is;
* the harvest takes the WHOLE ladder — no ``%y%b%d`` date filter, because
  mention tickers (``KXLEAVITTMENTION-26AUG27-<WORD>``) are not dated the way
  weather tickers are;
* orderbook depth is hourly-only and capped at
  ``MAX_DEPTH_MARKETS_PER_SERIES`` (the FR-0.7 harvester cadence rule);
* one failing series does not take down the tick;
* ``MENTION_SERIES`` is env-overridable.
"""

import importlib
import logging
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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
            "strike_type": None,
            "floor_strike": None,
            "cap_strike": None,
            "yes_sub_title": None,
            "close_time": "2026-08-28T03:59:00Z",
        },
    )


class _FakeKalshi:
    def __init__(self, ladders):
        # series -> list[MarketData], or an Exception instance to raise
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
    from src.bots import mention_bot as mb

    monkeypatch.setattr(mb.time, "sleep", lambda *_: None)
    instance = mb.MentionBot()
    instance.SERIES = ("KXTRUMPMENTION",)
    return instance


def test_mention_trading_is_disabled_by_default():
    from src.bots import mention_bot

    assert mention_bot.MENTION_TRADING_ENABLED is False


def test_mention_bot_is_registered_and_still_feed_only():
    import src.bots  # noqa: F401 - triggers the sanctioned registrations
    from src.bots import mention_bot
    from src.bots.registry import BotRegistry

    assert "mention" in BotRegistry.list_bots()
    assert BotRegistry.create("mention").name == "Mention"
    assert mention_bot.MENTION_TRADING_ENABLED is False


def test_default_series_tuple():
    from src.bots import mention_bot

    if os.getenv("MENTION_SERIES"):
        pytest.skip("MENTION_SERIES is set in this environment")
    assert mention_bot.MENTION_SERIES == (
        "KXTRUMPMENTION",
        "KXPRESMENTION",
        "KXLEAVITTMENTION",
    )


def test_feed_only_tick_harvests_whole_ladder_and_emits_no_signals(bot):
    # Two events with different dates on purpose: no date filter may apply.
    ladder = [
        _market("KXTRUMPMENTION-26AUG27-FARM"),
        _market("KXTRUMPMENTION-26DEC31-TARIFF", yes_bid=0.30, yes_ask=0.34),
    ]
    kalshi = _FakeKalshi({"KXTRUMPMENTION": ladder})
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    assert bot.tick(risk, dashboard) == []

    assert kalshi.ladder_calls == ["KXTRUMPMENTION"]
    assert "KXTRUMPMENTION-26AUG27-FARM (Market)" in dashboard.prices
    assert "KXTRUMPMENTION-26DEC31-TARIFF (Market)" in dashboard.prices
    # Quote row carries the full no-side context for the CSV tape.
    price, kwargs = dashboard.prices["KXTRUMPMENTION-26AUG27-FARM (Market)"]
    assert price == pytest.approx(0.10)
    assert kwargs["no_bid"] == pytest.approx(0.86)
    assert kwargs["volume"] == 25
    # Valuation marks reached both the risk manager and the exchange.
    assert risk.data["KXTRUMPMENTION-26DEC31-TARIFF"] == pytest.approx(0.30)
    assert risk.exchange.marks["KXTRUMPMENTION-26AUG27-FARM"] == pytest.approx(0.10)
    # FEED-ONLY: the risk gate was never even consulted.
    assert risk.orders == []


def test_depth_snapshot_is_hourly_and_capped(bot):
    ladder = [_market(f"KXTRUMPMENTION-26AUG27-W{i:02d}") for i in range(25)]
    kalshi = _FakeKalshi({"KXTRUMPMENTION": ladder})
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    bot.tick(risk, dashboard)

    # First tick snapshots (baseline), capped at 20 of the 25 markets.
    assert bot.MAX_DEPTH_MARKETS_PER_SERIES == 20
    assert len(kalshi.orderbook_calls) == 20
    assert len(dashboard.depths) == 20

    # An immediate second tick is inside the hourly window: no book calls.
    bot.tick(risk, dashboard)
    assert len(kalshi.orderbook_calls) == 20


def test_one_failing_series_does_not_take_down_the_tick(bot):
    bot.SERIES = ("KXPRESMENTION", "KXTRUMPMENTION")
    kalshi = _FakeKalshi(
        {
            "KXPRESMENTION": RuntimeError("api down"),
            "KXTRUMPMENTION": [_market("KXTRUMPMENTION-26AUG27-FARM")],
        }
    )
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    assert bot.tick(risk, dashboard) == []

    assert kalshi.ladder_calls == ["KXPRESMENTION", "KXTRUMPMENTION"]
    assert "KXTRUMPMENTION-26AUG27-FARM (Market)" in dashboard.prices


def test_mention_series_env_override(monkeypatch):
    from src.bots import mention_bot as mb

    monkeypatch.setenv("MENTION_SERIES", "kxfoomention, KXBARMENTION ,")
    try:
        importlib.reload(mb)
        assert mb.MENTION_SERIES == ("KXFOOMENTION", "KXBARMENTION")
        assert mb.MENTION_TRADING_ENABLED is False
    finally:
        monkeypatch.delenv("MENTION_SERIES", raising=False)
        importlib.reload(mb)
    assert mb.MENTION_SERIES == (
        "KXTRUMPMENTION",
        "KXPRESMENTION",
        "KXLEAVITTMENTION",
    )


def test_mention_series_env_capped_with_warning(monkeypatch, caplog):
    """15 configured series -> only MAX_MENTION_SERIES (12) harvested; the
    dropped series are named in a WARNING. Each series costs ~1s of the
    single-threaded market loop per tick, so the env var must not be able
    to widen toward the 95-series category unbounded."""
    from src.bots import mention_bot as mb

    names = [f"KXM{i:02d}MENTION" for i in range(15)]
    monkeypatch.setenv("MENTION_SERIES", ",".join(names))
    # The shared "MoneyPrinter" logger does not propagate to root, so attach
    # the capture handler directly for the duration of the reload.
    mp_logger = logging.getLogger("MoneyPrinter")
    mp_logger.addHandler(caplog.handler)
    try:
        importlib.reload(mb)
        assert mb.MAX_MENTION_SERIES == 12
        assert mb.MENTION_SERIES == tuple(names[:12])
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "[Mention]" in r.getMessage()
        ]
        assert any(all(n in msg for n in names[12:]) for msg in warnings)
    finally:
        mp_logger.removeHandler(caplog.handler)
        monkeypatch.delenv("MENTION_SERIES", raising=False)
        importlib.reload(mb)
    assert mb.MENTION_SERIES == (
        "KXTRUMPMENTION",
        "KXPRESMENTION",
        "KXLEAVITTMENTION",
    )
