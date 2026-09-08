"""
CRM Service

Handles all database operations for the Application model using
async SQLAlchemy sessions.
"""

import json as _json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.application import Application
from src.validators.application import ApplicationCreate

logger = logging.getLogger(__name__)


async def create_application(
    db: AsyncSession,
    app_data: ApplicationCreate,
) -> Application:
    logger.info("Creating CRM record for %s at %s", app_data.job_title, app_data.company_name)

    kwargs = dict(
        company_name=app_data.company_name,
        job_title=app_data.job_title,
        persona_used=app_data.persona_used,
        hr_email=app_data.hr_email,
        status=app_data.status,
        source_type=app_data.source_type,
        source_url=app_data.source_url,
        pdf_path=app_data.pdf_path,
        jd_text=app_data.jd_text,
        tailoring_skipped=app_data.tailoring_skipped,
        email_subject=app_data.email_subject,
        email_body=app_data.email_body,
        initial_ats_score=app_data.initial_ats_score,
        final_ats_score=app_data.final_ats_score,
        missing_skills=app_data.missing_skills,
        demanded_skills=app_data.demanded_skills,
        salary_offered=app_data.salary_offered,
        notes=app_data.notes,
    )
    if app_data.pixel_id is not None:
        kwargs["pixel_id"] = app_data.pixel_id
    db_app = Application(**kwargs)

    db.add(db_app)
    await db.commit()
    await db.refresh(db_app)

    logger.info("CRM record created: app_id=%s, pixel_id=%s", db_app.app_id, db_app.pixel_id)
    return db_app


async def get_application_by_pixel(
    db: AsyncSession,
    pixel_id: UUID,
) -> Optional[Application]:
    stmt = select(Application).where(Application.pixel_id == pixel_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_application_by_id(
    db: AsyncSession,
    app_id: UUID,
) -> Optional[Application]:
    """Fetch a single application by its primary key. Used to reconstruct a
    follow-up payload from the durable CRM record when the Redis copy is gone."""
    stmt = select(Application).where(Application.app_id == app_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def mark_email_opened(
    db: AsyncSession,
    pixel_id: UUID,
) -> Optional[Application]:
    stmt = (
        update(Application)
        .where(Application.pixel_id == pixel_id)
        .where(Application.opened_at.is_(None))
        .values(
            opened_at=func.now(),
            # Advance status to 'viewed' only if still 'applied' — never downgrade
            # interview/offer/rejected/etc.
            status=case((Application.status == "applied", "viewed"), else_=Application.status),
        )
    )
    result = await db.execute(stmt)

    if result.rowcount == 0:
        return None

    await db.commit()

    row = await db.execute(select(Application).where(Application.pixel_id == pixel_id))
    updated_app = row.scalar_one_or_none()
    if updated_app:
        logger.info("Marked email opened for app_id=%s", updated_app.app_id)
    return updated_app


async def update_application_status(
    db: AsyncSession,
    app_id: UUID,
    new_status: str,
) -> Optional[Application]:
    stmt = (
        update(Application)
        .where(Application.app_id == app_id)
        .values(status=new_status, updated_at=func.now())
    )
    result = await db.execute(stmt)

    if result.rowcount == 0:
        return None

    await db.commit()

    row = await db.execute(select(Application).where(Application.app_id == app_id))
    updated_app = row.scalar_one_or_none()
    if updated_app:
        logger.info("Updated status to %s for app_id=%s", new_status, app_id)
    return updated_app


async def update_application_notes(
    db: AsyncSession,
    app_id: UUID,
    salary_offered: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[Application]:
    values: Dict[str, Any] = {"updated_at": func.now()}
    if salary_offered is not None:
        values["salary_offered"] = salary_offered
    if notes is not None:
        values["notes"] = notes

    stmt = update(Application).where(Application.app_id == app_id).values(**values)
    await db.execute(stmt)
    await db.commit()

    row = await db.execute(select(Application).where(Application.app_id == app_id))
    return row.scalar_one_or_none()


# ── Duplicate Detection ──────────────────────────────────────


async def check_duplicate_by_url(
    db: AsyncSession,
    source_url: str,
) -> Optional[Application]:
    """Return the most recent application with the same source URL, or None."""
    stmt = (
        select(Application)
        .where(Application.source_url == source_url)
        .order_by(Application.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def check_duplicate_by_company_title(
    db: AsyncSession,
    company_name: str,
    job_title: str,
) -> Optional[Application]:
    """Return the most recent application for the same company+title, or None."""
    if not company_name or not job_title:
        return None
    stmt = (
        select(Application)
        .where(Application.company_name == company_name)
        .where(Application.job_title == job_title)
        .order_by(Application.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ── Follow-up Automation ────────────────────────────────────


async def find_applications_needing_followup(
    db: AsyncSession,
    days: int = 7,
) -> List[Application]:
    """
    Return applications that:
    - have an HR email
    - are still in 'applied' status
    - have not been opened (pixel never fired)
    - have not had a follow-up sent yet
    - were created more than `days` days ago
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    stmt = (
        select(Application)
        .where(Application.hr_email.isnot(None))
        .where(Application.status == "applied")
        .where(Application.opened_at.is_(None))
        .where(Application.followup_sent_at.is_(None))
        .where(Application.created_at <= cutoff)
        .order_by(Application.created_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def mark_followup_sent(
    db: AsyncSession,
    app_id: UUID,
) -> Optional[Application]:
    stmt = (
        update(Application)
        .where(Application.app_id == app_id)
        .values(followup_sent_at=func.now(), updated_at=func.now())
    )
    await db.execute(stmt)
    await db.commit()
    row = await db.execute(select(Application).where(Application.app_id == app_id))
    return row.scalar_one_or_none()


# ── Paginated Listing ────────────────────────────────────────


async def get_applications_page(
    db: AsyncSession,
    page: int = 0,
    page_size: int = 5,
) -> Tuple[List[Application], int]:
    """Return one page of applications plus the total count."""
    count_result = await db.execute(select(func.count()).select_from(Application))
    total = count_result.scalar_one()

    stmt = (
        select(Application)
        .order_by(Application.created_at.desc())
        .offset(page * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def get_recent_applications(db: AsyncSession, limit: int = 10) -> List[Application]:
    stmt = select(Application).order_by(Application.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_all_applications_for_export(db: AsyncSession) -> List[Application]:
    stmt = select(Application).order_by(Application.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ── Analytics ────────────────────────────────────────────────


async def get_weekly_stats(db: AsyncSession) -> Dict[str, Any]:
    """Basic weekly stats (kept for backward compat)."""
    return await get_full_stats(db)


async def get_full_stats(db: AsyncSession) -> Dict[str, Any]:
    """
    Full analytics: weekly funnel, all-time ATS averages by persona,
    and top missing skills across all applications.
    """
    seven_days_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)

    # All-time applications (for ATS + skills analysis)
    all_result = await db.execute(select(Application))
    all_apps: List[Application] = list(all_result.scalars().all())

    # This-week slice
    week_apps = [a for a in all_apps if a.created_at and a.created_at >= seven_days_ago]

    total_sent = len(week_apps)
    total_opened = sum(1 for a in week_apps if a.opened_at is not None)
    total_interviews = sum(1 for a in week_apps if a.status == "interview")
    total_offers = sum(
        1 for a in week_apps if a.status in ("offer_received", "offer_accepted")
    )
    total_accepted = sum(1 for a in week_apps if a.status == "offer_accepted")
    total_rejections = sum(1 for a in week_apps if a.status == "rejected")
    total_ghosted = sum(1 for a in week_apps if a.status == "ghosted")

    open_rate = round((total_opened / total_sent * 100) if total_sent > 0 else 0)
    interview_rate = round((total_interviews / total_sent * 100) if total_sent > 0 else 0)

    # Average ATS score by persona (all-time)
    persona_scores: Dict[str, List[int]] = {}
    for app in all_apps:
        if app.persona_used and app.final_ats_score is not None:
            persona_scores.setdefault(app.persona_used, []).append(app.final_ats_score)

    avg_ats_by_persona = {
        persona: round(sum(scores) / len(scores))
        for persona, scores in persona_scores.items()
    }

    # Top missing skills (all-time)
    skill_counter: Counter = Counter()
    for app in all_apps:
        if app.missing_skills:
            try:
                skills = _json.loads(app.missing_skills)
                if isinstance(skills, list):
                    skill_counter.update(skills)
            except Exception as e:
                logger.debug("Skipping unparseable missing_skills for app_id=%s: %s", app.app_id, str(e))

    top_missing_skills = skill_counter.most_common(10)

    return {
        "sent": total_sent,
        "opened": total_opened,
        "interviews": total_interviews,
        "offers": total_offers,
        "accepted": total_accepted,
        "rejections": total_rejections,
        "ghosted": total_ghosted,
        "open_rate": open_rate,
        "interview_rate": interview_rate,
        "avg_ats_by_persona": avg_ats_by_persona,
        "top_missing_skills": top_missing_skills,
        "total_all_time": len(all_apps),
    }
