import os
import sys
import requests
from dotenv import load_dotenv

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.data.kalshi_provider import KalshiProvider

def find_markets():
    load_dotenv(override=True)
    
    k_id = os.getenv("KALSHI_KEY_ID")
    k_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = os.getenv("KALSHI_API_URL")
    
    print(f"🕵️ Searching for Weather Markets on {k_url}...")
    
    # Initialize Provider (ReadOnly)
    try:
        provider = KalshiProvider(k_id, k_path, k_url, read_only=True)
        if not provider.connect():
            print("❌ Connection Failed. Cannot search.")
            return
    except Exception as e:
        print(f"❌ Init Failed: {e}")
        return

    # Known Weather Series Tickers
    from datetime import datetime, timedelta
    
    # Generate Date Strings (e.g., 26JAN29)
    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    
    date_strs = [
        today.strftime("%y%b%d").upper(),
        tomorrow.strftime("%y%b%d").upper()
    ]
    
    print(f"\n--- DATE-BASED MARKET SEARCH ({date_strs}) ---")
    
    try:
        # Fetch ALL active markets
        params = {"status": "active", "limit": 1000}
        resp = provider.session.get(f"{provider.api_url}/markets", params=params)
        
        if resp.status_code == 200:
            all_markets = resp.json().get('markets', [])
            print(f"Scanned {len(all_markets)} total active markets.")
            
            found_count = 0
            for m in all_markets:
                # Check for Date Match AND Weather Keyword
                ticker = m.get('ticker', '')
                title = m.get('title', '')
                
                has_date = any(d in ticker for d in date_strs)
                is_weather = "HIGH" in ticker or "TEMP" in ticker or "PRECIP" in ticker or "Weather" in title
                
                if has_date or is_weather:
                    print(f"  MATCH: {ticker} | {title} | Last: {m.get('last_price')}")
                    found_count += 1
                    
            if found_count == 0:
                print("  (No date-matched weather markets found)")
        else:
            print(f"  ❌ Bulk fetch failed: {resp.status_code}")
            
    except Exception as e:
        print(f"  ❌ Error: {e}")
        print("")

if __name__ == "__main__":
    find_markets()
