---
name: cli-workspace-manager
description: Manages the project's folder structure, enforces the "Host Organism" standards, and handles the deployment of Gemini CLI commands.
---

# CLI Workspace Manager

## Purpose
1.  **Standardization**: Enforce the canonical folder structure defined in `rules/project_standards.md`.
2.  **Deployment**: Install/Update commands into the `.gemini/commands/` runtime.
3.  **Hygiene**: Ensure the "Brain" (`agent_space`) is correctly situated within the "Host" project.

## Capabilities

### 1. `deploy-command`
*   **Trigger**: "Install the [x] command", "Update the [x] command".
*   **Action**:
    1.  Check if source exists: `commands/[name].toml`.
    2.  Ensure dest exists: `mkdir -p .gemini/commands`.
    3.  Copy file: `cp commands/[name].toml .gemini/commands/[name].toml`.
    4.  Verify existence.

### 2. `scaffold-host-project`
*   **Trigger**: "Set up a new project", "Initialize the folder structure", "Prepare the workspace".
*   **Action**:
    1.  **Read Standards**: Consult `rules/project_standards.md` to confirm the latest structure.
    2.  **Create Directories**:
        ```bash
        mkdir -p src tests config docs scripts .gemini/commands .gemini/tmp
        ```
    3.  **Check for Brain**: Verify `agent_space` exists.
        *   *If missing*: Warn the user "CRITICAL: `agent_space` (The Brain) is missing. Please submodule or copy it here."
    4.  **Create README**: Generate a basic `README.md` if one doesn't exist, referencing the agentic nature of the project.
    5.  **Create .gitignore**: Ensure sensitive paths (`.env`, `.gemini/tmp`) are ignored.

### 3. `audit-workspace`
*   **Trigger**: "Check if this project is set up correctly", "Audit the folders".
*   **Action**:
    1.  Check for existence of `src`, `tests`, `config`, `.gemini`.
    2.  Report missing directories.
    3.  Report if `agent_space` is not found.

## Usage Instructions
*   **Agents**: When starting a new task in a fresh directory, call `scaffold-host-project` first.
*   **Users**: Run `/run agents/cli-devops/task.toml` (if mapped) or ask the Orchestrator to "Initialize the workspace".

## Best Practices
*   **Atomic Operations**: Use `run_shell_command` for all file system mutations.
*   **Idempotency**: `mkdir -p` is safe to run multiple times. Do not fail if folders exist.
*   **Safety**: Never delete user code in `src/` during a scaffold operation. Only add missing structures.