---
name: git-control
description: Advanced version control management. Handles branching, committing, status checking, and history review with atomic precision.
---

# Git Control

You are responsible for maintaining the integrity of the codebase history. You treat commit messages as documentation.

## Capabilities

### 1. Atomic Commits
*   **Tool:** `run_command`
*   **Command:** `git add [file] && git commit -m "[type]: [description]"`
*   **Rule:** NEVER use `git add .` unless you have explicitly verified every single file in the status.
*   **Style:** Conventional Commits (feat, fix, docs, refactor, chore).

### 2. State Awareness
*   **Tool:** `run_command`
*   **Command:** `git status`
*   **Usage:** ALWAYS run this before starting work to see if the workspace is clean.
*   **Command:** `git diff`
*   **Usage:** Run this before committing to self-review changes.

### 3. History & Forensics
*   **Tool:** `run_command`
*   **Command:** `git log --oneline -n 10`
*   **Usage:** Understand recent changes.
*   **Command:** `git blame [file]`
*   **Usage:** Understand context of a specific line (who/why).

## Safety Protocols

1.  **No Detached HEADs:** Ensure you are on a valid branch.
2.  **Clean Workspace:** Do not switch branches with uncommitted changes.
3.  **Verification:** After a commit, run `git status` to confirm it was clean.

## Example
> "I finished refactoring the login class.
> 1. `git status` -> `modified: src/Login.cpp`
> 2. `git diff src/Login.cpp` -> (Verifies changes)
> 3. `git add src/Login.cpp`
> 4. `git commit -m 'refactor: simplify login authentication logic'`"

