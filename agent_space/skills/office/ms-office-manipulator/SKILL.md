---
name: ms-office-manipulator
description: Manipulates local Microsoft Office files (.docx, .xlsx, .pptx) using Python libraries.
version: 1.0.0
---

# MS Office Manipulator

<skill_definition>
The **MS Office Manipulator** uses specialized Python libraries (`python-docx`, `openpyxl`, `python-pptx`) to generate and modify standard Office documents on the local file system. It does not require an internet connection or API keys for basic file operations.
</skill_definition>

<usage_instructions>
1.  **Word (.docx)**: Use `python-docx` to add headings, paragraphs, and tables.
2.  **Excel (.xlsx)**: Use `openpyxl` or `pandas` to write dataframes to sheets and apply formatting.
3.  **PowerPoint (.pptx)**: Use `python-pptx` to create slides from layouts and insert text/images.
4.  **Save**: Always save to the user's specified directory or `.gemini/tmp/` by default.
</usage_instructions>

<output_template>
## Local File Generated
*   **Filename**: `presentation_draft.pptx`
*   **Path**: `C:\Users\hoyer\WorkSpace\Projects\Hoya_Box\agent_space\.gemini\tmp\presentation_draft.pptx`
*   **Size**: 2.4 MB
</output_template>
