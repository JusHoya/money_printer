
import os
import sys
import requests
import json
import time
import logging

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.utils.logger import logger
from src.data.kalshi_provider import KalshiProvider

# Add StreamHandler to the existing logger to see output in console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

def debug_btc_markets():
    print("--- Kalshi Market Scanner ---")
    
    # Load env vars
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    # Default to the one that works (elections)
    k_url = "https://api.elections.kalshi.com/trade-api/v2" 
    
    print(f"Target URL: {k_url}")

    if not k_id or not k_key:
        print("Error: KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PATH not set.")
        return

    provider = KalshiProvider(k_id, k_key, k_url, read_only=True)
    
    if not provider.connect():
        print("Failed to connect to Kalshi.")
        return

    print("Scanning ALL active markets (pagination)...")
    
    cursor = None
    btc_found = False
    all_series = set()
    
    for i in range(5):
        # Remove status filter to see EVERYTHING
        params = {"limit": 100} 
        if cursor: 
            params["cursor"] = cursor
            
        try:
            # Rate limit
            time.sleep(0.1)
            
            resp = provider.session.get(f"{k_url}/markets", params=params)
            if resp.status_code != 200:
                print(f"Error fetching page {i}: {resp.status_code} - {resp.text}")
                break
                
            data = resp.json()
            markets = data.get('markets', [])
            
            if not markets:
                print(f"Page {i}: Markets list is EMPTY.")
                print(f"Response Keys: {data.keys()}")
                print(f"Sample Data: {str(data)[:200]}")
                break
                
            print(f"Page {i}: Scanned {len(markets)} markets...")
            
            for m in markets:
                t = m.get('ticker', '')
                st = m.get('series_ticker', '')
                cat = m.get('category', '').lower()
                title = m.get('title', '').lower()
                
                all_series.add(st)
                
                # Check for Crypto/BTC
                is_target = 'btc' in t.lower() or 'bitcoin' in title or 'crypto' in cat or 'eth' in t.lower()
                
                if is_target:
                    print(f"!!! FOUND MATCH !!!")
                    print(f"  Ticker: {t}")
                    print(f"  Series: {st}")
                    print(f"  Category: {cat}")
                    print(f"  Title: {m.get('title')}")
                    print(f"  Status: {m.get('status')}")
                    print("-" * 20)
                    btc_found = True

            cursor = data.get('cursor')
            if not cursor:
                print("End of pagination.")
                break
            
        except Exception as e:
            print(f"Exception: {e}")
            break
            
    if not btc_found:
        print("\nSUMMARY: No BTC/Crypto markets found in scan.")
        print(f"Total Unique Series found: {len(all_series)}")
        # Print a sample of series to see what we DID find
        print("Sample of Series Tickers found:")
        print(list(all_series)[:20])
    
    return

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    debug_btc_markets()
