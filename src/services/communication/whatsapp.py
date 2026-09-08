"""
WhatsApp Outreach Service (click-to-chat, HITL)

Detects a recruiter contact phone number in a job posting, normalizes it to
international format, and drafts a short, formal WhatsApp-style intro message.
The bot then hands the user a wa.me click-to-chat link — the user reviews and
sends it themselves from WhatsApp (compliant, no API, no ban risk).

Nothing is ever sent automatically. Click-to-chat only pre-fills text, so the
resume PDF is shared manually by the user in the chat.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings

logger = logging.getLogger(__name__)

# Loose hint that a phone number *might* be present, to gate the LLM call.
# Matches a run of 8+ characters made of digits/spaces/dashes/parens/+ containing
# at least several digits.
_PHONE_HINT_RE = re.compile(r"(?:\+?\d[\d\s\-().]{7,}\d)")


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalize_number(raw: str) -> Optional[str]:
    """
    Normalize a raw phone string to digits-only international format.
    Applies the default country code when none is present. Returns None if the
    result doesn't look like a valid phone number (10–15 digits).
    """
    if not raw:
        return None
    raw = raw.strip()
    had_plus = raw.strip().startswith("+")
    digits = _digits_only(raw)

    if not digits:
        return None

    # Strip national trunk-prefix zero(s) on non-international numbers
    # (e.g. India '098765...' or '080-...' → drop the leading 0).
    if not had_plus:
        digits = digits.lstrip("0")

    if not digits:
        return None

    cc = _digits_only(settings.WHATSAPP_DEFAULT_COUNTRY_CODE)

    if had_plus:
        # Already international
        normalized = digits
    elif cc and digits.startswith(cc) and len(digits) > 10:
        # Already includes the country code
        normalized = digits
    elif len(digits) == 10:
        # Bare local mobile number → prepend default country code
        normalized = f"{cc}{digits}"
    elif 11 <= len(digits) <= 15:
        # Assume it already carries a country code
        normalized = digits
    else:
        return None

    if 10 <= len(normalized) <= 15:
        return normalized
    return None


_PHONE_PROMPT = """Extract the RECRUITER / HR CONTACT mobile phone number from this job posting.

Rules:
- Return ONLY the phone number, digits and an optional leading + only.
- Prefer a mobile/WhatsApp-style number over a landline/office/reception number.
- If there is NO usable contact phone number, return exactly: NONE
- Do not return fax numbers, PIN codes, salaries, years, or ID numbers.

JOB POSTING:
{text}

Phone number (or NONE):"""


async def extract_contact_phone(jd_text: str) -> Optional[str]:
    """
    Detect and normalize a recruiter contact phone number from the JD.
    Returns digits-only international format (e.g. '919876543210') or None.
    """
    if not jd_text or not _PHONE_HINT_RE.search(jd_text):
        return None

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=_PHONE_PROMPT.format(text=jd_text[:3000]),
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=20,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            answer = (response.text or "").strip()
            if not answer or answer.upper().startswith("NONE"):
                return None
            normalized = _normalize_number(answer)
            if normalized:
                logger.info("Detected recruiter phone (key #%d).", i + 1)
                return normalized
            return None
        except Exception as e:
            logger.warning("extract_contact_phone key #%d failed: %s", i + 1, str(e))

    # Fallback: best-effort regex on the first strong hint
    match = _PHONE_HINT_RE.search(jd_text)
    if match:
        return _normalize_number(match.group())
    return None


_WA_DRAFT_PROMPT = """Write a short, formal WhatsApp message from a job candidate to a recruiter about a specific role.

CANDIDATE: Prathamesh Shirole — AI/ML engineer, currently an AI Intern at Infosys Ltd. Skilled in Generative AI, RAG, Python, SQL. Builder of JobForge, an agentic AI job-application pipeline (may mention it in one short clause if it fits naturally).
ROLE: {job_title}
COMPANY: {company_name}

Requirements:
- WhatsApp style: concise, polite, professional but warmer than an email. NO "Dear Sir/Madam" and NO email signature block.
- Start with a courteous greeting (e.g. "Hello,").
- 1 line who you are, 1–2 lines expressing genuine interest in the {job_title} role at {company_name}, and mention you'd be happy to share your resume.
- End politely (e.g. "Thank you for your time.").
- Under 60 words total. Plain text only — no markdown, no emojis.

Output ONLY the message text."""


async def draft_whatsapp_message(job_title: str, company_name: str, jd_text: str = "") -> str:
    """Draft a short, formal WhatsApp intro message via Gemini (with a fallback)."""
    prompt = _WA_DRAFT_PROMPT.format(
        job_title=job_title or "the role",
        company_name=company_name or "your company",
    )
    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=200,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text = (response.text or "").strip()
            if text:
                return text
        except Exception as e:
            logger.warning("draft_whatsapp_message key #%d failed: %s", i + 1, str(e))

    # Fallback template
    return (
        f"Hello, my name is Prathamesh Shirole. I came across the {job_title or 'open'} "
        f"role at {company_name or 'your company'} and I'm very interested. "
        "I'm an AI/ML engineer (currently an AI Intern at Infosys) and would be glad to "
        "share my resume. Thank you for your time."
    )


def build_wa_link(phone: str, message: str) -> str:
    """
    Build a wa.me click-to-chat URL with a pre-filled message.

    safe="" fully percent-encodes every character (including '/'), so a literal
    slash in text like "AI/ML" can't truncate the message during the
    wa.me → WhatsApp handoff.
    """
    return f"https://wa.me/{_digits_only(phone)}?text={quote(message, safe='')}"
