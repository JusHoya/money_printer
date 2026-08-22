import requests
import sys

ENDPOINTS = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
    "https://api.kalshi.com/trade-api/v2",
    "https://kalshi.com/trade-api/v2",
    "https://kalshi.com/api/v2",
    "https://demo-api.kalshi.co/trade-api/v2"
]

def test_endpoints():
    print("--- Testing Kalshi API Endpoints ---")
    sys.stdout.flush()
    
    for url in ENDPOINTS:
        try:
            full_url = f"{url}/exchange/status"
            print(f"Testing {url} ... ", end="")
            sys.stdout.flush()
            
            resp = requests.get(full_url, timeout=5)
            print(f"[{resp.status_code}]")
            
            if resp.status_code == 200:
                print(f"  > ACTIVE: {resp.text[:50]}...")
        except Exception as e:
            print(f"[FAILED] {e}")
        sys.stdout.flush()

if __name__ == "__main__":
    test_endpoints()
