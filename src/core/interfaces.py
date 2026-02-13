from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class MarketData:
    """Standardized container for market information."""
    symbol: str
    timestamp: datetime
    price: float
    volume: float
    bid: float
    ask: float
    extra: Dict[str, Any] = None

@dataclass
class TradeSignal:
    """Output from a strategy indicating intent."""
    symbol: str
    side: str  # 'buy' or 'sell'
    quantity: int
    limit_price: Optional[float] = None
    confidence: float = 0.0  # 0.0 to 1.0

class DataProvider(ABC):
    """Interface for fetching data (Market, Weather, etc)."""
    
    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to source."""
        pass
        
    @abstractmethod
    def fetch_latest(self, symbol: str) -> MarketData:
        """Get the most recent data point."""
        pass

class Strategy(ABC):
    """Interface for Trading Logic."""
    
    @abstractmethod
    def analyze(self, data: MarketData) -> Optional[TradeSignal]:
        """Process data and potentially return a trade signal."""
        pass

    @abstractmethod
    def name(self) -> str:
        """Strategy name."""
        pass

class ExecutionEngine(ABC):
    """Interface for executing trades."""
    
    @abstractmethod
    def execute(self, signal: TradeSignal) -> bool:
        """Execute the trade signal."""
        pass
