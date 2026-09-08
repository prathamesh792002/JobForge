"""
Resume Tailoring Service — Self-Healing Optimization Loop

Implements the Intelligent Evaluation Brain's iterative optimization engine.
Uses Claude 3.5 Sonnet to progressively improve ATS keyword matching through:
  - Vocabulary Harmonization (Tier 3 gaps)
  - Semantic Bridging (Tier 2 gaps)
  - Dynamic Summary Rewriting
  - Design block force-merge (guarantees custom theme is never lost)

The loop runs up to 2 refinement passes until the ATS score reaches >= 80%
or the iteration limit is exhausted.

Pipeline position: C4 — called after classification, before PDF compilation.
"""

import logging
from typing import Callable, Awaitable
import os
import sys
import tempfile
import time
import json
import yaml
from pathlib import Path
from typing import List, Optional, Tuple

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings
from src.validators.application import ExtractionResult
from src.services.ai.classifier import (
    extract_categorized_skills,
    calculate_ats_score,
    _extract_resume_text,
)

logger = logging.getLogger(__name__)

# ── Target score threshold ────────────────────────────────────
TARGET_ATS_SCORE = 80

class PoorFitError(Exception):
    """Raised when the initial ATS score is below the minimum threshold."""
    def __init__(self, message: str, missing_skills: List[str], score: int):
        super().__init__(message)
        self.missing_skills = missing_skills
        self.score = score

# ══════════════════════════════════════════════════════════════
#  LLM PROMPTS
# ══════════════════════════════════════════════════════════════

TAILORING_SYSTEM_PROMPT = """You are an elite ATS resume optimization specialist.

Your task: Update the candidate's resume YAML to improve ATS keyword matching for a specific job.

You will be given:
1. The Job Description text
2. The candidate's current resume in YAML format
3. A list of MISSING SKILLS that the resume currently lacks compared to the JD
4. The current ATS match score and the target score

ABSOLUTE RULES — violating any of these invalidates your output:
1. DO NOT change, remove, or invent any: company names, job titles, employment dates,
   educational institutions, degree names, or GPA values.
2. DO NOT add new experience entries, education entries, or project entries.
3. VOCABULARY HARMONIZATION: Rewrite existing bullet points to use exact JD terminology
   (e.g., change 'RDBMS experience' to 'MySQL databases' if MySQL is in the JD).
4. SEMANTIC BRIDGING: If the resume has a related tool (e.g., Docker) but the JD requires
   an adjacent one (e.g., Kubernetes), modify the bullet to show adjacency
   (e.g., 'Utilized Docker within containerized environments targeting Kubernetes orchestration').
5. UPDATE the `technical_skills` section CONSERVATIVELY. Only append a missing skill if the
   candidate plausibly already has it based on their existing skills, experience, or projects
   (e.g., add 'Kubernetes' only if they already show Docker/containerization). Do NOT append
   skills the candidate has no basis for — a recruiter will test these in the interview and
   fabricated skills destroy credibility. Whenever possible, demonstrate a skill by weaving the
   JD keyword into an EXISTING bullet as evidence rather than merely listing it.
6. DYNAMIC SUMMARY INJECTION: You MUST generate a strict 2-sentence elevator pitch wrapped in quotes for the `summary` section.
   - Sentence 1: State the candidate's core identity and total experience tailored to the role.
   - Sentence 2: Explicitly name-drop the highest-priority framework/skill from the target job description.
7. Keep all modifications contextually accurate — no keyword stuffing.
8. THE "CONTENT BUDGETING" RULE (Strict 1-Page Enforcement): You must strictly enforce the following text limits to prevent overflow:
   - Professional Summary: Max 2 sentences / 45 words.
   - Current/Active Role: Max 4 bullet points.
   - Past Roles: Max 2 bullet points.
   - Academic Projects: Max 2 bullet points per project.
   If the existing text exceeds these limits, AGGRESSIVELY PRUNE the oldest or least relevant bullet points before compilation.
9. Preserve the entire YAML structure, all sections including `certifications_and_leadership`,
   and all formatting conventions. Use Markdown bold (**text**) not LaTeX bold.
10. Do NOT include the `design` block in your output — it will be merged automatically.

<prompt_rules>
CRITICAL SCHEMA RULE: You must return the EXACT RenderCV YAML schema. Do not change, rename, or delete any keys under cv: sections:. If you destroy the education, experience, projects, or skills hierarchy, the compiler will fail and output a blank page.

ONLY edit the text inside the highlights lists.

ONLY inject exactly two sentences into the summary list.

DO NOT alter the design block under any circumstances.
</prompt_rules>

<compiler_rules>
1. NO MARKDOWN: Output ONLY raw YAML text. NEVER wrap your response in ```yaml or ``` blocks.
2. SUMMARY SCHEMA: The `summary` key MUST be formatted as a list containing a single string. 
   CORRECT:
   summary:
     - "Sentence 1. Sentence 2."
   INCORRECT: 
   summary: "Sentence 1. Sentence 2."
3. NO LATEX POISON: Do NOT use special characters like &, %, $, or # in the text. Always spell them out (e.g., use "and" instead of "&", "percent" instead of "%"). 
</compiler_rules>

OUTPUT FORMAT:
Return a single JSON object with this exact schema:
{
  "updated_yaml": "The complete updated YAML resume content (without the design block)"
}
Do not return any other text, markdown, or explanation outside of this JSON object."""


TAILORING_USER_PROMPT = """JOB DESCRIPTION:
{jd_text}

---

CANDIDATE RESUME (YAML):
{base_yaml_content}

---

MISSING SKILLS TO INJECT (from ATS analysis):
{missing_skills}

CURRENT ATS SCORE: {current_score}%
TARGET ATS SCORE: {target_score}%

Optimize the resume to close the gap. Return valid JSON only."""


# ══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════

async def tailor_resume(
    extraction: ExtractionResult,
    base_yaml_content: str,
    chat_id: str,
    progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Tuple[str, str, bool, List[str], int, int]:
    """
    Tailor a resume YAML to match a job description using the
    Self-Healing Optimization Loop.

    Args:
        extraction: ExtractionResult containing the JD text and metadata.
        base_yaml_content: Raw YAML string of the base persona resume.
        chat_id: User's Telegram chat ID (used for temp file naming).
        progress_callback: Optional async callable to report live status
            updates back to the caller (e.g., Telegram status message).

    Returns:
        Tuple of (yaml_content, yaml_file_path, tailoring_skipped,
                  missing_skills, initial_score, final_score).
    """
    async def _report(msg: str):
        if progress_callback:
            try:
                await progress_callback(msg)
            except Exception:
                pass
    # ── Sparse JD Gate ───────────────────────────────────────
    if extraction.word_count < 40:
        logger.info(
            "Sparse JD gate triggered (word_count=%d < 40). Skipping full tailoring; rewriting summary only.",
            extraction.word_count,
        )
        await _report("⚙️ <b>JD is sparse — rewriting summary for this role...</b>")
        optimized_yaml = await _run_single_llm_pass(extraction.raw_jd_text, base_yaml_content, [], 0)
        if optimized_yaml:
            optimized_yaml = _force_merge_design(optimized_yaml, base_yaml_content)
            yaml_path = _save_yaml_to_temp(optimized_yaml, chat_id)
            return optimized_yaml, yaml_path, True, [], 0, 0
        yaml_path = _save_yaml_to_temp(base_yaml_content, chat_id)
        return base_yaml_content, yaml_path, True, [], 0, 0

    # ── Step 1: Extract categorized skills ────────────────────
    await _report("🔍 <b>Extracting &amp; categorizing skills from JD and resume...</b>")
    logger.info("Extracting categorized skills from JD and resume...")

    jd_skills = await extract_categorized_skills(extraction.raw_jd_text, cache_key_prefix="jd")
    resume_text = _extract_resume_text(base_yaml_content)
    # Cache resume skills by content hash. The base resume is a fixed file, so
    # this makes the resume side of the ATS score fully deterministic across runs
    # (Gemini is not guaranteed deterministic even at temperature=0) and avoids a
    # redundant LLM call on every pipeline run.
    resume_skills = await extract_categorized_skills(resume_text, cache_key_prefix="resume")

    # Retry once on empty results before trusting the "API failed / empty" verdict.
    # Transient Gemini failures return empty and are NOT cached, so a second call
    # re-hits the API. A genuinely skill-less JD stays empty (its result IS cached)
    # and correctly falls through to the bypass below — so this only recovers real
    # transient failures, it doesn't loop on legitimately empty input.
    def _has_skills(s: dict) -> bool:
        return any(
            len(s.get(t, [])) > 0
            for t in ("tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies")
        )

    if not _has_skills(jd_skills):
        logger.info("JD skills empty — retrying extraction once (possible transient failure).")
        jd_skills = await extract_categorized_skills(extraction.raw_jd_text, cache_key_prefix="jd")
    if not _has_skills(resume_skills):
        logger.info("Resume skills empty — retrying extraction once (possible transient failure).")
        resume_skills = await extract_categorized_skills(resume_text, cache_key_prefix="resume")

    logger.info(
        "JD skills — T1: %s | T2: %s | T3: %s",
        jd_skills["tier_1_languages"],
        jd_skills["tier_2_frameworks_infra"][:5],
        jd_skills["tier_3_methodologies"][:5],
    )
    logger.info(
        "Resume skills — T1: %s | T2: %s | T3: %s",
        resume_skills["tier_1_languages"],
        resume_skills["tier_2_frameworks_infra"][:5],
        resume_skills["tier_3_methodologies"][:5],
    )

    # Guard: if JD skill extraction returned all-empty tiers, the API failed —
    # don't reject based on a false 0% score.
    jd_extraction_ok = any(
        len(jd_skills.get(tier, [])) > 0
        for tier in ["tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies"]
    )
    if not jd_extraction_ok:
        logger.warning(
            "JD skill extraction returned empty (API failure). "
            "Bypassing ATS gate and running a direct tailoring pass."
        )
        await _report("⚠️ <b>Skill API unavailable — running direct tailoring pass...</b>")
        optimized_yaml = await _run_single_llm_pass(
            extraction.raw_jd_text, base_yaml_content, [], 0
        )
        if optimized_yaml:
            optimized_yaml = _force_merge_design(optimized_yaml, base_yaml_content)
            yaml_path = _save_yaml_to_temp(optimized_yaml, chat_id)
            return optimized_yaml, yaml_path, False, [], 0, 0
        yaml_path = _save_yaml_to_temp(base_yaml_content, chat_id)
        return base_yaml_content, yaml_path, False, [], 0, 0

    # Guard: if resume extraction returned all-empty tiers, the API failed —
    # this would produce a false 0% score and a spurious PoorFitError rejection.
    # Bypass ATS scoring and run a direct tailoring pass instead.
    resume_extraction_ok = any(
        len(resume_skills.get(tier, [])) > 0
        for tier in ["tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies"]
    )
    if not resume_extraction_ok:
        logger.warning(
            "Resume skill extraction returned empty (API failure). "
            "Bypassing ATS gate and running a direct tailoring pass."
        )
        await _report("⚠️ <b>Skill API unavailable — running direct tailoring pass...</b>")
        optimized_yaml = await _run_single_llm_pass(
            extraction.raw_jd_text, base_yaml_content, [], 0
        )
        if optimized_yaml:
            optimized_yaml = _force_merge_design(optimized_yaml, base_yaml_content)
            yaml_path = _save_yaml_to_temp(optimized_yaml, chat_id)
            return optimized_yaml, yaml_path, False, [], 0, 0
        yaml_path = _save_yaml_to_temp(base_yaml_content, chat_id)
        return base_yaml_content, yaml_path, False, [], 0, 0

    # ── Step 2: Compute initial ATS score ─────────────────────
    await _report("📊 <b>Computing initial ATS match score...</b>")
    initial_score, missing_skills, raw_score = calculate_ats_score(jd_skills, resume_skills)

    logger.info(
        "Initial ATS evaluation — Score: %d%% (raw: %d%%) | Missing: %s",
        initial_score, raw_score,
        ", ".join(missing_skills[:10]) or "none",
    )

    if initial_score < settings.MINIMUM_FIT_THRESHOLD:
        # Semantic rescue: keyword overlap can miss meaning-level fit (e.g. the JD
        # says "distributed systems", the resume says "scaled data pipelines").
        # Before rejecting, check embedding-based similarity — if the role is
        # semantically a real match, proceed instead of raising PoorFitError.
        semantic = None
        if settings.SEMANTIC_MATCH_ENABLED:
            try:
                from src.services.ai.classifier import semantic_fit_score
                semantic = await semantic_fit_score(extraction.raw_jd_text, resume_text)
            except Exception as e:
                logger.warning("Semantic fit check failed (non-fatal): %s", str(e))

        if semantic is not None and semantic >= settings.SEMANTIC_RESCUE_THRESHOLD:
            logger.info(
                "Semantic rescue: keyword %d%% < %d%% but semantic fit %d%% ≥ %d%% — proceeding.",
                initial_score, settings.MINIMUM_FIT_THRESHOLD,
                semantic, settings.SEMANTIC_RESCUE_THRESHOLD,
            )
            await _report(
                f"🧠 <b>Keyword score is low ({initial_score}%), but semantic fit is "
                f"{semantic}%</b> — this role is a real match. Proceeding with tailoring..."
            )
        else:
            raise PoorFitError(
                f"Initial ATS score {initial_score}% is below the minimum threshold of {settings.MINIMUM_FIT_THRESHOLD}%.",
                missing_skills,
                initial_score
            )

    # ── Step 3: Check if tailoring is needed ──────────────────
    if initial_score >= TARGET_ATS_SCORE:
        logger.info(
            "Initial score %d%% >= target %d%%. Skipping tailoring.",
            initial_score, TARGET_ATS_SCORE,
        )
        # Still rewrite summary for the specific JD even if score is high
        current_yaml = base_yaml_content
        final_yaml = await _run_single_llm_pass(
            extraction.raw_jd_text, current_yaml, missing_skills, initial_score
        )
        if final_yaml:
            final_yaml = _force_merge_design(final_yaml, base_yaml_content)
            yaml_path = _save_yaml_to_temp(final_yaml, chat_id)
            return final_yaml, yaml_path, False, missing_skills, initial_score, initial_score
        else:
            yaml_path = _save_yaml_to_temp(base_yaml_content, chat_id)
            return base_yaml_content, yaml_path, False, missing_skills, initial_score, initial_score

    # ── Step 4: Self-Healing Optimization Loop ────────────────
    current_yaml = base_yaml_content
    current_score = initial_score
    current_missing = missing_skills

    for pass_num in range(1, settings.MAX_OPTIMIZATION_PASSES + 1):
        await _report(
            f"⚙️ <b>Running optimization pass {pass_num}/{settings.MAX_OPTIMIZATION_PASSES}</b>\n"
            f"<i>Current score: {current_score}%</i> ➡️ <i>Target: {TARGET_ATS_SCORE}%</i>"
        )
        logger.info(
            "=== Optimization Pass %d/%d (score: %d%%, target: %d%%) ===",
            pass_num, settings.MAX_OPTIMIZATION_PASSES, current_score, TARGET_ATS_SCORE,
        )

        # Run LLM optimization pass
        optimized_yaml = await _run_single_llm_pass(
            extraction.raw_jd_text, current_yaml, current_missing, current_score
        )

        if not optimized_yaml:
            logger.warning("LLM pass %d returned no usable output, keeping previous YAML", pass_num)
            break

        # Force-merge design block
        optimized_yaml = _force_merge_design(optimized_yaml, base_yaml_content)
        current_yaml = optimized_yaml

        # Re-evaluate the optimized resume
        await _report(f"🔄 <b>Re-evaluating resume after pass {pass_num}...</b>")
        updated_resume_text = _extract_resume_text(current_yaml)
        updated_resume_skills = await extract_categorized_skills(updated_resume_text)
        new_score, new_missing, new_raw = calculate_ats_score(jd_skills, updated_resume_skills)

        logger.info(
            "Post-pass %d — Score: %d%% (raw: %d%%) | Missing: %s",
            pass_num, new_score, new_raw,
            ", ".join(new_missing[:10]) or "none",
        )

        current_score = new_score
        current_missing = new_missing

        # Check if target reached
        if current_score >= TARGET_ATS_SCORE:
            logger.info("Target score reached (%d%% >= %d%%). Breaking loop.", current_score, TARGET_ATS_SCORE)
            break

    # ── Step 5: Return final result ───────────────────────────
    final_score = current_score
    final_missing = current_missing

    logger.info(
        "Tailoring complete — Initial: %d%% → Final: %d%% | Remaining gaps: %s",
        initial_score, final_score,
        ", ".join(final_missing[:10]) or "none",
    )

    yaml_path = _save_yaml_to_temp(current_yaml, chat_id)
    return current_yaml, yaml_path, False, final_missing, initial_score, final_score


# ══════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════

async def _run_single_llm_pass(
    jd_text: str,
    current_yaml: str,
    missing_skills: List[str],
    current_score: int,
) -> Optional[str]:
    """
    Execute a single Gemini 2.5 Flash optimization pass with key rotation.

    Sends the JD, current resume YAML, missing skills list, and
    current score to Gemini. Parses the JSON response and returns
    the updated YAML string, or None on failure.

    Args:
        jd_text: Raw JD text.
        current_yaml: Current resume YAML content.
        missing_skills: List of skills to inject.
        current_score: Current ATS score percentage.

    Returns:
        Updated YAML string, or None if the LLM call fails.
    """
    yaml_without_design = _strip_design_block(current_yaml)
    full_prompt = TAILORING_SYSTEM_PROMPT + "\n\n" + TAILORING_USER_PROMPT.format(
        jd_text=jd_text[:4000],
        base_yaml_content=yaml_without_design,
        missing_skills=", ".join(missing_skills) if missing_skills else "None identified",
        current_score=current_score,
        target_score=TARGET_ATS_SCORE,
    )

    response_text = None
    last_error = None

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            logger.info("Calling Gemini 2.5 Flash for optimization pass (key #%d)...", i + 1)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=full_prompt,
                config=genai_types.GenerateContentConfig(temperature=0.0, max_output_tokens=8192),
            )
            response_text = response.text.strip()
            break
        except Exception as e:
            last_error = str(e)
            logger.warning("Tailoring LLM pass key #%d failed: %s", i + 1, last_error)

    if not response_text:
        logger.error("All Gemini keys failed for tailoring pass: %s", last_error)
        return None

    try:

        # Strip markdown code fences if present
        if response_text.startswith("```json"):
            response_text = response_text[len("```json"):].strip()
        elif response_text.startswith("```"):
            response_text = response_text[3:].strip()
        if response_text.endswith("```"):
            response_text = response_text[:-3].strip()

        # Parse JSON response
        parsed_json = json.loads(response_text)
        updated_yaml = parsed_json.get("updated_yaml", "")

        if not updated_yaml:
            logger.error("LLM returned empty updated_yaml")
            return None

        # 1. Strip markdown code blocks if the LLM hallucinated them
        if updated_yaml.startswith("```"):
            updated_yaml = updated_yaml.split("\n", 1)[-1]
        if updated_yaml.endswith("```"):
            updated_yaml = updated_yaml.rsplit("\n", 1)[0]
        updated_yaml = updated_yaml.replace("```yaml", "").replace("```", "").strip()

        # 2. Sanitize LaTeX poison characters
        # Note: % is intentionally NOT replaced — RenderCV auto-escapes it to \% in LaTeX
        updated_yaml = updated_yaml.replace("&", "and")
        
        # 3. Strip LaTeX-crashing Unicode characters injected by the LLM
        updated_yaml = updated_yaml.replace("\u201c", '"').replace("\u201d", '"')
        updated_yaml = updated_yaml.replace("\u2018", "'").replace("\u2019", "'")
        updated_yaml = updated_yaml.replace("\u2014", "-").replace("\u2013", "-")
        updated_yaml = updated_yaml.replace("\u2022", "").replace("\u00b7", "")
        updated_yaml = updated_yaml.replace("\u2026", "...")
        updated_yaml = updated_yaml.replace("\u2192", "->").replace("\u2190", "<-")
        updated_yaml = updated_yaml.replace("\u2191", "^").replace("\u2193", "v")
        # Final fallback: encode to ASCII, replacing anything still non-ASCII
        updated_yaml = updated_yaml.encode("ascii", errors="replace").decode("ascii")

        # Validate YAML is parseable
        parsed = yaml.safe_load(updated_yaml)
        if not isinstance(parsed, dict):
            logger.error("LLM returned non-dict YAML structure")
            return None

        logger.info("LLM pass returned valid YAML (%d chars)", len(updated_yaml))
        return updated_yaml

    except json.JSONDecodeError as e:
        logger.error("LLM returned invalid JSON: %s", str(e))
        return None

    except Exception as e:
        logger.error("Unexpected error in LLM pass: %s", str(e), exc_info=True)
        return None


def _strip_design_block(yaml_content: str) -> str:
    """
    Remove the `design:` block from YAML content before sending to LLM.

    This prevents Claude from mangling the theme configuration.
    The design block is always force-merged back from the original
    master resume after every LLM call.

    Args:
        yaml_content: Full YAML string potentially containing a design block.

    Returns:
        YAML string with the design block removed.
    """
    try:
        parsed = yaml.safe_load(yaml_content)
        if isinstance(parsed, dict) and "design" in parsed:
            del parsed["design"]
            return yaml.dump(parsed, sort_keys=False, allow_unicode=True)
    except yaml.YAMLError:
        pass
    return yaml_content


# ── Protected fields the LLM must NEVER alter (enforced, not just prompted) ──
# Identity / contact block at cv level.
_PROTECTED_CV_FIELDS = ("name", "location", "email", "phone", "social_networks", "website")
# Factual scalar fields on each experience entry.
_PROTECTED_ENTRY_FIELDS = ("company", "position", "location", "start_date", "end_date", "date")


def _restore_protected_fields_inplace(opt: dict, base) -> None:
    """
    Force-restore protected FACTUAL fields from the base resume into the tailored
    dict (mutates `opt`). Tailoring may only change summary, technical_skills, and
    experience/project highlights — never:
      - identity/contact (name, email, phone, location, socials)
      - company names, positions, and employment dates in experience
      - the entire education section (institution, degree, dates, and CGPA, which
        lives inside education highlights)

    This is the enforcement layer behind the "LLM cannot modify …" business rule;
    prompting alone is not trustworthy for factual integrity on a real resume.
    """
    if not isinstance(base, dict):
        return
    opt_cv, base_cv = opt.get("cv"), base.get("cv")
    if not isinstance(opt_cv, dict) or not isinstance(base_cv, dict):
        return

    # 1. Identity / contact
    for k in _PROTECTED_CV_FIELDS:
        if k in base_cv:
            opt_cv[k] = base_cv[k]

    opt_sec = opt_cv.get("sections")
    base_sec = base_cv.get("sections")
    if not isinstance(opt_sec, dict) or not isinstance(base_sec, dict):
        return

    # 2. Education is purely factual (institution/degree/dates/CGPA) → restore wholesale
    if isinstance(base_sec.get("education"), list):
        opt_sec["education"] = base_sec["education"]

    # 2b. Certifications / leadership / volunteering are factual credentials, not
    #     tailorable copy. Restore wholesale so the LLM can never merge two bullets
    #     into one (the "…skills.Volunteerism…" run-together bug) or drop/alter them.
    if isinstance(base_sec.get("certifications_and_leadership"), list):
        opt_sec["certifications_and_leadership"] = base_sec["certifications_and_leadership"]

    # 3. Experience: lock factual scalars per entry, keep tailored highlights.
    #    If the entry count changed (a rule violation), restore the whole section.
    base_exp = base_sec.get("experience")
    opt_exp = opt_sec.get("experience")
    if isinstance(base_exp, list):
        if not isinstance(opt_exp, list) or len(opt_exp) != len(base_exp):
            logger.warning("Tailoring altered experience entry count — restoring from base.")
            opt_sec["experience"] = base_exp
        else:
            for i, b in enumerate(base_exp):
                if isinstance(b, dict) and isinstance(opt_exp[i], dict):
                    for f in _PROTECTED_ENTRY_FIELDS:
                        if f in b:
                            opt_exp[i][f] = b[f]


def _force_merge_design(optimized_yaml: str, base_yaml_content: str) -> str:
    """
    Force-merge the original design block into the optimized YAML AND restore all
    protected factual fields.

    This is the critical guardrail that ensures the custom theme, section_order,
    layout configuration, and the candidate's real facts (identity, company names,
    positions, dates, education, GPA) are NEVER lost or altered, regardless of what
    the LLM outputs.

    Args:
        optimized_yaml: The LLM-generated YAML (may lack design block).
        base_yaml_content: The original master resume YAML with design block.

    Returns:
        Complete YAML string with design block + protected facts guaranteed intact.
    """
    try:
        optimized_parsed = yaml.safe_load(optimized_yaml)
        base_parsed = yaml.safe_load(base_yaml_content)

        if not isinstance(optimized_parsed, dict):
            logger.error("Cannot merge design: optimized YAML is not a dict")
            return base_yaml_content

        # Force the design block from the original
        if isinstance(base_parsed, dict) and "design" in base_parsed:
            optimized_parsed["design"] = base_parsed["design"]

        # Force-restore protected factual fields (enforcement of the "no-change" rule)
        _restore_protected_fields_inplace(optimized_parsed, base_parsed)

        # Hard Python Guard: Ensure summary is a list with at least one string
        if "cv" in optimized_parsed and "sections" in optimized_parsed["cv"]:
            summary = optimized_parsed["cv"]["sections"].get("summary", [])
            if isinstance(summary, str):
                optimized_parsed["cv"]["sections"]["summary"] = [summary]
            elif not summary:
                optimized_parsed["cv"]["sections"]["summary"] = ["Ambitious professional with experience in software development and optimization."]

        return yaml.dump(optimized_parsed, sort_keys=False, allow_unicode=True)

    except yaml.YAMLError as e:
        logger.error("YAML error during design merge: %s", str(e))
        return base_yaml_content


def _save_yaml_to_temp(yaml_content: str, chat_id: str) -> str:
    """
    Save YAML content to a temporary directory.

    Creates a directory structure: /tmp/jobforge_{chat_id}_{timestamp}/resume.yaml

    IMPORTANT: On Windows, tempfile.gettempdir() returns a path with 8.3 short
    names (e.g., PRATHA~1) which causes pdflatex to crash. We resolve the full
    long path using the Windows API before saving.

    Args:
        yaml_content: YAML string to save.
        chat_id: User's chat ID for directory naming.

    Returns:
        Absolute path to the saved YAML file.
    """
    timestamp = int(time.time())
    base_tmp = tempfile.gettempdir()

    # Resolve Windows 8.3 short paths (e.g., PRATHA~1 -> Prathamesh)
    # pdflatex chokes on tilde-paths, causing "No such file or directory".
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            get_long = ctypes.windll.kernel32.GetLongPathNameW
            get_long(base_tmp, buf, 32768)
            if buf.value:
                base_tmp = buf.value
        except Exception:
            pass  # Fall back to original path if resolution fails

    temp_dir = os.path.join(base_tmp, f"jobforge_{chat_id}_{timestamp}")
    os.makedirs(temp_dir, exist_ok=True)

    yaml_path = os.path.join(temp_dir, "resume.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    logger.info("Saved YAML to: %s", yaml_path)
    return yaml_path
