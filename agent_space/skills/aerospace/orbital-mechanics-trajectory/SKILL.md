---
name: orbital-mechanics-trajectory
description: Expertise in astrodynamics, mission design, and trajectory optimization.
version: 1.0.0
---

# Orbital Mechanics & Trajectory Skill

<skill_definition>
This skill provides the mathematical backbone for moving through space. It covers Keplerian elements, patched conics, N-body simulations, and trajectory optimization (Porkchop plots, Lambert's problem, and low-thrust transfers).
</skill_definition>

<usage_instructions>
1.  **Mission Design**: Calculate Delta-V requirements for transfers (Hohmann, Bi-elliptic, Interplanetary).
2.  **Trajectory Optimization**: Use Lambert solvers to find launch windows and C3 values.
3.  **Perturbation Analysis**: Account for J2, atmospheric drag, and solar radiation pressure.
4.  **Station Keeping**: Design maneuvers for halo orbits or constellation maintenance.
</usage_instructions>

<output_template>
## Trajectory Analysis
*   **Target**: [e.g., Mars]
*   **Transfer Type**: Hohmann / Lambert
*   **Total Delta-V**: [Value] m/s
*   **Launch Window**: [Date Range]
*   **C3 Energy**: [Value] km²/s²
</output_template>
