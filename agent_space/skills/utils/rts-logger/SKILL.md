---
name: rts-logger
description: Standardizes agent console output into a structured, RTS-style format (JSON/Tagged) for UI integration. Use for all status updates, action logs, and error reporting.
---

# RTS Logger

## Purpose
To ensure all agents "speak" the same protocol, allowing a future Graphical User Interface (GUI) to parse and visualize their activities in real-time.

## When to use
*   Every time an agent takes a significant action (e.g., "Scanning", "Building", "Thinking").
*   When reporting errors or warnings.
*   When broadcasting state changes (e.g., "Idle" -> "Working").

## Instructions
1.  **Determine Log Level:** `INFO`, `WARN`, `ERROR`, `SUCCESS`.
2.  **Identify Action Type:** `STATUS`, `ACTION`, `RESOURCE`, `COMMUNICATION`.
3.  **Format Output:**
    *   **Preferred:** JSON-lines for machine parsing.
    *   **Fallback:** Tagged text for human readability (until UI is ready).

## Format Specification

### JSON Mode (Strict)
```json
{
  "timestamp": "2023-10-27T10:00:00Z",
  "agent": "AgentName",
  "type": "ACTION",
  "message": "Scanning for open ports...",
  "data": { "target": "localhost", "ports_scanned": 50 }
}
```

### Tagged Mode (Human/Legacy)
Format: `[AGENT_NAME] [TYPE] Message`
*   `[FACTORY] [STATUS] Initializing...`
*   `[RESEARCHER] [ACTION] Searching web for 'AI trends'...`
*   `[BUILDER] [ERROR] File not found.`

## Best Practices
*   **Verbosity:** Be verbose about *what* is happening, but concise in the message.
*   **Structure:** Put variable data in the `data` field (JSON), not the message string.
*   **One Line per Log:** Never output multi-line logs unless it's a specific "content block".

## Examples
**Input:** Report that the scanner found 5 files.
**Output (JSON):** `{"agent": "Scanner", "type": "result", "message": "Scan complete.", "data": {"count": 5}}`
**Output (Tagged):** `[SCANNER] [SUCCESS] Scan complete. Found 5 files.`

