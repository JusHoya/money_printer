---
name: usage-reporter
description: Tracking and visualization of API resource consumption (Tokens, Context, Cost).
---

# Usage Reporter

## Purpose
To provide a compact, running tally of resource usage, ensuring the user is aware of the "cost of doing business" across the main agent and all sub-agent delegations.

## Constants (Gemini 2.0 Flash)
*   **Input:** $0.10 / 1M tokens
*   **Output:** $0.40 / 1M tokens
*   **Window:** 1,000,000 tokens (approx safe limit)
*   **Char/Token Ratio:** ~4 characters per token.

## Calculation Logic
1.  **Scan History:** Look at the current conversation turn.
2.  **Sub-Agents:** Identify `tool_code` outputs (representing sub-agent work). Count characters.
3.  **Main Context:** Estimate total characters of the conversation so far.
4.  **Math:**
    *   `TotalTokens = TotalChars / 4`
    *   `Percentage = (TotalTokens / 1,000,000) * 100`
    *   `Cost = (InputTokens * 1e-7) + (OutputTokens * 4e-7)` (Rough heuristic)

## Visualization (The "HUD")
The official "Context Meter" must be generated using the standard script to ensure visual consistency across the system.

**Script Path:** `skills/utils/usage-reporter/scripts/render_hud.py`
**Usage:** `python skills/utils/usage-reporter/scripts/render_hud.py [token_count]`

**Style Output:**
```text
  ╔════════════════════════════════════════════════════════════════╗
  ║                    G E M I N I   U S A G E                     ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  CONTEXT WINDOW: 1,000,000 Tokens                              ║
  ║  USED:             ~8,250 Tokens (0.83%)                       ║
  ║  [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 0%       ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  EST. COST:      $0.0008                                       ║
  ╚════════════════════════════════════════════════════════════════╝
```

## Usage Instructions
*   **Mandatory Trigger:** This HUD **MUST** be displayed immediately after the completion of any task that involved a `delegate_to_agent` call.
*   **Manual Trigger:** Use when the user explicitly asks "How much did that cost?" or "Status report".
*   **Dynamic Update:** recalculate the percentage based on the *actual* length of the conversation history visible to you.

