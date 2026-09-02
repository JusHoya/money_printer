"""Tweets lane (Kalshi X-settled series) -- NOT_READY stub (FACTORY_ARCHITECTURE section 4.1).

233 harvest tape rows, one live market, no truth join. Zero joinable units.
See ``reports/factory/coverage.json``.
"""
from __future__ import annotations

from typing import Any

from src.factory.lanes.base import NOT_READY, Lane, LaneStatus


class TweetsLane(Lane):
    name = "tweets"
    independent_unit = "event_ticker"

    def status(self) -> LaneStatus:
        return LaneStatus(
            state=NOT_READY,
            n_units=0,
            reason=(
                "233 feed-only tape rows, a single live X-settled market and no "
                "truth join; see reports/factory/coverage.json"
            ),
            next_data_eta=None,
        )

    def build_frames(self, config: Any):
        raise NotImplementedError("tweets lane has no substrate (NOT_READY)")


__all__ = ["TweetsLane"]
