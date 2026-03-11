import os
import sys
import requests
from dotenv import load_dotenv
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.data.kalshi_provider import KalshiProvider
from datetime import datetime

def diagnose():
    load_dotenv(override=True)
    k_id = os.getenv("KALSHI_KEY_ID")
    k_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = os.getenv("KALSHI_API_URL")

    print(f"Skippy Diagnostics: Probing Crypto Markets on {k_url}...")
    
    provider = KalshiProvider(k_id, k_path, k_url, read_only=True)
    if not provider.connect():
        print("❌ Auth Failed.")
        return

    targets = ["KXBTC15M", "KXBTC"]
    
    for series in targets:
        print(f"\n--- Checking Series: {series} ---")
        params = {"series_ticker": series}
        resp = provider.session.get(f"{provider.api_url}/markets", params=params)
        
        if resp.status_code == 200:
            markets = resp.json().get('markets', [])
            print(f"Found {len(markets)} active markets.")
            for m in markets:
                print(f"  Ticker: {m['ticker']} | Opens: {m['open_time']} | Expires: {m['expiration_time']} | Status: {m.get('status')}")
        else:
            print(f"Error {resp.status_code}: {resp.text}")

if __name__ == "__main__":
    diagnose()
