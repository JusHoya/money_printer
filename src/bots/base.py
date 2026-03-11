from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from src.core.interfaces import Strategy, TradeSignal, MarketData

class Bot(ABC):
    """Base class for all trading bots. Each bot targets a specific market."""

    def __init__(self, name: str):
        self.name = name
        self.strategies: Dict[str, Strategy] = {}

    @abstractmethod
    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        """Initialize providers and resolve initial tickers."""
        pass

    @abstractmethod
    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        """Execute one iteration of the bot's trading loop. Returns raw signals (not yet risk-checked)."""
        pass

    @abstractmethod
    def get_symbols(self) -> List[str]:
        """Return list of symbols this bot trades."""
        pass

    def add_strategy(self, key: str, strategy: Strategy):
        """Register a strategy with this bot."""
        self.strategies[key] = strategy
