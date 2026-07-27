"""Forecast calibration (PRD FR-2.2) and, later, the probability engine (FR-2.3).

Nothing is re-exported here on purpose: importing
``src.calibration.forecast_calibration`` must not drag in a sibling module that
a concurrent workstream is still writing.
"""

# Appended by workstream D (PRD FR-2.3). ``probability_engine`` lives beside
# ``forecast_calibration`` and imports it; it is deliberately NOT re-exported
# here, for the reason stated above -- importing one module of this package must
# not drag in the other.
