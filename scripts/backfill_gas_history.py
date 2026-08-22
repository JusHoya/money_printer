"""Backfill >=12 months of AAA daily national-average history (PRD FR-4.1/FR-4.2).

WHAT THIS DOES
--------------
AAA publishes only the current day's national average -- there is no public
history endpoint. The history therefore has to come from archived copies of the
same landing page, which the Wayback Machine has captured for years. This script

1. enumerates every archived capture of ``gasprices.aaa.com`` in the requested
   window via the **CDX API**;
2. **selects** which of them to fetch (see "WHY NOT EVERY CAPTURE" below), then
   fetches each at **concurrency 1** with a sleep between requests, into a
   resumable on-disk cache;
3. parses each with the *same* :func:`src.data.aaa_provider.parse_national_table`
   the live recorder uses, so a layout change cannot make the two diverge;
4. attributes each capture to an **ET calendar date** (see the timezone note
   below);
5. **measures** the publication-hour constant rather than assuming it, by
   sweeping candidate hours and reporting which minimises disagreements -- both
   globally and **per era**, because a constant that only holds for part of the
   span is worse than no constant at all;
6. cross-checks the result against itself two independent ways plus one external
   reference, and
7. writes ``data/gas_truth/aaa_daily_national.csv`` plus an audit artifact.

Supplies the history Phase 4 exit criterion 2 rests on. The default span is
**2022-01-01 .. 2026-07-29** (~4.6 years): EC-2 wants a fit on >=12 months *and*
month-end MAE on >=6 held-out month-ends, and a 14-month span cannot satisfy both
at once -- 12 months of training would leave only two month-ends to hold out. The
EIA and RBOB covariates come from :mod:`src.data.energy_covariates`; run
``--covariates`` to refresh them here, or that module's own CLI.

WHY NOT EVERY CAPTURE
---------------------
Wayback's crawl density is wildly uneven: ~72 captures/day through 2022 against
~2/day in 2026, so the default span holds **~37,300 captures across ~1,570 ET
days**. Fetching all of them at concurrency 1 would take ~36 hours to learn what
saturates almost immediately, because every post-publication capture of a given
day reports the same number -- a fact the intra-day consistency check measures
rather than assumes.

:func:`select_captures` therefore takes ~2,700 of them in two purposeful strata
(one anchor per day for the *values*, a quarterly dense-hour sample for the
*boundary*), always keeping anything already cached, and reports the excluded
count with its rationale on every run. ``--unlimited-captures`` fetches
everything if that is ever wanted.

WHY THE TIMEZONE IS THE WHOLE BALL GAME
---------------------------------------
Wayback timestamps are **UTC**; AAA publishes each morning **ET**; the contract
settles on an ET calendar date. Attributing a ``02:00Z`` capture to its UTC date
puts the value one day late for *every* row in the series -- a silent,
uniform off-by-one that no row-count or spot-check would reveal, and that would
bias every fitted lag in FR-4.2 by a full day. This project has already lost
months to precisely this class of bug (``feedback_timezone_kalshi_symbols``:
Kalshi symbols are ET, the VM is UTC, and training silently saw zero samples).

So the attribution is not merely implemented, it is **falsified**:

* **Chain check** (the strong one). The page prints both "Current Avg." and
  "Yesterday Avg.", so for captures attributed to consecutive dates ``D-1`` and
  ``D``, snapshot ``D``'s *Yesterday* must equal snapshot ``D-1``'s *Current*. A
  parse error, a timezone off-by-one, or a mis-selected column each break this;
  essentially nothing else does. Reported as ``disagreements`` out of
  ``comparable``.
* **Hour sweep**. The same check is recomputed under every candidate
  publication hour. The hour that minimises disagreements is the measured
  value of ``PUBLICATION_HOUR_ET``, printed so a red-team can see the margin
  rather than take the constant on faith.
* **Intra-day check** (independent of both). Wayback often holds several
  captures of the same ET day. Every capture attributed to one date must report
  the same *Current* value. This tests the parse and the attribution without
  using the Yesterday column at all, so it does not share a failure mode with
  the chain check.
* **Per-era sweep** (:func:`sweep_by_era`). The hour is re-identified within each
  capture year. A publication time that moved mid-span misattributes days of the
  affected era while that era's own chain check stays clean, and the global rate
  only degrades slightly -- so a global-only sweep averages the defect away. A
  disagreement between eras fails the run. The *row-level* cost of a simpler
  schedule is a different and much smaller number, measured separately by
  :func:`schedule_row_effect`; the rate must never be quoted as if it were that.
* **Schedule comparison** (:func:`compare_schedules`, :func:`schedule_row_effect`).
  Named alternatives to the shipped piecewise schedule are scored on every run and
  persisted, so the boundary choice cites an artifact rather than a remembered
  number.
* **External check vs EIA** (:func:`cross_check_against_eia`). The checks above are
  all internal: a systematically wrong column is perfectly self-consistent. Only
  an independent surveyor of the same quantity catches it. Note what it does
  **not** test: the AAA-EIA methodological offset is ~13.5 mills while a daily AAA
  move is 1-5 mills, so shifting the whole series by one day is invisible to it. It
  validates the *level*, never the alignment. The day-sensitive evidence for
  alignment is the chain check plus the record-date pin in
  ``tests/test_gas_backfill.py``.
* **Series reconciliation** (:func:`reconcile_with_disk`). Whether this artifact
  describes the file beside it. A date on disk this run did not reproduce is named
  rather than silently inherited through first-writer-wins.

GAPS
----
Wayback coverage is dense but not complete. Per contract §1.1 a day with no
capture is a **missing row**: this script has no interpolation path. It reports how many days in the window are missing so a consumer can
decide, and FR-4.2's model interpolates in memory and reports what it filled.

A capture that parses but looks wrong is written with ``quality=suspect``, never
dropped and never silently trusted.

USAGE
-----
::

    $env:PYTHONPATH = "."
    python scripts/backfill_gas_history.py                    # full 2022+ span
    python scripts/backfill_gas_history.py --audit-only       # re-audit the cache
    python scripts/backfill_gas_history.py --covariates       # + EIA/RBOB
    python scripts/backfill_gas_history.py --regenerate       # rewrite all rows

``--audit-only`` re-runs every check over the cached captures without a single
network request, which is how the evidence is reproduced cheaply.

The raw-capture cache defaults to a directory **outside the repository** (under
the OS temp dir): the selected captures are ~250 MB of HTML, and ``.gitignore`` is
orchestrator-owned, so defaulting in-repo would risk committing it. Pass
``--cache-dir data/gas_truth/cache`` if that path is ignored.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import ssl
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

if __package__ in (None, ""):  # pragma: no cover - direct-script invocation
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from src.data.aaa_provider import (
    AAA_CSV_PATH,
    AAA_URL,
    DEFAULT_USER_AGENT,
    GAS_TRUTH_DIR,
    MANIFEST_PATH,
    MAX_DAILY_JUMP,
    MAX_PLAUSIBLE_VALUE,
    MIN_PLAUSIBLE_VALUE,
    PUBLICATION_SCHEDULE,
    QUALITY_SUSPECT,
    SOURCE_WAYBACK,
    AAAObservation,
    AAAUnavailable,
    attribute_et_date,
    attribution_evidence,
    attribution_inconsistencies,
    check_yesterday_chain,
    flag_suspect_rows,
    parse_national_table,
    publication_hour_for,
    read_aaa_csv,
    update_manifest,
    upsert_observations,
    wayback_timestamp_to_et,
)


def _reattribute(obs: AAAObservation, hour: int) -> AAAObservation:
    """Recompute an observation's ET date under an explicit publication hour."""
    from dataclasses import replace as _replace

    if obs.captured_at_et is None:
        return obs
    return _replace(
        obs,
        date=attribute_et_date(obs.captured_at_et, publication_hour_et=hour),
    )


logger = logging.getLogger("backfill_gas_history")

CDX_URL = "https://web.archive.org/cdx/search/cdx"
#: ``id_`` asks the replay for the **archived original bytes** rather than the
#: rewritten page with Wayback's toolbar injected. That keeps ``raw_sha256``
#: reproducible and the parse free of archive furniture.
REPLAY_TEMPLATE = "https://web.archive.org/web/{timestamp}id_/{original}"

DEFAULT_START = "2022-01-01"
DEFAULT_END = "2026-07-29"
#: One-time bulk backfill against a service built for it. Wayback publishes no
#: Crawl-delay; concurrency stays at 1 and every request is spaced.
DEFAULT_SLEEP = 3.0
DEFAULT_RETRIES = 4
DEFAULT_CACHE_DIR = os.path.join(
    tempfile.gettempdir(), "money_printer_wayback_cache", "gasprices_aaa"
)
AUDIT_PATH = os.path.join(GAS_TRUTH_DIR, "backfill_audit.json")

RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Recorded in the audit so a settled question is not re-opened. Two counts of
#: "how many snapshot-days does Wayback have for gasprices.aaa.com" circulated
#: during Phase 4 and both are right about different things.
CDX_DAY_COUNT_RECONCILIATION = {
    "question": (
        "Two snapshot-day counts for gasprices.aaa.com over 2025-06-01 .. "
        "2026-07-29 were reported during Phase 4: 362 and 348."
    ),
    "resolved": True,
    "count_362": (
        "CDX collapsed to one row per day with NO statuscode filter: 362 days "
        "have at least one capture of any status."
    ),
    "count_348": (
        "CDX filtered to statuscode=200 and mimetype text/html: 348 distinct UTC "
        "days (347 distinct ET days) have a capture that can actually be parsed."
    ),
    "difference": (
        "14 days whose every capture is a 301 redirect or an archived 403/error "
        "page. Measured unfiltered totals over that window: 886x 200, 173x 301, "
        "37x 403, 8x '-', 1x 204."
    ),
    "authoritative_for_this_backfill": (
        "348 UTC / 347 ET. A 301 carries no averages table and an archived 403 is "
        "an error page; parsing either would fabricate a row or a spurious "
        "suspect, so only 200 + text/html captures are admissible."
    ),
}


def log(msg: str) -> None:
    """Print and log, so a long unattended run is followable either way."""
    print(msg, flush=True)
    logger.info(msg)


# ---------------------------------------------------------------------------
# HTTP with backoff
# ---------------------------------------------------------------------------
def _get_with_retry(
    session: requests.Session,
    url: str,
    *,
    retries: int,
    sleep_s: float,
    timeout: float = 90.0,
    params: Optional[dict] = None,
) -> Optional[requests.Response]:
    """GET with exponential backoff on transient failures.

    Wayback intermittently drops TLS connections mid-handshake
    (``SSLEOFError``) and answers bursts with 429/5xx. Observed live during
    development: an un-retried sweep died on a single ``UNEXPECTED_EOF_WHILE_
    READING``. Those are "ask again", not "this does not exist", so they are
    retried; a 404 is not.

    Returns ``None`` when every attempt failed, so the caller records a miss
    rather than treating an outage as an absent day.
    """
    delay = sleep_s
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout, params=params)
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ssl.SSLError,
        ) as exc:
            if attempt == retries:
                logger.info("GET %s failed after %d attempts: %s", url, retries, exc)
                return None
            wait = delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.info(
                "GET %s attempt %d/%d transport error (%s); retrying in %.1fs",
                url,
                attempt,
                retries,
                type(exc).__name__,
                wait,
            )
            time.sleep(wait)
            continue
        except Exception as exc:
            logger.info("GET %s raised %s: %s", url, type(exc).__name__, exc)
            return None

        if resp.status_code == 200:
            return resp
        if resp.status_code in RETRYABLE_STATUSES and attempt < retries:
            wait = delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.info(
                "GET %s attempt %d/%d HTTP %d; retrying in %.1fs",
                url,
                attempt,
                retries,
                resp.status_code,
                wait,
            )
            time.sleep(wait)
            continue
        logger.info("GET %s returned HTTP %d (not retried)", url, resp.status_code)
        return resp if resp.status_code == 200 else None
    return None


# ---------------------------------------------------------------------------
# CDX enumeration
# ---------------------------------------------------------------------------
def enumerate_snapshots(
    session: requests.Session,
    *,
    start: str,
    end: str,
    retries: int = DEFAULT_RETRIES,
    sleep_s: float = DEFAULT_SLEEP,
) -> List[Tuple[str, str, str]]:
    """List ``(timestamp, original_url, digest)`` for every usable capture.

    Only ``statuscode=200`` ``text/html`` captures are kept: a 301 carries no
    table and a 403 is an archived error page, and parsing either would create a
    fabricated row or a spurious ``suspect``.

    Deliberately **not** collapsed to one capture per day. Several captures of
    the same ET day are an asset -- they drive the intra-day consistency check --
    and a UTC-day collapse would also throw away exactly the near-midnight
    captures the publication-hour sweep needs.
    """
    params = {
        "url": "gasprices.aaa.com",
        "output": "json",
        "from": start.replace("-", ""),
        "to": end.replace("-", ""),
        "fl": "timestamp,original,digest,statuscode,mimetype",
        "limit": "100000",
    }
    resp = _get_with_retry(
        session, CDX_URL, retries=retries, sleep_s=sleep_s, params=params, timeout=180
    )
    if resp is None:
        raise RuntimeError("CDX enumeration failed after retries")
    rows = resp.json()
    if not rows:
        return []
    rows = rows[1:]  # drop the header row
    out: List[Tuple[str, str, str]] = []
    for row in rows:
        if len(row) < 5:
            continue
        ts, original, digest, status, mime = row[0], row[1], row[2], row[3], row[4]
        if status != "200" or "html" not in (mime or ""):
            continue
        out.append((ts, original, digest))
    out.sort(key=lambda r: r[0])
    return out


# ---------------------------------------------------------------------------
# Capture selection
# ---------------------------------------------------------------------------
#: A capture in this ET hour range is unambiguously *after* publication under
#: every candidate boundary hour (0..12), so it can anchor a day's value without
#: assuming where the boundary is. Non-circular by construction.
ANCHOR_ET_WINDOW = (12, 20)
ANCHOR_ET_TARGET = 15

#: Boundary identification needs captures at *consecutive* ET hours: candidates
#: ``h`` and ``h+1`` differ only in where an hour-``h`` capture lands, so a sample
#: with no capture at hour ``h`` cannot distinguish them (asserted in
#: ``tests/test_gas_backfill.py``). Hence one capture per ET hour across this
#: range on the sampled days.
BOUNDARY_ET_RANGE = (0, 13)
DEFAULT_BOUNDARY_DAYS_PER_QUARTER = 8


def _quarter(day: _date) -> str:
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def select_captures(
    captures: Sequence[Tuple[str, str, str]],
    *,
    already_cached: Optional[set] = None,
    boundary_days_per_quarter: int = DEFAULT_BOUNDARY_DAYS_PER_QUARTER,
    unlimited: bool = False,
) -> Tuple[List[Tuple[str, str, str]], Dict[str, object]]:
    """Choose which archived captures to fetch, and say what was left out.

    Wayback's coverage of this page is wildly uneven: ~72 captures/day through
    2022 against ~2/day in 2026, so the 2022-01-01 .. 2026-07-29 span holds
    ~37,500 captures. Fetching all of them at concurrency 1 would take on the
    order of 36 hours for information that saturates long before that -- every
    capture of one day after publication reports the same number.

    Two purposes, so two strata, and the split is stated rather than implied:

    * **anchor** -- one capture per ET day from :data:`ANCHOR_ET_WINDOW`, nearest
      :data:`ANCHOR_ET_TARGET`. This is what produces the daily series. The
      window is chosen so the capture is post-publication under *any* candidate
      boundary, so selecting it does not presuppose the answer to the boundary
      question.
    * **boundary** -- on a deterministic sample of days per calendar quarter, one
      capture per ET hour across :data:`BOUNDARY_ET_RANGE`. This is what
      identifies the publication hour, and stratifying by quarter is what lets a
      *shift* in that hour be localised to a quarter rather than smeared across
      the whole span.

    Days with no anchor-window capture contribute all their in-range captures
    instead, so a thin day is not silently dropped.

    Anything already on disk is always included -- it is free evidence, and
    discarding it would throw away density this backfill already paid for.

    Selection is deterministic (evenly spaced indices, no RNG) so a re-run
    fetches the same set and the artifact stays reproducible.

    Returns ``(selected, stats)``; ``stats`` reports how many captures were
    excluded and why, per ``measure-what-a-gate-excludes`` -- an undisclosed
    exclusion looks identical to full coverage.
    """
    cached = already_cached or set()
    by_day: Dict[_date, List[Tuple[str, str, str, datetime]]] = defaultdict(list)
    bad_ts = 0
    for ts, original, digest in captures:
        try:
            et = wayback_timestamp_to_et(ts)
        except ValueError:
            bad_ts += 1
            continue
        by_day[et.date()].append((ts, original, digest, et))

    if unlimited:
        selected = [(t[0], t[1], t[2]) for day in sorted(by_day) for t in by_day[day]]
        return selected, {
            "strategy": "unlimited",
            "total_captures": len(captures),
            "selected": len(selected),
            "excluded": 0,
            "bad_timestamps": bad_ts,
        }

    # -- boundary stratum: pick sample days per quarter, deterministically ----
    days_by_quarter: Dict[str, List[_date]] = defaultdict(list)
    for day in sorted(by_day):
        days_by_quarter[_quarter(day)].append(day)
    boundary_days: set = set()
    for quarter, days in sorted(days_by_quarter.items()):
        n = min(boundary_days_per_quarter, len(days))
        if n <= 0:
            continue
        if n == len(days):
            boundary_days.update(days)
            continue
        step = len(days) / n
        boundary_days.update(days[int(i * step)] for i in range(n))

    chosen: Dict[str, Tuple[str, str, str]] = {}
    anchor_days = 0
    thin_days = 0
    boundary_selected = 0

    for day in sorted(by_day):
        entries = sorted(by_day[day], key=lambda e: e[3])

        # anchor
        window = [
            e for e in entries if ANCHOR_ET_WINDOW[0] <= e[3].hour < ANCHOR_ET_WINDOW[1]
        ]
        if window:
            best = min(window, key=lambda e: abs(e[3].hour - ANCHOR_ET_TARGET))
            chosen[best[0]] = (best[0], best[1], best[2])
            anchor_days += 1
        else:
            # No safe anchor: take every capture this day has rather than guess.
            thin_days += 1
            for e in entries:
                chosen[e[0]] = (e[0], e[1], e[2])

        # boundary
        if day in boundary_days:
            seen_hours = set()
            for e in entries:
                hour = e[3].hour
                if not (BOUNDARY_ET_RANGE[0] <= hour < BOUNDARY_ET_RANGE[1]):
                    continue
                if hour in seen_hours:
                    continue
                seen_hours.add(hour)
                if e[0] not in chosen:
                    boundary_selected += 1
                chosen[e[0]] = (e[0], e[1], e[2])

    # -- always keep what is already on disk ---------------------------------
    free = 0
    for ts, original, digest in captures:
        if ts in cached and ts not in chosen:
            chosen[ts] = (ts, original, digest)
            free += 1

    selected = [chosen[k] for k in sorted(chosen)]
    return selected, {
        "strategy": "stratified anchor + quarterly boundary sample",
        "total_captures": len(captures),
        "selected": len(selected),
        "excluded": len(captures) - len(selected) - bad_ts,
        "bad_timestamps": bad_ts,
        "distinct_et_days": len(by_day),
        "days_with_anchor": anchor_days,
        "thin_days_taken_whole": thin_days,
        "boundary_sample_days": len(boundary_days),
        "boundary_only_captures": boundary_selected,
        "already_cached_kept": free,
        "anchor_et_window": list(ANCHOR_ET_WINDOW),
        "boundary_et_range": list(BOUNDARY_ET_RANGE),
        "boundary_days_per_quarter": boundary_days_per_quarter,
        "exclusion_rationale": (
            "Captures beyond one anchor per ET day plus the quarterly boundary "
            "sample carry no additional information about the daily value: every "
            "post-publication capture of a day reports the same number, verified "
            "by the intra-day consistency check. Excluding them turns a ~36-hour "
            "fetch into ~1 hour."
        ),
    }


# ---------------------------------------------------------------------------
# Capture cache
# ---------------------------------------------------------------------------
def _cache_path(cache_dir: str, timestamp: str) -> str:
    return os.path.join(cache_dir, f"{timestamp}.html")


def _index_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "index.json")


def read_cache_index(cache_dir: str) -> Dict[str, str]:
    """Map cached timestamp -> the ``original`` URL it was archived under.

    Needed because the captures are not all under one URL: CDX returns this page
    under ``https://``, ``http://`` and ``www.`` variants, and the replay URL
    embeds whichever one the capture used. Reconstructing a single canonical
    ``original`` in ``--audit-only`` mode would record a ``source_url`` that does
    not resolve to the capture the row was actually parsed from -- a provenance
    column that quietly stops being provenance.
    """
    path = _index_path(cache_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in (data or {}).items()}
    except (json.JSONDecodeError, OSError) as exc:
        logger.info("cache index unreadable (%s); treating as empty", exc)
        return {}


def _record_cache_index(cache_dir: str, timestamp: str, original: str) -> None:
    """Append one timestamp -> original mapping to the cache index."""
    index = read_cache_index(cache_dir)
    if index.get(timestamp) == original:
        return
    index[timestamp] = original
    tmp = f"{_index_path(cache_dir)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(index, fh, indent=0, sort_keys=True)
        os.replace(tmp, _index_path(cache_dir))
    except OSError as exc:  # pragma: no cover - index is an optimisation
        logger.info("could not update cache index: %s", exc)


#: A served capture instant may differ from the requested CDX timestamp by this
#: much and still be the same capture. CDX timestamps and ``Memento-Datetime``
#: agree exactly on every capture inspected; the tolerance exists only so a
#: one-second representational difference cannot invent a miss.
SERVED_TIMESTAMP_TOLERANCE_S = 2

_REPLAY_TS_RE = re.compile(r"/web/(\d{14})")


def served_timestamp(resp) -> Optional[str]:
    """The 14-digit UTC stamp of the capture Wayback **actually served**.

    ``Memento-Datetime`` is the authority; the post-redirect URL is the fallback.
    ``None`` means the response identified no capture, which is not treated as a
    mismatch -- an absent header must not manufacture a miss.
    """
    headers = getattr(resp, "headers", None) or {}
    raw = None
    try:
        raw = headers.get("Memento-Datetime")
    except AttributeError:  # pragma: no cover - dict-like is all that is needed
        raw = None
    if raw:
        try:
            return (
                parsedate_to_datetime(str(raw))
                .astimezone(timezone.utc)
                .strftime("%Y%m%d%H%M%S")
            )
        except (TypeError, ValueError, OverflowError):
            pass
    match = _REPLAY_TS_RE.search(str(getattr(resp, "url", "") or ""))
    return match.group(1) if match else None


def _timestamps_agree(requested: str, served: str) -> bool:
    try:
        a = datetime.strptime(requested, "%Y%m%d%H%M%S")
        b = datetime.strptime(served, "%Y%m%d%H%M%S")
    except ValueError:
        return requested == served
    return abs((a - b).total_seconds()) <= SERVED_TIMESTAMP_TOLERANCE_S


def fetch_snapshot(
    session: requests.Session,
    timestamp: str,
    original: str,
    *,
    cache_dir: str,
    sleep_s: float,
    retries: int,
    offline: bool = False,
) -> Optional[bytes]:
    """Return the archived bytes for one capture, using the cache when present.

    A published archive capture is immutable, so a hit is cached without expiry.
    A **failure is never cached** -- caching an absence would turn a transient
    Wayback outage into a permanent hole in the history, the failure mode
    :mod:`src.data.iem_cli_provider` documents from the CLI archive.

    THE REPLAY URL DOES NOT PROMISE THE CAPTURE YOU ASKED FOR
    --------------------------------------------------------
    ``/web/<timestamp>id_/<url>`` answers with the **nearest** capture when the
    requested one is not available, and it answers ``200`` while doing so. So a
    request for a timestamp that does not exist silently returns a *different
    day's page*, and the row built from it would be dated by the requested
    timestamp while its bytes came from another day -- a wrong value under a
    provenance URL that still resolves, which is the worst shape a data defect
    can take.

    Measured on this series: ``/web/20260728110106id_/https://gasprices.aaa.com/``
    returns HTTP 200 serving the 2026-07-27 capture
    (``Memento-Datetime: Mon, 27 Jul 2026 11:01:14 GMT``). Every response is
    therefore checked against :func:`served_timestamp` and a mismatch is a
    ``CAPTURE_MISS``: no row, no cache entry, and the day stays a legitimate gap.
    """
    path = _cache_path(cache_dir, timestamp)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        _record_cache_index(cache_dir, timestamp, original)
        with open(path, "rb") as fh:
            return fh.read()
    if offline:
        return None

    url = REPLAY_TEMPLATE.format(timestamp=timestamp, original=original)
    resp = _get_with_retry(session, url, retries=retries, sleep_s=sleep_s, timeout=90.0)
    time.sleep(sleep_s)
    if resp is None:
        # _get_with_retry already logged the specific transport/HTTP reason.
        logger.info("CAPTURE_MISS %s: no response after retries", timestamp)
        return None
    served = served_timestamp(resp)
    if served is not None and not _timestamps_agree(timestamp, served):
        logger.info(
            "CAPTURE_MISS %s: Wayback served capture %s instead (nearest-capture "
            "fallback); refusing to date another day's bytes as %s. URL %s",
            timestamp,
            served,
            timestamp,
            url,
        )
        return None
    body = resp.content or b""
    if not body:
        # Previously returned None with no log at all, so a run could report N
        # failures with nothing in the log explaining any of them
        # (`make-silent-rejections-observable`). An empty 200 is a distinct
        # failure from a 403 and has to be nameable.
        logger.info(
            "CAPTURE_MISS %s: HTTP %s with a zero-length body from %s",
            timestamp,
            getattr(resp, "status_code", "?"),
            url,
        )
        return None
    os.makedirs(cache_dir, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, path)
    _record_cache_index(cache_dir, timestamp, original)
    return body


# ---------------------------------------------------------------------------
# Parse pass
# ---------------------------------------------------------------------------
def parse_captures(
    captures: Sequence[Tuple[str, str, str]],
    *,
    cache_dir: str,
    session: Optional[requests.Session],
    sleep_s: float,
    retries: int,
    offline: bool,
    publication_hour_et: Optional[int] = None,
    progress_every: int = 25,
) -> Tuple[List[AAAObservation], List[Dict[str, object]]]:
    """Fetch (or read from cache) and parse every capture.

    Returns ``(observations, failures)``. One observation per *capture*, not per
    day -- collapsing to one per day happens later, after the intra-day
    consistency check has had a chance to see the duplicates.
    """
    observations: List[AAAObservation] = []
    failures: List[Dict[str, object]] = []

    for idx, (ts, original, _digest) in enumerate(captures, start=1):
        if progress_every and idx % progress_every == 0:
            log(
                f"  [{idx}/{len(captures)}] parsed={len(observations)} "
                f"failed={len(failures)}"
            )
        body = fetch_snapshot(
            session,
            ts,
            original,
            cache_dir=cache_dir,
            sleep_s=sleep_s,
            retries=retries,
            offline=offline or session is None,
        )
        if body is None:
            failures.append(
                {"timestamp": ts, "reason_code": "FETCH_FAILED", "detail": original}
            )
            continue
        try:
            captured_at = wayback_timestamp_to_et(ts)
        except ValueError as exc:
            failures.append(
                {"timestamp": ts, "reason_code": "BAD_TIMESTAMP", "detail": str(exc)}
            )
            continue
        url = REPLAY_TEMPLATE.format(timestamp=ts, original=original)
        try:
            obs = parse_national_table(
                body,
                source_url=url,
                captured_at=captured_at,
                source=SOURCE_WAYBACK,
                publication_hour_et=publication_hour_et,
                # An archive capture's retrieval time is the capture instant;
                # recording "now" would claim provenance the row does not have.
                fetched_at=captured_at.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
            )
        except AAAUnavailable as exc:
            # Contract §1.1: what cannot be parsed confidently is NOT guessed.
            # It is recorded as a failure here and simply produces no row.
            failures.append(
                {
                    "timestamp": ts,
                    "reason_code": exc.reason_code,
                    "detail": exc.detail,
                    "url": url,
                    "bytes": len(body),
                }
            )
            continue
        observations.append(obs)

    return observations, failures


# ---------------------------------------------------------------------------
# Intra-day consistency
# ---------------------------------------------------------------------------
def check_intraday_consistency(
    observations: Sequence[AAAObservation], *, tolerance: float = 0.0005
) -> Dict[str, object]:
    """Every capture attributed to one ET date must report the same Current value.

    Independent of the Yesterday chain check: it uses only the *Current* cell, so
    a defect in the Yesterday column cannot hide here and vice versa. Two
    captures of the same day disagreeing means either the attribution split a
    real day boundary or the parse is unstable.
    """
    by_date: Dict[_date, List[AAAObservation]] = defaultdict(list)
    for obs in observations:
        by_date[obs.date].append(obs)

    multi = 0
    inconsistent: List[Dict[str, object]] = []
    for day, obs_list in sorted(by_date.items()):
        if len(obs_list) < 2:
            continue
        multi += 1
        values = {round(o.value, 3) for o in obs_list}
        if max(values) - min(values) > tolerance:
            inconsistent.append(
                {
                    "date": day.isoformat(),
                    "values": sorted(values),
                    "captures": [
                        {
                            "captured_at_et": (
                                o.captured_at_et.isoformat()
                                if o.captured_at_et
                                else None
                            ),
                            "value": o.value,
                            "url": o.source_url,
                        }
                        for o in sorted(
                            obs_list, key=lambda x: x.captured_at_et or datetime.min
                        )
                    ],
                }
            )
    return {
        "dates_with_multiple_captures": multi,
        "inconsistent_dates": len(inconsistent),
        "details": inconsistent[:50],
    }


def collapse_to_daily(
    observations: Sequence[AAAObservation],
    *,
    tolerance: float = 0.0005,
) -> List[AAAObservation]:
    """One row per ET date.

    Prefers a capture taken inside :data:`ANCHOR_ET_WINDOW` (12:00-20:00 ET),
    latest first, and only falls back to a boundary-window capture when the day
    has none.

    Ranking by absolute capture instant alone is **wrong**, and the 2022-2026
    data proved it. Under a publication hour of 5 or 6, a capture at 05:00 ET on
    day ``D`` is attributed to ``D-1`` -- and chronologically it is *later* than
    ``D-1``'s own 15:00 anchor, so a plain "latest wins" rule handed the day to
    the pre-publication capture: exactly the one most likely to sit on the wrong
    side of the boundary. Measured effect of preferring the safe window, over the
    full 2022-2026 span under the 3-era publication schedule: the daily-series
    chain disagreement rate fell from 2.60% (38/1463) to 0.82% (12/1463).

    When same-day captures disagree the surviving row is marked
    ``quality=suspect`` -- the disagreement is real information about that day
    and suppressing it would hide a parse or attribution defect.
    """
    by_date: Dict[_date, List[AAAObservation]] = defaultdict(list)
    for obs in observations:
        by_date[obs.date].append(obs)

    _floor = datetime.min.replace(tzinfo=timezone.utc)

    def _rank(o: AAAObservation):
        et = o.captured_at_et
        safe = bool(
            et is not None and ANCHOR_ET_WINDOW[0] <= et.hour < ANCHOR_ET_WINDOW[1]
        )
        return (safe, et or _floor)

    out: List[AAAObservation] = []
    for day in sorted(by_date):
        obs_list = sorted(by_date[day], key=_rank)
        chosen = obs_list[-1]
        values = {round(o.value, 3) for o in obs_list}
        if len(obs_list) > 1 and (max(values) - min(values)) > tolerance:
            from dataclasses import replace as _replace

            chosen = _replace(chosen, quality=QUALITY_SUSPECT)
            logger.info(
                "AAA_SUSPECT %s: %d same-day captures disagree %s",
                day.isoformat(),
                len(obs_list),
                sorted(values),
            )
        out.append(chosen)
    return out


def sweep_by_era(
    observations: Sequence[AAAObservation],
    *,
    era_of: Callable[[AAAObservation], str],
    min_discriminating: int = 40,
) -> Dict[str, object]:
    """Run the publication-hour sweep independently per era.

    A single global constant is only fully defensible if AAA's overnight
    publication time actually held across the whole span. If it moved -- and it
    did, twice -- a global hour misattributes some days of the affected eras while
    the global rate merely degrades rather than failing, which is the kind of
    averaged-away defect that reaches production. Only a per-era sweep exposes the
    shift at all.

    How much of the series a global constant would actually move is a **separate
    and much smaller** quantity than the rate difference, because the anchor
    stratum draws captures from a window in which no candidate hour discriminates.
    It is measured by :func:`schedule_row_effect` and persisted, so this function's
    output is never the basis for a claim about row-level impact.

    An era whose sample cannot discriminate (too few captures whose attribution
    changes between candidate hours) reports ``identified: False`` rather than an
    argmin -- an unidentifiable era must not contribute a confident number
    (``audit-fixtures-for-degeneracy``).
    """
    by_era: Dict[str, List[AAAObservation]] = defaultdict(list)
    for obs in observations:
        if obs.captured_at_et is None:
            continue
        by_era[era_of(obs)].append(obs)

    out: Dict[str, object] = {}
    for era, obs_list in sorted(by_era.items()):
        discriminating = sum(
            1
            for o in obs_list
            if len(
                {
                    attribute_et_date(o.captured_at_et, publication_hour_et=h)
                    for h in range(0, 13)
                }
            )
            > 1
        )
        sweep = attribution_evidence(obs_list)
        scored = [r for r in sweep if r["comparable"] >= 20] or sweep
        best_rate = min(r["disagreement_rate"] for r in scored)
        tied = sorted(
            int(r["publication_hour_et"])
            for r in scored
            if r["disagreement_rate"] <= best_rate
        )
        out[era] = {
            "captures": len(obs_list),
            "distinct_dates": len({o.date for o in obs_list}),
            "discriminating_captures": discriminating,
            "identified": discriminating >= min_discriminating and len(tied) == 1,
            "best_hours": tied,
            "best_rate": round(best_rate, 6),
            "profile": {
                int(r["publication_hour_et"]): round(r["disagreement_rate"], 4)
                for r in sweep
            },
        }
    return out


def mark_chain_disagreements_suspect(
    daily: Sequence[AAAObservation], chain
) -> Tuple[List[AAAObservation], set]:
    """Mark both dates of every chain disagreement ``quality=suspect``.

    A date whose *Yesterday* contradicts its neighbour's *Current* is genuinely
    questionable whatever the cause, and from the pair alone there is no way to
    say which side is at fault -- so both are marked, exactly as
    :func:`~src.data.aaa_provider.flag_suspect_rows` does for jumps. Contract
    §1.1's answer to "this row looks wrong" is ``suspect``: keep it visible,
    exclude it from fits by default, never drop it and never silently trust it.

    Returns ``(rows, implicated_dates)``.
    """
    from dataclasses import replace as _replace

    implicated = set()
    for detail in chain.details:
        day = _date.fromisoformat(str(detail["date"]))
        implicated.add(day)
        implicated.add(day - timedelta(days=1))
    present = {o.date for o in daily}
    out = [
        _replace(o, quality=QUALITY_SUSPECT) if o.date in implicated else o
        for o in daily
    ]
    return out, implicated & present


# ---------------------------------------------------------------------------
# Why each suspect row is suspect
# ---------------------------------------------------------------------------
#: ``quality=suspect`` is set by four independent rules in three modules. Without
#: the reason a consumer of ``backfill_audit.json`` can see *that* 56 rows were
#: flagged and nothing about *why* any one of them was, which makes the field
#: unactionable -- "is this a parse defect or an ordinary noisy day?" is exactly
#: the question the flag exists to raise.
SUSPECT_OUT_OF_RANGE = "value_outside_plausibility_window"
SUSPECT_SAME_DAY_DISAGREEMENT = "same_day_captures_disagree"
SUSPECT_DAILY_JUMP = "day_over_day_jump_exceeds_limit"
SUSPECT_CHAIN_DISAGREEMENT = "yesterday_vs_current_chain_disagreement"
SUSPECT_UNATTRIBUTED = "unattributed"


def suspect_reasons(
    daily: Sequence[AAAObservation],
    observations: Sequence[AAAObservation],
    chain,
    *,
    tolerance: float = 0.0005,
    max_daily_jump: float = MAX_DAILY_JUMP,
) -> Dict[_date, List[str]]:
    """Which rule(s) make each date suspect.

    Every rule is **recomputed here from the data**, not read back out of the
    flags the pipeline set. Deriving the reason from the same code that set the
    flag would make the explanation true by construction and blind to a rule that
    fires for the wrong input; recomputing means a suspect row that comes back
    :data:`SUSPECT_UNATTRIBUTED` is a real signal that a fifth path exists.
    """
    reasons: Dict[_date, List[str]] = defaultdict(list)

    by_date: Dict[_date, List[AAAObservation]] = defaultdict(list)
    for obs in observations:
        by_date[obs.date].append(obs)

    for obs in daily:
        if not (MIN_PLAUSIBLE_VALUE <= obs.value <= MAX_PLAUSIBLE_VALUE):
            reasons[obs.date].append(SUSPECT_OUT_OF_RANGE)
        group = by_date.get(obs.date) or []
        if len(group) > 1:
            values = [round(o.value, 3) for o in group]
            if max(values) - min(values) > tolerance:
                reasons[obs.date].append(SUSPECT_SAME_DAY_DISAGREEMENT)

    ordered = sorted(daily, key=lambda o: o.date)
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur.date - prev.date).days != 1:
            continue
        if abs(cur.value - prev.value) > max_daily_jump:
            reasons[prev.date].append(SUSPECT_DAILY_JUMP)
            reasons[cur.date].append(SUSPECT_DAILY_JUMP)

    for detail in getattr(chain, "details", ()) or ():
        day = _date.fromisoformat(str(detail["date"]))
        reasons[day].append(SUSPECT_CHAIN_DISAGREEMENT)
        reasons[day - timedelta(days=1)].append(SUSPECT_CHAIN_DISAGREEMENT)

    return {day: sorted(set(items)) for day, items in reasons.items()}


# ---------------------------------------------------------------------------
# Does the artifact describe the file it claims to describe?
# ---------------------------------------------------------------------------
def reconcile_with_disk(
    daily: Sequence[AAAObservation],
    *,
    csv_path: str,
    start: _date,
    end: _date,
    regenerate: bool = False,
) -> Dict[str, object]:
    """Compare the series this run produced against the rows already on disk.

    THIS IS THE CHECK WHOSE ABSENCE LET AN ORPHAN ROW HIDE
    -----------------------------------------------------
    ``upsert_observations`` is first-writer-wins by design, so a row written by an
    *earlier* run survives even when the current run does not reproduce it. The
    audit's coverage block is computed from what this run produced, so the two can
    disagree silently: the committed artifact reported ``days_present=1550`` and
    listed ``2026-07-28`` as *missing* while the CSV beside it held 1551 rows
    including one for that very date -- a row citing a Wayback snapshot that no
    longer exists, preserved and never re-enumerated.

    **Must be called before the write.** Comparing against the file this run just
    produced is a tautology, and under ``--regenerate`` it is guaranteed to pass
    while the run silently drops rows -- a check that cannot fail
    (``circular-constraints-justify-nothing``).

    Restricted to ``[start, end]``: a narrower window than the file's span is a
    legitimate way to run this script, and rows outside the window are not
    evidence of anything wrong.
    """
    on_disk = set()
    for row in read_aaa_csv(csv_path):
        try:
            day = _date.fromisoformat((row.get("date") or "").strip())
        except (TypeError, ValueError):
            continue
        if start <= day <= end:
            on_disk.add(day)
    produced = {o.date for o in daily if start <= o.date <= end}
    not_reproduced = sorted(d.isoformat() for d in on_disk - produced)
    return {
        "csv_path": csv_path,
        "window": [start.isoformat(), end.isoformat()],
        "compared_before_write": True,
        "write_mode": "regenerate" if regenerate else "first-writer-wins",
        "rows_on_disk_in_window_before_write": len(on_disk),
        "rows_produced_by_this_run": len(produced),
        "dates_on_disk_not_reproduced_by_this_run": not_reproduced,
        "dates_produced_but_absent_from_disk": sorted(
            d.isoformat() for d in produced - on_disk
        ),
        "agrees": not not_reproduced,
        "note": (
            "Under first-writer-wins a date on disk that this run did not "
            "reproduce is an orphan: it survives untouched and nothing "
            "re-enumerates it. Under --regenerate the same list is the set of "
            "rows this run DROPS. Either way it must be explained, not merely "
            "counted -- re-derive from a capture that exists, or drop it with a "
            "correction record in manifest.json."
        ),
    }


# ---------------------------------------------------------------------------
# Is the piecewise publication schedule worth its complexity?
# ---------------------------------------------------------------------------
#: Named alternatives to :data:`PUBLICATION_SCHEDULE`, measured on every run so
#: the shipped choice cites an artifact rather than a remembered number. The
#: previously documented "2-era 3.57% / 3-era 3.12% / 4-era 3.05% / 5-era 2.99%"
#: comparison appeared in no committed artifact and its era definitions were not
#: recorded, so it was not reproducible; this table replaces it.
#:
#: ``per-capture-year`` is the schedule the persisted ``publication_hour_by_era``
#: table implies (2022->5, 2023->5, 2024->6, 2025->3, 2026->3), which places the
#: third boundary at 2025-01-01 rather than the shipped 2025-04-01. Both are
#: measured here so the shipped boundary is a comparison, not an assertion.
CANDIDATE_SCHEDULES: Dict[str, Tuple[Tuple[_date, int], ...]] = {
    "single-constant-5": ((_date(2022, 1, 1), 5),),
    "single-constant-3": ((_date(2022, 1, 1), 3),),
    "2-era": ((_date(2022, 1, 1), 5), (_date(2025, 4, 1), 3)),
    "3-era-shipped": tuple(PUBLICATION_SCHEDULE),
    "3-era-boundary-2025-01-01": (
        (_date(2022, 1, 1), 5),
        (_date(2024, 1, 1), 6),
        (_date(2025, 1, 1), 3),
    ),
    "4-era-per-capture-year": (
        (_date(2022, 1, 1), 5),
        (_date(2024, 1, 1), 6),
        (_date(2025, 1, 1), 3),
        (_date(2026, 1, 1), 3),
    ),
    "5-era-quarterly-2026Q2": (
        (_date(2022, 1, 1), 5),
        (_date(2024, 1, 1), 6),
        (_date(2025, 1, 1), 3),
        (_date(2026, 1, 1), 3),
        (_date(2026, 4, 1), 4),
    ),
}


def _hour_for(day: _date, schedule: Sequence[Tuple[_date, int]]) -> int:
    hour = schedule[0][1]
    for effective_from, value in schedule:
        if day >= effective_from:
            hour = value
        else:
            break
    return hour


def _reattribute_under_schedule(
    observations: Sequence[AAAObservation], schedule: Sequence[Tuple[_date, int]]
) -> List[AAAObservation]:
    from dataclasses import replace as _replace

    out = []
    for obs in observations:
        if obs.captured_at_et is None:
            out.append(obs)
            continue
        local = obs.captured_at_et
        hour = _hour_for(local.date(), schedule)
        out.append(
            _replace(obs, date=attribute_et_date(local, publication_hour_et=hour))
        )
    return out


def compare_schedules(
    observations: Sequence[AAAObservation],
    *,
    schedules: Dict[str, Tuple[Tuple[_date, int], ...]] = None,
    tolerance: float = 0.0005,
) -> List[Dict[str, object]]:
    """Score each candidate schedule on the same metric as the hour sweep.

    Same measure as :func:`~src.data.aaa_provider.attribution_evidence` (same-date
    plus cross-date inconsistencies over every capture), so the rows here are
    directly comparable to ``publication_hour_sweep``.
    """
    out: List[Dict[str, object]] = []
    for name, schedule in (schedules or CANDIDATE_SCHEDULES).items():
        counts = attribution_inconsistencies(
            _reattribute_under_schedule(observations, schedule), tolerance=tolerance
        )
        total = counts["total_checks"]
        out.append(
            {
                "name": name,
                "eras": len(schedule),
                "schedule": [
                    {"effective_from": d.isoformat(), "hour_et": h} for d, h in schedule
                ],
                "comparable": total,
                "disagreements": counts["total_inconsistent"],
                "disagreement_rate": (
                    round(counts["total_inconsistent"] / total, 6) if total else 0.0
                ),
            }
        )
    return out


#: Highest hour the publication-hour sweep considers. A capture at or after this
#: ET hour is post-publication under **every** candidate, so its attributed date
#: is the same whichever is chosen.
CANDIDATE_HOUR_CEILING = 12


def schedule_row_effect(
    observations: Sequence[AAAObservation],
    *,
    candidate_hours: Sequence[int] = (3, 4, 5, 6, 7),
    schedule: Sequence[Tuple[_date, int]] = None,
    tolerance: float = 0.0005,
    immune_at_or_after_hour: int = CANDIDATE_HOUR_CEILING,
) -> Dict[str, object]:
    """How much of the **written series** a single global constant would change.

    The disagreement *rate* answers "which attribution is more self-consistent";
    it does not answer "how much of the series would move", and those are
    different magnitudes by an order of magnitude. Most rows cannot move at all:
    the anchor stratum draws one capture per day from 12:00-20:00 ET **by
    construction**, and no candidate hour in ``0..CANDIDATE_HOUR_CEILING`` changes
    the attribution of a capture at or after that ceiling. That structural
    immunity is measured here rather than asserted, because it is the reason the
    row-level effect is small.

    TWO METRICS, BOTH REPORTED, BECAUSE THEY ANSWER DIFFERENT QUESTIONS
    ------------------------------------------------------------------
    * ``rows_redated`` -- of the rows the schedule actually wrote, how many would
      carry a **different date**. The narrow, direct question: which rows move.
    * ``rows_moved`` -- how many rows of the resulting *series* would differ,
      counting a date present under one attribution and not the other **plus** a
      date present under both whose value differs. Wider, because re-dating one
      capture can also change which capture wins its day in
      :func:`collapse_to_daily`.

    Neither dominates the other, and the measured data says so: at hour 3 this
    series re-dates 8 rows but only 7 series rows differ, because two re-datings
    can land on the same date and a re-dated capture can be collapsed away by one
    that keeps the same value. Reporting only ``rows_redated`` understates the
    consequence to the series; reporting only ``rows_moved`` overstates how many
    rows are individually mis-dated. A single number here is a number someone will
    quote out of context, so both are persisted with their definitions.
    """
    schedule = tuple(schedule or PUBLICATION_SCHEDULE)
    baseline = collapse_to_daily(
        _reattribute_under_schedule(observations, schedule), tolerance=tolerance
    )
    base_by_date = {o.date: round(o.value, 3) for o in baseline}
    total = len(base_by_date)

    immune = sum(
        1
        for o in baseline
        if o.captured_at_et is not None
        and o.captured_at_et.hour >= immune_at_or_after_hour
    )
    in_anchor_window = sum(
        1
        for o in baseline
        if o.captured_at_et is not None
        and ANCHOR_ET_WINDOW[0] <= o.captured_at_et.hour < ANCHOR_ET_WINDOW[1]
    )

    rows = []
    for hour in candidate_hours:
        single = collapse_to_daily(
            _reattribute_under_schedule(observations, ((schedule[0][0], hour),)),
            tolerance=tolerance,
        )
        single_by_date = {o.date: round(o.value, 3) for o in single}
        only_baseline = sorted(set(base_by_date) - set(single_by_date))
        only_single = sorted(set(single_by_date) - set(base_by_date))
        differing = sorted(
            d
            for d in set(base_by_date) & set(single_by_date)
            if abs(base_by_date[d] - single_by_date[d]) > tolerance
        )
        moved = len(only_baseline) + len(only_single) + len(differing)
        redated = [
            o.date
            for o in baseline
            if o.captured_at_et is not None
            and attribute_et_date(
                o.captured_at_et,
                publication_hour_et=_hour_for(o.captured_at_et.date(), schedule),
            )
            != attribute_et_date(o.captured_at_et, publication_hour_et=hour)
        ]
        rows.append(
            {
                "single_hour_et": hour,
                "rows_under_schedule": total,
                "rows_under_single_hour": len(single_by_date),
                "rows_redated": len(redated),
                "rows_redated_pct": (
                    round(100.0 * len(redated) / total, 2) if total else 0.0
                ),
                "dates_redated": [d.isoformat() for d in redated],
                "dates_only_under_schedule": [d.isoformat() for d in only_baseline],
                "dates_only_under_single_hour": [d.isoformat() for d in only_single],
                "dates_with_a_different_value": [d.isoformat() for d in differing],
                "rows_moved": moved,
                "rows_moved_pct": (round(100.0 * moved / total, 2) if total else 0.0),
            }
        )
    return {
        "schedule": [
            {"effective_from": d.isoformat(), "hour_et": h} for d, h in schedule
        ],
        "anchor_et_window": list(ANCHOR_ET_WINDOW),
        "candidate_hour_ceiling": immune_at_or_after_hour,
        "rows_total": total,
        "rows_structurally_immune": immune,
        "rows_structurally_immune_pct": (
            round(100.0 * immune / total, 2) if total else 0.0
        ),
        "rows_whose_capture_is_in_the_anchor_window": in_anchor_window,
        "immunity_note": (
            f"A capture at or after {immune_at_or_after_hour}:00 ET is "
            f"post-publication under every candidate hour in "
            f"0..{immune_at_or_after_hour}, so its attributed date is identical "
            f"under any of them. Those rows cannot move, whatever the constant. "
            f"Counted on the capture hour rather than on the anchor window, "
            f"because an evening capture (20:00-23:59 ET) is outside the anchor "
            f"window and equally immune."
        ),
        "metric_definitions": {
            "rows_redated": (
                "Rows the schedule wrote whose own capture would be attributed to "
                "a different date under the single constant."
            ),
            "rows_moved": (
                "Rows of the resulting series that would differ: a date present "
                "under one attribution and not the other, plus a date present "
                "under both whose value differs. Neither metric dominates the "
                "other -- re-dating a capture can change which capture wins its "
                "day (raising this above rows_redated), and two re-datings can "
                "land on one date or be collapsed away by a capture carrying the "
                "same value (lowering it below)."
            ),
        },
        "by_single_hour": rows,
        "worst_case_rows_redated": max((r["rows_redated"] for r in rows), default=0),
        "worst_case_rows_moved": max((r["rows_moved"] for r in rows), default=0),
    }


# ---------------------------------------------------------------------------
# External cross-check: AAA vs EIA's independent survey of the same quantity
# ---------------------------------------------------------------------------
def cross_check_against_eia(
    observations: Sequence[AAAObservation],
    *,
    eia_csv_path: str,
    warn_threshold: float = 0.10,
) -> Dict[str, object]:
    """Compare the backfilled AAA series against EIA's weekly retail survey.

    The Yesterday-chain and intra-day checks are both *internal* -- they prove
    the series is self-consistent, which a systematically wrong column would also
    be. Reading Mid-Grade instead of Regular produces a perfectly self-consistent
    series roughly $0.50/gal too high. Only an external reference catches that,
    which is the whole point of ``reconcile-against-external-ground-truth``.

    EIA surveys the same quantity (U.S. regular retail, all formulations)
    independently of AAA and dates it to Mondays, so the comparison is
    apples-to-apples in level even though EIA's figure is a weekly average
    against AAA's single day. Small differences are expected; a systematic
    offset near a grade spread is not.

    Returns the paired count and the mean/median/max absolute difference. Absent
    or unpaired data yields ``paired: 0`` and no verdict rather than a
    pass -- an empty comparison must not read as agreement
    (``measure-what-a-gate-excludes``).

    **This check is not day-sensitive and must not be cited as evidence that the
    ET attribution is aligned.** The AAA-EIA methodological offset measures ~13.5
    mills (``mean_signed_diff``) while a daily AAA move is 1-5 mills, so shifting
    the entire series by one calendar day changes the comparison by less than its
    own baseline offset. It validates the *level* -- which is what catches a wrong
    grade column, its actual purpose -- and says nothing about alignment. The
    day-sensitive evidence is the Yesterday chain check and the AAA record-date pin
    in ``tests/test_gas_backfill.py``.
    """
    if not os.path.exists(eia_csv_path):
        return {"paired": 0, "note": f"{eia_csv_path} absent; no comparison made"}

    import csv as _csv

    eia: Dict[str, float] = {}
    with open(eia_csv_path, "r", encoding="utf-8", newline="") as fh:
        for row in _csv.DictReader(fh):
            key = (row.get("week_ending") or "").strip()
            try:
                eia[key] = float(row.get("value") or "")
            except (TypeError, ValueError):
                continue

    aaa = {
        o.date.isoformat(): o.value
        for o in observations
        if o.quality != QUALITY_SUSPECT
    }
    diffs: List[Tuple[str, float, float, float]] = []
    for day, eia_value in sorted(eia.items()):
        if day in aaa:
            diffs.append((day, aaa[day], eia_value, round(aaa[day] - eia_value, 4)))

    if not diffs:
        return {
            "paired": 0,
            "eia_rows": len(eia),
            "note": "no AAA row falls on an EIA week_ending date; no comparison made",
        }

    abs_diffs = sorted(abs(d[3]) for d in diffs)
    mean_abs = sum(abs_diffs) / len(abs_diffs)
    median_abs = abs_diffs[len(abs_diffs) // 2]
    signed = [d[3] for d in diffs]
    worst = max(diffs, key=lambda d: abs(d[3]))
    return {
        "paired": len(diffs),
        "eia_rows": len(eia),
        "mean_abs_diff": round(mean_abs, 4),
        "median_abs_diff": round(median_abs, 4),
        "max_abs_diff": round(abs(worst[3]), 4),
        "mean_signed_diff": round(sum(signed) / len(signed), 4),
        "worst_date": worst[0],
        "worst_aaa": worst[1],
        "worst_eia": worst[2],
        "exceeds_threshold": mean_abs > warn_threshold,
        "warn_threshold": warn_threshold,
        "sample": [
            {"date": d[0], "aaa": d[1], "eia": d[2], "diff": d[3]} for d in diffs[:10]
        ],
    }


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def coverage_report(
    observations: Sequence[AAAObservation], *, start: _date, end: _date
) -> Dict[str, object]:
    """Which days of the window have a row, and which do not.

    Missing days are enumerated, not merely counted: a consumer deciding whether
    the history is fit for a lag fit needs to know whether the holes are
    scattered or clustered into one dead month. 2026-02 is visibly the thin
    month in Wayback's coverage of this page.
    """
    all_dates = {o.date for o in observations}
    # Count only dates inside the window. A capture near a window edge can be
    # attributed to the day *before* the window starts (an 01:29 ET capture on
    # Jun 1 carries May 31's value), which is a legitimate row but not evidence
    # of coverage inside the window -- counting it made days_present + missing
    # exceed days_in_window.
    have = {d for d in all_dates if start <= d <= end}
    outside = sorted(d.isoformat() for d in all_dates - have)
    total = (end - start).days + 1
    missing: List[str] = []
    cur = start
    while cur <= end:
        if cur not in have:
            missing.append(cur.isoformat())
        cur += timedelta(days=1)

    by_month: Dict[str, Dict[str, int]] = {}
    cur = start
    while cur <= end:
        key = cur.strftime("%Y-%m")
        slot = by_month.setdefault(key, {"days_in_window": 0, "present": 0})
        slot["days_in_window"] += 1
        if cur in have:
            slot["present"] += 1
        cur += timedelta(days=1)

    longest_run = 0
    run = 0
    cur = start
    while cur <= end:
        if cur in have:
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 0
        cur += timedelta(days=1)

    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "days_in_window": total,
        "days_present": len(have),
        "days_missing": len(missing),
        "coverage_pct": round(100.0 * len(have) / total, 2) if total else 0.0,
        "longest_consecutive_run": longest_run,
        "rows_outside_window": len(outside),
        "dates_outside_window": outside,
        "by_month": by_month,
        "missing_days": missing,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backfill AAA daily national-average gasoline history "
        "from the Wayback Machine (PRD FR-4.1).",
    )
    p.add_argument("--start", default=DEFAULT_START, help="window start, YYYY-MM-DD")
    p.add_argument("--end", default=DEFAULT_END, help="window end, YYYY-MM-DD")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    p.add_argument("--csv-path", default=AAA_CSV_PATH)
    p.add_argument("--manifest-path", default=MANIFEST_PATH)
    p.add_argument("--audit-path", default=AUDIT_PATH)
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--limit", type=int, default=0, help="cap captures (debugging)")
    p.add_argument(
        "--publication-hour",
        type=int,
        default=None,
        help="override the ET publication hour instead of the measured default",
    )
    p.add_argument(
        "--audit-only",
        action="store_true",
        help="parse only cached captures; make no network requests at all",
    )
    p.add_argument(
        "--regenerate",
        action="store_true",
        help="rewrite every row instead of first-writer-wins (contract §1)",
    )
    p.add_argument(
        "--no-write", action="store_true", help="compute and report, write nothing"
    )
    p.add_argument(
        "--covariates",
        action="store_true",
        help="also refresh the EIA weekly and RBOB daily covariates",
    )
    p.add_argument(
        "--boundary-days-per-quarter",
        type=int,
        default=DEFAULT_BOUNDARY_DAYS_PER_QUARTER,
        help="days per calendar quarter sampled densely for publication-hour "
        "identification (default %(default)s)",
    )
    p.add_argument(
        "--unlimited-captures",
        action="store_true",
        help="fetch every archived capture instead of the stratified selection "
        "(~37,500 captures / ~36h for the full span)",
    )
    p.add_argument(
        "--max-chain-disagreement-rate",
        type=float,
        default=0.02,
        help="fail above this Yesterday-vs-Current disagreement rate "
        "(default 0.02; zero is the expected outcome)",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    start = _date.fromisoformat(args.start)
    end = _date.fromisoformat(args.end)
    os.makedirs(args.cache_dir, exist_ok=True)

    session: Optional[requests.Session] = None
    if not args.audit_only:
        session = requests.Session()
        session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    # -- 1. enumerate ----------------------------------------------------
    if args.audit_only:
        cached = sorted(
            f[:-5]
            for f in os.listdir(args.cache_dir)
            if f.endswith(".html") and len(f) == 19
        )
        index = read_cache_index(args.cache_dir)
        unknown = [ts for ts in cached if ts not in index]
        captures = [(ts, index.get(ts, AAA_URL), "") for ts in cached]
        log(f"audit-only: {len(captures)} cached captures in {args.cache_dir}")
        if unknown:
            # Say so rather than silently recording a canonical-looking URL that
            # may not resolve to the capture the row came from.
            log(
                f"  WARNING: {len(unknown)} cached captures are missing from the "
                f"cache index; their source_url falls back to {AAA_URL} and may "
                f"not match the archived variant. Re-run without --audit-only to "
                f"restore exact provenance from CDX."
            )
    else:
        log(f"Enumerating CDX captures for gasprices.aaa.com {start} .. {end} ...")
        captures = enumerate_snapshots(
            session,
            start=args.start,
            end=args.end,
            retries=args.retries,
            sleep_s=args.sleep,
        )
        log(f"CDX: {len(captures)} usable (200/text-html) captures")

    if args.limit:
        captures = captures[: args.limit]
        log(f"--limit: truncated to {len(captures)} captures")

    if not captures:
        log("No captures to process.")
        return 1

    # -- 1b. select which captures to fetch ------------------------------
    cached_ts = {
        f[:-5]
        for f in os.listdir(args.cache_dir)
        if f.endswith(".html") and len(f) == 19
    }
    captures, selection = select_captures(
        captures,
        already_cached=cached_ts,
        boundary_days_per_quarter=args.boundary_days_per_quarter,
        unlimited=args.unlimited_captures or args.audit_only,
    )
    log(
        f"Selection ({selection['strategy']}): {selection['selected']} of "
        f"{selection['total_captures']} captures, {selection['excluded']} excluded"
    )
    if not args.audit_only and not args.unlimited_captures:
        log(
            f"  {selection['distinct_et_days']} ET days; "
            f"{selection['days_with_anchor']} with a safe anchor capture, "
            f"{selection['thin_days_taken_whole']} thin days taken whole; "
            f"{selection['boundary_sample_days']} boundary-sample days "
            f"(+{selection['boundary_only_captures']} captures); "
            f"{selection['already_cached_kept']} already-cached kept free"
        )

    # -- 2/3. fetch + parse ---------------------------------------------
    log(f"Fetching/parsing at concurrency 1, sleep {args.sleep}s ...")
    observations, failures = parse_captures(
        captures,
        cache_dir=args.cache_dir,
        session=session,
        sleep_s=args.sleep,
        retries=args.retries,
        offline=args.audit_only,
        publication_hour_et=args.publication_hour,
    )
    log(f"Parsed {len(observations)} captures; {len(failures)} failed")

    if not observations:
        log("Nothing parsed - refusing to write an empty series.")
        return 1

    # -- 4. measure the publication hour --------------------------------
    # The sweep re-attributes the SAME captures under each candidate hour, so it
    # is independent of whatever hour the parse pass happened to use.
    log("Sweeping candidate ET publication hours (chain check per hour) ...")
    sweep = attribution_evidence(observations)
    for row in sweep:
        log(
            f"  hour {row['publication_hour_et']:>2}: "
            f"inconsistent {row['disagreements']:>4} / {row['comparable']:>4} "
            f"(rate {row['disagreement_rate']:.4f}) "
            f"[same-date {row['same_date_inconsistent']}/"
            f"{row['same_date_groups']}, "
            f"cross-date {row['cross_date_inconsistent']}/"
            f"{row['cross_date_pairs']}] "
            f"dates {row['distinct_dates']}"
        )
    usable = [r for r in sweep if r["comparable"] >= 30] or sweep
    best_rate = min(r["disagreement_rate"] for r in usable)
    tied = [r for r in usable if r["disagreement_rate"] <= best_rate]
    tied_hours = sorted(int(r["publication_hour_et"]) for r in tied)
    best = min(
        tied, key=lambda r: (-int(r["comparable"]), int(r["publication_hour_et"]))
    )
    # Attribution is schedule-based unless an explicit hour is forced. The
    # per-era gate below therefore compares each era against the schedule's hour
    # FOR THAT ERA, not against one global constant -- judging a 2022 era by
    # 2026's hour is the very error the schedule exists to remove.
    forced_hour = args.publication_hour
    # How many captures the candidate hours actually disagree about. If this is
    # zero the sweep has NO discriminating power and a tie says nothing about the
    # publication hour -- reporting an argmin from a degenerate fixture is the
    # `audit-fixtures-for-degeneracy` trap, so it is named explicitly.
    discriminating = sum(
        1
        for o in observations
        if o.captured_at_et is not None
        and len(
            {
                attribute_et_date(o.captured_at_et, publication_hour_et=h)
                for h in range(0, 13)
            }
        )
        > 1
    )
    log(
        f"Hours tied at the minimum rate {best_rate:.4f}: {tied_hours} "
        f"({len(tied_hours)} of {len(usable)} candidates); "
        f"captures whose attribution changes across candidates: {discriminating}"
    )
    if forced_hour is None:
        schedule_text = ", ".join(
            f"{d.isoformat()}->{h}ET" for d, h in PUBLICATION_SCHEDULE
        )
        log(
            f"Attribution in use: piecewise schedule [{schedule_text}]; "
            f"the best SINGLE hour would be {tied_hours} at {best_rate:.4f}"
        )
    else:
        log(f"Attribution in use: forced single hour {forced_hour} ET")
    # -- 4b. does the hour HOLD ACROSS ERAS? -----------------------------
    # A shift mid-span would misattribute a whole era by one day while that era's
    # own chain check stayed clean, so the sweep is repeated per year.
    era_sweep = sweep_by_era(
        observations, era_of=lambda o: o.captured_at_et.strftime("%Y")
    )
    log("Per-era publication-hour sweep (by capture year):")
    for era, info in era_sweep.items():
        flag = "" if info["identified"] else "  <-- NOT identifiable on this sample"
        log(
            f"  {era}: captures {info['captures']:>5} "
            f"(discriminating {info['discriminating_captures']:>4}) "
            f"best hour(s) {info['best_hours']} at rate {info['best_rate']:.4f}"
            f"{flag}"
        )
    identified_eras = {
        era: info for era, info in era_sweep.items() if info["identified"]
    }
    era_hours = {era: info["best_hours"][0] for era, info in identified_eras.items()}

    def _in_force(era: str) -> int:
        """The hour the attribution actually applies within ``era``."""
        if forced_hour is not None:
            return int(forced_hour)
        # Mid-year is representative of the era for a schedule lookup.
        return publication_hour_for(_date(int(era), 7, 1))

    in_force = {era: _in_force(era) for era in sorted(era_hours)}
    # The gate: for every era whose hour the sample can actually identify, the
    # attribution in force there must be among the hours that era's own evidence
    # allows. This is the honest, weaker test -- the evidence must not
    # *contradict* the schedule -- rather than auto-adopting each era's argmin and
    # then citing the resulting rate as proof, which would be circular
    # (`circular-constraints-justify-nothing`).
    disagreeing = {
        era: {"era_evidence": info["best_hours"], "in_force": _in_force(era)}
        for era, info in identified_eras.items()
        if _in_force(era) not in info["best_hours"]
    }
    if disagreeing:
        log(
            f"FAIL: the attribution in force is contradicted by per-era evidence: "
            f"{disagreeing}. An hour that only holds for part of the span "
            f"misattributes an entire era by one calendar day while that era's "
            f"own chain check stays clean. Adjust PUBLICATION_SCHEDULE in "
            f"src/data/aaa_provider.py deliberately, with the sweep recorded."
        )
        return 2
    if era_hours:
        log(
            f"Attribution in force agrees with every identifiable era: "
            f"in-force {in_force} vs era evidence {era_hours} "
            f"({len(era_sweep) - len(identified_eras)} era(s) not identifiable "
            f"on their sample)"
        )
    if len(tied_hours) > 1:
        log(
            f"NOTE: {len(tied_hours)} single-hour candidates tie globally, so the "
            f"global sweep alone does not pin an hour. The per-era sweep above is "
            f"what constrains the schedule; the intra-day and EIA checks below are "
            f"independent of both."
        )

    # -- 4c. is the piecewise schedule worth its complexity? -------------
    # Persisted every run so the docstring cites an artifact instead of a
    # remembered number, and so the row-level cost of the simpler alternative is
    # stated at the magnitude it actually has.
    schedule_comparison: List[Dict[str, object]] = []
    row_effect: Dict[str, object] = {}
    if forced_hour is None:
        schedule_comparison = compare_schedules(observations)
        log("Candidate publication schedules (same metric as the hour sweep):")
        for row in sorted(
            schedule_comparison, key=lambda r: float(r["disagreement_rate"])
        ):
            log(
                f"  {str(row['name']):<28} eras {row['eras']} "
                f"inconsistent {row['disagreements']:>4}/{row['comparable']:>4} "
                f"(rate {float(row['disagreement_rate']):.4f})"
            )
        row_effect = schedule_row_effect(observations)
        log(
            f"Row-level effect of a single global constant: "
            f"{row_effect['rows_structurally_immune']} of "
            f"{row_effect['rows_total']} rows "
            f"({row_effect['rows_structurally_immune_pct']}%) are structurally "
            f"immune (captured at or after "
            f"{row_effect['candidate_hour_ceiling']}:00 ET)"
        )
        for row in row_effect["by_single_hour"]:
            log(
                f"  hour {row['single_hour_et']:>2}: {row['rows_redated']:>3} rows "
                f"re-dated ({row['rows_redated_pct']}%), "
                f"{row['rows_moved']:>3} series rows differ "
                f"({row['rows_moved_pct']}%)"
            )

    # Re-attribute explicitly under the attribution in use, so what is written
    # matches what was audited whatever the parse pass defaulted to.
    observations = [_reattribute(o, forced_hour) for o in observations]

    # -- 5. checks on the series as attributed --------------------------
    intraday = check_intraday_consistency(observations)
    log(
        f"Intra-day check: {intraday['dates_with_multiple_captures']} dates have "
        f">1 capture; {intraday['inconsistent_dates']} disagree"
    )

    daily = collapse_to_daily(observations)
    daily = flag_suspect_rows(daily)
    chain = check_yesterday_chain(daily)
    log(
        f"Yesterday-vs-Current chain: {chain.disagreements} disagreements out of "
        f"{chain.comparable} comparable consecutive pairs "
        f"(rate {chain.disagreement_rate:.4f}); "
        f"skipped {chain.skipped_gap} across gaps, "
        f"{chain.skipped_no_yesterday} with no Yesterday cell"
    )
    if chain.disagreements:
        daily, implicated = mark_chain_disagreements_suspect(daily, chain)
        log(
            f"  marked {len(implicated)} dates suspect from chain "
            f"disagreements: {sorted(d.isoformat() for d in implicated)[:10]}"
        )

    gas_dir = os.path.dirname(os.path.abspath(args.csv_path))
    eia_check = cross_check_against_eia(
        daily, eia_csv_path=os.path.join(gas_dir, "eia_weekly_regular.csv")
    )
    if eia_check.get("paired"):
        log(
            f"External cross-check vs EIA weekly regular: "
            f"{eia_check['paired']} paired dates, mean |diff| "
            f"${eia_check['mean_abs_diff']:.4f}, max ${eia_check['max_abs_diff']:.4f} "
            f"(worst {eia_check['worst_date']}: AAA {eia_check['worst_aaa']} vs "
            f"EIA {eia_check['worst_eia']})"
        )
        if eia_check["exceeds_threshold"]:
            log(
                f"  WARNING: mean |diff| exceeds ${eia_check['warn_threshold']:.2f} - "
                f"a systematic offset near a grade spread suggests the wrong "
                f"column is being read."
            )
    else:
        log(f"External cross-check vs EIA: {eia_check.get('note')}")

    coverage = coverage_report(daily, start=start, end=end)
    log(
        f"Coverage: {coverage['days_present']}/{coverage['days_in_window']} days "
        f"({coverage['coverage_pct']}%), {coverage['days_missing']} missing, "
        f"longest consecutive run {coverage['longest_consecutive_run']}"
        + (
            f", plus {coverage['rows_outside_window']} row(s) dated outside the "
            f"window"
            if coverage["rows_outside_window"]
            else ""
        )
    )
    suspect = [o for o in daily if o.quality == QUALITY_SUSPECT]
    reasons = suspect_reasons(daily, observations, chain)
    by_reason: Dict[str, int] = defaultdict(int)
    for o in suspect:
        for reason in reasons.get(o.date) or [SUSPECT_UNATTRIBUTED]:
            by_reason[reason] += 1
    log(f"quality=suspect rows: {len(suspect)} " f"({dict(sorted(by_reason.items()))})")
    unattributed = [o.date.isoformat() for o in suspect if not reasons.get(o.date)]
    if unattributed:
        log(
            f"WARN: {len(unattributed)} suspect row(s) match none of the four "
            f"known rules -- a fifth path sets quality=suspect and is unnamed: "
            f"{unattributed[:10]}"
        )

    # -- 5b. does the artifact describe the file it claims to? ------------
    # BEFORE the write: comparing against a file this run just produced cannot
    # fail, and under --regenerate it would pass while rows were being dropped.
    disk = reconcile_with_disk(
        daily,
        csv_path=args.csv_path,
        start=start,
        end=end,
        regenerate=args.regenerate,
    )
    if disk["agrees"]:
        log(
            f"Series reconciliation: "
            f"{disk['rows_on_disk_in_window_before_write']} rows already on disk "
            f"in the window, all reproduced by this run"
        )
    else:
        verb = "DROPS" if args.regenerate else "leaves unverified"
        log(
            f"WARN: {len(disk['dates_on_disk_not_reproduced_by_this_run'])} date(s) "
            f"on disk were NOT reproduced by this run, so this run {verb} them: "
            f"{disk['dates_on_disk_not_reproduced_by_this_run'][:10]}. "
            f"Re-derive them from a capture that exists, or record the drop in "
            f"manifest.json's corrections list."
        )

    # -- 6/7. write ------------------------------------------------------
    write_result: Dict[str, object] = {"skipped": True}
    if not args.no_write:
        write_result = upsert_observations(
            daily,
            path=args.csv_path,
            manifest_path=args.manifest_path,
            regenerate=args.regenerate,
        )
        log(
            f"Wrote {args.csv_path}: rows={write_result['rows']} "
            f"added={write_result['added']} unchanged={write_result['unchanged']} "
            f"corrections={write_result['corrections']}"
        )

    if args.covariates:
        from src.data.energy_covariates import backfill_covariates

        log("Refreshing EIA weekly + RBOB daily covariates ...")
        cov = backfill_covariates(
            gas_dir=os.path.dirname(os.path.abspath(args.csv_path))
        )
        for name, info in cov.items():
            log(
                f"  {name}: rows={info.get('rows')} "
                f"{info.get('first')} .. {info.get('last')}"
            )

    if not args.no_write:
        manifest = update_manifest(
            gas_dir=os.path.dirname(os.path.abspath(args.csv_path)),
            manifest_path=args.manifest_path,
        )
        log(f"Updated {args.manifest_path}")
        for name, info in (manifest.get("series") or {}).items():
            log(
                f"  {name}: rows={info.get('rows')} "
                f"{info.get('first')} .. {info.get('last')} "
                f"hash={str(info.get('content_hash'))[:16]}"
            )

    audit = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "capture_selection": selection,
        "captures_enumerated": len(captures),
        "captures_parsed": len(observations),
        "captures_failed": len(failures),
        "failures": failures[:100],
        "publication_hour_sweep": sweep,
        # The hour the rows were actually attributed under. Recomputing it from
        # the environment here (as an earlier version did) let the artifact claim
        # a different hour than the run used -- and the artifact is the evidence,
        # so a wrong field in it is a wrong result.
        "publication_schedule_in_force": (
            [
                {"effective_from": d.isoformat(), "hour_et": h}
                for d, h in PUBLICATION_SCHEDULE
            ]
            if forced_hour is None
            else [{"effective_from": start.isoformat(), "hour_et": int(forced_hour)}]
        ),
        "publication_hour_best_fit": best,
        "publication_hour_by_era": era_sweep,
        # The evidence for the SHIPPED boundaries, persisted so the constant's
        # docstring cites an artifact rather than a remembered comparison.
        "publication_schedule_alternatives": schedule_comparison,
        "publication_schedule_row_effect": row_effect,
        "cdx_day_count_reconciliation": CDX_DAY_COUNT_RECONCILIATION,
        "publication_hour_tied_at_minimum": tied_hours,
        "publication_hour_min_rate": best_rate,
        "captures_discriminating_between_hours": discriminating,
        "intraday_consistency": intraday,
        "external_cross_check_eia": eia_check,
        "yesterday_chain_check": {
            "comparable": chain.comparable,
            "agreements": chain.agreements,
            "disagreements": chain.disagreements,
            "disagreement_rate": round(chain.disagreement_rate, 6),
            "skipped_gap": chain.skipped_gap,
            "skipped_no_yesterday": chain.skipped_no_yesterday,
            "tolerance": chain.tolerance,
            "details": list(chain.details),
        },
        "coverage": coverage,
        "suspect_rows": [
            {
                "date": o.date.isoformat(),
                "value": o.value,
                "url": o.source_url,
                # Without this a consumer can see that 56 rows were flagged and
                # nothing about why any one of them was.
                "reason": "+".join(reasons.get(o.date) or [SUSPECT_UNATTRIBUTED]),
            }
            for o in suspect
        ],
        "suspect_rows_by_reason": dict(sorted(by_reason.items())),
        "series_reconciliation": disk,
        "write_result": write_result,
    }
    if not args.no_write:
        os.makedirs(os.path.dirname(os.path.abspath(args.audit_path)), exist_ok=True)
        with open(args.audit_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(audit, fh, indent=2, sort_keys=False)
            fh.write("\n")
        log(f"Wrote audit artifact {args.audit_path}")

    # A *systematic* parse or attribution defect produces a high disagreement
    # rate; a one-off publication anomaly produces one date, already marked
    # suspect above. So the gate is on the rate, and the threshold is stated
    # rather than implied. Zero is the expected outcome.
    if chain.comparable and chain.disagreement_rate > args.max_chain_disagreement_rate:
        log(
            f"FAIL: Yesterday-vs-Current disagreement rate "
            f"{chain.disagreement_rate:.4f} exceeds "
            f"{args.max_chain_disagreement_rate:.4f} "
            f"({chain.disagreements}/{chain.comparable}) - the parse or the ET "
            f"attribution is systematically wrong. See {args.audit_path}."
        )
        return 2
    if chain.disagreements:
        log(
            f"NOTE: {chain.disagreements}/{chain.comparable} chain disagreements "
            f"(rate {chain.disagreement_rate:.4f}) are within the "
            f"{args.max_chain_disagreement_rate:.4f} tolerance; the implicated "
            f"dates are marked suspect and excluded from fits by default."
        )
    if intraday["inconsistent_dates"]:
        log(
            f"WARN: {intraday['inconsistent_dates']} dates have disagreeing "
            f"same-day captures (rows marked suspect)."
        )
    log("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
