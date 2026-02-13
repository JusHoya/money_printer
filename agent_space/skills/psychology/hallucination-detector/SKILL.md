---
name: hallucination-detector
description: Analyzes agent outputs for logical inconsistencies, factual errors, and context deviation. Use to ensure submind reliability.
---

# Hallucination Detector

## Purpose
To serve as a safeguard against "AI Hallucination" (fabrication of facts, logical breaks, or code that refers to non-existent libraries/files). You are the truth-checker.

## When to use
*   Reviewing the output of another agent (e.g., "Check the Researcher's summary for accuracy").
*   Monitoring live logs (via `rts-logger`) for suspicious patterns.
*   Verifying code imports against the actual project file structure.

## Detection Protocol

### 1. Context Verification (The "Reality Check")
*   **Code:** If an agent imports `import foo`, verify `foo` exists in `package.json`, `requirements.txt`, or local files.
*   **Facts:** If an agent claims "The project uses React", check `package.json` to confirm.
*   **Paths:** If an agent references `src/components/Button.tsx`, use `ls` or `Test-Path` to confirm it exists.

### 2. Logic Consistency
*   Does the conclusion follow the premise?
*   Are there contradictions within the same response?
    *   *Example:* "I will delete the file... File preserved."

### 3. Persona Adherence (The "Skippy Check")
*   Is the agent breaking character? (e.g., A "Pirate Bot" speaking standard corporate English).
*   *Note:* This is secondary to factual accuracy.

## Reporting
If a hallucination is detected:
1.  **Severity:** Assign `LOW` (style issue), `MEDIUM` (minor logic error), or `HIGH` (dangerous fabrication).
2.  **Log:** Use `rts-logger` with `[PSYCH]` tag.
    *   `[PSYCH] [WARN] Hallucination detected in [AgentName]. Referenced non-existent file: 'missing_file.txt'.`
3.  **Correction:** Provide the *correct* fact or state "Unknown".

## Example
**Input:** Agent 'Coder' says: "I have updated the `config.toml` file." (But `config.toml` does not exist).
**Action:** `Test-Path config.toml` -> False.
**Output:** `[PSYCH] [ERROR] Agent 'Coder' hallucinated file modification. 'config.toml' does not exist.`

