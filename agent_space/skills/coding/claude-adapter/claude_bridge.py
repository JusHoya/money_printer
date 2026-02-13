import os
import anthropic
from dotenv import load_dotenv

def query_claude(prompt, model="claude-3-opus-20240229", max_tokens=4096, system_prompt="You are an expert coding assistant."):
    """
    Sends a prompt to Anthropic's Claude API and returns the response.
    """
    load_dotenv() # Load environment variables from .env
    
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your_key_here":
        return {"error": "ANTHROPIC_API_KEY not found or set to placeholder in .env"}

    try:
        client = anthropic.Anthropic(api_key=api_key)

        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        return {
            "content": message.content[0].text,
            "usage": {
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens
            }
        }

    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    # Simple test
    print("Testing Claude Bridge...")
    result = query_claude("Hello! Are you online?", model="claude-3-haiku-20240307")
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Response: {result['content']}")
        print(f"Usage: {result['usage']}")
