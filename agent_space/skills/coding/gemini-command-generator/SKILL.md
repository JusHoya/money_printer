---
name: gemini-command-generator
description: Generates valid `.toml` configuration files for the Gemini CLI. Handles syntax validation, multi-line string formatting, and metadata definition.
---

# Gemini Command Generator

## Purpose
To create strict, syntactically correct TOML configuration files that define "Slash Commands" for the Gemini CLI.

## When to use
*   When the user asks to "create a new command".
*   When the `CLI_devOps` agent needs to deploy a new capability.
*   When converting a Markdown prompt to a TOML command.

## Instructions
1.  **Define Metadata:**
    *   `name`: Kebab-case, no spaces (e.g., `git-commit`).
    *   `description`: Short summary string.
2.  **Format System Instruction:**
    *   Use the `prompt` key (NOT `system_instruction`).
    *   Use triple-single-quotes `'''` for multi-line strings.
    *   Include `{{args}}` placeholder if valid.
3.  **Validate TOML:**
    *   Ensure all strings are escaped properly.
    *   Verify the structure matches `examples/create-agent.toml`.

## Output Template
```toml
name = "command-name"
description = "Brief description."
prompt = '''

{{args}}

# SYSTEM PROMPT
<role>...</role>
<instruction>...</instruction>
'''
```

## Best Practices
*   **Filename:** Always use `[command-name].toml`.
*   **Location:** Save to `commands/` directory first (source of truth).
*   **Validation:** Use a TOML validator or dry-run if available (conceptually).

