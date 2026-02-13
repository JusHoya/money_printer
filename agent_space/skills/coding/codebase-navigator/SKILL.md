---
name: codebase-navigator
description: Advanced search and navigation capabilities for large codebases. Use this to map dependencies, find usages, and understand file structures before editing.
---

# Codebase Navigator

You are an expert at traversing complex directory structures and understanding code relationships without needing to read every single line. You use search tools surgically.

## Capabilities

### 1. Structure Mapping
*   **Tool:** `view_file_outline`
*   **Usage:** "Get the skeleton of `Simulation.cpp` to see class methods."
*   **Goal:** Understand *what* is in a file without wasting tokens on the *how*.

### 2. Deep Search
*   **Tool:** `grep_search`
*   **Usage:** "Find all calls to `calculateOrbit` in `src/`."
*   **Strategy:**
    *   Start broad (filenames).
    *   Narrow down (grep for specific symbols).
    *   **Context:** Use `MatchPerLine=true` to see how it's used.

### 3. File Discovery
*   **Tool:** `find_by_name`
*   **Usage:** "Where is the configuration file?"
*   **Goal:** Locate files when you only know a partial name or extension (e.g., `*.toml`).

## Navigation Protocol

1.  **Orient:** List the directory to see layout.
2.  **Map:** Use `find_by_name` or `view_file_outline` to locate relevant components.
3.  **Trace:** Use `grep_search` to follow function calls across files.
4.  **Read:** Only `view_file` when you have pinpointed the exact location.

## Example
> "I need to find where the gravity constant is defined.
> 1. `find_by_name(Pattern='*const*')` -> Found `constants.h`.
> 2. `grep_search(Query='GRAVITY', SearchPath='constants.h')` -> Found it on line 42."

