"""
URL Extraction Service

Scrapes job posting URLs using Firecrawl (primary) with Playwright fallback.
Extracts structured job data and returns an ExtractionResult.

Pipeline position: C2 — called after Telegram receives a URL.
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from google import genai
from google.genai import types as genai_types

from src.config import settings
from src.validators.application import ExtractionResult

logger = logging.getLogger(__name__)

# URL shortener / redirect hosts to unwrap to their real destination before scraping.
_SHORTENER_HOSTS = {"lnkd.in", "bit.ly", "t.co", "ow.ly", "buff.ly", "rebrand.ly", "tinyurl.com"}


async def _resolve_job_url(url: str) -> str:
    """
    Resolve wrapped / shortened job URLs to their real destination before scraping.

    Handles:
      - LinkedIn safety interstitial: linkedin.com/safety/go/?url=<encoded target>
      - Common URL shorteners (lnkd.in, bit.ly, …) by following HTTP redirects.

    Returns the original URL unchanged on any failure.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()

        # 1. Unwrap LinkedIn's "you're about to leave LinkedIn" interstitial
        if "linkedin.com" in host and "/safety/go" in parsed.path:
            target = parse_qs(parsed.query).get("url", [None])[0]
            if target:
                url = unquote(target)
                parsed = urlparse(url)
                host = (parsed.netloc or "").lower()
                logger.info("Unwrapped LinkedIn safety URL -> %s", url[:80])

        # 2. Follow shortener redirects to the final destination
        if any(host == s or host.endswith("." + s) for s in _SHORTENER_HOSTS):
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                final = str(resp.url)
                if final and final != url:
                    logger.info("Resolved shortener %s -> %s", url[:50], final[:80])
                    url = final
    except Exception as e:
        logger.warning("URL resolution failed for %s: %s", url[:60], str(e))
    return url

# ── LLM Extraction Prompt ─────────────────────────────────────
_LLM_EXTRACTION_PROMPT = """You are a job posting parser. Extract structured information from the job description text below.

Return ONLY valid JSON with this exact structure — no preamble, no explanation:
{{
  "job_title": "string or null",
  "company_name": "string or null",
  "hr_email": "email string or null",
  "required_skills": ["array of skill strings"]
}}

If a field is not found, set it to null (or [] for required_skills).

Job Description:
{text}"""

# ── Email Regex ───────────────────────────────────────────────
EMAIL_PATTERN = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"

# Common non-HR email domains to filter out
IGNORE_EMAIL_DOMAINS = {
    "example.com",
    "sentry.io",
    "wixpress.com",
    "googleapis.com",
    "schema.org",
    "w3.org",
    "cloudflare.com",
}


def _extract_hr_email(text: str) -> Optional[str]:
    """
    Extract the most likely HR email from raw text.
    Filters out common non-HR email patterns.
    """
    emails = re.findall(EMAIL_PATTERN, text)
    for email in emails:
        domain = email.split("@")[1].lower()
        if domain not in IGNORE_EMAIL_DOMAINS:
            return email
    return None


def _extract_job_title(text: str) -> Optional[str]:
    """
    Attempt to extract a job title from the first few lines of text.
    Uses common patterns found in job postings.
    """
    lines = text.strip().split("\n")
    for line in lines[:10]:
        line = line.strip()
        # Skip very short or very long lines
        if 5 < len(line) < 100:
            lower = line.lower()
            # Common job title indicators
            if any(
                kw in lower
                for kw in [
                    "engineer",
                    "developer",
                    "scientist",
                    "analyst",
                    "manager",
                    "architect",
                    "designer",
                    "lead",
                    "director",
                    "intern",
                    "specialist",
                ]
            ):
                return line
    return None


def _extract_company_name(text: str) -> Optional[str]:
    """
    Attempt to extract company name from common patterns.
    Looks for 'at Company' or 'Company is hiring' patterns.
    """
    import re as _re

    # Pattern: "at CompanyName" or "@ CompanyName"
    match = _re.search(r"(?:at|@)\s+([A-Z][A-Za-z0-9\s&.]+?)(?:\s*[-|,\n])", text[:500])
    if match:
        return match.group(1).strip()

    # Pattern: "CompanyName is hiring"
    match = _re.search(r"([A-Z][A-Za-z0-9\s&.]+?)\s+is\s+hiring", text[:500])
    if match:
        return match.group(1).strip()

    return None


def _extract_skills(text: str) -> list:
    """
    Extract required skills from job posting text.
    Looks for skills sections and common technology keywords.
    """
    skills = set()
    common_skills = [
        "python", "java", "javascript", "typescript", "go", "golang", "rust",
        "c++", "c#", "ruby", "php", "swift", "kotlin", "scala", "sql",
        "react", "angular", "vue", "node.js", "django", "flask", "fastapi",
        "spring", "express", "next.js",
        "aws", "gcp", "azure", "docker", "kubernetes", "terraform",
        "postgresql", "mongodb", "redis", "elasticsearch", "kafka",
        "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy", "spark",
        "git", "ci/cd", "rest", "graphql", "grpc", "microservices",
        "machine learning", "deep learning", "nlp", "computer vision",
        "llm", "generative ai", "genai", "data engineering", "etl",
        "agile", "scrum", "jira",
    ]

    text_lower = text.lower()
    for skill in common_skills:
        if skill in text_lower:
            skills.add(skill)

    return sorted(skills)


# ── LLM Structured Extraction ────────────────────────────────

async def extract_structured_fields_via_llm(raw_text: str) -> Optional[dict]:
    """
    Use Gemini to extract structured job fields from raw scraped/submitted text.

    Tries all configured Gemini keys in round-robin order. Returns None on
    total failure so callers can fall back to the regex helpers.

    Args:
        raw_text: Raw job description text (from scraping or direct input).

    Returns:
        Dict with job_title, company_name, hr_email, required_skills, or None.
    """
    gemini_keys = settings.ALL_GEMINI_KEYS
    if not gemini_keys:
        return None

    prompt = _LLM_EXTRACTION_PROMPT.format(text=raw_text[:5000])

    for i, api_key in enumerate(gemini_keys):
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=1024,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            response_text = response.text.strip()

            # Strip code fences if present
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            parsed = json.loads(response_text)

            # Validate email format
            hr_email = parsed.get("hr_email")
            if hr_email and not re.match(EMAIL_PATTERN, str(hr_email)):
                hr_email = None

            logger.info(
                "LLM extraction succeeded (key #%d): title=%s, company=%s, email=%s, skills=%d",
                i + 1,
                parsed.get("job_title"),
                parsed.get("company_name"),
                hr_email,
                len(parsed.get("required_skills") or []),
            )
            return {
                "job_title": parsed.get("job_title"),
                "company_name": parsed.get("company_name"),
                "hr_email": hr_email,
                "required_skills": parsed.get("required_skills") or [],
            }
        except Exception as e:
            logger.warning("LLM extraction key #%d failed: %s", i + 1, str(e))

    logger.warning("All Gemini keys failed for LLM extraction — using regex fallback.")
    return None


# ── Primary: Firecrawl ────────────────────────────────────────

async def scrape_url_firecrawl(url: str) -> str:
    """
    Scrape a URL using Firecrawl API.

    Firecrawl handles JavaScript rendering, anti-bot measures, and
    returns clean markdown content from job posting pages.

    Args:
        url: The job posting URL to scrape.

    Returns:
        Markdown text content of the page.

    Raises:
        ScrapingError: If Firecrawl returns empty content or times out.
    """
    from firecrawl import FirecrawlApp
    import asyncio

    logger.info("Firecrawl: scraping %s", url[:80])

    def _do_scrape():
        app = FirecrawlApp(api_key=settings.FIRECRAWL_API_KEY)
        try:
            # Firecrawl v2 API format
            return app.scrape(url, formats=["markdown"], timeout=30000)
        except (TypeError, AttributeError):
            # Firecrawl v1 API fallback
            return app.scrape_url(url, params={"formats": ["markdown"]})

    try:
        # Run the synchronous SDK call in a separate thread so it doesn't block the FastAPI event loop
        # Apply a 35 second absolute timeout
        result = await asyncio.wait_for(asyncio.to_thread(_do_scrape), timeout=35.0)
    except asyncio.TimeoutError:
        raise ScrapingError(f"Firecrawl scraping timed out after 35 seconds for {url}")
    except Exception as e:
        raise ScrapingError(f"Firecrawl scraping failed: {str(e)}")

    # For v2, result might be an object or dictionary. Handle both.
    markdown_content = None
    if isinstance(result, dict) and result.get("markdown"):
        markdown_content = result["markdown"]
    elif hasattr(result, "markdown") and result.markdown:
        markdown_content = result.markdown

    if markdown_content:
        logger.info(
            "Firecrawl: got %d chars from %s",
            len(markdown_content),
            url[:80],
        )
        return markdown_content

    raise ScrapingError(f"Firecrawl returned empty content for {url}")


# ── Fallback: Playwright ──────────────────────────────────────

async def scrape_url_playwright(url: str) -> str:
    """
    Scrape a URL using Playwright (headless Chromium).

    Used as fallback when Firecrawl fails or returns empty content.
    Waits for network idle to handle JavaScript-heavy pages.
    Runs synchronously in a thread to avoid Windows SelectorEventLoop bugs.

    Args:
        url: The job posting URL to scrape.

    Returns:
        Plain text content of the page body.

    Raises:
        ScrapingError: If Playwright fails to extract content.
    """
    import asyncio
    from playwright.sync_api import sync_playwright

    logger.info("Playwright fallback: scraping %s", url[:80])

    def _do_playwright_scrape():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                # Use domcontentloaded — networkidle times out on SPAs (Greenhouse,
                # Lever) because they continuously poll in the background.
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Give JS-rendered SPAs time to populate the DOM after initial load
                import time
                time.sleep(4)
                
                # Try focused selectors before falling back to full body.
                # Job boards (Greenhouse, Lever, Workday) embed the JD inside
                # a specific container; grabbing body.innerText picks up
                # nav bars, sidebars, and unrelated job listings.
                text = page.evaluate("""() => {
                    const selectors = [
                        '#content',
                        '.job-post',
                        '[data-testid="job-description"]',
                        '.posting-content',
                        '.section-wrapper',
                        'article',
                        'main'
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim().length > 300) {
                            return el.innerText;
                        }
                    }
                    return document.body.innerText;
                }""")

                if not text or len(text.strip()) < 20:
                    raise ScrapingError(
                        f"Playwright extracted insufficient content from {url}"
                    )

                logger.info(
                    "Playwright: got %d chars from %s",
                    len(text),
                    url[:80],
                )
                return text
            finally:
                browser.close()

    try:
        # Run Playwright synchronously in a thread pool. This avoids NotImplementedError
        # on Windows when Uvicorn forces the SelectorEventLoop.
        return await asyncio.to_thread(_do_playwright_scrape)
    except Exception as e:
        raise ScrapingError(f"Playwright scraping failed: {str(e)}")


# ── Public API ────────────────────────────────────────────────

async def extract_from_url(url: str) -> ExtractionResult:
    """
    Extract job posting data from a URL.

    Tries Firecrawl first, falls back to Playwright if that fails.
    Parses the raw text to extract structured fields.

    Args:
        url: The job posting URL.

    Returns:
        ExtractionResult with all extracted fields populated.

    Raises:
        ScrapingError: If both Firecrawl and Playwright fail.
    """
    # Unwrap LinkedIn safety wrappers / shortened links before scraping
    url = await _resolve_job_url(url)

    logger.info("Starting URL extraction for: %s", url[:80])

    raw_text = None

    # Step 1: Try Firecrawl (primary) — skipped if API key is not configured
    if settings.FIRECRAWL_API_KEY:
        try:
            raw_text = await scrape_url_firecrawl(url)
        except Exception as e:
            logger.warning(
                "Firecrawl failed for %s: %s. Trying Playwright...",
                url[:80],
                str(e),
            )
    else:
        logger.info("FIRECRAWL_API_KEY not set — going straight to Playwright.")

    # Step 2: Try Playwright (fallback)
    if not raw_text:
        try:
            raw_text = await scrape_url_playwright(url)
        except Exception as e:
            logger.error(
                "Playwright also failed for %s: %s",
                url[:80],
                str(e),
            )
            raise ScrapingError(
                f"Both Firecrawl and Playwright failed for {url}: {e}"
            )

    # Step 3: Extract structured data — LLM primary, regex fallback
    word_count = len(raw_text.split())
    llm_fields = await extract_structured_fields_via_llm(raw_text)

    if llm_fields:
        hr_email = llm_fields.get("hr_email") or _extract_hr_email(raw_text)
        job_title = llm_fields.get("job_title") or _extract_job_title(raw_text)
        company_name = llm_fields.get("company_name") or _extract_company_name(raw_text)
        required_skills = llm_fields.get("required_skills") or _extract_skills(raw_text)
    else:
        hr_email = _extract_hr_email(raw_text)
        job_title = _extract_job_title(raw_text)
        company_name = _extract_company_name(raw_text)
        required_skills = _extract_skills(raw_text)

    result = ExtractionResult(
        raw_jd_text=raw_text,
        job_title=job_title,
        company_name=company_name,
        hr_email=hr_email,
        application_link=url,
        required_skills=required_skills,
        source_type="link",
        word_count=word_count,
    )

    logger.info(
        "Extraction complete: title=%s, company=%s, skills=%d, words=%d, hr_email=%s",
        result.job_title,
        result.company_name,
        len(result.required_skills),
        result.word_count,
        result.hr_email,
    )

    return result


# ── Custom Exception ──────────────────────────────────────────

class ScrapingError(Exception):
    """Raised when both scraping methods fail."""
    pass
