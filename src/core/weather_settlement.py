"""Settlement-truth resolver for Kalshi daily-high weather markets.

PRD FR-1.2 / FR-1.5. :mod:`src.core.bracket_payoff` answers *"given a daily
high, does this bracket pay?"*. This module answers the question that comes
first: *"which daily high settles this ticker, and is it published yet?"*

Three questions, three functions:

``settlement_station_for(symbol)``
    Which station does Kalshi settle this series on (``KXHIGHNY`` -> ``KNYC``)?
    The mapping is **not** duplicated here -- it is read from the single
    authoritative city registry in :mod:`src.bots.weather_bot`
    (``WEATHER_CITIES`` / ``CITY_CONFIG``). The import is deferred to call time
    because ``weather_bot`` reaches ``src.core.matching_engine`` through
    ``src.bots.mixins`` -> ``src.core.risk_manager``, and ``matching_engine``
    imports *this* module at module level; a top-level import here would close
    that loop. Deferring it keeps one definition of the registry and no cycle.

``settlement_date_for(symbol)``
    Which calendar day does the event label name? The ``26JUL25`` segment is a
    **local** calendar date at the settlement station -- the day the NWS
    Climatological Report covers -- never a UTC date. Converting it in UTC is
    the class of bug that produced the review's "UTC hour-gate" defect, so the
    tz-aware interpretation lives in this one function (and in
    :func:`hours_since_settlement_day_close`, which uses the same tz).

``resolve_settlement_high(symbol)``
    What was the high? Settlement cache first (written by the FR-1.3 recorder,
    ``scripts/reconcile_weather.py``), then the IEM CLI archive, then
    ``None``.

``None`` means *truth is not published yet*, and it is never anything else.
This module will not fall back to the running observed maximum, to a METAR
reading, or to a neighbouring station: the CLI product is the settlement
authority and a substitute for it is a systematically-wrong default, which the
project's ``abort-on-missing-critical-input`` rule forbids. A caller that gets
``None`` at expiry must leave the position open and retry on a later sweep --
the day's CLI product is typically issued in the small hours of the following
morning, local time.

Nothing here inspects a ticker's suffix letter for contract direction
(PRD FR-1.1); the only thing parsed out of the ticker is the series prefix and
the event *date label*, both of which are identifiers, not semantics.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date as _date
from datetime import datetime, time as _time, timedelta
from typing import Any, Dict, Optional, Tuple

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core.bracket_payoff import is_weather_symbol  # noqa: F401  (re-export)
from src.data.iem_cli_provider import WEATHER_TRUTH_DIR
from src.utils.logger import logger

__all__ = [
    "SETTLEMENT_CACHE_PATH",
    "SETTLEMENT_TRUTH_GRACE_HOURS",
    "settlement_station_for",
    "settlement_date_for",
    "settlement_timezone_for",
    "settlement_close_for",
    "city_key_for_station",
    "city_keys",
    "resolve_settlement_high",
    "hours_since_settlement_day_close",
    "reset_caches",
]

#: Written by the FR-1.3 settlement recorder (``scripts/reconcile_weather.py``).
#: Shape: ``{"truth": {"<STATION>|<YYYY-MM-DD>": {"high": 83, ...}}, "markets": {...}}``.
SETTLEMENT_CACHE_PATH = os.path.join(WEATHER_TRUTH_DIR, "settlement_cache.json")

#: How long after a settlement day closes (local midnight) we still treat a
#: missing CLI high as ordinary latency rather than a fault. The final CLI
#: product for day D is issued the following morning local time -- observed
#: issuances for the tracked stations land within ~2 hours of local midnight
#: (e.g. product ``202607250626-KOKX-CDUS41-CLINYC`` for 2026-07-24). 36 hours
#: is therefore a full extra day of slack before anyone is paged.
SETTLEMENT_TRUTH_GRACE_HOURS = 36.0

#: Don't re-hit the archive for the same station/date more often than this after
#: a miss. ``update_market`` runs on the tick thread; an unpublished CLI high
#: must not turn into a network call every tick.
_MISS_RETRY_SECONDS = 900.0

# ``26JUL25`` -> (26, JUL, 25). Anchored so a bracket segment like ``B86.5``
# can never match.
_EVENT_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_lock = threading.Lock()
_cities_cache: Optional[Tuple[Any, ...]] = None
_cache_blob: Optional[Dict[str, Any]] = None
_cache_stamp: Optional[Tuple[float, int]] = None  # (mtime, size)
_provider = None
_miss_log: Dict[str, float] = {}


def reset_caches() -> None:
    """Drop every memoized handle. Tests use this; production never needs it."""
    global _cities_cache, _cache_blob, _cache_stamp, _provider
    with _lock:
        _cities_cache = None
        _cache_blob = None
        _cache_stamp = None
        _provider = None
        _miss_log.clear()


# ---------------------------------------------------------------------------
# City registry (single definition, imported -- never duplicated)
# ---------------------------------------------------------------------------
def _cities() -> Tuple[Any, ...]:
    """The authoritative city registry from :mod:`src.bots.weather_bot`.

    Deferred import; see the module docstring for why. Returns an empty tuple
    (logged at ERROR) if the registry cannot be reached, which makes every
    lookup below return ``None`` -- missing truth, not a guessed station.
    """
    global _cities_cache
    if _cities_cache is not None:
        return _cities_cache
    try:
        from src.bots.weather_bot import WEATHER_CITIES  # deferred: see docstring

        cities = tuple(WEATHER_CITIES)
    except Exception as exc:  # pragma: no cover - only on a broken install
        logger.error(
            "[WxSettle] city registry unavailable (%s); weather settlement "
            "cannot resolve a station and will report missing truth",
            exc,
        )
        cities = ()
    with _lock:
        _cities_cache = cities
    return cities


def _city_for(symbol: str):
    """The registry entry whose ``kalshi_series`` the ticker belongs to."""
    if not symbol:
        return None
    upper = str(symbol).upper()
    # Longest series prefix wins, so a future ``KXHIGHNYC`` could never be
    # swallowed by ``KXHIGHNY``.
    for city in sorted(_cities(), key=lambda c: len(c.kalshi_series), reverse=True):
        if upper.startswith(city.kalshi_series.upper()):
            return city
    return None


def settlement_station_for(symbol: str) -> Optional[str]:
    """``KXHIGHNY-26JUL25-B86.5`` -> ``"KNYC"``. ``None`` when unmapped."""
    city = _city_for(symbol)
    if city is None:
        if is_weather_symbol(symbol):
            logger.error(
                "[WxSettle] no settlement station registered for %s; "
                "cannot resolve its daily high",
                symbol,
            )
        return None
    return city.settlement_station


def city_key_for_station(station: str) -> Optional[str]:
    """``KNYC`` -> ``"NY"``: the city fragment that appears in the Kalshi ticker.

    Derived from the same registry as everything else, so adding a city (or
    correcting a settlement station) is a one-place change. The non-settlement
    airports KJFK/KORD are deliberately absent: after FR-1.4 nothing in the
    process emits them, and mapping them would resurrect the defect where a
    JFK observation valued a market that settles on Central Park.
    """
    if not station:
        return None
    key = str(station).upper().strip()
    for city in _cities():
        if city.settlement_station.upper() == key:
            return city.key
    return None


def city_keys() -> Tuple[str, ...]:
    """Every tracked city fragment, e.g. ``("NY", "CHI", "LAX", "MIA")``."""
    return tuple(city.key for city in _cities())


def settlement_timezone_for(symbol: str) -> Optional[str]:
    """IANA timezone of the settlement station, e.g. ``America/New_York``."""
    city = _city_for(symbol)
    return city.timezone if city is not None else None


def settlement_date_for(symbol: str) -> Optional[_date]:
    """The settlement station's **local** calendar day named by the ticker.

    ``KXHIGHNY-26JUL25-B86.5`` -> ``date(2026, 7, 25)``.

    This is a date *label*, not a direction inference: it identifies which day's
    Climatological Report settles the market. It is deliberately interpreted in
    the city's local timezone -- UTC would roll the label a day early or late
    for evening/early-morning ticks (the review's UTC hour-gate defect), which
    would settle a position against the wrong day's high.
    """
    if not symbol:
        return None
    tz_name = settlement_timezone_for(symbol)
    if tz_name is None:
        return None
    for segment in str(symbol).upper().split("-"):
        m = _EVENT_DATE_RE.match(segment)
        if not m:
            continue
        month = _MONTHS.get(m.group(2))
        if month is None:
            continue
        try:
            local_day = _date(2000 + int(m.group(1)), month, int(m.group(3)))
        except ValueError:
            logger.error(
                "[WxSettle] %s: event segment %s is not a real calendar date",
                symbol,
                segment,
            )
            return None
        # Prove the tz is loadable here, where the label is interpreted, so a
        # missing tzdata surfaces as missing truth rather than as a silently
        # UTC-flavoured date downstream.
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            logger.error(
                "[WxSettle] %s: timezone %s unavailable (%s); refusing to "
                "interpret the event date in UTC",
                symbol,
                tz_name,
                exc,
            )
            return None
        return local_day
    logger.error("[WxSettle] %s: no event-date segment (expected e.g. 26JUL25)", symbol)
    return None


def settlement_close_for(symbol: str) -> Optional[datetime]:
    """The tz-aware instant at which ``symbol``'s settlement day ends.

    ``KXHIGHNY-26SEP01-T83`` -> ``2026-09-02 00:00 America/New_York``: local
    midnight *following* the event date, in the settlement station's own
    timezone (so ~04:00Z for NY/MIA, 05:00Z for CHI, 07:00Z for LAX).

    This is the one definition of "when does a weather contract expire" in
    the runtime (PRD FR-F0.1). It is what
    :func:`src.core.bracket_payoff.attach_spec_to_signals` stamps onto every
    outgoing weather ``TradeSignal``, what ``SimulatedExchange.open_position``
    backfills for a weather position opened without one, and what
    ``SimulatedExchange._load_state`` backfills onto a restored position that
    predates the stamp. Before it existed no weather signal carried an
    expiration at all, so the exchange's EXPIRATION CHECK never fired and the
    FR-1.2 settlement branch was unreachable from the live path -- positions
    simply never left the book.

    Deterministic and offline: derived from the ticker's event-date label and
    the city registry only, never from a Kalshi ``close_time`` (which is the
    market's *trading* close, a different instant). ``None`` when the symbol is
    not a registered weather series, carries no event-date segment, or its
    timezone cannot be loaded -- the same cases in which
    :func:`settlement_date_for` reports missing truth.
    """
    tz_name = settlement_timezone_for(symbol)
    day = settlement_date_for(symbol)
    if tz_name is None or day is None:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    # Local midnight that ends the settlement day.
    return datetime.combine(day + timedelta(days=1), _time(0, 0), tzinfo=tz)


def hours_since_settlement_day_close(
    symbol: str, now: Optional[datetime] = None
) -> Optional[float]:
    """Hours elapsed since the settlement day ended, in the station's timezone.

    Negative while the day is still running. Used to decide whether missing
    truth is ordinary latency (INFO) or a fault worth paging about
    (see :data:`SETTLEMENT_TRUTH_GRACE_HOURS`).
    """
    day_close = settlement_close_for(symbol)
    if day_close is None:
        return None
    tz = day_close.tzinfo
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return (current - day_close).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Truth lookup
# ---------------------------------------------------------------------------
def _load_cache(path: str) -> Dict[str, Any]:
    """Settlement cache, re-read whenever the file on disk changes.

    The FR-1.3 recorder rewrites this file daily from a separate process, so a
    long-lived runtime must not hold a stale copy.
    """
    global _cache_blob, _cache_stamp
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        with _lock:
            _cache_blob, _cache_stamp = {}, None
        return {}
    if _cache_blob is not None and _cache_stamp == stamp:
        return _cache_blob
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if not isinstance(blob, dict):
            raise ValueError(
                f"settlement cache root is {type(blob).__name__}, want dict"
            )
    except Exception as exc:
        logger.error("[WxSettle] settlement cache %s unreadable (%s)", path, exc)
        blob = {}
    with _lock:
        _cache_blob, _cache_stamp = blob, stamp
    return blob


def _high_from_cache(station: str, day: _date, path: str) -> Optional[float]:
    truth = _load_cache(path).get("truth")
    if not isinstance(truth, dict):
        return None
    row = truth.get(f"{station}|{day.isoformat()}")
    if not isinstance(row, dict):
        return None
    high = row.get("high")
    if high is None:
        return None
    try:
        return float(high)
    except (TypeError, ValueError):
        logger.error(
            "[WxSettle] settlement cache holds a non-numeric high %r for %s %s",
            high,
            station,
            day,
        )
        return None


def _iem_provider():
    """Shared archive client, tuned for use from the runtime tick thread.

    Short timeout and no inter-request pause: this can be reached from
    ``SimulatedExchange.update_market`` during an expiry sweep, and a stalled
    HTTP call there stalls the whole tick loop. Combined with
    :data:`_MISS_RETRY_SECONDS` and the provider's own on-disk year cache, the
    worst case is one bounded request per station-day per 15 minutes.

    ``pending_ttl=0`` is deliberate and is the throttling contract stated
    above: *this* module owns the retry interval (:data:`_MISS_RETRY_SECONDS`),
    so the provider must not add a second, independent one on top. Anything
    else re-creates the defect this handle is famous for -- a process-lifetime
    singleton whose first "no product yet" answer became permanent, so a
    position that expired before the ~06:26-local CLI issuance could not settle
    until the process restarted.
    """
    global _provider
    if _provider is None:
        from src.data.iem_cli_provider import IEMCLIProvider

        with _lock:
            if _provider is None:
                _provider = IEMCLIProvider(
                    timeout=10, request_pause=0.0, pending_ttl=0.0
                )
    return _provider


def resolve_settlement_high(
    symbol: str,
    *,
    provider: Any = None,
    cache_path: Optional[str] = None,
    allow_network: bool = True,
) -> Optional[float]:
    """The CLI daily high that settles ``symbol``, or ``None`` if unpublished.

    Order: settlement cache (FR-1.3 recorder), then the IEM CLI archive. A
    ``None`` return is *missing truth* and nothing else -- never the running
    observed maximum, never a nearby station, never a forecast. Callers at
    expiry must hold the position and retry.
    """
    station = settlement_station_for(symbol)
    day = settlement_date_for(symbol)
    if station is None or day is None:
        return None

    path = cache_path or SETTLEMENT_CACHE_PATH
    high = _high_from_cache(station, day, path)
    if high is not None:
        return high

    if not allow_network and provider is None:
        return None

    key = f"{station}|{day.isoformat()}"
    now = time.monotonic()
    last_miss = _miss_log.get(key)
    if (
        provider is None
        and last_miss is not None
        and (now - last_miss) < _MISS_RETRY_SECONDS
    ):
        # Recently confirmed unpublished; don't re-hit the archive this tick.
        return None

    try:
        src = provider if provider is not None else _iem_provider()
        value = src.fetch_daily_high(station, day.isoformat())
    except Exception as exc:
        logger.error(
            "[WxSettle] CLI archive lookup failed for %s %s (%s); reporting "
            "missing truth, not a default",
            station,
            day,
            exc,
        )
        value = None

    if value is None:
        _miss_log[key] = now
        return None
    _miss_log.pop(key, None)
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.error(
            "[WxSettle] CLI archive returned a non-numeric high %r for %s %s",
            value,
            station,
            day,
        )
        return None
