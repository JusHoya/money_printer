
import os
import sys
import requests
import time
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.data.kalshi_provider import KalshiProvider

def dump_responses():
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")

    # 1. Check Trading API Error
    url_prod = "https://trading-api.kalshi.com/trade-api/v2/exchange/status"
    print(f"Checking {url_prod}...")
    try:
        resp = requests.get(url_prod)
        with open("prod_error_full.txt", "w") as f:
            f.write(f"Status: {resp.status_code}\n")
            f.write(f"Body: {resp.text}\n")
        print("Saved prod_error_full.txt")
    except Exception as e:
        print(f"Prod Error: {e}")

    # 2. Check Elections API Markets
    url_el = "https://api.elections.kalshi.com/trade-api/v2"
    print(f"Checking {url_el}/markets...")
    provider = KalshiProvider(k_id, k_key, url_el, read_only=True)
    
    # We need to sign this if we want to mimic the app
    # But for public markets, maybe we don't need auth?
    # Let's try auth first since we have keys
    
    try:
        full_url = f"{url_el}/markets"
        timestamp = str(int(time.time() * 1000))
        # path for signing is /markets
        sig = provider._sign_request("GET", "/markets", timestamp)
        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": k_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": timestamp
        }
        
        # Get first 200 markets
        params = {"limit": 200}
        resp = requests.get(full_url, headers=headers, params=params)
        
        with open("elections_markets_dump.json", "w") as f:
             json.dump(resp.json(), f, indent=2)
        print("Saved elections_markets_dump.json")

    except Exception as e:
        print(f"Elections Error: {e}")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    dump_responses()
