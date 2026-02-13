---
name: project-standards
description: Defines the mandatory folder structure and architectural guardrails for any project hosting the Hoya Box "Agent Brain".
---

# Hoya Box Project Standards & "Host Organism" Architecture

## 1. Philosophy: The Brain & The Body
The "Agent Brain" (currently `agent_space`) is a portable, modular intelligence unit. To function correctly, it must be grafted onto a "Host Organism" (Project) that follows a strict anatomical structure. This ensures that agents can navigate, modify, and build the project without getting lost or causing damage.

## 2. The Canonical Folder Structure
All projects managed by or hosting the Agent Brain MUST adhere to this structure:

```text
<PROJECT_ROOT>/
├── .gemini/               # [REQUIRED] CLI Runtime Environment
│   ├── commands/          # Active .toml command files
│   └── tmp/               # Temporary scratchpad for agents
├── agent_space/           # [REQUIRED] The Brain (Submodule or Copy)
│   ├── agents/            # Active Agents
│   ├── skills/            # Active Skills
│   └── rules/             # Constitution & Memory
├── config/                # [REQUIRED] Environment variables, secrets (gitignored), constants
├── docs/                  # [RECOMMENDED] Architecture decision records (ADR), API docs
├── scripts/               # [OPTIONAL] Setup, build, and deployment scripts
├── src/                   # [REQUIRED] Source code of the application
├── tests/                 # [REQUIRED] Unit and Integration tests
├── .gitignore             # [REQUIRED] Must exclude .gemini/tmp and secrets
└── README.md              # [REQUIRED] Entry point for humans
```

## 3. Guardrails & Hygiene Rules

### A. The "Do Not Touch" Zone
*   **`agent_space/`**: Agents should **NEVER** modify the core logic of `agent_space` unless specifically tasked with "Self-Improvement" or "Skill Creation". This is the kernel.
*   **`.gemini/commands/`**: Only the `cli-workspace-manager` is authorized to write here. Random agents dropping .toml files here causes pollution.

### B. Path Resolution
*   Agents must always use **Relative Paths** from `<PROJECT_ROOT>`.
*   Agents must assume that `agent_space` is at the root level, unless configured otherwise.

### C. Testing & Validation
*   **No Code Without Tests**: Any code written into `src/` MUST be accompanied by a corresponding test in `tests/`.
*   **Tests First**: Agents should prefer TDD (Test Driven Development).

### D. Configuration
*   **No Hardcoding**: Credentials, API keys, and environment-specific toggles must live in `config/` (e.g., `.env`, `settings.json`) and NEVER in `src/`.

### E. The Scientific Method Pipeline (Trading/Prediction Projects)
For any project involving algorithmic trading or predictive modeling, the following "Safe-to-Live" pipeline is mandatory:
1.  **Harvest**: Minimum 6-24 hours of live data collection via a monitoring-only dashboard.
2.  **Audit**: Automated win-rate and accuracy analysis against recorded history.
3.  **Refinement**: Autonomous parameter optimization (Genetic/Grid search) to reach performance targets.
4.  **The 95% Gate**: Live execution (Real funds) is PROHIBITED until the Audit phase returns a confirmed Win Rate of **>95%**.

## 4. Scaffolding Instructions
When initializing a new project:
1.  **Create Roots**: `mkdir src tests config docs scripts`
2.  **Plant the Brain**: `git submodule add <repo> agent_space` (or copy)
3.  **Initialize CLI**: `mkdir -p .gemini/commands`
4.  **Install Manager**: Deploy `cli-workspace-manager` to ensure future compliance.
