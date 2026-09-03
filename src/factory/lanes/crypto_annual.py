"""Crypto annual lane (KXBTCY/KXETHY) -- NOT_READY stub (FACTORY_ARCHITECTURE section 4.1).

One settlement (2027-01-01) and no truth: "NEVER a lane" per the coverage
matrix. The harvester is feed-only and the API-reported zero fee multiplier
is unverified (``configs/fees/fee_regime.csv`` pins it at 1).
"""
from __future__ import annotations

from typing import Any

from src.factory.lanes.base import NOT_READY, Lane, LaneStatus


class CryptoAnnualLane(Lane):
    name = "crypto_annual"
    independent_unit = "event_ticker"

    def status(self) -> LaneStatus:
        return LaneStatus(
            state=NOT_READY,
            n_units=0,
            reason=(
                "single settlement event (2027-01-01) and no truth: never a searchable "
                "lane; fee multiplier 0 unverified (pinned 1); see reports/factory/coverage.json"
            ),
            next_data_eta="2027-01-01",
        )

    def build_frames(self, config: Any):
        raise NotImplementedError("crypto_annual is not a searchable lane (NOT_READY)")


__all__ = ["CryptoAnnualLane"]
