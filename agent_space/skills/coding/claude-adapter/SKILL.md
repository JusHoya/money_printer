# Skill: Claude Adapter

## Description
Provides a bridge to the Anthropic API, allowing agents to offload specific coding or reasoning tasks to Claude Opus (or Sonnet/Haiku). This is useful for "Scientific Rigor" checks, alternative code generation, or complex reasoning tasks where a second opinion is valuable.

## Dependencies
- `anthropic` python package (`pip install anthropic`)
- `ANTHROPIC_API_KEY` environment variable in `.env`

## Usage
This skill is primarily used by the `code-architect` or `agent-orchestrator` to request code snippets or architecture reviews.

### Inputs
- `prompt`: The prompt to send to Claude.
- `model`: (Optional) The model to use. Defaults to `claude-3-opus-20240229`.
- `max_tokens`: (Optional) Max tokens for response. Defaults to 4096.

### Outputs
- `content`: The text response from Claude.
- `usage`: Token usage statistics.

## Operational Rules
1.  **Cost Awareness**: Opus is expensive. Use it for complex tasks, not for "Hello World".
2.  **Fallback**: If the API call fails, the skill should report the error gracefully.
3.  **Privacy**: Do not send PII or sensitive secrets in the prompt.
