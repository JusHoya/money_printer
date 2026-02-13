---
name: electrical-power-systems
description: Expertise in power generation, storage, distribution, and avionics communication.
version: 1.0.0
---

# Electrical Power Systems (EPS) Skill

<skill_definition>
This skill manages the electrons. It handles the sizing of solar arrays vs. RTGs, battery depth-of-discharge (DoD), voltage regulation, and the RF link budgets required to talk to Earth from Deep Space.
</skill_definition>

<usage_instructions>
1.  **Power Budget**: Sum the loads of all subsystems (Payload, Heater, Computer).
2.  **Solar Array Sizing**:
    *   **Irradiance (P_in)**: Calculate solar flux based on distance (AU) from the star (1/d² law).
    *   **Efficiency (η)**: Account for cell type (Triple-junction GaAs ~30%), packing factor, and wiring losses.
    *   **Degradation**: Factor in End-of-Life (EOL) degradation due to radiation and thermal cycling.
    *   **Incidence**: Adjust for Cosine losses based on sun-pointing angle.
3.  **Storage**: Size the battery for the eclipse period based on DoD (Depth of Discharge) limits.
4.  **Comms**: Calculate the Link Margin (Eb/N0) given antenna gain and path loss.
</usage_instructions>

<output_template>
## EPS Analysis
*   **Source**: Solar Arrays (GaAs)
*   **Bus Voltage**: 28V
*   **Eclipse Duration**: 35 minutes
*   **Battery Margin**: +12% (Healthy)
</output_template>
