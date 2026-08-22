import os
import sys
from dotenv import load_dotenv

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.data.kalshi_provider import KalshiProvider

def test_connection():
    load_dotenv(override=True)
    
    # 1. Load Credentials
    # Note: We expect the user to have updated .env with PROD credentials
    k_id = os.getenv("KALSHI_KEY_ID")
    k_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = os.getenv("KALSHI_API_URL")
    
    print("🔐 Checking Credentials...")
    print(f"   URL: {k_url}")
    print(f"   Key ID: {k_id}")
    print(f"   Key Path: {k_path}")
    
    if not (k_id and k_path and k_url):
        print("❌ Missing credentials in .env")
        return

    # 2. Initialize Provider in READ-ONLY Mode
    try:
        provider = KalshiProvider(k_id, k_path, k_url, read_only=True)
        print("   Provider Initialized (Mode: READ-ONLY)")
    except Exception as e:
        print(f"❌ Initialization Failed: {e}")
        return

    # 3. Test Connection
    if provider.connect():
        print("✅ Connection Successful!")
    else:
        print("❌ Connection Failed.")
        return

    # 4. Test Balance (Should work)
    bal = provider.get_balance()
    print(f"💰 Balance: ${bal:.2f}")

    # 5. Test Market Fetch (Should work)
    # Use a known active ticker if possible, or handle error
    ticker = "KXBTC" # Generic search might not work, need specific
    # Let's try to fetch a specific market if we knew one, 
    # but for now we just rely on connect() proving authentication worked.
    
    # 6. Test SAFETY (Trade Block)
    print("🛡️ Testing Safety Switch...")
    try:
        provider.place_order("TEST", "buy", 1, 0.50)
        print("❌ CRITICAL FAIL: Order was NOT blocked!")
    except RuntimeError as e:
        print(f"✅ SAFETY PASSED: {e}")

if __name__ == "__main__":
    test_connection()
