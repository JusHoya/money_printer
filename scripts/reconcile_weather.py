#!/usr/bin/env python3
"""reconcile_weather.py -- daily weather settlement reconciliation (PRD FR-1.3).

Phase 1 exit criterion 3: "the settlement cache contains KXHIGH entries for all
tracked cities with CLI values matching the IEM archive exactly, and the
reconcile report shows 0 unexplained mismatches."

This is the weather-era sibling of ``scripts/settlement_reconcile.py`` (the
crypto-era job that caught the 2026-06-10 phantom-PnL bug) and deliberately
mirrors its shape: a text report, a JSON summary, a persisted cache, a
threshold, a Discord alert and a non-zero exit on breach. It reuses that
module's ``sim_recorded_result`` verbatim rather than copying it.

WHAT IT CHECKS
--------------
For each tracked city and date it establishes three independent facts and
cross-checks them:

1. **CLI ground truth** -- the NWS Climatological Report daily maximum, via
   ``IEMCLIProvider`` (IEM primary, NWS CLI product fallback).
2. **Kalshi's published outcome** -- each market's ``result`` field, plus the
   ``expiration_value`` Kalshi itself settled on.
3. **What the simulator believed** -- derived from ``exchange_state.json``
   closed trades and ``trade_journal.jsonl``, where sim positions exist.

The expected outcome is computed from ``src/core/bracket_payoff.settles_yes``
using the market's API ``strike_type`` / ``floor_strike`` / ``cap_strike``.
**The ticker suffix is never parsed** (PRD FR-1.1); a market that arrives
without ``strike_type`` is an unexplained failure, not a guess.

VERIFIED API SHAPES (probed live 2026-07-25)
--------------------------------------------
``GET https://api.elections.kalshi.com/trade-api/v2/events/{event}?with_nested_markets=true``
returns ``{"event": {...}, "markets": [...]}`` -- the per-date view, six
markets for a KXHIGH city/day. ``GET .../markets?series_ticker=...&status=settled``
returns ``{"cursor": ..., "markets": [...]}`` and is used as the fallback path.

Event ticker format is ``{SERIES}-{YY}{MON}{DD}``, e.g. ``KXHIGHNY-26JUL24``.

Each settled market carries::

    status              "finalized"           (terminal; also settled/closed)
    result              "yes" | "no"
    strike_type         "between" | "greater" | "less"
    floor_strike        88        (None for "less")
    cap_strike          81        (None for "greater")
    expiration_value    "83.00"   <- Kalshi's own settlement input
    settlement_value_dollars, settlement_ts, rules_primary, ...

``expiration_value`` matched the IEM CLI high exactly for all twelve city/date
pairs probed on 2026-07-25, which is why a divergence between the two is
treated as an unexplained mismatch: it means our truth source has drifted from
the exchange's.

EXPLAINED vs UNEXPLAINED
------------------------
Only *unexplained* mismatches breach the threshold. Everything else is listed
in the report by category rather than silently dropped:

===================  ==========================================================
Category             Meaning
===================  ==========================================================
``NO_CLI_PRODUCT``   explained -- NWS has not published the day's report yet
``NOT_SETTLED``      explained -- market still open / not yet finalized
``VOIDED``           explained -- Kalshi voided the market
``NO_RESULT``        explained -- terminal status but no published result
``NO_EVENT``         explained -- Kalshi has no event for that city/date
``NO_MARKETS``       UNEXPLAINED -- the event exists but its ladder came back empty
``RESULT_MISMATCH``  UNEXPLAINED -- bracket_payoff disagrees with Kalshi
``TRUTH_MISMATCH``   UNEXPLAINED -- Kalshi's expiration_value != our CLI high
``SIM_MISMATCH``     UNEXPLAINED -- the simulator recorded a different outcome
``SPEC_ERROR``       UNEXPLAINED -- strike_type missing/unusable (never guessed)
===================  ==========================================================

COVERAGE FLOOR -- why "0 unexplained mismatches" is not enough on its own
------------------------------------------------------------------------
Exit criterion 3 is stated in terms of this report, so a run that checks
*nothing* must not be able to report success. Three paths made that possible
and each is now closed:

* a ladder that comes back empty produced zero rows and therefore zero
  mismatches -- now an ``NO_MARKETS`` row, and unexplained;
* a total Kalshi outage produced one explained ``NO_EVENT`` per city-day and
  nothing else -- now caught by the markets floor;
* an unpublished CLI product short-circuits every outcome check in
  :func:`classify_market` (there is no truth to compare against), so a whole
  run of ``NO_CLI_PRODUCT`` rows was "0 unexplained" -- now caught by the
  verified floor.

Two run-level floors therefore gate exit 0, both scaled to the city-days
actually requested (``len(dates) * len(stations)``):

``markets_checked >= --min-markets-per-city-day * city_days``
    the ladders were really fetched. Real KXHIGH ladders carry six markets, so
    the default of 4 still tolerates one of four cities being absent.
``verified >= --min-verified-per-city-day * city_days``
    an outcome was really compared. A row counts as *verified* only when the
    CLI high was published, Kalshi published a yes/no result, and
    ``bracket_payoff`` was run against both.

The floors are aggregates on purpose: one market legitimately unsettled, or one
city's product running late, is ordinary latency and stays explained -- while a
run in which nothing anywhere was checkable fails loudly (exit 3).

The sim-vs-truth leg is reported the same way: the summary always states how
many sim records were loaded and how many matched a reconciled market, so "no
SIM_MISMATCH" can never be silently confused with "the sim leg ran".

USAGE
-----
    python scripts/reconcile_weather.py --date 2026-07-24
    python scripts/reconcile_weather.py --days 3            # last 3 settled days
    python scripts/reconcile_weather.py --stations KNYC,KMDW --json
    python scripts/reconcile_weather.py --offline           # cache only

CRON (mirrors scripts/settlement_reconcile.py):
    # 13:30 UTC daily -- after the morning CLI products are issued.
    30 13 * * * cd /home/USER/money_printer && /usr/bin/python3 \
        scripts/reconcile_weather.py --days 2 \
        >> /home/USER/money_printer/logs/reconcile_weather.log 2>&1

Exit codes:
    0  reconciled, unexplained mismatches within threshold, coverage floors met
    1  unexplained mismatches exceeded threshold
    2  usage / fatal error
    3  coverage floor not met -- the run verified too little to mean anything
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:  # pragma: no cover - requests is a hard project dependency
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

from src.core.bracket_payoff import (  # noqa: E402
    BracketSpecError,
    parse_bracket_spec,
    settles_yes,
)
from src.data.iem_cli_provider import (  # noqa: E402
    WEATHER_TRUTH_DIR,
    IEMCLIError,
    IEMCLIProvider,
    STATIONS,
    get_station,
    normalize_date,
)

logger = logging.getLogger("reconcile_weather")

KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
TERMINAL_STATUSES = {"finalized", "settled", "closed"}
FETCH_TIMEOUT = 20

SETTLEMENT_CACHE_PATH = os.path.join(WEATHER_TRUTH_DIR, "settlement_cache.json")
REPORT_DIR = os.path.join(WEATHER_TRUTH_DIR, "reconcile")

DEFAULT_THRESHOLD = 0  # any unexplained mismatch is a breach

#: Coverage floors, per requested city-day. See "COVERAGE FLOOR" above. Real
#: KXHIGH ladders carry six markets (probed live for all four cities across
#: 2026-07-22..24), so 4 leaves room for one city of four to be missing without
#: paging anyone, while still failing a wholesale outage.
DEFAULT_MIN_MARKETS_PER_CITY_DAY = 4
#: At least one market per city-day must have been compared end to end. This is
#: what separates "one market legitimately unsettled" from "nothing was
#: checkable"; it is deliberately a low bar, because it only has to be
#: unreachable when the run genuinely verified nothing.
DEFAULT_MIN_VERIFIED_PER_CITY_DAY = 1

#: Exit code for a run that reported no mismatches because it checked nothing.
EXIT_COVERAGE = 3

# Locale-independent month abbreviations for the Kalshi event ticker.
_MON = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

# Categories that do NOT count against the threshold.
EXPLAINED_CATEGORIES = {
    "NO_CLI_PRODUCT",
    "NOT_SETTLED",
    "VOIDED",
    "NO_RESULT",
    "NO_EVENT",
}
UNEXPLAINED_CATEGORIES = {
    "RESULT_MISMATCH",
    "TRUTH_MISMATCH",
    "SIM_MISMATCH",
    "SPEC_ERROR",
    "NO_MARKETS",
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Reuse the crypto-era sim-outcome derivation rather than re-deriving it.
# scripts/ is not a package, so it is loaded by path. Keeping one definition of
# "what did the sim believe" means a fix to that logic cannot drift between the
# two reconcile jobs.
# ---------------------------------------------------------------------------
def _load_settlement_reconcile():
    path = os.path.join(_THIS_DIR, "settlement_reconcile.py")
    spec = importlib.util.spec_from_file_location("_settlement_reconcile", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SR = _load_settlement_reconcile()
sim_recorded_result = _SR.sim_recorded_result


# ---------------------------------------------------------------------------
# Kalshi access
# ---------------------------------------------------------------------------
def event_ticker(series_ticker: str, date: Any) -> str:
    """``("KXHIGHNY", "2026-07-24") -> "KXHIGHNY-26JUL24"``."""
    d = datetime.strptime(normalize_date(date), "%Y-%m-%d").date()
    return f"{series_ticker.upper()}-{d.year % 100:02d}{_MON[d.month - 1]}{d.day:02d}"


def fetch_event_markets(
    series_ticker: str, date: Any
) -> Optional[List[Dict[str, Any]]]:
    """Fetch one city/date's KXHIGH ladder from the public Kalshi V2 API.

    Returns the market list, or ``None`` when Kalshi has no such event (or the
    call failed). Network access is isolated here so tests monkeypatch this one
    function and never touch the wire.
    """
    if requests is None:  # pragma: no cover
        logger.error("requests unavailable; cannot reach Kalshi")
        return None
    ticker = event_ticker(series_ticker, date)
    url = f"{KALSHI_PROD_URL}/events/{ticker}"
    try:
        resp = requests.get(
            url,
            params={"with_nested_markets": "true"},
            headers={"Content-Type": "application/json"},
            timeout=FETCH_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Kalshi event fetch failed for %s: %s", ticker, exc)
        return None
    if resp.status_code == 404:
        logger.info("Kalshi has no event %s", ticker)
        return None
    if resp.status_code != 200:
        logger.error("Kalshi event %s returned HTTP %s", ticker, resp.status_code)
        return None
    try:
        payload = resp.json()
    except Exception as exc:
        logger.error("Kalshi event %s returned non-JSON: %s", ticker, exc)
        return None
    # Shape trap (verified 2026-07-25): the response carries BOTH a top-level
    # "markets" key and "event.markets". With ?with_nested_markets=true the
    # top-level list is present but EMPTY and the real six-market ladder lives
    # under event.markets. Keying on presence alone silently reconciles zero
    # markets and reports a clean run, so prefer whichever list is non-empty.
    top = payload.get("markets")
    nested = (payload.get("event") or {}).get("markets")
    for candidate in (nested, top):
        if isinstance(candidate, list) and candidate:
            return candidate
    if isinstance(top, list) or isinstance(nested, list):
        logger.error("Kalshi event %s returned zero markets", ticker)
        return []
    return None


# ---------------------------------------------------------------------------
# Sim ledger
# ---------------------------------------------------------------------------
def load_sim_outcomes() -> Dict[str, Dict[str, Any]]:
    """Map ticker -> what the simulator recorded, from state + journal.

    Read-only: this job never mutates the exchange state or the journal.
    """
    trades: List[Dict[str, Any]] = []
    trades.extend(_SR.load_closed_trades())
    trades.extend(_SR.load_journal_trades())

    out: Dict[str, Dict[str, Any]] = {}
    for trade in trades:
        ticker = trade.get("symbol") or trade.get("ticker")
        if not ticker:
            continue
        outcome = sim_recorded_result(trade)
        if outcome is None:
            continue
        out[str(ticker)] = {
            "sim_result": outcome,
            "strategy": trade.get("strategy_name", "Unknown"),
            "pnl": trade.get("pnl"),
            "contract_side": (trade.get("contract_side") or "YES"),
            "exit_time": trade.get("exit_time") or trade.get("close_time"),
        }
    return out


# ---------------------------------------------------------------------------
# Pure reconciliation core (no I/O -- this is what the tests drive)
# ---------------------------------------------------------------------------
def classify_market(
    market: Mapping[str, Any],
    cli_high: Optional[int],
    sim: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Reconcile one market. Returns a row with ``category`` and ``explained``.

    ``category`` is ``"MATCH"`` on success, otherwise one of the categories in
    the module docstring. ``explained=True`` means the row is reported but does
    not count against the mismatch threshold.
    """
    ticker = str(market.get("ticker") or "<unknown>")
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()

    row: Dict[str, Any] = {
        "ticker": ticker,
        "status": status,
        "kalshi_result": result or None,
        "cli_high": cli_high,
        "strike_type": market.get("strike_type"),
        "floor_strike": market.get("floor_strike"),
        "cap_strike": market.get("cap_strike"),
        "expiration_value": market.get("expiration_value"),
        "expected_result": None,
        "sim_result": (sim or {}).get("sim_result"),
        "category": "MATCH",
        "explained": True,
        "detail": "",
    }

    def fail(category: str, detail: str) -> Dict[str, Any]:
        row["category"] = category
        row["explained"] = category in EXPLAINED_CATEGORIES
        row["detail"] = detail
        return row

    # 1. Bracket semantics come from API fields only (FR-1.1). A market without
    #    strike_type is a hard failure -- inferring direction from the ticker
    #    suffix is the exact defect this rebuild removes.
    try:
        spec = parse_bracket_spec(ticker, market)
    except BracketSpecError as exc:
        return fail("SPEC_ERROR", str(exc))
    row["yes_rule"] = spec.describe()

    # 2. Truth availability.
    if cli_high is None:
        return fail("NO_CLI_PRODUCT", "no CLI daily high published for this date yet")

    # 3. Kalshi's own settlement input must agree with our truth source.
    exp_value = market.get("expiration_value")
    if exp_value not in (None, ""):
        try:
            kalshi_high = float(exp_value)
        except (TypeError, ValueError):
            kalshi_high = None
        if kalshi_high is not None and int(round(kalshi_high)) != int(cli_high):
            return fail(
                "TRUTH_MISMATCH",
                f"Kalshi settled on {kalshi_high:g}F but the CLI report says {cli_high}F",
            )

    # 4. Outcome availability.
    if result in ("void", "voided"):
        return fail("VOIDED", "Kalshi voided this market")
    if result not in ("yes", "no"):
        if status in TERMINAL_STATUSES:
            return fail(
                "NO_RESULT", f"status={status!r} but no yes/no result published"
            )
        return fail("NOT_SETTLED", f"status={status!r}; not yet settled")

    # 5. The actual check: does our payoff module agree with Kalshi?
    expected = "yes" if settles_yes(spec, cli_high) else "no"
    row["expected_result"] = expected
    if expected != result:
        return fail(
            "RESULT_MISMATCH",
            f"bracket_payoff says {expected.upper()} for high={cli_high}F "
            f"({spec.describe()}) but Kalshi settled {result.upper()}",
        )

    # 6. Where a sim position exists, it must agree with Kalshi too.
    if sim and sim.get("sim_result") and sim["sim_result"] != result:
        return fail(
            "SIM_MISMATCH",
            f"sim recorded {str(sim['sim_result']).upper()} but Kalshi settled "
            f"{result.upper()} (strategy={sim.get('strategy')}, pnl={sim.get('pnl')})",
        )

    return row


def reconcile_city(
    station: str,
    date: str,
    markets: Optional[List[Mapping[str, Any]]],
    cli_high: Optional[int],
    sim_outcomes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    truth_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconcile every market in one city's ladder for one date."""
    sim_outcomes = sim_outcomes or {}
    spec = get_station(station, allow_unverified=True)
    summary: Dict[str, Any] = {
        "station": station.upper(),
        "city": spec.city,
        "series_ticker": spec.series_ticker,
        "date": date,
        "event_ticker": event_ticker(spec.series_ticker, date),
        "cli_high": cli_high,
        "truth_source": truth_source,
        "markets_checked": 0,
        "matched": 0,
        # `verified` counts only rows where the outcome check actually ran:
        # truth published, result published, bracket_payoff compared against
        # both. It is the number the coverage floor is built on, because
        # `markets_checked` counts rows that short-circuited too.
        "verified": 0,
        "sim_checked": 0,
        "unexplained": 0,
        "explained": 0,
        "rows": [],
    }

    if not markets:
        if markets is None:
            summary["rows"].append(
                {
                    "ticker": summary["event_ticker"],
                    "category": "NO_EVENT",
                    "explained": True,
                    "detail": "Kalshi has no event for this city/date",
                    "cli_high": cli_high,
                }
            )
            summary["explained"] = 1
        else:
            # An event that exists but yields no markets means the ladder could
            # not be read -- an API-shape change, not a quiet day. Reporting it
            # as explained is how a renamed event ticker would have produced
            # "OK: 0 unexplained mismatches (0 markets checked)" forever.
            summary["rows"].append(
                {
                    "ticker": summary["event_ticker"],
                    "category": "NO_MARKETS",
                    "explained": False,
                    "detail": (
                        "Kalshi returned an empty market list for this event; "
                        "the ladder could not be reconciled"
                    ),
                    "cli_high": cli_high,
                }
            )
            summary["unexplained"] = 1
        return summary

    for market in markets:
        ticker = str(market.get("ticker") or "")
        sim = sim_outcomes.get(ticker)
        row = classify_market(market, cli_high, sim)
        summary["rows"].append(row)
        summary["markets_checked"] += 1
        if row.get("expected_result") is not None:
            summary["verified"] += 1
        if sim and sim.get("sim_result"):
            summary["sim_checked"] += 1
        if row["category"] == "MATCH":
            summary["matched"] += 1
        elif row["explained"]:
            summary["explained"] += 1
        else:
            summary["unexplained"] += 1

    return summary


def aggregate(city_summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll city summaries into the run-level totals used by the report."""
    by_category: Dict[str, int] = {}
    unexplained_rows: List[Dict[str, Any]] = []
    for city in city_summaries:
        for row in city["rows"]:
            cat = row["category"]
            by_category[cat] = by_category.get(cat, 0) + 1
            if not row.get("explained", True):
                unexplained_rows.append(
                    {**row, "station": city["station"], "date": city["date"]}
                )
    return {
        "cities": len(city_summaries),
        "markets_checked": sum(c["markets_checked"] for c in city_summaries),
        "matched": sum(c["matched"] for c in city_summaries),
        "verified": sum(c.get("verified", 0) for c in city_summaries),
        "sim_checked": sum(c.get("sim_checked", 0) for c in city_summaries),
        "city_days_with_truth": sum(
            1 for c in city_summaries if c.get("cli_high") is not None
        ),
        "explained": sum(c["explained"] for c in city_summaries),
        "unexplained": sum(c["unexplained"] for c in city_summaries),
        "by_category": by_category,
        "unexplained_rows": unexplained_rows,
    }


def evaluate_coverage(
    summary: Mapping[str, Any],
    *,
    min_markets_per_city_day: int = DEFAULT_MIN_MARKETS_PER_CITY_DAY,
    min_verified_per_city_day: int = DEFAULT_MIN_VERIFIED_PER_CITY_DAY,
) -> Dict[str, Any]:
    """Did this run verify enough for "0 unexplained mismatches" to mean anything?

    Returns the floors, the observed counts, and a list of human-readable
    failures. An empty ``failures`` list is the only thing that lets the job
    exit 0 -- see "COVERAGE FLOOR" in the module docstring.
    """
    totals = summary["totals"]
    city_days = max(
        1, len(summary.get("dates") or []) * len(summary.get("stations") or [])
    )
    markets_floor = int(min_markets_per_city_day) * city_days
    verified_floor = int(min_verified_per_city_day) * city_days

    failures: List[str] = []
    if totals["markets_checked"] < markets_floor:
        failures.append(
            f"only {totals['markets_checked']} markets were fetched across "
            f"{city_days} city-day(s); the floor is {markets_floor} "
            f"({min_markets_per_city_day}/city-day). Kalshi discovery is broken "
            f"or the exchange is unreachable -- this run verified nothing."
        )
    if totals.get("verified", 0) < verified_floor:
        failures.append(
            f"only {totals.get('verified', 0)} market outcome(s) were actually "
            f"compared against CLI truth across {city_days} city-day(s); the "
            f"floor is {verified_floor} ({min_verified_per_city_day}/city-day). "
            f"Ground truth or published results were absent, so a clean report "
            f"here would mean nothing was checked."
        )
    return {
        "city_days": city_days,
        "markets_floor": markets_floor,
        "verified_floor": verified_floor,
        "markets_checked": totals["markets_checked"],
        "verified": totals.get("verified", 0),
        "ok": not failures,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Settlement cache
# ---------------------------------------------------------------------------
def load_settlement_cache(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or SETTLEMENT_CACHE_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except FileNotFoundError:
        blob = {}
    except Exception as exc:
        logger.error("settlement cache %s unreadable (%s); starting empty", path, exc)
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    blob.setdefault("truth", {})
    blob.setdefault("markets", {})
    return blob


def save_settlement_cache(cache: Mapping[str, Any], path: Optional[str] = None) -> None:
    path = path or SETTLEMENT_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("could not write settlement cache %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Orchestration (I/O)
# ---------------------------------------------------------------------------
def reconcile_dates(
    dates: List[str],
    stations: List[str],
    *,
    provider: Optional[IEMCLIProvider] = None,
    market_fetcher=None,
    sim_outcomes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    cache: Optional[Dict[str, Any]] = None,
    allow_nws_fallback: bool = True,
) -> Dict[str, Any]:
    """Reconcile every (station, date) pair, updating the settlement cache."""
    if market_fetcher is None:
        market_fetcher = fetch_event_markets
    if provider is None:
        provider = IEMCLIProvider(cache_dir=os.path.join(WEATHER_TRUTH_DIR, "cache"))
    if cache is None:
        cache = load_settlement_cache()
    if sim_outcomes is None:
        sim_outcomes = load_sim_outcomes()

    city_summaries: List[Dict[str, Any]] = []
    for date in dates:
        for station in stations:
            spec = get_station(station, allow_unverified=True)

            # 1. Ground truth -> settlement cache.
            cli_high: Optional[int] = None
            truth_source: Optional[str] = None
            try:
                truth = provider.fetch_truth(
                    station, date, allow_nws_fallback=allow_nws_fallback
                )
            except IEMCLIError as exc:
                logger.error("truth lookup failed for %s %s: %s", station, date, exc)
                truth = None
            if truth and truth.get("high") is not None:
                cli_high = int(truth["high"])
                truth_source = str(truth.get("source"))
                cache["truth"][f"{station.upper()}|{date}"] = {
                    "high": cli_high,
                    "source": truth_source,
                    "source_url": truth.get("source_url"),
                    "product": truth.get("product"),
                    "fetched_at": truth.get("fetched_at"),
                }

            # 2. Kalshi ladder.
            markets = market_fetcher(spec.series_ticker, date)

            # 3+4. Compare.
            city = reconcile_city(
                station,
                date,
                markets,
                cli_high,
                sim_outcomes,
                truth_source=truth_source,
            )
            city_summaries.append(city)

            if markets:
                for market in markets:
                    ticker = str(market.get("ticker") or "")
                    if not ticker:
                        continue
                    cache["markets"][ticker] = {
                        "result": (market.get("result") or "").lower() or None,
                        "status": market.get("status"),
                        "expiration_value": market.get("expiration_value"),
                        "strike_type": market.get("strike_type"),
                        "floor_strike": market.get("floor_strike"),
                        "cap_strike": market.get("cap_strike"),
                    }

    totals = aggregate(city_summaries)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dates": dates,
        "stations": [s.upper() for s in stations],
        "cities": city_summaries,
        "totals": totals,
        # How many sim records existed at all, so the report can say plainly
        # that the sim-vs-truth leg had nothing to check rather than implying
        # it passed (PRD FR-1.3; the same silent-success trap as the coverage
        # floor, one layer down).
        "sim_records": len(sim_outcomes),
        "cache": cache,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_report(
    summary: Mapping[str, Any],
    *,
    threshold: int,
    coverage: Optional[Mapping[str, Any]] = None,
) -> str:
    totals = summary["totals"]
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("WEATHER SETTLEMENT RECONCILIATION REPORT (PRD FR-1.3)")
    lines.append("=" * 78)
    lines.append(f"  Generated         : {summary['generated_at']}")
    lines.append(f"  Dates             : {', '.join(summary['dates'])}")
    lines.append(f"  Stations          : {', '.join(summary['stations'])}")
    lines.append(f"  Markets checked   : {totals['markets_checked']}")
    lines.append(f"  Outcomes verified : {totals.get('verified', 0)}")
    lines.append(f"  Matched           : {totals['matched']}")
    lines.append(f"  Explained (no breach) : {totals['explained']}")
    lines.append(
        f"  UNEXPLAINED       : {totals['unexplained']}  (threshold {threshold})"
    )
    lines.append("")
    lines.append("  Per city/date:")
    lines.append(
        f"    {'DATE':11s} {'STN':5s} {'CLI':>4s} {'SRC':9s} "
        f"{'MKTS':>5s} {'VERI':>5s} {'OK':>4s} {'EXPL':>5s} {'BAD':>4s}"
    )
    lines.append("    " + "-" * 66)
    for city in summary["cities"]:
        lines.append(
            f"    {city['date']:11s} {city['station']:5s} "
            f"{str(city['cli_high']) if city['cli_high'] is not None else '--':>4s} "
            f"{str(city['truth_source'] or '--'):9s} "
            f"{city['markets_checked']:5d} {city.get('verified', 0):5d} "
            f"{city['matched']:4d} "
            f"{city['explained']:5d} {city['unexplained']:4d}"
        )

    # Coverage: "0 unexplained" is only evidence if something was verified.
    if coverage is not None:
        lines.append("")
        lines.append("  Coverage floor (a run that checks nothing must not pass):")
        lines.append(
            f"    markets fetched   : {coverage['markets_checked']:4d}  "
            f"(floor {coverage['markets_floor']} over {coverage['city_days']} city-day(s))"
        )
        lines.append(
            f"    outcomes verified : {coverage['verified']:4d}  "
            f"(floor {coverage['verified_floor']})"
        )
        lines.append(f"    COVERAGE          : {'OK' if coverage['ok'] else 'FAILED'}")
        for failure in coverage["failures"]:
            lines.append(f"      ! {failure}")

    # The sim-vs-truth leg, stated even (especially) when it had no work.
    sim_records = summary.get("sim_records")
    sim_checked = totals.get("sim_checked", 0)
    lines.append("")
    if sim_records is None:
        lines.append("  Sim leg           : not evaluated in this run")
    elif sim_checked:
        lines.append(
            f"  Sim leg           : {sim_checked} reconciled market(s) carried a "
            f"sim record (of {sim_records} loaded); each was compared to Kalshi's result"
        )
    else:
        lines.append(
            f"  Sim leg           : NOTHING TO CHECK -- {sim_records} sim record(s) "
            f"loaded, none for a reconciled market. No weather position has ever "
            f"been opened, so SIM_MISMATCH is untested against live data here "
            f"(it is covered by unit tests)."
        )

    by_cat = {k: v for k, v in totals["by_category"].items() if k != "MATCH"}
    if by_cat:
        lines.append("")
        lines.append(
            "  Non-match categories (explained items are listed, never dropped):"
        )
        for cat, n in sorted(by_cat.items()):
            tag = "explained" if cat in EXPLAINED_CATEGORIES else "UNEXPLAINED"
            lines.append(f"    {cat:18s} {n:4d}   [{tag}]")

    # Every explained row spelled out, so silence is always accounted for.
    explained_rows = [
        (c, r)
        for c in summary["cities"]
        for r in c["rows"]
        if r["category"] != "MATCH" and r.get("explained", True)
    ]
    if explained_rows:
        lines.append("")
        lines.append("  Explained detail:")
        for city, row in explained_rows[:30]:
            lines.append(
                f"    {city['date']} {city['station']:5s} {str(row['ticker'])[:30]:30s} "
                f"{row['category']:16s} {row.get('detail', '')[:40]}"
            )
        if len(explained_rows) > 30:
            lines.append(f"    ... and {len(explained_rows) - 30} more")

    bad = totals["unexplained_rows"]
    if bad:
        lines.append("")
        lines.append("  UNEXPLAINED MISMATCHES:")
        for row in bad[:25]:
            lines.append(
                f"    {row.get('date')} {row.get('station'):5s} "
                f"{str(row['ticker'])[:30]:30s} {row['category']}"
            )
            lines.append(f"        {row.get('detail', '')}")
        if len(bad) > 25:
            lines.append(f"    ... and {len(bad) - 25} more")

    lines.append("=" * 78)
    return "\n".join(lines)


def write_report_artifact(
    summary: Mapping[str, Any], report_text: str, report_dir: Optional[str] = None
) -> Dict[str, str]:
    """Persist the dated report artifact (JSON + text). Returns the paths."""
    report_dir = report_dir or REPORT_DIR
    os.makedirs(report_dir, exist_ok=True)
    stamp = summary["dates"][-1] if summary.get("dates") else "unknown"
    base = os.path.join(report_dir, f"reconcile_{stamp}")

    payload = {k: v for k, v in summary.items() if k != "cache"}
    json_path = f"{base}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    text_path = f"{base}.txt"
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(report_text + "\n")
    return {"json": json_path, "text": text_path}


def post_discord_alert(
    summary: Mapping[str, Any],
    threshold: int,
    coverage: Optional[Mapping[str, Any]] = None,
) -> None:
    """Alert on a mismatch breach or a coverage failure (mirrors settlement_reconcile)."""
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        log("DISCORD_WEBHOOK_URL not set - skipping Discord alert")
        return
    if requests is None:  # pragma: no cover
        return
    totals = summary["totals"]
    cats = ", ".join(
        f"{k}={v}"
        for k, v in sorted(totals["by_category"].items())
        if k in UNEXPLAINED_CATEGORIES
    )
    coverage_failed = bool(coverage and not coverage.get("ok", True))
    title = (
        "Weather reconciliation verified nothing (coverage floor)"
        if coverage_failed and totals["unexplained"] <= threshold
        else "Weather settlement reconciliation breach"
    )
    detail = "\n".join(f"- {f}" for f in (coverage or {}).get("failures", []))
    payload = {
        "embeds": [
            {
                "title": title,
                "description": (
                    f"**{totals['unexplained']} unexplained mismatches** "
                    f"(threshold {threshold})\n"
                    f"Dates: {', '.join(summary['dates'])}\n"
                    f"Categories: {cats or 'n/a'}\n"
                    f"Checked {totals['markets_checked']} markets across "
                    f"{totals['cities']} city-days, "
                    f"{totals.get('verified', 0)} outcome(s) verified."
                    + (f"\n{detail}" if detail else "")
                ),
                "color": 0xE74C3C,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        log(
            "Discord alert sent"
            if resp.status_code < 400
            else f"Discord alert failed (HTTP {resp.status_code})"
        )
    except Exception as exc:
        log(f"Discord alert failed: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcile Kalshi weather settlements against NWS CLI ground truth."
    )
    p.add_argument("--date", default=None, help="Date to reconcile (YYYY-MM-DD).")
    p.add_argument(
        "--days",
        type=int,
        default=1,
        help="Reconcile this many consecutive days ending at --date (default 1).",
    )
    p.add_argument(
        "--stations",
        default=",".join(STATIONS),
        help=f"Comma-separated stations (default {','.join(STATIONS)}).",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help="Unexplained mismatches tolerated before exit 1 (default 0).",
    )
    p.add_argument(
        "--min-markets-per-city-day",
        type=int,
        default=DEFAULT_MIN_MARKETS_PER_CITY_DAY,
        help=(
            "Coverage floor: markets that must be fetched per requested "
            f"city-day, else exit {EXIT_COVERAGE} "
            f"(default {DEFAULT_MIN_MARKETS_PER_CITY_DAY}; real ladders carry 6)."
        ),
    )
    p.add_argument(
        "--min-verified-per-city-day",
        type=int,
        default=DEFAULT_MIN_VERIFIED_PER_CITY_DAY,
        help=(
            "Coverage floor: outcomes that must be compared against CLI truth "
            f"per requested city-day, else exit {EXIT_COVERAGE} "
            f"(default {DEFAULT_MIN_VERIFIED_PER_CITY_DAY})."
        ),
    )
    p.add_argument(
        "--report-dir", default=REPORT_DIR, help="Where to write the artifact."
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Use cached truth only; never hit the network.",
    )
    p.add_argument(
        "--no-nws-fallback",
        action="store_true",
        help="Disable the NWS CLI product fallback.",
    )
    p.add_argument(
        "--no-discord", action="store_true", help="Suppress the Discord alert."
    )
    p.add_argument(
        "--json", action="store_true", help="Emit JSON instead of the text report."
    )
    p.add_argument("--quiet", action="store_true", help="Suppress INFO logging.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(override=False)
    except Exception:
        pass

    if args.days < 1:
        log("FATAL: --days must be >= 1")
        return 2

    # Default to yesterday: today's CLI product is not published until the
    # following morning, so reconciling "today" would only ever be NO_CLI_PRODUCT.
    end = normalize_date(
        args.date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    )
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    dates = [
        (end_d - timedelta(days=offset)).isoformat()
        for offset in range(args.days - 1, -1, -1)
    ]

    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]
    if not stations:
        log("FATAL: no stations selected")
        return 2

    provider = IEMCLIProvider(
        cache_dir=os.path.join(WEATHER_TRUTH_DIR, "cache"), offline=args.offline
    )
    summary = reconcile_dates(
        dates,
        stations,
        provider=provider,
        sim_outcomes=load_sim_outcomes(),
        allow_nws_fallback=not (args.no_nws_fallback or args.offline),
    )

    coverage = evaluate_coverage(
        summary,
        min_markets_per_city_day=args.min_markets_per_city_day,
        min_verified_per_city_day=args.min_verified_per_city_day,
    )
    summary["coverage"] = coverage

    save_settlement_cache(summary["cache"])
    report_text = format_report(summary, threshold=args.threshold, coverage=coverage)
    paths = write_report_artifact(summary, report_text, args.report_dir)

    if args.json:
        print(json.dumps({k: v for k, v in summary.items() if k != "cache"}, indent=2))
    else:
        print(report_text)
        log(f"Report: {paths['text']}")
        log(f"Cache : {SETTLEMENT_CACHE_PATH}")

    totals = summary["totals"]
    if totals["unexplained"] > args.threshold:
        log(
            f"BREACH: {totals['unexplained']} unexplained mismatches > "
            f"threshold {args.threshold}"
        )
        if not args.no_discord:
            post_discord_alert(summary, args.threshold, coverage)
        return 1

    # A clean mismatch count is only evidence if the run verified something.
    # Exit criterion 3 is quoted in terms of this report, so a vacuous pass
    # would be accepted as proof -- fail loudly instead.
    if not coverage["ok"]:
        log(
            f"COVERAGE FAILURE: {totals['unexplained']} unexplained mismatches, "
            f"but the run verified too little to mean anything "
            f"({coverage['markets_checked']} markets fetched vs floor "
            f"{coverage['markets_floor']}; {coverage['verified']} outcomes "
            f"verified vs floor {coverage['verified_floor']})"
        )
        for failure in coverage["failures"]:
            log(f"  - {failure}")
        if not args.no_discord:
            post_discord_alert(summary, args.threshold, coverage)
        return EXIT_COVERAGE

    log(
        f"OK: {totals['unexplained']} unexplained mismatches "
        f"({totals['matched']} matched, {totals['explained']} explained, "
        f"{totals['markets_checked']} markets checked, "
        f"{totals['verified']} outcomes verified vs floor "
        f"{coverage['verified_floor']}; sim leg checked "
        f"{totals.get('sim_checked', 0)} of {summary.get('sim_records', 0)} sim records)"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - top-level safety net
        log(f"FATAL: {exc}")
        sys.exit(2)
