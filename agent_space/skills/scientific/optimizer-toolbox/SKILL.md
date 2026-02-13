---
name: optimizer-toolbox
description: "Provides a suite of numerical optimization algorithms ranging from gradient descent to evolutionary strategies."
version: 1.0.0
---

# Optimizer Toolbox

## Overview
The `optimizer-toolbox` is the brain that drives models toward desired outcomes. It wraps standard libraries (like SciPy) and implements custom heuristics for finding global maxima/minima in complex, high-dimensional spaces.

## Capabilities

### 1. Scalar Minimization
*   **Routine:** `minimize_scalar(objective_func, bounds, method='brent')`
*   **Description:** Finds the minimum of a single-variable function.
*   **Use Case:** Tuning a single parameter (e.g., "Find the optimal PID gain").

### 2. Multivariate Optimization
*   **Routine:** `minimize_vector(objective_func, x0, bounds, constraints, method='SLSQP')`
*   **Description:** Solves N-dimensional optimization problems subject to equality/inequality constraints.
*   **Methods:** `BFGS`, `Nelder-Mead`, `SLSQP`, `L-BFGS-B`.

### 3. Global Search (Evolutionary)
*   **Routine:** `optimize_evolutionary(objective_func, bounds, population_size, generations)`
*   **Description:** Uses Differential Evolution or Genetic Algorithms to find global optima in non-convex spaces (where gradient descent gets stuck).
*   **Features:** Mutation, Crossover, Selection pressure tuning.

## Interface Standards
*   **Objective Function:** Must accept a vector `x` and return a scalar `cost`.
*   **Constraints:** Defined as dictionaries `{'type': 'ineq', 'fun': ...}`.

## Dependencies
*   Python: `scipy.optimize`, `numpy`

