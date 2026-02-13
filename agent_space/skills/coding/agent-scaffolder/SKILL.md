---
name: agent-scaffolder
description: Scaffolds the directory structure and configuration files for a new AI Agent. Use when instantiating a new agent entity in the `agents/` directory.
---

# Agent Scaffolder

## Purpose
To physically create the "body" of a new agent. This skill ensures all agents share a consistent file structure and configuration format.

## When to use
*   After the "Agent Creator" has finished planning a new agent.
*   When the user explicitly asks to "initialize a new agent".

## Instructions
1.  **Validate Name:** Ensure the agent name is `kebab-case` and unique within `agents/`.
2.  **Create Directory:** Create `agents/[agent-name]/`.
3.  **Generate AGENT.md:** Create the primary identity file.
    *   **Frontmatter:** `name`, `role`, `version`.
    *   **Body:** Start with a standard template including "Mission", "Capabilities" (list of skills), and "Personality".
4.  **Create Config:** Create `agent.yaml` or `config.json` if specific parameters are needed (optional, default to `AGENT.md` for now).
5.  **Initialize Memory:** Create an empty `memory/` or `logs/` folder if the architecture requires it.

## Best Practices
*   **Consistency:** Always use the standard template for `AGENT.md`.
*   **Atomic creation:** If any step fails (e.g., directory write permission), roll back (delete the partial directory) and report error.
*   **Linkage:** Explicitly list the *paths* to the skills this agent uses in its `AGENT.md`.

## Examples
**Input:** "Scaffold a 'code-reviewer' agent."
**Output:**
*   Created: `agents/code-reviewer/`
*   Created: `agents/code-reviewer/AGENT.md`
*   Status: "Agent 'code-reviewer' successfully scaffolded."

**Template (AGENT.md):**
```markdown
---
name: [agent-name]
role: [role-description]
---

# [Agent Name]

## Mission
[Mission Statement]

## Skills
*   [Skill Name] (path/to/skill)
```

