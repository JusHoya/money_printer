import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.bots.weather_bot import CITY_CONFIG
from src.data.nws_provider import NWSProvider


def probe_nws_history():
    load_dotenv()
    ua = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
    # PRD FR-1.4: probe the SETTLEMENT station (city registry), not an airport.
    station = CITY_CONFIG["NY"].settlement_station
    nws = NWSProvider(ua, station)

    print("--- Probing NWS History for Daily High ---")
    data = nws.fetch_latest(station)

    if data:
        print(f"Station: {data.extra.get('station_name')}")
        print(f"Current Temp: {data.extra.get('temperature_f')} F")
        print(f"Daily High So Far: {data.extra.get('max_temp_today_f')} F")

        # Verify if max_temp_today_f makes sense
        # If it's equal to current temp, maybe no history match?
    else:
        print("Fetch failed.")


if __name__ == "__main__":
    probe_nws_history()
