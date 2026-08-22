import requests
import json
import os
from datetime import datetime
import pytz

# Kalshi API Endpoint (Elections/Public)
API_URL = "https://api.elections.kalshi.com/trade-api/v2"

def list_btc_markets():
    print(f"--- Scanning {API_URL} for Bitcoin Markets ---")
    
    # 1. Fetch ALL Active Markets (paginate)
    # We can't rely on 'series_ticker' if we don't know it.
    # We'll filter client-side.
    
    found_markets = []
    cursor = None
    
    while True:
        params = {
            "limit": 100
            # "status": "active" # Removed to avoid 400 error?
        }
        if cursor:
            params["cursor"] = cursor
            
        try:
            resp = requests.get(f"{API_URL}/markets", params=params)
            
            if resp.status_code != 200:
                print(f"Error {resp.status_code}: {resp.text}")
                break
                
            data = resp.json()
            
            markets = data.get("markets", [])
            cursor = data.get("cursor")
            
            print(f"Fetched {len(markets)} markets... (Cursor: {cursor is not None})")
            
            for m in markets:
                # FILTER LOGIC
                # 1. Check Title
                title = m.get("title", "").lower()
                ticker = m.get("ticker", "").lower()
                subtitle = m.get("subtitle", "").lower()
                close_time_str = m.get("close_time", "")
                
                is_match = False
                match_reason = ""
                
                # A. Keyword Match
                if "bitcoin" in title or "btc" in ticker or "bitcoin" in subtitle:
                    is_match = True
                    match_reason = "KEYWORD"
                    
                # B. Timestamp Match (User supplied)
                # 2026-02-17T05:00:00Z (12am EST)
                # 2026-02-17T22:00:00Z (5pm EST)
                if "2026-02-17T05:00:00Z" in close_time_str or \
                   "2026-02-17T22:00:00Z" in close_time_str or \
                   "2026-02-20T22:00:00Z" in close_time_str:
                    is_match = True
                    match_reason = f"TIMESTAMP ({close_time_str})"
                
                if is_match:
                    print(f"\n[!] MATCH FOUND ({match_reason}): {ticker}")
                    print(f"    Title: {m.get('title')}")
                    print(f"    Close: {close_time_str}")
                    print(f"    Series: {m.get('series_ticker')}")
                    found_markets.append(m)
                    
            if not cursor:
                break
                
            # Safety Limit
            # if len(found_markets) > 50: break # removed to scan deep
            
        except Exception as e:
            print(f"Error fetching markets: {e}")
            break
            
    print(f"\n--- Found {len(found_markets)} Bitcoin-related Markets ---")

if __name__ == "__main__":
    list_btc_markets()
