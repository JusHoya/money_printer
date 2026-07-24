"""Phase 0 harvester tests (FR-0.7, exit criterion 6).

Covers:
- Kalshi orderbook parsing against the review-verified ``orderbook_fp``
  shape (string [price, qty] arrays), plus legacy shapes.
- ``fetch_orderbook`` call paths (single call, demo->production fallback).
- ``fetch_market_ladder``: one list call, V2 ``*_dollars`` parsing, bid+ask
  for every bracket, status/test-symbol filtering, pagination.
- Data-CSV row format: extended MARKET_DATA schema, DEPTH rows with JSON
  top-3 both sides, SIGNAL rows, and backward compatibility with existing
  consumers (pandas usecols read, DictReader read, legacy-header files).
- WeatherBot feed path: full-ladder capture with bid+ask for all brackets,
  active-market selection, and HOURLY (never per-tick) depth cadence.

Run: $env:PYTHONPATH="."; python -m pytest tests/test_phase0_harvester.py
"""

import csv
import json
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.core.interfaces import MarketData, TradeSignal
from src.data.kalshi_provider import KalshiProvider


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Review-verified live shape (2026-07-24):
# curl /markets/KXHIGHCHI-26JUL24-B78.5/orderbook
#   -> orderbook_fp.yes_dollars = [['0.2400', '31.00'], ...]
REVIEW_ORDERBOOK_FP = {
    "orderbook_fp": {
        "yes_dollars": [
            ["0.2400", "31.00"],
            ["0.2300", "150.00"],
            ["0.1000", "5.00"],
            ["0.0500", "2.00"],
        ],
        "no_dollars": [
            ["0.7000", "10.00"],
            ["0.7200", "40.00"],
        ],
    }
}


def _mock_response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _market_dict(
    ticker,
    status="active",
    bid="0.7200",
    ask="0.7400",
    no_bid="0.2600",
    no_ask="0.2800",
    last="0.7300",
    vol="1500.00",
    floor_strike=81.5,
):
    """A V2 /markets list entry with *_dollars STRING fields only."""
    return {
        "ticker": ticker,
        "status": status,
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
        "last_price_dollars": last,
        "volume_fp": vol,
        "floor_strike": floor_strike,
        "strike_type": "between",
        "close_time": "2026-07-25T00:00:00Z",
    }


def _md(symbol, bid, ask, no_bid, no_ask, last, vol):
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(),
        price=last,
        volume=vol,
        bid=bid,
        ask=ask,
        extra={"no_bid": no_bid, "no_ask": no_ask, "status": "active"},
    )


# ---------------------------------------------------------------------------
# Orderbook parsing (orderbook_fp string arrays — review-verified shape)
# ---------------------------------------------------------------------------


class TestOrderbookParsing:
    def test_review_verified_orderbook_fp_shape(self):
        book = KalshiProvider._parse_orderbook_levels(REVIEW_ORDERBOOK_FP, depth=3)
        # Floats, best-first, top-3 only
        assert book["yes"] == [(0.24, 31.0), (0.23, 150.0), (0.10, 5.0)]
        assert book["no"] == [(0.72, 40.0), (0.70, 10.0)]
        for price, qty in book["yes"] + book["no"]:
            assert isinstance(price, float)
            assert isinstance(qty, float)

    def test_dollars_arrays_under_orderbook_wrapper(self):
        payload = {"orderbook": REVIEW_ORDERBOOK_FP["orderbook_fp"]}
        book = KalshiProvider._parse_orderbook_levels(payload, depth=3)
        assert book["yes"][0] == (0.24, 31.0)
        assert book["no"][0] == (0.72, 40.0)

    def test_legacy_integer_cents_shape(self):
        payload = {"orderbook": {"yes": [[24, 31], [23, 150]], "no": [[70, 10]]}}
        book = KalshiProvider._parse_orderbook_levels(payload, depth=3)
        assert book["yes"] == [(0.24, 31.0), (0.23, 150.0)]
        assert book["no"] == [(0.70, 10.0)]

    def test_empty_and_missing_payloads(self):
        assert KalshiProvider._parse_orderbook_levels({}) == {"yes": [], "no": []}
        assert KalshiProvider._parse_orderbook_levels(None) == {"yes": [], "no": []}
        assert KalshiProvider._parse_orderbook_levels(
            {"orderbook_fp": {"yes_dollars": None, "no_dollars": None}}
        ) == {"yes": [], "no": []}

    def test_malformed_levels_skipped(self):
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.2400", "31.00"], ["bad", "x"], ["0.10"]],
                "no_dollars": [],
            }
        }
        book = KalshiProvider._parse_orderbook_levels(payload)
        assert book["yes"] == [(0.24, 31.0)]
        assert book["no"] == []

    def test_depth_truncation(self):
        book = KalshiProvider._parse_orderbook_levels(REVIEW_ORDERBOOK_FP, depth=2)
        assert len(book["yes"]) == 2
        assert book["yes"][0][0] >= book["yes"][1][0]  # best first


class TestFetchOrderbook:
    def test_single_call_on_public_api(self):
        prov = KalshiProvider()  # anonymous, production URL
        prov.session = MagicMock()
        prov.session.get.return_value = _mock_response(REVIEW_ORDERBOOK_FP)

        book = prov.fetch_orderbook("KXHIGHCHI-26JUL24-B78.5", depth=3)

        assert book["yes"][0] == (0.24, 31.0)
        assert prov.session.get.call_count == 1
        url = prov.session.get.call_args[0][0]
        assert url.endswith("/markets/KXHIGHCHI-26JUL24-B78.5/orderbook")

    def test_demo_empty_book_falls_back_to_production(self):
        prov = KalshiProvider(api_url="https://demo-api.kalshi.co/trade-api/v2")
        prov.session = MagicMock()
        prov.session.get.side_effect = [
            _mock_response({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}),
            _mock_response(REVIEW_ORDERBOOK_FP),
        ]

        book = prov.fetch_orderbook("KXHIGHCHI-26JUL24-B78.5")

        assert book["yes"][0] == (0.24, 31.0)
        assert prov.session.get.call_count == 2
        second_url = prov.session.get.call_args_list[1][0][0]
        assert second_url.startswith(KalshiProvider.PUBLIC_API_URL)

    def test_error_returns_none(self):
        prov = KalshiProvider()
        prov.session = MagicMock()
        prov.session.get.side_effect = Exception("boom")
        assert prov.fetch_orderbook("KXHIGHNY-26JUL24-B81.5") is None


# ---------------------------------------------------------------------------
# Ladder fetch: one list call, quotes for every bracket
# ---------------------------------------------------------------------------


class TestFetchMarketLadder:
    def test_parses_v2_dollar_strings_for_all_brackets(self):
        prov = KalshiProvider()
        prov.session = MagicMock()
        prov.session.get.return_value = _mock_response(
            {
                "markets": [
                    _market_dict("KXHIGHNY-26JUL24-B81.5"),
                    _market_dict(
                        "KXHIGHNY-26JUL24-B83.5",
                        bid="0.1500",
                        ask="0.1800",
                        no_bid="0.8200",
                        no_ask="0.8500",
                        last="0.1600",
                        vol="900.00",
                        floor_strike=83.5,
                    ),
                    _market_dict("KXHIGHNY-25JUL23-B79.5", status="settled"),
                    _market_dict("KX-TEST-FAKE-B1"),
                ],
                "cursor": None,
            }
        )

        mkts = prov.fetch_market_ladder("KXHIGHNY")

        # settled + test symbols filtered out
        assert [m.symbol for m in mkts] == [
            "KXHIGHNY-26JUL24-B81.5",
            "KXHIGHNY-26JUL24-B83.5",
        ]
        m0 = mkts[0]
        assert m0.bid == 0.72 and m0.ask == 0.74
        assert m0.extra["no_bid"] == 0.26 and m0.extra["no_ask"] == 0.28
        assert m0.price == 0.73
        assert m0.volume == 1500
        assert m0.extra["strike"] == 81.5
        # Every bracket carries both bid AND ask
        for m in mkts:
            assert m.bid > 0 and m.ask > 0
        # Exactly ONE list call — no per-market quote calls
        assert prov.session.get.call_count == 1

    def test_pagination_follows_cursor(self):
        prov = KalshiProvider()
        prov.session = MagicMock()
        prov.session.get.side_effect = [
            _mock_response(
                {
                    "markets": [_market_dict("KXHIGHNY-26JUL24-B81.5")],
                    "cursor": "next-page",
                }
            ),
            _mock_response(
                {"markets": [_market_dict("KXHIGHNY-26JUL24-B83.5")], "cursor": None}
            ),
        ]
        mkts = prov.fetch_market_ladder("KXHIGHNY")
        assert len(mkts) == 2
        assert prov.session.get.call_count == 2
        # cursor forwarded on page 2
        assert prov.session.get.call_args_list[1][1]["params"]["cursor"] == "next-page"

    def test_error_returns_empty_list(self):
        prov = KalshiProvider()
        prov.session = MagicMock()
        prov.session.get.side_effect = Exception("boom")
        assert prov.fetch_market_ladder("KXHIGHNY") == []


# ---------------------------------------------------------------------------
# Data-CSV writer (Dashboard) — schema + backward compatibility
# ---------------------------------------------------------------------------


@pytest.fixture
def dash(tmp_path, monkeypatch):
    """A real Dashboard writing under an isolated tmp cwd."""
    monkeypatch.chdir(tmp_path)
    from src.visualization.dashboard import Dashboard

    return Dashboard()


def _read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestDataCsvSchema:
    def test_header_is_extended_schema(self, dash):
        from src.visualization.dashboard import DATA_CSV_HEADER

        with open(dash.data_log_path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == DATA_CSV_HEADER
        # Legacy prefix preserved for existing consumers
        assert header[:5] == ["Timestamp", "Symbol", "Price", "Type", "Status"]
        assert "Bid" in header and "Ask" in header
        assert "Last" in header and "Volume" in header and "Depth" in header

    def test_market_data_row_records_quotes(self, dash):
        dash.update_price(
            "KXHIGHNY-26JUL24-B81.5 (Market)",
            0.72,
            bid=0.72,
            ask=0.74,
            no_bid=0.26,
            no_ask=0.28,
            last=0.73,
            volume=1500,
        )
        rows = _read_rows(dash.data_log_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["Type"] == "MARKET_DATA"
        assert r["Status"] == "REAL"
        assert float(r["Bid"]) == 0.72
        assert float(r["Ask"]) == 0.74
        assert float(r["NoBid"]) == 0.26
        assert float(r["NoAsk"]) == 0.28
        assert float(r["Last"]) == 0.73
        assert float(r["Volume"]) == 1500
        assert r["Depth"] == ""

    def test_non_market_rows_leave_quote_columns_blank(self, dash):
        dash.update_price("KXHIGHNY (F)", 81.0)
        r = _read_rows(dash.data_log_path)[0]
        assert r["Type"] == "MARKET_DATA"
        assert r["Bid"] == "" and r["Ask"] == "" and r["Volume"] == ""

    def test_depth_row_json_top3_both_sides(self, dash):
        levels = {
            "yes": [(0.24, 31.0), (0.23, 150.0), (0.10, 5.0), (0.05, 2.0)],
            "no": [(0.72, 40.0), (0.70, 10.0)],
        }
        dash.record_depth("KXHIGHCHI-26JUL24-B78.5", levels, last_price=0.24)
        r = _read_rows(dash.data_log_path)[0]
        assert r["Type"] == "DEPTH"
        depth = json.loads(r["Depth"])
        assert depth["yes"] == [[0.24, 31.0], [0.23, 150.0], [0.10, 5.0]]  # top-3
        assert depth["no"] == [[0.72, 40.0], [0.70, 10.0]]
        assert float(r["Bid"]) == 0.24  # best yes bid
        assert float(r["NoBid"]) == 0.72  # best no bid
        assert float(r["Ask"]) == pytest.approx(0.28)  # 1 - best no bid
        assert float(r["NoAsk"]) == pytest.approx(0.76)  # 1 - best yes bid
        assert float(r["Price"]) == 0.24

    def test_signal_rows_padded_to_header_width(self, dash):
        from src.visualization.dashboard import DATA_CSV_HEADER

        sig = TradeSignal(
            symbol="KXHIGHNY-26JUL24-B81.5", side="buy", quantity=1, limit_price=0.5
        )
        dash.record_signal(sig, status="EXECUTED")
        dash.update_price("X (Market)", 0.5, bid=0.5, ask=0.52)
        with open(dash.data_log_path, newline="", encoding="utf-8") as f:
            raw = list(csv.reader(f))
        # header + 2 rows, all exactly header width (pandas-safe)
        assert all(len(row) == len(DATA_CSV_HEADER) for row in raw)
        rows = _read_rows(dash.data_log_path)
        assert rows[0]["Type"] == "SIGNAL_BUY"

    def test_lab_style_pandas_read_with_bid_ask_usecols(self, dash):
        pd = pytest.importorskip("pandas")
        dash.update_price("A (Market)", 0.4, bid=0.4, ask=0.42, volume=10)
        dash.record_depth("A", {"yes": [(0.4, 1.0)], "no": [(0.58, 2.0)]})
        df = pd.read_csv(
            dash.data_log_path,
            usecols=["Timestamp", "Symbol", "Price", "Type", "Bid", "Ask"],
        )
        md = df[df["Type"] == "MARKET_DATA"]
        assert len(md) == 1
        assert float(md.iloc[0]["Bid"]) == 0.4
        # DEPTH rows are excluded by the Type filter existing consumers use
        assert "DEPTH" in df["Type"].values

    def test_legacy_header_file_still_readable_after_append(self, dash, tmp_path):
        # Mimic state_manager reading an OLD-schema file that received
        # new-width rows (e.g. mid-upgrade): named columns must still work.
        legacy = tmp_path / "legacy.csv"
        with open(legacy, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
        dash.data_log_path = str(legacy)
        dash.update_price("B (Market)", 0.6, bid=0.6, ask=0.62)
        rows = _read_rows(legacy)
        assert rows[0]["Symbol"] == "B (Market)"
        assert rows[0]["Type"] == "MARKET_DATA"


# ---------------------------------------------------------------------------
# WeatherBot feed path: full-ladder capture + hourly depth cadence
# ---------------------------------------------------------------------------


def _today_code():
    return datetime.now().strftime("%y%b%d").upper()


def _make_bot(ladder, orderbook=None):
    """WeatherBot without __init__ (avoids ML model loading), wired to mocks."""
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot.__new__(WeatherBot)
    bot.name = "Weather"
    bot.ticker_cache = {}
    bot._last_depth_snapshot = 0.0
    bot.METAR_STATIONS = ["KJFK"]  # single city keeps tests fast

    obs = MarketData(
        symbol="KJFK",
        timestamp=datetime.now(),
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={"temperature_f": 75.0, "max_temp_today_f": 80.0, "forecast": []},
    )
    bot.metar = MagicMock()
    bot.metar.fetch_latest.return_value = obs
    bot.nws = MagicMock()
    bot.nws.fetch_latest.return_value = MarketData(
        symbol="KNYC",
        timestamp=datetime.now(),
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={"forecast": []},
    )

    bot.kalshi = MagicMock()
    bot.kalshi.fetch_market_ladder.return_value = ladder
    bot.kalshi.fetch_orderbook.return_value = orderbook or {
        "yes": [(0.24, 31.0)],
        "no": [(0.70, 10.0)],
    }
    return bot


def _default_ladder():
    d = _today_code()
    return [
        _md(f"KXHIGHNY-{d}-B81.5", 0.72, 0.74, 0.26, 0.28, 0.73, 1500),
        _md(f"KXHIGHNY-{d}-B83.5", 0.15, 0.18, 0.82, 0.85, 0.16, 900),
        _md(f"KXHIGHNY-{d}-T85", 0.03, 0.05, 0.95, 0.97, 0.04, 120),
    ]


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr("src.bots.weather_bot.time.sleep", lambda s: None)


class TestWeatherLadderCapture:
    def test_all_brackets_recorded_with_bid_and_ask(self):
        ladder = _default_ladder()
        bot = _make_bot(ladder)
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)

        market_calls = [
            c
            for c in dashboard.update_price.call_args_list
            if c[0][0].endswith("(Market)")
        ]
        assert len(market_calls) == len(ladder)  # FULL ladder, not just modal
        recorded = {c[0][0].split(" ")[0] for c in market_calls}
        assert recorded == {m.symbol for m in ladder}
        for c in market_calls:
            assert c[1]["bid"] > 0
            assert c[1]["ask"] > 0
            assert "no_bid" in c[1] and "no_ask" in c[1]
            assert "last" in c[1] and "volume" in c[1]

    def test_active_market_is_highest_yes_bid(self):
        ladder = _default_ladder()
        bot = _make_bot(ladder)
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)

        # B81.5 has the highest YES bid (0.72) -> fused + valued
        expected = ladder[0].symbol
        rm.update_market_data.assert_any_call(expected, 0.72)

    def test_one_list_call_per_city_no_per_market_quote_calls(self):
        bot = _make_bot(_default_ladder())
        bot._last_depth_snapshot = time.time()  # depth NOT due
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)

        assert bot.kalshi.fetch_market_ladder.call_count == 1
        assert bot.kalshi.fetch_latest.call_count == 0
        assert bot.kalshi.fetch_orderbook.call_count == 0

    def test_ladder_filtered_to_tracked_dates(self):
        d = _today_code()
        stale = _md("KXHIGHNY-99DEC31-B50.5", 0.5, 0.52, 0.48, 0.5, 0.51, 10)
        ladder = _default_ladder() + [stale]
        bot = _make_bot(ladder)

        tracked = bot._ladder_for_city("KXHIGHNY")
        assert all(d in m.symbol for m in tracked)
        assert stale not in tracked

    def test_fallback_to_legacy_resolver_when_ladder_empty(self):
        bot = _make_bot([])
        bot.kalshi.fetch_latest.return_value = _md(
            "KXHIGHNY-26JUL24-B81.5", 0.72, 0.74, 0.26, 0.28, 0.73, 1500
        )
        bot._resolve_smart_ticker = MagicMock(return_value="KXHIGHNY-26JUL24-B81.5")
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)

        bot._resolve_smart_ticker.assert_called_once()
        bot.kalshi.fetch_latest.assert_called_once_with("KXHIGHNY-26JUL24-B81.5")


class TestHourlyDepthCadence:
    def test_first_tick_snapshots_then_holds_for_an_hour(self):
        ladder = _default_ladder()
        bot = _make_bot(ladder)
        rm, dashboard = MagicMock(), MagicMock()

        # Tick 1: baseline snapshot (last=0 -> due)
        bot.tick(rm, dashboard)
        assert bot.kalshi.fetch_orderbook.call_count == len(ladder)
        assert dashboard.record_depth.call_count == len(ladder)
        assert bot._last_depth_snapshot > 0

        # Tick 2 (moments later): NOT due -> zero additional orderbook calls
        bot.tick(rm, dashboard)
        assert bot.kalshi.fetch_orderbook.call_count == len(ladder)
        assert dashboard.record_depth.call_count == len(ladder)

        # Simulate >1h elapsed -> due again
        bot._last_depth_snapshot = time.time() - 3601
        bot.tick(rm, dashboard)
        assert bot.kalshi.fetch_orderbook.call_count == 2 * len(ladder)
        assert dashboard.record_depth.call_count == 2 * len(ladder)

    def test_depth_snapshot_records_via_dashboard(self):
        ladder = _default_ladder()
        bot = _make_bot(ladder)
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)

        sym, levels = dashboard.record_depth.call_args_list[0][0][:2]
        assert sym == ladder[0].symbol
        assert levels == {"yes": [(0.24, 31.0)], "no": [(0.70, 10.0)]}
        assert (
            dashboard.record_depth.call_args_list[0][1]["last_price"] == ladder[0].price
        )

    def test_depth_snapshot_capped_per_city(self):
        d = _today_code()
        big_ladder = [
            _md(f"KXHIGHNY-{d}-B{50 + i}.5", 0.1, 0.12, 0.88, 0.9, 0.11, 5)
            for i in range(40)
        ]
        bot = _make_bot(big_ladder)
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)

        from src.bots.weather_bot import WeatherBot

        assert (
            bot.kalshi.fetch_orderbook.call_count
            == WeatherBot.MAX_DEPTH_MARKETS_PER_CITY
        )

    def test_depth_errors_do_not_break_tick(self):
        bot = _make_bot(_default_ladder())
        bot.kalshi.fetch_orderbook.side_effect = Exception("rate limited")
        rm, dashboard = MagicMock(), MagicMock()

        bot.tick(rm, dashboard)  # must not raise

        assert dashboard.record_depth.call_count == 0
        # Quotes were still recorded despite depth failures
        market_calls = [
            c
            for c in dashboard.update_price.call_args_list
            if c[0][0].endswith("(Market)")
        ]
        assert len(market_calls) == 3
