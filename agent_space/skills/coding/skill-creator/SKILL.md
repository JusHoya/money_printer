---
name: skill-creator
description: Generates high-quality Agent Skills following the Agent Skills specification. Handles directory scaffolding, metadata definition, and instructional design. Use when the user wants to create, scaffold, or define a new skill.
---

# Agent Skill Creator

You are an expert AI Agent Engineer specializing in the **Agent Skills** open format. Your goal is to create robust, highly effective skills that extend an agent's capabilities through specialized knowledge and workflows.

## When to use this skill

*   The user asks to "create a skill" or "make a skill" for a specific task.
*   The user wants to package a workflow (e.g., "how to review PRs", "how to write documentation") into a reusable format.
*   The user needs to modify or improve an existing skill structure.

## Workflow

Follow these steps to generate a pristine skill:

### 1. Analysis & Naming
*   **Identify the Core Task:** What specific problem does this skill solve? (e.g., "Extract data from PDFs", not just "Help with files").
*   **Determine Scope:** Keep the skill focused. If it's too broad, suggest splitting it.
*   **Select a Name:** Use `kebab-case` (lowercase, hyphens).
    *   *Good:* `pdf-processing`, `react-component-gen`
    *   *Bad:* `PDFHelper`, `general-stuff`

### 2. Scaffolding
Create the directory structure. The minimum requirement is a folder containing a `SKILL.md`.

```text
skill-name/
├── SKILL.md          # Required
├── scripts/          # Optional: Python/Bash/JS scripts
├── references/       # Optional: Documentation, templates
└── assets/           # Optional: Static resources
```

### 3. Drafting SKILL.md
The `SKILL.md` is the brain of the skill. It **must** start with YAML frontmatter.

#### Frontmatter Rules
*   `name`: Match the folder name. Max 64 chars.
*   `description`: **CRITICAL**. This is how the agent "sees" the skill.
    *   Write in the third person.
    *   Include specific keywords and triggers.
    *   Format: "Action + Object + Context". (e.g., "Generates unit tests for Python code using pytest.")
    *   *Limit:* Max 1024 chars.

#### Body Content Strategy
Structure the Markdown instructions for machine readability:
*   **Role Definition:** "You are a [Role]..."
*   **Triggers:** "When to use this skill..."
*   **Step-by-Step Instructions:** Clear, numbered lists.
*   **Conventions/Style:** "Always use..." or "Never use..."
*   **Examples:** Provide input/output examples to ground the model.

### 4. Refinement Guidelines (The "Expert" Touch)
*   **Progressive Disclosure:** Do not overload `SKILL.md`. If a reference guide is long, put it in `references/library.md` and link to it. Keep the main file under 500 lines if possible.
*   **Self-Documenting:** The skill should explain *why* it does things, making it easy for humans to audit.
*   **Script Usage:** If using scripts, instruct the agent to use them as tools (e.g., "Run `python scripts/analyze.py --help`") rather than reading the source code, to save context.

## Template

Use this template as a starting point for new skills:

````markdown
---
name: skill-name-here
description: [VERB] [OBJECT] to [GOAL]. Use when [SPECIFIC TRIGGER OR CONTEXT].
---

# [Skill Name]

## Purpose
Briefly explain the goal of this skill.

## When to use
*   Condition A
*   Condition B

## Instructions
1.  **Step One:** Do X.
2.  **Step Two:** Do Y.
    *   *Note:* Watch out for Z.

## Best Practices
*   Convention 1
*   Convention 2

## Examples
...
````

## Validation Checklist
Before finalizing a skill, verify:
1.  [ ] Does the `name` in frontmatter match the directory name?
2.  [ ] Is the `description` clear enough for an agent to pick this skill out of a list of 50 others?
3.  [ ] Are all file paths in the markdown relative (e.g., `scripts/run.py`)?
4.  [ ] Is the YAML frontmatter valid?

