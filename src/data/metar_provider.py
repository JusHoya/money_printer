"""
METAR Aviation Weather Data Provider.

Fetches METAR observations from the Aviation Weather Center API.
Provides higher-precision temperature data (0.1C / 0.18F) than NWS (whole degrees)
by parsing the T-group from raw METAR remarks.

API: https://aviationweather.gov/api/data/metar (no auth required, 100 req/min)

PRD FR-1.4 — settlement stations only
-------------------------------------
Kalshi daily-high markets settle on a specific NWS station, not on "the city":

    KXHIGHNY  -> KNYC (New York City, Central Park)
    KXHIGHCHI -> KMDW (Chicago Midway Airport)
    KXHIGHLAX -> KLAX (Los Angeles International Airport)
    KXHIGHMIA -> KMIA (Miami International Airport)

The non-settlement airports KJFK and KORD were previously observed instead of
KNYC/KMDW; the microclimate difference is 3-5F, which poisons every
observation-driven decision. Probed live on 2026-07-25, the Aviation Weather
Center API serves all four settlement stations -- including the non-airport
KNYC -- with hourly reports carrying full T-group precision, so METAR is the
primary observation source for every settlement station.

Running daily max is computed over the STATION'S LOCAL CALENDAR DAY. A UTC
calendar day is wrong for every US city and catastrophically wrong for
Los Angeles, where UTC midnight falls at 17:00 local -- mid heating window.
A station with no configured timezone yields no daily max at all (``None``)
rather than a silently wrong UTC-day figure.
"""

import math
import re
import requests
from datetime import datetime, timezone
from typing import Dict, Optional, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core.interfaces import DataProvider, MarketData
from src.utils.logger import logger

# Settlement stations for the tracked Kalshi KXHIGH* series (PRD FR-1.4).
SETTLEMENT_STATIONS: List[str] = ["KNYC", "KMDW", "KLAX", "KMIA"]

# IANA timezone per settlement station, verified against the ``timeZone`` field
# of https://api.weather.gov/stations/<id> on 2026-07-25. Used only when the
# provider is constructed standalone; ``WeatherBot`` passes the authoritative
# map from its city config. ``tests/test_weather_stations.py`` asserts the two
# never drift apart.
DEFAULT_STATION_TIMEZONES: Dict[str, str] = {
    "KNYC": "America/New_York",
    "KMDW": "America/Chicago",
    "KLAX": "America/Los_Angeles",
    "KMIA": "America/New_York",
}

# Regex for T-group in METAR remarks: T{sign1}{temp3}{sign2}{dew3}
# sign: 0=positive, 1=negative; temp3/dew3: tenths of degrees C
# Example: T02720056 -> temp=+27.2C, dewpoint=+5.6C
_TGROUP_RE = re.compile(r"T(\d{8})")


def parse_tgroup(raw_metar: str) -> Optional[tuple]:
    """
    Extract high-precision temp and dewpoint from the T-group in a raw METAR string.

    Returns (temp_c, dewpoint_c) as floats with 0.1C precision, or None if not found.
    """
    match = _TGROUP_RE.search(raw_metar)
    if not match:
        return None

    digits = match.group(1)
    # First 4 digits: sign + 3-digit temp in tenths
    temp_sign = -1 if digits[0] == "1" else 1
    temp_c = temp_sign * int(digits[1:4]) / 10.0

    # Last 4 digits: sign + 3-digit dewpoint in tenths
    dew_sign = -1 if digits[4] == "1" else 1
    dew_c = dew_sign * int(digits[5:8]) / 10.0

    return (temp_c, dew_c)


class METARProvider(DataProvider):
    """
    Live data provider for METAR aviation weather observations.

    Provides faster updates and higher temperature precision than NWS by
    parsing the T-group from raw METAR strings (0.1C vs 1C resolution).
    """

    BASE_URL = "https://aviationweather.gov/api/data/metar"
    # PRD FR-1.4: settlement stations only. Airport proxies are not defaults.
    DEFAULT_STATIONS = list(SETTLEMENT_STATIONS)
    REQUEST_TIMEOUT = 10
    # Upper bound on the history window requested for a running daily max.
    # A local calendar day never exceeds 25h (DST fall-back) + slack.
    MAX_DAILY_MAX_HOURS = 30

    def __init__(
        self,
        stations: Optional[List[str]] = None,
        station_timezones: Optional[Dict[str, str]] = None,
    ):
        """
        :param stations: List of ICAO station IDs. Defaults to the four
            settlement stations (KNYC, KMDW, KLAX, KMIA).
        :param station_timezones: ``{station_id: IANA timezone}``. Required for
            the running daily max, which is computed over the station's local
            calendar day. Defaults to :data:`DEFAULT_STATION_TIMEZONES`; a
            station absent from the map gets no daily max rather than a
            UTC-day approximation.
        """
        self.stations = stations or self.DEFAULT_STATIONS
        self.station_timezones = dict(
            station_timezones
            if station_timezones is not None
            else DEFAULT_STATION_TIMEZONES
        )
        self._connected = False
        self._tz_cache: Dict[str, Optional[ZoneInfo]] = {}

    # ── DataProvider interface ──────────────────────────────────────

    def connect(self) -> bool:
        """Verify API is reachable with a lightweight test call."""
        try:
            resp = self._api_call(ids=self.stations[0], hours=1)
            if resp is not None:
                self._connected = True
                logger.info(
                    f"[METARProvider] Connected — test station {self.stations[0]}, "
                    f"got {len(resp)} observation(s)"
                )
                return True
        except Exception as e:
            logger.error(f"[METARProvider] Connect failed: {e}")

        self._connected = False
        return False

    def fetch_latest(self, symbol: str) -> Optional[MarketData]:
        """
        Fetch the most recent METAR observation for a single station.

        :param symbol: ICAO settlement station ID (e.g. 'KNYC')
        :returns: MarketData with temperature and metadata in extra dict, or None on failure.
        """
        observations = self._api_call(ids=symbol, hours=2)
        if not observations:
            logger.warning(f"[METARProvider] No observations for {symbol}")
            return None

        # API returns newest first
        latest = observations[0]
        return self._observation_to_market_data(symbol, latest)

    # ── Extended methods ────────────────────────────────────────────

    def fetch_all(self) -> Dict[str, MarketData]:
        """
        Fetch latest observations for ALL configured stations in a single API call.

        Returns dict keyed by station ID. Stations with no data are omitted.
        """
        result: Dict[str, MarketData] = {}

        ids_str = ",".join(self.stations)
        observations = self._api_call(ids=ids_str, hours=2)
        if not observations:
            logger.warning("[METARProvider] fetch_all returned no data")
            return result

        # Group observations by station, keep only the newest per station
        newest: Dict[str, dict] = {}
        for obs in observations:
            station = obs.get("icaoId", "")
            if station and station not in newest:
                newest[station] = obs

        for station, obs in newest.items():
            md = self._observation_to_market_data(station, obs)
            if md is not None:
                result[station] = md

        return result

    # ── Internal helpers ────────────────────────────────────────────

    def _api_call(
        self, ids: str, hours: int = 1, retries: int = 1
    ) -> Optional[List[dict]]:
        """
        Call the Aviation Weather Center METAR API.

        :param ids: Comma-separated ICAO station IDs.
        :param hours: How many hours of history to request.
        :param retries: Number of retries on failure (default 1).
        :returns: List of observation dicts, or None on persistent failure.
        """
        params = {"ids": ids, "format": "json", "hours": hours}
        last_error = None

        for attempt in range(1 + retries):
            try:
                resp = requests.get(
                    self.BASE_URL,
                    params=params,
                    timeout=self.REQUEST_TIMEOUT,
                )
                resp.raise_for_status()

                data = resp.json()
                # API returns a JSON array directly
                if isinstance(data, list):
                    return data

                logger.warning(
                    f"[METARProvider] Unexpected response type: {type(data)}"
                )
                return None

            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning(f"[METARProvider] Timeout (attempt {attempt + 1}): {e}")
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning(
                    f"[METARProvider] Request error (attempt {attempt + 1}): {e}"
                )
            except ValueError as e:
                # JSON decode error
                last_error = e
                logger.warning(
                    f"[METARProvider] JSON parse error (attempt {attempt + 1}): {e}"
                )

        logger.error(f"[METARProvider] API call failed after retries: {last_error}")
        return None

    def _observation_to_market_data(
        self, station: str, obs: dict
    ) -> Optional[MarketData]:
        """Convert a single METAR observation dict into a MarketData object."""
        try:
            raw_metar = obs.get("rawOb", "")

            # -- Temperature extraction --
            tgroup = parse_tgroup(raw_metar) if raw_metar else None
            if tgroup is not None:
                temp_c = tgroup[0]
                precision = "tenths"
            else:
                temp_c = obs.get("temp")
                if temp_c is None:
                    logger.warning(
                        f"[METARProvider] No temperature in observation for {station}"
                    )
                    return None
                temp_c = float(temp_c)
                precision = "whole"

            temp_f = temp_c * 9.0 / 5.0 + 32.0

            # -- Observation time and staleness --
            report_time_str = obs.get("reportTime", "")
            obs_dt = self._parse_report_time(report_time_str)
            now_utc = datetime.now(timezone.utc)
            age_seconds = (now_utc - obs_dt).total_seconds() if obs_dt else 0.0

            # -- Daily max temperature (station's LOCAL calendar day) --
            daily_max_f = self._get_daily_max_temp(station)
            if daily_max_f is None:
                daily_max_f = temp_f
            elif temp_f > daily_max_f:
                daily_max_f = temp_f

            # -- SPECI count (special reports indicate rapid changes) --
            speci_count = self._count_recent_reports(station)

            tz = self._timezone_for(station)
            local_day = (
                datetime.now(timezone.utc).astimezone(tz).date().isoformat()
                if tz
                else None
            )

            return MarketData(
                symbol=station,
                timestamp=datetime.now(),
                price=0.0,
                volume=0,
                bid=0,
                ask=0,
                extra={
                    "temperature_f": round(temp_f, 2),
                    "max_temp_today_f": round(daily_max_f, 2),
                    "temperature_c": round(temp_c, 1),
                    "description": obs.get("wxString") or "METAR observation",
                    "source": "live_metar",
                    "forecast": None,
                    "station_name": station,
                    # PRD FR-1.4 provenance: which station this observation is
                    # from and which calendar day the running max covers.
                    "settlement_station": station,
                    "station_timezone": self.station_timezones.get(station),
                    "max_temp_local_day": local_day,
                    "metar_raw": raw_metar,
                    "observation_time": (
                        obs_dt.isoformat() if obs_dt else report_time_str
                    ),
                    "metar_age_seconds": round(age_seconds, 1),
                    "speci_count": speci_count,
                    "temp_precision": precision,
                },
            )

        except Exception as e:
            logger.error(
                f"[METARProvider] Error parsing observation for {station}: {e}"
            )
            return None

    def _timezone_for(self, station_id: str) -> Optional[ZoneInfo]:
        """Resolve the station's IANA timezone, or ``None`` if unconfigured."""
        if station_id in self._tz_cache:
            return self._tz_cache[station_id]

        tz_name = self.station_timezones.get(station_id)
        tz: Optional[ZoneInfo] = None
        if tz_name:
            try:
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
                logger.error(
                    f"[METARProvider] Bad timezone {tz_name!r} for {station_id}: {e}"
                )
        self._tz_cache[station_id] = tz
        return tz

    def _hours_covering_local_day(self, tz: ZoneInfo) -> int:
        """History window (hours) that spans local midnight through now."""
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc.astimezone(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_h = (now_utc - local_midnight.astimezone(timezone.utc)).total_seconds()
        # +2h of slack: the API's window is inclusive of partial hours and the
        # oldest report of the local day may sit just inside the boundary.
        hours = int(math.ceil(elapsed_h / 3600.0)) + 2
        return max(2, min(hours, self.MAX_DAILY_MAX_HOURS))

    @staticmethod
    def _observation_datetime(obs: dict) -> Optional[datetime]:
        """UTC datetime of an observation: ``obsTime`` epoch, else reportTime.

        ``reportTime`` is normalised to the top of the hour by the API, so the
        epoch ``obsTime`` is preferred whenever present.
        """
        epoch = obs.get("obsTime")
        if epoch is not None:
            try:
                return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        return METARProvider._parse_report_time(obs.get("reportTime", ""))

    def _get_daily_max_temp(self, station_id: str) -> Optional[float]:
        """Highest temperature recorded so far in the station's LOCAL day.

        PRD FR-1.4: the running daily max must be scoped to the settlement
        station's own local calendar day, because that is the day Kalshi
        settles on. A UTC day boundary lands at 17:00 local in Los Angeles and
        20:00 local in New York, which would reset the running max in the
        middle of (or just after) the heating window.

        Returns ``None`` -- never a UTC-day approximation -- when the station
        has no configured timezone, so callers degrade to "current temperature
        is the best-known max" instead of consuming a wrong-day figure.
        """
        tz = self._timezone_for(station_id)
        if tz is None:
            logger.error(
                f"[METARProvider] No timezone configured for {station_id}; "
                f"refusing to compute a UTC-day daily max (PRD FR-1.4)"
            )
            return None

        hours = self._hours_covering_local_day(tz)
        observations = self._api_call(ids=station_id, hours=hours, retries=0)
        if not observations:
            return None

        local_today = datetime.now(timezone.utc).astimezone(tz).date()
        max_c = None

        for obs in observations:
            obs_dt = self._observation_datetime(obs)
            if obs_dt is None:
                continue
            if obs_dt.astimezone(tz).date() != local_today:
                continue

            raw = obs.get("rawOb", "")
            tgroup = parse_tgroup(raw) if raw else None

            if tgroup is not None:
                temp_c = tgroup[0]
            else:
                temp_c = obs.get("temp")
                if temp_c is None:
                    continue
                temp_c = float(temp_c)

            if max_c is None or temp_c > max_c:
                max_c = temp_c

        if max_c is not None:
            return max_c * 9.0 / 5.0 + 32.0
        return None

    def _count_recent_reports(self, station_id: str) -> int:
        """
        Count METAR reports in the last hour for a station.

        A high count (>2) suggests rapidly changing conditions
        (SPECI reports are issued on significant weather changes).
        """
        observations = self._api_call(ids=station_id, hours=1, retries=0)
        if not observations:
            return 0
        return sum(
            1
            for obs in observations
            if obs.get("icaoId", "").upper() == station_id.upper()
        )

    @staticmethod
    def _parse_report_time(report_time_str: str) -> Optional[datetime]:
        """
        Parse the reportTime string from the METAR API.

        The live API returns '2026-07-25T16:00:00.000Z'; older responses used
        '2026-04-02 14:53:00'. Both are UTC and both are accepted.
        """
        if not report_time_str:
            return None
        try:
            # Legacy space-separated form
            dt = datetime.strptime(report_time_str, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
        try:
            # Current form: ISO-8601, trailing 'Z' for UTC (fromisoformat only
            # accepts 'Z' from Python 3.11, so normalise it explicitly).
            normalised = str(report_time_str).strip()
            if normalised.endswith("Z"):
                normalised = normalised[:-1] + "+00:00"
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None
