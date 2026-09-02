"""Mention lane (KX*MENTION) -- NOT_READY stub (FACTORY_ARCHITECTURE section 4.1).

1,306 finalized markets / 42 events exist on Kalshi but the only quote tape
is the maia harvest since 2026-09-01 (post-cutoff, "looking" data) and no
historical quotes are attached to the settled record. Zero joinable
``event_ticker`` units. See ``reports/factory/coverage.json``.
"""
from __future__ import annotations

from typing import Any

from src.factory.lanes.base import NOT_READY, Lane, LaneStatus


class MentionLane(Lane):
    name = "mention"
    independent_unit = "event_ticker"

    def status(self) -> LaneStatus:
        return LaneStatus(
            state=NOT_READY,
            n_units=0,
            reason=(
                "no settleable quote substrate: maia mention tape starts 2026-09-01 "
                "(post-cutoff) and the 1,306 finalized markets carry no quotes; "
                "see reports/factory/coverage.json"
            ),
            next_data_eta=None,
        )

    def build_frames(self, config: Any):
        raise NotImplementedError("mention lane has no substrate (NOT_READY)")


__all__ = ["MentionLane"]
