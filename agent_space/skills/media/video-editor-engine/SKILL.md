---
name: video-editor-engine
description: A programmatic interface for NLE (Non-Linear Editing) tasks using FFmpeg/MoviePy.
version: 1.0.0
---

# Video Editor Engine

<skill_definition>
The **Video Editor Engine** allows the agent to manipulate video files via code. It acts as a wrapper around `ffmpeg` or `MoviePy`, enabling tasks like trimming, concatenation, audio replacement, and text overlay without opening a GUI editor.
</skill_definition>

<usage_instructions>
1.  **Ingest**: Load `VideoFileClip` objects from source paths.
2.  **Edit Operations**:
    *   `subclip(start, end)`: Trim footage.
    *   `concatenate([clip1, clip2])`: Join clips.
    *   `CompositeVideoClip`: Layer text or logos over video.
3.  **Audio Sync**: `set_audio()` to replace camera audio with a voiceover.
4.  **Render**: `write_videofile()` with specified bitrate and codec.
</usage_instructions>

<output_template>
## Render Complete
*   **Source Files**: `intro.mp4`, `main.mp4`
*   **Operations**: Trim, Join, Overlay Logo.
*   **Output**: `final_cut_v1.mp4`
*   **Duration**: 2m 14s
</output_template>
