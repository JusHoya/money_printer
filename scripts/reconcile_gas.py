#!/usr/bin/env python3
"""reconcile_gas.py -- AAA gas settlement reconciliation (PRD FR-4.4).

Phase 4 exit criterion 3, second half: *"settlement reconcile vs the published
AAA value shows 0 mismatches."*

This is the gas sibling of ``scripts/reconcile_weather.py`` (FR-1.3) and
deliberately mirrors its shape: a text report, a JSON summary, a persisted
settlement cache, a mismatch threshold of 0, coverage floors, a Discord alert
and a non-zero exit on breach. It reuses ``settlement_reconcile.sim_recorded_result``
verbatim rather than re-deriving "what did the sim believe".

WHAT IT CHECKS -- FOUR INDEPENDENT LEGS
---------------------------------------
For each requested settlement date it establishes facts from up to three
independent places and cross-checks them:

1. **AAA published value** -- the settlement authority, read from workstream A's
   ``data/gas_truth/aaa_daily_national.csv`` and from nowhere else. The job
   *writes* the FR-4.4 settlement cache but never reads it back as truth: the
   cache is this job's own output, so trusting it would make the reconcile
   validate against itself and would shadow any later correction to the AAA
   series. (The runtime resolver in ``src.data.gas_settlement`` does read the
   cache first -- that is the right place for it, exactly as weather does.)
2. **Kalshi's published outcome** -- each market's ``result``, plus the
   ``expiration_value`` Kalshi itself settled on.
3. **What the simulator believed** -- ``exchange_state.json`` closed trades and
   ``trade_journal.jsonl``, where sim positions exist.

===================  =====================================================
Leg                  Question, and what a failure means
===================  =====================================================
semantics            ``settles_yes(spec, kalshi_expiration_value)`` must
                     reproduce Kalshi's own ``result`` for every settled
                     market. Needs **no** external data, so it runs even
                     before the AAA series exists. A failure means our
                     payoff module disagrees with the exchange.
truth                Kalshi's ``expiration_value`` must equal our recorded
                     AAA value. A failure means our truth source drifted
                     from the exchange's.
outcome              ``settles_yes(spec, our AAA value)`` must reproduce
                     ``result``. This is the leg exit criterion 3 is
                     stated in terms of, and it needs the AAA series.
sim                  A sim position's recorded outcome must equal Kalshi's.
===================  =====================================================

The expected outcome is always computed from
``src.data.gas_settlement.settles_yes`` using the market's API ``strike_type``
and ``floor_strike``. **The ticker is never parsed for direction or for the
strike magnitude** (PRD FR-1.1); a market that arrives without ``strike_type``
is an unexplained failure, not a guess.

STRICTLY GREATER
----------------
YES pays iff ``value > floor_strike``; a settle exactly equal to the strike
pays NO. That is not read off the rule text alone: 15 of the 1,506 settled AAA
gas markets the API returned on 2026-07-29 settled with
``expiration_value == floor_strike`` exactly and every one settled NO. See
``src/data/gas_settlement`` and ``tests/test_gas_semantics.py``.

VERIFIED API SHAPES (probed live 2026-07-29, anonymous read)
-----------------------------------------------------------
``GET /events/{event}?with_nested_markets=true`` returns ``{"event": {...},
"markets": [...]}``. As with the weather event endpoint the top-level
``markets`` list is present but EMPTY and the real ladder lives under
``event.markets``; keying on presence alone silently reconciles zero markets
and reports a clean run, so whichever list is non-empty is preferred.

Event ticker format is ``{SERIES}-{YY}{MON}{DD}``, e.g. ``KXAAAGASM-26JUN30``.

``status=settled`` is the *query* value; the markets come back with
``status="finalized"``. Filtering the response on ``"settled"`` yields nothing.

**Settled markets are pruned.** On 2026-07-29 the events endpoint listed 33
monthly events back to 2023-12-31, but only ``26MAY31`` and ``26JUN30`` still
carried markets. Reconciling a month-end older than roughly two months is
therefore permanently impossible, which is why ``--harvest-truth`` persists the
pinned intervals as a committed fixture the moment they are retrievable. That
external retention limit is reported as its own explained category (``PRUNED``,
see :data:`RETENTION_DAYS`) rather than as an unexplained empty ladder, so a
cron is never paged about Kalshi's retention policy.

EXPLAINED vs UNEXPLAINED
------------------------
Only *unexplained* mismatches breach the threshold. Everything else is listed
in the report by category rather than silently dropped:

=====================  ====================================================
Category               Meaning
=====================  ====================================================
``NO_AAA_VALUE``       explained -- AAA has not published this date yet, or
                       the day was never harvested
``NOT_SETTLED``        explained -- market still open / not yet finalized
``VOIDED``             explained -- Kalshi voided the market
``NO_RESULT``          explained -- terminal status but no published result
``NO_EVENT``           explained -- Kalshi has no event for that date, and the
                       date is recent enough that it should still have one
``PRUNED``             explained -- the settlement date predates Kalshi's
                       settled-market retention window, so its ladder is
                       permanently unretrievable. An external limit, not a
                       defect; the period is excluded from the coverage floors
                       and the exclusion is counted on every run
``TRUTH_EXCEPTION``    explained -- a *registered, dated* disagreement between
                       Kalshi's settlement input and our AAA record that is
                       provably immaterial to this market's outcome. See
                       "TRUTH EXCEPTIONS"; every other leg still had to pass
``NO_MARKETS``         UNEXPLAINED -- the event exists, its ladder was empty,
                       and it is inside the retention window
``SPEC_ERROR``         UNEXPLAINED -- strike_type/floor_strike unusable
``SEMANTICS_MISMATCH`` UNEXPLAINED -- our payoff disagrees with Kalshi on
                       Kalshi's own settlement input
``TRUTH_MISMATCH``     UNEXPLAINED -- Kalshi's expiration_value != our AAA value
``RESULT_MISMATCH``    UNEXPLAINED -- our payoff on our AAA value != result
``SIM_MISMATCH``       UNEXPLAINED -- the simulator recorded a different outcome
``LADDER_CONTRADICTION`` UNEXPLAINED -- the ladder's own results are
                       non-monotonic (a YES at or above a NO strike), which is
                       impossible under a strictly-greater rule
``PIN_MISMATCH``       UNEXPLAINED -- our AAA value falls outside the interval
                       the ladder's published results bracket it to
=====================  ====================================================

TRUTH EXCEPTIONS -- one dated disagreement, not a widened tolerance
------------------------------------------------------------------
The truth leg's tolerance is $0.0005 and stays there. Widening it to absorb a
known disagreement would blind the leg: the one disagreement on record is
$0.0040, eight times the tolerance, so a tolerance able to hide it would also
hide two full strike steps of the finest observed ladder.

Instead :data:`TRUTH_EXCEPTIONS` registers the individual settlement date, with
both numbers written down and the evidence attached. An entry only applies when
**four** independent conditions all hold, each checked per market at run time:

1. the (series, settlement date) pair is registered;
2. Kalshi's ``expiration_value`` still equals the registered value;
3. our AAA record still equals the registered value;
4. *and* the two values give the **same** payoff for this market's own strike --
   recomputed live, never asserted. If any market's outcome would differ the
   exception does not apply to it and ``TRUTH_MISMATCH`` fires.

Because the exception is pinned to two literal numbers on one date, it cannot
absorb a systematic shift: a shift moves every other date's ``expiration_value``
too, and those rows have no entry, so the run goes red on them. And because the
outcome leg still runs on **our** value, the exit-criterion-3 comparison is
never skipped -- an excepted row is a verified row, and a wrong outcome still
breaches as ``RESULT_MISMATCH``.

COVERAGE FLOOR -- why "0 unexplained mismatches" is not enough on its own
------------------------------------------------------------------------
Exit criterion 3 is stated in terms of this report, so a run that checks
*nothing* must not be able to report success. Five paths made that possible and
each is now closed:

* an empty ladder produced zero rows and therefore zero mismatches -- now an
  ``NO_MARKETS`` row, and unexplained;
* a total Kalshi outage produced one explained ``NO_EVENT`` per period and
  nothing else -- now caught by the markets floor;
* an absent ``aaa_daily_national.csv`` short-circuits the truth and outcome
  legs in :func:`classify_market`, so a whole run of ``NO_AAA_VALUE`` rows was
  "0 unexplained" -- now caught by the verified floor;
* reconciling only future periods (nothing settled yet) leaves every row
  ``NOT_SETTLED`` -- now caught by the semantics floor;
* **a PARTIAL AAA series.** The floors used to be run-level aggregates only, so
  one well-covered period carried the whole run: with AAA degraded to 1 of the
  standing cron's 4 periods the job reported ``COVERAGE: OK`` and exit 0 while
  three periods had verified nothing against the authority (and 17 daily
  periods with AAA covering one reported OK with 321 of 338 markets never
  compared to AAA). Partial degradation is the *likelier* live-harvester
  failure than total absence, and an aggregate cannot see it -- so every floor
  is now evaluated **per period as well as** in aggregate, and the per-period
  counts and floors are printed on every run.

Three floors gate exit 0. Each is applied twice: once to every individual
period, and once to the run as a whole scaled by the periods actually requested
(``len(dates) * len(series)``, or the exact per-series pairs when supplied).

``markets_checked >= --min-markets-per-period``
    the ladder was really fetched. Observed live minimums are 33 markets for
    a monthly ladder, 18 weekly, 13 daily, so the default of 8 tolerates a
    substantially thinner ladder while still failing a wholesale outage.
``semantics_verified >= --min-semantics-per-period``
    at least one settled market was checked against Kalshi's own settlement
    input. This floor needs no external data, so it fails only when nothing
    was settled or nothing was fetched.
``verified >= --min-verified-per-period``
    at least one outcome was compared against **our** AAA value. This is the
    leg exit criterion 3 quotes, and it is the floor that fires when
    workstream A's series is missing or partial -- a clean report there would
    mean the authority was never consulted for that period.

Within a period the floors are still counts, not ratios: one market legitimately
unsettled is ordinary latency. What is no longer possible is a period that
compared *nothing* to the authority passing because a sibling period did.

TWO NAMED, COUNTED EXEMPTIONS -- and nothing else
-------------------------------------------------
A per-period floor that fires on the authority's own declared behaviour is a
floor somebody switches off, so exactly two exemptions exist. Both are named,
both are counted and printed on every run (``measure-what-a-gate-excludes``),
and neither can carry a whole run:

``PRUNED`` -- whole period exempt
    Kalshi no longer serves the ladder at all (:data:`RETENTION_DAYS`). No code
    change can overcome it. A run in which *every* period is PRUNED fails.
``AAA_GAP`` -- the OUTCOME floor only
    AAA published no row for the date, and the series carries a row within
    :data:`AAA_GAP_WINDOW_DAYS` on **both** sides -- so the harvest demonstrably
    covers the neighbourhood and this one day is the contract's own §1.1 gap
    ("Wayback coverage is ~24 of 30 days per month... a missing day is a missing
    row"). The markets and semantics floors still apply to such a period, and a
    run in which *every* in-scope period is exempt fails. The test is local and
    two-sided precisely so it cannot absorb degradation: a harvester that stopped
    has no row after the date, and the red-team's single-row CSV has no row
    within three days of any of its failing periods.

``quality=suspect`` ROWS ARE VERIFIED, NEVER CACHED
---------------------------------------------------
Contract §1.1 excludes ``quality=suspect`` rows from *fits*. This job loads them
anyway (``include_suspect=True``), because for a reconcile a suspect row is
evidence: comparing it against Kalshi's own ``expiration_value`` is an
independent audit of the suspect flag, and dropping it instead means the
authority was never consulted for that date at all. On 2026-07-30 the three
suspect rows inside the retrievable daily window (2026-06-05, 2026-06-07,
2026-07-25) each matched Kalshi's settlement input exactly.

A suspect value is **never written into the FR-4.4 settlement cache.** The
runtime resolver is cache-first and its CSV fallback excludes suspect rows, so
caching one would settle a position on a value the runtime's own policy rejects.
The cache write therefore sees ``None`` for a suspect date, which under the
improve-only recorder leaves any prior good entry untouched. The report marks
such a period's source with a trailing ``?``.

USAGE
-----
    python scripts/reconcile_gas.py                       # last settled month-end
    python scripts/reconcile_gas.py --periods 2
    python scripts/reconcile_gas.py --series KXAAAGASD --periods 7
    python scripts/reconcile_gas.py --date 2026-06-30 --json
    python scripts/reconcile_gas.py --harvest-truth       # refresh the pinned fixture

CRON (mirrors scripts/reconcile_weather.py):
    # 15:00 UTC daily -- after AAA publishes (Kalshi expires 10:00 ET).
    # ALL THREE series. A reconcile that omits a series the bot harvests is not
    # a reconcile: this line used to name only the monthly and daily series,
    # excluding KXAAAGASW -- the one carrying the only truth disagreement on
    # record -- so its "0 mismatches" was a property of the chosen scope. Keep
    # the list equal to ``gas_bot.GAS_SERIES`` plus the daily series this job
    # also governs; a test parses THIS line and asserts it.
    0 15 * * * cd /home/USER/money_printer && /usr/bin/python3 \
        scripts/reconcile_gas.py --series KXAAAGASM,KXAAAGASW,KXAAAGASD \
        --periods 2 >> /home/USER/money_printer/logs/reconcile_gas.log 2>&1

Exit codes:
    0  reconciled, unexplained mismatches within threshold, coverage floors met
    1  unexplained mismatches exceeded threshold
    2  usage / fatal error
    3  coverage floor not met -- the run verified too little to mean anything
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:  # pragma: no cover - requests is a hard project dependency
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

from src.data.gas_settlement import (  # noqa: E402
    AAA_DAILY_CSV,
    GAS_SERIES,
    PRIMARY_SERIES,
    REPORT_DIR,
    SETTLEMENT_CACHE_PATH,
    SOURCE_AAA_SERIES,
    SOURCE_KALSHI_SETTLEMENT,
    TERMINAL_STATUSES,
    AAARow,
    GasSpecError,
    GasTruthError,
    event_ticker,
    expected_result,
    get_series,
    load_aaa_series,
    load_settlement_cache,
    normalize_date,
    parse_gas_spec,
    pin_truth_from_ladder,
    record_truth,
    save_settlement_cache,
    series_for,
    settlement_date_for,
    settlement_dates,
    truth_key,
)

logger = logging.getLogger("reconcile_gas")

KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
FETCH_TIMEOUT = 20
USER_AGENT = (
    "money-printer/gas-truth (github.com/JusHoya/money_printer; "
    "contact via repo owner)"
)

DEFAULT_THRESHOLD = 0  # any unexplained mismatch is a breach

#: Coverage floors, per requested (series, date) period. See "COVERAGE FLOOR".
#: Live ladder sizes on 2026-07-29: monthly 33/41, weekly 18..35, daily 13..35.
DEFAULT_MIN_MARKETS_PER_PERIOD = 8
DEFAULT_MIN_SEMANTICS_PER_PERIOD = 1
DEFAULT_MIN_VERIFIED_PER_PERIOD = 1

#: Exit code for a run that reported no mismatches because it checked nothing.
EXIT_COVERAGE = 3

#: The series the standing cron line in the module docstring must pass. Every
#: series the bot harvests (``gas_bot.GAS_SERIES`` = monthly + weekly) plus the
#: daily series this job also governs. The previous standing line was
#: ``KXAAAGASM,KXAAAGASD`` and therefore *excluded* ``KXAAAGASW`` -- the one
#: series carrying a truth disagreement -- so its "0 mismatches" described a
#: chosen scope rather than the system. ``tests/test_gas_reconcile.py`` asserts
#: this tuple against both registries AND against the docstring text, because a
#: constant nobody passes is not a cron line.
STANDING_CRON_SERIES = ("KXAAAGASM", "KXAAAGASW", "KXAAAGASD")

#: AAA publishes to three decimals and Kalshi's ``expiration_value`` is a
#: three-decimal string, so half a tenth of a cent is the tightest honest
#: tolerance. It is deliberately far tighter than any strike spacing observed
#: (0.002 minimum), per ``guard-tighter-than-the-gate-it-feeds``.
DEFAULT_TRUTH_TOLERANCE = 0.0005

#: How far back Kalshi still serves settled gas markets, in days. Probed live
#: 2026-07-29: the monthly ladders for 2026-05-31 (59 days old) and 2026-06-30
#: were both fully retrievable; 2026-04-30 (90 days) resolved as an event but
#: carried zero markets, as did every older month-end. The true horizon is
#: therefore somewhere in [59, 90) days and 60 is the conservative choice -- it
#: never explains away a period we have observed to BE retrievable, so a genuine
#: empty ladder inside the window stays an unexplained ``NO_MARKETS``.
RETENTION_DAYS = 60

#: How far either side of a settlement date the AAA series must carry a row for
#: an absent row ON that date to count as an isolated contract §1.1 gap rather
#: than a degraded harvest. Wayback coverage is ~24 of 30 days per month and a
#: missing day is recorded as a MISSING ROW by design, so a wide historical
#: window legitimately contains dates with no truth at all.
#:
#: The test is local and two-sided on purpose. With ``W`` days either side, a run
#: of ``L`` consecutive missing days is exempt as follows: **every** day of it
#: when ``L <= W``; only its inner days when ``W < L < 2W``; and **none** of it
#: when ``L >= 2W``. So at the default of 3 a one-to-three-day hole is entirely
#: explained and a week-long hole is entirely unexplained. Both real holes in the
#: retrievable daily window (2026-05-28, 2026-06-02) are single days.
#:
#: It also fails the two shapes that matter: a harvester that STOPPED has no row
#: after the date, and a series that never covered the window has none on either
#: side -- the red-team's single-row CSV is within three days of none of its
#: failing periods, which is what stops this exemption from reopening D3.
AAA_GAP_WINDOW_DAYS = 3

#: Registered settlement-truth exceptions, keyed ``(series, settlement date)``.
#: See "TRUTH EXCEPTIONS" in the module docstring for the four conditions an
#: entry must satisfy before it applies. Entries are dated, carry both numbers
#: literally, and are pinned by ``tests/test_gas_reconcile.py`` -- adding one is
#: a deliberate act with a test edit attached, never a quiet widening.
TRUTH_EXCEPTIONS: Dict[tuple, Dict[str, Any]] = {
    ("KXAAAGASW", "2026-07-13"): {
        "rule": "AAA_INTRADAY_REVISION",
        "registered_on": "2026-07-30",
        "kalshi_expiration_value": 3.876,
        "our_aaa_value": 3.872,
        "evidence": (
            "Kalshi settled KXAAAGASW-26JUL13 on 3.876. Our AAA record for "
            "2026-07-13 is 3.872, parsed from the Wayback snapshot captured "
            "2026-07-13T17:23:51Z (13:23 ET) -- more than three hours after "
            "Kalshi's 10:00 ET expiration, and 3.876 is exactly our 2026-07-12 "
            "value. The most likely reading is that AAA displayed 3.876 at "
            "10:00 ET on 07-13 and had moved to 3.872 by 13:23 ET; the original "
            "10:00 ET page state is not retrievable, so the residual is "
            "recorded rather than resolved. It is isolated, not a shifted "
            "series: across all 79 pinned settlements 75 match our same-day "
            "value, this one matches our previous-day value, 3 have no AAA row, "
            "and 0 match neither. The 07-13 ladder brackets the settle to "
            "(3.860, 3.880] at 0.020 strike spacing, so 3.872 and 3.876 lie "
            "between the same two strikes and every one of the 20 published "
            "results is reproduced by either value -- which condition 4 "
            "recomputes per market rather than taking on trust."
        ),
        "evidence_paths": (
            "tests/fixtures/gas/kalshi_pinned_truth.csv",
            "data/gas_truth/aaa_daily_national.csv",
        ),
    },
}

EXPLAINED_CATEGORIES = frozenset(
    {
        "NO_AAA_VALUE",
        "NOT_SETTLED",
        "VOIDED",
        "NO_RESULT",
        "NO_EVENT",
        "PRUNED",
        "TRUTH_EXCEPTION",
    }
)
UNEXPLAINED_CATEGORIES = frozenset(
    {
        "NO_MARKETS",
        "SPEC_ERROR",
        "SEMANTICS_MISMATCH",
        "TRUTH_MISMATCH",
        "RESULT_MISMATCH",
        "SIM_MISMATCH",
        "LADDER_CONTRADICTION",
        "PIN_MISMATCH",
    }
)

FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "gas")
PINNED_TRUTH_CSV = os.path.join(FIXTURE_DIR, "kalshi_pinned_truth.csv")
PINNED_TRUTH_MANIFEST = os.path.join(FIXTURE_DIR, "kalshi_pinned_truth_manifest.json")


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Reuse the crypto-era sim-outcome derivation rather than re-deriving it.
# scripts/ is not a package, so it is loaded by path -- the same mechanism
# reconcile_weather.py uses, so a fix to "what did the sim believe" cannot
# drift between the three reconcile jobs.
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
# Kalshi access (isolated so tests monkeypatch one function and never hit the wire)
# ---------------------------------------------------------------------------
def _kalshi_get(path: str, params: Optional[Mapping[str, Any]] = None):
    if requests is None:  # pragma: no cover
        logger.error("requests unavailable; cannot reach Kalshi")
        return None
    url = f"{KALSHI_PROD_URL}{path}"
    try:
        return requests.get(
            url,
            params=params or {},
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Kalshi GET %s failed: %s", url, exc)
        return None


def fetch_event_markets(
    series_ticker: str, date: Any
) -> Optional[List[Dict[str, Any]]]:
    """Fetch one settlement period's gas ladder from the public Kalshi V2 API.

    Returns the market list, ``[]`` when the event exists but yields no markets
    (an API-shape change or a pruned settled event), or ``None`` when Kalshi has
    no such event / the call failed.
    """
    ticker = event_ticker(series_ticker, date)
    resp = _kalshi_get(f"/events/{ticker}", {"with_nested_markets": "true"})
    if resp is None:
        return None
    status = getattr(resp, "status_code", None)
    if status == 404:
        logger.info("Kalshi has no event %s", ticker)
        return None
    if status != 200:
        logger.error("Kalshi event %s returned HTTP %s", ticker, status)
        return None
    try:
        payload = resp.json()
    except Exception as exc:
        logger.error("Kalshi event %s returned non-JSON: %s", ticker, exc)
        return None
    # Shape trap: the response carries BOTH a top-level "markets" key and
    # "event.markets". With ?with_nested_markets=true the top-level list is
    # present but EMPTY and the real ladder lives under event.markets. Keying on
    # presence alone silently reconciles zero markets and reports a clean run.
    top = payload.get("markets")
    nested = (payload.get("event") or {}).get("markets")
    for candidate in (nested, top):
        if isinstance(candidate, list) and candidate:
            return candidate
    if isinstance(top, list) or isinstance(nested, list):
        # WARNING, not ERROR: whether an empty ladder is Kalshi's retention limit
        # or a real failure to read it depends on the settlement date's age,
        # which only :func:`reconcile_period` knows. It classifies this as
        # PRUNED (explained) or NO_MARKETS (unexplained); logging ERROR here
        # would page a log-scraping cron about the exchange's retention policy.
        logger.warning(
            "Kalshi event %s returned zero markets; reconcile_period will "
            "classify this as PRUNED (older than the ~%d-day settled-market "
            "retention window) or as an unexplained NO_MARKETS",
            ticker,
            RETENTION_DAYS,
        )
        return []
    return None


def fetch_settled_markets(
    series_ticker: str, *, page_pause: float = 0.4
) -> List[Dict[str, Any]]:
    """Every settled market the API still returns for a series, all pages.

    Used by ``--harvest-truth``. Note the status trap: the query value is
    ``settled`` and the returned markets carry ``status="finalized"``.
    """
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        resp = _kalshi_get("/markets", params)
        if resp is None or getattr(resp, "status_code", None) != 200:
            logger.error(
                "settled-market harvest for %s failed (HTTP %s)",
                series_ticker,
                getattr(resp, "status_code", None) if resp is not None else "n/a",
            )
            break
        try:
            payload = resp.json()
        except Exception as exc:
            logger.error(
                "settled-market harvest for %s: non-JSON (%s)", series_ticker, exc
            )
            break
        markets = payload.get("markets") or []
        out.extend(markets)
        cursor = payload.get("cursor")
        if not cursor or not markets:
            break
        if page_pause:
            time.sleep(page_pause)
    return out


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
def registered_truth_exception(ticker: str) -> Optional[Dict[str, Any]]:
    """The :data:`TRUTH_EXCEPTIONS` entry for this ticker's period, if any.

    The series and the settlement *date label* are read from the ticker, which
    is identity, not semantics -- direction and strike still come only from the
    API fields (PRD FR-1.1).
    """
    series = series_for(ticker)
    day = settlement_date_for(ticker)
    if series is None or day is None:
        return None
    return TRUTH_EXCEPTIONS.get((series, day.isoformat()))


def evaluate_truth_exception(
    ticker: str,
    spec: Any,
    kalshi_value: float,
    aaa_value: float,
    *,
    tolerance: float,
) -> tuple:
    """``(entry, applies, why_not)`` for a truth-leg disagreement.

    ``entry`` is the registered exception for this ticker's period (or ``None``
    when the date is not registered at all). ``applies`` is True only when all
    four conditions in "TRUTH EXCEPTIONS" hold; ``why_not`` names the first that
    did not, so a TRUTH_MISMATCH detail can say that a registered exception was
    considered and rejected instead of leaving the reader guessing.
    """
    entry = registered_truth_exception(ticker)
    if entry is None:
        return (None, False, "")

    registered_kalshi = float(entry["kalshi_expiration_value"])
    registered_ours = float(entry["our_aaa_value"])
    if abs(kalshi_value - registered_kalshi) > tolerance:
        return (
            entry,
            False,
            f"the registered exception {entry['rule']} is pinned to Kalshi's "
            f"{registered_kalshi:.3f} and this market settled on "
            f"{kalshi_value:.3f}",
        )
    if abs(aaa_value - registered_ours) > tolerance:
        return (
            entry,
            False,
            f"the registered exception {entry['rule']} is pinned to our "
            f"{registered_ours:.3f} and our record now says {aaa_value:.3f}",
        )
    # Condition 4, recomputed live per market: the disagreement must not change
    # THIS strike's payoff. A registered date does not license a material
    # difference anywhere on the ladder.
    try:
        under_kalshi = expected_result(spec, kalshi_value)
        under_ours = expected_result(spec, aaa_value)
    except GasSpecError as exc:  # pragma: no cover - spec already parsed
        return (entry, False, f"payoff could not be recomputed: {exc}")
    if under_kalshi != under_ours:
        return (
            entry,
            False,
            f"the registered exception {entry['rule']} only covers a difference "
            f"immaterial to the outcome, but this strike pays "
            f"{under_kalshi.upper()} on {kalshi_value:.3f} and "
            f"{under_ours.upper()} on {aaa_value:.3f}",
        )
    return (entry, True, "")


def classify_market(
    market: Mapping[str, Any],
    aaa_value: Optional[float],
    sim: Optional[Mapping[str, Any]] = None,
    *,
    truth_tolerance: float = DEFAULT_TRUTH_TOLERANCE,
) -> Dict[str, Any]:
    """Reconcile one market. Returns a row with ``category`` and ``explained``.

    ``category`` is ``"MATCH"`` on success, otherwise one of the categories in
    the module docstring. ``explained=True`` means the row is reported but does
    not count against the mismatch threshold.

    The legs run in a deliberate order: the semantics leg first, because it
    needs no external data and a failure there invalidates every later
    comparison.
    """
    ticker = str(market.get("ticker") or "<unknown>")
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()

    row: Dict[str, Any] = {
        "ticker": ticker,
        "status": status,
        "kalshi_result": result or None,
        "aaa_value": aaa_value,
        "strike_type": market.get("strike_type"),
        "floor_strike": market.get("floor_strike"),
        "expiration_value": market.get("expiration_value"),
        "expected_result": None,
        "semantics_checked": False,
        "sim_result": (sim or {}).get("sim_result"),
        "category": "MATCH",
        "explained": True,
        "detail": "",
        # Set only when a registered TRUTH_EXCEPTIONS entry actually applied.
        "truth_exception": None,
    }

    def fail(category: str, detail: str) -> Dict[str, Any]:
        row["category"] = category
        row["explained"] = category in EXPLAINED_CATEGORIES
        row["detail"] = detail
        return row

    # 1. Semantics come from API fields only (FR-1.1 / Phase 1 EC-2). A market
    #    without strike_type is a hard failure -- inferring direction (or the
    #    strike) from the ticker is the exact defect this rebuild removes.
    try:
        spec = parse_gas_spec(ticker, market)
    except GasSpecError as exc:
        return fail("SPEC_ERROR", str(exc))
    row["yes_rule"] = spec.describe()

    # 2. Outcome availability.
    if result in ("void", "voided"):
        return fail("VOIDED", "Kalshi voided this market")
    if result not in ("yes", "no"):
        if status in TERMINAL_STATUSES:
            return fail(
                "NO_RESULT", f"status={status!r} but no yes/no result published"
            )
        return fail("NOT_SETTLED", f"status={status!r}; not yet settled")

    # 3. SEMANTICS LEG. Does our payoff module reproduce Kalshi's own result
    #    from Kalshi's own settlement input? Needs no external truth, so it is
    #    the one leg that always runs on a settled market -- and it is what
    #    proves the strictly-greater boundary against the exchange.
    raw_exp = market.get("expiration_value")
    kalshi_value: Optional[float] = None
    if raw_exp not in (None, ""):
        try:
            kalshi_value = float(raw_exp)
        except (TypeError, ValueError):
            return fail(
                "SPEC_ERROR",
                f"expiration_value={raw_exp!r} is not numeric; cannot verify "
                f"semantics against the exchange",
            )
    if kalshi_value is not None:
        row["semantics_checked"] = True
        try:
            derived = expected_result(spec, kalshi_value)
        except GasSpecError as exc:
            return fail("SPEC_ERROR", str(exc))
        row["semantics_expected"] = derived
        if derived != result:
            return fail(
                "SEMANTICS_MISMATCH",
                f"our payoff says {derived.upper()} for value={kalshi_value:.3f} "
                f"({spec.describe()}) but Kalshi settled {result.upper()} on that "
                f"same value",
            )

    # 4. Truth availability. This is where "AAA has not published yet" lives.
    if aaa_value is None:
        return fail(
            "NO_AAA_VALUE",
            "no AAA national average recorded for this settlement date yet",
        )

    # 5. TRUTH LEG. Kalshi's settlement input must agree with our AAA value.
    #    A disagreement is a breach UNLESS it is a registered, dated exception
    #    that is provably immaterial to this market's outcome. The tolerance is
    #    never widened to make one disappear -- see "TRUTH EXCEPTIONS".
    if kalshi_value is not None and abs(kalshi_value - aaa_value) > truth_tolerance:
        entry, applies, why_not = evaluate_truth_exception(
            ticker, spec, kalshi_value, aaa_value, tolerance=truth_tolerance
        )
        if not applies:
            detail = (
                f"Kalshi settled on {kalshi_value:.3f} but our AAA record says "
                f"{aaa_value:.3f} (tolerance {truth_tolerance:g})"
            )
            if entry is not None:
                detail += f"; {why_not}"
            return fail("TRUTH_MISMATCH", detail)
        row["truth_exception"] = entry["rule"]
        row["truth_exception_detail"] = (
            f"registered {entry['rule']} ({entry['registered_on']}): Kalshi "
            f"settled on {kalshi_value:.3f}, our AAA record says "
            f"{aaa_value:.3f}, and both give {expected_result(spec, aaa_value).upper()} "
            f"for {spec.describe()} -- immaterial to this outcome. The outcome "
            f"leg below still ran on OUR value."
        )

    # 6. OUTCOME LEG. The check exit criterion 3 is stated in terms of.
    try:
        expected = expected_result(spec, aaa_value)
    except GasSpecError as exc:
        return fail("SPEC_ERROR", str(exc))
    row["expected_result"] = expected
    if expected != result:
        return fail(
            "RESULT_MISMATCH",
            f"our payoff says {expected.upper()} for AAA value="
            f"{aaa_value:.3f} ({spec.describe()}) but Kalshi settled "
            f"{result.upper()}",
        )

    # 7. SIM LEG. Where a sim position exists, it must agree with Kalshi too.
    if sim and sim.get("sim_result") and sim["sim_result"] != result:
        return fail(
            "SIM_MISMATCH",
            f"sim recorded {str(sim['sim_result']).upper()} but Kalshi settled "
            f"{result.upper()} (strategy={sim.get('strategy')}, pnl={sim.get('pnl')})",
        )

    # Every leg passed. Label the row for the registered exception if one was
    # applied, so it is reported by its own category rather than as a plain
    # MATCH -- an exception that reads as a clean match is an exception nobody
    # will ever revisit.
    if row["truth_exception"]:
        return fail("TRUTH_EXCEPTION", row["truth_exception_detail"])

    return row


def period_age_days(date: str, as_of: Optional[str] = None) -> int:
    """Whole days between ``date`` and ``as_of`` (default: today, UTC)."""
    day = datetime.strptime(normalize_date(date), "%Y-%m-%d").date()
    ref = (
        datetime.strptime(normalize_date(as_of), "%Y-%m-%d").date()
        if as_of is not None
        else datetime.now(timezone.utc).date()
    )
    return (ref - day).days


def reconcile_period(
    series_ticker: str,
    date: str,
    markets: Optional[Sequence[Mapping[str, Any]]],
    aaa_value: Optional[float],
    sim_outcomes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    truth_source: Optional[str] = None,
    truth_tolerance: float = DEFAULT_TRUTH_TOLERANCE,
    as_of: Optional[str] = None,
    retention_days: int = RETENTION_DAYS,
) -> Dict[str, Any]:
    """Reconcile every market in one settlement period's ladder.

    ``as_of`` (default today, UTC) is only used to decide whether an empty or
    absent ladder is Kalshi's retention limit (``PRUNED``, explained and exempt
    from the coverage floors) or a real failure to read the ladder
    (``NO_MARKETS`` / ``NO_EVENT``). Tests pass it explicitly so the
    classification cannot drift with the wall clock.
    """
    sim_outcomes = sim_outcomes or {}
    spec = get_series(series_ticker)
    summary: Dict[str, Any] = {
        "series_ticker": spec.series_ticker,
        "cadence": spec.cadence,
        "date": date,
        "event_ticker": event_ticker(spec.series_ticker, date),
        "aaa_value": aaa_value,
        "truth_source": truth_source,
        "markets_checked": 0,
        "matched": 0,
        # `verified` counts only rows where the OUTCOME leg ran: AAA truth
        # published, result published, payoff compared against both. It is what
        # the exit-criterion-3 coverage floor is built on, because
        # `markets_checked` counts rows that short-circuited too.
        "verified": 0,
        # `semantics_verified` counts rows checked against Kalshi's own
        # settlement input -- reported separately so "0 unexplained" can never
        # be confused between "we agreed with the exchange" and "we had no AAA
        # data and therefore compared nothing to the authority".
        "semantics_verified": 0,
        "sim_checked": 0,
        "unexplained": 0,
        "explained": 0,
        # Rows that passed every leg but carried a registered, dated truth
        # exception. Counted separately so the report can never present one as
        # an ordinary match.
        "truth_exceptions": 0,
        "pinned": None,
        # Set to a category name when this period is exempt from the per-period
        # coverage floors, and to None otherwise. The only exemption is Kalshi's
        # settled-market retention limit, which no code change can overcome.
        "coverage_excluded": None,
        "age_days": period_age_days(date, as_of),
        "rows": [],
    }

    if not markets:
        age = summary["age_days"]
        pruned = age > int(retention_days)
        retention_note = (
            f"this settlement date is {age} days old and Kalshi serves settled "
            f"gas markets for roughly {retention_days} days, so its ladder is "
            f"permanently unretrievable -- an external retention limit, not a "
            f"defect. The period is excluded from the coverage floors and the "
            f"exclusion is counted on every run"
        )
        if markets is None:
            if pruned:
                summary["rows"].append(
                    {
                        "ticker": summary["event_ticker"],
                        "category": "PRUNED",
                        "explained": True,
                        "detail": f"Kalshi has no event for this date; {retention_note}",
                        "aaa_value": aaa_value,
                    }
                )
                summary["explained"] = 1
                summary["coverage_excluded"] = "PRUNED"
            else:
                summary["rows"].append(
                    {
                        "ticker": summary["event_ticker"],
                        "category": "NO_EVENT",
                        "explained": True,
                        "detail": (
                            f"Kalshi has no event for this settlement date, which "
                            f"is only {age} days old and inside the "
                            f"{retention_days}-day retention window"
                        ),
                        "aaa_value": aaa_value,
                    }
                )
                summary["explained"] = 1
        elif pruned:
            # An empty ladder on a date Kalshi no longer serves. Explained, and
            # reported as its own category so a cron is not paged about the
            # exchange's retention policy.
            summary["rows"].append(
                {
                    "ticker": summary["event_ticker"],
                    "category": "PRUNED",
                    "explained": True,
                    "detail": (
                        f"Kalshi returned an empty market list for this event; "
                        f"{retention_note}"
                    ),
                    "aaa_value": aaa_value,
                }
            )
            summary["explained"] = 1
            summary["coverage_excluded"] = "PRUNED"
        else:
            # An event INSIDE the retention window that yields no markets means
            # the ladder could not be read -- an API-shape change or a renamed
            # event ticker. Reporting that as explained is how the job would
            # have produced "OK: 0 unexplained mismatches (0 markets checked)"
            # forever.
            summary["rows"].append(
                {
                    "ticker": summary["event_ticker"],
                    "category": "NO_MARKETS",
                    "explained": False,
                    "detail": (
                        f"Kalshi returned an empty market list for this event and "
                        f"the settlement date is only {age} days old, inside the "
                        f"{retention_days}-day settled-market retention window; "
                        f"the ladder could not be reconciled"
                    ),
                    "aaa_value": aaa_value,
                }
            )
            summary["unexplained"] = 1
        return summary

    for market in markets:
        ticker = str(market.get("ticker") or "")
        row = classify_market(
            market,
            aaa_value,
            sim_outcomes.get(ticker),
            truth_tolerance=truth_tolerance,
        )
        summary["rows"].append(row)
        summary["markets_checked"] += 1
        if row.get("semantics_checked"):
            summary["semantics_verified"] += 1
        if row.get("expected_result") is not None:
            summary["verified"] += 1
        if sim_outcomes.get(ticker) and sim_outcomes[ticker].get("sim_result"):
            summary["sim_checked"] += 1
        if row.get("truth_exception"):
            summary["truth_exceptions"] += 1
        if row["category"] == "MATCH":
            summary["matched"] += 1
        elif row["explained"]:
            summary["explained"] += 1
        else:
            summary["unexplained"] += 1

    # Ladder-level checks. These cannot be expressed per-market: a
    # non-monotonic ladder is a contradiction between markets, and the pinned
    # interval is a property of the whole ladder.
    settled = [
        m for m in markets if str(m.get("result") or "").lower() in ("yes", "no")
    ]
    if settled:
        try:
            pinned = pin_truth_from_ladder(settled)
        except GasSpecError as exc:
            summary["rows"].append(
                {
                    "ticker": summary["event_ticker"],
                    "category": "LADDER_CONTRADICTION",
                    "explained": False,
                    "detail": str(exc),
                    "aaa_value": aaa_value,
                }
            )
            summary["unexplained"] += 1
        else:
            summary["pinned"] = pinned.as_dict()
            if aaa_value is not None and not pinned.contains(aaa_value):
                summary["rows"].append(
                    {
                        "ticker": summary["event_ticker"],
                        "category": "PIN_MISMATCH",
                        "explained": False,
                        "detail": (
                            f"our AAA value {aaa_value:.3f} falls outside the "
                            f"interval the ladder's own results bracket the "
                            f"settle to: ("
                            f"{_fmt(pinned.low_exclusive)}, "
                            f"{_fmt(pinned.high_inclusive)}]"
                        ),
                        "aaa_value": aaa_value,
                    }
                )
                summary["unexplained"] += 1

    return summary


def _fmt(value: Optional[float]) -> str:
    return "-inf" if value is None else f"{value:.3f}"


def aggregate(period_summaries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Roll period summaries into the run-level totals used by the report."""
    by_category: Dict[str, int] = {}
    unexplained_rows: List[Dict[str, Any]] = []
    for period in period_summaries:
        for row in period["rows"]:
            cat = row["category"]
            by_category[cat] = by_category.get(cat, 0) + 1
            if not row.get("explained", True):
                unexplained_rows.append(
                    {
                        **row,
                        "series": period["series_ticker"],
                        "date": period["date"],
                    }
                )
    return {
        "periods": len(period_summaries),
        "markets_checked": sum(p["markets_checked"] for p in period_summaries),
        "matched": sum(p["matched"] for p in period_summaries),
        "verified": sum(p.get("verified", 0) for p in period_summaries),
        "semantics_verified": sum(
            p.get("semantics_verified", 0) for p in period_summaries
        ),
        "sim_checked": sum(p.get("sim_checked", 0) for p in period_summaries),
        "truth_exceptions": sum(p.get("truth_exceptions", 0) for p in period_summaries),
        "periods_with_truth": sum(
            1 for p in period_summaries if p.get("aaa_value") is not None
        ),
        "periods_pinned": sum(1 for p in period_summaries if p.get("pinned")),
        "explained": sum(p["explained"] for p in period_summaries),
        "unexplained": sum(p["unexplained"] for p in period_summaries),
        "by_category": by_category,
        "unexplained_rows": unexplained_rows,
    }


def evaluate_coverage(
    summary: Mapping[str, Any],
    *,
    min_markets_per_period: int = DEFAULT_MIN_MARKETS_PER_PERIOD,
    min_semantics_per_period: int = DEFAULT_MIN_SEMANTICS_PER_PERIOD,
    min_verified_per_period: int = DEFAULT_MIN_VERIFIED_PER_PERIOD,
) -> Dict[str, Any]:
    """Did this run verify enough for "0 unexplained mismatches" to mean anything?

    Returns the floors, the observed counts, and a list of human-readable
    failures. An empty ``failures`` list is the only thing that lets the job
    exit 0 -- see "COVERAGE FLOOR" in the module docstring.

    Each floor is applied **twice**: once to every individual period, and once
    to the run in aggregate. The aggregate alone was blind to partial
    degradation -- one well-covered period carried three that had verified
    nothing against the settlement authority, and the run reported OK. The
    per-period counts and floors are returned in ``per_period`` and printed by
    :func:`format_report` on every run, whether they pass or fail.

    Periods Kalshi can no longer serve at all (``PRUNED``) are exempt from the
    per-period floors and listed in ``excluded``; a run in which *every* period
    was excluded fails, because it verified nothing.
    """
    totals = summary["totals"]
    period_summaries = list(summary.get("periods") or [])
    # Prefer the exact per-series settlement dates when the caller supplied
    # them: a mixed-cadence run's cross product counts pairs that were never
    # reconciled, so the denominator exceeds the number of periods that exist
    # and a healthy run fails its own floor.
    per_series = summary.get("per_series_dates")
    if per_series:
        periods = max(1, sum(len(v) for v in per_series.values()))
    else:
        periods = max(
            1, len(summary.get("dates") or []) * len(summary.get("series") or [])
        )
    markets_floor = int(min_markets_per_period) * periods
    semantics_floor = int(min_semantics_per_period) * periods
    verified_floor = int(min_verified_per_period) * periods

    failures: List[str] = []

    # ------------------------------------------------------------------
    # PER-PERIOD floors. An aggregate cannot see a period that verified
    # nothing, and partial AAA loss is the likelier live failure than total
    # absence -- see the fifth bullet of "COVERAGE FLOOR".
    # ------------------------------------------------------------------
    per_period: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for period in period_summaries:
        entry = {
            "series": period.get("series_ticker"),
            "date": period.get("date"),
            "markets_checked": period.get("markets_checked", 0),
            "semantics_verified": period.get("semantics_verified", 0),
            "verified": period.get("verified", 0),
            "markets_floor": int(min_markets_per_period),
            "semantics_floor": int(min_semantics_per_period),
            "verified_floor": int(min_verified_per_period),
            "excluded": period.get("coverage_excluded"),
            # Set when the OUTCOME floor alone is exempted for a named, counted
            # reason (an isolated contract §1.1 AAA gap). The markets and
            # semantics floors still apply to such a period.
            "verified_exempt": period.get("verified_exempt"),
            "truth_quality": period.get("truth_quality"),
            "age_days": period.get("age_days"),
            "ok": True,
            "failures": [],
        }
        if entry["excluded"]:
            entry["ok"] = None  # neither pass nor fail: not evaluated
            excluded.append(entry)
            per_period.append(entry)
            continue
        label = f"{entry['date']} {entry['series']}"
        if entry["markets_checked"] < entry["markets_floor"]:
            entry["failures"].append(
                f"{label}: only {entry['markets_checked']} market(s) fetched "
                f"(floor {entry['markets_floor']}) -- this period's ladder was "
                f"not read"
            )
        if entry["semantics_verified"] < entry["semantics_floor"]:
            entry["failures"].append(
                f"{label}: only {entry['semantics_verified']} market(s) checked "
                f"against Kalshi's own settlement input (floor "
                f"{entry['semantics_floor']}) -- nothing in this period was "
                f"settled"
            )
        if entry["verified_exempt"]:
            pass  # named, counted exemption -- see AAA_GAP_WINDOW_DAYS
        elif entry["verified"] < entry["verified_floor"]:
            entry["failures"].append(
                f"{label}: {entry['verified']} outcome(s) compared against OUR "
                f"AAA value (floor {entry['verified_floor']}) -- this period "
                f"verified NOTHING against the settlement authority. An "
                f"aggregate floor would have let a sibling period cover for it; "
                f"check that the AAA series covers {entry['date']}"
            )
        entry["ok"] = not entry["failures"]
        failures.extend(entry["failures"])
        per_period.append(entry)

    evaluated = [e for e in per_period if e["excluded"] is None]
    verified_exempt = [e for e in evaluated if e["verified_exempt"]]
    if period_summaries and not evaluated:
        failures.append(
            f"all {len(period_summaries)} requested period(s) were excluded from "
            f"the per-period floors as PRUNED (older than Kalshi's "
            f"~{RETENTION_DAYS}-day settled-market retention), so this run "
            f"verified nothing. Reconcile a period Kalshi still serves."
        )
    # An exemption must never become a way to pass while comparing nothing to
    # the authority. If EVERY in-scope period was exempted, the run is vacuous.
    elif evaluated and len(verified_exempt) == len(evaluated):
        failures.append(
            f"every one of the {len(evaluated)} in-scope period(s) had its "
            f"outcome floor exempted as an isolated AAA gap, so nothing in this "
            f"run was compared against the settlement authority. An exemption is "
            f"not a pass."
        )

    # ------------------------------------------------------------------
    # AGGREGATE floors, kept as-is: they also cover the periods the caller
    # asked for but that never produced a summary at all.
    # ------------------------------------------------------------------
    if totals["markets_checked"] < markets_floor:
        failures.append(
            f"only {totals['markets_checked']} markets were fetched across "
            f"{periods} period(s); the floor is {markets_floor} "
            f"({min_markets_per_period}/period). Kalshi discovery is broken, the "
            f"exchange is unreachable, or the settled events have been pruned -- "
            f"this run verified nothing."
        )
    if totals.get("semantics_verified", 0) < semantics_floor:
        failures.append(
            f"only {totals.get('semantics_verified', 0)} settled market(s) were "
            f"checked against Kalshi's own settlement input across {periods} "
            f"period(s); the floor is {semantics_floor} "
            f"({min_semantics_per_period}/period). Nothing in this run was "
            f"settled, so the payoff semantics were never exercised."
        )
    if totals.get("verified", 0) < verified_floor:
        failures.append(
            f"only {totals.get('verified', 0)} outcome(s) were compared against "
            f"OUR AAA value across {periods} period(s); the floor is "
            f"{verified_floor} ({min_verified_per_period}/period). The published "
            f"AAA value is the settlement authority for FR-4.4, so a clean report "
            f"without it would mean the authority was never consulted -- check "
            f"that data/gas_truth/aaa_daily_national.csv covers these dates."
        )
    return {
        "periods": periods,
        "markets_floor": markets_floor,
        "semantics_floor": semantics_floor,
        "verified_floor": verified_floor,
        "markets_checked": totals["markets_checked"],
        "semantics_verified": totals.get("semantics_verified", 0),
        "verified": totals.get("verified", 0),
        # Per-period evaluation. ``per_period`` carries one entry per reconciled
        # period (``ok=None`` when excluded); ``periods_excluded`` is printed on
        # every run so a widening carve-out is a rising number, not a silence.
        "min_markets_per_period": int(min_markets_per_period),
        "min_semantics_per_period": int(min_semantics_per_period),
        "min_verified_per_period": int(min_verified_per_period),
        "per_period": per_period,
        "periods_evaluated": len(evaluated),
        "periods_excluded": len(excluded),
        "excluded": excluded,
        "periods_verified_exempt": len(verified_exempt),
        "verified_exempt": verified_exempt,
        "periods_failing": sum(1 for e in evaluated if not e["ok"]),
        "ok": not failures,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Orchestration (I/O)
# ---------------------------------------------------------------------------
def _aaa_value_for(date: str, aaa_series: Mapping[str, AAARow]) -> tuple:
    """``(value, source, source_url, quality)`` for one settlement date.

    Deliberately **does not** consult the settlement cache. The cache is this
    job's own prior output; reading it back as truth would make the reconcile
    validate against itself, and it would shadow a later correction to
    ``aaa_daily_national.csv`` -- a run would keep reporting clean against the
    value it recorded the first time. ``scripts/reconcile_weather.py`` has the
    same one-way relationship with its cache (write-only here, read at runtime
    by the settlement resolver), and the reason is the same.
    """
    row = aaa_series.get(date)
    if row is not None:
        return (
            float(row.value),
            row.source or SOURCE_AAA_SERIES,
            row.source_url,
            row.quality or "ok",
        )
    return (None, None, "", None)


def aaa_gap_is_bracketed(
    date: str,
    aaa_series: Mapping[str, AAARow],
    *,
    window_days: int = AAA_GAP_WINDOW_DAYS,
) -> bool:
    """Does the AAA series carry a row within ``window_days`` on BOTH sides?

    Used only to decide whether an ABSENT row is an isolated contract §1.1 gap
    (the series demonstrably covers this neighbourhood) or a degraded harvest.
    Two-sided by design: a harvester that stopped has no row after the date, and
    a series that never covered the window has none on either side.
    """
    day = datetime.strptime(normalize_date(date), "%Y-%m-%d").date()
    before = any(
        (day - timedelta(days=offset)).isoformat() in aaa_series
        for offset in range(1, int(window_days) + 1)
    )
    after = any(
        (day + timedelta(days=offset)).isoformat() in aaa_series
        for offset in range(1, int(window_days) + 1)
    )
    return before and after


def reconcile_dates(
    dates: Sequence[str],
    series_list: Sequence[str],
    *,
    market_fetcher=None,
    aaa_series: Optional[Mapping[str, AAARow]] = None,
    sim_outcomes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    cache: Optional[Dict[str, Any]] = None,
    truth_tolerance: float = DEFAULT_TRUTH_TOLERANCE,
    per_series_dates: Optional[Mapping[str, Sequence[str]]] = None,
    as_of: Optional[str] = None,
    retention_days: int = RETENTION_DAYS,
    aaa_gap_window_days: int = AAA_GAP_WINDOW_DAYS,
) -> Dict[str, Any]:
    """Reconcile every (series, date) pair, updating the settlement cache.

    ``per_series_dates`` restricts which pairs are reconciled, so a run that
    mixes cadences (monthly plus daily) does not reconcile the monthly series
    against a Tuesday. Without it the full cross product is reconciled: the
    off-cadence pairs come back ``NO_EVENT`` -- explained, but noise -- and the
    period count the coverage floor is scaled from becomes the cross product
    rather than the number of periods that exist, so a healthy mixed-cadence run
    is judged against a denominator it was never going to fill. It is threaded
    through to :func:`evaluate_coverage` for that reason.
    """
    if market_fetcher is None:
        market_fetcher = fetch_event_markets
    if cache is None:
        cache = load_settlement_cache()
    if sim_outcomes is None:
        sim_outcomes = load_sim_outcomes()
    if aaa_series is None:
        try:
            # include_suspect: a ``quality=suspect`` row is excluded from FITS by
            # contract §1.1, but for a reconcile it is evidence -- comparing it
            # against Kalshi's own settlement input is an independent audit of
            # the suspect flag itself, and excluding it instead means the
            # authority was never consulted for that date. Suspect values are
            # verified but never promoted into the settlement cache below, so the
            # runtime settlement path (which excludes them) is unchanged.
            aaa_series = load_aaa_series(include_suspect=True)
        except GasTruthError as exc:
            logger.error(
                "AAA series unusable (%s); every outcome check will report "
                "NO_AAA_VALUE and the coverage floor will fail this run",
                exc,
            )
            aaa_series = {}

    period_summaries: List[Dict[str, Any]] = []
    for date in dates:
        for series_ticker in series_list:
            if per_series_dates is not None:
                allowed = per_series_dates.get(series_ticker) or per_series_dates.get(
                    series_ticker.upper()
                )
                if allowed is not None and date not in allowed:
                    continue
            value, source, source_url, quality = _aaa_value_for(date, aaa_series)
            markets = market_fetcher(series_ticker, date)
            period = reconcile_period(
                series_ticker,
                date,
                markets,
                value,
                sim_outcomes,
                truth_source=source,
                truth_tolerance=truth_tolerance,
                as_of=as_of,
                retention_days=retention_days,
            )
            period["truth_quality"] = quality
            # An ABSENT AAA row that the series demonstrably brackets is the
            # contract's own §1.1 gap policy, not a degraded harvest, so the
            # per-period OUTCOME floor is exempted for it -- named, counted and
            # printed, never silent. The markets and semantics floors still
            # apply, and a run in which every period is exempt still fails.
            if value is None and aaa_gap_is_bracketed(
                date, aaa_series, window_days=aaa_gap_window_days
            ):
                period["verified_exempt"] = "AAA_GAP"
            period_summaries.append(period)

            # Record what we learned, so the next run is cheaper and the
            # runtime resolver can settle from the cache. ``record_truth`` can
            # only ever IMPROVE the cache: a run whose AAA series is partial
            # passes value=None for the dates it could not resolve, and a null
            # must never displace a number an earlier healthy run established
            # (the runtime resolver is cache-first, so that demoted the
            # settlement path's primary source).
            #
            # A ``quality=suspect`` value is VERIFIED above but never promoted
            # into the cache: the runtime resolver is cache-first and its CSV
            # fallback excludes suspect rows, so caching one would settle a
            # position on a value the runtime's own policy rejects. The cache
            # write therefore sees None for a suspect date, which under the
            # improve-only rule leaves any prior good entry untouched.
            cacheable = value if quality == "ok" else None
            if cacheable is None and value is not None:
                logger.warning(
                    "[GasSettle] %s %s: AAA value %.3f is quality=%r -- verified "
                    "against Kalshi but NOT written to the settlement cache, "
                    "because the runtime settlement path excludes suspect rows",
                    series_ticker,
                    date,
                    value,
                    quality,
                )
            pinned = period.get("pinned")
            if cacheable is not None or pinned:
                record_truth(
                    cache,
                    series_ticker,
                    date,
                    cacheable,
                    source=source or SOURCE_KALSHI_SETTLEMENT,
                    source_url=source_url,
                    pinned=None,
                )
                if pinned:
                    cache["truth"][truth_key(series_ticker, date)]["pinned"] = pinned

            for market in markets or []:
                ticker = str(market.get("ticker") or "")
                if not ticker:
                    continue
                cache.setdefault("markets", {})[ticker] = {
                    "result": (market.get("result") or "").lower() or None,
                    "status": market.get("status"),
                    "expiration_value": market.get("expiration_value"),
                    "strike_type": market.get("strike_type"),
                    "floor_strike": market.get("floor_strike"),
                }

    totals = aggregate(period_summaries)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dates": list(dates),
        "series": [s.upper() for s in series_list],
        "periods": period_summaries,
        "totals": totals,
        # How many sim records existed at all, so the report can say plainly
        # that the sim-vs-truth leg had nothing to check rather than implying
        # it passed (the same silent-success trap as the coverage floor).
        "sim_records": len(sim_outcomes),
        "aaa_rows_loaded": len(aaa_series),
        "per_series_dates": (
            {k: list(v) for k, v in per_series_dates.items()}
            if per_series_dates is not None
            else None
        ),
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
    lines.append("AAA GAS SETTLEMENT RECONCILIATION REPORT (PRD FR-4.4)")
    lines.append("=" * 78)
    lines.append(f"  Generated          : {summary['generated_at']}")
    lines.append(f"  Settlement dates   : {', '.join(summary['dates'])}")
    lines.append(f"  Series             : {', '.join(summary['series'])}")
    lines.append(f"  AAA rows loaded    : {summary.get('aaa_rows_loaded', 0)}")
    lines.append(f"  Markets checked    : {totals['markets_checked']}")
    lines.append(
        f"  Semantics verified : {totals.get('semantics_verified', 0)}  "
        f"(vs Kalshi's own expiration_value)"
    )
    lines.append(
        f"  Outcomes verified  : {totals.get('verified', 0)}  "
        f"(vs OUR published AAA value -- the FR-4.4 authority)"
    )
    lines.append(f"  Matched            : {totals['matched']}")
    lines.append(f"  Explained (no breach) : {totals['explained']}")
    lines.append(
        f"  Truth exceptions   : {totals.get('truth_exceptions', 0)}  "
        f"(registered, dated, outcome-immaterial -- see TRUTH EXCEPTIONS)"
    )
    lines.append(
        f"  UNEXPLAINED        : {totals['unexplained']}  (threshold {threshold})"
    )
    lines.append("")
    lines.append("  Per settlement period:")
    lines.append(
        f"    {'DATE':11s} {'SERIES':11s} {'AAA':>7s} {'SRC':14s} "
        f"{'MKTS':>5s} {'SEM':>4s} {'VERI':>5s} {'OK':>4s} {'EXPL':>5s} {'BAD':>4s} "
        f"{'PINNED (low, high]':>22s}"
    )
    lines.append("    " + "-" * 100)
    for period in summary["periods"]:
        pinned = period.get("pinned") or {}
        interval = (
            f"({_fmt(pinned.get('value_low_exclusive'))}, "
            f"{_fmt(pinned.get('value_high_inclusive'))}]"
            if pinned
            else "--"
        )
        # A '?' suffix marks a quality=suspect AAA row: verified against Kalshi
        # here, but never promoted into the settlement cache.
        source_label = str(period["truth_source"] or "--")
        if period.get("truth_quality") not in (None, "ok"):
            source_label = f"{source_label}?"
        lines.append(
            f"    {period['date']:11s} {period['series_ticker']:11s} "
            f"{(('%.3f' % period['aaa_value']) if period['aaa_value'] is not None else '--'):>7s} "
            f"{source_label:14s} "
            f"{period['markets_checked']:5d} {period.get('semantics_verified', 0):4d} "
            f"{period.get('verified', 0):5d} {period['matched']:4d} "
            f"{period['explained']:5d} {period['unexplained']:4d} "
            f"{interval:>22s}"
        )

    # Coverage: "0 unexplained" is only evidence if something was verified.
    if coverage is not None:
        lines.append("")
        lines.append("  Coverage floor (a run that checks nothing must not pass):")
        lines.append(
            f"    markets fetched     : {coverage['markets_checked']:4d}  "
            f"(floor {coverage['markets_floor']} over {coverage['periods']} period(s))"
        )
        lines.append(
            f"    semantics verified  : {coverage['semantics_verified']:4d}  "
            f"(floor {coverage['semantics_floor']})"
        )
        lines.append(
            f"    outcomes verified   : {coverage['verified']:4d}  "
            f"(floor {coverage['verified_floor']})"
        )
        lines.append(
            f"    COVERAGE            : {'OK' if coverage['ok'] else 'FAILED'}"
        )

        # PER-PERIOD floors, printed pass or fail. An aggregate cannot see a
        # period that verified nothing against the settlement authority, so the
        # per-period counts are stated for every period on every run -- and the
        # number of periods EXCLUDED is stated with its rationale, so a widening
        # carve-out shows up as a rising number.
        per_period = coverage.get("per_period")
        if per_period:
            lines.append("")
            lines.append(
                "  Per-period coverage floor (an aggregate hides a period that "
                "verified nothing):"
            )
            lines.append(
                f"    {'DATE':11s} {'SERIES':11s} {'MKTS':>9s} {'SEM':>9s} "
                f"{'VERI':>9s}  VERDICT"
            )
            lines.append("    " + "-" * 70)
            for entry in per_period:
                if entry["excluded"]:
                    verdict = f"EXCLUDED ({entry['excluded']})"
                elif not entry["ok"]:
                    verdict = "FAILED"
                elif entry["verified_exempt"]:
                    verdict = f"OK (outcome floor EXEMPT: {entry['verified_exempt']})"
                elif entry.get("truth_quality") not in (None, "ok"):
                    verdict = f"OK (AAA row quality={entry['truth_quality']})"
                else:
                    verdict = "OK"
                lines.append(
                    f"    {str(entry['date']):11s} {str(entry['series']):11s} "
                    f"{entry['markets_checked']:4d}/{entry['markets_floor']:<4d} "
                    f"{entry['semantics_verified']:4d}/{entry['semantics_floor']:<4d} "
                    f"{entry['verified']:4d}/{entry['verified_floor']:<4d}  {verdict}"
                )
            lines.append(
                f"    periods evaluated   : {coverage.get('periods_evaluated', 0)}  "
                f"({coverage.get('periods_failing', 0)} failing)"
            )
            lines.append(
                f"    periods EXCLUDED    : {coverage.get('periods_excluded', 0)}  "
                f"(Kalshi no longer serves settled markets older than "
                f"~{RETENTION_DAYS} days; excluded periods verify nothing and "
                f"are exempt from the per-period floors)"
            )
            lines.append(
                f"    outcome floor EXEMPT: "
                f"{coverage.get('periods_verified_exempt', 0)}  "
                f"(AAA has no row for the date and the series brackets it within "
                # ASCII only: this text is printed, and a Windows console in a
                # legacy codepage mangles a section sign into a replacement char.
                f"{AAA_GAP_WINDOW_DAYS} day(s) either side -- data contract 1.1 "
                f"records a missing day as a missing row. The markets and "
                f"semantics floors still applied; a run where EVERY in-scope "
                f"period is exempt fails)"
            )
            for entry in coverage.get("verified_exempt", []):
                lines.append(
                    f"      ~ {entry['date']} {entry['series']}: outcome floor "
                    f"exempt ({entry['verified_exempt']}) -- no AAA row for this "
                    f"date, series covers the neighbourhood"
                )
        for failure in coverage["failures"]:
            lines.append(f"      ! {failure}")

    # The sim-vs-truth leg, stated even (especially) when it had no work.
    sim_records = summary.get("sim_records")
    sim_checked = totals.get("sim_checked", 0)
    lines.append("")
    if sim_records is None:
        lines.append("  Sim leg            : not evaluated in this run")
    elif sim_checked:
        lines.append(
            f"  Sim leg            : {sim_checked} reconciled market(s) carried a "
            f"sim record (of {sim_records} loaded); each was compared to Kalshi's result"
        )
    else:
        lines.append(
            f"  Sim leg            : NOTHING TO CHECK -- {sim_records} sim record(s) "
            f"loaded, none for a reconciled market. No gas position has been "
            f"opened yet, so SIM_MISMATCH is untested against live data here "
            f"(it is covered by unit tests)."
        )

    by_cat = {k: v for k, v in totals["by_category"].items() if k != "MATCH"}
    if by_cat:
        lines.append("")
        lines.append(
            "  Non-match categories (explained items are listed, never dropped):"
        )
        for cat, count in sorted(by_cat.items()):
            tag = "explained" if cat in EXPLAINED_CATEGORIES else "UNEXPLAINED"
            lines.append(f"    {cat:22s} {count:4d}   [{tag}]")

    # Registered truth exceptions, stated in full with their evidence. An
    # exception summarised as one truncated line is an exception nobody revisits.
    applied = sorted(
        {
            (p["series_ticker"], p["date"], r["truth_exception"])
            for p in summary["periods"]
            for r in p["rows"]
            if r.get("truth_exception")
        }
    )
    if applied:
        lines.append("")
        lines.append(
            "  REGISTERED TRUTH EXCEPTIONS APPLIED (explained; the $"
            f"{DEFAULT_TRUTH_TOLERANCE:g} tolerance was NOT widened):"
        )
        for series, date, rule in applied:
            entry = TRUTH_EXCEPTIONS.get((series, date), {})
            count = sum(
                1
                for p in summary["periods"]
                if p["series_ticker"] == series and p["date"] == date
                for r in p["rows"]
                if r.get("truth_exception")
            )
            lines.append(
                f"    {date} {series} {rule}  registered "
                f"{entry.get('registered_on', '?')}  applied to {count} market(s)"
            )
            lines.append(
                f"        Kalshi {entry.get('kalshi_expiration_value')} vs ours "
                f"{entry.get('our_aaa_value')}; every excepted market's outcome "
                f"leg still ran against OUR value and agreed with Kalshi's result"
            )
            for path in entry.get("evidence_paths", ()):
                lines.append(f"        evidence: {path}")

    explained_rows = [
        (p, r)
        for p in summary["periods"]
        for r in p["rows"]
        if r["category"] != "MATCH" and r.get("explained", True)
    ]
    if explained_rows:
        lines.append("")
        lines.append("  Explained detail:")
        for period, row in explained_rows[:30]:
            lines.append(
                f"    {period['date']} {str(row['ticker'])[:28]:28s} "
                f"{row['category']:18s} {row.get('detail', '')[:44]}"
            )
        if len(explained_rows) > 30:
            lines.append(f"    ... and {len(explained_rows) - 30} more")

    bad = totals["unexplained_rows"]
    if bad:
        lines.append("")
        lines.append("  UNEXPLAINED MISMATCHES:")
        for row in bad[:25]:
            lines.append(
                f"    {row.get('date')} {str(row['ticker'])[:32]:32s} {row['category']}"
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
    target = report_dir or REPORT_DIR
    os.makedirs(target, exist_ok=True)
    stamp = summary["dates"][-1] if summary.get("dates") else "unknown"
    base = os.path.join(target, f"reconcile_gas_{stamp}")

    payload = {k: v for k, v in summary.items() if k != "cache"}
    json_path = f"{base}.json"
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
    text_path = f"{base}.txt"
    with open(text_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report_text + "\n")
    return {"json": json_path, "text": text_path}


def post_discord_alert(
    summary: Mapping[str, Any],
    threshold: int,
    coverage: Optional[Mapping[str, Any]] = None,
) -> None:
    """Alert on a mismatch breach or a coverage failure (mirrors reconcile_weather)."""
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
        "Gas reconciliation verified nothing (coverage floor)"
        if coverage_failed and totals["unexplained"] <= threshold
        else "AAA gas settlement reconciliation breach"
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
                    f"Series: {', '.join(summary['series'])}\n"
                    f"Categories: {cats or 'n/a'}\n"
                    f"Checked {totals['markets_checked']} markets across "
                    f"{totals['periods']} period(s), "
                    f"{totals.get('semantics_verified', 0)} semantics and "
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
# --harvest-truth: the Kalshi-only, AAA-independent truth series
# ---------------------------------------------------------------------------
PINNED_TRUTH_COLUMNS = (
    "settlement_date",
    "series",
    "period_kind",
    "event_ticker",
    "value_low_exclusive",
    "value_high_inclusive",
    "interval_width",
    "n_markets",
    "n_yes",
    "n_no",
    "max_yes_strike",
    "min_no_strike",
    "kalshi_expiration_value",
    "monotonic",
    "source",
    "source_url",
    "fetched_at",
)


def _csv_number(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


#: Fields kept when a settled market is committed verbatim as evidence. Quotes,
#: order books and price history are dropped; everything that establishes the
#: settlement rule and its outcome is kept.
_EVIDENCE_FIELDS = (
    "ticker",
    "event_ticker",
    "status",
    "result",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "expiration_value",
    "settlement_ts",
    "open_time",
    "close_time",
    "expected_expiration_time",
    "settlement_value_dollars",
    "yes_sub_title",
    "no_sub_title",
    "rules_primary",
    "title",
    "volume_fp",
    "open_interest_fp",
)


def _trim_market(market: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: market.get(k) for k in _EVIDENCE_FIELDS if k in market}


def _write_evidence(
    path: str,
    markets: Sequence[Mapping[str, Any]],
    *,
    fetched_at: str,
    query: str,
    note: str,
) -> None:
    """Commit settled markets verbatim, with the query that produced them."""
    blob = {
        "_provenance": {
            "fetched_at": fetched_at,
            "generator": "scripts/reconcile_gas.py --harvest-truth",
            "endpoint": f"GET {KALSHI_PROD_URL}/markets",
            "query": query,
            "note": note,
            "market_count": len(markets),
        },
        "markets": list(markets),
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(blob, handle, indent=1, sort_keys=False)
        handle.write("\n")


def harvest_pinned_truth(
    series_list: Sequence[str],
    *,
    fetcher=None,
    fixture_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Harvest every retrievable settled ladder and persist the pinned truth.

    Writes ``kalshi_pinned_truth.csv`` plus its manifest under
    ``tests/fixtures/gas/``. The series is derived **only** from Kalshi's
    published settlement results -- no AAA source is consulted -- which is what
    makes it admissible as held-out truth for a projection fitted on the AAA
    history (``circular-constraints-justify-nothing``).
    """
    fetcher = fetcher or fetch_settled_markets
    target_dir = fixture_dir or FIXTURE_DIR
    os.makedirs(target_dir, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows: List[Dict[str, Any]] = []
    tie_markets: List[Dict[str, Any]] = []
    primary_markets: List[Dict[str, Any]] = []
    total_markets = 0
    for series_ticker in series_list:
        spec = get_series(series_ticker)
        markets = fetcher(spec.series_ticker)
        total_markets += len(markets)
        if spec.series_ticker == PRIMARY_SERIES:
            primary_markets = [_trim_market(m) for m in markets]
        by_event: Dict[str, List[Mapping[str, Any]]] = {}
        for market in markets:
            by_event.setdefault(str(market.get("event_ticker") or ""), []).append(
                market
            )
            raw = market.get("expiration_value")
            if raw not in (None, "") and market.get("floor_strike") is not None:
                try:
                    if abs(float(raw) - float(market["floor_strike"])) < 1e-9:
                        tie_markets.append(_trim_market(market))
                except (TypeError, ValueError):
                    pass
        for event, ladder in by_event.items():
            if not event:
                continue
            try:
                pinned = pin_truth_from_ladder(ladder)
            except GasSpecError as exc:
                logger.error("cannot pin %s: %s", event, exc)
                continue
            rows.append(
                {
                    "settlement_date": pinned.settlement_date,
                    "series": spec.series_ticker,
                    "period_kind": spec.cadence,
                    "event_ticker": event,
                    "value_low_exclusive": pinned.low_exclusive,
                    "value_high_inclusive": pinned.high_inclusive,
                    "interval_width": pinned.interval_width,
                    "n_markets": pinned.n_markets,
                    "n_yes": pinned.n_yes,
                    "n_no": pinned.n_no,
                    "max_yes_strike": pinned.low_exclusive,
                    "min_no_strike": pinned.high_inclusive,
                    "kalshi_expiration_value": pinned.kalshi_expiration_value,
                    "monotonic": True,
                    "source": SOURCE_KALSHI_SETTLEMENT,
                    "source_url": (
                        f"{KALSHI_PROD_URL}/markets?series_ticker="
                        f"{spec.series_ticker}&status=settled"
                    ),
                    "fetched_at": fetched_at,
                }
            )

    ties = len(tie_markets)
    rows.sort(key=lambda r: (r["settlement_date"], r["series"]))
    lines = [",".join(PINNED_TRUTH_COLUMNS)]
    for row in rows:
        lines.append(
            ",".join(
                [
                    row["settlement_date"],
                    row["series"],
                    row["period_kind"],
                    row["event_ticker"],
                    _csv_number(row["value_low_exclusive"]),
                    _csv_number(row["value_high_inclusive"]),
                    _csv_number(row["interval_width"]),
                    str(row["n_markets"]),
                    str(row["n_yes"]),
                    str(row["n_no"]),
                    _csv_number(row["max_yes_strike"]),
                    _csv_number(row["min_no_strike"]),
                    _csv_number(row["kalshi_expiration_value"]),
                    "true",
                    row["source"],
                    row["source_url"],
                    row["fetched_at"],
                ]
            )
        )
    csv_text = "\n".join(lines) + "\n"
    csv_path = os.path.join(target_dir, "kalshi_pinned_truth.csv")
    with open(csv_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(csv_text)

    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault(row["period_kind"], []).append(row)
    manifest = {
        "generated_at": fetched_at,
        "generator": "scripts/reconcile_gas.py --harvest-truth",
        "endpoint": f"GET {KALSHI_PROD_URL}/markets",
        "authority": (
            "Derived exclusively from Kalshi's published settlement results. No "
            "AAA-sourced series was consulted, which is what makes this usable "
            "as held-out truth against a model fitted on the AAA history."
        ),
        "settled_markets_harvested": total_markets,
        "rows": len(rows),
        "content_sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
        "by_period_kind": {
            kind: {
                "rows": len(values),
                "first": min(r["settlement_date"] for r in values),
                "last": max(r["settlement_date"] for r in values),
                # ``None`` when every ladder in this group was one-sided, which
                # constrains the value but does not bracket it. Reported as
                # absent rather than as a number, so a one-sided harvest cannot
                # be mistaken for a tight one.
                "max_interval_width": max(
                    (
                        r["interval_width"]
                        for r in values
                        if r["interval_width"] is not None
                    ),
                    default=None,
                ),
                "one_sided": sum(1 for r in values if r["interval_width"] is None),
            }
            for kind, values in sorted(by_kind.items())
        },
        "month_end_dates": sorted(
            r["settlement_date"] for r in rows if r["period_kind"] == "monthly"
        ),
        "boundary_tie_markets": ties,
        "retention_note": (
            "The public API returns settled markets for roughly the last two "
            "months only; older events resolve but carry zero markets. The "
            "earliest retrievable settlement date at generation time is "
            + (min(r["settlement_date"] for r in rows) if rows else "n/a")
            + ". More month-ends cannot be pinned from Kalshi until they occur."
        ),
    }
    manifest_path = os.path.join(target_dir, "kalshi_pinned_truth_manifest.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=False)
        handle.write("\n")

    # The two verbatim evidence files the golden table reads. Regenerated from
    # the same harvest so the provenance in each one is the query that actually
    # produced it, and a re-run cannot silently disagree with the CSV above.
    ladders_path = os.path.join(target_dir, "kxaaagasm_settled_ladders.json")
    _write_evidence(
        ladders_path,
        primary_markets,
        fetched_at=fetched_at,
        query=f"series_ticker={PRIMARY_SERIES}&status=settled&limit=1000",
        note=(
            "Every market the public API still returns as settled for the traded "
            "monthly series, verbatim. Kalshi prunes settled markets after "
            "roughly two months, so this is the complete retrievable monthly "
            "history."
        ),
    )
    tie_markets.sort(key=lambda m: str(m.get("ticker")))
    ties_path = os.path.join(target_dir, "gas_boundary_ties.json")
    _write_evidence(
        ties_path,
        tie_markets,
        fetched_at=fetched_at,
        query="series_ticker={" + ",".join(series_list) + "}&status=settled",
        note=(
            "Settled markets whose published expiration_value equals "
            "floor_strike exactly. Every one settled NO, which is the live proof "
            "that the payoff is strictly greater-than. A >= payoff would invert "
            "every row in this file."
        ),
    )
    return {
        "csv": csv_path,
        "manifest": manifest_path,
        "manifest_blob": manifest,
        "ladders": ladders_path,
        "ties": ties_path,
    }


def load_pinned_truth(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read the committed pinned-truth series back (used by tests and WS-D)."""
    import csv as _csv

    target = path or PINNED_TRUTH_CSV
    with open(target, "r", encoding="utf-8", newline="") as handle:
        reader = _csv.DictReader(handle)
        missing = [
            c for c in PINNED_TRUTH_COLUMNS if c not in (reader.fieldnames or [])
        ]
        if missing:
            raise GasTruthError(f"{target} is missing column(s) {missing}")
        return [dict(row) for row in reader]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile Kalshi AAA gas settlements against the published AAA "
            "national average (PRD FR-4.4)."
        )
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Newest settlement date to reconcile (YYYY-MM-DD). Defaults to the "
        "most recent settled period for each series.",
    )
    parser.add_argument(
        "--periods",
        type=int,
        default=1,
        help="Reconcile this many consecutive settlement periods ending at "
        "--date (default 1). Cadence comes from the series registry.",
    )
    parser.add_argument(
        "--series",
        default=PRIMARY_SERIES,
        help=f"Comma-separated series (default {PRIMARY_SERIES}; "
        f"known {','.join(sorted(GAS_SERIES))}).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help="Unexplained mismatches tolerated before exit 1 (default 0).",
    )
    parser.add_argument(
        "--min-markets-per-period",
        type=int,
        default=DEFAULT_MIN_MARKETS_PER_PERIOD,
        help=(
            "Coverage floor: markets that must be fetched per requested period, "
            f"else exit {EXIT_COVERAGE} (default "
            f"{DEFAULT_MIN_MARKETS_PER_PERIOD}; live ladders carry 13-41)."
        ),
    )
    parser.add_argument(
        "--min-semantics-per-period",
        type=int,
        default=DEFAULT_MIN_SEMANTICS_PER_PERIOD,
        help=(
            "Coverage floor: settled markets that must be checked against "
            f"Kalshi's own expiration_value per period, else exit {EXIT_COVERAGE} "
            f"(default {DEFAULT_MIN_SEMANTICS_PER_PERIOD})."
        ),
    )
    parser.add_argument(
        "--min-verified-per-period",
        type=int,
        default=DEFAULT_MIN_VERIFIED_PER_PERIOD,
        help=(
            "Coverage floor: outcomes that must be compared against OUR AAA "
            f"value per period, else exit {EXIT_COVERAGE} (default "
            f"{DEFAULT_MIN_VERIFIED_PER_PERIOD}). This is the exit-criterion-3 leg."
        ),
    )
    parser.add_argument(
        "--truth-tolerance",
        type=float,
        default=DEFAULT_TRUTH_TOLERANCE,
        help=(
            "Absolute USD/gal tolerance between Kalshi's expiration_value and "
            f"our AAA record (default {DEFAULT_TRUTH_TOLERANCE}). Do NOT widen "
            f"this to absorb a known disagreement -- register the date in "
            f"TRUTH_EXCEPTIONS instead, where it is dated, evidenced, and "
            f"checked for immateriality per market."
        ),
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=RETENTION_DAYS,
        help=(
            "A settlement date older than this whose ladder comes back empty is "
            f"reported as PRUNED (explained, exempt from the per-period coverage "
            f"floors) rather than as an unexplained empty ladder (default "
            f"{RETENTION_DAYS}; Kalshi's observed horizon is in [59, 90) days)."
        ),
    )
    parser.add_argument(
        "--aaa-gap-window-days",
        type=int,
        default=AAA_GAP_WINDOW_DAYS,
        help=(
            "A settlement date with NO AAA row is exempted from the per-period "
            "outcome floor only if the series carries a row within this many days "
            f"on BOTH sides (default {AAA_GAP_WINDOW_DAYS}) -- contract §1.1 "
            f"records a missing day as a missing row. Set 0 to exempt nothing."
        ),
    )
    parser.add_argument(
        "--aaa-csv",
        default=AAA_DAILY_CSV,
        help="Path to the AAA daily national-average series (contract §1).",
    )
    parser.add_argument(
        "--report-dir", default=REPORT_DIR, help="Where to write the artifact."
    )
    parser.add_argument(
        "--harvest-truth",
        action="store_true",
        help="Refresh tests/fixtures/gas/kalshi_pinned_truth.csv from a live "
        "settled-market harvest of every known gas series, then exit.",
    )
    parser.add_argument(
        "--no-discord", action="store_true", help="Suppress the Discord alert."
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of the text report."
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress INFO logging.")
    return parser


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

    series_list: List[str] = []
    for raw in str(args.series).split(","):
        name = raw.strip().upper()
        if not name:
            continue
        try:
            series_list.append(get_series(name).series_ticker)
        except GasSpecError as exc:
            log(f"FATAL: {exc}")
            return 2
    if not series_list:
        log("FATAL: no series selected")
        return 2

    if args.harvest_truth:
        paths = harvest_pinned_truth(sorted(GAS_SERIES))
        blob = paths["manifest_blob"]
        log(f"Pinned truth : {paths['csv']} ({blob['rows']} rows)")
        log(f"Manifest     : {paths['manifest']} sha256={blob['content_sha256'][:16]}")
        log(f"Ladders      : {paths['ladders']}")
        log(f"Ties         : {paths['ties']}")
        log(f"Month-ends   : {blob['month_end_dates'] or 'NONE'}")
        for kind, info in blob["by_period_kind"].items():
            log(
                f"  {kind:8s} {info['rows']:3d} rows {info['first']}..{info['last']} "
                f"max interval width {info['max_interval_width']}"
            )
        log(f"Boundary ties: {blob['boundary_tie_markets']}")
        return 0

    if args.periods < 1:
        log("FATAL: --periods must be >= 1")
        return 2

    # Default to yesterday as the anchor: AAA publishes the day's average on
    # that morning (Kalshi expires 10:00 ET), so anchoring on "today" would
    # reconcile a period whose truth may not exist yet.
    anchor = normalize_date(
        args.date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    )

    # Each series has its own cadence, so the requested dates are per-series and
    # only the pairs a series actually settles on are reconciled. Reconciling
    # the cross product instead emits an explained NO_EVENT for every
    # off-cadence pair and scales the coverage floor by a period count larger
    # than the number of periods that exist.
    per_series_dates = {
        s: settlement_dates(s, anchor, args.periods) for s in series_list
    }
    dates = sorted({d for values in per_series_dates.values() for d in values})

    try:
        # See reconcile_dates: suspect rows are VERIFIED here (an independent
        # audit of the suspect flag) but never promoted into the settlement cache.
        aaa_series = load_aaa_series(args.aaa_csv, include_suspect=True)
    except GasTruthError as exc:
        log(f"AAA series unusable: {exc}")
        aaa_series = {}

    summary = reconcile_dates(
        dates,
        series_list,
        aaa_series=aaa_series,
        sim_outcomes=load_sim_outcomes(),
        truth_tolerance=args.truth_tolerance,
        per_series_dates=per_series_dates,
        # The retention decision is made against the real clock, not the
        # requested anchor: whether Kalshi still serves a ladder depends on
        # today, not on which date the operator asked about.
        as_of=None,
        retention_days=args.retention_days,
        aaa_gap_window_days=args.aaa_gap_window_days,
    )

    coverage = evaluate_coverage(
        summary,
        min_markets_per_period=args.min_markets_per_period,
        min_semantics_per_period=args.min_semantics_per_period,
        min_verified_per_period=args.min_verified_per_period,
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
            f"{coverage['markets_floor']}; {coverage['semantics_verified']} "
            f"semantics vs floor {coverage['semantics_floor']}; "
            f"{coverage['verified']} outcomes verified vs floor "
            f"{coverage['verified_floor']})"
        )
        for failure in coverage["failures"]:
            log(f"  - {failure}")
        if not args.no_discord:
            post_discord_alert(summary, args.threshold, coverage)
        return EXIT_COVERAGE

    log(
        f"OK: {totals['unexplained']} unexplained mismatches "
        f"({totals['matched']} matched, {totals['explained']} explained, "
        f"{totals.get('truth_exceptions', 0)} registered truth exception(s), "
        f"{totals['markets_checked']} markets checked, "
        f"{totals['semantics_verified']} semantics verified, "
        f"{totals['verified']} outcomes verified vs floor "
        f"{coverage['verified_floor']}; "
        f"{coverage.get('periods_evaluated', 0)} period(s) each met the "
        f"per-period floors "
        f"({coverage.get('min_verified_per_period')} outcome(s) vs OUR AAA value), "
        f"{coverage.get('periods_excluded', 0)} excluded as PRUNED, "
        f"{coverage.get('periods_verified_exempt', 0)} outcome-floor exempt as "
        f"AAA_GAP; sim leg checked "
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
