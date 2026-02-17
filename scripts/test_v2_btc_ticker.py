import requests
import os

# Test if we can fetch a specific KXBTCD market via V2 API
TICKER_TO_TEST = "KXBTCD-26FEB1700-T60999.99"
API_URL = "https://api.elections.kalshi.com/trade-api/v2"

def test_v2_fetch():
    print(f"--- Testing V2 Fetch for {TICKER_TO_TEST} ---")
    url = f"{API_URL}/markets/{TICKER_TO_TEST}"
    
    try:
        resp = requests.get(url)
        print(f"Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            m = data.get("market", {})
            print("SUCCESS! V2 returned data:")
            print(f"  Ticker: {m.get('ticker')}")
            print(f"  Last Price: {m.get('last_price')}")
            print(f"  Status: {m.get('status')}")
        else:
            print(f"Error: {resp.text}")
            
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_v2_fetch()
