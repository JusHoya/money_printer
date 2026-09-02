"""Lane implementations: weather (READY) plus NOT_READY / NOT_PROMOTABLE stubs.

``ALL_LANES`` maps the registry name to the lane *class*; instantiate to
query ``status()``. Importing this package pulls no pandas: the weather
lane imports the evaluator chain lazily inside ``build_opportunities``.
"""
from __future__ import annotations

from typing import Dict, Type

from src.factory.lanes.base import (
    LANE_STATES,
    NOT_PROMOTABLE,
    NOT_READY,
    READY,
    SEARCH_FLOOR_UNITS,
    Lane,
    LaneStatus,
)
from src.factory.lanes.crypto_annual import CryptoAnnualLane
from src.factory.lanes.gas import GasLane
from src.factory.lanes.mention import MentionLane
from src.factory.lanes.tweets import TweetsLane
from src.factory.lanes.weather import WeatherLane

ALL_LANES: Dict[str, Type[Lane]] = {
    WeatherLane.name: WeatherLane,
    GasLane.name: GasLane,
    MentionLane.name: MentionLane,
    TweetsLane.name: TweetsLane,
    CryptoAnnualLane.name: CryptoAnnualLane,
}

__all__ = [
    "ALL_LANES",
    "CryptoAnnualLane",
    "GasLane",
    "LANE_STATES",
    "Lane",
    "LaneStatus",
    "MentionLane",
    "NOT_PROMOTABLE",
    "NOT_READY",
    "READY",
    "SEARCH_FLOOR_UNITS",
    "TweetsLane",
    "WeatherLane",
]
