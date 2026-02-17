import requests

BASE_V1_URL = "https://api.elections.kalshi.com/v1"

# Target: 5pm EST on Feb 17, 2026.
# 5pm EST = 17:00 EST. 
# 5pm EST = 22:00 UTC.

CANDIDATES = [
    "KXBTCD-26FEB1717", # 17:00 EST?
    "KXBTCD-26FEB1722", # 22:00 UTC?
    "KXBTCD-26FEB1705", # 05:00 UTC (Next day)? (That was the 12am one: 1700)
    "KXBTCD-26FEB1716", # 4pm?
    "KXBTCD-26FEB1712", # noon?
]

def probe_tickers():
    print("--- Probing Event Tickers ---")
    for ticker in CANDIDATES:
        url = f"{BASE_V1_URL}/series/KXBTCD/events/{ticker}"
        try:
            resp = requests.get(url, timeout=2)
            print(f"Testing {ticker} -> {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                event = data.get("event")
                if event:
                    print(f"  [FOUND] Ticker: {event.get('ticker')}")
                    print(f"  [FOUND] Title:  {event.get('title')}")
                    print(f"  [FOUND] Date:   {event.get('target_datetime')}")
                    print(f"  [FOUND] Mkts:   {len(event.get('markets', []))}")
                else:
                     print(f"  [EMPTY] Status 200 but no event data.")

        except Exception as e:
            print(f"  [ERROR] {ticker}: {e}")

if __name__ == "__main__":
    probe_tickers()
