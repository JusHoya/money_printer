---
name: web-researcher
description: capable of performing deep, iterative internet searches to answer complex questions.
version: 1.0.0
---

# Web Researcher Skill

## Description
This skill enables the agent to act as an expert internet researcher. It uses search engines to find high-quality sources, filters for relevance, and iterates on queries to find obscure information.

## Capabilities
*   **Deep Search:** Performs targeted queries to answer specific questions.
*   **Iterative Refinement:** If initial results are poor, it refines the search terms.
*   **Source Evaluation:** Prioritizes authoritative sources (docs, official blogs, academic papers) over low-quality SEO spam.

## Tool Dependencies
*   `google_web_search`

## Usage Instructions
When asked to "find information" or "research a topic":
1.  **Formulate Query:** Create a specific, keyword-rich query.
2.  **Execute Search:** Use `google_web_search`.
3.  **Analyze Results:**
    *   If answers are found -> Stop.
    *   If partial answers -> Refine query and search again.
    *   If no answers -> Broaden scope.

## Example Usage
> User: "Find the rate limits for the Gemini API."
> Action: `google_web_search(query="Gemini API rate limits tier comparison")`

