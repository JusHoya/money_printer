---
name: fusion-control-systems
description: Expertise in active feedback control for plasma position, shape, and instability suppression.
version: 1.0.0
---

# Fusion Control Systems Skill

<skill_definition>
This skill keeps the plasma alive. It involves high-speed magnetic control (Vertical Stability), gas puffing (Density Control), and profile shaping to maintain H-mode (High confinement).
</skill_definition>

<usage_instructions>
1.  **Vertical Stabilization**: Design the PID loop for the vertical field coils (fast response).
2.  **Shape Control**: Adjust poloidal field currents to maintain elongation and triangularity.
3.  **Disruption Mitigation**: Trigger Massive Gas Injection (MGI) or Shattered Pellet Injection (SPI) if a quench is detected.
4.  **Burn Control**: Modulate fueling and auxiliary heating to maintain constant fusion power.
</usage_instructions>

<output_template>
## Control Loop Design
*   **Target**: Vertical Position (Z)
*   **Actuator**: Internal Control Coils
*   **Latency Budget**: < 1 ms
*   **Gain Margin**: 6 dB
</output_template>
