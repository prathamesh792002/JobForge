"""Debug script for skill extraction JSON issue."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types as genai_types
from src.config.settings import settings

JD = "Python ML Engineer. Needs PyTorch, FastAPI, Docker, AWS, Git, CI/CD."

PROMPT = """Extract skills from this JD into three tiers.
Return ONLY a JSON object with these keys:
{
  "tier_1_languages": [],
  "tier_2_frameworks_infra": [],
  "tier_3_methodologies": []
}

JD: """ + JD

async def main():
    key = settings.ALL_GEMINI_KEYS[0]
    client = genai.Client(api_key=key)

    print("=== Test 1: with response_mime_type (current code) ===")
    try:
        r = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=PROMPT,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=1024,
                response_mime_type="application/json",
            ),
        )
        print("text:", repr(r.text[:300] if r.text else None))
        print("finish_reason:", r.candidates[0].finish_reason if r.candidates else "N/A")
    except Exception as e:
        print("Error:", e)

    print("\n=== Test 2: without response_mime_type, with thinking disabled ===")
    try:
        r2 = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=PROMPT,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=1024,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        print("text:", repr(r2.text[:300] if r2.text else None))
        try:
            parsed = json.loads(r2.text or "")
            print("Parsed OK:", parsed)
        except Exception as pe:
            print("Parse error:", pe)
    except Exception as e:
        print("Error:", e)

asyncio.run(main())
