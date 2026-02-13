---
name: simulation-engine
description: "Orchestrates deterministic and stochastic simulation experiments, managing random states, parallel execution, and result aggregation."
version: 1.0.0
---

# Simulation Engine

## Overview
The `simulation-engine` is the runtime kernel for executing models. It ensures that "random" is never actually random (unless you want it to be) by strictly managing seeds. It also handles the heavy lifting of running the same model 10,000 times without crashing the thread.

## Capabilities

### 1. Monte Carlo Orchestrator
*   **Routine:** `run_monte_carlo(model_func, iterations, config)`
*   **Description:** Executes a model function `N` times, injecting variations based on `config`.
*   **Key Features:**
    *   Automatic seed generation per iteration (derived from a master seed).
    *   Progress tracking via `rts-logger`.
    *   Result aggregation (mean, std dev, raw data).

### 2. Random State Manager
*   **Routine:** `get_rng(seed)`
*   **Description:** Returns a unified random number generator (e.g., `numpy.random.Generator`) ensures strict reproducibility.
*   **Rule:** NEVER use global random state (e.g., `random.random()`). ALWAYS pass the RNG object.

### 3. Parallel Executor
*   **Routine:** `execute_parallel(tasks, max_workers)`
*   **Description:** Distributes simulation tasks across available cores.
*   **Safety:** Handles serialization of results and exception catching so one bad run doesn't kill the batch.

## Interface Standards
*   **Input:** Simulation configurations must be JSON-serializable dictionaries.
*   **Output:** Results must be returned as structured data (Pandas DataFrame or Dictionary List).

## Dependencies
*   Python: `numpy`, `multiprocessing`, `concurrent.futures`

