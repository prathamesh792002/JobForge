import asyncio
import logging
from uuid import uuid4
from pathlib import Path

from app.schemas.application import ExtractionResult
from app.services.tailor import tailor_resume
from app.services.mailer import draft_email, send_email

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("e2e_test")

async def dummy_progress(msg: str):
    print(f"UI UPDATE: {msg}")

async def run_e2e_test():
    logger.info("Starting End-to-End Test...")

    # 1. Fake JD Extraction
    extraction = ExtractionResult(
        company_name="Google",
        job_title="AI Engineer",
        raw_jd_text="We are looking for a Software Engineer with 3+ years of Python, PyTorch, and NLP experience to build agentic AI systems.",
        hr_email="hr@google.com",
        source_type="link",
        application_link="http://google.com/careers",
        word_count=50,
        character_count=300
    )

    # 2. Tailor YAML & Compile PDF
    logger.info("--- 1. Extraction & Tailoring ---")
    persona_tag = "SWE"
    pdf_path, initial_score, final_score, skipped = await tailor_resume(
        chat_id="dummy",
        extraction=extraction, 
        persona_tag=persona_tag, 
        progress_callback=dummy_progress
    )
    logger.info(f"PDF Generated at: {pdf_path}")
    logger.info(f"ATS Score jumped from {initial_score}% -> {final_score}%")

    # 3. Draft Email Hook
    logger.info("--- 2. Synthesizing Cold Email ---")
    subject, html_body = await draft_email(
        candidate_name="Prathamesh Shirole",
        job_title=extraction.job_title,
        company_name=extraction.company_name,
        pdf_path=pdf_path,
        jd_text=extraction.raw_jd_text
    )
    
    print(f"\n--- SYNTHESIZED EMAIL ---")
    print(f"Subject: {subject}")
    print(f"Body: {html_body}\n-------------------------\n")

    # 4. Dispatch Email (will gracefully fail if no password)
    logger.info("--- 3. Dispatching Email via SMTP ---")
    pixel_id = uuid4()
    success = await send_email(
        to_email=extraction.hr_email,
        subject=subject,
        html_body=html_body,
        pdf_path=pdf_path,
        pixel_id=pixel_id
    )
    
    if not success:
        logger.warning("SMTP Dispatch aborted (expected if .env lacks App Password).")
    else:
        logger.info("SMTP Dispatch successful!")
        
    logger.info("E2E Test completed.")

if __name__ == "__main__":
    asyncio.run(run_e2e_test())
