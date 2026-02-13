---
name: content-engine
description: Architects multi-channel content strategies and automates content creation. Use when planning blog posts, social media threads, or documentation updates based on technical milestones.
---

# Content Engine

## Purpose
To translate complex engineering milestones into engaging narratives for diverse stakeholders. This skill enforces the "Silent Partner" protocol: professional, evidence-based, and devoid of AI-smells.

## When to use
*   After completing a technical milestone (e.g., successful simulation).
*   When transforming raw logs or git diffs into public-facing updates.
*   Updating `README.md`, `CHANGELOG.md`, or Project Wikis.

## Instructions

### Step 1: Data Extraction
*   Read the recent `git diff`, `task.md`, or simulation log.
*   Identify the "Hero Metric": A specific, quantifiable improvement (e.g., "30% faster", "J2 drift eliminated").

### Step 2: Content Generation
*   **Generate Hooks:** Draft 3 variations of a "Hook" that focuses on the engineering feat, not the process.
*   **Draft Threads:** Create a 4-post Twitter/X thread following the [Problem -> Solution -> Impact -> Proof] flow.
*   **Apply Humanization:** Process all drafts through the `text-humanizer` skill to ensure the "AI Vocabulary Blacklist" is respected.

### Step 3: Visual Integration
*   **Standardize:** Call `skills/visualization/visual-standardizer/SKILL.md` to propose a color palette for the update.
*   **Plot:** If telemetry is available, call `skills/visualization/plot-generator/SKILL.md`.
*   **ASCII HUD:** For CLI-based success stories, use `skills/visualization/ascii-ui-renderer/SKILL.md`.

## Best Practices
*   **No Fluff:** Avoid words like "delve", "tapestry", or "paramount".
*   **Scientific Rigor:** Cite specific commit hashes or data points.
*   **Visual Proof:** "Pics or it didn't happen." Always accompany updates with a visual artifact.

## Examples
**Input:** "Fixed reaction wheel saturation issues in the 6DOF sim."
**Output:**
> **Tweet:** No more saturation. 🛰️ By implementing a momentum dumping strategy using magnetic torquers, we've kept our reaction  wheels within 20% of nominal speed during a 48-hour GEO station-keeping sim.
> **Image Prompt:** "Clean vector illustration of a satellite with glowing torque rods, momentum vectors decreasing, dark space background, technical HUD overlay."