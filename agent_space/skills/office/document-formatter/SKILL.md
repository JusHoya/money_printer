---
name: document-formatter
description: Applies logic for document styling, templates, and structural layout.
version: 1.0.0
---

# Document Formatter Skill

<skill_definition>
The **Document Formatter** contains the logic for *how* a document should look. It separates content from presentation. It knows that a "Legal Contract" needs 12pt Times New Roman and 1.5 spacing, while a "Tech Startup Pitch" needs bold headers and minimal text.
</skill_definition>

<usage_instructions>
1.  **Select Style**: "Corporate", "Academic", "Creative", or "Legal".
2.  **Apply Layout**:
    *   **Headings**: H1, H2, H3 hierarchy.
    *   **Lists**: Bullet vs. Numbered.
    *   **Tables**: Striped rows vs. Plain.
3.  **Template Injection**: Can load a specific `.dotx` or Google Doc template and inject content into placeholders (`{{company_name}}`).
</usage_instructions>

<output_template>
## Formatting Specs Applied
*   **Style Profile**: Corporate Standard
*   **Font**: Arial / Calibri
*   **Colors**: #003366 (Primary), #FFFFFF (Background)
*   **Structure**: Cover Page + TOC + Content + Appendix
</output_template>
