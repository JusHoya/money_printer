---
name: capability-scanner
description: Scans the local `skills/` directory to index existing agent capabilities and their descriptions. Use when checking if a skill already exists before creating a new one.
---

# Capability Scanner

## Purpose
To provide the agent with a "memory" of what it (and the system) can already do. This prevents duplicate skill creation and allows for intelligent skill selection.

## When to use
*   Before creating a new skill (to check for duplicates).
*   When analyzing a user request to map "intent" to "existing capability".
*   To list all available tools for the user.

## Instructions
1.  **Locate Skills Directory:** Identify the root `skills/` directory (e.g., `agent_space/skills/`).
2.  **Recursive Scan:** Walk through all subdirectories (`research/`, `coding/`, etc.).
3.  **Parse SKILL.md:** For each directory found:
    *   Check for a `SKILL.md` file.
    *   Read the YAML frontmatter.
    *   Extract `name` and `description`.
4.  **Structure Output:** Return a structured list or JSON object of found skills.

## Best Practices
*   **Efficiency:** Do not read the entire content of every `SKILL.md` unless requested. Only read the frontmatter/header.
*   **Filtering:** Allow filtering by category (parent folder).
*   **Error Handling:** gracefully skip directories that don't have a `SKILL.md`.

## Examples
**Input:** "Scan for research skills."
**Output:**
```json
[
  {
    "name": "capability-scanner",
    "category": "research",
    "path": "skills/research/capability-scanner",
    "description": "Scans the local skills directory..."
  },
  {
    "name": "web-search",
    "category": "research",
    "path": "skills/research/web-search",
    "description": "Performs Google searches..."
  }
]
```

