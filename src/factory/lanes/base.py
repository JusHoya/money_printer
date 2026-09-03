"""``Lane`` ABC -- one per market family the factory can search (FACTORY_ARCHITECTURE section 1.1).

A lane names its *independent unit* (the thing a date-clustered bootstrap
resamples: ``target_date`` for weather, ``settlement_date`` for gas,
``event_ticker`` for mention), reports its readiness, and builds the
``FrameSet`` the evolutionary search runs on.

Readiness is one of three states:

* ``READY`` -- a settlement-true frame exists with at least
  :data:`SEARCH_FLOOR_UNITS` independent units (PRD FR-F1 / board rule).
* ``NOT_PROMOTABLE`` -- a frame could be built but the unit count is below
  the floor; it may be reported, never promoted.
* ``NOT_READY`` -- no settleable substrate yet (feed-only harvesters).

This module carries no heavy imports so the lane *board* can be produced
without pandas or the evaluator chain.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

READY = "READY"
NOT_PROMOTABLE = "NOT_PROMOTABLE"
NOT_READY = "NOT_READY"
LANE_STATES = (READY, NOT_PROMOTABLE, NOT_READY)

#: Minimum independent units before ``factory.py run`` will search a lane.
SEARCH_FLOOR_UNITS = 40


@dataclass(frozen=True)
class LaneStatus:
    state: str
    n_units: int
    reason: str
    #: ISO date when more data is expected, if known (coverage board column).
    next_data_eta: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in LANE_STATES:
            raise ValueError(f"unknown lane state {self.state!r}")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Lane(ABC):
    """A searchable market family."""

    #: Registry key (``weather``, ``gas``, ``mention``, ``tweets``, ``crypto_annual``).
    name: str = ""
    #: Column the bootstrap clusters on.
    independent_unit: str = ""

    @abstractmethod
    def status(self) -> LaneStatus:
        """Cheap readiness probe: no frame build, no pandas."""

    @abstractmethod
    def build_frames(self, config: Any):
        """Build the lane's ``FrameSet`` (parity, search, gefs_twin). Lab-only."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r} unit={self.independent_unit!r}>"


__all__ = [
    "LANE_STATES",
    "Lane",
    "LaneStatus",
    "NOT_PROMOTABLE",
    "NOT_READY",
    "READY",
    "SEARCH_FLOOR_UNITS",
]
