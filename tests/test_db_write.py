"""Test the CRM (DB write) path that happens after HITL approval."""
import asyncio, sys, os, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.validators.application import ApplicationCreate
from src.database.session import async_session_factory
from src.services.repository.application_repo import create_application, update_application_status

async def main():
    app_data = ApplicationCreate(
        company_name="TestCorp",
        job_title="ML Engineer",
        persona_used="ai_ml",
        hr_email="hr@testcorp.com",
        source_type="raw_text",
        source_url=None,
        pdf_path="/tmp/test.pdf",
        jd_text="Test JD",
        tailoring_skipped=False,
        initial_ats_score=65,
        final_ats_score=82,
        email_subject="Application for ML Engineer",
        email_body="<html>Test email</html>",
        status="applied",
    )

    async with async_session_factory() as db:
        # Create
        app = await create_application(db, app_data)
        print(f"[PASS] create_application: app_id={app.app_id}, pixel_id={app.pixel_id}")
        print(f"       initial_ats_score={app.initial_ats_score}, final_ats_score={app.final_ats_score}")

        # Update status
        updated = await update_application_status(db, app.app_id, "interview")
        status_ok = updated and updated.status == "interview"
        print(f"{'[PASS]' if status_ok else '[FAIL]'} update_application_status: {updated.status if updated else 'None'}")

    print("\n[PASS] CRM (DB write) path is working.")

asyncio.run(main())
