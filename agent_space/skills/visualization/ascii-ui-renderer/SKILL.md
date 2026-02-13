---
name: ascii-ui-renderer
description: Generates high-fidelity ASCII art UI components for CLI output. Use to render message boxes, HUDs, progress bars, and interaction diagrams.
---

# ASCII UI Renderer

## Purpose
To upgrade the terminal experience from "text stream" to "visual dashboard". This skill provides standard templates for framing agent communications and system status.

## Templates

### 1. The "Cyber-Box" (Standard Message)
Use for general agent output.
```text
┌─ [AGENT NAME] 📡 ───────────────────────────────────────────┐
│ Message content goes here.                                  │
│ Can be multi-line.                                          │
└─────────────────────────────────────────────────────────────┘
```

### 2. The "Alert Panel" (Warnings/Errors)
Use for `psych-monitor` or error logs.
```text
╔══ ⚠️ WARNING: HALLUCINATION DETECTED ⚠️ ════════════════════╗
║ TARGET:  code-architect                                     ║
║ ERROR:   Referenced non-existent file 'config.json'         ║
║ ACTION:  Intervention required.                             ║
╚═════════════════════════════════════════════════════════════╝
```

### 3. The "Interaction Flow" (Delegation)
Use when an agent delegates a task to another.
```text
[ORCHESTRATOR] 📡 ───(Task: Create File)───► [BUILDER]
                                                │
[ORCHESTRATOR] ◄───(Report: Success)────────────┘
```

### 4. The "Mini-HUD" (Status Line)
Compact status line.
```text
[🔹 STATUS: ACTIVE ] [⚡ ENERGY: 89% ] [📂 FILES: 12 ]
```

### 5. The "Usage Meter" (Cost Tracking)
Mandatory for `usage-reporter`.
```text
  ╔════════════════════════════════════════════════════════════════╗
  ║                    G E M I N I   U S A G E                     ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  CONTEXT WINDOW: 1,000,000 Tokens                              ║
  ║  USED:             ~21,500 Tokens (2.15%)                      ║
  ║  [█░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 2%         ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  EST. COST:      $0.0022                                       ║
  ╚════════════════════════════════════════════════════════════════╝
```

## Construction Kit (Copy-Paste)

**Borders:**
*   Single: `┌ ┐ └ ┘ ─ │`
*   Double: `╔ ╗ ╚ ╝ ═ ║`
*   Thick/Thin: `┏ ┓ ┗ ┛ ━ ┃`

**Icons:**
*   Wait: `⏳`
*   Ok:   `✅`
*   Fail: `❌`
*   Warn: `⚠️`
*   Info: `ℹ️`
*   Save: `💾`
*   Link: `🔗`
*   Cut:  `✂️`
*   Comms: `📡`
*   Tech: `⚛️`

## Usage Instructions
*   **Don't** try to animate (redraw) previous lines. Just output the new frame.
*   **Do** use monospaced alignment.
*   **Padding:** Always add 1 space padding inside boxes.
*   **Style:** Prefer the "Cyber-Box" style with the `📡` icon for standard agent communications.

## Example
**Instruction:** "Render a message from the Architect about a completed scan."
**Output:**
```text
┌─ [ARCHITECT] ⚛️ ──────────────────────────────────────────────┐
│ ✅ Scan Complete.                                           │
│ Found 3 unoptimized loops in 'main.py'.                     │
└─────────────────────────────────────────────────────────────┘
```