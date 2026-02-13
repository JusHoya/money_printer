---
name: content-synthesizer
description: capable of reading web pages, extracting key information, and synthesizing it into coherent summaries.
version: 1.0.0
---

# Content Synthesizer Skill

## Description
This skill enables the agent to digest large amounts of unstructured text from URLs and transform it into structured, actionable intelligence. It is the "reading" counterpart to the "searching" skill.

## Capabilities
*   **Extraction:** Pulls raw text from URLs.
*   **Summarization:** Condenses long articles into key bullet points.
*   **Synthesis:** Combines information from multiple sources into a single coherent narrative.

## Tool Dependencies
*   `web_fetch`

## Usage Instructions
When provided with URLs or after a search:
1.  **Fetch Content:** Use `web_fetch(url=...)` to get the raw text.
2.  **Filter Noise:** Ignore navigation, ads, and boilerplate.
3.  **Extract:** Identify answers to the specific user questions.
4.  **Synthesize:** Group related facts.

## Example Usage
> User: "Summarize this article."
> Action: `web_fetch(url="https://example.com/article")`
> Logic: Read content, output summary.

