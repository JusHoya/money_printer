"""Quick test: Does Kalshi API reject status=active param?"""
import requests

API = "https://api.elections.kalshi.com/trade-api/v2"

# Test 1: With status filter
print("=== Test 1: series_ticker + status=active ===")
r1 = requests.get(f"{API}/markets", params={"series_ticker": "KXBTC15M", "status": "active"}, timeout=10)
print(f"HTTP {r1.status_code}")
if r1.status_code != 200:
    print(f"Response: {r1.text[:500]}")
else:
    print(f"Markets: {len(r1.json().get('markets', []))}")

# Test 2: Without status filter
print("\n=== Test 2: series_ticker only (no status) ===")
r2 = requests.get(f"{API}/markets", params={"series_ticker": "KXBTC15M", "limit": 200}, timeout=10)
print(f"HTTP {r2.status_code}")
if r2.status_code == 200:
    markets = r2.json().get("markets", [])
    print(f"Total: {len(markets)}")
    active = [m for m in markets if m.get("status") == "active"]
    print(f"Active (client-side): {len(active)}")
    for m in active[:3]:
        print(f"  {m['ticker']} | exp={m.get('expiration_time','?')}")

# Test 3: With cursor pagination + no status
print("\n=== Test 3: Paginated (no status filter) ===")
cursor = None
all_active = []
page = 0
while page < 10:
    params = {"series_ticker": "KXBTC15M", "limit": 200}
    if cursor:
        params["cursor"] = cursor
    r = requests.get(f"{API}/markets", params=params, timeout=10)
    data = r.json()
    markets = data.get("markets", [])
    for m in markets:
        if m.get("status") == "active":
            all_active.append(m)
    cursor = data.get("cursor")
    page += 1
    if not cursor or not markets:
        break
print(f"Total pages: {page}")
print(f"Active found: {len(all_active)}")
for m in all_active[:5]:
    print(f"  {m['ticker']} | status={m['status']} | exp={m.get('expiration_time','?')}")
