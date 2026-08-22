
import os
import sys
import requests
import json
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.utils.logger import logger

# Console logging
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

from src.data.kalshi_provider import KalshiProvider

def debug_series():
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    url = "https://api.elections.kalshi.com/trade-api/v2"
    
    provider = KalshiProvider(k_id, k_key, url, read_only=True)
    provider.connect()
    
    print(f"Fetching /series from {url}...")
    try:
        # GET /series
        # Pagination usually supported
        resp = provider.session.get(f"{url}/series")
        print(f"Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            series_list = data.get('series', [])
            print(f"Found {len(series_list)} series definitions.")
            
            btc_series = [s for s in series_list if 'BTC' in s.get('ticker', '').upper() or 'CRYPTO' in s.get('title', '').upper()]
            
            if btc_series:
                print("!!! FOUND BTC SERIES !!!")
                for s in btc_series:
                    print(f"Ticker: {s.get('ticker')} | Title: {s.get('title')}")
            else:
                print("No BTC series found in definition list.")
                
            # Dump all tickers for manual check
            with open("all_series_definitions.json", "w") as f:
                json.dump(series_list, f, indent=2)
            print("Saved definitions to all_series_definitions.json")
            
        else:
            print(f"Error: {resp.text}")
            
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    debug_series()
