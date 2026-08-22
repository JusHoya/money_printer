"""AAA national average retail gasoline price provider (PRD FR-4.1).

WHAT THIS PROVIDES
------------------
``KXAAAGASM`` settles on the **AAA national average retail price for regular
gasoline as published by AAA on one specific calendar date**
(``docs/phase4_data_contract.md`` §0). This module is the only sanctioned reader
of that number. It has two callers:

* the **live recorder** -- one request per day, appending a provenance-carrying
  row to ``data/gas_truth/aaa_daily_national.csv``;
* the **backfill** (:mod:`scripts.backfill_gas_history`) -- the same parser
  applied to Wayback Machine snapshots of the same page.

Both go through :func:`parse_national_table`, so a layout change breaks the
backfill and the live path together rather than silently diverging.

WHERE THE VALUE LIVES IN THE PAGE
---------------------------------
``https://gasprices.aaa.com/`` renders a table under an ``<h1>`` reading
"National average gas prices"::

    |                | Regular | Mid-Grade | Premium | Diesel | E85    |
    | Current Avg.   | $4.0910 | $4.5890   | $4.9720 | $5.3290| $3.1100|
    | Yesterday Avg. | $4.0990 | $4.5900   | $4.9750 | $5.3210| $3.1270|
    | Week Ago Avg.  | ...
    | Month Ago Avg. | ...
    | Year Ago Avg.  | ...

The parser locates that table by **signature** (a header cell starting
"Regular" plus a body row starting "Current Avg"), not by position, and reads
the Regular column **by header index** rather than by a hardcoded column
number. Two page variants were observed over 2025-06 .. 2026-07: values
rendered to 3 decimals (through 2026-05) and to 4 decimals (from 2026-06). Both
parse; values are stored rounded to 3 decimals per the contract. In every
snapshot inspected the fourth decimal was zero, so the rounding is lossless in
practice; a non-zero fourth decimal is logged (residual <= $0.0005, three
orders below the $0.01 strike granularity) but is not by itself a defect.

If more than one table matches the signature the parse **aborts** rather than
guessing which one is national -- a state-level table has the same shape, and
picking the wrong one would silently record the wrong index.

WHICH CALENDAR DATE A VALUE BELONGS TO
--------------------------------------
AAA republishes each morning **Eastern time**, and the contract settles on a
value "on <date> according to AAA" -- an ET calendar date. Every timestamp this
module handles is therefore converted to ``America/New_York`` before a date is
taken. Two distinct traps, both of which have bitten this project before (see
``feedback_timezone_kalshi_symbols``: Kalshi symbols are ET while the VM runs
UTC, which produced zero training samples for months):

1. **UTC-vs-ET date.** A Wayback snapshot stamped ``02:00Z`` is 21:00 or 22:00
   ET on the *previous* day, and its "Current Avg." is the previous ET day's
   value. Attributing it to the UTC date would shift that row forward one day.
2. **Pre-publication window.** A snapshot taken after ET midnight but before
   that morning's republication still shows the *previous* day's value. Naive
   ET-date attribution is wrong for exactly those snapshots.

Trap 2 is handled by :data:`PUBLICATION_SCHEDULE`: a snapshot whose ET clock time
is before the hour in force on its ET date is attributed to the previous ET day.
Those hours are not assumed -- they are **measured** over the 2,721 parseable
archived captures spanning 2022-01-01 .. 2026-07-29, with the sweep recorded on
the constant itself and persisted in ``data/gas_truth/backfill_audit.json``, and
the measurement is reproducible via :func:`attribution_evidence` and the
backfill's ``--audit-only`` mode. An initial plausible guess of 07:00 ET ("AAA
publishes each morning") was the worst of the candidates measured; the backfill
refuses to write a series under an attribution its own evidence contradicts,
which is how that guess was caught rather than shipped.

The *live* recorder's hour is :data:`PUBLICATION_HOUR_ET` (the era in force
today). It is the same function and the same override that decide which ET day a
scrape *failure* is recorded against -- see :meth:`AAAProvider.today_et` -- so the
failure key and the observation date cannot drift apart.

THE SELF-CHECK THAT MAKES THE ATTRIBUTION FALSIFIABLE
-----------------------------------------------------
The page publishes both "Current Avg." and "Yesterday Avg.". That redundancy is
a free, strong test of the parse *and* the date attribution together: for two
snapshots attributed to consecutive dates ``D-1`` and ``D``, snapshot ``D``'s
*Yesterday* must equal snapshot ``D-1``'s *Current*. A parse error, an off-by-one
in the timezone conversion, or a mis-selected column all break that chain, and
nothing else plausibly does. :func:`check_yesterday_chain` implements it and the
backfill reports the disagreement count as its headline evidence. Per
``circular-constraints-justify-nothing`` this is a genuinely independent check:
the two quantities compared come from different snapshots and different table
rows.

ABORT, NEVER DEFAULT (FR-4.1, ``abort-on-missing-critical-input``)
-----------------------------------------------------------------
On any fetch or parse failure this module raises :class:`AAAUnavailable` and
emits exactly one alert. It never returns a stale value, a last-known-good
value, or a guess. The reason is specific to this series: the projected AAA
average is compared against a ``floor_strike`` to size a real position, so a
plausible-looking default does not degrade the signal -- it manufactures one.

:meth:`AAAProvider.signal_gate` is the interface a strategy consumes; when the
day's scrape failed it returns ``allow=False`` with a reason code, which is the
"zero signals that day" half of Phase 4 exit criterion 1. It has exactly one
consumer, and that is load-bearing: :class:`src.strategies.gas_convergence.
GasConvergenceStrategy` evaluates it on every ``analyze`` call before anything is
priced. A gate with no consumer is not a gate -- this one shipped that way once,
implemented and alerting correctly while the strategy gated on data *freshness*
alone and traded the failed day on yesterday's row.

The block is cleared only by an ``aaa_live`` row for the same publication day.
Accepting any row for the day was the second half of that defect: the Wayback
backfill writes a row for the current day routinely, so an archived capture
cleared an outstanding operator alert.

GAPS ARE ABSENT ROWS, NEVER INVENTED ONES
-----------------------------------------
Per contract §1.1 a day with no retrievable snapshot is a **missing row**. This
module has no interpolation path at all. A row whose parse looked wrong (outside
``[1.00, 9.00]``) is written with ``quality=suspect`` and excluded from fits by
default; the neighbour-jump check that also produces ``suspect`` is a
whole-series property and lives in :func:`flag_suspect_rows`.

CORRECTIONS ARE APPENDED, NOT APPLIED
-------------------------------------
``raw_sha256`` makes each row evidence: the bytes it was parsed from are
identified. Editing a committed row would break that chain, so a re-fetch that
disagrees with an existing row leaves the row alone and appends a record to
``manifest.json``'s ``corrections`` list (``correct-the-record-not-the-as-run-
artifact``). Wholesale regeneration is the sanctioned way to change rows, and is
explicit (``backfill_gas_history.py --regenerate``).

POLITENESS
----------
``gasprices.aaa.com/robots.txt`` (verified 2026-07-29) disallows only
``/wp-admin/`` and asks ``Crawl-delay: 10``. The landing page is allowed. This
module enforces a 10 s minimum spacing between its own requests process-wide and
sends a ``User-Agent`` naming the project with a contact address.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:  # pragma: no cover - requests is a hard project dependency
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
GAS_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "gas_truth")
AAA_CSV_PATH = os.path.join(GAS_TRUTH_DIR, "aaa_daily_national.csv")
MANIFEST_PATH = os.path.join(GAS_TRUTH_DIR, "manifest.json")

#: Per-ET-day record of scrape failures. Not one of the contract §1 series -- it
#: is operational state, not evidence about gas prices -- but it must be on disk
#: rather than in memory because the recorder and the strategy are different
#: processes: an in-memory flag would let the bot trade on a day whose scrape
#: failed in another process (PRD FR-4.1, Phase 4 EC-1).
SCRAPE_FAILURES_PATH = os.path.join(GAS_TRUTH_DIR, "scrape_failures.json")

#: Contract §1: exact header of ``aaa_daily_national.csv``. Fixed by the
#: inter-agent data contract; three workstreams read it.
AAA_CSV_COLUMNS: Tuple[str, ...] = (
    "date",
    "value",
    "source",
    "source_url",
    "fetched_at",
    "raw_sha256",
    "quality",
)

# ---------------------------------------------------------------------------
# Upstream
# ---------------------------------------------------------------------------
AAA_URL = "https://gasprices.aaa.com/"

DEFAULT_USER_AGENT = (
    "money-printer/gas-convergence (+https://github.com/JusHoya/money_printer; "
    "contact hoyeriiim87@gmail.com)"
)

#: ``Crawl-delay: 10`` from ``gasprices.aaa.com/robots.txt``, verified
#: 2026-07-29. Enforced process-wide, not per-instance, so two provider objects
#: in one process cannot together exceed the published rate.
CRAWL_DELAY_SECONDS = 10.0

ET = ZoneInfo("America/New_York")

#: AAA's republication hour (ET) **is not constant over time**, so the schedule
#: is piecewise: ``(effective_from, hour)`` in ascending date order. A capture
#: whose ET clock time is before the hour in force on its ET date is attributed
#: one day back.
#:
#: **Measured, not assumed, and the constancy was itself tested.** Swept over the
#: 2,721 parseable Wayback captures spanning 2022-01-01 .. 2026-07-29 (1,056 of
#: which change attribution between candidate hours). Per capture year, each
#: independently identifying a single hour -- persisted as
#: ``backfill_audit.json``'s ``publication_hour_by_era``:
#:
#: ==== ==== ==== ==== ==== ====
#: year 2022 2023 2024 2025 2026
#: ---- ---- ---- ---- ---- ----
#: hour 5    5    6    3    3
#: rate .035 .039 .039 .038 .021
#: ==== ==== ==== ==== ==== ====
#:
#: HOW BIG THE EFFECT ACTUALLY IS -- the honest magnitude
#: ......................................................
#: This schedule scores better than any single global constant, but the earlier
#: claim that a single constant "would misattribute entire years by a full
#: calendar day" and is "therefore not defensible" was an overstatement, and the
#: measurement contradicts it. Both figures below are recomputed on every run and
#: persisted as ``publication_schedule_alternatives`` and
#: ``publication_schedule_row_effect``:
#:
#: * On the sweep's own metric the best single hour (5) scores 4.92%
#:   (150/3,046) against 3.12% (95/3,046) for this schedule.
#: * On **written rows**, swapping this schedule for that single constant
#:   **re-dates 9 of 1,550 rows (0.58%)**; across candidate hours 3..7 the worst
#:   case is 13 rows (0.84%, at hour 7).
#: * On the **resulting series** -- which also counts a day gained or lost and a
#:   day whose winning capture changes -- the same swap changes 25 rows (1.61%),
#:   worst case 52 (3.35%). Neither figure dominates the other: at hour 3 the
#:   series re-dates 8 rows while only 7 series rows differ.
#:
#: All three are in ``publication_schedule_row_effect``, each with its definition,
#: because a lone number here is a number someone will quote out of context.
#:
#: These magnitudes are far below the rate difference because **90.4% of rows
#: (1,401 of 1,550) cannot move at all**: they were captured at or after 12:00 ET,
#: which is post-publication under every candidate hour in 0..12. That is largely
#: by construction -- the backfill's anchor stratum draws one capture per day from
#: 12:00-20:00 ET -- with the evening captures immune for the same reason. The
#: sweep's rate is computed over every capture, *including* the near-midnight
#: boundary sample selected precisely because it discriminates, so it is the right
#: statistic for identifying the hour and the wrong one for sizing the consequence.
#:
#: The schedule is kept because it is measurably the best of the alternatives on
#: both metrics and costs nothing at runtime, not because the alternative is
#: catastrophic. It is not.
#:
#: Boundaries are placed only where the evidence is stable, not at every quarterly
#: argmin. Quarterly estimates wobble non-monotonically inside 2022 (4/6/5) on
#: 26-104 discriminating captures each, which is estimation noise, and fitting
#: boundaries to noise is how a schedule stops generalising. The measured
#: comparison of named alternatives is in ``publication_schedule_alternatives``;
#: as of the 2026-07-29 run: this 3-era schedule 3.12%, a 5-era quarterly variant
#: 3.38%, this schedule with the third boundary moved to 2025-01-01 3.44%, a
#: 4-era per-capture-year schedule 3.44%, 2-era 3.57%, single constant 4.92%. An
#: earlier revision of this comment cited "2-era 3.57%, 3-era 3.12%, 4-era 3.05%,
#: 5-era 2.99%" -- those era definitions were never recorded and the numbers
#: appear in no committed artifact, so they were not reproducible and have been
#: replaced by the persisted table.
#:
#: Note what that table does *not* settle: the third boundary sits at 2025-04-01
#: while the persisted per-year evidence alone would place it at 2025-01-01. The
#: quarterly analysis that localised it was never persisted and the 2,721 parsed
#: captures are not cached, so the *original* localisation is not reproducible
#: from committed artifacts. What is now reproducible is the comparison above,
#: which prefers 2025-04-01 over 2025-01-01 by 0.32pp on the same captures.
#:
#: The 2024 boundary is only localisable to a range -- the rate is flat at 3.12%
#: for any boundary date from 2023-10-01 to 2024-02-01 -- so 2024-01-01 is a
#: representative choice within that plateau, not a measured changeover date.
#:
#: The residual ~3% is expected and irreducible: an overnight publication job does
#: not complete at the same minute daily, so captures within minutes of the
#: boundary land on either side. Dates implicated in a residual disagreement are
#: marked ``quality=suspect`` rather than silently accepted.
PUBLICATION_SCHEDULE: Tuple[Tuple[_date, int], ...] = (
    (_date(2022, 1, 1), 5),
    (_date(2024, 1, 1), 6),
    (_date(2025, 4, 1), 3),
)

#: The hour in force today, i.e. what the **live recorder** uses. Overridable via
#: the environment so a future measurement can move it without a code change --
#: but the backfill fails loudly if its sweep contradicts whatever is set, so it
#: cannot drift silently.
#:
#: Operational preference (no longer a correctness dependency): the live recorder
#: should fetch inside 12:00-20:00 ET, where *no* candidate hour changes the
#: attributed date, so this constant stops mattering for live rows altogether.
#: 2026Q2-Q3 measured 4 rather than 3 -- worth only 2 disagreements in 3,049, so
#: not enough to justify another boundary, but enough that a 03:00-04:00 ET cron
#: would be a bad idea.
#:
#: This used to be the *only* thing keeping a scrape failure from stranding a day:
#: the failure was keyed on the raw ET date while the observation was keyed on the
#: attributed date, and outside that window the two differ. That is now fixed in
#: code -- :meth:`AAAProvider.today_et` shares the attribution -- so a cron running
#: at 01:00 ET is merely suboptimal rather than a correctness hazard.
PUBLICATION_HOUR_ET = int(os.getenv("AAA_PUBLICATION_HOUR_ET", "3"))


def publication_hour_for(day: _date) -> int:
    """The republication hour (ET) in force on ``day``, per
    :data:`PUBLICATION_SCHEDULE`.

    Dates before the first entry use the earliest known hour: the schedule was
    measured from 2022-01-01 and extrapolating backwards is a guess, but the
    earliest measured regime is a better one than today's.
    """
    hour = PUBLICATION_SCHEDULE[0][1]
    for effective_from, value in PUBLICATION_SCHEDULE:
        if day >= effective_from:
            hour = value
        else:
            break
    return hour


#: Contract §1.1 plausibility window for a US national average, USD/gal.
MIN_PLAUSIBLE_VALUE = 1.00
MAX_PLAUSIBLE_VALUE = 9.00

#: Contract §1.1: a day-over-day move larger than this marks both neighbours of
#: the jump as ``suspect``. The largest single-day move in the observed 14-month
#: sample is far below it; a genuine $0.15 overnight move in a *national* average
#: does not happen without a refinery-scale event.
MAX_DAILY_JUMP = 0.15

#: Contract §1: the only two admissible ``source`` values for this series.
SOURCE_LIVE = "aaa_live"
SOURCE_WAYBACK = "aaa_wayback"

QUALITY_OK = "ok"
QUALITY_SUSPECT = "suspect"

#: HTTP statuses worth retrying rather than treating as "this does not exist".
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Reason codes -- every abort path names one of these, and only these.
# ---------------------------------------------------------------------------
REASON_OFFLINE = "AAA_OFFLINE"
REASON_HTTP_ERROR = "AAA_HTTP_ERROR"
REASON_EMPTY_BODY = "AAA_EMPTY_BODY"
REASON_SITE_OFFLINE = "AAA_SITE_OFFLINE"
REASON_TABLE_NOT_FOUND = "AAA_TABLE_NOT_FOUND"
REASON_TABLE_AMBIGUOUS = "AAA_TABLE_AMBIGUOUS"
REASON_COLUMN_NOT_FOUND = "AAA_COLUMN_NOT_FOUND"
REASON_ROW_NOT_FOUND = "AAA_ROW_NOT_FOUND"
REASON_VALUE_UNPARSEABLE = "AAA_VALUE_UNPARSEABLE"
REASON_VALUE_OUT_OF_RANGE = "AAA_VALUE_OUT_OF_RANGE"
REASON_NO_DATA = "AAA_NO_DATA"
REASON_STALE = "AAA_STALE"
REASON_SUSPECT = "AAA_SUSPECT"
REASON_SCRAPE_FAILED_TODAY = "AAA_SCRAPE_FAILED_TODAY"

REASON_CODES = frozenset(
    {
        REASON_OFFLINE,
        REASON_HTTP_ERROR,
        REASON_EMPTY_BODY,
        REASON_SITE_OFFLINE,
        REASON_TABLE_NOT_FOUND,
        REASON_TABLE_AMBIGUOUS,
        REASON_COLUMN_NOT_FOUND,
        REASON_ROW_NOT_FOUND,
        REASON_VALUE_UNPARSEABLE,
        REASON_VALUE_OUT_OF_RANGE,
        REASON_NO_DATA,
        REASON_STALE,
        REASON_SUSPECT,
        REASON_SCRAPE_FAILED_TODAY,
    }
)


class AAAUnavailable(Exception):
    """No honest AAA national average could be produced.

    Always a hard abort for the caller. There is no safe default for "what is
    the national average today": a substituted value is compared against a
    ``floor_strike`` and sizes a position, so a plausible default does not
    weaken the signal, it fabricates one.

    :param reason_code: one of :data:`REASON_CODES`, for machine-readable logs.
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        if reason_code not in REASON_CODES:
            raise ValueError(f"unknown AAA reason code {reason_code!r}")
        self.reason_code = reason_code
        self.detail = message
        super().__init__(f"{reason_code}: {message}" if message else reason_code)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AAAObservation:
    """One parsed AAA national-average reading, with its provenance.

    ``date`` is the **ET calendar date the value is published for**, already
    resolved through :func:`attribute_et_date`. ``yesterday_value`` and the
    other comparison figures are carried in memory for
    :func:`check_yesterday_chain` and are deliberately *not* persisted: the
    contract fixes the CSV columns, and a second copy of the same number in the
    same file is a consistency hazard rather than evidence.
    """

    date: _date
    value: float
    source: str
    source_url: str
    fetched_at: str
    raw_sha256: str
    quality: str = QUALITY_OK
    yesterday_value: Optional[float] = None
    week_ago_value: Optional[float] = None
    month_ago_value: Optional[float] = None
    year_ago_value: Optional[float] = None
    #: ET wall-clock instant the page was captured, when known (Wayback
    #: snapshots always know it; a live fetch uses its own request time).
    captured_at_et: Optional[datetime] = None
    #: Set when the source rendered a non-zero fourth decimal, i.e. the stored
    #: 3-decimal value is a rounding of the published figure.
    rounded_from: Optional[str] = None

    def csv_row(self) -> Dict[str, str]:
        """Render to the exact contract §1 column set."""
        return {
            "date": self.date.isoformat(),
            "value": f"{self.value:.3f}",
            "source": self.source,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "raw_sha256": self.raw_sha256,
            "quality": self.quality,
        }


@dataclass(frozen=True)
class SignalGate:
    """Whether gas signals may be produced, and if not, why not.

    FR-4.1's "scrape failure ... aborts signals" and Phase 4 exit criterion 1's
    "zero signals that day". WS-C's strategy consumes this; ``allow=False`` must
    short-circuit signal generation before any projection is attempted.

    Every rejection carries ``reason_code`` *and* ``detail`` with the measured
    value that failed, so a gate that silently rejects everything is visible
    from the logs alone (``make-silent-rejections-observable``).
    """

    allow: bool
    reason_code: Optional[str] = None
    detail: str = ""
    observation: Optional[AAAObservation] = None
    age_days: Optional[int] = None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def wayback_timestamp_to_et(timestamp: str) -> datetime:
    """Convert a 14-digit Wayback ``YYYYMMDDhhmmss`` **UTC** stamp to ET.

    Wayback timestamps are UTC. Returning a tz-aware ET datetime here (rather
    than a bare date) keeps the pre-publication decision in
    :func:`attribute_et_date` available to the caller and testable on its own.
    """
    ts = str(timestamp).strip()
    if len(ts) != 14 or not ts.isdigit():
        raise ValueError(f"not a 14-digit Wayback timestamp: {timestamp!r}")
    naive = datetime.strptime(ts, "%Y%m%d%H%M%S")
    return naive.replace(tzinfo=timezone.utc).astimezone(ET)


def attribute_et_date(
    captured_at: datetime,
    *,
    publication_hour_et: int = None,
) -> _date:
    """Which ET calendar date a capture's "Current Avg." belongs to.

    Two corrections, in order:

    1. Convert to ET. A ``02:00Z`` capture is the previous ET evening; taking
       the UTC date would shift the row a day forward.
    2. If the ET clock time is before the publication hour **in force on that
       date**, AAA has not republished yet and the page still shows the previous
       ET day's figure, so attribute one day back.

    The hour is era-dependent (:data:`PUBLICATION_SCHEDULE`): AAA moved it twice
    over 2022-2026. Passing ``publication_hour_et`` overrides the schedule with a
    fixed hour, which is what the candidate-hour sweep needs; leaving it ``None``
    -- the production path -- resolves the hour from the capture's own ET date, so
    a 2022 capture is never judged against 2026's schedule.

    :param captured_at: tz-aware capture instant. A naive datetime is rejected
        rather than assumed to be UTC or local -- guessing the zone here is the
        exact bug this function exists to prevent.
    """
    if captured_at.tzinfo is None:
        raise ValueError(
            "attribute_et_date requires a timezone-aware datetime; a naive "
            "value would silently inherit the host's zone (the VM runs UTC, "
            "AAA publishes ET)"
        )
    local = captured_at.astimezone(ET)
    hour = (
        publication_hour_for(local.date())
        if publication_hour_et is None
        else publication_hour_et
    )
    if local.hour < hour:
        return (local - timedelta(days=1)).date()
    return local.date()


# ---------------------------------------------------------------------------
# HTML table extraction (stdlib only -- no bs4/lxml dependency)
# ---------------------------------------------------------------------------
class _TableCollector(HTMLParser):
    """Collect every ``<table>`` as rows of whitespace-normalised cell text.

    Also records the text of the most recent heading preceding each table, which
    is how the national table is distinguished from an identically-shaped
    state-level one.

    Written against :mod:`html.parser` rather than BeautifulSoup on purpose:
    ``beautifulsoup4`` is installed in the current environment but is **not**
    declared in ``requirements.txt``, and an undeclared import is how a VM
    deploy breaks. Nothing here needs more than the stdlib.
    """

    _HEADINGS = ("h1", "h2", "h3", "h4")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[Tuple[Optional[str], List[List[str]]]] = []
        self._table: Optional[List[List[str]]] = None
        self._table_heading: Optional[str] = None
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._heading_buf: Optional[List[str]] = None
        self._last_heading: Optional[str] = None
        self._depth = 0

    # -- tags ------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
                self._table_heading = self._last_heading
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in self._HEADINGS:
            self._heading_buf = []

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        if tag == "table":
            if self._depth == 1 and self._table is not None:
                self.tables.append((self._table_heading, self._table))
                self._table = None
            self._depth = max(0, self._depth - 1)
        elif tag == "tr":
            if self._row is not None and self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                self._row.append(_norm_ws("".join(self._cell)))
            self._cell = None
        elif tag in self._HEADINGS:
            if self._heading_buf is not None:
                self._last_heading = _norm_ws("".join(self._heading_buf))
            self._heading_buf = None

    def handle_data(self, data: str) -> None:  # noqa: D102
        if self._cell is not None:
            self._cell.append(data)
        elif self._heading_buf is not None:
            self._heading_buf.append(data)


#: Signatures of AAA's maintenance page. Matched on the document head only, so a
#: news article quoting the phrase cannot trip it.
_MAINTENANCE_SIGNATURES = (
    "<title>site is offline</title>",
    'name="autoupdater" content="maintenance"',
)


def _looks_like_maintenance_page(text: str) -> bool:
    """True when the body is AAA's "Site is offline" page rather than content."""
    head = text[:4000].lower()
    return any(sig in head for sig in _MAINTENANCE_SIGNATURES)


def _norm_ws(text: str) -> str:
    """Collapse whitespace, including the NBSPs AAA's markup is full of."""
    return " ".join(text.replace("\xa0", " ").split())


def _norm_label(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces -- for label matching."""
    return _norm_ws(re.sub(r"[^0-9a-z ]+", " ", text.lower()))


_VALUE_RE = re.compile(r"-?\$?\s*(\d+(?:\.\d+)?)")


def _parse_money(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse ``$4.0910`` / ``$3.140`` / ``3.14`` to a float.

    Returns ``(value, raw_digits)``. ``(None, None)`` when the cell holds no
    number at all (``N/A``, ``--``, empty) -- the caller decides whether that is
    fatal, because a missing *Year Ago* column is not a reason to reject today's
    price.
    """
    if not text:
        return None, None
    m = _VALUE_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1)
    try:
        return float(raw), raw
    except ValueError:  # pragma: no cover - regex guarantees a float literal
        return None, None


#: Row labels read out of the national table, mapped to observation fields.
_ROW_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("current", "value"),
    ("yesterday", "yesterday_value"),
    ("week ago", "week_ago_value"),
    ("month ago", "month_ago_value"),
    ("year ago", "year_ago_value"),
)


def _table_matches_signature(rows: Sequence[Sequence[str]]) -> bool:
    """True when a table looks like the AAA averages grid.

    Signature rather than position: a header cell starting "regular", plus a
    first-column label containing "avg". Both are required, so a navigation or ad
    table cannot match.

    Deliberately keyed on *any* "... Avg." row rather than on "Current Avg."
    specifically. Requiring the Current row here would make
    :data:`REASON_ROW_NOT_FOUND` unreachable and would report a renamed row
    ("Today Avg.") as "no table found" -- a misleading diagnosis that would send
    the next maintainer looking for a page redesign instead of a one-word label
    change.
    """
    has_regular = False
    has_avg_row = False
    for row in rows:
        for idx, cell in enumerate(row):
            label = _norm_label(cell)
            if idx > 0 and label.startswith("regular"):
                has_regular = True
            if idx == 0 and "avg" in label:
                has_avg_row = True
    return has_regular and has_avg_row


def _regular_column_index(rows: Sequence[Sequence[str]]) -> Optional[int]:
    """Index of the Regular column, read from the header row.

    Never hardcoded. If AAA reorders the grades -- or inserts a column -- a
    fixed index would silently start recording Mid-Grade as the national
    regular average, which no downstream check would catch.
    """
    for row in rows:
        for idx, cell in enumerate(row):
            if idx == 0:
                continue
            if _norm_label(cell).startswith("regular"):
                return idx
    return None


def parse_national_table(
    body: bytes,
    *,
    source_url: str,
    captured_at: datetime,
    source: str,
    publication_hour_et: int = None,
    fetched_at: Optional[str] = None,
) -> AAAObservation:
    """Parse an AAA landing-page body into an :class:`AAAObservation`.

    Pure function of ``body`` plus the capture instant: the live path and the
    Wayback backfill both call exactly this, so a layout change cannot make the
    two diverge.

    :param body: raw response bytes. ``raw_sha256`` is taken over these bytes,
        so the stored hash identifies precisely what was parsed.
    :param captured_at: tz-aware instant the page was captured.
    :raises AAAUnavailable: on any structural or value failure. There is no
        partial success and no default.
    """
    if not body:
        raise AAAUnavailable(REASON_EMPTY_BODY, f"zero-length body from {source_url}")

    raw_sha256 = hashlib.sha256(body).hexdigest()
    text = body.decode("utf-8", errors="replace")

    # AAA serves its own WordPress maintenance page with HTTP 200, so a status
    # check cannot see it. Observed 7 times in 2,729 archived captures
    # (2022-07-15 and six dates in Oct-Dec 2023), always ~905 bytes titled
    # "Site is offline". It is named separately from a missing table because the
    # operator response differs completely: "AAA is down, retry later" versus
    # "AAA redesigned the page and the parser needs work".
    if _looks_like_maintenance_page(text):
        raise AAAUnavailable(
            REASON_SITE_OFFLINE,
            f"AAA served its maintenance page ({len(body)} bytes) at {source_url}",
        )

    collector = _TableCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception as exc:  # pragma: no cover - html.parser is lenient
        raise AAAUnavailable(
            REASON_TABLE_NOT_FOUND, f"HTML parse failed for {source_url}: {exc}"
        ) from exc

    candidates = [
        (heading, rows)
        for heading, rows in collector.tables
        if _table_matches_signature(rows)
    ]
    if not candidates:
        raise AAAUnavailable(
            REASON_TABLE_NOT_FOUND,
            f"no table with a Regular column and a Current Avg row in "
            f"{source_url} ({len(collector.tables)} tables seen, "
            f"{len(body)} bytes)",
        )
    if len(candidates) > 1:
        national = [c for c in candidates if "national" in _norm_label(c[0] or "")]
        if len(national) != 1:
            raise AAAUnavailable(
                REASON_TABLE_AMBIGUOUS,
                f"{len(candidates)} tables match the averages signature in "
                f"{source_url} and {len(national)} carry a National heading; "
                f"refusing to guess which is the national index",
            )
        candidates = national

    heading, rows = candidates[0]
    col = _regular_column_index(rows)
    if col is None:
        raise AAAUnavailable(
            REASON_COLUMN_NOT_FOUND,
            f"no Regular column header in the averages table at {source_url}",
        )

    values: Dict[str, Optional[float]] = {}
    raw_digits: Dict[str, Optional[str]] = {}
    for row in rows:
        if not row:
            continue
        label = _norm_label(row[0])
        for prefix, field in _ROW_FIELDS:
            if label.startswith(prefix) and field not in values:
                cell = row[col] if col < len(row) else ""
                val, raw = _parse_money(cell)
                values[field] = val
                raw_digits[field] = raw

    if "value" not in values:
        raise AAAUnavailable(
            REASON_ROW_NOT_FOUND,
            f"no 'Current Avg' row in the averages table at {source_url}",
        )
    current = values["value"]
    if current is None:
        raise AAAUnavailable(
            REASON_VALUE_UNPARSEABLE,
            f"Current Avg / Regular cell held no number at {source_url}",
        )

    quality = QUALITY_OK
    if not (MIN_PLAUSIBLE_VALUE <= current <= MAX_PLAUSIBLE_VALUE):
        # Contract §1.1: out-of-window is recorded as suspect, not silently
        # dropped and not silently trusted. The row stays visible; fits exclude
        # it by default.
        quality = QUALITY_SUSPECT
        logger.info(
            "AAA_SUSPECT value %.4f outside [%.2f, %.2f] at %s",
            current,
            MIN_PLAUSIBLE_VALUE,
            MAX_PLAUSIBLE_VALUE,
            source_url,
        )

    rounded_from = None
    raw_current = raw_digits.get("value") or ""
    if "." in raw_current and len(raw_current.split(".", 1)[1]) > 3:
        tail = raw_current.split(".", 1)[1][3:]
        if tail.strip("0"):
            rounded_from = raw_current
            logger.info(
                "AAA value %s carries a non-zero 4th decimal; stored as %.3f "
                "(residual <= $0.0005) from %s",
                raw_current,
                round(current, 3),
                source_url,
            )

    et_date = attribute_et_date(captured_at, publication_hour_et=publication_hour_et)

    return AAAObservation(
        date=et_date,
        value=round(current, 3),
        source=source,
        source_url=source_url,
        fetched_at=fetched_at or _utc_now_iso(),
        raw_sha256=raw_sha256,
        quality=quality,
        yesterday_value=(
            round(values["yesterday_value"], 3)
            if values.get("yesterday_value") is not None
            else None
        ),
        week_ago_value=(
            round(values["week_ago_value"], 3)
            if values.get("week_ago_value") is not None
            else None
        ),
        month_ago_value=(
            round(values["month_ago_value"], 3)
            if values.get("month_ago_value") is not None
            else None
        ),
        year_ago_value=(
            round(values["year_ago_value"], 3)
            if values.get("year_ago_value") is not None
            else None
        ),
        captured_at_et=captured_at.astimezone(ET),
        rounded_from=rounded_from,
    )


# ---------------------------------------------------------------------------
# The Yesterday-vs-Current chain check
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChainCheckResult:
    """Outcome of the Yesterday-vs-Current cross-check over a series.

    ``comparable`` counts only consecutive-date pairs where both a *Yesterday*
    figure and the prior day's *Current* exist; a Wayback gap makes a pair
    non-comparable rather than a disagreement. Reporting ``comparable``
    alongside ``disagreements`` is the point -- a check that silently skips most
    of its input looks identical to a passing one
    (``measure-what-a-gate-excludes``).
    """

    comparable: int
    agreements: int
    disagreements: int
    skipped_no_yesterday: int
    skipped_gap: int
    tolerance: float
    details: Tuple[Dict[str, object], ...] = ()

    @property
    def disagreement_rate(self) -> float:
        return (self.disagreements / self.comparable) if self.comparable else 0.0


def check_yesterday_chain(
    observations: Iterable[AAAObservation],
    *,
    tolerance: float = 0.0005,
    max_details: int = 200,
) -> ChainCheckResult:
    """Cross-check each snapshot's *Yesterday* against the prior day's *Current*.

    The strongest available evidence that both the parse and the ET date
    attribution are right, because it is the page validating itself: the two
    numbers compared come from different snapshots and different table rows, so
    no single mistake can make them agree by construction.

    Only **consecutive** attributed dates are compared. A pair separated by a
    Wayback gap is counted in ``skipped_gap``: AAA's "Yesterday" means the
    previous calendar day, so comparing across a two-day gap would manufacture
    disagreements that say nothing about the parse.

    ``tolerance`` is half a stored least-significant digit, so a 3-decimal
    rounding difference is not counted as a disagreement.
    """
    # Several captures can share an attributed date. Resolve deterministically to
    # the latest capture in the ET day -- the same rule
    # ``backfill_gas_history.collapse_to_daily`` uses, so the audited series and
    # the written series cannot disagree. Left to dict-insertion order this would
    # silently depend on CDX ordering, making the headline evidence
    # irreproducible.
    by_date: Dict[_date, AAAObservation] = {}
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    for obs in observations:
        prior = by_date.get(obs.date)
        if prior is None or (obs.captured_at_et or _epoch) >= (
            prior.captured_at_et or _epoch
        ):
            by_date[obs.date] = obs

    comparable = agreements = disagreements = 0
    skipped_no_yesterday = skipped_gap = 0
    details: List[Dict[str, object]] = []

    for day in sorted(by_date):
        obs = by_date[day]
        if obs.yesterday_value is None:
            skipped_no_yesterday += 1
            continue
        prev = by_date.get(day - timedelta(days=1))
        if prev is None:
            skipped_gap += 1
            continue
        comparable += 1
        delta = abs(obs.yesterday_value - prev.value)
        if delta <= tolerance:
            agreements += 1
        else:
            disagreements += 1
            if len(details) < max_details:
                details.append(
                    {
                        "date": day.isoformat(),
                        "yesterday_on_date": obs.yesterday_value,
                        "current_on_prev_date": prev.value,
                        "delta": round(delta, 4),
                        "snapshot_url": obs.source_url,
                        "prev_snapshot_url": prev.source_url,
                        "captured_at_et": (
                            obs.captured_at_et.isoformat()
                            if obs.captured_at_et
                            else None
                        ),
                        "prev_captured_at_et": (
                            prev.captured_at_et.isoformat()
                            if prev.captured_at_et
                            else None
                        ),
                    }
                )

    return ChainCheckResult(
        comparable=comparable,
        agreements=agreements,
        disagreements=disagreements,
        skipped_no_yesterday=skipped_no_yesterday,
        skipped_gap=skipped_gap,
        tolerance=tolerance,
        details=tuple(details),
    )


def attribution_inconsistencies(
    observations: Sequence[AAAObservation], *, tolerance: float = 0.0005
) -> Dict[str, int]:
    """Count attribution inconsistencies across **every** capture.

    Two independent constraints, both evaluated over all captures rather than one
    per day:

    * **same-date**: every capture attributed to one date must report the same
      *Current*. This is what carries information about where the day boundary
      sits -- a boundary in the wrong place lands a pre-publication capture
      (still showing yesterday's figure) in the same bucket as an afternoon one.
    * **cross-date**: a capture's *Yesterday* must match the previous date's
      *Current*.

    Deduplicating to one capture per day before measuring -- as
    :func:`check_yesterday_chain` deliberately does when *building* the series --
    discards precisely the near-midnight captures that identify the boundary, so
    the measurement would tie at every candidate hour and an argmin over it would
    be meaningless (``audit-fixtures-for-degeneracy``). Hence a separate function.

    The cross-date comparison uses the closest of the previous date's values so a
    date that is *already* same-date inconsistent is not counted twice for one
    underlying defect.
    """
    by_date: Dict[_date, List[AAAObservation]] = {}
    for o in observations:
        by_date.setdefault(o.date, []).append(o)

    same_date_groups = 0
    same_date_bad = 0
    for day, group in by_date.items():
        if len(group) < 2:
            continue
        same_date_groups += 1
        values = [round(o.value, 3) for o in group]
        if max(values) - min(values) > tolerance:
            same_date_bad += 1

    cross_pairs = 0
    cross_bad = 0
    for day, group in by_date.items():
        prev = by_date.get(day - timedelta(days=1))
        if not prev:
            continue
        prev_values = [p.value for p in prev]
        for o in group:
            if o.yesterday_value is None:
                continue
            cross_pairs += 1
            if min(abs(o.yesterday_value - pv) for pv in prev_values) > tolerance:
                cross_bad += 1

    return {
        "same_date_groups": same_date_groups,
        "same_date_inconsistent": same_date_bad,
        "cross_date_pairs": cross_pairs,
        "cross_date_inconsistent": cross_bad,
        "total_checks": same_date_groups + cross_pairs,
        "total_inconsistent": same_date_bad + cross_bad,
    }


def attribution_evidence(
    observations: Iterable[AAAObservation],
    *,
    candidate_hours: Sequence[int] = tuple(range(0, 13)),
    tolerance: float = 0.0005,
) -> List[Dict[str, object]]:
    """Re-attribute a fixed set of captures under each candidate publication hour
    and report the inconsistency count for each.

    This is what makes :data:`PUBLICATION_HOUR_ET` a measurement rather than an
    assumption: the correct hour is the one under which the page's own redundant
    figures stop contradicting each other. Requires ``captured_at_et`` on every
    observation, so it runs on backfill output rather than on rows read back from
    the CSV (the contract fixes the CSV's columns, and the capture instant is not
    one of them).

    ``disagreements``/``comparable`` are reported per hour so a caller can see
    the *margin* between candidates, not just the winner -- a flat profile means
    the sample cannot identify the hour and must be reported as such.
    """
    obs = [o for o in observations if o.captured_at_et is not None]
    out: List[Dict[str, object]] = []
    for hour in candidate_hours:
        reattributed = [
            replace(
                o,
                date=attribute_et_date(o.captured_at_et, publication_hour_et=hour),
            )
            for o in obs
        ]
        counts = attribution_inconsistencies(reattributed, tolerance=tolerance)
        total = counts["total_checks"]
        out.append(
            {
                "publication_hour_et": hour,
                "comparable": total,
                "agreements": total - counts["total_inconsistent"],
                "disagreements": counts["total_inconsistent"],
                "disagreement_rate": (
                    round(counts["total_inconsistent"] / total, 6) if total else 0.0
                ),
                "same_date_groups": counts["same_date_groups"],
                "same_date_inconsistent": counts["same_date_inconsistent"],
                "cross_date_pairs": counts["cross_date_pairs"],
                "cross_date_inconsistent": counts["cross_date_inconsistent"],
                "distinct_dates": len({o.date for o in reattributed}),
            }
        )
    return out


def flag_suspect_rows(
    observations: Sequence[AAAObservation],
    *,
    max_daily_jump: float = MAX_DAILY_JUMP,
) -> List[AAAObservation]:
    """Mark ``quality=suspect`` on rows implicated in an implausible jump.

    Contract §1.1's second suspect trigger. It is a whole-series property, so it
    cannot live in :func:`parse_national_table`, which sees one page at a time.
    Only **consecutive** calendar days are compared -- a $0.20 move across a
    four-day Wayback gap is ordinary, not suspect.

    Both sides of a jump are flagged: from two rows alone there is no way to say
    which one is wrong, and guessing would be exactly the false confidence the
    ``suspect`` flag exists to avoid.
    """
    ordered = sorted(observations, key=lambda o: o.date)
    flagged: set = set()
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur.date - prev.date).days != 1:
            continue
        if abs(cur.value - prev.value) > max_daily_jump:
            flagged.add(prev.date)
            flagged.add(cur.date)
            logger.info(
                "AAA_SUSPECT day-over-day jump %.3f -> %.3f (%.3f > %.3f) "
                "between %s and %s",
                prev.value,
                cur.value,
                abs(cur.value - prev.value),
                max_daily_jump,
                prev.date.isoformat(),
                cur.date.isoformat(),
            )
    if not flagged:
        return list(ordered)
    return [
        replace(o, quality=QUALITY_SUSPECT) if o.date in flagged else o for o in ordered
    ]


# ---------------------------------------------------------------------------
# CSV persistence
# ---------------------------------------------------------------------------
def read_aaa_csv(path: str = AAA_CSV_PATH) -> List[Dict[str, str]]:
    """Read ``aaa_daily_national.csv``. Missing file reads as no rows."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if (r.get("date") or "").strip()]


def write_aaa_csv(rows: Sequence[Mapping[str, str]], path: str = AAA_CSV_PATH) -> str:
    """Write the series atomically, sorted ascending by date, UTF-8 + LF.

    LF is explicit, not inherited from the platform: a hash-gated fixture
    checked out with CRLF on Windows hashes differently from the same bytes on
    the Linux VM, which reds a test for a file nobody changed
    (``hash-gated-fixtures-need-eol-lf``). ``manifest.json``'s ``content_hash``
    is taken over these bytes, so the line ending is load-bearing.

    Atomic via write-to-temp-then-replace so an interrupted run cannot leave a
    half-written series that the next run reads as truth.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    ordered = sorted(rows, key=lambda r: str(r["date"]))
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(AAA_CSV_COLUMNS), lineterminator="\n"
        )
        writer.writeheader()
        for row in ordered:
            writer.writerow({k: row.get(k, "") for k in AAA_CSV_COLUMNS})
    os.replace(tmp, path)
    return path


def _values_agree(a: str, b: str) -> bool:
    try:
        return abs(float(a) - float(b)) < 5e-4
    except (TypeError, ValueError):
        return str(a) == str(b)


def upsert_observations(
    observations: Sequence[AAAObservation],
    *,
    path: str = AAA_CSV_PATH,
    manifest_path: str = MANIFEST_PATH,
    regenerate: bool = False,
) -> Dict[str, object]:
    """Merge observations into the series, first-writer-wins on ``date``.

    An existing row is **never edited**. Its ``raw_sha256`` identifies the bytes
    it was parsed from, and that chain is what makes the row evidence; a
    silent rewrite would break it while every test stayed green. A re-fetch that
    disagrees therefore leaves the row alone and appends a record to
    ``manifest.json``'s ``corrections`` list, per
    ``correct-the-record-not-the-as-run-artifact``.

    ``regenerate=True`` is the sanctioned way to actually change rows: it
    discards the existing file and rewrites the whole series, which keeps the
    hash chain internally consistent instead of patching it.
    """
    existing = [] if regenerate else read_aaa_csv(path)
    by_date: Dict[str, Dict[str, str]] = {r["date"]: dict(r) for r in existing}

    added = 0
    unchanged = 0
    corrections: List[Dict[str, object]] = []

    for obs in observations:
        row = obs.csv_row()
        key = row["date"]
        prior = by_date.get(key)
        if prior is None:
            by_date[key] = row
            added += 1
            continue
        if (
            _values_agree(prior.get("value", ""), row["value"])
            and prior.get("quality") == row["quality"]
        ):
            unchanged += 1
            continue
        corrections.append(
            {
                "recorded_at": _utc_now_iso(),
                "series": "aaa_daily_national",
                "date": key,
                "kept_value": prior.get("value"),
                "kept_source": prior.get("source"),
                "kept_source_url": prior.get("source_url"),
                "kept_raw_sha256": prior.get("raw_sha256"),
                "kept_quality": prior.get("quality"),
                "observed_value": row["value"],
                "observed_source": row["source"],
                "observed_source_url": row["source_url"],
                "observed_raw_sha256": row["raw_sha256"],
                "observed_quality": row["quality"],
                "note": (
                    "Existing row retained; its raw_sha256 chain is evidence. "
                    "Apply with backfill_gas_history.py --regenerate."
                ),
            }
        )
        logger.warning(
            "AAA row for %s already recorded as %s (%s); newly observed %s (%s). "
            "Keeping the recorded row and appending a correction.",
            key,
            prior.get("value"),
            prior.get("source"),
            row["value"],
            row["source"],
        )

    write_aaa_csv(list(by_date.values()), path=path)
    if corrections:
        append_corrections(corrections, manifest_path=manifest_path)

    return {
        "added": added,
        "unchanged": unchanged,
        "corrections": len(corrections),
        "rows": len(by_date),
        "path": path,
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _empty_manifest() -> Dict[str, object]:
    blank = {"rows": 0, "first": "", "last": "", "content_hash": ""}
    return {
        "generated_at": _utc_now_iso(),
        "series": {
            "aaa_daily_national": {
                "rows": 0,
                "first": "",
                "last": "",
                "by_source": {SOURCE_LIVE: 0, SOURCE_WAYBACK: 0},
                "suspect": 0,
                "content_hash": "",
            },
            "eia_weekly_regular": dict(blank),
            "rbob_daily": dict(blank),
        },
        "corrections": [],
    }


def read_manifest(manifest_path: str = MANIFEST_PATH) -> Dict[str, object]:
    """Read ``manifest.json``, falling back to the contract §1 skeleton."""
    if not os.path.exists(manifest_path):
        return _empty_manifest()
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("manifest.json unreadable (%s); starting a fresh one", exc)
        return _empty_manifest()
    skeleton = _empty_manifest()
    skeleton.update(data)
    series = dict(skeleton.get("series") or {})
    for name, blank in _empty_manifest()["series"].items():  # type: ignore[union-attr]
        series.setdefault(name, blank)
    skeleton["series"] = series
    skeleton.setdefault("corrections", [])
    return skeleton


def write_manifest(
    manifest: Mapping[str, object], manifest_path: str = MANIFEST_PATH
) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(manifest_path)) or ".", exist_ok=True)
    tmp = f"{manifest_path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, manifest_path)
    return manifest_path


def append_corrections(
    corrections: Sequence[Mapping[str, object]],
    *,
    manifest_path: str = MANIFEST_PATH,
) -> None:
    """Append correction records. Never rewrites an existing one."""
    if not corrections:
        return
    manifest = read_manifest(manifest_path)
    existing = list(manifest.get("corrections") or [])
    existing.extend(dict(c) for c in corrections)
    manifest["corrections"] = existing
    manifest["generated_at"] = _utc_now_iso()
    write_manifest(manifest, manifest_path)


def _summarise_csv(path: str, date_column: str) -> Dict[str, object]:
    if not os.path.exists(path):
        return {"rows": 0, "first": "", "last": "", "content_hash": ""}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get(date_column) or "").strip()]
    dates = sorted((r[date_column] or "").strip() for r in rows)
    return {
        "rows": len(rows),
        "first": dates[0] if dates else "",
        "last": dates[-1] if dates else "",
        "content_hash": _sha256_file(path),
    }


def update_manifest(
    *,
    gas_dir: str = GAS_TRUTH_DIR,
    manifest_path: str = MANIFEST_PATH,
) -> Dict[str, object]:
    """Recompute every series summary in ``manifest.json`` (contract §1).

    ``content_hash`` is SHA-256 over the CSV bytes on disk, so the manifest
    identifies exactly the artifact a consumer will read. ``corrections`` is
    carried through untouched -- it is append-only.
    """
    manifest = read_manifest(manifest_path)
    series = dict(manifest.get("series") or {})

    aaa_path = os.path.join(gas_dir, "aaa_daily_national.csv")
    aaa = _summarise_csv(aaa_path, "date")
    by_source = {SOURCE_LIVE: 0, SOURCE_WAYBACK: 0}
    suspect = 0
    for row in read_aaa_csv(aaa_path):
        src = (row.get("source") or "").strip()
        by_source[src] = by_source.get(src, 0) + 1
        if (row.get("quality") or "").strip() == QUALITY_SUSPECT:
            suspect += 1
    aaa["by_source"] = by_source
    aaa["suspect"] = suspect
    series["aaa_daily_national"] = aaa

    series["eia_weekly_regular"] = _summarise_csv(
        os.path.join(gas_dir, "eia_weekly_regular.csv"), "week_ending"
    )
    series["rbob_daily"] = _summarise_csv(
        os.path.join(gas_dir, "rbob_daily.csv"), "date"
    )

    manifest["series"] = series
    manifest["generated_at"] = _utc_now_iso()
    manifest.setdefault("corrections", [])
    write_manifest(manifest, manifest_path)
    return manifest


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
def post_discord_alert(reason_code: str, detail: str) -> bool:
    """Post a scrape-failure alert to Discord when a webhook is configured.

    Mirrors ``scripts/reconcile_weather.py``'s ``post_discord_alert``: silent
    no-op when ``DISCORD_WEBHOOK_URL`` is unset so tests and laptops do not need
    a webhook. Returns whether a message was actually delivered.

    Per PRD §6 "alerts are actionable": this fires only when the day's value
    could not be obtained, which does require operator attention -- it stops gas
    signals for the day.
    """
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        logger.info("DISCORD_WEBHOOK_URL not set - skipping AAA scrape alert")
        return False
    if requests is None:  # pragma: no cover
        logger.warning("requests unavailable - cannot post AAA scrape alert")
        return False
    payload = {
        "embeds": [
            {
                "title": "AAA gas scrape failed - gas signals disabled",
                "description": (
                    f"**Reason:** `{reason_code}`\n"
                    f"{detail}\n\n"
                    f"No AAA national average was recorded. Gas signal "
                    f"generation is aborted until a value is available "
                    f"(FR-4.1)."
                ),
                "color": 0xE74C3C,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        ok = resp.status_code < 400
        logger.info(
            "AAA scrape alert %s",
            "sent" if ok else f"failed (HTTP {resp.status_code})",
        )
        return ok
    except Exception as exc:
        logger.warning("AAA scrape alert failed: %s", exc)
        return False


def read_scrape_failures(path: str = SCRAPE_FAILURES_PATH) -> Dict[str, List[dict]]:
    """Read the ET-day -> failure-records map. Missing file reads as empty."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return {str(k): list(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError, AttributeError, TypeError) as exc:
        logger.warning("scrape_failures.json unreadable (%s); treating as empty", exc)
        return {}


def record_scrape_failure(
    day: _date,
    reason_code: str,
    detail: str,
    *,
    path: str = SCRAPE_FAILURES_PATH,
    max_days: int = 90,
) -> None:
    """Persist that the scrape for ``day`` failed.

    Append-only within a day, and pruned to ``max_days`` so the file cannot grow
    without bound. Written atomically -- a half-written failure file that reads as
    empty would silently re-enable trading on a failed day, which is the opposite
    of what it exists to prevent.
    """
    failures = read_scrape_failures(path)
    failures.setdefault(day.isoformat(), []).append(
        {"at": _utc_now_iso(), "reason_code": reason_code, "detail": detail}
    )
    for stale in sorted(failures)[:-max_days] if len(failures) > max_days else []:
        failures.pop(stale, None)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(failures, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:  # pragma: no cover
        logger.warning("could not record AAA scrape failure: %s", exc)


def _default_alert_hook(reason_code: str, detail: str) -> None:
    """One WARNING line, then Discord. The log line is the alert of record.

    Logging first and unconditionally matters: Discord may be unconfigured or
    down, and an alert that exists only in a webhook that failed is not an
    alert.
    """
    logger.warning("AAA_SCRAPE_FAILED %s: %s", reason_code, detail)
    post_discord_alert(reason_code, detail)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class _CrawlLimiter:
    """Process-wide minimum spacing between requests to one host.

    Per-instance state would be defeated by two provider objects in one
    process, which is precisely how a polite crawler accidentally stops being
    one.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, sleeper: Callable[[float], None] = time.sleep) -> float:
        with self._lock:
            now = time.monotonic()
            gap = now - self._last
            delay = (
                self.min_interval - gap
                if self._last and gap < self.min_interval
                else 0.0
            )
            if delay > 0:
                sleeper(delay)
            self._last = time.monotonic()
            return delay


_AAA_LIMITER = _CrawlLimiter(CRAWL_DELAY_SECONDS)


class AAAProvider:
    """Reads the AAA national average for regular gasoline (FR-4.1).

    One request per day is all the live recorder needs, and
    :data:`CRAWL_DELAY_SECONDS` is enforced regardless.

    Every failure path raises :class:`AAAUnavailable` and fires the alert hook
    exactly once. Nothing here returns a stale or defaulted price.
    """

    def __init__(
        self,
        *,
        url: str = AAA_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 45.0,
        csv_path: str = AAA_CSV_PATH,
        manifest_path: str = MANIFEST_PATH,
        failures_path: str = SCRAPE_FAILURES_PATH,
        alert_hook: Optional[Callable[[str, str], None]] = None,
        session=None,
        limiter: Optional[_CrawlLimiter] = None,
        publication_hour_et: Optional[int] = None,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout
        self.csv_path = csv_path
        self.manifest_path = manifest_path
        self.failures_path = failures_path
        self.alert_hook = alert_hook or _default_alert_hook
        self.publication_hour_et = publication_hour_et
        self._limiter = limiter or _AAA_LIMITER
        self._session = session
        #: Reason codes alerted on, in order, for assertions and diagnostics.
        self.alerts: List[Tuple[str, str]] = []

    # -- plumbing --------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            if requests is None:  # pragma: no cover
                raise AAAUnavailable(
                    REASON_OFFLINE, "requests is unavailable in this environment"
                )
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        return self._session

    def _alert(self, reason_code: str, detail: str) -> None:
        self.alerts.append((reason_code, detail))
        try:
            self.alert_hook(reason_code, detail)
        except Exception as exc:  # pragma: no cover - an alert must not mask the abort
            logger.warning("AAA alert hook raised %s: %s", type(exc).__name__, exc)

    def connect(self) -> bool:
        """Cheap reachability probe. Never raises; ``False`` means unreachable."""
        try:
            self.fetch_current(alert_on_failure=False)
            return True
        except AAAUnavailable as exc:
            logger.info("AAAProvider.connect failed: %s", exc)
            return False

    # -- fetch -----------------------------------------------------------
    def today_et(self, as_of: Optional[_date] = None) -> _date:
        """The **AAA publication day** this process is working on, in ET.

        Injectable rather than implicit. The VM runs UTC and AAA publishes ET, so
        every place this code says "today" is a place the two can diverge -- and
        an untestable "now" is how that divergence stays invisible.

        This is deliberately the *attributed* publication day
        (:func:`attribute_et_date`), **not** the raw ET calendar date, and it is
        computed by the very same function -- with the very same
        ``publication_hour_et`` override -- that decides what date a fetched
        observation is written under. Before the schedule's publication hour the
        two answers differ: at 01:00 ET the page still shows the previous day's
        figure, so a successful fetch writes a row dated ``D-1`` while the raw ET
        date is ``D``. Keying the failure record on the raw ET date would record
        the failure against ``D`` and clear it only with a row for ``D``, which
        that fetch never writes -- the block would never lift and the day would be
        stranded. Sharing one attribution function makes that decoupling
        impossible by construction rather than by an operational instruction to
        run the cron between 12:00 and 20:00 ET (advice is not code).

        A gas *market* day is a different quantity -- Kalshi's ET calendar date --
        and lives in :data:`src.strategies.gas_convergence.MARKET_TZ`. The two are
        not interchangeable and are deliberately not shared.
        """
        if as_of is not None:
            return as_of
        return attribute_et_date(
            datetime.now(timezone.utc), publication_hour_et=self.publication_hour_et
        )

    def fetch_current(
        self, *, alert_on_failure: bool = True, as_of: Optional[_date] = None
    ) -> AAAObservation:
        """Fetch and parse today's AAA national average.

        :param as_of: ET date the attempt belongs to; defaults to the real ET
            today. A failure is recorded against this date, which is what blocks
            signals for the day.
        :raises AAAUnavailable: on any transport, HTTP, or parse failure. The
            alert hook fires once first (unless ``alert_on_failure=False``,
            which exists for the reachability probe so a probe does not page
            the operator).
        """
        try:
            return self._fetch_current_inner()
        except AAAUnavailable as exc:
            if alert_on_failure:
                # Persist the failure BEFORE alerting: the alert is best-effort
                # (Discord may be down) but the signal block must not be.
                record_scrape_failure(
                    self.today_et(as_of),
                    exc.reason_code,
                    exc.detail or str(exc),
                    path=self.failures_path,
                )
                self._alert(exc.reason_code, exc.detail or str(exc))
            raise

    def _fetch_current_inner(self) -> AAAObservation:
        self._limiter.wait()
        requested_at = datetime.now(timezone.utc)
        try:
            resp = self.session.get(self.url, timeout=self.timeout)
        except AAAUnavailable:
            raise
        except Exception as exc:
            raise AAAUnavailable(
                REASON_OFFLINE, f"GET {self.url} raised {type(exc).__name__}: {exc}"
            ) from exc

        status = getattr(resp, "status_code", None)
        if status != 200:
            retry = " (retryable)" if status in RETRYABLE_STATUSES else ""
            raise AAAUnavailable(
                REASON_HTTP_ERROR, f"GET {self.url} returned HTTP {status}{retry}"
            )

        body = resp.content or b""
        return parse_national_table(
            body,
            source_url=self.url,
            captured_at=requested_at,
            source=SOURCE_LIVE,
            publication_hour_et=self.publication_hour_et,
            fetched_at=requested_at.replace(microsecond=0).isoformat(),
        )

    # -- record ----------------------------------------------------------
    def record_daily(self, *, as_of: Optional[_date] = None) -> AAAObservation:
        """Fetch today's value and persist it with provenance (FR-4.1).

        The live recorder's whole job. On failure it alerts, records the failure
        against ``as_of``, and re-raises, so the caller cannot mistake a failed
        day for a recorded one.
        """
        obs = self.fetch_current(as_of=as_of)
        result = upsert_observations(
            [obs], path=self.csv_path, manifest_path=self.manifest_path
        )
        update_manifest(
            gas_dir=os.path.dirname(os.path.abspath(self.csv_path)),
            manifest_path=self.manifest_path,
        )
        logger.info(
            "AAA recorded %s = $%.3f (%s, quality=%s); series now %d rows "
            "(added=%d unchanged=%d corrections=%d)",
            obs.date.isoformat(),
            obs.value,
            obs.source,
            obs.quality,
            result["rows"],
            result["added"],
            result["unchanged"],
            result["corrections"],
        )
        return obs

    # -- read back -------------------------------------------------------
    def latest_recorded(
        self, *, include_suspect: bool = False
    ) -> Optional[Tuple[_date, float, Dict[str, str]]]:
        """Newest persisted row, or ``None`` when the series is empty."""
        rows = read_aaa_csv(self.csv_path)
        best: Optional[Tuple[_date, float, Dict[str, str]]] = None
        for row in rows:
            if not include_suspect and (row.get("quality") or "") == QUALITY_SUSPECT:
                continue
            try:
                day = _date.fromisoformat((row.get("date") or "").strip())
                val = float(row.get("value") or "")
            except (TypeError, ValueError):
                continue
            if best is None or day > best[0]:
                best = (day, val, row)
        return best

    def consecutive_days_recorded(
        self, *, as_of: Optional[_date] = None, include_suspect: bool = False
    ) -> int:
        """Length of the unbroken daily run ending at the newest recorded row.

        Phase 4 exit criterion 1 gates on ">=14 consecutive daily values
        persisted with provenance", so the count is computed here rather than
        eyeballed from the CSV. ``suspect`` rows do not extend the run by
        default: a run counted through a row excluded from fits would overstate
        the evidence.
        """
        rows = read_aaa_csv(self.csv_path)
        days = set()
        for row in rows:
            if not include_suspect and (row.get("quality") or "") == QUALITY_SUSPECT:
                continue
            try:
                days.add(_date.fromisoformat((row.get("date") or "").strip()))
            except (TypeError, ValueError):
                continue
        if as_of is not None:
            days = {d for d in days if d <= as_of}
        if not days:
            return 0
        run = 0
        cur = max(days)
        while cur in days:
            run += 1
            cur -= timedelta(days=1)
        return run

    def longest_consecutive_run(
        self, *, include_suspect: bool = False
    ) -> Dict[str, object]:
        """Longest unbroken daily run **anywhere** in the series.

        Distinct from :meth:`consecutive_days_recorded`, which measures only the
        run ending at the newest row and therefore collapses whenever the most
        recent days contain a ``suspect``. Phase 4 exit criterion 1 asks for
        ">=14 consecutive daily values persisted with provenance", which is a
        property of the series as a whole, so it is computed rather than read off
        by eye.

        Returns ``{"days", "first", "last"}``; ``days`` is 0 for an empty series.
        """
        days = set()
        for row in read_aaa_csv(self.csv_path):
            if not include_suspect and (row.get("quality") or "") == QUALITY_SUSPECT:
                continue
            try:
                days.add(_date.fromisoformat((row.get("date") or "").strip()))
            except (TypeError, ValueError):
                continue
        best = {"days": 0, "first": None, "last": None}
        for day in days:
            if day - timedelta(days=1) in days:
                continue  # not the start of a run
            length = 0
            cur = day
            while cur in days:
                length += 1
                cur += timedelta(days=1)
            if length > int(best["days"]):
                best = {
                    "days": length,
                    "first": day.isoformat(),
                    "last": (day + timedelta(days=length - 1)).isoformat(),
                }
        return best

    # -- the gate a strategy consumes ------------------------------------
    def signal_gate(
        self,
        *,
        as_of: Optional[_date] = None,
        max_age_days: int = 2,
        allow_suspect: bool = False,
    ) -> SignalGate:
        """May gas signals be produced right now?

        FR-4.1 ("scrape failure ... aborts signals") and contract §3's data
        freshness gate ("newest ``aaa_daily_national`` row is ``<= 2`` days
        old"). ``allow=False`` must short-circuit signal generation before any
        projection runs.

        Freshness is judged from the **persisted series**, not from an in-memory
        fetch result, so a process that failed its scrape today and a process
        that never ran at all are treated identically -- both have no fresh row,
        and neither may trade.
        """
        today = self.today_et(as_of)

        # FR-4.1 "scrape failure ... aborts signals" / EC-1 "zero signals that
        # day". This is a SEPARATE condition from the contract §3 freshness gate
        # below, and both must hold. Freshness alone would let a day whose scrape
        # failed trade on yesterday's row, which is exactly the day an operator
        # has been told to look at.
        #
        # The block is cleared ONLY by a genuinely live observation for the same
        # publication day -- ``source == aaa_live``, i.e. positive evidence that
        # *today's own fetch* succeeded. The earlier version accepted any row for
        # the day, which broke the mechanism in the shipped configuration: the
        # Wayback backfill routinely writes a row for the current day, so a
        # recorded failure was cleared by an archived capture and ``allow`` came
        # back True with an alert already sent.
        #
        # A same-day ``aaa_wayback`` row must never clear the block, under any
        # circumstance. Three reasons, and they are about what the row is evidence
        # OF rather than about whether its number is good:
        #
        # 1. It says nothing about the live path. The recorded failure means the
        #    recorder could not reach or parse AAA; an archived copy of the page
        #    is not evidence that condition has ended, and the operator alert is
        #    still outstanding.
        # 2. It is available on demand. ``backfill_gas_history.py`` can be re-run
        #    at any moment, so accepting a Wayback row makes clearing an operator
        #    alert something any unrelated process can do as a side effect --
        #    the failure record would stop being a gate and become a suggestion.
        # 3. Provenance timing. A capture archived early on the publication day
        #    can predate AAA's republication, so a same-day archived row is not
        #    even reliably the day's settled figure.
        #
        # Abort, never default (``abort-on-missing-critical-input``): the absence
        # of a live row is treated as "no live value today", not as "probably
        # fine".
        failures = read_scrape_failures(self.failures_path)
        today_failures = failures.get(today.isoformat()) or []
        if today_failures:
            rows_today = [
                r
                for r in read_aaa_csv(self.csv_path)
                if (r.get("date") or "").strip() == today.isoformat()
            ]
            live_today = [
                r
                for r in rows_today
                if (r.get("source") or "").strip() == SOURCE_LIVE
                and (
                    allow_suspect or (r.get("quality") or "").strip() != QUALITY_SUSPECT
                )
            ]
            if not live_today:
                last = today_failures[-1]
                sources = sorted(
                    {
                        (
                            f"{(r.get('source') or '?').strip()}"
                            f"/{(r.get('quality') or '?').strip()}"
                        )
                        for r in rows_today
                    }
                )
                detail = (
                    f"{len(today_failures)} scrape failure(s) recorded for "
                    f"{today.isoformat()} and no admissible {SOURCE_LIVE} row "
                    f"persisted for that day; rows present for that day: "
                    f"{len(rows_today)} {sources or '[]'}; most recent "
                    f"{last.get('reason_code')}: {last.get('detail')}"
                )
                logger.info(
                    "GAS_SIGNAL_BLOCKED %s: %s", REASON_SCRAPE_FAILED_TODAY, detail
                )
                return SignalGate(False, REASON_SCRAPE_FAILED_TODAY, detail)

        latest = self.latest_recorded(include_suspect=allow_suspect)
        if latest is None:
            detail = f"no admissible rows in {self.csv_path}"
            logger.info("GAS_SIGNAL_BLOCKED %s: %s", REASON_NO_DATA, detail)
            return SignalGate(False, REASON_NO_DATA, detail)

        day, value, row = latest
        age = (today - day).days
        if age > max_age_days:
            detail = (
                f"newest AAA row is {day.isoformat()} = {age} days old "
                f"(limit {max_age_days}) as of {today.isoformat()}"
            )
            logger.info("GAS_SIGNAL_BLOCKED %s: %s", REASON_STALE, detail)
            return SignalGate(False, REASON_STALE, detail, age_days=age)

        obs = AAAObservation(
            date=day,
            value=value,
            source=row.get("source", ""),
            source_url=row.get("source_url", ""),
            fetched_at=row.get("fetched_at", ""),
            raw_sha256=row.get("raw_sha256", ""),
            quality=row.get("quality", QUALITY_OK),
        )
        logger.info(
            "GAS_SIGNAL_ALLOWED newest AAA row %s = $%.3f (%d days old, "
            "limit %d, quality=%s)",
            day.isoformat(),
            value,
            age,
            max_age_days,
            obs.quality,
        )
        return SignalGate(True, None, "", observation=obs, age_days=age)


# ---------------------------------------------------------------------------
# CLI -- the daily recorder's entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Record today's value, or report the series state.

    Invoke as ``python -m src.data.aaa_provider --record`` (one request/day).
    Exit codes are meaningful so an unattended cron cannot mistake a failed
    scrape for a recorded day:

    * ``0`` -- recorded, or the requested report succeeded
    * ``1`` -- scrape or parse failed; an alert was emitted and no row written
    * ``2`` -- signals are blocked (``--gate`` only)
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="AAA national-average gasoline recorder (PRD FR-4.1)."
    )
    parser.add_argument(
        "--record", action="store_true", help="fetch today's value and persist it"
    )
    parser.add_argument(
        "--gate", action="store_true", help="report whether gas signals are allowed"
    )
    parser.add_argument(
        "--status", action="store_true", help="report row count and consecutive-day run"
    )
    parser.add_argument("--csv-path", default=AAA_CSV_PATH)
    parser.add_argument("--manifest-path", default=MANIFEST_PATH)
    parser.add_argument("--max-age-days", type=int, default=2)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    provider = AAAProvider(csv_path=args.csv_path, manifest_path=args.manifest_path)

    if args.record:
        try:
            obs = provider.record_daily()
        except AAAUnavailable as exc:
            # The alert has already fired inside record_daily. Exiting non-zero is
            # what stops a cron wrapper from reporting a successful day.
            print(f"FAILED {exc.reason_code}: {exc.detail}", flush=True)
            return 1
        print(
            f"recorded {obs.date.isoformat()} = ${obs.value:.3f} "
            f"({obs.source}, quality={obs.quality}, sha256={obs.raw_sha256[:16]})",
            flush=True,
        )

    if args.status or args.record:
        rows = read_aaa_csv(args.csv_path)
        run = provider.consecutive_days_recorded()
        longest = provider.longest_consecutive_run()
        print(
            f"series: {len(rows)} rows; run ending at the newest row = "
            f"{run} day(s); longest run anywhere = {longest['days']} day(s) "
            f"({longest['first']} .. {longest['last']})",
            flush=True,
        )

    if args.gate:
        gate = provider.signal_gate(max_age_days=args.max_age_days)
        if gate.allow:
            print(
                f"signals ALLOWED (newest row {gate.observation.date.isoformat()} "
                f"= ${gate.observation.value:.3f}, {gate.age_days} day(s) old)",
                flush=True,
            )
        else:
            print(f"signals BLOCKED {gate.reason_code}: {gate.detail}", flush=True)
            return 2

    if not (args.record or args.gate or args.status):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
