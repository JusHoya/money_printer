import sys
import os

# Robust path addition
current_dir = os.path.dirname(os.path.abspath(__file__))
target_dir = os.path.abspath(os.path.join(current_dir, '../../coding/claude-adapter'))
sys.path.append(target_dir)

print(f"DEBUG: Adding path: {target_dir}")

try:
    from claude_bridge import query_claude
except ImportError as e:
    print(f"Error: Could not import claude_bridge. Path: {sys.path}")
    print(f"Exception: {e}")
    sys.exit(1)

prompt = """
You are an expert DevOps engineer and Python scripter.
Write a robust Python script named `distill.py` that synchronizes "knowledge" from a Source directory to a Master directory.

Requirements:
1.  **Arguments**: Use `argparse`.
    *   `--source`: Path to the deployed/child project.
    *   `--master`: Path to the master/current project (default: current working dir).
    *   `--execute`: If not present, default to DRY RUN (print actions but do not copy).
    *   `--verbose`: Show detailed logs.

2.  **File Synchronization (Agents & Skills)**:
    *   Recursively walk `agents/` and `skills/` directories in Source.
    *   Compare with Master.
    *   Identify **NEW** files (exist in Source, not Master).
    *   Identify **MODIFIED** files (exist in both, but Source is different content/hash).
    *   In `--execute` mode, copy these files to Master, preserving structure.

3.  **Memory Merging (.gemini/GEMINI.md)**:
    *   Read `.gemini/GEMINI.md` from both Source and Master.
    *   Extract bullet points specifically from the section `## Gemini Added Memories`.
    *   Identify memories present in Source but MISSING in Master.
    *   In `--execute` mode, append these new lines to the Master's `GEMINI.md` under the correct header.

4.  **Output**:
    *   Use clear emojis for logging (e.g., 🆕 for new, 📝 for modified, 🧠 for memory).
    *   Print a summary at the end.

5.  **Code Style**:
    *   Clean, PEP8 compliant.
    *   Use `pathlib`, `hashlib`, `shutil`.
    *   Include a `if __name__ == "__main__":` block.
    *   **CRITICAL**: Do not wrap the output in markdown code blocks. Return RAW python code only.

"""

print("Requesting code from Claude Haiku...")
# Using a faster model for the test if Opus is too slow/expensive, but user requested rigorous.
# sticking to Opus or Sonnet.
result = query_claude(prompt, model="claude-3-haiku-20240307")

if "error" in result:
    print(f"Error calling Claude: {result['error']}")
else:
    # Basic cleanup if Claude adds markdown blocks despite instructions
    content = result['content']
    if content.startswith("```python"):
        content = content.replace("```python", "", 1)
    if content.endswith("```"):
        content = content[:-3]
    
    # Write to file
    output_path = os.path.join(current_dir, "distill.py")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content.strip())
    
    print(f"Success! Code generated and saved to {output_path}")