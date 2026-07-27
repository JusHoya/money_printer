import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core.interfaces import DataProvider, MarketData
from src.data.metar_provider import DEFAULT_STATION_TIMEZONES


class NWSProvider(DataProvider):
    """
    Live Data Provider for National Weather Service (NWS).
    API Docs: https://www.weather.gov/documentation/services-web-api

    PRD FR-1.4: stations here are Kalshi SETTLEMENT stations (KNYC, KMDW,
    KLAX, KMIA). This provider is the observation fallback when the Aviation
    Weather Center METAR feed is unavailable, and is the sole source of the
    7-day point forecast. The running daily max is computed over the STATION'S
    local calendar day, resolved from the ``timeZone`` field of the station
    metadata endpoint -- never the process's local day and never a UTC day.
    """

    BASE_URL = "https://api.weather.gov"

    def __init__(self, user_agent: str, station_id: str = "KNYC"):
        """
        :param user_agent: Required string "(App Name, Email)"
        :param station_id: ICAO settlement station ID (e.g., 'KNYC') OR a list
            of IDs ['KNYC', 'KMDW']
        """
        self.user_agent = user_agent
        # Normalize input to list
        if isinstance(station_id, str):
            self.stations = [station_id]
        else:
            self.stations = station_id

        self.headers = {"User-Agent": self.user_agent}
        # Per-station metadata: { 'KNYC': {'name':..., 'forecast_url':..., 'timezone':...} }
        self.station_cache = {}
        self._tz_cache: Dict[str, Optional[ZoneInfo]] = {}

    def connect(self) -> bool:
        """
        Verifies connection for ALL configured stations.
        """
        success_count = 0
        for station in self.stations:
            print(f"[NWSProvider] Connecting to {station}...")
            if self._connect_station(station):
                success_count += 1

        return success_count > 0

    def _connect_station(self, station_id: str) -> bool:
        try:
            url = f"{self.BASE_URL}/stations/{station_id}"
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            props = resp.json().get("properties", {})
            geom = resp.json().get("geometry", {})

            coords = geom.get("coordinates")
            if not coords:
                return False

            lat, lon = coords[1], coords[0]

            # Get Point Data
            point_url = f"{self.BASE_URL}/points/{lat},{lon}"
            resp = requests.get(point_url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            point_props = resp.json()["properties"]

            self.station_cache[station_id] = {
                "name": props.get("name"),
                "forecast_url": point_props.get("forecast"),
                # IANA timezone straight from the station metadata — the
                # authority for the local calendar day the daily max spans.
                "timezone": props.get("timeZone"),
            }
            print(f"[NWSProvider] Resolved {station_id}: {props.get('name')}")
            return True

        except Exception as e:
            print(f"[NWSProvider] Error connecting to {station_id}: {e}")
            return False

    def fetch_forecast(self, station_id: str = None) -> Optional[List[Dict[str, Any]]]:
        """
        Fetches the 7-day forecast for a specific station.
        """
        target = station_id if station_id else self.stations[0]
        meta = self.station_cache.get(target)

        if not meta:
            if not self._connect_station(target):
                return None
            meta = self.station_cache.get(target)

        try:
            resp = requests.get(meta["forecast_url"], headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()["properties"]["periods"]
        except Exception as e:
            print(f"[NWSProvider] Forecast Fetch Error ({target}): {e}")
            return None

    def timezone_for(self, station_id: str) -> Optional[ZoneInfo]:
        """Resolve a station's IANA timezone.

        Order of authority: cached station metadata (``timeZone`` from
        ``/stations/{id}``) -> a live metadata fetch -> the settlement-station
        table in ``metar_provider``. Returns ``None`` when all three fail, and
        callers must then decline to compute a daily max rather than fall back
        to a UTC or process-local day (PRD FR-1.4).
        """
        if station_id in self._tz_cache:
            return self._tz_cache[station_id]

        tz_name = (self.station_cache.get(station_id) or {}).get("timezone")
        if not tz_name:
            self._connect_station(station_id)
            tz_name = (self.station_cache.get(station_id) or {}).get("timezone")
        if not tz_name:
            tz_name = DEFAULT_STATION_TIMEZONES.get(station_id)

        tz = None
        if tz_name:
            try:
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
                print(f"[NWSProvider] Bad timezone {tz_name!r} for {station_id}: {e}")
        self._tz_cache[station_id] = tz
        return tz

    @staticmethod
    def _parse_obs_timestamp(ts_str: str) -> Optional[datetime]:
        """Parse an NWS observation timestamp ('2026-07-25T15:51:00+00:00')."""
        if not ts_str:
            return None
        try:
            normalised = str(ts_str).strip()
            if normalised.endswith("Z"):
                normalised = normalised[:-1] + "+00:00"
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    def _get_daily_max_temp(self, station_id: str) -> Optional[float]:
        """Max temp recorded so far in the STATION'S local calendar day.

        PRD FR-1.4. The previous implementation compared UTC observation
        timestamps against the *process's* local date via a string prefix
        match — wrong on the UTC VM for every city and wrong on a US laptop
        for anything west of it. Both the "today" reference and the per-
        observation date are now taken in the station's own timezone.
        """
        tz = self.timezone_for(station_id)
        if tz is None:
            print(
                f"[NWSProvider] No timezone for {station_id}; refusing to "
                f"compute a UTC-day daily max (PRD FR-1.4)"
            )
            return None

        try:
            # Fetch recent observations (returns ~24h+ worth)
            url = f"{self.BASE_URL}/stations/{station_id}/observations"
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()

            features = resp.json().get("features", [])
            if not features:
                return None

            return self._max_from_features(features, tz)

        except Exception as e:
            print(f"[NWSProvider] History Fetch Error ({station_id}): {e}")
            return None

    @classmethod
    def _max_from_features(cls, features: List[dict], tz: ZoneInfo) -> Optional[float]:
        """Highest temperature (F) among features falling on ``tz``'s today."""
        local_today = datetime.now(timezone.utc).astimezone(tz).date()
        max_c = None

        for f in features:
            props = f.get("properties", {}) or {}
            obs_dt = cls._parse_obs_timestamp(props.get("timestamp", ""))
            if obs_dt is None:
                continue
            if obs_dt.astimezone(tz).date() != local_today:
                continue

            val = (props.get("temperature") or {}).get("value")
            if val is None:
                continue
            val = float(val)
            if max_c is None or val > max_c:
                max_c = val

        if max_c is None:
            return None
        return (max_c * 9 / 5) + 32

    def fetch_latest(self, symbol: str = None) -> MarketData:
        """
        Fetches latest for ALL stations (returns list) OR specific one.
        Currently returns the FIRST one to satisfy single-return interface,
        or we need to update interface to allow list returns.

        ADAPTATION: If symbol is passed (e.g. 'KNYC'), fetch that.
        If None, iterate all and return a list of MarketData objects?
        The current Architecture expects ONE return object.

        WORKAROUND: We will return the PRIMARY station (index 0) by default,
        but allow specific queries. The Dashboard loop should iterate stations.
        """
        target = symbol if symbol else self.stations[0]

        # ... (Existing fetch logic adapted for 'target')
        url = f"{self.BASE_URL}/stations/{target}/observations/latest"

        try:
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()["properties"]

            temp_c = data.get("temperature", {}).get("value")
            temp_f = (temp_c * 9 / 5) + 32 if temp_c is not None else None

            forecast_periods = self.fetch_forecast(target)

            # NEW: Get Daily High so far
            daily_high_f = self._get_daily_max_temp(target)

            # If no history (e.g. start of day), assume current is high
            if daily_high_f is None and temp_f is not None:
                daily_high_f = temp_f
            # If current is higher than history (lag), update it
            if temp_f and daily_high_f and temp_f > daily_high_f:
                daily_high_f = temp_f

            tz = self.timezone_for(target)
            local_day = (
                datetime.now(timezone.utc).astimezone(tz).date().isoformat()
                if tz
                else None
            )

            return MarketData(
                symbol=target,
                timestamp=datetime.now(),
                price=0.0,
                volume=0,
                bid=0,
                ask=0,
                extra={
                    "temperature_f": temp_f,
                    "max_temp_today_f": daily_high_f,
                    "temperature_c": temp_c,
                    "description": data.get("textDescription"),
                    "source": "live_nws",
                    "forecast": forecast_periods,
                    "station_name": self.station_cache.get(target, {}).get("name"),
                    # PRD FR-1.4 provenance
                    "settlement_station": target,
                    "station_timezone": self.station_cache.get(target, {}).get(
                        "timezone"
                    )
                    or DEFAULT_STATION_TIMEZONES.get(target),
                    "max_temp_local_day": local_day,
                },
            )
        except Exception as e:
            print(f"[NWSProvider] Fetch Error ({target}): {e}")
            return None
