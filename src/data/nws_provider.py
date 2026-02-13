import requests
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from src.core.interfaces import DataProvider, MarketData

class NWSProvider(DataProvider):
    """
    Live Data Provider for National Weather Service (NWS).
    API Docs: https://www.weather.gov/documentation/services-web-api
    """
    
    BASE_URL = "https://api.weather.gov"
    
    def __init__(self, user_agent: str, station_id: str = "KJFK"):
        """
        :param user_agent: Required string "(App Name, Email)"
        :param station_id: ICAO station ID (e.g., 'KJFK') OR a list of IDs ['KJFK', 'KLAX']
        """
        self.user_agent = user_agent
        # Normalize input to list
        if isinstance(station_id, str):
            self.stations = [station_id]
        else:
            self.stations = station_id
            
        self.headers = {"User-Agent": self.user_agent}
        # Dictionary to store per-station data: { 'KJFK': { 'grid_id': ..., 'forecast_url': ... } }
        self.station_cache = {} 
        
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
            props = resp.json().get('properties', {})
            geom = resp.json().get('geometry', {})
            
            coords = geom.get('coordinates')
            if not coords: return False
                
            lat, lon = coords[1], coords[0]
            
            # Get Point Data
            point_url = f"{self.BASE_URL}/points/{lat},{lon}"
            resp = requests.get(point_url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            point_props = resp.json()['properties']
            
            self.station_cache[station_id] = {
                "name": props.get('name'),
                "forecast_url": point_props.get('forecast')
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
            if not self._connect_station(target): return None
            meta = self.station_cache.get(target)
            
        try:
            resp = requests.get(meta['forecast_url'], headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()['properties']['periods']
        except Exception as e:
            print(f"[NWSProvider] Forecast Fetch Error ({target}): {e}")
            return None

    def _get_daily_max_temp(self, station_id: str) -> Optional[float]:
        """
        Fetches observation history to find the max temp recorded so far TODAY.
        """
        try:
            # Fetch recent observations (returns ~24h worth usually)
            url = f"{self.BASE_URL}/stations/{station_id}/observations"
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            
            features = resp.json().get('features', [])
            if not features: return None
            
            max_c = -999.0
            found_data = False
            
            # Simple Date Check (System Local Time vs Observation Time)
            # Ideally we check Station Local Time, but System Local is close enough for US Trading
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            for f in features:
                props = f.get('properties', {})
                ts_str = props.get('timestamp', '')
                
                # Check if observation is from Today
                # ISO Format: 2026-01-31T15:53:00+00:00
                if ts_str.startswith(today_str):
                    val = props.get('temperature', {}).get('value')
                    if val is not None:
                        if val > max_c:
                            max_c = val
                            found_data = True
                            
            if found_data:
                return (max_c * 9/5) + 32
            return None
            
        except Exception as e:
            print(f"[NWSProvider] History Fetch Error ({station_id}): {e}")
            return None

    def fetch_latest(self, symbol: str = None) -> MarketData:
        """
        Fetches latest for ALL stations (returns list) OR specific one.
        Currently returns the FIRST one to satisfy single-return interface,
        or we need to update interface to allow list returns.
        
        ADAPTATION: If symbol is passed (e.g. 'KJFK'), fetch that.
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
            data = resp.json()['properties']
            
            temp_c = data.get('temperature', {}).get('value')
            temp_f = (temp_c * 9/5) + 32 if temp_c is not None else None
            
            forecast_periods = self.fetch_forecast(target)
            
            # NEW: Get Daily High so far
            daily_high_f = self._get_daily_max_temp(target)
            
            # If no history (e.g. start of day), assume current is high
            if daily_high_f is None and temp_f is not None:
                daily_high_f = temp_f
            # If current is higher than history (lag), update it
            if temp_f and daily_high_f and temp_f > daily_high_f:
                daily_high_f = temp_f
            
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
                    "description": data.get('textDescription'),
                    "source": "live_nws",
                    "forecast": forecast_periods,
                    "station_name": self.station_cache.get(target, {}).get('name')
                }
            )
        except Exception as e:
            print(f"[NWSProvider] Fetch Error ({target}): {e}")
            return None
