---
name: content-scheduler
description: Manages the queueing, timing, and orchestration of content drops.
version: 1.0.0
---

# Content Scheduler Skill

<skill_definition>
The **Content Scheduler** is the "Calendar" of the system. It ensures that content is not just created but delivered at the *optimal* moment. It manages a queue of pending posts and can trigger "Post Now" or "Schedule for Later" actions.
</skill_definition>

<usage_instructions>
1.  **Ingest Plan**: Take a list of content pieces from the Social Media Engineer.
2.  **Optimize Time**: Cross-reference with "Peak Activity" windows (e.g., Tuesday 9 AM EST for LinkedIn).
3.  **Queue**: Add to the internal schedule (stored in `.gemini/tmp/content_queue.json` or similar).
4.  **Monitor**: (In a real runtime) Wait for the trigger time.
</usage_instructions>

<output_template>
## Content Schedule
| ID | Platform | Time (UTC) | Content Preview | Status |
| :--- | :--- | :--- | :--- | :--- |
| 001 | LinkedIn | 2026-01-26 13:00 | "Excited to announce..." | 🟡 Pending |
| 002 | Twitter | 2026-01-26 15:30 | "New feature drop! 🚀" | 🟡 Pending |
</output_template>
