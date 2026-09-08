"""
Vision Extraction Service

Processes job posting screenshots via Gemini 1.5 Pro Vision API.
Downloads image bytes from Telegram, sends to Gemini with a structured
prompt, and returns an ExtractionResult.

Pipeline position: C2 — called when Telegram receives an image.
"""

import hashlib
import json
import logging
import re
from typing import Optional

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings
from src.database.redis import get_redis as _get_redis
from src.validators.application import ExtractionResult

logger = logging.getLogger(__name__)

# Cache vision extraction keyed by image-content hash so re-posting the same
# screenshot yields an identical JD text (and therefore an identical ATS score)
# instead of re-running the non-deterministic vision model each time.
VISION_CACHE_PREFIX = "app:vision_cache:v2:"  # v2: now also captures contact phone
VISION_CACHE_TTL = 259200  # 72 hours

# ── Email Regex (shared with extractor.py) ────────────────────
EMAIL_PATTERN = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"

# ── Gemini Vision System Prompt ───────────────────────────────
VISION_PROMPT = """You are an ATS parser. Examine the attached job posting image carefully.
Extract ALL information visible in the image, including any contact details.

Return ONLY valid JSON with this exact structure — no preamble, no explanation:
{
  "job_title": "string or null",
  "company_name": "string or null",
  "required_skills": ["array of skill strings"],
  "experience_required": "string or null",
  "job_description_text": "complete extracted job description text, INCLUDING any contact phone numbers and emails exactly as shown",
  "hr_email": "email address string or null",
  "hr_phone": "recruiter/HR contact phone number exactly as shown (with country code if visible) or null",
  "application_link": "URL string or null"
}

If a field is not visible in the image, set it to null.
Always transcribe any phone number visible in the image — both in hr_phone and within job_description_text."""


async def download_telegram_photo(file_id: str, bot) -> bytes:
    """
    Download image bytes from Telegram servers.

    Args:
        file_id: Telegram file ID (from message.photo[-1].file_id).
        bot: The telegram.Bot instance.

    Returns:
        Raw image bytes.
    """
    logger.info("Downloading Telegram photo file_id=%s", file_id[:20])
    file = await bot.get_file(file_id)
    byte_array = await file.download_as_bytearray()
    image_bytes = bytes(byte_array)
    logger.info("Downloaded %d bytes from Telegram", len(image_bytes))
    return image_bytes


async def extract_from_image(
    image_bytes: bytes,
    file_id: Optional[str] = None,
) -> ExtractionResult:
    """
    Extract job posting data from an image using Gemini 1.5 Pro Vision.

    Sends the image to Gemini with a structured prompt and parses
    the JSON response into an ExtractionResult.

    Args:
        image_bytes: Raw image bytes (JPEG, PNG, etc.).
        file_id: Optional Telegram file ID for logging.

    Returns:
        ExtractionResult with all extracted fields.

    Raises:
        VisionExtractionError: If Gemini fails or returns invalid data.
    """
    logger.info(
        "Starting vision extraction (image_size=%d bytes, file_id=%s)",
        len(image_bytes),
        file_id[:20] if file_id else "N/A",
    )

    # ── Cache check (keyed by image content, not Telegram file_id) ──────
    img_hash = hashlib.sha256(image_bytes).hexdigest()
    cache_key = f"{VISION_CACHE_PREFIX}{img_hash}"
    redis_client = None
    try:
        redis_client = await _get_redis()
        cached = await redis_client.get(cache_key)
        if cached:
            logger.info("Vision cache hit — reusing prior extraction for this image.")
            return ExtractionResult(**json.loads(cached))
    except Exception as e:
        logger.warning("Vision cache check failed: %s", str(e))

    raw_response = None
    last_error = None
    gemini_keys = settings.ALL_GEMINI_KEYS

    if not gemini_keys:
        raise VisionExtractionError("No Gemini API keys found in configuration.")

    for i, api_key in enumerate(gemini_keys):
        try:
            logger.info("Attempting Gemini Vision with key #%d...", i + 1)
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=[
                    VISION_PROMPT,
                    genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ],
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=4096,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            raw_response = response.text.strip()
            break  # Success! Exit the rotation loop
            
        except Exception as e:
            last_error = str(e)
            logger.warning("Gemini Vision failed with key #%d (%s).", i + 1, last_error)
                
    if not raw_response:
        logger.error("All configured Gemini keys failed.")
        raise VisionExtractionError(f"All Gemini keys failed. Last error: {last_error}")

    logger.debug("Raw vision response: %s", raw_response[:500])

    try:
        json_text = raw_response
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0].strip()
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0].strip()

        parsed = json.loads(json_text)

    except json.JSONDecodeError as e:
        logger.error("Failed to parse vision response as JSON: %s", str(e))
        raise VisionExtractionError(
            f"Vision model returned invalid JSON: {str(e)}"
        )

    # Extract fields from parsed JSON
    jd_text = parsed.get("job_description_text", "") or ""
    job_title = parsed.get("job_title")
    company_name = parsed.get("company_name")
    hr_email = parsed.get("hr_email")
    application_link = parsed.get("application_link")
    required_skills = parsed.get("required_skills", []) or []

    # Validate hr_email format if present
    if hr_email and not re.match(EMAIL_PATTERN, hr_email):
        logger.warning("Invalid email format from Gemini: %s", hr_email)
        hr_email = None

    # Normalize any contact phone the vision model transcribed
    contact_phone = None
    hr_phone_raw = parsed.get("hr_phone")
    if hr_phone_raw:
        try:
            from src.services.communication.whatsapp import _normalize_number
            contact_phone = _normalize_number(str(hr_phone_raw))
        except Exception as e:
            logger.warning("Phone normalization failed for vision result: %s", str(e))

    word_count = len(jd_text.split())

    result = ExtractionResult(
        raw_jd_text=jd_text,
        job_title=job_title,
        company_name=company_name,
        hr_email=hr_email,
        contact_phone=contact_phone,
        application_link=application_link,
        required_skills=required_skills,
        source_type="image",
        word_count=word_count,
    )

    logger.info(
        "Vision extraction complete: title=%s, company=%s, skills=%d, words=%d",
        result.job_title,
        result.company_name,
        len(result.required_skills),
        result.word_count,
    )

    # Cache the extraction by image content hash for determinism on re-uploads —
    # but ONLY if it looks like a confident read. A poor/empty extraction (few
    # words AND no title AND no company) is likely a transient failure or an
    # unreadable image; caching it would lock in garbage for 72h, so we skip it
    # and let a re-send re-extract.
    is_confident = (
        result.word_count >= 40
        or bool(result.job_title)
        or bool(result.company_name)
    )
    if redis_client is not None and is_confident:
        try:
            await redis_client.setex(cache_key, VISION_CACHE_TTL, result.model_dump_json())
            logger.info("Cached vision extraction (%d sec TTL).", VISION_CACHE_TTL)
        except Exception as e:
            logger.warning("Vision cache set failed: %s", str(e))
    elif not is_confident:
        logger.info("Low-confidence vision read — not caching (allows retry on re-send).")

    return result


# ── Custom Exception ──────────────────────────────────────────

class VisionExtractionError(Exception):
    """Raised when Gemini Vision extraction fails."""
    pass
