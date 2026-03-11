
import os
import sys
import requests
import json
import time

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.kalshi_provider import KalshiProvider

def deep_scan_series():
    print("--- Kalshi Deep Series Scanner ---")
    
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = "https://api.elections.kalshi.com/trade-api/v2" 
    
    print(f"Target URL: {k_url}")

    provider = KalshiProvider(k_id, k_key, k_url, read_only=True)
    if not provider.connect():
        print("Failed to connect.")
        return

    print("Scanning ALL active markets data to extract Series Tickers...")
    
    cursor = None
    all_series = set()
    total_markets = 0
    
    # Page limit usually ~100 pages is enough for all markets if limit=100 (10k markets)
    for i in range(200): 
        params = {"limit": 100} # Get EVERYTHING (active/closed doesn't matter, we want definitions)
        # Actually better to filter "active" to reduce noise? 
        # User wants to find the *currently tradable* hourly market.
        params["status"] = "active"
        
        if cursor: 
            params["cursor"] = cursor
            
        try:
            # Rate limit aggressive
            time.sleep(0.05)
            
            resp = provider.session.get(f"{k_url}/markets", params=params)
            if resp.status_code != 200:
                print(f"Error fetching page {i}: {resp.status_code}")
                break
                
            data = resp.json()
            markets = data.get('markets', [])
            
            if not markets:
                print("No more markets.")
                break
                
            total_markets += len(markets)
            print(f"Page {i}: +{len(markets)} markets (Total: {total_markets})")
            
            for m in markets:
                st = m.get('series_ticker', 'UNKNOWN')
                # print(f"  Found series: {st}") 
                all_series.add(st)

            cursor = data.get('cursor')
            if not cursor:
                break
            
        except Exception as e:
            print(f"Exception: {e}")
            break
            
    # Save to file
    sorted_series = sorted(list(all_series))
    print(f"\nScan Complete. Found {len(sorted_series)} unique series tickers.")
    
    with open("all_series_tickers.txt", "w") as f:
        for s in sorted_series:
            f.write(f"{s}\n")
            
    print("Saved to all_series_tickers.txt")
    
    # Quick filter print
    print("\n--- ALL UNIQUE SERIES TICKERS FOUND ---")
    for s in sorted_series:
        print(s)

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    deep_scan_series()
