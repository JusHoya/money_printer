import requests
import json
import os

# User provided URL: https://api.elections.kalshi.com/v1/series/KXBTCD/events/KXBTCD-26FEB1700
# We will verify this and also check if we can list ALL events for that series.

BASE_V1_URL = "https://api.elections.kalshi.com/v1"

def inspect_event():
    event_ticker = "KXBTCD-26FEB1700"
    url = f"{BASE_V1_URL}/series/KXBTCD/events/{event_ticker}"
    
    print(f"--- Querying V1 API: {url} ---")
    try:
        resp = requests.get(url)
        print(f"Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            
            with open("btc_event_data.json", "w") as f:
                json.dump(data, f, indent=2)
            print("Saved event data to btc_event_data.json")
            
            # Extract Markets
            markets = data.get("markets", [])
            print(f"\nFound {len(markets)} markets in this event.")
            for m in markets:
                print(f"  Ticker: {m.get('ticker')}")
                print(f"  Status: {m.get('status')}")
                print(f"  Title:  {m.get('title')}")
                print(f"  Strike: {m.get('floor_strike')} - {m.get('cap_strike')} (Rule: {m.get('strike_type')})")
                print("-" * 30)
                
            # Check Series Metadata if available
            series = data.get("series", {})
            print(f"\nSeries Info: {series}")
            
        else:
            print(f"Error: {resp.text}")
            
    except Exception as e:
        print(f"Exception: {e}")

    # Also try to list ALL events for the series?
    print(f"\n--- Querying Series Index: {BASE_V1_URL}/series/KXBTCD/events ---")
    try:
        list_url = f"{BASE_V1_URL}/series/KXBTCD/events"
        resp = requests.get(list_url)
        if resp.status_code == 200:
            events = resp.json().get("events", [])
            print(f"Found {len(events)} total events for KXBTCD.")
            
            # Filter for active?
            for e in events[:5]: # Show first 5
                print(f"  Event: {e.get('event_ticker')} | Date: {e.get('date')}")
        else:
             print(f"List Error: {resp.status_code}")
    except Exception as e:
        print(f"List Exception: {e}")

if __name__ == "__main__":
    inspect_event()
