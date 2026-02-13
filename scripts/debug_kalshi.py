import os
import sys
from dotenv import load_dotenv

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.kalshi_provider import KalshiProvider

def debug_balance():
    load_dotenv()
    print("\n--- Debugging Kalshi Balance ---")
    
    key_id = os.getenv("KALSHI_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    api_url = os.getenv("KALSHI_API_URL", "https://demo-api.kalshi.co/trade-api/v2")

    if not key_id or not key_path:
        print("Missing credentials.")
        return

    kalshi = KalshiProvider(key_id=key_id, private_key_path=key_path, api_url=api_url)
    if kalshi.connect():
        print("Attempting to fetch balance...")
        bal = kalshi.get_balance()
        print(f"Result: ${bal:.2f}")

if __name__ == "__main__":
    debug_balance()
