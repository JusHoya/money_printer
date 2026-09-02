"""Tests for the feed-only X-settled-market harvester (``tweets`` bot).

Defended here:

* the kill switch ships ``False``, there is no strategy behind it, the bot is
  registered as ``tweets``, and a tick emits zero signals;
* with ``X_FEED_ENABLED`` off (the default) the tick harvests the Kalshi
  ladders and makes NO X request — the pay-per-use meter cannot start by
  accident;
* with the feed on, a poll appends to the tape, writes one ``@handle (X)``
  data-log row with the running counts, and respects the provider's >=60s
  floor on the next tick;
* the default handle set is ONLY the account behind the live market
  (@realDonaldTrump) — @elonmusk is a cost decision made in the module
  docstring, and a silent default change would undo it;
* one failing Kalshi series does not take the tick down, and a failed X
  ``connect()`` is retried rather than left dead.
"""

import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.interfaces import MarketData
from src.data.x_provider import XProvider
from tests.test_x_provider import USERS_BY, _FakeResponse, _FakeSession, _timeline


def _market(symbol, yes_bid=0.62, yes_ask=0.69, last=0.65, volume=32):
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
            "strike_type": "greater",
            "floor_strike": 0.0,
            "cap_strike": None,
            "yes_sub_title": "Above 0",
            "close_time": "2026-10-01T16:00:00Z",
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
        return {"yes": [[0.62, 10]], "no": [[0.31, 10]]}


class _FakeDashboard:
    def __init__(self):
        self.prices = {}
        self.rows = []
        self.depths = []

    def update_price(self, name, price, **kwargs):
        self.prices[name] = (price, kwargs)
        self.rows.append((name, price, kwargs))

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


POTUS_USER = _FakeResponse({"data": [{"id": "25073877", "username": "realDonaldTrump"}]})


def _kalshi():
    return _FakeKalshi(
        {
            "KXPOTUSTWEETS": [_market("KXPOTUSTWEETS-26OCT01-0")],
            "KXELONTWEETS": [],  # dormant since 2025-04 (live-verified 2026-09-01)
        }
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    from src.bots import tweets_bot

    monkeypatch.setattr(tweets_bot.time, "sleep", lambda *_: None)


def _bot_with_x(monkeypatch, tmp_path, responses, enabled=True):
    from src.bots.tweets_bot import TweetsBot

    if enabled:
        monkeypatch.setenv("X_FEED_ENABLED", "1")
        monkeypatch.setenv("X_BEARER_TOKEN", "test-bearer")
    else:
        monkeypatch.delenv("X_FEED_ENABLED", raising=False)
    session = _FakeSession(responses)
    provider = XProvider(handles=["realDonaldTrump"], feed_dir=tmp_path, session=session)
    return TweetsBot(x_provider=provider), session


# --------------------------------------------------------------------------
# Posture
# --------------------------------------------------------------------------


def test_tweets_trading_is_disabled_and_registered_feed_only():
    import src.bots  # noqa: F401 - triggers the sanctioned registrations
    from src.bots import tweets_bot
    from src.bots.registry import BotRegistry

    assert tweets_bot.TWEETS_TRADING_ENABLED is False
    assert "tweets" in BotRegistry.list_bots()
    assert BotRegistry.create("tweets").name == "Tweets"


def test_default_series_and_handles(monkeypatch):
    from src.bots import tweets_bot

    monkeypatch.delenv("X_TRACK_HANDLES", raising=False)
    monkeypatch.delenv("TWEETS_SERIES", raising=False)
    assert tweets_bot._parse_tweets_series() == ("KXPOTUSTWEETS", "KXELONTWEETS")
    # Cost decision: only the account behind the live market by default.
    assert tweets_bot._default_handles() == ["realDonaldTrump"]
    assert "elonmusk" not in [h.lower() for h in tweets_bot.DEFAULT_TRACK_HANDLES]

    monkeypatch.setenv("X_TRACK_HANDLES", "@elonmusk, realDonaldTrump")
    assert tweets_bot._default_handles() == ["elonmusk", "realDonaldTrump"]


def test_series_env_override_is_capped(monkeypatch):
    from src.bots import tweets_bot

    monkeypatch.setenv("TWEETS_SERIES", ",".join(f"KXS{i}" for i in range(9)))
    parsed = tweets_bot._parse_tweets_series()
    assert len(parsed) == tweets_bot.MAX_TWEETS_SERIES
    assert parsed[0] == "KXS0"


# --------------------------------------------------------------------------
# X disabled (the shipped default)
# --------------------------------------------------------------------------


def test_x_disabled_tick_harvests_kalshi_and_never_touches_x(monkeypatch, tmp_path):
    bot, session = _bot_with_x(monkeypatch, tmp_path, [], enabled=False)
    kalshi = _kalshi()
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    assert bot.x_connected is False
    assert bot.tick(risk, dashboard) == []

    assert kalshi.ladder_calls == ["KXPOTUSTWEETS", "KXELONTWEETS"]
    assert "KXPOTUSTWEETS-26OCT01-0 (Market)" in dashboard.prices
    assert risk.exchange.marks["KXPOTUSTWEETS-26OCT01-0"] == pytest.approx(0.62)
    # FEED-ONLY: the risk gate was never consulted, and the X meter never ran.
    assert risk.orders == []
    assert session.calls == []
    assert not any(name.endswith("(X)") for name in dashboard.prices)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# X enabled
# --------------------------------------------------------------------------


def test_x_enabled_poll_writes_tape_and_dashboard_row(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    posts = [
        {"id": "902", "text": "second", "created_at": f"{today}T12:05:00.000Z"},
        {"id": "901", "text": "first", "created_at": "2026-01-01T12:00:00.000Z"},
    ]
    bot, session = _bot_with_x(
        monkeypatch,
        tmp_path,
        [POTUS_USER, _timeline(posts, newest_id="902"), _timeline([])],
    )
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(_kalshi())
    assert bot.x_connected is True
    assert bot.tick(risk, dashboard) == []

    # The tape has both raw posts.
    tape = tmp_path / f"x_posts_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    lines = [json.loads(l) for l in tape.read_text(encoding="utf-8").splitlines()]
    assert [l["post"]["id"] for l in lines] == ["902", "901"]

    # One dashboard row: today's count by created_at, new-this-poll, running total.
    price, kwargs = dashboard.prices["@realDonaldTrump (X)"]
    assert price == 1.0
    assert kwargs["volume"] == 2
    assert kwargs["last"] == 2

    # Immediate second tick: inside the 60s floor -> no new X request, no new row.
    n_calls = len(session.calls)
    n_rows = len(dashboard.rows)
    bot.tick(risk, dashboard)
    assert len(session.calls) == n_calls
    assert len(dashboard.rows) == n_rows + 1  # only the Kalshi ladder row

    # Past the floor with nothing new: one request, and a heartbeat row whose
    # counts are unchanged and whose volume is 0.
    bot.x._last_poll["realdonaldtrump"] -= bot.x.MIN_POLL_INTERVAL_S + 1
    bot.tick(risk, dashboard)
    assert len(session.calls) == n_calls + 1
    assert session.calls[-1]["params"]["since_id"] == "902"
    price, kwargs = dashboard.prices["@realDonaldTrump (X)"]
    assert price == 1.0
    assert kwargs["volume"] == 0
    assert kwargs["last"] == 2
    assert len(dashboard.rows) == n_rows + 3  # Kalshi row + X heartbeat row


def test_x_first_poll_with_no_posts_still_writes_a_heartbeat_row(monkeypatch, tmp_path):
    bot, _ = _bot_with_x(monkeypatch, tmp_path, [POTUS_USER, _timeline([])])
    dashboard = _FakeDashboard()

    bot.setup(_kalshi())
    bot.tick(_FakeRiskManager(), dashboard)
    price, kwargs = dashboard.prices["@realDonaldTrump (X)"]
    assert price == 0.0
    assert kwargs["volume"] == 0
    assert kwargs["last"] == 0
    assert list(tmp_path.iterdir()) == []  # nothing to tape


def test_failed_x_connect_is_retried_after_the_interval(monkeypatch, tmp_path):
    from src.bots import tweets_bot

    bot, session = _bot_with_x(
        monkeypatch,
        tmp_path,
        [_FakeResponse({}, status=500), POTUS_USER, _timeline([])],
    )
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(_kalshi())
    assert bot.x_connected is False
    assert len(session.calls) == 1

    # Inside the retry window: no reconnect attempt.
    bot.tick(risk, dashboard)
    assert len(session.calls) == 1

    # Past the window: reconnect succeeds and the poll proceeds.
    bot._next_x_connect -= tweets_bot.X_CONNECT_RETRY_S + 1
    bot.tick(risk, dashboard)
    assert bot.x_connected is True
    assert len(session.calls) == 3


# --------------------------------------------------------------------------
# Kalshi side robustness
# --------------------------------------------------------------------------


def test_one_failing_series_does_not_take_down_the_tick(monkeypatch, tmp_path):
    bot, _ = _bot_with_x(monkeypatch, tmp_path, [], enabled=False)
    kalshi = _FakeKalshi(
        {
            "KXPOTUSTWEETS": RuntimeError("api down"),
            "KXELONTWEETS": [_market("KXELONTWEETS-26SEP05-B300")],
        }
    )
    dashboard = _FakeDashboard()

    bot.setup(kalshi)
    assert bot.tick(_FakeRiskManager(), dashboard) == []
    assert "KXELONTWEETS-26SEP05-B300 (Market)" in dashboard.prices


def test_depth_snapshot_is_hourly_and_capped(monkeypatch, tmp_path):
    bot, _ = _bot_with_x(monkeypatch, tmp_path, [], enabled=False)
    ladder = [_market(f"KXELONTWEETS-26SEP05-B{100 + 50 * i}") for i in range(25)]
    kalshi = _FakeKalshi({"KXPOTUSTWEETS": [], "KXELONTWEETS": ladder})
    dashboard = _FakeDashboard()
    risk = _FakeRiskManager()

    bot.setup(kalshi)
    bot.tick(risk, dashboard)
    assert len(kalshi.orderbook_calls) == bot.MAX_DEPTH_MARKETS_PER_SERIES == 20

    bot.tick(risk, dashboard)  # inside the hourly window: no more book calls
    assert len(kalshi.orderbook_calls) == 20
