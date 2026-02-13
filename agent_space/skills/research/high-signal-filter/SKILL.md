---
name: high-signal-filter
description: A logic gate to reject mainstream "fluff" and prioritize deep technical content.
version: 1.0.0
---

# High-Signal Filter Skill

<skill_definition>
The **High-Signal Filter** protects the newsletter from becoming "pop-science". It rejects stories about "What color is Mars?" and prioritizes stories about "Specific Impulse improvements in Nuclear Thermal Propulsion".
</skill_definition>

<usage_instructions>
1.  **Ingest**: Take a list of headlines/abstracts.
2.  **Score (0-10)**:
    *   +3 for Specific Engineering Metric (ISP, delta-v, Reynolds number).
    *   +3 for Novel Algorithm/Material.
    *   -5 for "Aliens", "UFOs", or "Billionaire Space Race" drama.
3.  **Pass Threshold**: Only items with Score > 5 move to the SME.
</usage_instructions>

<output_template>
## Signal Analysis
*   **Input**: "Musk tweets about dogecoin" -> **REJECT** (Score: -10)
*   **Input**: "ESA juice mission confirms Ganymede magnetic field interactions" -> **ACCEPT** (Score: 8)
</output_template>
