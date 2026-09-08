"""
Proactive Job Discovery Service

Periodically fetches public RSS/Atom job feeds, filters by keywords, dedupes
against the CRM and a Redis "seen" set, then ATS-pre-screens new roles by
reusing the existing extractor + classifier + scoring stack. Roles scoring
above the configured threshold are returned for surfacing on Telegram.

Never touches email. Sources are public job-board feeds configured in .env
(JOB_DISCOVERY_SOURCES).

Pipeline position: background task — runs on an interval from main.py.
"""

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import httpx

from src.config.settings import settings

logger = logging.getLogger(__name__)

SEEN_PREFIX = "discovery:seen:"          # dedupe already-considered job URLs
LEAD_PREFIX = "discovery:lead:"          # maps a short lead_id → job URL (for Apply button)
SEEN_TTL = 30 * 24 * 3600
LEAD_TTL = 7 * 24 * 3600

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _lead_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _parse_feed(xml_bytes: bytes) -> List[Dict[str, str]]:
    """Parse RSS 2.0 or Atom XML into a list of {title, url, summary}."""
    leads: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("Feed XML parse error: %s", str(e))
        return leads

    # RSS 2.0: channel/item with <link> text
    items = root.findall(".//item")
    if items:
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            summary = _strip_html(item.findtext("description") or "")
            if link:
                leads.append({"title": title, "url": link, "summary": summary})
        return leads

    # Atom: entry with <link href="...">
    entries = root.findall(f".//{_ATOM_NS}entry")
    for entry in entries:
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        link = ""
        for link_el in entry.findall(f"{_ATOM_NS}link"):
            rel = link_el.get("rel", "alternate")
            if rel == "alternate" or not link:
                link = link_el.get("href", "")
        summary = _strip_html(
            entry.findtext(f"{_ATOM_NS}summary")
            or entry.findtext(f"{_ATOM_NS}content")
            or ""
        )
        if link:
            leads.append({"title": title, "url": link, "summary": summary})
    return leads


async def _fetch_feed(url: str) -> List[Dict[str, str]]:
    """Fetch a single RSS/Atom feed and parse it. Returns [] on any failure."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (JobForge)"})
            resp.raise_for_status()
            leads = _parse_feed(resp.content)
            logger.info("Discovery: fetched %d leads from %s", len(leads), url[:60])
            return leads
    except Exception as e:
        logger.warning("Discovery: failed to fetch feed %s: %s", url[:60], str(e))
        return []


def _matches_keywords(lead: Dict[str, str], keywords: List[str]) -> bool:
    """
    Word-boundary keyword match. A short keyword like "ai" matches the standalone
    word "ai" (or "ai/ml") but NOT substrings inside "maintain", "training",
    "domain", etc. Handles keywords with symbols (e.g. "c++") via non-alphanumeric
    boundaries rather than \\b.
    """
    if not keywords:
        return True
    haystack = f"{lead.get('title', '')} {lead.get('summary', '')}".lower()
    for k in keywords:
        pattern = r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])"
        if re.search(pattern, haystack):
            return True
    return False


async def _prescreen(url: str) -> Optional[Dict[str, Any]]:
    """
    Run the front half of the pipeline (extract → classify → ATS score) on a
    single job URL. Returns {score, persona, company, title} or None if the
    posting was too sparse / unscrapable.
    """
    from src.services.ai.extractor import extract_from_url
    from src.services.ai.classifier import (
        classify_jd,
        get_yaml_by_persona,
        extract_categorized_skills,
        calculate_ats_score,
        _extract_resume_text,
    )

    extraction = await extract_from_url(url)
    if extraction.word_count < 40:
        logger.info("Discovery: skipping sparse posting (%d words) %s", extraction.word_count, url[:60])
        return None

    persona = await classify_jd(extraction.raw_jd_text)
    base_yaml = get_yaml_by_persona(persona)
    resume_text = _extract_resume_text(base_yaml)

    jd_skills = await extract_categorized_skills(extraction.raw_jd_text, cache_key_prefix="jd")
    resume_skills = await extract_categorized_skills(resume_text, cache_key_prefix="resume")

    # Guard against an all-empty JD extraction (API failure) → false 0%
    if not any(jd_skills.get(t) for t in ("tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies")):
        logger.info("Discovery: skill extraction empty for %s — skipping.", url[:60])
        return None

    score, _missing, _raw = calculate_ats_score(jd_skills, resume_skills)
    return {
        "score": score,
        "persona": persona,
        "company": extraction.company_name or "",
        "title": extraction.job_title or "",
    }


async def discover_jobs(db, redis) -> List[Dict[str, Any]]:
    """
    Scan all configured feeds and return new, high-fit job leads.

    Each returned dict: {lead_id, url, title, company, persona, score}.
    Side effects: marks considered URLs as seen in Redis; stores lead_id→url
    mappings for the Apply button.
    """
    from src.services.repository.application_repo import check_duplicate_by_url

    sources = settings.DISCOVERY_SOURCE_LIST
    if not sources:
        logger.info("Discovery: no sources configured — nothing to scan.")
        return []

    keywords = settings.DISCOVERY_KEYWORD_LIST
    min_ats = settings.JOB_DISCOVERY_MIN_ATS
    max_surface = settings.JOB_DISCOVERY_MAX_PER_RUN
    max_screen = settings.JOB_DISCOVERY_MAX_SCREEN

    # 1. Gather candidate URLs across all feeds (keyword-filtered, deduped)
    candidates: List[Dict[str, str]] = []
    seen_urls_this_run = set()
    for source in sources:
        leads = await _fetch_feed(source)
        for lead in leads:
            url = lead["url"]
            if url in seen_urls_this_run:
                continue
            if not _matches_keywords(lead, keywords):
                continue
            seen_key = f"{SEEN_PREFIX}{_lead_id(url)}"
            if await redis.exists(seen_key):
                continue
            if await check_duplicate_by_url(db, url):
                await redis.set(seen_key, "1", ex=SEEN_TTL)  # already applied — don't reconsider
                continue
            seen_urls_this_run.add(url)
            candidates.append(lead)

    if not candidates:
        logger.info("Discovery: no new candidate roles this run.")
        return []

    logger.info("Discovery: %d new candidate(s); pre-screening up to %d.", len(candidates), max_screen)

    # 2. Pre-screen (bounded) and collect high-fit leads
    results: List[Dict[str, Any]] = []
    screened = 0
    for lead in candidates:
        if screened >= max_screen or len(results) >= max_surface:
            break
        url = lead["url"]
        seen_key = f"{SEEN_PREFIX}{_lead_id(url)}"
        # Mark seen up-front so a below-threshold or broken URL isn't re-screened every run.
        await redis.set(seen_key, "1", ex=SEEN_TTL)
        screened += 1

        try:
            screen = await _prescreen(url)
        except Exception as e:
            logger.warning("Discovery: pre-screen failed for %s: %s", url[:60], str(e))
            continue

        if not screen or screen["score"] < min_ats:
            continue

        lead_id = _lead_id(url)
        await redis.set(f"{LEAD_PREFIX}{lead_id}", url, ex=LEAD_TTL)
        results.append({
            "lead_id": lead_id,
            "url": url,
            "title": screen["title"] or lead.get("title", "Unknown Role"),
            "company": screen["company"] or "Unknown",
            "persona": screen["persona"],
            "score": screen["score"],
        })

    logger.info("Discovery: surfacing %d high-fit role(s).", len(results))
    return results
