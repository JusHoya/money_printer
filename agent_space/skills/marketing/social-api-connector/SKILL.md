---
name: social-api-connector
description: Handles the technical handshake, authentication, and payload delivery to various social media APIs.
version: 1.0.0
---

# Social API Connector

<skill_definition>
The **Social API Connector** is the "Hand" of the system. It abstracts away the complexity of OAuth 1.0a/2.0, signature generation, and rate limit handling. It provides a unified interface for the agent to say "Post this text to Twitter" without worrying about headers or JSON structures.
</skill_definition>

<usage_instructions>
1.  **Load Auth**: Read `config/social_credentials.json`.
2.  **Select Driver**: Choose the correct logic for the platform (Twitter v2, LinkedIn UGC, Graph API).
3.  **Construct Payload**: Format the text, attach media IDs (if any).
4.  **Execute**: Send the HTTP request.
5.  **Handle Response**: Parse the success JSON for the Post ID or handle 429 Rate Limit errors gracefully.
</usage_instructions>

<output_template>
## API Action Report
*   **Platform**: [e.g., Twitter]
*   **Endpoint**: `POST /2/tweets`
*   **Status**: 201 Created
*   **Post ID**: `1234567890`
*   **Link**: `https://twitter.com/user/status/1234567890`
</output_template>
