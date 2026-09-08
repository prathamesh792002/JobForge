import html as _html
import io
import csv
import logging
import math
from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import settings
from src.controllers.bot.keyboards import build_history_pagination_keyboard
from src.database.session import async_session_factory
from src.services.repository.application_repo import (
    get_full_stats,
    get_applications_page,
    get_all_applications_for_export,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 5

STATUS_EMOJI = {
    "applied":        "📨",
    "viewed":         "👀",
    "interview":      "🎯",
    "offer_received": "💰",
    "offer_accepted": "✅",
    "ghosted":        "👻",
    "rejected":       "❌",
    "pending":        "⏳",
    "failed":         "⚠️",
}


def is_authorized(chat_id: str) -> bool:
    return str(chat_id) == str(settings.TELEGRAM_CHAT_ID)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Received /stats from chat_id=%s", chat_id)

    try:
        async with async_session_factory() as db:
            s = await get_full_stats(db)

        # ── Weekly funnel ────────────────────────────────────
        lines = [
            "📊 <b>JobForge Stats</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "<b>This week</b>",
            f"  📨  Sent:         {s['sent']}",
            f"  👀  Opened:       {s['opened']}  ({s['open_rate']}%)",
            f"  🎯  Interviews:   {s['interviews']}  ({s['interview_rate']}%)",
            f"  💰  Offers:       {s['offers']}",
            f"  ✅  Accepted:     {s['accepted']}",
            f"  👻  Ghosted:      {s['ghosted']}",
            f"  ❌  Rejected:     {s['rejections']}",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"<b>All-time total:</b> {s['total_all_time']} application(s)",
        ]

        # ── ATS by persona ───────────────────────────────────
        if s["avg_ats_by_persona"]:
            lines.append("\n<b>Avg ATS score by persona (all-time)</b>")
            persona_labels = {"ai_ml": "AI/ML", "swe": "SWE", "game_dev": "Game Dev"}
            for persona, avg in sorted(s["avg_ats_by_persona"].items(), key=lambda x: -x[1]):
                label = persona_labels.get(persona, persona.upper())
                lines.append(f"  • {label}: {avg}%")

        # ── Top missing skills ───────────────────────────────
        if s["top_missing_skills"]:
            lines.append("\n<b>Top missing skills (all-time)</b>")
            for skill, count in s["top_missing_skills"][:8]:
                lines.append(f"  • {_html.escape(skill)}  ×{count}")

        lines.append("\n<i>Week = last 7 days  •  Updated just now</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error("Error fetching stats: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Database error: {str(e)}")


async def insights_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Outcome analysis: what's converting (personas, ATS bands, missing skills)."""
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Received /insights from chat_id=%s", chat_id)
    try:
        from src.services.ai.insights import generate_insights_text
        async with async_session_factory() as db:
            text = await generate_insights_text(db)
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logger.error("Error generating insights: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Insights failed: {str(e)}")


async def demand_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Market-demand report: most in-demand skills across JDs, per persona.
    Usage: /demand (last 7 days) or /demand all (all-time)."""
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    args = (context.args or [])
    window = None if (args and args[0].lower() in ("all", "alltime", "all-time")) else 7
    logger.info("Received /demand from chat_id=%s (window=%s)", chat_id, window)
    try:
        from src.services.ai.insights import generate_demand_report
        async with async_session_factory() as db:
            text = await generate_demand_report(db, window_days=window)
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logger.error("Error generating demand report: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Demand report failed: {str(e)}")


async def coach_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skill-gap learning roadmap from aggregated missing skills (Gemini-drafted)."""
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Received /coach from chat_id=%s", chat_id)
    status = await update.message.reply_text("🎓 Analyzing your skill gaps...")
    try:
        from src.services.ai.insights import generate_coach_plan
        async with async_session_factory() as db:
            plan = await generate_coach_plan(db)
        await status.edit_text(plan)
    except Exception as e:
        logger.error("Error generating coach plan: %s", e, exc_info=True)
        await status.edit_text(f"❌ Coach failed: {str(e)}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Received /history from chat_id=%s", chat_id)
    await _send_history_page(update.message, page=0)


async def _send_history_page(message_or_reply, page: int) -> None:
    """Send (or re-send) a history page; works for both initial send and pagination."""
    try:
        async with async_session_factory() as db:
            apps, total = await get_applications_page(db, page=page, page_size=PAGE_SIZE)

        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        start = page * PAGE_SIZE + 1

        if not apps and page == 0:
            await message_or_reply.reply_text("No applications logged yet. Apply to some jobs first!")
            return

        lines = [f"<b>📊 Applications (Page {page + 1}/{total_pages})</b>\n"]
        for i, app in enumerate(apps, start):
            company = _html.escape(app.company_name or "Unknown Company")
            title = _html.escape(app.job_title or "Unknown Role")
            date_str = app.created_at.strftime("%d %b %Y") if app.created_at else "—"
            status = app.status or "unknown"
            emoji = STATUS_EMOJI.get(status, "•")
            ats = f"  |  ATS: {app.final_ats_score}%" if app.final_ats_score else ""
            opened = "  👀 Opened" if app.opened_at else ""
            lines.append(
                f"{i}. <b>{company}</b> — {title}\n"
                f"   {emoji} {status.replace('_', ' ').capitalize()}  |  {date_str}{ats}{opened}"
            )

        lines.append(f"\n<i>Total: {total}  •  /export for CSV</i>")

        markup = build_history_pagination_keyboard(page, total_pages)
        await message_or_reply.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logger.error("Error fetching history: %s", e, exc_info=True)
        await message_or_reply.reply_text(f"❌ Database error: {str(e)}")


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import datetime
    from telegram import InputFile

    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Received /export from chat_id=%s", chat_id)

    try:
        async with async_session_factory() as db:
            apps = await get_all_applications_for_export(db)

        if not apps:
            await update.message.reply_text("No applications logged yet. Apply to some jobs first!")
            return

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "#", "Company", "Job Title", "Persona", "Applied On", "Status",
            "HR Email", "ATS Score (Initial)", "ATS Score (Final)", "Tailoring Skipped",
            "Email Opened", "Opened On", "Follow-up Sent", "Salary Offered", "Notes", "Source",
        ])

        for i, app in enumerate(apps, 1):
            applied_on = app.created_at.strftime("%d-%b-%Y") if app.created_at else ""
            opened_on = app.opened_at.strftime("%d-%b-%Y") if app.opened_at else ""
            followup_on = app.followup_sent_at.strftime("%d-%b-%Y") if app.followup_sent_at else ""

            writer.writerow([
                i,
                app.company_name or "",
                app.job_title or "",
                app.persona_used or "",
                applied_on,
                (app.status or "").replace("_", " ").capitalize(),
                app.hr_email or "",
                f"{app.initial_ats_score}%" if app.initial_ats_score is not None else "N/A",
                f"{app.final_ats_score}%" if app.final_ats_score is not None else "N/A",
                "Skipped" if app.tailoring_skipped else "Done",
                "Yes" if app.opened_at else "No",
                opened_on,
                followup_on,
                app.salary_offered or "",
                app.notes or "",
                app.source_type or "",
            ])

        csv_bytes = "﻿" + output.getvalue()
        raw = csv_bytes.encode("utf-8")
        bytes_io = io.BytesIO(raw)
        bytes_io.seek(0)

        filename = f"JobForge_Applications_{datetime.now().strftime('%d%b%Y')}.csv"
        document = InputFile(bytes_io, filename=filename)

        logger.info("Sending CSV export: %d bytes, %d records", len(raw), len(apps))

        await context.bot.send_document(
            chat_id=int(chat_id),
            document=document,
            caption=f"📋 <b>Job Application History</b>\n{len(apps)} record(s) exported.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error("Error exporting CSV: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Export failed: {str(e)}")
