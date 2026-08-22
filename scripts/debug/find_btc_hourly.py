
import os
import sys
import time
import json
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.kalshi_provider import KalshiProvider
from dotenv import load_dotenv


import os
import sys
import time
import json
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.kalshi_provider import KalshiProvider
from dotenv import load_dotenv

def verify_btc_ticker():
    load_dotenv(override=True)
    print("--- Kalshi Link Verifier ---")
    
    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    
    # TRY THE API.KALSHI.CO (dot CO)
    k_url = "https://api.kalshi.co/trade-api/v2" 
    
    print(f"Target URL: {k_url}")

    # Try AUTHENTICATED to see why it fails
    # provider = KalshiProvider(key_id=None, private_key_path=None, api_url=k_url, read_only=True)
    provider = KalshiProvider(k_id, k_key, k_url, read_only=True)
    
    print("Attempting Authenticated Connection...")
    # Manual connect check to print error
    try:
        url = f"{k_url}/exchange/status"
        headers = {"Content-Type": "application/json"}
        timestamp = str(int(time.time() * 1000))
        path = "/exchange/status"
        signature = provider._sign_request("GET", path, timestamp)
        headers.update({
            "KALSHI-ACCESS-KEY": provider.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp
        })
        
        resp = provider.session.get(url, headers=headers)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        
    except Exception as e:
        print(f"Connection Exception: {e}")

    # User provided: kxbtcd-26feb1623



    # User provided: kxbtcd-26feb1623
    # Format appears to be: kxbtcd-YYMMMDDHH
    target_ticker = "kxbtcd-26feb1623"
    
    print(f"Checking {target_ticker} on {k_url} ...")
    
    try:
        resp = provider.session.get(f"{k_url}/markets/{target_ticker}")
        
        if resp.status_code == 200:
            print("\n!!! FOUND !!!")
            data = resp.json()
            m = data.get('market', {})
            print(f"  Ticker: {m.get('ticker')}")
            print(f"  Series: {m.get('series_ticker')}")
            print(f"  Title:  {m.get('title')}")
            print(f"  Status: {m.get('status')}")
            
            # If found, this is the solution.
            
        else:
            print(f"Not Found ({resp.status_code})")
            print(f"Response: {resp.text}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    verify_btc_ticker()
