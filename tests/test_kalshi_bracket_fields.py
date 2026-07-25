"""KalshiProvider must surface bracket semantics on every MarketData (FR-1.1).

``MarketData.extra`` carries ``strike_type``, ``floor_strike``, ``cap_strike``
and ``yes_sub_title`` for every market, on every code path that builds a
``MarketData``. Everything downstream -- strategies, the sim settlement path,
the reconcile report -- derives contract direction from these fields and never
from the ticker string.

Offline tests run against ``tests/fixtures/kxhighny_markets.json``, a verbatim
capture of::

    GET https://api.elections.kalshi.com/trade-api/v2/markets
        ?series_ticker=KXHIGHNY&status=open&limit=40

taken anonymously on 2026-07-25. One clearly-marked live test re-probes that
endpoint so a Kalshi schema change surfaces loudly; it skips on any network
failure so CI stays deterministic offline.
"""

from __future__ import annotations

import json
import os

import pytest

from src.core.bracket_payoff import (
    STRIKE_TYPE_BETWEEN,
    STRIKE_TYPE_GREATER,
    STRIKE_TYPE_LESS,
    parse_bracket_spec,
)
from src.data.kalshi_provider import KalshiProvider

DEG = "°"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "kxhighny_markets.json")
LIVE_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"

# Live-probed 2026-07-25. (ticker, strike_type, floor_strike, cap_strike)
GOLDEN_TICKERS = {
    "KXHIGHNY-26JUL25-T87": (STRIKE_TYPE_GREATER, 87.0, None),
    "KXHIGHNY-26JUL25-T80": (STRIKE_TYPE_LESS, None, 80.0),
    "KXHIGHNY-26JUL25-B86.5": (STRIKE_TYPE_BETWEEN, 86.0, 87.0),
    "KXHIGHNY-26JUL26-T88": (STRIKE_TYPE_GREATER, 88.0, None),
    "KXHIGHNY-26JUL26-T81": (STRIKE_TYPE_LESS, None, 81.0),
    "KXHIGHNY-26JUL26-B87.5": (STRIKE_TYPE_BETWEEN, 87.0, 88.0),
}


@pytest.fixture(scope="module")
def fixture_markets():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    markets = payload["markets"]
    assert markets, "fixture must contain markets"
    return markets


@pytest.fixture
def provider():
    return KalshiProvider(key_id=None, private_key_path=None, read_only=True)


# ----------------------------------------------------------------------
# Offline: fixture -> _parse_market_data -> extra
# ----------------------------------------------------------------------


def test_fixture_covers_all_three_strike_types(fixture_markets):
    types = {m.get("strike_type") for m in fixture_markets}
    assert {STRIKE_TYPE_BETWEEN, STRIKE_TYPE_GREATER, STRIKE_TYPE_LESS} <= types


@pytest.mark.parametrize("ticker", sorted(GOLDEN_TICKERS))
def test_golden_ticker_bracket_fields_in_extra(provider, fixture_markets, ticker):
    """The six live-probed goldens parse into extra with exact values."""
    raw = next((m for m in fixture_markets if m.get("ticker") == ticker), None)
    assert raw is not None, f"{ticker} missing from fixture"

    md = provider._parse_market_data(ticker, raw, "fixture")
    strike_type, floor_strike, cap_strike = GOLDEN_TICKERS[ticker]

    assert md.extra["strike_type"] == strike_type
    assert md.extra["floor_strike"] == floor_strike
    assert md.extra["cap_strike"] == cap_strike


def test_every_fixture_market_carries_the_bracket_keys(provider, fixture_markets):
    """FR-1.1: the keys are present on EVERY market, not just the goldens."""
    required = ("strike_type", "floor_strike", "cap_strike", "yes_sub_title")
    for raw in fixture_markets:
        md = provider._parse_market_data(raw["ticker"], raw, "fixture")
        for key in required:
            assert key in md.extra, f"{raw['ticker']}: extra missing {key!r}"
        assert md.extra["strike_type"] is not None, raw["ticker"]
        assert md.extra["yes_sub_title"], raw["ticker"]


def test_strike_field_types_are_float_or_none(provider, fixture_markets):
    """Strikes are floats when present and None when absent -- never 0.0."""
    for raw in fixture_markets:
        md = provider._parse_market_data(raw["ticker"], raw, "fixture")
        for key in ("floor_strike", "cap_strike"):
            value = md.extra[key]
            assert value is None or isinstance(
                value, float
            ), f"{raw['ticker']}: {key} is {type(value).__name__}"
            assert (
                value != 0.0
            ), f"{raw['ticker']}: {key} is 0.0 -- a missing strike was coerced"
        assert isinstance(md.extra["strike_type"], str)
        assert isinstance(md.extra["yes_sub_title"], str)


def test_absent_strikes_stay_none(provider, fixture_markets):
    """``greater`` has no cap_strike; ``less`` has no floor_strike."""
    seen = set()
    for raw in fixture_markets:
        md = provider._parse_market_data(raw["ticker"], raw, "fixture")
        stype = md.extra["strike_type"]
        seen.add(stype)
        if stype == STRIKE_TYPE_GREATER:
            assert md.extra["floor_strike"] is not None, raw["ticker"]
            assert md.extra["cap_strike"] is None, raw["ticker"]
        elif stype == STRIKE_TYPE_LESS:
            assert md.extra["floor_strike"] is None, raw["ticker"]
            assert md.extra["cap_strike"] is not None, raw["ticker"]
        elif stype == STRIKE_TYPE_BETWEEN:
            assert md.extra["floor_strike"] is not None, raw["ticker"]
            assert md.extra["cap_strike"] is not None, raw["ticker"]
            assert md.extra["cap_strike"] > md.extra["floor_strike"], raw["ticker"]
    assert len(seen) == 3


def test_every_fixture_market_builds_a_bracket_spec(provider, fixture_markets):
    """extra -> parse_bracket_spec succeeds for every live market."""
    for raw in fixture_markets:
        md = provider._parse_market_data(raw["ticker"], raw, "fixture")
        spec = parse_bracket_spec(md.symbol, md.extra)
        published = md.extra["yes_sub_title"].replace(DEG, "")
        assert spec.describe() == published, raw["ticker"]


def test_yes_sub_title_is_kalshis_published_rule(provider, fixture_markets):
    for raw in fixture_markets:
        md = provider._parse_market_data(raw["ticker"], raw, "fixture")
        assert md.extra["yes_sub_title"] == raw["yes_sub_title"]


def test_legacy_strike_alias_still_mirrors_floor_strike(provider, fixture_markets):
    """``extra['strike']`` stays as the mothballed crypto path expects it."""
    for raw in fixture_markets:
        md = provider._parse_market_data(raw["ticker"], raw, "fixture")
        assert md.extra["strike"] == md.extra["floor_strike"]


def test_ladder_path_carries_bracket_fields(provider, fixture_markets, monkeypatch):
    """fetch_market_ladder must not drop bracket fields (single build site)."""

    def fake_page(api_url, params):
        return list(fixture_markets), None

    monkeypatch.setattr(provider, "_fetch_markets_page", fake_page)
    out = provider.fetch_market_ladder("KXHIGHNY", statuses=("active",))
    assert out, "ladder returned nothing"
    for md in out:
        assert md.extra["strike_type"] in (
            STRIKE_TYPE_BETWEEN,
            STRIKE_TYPE_GREATER,
            STRIKE_TYPE_LESS,
        )
        assert "floor_strike" in md.extra and "cap_strike" in md.extra
        parse_bracket_spec(md.symbol, md.extra)


def test_fetch_latest_carries_bracket_fields(provider, fixture_markets, monkeypatch):
    raw = next(m for m in fixture_markets if m["ticker"].endswith("T80"))

    monkeypatch.setattr(
        provider, "_fetch_market_raw", lambda symbol, api_url: dict(raw)
    )
    md = provider.fetch_latest(raw["ticker"])
    assert md is not None
    assert md.extra["strike_type"] == STRIKE_TYPE_LESS
    assert md.extra["floor_strike"] is None
    assert md.extra["cap_strike"] == 80.0


# ----------------------------------------------------------------------
# Synthetic edge cases (no fixture dependency)
# ----------------------------------------------------------------------


def test_missing_strike_fields_are_none_not_zero(provider):
    md = provider._parse_market_data("KX-NOSTRIKES", {"status": "active"}, "test")
    assert md.extra["floor_strike"] is None
    assert md.extra["cap_strike"] is None
    assert md.extra["strike_type"] is None
    assert md.extra["yes_sub_title"] is None


def test_zero_strike_is_preserved_not_confused_with_missing(provider):
    """A genuine 0 strike must round-trip as 0.0, distinct from None."""
    md = provider._parse_market_data(
        "KX-ZERO", {"strike_type": "greater", "floor_strike": 0}, "test"
    )
    assert md.extra["floor_strike"] == 0.0
    assert md.extra["cap_strike"] is None


def test_string_strikes_are_coerced_to_float(provider):
    md = provider._parse_market_data(
        "KX-STR",
        {"strike_type": "BETWEEN", "floor_strike": "86", "cap_strike": "87.0"},
        "test",
    )
    assert md.extra["floor_strike"] == 86.0
    assert md.extra["cap_strike"] == 87.0
    # strike_type is normalized so downstream comparisons are case-proof.
    assert md.extra["strike_type"] == STRIKE_TYPE_BETWEEN


def test_garbage_strike_becomes_none_and_aborts_downstream(provider):
    """A malformed strike must not become a number we would trade on."""
    md = provider._parse_market_data(
        "KX-GARBAGE",
        {"strike_type": "between", "floor_strike": "warm", "cap_strike": 87},
        "test",
    )
    assert md.extra["floor_strike"] is None
    with pytest.raises(Exception):
        parse_bracket_spec(md.symbol, md.extra)


def test_blank_strike_type_is_none(provider):
    md = provider._parse_market_data(
        "KX-BLANK", {"strike_type": "   ", "floor_strike": 86}, "test"
    )
    assert md.extra["strike_type"] is None


def test_v1_aliases_are_folded_in(provider):
    """V1 (BTC hourly discovery) uses close_date / sub_title."""
    md = provider._parse_market_data(
        "KXBTCD-26JUL2512-T61000",
        {
            "strike_type": "greater",
            "floor_strike": 61000,
            "close_date": "2026-07-25T17:00:00Z",
            "sub_title": "$61,000 or above",
            "yes_bid": 45,
            "yes_ask": 55,
        },
        "v1_discovery",
    )
    assert md.extra["close_time"] == "2026-07-25T17:00:00Z"
    assert md.extra["yes_sub_title"] == "$61,000 or above"
    assert md.extra["floor_strike"] == 61000.0
    assert md.extra["cap_strike"] is None
    assert md.bid == 0.45 and md.ask == 0.55


# ----------------------------------------------------------------------
# LIVE probe -- skips on any network failure
# ----------------------------------------------------------------------


def test_live_kalshi_bracket_semantics_unchanged():
    """LIVE: re-probe Kalshi and fail loudly if bracket semantics changed.

    Skips on any request failure so the suite stays green offline. Asserts:
      * the golden tickers (when still listed) keep their exact
        strike_type / floor_strike / cap_strike;
      * the structural invariant holds for every open KXHIGHNY market --
        ``greater`` has only a floor, ``less`` has only a cap, ``between``
        has both with cap == floor + 1;
      * our derived rule still equals Kalshi's own ``yes_sub_title``.
    """
    requests = pytest.importorskip("requests")

    try:
        resp = requests.get(
            LIVE_URL,
            params={"series_ticker": "KXHIGHNY", "status": "open", "limit": 40},
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
    except Exception as exc:  # noqa: BLE001 - offline is a skip, not a failure
        pytest.skip(f"Kalshi live probe unavailable: {exc}")

    if not markets:
        pytest.skip("no open KXHIGHNY markets right now")

    provider = KalshiProvider(key_id=None, private_key_path=None, read_only=True)
    by_ticker = {}
    for raw in markets:
        md = provider._parse_market_data(raw["ticker"], raw, "live")
        by_ticker[raw["ticker"]] = md

        stype = md.extra["strike_type"]
        floor_strike = md.extra["floor_strike"]
        cap_strike = md.extra["cap_strike"]
        assert stype in (
            STRIKE_TYPE_BETWEEN,
            STRIKE_TYPE_GREATER,
            STRIKE_TYPE_LESS,
        ), f"{raw['ticker']}: unknown strike_type {stype!r} -- Kalshi schema changed"

        if stype == STRIKE_TYPE_GREATER:
            assert floor_strike is not None and cap_strike is None, raw["ticker"]
        elif stype == STRIKE_TYPE_LESS:
            assert cap_strike is not None and floor_strike is None, raw["ticker"]
        else:
            assert floor_strike is not None and cap_strike is not None, raw["ticker"]
            assert cap_strike == floor_strike + 1, raw["ticker"]

        spec = parse_bracket_spec(md.symbol, md.extra)
        published = (md.extra["yes_sub_title"] or "").replace(DEG, "")
        assert spec.describe() == published, (
            f"{raw['ticker']}: our rule {spec.describe()!r} no longer matches "
            f"Kalshi's published {published!r}"
        )

    matched = 0
    for ticker, (stype, floor_strike, cap_strike) in GOLDEN_TICKERS.items():
        md = by_ticker.get(ticker)
        if md is None:
            continue  # market has since expired off the open list
        matched += 1
        assert md.extra["strike_type"] == stype, ticker
        assert md.extra["floor_strike"] == floor_strike, ticker
        assert md.extra["cap_strike"] == cap_strike, ticker

    assert matched or by_ticker, "live probe verified nothing"
