---
name: text-humanizer
description: Detects and removes AI-generated patterns, artifacts, and metadata to produce authentic, human-sounding text. Use when the user wants to humanize text, bypass AI detection, or make writing sound less robotic.
---

# Text Humanizer

This skill specializes in rewriting text to remove the statistical and stylistic patterns common to Large Language Models (LLMs). It targets the "uncanny valley" of perfect grammar and predictable vocabulary.

## When to use this skill
- When the user explicitly asks to "humanize" text.
- When the user wants to remove "AI flavor," "robotic tone," or "ChatGPT-isms" from a draft.
- When text contains obvious AI artifacts (e.g., "As an AI language model").
- When the goal is to bypass AI detection software by increasing "burstiness" and "perplexity."

## Humanization Protocol

### 1. Sanitize Metadata & Artifacts
Immediate removal of non-content markers is the first step.
- **Preambles/Disclaimers:** Remove "As an AI language model...", "Here is the text...", "I cannot fulfill...", "Note that...".
- **Chat Residue:** Remove conversational fillers like "Sure!", "Certainly!", "I can help with that."
- **Formatting:** Collapse excessive bullet point lists into paragraphs if they disrupt the narrative flow.

### 2. The "AI Vocabulary" Blacklist
Scan for and replace these words/phrases. They are statistically over-represented in LLM outputs:

**The "Red Flags" (Aggressively Replace):**
- *Delve* (e.g., "Let's delve into...")
- *Tapestry* (e.g., "A rich tapestry of...")
- *Landscape* (e.g., "In the evolving landscape...")
- *Nuance / Nuanced*
- *Underscore*
- *Paramount*
- *Crucial / Pivotal*
- *Symphony* (e.g., "A symphony of flavors")
- *Leverage* (as a verb for "use")
- *Testament* (e.g., "A testament to...")
- *Game-changer*
- *Foster* (e.g., "foster innovation")
- *Beacon* (e.g., "A beacon of hope")
- *Myriad*
- *Unleash*

**The "Glue" (Predictable Transitions):**
- *In conclusion / In summary* (Replace with a final strong thought or cut entirely)
- *Furthermore / Moreover / Additionally* (Replace with "Also," "Plus," or just start the sentence)
- *It is important to note that* (Cut this. Just say the thing.)
- *On the other hand*
- *Conversely*

### 3. Structural Rewriting
AI text is often characterized by "low perplexity" (predictability) and "low burstiness" (uniform sentence structure).

*   **Vary Sentence Length:** Mix very short, punchy sentences with longer, meandering ones.
    *   *AI:* "The cat sat on the mat. It was a sunny day. The cat was happy."
    *   *Human:* "The cat sat on the mat, soaking up the sun. Pure bliss."
*   **Active Voice:** Flip passive constructions.
    *   *AI:* "It can be argued that..." -> *Human:* "Critics argue..."
    *   *AI:* "Implementation was achieved by..." -> *Human:* "The team implemented..."
*   **Directness:** Remove hedging.
    *   *AI:* "It suggests that it might be likely..." -> *Human:* "It suggests..."
*   **Idioms & Phrasal Verbs:** Use "look into" instead of "examine," "set up" instead of "establish."

## Examples

### Example 1: Corporate Speak
**Input:**
> "In the rapidly evolving landscape of digital marketing, it is paramount to leverage data analytics. Furthermore, this approach underscores the importance of understanding customer nuances. It is a testament to the power of technology."

**Humanized Output:**
> "Digital marketing moves fast, so using data analytics is a must. It really highlights how important it is to get your customers. That's the real power of tech."

### Example 2: Creative Writing
**Input:**
> "The forest was a symphony of sounds. The trees stood as sentinels, creating a rich tapestry of green. Let us delve into the mystery of the woods."

**Humanized Output:**
> "The forest was loud. Trees stood like guards, a massive wall of green. Let's see what's hiding in there."

## Constraints
- **Meaning:** Do not alter the factual accuracy or core message.
- **Tone:** Match the requested persona. A medical paper should still sound professional, just not robotic.
- **Context:** If the text is code or a specific data format, do not humanize the syntax.

