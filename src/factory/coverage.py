"""Lane coverage board: ``reports/factory/coverage.json`` (FACTORY_ARCHITECTURE section 1.1, 4.1).

One entry per registered lane -- state, independent units vs the 40-unit
search floor, the reason, and the next data ETA where one is known. The
content is **timestamp-free** so a byte-hash monitor (Hermes cron) only
posts on change.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from src.factory.lanes import ALL_LANES
from src.factory.lanes.base import SEARCH_FLOOR_UNITS

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
DEFAULT_COVERAGE_PATH = os.path.join(REPO_ROOT, "reports", "factory", "coverage.json")

#: Data events the board knows about (architecture section 4.1 / FR-F0.5).
NEXT_DATA_ETA: Dict[str, Optional[str]] = {
    # holdout-B (07-26..08-31) must be backfilled before the ~2026-10-03 retention expiry
    "weather": "2026-10-03",
}
DATA_EVENTS = [
    {"lane": "weather", "event": "holdout-B retention expiry (backfill before)", "date": "2026-10-03"},
    {"lane": "weather", "event": "ladders_2026-09 capture timer kill date", "date": "2026-09-15"},
]


def compute_coverage() -> Dict[str, Any]:
    lanes = []
    for name, cls in ALL_LANES.items():
        st = cls().status()
        lanes.append(
            {
                "lane": name,
                "state": st.state,
                "independent_unit": cls.independent_unit,
                "n_units": int(st.n_units),
                "floor": SEARCH_FLOOR_UNITS,
                "searchable": bool(st.state == "READY" and st.n_units >= SEARCH_FLOOR_UNITS),
                "reason": st.reason,
                "next_data_eta": NEXT_DATA_ETA.get(name, st.next_data_eta),
            }
        )
    return {
        "schema_version": 1,
        "floor": SEARCH_FLOOR_UNITS,
        "lanes": lanes,
        "data_events": DATA_EVENTS,
    }


def write_coverage(path: str = DEFAULT_COVERAGE_PATH) -> Dict[str, Any]:
    cov = compute_coverage()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cov, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return cov


__all__ = ["DEFAULT_COVERAGE_PATH", "NEXT_DATA_ETA", "compute_coverage", "write_coverage"]
