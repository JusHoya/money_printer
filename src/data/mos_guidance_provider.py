"""Archived model guidance (MOS/NBM) from the Iowa Environmental Mesonet.

PRD FR-1.6 ("archived model guidance (MOS/NBM via IEM) backfilled where
available to seed forecast-error calibration") and FR-2.2. This module is the
*forecast* side of the calibration pair; :mod:`src.data.iem_cli_provider` is the
*truth* side.

It emits :class:`GuidanceForecast` records in the project's normalized,
**source-agnostic** forecast schema (:data:`FORECAST_FIELDS`), so the same
calibrator that consumes GFS-MOS backfill also consumes a GEFS ensemble or an
NWS point forecast without a schema change.

NAMING NOTE
-----------
The file is called ``mos_guidance_provider`` and not ``nbm_provider`` because
the NBM models this endpoint serves (``NBS``/``NBE``) **do not carry a
max/min field** -- see the contract table below. The working daily-high source
here is ``MEX`` (GFS extended MOS). Naming it after NBM would have been a lie
about what it returns.

VERIFIED UPSTREAM CONTRACT (probed live 2026-07-26)
---------------------------------------------------
Endpoint: ``GET https://mesonet.agron.iastate.edu/api/1/mos.json``

=========================================  =======  ==================================================
Query                                      Status   Result
=========================================  =======  ==================================================
``station=KNYC&model=MEX&runtime=2026-     200      15 rows, ftime +24h..+192h, **``n_x`` populated on
06-01T00:00:00Z``                                   all 15**
``...&model=NBS&...``                      200      23 rows, 3-hourly +6h..+72h, **``n_x`` null on all
                                                    23** -- NBM carries no max/min here
``...&model=NBE&...``                      200      20 rows, +24h..+264h, **``n_x`` null on all 20**
``...&model=GFS``/``NAM``                  200      21 rows, ``n_x`` on 5 (00Z/12Z ftimes only)
``...&model=LAV``                          200      38 rows, ``n_x`` null on all
``...&model=ECM``                          404      no ECMWF MOS archived for these ICAOs
``...&model=MAV``                          422      **not a valid model** despite existing as a real
                                                    MOS product (see whitelist below)
``...&model=ZZZ``                          422      reveals the whitelist:
                                                    ``^(AVN|GFS|ETA|NAM|NBS|NBE|ECM|LAV|MEX)$``
``station=ZZZZ&model=MEX&runtime=...``     404      unknown station is an honest error
``model=MEX&runtime=1990-06-01T00:00:00Z`` 404      pre-archive runtime is an honest error
``model=MEX&runtime=2030-06-01T00:00:00Z`` 404      future runtime is an honest error
``model=MEX&runtime=2026-06-01T06:00:00Z`` 404      MEX runs at **00Z and 12Z only**; 06Z/18Z are 404
``station=KNYC&model=MEX`` (no runtime)    **200**  **silently returns the LATEST run** (probed:
                                                    ``2026-07-26 12:00``)
``...&sts=...&ets=...`` (no runtime)       **200**  ``sts``/``ets`` are **ignored**; latest run again
``station=KNYC&station=KMDW&...``          200      repeated ``station`` params batch -- 30 rows, both
                                                    stations. 4 stations in one call = 60 rows.
``station=KNYC,KMDW`` (comma list)         422      comma lists are rejected; repeat the param instead
``station=knyc`` (lowercase)               422      station must be uppercase
``runtime=2026-06-01`` (date only)         200      accepted, treated as 00Z
``model=`` omitted                         422      required
``station=`` omitted                       422      required
``runtime=2024-01-01T00:00:00Z``           200      archive reaches back at least to 2024-01-01
``runtime=2020-01-01T00:00:00Z``           404      ...and not to 2020
=========================================  =======  ==================================================

Traps guarded in code, each one a repeat of a class that has already cost this
project a review cycle:

1. **Omitting ``runtime`` returns the latest run with HTTP 200.** This is the
   same defect as the CLI archive's ignored ``date`` parameter (which returned
   five-year-old data at HTTP 200). A backfill loop that dropped the parameter
   would stamp *today's* forecast onto every historical target date and produce
   a spectacular, entirely fictional calibration. ``runtime`` is therefore
   always sent, and :meth:`MOSGuidanceProvider.fetch_run` re-validates that
   every returned row's ``runtime`` equals the requested one.
2. **``sts``/``ets`` look like a range query and are silently ignored.** There
   is no bulk-range route; one request per run is the only correct access
   pattern. The provider never accepts a range parameter it cannot honour.
3. **Station and model are re-validated per row.** A batched request asks for
   four stations at once; a cross-station or cross-model substitution would be
   invisible in aggregate, so any row outside the requested sets is a hard
   error rather than a filtered-out oddity.
4. **HTTP 404 means "this run is not in the archive"**, which for a backfill is
   a legitimate gap, not a failure. It is returned as an empty list and counted,
   never as a substituted neighbouring run.

The raw-text route was evaluated and **rejected for backfill** (probed
2026-07-26). ``GET /cgi-bin/afos/retrieve.py?pil=NBSNYC&limit=1`` does return
NBM guidance text carrying both ``TXN`` (max/min) and ``XND`` (its standard
deviation, i.e. a real spread) -- but it serves *only the latest* product:
``&date=2026-06-01`` is silently ignored (HTTP 200, today's bulletin),
``&sdate=/&edate=`` return ``ERROR: Could not Find:`` inside an HTTP 200 body,
and ``/api/1/nws/afos/list.json?pil=NBSNYC&date=2026-06-01`` returns zero rows.
So NBM's spread field is reachable live but **not historically**, and the
backfill's ``spread_f`` column is therefore blank for this source -- blank
meaning "this source publishes no spread", which is not the same fact as a
spread of 0.0 and is never encoded as one.

WHAT ``n_x`` MEANS -- verified empirically, not assumed (2026-07-26)
--------------------------------------------------------------------
``n_x`` is the classic MOS **N/X** field: daytime maximum *or* nighttime
minimum, depending on which 12-hour period the forecast hour closes. Nothing in
the JSON payload says which. It was therefore resolved by measurement: 840 rows
(14 x 00Z MEX runs, 4 stations, one summer week and one winter week) were
bucketed by the **local** hour of ``ftime`` and differenced against the CLI
truth archive::

    bucket (local hour of ftime)   mean(n_x - CLI_high)   mean(n_x - CLI_low)
    KNYC  19-20 (00Z ftime)              -0.70 / +0.62         +11.3 / +15.9
    KNYC  07-08 (12Z ftime)             -10.2  / -14.5          +1.7 /  +0.6
    KMDW  18-19 (00Z ftime)              -0.48 / +2.80         +14.6 / +19.5
    KMDW  06-07 (12Z ftime)             -11.0  / -14.0          +4.1 /  +2.4
    KLAX  16-17 (00Z ftime)              -1.68 / +0.00         +22.8 /  +9.7
    KLAX  04-05 (12Z ftime)             -22.9  /  -9.0          +0.9 /  +0.6
    KMIA  19-20 (00Z ftime)              -0.68 / -1.57         +16.3 / +13.2
    KMIA  07-08 (12Z ftime)             -16.2  / -14.0          +1.1 /  +0.9

The evening-``ftime`` rows track the CLI **high** and the morning-``ftime`` rows
track the CLI **low**, in every city and both seasons. Hence:

* an ``n_x`` whose ``ftime`` lands in the station's local afternoon/evening
  (:data:`MAX_LOCAL_HOUR_RANGE`) is the **daytime maximum** for the local
  calendar date of that ``ftime``;
* an ``n_x`` whose ``ftime`` lands in the local morning
  (:data:`MIN_LOCAL_HOUR_RANGE`) is the overnight **minimum** and is discarded
  here;
* any other local hour is *not* classified -- it is skipped and logged, because
  guessing is how a sign error gets into a calibration file.

The classification is done on the **local** hour, never the UTC hour. A UTC
hour gate is the exact bug the weather stack was rebuilt to remove, and it would
break here the moment a station's offset changed with DST.

The raw MEX bulletin corroborates the mapping independently: for the
2026-07-26 12Z run, ``MEXNYC`` prints ``FHR 24 36| 48 60|`` under the day
labels ``MON 27| TUE 28|`` with ``N/X 69 79| 70 79|`` -- i.e. FHR 24 (12Z Mon)
and FHR 36 (00Z Tue) are both filed under **Monday the 27th**, which is exactly
"the local calendar date of ``ftime``" for the 00Z-ftime maximum.

AVAILABILITY (matters for what "day-of" means)
----------------------------------------------
The MEX bulletin header carries its WMO transmission stamp: the 2026-07-26 12Z
run was transmitted as ``FEUS21 KWNO 261200``. GFS extended-MOS guidance is
issued shortly after its nominal run time, so a ``runtime`` of ``00Z`` on day D
is on the wire in the small hours of day D local for the eastern cities and the
*evening of D-1* for KLAX -- in all four cities, before that day's maximum
occurs. That is what makes the shortest lead bucket an honest "day-of"
forecast. The precise transmission delay per run was **not** measured here; the
archive stores the model runtime, not the receipt time, so
:attr:`GuidanceForecast.init_time_utc` is the model runtime and lead times are
computed from it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:  # pragma: no cover - requests is a hard project dependency
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

from src.data.iem_cli_provider import STATIONS, StationSpec, get_station

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / endpoints
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
FORECAST_ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "forecast_archive")
CACHE_SUBDIR = "cache"

IEM_MOS_URL = "https://mesonet.agron.iastate.edu/api/1/mos.json"

DEFAULT_USER_AGENT = (
    "money-printer/forecast-calibration (github.com/JusHoya/money_printer; "
    "contact via repo owner)"
)

#: Exactly the whitelist the endpoint reveals on a bad ``model`` (HTTP 422).
#: Kept verbatim so a typo fails locally instead of costing a round trip.
MODEL_WHITELIST = frozenset(
    {"AVN", "GFS", "ETA", "NAM", "NBS", "NBE", "ECM", "LAV", "MEX"}
)

#: Models this endpoint actually populates ``n_x`` for, probed 2026-07-26.
#: ``NBS``/``NBE``/``LAV`` return ``n_x=null`` on every row and are useless as a
#: daily-high source here regardless of their forecast skill.
MODELS_WITH_NX = frozenset({"MEX", "GFS", "NAM", "ETA"})

#: Nominal run hours (UTC) per model. ``MEX`` probed: 00Z/12Z exist, 06Z/18Z 404.
MODEL_RUN_HOURS: Dict[str, Tuple[int, ...]] = {
    "MEX": (0, 12),
    "GFS": (0, 6, 12, 18),
    "NAM": (0, 6, 12, 18),
    "NBS": (0, 6, 12, 18),
    "NBE": (0, 6, 12, 18),
}

#: Local-hour windows used to decide whether an ``n_x`` is a max or a min.
#: Half-open ``[lo, hi)``. See "WHAT ``n_x`` MEANS" in the module docstring.
MAX_LOCAL_HOUR_RANGE = (14, 24)
MIN_LOCAL_HOUR_RANGE = (0, 12)

#: Source label written into every emitted row. One label per (endpoint, model,
#: field) triple, so a calibration file can never be ambiguous about what it
#: measured.
SOURCE_GFS_MEX = "gfs_mex"

#: The normalized, source-agnostic forecast-series schema (PRD FR-2.2). Any
#: future forecast source -- GEFS ensemble, NWS point forecast -- writes these
#: same columns and the calibrator needs no change.
FORECAST_FIELDS: Tuple[str, ...] = (
    "city",
    "station",
    "target_date",
    "init_time_utc",
    "lead_hours",
    "source",
    "forecast_high_f",
    "spread_f",
    "provenance",
)


class MOSGuidanceError(RuntimeError):
    """The guidance archive could not be trusted.

    Always a hard abort for the caller. There is no safe default forecast: a
    substituted or mis-dated value does not degrade a calibration gracefully,
    it inverts it.
    """


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GuidanceForecast:
    """One (source, run, city, target local date) daily-high forecast.

    :param target_date: the **local** calendar date whose daily maximum is
        forecast, ``YYYY-MM-DD``.
    :param init_time_utc: the model runtime, ``YYYY-MM-DDTHH:MM:SSZ``.
    :param lead_hours: whole hours from ``init_time_utc`` to the **start of the
        target local day**. Negative means the run initialised after the local
        day had already begun.
    :param spread_f: the source's own uncertainty, or ``None`` when the source
        publishes none. ``None`` is written to CSV as an empty field and is
        never coerced to ``0.0``: "no spread published" and "a spread of zero"
        are different facts and conflating them silently overstates confidence.
    """

    city: str
    station: str
    target_date: str
    init_time_utc: str
    lead_hours: int
    source: str
    forecast_high_f: float
    spread_f: Optional[float]
    provenance: str

    def as_row(self) -> Dict[str, Any]:
        """Dict in :data:`FORECAST_FIELDS` order, ready for ``csv.DictWriter``."""
        d = asdict(self)
        return {k: d[k] for k in FORECAST_FIELDS}

    @property
    def key(self) -> Tuple[str, str, str, str]:
        """Identity of a forecast: one source+run+city forecasts one local day once."""
        return (self.source, self.city, self.target_date, self.init_time_utc)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_MISSING_TOKENS = {"", "M", "MM", "NA", "N/A", "NONE", "NULL", "-99", "-999"}


def _as_float(value: Any) -> Optional[float]:
    """Parse a guidance temperature, treating the archive's missing tokens as ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.upper() in _MISSING_TOKENS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_runtime(value: Any) -> datetime:
    """Coerce a runtime to a tz-aware UTC ``datetime``.

    Accepts ``datetime`` (naive is assumed UTC), ``date``, and the archive's own
    ``"YYYY-MM-DD HH:MM"`` as well as ISO ``"...T00:00:00Z"``.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        raise MOSGuidanceError("empty runtime")
    normalized = text.replace("Z", "+00:00").replace(" ", "T")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MOSGuidanceError(f"unparseable runtime {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_runtime(dt: datetime) -> str:
    """``datetime`` -> the exact string the endpoint and our CSVs both use."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ftime(value: Any) -> datetime:
    """Archive ``ftime`` (``"YYYY-MM-DD HH:MM"``, UTC) -> tz-aware UTC datetime."""
    return parse_runtime(value)


def local_day_start_utc(target_date: str, tz_name: str) -> datetime:
    """UTC instant at which ``target_date`` begins in ``tz_name``.

    Uses :mod:`zoneinfo`, so a DST transition is handled by the tz database
    rather than by a fixed offset -- the failure mode that produced the UTC
    hour-gate bug in the previous weather stack.
    """
    y, m, d = (int(p) for p in target_date.split("-"))
    return datetime(y, m, d, 0, 0, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


def lead_hours_for(init_time_utc: datetime, target_date: str, tz_name: str) -> int:
    """Whole hours from a model runtime to the start of the target local day."""
    delta = local_day_start_utc(target_date, tz_name) - init_time_utc.astimezone(
        timezone.utc
    )
    return int(round(delta.total_seconds() / 3600.0))


def _in_range(hour: int, rng: Tuple[int, int]) -> bool:
    return rng[0] <= hour < rng[1]


def run_times(start_date: Any, end_date: Any, hours: Sequence[int]) -> List[datetime]:
    """Every model runtime in ``[start_date, end_date]`` at the given UTC hours."""
    start = parse_runtime(start_date).date()
    end = parse_runtime(end_date).date()
    if end < start:
        raise MOSGuidanceError(f"end_date {end} precedes start_date {start}")
    out: List[datetime] = []
    cur = start
    while cur <= end:
        for h in sorted(hours):
            out.append(
                datetime(cur.year, cur.month, cur.day, int(h), tzinfo=timezone.utc)
            )
        cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class MOSGuidanceProvider:
    """Archived MOS/NBM guidance, normalized to :class:`GuidanceForecast`.

    Deliberately not a :class:`src.core.interfaces.DataProvider` subclass, for
    the same reason :class:`~src.data.iem_cli_provider.IEMCLIProvider` is not:
    ``fetch_latest`` must return :class:`MarketData` (price/bid/ask/volume),
    which has no honest meaning for a forecast temperature.

    Responses are cached per ``(model, runtime)`` under
    ``data/forecast_archive/cache/``. A run in the archive is immutable, so a
    cached run never expires -- but an **empty or 404 run is never cached**, for
    the same reason the CLI provider refuses to cache an empty year: one
    transient upstream fault would otherwise freeze that run as a permanent gap.

    :param offline: refuse all network calls and serve only from cache. Used by
        the deterministic rebuild path so a calibration can be regenerated
        without the archive being reachable.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        *,
        session: Any = None,
        user_agent: str = DEFAULT_USER_AGENT,
        request_pause: float = 0.4,
        timeout: int = 60,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir or os.path.join(FORECAST_ARCHIVE_DIR, CACHE_SUBDIR)
        self.user_agent = user_agent
        self.request_pause = float(request_pause)
        self.timeout = int(timeout)
        self.offline = bool(offline)
        self._session = session
        #: Counters a backfill can report instead of guessing at coverage.
        self.stats: Dict[str, int] = {
            "runs_requested": 0,
            "runs_from_cache": 0,
            "runs_fetched": 0,
            "runs_missing_404": 0,
            "runs_failed": 0,
            "rows_seen": 0,
            "rows_nx_null": 0,
            "rows_min_discarded": 0,
            "rows_unclassified_hour": 0,
            "forecasts_emitted": 0,
        }

    # -- plumbing ---------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            if requests is None:  # pragma: no cover
                raise MOSGuidanceError(
                    "requests is unavailable; cannot reach the guidance archive"
                )
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        return self._session

    def connect(self) -> bool:
        """Verify the archive answers for a known-good station/model/run."""
        os.makedirs(self.cache_dir, exist_ok=True)
        if self.offline:
            logger.info(
                "MOSGuidanceProvider: offline mode, cache at %s", self.cache_dir
            )
            return True
        probe_run = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=2)
        try:
            rows = self.fetch_run(["KNYC"], "MEX", probe_run)
        except MOSGuidanceError as exc:
            logger.error("MOSGuidanceProvider.connect failed: %s", exc)
            return False
        if not rows:
            logger.error(
                "MOSGuidanceProvider.connect: archive has no MEX run at %s",
                format_runtime(probe_run),
            )
            return False
        return True

    # -- cache ------------------------------------------------------------
    def _cache_path(
        self, model: str, runtime: datetime, stations: Sequence[str]
    ) -> str:
        stamp = runtime.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")
        tag = "-".join(sorted(s.upper() for s in stations))
        return os.path.join(self.cache_dir, f"mos_{model.upper()}_{tag}_{stamp}.json")

    def _load_cache(self, path: str) -> Optional[List[Dict[str, Any]]]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.error("guidance cache %s unreadable (%s); ignoring it", path, exc)
            return None
        rows = blob.get("rows") if isinstance(blob, dict) else None
        if not isinstance(rows, list) or not rows:
            # An empty cached run is never authoritative -- see the class
            # docstring. Self-heal instead of freezing the gap.
            return None
        return rows

    def _save_cache(self, path: str, rows: List[Dict[str, Any]], meta: Dict) -> None:
        if not rows:
            return  # never persist an empty run
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"_meta": meta, "rows": rows}, fh, sort_keys=True)
            os.replace(tmp, path)
        except Exception as exc:
            logger.error("could not write guidance cache %s: %s", path, exc)

    # -- fetch ------------------------------------------------------------
    def fetch_run(
        self,
        stations: Sequence[str],
        model: str,
        runtime: Any,
        *,
        force_refresh: bool = False,
        allow_unverified_station: bool = True,
    ) -> List[Dict[str, Any]]:
        """Raw rows for one ``(stations, model, runtime)``; ``[]`` if not archived.

        ``runtime`` is **always** sent. Omitting it is an HTTP 200 that returns
        the newest run in the archive (documented trap 1), so this method has no
        code path that can issue a runtime-less request.

        Every returned row is re-validated against the request: a row whose
        ``runtime``, ``model``, or ``station`` does not match what was asked for
        raises :class:`MOSGuidanceError` rather than being quietly dropped.
        """
        model = str(model).upper().strip()
        if model not in MODEL_WHITELIST:
            raise MOSGuidanceError(
                f"model {model!r} is not in the endpoint's whitelist "
                f"{sorted(MODEL_WHITELIST)} (it answers HTTP 422)"
            )
        wanted: List[str] = []
        for s in stations:
            key = str(s).upper().strip()
            if not (3 <= len(key) <= 6):
                raise MOSGuidanceError(
                    f"station {s!r} is invalid: the endpoint requires 3-6 uppercase "
                    f"characters and rejects comma lists (HTTP 422)"
                )
            # Refuse a station the project has not verified, unless asked.
            if key in STATIONS or allow_unverified_station:
                wanted.append(key)
            else:  # pragma: no cover - defensive
                raise MOSGuidanceError(f"station {key} is not in the verified registry")
        if not wanted:
            raise MOSGuidanceError("no stations requested")

        rt = parse_runtime(runtime)
        rt_str = format_runtime(rt)
        rt_archive = rt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        path = self._cache_path(model, rt, wanted)
        self.stats["runs_requested"] += 1

        if not force_refresh:
            cached = self._load_cache(path)
            if cached is not None:
                self.stats["runs_from_cache"] += 1
                return cached

        if self.offline:
            raise MOSGuidanceError(
                f"offline mode: no cached {model} run at {rt_str} for {wanted}"
            )

        params: List[Tuple[str, str]] = [("station", s) for s in wanted]
        params += [("model", model), ("runtime", rt_str)]
        try:
            resp = self.session.get(IEM_MOS_URL, params=params, timeout=self.timeout)
        except Exception as exc:
            self.stats["runs_failed"] += 1
            raise MOSGuidanceError(f"GET {IEM_MOS_URL} failed: {exc}") from exc
        finally:
            if self.request_pause:
                time.sleep(self.request_pause)

        status = getattr(resp, "status_code", None)
        source_url = getattr(resp, "url", IEM_MOS_URL)
        if status == 404:
            # A run that is simply not archived. For a multi-year backfill this
            # is normal (model outages, pre-archive dates, non-existent run
            # hours) and is counted, not raised, and never cached.
            self.stats["runs_missing_404"] += 1
            logger.info(
                "guidance archive has no %s run at %s for %s (HTTP 404)",
                model,
                rt_str,
                ",".join(wanted),
            )
            return []
        if status != 200:
            self.stats["runs_failed"] += 1
            raise MOSGuidanceError(
                f"guidance archive returned HTTP {status} for {model} {rt_str} "
                f"{wanted}: {str(getattr(resp, 'text', ''))[:200]}"
            )
        try:
            payload = resp.json()
        except Exception as exc:
            self.stats["runs_failed"] += 1
            raise MOSGuidanceError(
                f"guidance archive returned non-JSON for {model} {rt_str}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            self.stats["runs_failed"] += 1
            raise MOSGuidanceError(
                f"guidance payload for {model} {rt_str} lacks a 'data' list"
            )

        rows: List[Dict[str, Any]] = []
        wanted_set = set(wanted)
        for raw in payload["data"]:
            if not isinstance(raw, Mapping):
                raise MOSGuidanceError(
                    f"guidance row for {model} {rt_str} is not an object"
                )
            got_rt = str(raw.get("runtime") or "").strip()
            if got_rt != rt_archive:
                # Trap 1: this is what a silently-substituted latest run looks
                # like from the inside.
                raise MOSGuidanceError(
                    f"guidance archive returned runtime {got_rt!r} for a "
                    f"{rt_archive!r} request ({model}); refusing the response "
                    f"(the endpoint returns the LATEST run when 'runtime' is "
                    f"not honoured)"
                )
            got_model = str(raw.get("model") or "").upper().strip()
            if got_model != model:
                raise MOSGuidanceError(
                    f"guidance archive returned model {got_model!r} for a "
                    f"{model!r} request; refusing a cross-model substitution"
                )
            got_station = str(raw.get("station") or "").upper().strip()
            if got_station not in wanted_set:
                raise MOSGuidanceError(
                    f"guidance archive returned station {got_station!r} which was "
                    f"not requested ({sorted(wanted_set)}); refusing a "
                    f"cross-station substitution"
                )
            rows.append(dict(raw))

        if not rows:
            logger.error(
                "guidance archive returned zero rows for %s %s %s at HTTP 200; "
                "reported as a gap, not cached",
                model,
                rt_str,
                ",".join(wanted),
            )
            return rows

        self.stats["runs_fetched"] += 1
        rows.sort(key=lambda r: (str(r.get("station")), str(r.get("ftime"))))
        self._save_cache(
            path,
            rows,
            {
                "model": model,
                "runtime": rt_str,
                "stations": wanted,
                "source_url": source_url,
                "row_count": len(rows),
            },
        )
        return rows

    # -- normalization ----------------------------------------------------
    def daily_highs_from_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        source: str = SOURCE_GFS_MEX,
        stations: Optional[Mapping[str, StationSpec]] = None,
    ) -> List[GuidanceForecast]:
        """Turn raw archive rows into daily-high :class:`GuidanceForecast` records.

        Only rows whose ``n_x`` lands in the station's local afternoon/evening
        survive; see "WHAT ``n_x`` MEANS". Rows carrying a null ``n_x`` (every
        NBS/NBE row) and rows that are overnight minima are counted in
        :attr:`stats` rather than silently dropped, so a source that yields
        nothing says so loudly instead of producing an empty calibration.
        """
        out: List[GuidanceForecast] = []
        for raw in rows:
            self.stats["rows_seen"] += 1
            station = str(raw.get("station") or "").upper().strip()
            try:
                spec = (
                    stations[station]
                    if stations is not None
                    else get_station(station, allow_unverified=True)
                )
            except Exception:
                logger.error(
                    "guidance row for unknown station %r skipped (no timezone "
                    "known, so its local target date cannot be derived)",
                    station,
                )
                continue

            nx = _as_float(raw.get("n_x"))
            if nx is None:
                self.stats["rows_nx_null"] += 1
                continue

            ftime = _parse_ftime(raw.get("ftime"))
            init = parse_runtime(raw.get("runtime"))
            local = ftime.astimezone(ZoneInfo(spec.timezone))

            if _in_range(local.hour, MIN_LOCAL_HOUR_RANGE):
                self.stats["rows_min_discarded"] += 1
                continue
            if not _in_range(local.hour, MAX_LOCAL_HOUR_RANGE):
                self.stats["rows_unclassified_hour"] += 1
                logger.warning(
                    "n_x at %s is local hour %02d for %s, which is neither the "
                    "max window %s nor the min window %s; skipping rather than "
                    "guessing whether it is a maximum",
                    ftime.isoformat(),
                    local.hour,
                    station,
                    MAX_LOCAL_HOUR_RANGE,
                    MIN_LOCAL_HOUR_RANGE,
                )
                continue

            target_date = local.date().isoformat()
            lead = lead_hours_for(init, target_date, spec.timezone)
            provenance = (
                f"{IEM_MOS_URL}?station={station}"
                f"&model={str(raw.get('model') or '').upper()}"
                f"&runtime={format_runtime(init)}"
                f"#ftime={ftime.strftime('%Y-%m-%dT%H:%MZ')}"
            )
            out.append(
                GuidanceForecast(
                    city=_city_code(spec),
                    station=station,
                    target_date=target_date,
                    init_time_utc=format_runtime(init),
                    lead_hours=lead,
                    source=source,
                    forecast_high_f=float(nx),
                    # MEX publishes no spread, and the NBM spread field (XND) is
                    # not historically retrievable -- see the module docstring.
                    # Blank, never 0.0.
                    spread_f=None,
                    provenance=provenance,
                )
            )
            self.stats["forecasts_emitted"] += 1

        out.sort(key=lambda f: (f.city, f.target_date, f.init_time_utc))
        return out

    def fetch_daily_highs(
        self,
        stations: Sequence[str],
        model: str,
        runtime: Any,
        *,
        source: str = SOURCE_GFS_MEX,
        force_refresh: bool = False,
    ) -> List[GuidanceForecast]:
        """One run, fetched and normalized. ``[]`` when the run is not archived."""
        rows = self.fetch_run(stations, model, runtime, force_refresh=force_refresh)
        return self.daily_highs_from_rows(rows, source=source)


def _city_code(spec: StationSpec) -> str:
    """``KXHIGHNY`` -> ``NY``. The calibration filename's ``<CITY>`` token.

    Derived from the Kalshi series ticker rather than the city name so the code
    that names a calibration file and the code that finds the market agree by
    construction.
    """
    ticker = spec.series_ticker.upper()
    return ticker[len("KXHIGH") :] if ticker.startswith("KXHIGH") else ticker


def city_code(station: str) -> str:
    """Public form of :func:`_city_code`, by station id."""
    return _city_code(get_station(station, allow_unverified=True))


__all__ = [
    "MOSGuidanceProvider",
    "MOSGuidanceError",
    "GuidanceForecast",
    "FORECAST_FIELDS",
    "MODEL_WHITELIST",
    "MODELS_WITH_NX",
    "MODEL_RUN_HOURS",
    "MAX_LOCAL_HOUR_RANGE",
    "MIN_LOCAL_HOUR_RANGE",
    "SOURCE_GFS_MEX",
    "IEM_MOS_URL",
    "FORECAST_ARCHIVE_DIR",
    "city_code",
    "format_runtime",
    "parse_runtime",
    "lead_hours_for",
    "local_day_start_utc",
    "run_times",
]
