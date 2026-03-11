"""Bot plugin system. Import all bot modules for auto-registration."""
from src.bots.btc_15m_bot import BTC15mBot
from src.bots.btc_hourly_bot import BTCHourlyBot
from src.bots.weather_bot import WeatherBot

__all__ = ['BTC15mBot', 'BTCHourlyBot', 'WeatherBot']
