from src.core.interfaces import DataProvider, MarketData
from datetime import datetime
import random

class MockKalshiProvider(DataProvider):
    """Mock provider for Kalshi Market Data."""
    
    def connect(self) -> bool:
        print("[MockKalshi] Connected to simulated exchange.")
        return True
        
    def fetch_latest(self, symbol: str) -> MarketData:
        # Simulate a random walk price
        base_price = 0.50
        noise = random.uniform(-0.05, 0.05)
        price = base_price + noise
        
        return MarketData(
            symbol=symbol,
            timestamp=datetime.now(),
            price=round(price, 2),
            volume=random.randint(100, 1000),
            bid=round(price - 0.02, 2),
            ask=round(price + 0.02, 2),
            extra={"source": "mock_kalshi"}
        )

class MockNWSProvider(DataProvider):
    """Mock provider for Weather Data."""
    
    def connect(self) -> bool:
        print("[MockNWS] Connected to NWS Satellites (Simulated).")
        return True
        
    def fetch_latest(self, location: str) -> MarketData:
        # Simulate weather data wrapped in MarketData struct
        temp = random.randint(60, 90)
        precip_prob = random.random()
        
        return MarketData(
            symbol=location,
            timestamp=datetime.now(),
            price=precip_prob, # Treating probability as "price" for normalization
            volume=0,
            bid=0,
            ask=0,
            extra={
                "temperature": temp,
                "precipitation_probability": precip_prob,
                "source": "mock_nws"
            }
        )
