from typing import Dict, Type, List
from src.bots.base import Bot

class BotRegistry:
    """Plugin registry for trading bots."""
    _bots: Dict[str, Type[Bot]] = {}

    @classmethod
    def register(cls, name: str):
        """Decorator to register a bot class."""
        def wrapper(bot_cls):
            cls._bots[name] = bot_cls
            return bot_cls
        return wrapper

    @classmethod
    def create(cls, name: str, **kwargs) -> Bot:
        """Instantiate a registered bot by name."""
        if name not in cls._bots:
            available = ', '.join(cls._bots.keys())
            raise ValueError(f"Unknown bot '{name}'. Available: {available}")
        return cls._bots[name](**kwargs)

    @classmethod
    def create_all(cls, **kwargs) -> List[Bot]:
        """Instantiate all registered bots."""
        return [bot_cls(**kwargs) for bot_cls in cls._bots.values()]

    @classmethod
    def list_bots(cls) -> List[str]:
        """List all registered bot names."""
        return list(cls._bots.keys())
