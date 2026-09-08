import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

from app.config import settings
from app.services.vision import extract_from_image

async def run_test():
    print("\n=============================================")
    print("      MULTI-PROVIDER FALLBACK TEST           ")
    print("=============================================\n")
    
    # Force the first key to fail, but provide a valid secondary key
    # For testing purposes, we'll just show the logging output
    original_key = settings.GOOGLE_API_KEY
    settings.GOOGLE_API_KEY = "INVALID_KEY_1"
    
    # We will temporarily inject a dummy key into ALL_GEMINI_KEYS array
    # using a monkeypatch just for this test script so we can observe
    # the rotation logic without actually needing a real secondary key
    from unittest.mock import patch, PropertyMock
    
    dummy_keys = [
        "INVALID_KEY_1",
        "INVALID_KEY_2", 
        "INVALID_KEY_3"
    ]
    
    print("[TEST] Forced settings.ALL_GEMINI_KEYS to have 3 invalid keys to test rotation.")
    print("---------------------------------------------\n")
    
    # Create a tiny 1x1 black JPEG image to test the API bytes
    tiny_jpeg = bytes.fromhex(
        "ffd8ffe000104a46494600010101004800480000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc00011080001000103012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fdd08a28a00f"
    )

    try:
        with patch("app.config.Settings.ALL_GEMINI_KEYS", new_callable=PropertyMock) as mock_keys:
            mock_keys.return_value = dummy_keys
            result = await extract_from_image(tiny_jpeg, "test_file_id")
            print("\n✅ SUCCESS: Fallback layer successfully parsed the image data!")
            print(f"Title: {result.job_title}")
    except Exception as e:
        print(f"\n❌ FAILED: All providers returned errors. Final error: {str(e)}")
        print("Note: This is expected if your Anthropic/OpenAI keys are invalid or absent!")

if __name__ == "__main__":
    asyncio.run(run_test())
