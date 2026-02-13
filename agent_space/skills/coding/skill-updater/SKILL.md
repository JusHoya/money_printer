---
name: skill-updater
description: Rewrites and refactors a `SKILL.md` file based on a specific critique, improving its clarity, structure, and technical quality. Use to fix "C" or lower grade skills.
---

# Skill Updater ("The Editor")

## Purpose
To take a "Draft" or "Poor" skill and transform it into a "Gold Standard" skill. This is the remediation arm of the Skill Auditor.

## When to use
*   After `skill-reviewer` gives a grade of B- or lower.
*   When a user explicitly asks to "Improve this skill".

## Instructions

1.  **Analyze Context:** Read the **Original SKILL.md** and the **Critique/Audit Report**.
2.  **Plan the Rewrite:**
    *   Keep the core *intent* of the skill (don't change what it does).
    *   Change *how* it explains it (clarity, structure).
    *   Update *technical details* (better libraries, safer code).
3.  **Apply Standards:**
    *   **Frontmatter:** Ensure `description` is 3rd person, clear, and keyword-rich.
    *   **Structure:** Use standard headers: `# [Name]`, `## Purpose`, `## When to use`, `## Instructions`, `## Best Practices`, `## Examples`.
    *   **Tone:** Professional, objective, instructional.
4.  **Generate Output:** Output the **FULL, COMPLETE** new `SKILL.md` content. Do not output partial diffs.

## Best Practices
*   **Preserve Name:** Do not change the `name` in the YAML frontmatter unless explicitly told to.
*   **Enhance Examples:** If the original had bad examples, write new, better ones.
*   **Remove Fluff:** Delete conversational filler ("I think you should...", "Here is the file..."). Just give the instructions.

## Input Format
*   `Original Content`: [The text of the old file]
*   `Critique`: [The output from skill-reviewer]

## Output Format
(The raw markdown of the new file)

