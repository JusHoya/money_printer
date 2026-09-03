"""Gas lane (KXAAAGASM/W) -- NOT_PROMOTABLE stub (FACTORY_ARCHITECTURE section 4.1).

The substrate exists (``reports/phase4/gas_quote_tape.csv``: 39,623 rows,
405 markets, 14 settlement events; AAA truth to 2026-07-29) but 14
independent ``settlement_date`` units is far under the 40-unit search
floor. F5 wraps ``scripts/gas_backtest.py`` with ``clock=`` and ``gate=``
injection; until then this lane only reports.
"""
from __future__ import annotations

from typing import Any

from src.factory.lanes.base import NOT_PROMOTABLE, SEARCH_FLOOR_UNITS, Lane, LaneStatus

#: Settlement events in the Phase-4 tape (architecture section 4.1).
GAS_SETTLEMENT_EVENTS = 14


class GasLane(Lane):
    name = "gas"
    independent_unit = "settlement_date"

    def status(self) -> LaneStatus:
        return LaneStatus(
            state=NOT_PROMOTABLE,
            n_units=GAS_SETTLEMENT_EVENTS,
            reason=(
                f"{GAS_SETTLEMENT_EVENTS} settlement events in reports/phase4/gas_quote_tape.csv "
                f"< {SEARCH_FLOOR_UNITS}-unit search floor; Phase 4 HALT stands; "
                "F5 lane (gas_backtest.py replay with clock=/gate= injection)"
            ),
            next_data_eta=None,
        )

    def build_frames(self, config: Any):
        raise NotImplementedError(
            "gas lane frames are an F5 deliverable (NOT_PROMOTABLE today)"
        )


__all__ = ["GAS_SETTLEMENT_EVENTS", "GasLane"]
