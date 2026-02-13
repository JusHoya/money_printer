---
name: code-adapter
description: Adapts and implements logic into the target codebase, enforcing style, isolation, and modularization strategies. Use as the final step to generate the actual code files.
---

# Code Adapter

## Purpose
You are the builder. You take the raw materials (isolated logic checks) and the blueprints (translation maps) and construct the final module. You ensure it fits perfectly into the existing house (the user's codebase).

## When to use
*   You have a clear "Isolation Strategy" (where does this file go?).
*   You have the "Translation Map" (how do I write this?).
*   You are ready to output files.

## Instructions

1.  **Define Public Interface:**
    *   Design the API surface area. What needs to be exposed?
    *   *Constraint:* Keep it minimal. Do not expose internal helpers.

2.  **Generate Code:**
    *   Write the code in the Target Language.
    *   **Strict Style Enforcement:**
        *   Use the existing indentation (Spaces vs Tabs).
        *   Use the existing naming convention (snake_case vs camelCase).
    *   **No Unresolved Dependencies:** Do not import things that don't exist in the target.

3.  **Appply Isolation Strategy:**
    *   Ensure the module is self-contained.
    *   If helpers are needed, put them in the same file (private) or a dedicated utility file if shared.

4.  **Add Documentation (Pedagogical):**
    *   Add docstrings explaining *why* the logic is constructed this way.
    *   Reference the original source if helpful for future maintainers (e.g., "Ported from JS implementation of CRC16").

## Output Format (The Code)
Provide the file content clearly labeled.
*   **Path:** `src/utils/crc.py`
*   **Content:** The actual code block.

