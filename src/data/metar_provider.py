"""
METAR Aviation Weather Data Provider.

Fetches METAR observations from the Aviation Weather Center API.
Provides higher-precision temperature data (0.1C / 0.18F) than NWS (whole degrees)
by parsing the T-group from raw METAR remarks.

API: https://aviationweather.gov/api/data/metar (no auth required, 100 req/min)
"""

import re
import requests
from datetime import datetime, timezone
from typing import Dict, Optional, List

from src.core.interfaces import DataProvider, MarketData
from src.utils.logger import logger

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
    DEFAULT_STATIONS = ["KJFK", "KLAX", "KORD", "KMIA"]
    REQUEST_TIMEOUT = 10

    def __init__(self, stations: Optional[List[str]] = None):
        """
        :param stations: List of ICAO station IDs. Defaults to KJFK, KLAX, KORD, KMIA.
        """
        self.stations = stations or self.DEFAULT_STATIONS
        self._connected = False

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

        :param symbol: ICAO station ID (e.g. 'KJFK')
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

            # -- Daily max temperature --
            daily_max_f = self._get_daily_max_temp(station)
            if daily_max_f is None:
                daily_max_f = temp_f
            elif temp_f > daily_max_f:
                daily_max_f = temp_f

            # -- SPECI count (special reports indicate rapid changes) --
            speci_count = self._count_recent_reports(station)

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

    def _get_daily_max_temp(self, station_id: str) -> Optional[float]:
        """
        Fetch up to 24 hours of observations and return the highest temperature
        recorded today (UTC). Uses T-group precision where available.
        """
        observations = self._api_call(ids=station_id, hours=24, retries=0)
        if not observations:
            return None

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        max_c = None

        for obs in observations:
            report_time_str = obs.get("reportTime", "")
            if not report_time_str.startswith(today_str):
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

        Expected format: '2026-04-02 14:53:00' (UTC).
        """
        if not report_time_str:
            return None
        try:
            # Try ISO-ish format the API returns
            dt = datetime.strptime(report_time_str, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            # Fallback: full ISO with timezone
            dt = datetime.fromisoformat(report_time_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
