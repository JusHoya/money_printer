"""Harvest-CSV bracket semantics (PRD FR-0.7 + FR-1.1, Phase 1 exits 2 & 6).

The defect this file guards against: the VM harvester recorded prices but not
the fields that say what a contract *means*, so replaying a harvest for Phase 2
calibration or the go/no-go EV report would need a post-hoc metadata re-fetch --
which is precisely how the old system ended up with inverted B/T weather
semantics.

Coverage:
1.  ``Dashboard.update_price`` writes ``StrikeType`` / ``FloorStrike`` /
    ``CapStrike`` for all three contract types, EMPTY (never ``0``) where the
    market genuinely has no such strike;
2.  a full round trip -- real writer -> real reader -> ``parse_bracket_spec``
    -> ``settles_yes`` -- proving a harvested ladder is settleable offline;
3.  an old-format (narrow) CSV still parses and is REPORTED as unusable for
    brackets, not silently mis-parsed;
4.  no default station list reachable from a strategy input path names the
    non-settlement airports KJFK/KORD;
5.  depth rows carry the same semantics as quote rows.

Run: $env:PYTHONPATH="."; python -m pytest tests/test_harvest_bracket_columns.py -v
"""

import ast
import csv
import io
import json
import os
import re
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.backtest.data_loader import (  # noqa: E402
    HARVEST_BRACKET_COLUMNS,
    HarvestBracketStats,
    bracket_extra_from_row,
    load_harvest_csv,
    spec_for_harvest_row,
    strip_market_suffix,
)
from src.core.bracket_payoff import (  # noqa: E402
    BracketSpecError,
    parse_bracket_spec,
    settles_yes,
)
from src.visualization.dashboard import DATA_CSV_HEADER, Dashboard  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The non-settlement airports. Neither settles any KXHIGH market.
AIRPORT_PROXIES = ("KJFK", "KORD")
_PROXY_RE = re.compile("|".join(AIRPORT_PROXIES))


# ---------------------------------------------------------------------------
# Fixtures: a real Dashboard writing a real CSV under a tmp cwd
# ---------------------------------------------------------------------------


@pytest.fixture
def dash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Dashboard()


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# A real Kalshi ladder shape (fields as KalshiProvider emits them, verified
# against tests/fixtures/kxhighny_markets.json).
LADDER = [
    # ticker, price, bid, ask, strike_type, floor, cap, yes_sub_title
    ("KXHIGHNY-26JUL25-T80", 0.08, 0.07, 0.09, "less", None, 80.0, "79° or below"),
    ("KXHIGHNY-26JUL25-B84.5", 0.31, 0.30, 0.32, "between", 84.0, 85.0, "84° to 85°"),
    ("KXHIGHNY-26JUL25-B86.5", 0.44, 0.43, 0.45, "between", 86.0, 87.0, "86° to 87°"),
    ("KXHIGHNY-26JUL25-T87", 0.12, 0.11, 0.13, "greater", 87.0, None, "88° or above"),
]


def _write_ladder(dashboard):
    """Harvest the synthetic ladder through the REAL update_price path."""
    for ticker, price, bid, ask, st, floor, cap, sub in LADDER:
        dashboard.update_price(
            f"{ticker} (Market)",
            price,
            bid=bid,
            ask=ask,
            no_bid=round(1 - ask, 2),
            no_ask=round(1 - bid, 2),
            last=price,
            volume=250,
            strike_type=st,
            floor_strike=floor,
            cap_strike=cap,
            yes_sub_title=sub,
        )


# ===========================================================================
# 1. update_price writes the bracket columns
# ===========================================================================


class TestUpdatePriceWritesBracketColumns:
    def test_header_carries_the_three_new_columns_at_the_tail(self, dash):
        with open(dash.data_log_path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == DATA_CSV_HEADER
        # Append-only: the legacy 12-column prefix is untouched, so every
        # existing by-name consumer keeps working.
        assert header[:12] == [
            "Timestamp",
            "Symbol",
            "Price",
            "Type",
            "Status",
            "Bid",
            "Ask",
            "NoBid",
            "NoAsk",
            "Last",
            "Volume",
            "Depth",
        ]
        assert header[12:] == list(HARVEST_BRACKET_COLUMNS)

    def test_between_market_records_both_strikes(self, dash):
        _write_ladder(dash)
        row = [r for r in _rows(dash.data_log_path) if "B86.5" in r["Symbol"]][0]
        assert row["StrikeType"] == "between"
        assert float(row["FloorStrike"]) == 86.0
        assert float(row["CapStrike"]) == 87.0

    def test_greater_market_has_empty_cap_not_zero(self, dash):
        _write_ladder(dash)
        row = [
            r for r in _rows(dash.data_log_path) if r["Symbol"].endswith("T87 (Market)")
        ][0]
        assert row["StrikeType"] == "greater"
        assert float(row["FloorStrike"]) == 87.0
        # A 'greater' market genuinely has no cap. "" replays as absent;
        # 0 would replay as a 0F strike and settle every contract wrong.
        assert row["CapStrike"] == ""
        assert row["CapStrike"] != "0" and row["CapStrike"] != "0.0"

    def test_less_market_has_empty_floor_not_zero(self, dash):
        _write_ladder(dash)
        row = [
            r for r in _rows(dash.data_log_path) if r["Symbol"].endswith("T80 (Market)")
        ][0]
        assert row["StrikeType"] == "less"
        assert row["FloorStrike"] == ""
        assert row["FloorStrike"] != "0" and row["FloorStrike"] != "0.0"
        assert float(row["CapStrike"]) == 80.0

    def test_non_market_rows_leave_bracket_columns_blank(self, dash):
        dash.update_price("KXHIGHNY (F)", 81.0)  # a temperature row
        row = _rows(dash.data_log_path)[0]
        assert row["StrikeType"] == ""
        assert row["FloorStrike"] == "" and row["CapStrike"] == ""

    def test_every_row_is_exactly_header_width(self, dash):
        from src.core.interfaces import TradeSignal

        _write_ladder(dash)
        dash.record_depth("KXHIGHNY-26JUL25-B86.5", {"yes": [(0.43, 10.0)], "no": []})
        dash.record_signal(
            TradeSignal(
                symbol="KXHIGHNY-26JUL25-B86.5",
                side="buy",
                quantity=1,
                limit_price=0.44,
            )
        )
        with open(dash.data_log_path, newline="", encoding="utf-8") as f:
            raw = list(csv.reader(f))
        assert raw, "no rows written"
        assert all(len(r) == len(DATA_CSV_HEADER) for r in raw), [
            (len(r), r[:4]) for r in raw if len(r) != len(DATA_CSV_HEADER)
        ]

    def test_unparseable_strike_is_blank_not_guessed(self, dash):
        dash.update_price(
            "KXHIGHNY-26JUL25-B86.5 (Market)",
            0.44,
            strike_type="between",
            floor_strike="not-a-number",
            cap_strike=87,
        )
        row = _rows(dash.data_log_path)[0]
        assert row["FloorStrike"] == ""  # fails loud on replay, never 0
        assert float(row["CapStrike"]) == 87.0


# ===========================================================================
# 2. Round trip: writer -> reader -> bracket_payoff (offline settleability)
# ===========================================================================


# Known daily high -> the ticker that must settle YES. 86F: inside B86.5
# (86-87), below T87's 88 threshold, above T80's 79 threshold.
ROUND_TRIP_CASES = [
    (79.0, "KXHIGHNY-26JUL25-T80"),  # 'less' cap=80 pays at <= 79
    (84.0, "KXHIGHNY-26JUL25-B84.5"),
    (86.0, "KXHIGHNY-26JUL25-B86.5"),
    (88.0, "KXHIGHNY-26JUL25-T87"),  # 'greater' floor=87 pays at >= 88
]


class TestHarvestRoundTripSettlement:
    """The load-bearing test: a harvest is settleable with no re-fetch."""

    @pytest.fixture
    def replayed(self, dash):
        _write_ladder(dash)
        markets, stats = load_harvest_csv(dash.data_log_path)
        return markets, stats, dash.data_log_path

    def test_reader_recovers_every_market_row(self, replayed):
        markets, stats, _ = replayed
        assert len(markets) == len(LADDER)
        assert {m.symbol for m in markets} == {t[0] for t in LADDER}
        # " (Market)" is a dashboard display suffix, not part of the ticker.
        assert all(" (Market)" not in m.symbol for m in markets)
        assert stats.weather_rows == len(LADDER)
        assert stats.usable == len(LADDER)
        assert stats.unusable == 0

    def test_bracket_fields_survive_the_round_trip(self, replayed):
        markets, _, _ = replayed
        by_sym = {m.symbol: m for m in markets}
        assert by_sym["KXHIGHNY-26JUL25-B86.5"].extra["strike_type"] == "between"
        assert by_sym["KXHIGHNY-26JUL25-B86.5"].extra["floor_strike"] == 86.0
        assert by_sym["KXHIGHNY-26JUL25-B86.5"].extra["cap_strike"] == 87.0
        # Absent strikes come back as None, NOT 0.0
        assert by_sym["KXHIGHNY-26JUL25-T87"].extra["cap_strike"] is None
        assert by_sym["KXHIGHNY-26JUL25-T80"].extra["floor_strike"] is None

    @pytest.mark.parametrize("high,winner", ROUND_TRIP_CASES)
    def test_settlement_from_replayed_rows_only(self, replayed, high, winner):
        """Settle the whole ladder from the CSV alone, for a known high.

        Exactly one bracket pays, and it is the right one -- across all three
        strike_type values, including the off-by-one on the one-sided types.
        """
        markets, _, _ = replayed
        winners = []
        for md in markets:
            spec = parse_bracket_spec(md.symbol, md.extra)  # no re-fetch
            if settles_yes(spec, high):
                winners.append(md.symbol)
        assert winners == [winner], f"high={high}F settled {winners}"

    def test_prices_and_quotes_survive_too(self, replayed):
        markets, _, _ = replayed
        md = {m.symbol: m for m in markets}["KXHIGHNY-26JUL25-B86.5"]
        assert md.bid == 0.43
        assert md.ask == 0.45
        assert md.extra["no_bid"] == pytest.approx(0.55)
        assert md.volume == 250

    def test_round_trip_would_fail_if_columns_were_dropped(self, dash):
        """Mutation check: the round trip must be able to fail."""
        # Same ladder, harvested WITHOUT bracket kwargs (the old writer).
        for ticker, price, bid, ask, *_ in LADDER:
            dash.update_price(f"{ticker} (Market)", price, bid=bid, ask=ask)
        markets, stats = load_harvest_csv(dash.data_log_path)
        assert stats.unusable == len(LADDER)
        for md in markets:
            with pytest.raises(BracketSpecError):
                parse_bracket_spec(md.symbol, md.extra)


# ===========================================================================
# 3. Backward compatibility with pre-bracket (narrow) harvests
# ===========================================================================


LEGACY_HEADER = [
    "Timestamp",
    "Symbol",
    "Price",
    "Type",
    "Status",
    "Bid",
    "Ask",
    "NoBid",
    "NoAsk",
    "Last",
    "Volume",
    "Depth",
]


@pytest.fixture
def legacy_csv(tmp_path):
    """A Phase-0-format harvest: 12 columns, no bracket columns."""
    path = tmp_path / "data_20260720_010101.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(LEGACY_HEADER)
        w.writerow(
            [
                "2026-07-20T12:00:00",
                "KXHIGHNY-26JUL20-B86.5 (Market)",
                0.44,
                "MARKET_DATA",
                "REAL",
                0.43,
                0.45,
                0.55,
                0.57,
                0.44,
                250,
                "",
            ]
        )
    return path


class TestLegacyHarvestBackwardCompatibility:
    def test_old_format_row_parses_without_raising(self, legacy_csv):
        markets, stats = load_harvest_csv(legacy_csv)
        assert len(markets) == 1
        assert markets[0].symbol == "KXHIGHNY-26JUL20-B86.5"
        assert markets[0].bid == 0.43  # quotes still usable

    def test_old_format_row_is_reported_unusable_not_mis_parsed(self, legacy_csv):
        markets, stats = load_harvest_csv(legacy_csv)
        assert stats.unusable == 1
        assert stats.usable == 0
        assert stats.files_missing_columns == [legacy_csv.name]
        report = "\n".join(stats.report_lines())
        assert "1 rows unusable" in report
        assert "harvested before bracket columns existed" in report

    def test_old_format_row_never_gets_invented_semantics(self, legacy_csv):
        """No fallback may reconstruct direction from the 'B' in the ticker."""
        markets, _ = load_harvest_csv(legacy_csv)
        md = markets[0]
        assert md.extra["strike_type"] is None
        assert md.extra["floor_strike"] is None
        assert md.extra["cap_strike"] is None
        assert "strike" not in md.extra
        with pytest.raises(BracketSpecError):
            parse_bracket_spec(md.symbol, md.extra)

    def test_dictreader_of_new_file_is_readable_by_old_style_consumers(self, dash):
        """Existing by-name readers (state_manager, /api/logs/data) unaffected."""
        _write_ladder(dash)
        rows = _rows(dash.data_log_path)
        for r in rows:
            for col in LEGACY_HEADER:
                assert col in r

    def test_pandas_usecols_of_legacy_columns_still_works(self, dash):
        pd = pytest.importorskip("pandas")
        _write_ladder(dash)
        df = pd.read_csv(
            dash.data_log_path,
            usecols=["Timestamp", "Symbol", "Price", "Type", "Bid", "Ask"],
        )
        assert len(df[df["Type"] == "MARKET_DATA"]) == len(LADDER)

    def test_mixed_widths_tally_across_files(self, dash, legacy_csv):
        _write_ladder(dash)
        stats = HarvestBracketStats()
        _, stats = load_harvest_csv(legacy_csv, stats=stats)
        _, stats = load_harvest_csv(dash.data_log_path, stats=stats)
        assert stats.files == 2
        assert stats.files_missing_columns == [legacy_csv.name]
        assert stats.usable == len(LADDER)
        assert stats.unusable == 1


# ===========================================================================
# 4. Guard: no airport proxy in any default station list
# ===========================================================================


# Files whose default station data feeds a strategy input path. simulate.py
# and mock_providers drive `scripts/simulate.py --bot weather`, which runs a
# real strategy; weather_bot is the live feed.
STATION_DEFAULT_SOURCES = (
    os.path.join(REPO_ROOT, "src", "data", "mock_providers.py"),
    os.path.join(REPO_ROOT, "scripts", "simulate.py"),
    os.path.join(REPO_ROOT, "src", "bots", "weather_bot.py"),
)


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr):
            const = body[0].value
            if isinstance(const, ast.Constant) and isinstance(const.value, str):
                ids.add(id(const))
    return ids


def _executable_proxy_hits(path):
    """(file, line, snippet) for KJFK/KORD in evaluable source.

    Comments never reach the AST and docstrings are skipped, so a comment
    documenting the defect does not mask a real regression -- only data the
    interpreter can hand to a provider lands here.
    """
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source, filename=path)
    docstrings = _docstring_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and _PROXY_RE.search(node.value.upper()):
                hits.append((path, node.lineno, repr(node.value)[:80]))
        elif isinstance(node, ast.Name) and _PROXY_RE.search(node.id.upper()):
            hits.append((path, node.lineno, node.id))
        elif isinstance(node, ast.keyword) and node.arg:
            if _PROXY_RE.search(node.arg.upper()):
                hits.append((path, node.lineno, node.arg))
    return hits


class TestNoAirportProxyInDefaultStationLists:
    @pytest.mark.parametrize("path", STATION_DEFAULT_SOURCES, ids=os.path.basename)
    def test_no_proxy_in_executable_source(self, path):
        hits = _executable_proxy_hits(path)
        assert not hits, (
            "Non-settlement airport (KJFK/KORD) reachable from a strategy "
            "input path (PRD FR-1.4, Phase 1 exit criterion 6):\n"
            + "\n".join(
                f"  {os.path.relpath(f, REPO_ROOT)}:{line}  ->  {snippet}"
                for f, line, snippet in hits
            )
        )

    def test_mock_provider_default_stations_are_settlement_stations(self):
        from src.bots.weather_bot import SETTLEMENT_STATIONS
        from src.data.mock_providers import _DEFAULT_STATIONS, MockNWSProvider

        assert not set(_DEFAULT_STATIONS) & set(AIRPORT_PROXIES), (
            f"src/data/mock_providers.py::_DEFAULT_STATIONS still keys on "
            f"{sorted(set(_DEFAULT_STATIONS) & set(AIRPORT_PROXIES))}"
        )
        # Every tracked city's settlement station is simulatable.
        assert set(SETTLEMENT_STATIONS) <= set(_DEFAULT_STATIONS)
        # A bare MockNWSProvider() feeds `simulate.py --bot weather`.
        assert MockNWSProvider().stations == ["KNYC"]

    def test_simulate_live_station_default_is_the_settlement_station(self):
        """scripts/simulate.py must not fall back to an airport proxy."""
        import scripts.simulate as sim

        src = io.open(sim.__file__, encoding="utf-8").read()
        assert 'NWS_STATION_ID", "KJFK"' not in src
        assert 'CITY_CONFIG["NY"].settlement_station' in src
        assert sim.CITY_CONFIG["NY"].settlement_station == "KNYC"

    def test_guard_catches_a_reintroduction(self, tmp_path):
        """Mutation check: this guard must be able to fail."""
        bad = tmp_path / "regressed.py"
        bad.write_text('_STATIONS = {"KJFK": 1, "KORD": 2}\n', encoding="utf-8")
        assert len(_executable_proxy_hits(str(bad))) == 2


# ===========================================================================
# 5. Depth rows carry the same semantics
# ===========================================================================


class TestDepthRowBracketColumns:
    def test_depth_row_records_bracket_columns(self, dash):
        dash.record_depth(
            "KXHIGHNY-26JUL25-B86.5",
            {"yes": [(0.43, 31.0), (0.42, 10.0)], "no": [(0.55, 40.0)]},
            last_price=0.44,
            strike_type="between",
            floor_strike=86,
            cap_strike=87,
        )
        row = _rows(dash.data_log_path)[0]
        assert row["Type"] == "DEPTH"
        assert row["StrikeType"] == "between"
        assert float(row["FloorStrike"]) == 86.0
        assert float(row["CapStrike"]) == 87.0
        assert json.loads(row["Depth"])["yes"][0] == [0.43, 31.0]

    def test_depth_row_greater_market_has_empty_cap(self, dash):
        dash.record_depth(
            "KXHIGHNY-26JUL25-T87",
            {"yes": [(0.11, 5.0)], "no": [(0.87, 3.0)]},
            last_price=0.12,
            strike_type="greater",
            floor_strike=87,
            cap_strike=None,
        )
        row = _rows(dash.data_log_path)[0]
        assert row["CapStrike"] == ""

    def test_depth_row_is_settleable_offline(self, dash):
        dash.record_depth(
            "KXHIGHNY-26JUL25-T80",
            {"yes": [(0.07, 5.0)], "no": [(0.91, 3.0)]},
            last_price=0.08,
            strike_type="less",
            cap_strike=80,
        )
        row = _rows(dash.data_log_path)[0]
        spec = parse_bracket_spec(
            strip_market_suffix(row["Symbol"]), bracket_extra_from_row(row)
        )
        assert settles_yes(spec, 79.0) is True
        assert settles_yes(spec, 80.0) is False  # 'less' cap=80 means <= 79

    def test_depth_rows_without_bracket_kwargs_are_blank_not_zero(self, dash):
        dash.record_depth("KXHIGHNY-26JUL25-B86.5", {"yes": [(0.43, 1.0)], "no": []})
        row = _rows(dash.data_log_path)[0]
        assert row["StrikeType"] == ""
        assert row["FloorStrike"] == "" and row["CapStrike"] == ""

    def test_weather_bot_passes_bracket_fields_to_record_depth(self):
        """The producer side: _snapshot_depth must forward the API fields."""
        from datetime import datetime
        from unittest.mock import MagicMock

        from src.bots.weather_bot import WeatherBot
        from src.core.interfaces import MarketData

        bot = WeatherBot.__new__(WeatherBot)
        bot.kalshi = MagicMock()
        bot.kalshi.fetch_orderbook.return_value = {"yes": [(0.43, 5.0)], "no": []}
        ladder = [
            MarketData(
                symbol="KXHIGHNY-26JUL25-T87",
                timestamp=datetime.now(),
                price=0.12,
                volume=10,
                bid=0.11,
                ask=0.13,
                extra={
                    "strike_type": "greater",
                    "floor_strike": 87.0,
                    "cap_strike": None,
                },
            )
        ]
        dashboard = MagicMock()
        bot._snapshot_depth(ladder, dashboard)

        kwargs = dashboard.record_depth.call_args[1]
        assert kwargs["strike_type"] == "greater"
        assert kwargs["floor_strike"] == 87.0
        assert kwargs["cap_strike"] is None


# ===========================================================================
# 6. Kalshi WebSocket feed is FR-1.1 compliant (or fails loud)
# ===========================================================================


def _bare_ws():
    """A KalshiWebSocket with state but no network (no __init__ side effects)."""
    import threading
    from collections import deque

    from src.data.kalshi_ws import KalshiWebSocket, _OrderbookState

    ws = KalshiWebSocket.__new__(KalshiWebSocket)
    ws._books = {}
    ws._books_lock = threading.Lock()
    ws._trades = {}
    ws._trades_lock = threading.Lock()
    ws._brackets = {}
    ws._brackets_lock = threading.Lock()
    ws._bracket_warned = set()
    book = _OrderbookState()
    book.update(yes_bid=0.43, yes_ask=0.45, no_bid=0.55, no_ask=0.57, last_price=0.44)
    ws._books["KXHIGHNY-26JUL25-B86.5"] = book
    ws._trades["KXHIGHNY-26JUL25-B86.5"] = deque(maxlen=10)
    return ws


class TestKalshiWebSocketBracketCompliance:
    """The WS quote channels carry no strike metadata (AsyncAPI, 2026-07-25);
    market_lifecycle_v2 does. The provider must consume that channel and, when
    semantics are still unknown, fail loud rather than emit a bracket-blind
    MarketData."""

    def test_subscribes_to_the_lifecycle_channel(self):
        from unittest.mock import MagicMock

        from src.data.kalshi_ws import KalshiWebSocket

        ws = KalshiWebSocket.__new__(KalshiWebSocket)
        sent = []
        ws._ws_send = lambda msg: sent.append(msg)
        ws._next_msg_id = MagicMock(side_effect=range(1, 10))
        ws._send_subscribe(["KXHIGHNY-26JUL25-B86.5"])
        channels = [m["params"]["channels"][0] for m in sent]
        assert "market_lifecycle_v2" in channels
        assert "orderbook_delta" in channels and "trade" in channels

    def test_lifecycle_created_message_seeds_bracket_fields(self):
        ws = _bare_ws()
        ws._on_message(
            None,
            json.dumps(
                {
                    "type": "market_lifecycle_v2",
                    "msg": {
                        "market_ticker": "KXHIGHNY-26JUL25-B86.5",
                        "event_type": "created",
                        "additional_metadata": {
                            "yes_sub_title": "86° to 87°",
                            "strike_type": "between",
                            "floor_strike": 86,
                            "cap_strike": 87,
                        },
                    },
                }
            ),
        )
        md = ws.fetch_latest("KXHIGHNY-26JUL25-B86.5")
        spec = parse_bracket_spec(md.symbol, md.extra)
        assert spec.strike_type == "between"
        assert settles_yes(spec, 86.0) and not settles_yes(spec, 88.0)

    def test_metadata_updated_message_top_level_fields(self):
        ws = _bare_ws()
        ws._handle_market_lifecycle(
            {
                "msg": {
                    "market_ticker": "KXHIGHNY-26JUL25-B86.5",
                    "event_type": "metadata_updated",
                    "strike_type": "greater",
                    "floor_strike": 87,
                }
            }
        )
        md = ws.fetch_latest("KXHIGHNY-26JUL25-B86.5")
        spec = parse_bracket_spec(md.symbol, md.extra)
        assert spec.strike_type == "greater"
        assert settles_yes(spec, 88.0) and not settles_yes(spec, 87.0)

    def test_unknown_semantics_fail_loud_not_silent(self, caplog):
        import logging

        ws = _bare_ws()
        with caplog.at_level(logging.ERROR, logger="kalshi_ws"):
            md = ws.fetch_latest("KXHIGHNY-26JUL25-B86.5")
        # Keys are present and explicitly None -- never absent, never guessed.
        assert set(md.extra) >= {"strike_type", "floor_strike", "cap_strike"}
        assert md.extra["strike_type"] is None
        with pytest.raises(BracketSpecError):
            parse_bracket_spec(md.symbol, md.extra)
        assert any("no bracket semantics known" in r.message for r in caplog.records)

    def test_rest_seed_closes_the_pre_connect_gap(self):
        """A market opened before we connected emits no lifecycle message."""
        ws = _bare_ws()
        ws.seed_bracket_metadata(
            "KXHIGHNY-26JUL25-B86.5",
            {"strike_type": "between", "floor_strike": 86.0, "cap_strike": 87.0},
        )
        md = ws.fetch_latest("KXHIGHNY-26JUL25-B86.5")
        assert parse_bracket_spec(md.symbol, md.extra).cap_strike == 87.0


# ===========================================================================
# 7. Reader-side helpers used by scripts/lab.py
# ===========================================================================


class TestReaderHelpers:
    def test_blank_cells_become_none_not_zero(self):
        extra = bracket_extra_from_row(
            {"StrikeType": "greater", "FloorStrike": "87.0", "CapStrike": ""}
        )
        assert extra == {
            "strike_type": "greater",
            "floor_strike": 87.0,
            "cap_strike": None,
        }

    def test_missing_columns_become_none(self):
        assert bracket_extra_from_row({"Symbol": "X"}) == {
            "strike_type": None,
            "floor_strike": None,
            "cap_strike": None,
        }

    def test_spec_for_row_tallies_failures_at_the_call_site(self):
        stats = HarvestBracketStats()
        good = {
            "Symbol": "KXHIGHNY-26JUL25-B86.5 (Market)",
            "StrikeType": "between",
            "FloorStrike": 86,
            "CapStrike": 87,
        }
        bad = {"Symbol": "KXHIGHNY-26JUL25-B86.5 (Market)"}
        assert spec_for_harvest_row(good, stats) is not None
        assert spec_for_harvest_row(bad, stats) is None
        assert (stats.usable, stats.unusable) == (1, 1)
        assert stats.reasons  # the reason is recorded, not swallowed

    def test_lab_loads_a_mixed_width_log_dir(self, tmp_path, monkeypatch, legacy_csv):
        """scripts/lab.py must survive both harvest widths and report both."""
        import shutil

        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir()
        shutil.copy(legacy_csv, logs / legacy_csv.name)

        d = Dashboard()  # writes logs/data_<ts>.csv in the new format
        _write_ladder(d)

        import importlib

        lab_mod = importlib.import_module("scripts.lab")
        lab = lab_mod.Lab()
        assert lab.bracket_stats.usable == len(LADDER)
        assert lab.bracket_stats.unusable == 1
        assert legacy_csv.name in lab.bracket_stats.files_missing_columns
        # And the MarketData it hands strategies carries the semantics.
        rows = [r for r in lab.data if "B86.5" in str(r["Symbol"])]
        md = lab._market_data(rows[-1], "audit")
        assert md.extra["strike_type"] == "between"
