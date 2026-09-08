"""
Persona Classifier Service + Intelligent Evaluation Brain

Classifies job descriptions into persona categories using Gemini 1.5 Flash.
Returns a persona tag (AI_ML, SWE, DATA) and loads the corresponding
base YAML resume file.

NEW: Implements the 3-Tier Weighted Skill Extraction and ATS Scoring Matrix.
- Tier 1 (Core Languages):        50% weight
- Tier 2 (Frameworks/Cloud/Infra): 35% weight
- Tier 3 (Methodologies/Tools/DBs): 15% weight
- Honesty Cap: 78% ceiling if any Tier 1 language is missing.

Pipeline position: C3 — called after extraction, before tailoring.
"""

import logging
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from google import genai
from google.genai import types as genai_types
import yaml
import hashlib

from src.config.settings import settings
from src.database.redis import get_redis as _get_redis

JD_CACHE_PREFIX = "app:jd_cache:"
JD_CACHE_TTL = 259200  # 72 hours for JD skills cache

logger = logging.getLogger(__name__)

# ── Persona Map ───────────────────────────────────────────────
# src/services/ai/classifier.py → parent.parent.parent = src/
PERSONAS_DIR = Path(__file__).parent.parent.parent / "resources" / "personas"

MASTER_RESUME_PATH = PERSONAS_DIR / "master_resume.yaml"

DEFAULT_PERSONA = "swe"

# Valid persona tags
VALID_TAGS = {"ai_ml", "swe", "game_dev"}

# ── Classification Prompt (EXACT as specified) ────────────────
CLASSIFICATION_PROMPT = """Analyze the following Job Description and classify it into exactly one of these three categories:
- 'ai_ml' (If it involves LLMs, computer vision, data science, Python backend data pipelines)
- 'game_dev' (If it involves Unity, C#, graphics rendering, gameplay programming)
- 'swe' (If it involves general full-stack, Java, backend architectures, databases)

Job Description (first 800 chars):
{jd_text}

Output ONLY the category string. Do not include any other text."""

# ── Skill Extraction Prompt ───────────────────────────────────
SKILL_EXTRACTION_PROMPT = """You are a technical recruiter AI. Analyze the following text and extract ALL technical skills, tools, languages, frameworks, and methodologies mentioned.

Categorize them into exactly three tiers:

- tier_1_languages: Primary programming languages and core architectural technologies (e.g., Python, Java, C++, Go, Rust, JavaScript, TypeScript, Scala, Ruby, Kotlin, Swift, R, SQL)
- tier_2_frameworks_infra: Frameworks, libraries, cloud platforms, infrastructure, containers, and runtime environments (e.g., React, Angular, Django, Flask, Spring, PyTorch, TensorFlow, AWS, GCP, Azure, Docker, Kubernetes, Redis, Kafka, Spark, Airflow, Node.js, .NET)
- tier_3_methodologies: Developer tools, processes, version control, databases, methodologies, and soft technical practices (e.g., Git, CI/CD, Agile, Scrum, REST API, GraphQL, MySQL, PostgreSQL, MongoDB, Linux, Jira, Jenkins, Terraform, Ansible)

IMPORTANT RULES:
- Extract skills from the text AS THEY APPEAR - do not invent skills that are not mentioned.
- Normalize each skill to its canonical short name (e.g., "Amazon Web Services" -> "AWS").
- Return lowercase strings only.
- If a category has zero skills, return an empty array.

TEXT TO ANALYZE:
{text}

Return your response as a valid JSON object ONLY with this exact schema:
{{
  "tier_1_languages": [],
  "tier_2_frameworks_infra": [],
  "tier_3_methodologies": []
}}"""


# ── Tier Weights ──────────────────────────────────────────────
TIER_1_WEIGHT = 0.50
TIER_2_WEIGHT = 0.35
TIER_3_WEIGHT = 0.15
HONESTY_CAP = 78  # Max score if any Tier 1 language is missing


# ══════════════════════════════════════════════════════════════
#  EXISTING FUNCTIONS (unchanged)
# ══════════════════════════════════════════════════════════════

async def classify_jd(jd_text: str) -> str:
    """
    Classify a job description into a persona category.

    Sends the first 800 characters to Gemini 1.5 Flash and
    expects exactly one of: ai_ml, game_dev, swe.

    Args:
        jd_text: Raw job description text.

    Returns:
        Persona tag string. Defaults to 'swe' if classification fails.
    """
    logger.info("Classifying JD (%d chars)...", len(jd_text))

    prompt = CLASSIFICATION_PROMPT.format(jd_text=jd_text[:800])

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=50,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text = response.text.strip().lower().replace("'", "").replace('"', "")

            if text in VALID_TAGS:
                logger.info("Classified as: %s (key #%d)", text, i + 1)
                return text
            if "ai" in text or "ml" in text:
                return "ai_ml"
            if "game" in text or "unity" in text:
                return "game_dev"

            logger.warning("Unknown tag '%s' from key #%d — retrying with next key.", text, i + 1)

        except Exception as e:
            logger.warning("classify_jd key #%d failed: %s", i + 1, str(e))

    logger.error("All Gemini keys failed for classification. Defaulting to '%s'.", DEFAULT_PERSONA)
    return DEFAULT_PERSONA

def get_yaml_by_persona(persona_tag: str) -> str:
    """
    Loads and returns the base YAML string for the specified persona.
    """
    filename = f"base_{persona_tag}.yaml"
    filepath = PERSONAS_DIR / filename
    
    if not filepath.exists():
        logger.warning("Persona file %s not found. Falling back to base_swe.yaml", filename)
        filepath = PERSONAS_DIR / "base_swe.yaml"
        
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error("Error loading persona YAML %s: %s", filename, str(e))
        return ""

def load_persona_yaml(persona_tag: str) -> str:
    """
    Load the base persona YAML file for a given tag.

    Args:
        persona_tag: One of 'AI_ML', 'SWE', 'DATA'.

    Returns:
        Raw YAML content as a string.

    Raises:
        FileNotFoundError: If the persona YAML file doesn't exist.
    """
    content = get_yaml_by_persona(persona_tag)
    logger.info("Loaded persona YAML for %s", persona_tag)
    return content


def parse_persona_yaml(yaml_content: str) -> dict:
    """
    Parse a persona YAML string into a Python dict.

    Args:
        yaml_content: Raw YAML string.

    Returns:
        Parsed dictionary matching the RenderCV YAML schema.
    """
    return yaml.safe_load(yaml_content)


async def classify_and_load(jd_text: str) -> Tuple[str, str, dict]:
    """
    Classify the JD and load the corresponding persona YAML.

    Convenience function that combines classification and YAML loading.

    Args:
        jd_text: Raw job description text.

    Returns:
        Tuple of (persona_tag, raw_yaml_content, parsed_yaml_dict).
    """
    persona_tag = await classify_jd(jd_text)
    yaml_content = load_persona_yaml(persona_tag)
    yaml_dict = parse_persona_yaml(yaml_content)

    return persona_tag, yaml_content, yaml_dict


# ══════════════════════════════════════════════════════════════
#  NEW: INTELLIGENT EVALUATION BRAIN
# ══════════════════════════════════════════════════════════════

async def extract_categorized_skills(text: str, cache_key_prefix: str = "") -> Dict[str, List[str]]:
    """
    Extract and categorize technical skills from text using Gemini 1.5 Flash.

    Uses a structured LLM call to extract skills into three tiers.
    If a valid cache_key_prefix is provided (e.g. for JDs), it checks Redis
    first and caches the result for 72 hours.

    Args:
        text: Raw text to analyze (JD or resume content).
        cache_key_prefix: Optional prefix to enable caching.

    Returns:
        Dict with keys: tier_1_languages, tier_2_frameworks_infra, tier_3_methodologies.
    """
    empty_result = {
        "tier_1_languages": [],
        "tier_2_frameworks_infra": [],
        "tier_3_methodologies": [],
    }

    if not text or len(text.strip()) < 20:
        logger.warning("Text too short for skill extraction (%d chars)", len(text or ""))
        return empty_result

    # 1. Check Cache
    redis_client = None
    cache_key = None
    if cache_key_prefix:
        try:
            redis_client = await _get_redis()
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cache_key = f"{JD_CACHE_PREFIX}{text_hash}"
            cached_data = await redis_client.get(cache_key)
            if cached_data:
                logger.info("Cache hit for skill extraction! Bypassing LLM.")
                return json.loads(cached_data)
        except Exception as e:
            logger.warning("Redis cache check failed: %s", str(e))

    prompt = SKILL_EXTRACTION_PROMPT.format(text=text[:3000])
    response_text = None
    last_error = None

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            logger.info("Calling Gemini for skill extraction (key #%d)...", i + 1)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=1024,
                    response_mime_type="application/json",
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            response_text = response.text.strip()
            break
        except Exception as e:
            last_error = str(e)
            logger.warning("Skill extraction key #%d failed: %s", i + 1, last_error)

    if not response_text:
        logger.error("All Gemini keys failed for skill extraction: %s", last_error)
        return empty_result

    # Strip code fences if present
    if response_text.startswith("```json"):
        response_text = response_text[len("```json"):].strip()
    elif response_text.startswith("```"):
        response_text = response_text[3:].strip()
    if response_text.endswith("```"):
        response_text = response_text[:-3].strip()

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("Gemini returned invalid JSON for skill extraction: %s", str(e))
        return empty_result

    result = {}
    for tier_key in ["tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies"]:
        raw_list = parsed.get(tier_key, [])
        if isinstance(raw_list, list):
            result[tier_key] = list(set(
                s.strip().lower() for s in raw_list if isinstance(s, str) and s.strip()
            ))
        else:
            result[tier_key] = []

    logger.info(
        "Skills extracted — T1: %d, T2: %d, T3: %d",
        len(result["tier_1_languages"]),
        len(result["tier_2_frameworks_infra"]),
        len(result["tier_3_methodologies"]),
    )

    if cache_key and redis_client:
        try:
            await redis_client.setex(cache_key, JD_CACHE_TTL, json.dumps(result))
            logger.info("Saved extracted skills to cache (%d sec TTL).", JD_CACHE_TTL)
        except Exception as e:
            logger.warning("Redis cache set failed: %s", str(e))

    return result


def _extract_resume_text(yaml_content: str) -> str:
    """
    Flatten a RenderCV YAML resume into plain text for skill extraction.

    Walks through all sections (summary, skills, experience, projects,
    certifications) and concatenates every string value into a single
    searchable text block.

    Args:
        yaml_content: Raw YAML string of the resume.

    Returns:
        Flattened plain text containing all resume content.
    """
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return yaml_content  # Fall back to raw text

    parts = []

    if not isinstance(parsed, dict):
        return yaml_content
    
    cv = parsed.get("cv", parsed)
    if not isinstance(cv, dict):
        return yaml_content
    sections = cv.get("sections", {})

    for section_name, section_data in sections.items():
        if isinstance(section_data, list):
            for item in section_data:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    # Experience/Education entries
                    for key, val in item.items():
                        if isinstance(val, str):
                            parts.append(val)
                        elif isinstance(val, list):
                            for sub in val:
                                if isinstance(sub, str):
                                    parts.append(sub)

    return " ".join(parts)


def calculate_ats_score(
    jd_skills: Dict[str, List[str]],
    resume_skills: Dict[str, List[str]],
) -> Tuple[int, List[str], int]:
    """
    Compute the weighted ATS match score using the 3-Tier Heuristic Matrix.

    Formula:
        Score = (Tier1_match * 0.50) + (Tier2_match * 0.35) + (Tier3_match * 0.15)

    Where each tier_match = matched_count / required_count (0-1 range).

    Honesty Cap: If ANY Tier 1 language from the JD is completely absent
    from the resume, the final score is capped at 78% regardless of the
    mathematical result.

    Args:
        jd_skills: Categorized skills extracted from the Job Description.
        resume_skills: Categorized skills extracted from the candidate resume.

    Returns:
        Tuple of (final_score, missing_skills_flat_list, raw_score_before_cap).
        - final_score: Integer percentage (0-100), possibly capped at 78.
        - missing_skills_flat_list: All skills in JD but absent in resume.
        - raw_score_before_cap: The uncapped mathematical score.
    """
    missing_skills: List[str] = []
    tier_1_has_gap = False

    # Flatten resume skills into a single set for matching
    resume_all = set()
    for tier_key in ["tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies"]:
        resume_all.update(resume_skills.get(tier_key, []))

    def _compute_tier_score(jd_tier: List[str], tier_name: str) -> float:
        nonlocal tier_1_has_gap
        if not jd_tier:
            return 1.0  # No requirements = perfect match

        matched = 0
        for skill in jd_tier:
            # Check if skill exists in ANY tier of the resume (cross-tier matching)
            if skill in resume_all:
                matched += 1
            else:
                missing_skills.append(skill)
                if tier_name == "tier_1":
                    tier_1_has_gap = True

        return matched / len(jd_tier)

    # Compute per-tier scores
    t1_score = _compute_tier_score(jd_skills.get("tier_1_languages", []), "tier_1")
    t2_score = _compute_tier_score(jd_skills.get("tier_2_frameworks_infra", []), "tier_2")
    t3_score = _compute_tier_score(jd_skills.get("tier_3_methodologies", []), "tier_3")

    # If all tiers are completely empty, extraction failed or the JD is completely blank.
    # We should return a 0 score rather than a false 100%.
    has_any_jd_skills = any(
        len(jd_skills.get(tier, [])) > 0 
        for tier in ["tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies"]
    )
    
    if not has_any_jd_skills:
        logger.warning("No JD skills extracted! ATS score forced to 0%.")
        return 0, ["API Error or no skills found"], 0

    # Weighted sum
    raw_score = int(round(
        (t1_score * TIER_1_WEIGHT + t2_score * TIER_2_WEIGHT + t3_score * TIER_3_WEIGHT) * 100
    ))

    # Clamp to 0-100
    raw_score = max(0, min(100, raw_score))

    # Apply honesty cap
    final_score = min(raw_score, HONESTY_CAP) if tier_1_has_gap else raw_score

    # Deduplicate missing skills
    missing_skills = list(dict.fromkeys(missing_skills))

    logger.info(
        "ATS Score — T1: %.0f%% T2: %.0f%% T3: %.0f%% | Raw: %d%% | Final: %d%% | T1 Gap: %s | Missing: %s",
        t1_score * 100, t2_score * 100, t3_score * 100,
        raw_score, final_score, tier_1_has_gap,
        ", ".join(missing_skills[:10]) or "none",
    )

    return final_score, missing_skills, raw_score


# ══════════════════════════════════════════════════════════════
#  SEMANTIC FIT (embedding-based, complements keyword ATS)
# ══════════════════════════════════════════════════════════════

EMB_CACHE_PREFIX = "app:emb:"
EMB_CACHE_TTL = 259200  # 72h


async def _embed_text(text: str) -> Optional[List[float]]:
    """Embed text via the Gemini embeddings API, with Redis caching by content hash."""
    text = (text or "")[:8000]
    if not text.strip():
        return None

    cache_key = f"{EMB_CACHE_PREFIX}{hashlib.sha256(text.encode()).hexdigest()}"
    try:
        r = await _get_redis()
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        r = None

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            resp = await client.aio.models.embed_content(
                model=settings.EMBEDDING_MODEL, contents=text
            )
            vec = list(resp.embeddings[0].values)
            if r is not None:
                try:
                    await r.setex(cache_key, EMB_CACHE_TTL, json.dumps(vec))
                except Exception:
                    pass
            return vec
        except Exception as e:
            logger.warning("Embedding key #%d failed: %s", i + 1, str(e)[:120])
    return None


async def semantic_fit_score(jd_text: str, resume_text: str) -> Optional[int]:
    """
    Meaning-level fit between JD and resume: cosine similarity of embeddings,
    scaled to 0-100. Credits semantically related experience even when exact
    keywords differ (e.g. 'scaled data pipelines' vs 'distributed systems').
    Returns None if embeddings are unavailable (callers must degrade gracefully).
    """
    v1 = await _embed_text(jd_text)
    v2 = await _embed_text(resume_text)
    if not v1 or not v2 or len(v1) != len(v2):
        return None

    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = sum(a * a for a in v1) ** 0.5
    n2 = sum(b * b for b in v2) ** 0.5
    if n1 == 0 or n2 == 0:
        return None
    cos = dot / (n1 * n2)
    score = int(round(max(0.0, min(1.0, cos)) * 100))
    logger.info("Semantic fit: %d%% (cosine=%.3f)", score, cos)
    return score
