---
name: thumbnail-designer
description: Generates high-contrast, clickable thumbnails using image composition libraries.
version: 1.0.0
---

# Thumbnail Designer Skill

<skill_definition>
The **Thumbnail Designer** creates static assets designed to stop the scroll. It uses `Pillow` (PIL) or `ImageMagick` to layer text, subject cutouts, and backgrounds. It understands the "Rule of Thirds" and contrast theory.
</skill_definition>

<usage_instructions>
1.  **Canvas Setup**: 1280x720 (YouTube) or 1080x1920 (Stories).
2.  **Composition**:
    *   **Background**: High contrast or blurred image.
    *   **Subject**: Image of person/object (ideally with background removed).
    *   **Text**: 3-5 words max, large font, stroke/drop-shadow.
3.  **Process**: Load images -> Resize -> Paste Layer -> Draw Text -> Save.
</usage_instructions>

<output_template>
## Thumbnail Generated
*   **Filename**: `thumb_v1.png`
*   **Text Overlay**: "IT WORKED!"
*   **Colors**: Yellow Text on Dark Blue Background.
*   **Path**: `.../.gemini/tmp/thumb_v1.png`
</output_template>
