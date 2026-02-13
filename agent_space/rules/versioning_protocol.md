# Skippy System Versioning Protocol

This document establishes the strict versioning and commit message protocol for the **Skippy Master Branch** and **Dev Branches**.

## 1. Version Format
**`Skippy X.Y.Z`**
- **X** = Major Revision
- **Y** = Minor Revision
- **Z** = Patch Revision

## 2. Commit Message Requirement
The calculated version tag **MUST** be appended to the **front** of every commit message.
*Example:* `[Skippy 2.0.0] Overhauled orchestration engine`

## 3. Increment Logic

### 🔴 Major Revision (X)
*Exclusive Trigger:* **Activity in `skills_dev_branch`**
Any of the following actions performed within or merged from `skills_dev_branch` triggers a Major revision:
1.  **Modification** of the **Orchestrator**.
2.  **Modification** of an **Existing Agent**.
3.  **Modification** of an **Existing Skill**.
4.  **Creation** of a **New Agent** or **Skill**.
5.  **Rollover:** Minor Revision (Y) >= 10.

*Transition Example:* `Skippy 1.3.0` -> `Skippy 2.0.0`

### 🟡 Minor Revision (Y)
*Triggers (Outside `skills_dev_branch`):*
1.  **Knowledge Distillation** (System learning/memory updates).
2.  **Creation** of a **New Agent** (Hotfix/Master direct).
3.  **Modification** of an **Existing Agent** (Hotfix/Master direct).
    - *Accumulation:* Add count to current Y.

*Transition Example:* `Skippy 1.0.0` -> `Skippy 1.1.0`

### 🟢 Patch Revision (Z)
*Triggers (Outside `skills_dev_branch`):*
1.  **Creation** of a **New Skill** (Hotfix/Master direct).
2.  **Modification** of an **Existing Skill** (Hotfix/Master direct).
    - *Accumulation:* Add count to current Z.

*Transition Example:* `Skippy 1.0.0` -> `Skippy 1.0.1`

## 4. Branch Policy
- **`skills_dev_branch`**: The "Major Version Factory". All work here targets the next Major X.
- **`main`**: The Stable Skippy. Receives merges or hotfixes (Minor/Patch).

## 5. Current State Tracking
The current version is tracked in `agent_space/.gemini/GEMINI.md`.