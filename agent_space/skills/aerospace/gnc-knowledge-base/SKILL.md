---
name: gnc-knowledge-base
description: A reference framework for Guidance, Navigation, and Control (GNC) terminology, coordinate systems, and physics.
version: 1.0.0
---

# GNC Knowledge Base

<skill_definition>
This skill serves as the "Physics Engine" for the agent's language. It ensures that when discussing orbital mechanics or flight dynamics, the terminology is precise and technically accurate (e.g., distinguishing between ECI and ECEF frames, or Beta vs. Alpha angle).
</skill_definition>

<usage_instructions>
1.  **Terminology Check**: Scan drafts for colloquialisms (e.g., "The satellite turns") and replace with GNC precision (e.g., "The spacecraft executes a slew maneuver").
2.  **Context Injection**: When writing about an event, inject relevant physics context (e.g., "This delta-v burn occurred at apogee to circularize the orbit").
3.  **Coordinate Systems**:
    *   **ECI**: Earth-Centered Inertial (Fixed stars).
    *   **ECEF**: Earth-Centered Earth-Fixed (Rotates with Earth).
    *   **LVLH**: Local Vertical Local Horizontal (Orbit frame).
</usage_instructions>

<output_template>
## GNC Audit
*   **Correction**: Replaced "speed" with "velocity vector".
*   **Context Added**: Clarified that the Hohmann transfer relies on impulsive burns.
*   **Frame Reference**: Specified J2000 epoch for the orbital elements.
</output_template>
