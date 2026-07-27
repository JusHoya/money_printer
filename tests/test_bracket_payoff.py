"""Golden-table tests for the shared bracket payoff module (PRD FR-1.2).

This file is Phase 1 exit criterion 1. It pins Kalshi's daily-high settlement
semantics to a table of cases live-probed from the production API on
2026-07-25, sweeps the boundary temperatures around every strike, and keeps a
mutation gate proving the table would catch a reintroduction of the legacy
ticker-suffix parser that the 2026-07-24 review found had inverted 372 of 472
historical weather rows.

Live provenance (anonymous read, no auth required)::

    GET https://api.elections.kalshi.com/trade-api/v2/markets
        ?series_ticker=KXHIGH{NY,CHI,LAX,MIA,DEN}&status=open&limit=60

    KXHIGHNY-26JUL25-T87    greater  floor=87   cap=None  "88 or above"
    KXHIGHNY-26JUL25-T80    less     floor=None cap=80    "79 or below"
    KXHIGHNY-26JUL25-B86.5  between  floor=86   cap=87    "86 to 87"
    KXHIGHNY-26JUL26-T88    greater  floor=88   cap=None  "89 or above"
    KXHIGHNY-26JUL26-T81    less     floor=None cap=81    "80 or below"
    KXHIGHNY-26JUL26-B87.5  between  floor=87   cap=88    "87 to 88"
    KXHIGHCHI-26JUL26-T96   greater  floor=96   cap=None  "97 or above"
    KXHIGHDEN-26JUL26-T96   less     floor=None cap=96    "95 or below"
    KXHIGHMIA-26JUL26-T94   greater  floor=94   cap=None  "95 or above"
    KXHIGHDEN-26JUL25-T94   less     floor=None cap=94    "93 or below"
    KXHIGHMIA-26JUL26-T87   less     floor=None cap=87    "86 or below"
    KXHIGHDEN-26JUL25-T101  greater  floor=101  cap=None  "102 or above"

The last five rows are deliberate SUFFIX COLLISIONS captured from the live
ladder: ``T96``, ``T94`` and ``T87`` each appear as BOTH a ``greater`` and a
``less`` contract on the same day. No parser that reads the suffix can be
right about both, which is the live-data proof that the letter and number in
a ticker carry no direction at all.

2026-07-25 API survey, cited by the mutation gate below: across 952 one-sided
KXHIGH markets (7 cities; ``open`` + ``settled`` + ``finalized`` + ``closed``)
the suffix number equals ``floor_strike`` for every ``greater`` and
``cap_strike`` for every ``less``, with zero exceptions. That is why the
``greater`` arm's real divergences are all at ``high == floor``, and why the
label-vs-field separator in the table is marked SYNTHETIC.

Every test here is offline-deterministic; the live probe lives in
``tests/test_kalshi_bracket_fields.py``.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
from collections import Counter, defaultdict

import pytest

from src.core.bracket_payoff import (
    STRIKE_TYPE_BETWEEN,
    STRIKE_TYPE_GREATER,
    STRIKE_TYPE_LESS,
    VALID_STRIKE_TYPES,
    BracketSpec,
    BracketSpecError,
    is_weather_symbol,
    p_yes_from_cdf,
    parse_bracket_spec,
    settlement_price,
    settles_yes,
    spec_from_position,
    yes_bounds,
)

DEG = "°"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "kxhighny_markets.json")


# ======================================================================
# THE GOLDEN TABLE
# ======================================================================
# One entry per live-probed contract. ``expected`` maps an observed daily
# high (whole degrees F, as the NWS Climatological Report publishes it) to
# whether the contract settles YES. Required boundary coverage per PRD
# Phase 1 exit criterion 1: floor-1 / floor / cap / cap+1 for ``between``,
# floor-1 / floor / floor+1 for ``greater``, cap-1 / cap / cap+1 for
# ``less``, plus surrounding temperatures.
#
# The LAX/CHI rows carry the strike_type verified live for those tickers;
# their floor/cap follow the invariant confirmed across all three cities
# (T<n> greater => floor=n; T<n> less => cap=n; B<n>.5 => floor=n, cap=n+1).
GOLDEN_BRACKETS = [
    # ---------------- between ----------------
    {
        "ticker": "KXHIGHNY-26JUL25-B86.5",
        "strike_type": STRIKE_TYPE_BETWEEN,
        "floor_strike": 86,
        "cap_strike": 87,
        "yes_sub_title": f"86{DEG} to 87{DEG}",
        "bounds": (86.0, 87.0),
        "expected": {
            84: False,  # well below
            85: False,  # floor - 1
            86: True,  # floor
            87: True,  # cap
            88: False,  # cap + 1
            90: False,  # well above
        },
    },
    {
        "ticker": "KXHIGHNY-26JUL26-B87.5",
        "strike_type": STRIKE_TYPE_BETWEEN,
        "floor_strike": 87,
        "cap_strike": 88,
        "yes_sub_title": f"87{DEG} to 88{DEG}",
        "bounds": (87.0, 88.0),
        "expected": {85: False, 86: False, 87: True, 88: True, 89: False, 91: False},
    },
    {
        "ticker": "KXHIGHCHI-26JUL24-B78.5",
        "strike_type": STRIKE_TYPE_BETWEEN,
        "floor_strike": 78,
        "cap_strike": 79,
        "yes_sub_title": f"78{DEG} to 79{DEG}",
        "bounds": (78.0, 79.0),
        "expected": {76: False, 77: False, 78: True, 79: True, 80: False, 82: False},
    },
    # ---------------- greater ----------------
    # Off-by-one: floor_strike=87 pays only at 88+ ("greater than 87").
    {
        "ticker": "KXHIGHNY-26JUL25-T87",
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 87,
        "cap_strike": None,
        "yes_sub_title": f"88{DEG} or above",
        "bounds": (88.0, float("inf")),
        "expected": {
            85: False,  # well below
            86: False,  # floor - 1
            87: False,  # floor  <-- does NOT pay
            88: True,  # floor + 1
            89: True,
            120: True,  # unbounded above
        },
    },
    {
        "ticker": "KXHIGHNY-26JUL26-T88",
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 88,
        "cap_strike": None,
        "yes_sub_title": f"89{DEG} or above",
        "bounds": (89.0, float("inf")),
        "expected": {86: False, 87: False, 88: False, 89: True, 90: True},
    },
    {
        "ticker": "KXHIGHLAX-26JUL24-T87",
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 87,
        "cap_strike": None,
        "yes_sub_title": f"88{DEG} or above",
        "bounds": (88.0, float("inf")),
        "expected": {85: False, 86: False, 87: False, 88: True, 89: True},
    },
    # ---------------- less ----------------
    # Off-by-one: cap_strike=80 pays only at 79- ("less than 80").
    {
        "ticker": "KXHIGHNY-26JUL25-T80",
        "strike_type": STRIKE_TYPE_LESS,
        "floor_strike": None,
        "cap_strike": 80,
        "yes_sub_title": f"79{DEG} or below",
        "bounds": (float("-inf"), 79.0),
        "expected": {
            -10: True,  # unbounded below
            77: True,
            78: True,  # cap - 2
            79: True,  # cap - 1
            80: False,  # cap    <-- does NOT pay
            81: False,  # cap + 1
        },
    },
    {
        "ticker": "KXHIGHNY-26JUL26-T81",
        "strike_type": STRIKE_TYPE_LESS,
        "floor_strike": None,
        "cap_strike": 81,
        "yes_sub_title": f"80{DEG} or below",
        "bounds": (float("-inf"), 80.0),
        "expected": {78: True, 79: True, 80: True, 81: False, 82: False},
    },
    {
        "ticker": "KXHIGHLAX-26JUL24-T80",
        "strike_type": STRIKE_TYPE_LESS,
        "floor_strike": None,
        "cap_strike": 80,
        "yes_sub_title": f"79{DEG} or below",
        "bounds": (float("-inf"), 79.0),
        "expected": {77: True, 78: True, 79: True, 80: False, 81: False},
    },
    # ------------- suffix collisions (live-probed 2026-07-25) -------------
    # T96 / T94 / T87 each exist as BOTH directions on the live ladder. Each
    # pair below is an independent separator for the mutation gate: the legacy
    # parser maps the two members of a pair to the SAME rule, so no single
    # legacy answer can satisfy both, whatever boundary rows anyone deletes.
    {
        "ticker": "KXHIGHCHI-26JUL26-T96",
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 96,
        "cap_strike": None,
        "yes_sub_title": f"97{DEG} or above",
        "bounds": (97.0, float("inf")),
        "expected": {94: False, 95: False, 96: False, 97: True, 98: True},
    },
    {
        "ticker": "KXHIGHDEN-26JUL26-T96",
        "strike_type": STRIKE_TYPE_LESS,
        "floor_strike": None,
        "cap_strike": 96,
        "yes_sub_title": f"95{DEG} or below",
        "bounds": (float("-inf"), 95.0),
        "expected": {93: True, 94: True, 95: True, 96: False, 97: False},
    },
    {
        "ticker": "KXHIGHMIA-26JUL26-T94",
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 94,
        "cap_strike": None,
        "yes_sub_title": f"95{DEG} or above",
        "bounds": (95.0, float("inf")),
        "expected": {92: False, 93: False, 94: False, 95: True, 96: True},
    },
    {
        "ticker": "KXHIGHDEN-26JUL25-T94",
        "strike_type": STRIKE_TYPE_LESS,
        "floor_strike": None,
        "cap_strike": 94,
        "yes_sub_title": f"93{DEG} or below",
        "bounds": (float("-inf"), 93.0),
        "expected": {91: True, 92: True, 93: True, 94: False, 95: False},
    },
    {
        "ticker": "KXHIGHMIA-26JUL26-T87",
        "strike_type": STRIKE_TYPE_LESS,
        "floor_strike": None,
        "cap_strike": 87,
        "yes_sub_title": f"86{DEG} or below",
        "bounds": (float("-inf"), 86.0),
        "expected": {84: True, 85: True, 86: True, 87: False, 88: False},
    },
    {
        "ticker": "KXHIGHDEN-26JUL25-T101",
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 101,
        "cap_strike": None,
        "yes_sub_title": f"102{DEG} or above",
        "bounds": (102.0, float("inf")),
        "expected": {99: False, 100: False, 101: False, 102: True, 103: True},
    },
    # ------------------------- SYNTHETIC row -------------------------
    # NOT a live market. Constructed because no live KXHIGH market has this
    # shape: the 2026-07-25 survey of 952 one-sided markets found the suffix
    # number equal to the authoritative strike in 952/952 cases, so every REAL
    # `greater` divergence from the legacy parser is confined to the single
    # temperature `high == floor_strike`.
    #
    # FR-1.1 says the API fields are authoritative and the label is not. This
    # row is that sentence as data: a `greater` bracket whose ticker says 85
    # and whose ``floor_strike`` says 87. The legacy parser then disagrees
    # across a RANGE (85, 86, 87), not at one point — a structurally different
    # separator, so deleting a `floor: False` boundary row cannot disarm the
    # `greater` arm of the mutation gate.
    #
    # It is excluded from every live-provenance assertion by its
    # ``synthetic`` flag.
    {
        "ticker": "KXHIGHNY-26JUL25-T85",
        "synthetic": True,
        "strike_type": STRIKE_TYPE_GREATER,
        "floor_strike": 87,
        "cap_strike": None,
        "yes_sub_title": f"88{DEG} or above",
        "bounds": (88.0, float("inf")),
        "expected": {
            83: False,
            84: False,
            85: False,  # legacy reads the LABEL (85) and pays here
            86: False,  # legacy pays here too
            87: False,  # ...and here
            88: True,
            89: True,
        },
    },
]

# Rows captured verbatim from the live API (everything but the labelled
# synthetic). Used by the provenance assertions.
LIVE_BRACKETS = [r for r in GOLDEN_BRACKETS if not r.get("synthetic")]


def _golden_by_ticker(ticker):
    for row in GOLDEN_BRACKETS:
        if row["ticker"] == ticker:
            return row
    raise KeyError(ticker)


def _spec(row) -> BracketSpec:
    return BracketSpec(
        ticker=row["ticker"],
        strike_type=row["strike_type"],
        floor_strike=row["floor_strike"],
        cap_strike=row["cap_strike"],
    )


# Flattened (ticker, temp, expected) rows for per-case test IDs.
GOLDEN_ROWS = [
    pytest.param(
        row["ticker"],
        temp,
        expect,
        id=f"{row['strike_type']}-{row['ticker']}-{temp}F",
    )
    for row in GOLDEN_BRACKETS
    for temp, expect in sorted(row["expected"].items())
]


def test_golden_table_covers_every_strike_type():
    """The table must exercise all three live strike_type values."""
    covered = {row["strike_type"] for row in GOLDEN_BRACKETS}
    assert covered == set(
        VALID_STRIKE_TYPES
    ), f"golden table covers {sorted(covered)}, expected {sorted(VALID_STRIKE_TYPES)}"


@pytest.mark.parametrize("ticker,temp,expected", GOLDEN_ROWS)
def test_settles_yes_matches_golden(ticker, temp, expected):
    row = _golden_by_ticker(ticker)
    spec = _spec(row)
    assert settles_yes(spec, temp) is expected, (
        f"{ticker} ({row['yes_sub_title']}) at {temp}F: "
        f"expected settles_yes={expected}"
    )


@pytest.mark.parametrize("ticker,temp,expected", GOLDEN_ROWS)
def test_settlement_price_matches_golden(ticker, temp, expected):
    row = _golden_by_ticker(ticker)
    spec = _spec(row)
    price = settlement_price(spec, temp)
    assert price == (1.0 if expected else 0.0), (
        f"{ticker} at {temp}F: expected terminal YES price "
        f"{1.0 if expected else 0.0}, got {price}"
    )


@pytest.mark.parametrize(
    "ticker", [pytest.param(r["ticker"], id=r["ticker"]) for r in GOLDEN_BRACKETS]
)
def test_yes_bounds_match_golden(ticker):
    row = _golden_by_ticker(ticker)
    assert yes_bounds(_spec(row)) == row["bounds"]


@pytest.mark.parametrize(
    "ticker", [pytest.param(r["ticker"], id=r["ticker"]) for r in GOLDEN_BRACKETS]
)
def test_describe_reproduces_kalshi_yes_sub_title(ticker):
    """``BracketSpec.describe()`` must reproduce Kalshi's published YES rule.

    This is the cheap, always-on cross-check that our interpretation of
    floor/cap agrees with the exchange's own words.
    """
    row = _golden_by_ticker(ticker)
    expected = row["yes_sub_title"].replace(DEG, "")
    assert _spec(row).describe() == expected


def test_all_golden_temperatures_settle_exactly_one_ny_bracket():
    """The 26JUL25 NY ladder is a partition: every high settles exactly one YES.

    Live ladder: T80 (<=79), B80.5, B82.5, B84.5, B86.5 (86-87), T87 (>=88).
    """
    ladder = [
        BracketSpec("KXHIGHNY-26JUL25-T80", STRIKE_TYPE_LESS, None, 80),
        BracketSpec("KXHIGHNY-26JUL25-B80.5", STRIKE_TYPE_BETWEEN, 80, 81),
        BracketSpec("KXHIGHNY-26JUL25-B82.5", STRIKE_TYPE_BETWEEN, 82, 83),
        BracketSpec("KXHIGHNY-26JUL25-B84.5", STRIKE_TYPE_BETWEEN, 84, 85),
        BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86, 87),
        BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87, None),
    ]
    for high in range(60, 101):
        winners = [s.ticker for s in ladder if settles_yes(s, high)]
        assert len(winners) == 1, f"high={high}F settled {winners}, expected exactly 1"


# ======================================================================
# End-to-end: provider extra -> parse_bracket_spec -> settles_yes
# ======================================================================


def _load_fixture_markets():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["markets"]


def _provider():
    from src.data.kalshi_provider import KalshiProvider

    return KalshiProvider(key_id=None, private_key_path=None, read_only=True)


@pytest.mark.parametrize("ticker,temp,expected", GOLDEN_ROWS)
def test_golden_reachable_from_api_shaped_extra(ticker, temp, expected):
    """The provider's ``extra`` must carry enough to settle the golden rows.

    Builds ``extra`` through the real ``_parse_market_data`` from an
    API-shaped market dict, then settles through the shared payoff module.
    This is the regression that proves provider and payoff agree (FR-1.1 +
    FR-1.2); if either side drifts, this fails rather than the two quietly
    disagreeing in production.
    """
    row = _golden_by_ticker(ticker)
    api_market = {
        "ticker": row["ticker"],
        "status": "active",
        "strike_type": row["strike_type"],
        "yes_bid_dollars": "0.4500",
        "yes_ask_dollars": "0.4700",
        "no_bid_dollars": "0.5300",
        "no_ask_dollars": "0.5500",
        "last_price_dollars": "0.4600",
        "volume_fp": "1234.00",
        "close_time": "2026-07-26T04:59:00Z",
        "yes_sub_title": row["yes_sub_title"],
    }
    # Kalshi OMITS the irrelevant strike entirely; mirror that exactly.
    if row["floor_strike"] is not None:
        api_market["floor_strike"] = row["floor_strike"]
    if row["cap_strike"] is not None:
        api_market["cap_strike"] = row["cap_strike"]

    md = _provider()._parse_market_data(row["ticker"], api_market, "test")
    spec = parse_bracket_spec(md.symbol, md.extra)

    assert spec.strike_type == row["strike_type"]
    assert spec.floor_strike == (
        None if row["floor_strike"] is None else float(row["floor_strike"])
    )
    assert spec.cap_strike == (
        None if row["cap_strike"] is None else float(row["cap_strike"])
    )
    assert settles_yes(spec, temp) is expected


def test_every_fixture_market_parses_and_matches_its_yes_sub_title():
    """Every live-captured KXHIGHNY market survives the provider->payoff path."""
    markets = _load_fixture_markets()
    assert len(markets) >= 6, "fixture should hold a full ladder"
    provider = _provider()
    for m in markets:
        md = provider._parse_market_data(m["ticker"], m, "fixture")
        spec = parse_bracket_spec(md.symbol, md.extra)
        published = (md.extra.get("yes_sub_title") or "").replace(DEG, "")
        assert spec.describe() == published, (
            f"{m['ticker']}: derived rule {spec.describe()!r} disagrees with "
            f"Kalshi's published {published!r}"
        )


def test_spec_from_position_round_trips():
    """A persisted position dict settles identically to the live market."""
    row = _golden_by_ticker("KXHIGHNY-26JUL25-B86.5")
    spec = _spec(row)
    position = {"symbol": row["ticker"], "quantity": 5}
    position.update(spec.as_dict())
    rebuilt = spec_from_position(position)
    assert rebuilt.strike_type == spec.strike_type
    assert rebuilt.floor_strike == spec.floor_strike
    assert rebuilt.cap_strike == spec.cap_strike
    for temp, expected in row["expected"].items():
        assert settles_yes(rebuilt, temp) is expected


def test_is_weather_symbol():
    assert is_weather_symbol("KXHIGHNY-26JUL25-B86.5") is True
    assert is_weather_symbol("KXHIGHCHI-26JUL24-T80") is True
    assert is_weather_symbol("KXBTC15M-26JUL25-30") is False
    assert is_weather_symbol("") is False


# ======================================================================
# Abort-on-ambiguity: BracketSpecError, never a silent default
# ======================================================================

BAD_EXTRAS = [
    pytest.param(None, id="extra-is-None"),
    pytest.param({}, id="extra-is-empty"),
    pytest.param({"floor_strike": 86, "cap_strike": 87}, id="strike_type-missing"),
    pytest.param(
        {"strike_type": None, "floor_strike": 86, "cap_strike": 87},
        id="strike_type-None",
    ),
    pytest.param(
        {"strike_type": "", "floor_strike": 86, "cap_strike": 87},
        id="strike_type-empty",
    ),
    pytest.param(
        {"strike_type": "   ", "floor_strike": 86, "cap_strike": 87},
        id="strike_type-blank",
    ),
    pytest.param(
        {"strike_type": "above", "floor_strike": 86}, id="strike_type-unknown-above"
    ),
    pytest.param(
        {"strike_type": "bracket", "floor_strike": 86, "cap_strike": 87},
        id="strike_type-unknown-bracket",
    ),
    pytest.param(
        {"strike_type": "between", "cap_strike": 87}, id="between-missing-floor"
    ),
    pytest.param(
        {"strike_type": "between", "floor_strike": 86}, id="between-missing-cap"
    ),
    pytest.param({"strike_type": "between"}, id="between-missing-both"),
    pytest.param({"strike_type": "greater"}, id="greater-missing-floor"),
    pytest.param(
        {"strike_type": "greater", "cap_strike": 87}, id="greater-only-has-cap"
    ),
    pytest.param({"strike_type": "less"}, id="less-missing-cap"),
    pytest.param({"strike_type": "less", "floor_strike": 80}, id="less-only-has-floor"),
    pytest.param(
        {"strike_type": "between", "floor_strike": "eighty-six", "cap_strike": 87},
        id="floor-not-numeric",
    ),
    pytest.param(
        {"strike_type": "between", "floor_strike": 86, "cap_strike": "hot"},
        id="cap-not-numeric",
    ),
    pytest.param(
        {"strike_type": "greater", "floor_strike": True},
        id="floor-is-bool",
    ),
    pytest.param(
        {"strike_type": "between", "floor_strike": 88, "cap_strike": 86},
        id="cap-below-floor",
    ),
    pytest.param("not-a-mapping", id="extra-is-a-string"),
]


@pytest.mark.parametrize("extra", BAD_EXTRAS)
def test_parse_bracket_spec_aborts_on_ambiguity(extra):
    """Ambiguous semantics must raise, never silently default to a direction."""
    with pytest.raises(BracketSpecError):
        parse_bracket_spec("KXHIGHNY-26JUL25-B86.5", extra)


def test_missing_strike_is_never_coerced_to_zero():
    """A None strike must stay None all the way through the spec.

    A ``greater`` market has no ``cap_strike``. Defaulting it to 0.0 would
    make the YES band ``[floor+1, 0]`` -- empty -- and settle every contract
    NO forever.
    """
    spec = parse_bracket_spec(
        "KXHIGHNY-26JUL25-T87", {"strike_type": "greater", "floor_strike": 87}
    )
    assert spec.cap_strike is None
    assert settles_yes(spec, 95) is True


def test_settles_yes_requires_an_observed_high():
    spec = _spec(_golden_by_ticker("KXHIGHNY-26JUL25-B86.5"))
    with pytest.raises(BracketSpecError):
        settles_yes(spec, None)
    with pytest.raises(BracketSpecError):
        settles_yes(spec, float("nan"))
    with pytest.raises(BracketSpecError):
        settles_yes(spec, "warm")


# ======================================================================
# p_yes_from_cdf
# ======================================================================

# A step CDF over whole-degree daily highs for a synthetic NY summer day.
STEP_PMF = {
    78: 0.03,
    79: 0.05,
    80: 0.07,
    81: 0.10,
    82: 0.12,
    83: 0.14,
    84: 0.14,
    85: 0.12,
    86: 0.10,
    87: 0.07,
    88: 0.04,
    89: 0.02,
}


def _step_cdf(x):
    """P(daily_high <= x) for the discrete STEP_PMF."""
    return sum(p for t, p in STEP_PMF.items() if t <= x)


def test_step_pmf_is_a_distribution():
    assert abs(sum(STEP_PMF.values()) - 1.0) < 1e-12


def test_p_yes_between_is_exactly_the_band_mass():
    spec = BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86, 87)
    expected = STEP_PMF[86] + STEP_PMF[87]
    assert abs(p_yes_from_cdf(spec, _step_cdf) - expected) < 1e-12


def test_p_yes_greater_is_the_upper_tail():
    spec = BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87, None)
    expected = sum(p for t, p in STEP_PMF.items() if t >= 88)
    assert abs(p_yes_from_cdf(spec, _step_cdf) - expected) < 1e-12


def test_p_yes_less_is_the_lower_tail():
    spec = BracketSpec("KXHIGHNY-26JUL25-T80", STRIKE_TYPE_LESS, None, 80)
    expected = sum(p for t, p in STEP_PMF.items() if t <= 79)
    assert abs(p_yes_from_cdf(spec, _step_cdf) - expected) < 1e-12


def test_greater_and_less_are_complements_across_a_split():
    """``less`` at cap C and ``greater`` at floor C-1 partition the line."""
    lower = BracketSpec("X-T85", STRIKE_TYPE_LESS, None, 85)  # <= 84
    upper = BracketSpec("X-T84", STRIKE_TYPE_GREATER, 84, None)  # >= 85
    total = p_yes_from_cdf(lower, _step_cdf) + p_yes_from_cdf(upper, _step_cdf)
    assert abs(total - 1.0) < 1e-12


def test_full_ladder_probabilities_sum_to_one():
    """The live NY ladder shape must integrate to 1.0 +/- 0.01 (FR-2.4 precursor)."""
    ladder = [
        BracketSpec("KXHIGHNY-26JUL25-T80", STRIKE_TYPE_LESS, None, 80),
        BracketSpec("KXHIGHNY-26JUL25-B80.5", STRIKE_TYPE_BETWEEN, 80, 81),
        BracketSpec("KXHIGHNY-26JUL25-B82.5", STRIKE_TYPE_BETWEEN, 82, 83),
        BracketSpec("KXHIGHNY-26JUL25-B84.5", STRIKE_TYPE_BETWEEN, 84, 85),
        BracketSpec("KXHIGHNY-26JUL25-B86.5", STRIKE_TYPE_BETWEEN, 86, 87),
        BracketSpec("KXHIGHNY-26JUL25-T87", STRIKE_TYPE_GREATER, 87, None),
    ]
    total = sum(p_yes_from_cdf(s, _step_cdf) for s in ladder)
    assert abs(total - 1.0) <= 0.01, f"ladder sums to {total}, expected 1.0 +/- 0.01"


def test_p_yes_is_clamped_to_unit_interval():
    spec = BracketSpec("X-B86.5", STRIKE_TYPE_BETWEEN, 86, 87)
    assert p_yes_from_cdf(spec, lambda x: 5.0) == 0.0  # flat cdf -> zero mass
    assert 0.0 <= p_yes_from_cdf(spec, lambda x: -3.0) <= 1.0


def test_p_yes_aborts_on_a_broken_cdf():
    spec = BracketSpec("X-B86.5", STRIKE_TYPE_BETWEEN, 86, 87)
    with pytest.raises(BracketSpecError):
        p_yes_from_cdf(spec, lambda x: None)
    with pytest.raises(BracketSpecError):
        p_yes_from_cdf(spec, lambda x: float("nan"))


# ======================================================================
# MUTATION GATE (PRD Phase 1 exit criterion 1)
# ======================================================================


def _legacy_suffix_payoff(ticker, high):
    """The DELETED pre-Phase-1 parser, reproduced here as the mutant.

    It read contract direction off the ticker's suffix letter and treated
    every non-B ticker as one-sided "at or above". Defined in the test file
    ONLY -- it must never exist under ``src/`` again.
    """
    strike_str = ticker.split("-")[-1]
    is_above = not strike_str.startswith("B")
    strike = float(re.sub(r"[A-Za-z]", "", strike_str))
    return (high >= strike) if is_above else (high <= strike)


@pytest.mark.parametrize("strike_type", list(VALID_STRIKE_TYPES))
def test_golden_table_detects_the_legacy_suffix_mutant(strike_type):
    """FR-1.1 MUTATION GATE: the golden table must kill the legacy parser.

    For EACH of the three contract types, at least one golden row's legacy
    answer must differ from the correct answer -- i.e. if the suffix-letter
    parser were swapped back into ``src/``, this file would go red for that
    type rather than passing and letting an inverted payoff reach capital.

    Historical stakes: the 2026-07-24 review attributed 372 of 472 wrong
    weather rows to exactly this parser, with ``less`` contracts evaluated
    exactly backwards.
    """
    divergences = []
    for row in GOLDEN_BRACKETS:
        if row["strike_type"] != strike_type:
            continue
        spec = _spec(row)
        for temp, expected in sorted(row["expected"].items()):
            try:
                mutant = _legacy_suffix_payoff(row["ticker"], temp)
            except ValueError:
                continue
            correct = settles_yes(spec, temp)
            assert correct is expected, "golden table self-consistency"
            if mutant != correct:
                divergences.append((row["ticker"], temp, mutant, correct))

    assert divergences, (
        f"MUTATION GATE FAILED for strike_type={strike_type!r}: the legacy "
        f"suffix-letter parser agrees with the golden table on every row, so "
        f"reintroducing it would not fail any test. Add a boundary "
        f"temperature that separates them."
    )


def _divergences(rows):
    """``[(ticker, temp, legacy, correct)]`` where the legacy parser is wrong."""
    out = []
    for row in rows:
        spec = _spec(row)
        for temp, expected in sorted(row["expected"].items()):
            try:
                mutant = _legacy_suffix_payoff(row["ticker"], temp)
            except ValueError:
                continue
            correct = settles_yes(spec, temp)
            assert (
                correct is expected
            ), f"{row['ticker']}@{temp}: table self-consistency"
            if mutant != correct:
                out.append((row["ticker"], temp, mutant, correct))
    return out


def test_report_mutation_kill_counts_per_contract_type(capsys):
    """Print the per-type kill counts the red-team asked to see re-reported."""
    lines = []
    for strike_type in VALID_STRIKE_TYPES:
        rows = [r for r in GOLDEN_BRACKETS if r["strike_type"] == strike_type]
        div = _divergences(rows)
        total = sum(len(r["expected"]) for r in rows)
        lines.append(
            f"{strike_type:>8}: {len(div):>2} killing rows / {total:>2} golden "
            f"rows across {len({d[0] for d in div})} of {len(rows)} tickers"
        )
    with capsys.disabled():
        print("\nMUTATION KILL COUNTS (legacy suffix parser vs golden table)")
        for line in lines:
            print("  " + line)


@pytest.mark.parametrize("strike_type", list(VALID_STRIKE_TYPES))
def test_mutation_gate_is_not_one_row_deep(strike_type):
    """Deleting ANY single golden row must leave the gate armed.

    The 2026-07-25 red-team measured the ``greater`` arm as one row deep:
    3 of 16 rows diverged and all three were the same case (``temp == floor``),
    so removing two entries would have disarmed that contract type while the
    file stayed green. This asserts the property directly — for every contract
    type, and for every single-row deletion.
    """
    rows = [r for r in GOLDEN_BRACKETS if r["strike_type"] == strike_type]
    all_div = _divergences(rows)
    assert all_div, f"no divergence at all for {strike_type!r}"

    for ticker, temp, _, _ in all_div:
        survivors = [d for d in all_div if not (d[0] == ticker and d[1] == temp)]
        assert survivors, (
            f"MUTATION GATE IS ONE ROW DEEP for {strike_type!r}: deleting "
            f"{ticker} @ {temp}F would leave the legacy suffix parser "
            f"undetected for this contract type"
        )


@pytest.mark.parametrize("strike_type", list(VALID_STRIKE_TYPES))
def test_mutation_gate_kills_from_more_than_one_ticker(strike_type):
    """The gate must not depend on a single market surviving in the table."""
    rows = [r for r in GOLDEN_BRACKETS if r["strike_type"] == strike_type]
    tickers = {d[0] for d in _divergences(rows)}
    assert len(tickers) >= 2, (
        f"only {sorted(tickers)} separates {strike_type!r} from the legacy "
        f"parser; deleting that one market disarms the gate"
    )


def test_greater_arm_has_a_separator_that_is_not_the_floor_boundary():
    """The ``greater`` arm must diverge for a structurally different reason.

    Every REAL ``greater`` market has ``suffix number == floor_strike`` (952/952
    in the 2026-07-25 survey), so real divergence is mathematically confined to
    ``high == floor``. The SYNTHETIC label-vs-field row supplies the second
    mechanism: the API's ``floor_strike`` and the ticker's label disagree, so
    the legacy parser is wrong across a range of temperatures.
    """
    rows = [r for r in GOLDEN_BRACKETS if r["strike_type"] == STRIKE_TYPE_GREATER]
    by_ticker = {r["ticker"]: r for r in rows}
    off_boundary = [
        d for d in _divergences(rows) if d[1] != by_ticker[d[0]]["floor_strike"]
    ]
    assert off_boundary, (
        "every `greater` divergence is at temp == floor_strike; the gate has "
        "only one mechanism and a boundary-row deletion weakens it"
    )
    # ...and it must be more than one temperature, i.e. genuinely a range.
    assert len(off_boundary) >= 2, off_boundary


def test_suffix_collisions_prove_the_ticker_carries_no_direction():
    """Live pairs where one suffix means opposite things.

    ``T96``/``T94``/``T87`` each appear in the table as both a ``greater`` and
    a ``less`` contract, captured from the live ladder on 2026-07-25. Any
    parser keyed on the suffix must give both members of a pair the same
    answer, so each pair is an independent, data-driven separator.
    """
    by_suffix = defaultdict(set)
    for row in LIVE_BRACKETS:
        by_suffix[row["ticker"].split("-")[-1]].add(row["strike_type"])
    collisions = {s: t for s, t in by_suffix.items() if len(t) > 1}
    assert collisions, "no suffix collision in the table"
    assert {"T96", "T94", "T87"} <= set(collisions), sorted(collisions)

    for suffix, types in collisions.items():
        assert {STRIKE_TYPE_GREATER, STRIKE_TYPE_LESS} <= types, (suffix, types)
        rows = [r for r in LIVE_BRACKETS if r["ticker"].endswith("-" + suffix)]
        # The legacy parser gives one answer per (suffix, temp); the truth
        # gives two. Find a temperature where the truths disagree.
        temps = set.intersection(*[set(r["expected"]) for r in rows])
        assert any(
            len({r["expected"][t] for r in rows}) > 1 for t in temps
        ), f"{suffix}: the collided contracts never disagree in the table"


def test_only_the_labelled_row_is_synthetic():
    """Provenance hygiene: exactly one row is not a live market, and it says so."""
    synthetic = [r["ticker"] for r in GOLDEN_BRACKETS if r.get("synthetic")]
    assert synthetic == ["KXHIGHNY-26JUL25-T85"], synthetic
    for row in LIVE_BRACKETS:
        suffix = row["ticker"].split("-")[-1]
        number = float(re.sub(r"[A-Za-z]", "", suffix))
        if row["strike_type"] == STRIKE_TYPE_GREATER:
            assert number == row["floor_strike"], (
                f"{row['ticker']} claims live provenance but its suffix "
                f"disagrees with floor_strike; live markets never do"
            )
        elif row["strike_type"] == STRIKE_TYPE_LESS:
            assert number == row["cap_strike"], row["ticker"]


def test_no_duplicate_tickers_in_the_golden_table():
    dupes = [
        t for t, n in Counter(r["ticker"] for r in GOLDEN_BRACKETS).items() if n > 1
    ]
    assert not dupes, dupes


def test_mutation_gate_pins_the_known_divergences():
    """Spot-pin the three canonical inversions the legacy parser produced."""
    # between: B86.5 read as "<= 86.5" pays at 84; truth says NO.
    assert _legacy_suffix_payoff("KXHIGHNY-26JUL25-B86.5", 84) is True
    assert settles_yes(_spec(_golden_by_ticker("KXHIGHNY-26JUL25-B86.5")), 84) is False

    # greater: T87 read as ">= 87" pays at 87; truth is "88 or above".
    assert _legacy_suffix_payoff("KXHIGHNY-26JUL25-T87", 87) is True
    assert settles_yes(_spec(_golden_by_ticker("KXHIGHNY-26JUL25-T87")), 87) is False

    # less: T80 read as ">= 80" is exactly backwards.
    assert _legacy_suffix_payoff("KXHIGHNY-26JUL25-T80", 77) is False
    assert settles_yes(_spec(_golden_by_ticker("KXHIGHNY-26JUL25-T80")), 77) is True
    assert _legacy_suffix_payoff("KXHIGHNY-26JUL25-T80", 81) is True
    assert settles_yes(_spec(_golden_by_ticker("KXHIGHNY-26JUL25-T80")), 81) is False


# ======================================================================
# Static guard: no direction inference from ticker suffix letters
# ======================================================================
#
# The guard this replaces was ``re.compile(r"startswith\(['\"]B['\"]\)")`` --
# a literal-string match that caught exactly ONE of the eight semantically
# identical spellings a red-team planted on 2026-07-25:
#
#   CAUGHT    _t.startswith("B")
#   SLIPS BY  _t[0] == "B" | "B" in _t[:1] | not _t.startswith("T")
#   SLIPS BY  _t[:1] == "B" | {"B":True,"T":False}[_t[0]]
#   SLIPS BY  re.match(r"^B", _t) | _t.lower().startswith('b')
#
# The replacement is an AST walk that looks for the *idea* -- a one-character
# direction letter (B or T) probed against the head of a string -- rather than
# for one way of typing it. Scope is the two trees where bracket direction is
# decided (``src/strategies/`` and ``src/core/``) plus, as a wider net, the
# rest of ``src/``; the alphabet is restricted to B/T so that legitimate
# magnitude and date-label extraction (``symbol.split("-")[-1]``,
# ``re.sub(r"[A-Za-z]", "", part)``, city/date prefix matching) stays green.
# ``tests`` are deliberately not scanned: this very file must reproduce the
# legacy parser to keep the mutation gate armed.

DIRECTION_LETTERS = frozenset("bt")

# Trees where a bracket's direction is decided. Scanned first and named
# separately so a failure points at the FR-1.1-critical code.
DIRECTION_CRITICAL_DIRS = (
    os.path.join(REPO_ROOT, "src", "strategies"),
    os.path.join(REPO_ROOT, "src", "core"),
)

_PREFIX_METHODS = {"startswith", "endswith"}
_RE_FUNCS = {"match", "search", "fullmatch", "compile"}
_MEMBERSHIP_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def _is_direction_letter(node) -> bool:
    """A one-character ``"B"``/``"T"`` string literal, any case."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and len(node.value) == 1
        and node.value.lower() in DIRECTION_LETTERS
    )


def _regex_anchors_on_a_direction_letter(pattern: str) -> bool:
    """``^B``, ``\\AB``, ``^[BT]`` and friends -- a head-anchored letter test."""
    if not isinstance(pattern, str):
        return False
    body = pattern
    for anchor in ("\\A", "^"):
        if body.startswith(anchor):
            body = body[len(anchor) :]
            break
    else:
        return False
    if not body:
        return False
    if body[0] == "[":
        end = body.find("]")
        inside = body[1:end] if end > 0 else body[1:]
        letters = {c.lower() for c in inside if c.isalpha()}
        return bool(letters) and letters <= DIRECTION_LETTERS
    return body[0].isalpha() and body[0].lower() in DIRECTION_LETTERS


def _suffix_letter_probes(path):
    """(lineno, snippet) for every direction-letter probe in a module.

    Detects, semantically rather than textually:

    * ``<expr>.startswith("B")`` / ``.endswith(...)``, including through
      ``.lower()``/``.upper()`` and with the result negated;
    * ``<expr> == "B"`` / ``!=`` / ``"B" in <expr>`` -- the index and slice
      spellings (``_t[0]``, ``_t[:1]``);
    * a dict literal keyed by direction letters (the lookup-table spelling);
    * ``re.match(r"^B", ...)`` and the other head-anchored regex spellings.
    """
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    hits = {}

    def record(node):
        lineno = getattr(node, "lineno", 0)
        if lineno and lineno not in hits:
            hits[lineno] = lines[lineno - 1].strip()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _PREFIX_METHODS:
                for arg in node.args:
                    candidates = (
                        arg.elts if isinstance(arg, (ast.Tuple, ast.List)) else [arg]
                    )
                    if any(_is_direction_letter(c) for c in candidates):
                        record(node)
            elif node.func.attr in _RE_FUNCS and node.args:
                first = node.args[0]
                if isinstance(
                    first, ast.Constant
                ) and _regex_anchors_on_a_direction_letter(first.value):
                    record(node)
        elif isinstance(node, ast.Compare):
            if any(isinstance(op, _MEMBERSHIP_OPS) for op in node.ops):
                operands = [node.left, *node.comparators]
                if any(_is_direction_letter(o) for o in operands):
                    record(node)
        elif isinstance(node, ast.Dict):
            if any(_is_direction_letter(k) for k in node.keys):
                record(node)

    return [(path, lineno, snippet) for lineno, snippet in sorted(hits.items())]


def _python_files_under(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _src_python_files():
    yield from _python_files_under(os.path.join(REPO_ROOT, "src"))


def _format(hits):
    return "\n  ".join(
        f"{os.path.relpath(f, REPO_ROOT)}:{ln}: {text}" for f, ln, text in hits
    )


@pytest.mark.parametrize(
    "root", DIRECTION_CRITICAL_DIRS, ids=lambda p: os.path.basename(p)
)
def test_direction_critical_trees_never_probe_a_ticker_suffix_letter(root):
    """PRD Phase 1 exit criterion 2, for the code that decides direction.

    Contract direction comes from the API's ``strike_type`` and nothing else.
    A hit here means some module under ``src/strategies/`` or ``src/core/``
    branches on a ``B``/``T`` character -- the exact idiom the 2026-07-24
    review blamed for 372 of 472 inverted historical weather rows.
    """
    hits = []
    for path in _python_files_under(root):
        hits.extend(_suffix_letter_probes(path))
    assert not hits, (
        f"ticker-suffix direction inference found under "
        f"{os.path.relpath(root, REPO_ROOT)} (use strike_type from the API "
        f"instead):\n  " + _format(hits)
    )


def test_no_module_infers_direction_from_ticker_suffix_letter():
    """The same guard, widened to every module under ``src/``.

    Legitimate magnitude and date-label extraction survives this by
    construction: ``src/bots/mixins.py`` (crypto ATM strike selection),
    ``src/visualization/dashboard.py`` (KXBTCD display sort),
    ``src/backtest/data_loader.py`` (legacy crypto numeric strike),
    ``src/core/weather_settlement.py`` (city-series prefix, event-date
    segment) and the city-key lookups in the two weather strategies all parse
    NUMBERS or match MULTI-character names -- none of them probes a single
    direction letter.
    """
    hits = []
    for path in _src_python_files():
        hits.extend(_suffix_letter_probes(path))
    assert not hits, (
        "ticker-suffix direction inference found under src/ "
        "(use strike_type from the API instead):\n  " + _format(hits)
    )


# ---------------------------------------------------------------------
# The guard must be able to fail: eight spellings, one idea
# ---------------------------------------------------------------------

PLANTED_VIOLATIONS = [
    ("canonical_startswith_B", 'is_above = not _t.startswith("B")'),
    ("index_eq_B", 'is_above = _t[0] == "B"'),
    ("membership_in_slice", 'is_above = "B" in _t[:1]'),
    ("negated_startswith_T", 'is_above = not _t.startswith("T")'),
    ("slice_eq_B", 'is_above = _t[:1] == "B"'),
    ("dict_lookup", 'is_above = {"B": True, "T": False}[_t[0]]'),
    ("regex_anchor", 'is_above = bool(re.match(r"^B", _t))'),
    ("lower_startswith_b", "is_above = _t.lower().startswith('b')"),
]

# Verbatim from the modules a verifier confirmed legitimate on 2026-07-25.
# These must stay green or the guard is unusable.
LEGITIMATE_SNIPPETS = [
    (
        "mixins-66",
        'strike_val = float(re.sub(r"[A-Za-z]", "", m.symbol.split("-")[-1]))',
    ),
    (
        "mixins-152",
        'val = float(re.sub(r"[A-Za-z]", "", m.get("ticker", "").split("-")[-1]))',
    ),
    ("mixins-250", 'return float(re.sub(r"[A-Za-z]", "", m.symbol.split("-")[-1]))'),
    (
        "dashboard-483",
        'strike_val = float(re.sub(r"[^\\d.]", "", clean_sym.split("-")[-1]))',
    ),
    (
        "data_loader-324",
        "strike = (floor_strike or 0.0) or _parse_strike_from_ticker(ticker)",
    ),
    ("weather_settlement-158", "found = upper.startswith(city.kalshi_series.upper())"),
    ("weather_settlement-221", "m = _EVENT_DATE_RE.match(segment)"),
    ("weather_strategy-194", 'city_key = symbol.split("-")[0]'),
    ("ml_weather-85", 'city_key = symbol.split("-")[0]'),
    ("month-lookup", '_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3}'),
    ("series-prefix", 'is_weather = symbol.upper().startswith("KXHIGH")'),
]


@pytest.mark.parametrize(
    "label,line", [pytest.param(lbl, ln, id=lbl) for lbl, ln in PLANTED_VIOLATIONS]
)
def test_guard_catches_every_spelling_of_suffix_inference(tmp_path, label, line):
    """All eight spellings the old literal regex missed must be CAUGHT."""
    planted = tmp_path / f"planted_{label}.py"
    planted.write_text(
        "import re\n\n\ndef direction(ticker):\n"
        '    _t = ticker.split("-")[-1]\n'
        f"    {line}\n"
        "    return is_above\n",
        encoding="utf-8",
    )
    hits = _suffix_letter_probes(str(planted))
    assert hits, f"guard did not catch the {label!r} spelling: {line}"


@pytest.mark.parametrize(
    "label,line", [pytest.param(lbl, ln, id=lbl) for lbl, ln in LEGITIMATE_SNIPPETS]
)
def test_guard_does_not_flag_legitimate_magnitude_or_label_parsing(
    tmp_path, label, line
):
    """Numbers, dates and multi-character names are not direction inference."""
    clean = tmp_path / f"clean_{label.replace('-', '_')}.py"
    clean.write_text(
        "import re\n\n_EVENT_DATE_RE = re.compile(r'(\\d{2})([A-Z]{3})(\\d{2})')\n\n\n"
        "def parse(symbol, ticker=None, m=None, clean_sym=None, upper=None, "
        "city=None, segment=None, floor_strike=None):\n"
        f"    {line}\n",
        encoding="utf-8",
    )
    hits = _suffix_letter_probes(str(clean))
    assert not hits, f"false positive on legitimate line ({label}): {hits}"


def test_the_old_literal_regex_really_was_one_of_eight():
    """Document the improvement the AST guard represents."""
    old = re.compile(r"startswith\(['\"]B['\"]\)")
    caught_by_old = [lbl for lbl, line in PLANTED_VIOLATIONS if old.search(line)]
    assert caught_by_old == ["canonical_startswith_B"], caught_by_old
    assert len(PLANTED_VIOLATIONS) == 8
