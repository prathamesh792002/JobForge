"""
JobForge Pipeline Test Suite
Tests all pipeline stages: Classifier, Extractor (LLM), Tailor, Compiler, Mailer
Run: python tests/run_tests.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"

JD_ML = (
    "We are hiring a Python Machine Learning Engineer to build and deploy LLMs. "
    "Must know PyTorch, HuggingFace transformers, MLflow, FastAPI, Docker, AWS, "
    "Git, and CI/CD pipelines. Experience with RAG and vector databases is a plus."
)
JD_SWE = (
    "Full-stack Java developer needed. Spring Boot, React, PostgreSQL, REST APIs, "
    "Kubernetes, Docker, Git, CI/CD, and microservices architecture required."
)
JD_GAME = (
    "Unity game developer with C#, Unreal Engine, gameplay programming, "
    "physics systems, and shader development experience needed."
)


async def test_classifier():
    print("\n--- STAGE 2: CLASSIFIER ---")
    from src.services.ai.classifier import classify_jd, classify_and_load

    tag = await classify_jd(JD_ML)
    status = PASS if tag == "ai_ml" else FAIL
    print(f"{status} classify_jd(ML): {tag}")

    tag2 = await classify_jd(JD_SWE)
    status = PASS if tag2 == "swe" else FAIL
    print(f"{status} classify_jd(SWE): {tag2}")

    tag3 = await classify_jd(JD_GAME)
    status = PASS if tag3 in ("game_dev", "swe") else FAIL
    print(f"{status} classify_jd(GAME): {tag3}")

    persona_tag, yaml_content, yaml_dict = await classify_and_load(JD_ML)
    status = PASS if yaml_content and isinstance(yaml_dict, dict) else FAIL
    print(f"{status} classify_and_load: persona={persona_tag}, yaml_len={len(yaml_content)}")

    return True


async def test_skill_extraction_and_ats():
    print("\n--- STAGE 2b: SKILL EXTRACTION + ATS SCORING ---")
    from src.services.ai.classifier import extract_categorized_skills, calculate_ats_score

    jd_skills = await extract_categorized_skills(JD_ML, cache_key_prefix="test")
    t1 = jd_skills["tier_1_languages"]
    t2 = jd_skills["tier_2_frameworks_infra"]
    t3 = jd_skills["tier_3_methodologies"]
    status = PASS if t1 or t2 else FAIL
    print(f"{status} extract_categorized_skills: T1={t1}, T2={t2[:3]}, T3={t3[:3]}")

    resume_skills = {
        "tier_1_languages": ["python"],
        "tier_2_frameworks_infra": ["fastapi", "docker", "pytorch"],
        "tier_3_methodologies": ["git", "ci/cd"],
    }
    score, missing, raw = calculate_ats_score(jd_skills, resume_skills)
    status = PASS if 0 <= score <= 100 else FAIL
    print(f"{status} calculate_ats_score: score={score}%, raw={raw}%, missing_count={len(missing)}")
    print(f"       Missing: {missing[:8]}")

    return True


async def test_llm_extraction():
    print("\n--- STAGE 1: LLM FIELD EXTRACTION ---")
    from src.services.ai.extractor import extract_structured_fields_via_llm

    result = await extract_structured_fields_via_llm(JD_ML)
    if result:
        status = PASS
        print(f"{status} extract_structured_fields_via_llm:")
        print(f"       title={result.get('job_title')}")
        print(f"       company={result.get('company_name')}")
        print(f"       email={result.get('hr_email')}")
        print(f"       skills={result.get('required_skills', [])[:5]}")
    else:
        print(f"{FAIL} extract_structured_fields_via_llm returned None")

    return True


async def test_tailoring():
    print("\n--- STAGE 3: TAILORING ---")
    from src.validators.application import ExtractionResult
    from src.services.ai.classifier import get_yaml_by_persona
    from src.services.ai.tailor import tailor_resume

    extraction = ExtractionResult(
        raw_jd_text=JD_ML,
        job_title="ML Engineer",
        company_name="TestCorp",
        hr_email="hr@testcorp.com",
        application_link="https://example.com/jobs/ml-engineer",
        required_skills=["python", "pytorch", "mlflow", "fastapi", "docker"],
        source_type="raw_text",
        word_count=len(JD_ML.split()),
    )

    base_yaml = get_yaml_by_persona("ai_ml")
    if not base_yaml:
        print(f"{FAIL} Could not load ai_ml persona YAML")
        return False

    print(f"       Base YAML loaded: {len(base_yaml)} chars")

    try:
        yaml_content, yaml_path, skipped, missing, initial_score, final_score = await tailor_resume(
            extraction=extraction,
            base_yaml_content=base_yaml,
            chat_id="test_user_123",
        )
        status = PASS if yaml_content else FAIL
        print(f"{status} tailor_resume:")
        print(f"       initial_score={initial_score}%, final_score={final_score}%")
        print(f"       skipped={skipped}, yaml_path={yaml_path}")
        print(f"       missing_skills={missing[:5]}")
        return yaml_path if yaml_content else None
    except Exception as e:
        print(f"{FAIL} tailor_resume raised: {e}")
        return None


async def test_pdf_compilation(yaml_path: str):
    print("\n--- STAGE 4: PDF COMPILATION ---")
    from src.services.document.compiler import compile_pdf

    try:
        pdf_path = await compile_pdf(yaml_path, use_custom_theme=True)
        print(f"{PASS} compile_pdf: {pdf_path}")
        import os
        size = os.path.getsize(pdf_path)
        print(f"       PDF size: {size:,} bytes")
        return pdf_path
    except Exception as e:
        print(f"{FAIL} compile_pdf raised: {e}")
        return None


async def test_email_draft(pdf_path: str):
    print("\n--- STAGE 6: EMAIL DRAFT ---")
    from src.services.communication.mailer import draft_email

    try:
        subject, html_body = await draft_email(
            candidate_name="Prathamesh Shirole",
            job_title="ML Engineer",
            company_name="TestCorp",
            pdf_path=pdf_path,
            jd_text=JD_ML,
        )
        status = PASS if subject and html_body else FAIL
        print(f"{status} draft_email:")
        print(f"       Subject: {subject}")
        print(f"       Body length: {len(html_body)} chars")
        return True
    except Exception as e:
        print(f"{FAIL} draft_email raised: {e}")
        return False


async def test_redis_connection():
    print("\n--- INFRASTRUCTURE: REDIS ---")
    try:
        from src.database.redis import get_redis
        r = await get_redis()
        await r.set("jobforge_test_key", "hello", ex=10)
        val = await r.get("jobforge_test_key")
        status = PASS if val == "hello" else FAIL
        print(f"{status} Redis ping + get/set: {val}")
        return True
    except Exception as e:
        print(f"{SKIP} Redis not reachable: {e}")
        return False


async def test_db_connection():
    print("\n--- INFRASTRUCTURE: DATABASE ---")
    try:
        from src.database.session import async_session_factory
        async with async_session_factory() as session:
            from sqlalchemy import text
            result = await session.execute(text("SELECT 1"))
            val = result.scalar()
            status = PASS if val == 1 else FAIL
            print(f"{status} DB SELECT 1: {val}")
        return True
    except Exception as e:
        print(f"{SKIP} Database not reachable: {e}")
        return False


async def main():
    print("=" * 55)
    print("  JobForge Pipeline Test Suite")
    print("=" * 55)

    results = {}

    # LLM / AI stages (don't need Docker)
    results["classifier"] = await test_classifier()
    results["ats_scoring"] = await test_skill_extraction_and_ats()
    results["llm_extraction"] = await test_llm_extraction()
    yaml_path = await test_tailoring()
    results["tailoring"] = bool(yaml_path)

    # PDF compilation (needs RenderCV + TinyTeX)
    pdf_path = None
    if yaml_path:
        pdf_path = await test_pdf_compilation(yaml_path)
        results["pdf_compilation"] = bool(pdf_path)
    else:
        print(f"\n--- STAGE 4: PDF COMPILATION ---\n{SKIP} Skipping (tailoring failed)")
        results["pdf_compilation"] = False

    # Email draft (needs Gemini, no SMTP sent)
    if pdf_path:
        results["email_draft"] = await test_email_draft(pdf_path)
    else:
        print(f"\n--- STAGE 6: EMAIL DRAFT ---\n{SKIP} Skipping (no PDF path)")
        results["email_draft"] = False

    # Infrastructure (optional - needs Docker)
    results["redis"] = await test_redis_connection()
    results["database"] = await test_db_connection()

    # Summary
    print("\n" + "=" * 55)
    print("  RESULTS SUMMARY")
    print("=" * 55)
    for name, passed in results.items():
        icon = PASS if passed else FAIL
        print(f"  {icon}  {name}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  {passed}/{total} tests passed")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
