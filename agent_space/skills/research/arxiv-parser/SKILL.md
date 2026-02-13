---
name: arxiv-parser
description: Specialized tool for ingesting, filtering, and summarizing aerospace pre-prints from ArXiv.org.
version: 1.0.0
---

# ArXiv Parser Skill

<skill_definition>
The **ArXiv Parser** is the eyes of "The Scout". It interfaces with the ArXiv API specifically targeting categories like `astro-ph.IM` (Instrumentation), `cs.RO` (Robotics), and `physics.flu-dyn` (Fluid Dynamics). It looks for high-impact technical papers before they hit mainstream news.
</skill_definition>

<usage_instructions>
1.  **Query**: Search specifically for "GNC", "Hypersonic", "Fusion", "Deep Space".
2.  **Filter**: Prioritize papers with high citation velocity or from reputable labs (JPL, MIT, Stanford).
3.  **Summarize**: Extract the "Abstract" and "Conclusion". Ignore the math proofs; focus on the *result* and *application*.
</usage_instructions>

<output_template>
## ArXiv Intel
*   **Title**: "Robust H-infinity Control for Hypersonic Glide Vehicles"
*   **ID**: 2305.12345
*   **Significance**: Proposes a new method to handle atmospheric uncertainty during reentry.
*   **Link**: `arxiv.org/abs/2305.12345`
</output_template>
