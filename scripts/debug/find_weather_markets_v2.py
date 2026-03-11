import os
import sys
import requests
from dotenv import load_dotenv

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.data.kalshi_provider import KalshiProvider

def find_markets_v2():
    load_dotenv(override=True)
    
    k_id = os.getenv("KALSHI_KEY_ID")
    k_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = os.getenv("KALSHI_API_URL")
    
    print(f"🕵️ V2 Search: Probing Weather Markets on {k_url}...")
    
    try:
        provider = KalshiProvider(k_id, k_path, k_url, read_only=True)
        if not provider.connect():
            print("❌ Connection Failed.")
            return
    except Exception as e:
        print(f"❌ Init Failed: {e}")
        return

    # Target Series
    targets = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "HIGHNY", "HIGHCHI"]
    
    for series in targets:
        print(f"\n--- Checking Series: {series} ---")
        try:
            # Minimal params per docs: just series_ticker
            params = {"series_ticker": series}
            
            # NOTE: We access the session directly to bypass any provider abstraction that might add unwanted headers/params
            # if we wanted to test pure public access, but we'll use the provider's session to keep auth if configured.
            # Actually, let's try WITHOUT auth headers first to test the 'Public' claim.
            
            url = f"{provider.api_url}/markets"
            resp = requests.get(url, params=params) # Pure Public Request
            
            if resp.status_code == 200:
                data = resp.json()
                markets = data.get('markets', [])
                if markets:
                    print(f" ✅ FOUND {len(markets)} MARKETS!")
                    # Find the one expiring soonest (today/tomorrow)
                    for m in markets[:5]:
                        print(f"    Ticker: {m['ticker']}")
                        print(f"    Title:  {m['title']}")
                        print(f"    Status: {m['status']}")
                        print(f"    Expire: {m['expiration_time']}")
                        print("-" * 20)
                else:
                    print("    (Response 200 OK, but empty market list)")
            else:
                print(f"    ❌ Failed: {resp.status_code}")
                # print(f"    Response: {resp.text[:200]}")
                
        except Exception as e:
            print(f"    ⚠️ Error: {e}")

if __name__ == "__main__":
    find_markets_v2()
