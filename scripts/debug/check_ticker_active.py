
import os
import sys
import requests
import json
import time

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.kalshi_provider import KalshiProvider

def check_active():
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = "https://api.elections.kalshi.com/trade-api/v2" 
    
    target = "KXBTC15M"
    print(f"Checking for ACTIVE markets in series: {target} on {k_url}")

    provider = KalshiProvider(k_id, k_key, k_url, read_only=True)
    if not provider.connect():
        print("Failed to connect.")
        return

    # Remove status filter as it causes 400 on elections API?
    params = {"series_ticker": target, "limit": 100}
    try:
        resp = provider.session.get(f"{k_url}/markets", params=params)
        print(f"Status Code: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            markets = data.get('markets', [])
            print(f"Found {len(markets)} active markets.")
            for m in markets[:5]:
                print(f"  {m.get('ticker')} | {m.get('title')} | Expires: {m.get('expiration_time')}")
        else:
            print(f"Error: {resp.text}")
            
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    check_active()
