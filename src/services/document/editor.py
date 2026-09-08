"""
Resume Editor Service — HITL Manual Override

Applies targeted natural language edits to the resume YAML using Gemini 2.5 Flash
with round-robin key rotation across all configured GEMINI_API_KEY_* keys.
Ensures zero collateral damage and preserves the design block format.
"""

import json
import logging
from typing import Optional

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings
from src.services.ai.tailor import _strip_design_block, _force_merge_design

logger = logging.getLogger(__name__)

EDITOR_SYSTEM_PROMPT = """You are an elite Resume Editor AI. Your task is to apply a specific, targeted correction to a candidate's YAML resume based on their exact instruction.

ABSOLUTE RULES — violating any of these invalidates your output:
1. TARGETED EDITING ONLY: You must ONLY modify the specific bullet point, sentence, or section requested in the Edit Instruction.
2. ZERO COLLATERAL DAMAGE: DO NOT rewrite, optimize, or alter any other part of the resume. Do not touch adjacent skills, summaries, or other jobs unless explicitly asked.
3. NO LAZY TRUNCATION: You must output the ENTIRE, complete YAML file. Do NOT use placeholders like "..." or "[rest of resume remains the same]".
4. DO NOT add new experience entries, education entries, or project entries.
5. Preserve the entire YAML structure and all formatting conventions. Use Markdown bold (**text**) not LaTeX bold.
6. Do NOT include the `design` block in your output.
7. ESCAPE PROPERLY: Because your output is JSON, ensure the multiline YAML string is perfectly escaped (e.g., using \\n for newlines and escaping quotes) so it passes standard JSON parsers.

<user_instruction>
{user_instruction}
</user_instruction>

<current_resume_yaml>
{current_yaml}
</current_resume_yaml>

OUTPUT FORMAT:
Return a single JSON object with this exact schema:
{{
  "updated_yaml": "The complete, properly escaped updated YAML resume content"
}}
Do not return any other text, markdown blocks, or explanation outside of this JSON object."""


async def apply_manual_edit(base_yaml_content: str, user_instruction: str) -> Optional[str]:
    """
    Apply a targeted natural language edit to the YAML resume.

    Strips the design block, sends to Gemini 2.5 Flash with strict constraints
    using round-robin key rotation, then force-merges the design block back.

    Args:
        base_yaml_content: The current, full YAML content of the resume.
        user_instruction: The user's specific natural language edit request.

    Returns:
        The fully updated, perfectly formatted YAML string, or None if failed.
    """
    gemini_keys = settings.ALL_GEMINI_KEYS
    if not gemini_keys:
        logger.error("No Gemini API keys configured for manual edit.")
        return None

    yaml_without_design = _strip_design_block(base_yaml_content)
    prompt = EDITOR_SYSTEM_PROMPT.format(
        user_instruction=user_instruction,
        current_yaml=yaml_without_design,
    )

    response_text = None
    last_error = None

    for i, api_key in enumerate(gemini_keys):
        try:
            logger.info("Calling Gemini 2.5 Flash for manual edit (key #%d)...", i + 1)
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=8192,
                ),
            )
            if not response.text:
                raise ValueError("Gemini returned empty response (possible token limit issue)")
            response_text = response.text.strip()
            break
        except Exception as e:
            last_error = str(e)
            logger.warning("Gemini manual edit failed with key #%d: %s", i + 1, last_error)

    if not response_text:
        logger.error("All Gemini keys failed for manual edit. Last error: %s", last_error)
        return None

    # Strip markdown code fences if the model hallucinated them
    if response_text.startswith("```json"):
        response_text = response_text[len("```json"):].strip()
    elif response_text.startswith("```"):
        response_text = response_text[3:].strip()
    if response_text.endswith("```"):
        response_text = response_text[:-3].strip()

    try:
        parsed_json = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("Gemini returned invalid JSON during manual edit: %s", str(e))
        logger.error("Raw response: %s", response_text[:500])
        return None

    updated_yaml = parsed_json.get("updated_yaml", "")
    if not updated_yaml:
        logger.error("Gemini returned empty updated_yaml during manual edit")
        return None

    final_yaml = _force_merge_design(updated_yaml, base_yaml_content)
    logger.info("Manual edit applied and design block merged successfully.")
    return final_yaml
