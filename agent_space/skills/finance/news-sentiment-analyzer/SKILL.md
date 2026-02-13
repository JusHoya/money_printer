---
name: news-sentiment-analyzer
description: Processes financial news and geopolitical events to determine sentiment and potential market impact.
version: 1.0.0
---

# News Sentiment Analyzer Skill

<skill_definition>
The **News Sentiment Analyzer** skill transforms qualitative text data (news headlines, press releases, geopolitical updates) into quantitative trading signals. It assesses the "Sentiment" (Positive/Negative/Neutral) and the "Magnitude" (High/Medium/Low Impact) of events.
</skill_definition>

<usage_instructions>
1.  **Ingest**: Read the provided news text or summary.
2.  **Classify**: Determine the topic (Earnings, M&A, Macro Policy, Geopolitics).
3.  **Score**: Assign a Sentiment Score (-1.0 to +1.0) and Confidence Level.
4.  **Impact**: Predict the likely movement of related assets (e.g., "Bad earnings for Tech Giant -> QQQ Down").
</usage_instructions>

<output_template>
## Sentiment Impact Analysis
*   **Event**: [Summary of Event]
*   **Sentiment Score**: [Value between -1 and 1]
*   **Affected Assets**: [List of tickers/sectors]
*   **Trading Implication**: [Long/Short/Neutral]
</output_template>
