
import os
import sys
import requests
import time
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.data.kalshi_provider import KalshiProvider

def check_prod_error():
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    url = "https://trading-api.kalshi.com/trade-api/v2"

    print(f"Checking {url}...")
    provider = KalshiProvider(k_id, k_key, url, read_only=True)
    
    try:
        if not provider.connect():
            print("Connect failed (as expected).")
            # Manually fetch status to get body
            path = "/exchange/status"
            timestamp = str(int(time.time() * 1000))
            sig = provider._sign_request("GET", path, timestamp)
            headers = {
                "Content-Type": "application/json",
                "KALSHI-ACCESS-KEY": k_id,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": timestamp
            }
            resp = requests.get(f"{url}{path}", headers=headers)
            print(f"STATUS: {resp.status_code}")
            print(f"BODY: {resp.text}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    check_prod_error()
