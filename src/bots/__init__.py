"""Bot plugin system. Import all bot modules for auto-registration."""

from src.bots.btc_15m_bot import BTC15mBot
from src.bots.btc_hourly_bot import BTCHourlyBot
from src.bots.weather_bot import WeatherBot
from src.bots.crypto_15m_bot import Crypto15mBot  # registers eth/sol/doge/xrp_15m

__all__ = ["BTC15mBot", "BTCHourlyBot", "WeatherBot", "Crypto15mBot"]
