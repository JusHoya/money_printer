import requests
from datetime import datetime
from typing import Dict, Any, Optional
from src.core.interfaces import DataProvider, MarketData

class CoinbaseProvider(DataProvider):
    """
    Live Data Provider for Coinbase (Crypto Prices).
    API Docs: https://docs.cloud.coinbase.com/exchange/reference/exchangerestapi_getproductticker
    """
    
    BASE_URL = "https://api.exchange.coinbase.com"
    
    def __init__(self, product_id: str = "BTC-USD"):
        self.product_id = product_id
        
    def connect(self) -> bool:
        """
        Verifies connection to Coinbase API.
        """
        print(f"[CoinbaseProvider] Connecting to {self.BASE_URL} for {self.product_id}...")
        try:
            url = f"{self.BASE_URL}/products/{self.product_id}/ticker"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            print(f"[CoinbaseProvider] Connection Successful. Price: {resp.json().get('price')}")
            return True
        except Exception as e:
            print(f"[CoinbaseProvider] Connection Failed: {e}")
            return False

    def fetch_latest(self, symbol: str = None) -> MarketData:
        """
        Fetches current ticker price.
        """
        target = symbol if symbol else self.product_id
        url = f"{self.BASE_URL}/products/{target}/ticker"
        
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            
            price = float(data.get('price', 0))
            
            return MarketData(
                symbol=target,
                timestamp=datetime.now(),
                price=price,
                volume=float(data.get('volume', 0)),
                bid=float(data.get('bid', 0)),
                ask=float(data.get('ask', 0)),
                extra={
                    "source": "live_coinbase",
                    "time": data.get('time')
                }
            )
            
        except Exception as e:
            print(f"[CoinbaseProvider] Fetch Error: {e}")
            return None
