"""
Full end-to-end pipeline test with a real job posting URL.
Tests: URL scraping → classification → ATS scoring → tailoring → PDF → email draft
Run: python tests/test_e2e_url.py
"""
import asyncio, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

JOB_URL = "https://job-boards.greenhouse.io/cloudflare/jobs/7914628"

PASS = "[PASS]"
FAIL = "[FAIL]"
SEP  = "-" * 60


async def main():
    print("=" * 60)
    print("  JobForge — Full E2E Pipeline Test")
    print(f"  URL: {JOB_URL}")
    print("=" * 60)

    # ── Stage 1: URL Extraction ──────────────────────────────────
    print(f"\n{SEP}\nSTAGE 1: URL EXTRACTION (Playwright)\n{SEP}")
    t0 = time.time()
    from src.services.ai.extractor import extract_from_url
    try:
        extraction = await extract_from_url(JOB_URL)
        elapsed = time.time() - t0
        print(f"{PASS} extract_from_url ({elapsed:.1f}s)")
        print(f"  Job title  : {extraction.job_title}")
        print(f"  Company    : {extraction.company_name}")
        print(f"  HR email   : {extraction.hr_email}")
        print(f"  Word count : {extraction.word_count}")
        print(f"  Skills     : {extraction.required_skills[:6]}")
        print(f"  Raw text   : {extraction.raw_jd_text[:200].strip()}...")
    except Exception as e:
        print(f"{FAIL} extract_from_url: {e}")
        return

    # ── Stage 2: Classification ──────────────────────────────────
    print(f"\n{SEP}\nSTAGE 2: CLASSIFICATION\n{SEP}")
    t0 = time.time()
    from src.services.ai.classifier import classify_jd, get_yaml_by_persona
    try:
        persona_tag = await classify_jd(extraction.raw_jd_text)
        base_yaml = get_yaml_by_persona(persona_tag)
        elapsed = time.time() - t0
        print(f"{PASS} classify_jd ({elapsed:.1f}s): persona = {persona_tag}")
        print(f"  Base YAML  : {len(base_yaml)} chars")
    except Exception as e:
        print(f"{FAIL} classify_jd: {e}")
        return

    # ── Stage 2b: ATS Scoring ────────────────────────────────────
    print(f"\n{SEP}\nSTAGE 2b: SKILL EXTRACTION + ATS SCORING\n{SEP}")
    t0 = time.time()
    from src.services.ai.classifier import extract_categorized_skills, calculate_ats_score, _extract_resume_text
    try:
        jd_skills    = await extract_categorized_skills(extraction.raw_jd_text, cache_key_prefix="e2e_test")
        resume_text  = _extract_resume_text(base_yaml)
        resume_skills = await extract_categorized_skills(resume_text)
        score, missing, raw = calculate_ats_score(jd_skills, resume_skills)
        elapsed = time.time() - t0
        print(f"{PASS} ATS scoring ({elapsed:.1f}s)")
        print(f"  JD T1 langs   : {jd_skills['tier_1_languages']}")
        print(f"  JD T2 infra   : {jd_skills['tier_2_frameworks_infra'][:5]}")
        print(f"  Resume T1     : {resume_skills['tier_1_languages']}")
        print(f"  Initial score : {score}% (raw {raw}%)")
        print(f"  Missing       : {missing[:8]}")
    except Exception as e:
        print(f"{FAIL} ATS scoring: {e}")
        return

    # ── Stage 3: Tailoring ───────────────────────────────────────
    print(f"\n{SEP}\nSTAGE 3: TAILORING (self-healing loop)\n{SEP}")
    t0 = time.time()
    from src.services.ai.tailor import tailor_resume

    async def progress(msg):
        clean = msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        clean = clean.replace("&amp;", "&").replace("&lt;", "<")
        print(f"  > {clean.strip()}")

    try:
        yaml_content, yaml_path, skipped, missing_out, init_score, final_score = await tailor_resume(
            extraction=extraction,
            base_yaml_content=base_yaml,
            chat_id="e2e_test",
            progress_callback=progress,
        )
        elapsed = time.time() - t0
        print(f"{PASS} tailor_resume ({elapsed:.1f}s)")
        print(f"  Skipped      : {skipped}")
        print(f"  Score        : {init_score}% → {final_score}%")
        print(f"  YAML path    : {yaml_path}")
        print(f"  YAML size    : {len(yaml_content)} chars")
    except Exception as e:
        print(f"{FAIL} tailor_resume: {e}")
        return

    # ── Stage 4: PDF Compilation ─────────────────────────────────
    print(f"\n{SEP}\nSTAGE 4: PDF COMPILATION (RenderCV)\n{SEP}")
    t0 = time.time()
    from src.services.document.compiler import compile_pdf
    try:
        pdf_path = await compile_pdf(yaml_path, use_custom_theme=True)
        elapsed = time.time() - t0
        size_kb = os.path.getsize(pdf_path) // 1024
        print(f"{PASS} compile_pdf ({elapsed:.1f}s)")
        print(f"  PDF path : {pdf_path}")
        print(f"  PDF size : {size_kb} KB")
    except Exception as e:
        print(f"{FAIL} compile_pdf: {e}")
        return

    # ── Stage 5: Email Draft ─────────────────────────────────────
    print(f"\n{SEP}\nSTAGE 5: EMAIL DRAFT\n{SEP}")
    t0 = time.time()
    from src.services.communication.mailer import draft_email
    try:
        subject, html_body = await draft_email(
            candidate_name="Prathamesh Shirole",
            job_title=extraction.job_title or "ML Engineer",
            company_name=extraction.company_name or "Company",
            pdf_path=pdf_path,
            jd_text=extraction.raw_jd_text,
        )
        elapsed = time.time() - t0
        print(f"{PASS} draft_email ({elapsed:.1f}s)")
        print(f"  Subject     : {subject}")
        print(f"  Body length : {len(html_body)} chars")
        print(f"  Preview     : {html_body[:200].strip()}...")
    except Exception as e:
        print(f"{FAIL} draft_email: {e}")
        return

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("  PIPELINE COMPLETE — All stages passed")
    print(f"  Job   : {extraction.job_title} @ {extraction.company_name}")
    print(f"  ATS   : {init_score}% → {final_score}%")
    print(f"  PDF   : {size_kb} KB")
    print(f"  Email : {subject}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
