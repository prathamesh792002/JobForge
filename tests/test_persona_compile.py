"""Verify all 3 persona YAMLs compile to PDF without errors."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.ai.classifier import get_yaml_by_persona
from src.services.document.compiler import compile_pdf
from src.services.ai.tailor import _save_yaml_to_temp

async def main():
    results = {}
    for persona in ["ai_ml", "swe", "game_dev"]:
        yaml_content = get_yaml_by_persona(persona)
        yaml_path = _save_yaml_to_temp(yaml_content, f"test_{persona}")
        try:
            pdf_path = await compile_pdf(yaml_path, use_custom_theme=True)
            size_kb = os.path.getsize(pdf_path) // 1024
            print(f"[PASS] {persona}: {size_kb} KB  ->  {pdf_path}")
            results[persona] = True
        except Exception as e:
            print(f"[FAIL] {persona}: {e}")
            results[persona] = False

    passed = sum(results.values())
    print(f"\n{passed}/3 persona YAMLs compiled successfully.")

asyncio.run(main())
