"""Offline tests for the weather settlement reconcile job (PRD FR-1.3).

Phase 1 exit criterion 3 requires the reconcile report to show "0 unexplained
mismatches". These tests prove the report can *distinguish* the states that
claim makes meaningful:

  (a) an injected mismatch is flagged,
  (b) a consistent ladder reports zero mismatches,
  (c) an unexplained mismatch exits non-zero,
  (d) an unsettled market is explained, not counted as a mismatch,
  (e) a run that verified nothing -- no ladder, no exchange, or no published
      ground truth -- fails the coverage floor instead of reporting a vacuous
      "0 unexplained mismatches". Without (e) the exit criterion is satisfiable
      by a job that checked nothing at all.

Expected outcomes are computed through ``src.core.bracket_payoff`` rather than
hand-written, so a regression in the between/greater/less semantics -- the
defect that made the historical weather book unusable -- fails these tests too
(see ``test_semantics_regression_is_caught``).

No network: the Kalshi fetcher and the CLI provider are injected.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.bracket_payoff import BracketSpec, settles_yes  # noqa: E402


def _load_reconcile_module():
    """Load scripts/reconcile_weather.py (scripts/ is not a package)."""
    path = os.path.join(REPO_ROOT, "scripts", "reconcile_weather.py")
    spec = importlib.util.spec_from_file_location("reconcile_weather_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rw = _load_reconcile_module()


# ---------------------------------------------------------------------------
# Synthetic ladder -- mirrors the real KXHIGHNY-26JUL24 shape probed 2026-07-25
# ---------------------------------------------------------------------------
CLI_HIGH = 83  # the real KNYC daily high for 2026-07-24


def make_market(
    ticker: str,
    strike_type: str,
    *,
    floor: Optional[float] = None,
    cap: Optional[float] = None,
    result: Optional[str] = None,
    status: str = "finalized",
    expiration_value: Optional[str] = "83.00",
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "result": result,
        "status": status,
        "expiration_value": expiration_value,
    }


def truthful_result(strike_type: str, floor, cap, high: int) -> str:
    """The result Kalshi *should* publish, via the shared payoff module."""
    spec = BracketSpec(
        ticker="x", strike_type=strike_type, floor_strike=floor, cap_strike=cap
    )
    return "yes" if settles_yes(spec, high) else "no"


#: The six-market ladder Kalshi actually published for NYC on 2026-07-24.
LADDER = [
    ("KXHIGHNY-26JUL24-T81", "less", None, 81),
    ("KXHIGHNY-26JUL24-B81.5", "between", 81, 82),
    ("KXHIGHNY-26JUL24-B83.5", "between", 83, 84),
    ("KXHIGHNY-26JUL24-B85.5", "between", 85, 86),
    ("KXHIGHNY-26JUL24-B87.5", "between", 87, 88),
    ("KXHIGHNY-26JUL24-T88", "greater", 88, None),
]


def consistent_ladder(high: int = CLI_HIGH) -> List[Dict[str, Any]]:
    """A ladder whose published results all agree with bracket_payoff."""
    return [
        make_market(
            ticker,
            st,
            floor=floor,
            cap=cap,
            result=truthful_result(st, floor, cap, high),
            expiration_value=f"{high}.00",
        )
        for ticker, st, floor, cap in LADDER
    ]


# ---------------------------------------------------------------------------
# Sanity: the synthetic ladder encodes the live-verified semantics
# ---------------------------------------------------------------------------
def test_synthetic_ladder_reproduces_the_live_kalshi_outcomes():
    """At 83F only B83.5 pays YES -- exactly what Kalshi published."""
    results = {m["ticker"]: m["result"] for m in consistent_ladder()}
    assert results == {
        "KXHIGHNY-26JUL24-T81": "no",  # less, cap 81 -> YES only <= 80
        "KXHIGHNY-26JUL24-B81.5": "no",
        "KXHIGHNY-26JUL24-B83.5": "yes",  # between 83..84
        "KXHIGHNY-26JUL24-B85.5": "no",
        "KXHIGHNY-26JUL24-B87.5": "no",
        "KXHIGHNY-26JUL24-T88": "no",  # greater, floor 88 -> YES only >= 89
    }


# ---------------------------------------------------------------------------
# (b) consistent set -> zero mismatches
# ---------------------------------------------------------------------------
def test_consistent_ladder_reports_zero_mismatches():
    summary = rw.reconcile_city("KNYC", "2026-07-24", consistent_ladder(), CLI_HIGH)
    assert summary["markets_checked"] == 6
    assert summary["matched"] == 6
    assert summary["unexplained"] == 0
    assert summary["explained"] == 0
    assert all(row["category"] == "MATCH" for row in summary["rows"])


def test_consistent_ladder_at_a_boundary_temperature():
    """cap edge: at 84F the 83-84 bracket still pays, 85-86 does not."""
    summary = rw.reconcile_city("KNYC", "2026-07-24", consistent_ladder(84), 84)
    assert summary["unexplained"] == 0
    by_ticker = {r["ticker"]: r["expected_result"] for r in summary["rows"]}
    assert by_ticker["KXHIGHNY-26JUL24-B83.5"] == "yes"
    assert by_ticker["KXHIGHNY-26JUL24-B85.5"] == "no"


# ---------------------------------------------------------------------------
# (a) injected mismatch -> flagged
# ---------------------------------------------------------------------------
def test_injected_result_mismatch_is_flagged():
    markets = consistent_ladder()
    # Flip the one YES to NO: Kalshi now disagrees with bracket_payoff.
    victim = next(m for m in markets if m["ticker"] == "KXHIGHNY-26JUL24-B83.5")
    victim["result"] = "no"

    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)
    assert summary["unexplained"] == 1
    bad = [r for r in summary["rows"] if not r["explained"]]
    assert bad[0]["category"] == "RESULT_MISMATCH"
    assert bad[0]["expected_result"] == "yes"
    assert bad[0]["kalshi_result"] == "no"
    assert "83" in bad[0]["detail"]


def test_truth_value_mismatch_is_flagged():
    """Kalshi settling on a different high than the CLI report is a breach."""
    markets = consistent_ladder()
    for m in markets:
        m["expiration_value"] = "85.00"  # CLI says 83

    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)
    assert summary["unexplained"] == 6
    assert all(r["category"] == "TRUTH_MISMATCH" for r in summary["rows"])
    assert "85" in summary["rows"][0]["detail"] and "83" in summary["rows"][0]["detail"]


def test_sim_mismatch_is_flagged():
    """The phantom-PnL guard: the sim recorded an outcome Kalshi did not.

    This drives the real six-market ladder with published truth, so the
    contradicting sim record is reached only after the payoff comparison has
    already agreed with Kalshi -- i.e. the SIM leg is genuinely exercised and
    not standing in for an earlier failure. No KXHIGH position has ever existed
    in the live ledger (487 sim tickers, 0 KXHIGH as of 2026-07-25), so this is
    the only place the leg is proven; the report says so explicitly rather than
    implying the live run checked it.
    """
    markets = consistent_ladder()
    sim = {
        "KXHIGHNY-26JUL24-B83.5": {
            "sim_result": "no",  # sim lost it; Kalshi settled YES
            "strategy": "WeatherFarBracket",
            "pnl": -12.5,
        }
    }
    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH, sim)
    bad = [r for r in summary["rows"] if not r["explained"]]
    assert len(bad) == 1
    assert bad[0]["category"] == "SIM_MISMATCH"
    assert "WeatherFarBracket" in bad[0]["detail"]
    # The comparison ran on a fully verified row, and the sim leg is counted.
    assert bad[0]["expected_result"] == "yes"
    assert bad[0]["kalshi_result"] == "yes"
    assert summary["sim_checked"] == 1
    assert summary["verified"] == 6


def test_report_states_plainly_when_the_sim_leg_had_nothing_to_check():
    """FR-1.3 / Finding D: silence about the sim leg is the Finding C trap again."""
    city = rw.reconcile_city("KNYC", "2026-07-24", consistent_ladder(), CLI_HIGH)
    summary = {
        "generated_at": "2026-07-25T00:00:00Z",
        "dates": ["2026-07-24"],
        "stations": ["KNYC"],
        "cities": [city],
        "totals": rw.aggregate([city]),
        "sim_records": 487,
    }
    text = rw.format_report(summary, threshold=0)
    assert "Sim leg" in text
    assert "NOTHING TO CHECK" in text
    assert "487" in text

    with_sim = rw.reconcile_city(
        "KNYC",
        "2026-07-24",
        consistent_ladder(),
        CLI_HIGH,
        {"KXHIGHNY-26JUL24-B83.5": {"sim_result": "yes", "strategy": "S", "pnl": 1.0}},
    )
    text2 = rw.format_report(
        dict(
            summary, cities=[with_sim], totals=rw.aggregate([with_sim]), sim_records=1
        ),
        threshold=0,
    )
    assert "NOTHING TO CHECK" not in text2
    assert "1 reconciled market(s) carried a sim record" in text2


def test_agreeing_sim_outcome_is_not_a_mismatch():
    markets = consistent_ladder()
    sim = {
        "KXHIGHNY-26JUL24-B83.5": {
            "sim_result": "yes",
            "strategy": "WeatherFarBracket",
            "pnl": 40.0,
        }
    }
    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH, sim)
    assert summary["unexplained"] == 0
    assert summary["matched"] == 6


def test_missing_strike_type_is_unexplained_never_inferred():
    """FR-1.1: direction is never inferred from the ticker suffix."""
    markets = consistent_ladder()
    markets[0].pop("strike_type")
    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)
    bad = [r for r in summary["rows"] if not r["explained"]]
    assert len(bad) == 1
    assert bad[0]["category"] == "SPEC_ERROR"
    assert bad[0]["category"] in rw.UNEXPLAINED_CATEGORIES


# ---------------------------------------------------------------------------
# (d) explained categories
# ---------------------------------------------------------------------------
def test_unsettled_market_is_explained_not_a_mismatch():
    markets = consistent_ladder()
    markets[2].update({"status": "active", "result": None})
    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)

    assert summary["unexplained"] == 0
    assert summary["explained"] == 1
    row = next(r for r in summary["rows"] if r["category"] != "MATCH")
    assert row["category"] == "NOT_SETTLED"
    assert row["explained"] is True
    assert row["category"] in rw.EXPLAINED_CATEGORIES


def test_missing_cli_product_is_explained():
    """Before the morning product is issued there is no truth to compare to."""
    summary = rw.reconcile_city("KNYC", "2026-07-25", consistent_ladder(), None)
    assert summary["unexplained"] == 0
    assert summary["explained"] == 6
    assert all(r["category"] == "NO_CLI_PRODUCT" for r in summary["rows"])


def test_voided_market_is_explained():
    markets = consistent_ladder()
    markets[1]["result"] = "void"
    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)
    assert summary["unexplained"] == 0
    assert any(r["category"] == "VOIDED" for r in summary["rows"])


def test_terminal_status_without_a_result_is_explained_but_reported():
    markets = consistent_ladder()
    markets[3].update({"status": "settled", "result": ""})
    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)
    row = next(r for r in summary["rows"] if r["category"] != "MATCH")
    assert row["category"] == "NO_RESULT"
    assert row["explained"] is True
    assert summary["unexplained"] == 0


def test_absent_event_is_explained():
    summary = rw.reconcile_city("KNYC", "2026-07-24", None, CLI_HIGH)
    assert summary["unexplained"] == 0
    assert summary["rows"][0]["category"] == "NO_EVENT"
    assert summary["markets_checked"] == 0
    assert summary["verified"] == 0


def test_empty_ladder_is_unexplained_not_silent():
    """An event that yields no markets is a discovery failure, not a quiet day."""
    summary = rw.reconcile_city("KNYC", "2026-07-24", [], CLI_HIGH)
    assert summary["unexplained"] == 1
    assert summary["rows"][0]["category"] == "NO_MARKETS"
    assert summary["rows"][0]["explained"] is False
    assert "NO_MARKETS" in rw.UNEXPLAINED_CATEGORIES


def test_verified_counts_only_rows_whose_outcome_was_compared():
    """``markets_checked`` counts rows; ``verified`` counts real comparisons."""
    full = rw.reconcile_city("KNYC", "2026-07-24", consistent_ladder(), CLI_HIGH)
    assert full["markets_checked"] == 6 and full["verified"] == 6

    # No published truth -> every row short-circuits before the outcome check.
    no_truth = rw.reconcile_city("KNYC", "2026-07-25", consistent_ladder(), None)
    assert no_truth["markets_checked"] == 6 and no_truth["verified"] == 0

    # One market still open -> five comparisons actually ran.
    partial = consistent_ladder()
    partial[2].update({"status": "active", "result": None})
    mixed = rw.reconcile_city("KNYC", "2026-07-24", partial, CLI_HIGH)
    assert mixed["markets_checked"] == 6 and mixed["verified"] == 5


def test_explained_and_unexplained_category_sets_are_disjoint():
    assert not (rw.EXPLAINED_CATEGORIES & rw.UNEXPLAINED_CATEGORIES)


# ---------------------------------------------------------------------------
# Semantics regression guard
# ---------------------------------------------------------------------------
def legacy_result(strike_type, floor, cap, high: int) -> str:
    """The outcome the DELETED suffix-letter parser would have published.

    Its documented defect is an off-by-one on both one-sided types: it treated
    ``greater`` as ``high >= floor`` (truth: ``>= floor + 1``) and ``less`` as
    ``high <= cap`` (truth: ``<= cap - 1``).
    """
    if strike_type == "greater":
        return "yes" if high >= floor else "no"
    if strike_type == "less":
        return "yes" if high <= cap else "no"
    return truthful_result(strike_type, floor, cap, high)


# Each off-by-one is only observable at its own boundary temperature: at the
# floor of a `greater` contract, and at the cap of a `less` one. A single
# temperature cannot expose both, so both are exercised.
@pytest.mark.parametrize(
    "high, exposed_ticker",
    [
        (88, "KXHIGHNY-26JUL24-T88"),  # greater, floor 88: legacy YES, truth NO
        (81, "KXHIGHNY-26JUL24-T81"),  # less,    cap 81:   legacy YES, truth NO
    ],
)
def test_semantics_regression_is_caught(high, exposed_ticker):
    """Mutation test: results published under the legacy rule must be flagged.

    If this reconcile ever stopped deriving expectations from
    ``bracket_payoff``, it would rubber-stamp the very defect it exists to
    detect. Proving the gate can fail is what makes its green runs meaningful.
    """
    markets = [
        make_market(
            ticker,
            st,
            floor=floor,
            cap=cap,
            result=legacy_result(st, floor, cap, high),
            expiration_value=f"{high}.00",
        )
        for ticker, st, floor, cap in LADDER
    ]

    summary = rw.reconcile_city("KNYC", "2026-07-24", markets, high)
    bad = {r["ticker"] for r in summary["rows"] if r["category"] == "RESULT_MISMATCH"}
    assert exposed_ticker in bad, f"legacy off-by-one at {high}F went undetected"

    # Control: with truthful results at the same temperature, nothing is flagged.
    clean = rw.reconcile_city("KNYC", "2026-07-24", consistent_ladder(high), high)
    assert clean["unexplained"] == 0


# ---------------------------------------------------------------------------
# Aggregation, event tickers, report
# ---------------------------------------------------------------------------
def test_event_ticker_format_matches_the_live_api():
    assert rw.event_ticker("KXHIGHNY", "2026-07-24") == "KXHIGHNY-26JUL24"
    assert rw.event_ticker("KXHIGHCHI", "2026-07-04") == "KXHIGHCHI-26JUL04"
    assert rw.event_ticker("KXHIGHLAX", "2026-01-01") == "KXHIGHLAX-26JAN01"
    assert rw.event_ticker("kxhighmia", "2025-12-31") == "KXHIGHMIA-25DEC31"


def test_aggregate_totals_across_cities():
    good = rw.reconcile_city("KNYC", "2026-07-24", consistent_ladder(), CLI_HIGH)
    broken_markets = consistent_ladder()
    broken_markets[2]["result"] = "no"
    bad = rw.reconcile_city("KMDW", "2026-07-24", broken_markets, CLI_HIGH)

    totals = rw.aggregate([good, bad])
    assert totals["cities"] == 2
    assert totals["markets_checked"] == 12
    assert totals["unexplained"] == 1
    assert totals["by_category"]["RESULT_MISMATCH"] == 1
    assert totals["unexplained_rows"][0]["station"] == "KMDW"


def test_report_lists_explained_rows_rather_than_dropping_them():
    markets = consistent_ladder()
    markets[2].update({"status": "active", "result": None})
    city = rw.reconcile_city("KNYC", "2026-07-24", markets, CLI_HIGH)
    summary = {
        "generated_at": "2026-07-25T00:00:00Z",
        "dates": ["2026-07-24"],
        "stations": ["KNYC"],
        "cities": [city],
        "totals": rw.aggregate([city]),
    }
    text = rw.format_report(summary, threshold=0)
    assert "NOT_SETTLED" in text
    assert "Explained detail" in text
    assert "UNEXPLAINED       : 0" in text


# ---------------------------------------------------------------------------
# (c) exit codes, end to end, no network
# ---------------------------------------------------------------------------
class StubProvider:
    """Stands in for IEMCLIProvider; returns a fixed CLI high per station."""

    def __init__(self, highs: Dict[str, Optional[int]]):
        self.highs = highs

    def fetch_truth(self, station, date, allow_nws_fallback=True, **kwargs):
        high = self.highs.get(station.upper())
        if high is None:
            return None
        return {
            "station": station.upper(),
            "date": date,
            "high": high,
            "source": "iem_cli",
            "source_url": "https://fake/cli",
            "product": "fake-product",
            "fetched_at": "2026-07-25T00:00:00Z",
        }


@pytest.fixture()
def isolated_outputs(tmp_path, monkeypatch):
    """Redirect the cache and report artifacts into tmp_path."""
    monkeypatch.setattr(
        rw, "SETTLEMENT_CACHE_PATH", str(tmp_path / "settlement_cache.json")
    )
    monkeypatch.setattr(rw, "REPORT_DIR", str(tmp_path / "reconcile"))
    monkeypatch.setattr(rw, "load_sim_outcomes", lambda: {})
    return tmp_path


def _run_main(monkeypatch, isolated_outputs, markets_by_series, highs, extra_argv=()):
    monkeypatch.setattr(
        rw, "fetch_event_markets", lambda series, date: markets_by_series.get(series)
    )
    monkeypatch.setattr(rw, "IEMCLIProvider", lambda *a, **k: StubProvider(highs))
    argv = [
        "--date",
        "2026-07-24",
        "--stations",
        "KNYC",
        "--report-dir",
        str(isolated_outputs / "reconcile"),
        "--no-discord",
        "--quiet",
        *extra_argv,
    ]
    return rw.main(argv)


def test_main_exits_zero_on_a_consistent_run(monkeypatch, isolated_outputs, capsys):
    code = _run_main(
        monkeypatch,
        isolated_outputs,
        {"KXHIGHNY": consistent_ladder()},
        {"KNYC": CLI_HIGH},
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "UNEXPLAINED       : 0" in out

    # The settlement cache records the CLI truth with its provenance.
    with open(isolated_outputs / "settlement_cache.json", "r", encoding="utf-8") as fh:
        cache = json.load(fh)
    assert cache["truth"]["KNYC|2026-07-24"]["high"] == CLI_HIGH
    assert cache["truth"]["KNYC|2026-07-24"]["source"] == "iem_cli"
    assert cache["markets"]["KXHIGHNY-26JUL24-B83.5"]["result"] == "yes"

    # And a dated artifact exists.
    assert (isolated_outputs / "reconcile" / "reconcile_2026-07-24.json").exists()
    assert (isolated_outputs / "reconcile" / "reconcile_2026-07-24.txt").exists()


def test_main_exits_non_zero_on_an_unexplained_mismatch(
    monkeypatch, isolated_outputs, capsys
):
    markets = consistent_ladder()
    markets[2]["result"] = "no"  # inject the mismatch
    code = _run_main(
        monkeypatch, isolated_outputs, {"KXHIGHNY": markets}, {"KNYC": CLI_HIGH}
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "UNEXPLAINED MISMATCHES" in out
    assert "RESULT_MISMATCH" in out

    with open(
        isolated_outputs / "reconcile" / "reconcile_2026-07-24.json",
        "r",
        encoding="utf-8",
    ) as fh:
        artifact = json.load(fh)
    assert artifact["totals"]["unexplained"] == 1


def test_one_legitimately_unsettled_market_still_exits_zero(
    monkeypatch, isolated_outputs, capsys
):
    """The distinction the coverage floor has to preserve.

    *One* market still open is ordinary latency: five outcomes were verified,
    so the run is meaningful and the explained row is reported, not counted.
    Contrast with the two tests below, where nothing was checkable at all.
    """
    markets = consistent_ladder()
    markets[2].update({"status": "active", "result": None})
    code = _run_main(
        monkeypatch, isolated_outputs, {"KXHIGHNY": markets}, {"KNYC": CLI_HIGH}
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "NOT_SETTLED" in out
    assert "Outcomes verified : 5" in out
    assert "COVERAGE          : OK" in out


def test_wholly_unsettled_ladder_fails_the_coverage_floor(
    monkeypatch, isolated_outputs, capsys
):
    """CHANGED ASSERTION (was: exit 0).

    This case used to be asserted as a pass under the heading "an unsettled
    ladder ... is not a breach". It is not a breach -- but it is also not a
    reconciliation: no outcome was compared against anything, so reporting
    "0 unexplained mismatches" as evidence for Phase 1 exit criterion 3 would
    be a vacuous pass. The run now fails the verified floor.
    """
    markets = consistent_ladder()
    for m in markets:
        m.update({"status": "active", "result": None})
    code = _run_main(
        monkeypatch, isolated_outputs, {"KXHIGHNY": markets}, {"KNYC": CLI_HIGH}
    )
    out = capsys.readouterr().out
    assert code == rw.EXIT_COVERAGE
    assert "NOT_SETTLED" in out
    assert "COVERAGE          : FAILED" in out
    assert "Outcomes verified : 0" in out


def test_absent_cli_product_everywhere_fails_the_coverage_floor(
    monkeypatch, isolated_outputs, capsys
):
    """CHANGED ASSERTION (was: exit 0) -- Finding C, path 3.

    ``classify_market`` short-circuits at ``cli_high is None`` before the
    outcome check, so a run with no ground truth anywhere scores every market
    ``NO_CLI_PRODUCT`` and zero unexplained mismatches. A verifier showed the
    *identical flipped ladder* that scores exit 1 with truth present scored
    exit 0 with truth absent. Both halves of that pair are asserted here.
    """
    flipped = consistent_ladder()
    flipped[2]["result"] = "no"  # the mismatch that must be caught

    with_truth = _run_main(
        monkeypatch, isolated_outputs, {"KXHIGHNY": flipped}, {"KNYC": CLI_HIGH}
    )
    capsys.readouterr()
    assert with_truth == 1  # caught, because truth was published

    without_truth = _run_main(
        monkeypatch, isolated_outputs, {"KXHIGHNY": consistent_ladder()}, {"KNYC": None}
    )
    out = capsys.readouterr().out
    assert (
        without_truth == rw.EXIT_COVERAGE
    ), "a run with no ground truth verified nothing and must not report success"
    assert "NO_CLI_PRODUCT" in out
    assert "COVERAGE          : FAILED" in out


def test_zero_market_ladder_fails(monkeypatch, isolated_outputs, capsys):
    """Finding C, path 1: an event whose market list comes back empty.

    If Kalshi renames the event-ticker format, ``fetch_event_markets`` returns
    ``[]`` and the old job looped zero times: "OK: 0 unexplained mismatches
    (0 markets checked)", forever, on the nightly cron. The ``logger.error``
    it emitted never reached the exit code.
    """
    code = _run_main(
        monkeypatch, isolated_outputs, {"KXHIGHNY": []}, {"KNYC": CLI_HIGH}
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "NO_MARKETS" in out


def test_total_kalshi_outage_fails(monkeypatch, isolated_outputs, capsys):
    """Finding C, path 2: every fetch returns None (network down / API moved).

    Four explained ``NO_EVENT`` rows used to be indistinguishable from a green
    run. The markets floor now separates them.
    """
    code = _run_main(monkeypatch, isolated_outputs, {}, {"KNYC": CLI_HIGH})
    out = capsys.readouterr().out
    assert code == rw.EXIT_COVERAGE
    assert "NO_EVENT" in out
    assert "COVERAGE          : FAILED" in out


def test_coverage_floors_scale_with_the_request():
    """The floors are per requested city-day, not a fixed number."""
    summary = {
        "dates": ["2026-07-23", "2026-07-24"],
        "stations": ["KNYC", "KMDW"],
        "totals": {"markets_checked": 24, "verified": 24},
    }
    cov = rw.evaluate_coverage(summary)
    assert cov["city_days"] == 4
    assert cov["markets_floor"] == 4 * rw.DEFAULT_MIN_MARKETS_PER_CITY_DAY
    assert cov["verified_floor"] == 4 * rw.DEFAULT_MIN_VERIFIED_PER_CITY_DAY
    assert cov["ok"] and not cov["failures"]

    thin = dict(summary, totals={"markets_checked": 6, "verified": 6})
    assert not rw.evaluate_coverage(thin)["ok"]

    unverified = dict(summary, totals={"markets_checked": 24, "verified": 0})
    failures = rw.evaluate_coverage(unverified)["failures"]
    assert len(failures) == 1 and "compared against CLI truth" in failures[0]


def test_one_missing_city_of_four_still_clears_the_markets_floor():
    """Tolerance check: the floor must not page on ordinary partial coverage."""
    summary = {
        "dates": ["2026-07-24"],
        "stations": ["KNYC", "KMDW", "KLAX", "KMIA"],
        "totals": {"markets_checked": 18, "verified": 18},  # 3 of 4 ladders
    }
    assert rw.evaluate_coverage(summary)["ok"]


def test_threshold_can_tolerate_a_known_mismatch(monkeypatch, isolated_outputs, capsys):
    markets = consistent_ladder()
    markets[2]["result"] = "no"
    code = _run_main(
        monkeypatch,
        isolated_outputs,
        {"KXHIGHNY": markets},
        {"KNYC": CLI_HIGH},
        extra_argv=("--threshold", "1"),
    )
    capsys.readouterr()
    assert code == 0


def test_main_spans_multiple_days(monkeypatch, isolated_outputs, capsys):
    code = _run_main(
        monkeypatch,
        isolated_outputs,
        {"KXHIGHNY": consistent_ladder()},
        {"KNYC": CLI_HIGH},
        extra_argv=("--days", "3"),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "2026-07-22, 2026-07-23, 2026-07-24" in out
    assert "Markets checked   : 18" in out
