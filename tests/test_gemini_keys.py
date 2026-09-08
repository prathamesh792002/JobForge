"""Test all Gemini API keys individually."""
import asyncio, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from google import genai
from google.genai import types as genai_types
from src.config.settings import settings

async def main():
    keys = settings.ALL_GEMINI_KEYS
    print(f"Testing {len(keys)} Gemini API keys against model: {settings.GEMINI_MODEL}\n")

    working = 0
    failed = 0

    for i, key in enumerate(keys):
        label = f"Key #{i+1} (...{key[-8:]})"
        try:
            client = genai.Client(api_key=key)
            start = time.time()
            resp = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents="Reply with exactly: OK",
                config=genai_types.GenerateContentConfig(temperature=0.0, max_output_tokens=5),
            )
            elapsed = time.time() - start
            text = (resp.text or "").strip()
            print(f"  [PASS] {label} -> \"{text}\" ({elapsed:.1f}s)")
            working += 1
        except Exception as e:
            err = str(e)[:100]
            print(f"  [FAIL] {label} -> {err}")
            failed += 1

    print(f"\nResults: {working}/{len(keys)} working, {failed} failed")
    if failed == 0:
        print("All keys are healthy!")
    else:
        print(f"WARNING: {failed} key(s) are broken!")

asyncio.run(main())
