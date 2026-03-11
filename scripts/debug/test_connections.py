import os
import sys
import requests
from dotenv import load_dotenv

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.nws_provider import NWSProvider
# from src.data.kalshi_provider import KalshiProvider # Commented out until user has keys

def test_nws():
    print("\n--- Testing NWS Provider ---")
    user_agent = os.getenv("NWS_USER_AGENT", "(TestBot, test@example.com)")
    station = os.getenv("NWS_STATION_ID", "KJFK")
    
    # DEBUG: Direct API Call to see structure
    print(f"DEBUG: Fetching {station} info...")
    try:
        r = requests.get(f"https://api.weather.gov/stations/{station}", headers={"User-Agent": user_agent})
        print(f"DEBUG keys: {r.json().get('properties', {}).keys()}")
    except Exception as e:
        print(e)

    nws = NWSProvider(user_agent=user_agent, station_id=station)
    # Debug: print what NWS provider sees
    if nws.connect():
        data = nws.fetch_latest()
        if data:
            print(f"SUCCESS: Fetched Weather for {data.symbol}")
            print(f"Temp: {data.extra['temperature_f']} F")
            print(f"Condition: {data.extra['description']}")
        else:
            print("FAILURE: Could not fetch data.")
    else:
        print("FAILURE: Could not connect.")

def test_kalshi():
    print("\n--- Testing Kalshi Provider ---")
    key_id = os.getenv("KALSHI_KEY_ID")
    # Support both path or direct key content
    key_input = os.getenv("KALSHI_PRIVATE_KEY") or os.getenv("KALSHI_PRIVATE_KEY_PATH")
    api_url = os.getenv("KALSHI_API_URL", "https://demo-api.kalshi.co/trade-api/v2")

    if not key_id or "your_key" in key_id:
        print("SKIPPING: KALSHI_KEY_ID not set correctly in .env")
        return
    
    if not key_input:
        print("FAILURE: Neither KALSHI_PRIVATE_KEY nor KALSHI_PRIVATE_KEY_PATH found in .env")
        return

    from src.data.kalshi_provider import KalshiProvider
    
    kalshi = KalshiProvider(key_id=key_id, private_key_path=key_input, api_url=api_url)
    # Try to fetch list of markets to see ticker format
    print("Attempting to list active markets to discover ticker format...")
    path = "/markets"
    url = f"{api_url}{path}"
    # Minimal signature for base path
    import time
    timestamp = str(int(time.time() * 1000))
    signature = kalshi._sign_request("GET", path, timestamp)
    
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }
    
    try:
        resp = requests.get(url, headers=headers, params={"limit": 100})
        if resp.status_code == 200:
            markets = resp.json().get('markets', [])
            print(f"Fetched {len(markets)} markets. Searching for weather...")
            weather_markets = [m for m in markets if 'rain' in m.get('title', '').lower() or 'precip' in m.get('ticker', '').lower()]
            
            if weather_markets:
                for m in weather_markets:
                    print(f"- {m.get('ticker')}: {m.get('title')}")
            else:
                print("No obvious weather markets found in the first 100. Dumping 3 random ones:")
                import random
                for m in random.sample(markets, min(3, len(markets))):
                    print(f"- {m.get('ticker')}: {m.get('title')}")

        else:
            print(f"Listing failed: {resp.status_code} - {resp.text}")
            
    except Exception as e:
        print(f"List Error: {e}")

if __name__ == "__main__":
    load_dotenv()
    test_nws()
    test_kalshi()
