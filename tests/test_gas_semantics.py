"""Golden-table tests for AAA gas settlement semantics (PRD FR-4.4).

This file is the Phase 4 analogue of ``tests/test_bracket_payoff.py``. It pins
Kalshi's ``KXAAAGAS*`` settlement rule to a table keyed on markets probed live
from the production API on 2026-07-29, sweeps the boundary values around every
strike, and keeps a mutation gate proving the table would go red if the payoff's
``>`` were ever weakened to ``>=``.

LIVE PROVENANCE (anonymous read, no auth required)
-------------------------------------------------
    GET https://api.elections.kalshi.com/trade-api/v2/markets
        ?series_ticker=KXAAAGAS{M,W,D}&status=settled&limit=1000

Harvested 2026-07-29: 74 monthly, 266 weekly and 1,166 daily settled markets --
1,506 in total, every one ``strike_type="greater"`` with a ``floor_strike`` and
no ``cap_strike``. The monthly ladders and the boundary-tie subset are committed
verbatim under ``tests/fixtures/gas/`` and every assertion below reads them, so
the table cannot drift from the exchange without this file failing.

    KXAAAGASM-26JUN30-3.84   greater  floor=3.84   settle 3.847  ->  YES
    KXAAAGASM-26JUN30-3.89   greater  floor=3.89   settle 3.847  ->  NO
    KXAAAGASM-26MAY31-4.33   greater  floor=4.33   settle 4.336  ->  YES
    KXAAAGASM-26MAY31-4.34   greater  floor=4.34   settle 4.336  ->  NO
    KXAAAGASD-26JUN29-3.860  greater  floor=3.860  settle 3.860  ->  NO   <-- tie
    KXAAAGASW-26JUL27-4.110  greater  floor=4.110  settle 4.110  ->  NO   <-- tie

THE BOUNDARY IS LIVE-PROVEN, NOT INFERRED FROM PROSE
----------------------------------------------------
``rules_primary`` says "strictly greater than", but prose is not a gate. Of the
1,506 settled markets, **15 settled with ``expiration_value == floor_strike``
exactly and all 15 settled NO** -- zero settled YES. A ``>=`` payoff inverts
every one of those 15 published results; a strict ``>`` payoff reproduces all
1,506 with zero disagreements. Those 15 markets are committed at
``tests/fixtures/gas/gas_boundary_ties.json`` and are the mutation gate's
separators.

Note the trap in the other direction: the tie market's own ``yes_sub_title``
reads ``"Above 3.860"`` and it settled NO on exactly 3.860. Anything that reads
the boundary off a display label gets it wrong.

WHY THIS FILE COVERS THE DAILY AND WEEKLY SERIES TOO
----------------------------------------------------
Phase 4 trades only ``KXAAAGASM``, but the monthly series is **silent** on the
boundary: neither retrievable month-end settle (4.336, 3.847) landed on a
strike, so a monthly-only table cannot tell ``>`` from ``>=`` at all. All 15
boundary proofs come from ``KXAAAGASD``/``KXAAAGASW``, which settle on the same
published AAA number under the same rule text. Excluding them would leave the
gate disarmed on the one comparison that matters.

Every test here is offline-deterministic and reads only committed fixtures.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import types
from collections import Counter

import pytest

from src.data.gas_settlement import (
    GAS_SERIES,
    STRIKE_TYPE_GREATER,
    VALUE_CEILING,
    VALUE_FLOOR,
    VERIFIED_STRIKE_TYPES,
    AAARow,
    GasSpec,
    GasSpecError,
    GasTruthError,
    event_ticker,
    expected_result,
    is_gas_symbol,
    load_aaa_series,
    month_end,
    parse_gas_spec,
    pin_truth_from_ladder,
    pin_truth_from_settled_markets,
    series_for,
    settlement_date_for,
    settlement_dates,
    settlement_price,
    settles_yes,
    spec_from_position,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "gas")
MONTHLY_FIXTURE = os.path.join(FIXTURE_DIR, "kxaaagasm_settled_ladders.json")
TIES_FIXTURE = os.path.join(FIXTURE_DIR, "gas_boundary_ties.json")
PINNED_CSV = os.path.join(FIXTURE_DIR, "kalshi_pinned_truth.csv")
PINNED_MANIFEST = os.path.join(FIXTURE_DIR, "kalshi_pinned_truth_manifest.json")
MODULE_PATH = os.path.join(REPO_ROOT, "src", "data", "gas_settlement.py")


def _load_markets(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)["markets"]


MONTHLY_MARKETS = _load_markets(MONTHLY_FIXTURE)
TIE_MARKETS = _load_markets(TIES_FIXTURE)


# ======================================================================
# THE GOLDEN TABLE
# ======================================================================
# One entry per live-probed contract, with the settle Kalshi published and the
# result it published. ``extra_expected`` maps additional AAA values (USD/gal)
# to whether the contract settles YES. The three required boundary probes --
# strike-0.001, exactly strike, strike+0.001 -- are generated for every row by
# :func:`_boundary_rows`, so no row can omit them.
GOLDEN_CONTRACTS = [
    # ---------------- monthly: the traded series (PRD FR-4.3) ----------------
    {
        "ticker": "KXAAAGASM-26JUN30-3.84",
        "series": "KXAAAGASM",
        "floor_strike": 3.84,
        "settled_value": 3.847,
        "settled_result": "yes",
        "rules_excerpt": "strictly greater than $3.84 on Jun 30, 2026",
        "extra_expected": {3.00: False, 3.50: False, 3.84: False, 4.50: True},
    },
    {
        "ticker": "KXAAAGASM-26JUN30-3.89",
        "series": "KXAAAGASM",
        "floor_strike": 3.89,
        "settled_value": 3.847,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $3.89 on Jun 30, 2026",
        "extra_expected": {3.847: False, 3.90: True, 5.00: True},
    },
    {
        "ticker": "KXAAAGASM-26JUN30-3.00",
        "series": "KXAAAGASM",
        "floor_strike": 3.00,
        "settled_value": 3.847,
        "settled_result": "yes",
        "rules_excerpt": "strictly greater than $3.00 on Jun 30, 2026",
        "extra_expected": {1.50: False, 2.99: False, 3.847: True},
    },
    {
        "ticker": "KXAAAGASM-26MAY31-4.33",
        "series": "KXAAAGASM",
        "floor_strike": 4.33,
        "settled_value": 4.336,
        "settled_result": "yes",
        "rules_excerpt": "strictly greater than $4.33 on May 31, 2026",
        "extra_expected": {4.32: False, 4.336: True},
    },
    {
        "ticker": "KXAAAGASM-26MAY31-4.34",
        "series": "KXAAAGASM",
        "floor_strike": 4.34,
        "settled_value": 4.336,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $4.34 on May 31, 2026",
        "extra_expected": {4.336: False, 4.35: True},
    },
    {
        "ticker": "KXAAAGASM-26MAY31-4.60",
        "series": "KXAAAGASM",
        "floor_strike": 4.60,
        "settled_value": 4.336,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $4.60 on May 31, 2026",
        "extra_expected": {4.336: False, 4.599: False, 4.601: True},
    },
    # ------- boundary ties: the live proof that value == strike pays NO -------
    # These are the only markets in the whole history whose settle landed
    # exactly on a strike. Each one is an independent separator between `>`
    # and `>=`, and none of them is monthly -- see the module docstring.
    {
        "ticker": "KXAAAGASD-26JUN29-3.860",
        "series": "KXAAAGASD",
        "floor_strike": 3.860,
        "settled_value": 3.860,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $3.860 on Jun 29, 2026",
        "extra_expected": {3.859: False, 3.861: True},
    },
    {
        "ticker": "KXAAAGASW-26JUL27-4.110",
        "series": "KXAAAGASW",
        "floor_strike": 4.110,
        "settled_value": 4.110,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $4.110 on Jul 27, 2026",
        "extra_expected": {4.109: False, 4.111: True},
    },
    {
        "ticker": "KXAAAGASD-26JUN15-4.065",
        "series": "KXAAAGASD",
        "floor_strike": 4.065,
        "settled_value": 4.065,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $4.065 on Jun 15, 2026",
        "extra_expected": {4.064: False, 4.066: True},
    },
    {
        "ticker": "KXAAAGASD-26MAY24-4.515",
        "series": "KXAAAGASD",
        "floor_strike": 4.515,
        "settled_value": 4.515,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $4.515 on May 24, 2026",
        "extra_expected": {4.514: False, 4.516: True},
    },
    {
        "ticker": "KXAAAGASD-26JUL22-4.060",
        "series": "KXAAAGASD",
        "floor_strike": 4.060,
        "settled_value": 4.060,
        "settled_result": "no",
        "rules_excerpt": "strictly greater than $4.060 on Jul 22, 2026",
        "extra_expected": {4.059: False, 4.061: True},
    },
]

#: The three boundary probes Phase 4 requires for every strike, as offsets.
BOUNDARY_OFFSETS = (-0.001, 0.0, +0.001)


def _spec(row) -> GasSpec:
    return GasSpec(
        ticker=row["ticker"],
        strike_type=STRIKE_TYPE_GREATER,
        floor_strike=row["floor_strike"],
    )


def _boundary_rows():
    """``(ticker, value, expected, label)`` for the mandated boundary probes."""
    out = []
    for row in GOLDEN_CONTRACTS:
        floor = float(row["floor_strike"])
        for offset in BOUNDARY_OFFSETS:
            value = floor if offset == 0.0 else round(floor + offset, 6)
            label = (
                "strike"
                if offset == 0.0
                else ("strike-0.001" if offset < 0 else "strike+0.001")
            )
            out.append((row["ticker"], value, value > floor, label))
    return out


BOUNDARY_ROWS = _boundary_rows()

GOLDEN_ROWS = [
    pytest.param(
        row["ticker"],
        value,
        expect,
        id=f"{row['ticker']}-{value:g}",
    )
    for row in GOLDEN_CONTRACTS
    for value, expect in sorted(row["extra_expected"].items())
]

BOUNDARY_PARAMS = [
    pytest.param(ticker, value, expect, id=f"{ticker}-{label}")
    for ticker, value, expect, label in BOUNDARY_ROWS
]


def _golden_by_ticker(ticker):
    for row in GOLDEN_CONTRACTS:
        if row["ticker"] == ticker:
            return row
    raise KeyError(ticker)


# ----------------------------------------------------------------------
# The table itself
# ----------------------------------------------------------------------
def test_golden_table_covers_every_series_and_both_settle_relations():
    """The table must span all three AAA series and both YES and NO settles."""
    series = {row["series"] for row in GOLDEN_CONTRACTS}
    assert series == set(GAS_SERIES), sorted(series)
    results = Counter(row["settled_result"] for row in GOLDEN_CONTRACTS)
    assert results["yes"] >= 2 and results["no"] >= 2, results


def test_every_golden_row_carries_the_three_mandated_boundary_probes():
    """strike-0.001 / strike / strike+0.001 exist for every strike in the table."""
    by_ticker = {}
    for ticker, _value, _expect, label in BOUNDARY_ROWS:
        by_ticker.setdefault(ticker, set()).add(label)
    for row in GOLDEN_CONTRACTS:
        assert by_ticker[row["ticker"]] == {
            "strike-0.001",
            "strike",
            "strike+0.001",
        }, row["ticker"]


@pytest.mark.parametrize("ticker,value,expected", BOUNDARY_PARAMS)
def test_boundary_probes_match_strictly_greater(ticker, value, expected):
    """The whole contract, at the only place it can be got wrong."""
    spec = _spec(_golden_by_ticker(ticker))
    assert settles_yes(spec, value) is expected, (
        f"{ticker} ({spec.describe()}) at value={value!r}: expected "
        f"settles_yes={expected}"
    )


def test_a_settle_exactly_on_the_strike_pays_no():
    """Stated as its own named assertion because it is the entire trap."""
    for row in GOLDEN_CONTRACTS:
        spec = _spec(row)
        assert settles_yes(spec, row["floor_strike"]) is False, row["ticker"]
        assert settlement_price(spec, row["floor_strike"]) == 0.0


@pytest.mark.parametrize("ticker,value,expected", GOLDEN_ROWS)
def test_settles_yes_matches_golden(ticker, value, expected):
    spec = _spec(_golden_by_ticker(ticker))
    assert settles_yes(spec, value) is expected


@pytest.mark.parametrize("ticker,value,expected", GOLDEN_ROWS)
def test_settlement_price_matches_golden(ticker, value, expected):
    spec = _spec(_golden_by_ticker(ticker))
    assert settlement_price(spec, value) == (1.0 if expected else 0.0)


@pytest.mark.parametrize(
    "ticker", [pytest.param(r["ticker"], id=r["ticker"]) for r in GOLDEN_CONTRACTS]
)
def test_golden_row_reproduces_the_result_kalshi_published(ticker):
    """Our payoff on Kalshi's own settlement input must equal Kalshi's result."""
    row = _golden_by_ticker(ticker)
    spec = _spec(row)
    assert expected_result(spec, row["settled_value"]) == row["settled_result"], (
        f"{ticker}: settle {row['settled_value']} vs strike {row['floor_strike']} "
        f"-- Kalshi published {row['settled_result'].upper()}"
    )


@pytest.mark.parametrize(
    "ticker", [pytest.param(r["ticker"], id=r["ticker"]) for r in GOLDEN_CONTRACTS]
)
def test_describe_spells_the_strict_comparison_out(ticker):
    row = _golden_by_ticker(ticker)
    described = _spec(row).describe()
    assert described == f"strictly above {row['floor_strike']:g}"
    # ...and it must not reproduce Kalshi's own misleading label.
    assert not described.lower().startswith("above ")


# ----------------------------------------------------------------------
# Full-fixture cross-check: every committed live market, not just the table
# ----------------------------------------------------------------------
def _settled(markets):
    return [
        m
        for m in markets
        if str(m.get("result") or "").lower() in ("yes", "no")
        and m.get("expiration_value") not in (None, "")
    ]


@pytest.mark.parametrize(
    "fixture,label",
    [
        pytest.param(MONTHLY_MARKETS, "monthly-ladders", id="monthly"),
        pytest.param(TIE_MARKETS, "boundary-ties", id="ties"),
    ],
)
def test_every_committed_live_market_settles_as_kalshi_published(fixture, label):
    """Strict ``>`` must reproduce every published result in the fixtures.

    This is the wide net behind the golden table: a semantics change that the
    hand-written rows happened to miss still fails here.
    """
    rows = _settled(fixture)
    assert rows, f"{label}: fixture holds no settled market"
    wrong = []
    for market in rows:
        spec = parse_gas_spec(market["ticker"], market)
        derived = expected_result(spec, float(market["expiration_value"]))
        if derived != str(market["result"]).lower():
            wrong.append(
                (
                    market["ticker"],
                    market["expiration_value"],
                    market["result"],
                    derived,
                )
            )
    assert not wrong, f"{label}: {len(wrong)} disagreement(s) with Kalshi: {wrong[:5]}"


def test_the_tie_fixture_really_is_all_exact_ties_and_all_no():
    """Provenance hygiene for the mutation gate's ammunition."""
    assert len(TIE_MARKETS) >= 10, len(TIE_MARKETS)
    for market in TIE_MARKETS:
        assert float(market["expiration_value"]) == float(
            market["floor_strike"]
        ), market["ticker"]
        assert market["result"] == "no", market["ticker"]
        assert "strictly greater than" in market["rules_primary"]
    # The ties must not all come from one settlement date or one series, or the
    # gate rests on a single event surviving in the fixture.
    assert len({m["event_ticker"] for m in TIE_MARKETS}) >= 8
    assert len({m["ticker"].split("-")[0] for m in TIE_MARKETS}) >= 2


def test_no_committed_market_carries_an_unexpected_shape():
    """Everything is ``greater`` with a floor and no cap; assert it, don't assume."""
    for market in MONTHLY_MARKETS + TIE_MARKETS:
        assert market["strike_type"] == STRIKE_TYPE_GREATER, market["ticker"]
        assert market.get("floor_strike") is not None, market["ticker"]
        assert market.get("cap_strike") is None, market["ticker"]
        assert VALUE_FLOOR <= float(market["floor_strike"]) <= VALUE_CEILING


# ======================================================================
# MUTATION GATE: swap ``>`` for ``>=`` in the shipped source
# ======================================================================
# The requirement is that a gate you have not shown can fail is not evidence.
# So rather than re-implementing a mutant here, this loads the real
# ``src/data/gas_settlement.py``, rewrites the comparison inside ``settles_yes``
# from Gt to GtE at the AST level, executes the mutated module, and re-runs the
# golden table against it. Two things are asserted: the mutation actually
# happened (exactly one comparison, so a refactor that moves the comparison
# elsewhere fails loudly instead of silently mutating nothing), and the table
# goes red.


class _GtToGtE(ast.NodeTransformer):
    def __init__(self):
        self.count = 0

    def visit_Compare(self, node):  # noqa: N802 - ast API
        self.generic_visit(node)
        node.ops = [(ast.GtE() if isinstance(op, ast.Gt) else op) for op in node.ops]
        self.count += sum(isinstance(op, ast.GtE) for op in node.ops)
        return node


def _mutate_settles_yes():
    """``(module, mutations)`` -- the shipped module with ``>`` weakened to ``>=``."""
    with open(MODULE_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=MODULE_PATH)
    mutations = 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "settles_yes":
            transformer = _GtToGtE()
            node.body = [transformer.visit(stmt) for stmt in node.body]
            mutations += transformer.count
    ast.fix_missing_locations(tree)
    name = "gas_settlement_mutant"
    module = types.ModuleType(name)
    module.__file__ = MODULE_PATH
    # dataclasses resolves a field's string annotation through
    # sys.modules[cls.__module__], so the mutant has to be registered while it
    # executes or GasSpec's own definition raises.
    sys.modules[name] = module
    try:
        exec(compile(tree, MODULE_PATH, "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module, mutations


def test_the_payoff_comparison_is_exactly_one_gt_inside_settles_yes():
    """Pin the invariant the mutation gate assumes.

    If the strict comparison is ever refactored out of ``settles_yes`` -- into a
    helper, a lookup, or a numpy expression -- the mutation below would rewrite
    nothing, pass, and report a gate that no longer exists. Assert the shape
    directly instead (``pin-the-invariants-a-frozen-golden-assumes``).
    """
    _module, mutations = _mutate_settles_yes()
    assert mutations == 1, (
        f"expected exactly one `>` comparison inside settles_yes, mutated "
        f"{mutations}. The mutation gate below is only meaningful while the "
        f"payoff comparison lives there."
    )


def _mutant_failures():
    """Golden rows on which the ``>=`` mutant disagrees with the shipped module."""
    module, mutations = _mutate_settles_yes()
    assert mutations == 1
    out = []
    for ticker, value, expected, label in BOUNDARY_ROWS:
        row = _golden_by_ticker(ticker)
        spec = module.GasSpec(
            ticker=ticker,
            strike_type=STRIKE_TYPE_GREATER,
            floor_strike=row["floor_strike"],
        )
        got = module.settles_yes(spec, value)
        if got is not expected:
            out.append((ticker, label, value, got, expected))
    return out


def test_mutation_gate_the_ge_mutant_goes_red():
    """MUTATION GATE: weakening ``>`` to ``>=`` must break the golden table."""
    failures = _mutant_failures()
    assert failures, (
        "MUTATION GATE FAILED: the `>=` mutant agrees with the golden table on "
        "every boundary probe, so weakening the payoff would not fail any test. "
        "Add a value exactly equal to a strike."
    )
    # Every failure must be the exact-strike probe -- that is the only value at
    # which `>` and `>=` can differ, so anything else means a probe is wrong.
    labels = {label for _t, label, _v, _g, _e in failures}
    assert labels == {"strike"}, labels


def test_mutation_gate_fires_on_every_golden_strike_not_just_one():
    """One killing row is a gate one deletion deep. Require all of them."""
    failures = _mutant_failures()
    killed = {ticker for ticker, _label, _v, _g, _e in failures}
    assert killed == {row["ticker"] for row in GOLDEN_CONTRACTS}, sorted(killed)
    assert len(killed) >= 8


def test_mutation_gate_also_inverts_the_live_published_results():
    """The mutant must contradict the exchange, not just our table.

    This is the strongest form available: with ``>=`` in place, our payoff
    disagrees with Kalshi's own published ``result`` on real settled markets.
    """
    module, _mutations = _mutate_settles_yes()
    inverted = []
    for market in _settled(MONTHLY_MARKETS + TIE_MARKETS):
        spec = module.GasSpec(
            ticker=market["ticker"],
            strike_type=STRIKE_TYPE_GREATER,
            floor_strike=float(market["floor_strike"]),
        )
        got = (
            "yes"
            if module.settles_yes(spec, float(market["expiration_value"]))
            else "no"
        )
        if got != str(market["result"]).lower():
            inverted.append((market["ticker"], market["expiration_value"], got))
    assert len(inverted) >= 10, (
        f"the `>=` mutant only contradicts {len(inverted)} live published "
        f"result(s); the boundary-tie fixture should supply at least 10"
    )
    for ticker, _value, got in inverted:
        assert got == "yes", (ticker, got)


def test_report_mutation_kill_counts(capsys):
    """Print the kill counts a red-team asked to see re-reported."""
    failures = _mutant_failures()
    module, _m = _mutate_settles_yes()
    live = _settled(MONTHLY_MARKETS + TIE_MARKETS)
    live_inverted = 0
    for market in live:
        spec = module.GasSpec(
            ticker=market["ticker"],
            strike_type=STRIKE_TYPE_GREATER,
            floor_strike=float(market["floor_strike"]),
        )
        got = (
            "yes"
            if module.settles_yes(spec, float(market["expiration_value"]))
            else "no"
        )
        live_inverted += got != str(market["result"]).lower()
    with capsys.disabled():
        print("\nMUTATION KILL COUNTS (`>` weakened to `>=` in settles_yes)")
        print(
            f"  boundary probes  : {len(failures):>3d} killed / "
            f"{len(BOUNDARY_ROWS)} probes across "
            f"{len({f[0] for f in failures})} of {len(GOLDEN_CONTRACTS)} contracts"
        )
        print(
            f"  live published   : {live_inverted:>3d} inverted / {len(live)} "
            f"committed settled markets"
        )
        for ticker, label, value, got, expected in failures:
            print(
                f"    {ticker:26s} {label:13s} value={value:<7g} "
                f"mutant={got} truth={expected}"
            )


# ======================================================================
# Abort-on-ambiguity: GasSpecError, never a silent default
# ======================================================================

BAD_EXTRAS = [
    pytest.param(None, id="extra-is-None"),
    pytest.param({}, id="extra-is-empty"),
    pytest.param({"floor_strike": 3.89}, id="strike_type-missing"),
    pytest.param({"strike_type": None, "floor_strike": 3.89}, id="strike_type-None"),
    pytest.param({"strike_type": "", "floor_strike": 3.89}, id="strike_type-empty"),
    pytest.param({"strike_type": "   ", "floor_strike": 3.89}, id="strike_type-blank"),
    pytest.param({"strike_type": "above", "floor_strike": 3.89}, id="unknown-above"),
    pytest.param(
        {"strike_type": "less", "cap_strike": 3.89}, id="less-unverified-for-gas"
    ),
    pytest.param(
        {"strike_type": "between", "floor_strike": 3.8, "cap_strike": 3.9},
        id="between-unverified-for-gas",
    ),
    pytest.param({"strike_type": "greater"}, id="greater-missing-floor"),
    pytest.param(
        {"strike_type": "greater", "floor_strike": None}, id="greater-floor-None"
    ),
    pytest.param(
        {"strike_type": "greater", "floor_strike": ""}, id="greater-floor-empty"
    ),
    pytest.param(
        {"strike_type": "greater", "floor_strike": "four dollars"},
        id="floor-not-numeric",
    ),
    pytest.param({"strike_type": "greater", "floor_strike": True}, id="floor-is-bool"),
    pytest.param(
        {"strike_type": "greater", "floor_strike": float("nan")}, id="floor-is-nan"
    ),
    pytest.param(
        {"strike_type": "greater", "floor_strike": 3.89, "cap_strike": 4.0},
        id="greater-with-unexpected-cap",
    ),
    pytest.param("not-a-mapping", id="extra-is-a-string"),
]


@pytest.mark.parametrize("extra", BAD_EXTRAS)
def test_parse_gas_spec_aborts_on_ambiguity(extra):
    """Ambiguous semantics must raise, never silently default to a direction."""
    with pytest.raises(GasSpecError):
        parse_gas_spec("KXAAAGASM-26JUN30-3.89", extra)


def test_the_strike_is_never_read_from_the_ticker():
    """PRD FR-1.1: the ticker's last segment looks exactly like the strike.

    ``KXAAAGASM-26JUN30-3.89`` ends in ``3.89``, which is the same number as
    ``floor_strike``. That coincidence is the temptation; this asserts the code
    refuses rather than takes it, so a market whose API fields are missing can
    never settle off its label.
    """
    with pytest.raises(GasSpecError):
        parse_gas_spec("KXAAAGASM-26JUN30-3.89", {"strike_type": "greater"})


def test_the_api_field_wins_when_it_disagrees_with_the_ticker():
    """A synthetic label/field mismatch must settle on the field.

    Live gas markets always have suffix == floor_strike, so this case cannot be
    harvested; it is the FR-1.1 sentence expressed as data. If anything ever
    read the label, this test inverts.
    """
    spec = parse_gas_spec(
        "KXAAAGASM-26JUN30-3.00", {"strike_type": "greater", "floor_strike": 4.50}
    )
    assert spec.floor_strike == 4.50
    assert settles_yes(spec, 3.847) is False  # label would have said YES
    assert settles_yes(spec, 4.501) is True


def test_settles_yes_requires_a_published_value():
    spec = _spec(_golden_by_ticker("KXAAAGASM-26JUN30-3.89"))
    with pytest.raises(GasSpecError):
        settles_yes(spec, None)
    with pytest.raises(GasSpecError):
        settles_yes(spec, float("nan"))
    with pytest.raises(GasSpecError):
        settles_yes(spec, float("inf"))
    with pytest.raises(GasSpecError):
        settles_yes(spec, "expensive")
    with pytest.raises(GasSpecError):
        settles_yes(spec, True)


def test_spec_from_position_round_trips():
    row = _golden_by_ticker("KXAAAGASM-26JUN30-3.89")
    spec = _spec(row)
    position = {"symbol": row["ticker"], "quantity": 3}
    position.update(spec.as_dict())
    rebuilt = spec_from_position(position)
    assert rebuilt.floor_strike == spec.floor_strike
    assert rebuilt.strike_type == spec.strike_type
    assert settles_yes(rebuilt, row["floor_strike"]) is False
    assert settles_yes(rebuilt, row["settled_value"]) is (
        row["settled_result"] == "yes"
    )


def test_verified_strike_types_is_only_greater():
    """A ``less``/``between`` gas rule would be a guess until it is probed."""
    assert VERIFIED_STRIKE_TYPES == (STRIKE_TYPE_GREATER,)


# ======================================================================
# Symbols, series and settlement dates
# ======================================================================
def test_is_gas_symbol_and_series_for():
    assert is_gas_symbol("KXAAAGASM-26JUN30-3.89") is True
    assert is_gas_symbol("KXAAAGASD-26JUL29-4.140") is True
    assert is_gas_symbol("KXHIGHNY-26JUL25-B86.5") is False
    assert is_gas_symbol("") is False
    assert series_for("KXAAAGASM-26JUN30-3.89") == "KXAAAGASM"
    assert series_for("KXAAAGASW-26JUL27-4.110") == "KXAAAGASW"
    assert series_for("KXHIGHNY-26JUL25-T87") is None


def test_a_neighbouring_series_is_not_swallowed_by_a_prefix():
    """``KXAAAGASMAX`` is a different, annual series. Longest prefix must win."""
    assert series_for("KXAAAGASMAX-25DEC31-5.00") is None
    assert is_gas_symbol("KXAAAGASMAX-25DEC31-5.00") is False


def test_settlement_date_for_reads_the_event_label():
    from datetime import date

    assert settlement_date_for("KXAAAGASM-26JUN30-3.89") == date(2026, 6, 30)
    assert settlement_date_for("KXAAAGASD-26MAY24-4.515") == date(2026, 5, 24)
    # A strike segment must never be mistaken for a date label.
    assert settlement_date_for("KXAAAGASM-3.89") is None
    assert settlement_date_for("") is None


def test_event_ticker_round_trips_against_the_live_fixture():
    for market in MONTHLY_MARKETS:
        day = settlement_date_for(market["ticker"])
        assert event_ticker("KXAAAGASM", day) == market["event_ticker"]


def test_month_end_and_monthly_settlement_dates():
    from datetime import date

    assert month_end(date(2026, 2, 10)) == date(2026, 2, 28)
    assert month_end(date(2024, 2, 10)) == date(2024, 2, 29)
    assert month_end(date(2026, 12, 1)) == date(2026, 12, 31)
    # Anchored mid-month, the newest *settled* month-end is the previous one.
    dates = settlement_dates("KXAAAGASM", "2026-07-28", 3)
    assert dates == ["2026-04-30", "2026-05-31", "2026-06-30"], dates
    # Anchored exactly on a month-end, that month-end is in scope.
    assert settlement_dates("KXAAAGASM", "2026-06-30", 1) == ["2026-06-30"]


def test_weekly_and_daily_settlement_dates_follow_their_cadence():
    assert settlement_dates("KXAAAGASD", "2026-07-29", 3) == [
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
    ]
    # Live KXAAAGASW events are Mondays; anchoring on a Tuesday must step back.
    assert settlement_dates("KXAAAGASW", "2026-07-28", 2) == [
        "2026-07-20",
        "2026-07-27",
    ]


def test_the_committed_fixture_dates_are_reachable_from_the_cadence_helper():
    """A cadence bug that silently reconciles the wrong days fails here."""
    fixture_events = {m["event_ticker"] for m in MONTHLY_MARKETS}
    dates = settlement_dates("KXAAAGASM", "2026-07-28", 2)
    assert {event_ticker("KXAAAGASM", d) for d in dates} == fixture_events


# ======================================================================
# Pinning truth from a settled ladder (Kalshi-only truth channel)
# ======================================================================
def test_pin_truth_from_the_live_monthly_ladders():
    pinned = pin_truth_from_settled_markets(MONTHLY_MARKETS)
    assert sorted(pinned) == ["KXAAAGASM-26JUN30", "KXAAAGASM-26MAY31"]
    expected = {
        "KXAAAGASM-26MAY31": (4.33, 4.34, 4.336),
        "KXAAAGASM-26JUN30": (3.84, 3.85, 3.847),
    }
    for event, (low, high, settle) in expected.items():
        result = pinned[event]
        assert result.low_exclusive == pytest.approx(low)
        assert result.high_inclusive == pytest.approx(high)
        assert result.interval_width == pytest.approx(0.01, abs=1e-9)
        assert result.kalshi_expiration_value == pytest.approx(settle)
        # The interval must contain the value Kalshi says it settled on. It is
        # derived from the results alone, so this is a real cross-check.
        assert result.contains(settle) is True
        assert result.contains(low) is False  # low bound is EXCLUSIVE
        assert result.contains(high) is True  # high bound is INCLUSIVE


def test_pin_refuses_a_non_monotonic_ladder():
    """A YES at or above a NO strike is impossible under a strict rule."""
    ladder = [
        {
            "ticker": "X-26JUN30-4.00",
            "strike_type": "greater",
            "floor_strike": 4.00,
            "result": "yes",
            "event_ticker": "X-26JUN30",
        },
        {
            "ticker": "X-26JUN30-3.90",
            "strike_type": "greater",
            "floor_strike": 3.90,
            "result": "no",
            "event_ticker": "X-26JUN30",
        },
    ]
    with pytest.raises(GasSpecError, match="non-monotonic"):
        pin_truth_from_ladder(ladder)


def test_pin_refuses_a_tie_between_yes_and_no_at_the_same_strike():
    ladder = [
        {
            "ticker": "X-26JUN30-4.00a",
            "strike_type": "greater",
            "floor_strike": 4.00,
            "result": "yes",
            "event_ticker": "X-26JUN30",
        },
        {
            "ticker": "X-26JUN30-4.00b",
            "strike_type": "greater",
            "floor_strike": 4.00,
            "result": "no",
            "event_ticker": "X-26JUN30",
        },
    ]
    with pytest.raises(GasSpecError, match="non-monotonic"):
        pin_truth_from_ladder(ladder)


def test_pin_refuses_an_empty_or_unsettled_ladder():
    with pytest.raises(GasSpecError):
        pin_truth_from_ladder([])
    with pytest.raises(GasSpecError):
        pin_truth_from_ladder(
            [
                {
                    "ticker": "KXAAAGASM-26AUG31-4.60",
                    "strike_type": "greater",
                    "floor_strike": 4.60,
                    "result": "",
                    "status": "active",
                    "event_ticker": "KXAAAGASM-26AUG31",
                }
            ]
        )


def test_pin_handles_a_one_sided_ladder_without_inventing_a_bound():
    all_yes = [
        {
            "ticker": "KXAAAGASM-26JUN30-3.00",
            "strike_type": "greater",
            "floor_strike": 3.00,
            "result": "yes",
            "event_ticker": "KXAAAGASM-26JUN30",
        },
        {
            "ticker": "KXAAAGASM-26JUN30-3.10",
            "strike_type": "greater",
            "floor_strike": 3.10,
            "result": "yes",
            "event_ticker": "KXAAAGASM-26JUN30",
        },
    ]
    pinned = pin_truth_from_ladder(all_yes)
    assert pinned.low_exclusive == pytest.approx(3.10)
    assert pinned.high_inclusive is None
    assert pinned.interval_width is None
    assert pinned.midpoint is None
    assert pinned.contains(9.00) is True
    assert pinned.contains(3.10) is False


def test_pin_never_uses_expiration_value_to_set_the_bounds():
    """The interval must come from the results, not from Kalshi's own number.

    If ``expiration_value`` leaked into the bounds, the series would stop being
    an independent truth channel (``circular-constraints-justify-nothing``).
    """
    ladder = [
        {
            "ticker": "X-26JUN30-3.84",
            "strike_type": "greater",
            "floor_strike": 3.84,
            "result": "yes",
            "event_ticker": "X-26JUN30",
            "expiration_value": "9.999",
        },
        {
            "ticker": "X-26JUN30-3.85",
            "strike_type": "greater",
            "floor_strike": 3.85,
            "result": "no",
            "event_ticker": "X-26JUN30",
            "expiration_value": "9.999",
        },
    ]
    pinned = pin_truth_from_ladder(ladder)
    assert pinned.low_exclusive == pytest.approx(3.84)
    assert pinned.high_inclusive == pytest.approx(3.85)
    assert pinned.kalshi_expiration_value == pytest.approx(9.999)
    # ...and the absurd value is reported as NOT contained, rather than the
    # bounds being stretched to accommodate it.
    assert pinned.contains(9.999) is False


# ======================================================================
# The committed pinned-truth series (what WS-D consumes)
# ======================================================================
def _read_pinned_csv():
    import csv

    with open(PINNED_CSV, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_pinned_truth_fixture_is_internally_consistent():
    rows = _read_pinned_csv()
    assert rows, "pinned truth fixture is empty"
    for row in rows:
        low = float(row["value_low_exclusive"])
        high = float(row["value_high_inclusive"])
        assert low < high, row
        assert float(row["interval_width"]) == pytest.approx(high - low, abs=1e-9)
        settle = float(row["kalshi_expiration_value"])
        assert low < settle <= high, row
        assert row["monotonic"] == "true"
        assert row["source"] == "kalshi_settlement"
        assert int(row["n_yes"]) + int(row["n_no"]) == int(row["n_markets"])


def test_pinned_truth_manifest_matches_the_csv_bytes():
    """The manifest's hash must describe the committed file.

    Hashed over LF-normalized bytes so a CRLF checkout cannot red this for a
    file nobody changed (``hash-gated-fixtures-need-eol-lf``;
    ``tests/fixtures/**`` is pinned ``eol=lf`` in .gitattributes, and this is
    belt-and-braces on top of that).
    """
    with open(PINNED_CSV, "rb") as handle:
        raw = handle.read().replace(b"\r\n", b"\n")
    with open(PINNED_MANIFEST, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert hashlib.sha256(raw).hexdigest() == manifest["content_sha256"]
    assert manifest["rows"] == len(_read_pinned_csv())


def test_pinned_truth_states_how_many_month_ends_it_actually_has(capsys):
    """Report the month-end count and interval width, and do not overstate it.

    Phase 4 exit criterion 2 wants >=6 held-out month-ends. Kalshi prunes
    settled markets after roughly two months, so the number of month-ends this
    AAA-independent channel can supply is bounded by that window. This test
    prints the real number rather than asserting a number the API cannot meet.
    """
    rows = _read_pinned_csv()
    monthly = [r for r in rows if r["period_kind"] == "monthly"]
    weekly = [r for r in rows if r["period_kind"] == "weekly"]
    daily = [r for r in rows if r["period_kind"] == "daily"]
    assert monthly, "no month-end was pinned at all"
    with capsys.disabled():
        print("\nKALSHI-PINNED TRUTH (independent of every AAA source)")
        for label, group in (
            ("monthly", monthly),
            ("weekly", weekly),
            ("daily", daily),
        ):
            if not group:
                continue
            widths = [float(r["interval_width"]) for r in group]
            print(
                f"  {label:8s} {len(group):3d} period(s) "
                f"{min(r['settlement_date'] for r in group)}"
                f"..{max(r['settlement_date'] for r in group)}  "
                f"interval width {min(widths):.3f}..{max(widths):.3f}"
            )
        print("  month-ends pinned:")
        for row in monthly:
            print(
                f"    {row['settlement_date']}  "
                f"({row['value_low_exclusive']}, {row['value_high_inclusive']}]  "
                f"width {row['interval_width']}  "
                f"kalshi settle {row['kalshi_expiration_value']}  "
                f"from {row['n_markets']} markets"
            )


def test_daily_pins_cover_both_month_ends_independently():
    """The daily series pins the same month-ends as the monthly series.

    Two disjoint ladders bracketing the same date is a genuinely independent
    cross-check inside the Kalshi-only channel, and it tightens the month-end
    interval from 1.0c to 0.5c.
    """
    rows = _read_pinned_csv()
    by_date = {}
    for row in rows:
        by_date.setdefault(row["settlement_date"], {})[row["series"]] = row
    month_ends = [d for d, per_series in by_date.items() if "KXAAAGASM" in per_series]
    assert month_ends, "no month-end pinned"
    for date in month_ends:
        per_series = by_date[date]
        assert "KXAAAGASD" in per_series, (
            f"{date}: only the monthly ladder pins this month-end, so there is "
            f"no independent second bracket"
        )
        monthly, daily = per_series["KXAAAGASM"], per_series["KXAAAGASD"]
        m_lo, m_hi = (
            float(monthly["value_low_exclusive"]),
            float(monthly["value_high_inclusive"]),
        )
        d_lo, d_hi = (
            float(daily["value_low_exclusive"]),
            float(daily["value_high_inclusive"]),
        )
        # The two intervals must overlap; disjoint brackets would mean one of
        # the two ladders contradicts the other.
        assert max(m_lo, d_lo) < min(m_hi, d_hi), (date, monthly, daily)
        assert float(daily["interval_width"]) <= float(monthly["interval_width"])


# ======================================================================
# AAA series loading (workstream A's file, read-only here)
# ======================================================================
AAA_HEADER = "date,value,source,source_url,fetched_at,raw_sha256,quality"


def _write_aaa(tmp_path, rows, header=AAA_HEADER):
    path = tmp_path / "aaa_daily_national.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return str(path)


def test_absent_aaa_series_is_empty_not_an_error(tmp_path):
    """WS-A writes this file concurrently; absence must not crash a consumer."""
    assert load_aaa_series(str(tmp_path / "nope.csv")) == {}


def test_aaa_series_loads_and_reports_provenance(tmp_path):
    path = _write_aaa(
        tmp_path,
        [
            "2026-06-30,3.847,aaa_wayback,https://web.archive.org/x,"
            "2026-07-29T00:00:00Z,abc,ok",
            "2026-05-31,4.336,aaa_live,https://gasprices.aaa.com/,"
            "2026-07-29T00:00:00Z,def,ok",
        ],
    )
    series = load_aaa_series(path)
    assert set(series) == {"2026-05-31", "2026-06-30"}
    assert isinstance(series["2026-06-30"], AAARow)
    assert series["2026-06-30"].value == pytest.approx(3.847)
    assert series["2026-06-30"].source == "aaa_wayback"
    assert series["2026-05-31"].source_url == "https://gasprices.aaa.com/"


def test_suspect_rows_are_excluded_by_default(tmp_path):
    path = _write_aaa(
        tmp_path,
        [
            "2026-06-30,3.847,aaa_live,u,2026-07-29T00:00:00Z,h,ok",
            "2026-06-29,7.500,aaa_live,u,2026-07-29T00:00:00Z,h,suspect",
        ],
    )
    assert set(load_aaa_series(path)) == {"2026-06-30"}
    assert set(load_aaa_series(path, include_suspect=True)) == {
        "2026-06-29",
        "2026-06-30",
    }


def test_unparseable_and_implausible_values_are_dropped_not_defaulted(tmp_path):
    path = _write_aaa(
        tmp_path,
        [
            "2026-06-30,3.847,aaa_live,u,t,h,ok",
            "2026-06-29,n/a,aaa_live,u,t,h,ok",
            "2026-06-28,0.10,aaa_live,u,t,h,ok",
            "2026-06-27,99.00,aaa_live,u,t,h,ok",
            "not-a-date,4.00,aaa_live,u,t,h,ok",
        ],
    )
    series = load_aaa_series(path)
    assert set(series) == {"2026-06-30"}


def test_a_schema_break_raises_rather_than_reading_zero_rows(tmp_path):
    """Missing columns must not look identical to "AAA published nothing"."""
    path = _write_aaa(tmp_path, ["2026-06-30,3.847"], header="date,value")
    with pytest.raises(GasTruthError):
        load_aaa_series(path)


def test_gaps_stay_gaps(tmp_path):
    """Contract §1.1: a missing day is a missing row. Nothing interpolates."""
    path = _write_aaa(
        tmp_path,
        [
            "2026-06-27,3.878,aaa_live,u,t,h,ok",
            "2026-06-30,3.847,aaa_live,u,t,h,ok",
        ],
    )
    series = load_aaa_series(path)
    assert "2026-06-28" not in series
    assert "2026-06-29" not in series
