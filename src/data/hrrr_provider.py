"""
HRRR-Proxy Weather Model Provider (Sprint 1, Task 1.6)

Fetches NWS hourly gridpoint forecasts (numerical model output) and compares
them against the NWS text forecast (human-edited, what Kalshi prices off).
The divergence between model output and text forecast is the trading signal.

HRRR (High-Resolution Rapid Refresh) is the best same-day temperature
predictor. When it diverges from the NWS text forecast, there is exploitable
edge on Kalshi weather markets.
"""

import logging
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

from src.core.interfaces import DataProvider, MarketData

logger = logging.getLogger(__name__)

# Default station coordinates (lat, lon)
DEFAULT_STATIONS = {
    "KNYC": (40.78, -73.97),
    "KLAX": (33.94, -118.41),
    "KMDW": (41.79, -87.75),
    "KMIA": (25.79, -80.29),
}


class HRRRProvider(DataProvider):
    """
    Data provider that uses NWS hourly gridpoint forecasts as a proxy for
    HRRR model data. Compares model-driven hourly forecasts against the
    human-edited NWS text forecast to find divergences.

    The NWS gridpoint hourly forecast endpoint returns data derived from
    numerical weather models (including HRRR), while the standard /forecast
    endpoint returns human-edited text forecasts. Kalshi prices off the
    text forecast, so divergence = edge.
    """

    BASE_URL = "https://api.weather.gov"

    def __init__(
        self, user_agent: str, stations: Dict[str, Tuple[float, float]] = None
    ):
        """
        :param user_agent: Required NWS API user-agent string "(AppName, email)"
        :param stations: Dict mapping station_id -> (lat, lon). Grid info is
                         looked up automatically via the NWS /points endpoint.
                         Defaults to KNYC, KLAX, KMDW, KMIA.
        """
        self.user_agent = user_agent
        self.headers = {"User-Agent": self.user_agent, "Accept": "application/geo+json"}

        # Build station config from coordinates; grid info resolved on connect()
        self._stations: Dict[str, Dict[str, Any]] = {}
        src = stations if stations else DEFAULT_STATIONS
        for station_id, coords in src.items():
            lat, lon = coords
            self._stations[station_id] = {
                "lat": lat,
                "lon": lon,
                "grid_office": None,
                "grid_x": None,
                "grid_y": None,
                "connected": False,
            }

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Resolve grid coordinates for all stations via NWS /points API."""
        success_count = 0
        for station_id in self._stations:
            if self._connect_station(station_id):
                success_count += 1
        connected = success_count > 0
        if connected:
            logger.info(
                "HRRRProvider connected: %d/%d stations ready",
                success_count,
                len(self._stations),
            )
        else:
            logger.error("HRRRProvider failed to connect any stations")
        return connected

    def fetch_latest(self, symbol: str) -> Optional[MarketData]:
        """
        Fetch model (hourly grid) and text forecast for a station, compute
        divergence. Returns MarketData with divergence details in extra dict.

        :param symbol: Station ID (e.g. 'KNYC')
        :returns: MarketData or None on failure
        """
        station = self._stations.get(symbol)
        if not station:
            logger.error("Unknown station: %s", symbol)
            return None

        if not station["connected"]:
            if not self._connect_station(symbol):
                return None

        hourly_temps = self._fetch_hourly_grid(symbol)
        text_forecast = self._fetch_text_forecast(symbol)

        if hourly_temps is None or text_forecast is None:
            logger.warning("Failed to fetch data for %s", symbol)
            return None

        # Extract model high/low from hourly grid temps
        temps_only = [t for _, t in hourly_temps]
        model_high = max(temps_only) if temps_only else None
        model_low = min(temps_only) if temps_only else None

        # Extract text forecast high/low
        forecast_high = text_forecast.get("high_f")
        forecast_low = text_forecast.get("low_f")

        # Compute divergence (model high minus forecast high)
        divergence = None
        if model_high is not None and forecast_high is not None:
            divergence = round(model_high - forecast_high, 1)

        return MarketData(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            price=0.0,
            volume=0,
            bid=0,
            ask=0,
            extra={
                "model_high_f": model_high,
                "model_low_f": model_low,
                "forecast_high_f": forecast_high,
                "forecast_low_f": forecast_low,
                "divergence_f": divergence,
                "hourly_temps": hourly_temps,
                "source": "hrrr_proxy",
            },
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_divergence(self, station_id: str) -> Optional[float]:
        """Convenience method: fetch latest and return just the divergence value."""
        data = self.fetch_latest(station_id)
        if data and data.extra:
            return data.extra.get("divergence_f")
        return None

    def get_stations(self) -> List[str]:
        """Return list of configured station IDs."""
        return list(self._stations.keys())

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _connect_station(self, station_id: str) -> bool:
        """Resolve grid office/x/y for a station via the NWS /points API."""
        station = self._stations.get(station_id)
        if not station:
            return False

        try:
            url = f"{self.BASE_URL}/points/{station['lat']},{station['lon']}"
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            props = resp.json().get("properties", {})

            grid_office = props.get("gridId")
            grid_x = props.get("gridX")
            grid_y = props.get("gridY")

            if not all([grid_office, grid_x is not None, grid_y is not None]):
                logger.error("Incomplete grid data for %s: %s", station_id, props)
                return False

            station["grid_office"] = grid_office
            station["grid_x"] = grid_x
            station["grid_y"] = grid_y
            station["connected"] = True
            logger.info(
                "HRRRProvider resolved %s -> %s/%s,%s",
                station_id,
                grid_office,
                grid_x,
                grid_y,
            )
            return True

        except requests.RequestException as e:
            logger.error("HRRRProvider connect error for %s: %s", station_id, e)
            return False

    def _fetch_hourly_grid(self, station_id: str) -> Optional[List[Tuple[str, float]]]:
        """
        Fetch hourly gridpoint forecast (model output) for a station.
        Returns list of (ISO hour string, temperature in F).
        Filters to today's remaining hours only.
        """
        station = self._stations[station_id]
        office = station["grid_office"]
        x = station["grid_x"]
        y = station["grid_y"]

        url = f"{self.BASE_URL}/gridpoints/{office}/{x},{y}/forecast/hourly"

        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            periods = resp.json().get("properties", {}).get("periods", [])

            if not periods:
                logger.warning("No hourly periods returned for %s", station_id)
                return None

            now = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")
            hourly_temps = []

            for period in periods:
                start = period.get("startTime", "")
                # Filter to today only (startTime is ISO 8601)
                if today_str not in start:
                    # Once we pass today, stop collecting
                    if hourly_temps:
                        break
                    continue

                temp = period.get("temperature")
                unit = period.get("temperatureUnit", "F")
                if temp is None:
                    continue

                # Convert to Fahrenheit if needed
                temp_f = float(temp)
                if unit == "C":
                    temp_f = temp_f * 9.0 / 5.0 + 32.0

                hourly_temps.append((start, round(temp_f, 1)))

            return hourly_temps if hourly_temps else None

        except requests.RequestException as e:
            logger.error("HRRRProvider hourly grid error for %s: %s", station_id, e)
            return None

    def _fetch_text_forecast(self, station_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the NWS text forecast (human-edited, 7-day periods).
        Extracts today's high and tonight's low from the period names.
        Returns dict with 'high_f' and 'low_f'.
        """
        station = self._stations[station_id]
        office = station["grid_office"]
        x = station["grid_x"]
        y = station["grid_y"]

        url = f"{self.BASE_URL}/gridpoints/{office}/{x},{y}/forecast"

        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            periods = resp.json().get("properties", {}).get("periods", [])

            if not periods:
                logger.warning("No forecast periods returned for %s", station_id)
                return None

            high_f = None
            low_f = None

            for period in periods:
                _name = period.get("name", "").lower()  # noqa: F841
                temp = period.get("temperature")
                if temp is None:
                    continue

                unit = period.get("temperatureUnit", "F")
                temp_f = float(temp)
                if unit == "C":
                    temp_f = temp_f * 9.0 / 5.0 + 32.0

                is_daytime = period.get("isDaytime", True)

                # First daytime period = today's high
                if is_daytime and high_f is None:
                    high_f = round(temp_f, 1)
                # First nighttime period = tonight's low
                elif not is_daytime and low_f is None:
                    low_f = round(temp_f, 1)

                if high_f is not None and low_f is not None:
                    break

            return {"high_f": high_f, "low_f": low_f}

        except requests.RequestException as e:
            logger.error("HRRRProvider text forecast error for %s: %s", station_id, e)
            return None
