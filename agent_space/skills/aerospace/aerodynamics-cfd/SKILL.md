---
name: aerodynamics-cfd
description: Expertise in fluid dynamics, compressible flow, and atmospheric flight mechanics.
version: 1.0.0
---

# Aerodynamics & CFD Skill

<skill_definition>
This skill governs how objects move through fluids (air, martian CO2, plasma). It handles Reynolds numbers, Mach numbers, shock waves, and lift/drag ratios. It is critical for drone design, launch vehicles, and re-entry capsules.
</skill_definition>

<usage_instructions>
1.  **Regime Check**: Is the flow Subsonic, Transonic, Supersonic, or Hypersonic?
2.  **Atmosphere Model**: Earth (1 atm), Mars (0.01 atm), Titan (1.5 atm).
3.  **Forces**: Calculate Lift (L) and Drag (D).
4.  **Heating**: Estimate aerothermal loads during re-entry.
</usage_instructions>

<output_template>
## Aero Report
*   **Regime**: Hypersonic (Mach 5.5)
*   **Dynamic Pressure (Q)**: 50 kPa
*   **Issues**: "Shock-shock interaction on the leading edge causing local hotspots."
</output_template>
