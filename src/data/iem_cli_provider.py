"""Ground-truth daily-high provider: NWS Climatological Report (CLI) via IEM.

PRD FR-1.3 (settlement recorder) and FR-1.6 (historical backfill). This is the
*settlement truth* source for the KXHIGH* weather markets: Kalshi's own rules
text names it explicitly --

    "If the highest temperature recorded in Central Park, New York for
     July 24, 2026 as reported by the National Weather Service's
     Climatological Report (Daily), is greater than 88 degrees, then the
     market resolves to Yes."
    -- rules_primary, KXHIGHNY-26JUL24-T88, fetched 2026-07-25

VERIFIED UPSTREAM CONTRACT (probed live 2026-07-25)
---------------------------------------------------
Endpoint: ``GET https://mesonet.agron.iastate.edu/json/cli.py``

===============================  ========  ==========================================
Query                            Status    Result
===============================  ========  ==========================================
``station=KNYC&year=2026``       200       205 rows, 2026-01-01 .. 2026-07-24
``station=KNYC&year=2025``       200       363 rows, 2025-01-01 .. 2025-12-31 (2 gaps)
``station=KNYC&date=2026-07-24`` 200       **365 rows of 2019** -- ``date`` IGNORED
``station=KNYC`` (no filter)     200       **365 rows of 2019** -- same default
``station=KNYC&year=2026&fmt=json`` 200    byte-identical to no ``fmt`` -- no-op
``station=ZZZZ&year=2026``       200       ``{"results": []}`` -- bad station is NOT an error
``station=KNYC&year=2099``       422       out-of-range year
===============================  ========  ==========================================

Two traps follow directly from that table and are guarded in code:

1. **``date`` is not a filter.** Omitting ``year`` (or passing only ``date``)
   silently returns the 2019 archive with HTTP 200. A caller that trusted it
   would backfill five-year-old temperatures as today's truth. ``year`` is
   therefore always sent, and :meth:`IEMCLIProvider.fetch_year` re-validates
   that every returned row's ``valid`` field actually falls in the requested
   year, raising :class:`IEMCLIError` if not.
2. **An unknown station returns 200 with zero rows.** That is indistinguishable
   from a real outage at the HTTP layer, so an empty archive is logged at ERROR
   and surfaced as ``None`` / ``[]`` -- never as a substituted value from a
   neighbouring station (project rule ``abort-on-missing-critical-input``).

Row shape (fields this module consumes): ``station``, ``valid`` (``YYYY-MM-DD``),
``high`` (int F), ``low``, ``high_time`` (e.g. ``"128 PM"``), ``product``
(e.g. ``202607250626-KOKX-CDUS41-CLINYC``), ``link``, ``name``, ``wfo``.

CROSS-CHECK AGAINST THE SETTLEMENT AUTHORITY (2026-07-25)
---------------------------------------------------------
Kalshi publishes its own settlement input on every finalized market as
``expiration_value``. It matched the IEM CLI high exactly for all twelve
city/date pairs probed (2026-07-22..24 x NY/CHI/LAX/MIA)::

    KXHIGHNY-26JUL22 85.00 == KNYC 85     KXHIGHCHI-26JUL22 73.00 == KMDW 73
    KXHIGHNY-26JUL23 79.00 == KNYC 79     KXHIGHCHI-26JUL23 77.00 == KMDW 77
    KXHIGHNY-26JUL24 83.00 == KNYC 83     KXHIGHCHI-26JUL24 79.00 == KMDW 79
    KXHIGHLAX-26JUL22 77.00 == KLAX 77    KXHIGHMIA-26JUL22 92.00 == KMIA 92
    KXHIGHLAX-26JUL23 80.00 == KLAX 80    KXHIGHMIA-26JUL23 93.00 == KMIA 93
    KXHIGHLAX-26JUL24 78.00 == KLAX 78    KXHIGHMIA-26JUL24 93.00 == KMIA 93

``scripts/reconcile_weather.py`` re-runs that comparison daily.

FALLBACK (PRD FR-1.3, "NWS CLI product fallback")
-------------------------------------------------
When IEM has no row for a date, the raw NWS text product is parsed instead.
Two routes were verified, both returning the same product for 2026-07-24:

* ``https://mesonet.agron.iastate.edu/api/1/nwstext/<product>`` -- needs the
  ``product`` id, which only IEM knows, so it is useful for re-parsing a row
  IEM already has, not for filling a gap.
* ``https://api.weather.gov/products/types/CLI/locations/<CLI_LOC>`` -- the
  independent route, and the one used for gap-filling. The location code is the
  CLI site code, **not** the WFO: ``NYC``/``MDW``/``LAX``/``MIA`` all return
  products; ``CHI`` returns zero (``ORD`` also works but is the wrong station
  for Kalshi's Chicago settlement, which is Midway).

Any value obtained this way is tagged ``source="nws_cli"`` in its provenance so
a downstream consumer can tell which authority produced it.

CACHING RULES (each one exists because its absence was a demonstrated defect)
----------------------------------------------------------------------------
Caching a *published* value is free accuracy; caching the *absence* of one is
how a hold-and-retry design dies. Three rules keep them apart:

1. **In-memory and on-disk snapshots share one freshness rule.** A closed past
   year is immutable and cached forever; the current year expires after
   :attr:`IEMCLIProvider.current_year_ttl`. The process-lifetime memo used to
   have no expiry at all, so a long-lived provider (and
   ``weather_settlement``'s module-level singleton is exactly that) answered
   every later lookup from the first snapshot it ever took.
2. **"Not published yet" is never cached as an answer.** When the requested
   date has no row -- or a row with no maximum -- and the date is recent enough
   that the product could still be issued
   (:attr:`IEMCLIProvider.pending_lookback_days`), the year is re-fetched live
   before ``None`` is returned. Without this, a position whose settlement day
   ended before the ~06:26-local CLI issuance held open until the process
   restarted, no matter how often the expiry sweep retried.
3. **An empty response is never persisted.** IEM answers HTTP 200 with zero
   rows both for an unknown station and for a transient upstream fault, and a
   past year's cache is otherwise treated as immutable -- so one blip during a
   backfill would truncate that station-year permanently. Empty results are
   returned to the caller (and logged at ERROR) but never written to disk or
   memoized.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

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
WEATHER_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "weather_truth")
CACHE_SUBDIR = "cache"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
IEM_NWSTEXT_URL = "https://mesonet.agron.iastate.edu/api/1/nwstext/{product}"
NWS_PRODUCT_LIST_URL = "https://api.weather.gov/products/types/CLI/locations/{loc}"

DEFAULT_USER_AGENT = (
    "money-printer/weather-truth (github.com/JusHoya/money_printer; "
    "contact via repo owner)"
)

SOURCE_IEM = "iem_cli"
SOURCE_NWS = "nws_cli"

# Highest year the archive will accept (probe: 2099 -> HTTP 422).
_MIN_YEAR = 1900
_MAX_YEAR = 2100


# ---------------------------------------------------------------------------
# Station registry (PRD A4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StationSpec:
    """One settlement station and the Kalshi series it settles.

    ``cli_location`` is the NWS CLI *site* code used by
    ``api.weather.gov/products/types/CLI/locations/<code>`` -- it is not the
    WFO id and not the ICAO id, which is why it is recorded explicitly.
    """

    station: str
    series_ticker: str
    city: str
    timezone: str
    cli_location: str
    site_name: str
    verified: bool = False


#: The four PRD A4 core cities. Every field below was probed live 2026-07-25.
CORE_STATIONS: Dict[str, StationSpec] = {
    "KNYC": StationSpec(
        station="KNYC",
        series_ticker="KXHIGHNY",
        city="New York",
        timezone="America/New_York",
        cli_location="NYC",
        site_name="CENTRAL PARK NY",
        verified=True,
    ),
    "KMDW": StationSpec(
        station="KMDW",
        series_ticker="KXHIGHCHI",
        city="Chicago",
        timezone="America/Chicago",
        cli_location="MDW",
        site_name="CHICAGO MIDWAY AIRPORT IL",
        verified=True,
    ),
    "KLAX": StationSpec(
        station="KLAX",
        series_ticker="KXHIGHLAX",
        city="Los Angeles",
        timezone="America/Los_Angeles",
        cli_location="LAX",
        site_name="LOS ANGELES AIRPORT CA",
        verified=True,
    ),
    "KMIA": StationSpec(
        station="KMIA",
        series_ticker="KXHIGHMIA",
        city="Miami",
        timezone="America/New_York",
        cli_location="MIA",
        site_name="MIAMI FL",
        verified=True,
    ),
}

#: PRD A4 expansion set. Deliberately NOT in :data:`STATIONS` until each entry's
#: settlement station, CLI location code and Kalshi series ticker have been
#: probed the same way the core four were. ``verified=False`` makes an
#: unverified entry impossible to use by accident -- :func:`get_station` refuses
#: it unless the caller passes ``allow_unverified=True``.
EXPANSION_STATIONS: Dict[str, StationSpec] = {
    "KDEN": StationSpec(
        "KDEN", "KXHIGHDEN", "Denver", "America/Denver", "DEN", "DENVER CO"
    ),
    "KPHL": StationSpec(
        "KPHL",
        "KXHIGHPHIL",
        "Philadelphia",
        "America/New_York",
        "PHL",
        "PHILADELPHIA PA",
    ),
    "KAUS": StationSpec(
        "KAUS", "KXHIGHAUS", "Austin", "America/Chicago", "AUS", "AUSTIN TX"
    ),
    "KHOU": StationSpec(
        "KIAH", "KXHIGHHOU", "Houston", "America/Chicago", "IAH", "HOUSTON TX"
    ),
}

#: The active registry. Extending to an expansion city is a one-line move from
#: :data:`EXPANSION_STATIONS` once its fields are probed and ``verified=True``.
STATIONS: Dict[str, StationSpec] = dict(CORE_STATIONS)

#: Convenience: Kalshi series ticker -> station.
SERIES_TO_STATION: Dict[str, str] = {
    spec.series_ticker: station for station, spec in STATIONS.items()
}


def get_station(station: str, *, allow_unverified: bool = False) -> StationSpec:
    """Look up a :class:`StationSpec`, refusing unverified expansion entries."""
    key = str(station).upper().strip()
    if key in STATIONS:
        return STATIONS[key]
    if key in EXPANSION_STATIONS:
        spec = EXPANSION_STATIONS[key]
        if not (allow_unverified or spec.verified):
            raise IEMCLIError(
                f"{key} is an unverified expansion station: probe its CLI "
                f"location code and Kalshi series before trading it, or pass "
                f"allow_unverified=True for research use"
            )
        return spec
    raise IEMCLIError(
        f"unknown station {station!r} (known: {sorted(STATIONS)} + "
        f"expansion {sorted(EXPANSION_STATIONS)})"
    )


def station_for_series(series_ticker: str) -> Optional[str]:
    """Reverse lookup: ``KXHIGHNY`` -> ``KNYC``. ``None`` if unmapped."""
    return SERIES_TO_STATION.get(str(series_ticker).upper().strip())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class IEMCLIError(RuntimeError):
    """Ground truth could not be established.

    Always a hard abort for the caller. There is no safe default for "what was
    the high temperature": a wrong value settles simulated positions against
    fiction, which is precisely the failure class this pivot exists to remove.
    """


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_MISSING_TOKENS = {"", "M", "MM", "T", "NA", "N/A", "NONE", "NULL"}


def normalize_date(value: Any) -> str:
    """Coerce ``date``/``datetime``/``str`` to an ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        raise IEMCLIError("empty date")
    # Tolerate a full timestamp, but require an ISO-ish prefix.
    candidate = text[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError as exc:
        raise IEMCLIError(f"unparseable date {value!r} (want YYYY-MM-DD)") from exc
    return candidate


def _as_int(value: Any) -> Optional[int]:
    """Parse an integer temperature, treating IEM's missing tokens as ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int,)):
        return int(value)
    if isinstance(value, float):
        return int(round(value))
    text = str(value).strip()
    if text.upper() in _MISSING_TOKENS:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in _MISSING_TOKENS:
        return None
    return text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _parse_utc_stamp(value: Any) -> Optional[float]:
    """``"2026-07-25T00:00:00Z"`` -> epoch seconds, or ``None`` if unparseable."""
    if not value:
        return None
    try:
        return (
            datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError):
        return None


def date_range(start_date: Any, end_date: Any) -> List[str]:
    """Inclusive list of ``YYYY-MM-DD`` strings from ``start_date`` to ``end_date``."""
    start = datetime.strptime(normalize_date(start_date), "%Y-%m-%d").date()
    end = datetime.strptime(normalize_date(end_date), "%Y-%m-%d").date()
    if end < start:
        raise IEMCLIError(f"end_date {end} precedes start_date {start}")
    out: List[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Row normalization
# ---------------------------------------------------------------------------
#: Stable column order for every CSV this module and its scripts emit.
ROW_FIELDS = (
    "station",
    "date",
    "high",
    "low",
    "high_time",
    "source",
    "source_url",
    "product",
    "fetched_at",
)


def normalize_row(
    raw: Mapping[str, Any], *, source_url: str, fetched_at: str
) -> Dict[str, Any]:
    """Map one raw IEM ``results`` entry onto the :data:`ROW_FIELDS` shape.

    ``high`` is ``None`` when the archive carries no maximum for the day; the
    row is still returned so callers can see the gap explicitly rather than
    inferring it from an absence.
    """
    return {
        "station": str(raw.get("station") or "").upper() or None,
        "date": normalize_date(raw.get("valid")),
        "high": _as_int(raw.get("high")),
        "low": _as_int(raw.get("low")),
        "high_time": _clean_str(raw.get("high_time")),
        "source": SOURCE_IEM,
        "source_url": source_url,
        "product": _clean_str(raw.get("product")),
        "fetched_at": fetched_at,
    }


# ---------------------------------------------------------------------------
# NWS CLI text parsing (fallback path)
# ---------------------------------------------------------------------------
_SUMMARY_RE = re.compile(
    r"\.\.\.\s*(?:THE\s+)?(?P<site>.+?)\s+CLIMATE\s+SUMMARY\s+FOR\s+"
    r"(?P<month>[A-Z]+)\.?\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
# "  MAXIMUM         83    128 PM ..." -- but NOT "MAXIMUM TEMPERATURE (F)   85"
# from the month-to-date block further down the product.
_MAXIMUM_RE = re.compile(r"^\s*MAXIMUM\s+(-?\d+)\b")
_SECTION_RE = re.compile(r"^\s*(YESTERDAY|TODAY)\b", re.IGNORECASE)
_TEMP_HEADER_RE = re.compile(r"^\s*TEMPERATURE\s*\(F\)", re.IGNORECASE)

_MONTHS = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "SEPT": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def parse_cli_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract ``{date, high, site, section}`` from a raw NWS CLI product.

    Returns ``None`` when the product is not a parseable climate summary. A
    product whose header parses but whose MAXIMUM line does not yields
    ``high=None`` -- an explicit "no value", never a guess.

    Two CLI products are issued per site per day: a late-evening preliminary
    one summarising ``TODAY``, and a next-morning one summarising
    ``YESTERDAY``. Both name the same calendar date in the header, so the
    section label is returned for the caller to prefer the final (``YESTERDAY``)
    issuance.
    """
    if not text:
        return None
    m = _SUMMARY_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group("month").upper())
    if month is None:
        return None
    try:
        summary_date = _date(
            int(m.group("year")), month, int(m.group("day"))
        ).isoformat()
    except ValueError:
        return None

    high: Optional[int] = None
    section: Optional[str] = None
    seen_temp_header = False
    current_section: Optional[str] = None
    for line in text.splitlines():
        if _TEMP_HEADER_RE.match(line):
            seen_temp_header = True
            continue
        if not seen_temp_header:
            continue
        sec = _SECTION_RE.match(line)
        if sec:
            current_section = sec.group(1).upper()
            continue
        mx = _MAXIMUM_RE.match(line)
        if mx:
            high = int(mx.group(1))
            section = current_section
            break

    return {
        "date": summary_date,
        "high": high,
        "site": m.group("site").strip(),
        "section": section,
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class IEMCLIProvider:
    """Daily-high ground truth from the NWS CLI product.

    Shaped like the project's other providers (``connect()`` plus explicit
    fetch methods) but deliberately **not** a subclass of
    :class:`src.core.interfaces.DataProvider`: that ABC's ``fetch_latest`` must
    return :class:`MarketData` (price/bid/ask/volume), which has no honest
    meaning for a settled daily maximum temperature. Forcing the shape would
    mean inventing fields, so the contract is stated here instead.

    Caching is per ``(station, year)`` under ``data/weather_truth/cache/``, in
    memory and on disk under one freshness rule; missing truth is never cached
    as an answer. See "CACHING RULES" in the module docstring -- every clause
    there is load-bearing for the FR-1.3 hold-and-retry design.

    :param current_year_ttl: how long a current-year snapshot (memory or disk)
        stays authoritative. A closed past year never expires.
    :param pending_ttl: minimum seconds between live re-checks of a date whose
        product may still be unissued. The default ``0.0`` re-checks on every
        lookup, because the callers own the throttling that matters
        (``weather_settlement`` retries a given station-day at most once per 15
        minutes, and :attr:`request_pause` rate-limits the archive itself).
    :param pending_lookback_days: how far back a missing row is still treated
        as "not published yet" rather than a settled gap in the archive.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        *,
        session: Any = None,
        user_agent: str = DEFAULT_USER_AGENT,
        request_pause: float = 0.5,
        timeout: int = 30,
        current_year_ttl: int = 6 * 3600,
        pending_ttl: float = 0.0,
        pending_lookback_days: int = 7,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir or os.path.join(WEATHER_TRUTH_DIR, CACHE_SUBDIR)
        self.user_agent = user_agent
        self.request_pause = float(request_pause)
        self.timeout = int(timeout)
        self.current_year_ttl = int(current_year_ttl)
        self.pending_ttl = float(pending_ttl)
        self.pending_lookback_days = int(pending_lookback_days)
        self.offline = bool(offline)
        self._session = session
        #: key -> (epoch seconds the snapshot was taken, rows). The timestamp is
        #: what makes the memo expirable; a bare dict here was permanent.
        self._mem: Dict[str, tuple] = {}
        self._connected = False

    # -- plumbing ---------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            if requests is None:  # pragma: no cover
                raise IEMCLIError(
                    "requests is unavailable; cannot reach the CLI archive"
                )
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        return self._session

    def connect(self) -> bool:
        """Verify the archive answers for a known-good station.

        Returns ``False`` (and logs at ERROR) rather than raising, so a caller
        can decide whether to proceed on cache alone.
        """
        os.makedirs(self.cache_dir, exist_ok=True)
        if self.offline:
            logger.info(
                "IEMCLIProvider: offline mode, using cache at %s", self.cache_dir
            )
            self._connected = True
            return True
        try:
            rows = self._fetch_year_live("KNYC", datetime.now(timezone.utc).year)
        except IEMCLIError as exc:
            logger.error("IEMCLIProvider.connect failed: %s", exc)
            self._connected = False
            return False
        self._connected = bool(rows)
        if not self._connected:
            logger.error("IEMCLIProvider.connect: archive returned zero rows for KNYC")
        return self._connected

    def _get(self, url: str, params: Optional[Mapping[str, Any]] = None):
        if self.offline:
            raise IEMCLIError(f"offline mode: refusing network call to {url}")
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except Exception as exc:
            raise IEMCLIError(f"GET {url} failed: {exc}") from exc
        finally:
            if self.request_pause:
                time.sleep(self.request_pause)
        return resp

    # -- cache ------------------------------------------------------------
    def _cache_path(self, station: str, year: int) -> str:
        return os.path.join(
            self.cache_dir, f"iem_cli_{station.upper()}_{int(year)}.json"
        )

    def _load_cache(self, station: str, year: int) -> Optional[Dict[str, Any]]:
        path = self._cache_path(station, year)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.error(
                "weather-truth cache %s unreadable (%s); ignoring it", path, exc
            )
            return None
        if not isinstance(blob, dict) or not isinstance(blob.get("rows"), list):
            logger.error(
                "weather-truth cache %s has an unexpected shape; ignoring it", path
            )
            return None
        return blob

    def _save_cache(
        self, station: str, year: int, rows: List[Dict[str, Any]], meta: Dict
    ) -> None:
        path = self._cache_path(station, year)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"_meta": meta, "rows": rows}, fh, indent=1, sort_keys=False)
            os.replace(tmp, path)
        except Exception as exc:
            logger.error("could not write weather-truth cache %s: %s", path, exc)

    def _snapshot_is_fresh(self, fetched_epoch: Optional[float], year: int) -> bool:
        """One freshness rule for both the in-memory memo and the disk cache.

        A closed past year is immutable, so any snapshot of it stays valid. The
        current year keeps gaining rows every morning, so its snapshot expires
        after :attr:`current_year_ttl`.
        """
        if year < datetime.now(timezone.utc).year:
            return True  # a closed year cannot gain rows
        if fetched_epoch is None:
            return False
        return (_utc_now_epoch() - fetched_epoch) < self.current_year_ttl

    def _cache_is_fresh(self, blob: Mapping[str, Any], year: int) -> bool:
        # An empty snapshot is never authoritative -- see CACHING RULES (3).
        # Written caches are non-empty by construction, but a file left behind
        # by an older build (or a truncated write) must self-heal rather than
        # freeze a station-year at zero rows.
        if not blob.get("rows"):
            return False
        return self._snapshot_is_fresh(
            _parse_utc_stamp((blob.get("_meta") or {}).get("fetched_at")), year
        )

    def _publication_pending(self, target: str) -> bool:
        """Could the CLI product for ``target`` still be issued?

        The product for day D is issued the following morning local time, and
        IEM ingests it minutes later. Inside the lookback window a missing row
        therefore means "not yet", which must stay re-checkable; outside it, the
        gap is part of the archive and re-querying every lookup buys nothing.
        """
        try:
            day = datetime.strptime(target, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return False
        today = datetime.now(timezone.utc).date()
        return day >= today - timedelta(days=self.pending_lookback_days)

    # -- IEM primary ------------------------------------------------------
    def _fetch_year_live(self, station: str, year: int) -> List[Dict[str, Any]]:
        """One live archive call. Always sends ``year`` -- see module docstring."""
        station = str(station).upper().strip()
        year = int(year)
        if not (_MIN_YEAR <= year <= _MAX_YEAR):
            raise IEMCLIError(f"year {year} outside the archive's accepted range")

        params = {"station": station, "year": year}
        resp = self._get(IEM_CLI_URL, params=params)
        source_url = getattr(resp, "url", IEM_CLI_URL)
        status = getattr(resp, "status_code", None)
        if status != 200:
            raise IEMCLIError(
                f"IEM CLI archive returned HTTP {status} for {station} {year}"
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise IEMCLIError(
                f"IEM CLI archive returned non-JSON for {station} {year}: {exc}"
            )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results"), list
        ):
            raise IEMCLIError(
                f"IEM CLI archive payload for {station} {year} lacks a 'results' list"
            )

        fetched_at = _utc_now_iso()
        rows: List[Dict[str, Any]] = []
        for raw in payload["results"]:
            if not isinstance(raw, Mapping):
                raise IEMCLIError(
                    f"IEM CLI archive row for {station} {year} is not an object"
                )
            row = normalize_row(raw, source_url=source_url, fetched_at=fetched_at)
            # Guard the documented `date`-is-ignored trap: a response that is
            # silently some other year must never be persisted as truth.
            if not row["date"].startswith(f"{year}-"):
                raise IEMCLIError(
                    f"IEM CLI archive returned a {row['date'][:4]} row for a "
                    f"{year} request ({station}); refusing to trust the response "
                    f"(the endpoint defaults to 2019 when 'year' is not honoured)"
                )
            if row["station"] and row["station"] != station:
                raise IEMCLIError(
                    f"IEM CLI archive returned station {row['station']} for a "
                    f"{station} request; refusing a cross-station substitution"
                )
            rows.append(row)

        rows.sort(key=lambda r: r["date"])
        meta = {
            "station": station,
            "year": year,
            "fetched_at": fetched_at,
            "source_url": source_url,
            "row_count": len(rows),
            "provider_generated_at": payload.get("generated_at"),
        }
        if not rows:
            # CACHING RULES (3): an empty 200 is indistinguishable from a
            # transient upstream fault, and a past year's cache would otherwise
            # be treated as immutable forever. Report it, never persist it.
            logger.error(
                "IEM CLI archive returned zero rows for %s %s (HTTP 200). An "
                "unknown station also returns 200/empty, so this is reported as "
                "missing truth, not substituted -- and not cached, so a "
                "transient blip cannot truncate this station-year permanently.",
                station,
                year,
            )
            return rows
        self._save_cache(station, year, rows, meta)
        return rows

    def _year_snapshot(
        self, station: str, year: int, *, force_refresh: bool = False
    ) -> tuple:
        """``(rows, fetched_epoch, came_from_live)`` for one station-year.

        The extra two values are what let :meth:`fetch_row` tell "I already
        asked the archive this instant" from "I am reading a snapshot taken
        some time ago" -- the distinction Finding A turned on.
        """
        station = str(station).upper().strip()
        year = int(year)
        key = f"{station}:{year}"

        if not force_refresh:
            memo = self._mem.get(key)
            if memo is not None and self._snapshot_is_fresh(memo[0], year):
                return [dict(r) for r in memo[1]], memo[0], False

        blob = None if force_refresh else self._load_cache(station, year)
        if blob is not None and self._cache_is_fresh(blob, year):
            rows = [dict(r) for r in blob["rows"]]
            stamp = _parse_utc_stamp((blob.get("_meta") or {}).get("fetched_at"))
            if stamp is None:
                stamp = _utc_now_epoch()
            self._mem[key] = (stamp, rows)
            return [dict(r) for r in rows], stamp, False

        try:
            rows = self._fetch_year_live(station, year)
        except IEMCLIError:
            if blob is not None:
                logger.error(
                    "live IEM fetch for %s %s failed; falling back to the stale "
                    "cache (cached values are real observations, not defaults)",
                    station,
                    year,
                )
                rows = [dict(r) for r in blob["rows"]]
                # Memoized at the snapshot's real age, so the next lookup
                # retries the archive instead of inheriting the outage forever.
                stamp = _parse_utc_stamp((blob.get("_meta") or {}).get("fetched_at"))
                if stamp is None:
                    stamp = 0.0
                self._mem[key] = (stamp, rows)
                return [dict(r) for r in rows], stamp, False
            raise

        stamp = _utc_now_epoch()
        if rows:
            self._mem[key] = (stamp, rows)
        else:
            # CACHING RULES (3): do not memoize an empty archive response.
            self._mem.pop(key, None)
        return [dict(r) for r in rows], stamp, True

    def fetch_year(
        self, station: str, year: int, *, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """All CLI rows for one station-year, normalized to :data:`ROW_FIELDS`.

        Served from memory or the on-disk cache while that snapshot is fresh (a
        closed past year always is; the current year expires after
        :attr:`current_year_ttl`). Raises :class:`IEMCLIError` on any transport
        or shape failure.
        """
        rows, _stamp, _live = self._year_snapshot(
            station, year, force_refresh=force_refresh
        )
        return rows

    def fetch_range(
        self,
        station: str,
        start_date: Any,
        end_date: Any,
        *,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Rows for an inclusive date span, one archive call per calendar year."""
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        if end < start:
            raise IEMCLIError(f"end_date {end} precedes start_date {start}")
        first_year, last_year = int(start[:4]), int(end[:4])

        out: List[Dict[str, Any]] = []
        for year in range(first_year, last_year + 1):
            for row in self.fetch_year(station, year, force_refresh=force_refresh):
                if start <= row["date"] <= end:
                    out.append(row)
        out.sort(key=lambda r: r["date"])
        return out

    @staticmethod
    def _find_row(
        rows: Iterable[Mapping[str, Any]], target: str
    ) -> Optional[Dict[str, Any]]:
        for row in rows:
            if row["date"] == target:
                return dict(row)
        return None

    def fetch_row(self, station: str, date: Any, **kwargs) -> Optional[Dict[str, Any]]:
        """The single normalized row for one station-date, or ``None``.

        CACHING RULES (2): if the date has no published maximum yet and the
        product could still be issued, a cached snapshot is not allowed to be
        the final word -- the year is re-fetched live before ``None`` is
        returned. This is what makes "truth not published yet" a re-checkable
        state instead of a memoized fact, so a position waiting on the morning
        CLI product settles on the retry that follows the issuance rather than
        on the next process restart.
        """
        target = normalize_date(date)
        force = bool(kwargs.pop("force_refresh", False))
        rows, stamp, from_live = self._year_snapshot(
            station, int(target[:4]), force_refresh=force, **kwargs
        )
        row = self._find_row(rows, target)
        if row is not None and row.get("high") is not None:
            return row
        if from_live or not self._publication_pending(target):
            return row
        age = _utc_now_epoch() - (stamp if stamp is not None else 0.0)
        if age < self.pending_ttl:
            return row
        logger.info(
            "no published CLI maximum for %s on %s in a snapshot %.0fs old; "
            "re-checking the archive live (missing truth is never cached)",
            str(station).upper().strip(),
            target,
            max(age, 0.0),
        )
        try:
            rows, _stamp, _live = self._year_snapshot(
                station, int(target[:4]), force_refresh=True
            )
        except IEMCLIError as exc:
            logger.error(
                "live re-check for %s %s failed (%s); keeping the cached view",
                station,
                target,
                exc,
            )
            return row
        return self._find_row(rows, target)

    def fetch_daily_high(self, station: str, date: Any, **kwargs) -> Optional[int]:
        """The CLI daily maximum (F) for one station-date.

        ``None`` -- logged at ERROR -- when the archive has no row for the date
        or the row carries no maximum. Never a substituted or interpolated
        value (project rule ``abort-on-missing-critical-input``).
        """
        target = normalize_date(date)
        row = self.fetch_row(station, target, **kwargs)
        if row is None:
            logger.error(
                "no CLI row for %s on %s: the archive has no climate product "
                "for that date (yet). Reporting missing truth, not a default.",
                station,
                target,
            )
            return None
        if row.get("high") is None:
            logger.error(
                "CLI row for %s on %s carries no maximum temperature "
                "(product %s). Reporting missing truth, not a default.",
                station,
                target,
                row.get("product"),
            )
            return None
        return int(row["high"])

    def latest_available(self, station: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Newest row the archive holds for a station (current year)."""
        rows = [
            r
            for r in self.fetch_year(station, datetime.now(timezone.utc).year, **kwargs)
            if r.get("high") is not None
        ]
        return rows[-1] if rows else None

    # -- NWS fallback -----------------------------------------------------
    def fetch_daily_high_nws(
        self, station: str, date: Any, *, max_products: int = 20
    ) -> Optional[Dict[str, Any]]:
        """Gap-fill one station-date straight from the NWS CLI text product.

        Used only when :meth:`fetch_daily_high` came back empty. Returns a
        :data:`ROW_FIELDS`-shaped row tagged ``source="nws_cli"`` so the
        provenance of every stored value names the authority that produced it,
        or ``None`` when NWS has no matching product either.

        Prefers the next-morning ``YESTERDAY`` issuance over the same-evening
        ``TODAY`` preliminary, since only the former is final.
        """
        target = normalize_date(date)
        spec = get_station(station, allow_unverified=True)
        list_url = NWS_PRODUCT_LIST_URL.format(loc=spec.cli_location)

        try:
            resp = self._get(list_url)
        except IEMCLIError as exc:
            logger.error(
                "NWS CLI fallback unavailable for %s %s: %s", station, target, exc
            )
            return None
        if getattr(resp, "status_code", None) != 200:
            logger.error(
                "NWS CLI product list for %s returned HTTP %s",
                spec.cli_location,
                getattr(resp, "status_code", None),
            )
            return None
        try:
            graph = resp.json().get("@graph") or []
        except Exception as exc:
            logger.error(
                "NWS CLI product list for %s was not JSON: %s", spec.cli_location, exc
            )
            return None

        candidates: List[Dict[str, Any]] = []
        for entry in graph[:max_products]:
            product_url = entry.get("@id")
            if not product_url:
                continue
            try:
                p = self._get(product_url)
            except IEMCLIError as exc:
                logger.error("NWS CLI product fetch failed (%s): %s", product_url, exc)
                continue
            if getattr(p, "status_code", None) != 200:
                continue
            try:
                text = p.json().get("productText") or ""
            except Exception:
                continue
            parsed = parse_cli_text(text)
            if not parsed or parsed["date"] != target:
                continue
            candidates.append(
                {
                    "parsed": parsed,
                    "product": entry.get("id") or entry.get("productCode"),
                    "source_url": product_url,
                }
            )
            if parsed.get("section") == "YESTERDAY":
                break  # the final issuance; stop paying for more requests

        if not candidates:
            logger.error(
                "NWS CLI fallback found no product for %s on %s "
                "(location %s); ground truth remains missing",
                station,
                target,
                spec.cli_location,
            )
            return None

        chosen = next(
            (c for c in candidates if c["parsed"].get("section") == "YESTERDAY"),
            candidates[0],
        )
        high = chosen["parsed"].get("high")
        if high is None:
            logger.error(
                "NWS CLI product %s for %s on %s has no MAXIMUM line; "
                "reporting missing truth, not a default",
                chosen["source_url"],
                station,
                target,
            )
            return None

        return {
            "station": str(station).upper(),
            "date": target,
            "high": int(high),
            "low": None,
            "high_time": None,
            "source": SOURCE_NWS,
            "source_url": chosen["source_url"],
            "product": chosen["product"],
            "fetched_at": _utc_now_iso(),
        }

    # -- combined ---------------------------------------------------------
    def fetch_truth(
        self, station: str, date: Any, *, allow_nws_fallback: bool = True, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """IEM first, NWS CLI second: one row of settlement truth, or ``None``.

        The returned row's ``source`` field always names which authority the
        value came from (:data:`SOURCE_IEM` or :data:`SOURCE_NWS`).
        """
        target = normalize_date(date)
        try:
            row = self.fetch_row(station, target, **kwargs)
        except IEMCLIError as exc:
            logger.error("IEM lookup failed for %s %s: %s", station, target, exc)
            row = None
        if row is not None and row.get("high") is not None:
            return row

        if not allow_nws_fallback:
            return None
        logger.info(
            "IEM has no daily high for %s on %s; trying the NWS CLI product fallback",
            station,
            target,
        )
        return self.fetch_daily_high_nws(station, target)


__all__ = [
    "IEMCLIProvider",
    "IEMCLIError",
    "StationSpec",
    "STATIONS",
    "CORE_STATIONS",
    "EXPANSION_STATIONS",
    "SERIES_TO_STATION",
    "ROW_FIELDS",
    "SOURCE_IEM",
    "SOURCE_NWS",
    "IEM_CLI_URL",
    "NWS_PRODUCT_LIST_URL",
    "get_station",
    "station_for_series",
    "normalize_date",
    "normalize_row",
    "parse_cli_text",
    "date_range",
]
