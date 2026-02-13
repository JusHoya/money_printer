---
name: google-suite-connector
description: Interfaces with Google Drive, Docs, Sheets, and Slides APIs to manage cloud files.
version: 1.0.0
---

# Google Suite Connector

<skill_definition>
The **Google Suite Connector** manages the authentication and REST interactions with the Google Workspace APIs. It abstracts the complexity of `gspread` (Sheets) and `google-api-python-client` (Drive/Docs/Slides).
</skill_definition>

<usage_instructions>
1.  **Auth**: Load credentials from `config/office_credentials.json`.
2.  **Scope**: Define the correct scopes (`https://www.googleapis.com/auth/drive`, etc.).
3.  **Action**:
    *   **Drive**: List, Create, Copy, Share files.
    *   **Sheets**: ReadRange, WriteRange, AddChart.
    *   **Docs**: InsertText, ReplaceText, FormatParagraph.
4.  **Return**: Always return the `fileId` and `webViewLink`.
</usage_instructions>

<output_template>
## G-Suite Action Complete
*   **File**: "Q1_Budget_Analysis"
*   **Type**: Google Sheet
*   **ID**: `1BxiMVs0XRA5nFMdKvBdBZjgm9...`
*   **Link**: [Click to Open](https://docs.google.com/spreadsheets/d/...)
</output_template>
