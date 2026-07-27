"""Tests for the Phase 2 ladder-history backfill (src.data.kalshi_history).

Everything here runs OFFLINE against recorded API fixtures in
``tests/fixtures/ladders/`` -- the same JSON the live endpoints returned on
2026-07-26/27. The point of the fixtures is that the parsing traps this module
exists to avoid (the ``_dollars`` suffix, the empty-book sentinels, the
never-coerce-a-missing-price rule) are pinned against real payloads rather
than against hand-written ones that could be wrong in the same direction as
the code.

Run only this file (the machine cannot take the full suite)::

    $env:PYTHONPATH = "."
    python -m pytest tests/test_kalshi_history.py -v
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from src.core.bracket_payoff import parse_bracket_spec, settles_yes
from src.data.kalshi_history import (
    LADDER_COLUMNS,
    KalshiHistoryClient,
    RequestRecord,
    _candle_dollars,
    _optional_float,
    build_day_rows,
    date_range,
    event_ticker_for,
    is_quoted,
    load_cli_truth,
    load_ladders,
    no_side_from_yes,
    write_day_csv,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ladders"


def _load(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def markets_payload() -> dict:
    return _load("kxhighny_26jul17_markets.json")


@pytest.fixture(scope="module")
def candles_b85() -> dict:
    return _load("kxhighny_26jul17_b85_5_candles.json")


@pytest.fixture(scope="module")
def candles_t90() -> dict:
    return _load("kxhighny_26jul17_t90_candles.json")


@pytest.fixture(scope="module")
def fee_table() -> dict:
    return _load("kalshi_fee_table_2026_07_07.json")


# ----------------------------------------------------------------------
# Parsing primitives
# ----------------------------------------------------------------------


def test_event_ticker_format():
    assert event_ticker_for("KXHIGHNY", dt.date(2026, 7, 17)) == "KXHIGHNY-26JUL17"
    assert event_ticker_for("KXHIGHMIA", dt.date(2026, 5, 18)) == "KXHIGHMIA-26MAY18"


def test_candle_dollars_requires_the_dollars_suffix(candles_b85):
    """The naive ``c["yes_ask"]["close"]`` read returns None on the live API.

    This is the trap the module docstring calls out; pin it so nobody
    "simplifies" the accessor.
    """
    candle = candles_b85["candlesticks"][10]
    assert candle["yes_ask"].get("close") is None
    assert candle["yes_ask"].get("close_dollars") is not None
    assert _candle_dollars(candle, "yes_ask", "close") == pytest.approx(
        float(candle["yes_ask"]["close_dollars"])
    )


def test_candle_dollars_missing_node_is_none():
    assert _candle_dollars({}, "yes_bid", "close") is None
    assert _candle_dollars({"yes_bid": None}, "yes_bid", "close") is None
    assert _candle_dollars({"yes_bid": {}}, "yes_bid", "close") is None


def test_optional_float_never_invents_zero():
    for missing in (None, "", "   ", "abc", float("nan"), True, False):
        assert _optional_float(missing) is None, missing
    assert _optional_float("0.0700") == pytest.approx(0.07)
    assert _optional_float("0.0000") == 0.0  # a real, present zero survives


def test_is_quoted_treats_sentinels_as_no_quote():
    assert is_quoted(0.28, 0.30) is True
    assert is_quoted(0.0, 0.30) is False  # no bid
    assert is_quoted(0.28, 1.0) is False  # no ask
    assert is_quoted(0.0, 1.0) is False  # empty book
    assert is_quoted(None, 0.30) is False
    assert is_quoted(0.28, None) is False


def test_no_side_is_the_exact_complement():
    no_bid, no_ask = no_side_from_yes(0.28, 0.30)
    assert no_bid == pytest.approx(0.70)  # 1 - yes_ask
    assert no_ask == pytest.approx(0.72)  # 1 - yes_bid
    # A NO position bought at no_ask and a YES bought at yes_ask always cost
    # more than $1 -- the spread. Sanity: the identity never crosses the book.
    assert no_bid <= no_ask


def test_no_side_propagates_none():
    assert no_side_from_yes(None, 0.30) == (0.70, None)
    assert no_side_from_yes(0.28, None) == (None, 0.72)
    assert no_side_from_yes(None, None) == (None, None)


def test_date_range_inclusive_and_empty():
    r = date_range(dt.date(2026, 7, 1), dt.date(2026, 7, 3))
    assert r == [dt.date(2026, 7, 1), dt.date(2026, 7, 2), dt.date(2026, 7, 3)]
    assert date_range(dt.date(2026, 7, 3), dt.date(2026, 7, 1)) == []


# ----------------------------------------------------------------------
# Recorded-fixture shape (the upstream contract this module depends on)
# ----------------------------------------------------------------------


def test_recorded_ladder_is_a_complete_partition(markets_payload):
    """The six-bracket KXHIGHNY ladder tiles the temperature axis with no gap.

    If Kalshi ever changes the ladder shape, the EV report's "bracket distance
    from outcome" banding silently changes meaning -- so pin it.
    """
    markets = markets_payload["markets"]
    assert len(markets) == 6
    bands = []
    for m in markets:
        spec = parse_bracket_spec(m["ticker"], m)
        lo, hi = (
            (-math.inf, m["cap_strike"] - 1)
            if spec.strike_type == "less"
            else (m["floor_strike"] + 1, math.inf)
            if spec.strike_type == "greater"
            else (m["floor_strike"], m["cap_strike"])
        )
        bands.append((lo, hi))
    bands.sort()
    assert math.isinf(bands[0][0]) and math.isinf(bands[-1][1])
    for (_, hi), (lo, _) in zip(bands, bands[1:]):
        assert lo == hi + 1, f"gap or overlap between {hi} and {lo}"


def test_recorded_markets_carry_api_bracket_semantics(markets_payload):
    for m in markets_payload["markets"]:
        spec = parse_bracket_spec(m["ticker"], m)
        assert spec.strike_type in ("between", "greater", "less")
        assert m.get("result") in ("yes", "no")
        assert m.get("expiration_value") is not None


def test_bracket_payoff_reproduces_kalshi_result_on_the_fixture(markets_payload):
    """Every recorded market's settled outcome, recomputed from API fields."""
    matched = 0
    for m in markets_payload["markets"]:
        spec = parse_bracket_spec(m["ticker"], m)
        high = float(m["expiration_value"])
        assert settles_yes(spec, high) == (m["result"] == "yes"), m["ticker"]
        matched += 1
    assert matched == 6


def test_recorded_candles_expose_both_book_sides(candles_b85):
    cs = candles_b85["candlesticks"]
    assert len(cs) >= 24
    for c in cs:
        assert "yes_bid" in c and "yes_ask" in c
        assert "volume_fp" in c and "open_interest_fp" in c


def test_no_quote_sentinel_appears_in_real_data(candles_b85):
    """An ask of 1.0000 with a bid of 0.0100 is an empty book, not a price."""
    first = candles_b85["candlesticks"][0]
    assert _candle_dollars(first, "yes_ask", "open") == pytest.approx(1.0)
    assert (
        is_quoted(
            _candle_dollars(first, "yes_bid", "open"),
            _candle_dollars(first, "yes_ask", "open"),
        )
        is False
    )


# ----------------------------------------------------------------------
# Row assembly, end to end, with a fake client
# ----------------------------------------------------------------------


class _FakeClient:
    """Replays the recorded fixtures; asserts no network is touched."""

    def __init__(self, markets_payload, candles_by_ticker, empty=False):
        self._markets = [] if empty else markets_payload["markets"]
        self._candles = candles_by_ticker
        self.calls = []

    def fetch_event_markets(self, series, target_date):
        self.calls.append(("markets", series, target_date))
        rec = RequestRecord(
            "fake://markets", {}, 200, "2026-07-27T00:00:00Z", None, len(self._markets)
        )
        return list(self._markets), rec

    def fetch_candlesticks(
        self, series, market_ticker, start_ts, end_ts, period_interval=60
    ):
        self.calls.append(("candles", market_ticker))
        payload = self._candles.get(market_ticker, {"candlesticks": []})
        rec = RequestRecord(
            "fake://candles",
            {},
            200,
            "2026-07-27T00:00:00Z",
            None,
            len(payload["candlesticks"]),
        )
        return list(payload["candlesticks"]), [rec]


@pytest.fixture()
def fake_client(markets_payload, candles_b85, candles_t90):
    return _FakeClient(
        markets_payload,
        {
            "KXHIGHNY-26JUL17-B85.5": candles_b85,
            "KXHIGHNY-26JUL17-T90": candles_t90,
        },
    )


TRUTH = {"2026-07-17": 86.0}


def test_build_day_rows_produces_one_row_per_candle(
    fake_client, candles_b85, candles_t90
):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    expected = len(candles_b85["candlesticks"]) + len(candles_t90["candlesticks"])
    assert len(res.rows) == expected
    assert res.markets == 6
    assert res.markets_with_candles == 2
    assert res.empty is False
    assert set(res.rows[0]) >= set(LADDER_COLUMNS)


def test_build_day_rows_recomputes_settlement_and_matches_kalshi(fake_client):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    assert res.payoff_checked == 6
    assert res.payoff_matched == 6
    assert res.bracket_spec_errors == []


def test_build_day_rows_flags_truth_disagreement_without_picking_a_winner(
    fake_client,
):
    """A CLI high that contradicts Kalshi is REPORTED, not silently resolved."""
    res = build_day_rows(
        fake_client,
        "KXHIGHNY",
        "NY",
        "KNYC",
        dt.date(2026, 7, 17),
        {"2026-07-17": 88.0},
    )
    assert res.truth_checked == 6
    assert len(res.truth_disagreements) == 6
    row = res.rows[0]
    # Both readings survive on the row; neither overwrites the other.
    assert row["expiration_value"] == 86.0
    assert row["cli_high"] == 88.0
    assert row["truth_agrees"] is False
    assert row["recomputed_yes_expval"] is not None
    assert row["recomputed_yes_cli"] is not None


def test_build_day_rows_reports_missing_truth_as_missing(fake_client):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), {}
    )
    assert res.truth_checked == 0
    assert all(r["cli_high"] is None for r in res.rows)
    assert all(r["recomputed_yes_cli"] is None for r in res.rows)
    # Kalshi's own settlement still validates -- the two joins are independent.
    assert res.payoff_matched == 6


def test_empty_day_is_reported_not_skipped(markets_payload):
    client = _FakeClient(markets_payload, {}, empty=True)
    res = build_day_rows(client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 3, 1), TRUTH)
    assert res.rows == []
    assert res.empty is True
    assert res.empty_reason and "zero markets" in res.empty_reason


def test_rows_carry_the_no_side_identity(fake_client):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    checked = 0
    for r in res.rows:
        if r["yes_bid"] is None or r["yes_ask"] is None:
            continue
        assert r["no_bid"] == pytest.approx(1.0 - r["yes_ask"])
        assert r["no_ask"] == pytest.approx(1.0 - r["yes_bid"])
        checked += 1
    assert checked > 0


def test_minutes_to_close_is_monotone_decreasing(fake_client):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    per_market = {}
    for r in res.rows:
        per_market.setdefault(r["market_ticker"], []).append(r["minutes_to_close"])
    for ticker, mins in per_market.items():
        assert mins == sorted(mins, reverse=True), ticker
        assert min(mins) >= 0, ticker


# ----------------------------------------------------------------------
# Persistence + loader (the workstream-E entry point)
# ----------------------------------------------------------------------


def test_write_then_load_round_trip(fake_client, tmp_path):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    path = write_day_csv(res, tmp_path)
    assert path is not None and path.exists()
    assert path.name == "2026-07-17.csv"
    assert path.parent.name == "KXHIGHNY"

    df = load_ladders(tmp_path)
    assert len(df) == len(res.rows)
    assert list(df.columns) == list(LADDER_COLUMNS)
    assert df["yes_bid"].dtype.kind == "f"
    assert df["has_quote"].isin([True, False]).all()
    assert str(df["ts_utc"].dt.tz) == "UTC"
    assert df["yes_bid"].max() <= 1.0 and df["yes_bid"].min() >= 0.0


def test_loader_never_reads_a_missing_quote_as_zero(fake_client, tmp_path):
    """A blank cell must load as NaN, not as a free contract."""
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    res.rows[0]["yes_bid"] = None
    write_day_csv(res, tmp_path)
    df = load_ladders(tmp_path)
    blanks = df["yes_bid"].isna().sum()
    assert blanks == 1
    assert not (df["yes_bid"] == 0.0).any() or blanks == 1


def test_loader_filters(fake_client, tmp_path):
    res = build_day_rows(
        fake_client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 7, 17), TRUTH
    )
    write_day_csv(res, tmp_path)
    assert len(load_ladders(tmp_path, cities=["NY"])) == len(res.rows)
    assert len(load_ladders(tmp_path, cities=["KXHIGHNY"])) == len(res.rows)
    assert len(load_ladders(tmp_path, cities=["MIA"])) == 0
    assert len(load_ladders(tmp_path, start_date="2026-07-18")) == 0
    assert len(load_ladders(tmp_path, end_date="2026-07-16")) == 0
    quoted = load_ladders(tmp_path, quoted_only=True)
    assert len(quoted) < len(res.rows)
    assert quoted["has_quote"].all()


def test_empty_day_writes_no_file(markets_payload, tmp_path):
    client = _FakeClient(markets_payload, {}, empty=True)
    res = build_day_rows(client, "KXHIGHNY", "NY", "KNYC", dt.date(2026, 3, 1), TRUTH)
    assert write_day_csv(res, tmp_path) is None
    assert load_ladders(tmp_path).empty


def test_load_cli_truth_reads_the_phase1_file():
    truth = load_cli_truth("KNYC")
    assert truth, "Phase 1 CLI truth for KNYC should exist"
    assert truth.get("2026-07-17") == 86.0
    assert all(isinstance(v, float) for v in truth.values())


def test_load_cli_truth_missing_station_is_empty_not_fatal(tmp_path):
    assert load_cli_truth("KZZZ", truth_dir=tmp_path) == {}


# ----------------------------------------------------------------------
# Fee schedule (the EV report's other critical input)
# ----------------------------------------------------------------------


def _taker_fee_reference(
    price: float, contracts: int, multiplier: float = 1.0
) -> float:
    """The published Kalshi taker formula, transcribed from the fee schedule.

    ``fees = round up(M x 0.07 x C x P x (1-P))``, rounded up to the cent.
    ``round(..., 9)`` before the ceil removes binary-float representation
    error: ``0.07 * 100 * 0.10 * 0.90`` evaluates to ``0.6300000000000001``,
    which a bare ``ceil`` turns into $0.64 against a published $0.63.
    """
    if price <= 0 or price >= 1.0 or contracts <= 0:
        return 0.0
    raw = multiplier * 0.07 * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100, 9)) / 100.0


def test_reference_taker_formula_reproduces_every_published_row(fee_table):
    rows = fee_table["general_trading_fees_table"]
    assert len(rows) == 21
    for row in rows:
        assert _taker_fee_reference(row["price"], 1) == pytest.approx(
            row["fee_1_contract"]
        ), row
        assert _taker_fee_reference(row["price"], 100) == pytest.approx(
            row["fee_100_contracts"]
        ), row


def test_weather_series_is_not_on_the_maker_fee_list():
    """Live series metadata is the maker-fee discriminator.

    ``fee_type == "quadratic_with_maker_fees"`` marks the series that charge a
    maker fee; plain ``"quadratic"`` does not. Recorded live 2026-07-26/27.
    """
    weather = _load("series_kxhighny.json")["series"]
    gas = _load("series_kxaaagasm.json")["series"]
    assert weather["ticker"] == "KXHIGHNY"
    assert weather["fee_type"] == "quadratic"
    assert weather["fee_multiplier"] == 1
    assert gas["fee_type"] == "quadratic_with_maker_fees"


def test_maker_multiplier_default_is_zero(fee_table):
    """Published default M for the maker formula is 0 -> $0 maker fee."""
    assert fee_table["maker_multiplier_default"] == 0
    assert fee_table["taker_multiplier_default"] == 1
    assert "0.0175" in fee_table["maker_formula_verbatim"]
    assert "0.07" in fee_table["taker_formula_verbatim"]


# ----------------------------------------------------------------------
# Client plumbing (no network)
# ----------------------------------------------------------------------


def test_client_rejects_unsupported_period_interval():
    client = KalshiHistoryClient()
    with pytest.raises(ValueError, match="period_interval"):
        client.fetch_candlesticks("KXHIGHNY", "X", 0, 1, period_interval=5)


def test_client_is_anonymous_without_credentials():
    client = KalshiHistoryClient()
    headers = client.provider._get_authenticated_headers("GET", "/markets")
    assert client.provider.anonymous is True
    assert "KALSHI-ACCESS-KEY" not in headers
