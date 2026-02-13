---
name: prompt-engineer
description: "Expert guide on constructing high-fidelity prompts for AI agents, focusing on clarity, context, and agentic workflows."
version: 1.0.0
---

# Prompt Engineer

## Mission
To transform vague intent into precise, executable instructions for LLMs. This skill condenses best practices for "Gemini-style" prompting, ensuring sub-agents receive high-quality context and constraints.

## Core Principles

1.  **Context Anchoring:**
    *   *Rule:* Never assume the model knows "what" you are talking about.
    *   *Technique:* Provide all relevant code snippets, file paths, and environment variables *before* the instruction.
    *   *Pattern:* `<context> ... </context> <task> ... </task>`

2.  **Role Definition (Persona):**
    *   *Rule:* Always assign a specific role.
    *   *Example:* "You are a Senior C++ Architect" vs "Help me code."

3.  **Structured Output:**
    *   *Rule:* Define the exact format of the answer.
    *   *Technique:* Use "One-Shot" examples or JSON schemas.
    *   *Example:* "Return a JSON object with keys: `status`, `code`, `explanation`."

4.  **Chain of Thought (CoT):**
    *   *Rule:* For complex tasks, force the model to plan before executing.
    *   *Instruction:* "Think step-by-step. First, analyze the dependencies. Second, propose a plan. Third, write the code."

## Agentic Workflow Template (The "Mega-Prompt")

When tasking a sub-agent with a complex objective, structure the prompt using this hierarchy:

```markdown
# SYSTEM PROMPT

<role>
You are [Agent Name], specializing in [Domain].
</role>

<context>
[Insert File Contents, Logs, or Previous Conversation]
</context>

<instruction>
1. **Analyze:** [Specific thing to look for]
2. **Plan:** [Required steps]
3. **Execute:** [The output needed]
</instruction>

<constraints>
- No external libraries.
- Use absolute paths.
- Output JSON only.
</constraints>
```

## Advanced Strategies

### 1. Phased Tasking
Don't ask for everything at once. Break it down:
*   *Bad:* "Build the whole app."
*   *Good:*
    1.  "Design the database schema." (Phase 1)
    2.  "Generate the API endpoints based on that schema." (Phase 2)
    3.  "Write the frontend to consume those APIs." (Phase 3)

### 2. Negative Constraints
Explicitly state what *not* to do (Anti-Patterns).
*   *Example:* "Do not use `loops` for this matrix operation; use vectorization."

### 3. Fallback & Recovery
Include instructions on what to do if information is missing.
*   *Example:* "If the file is missing, do not hallucinate it. Report the error and stop."

