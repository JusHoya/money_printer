---
name: 3d-scene-generator
description: Generates interactive 3D WebGL visualizations using Three.js from simulation data.
version: 1.0.0
---

# 3D Scene Generator Skill

<skill_definition>
The **3D Scene Generator** is the "RTS Hook." it translates raw simulation state data (positions, rotations, velocities) into an interactive, browser-based 3D environment. It uses **Three.js** to handle rendering, lighting, and camera controls.
</skill_definition>

<usage_instructions>
1.  **Ingest Data**: Take a JSON array of state objects (e.g., `[{ "time": 0, "x": 0, "y": 0, "z": 0, "rot": [0,0,0] }, ...]`).
2.  **Select Template**: Use a predefined Three.js boilerplate (e.g., Space, Atmosphere, Generic Grid).
3.  **Inject Logic**: Embed the simulation data directly into the HTML/JS script.
4.  **Export**: Save a standalone `.html` file that the user can open to "watch" the simulation.
</usage_instructions>

<output_template>
## 🎨 3D Visualization Generated
*   **Scene Type**: [e.g., Reentry Simulation]
*   **Entities**: [e.g., Titan Drone, Atmosphere Mesh]
*   **Viewer Path**: `[absolute_path_to_html]`
*   **Instruction**: "Open the HTML file in your browser to view the interactive replay."
</output_template>

<visual_identity>
All generated scenes should default to a "Dark Mode" aesthetic with high-contrast, technical UI elements (HUD style) to match the Hoya Box brand.
</visual_identity>
