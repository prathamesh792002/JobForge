import asyncio
import logging
from src.services.ai.tailor import tailor_resume, PoorFitError
# pyrefly: ignore [missing-import]
from src.validators.application import ExtractionResult

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_a1_poor_fit():
    print("--- RUNNING TEST A1: Poor Fit Rejection ---")
    jd_text = "We are looking for a Registered Nurse with 5 years of experience in patient care, IV administration, and EHR software."
    
    extraction = ExtractionResult(
        raw_jd_text=jd_text,
        job_title="Registered Nurse",
        company_name="City Hospital",
        required_skills=["patient care", "IV", "EHR"],
        source_type="raw_text",
        word_count=50
    )
    
    try:
        await tailor_resume(extraction, "swe", "test_chat_id")
        print("FAIL: tailor_resume completed successfully instead of raising PoorFitError.")
    except PoorFitError as e:
        print(f"PASS: Caught PoorFitError: {str(e)}")
    except Exception as e:
        print(f"FAIL: Caught unexpected exception: {str(e)}")
        raise e

async def main():
    await test_a1_poor_fit()

if __name__ == "__main__":
    asyncio.run(main())
