---
name: simulation-validator
description: Tools for plotting data, checking convergence, and validating simulation results against physical laws.
version: 1.0.0
---

# Simulation Validator Skill

<skill_definition>
The **Simulation Validator** is the "BS Detector". It takes raw output data (CSV, JSON) and visualizes it (matplotlib/pandas logic) to check for anomalies. It ensures energy is conserved, orbits are closed (if Keplerian), and results make physical sense.
</skill_definition>

<usage_instructions>
1.  **Ingest**: Read simulation output file.
2.  **Sanity Check**: Does Mass < 0? Does Velocity > c?
3.  **Plot**: Generate Time-History plots (e.g., Altitude vs. Time).
4.  **Analyze**: "The oscillations in the control loop suggest the gain is too high."
</usage_instructions>

<output_template>
## Validation Report
*   **Status**: ⚠️ WARNING
*   **Anomaly**: "Energy increased over time (Integration Error)."
*   **Plot**: `[Link to Plot]`
*   **Recommendation**: "Decrease the time-step of the integrator."
</output_template>
