"""
JobForge — FastAPI Application Entry Point

Initializes the FastAPI app, configures the Telegram bot application,
registers the webhook on startup, and cleans up resources on shutdown.
"""

import logging
import sys
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# ── Windows Playwright Fix ───────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI

from src.config.settings import settings
from src.database.session import dispose_engine

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages application startup and shutdown lifecycle.

    Startup:
        - Create and initialize the python-telegram-bot Application
        - Register Telegram webhook
        - Store bot app in FastAPI state for webhook endpoint access
    Shutdown:
        - Dispose database engine
        - Close Redis connections
        - Shutdown bot application
    """
    from src.controllers.bot import setup_bot_app
    from src.database.redis import close_redis, get_redis

    logger.info("JobForge starting up...")
    logger.info("  Webhook URL: %s", settings.TELEGRAM_WEBHOOK_URL)
    logger.info("  Database: %s", settings.DATABASE_URL.split("@")[-1])
    logger.info("  Redis: %s", settings.REDIS_URL)

    # ── Initialize Telegram Bot ──────────────────────────────
    ptb_app = setup_bot_app()

    # Initialize the bot application (but don't start polling)
    await ptb_app.initialize()

    # Register webhook with Telegram
    try:
        webhook_set = await ptb_app.bot.set_webhook(
            url=settings.TELEGRAM_WEBHOOK_URL,
            allowed_updates=["message", "callback_query"],
        )
        if webhook_set:
            logger.info("Telegram webhook registered successfully.")
        else:
            logger.error("Failed to register Telegram webhook!")
    except Exception as e:
        logger.error("Error registering webhook: %s", str(e))
        logger.info("Bot will still process updates if webhook was previously set.")

    # Register command suggestions (shown when user types '/' in chat)
    try:
        from telegram import BotCommand
        await ptb_app.bot.set_my_commands([
            BotCommand("apply",    "Start a new job application"),
            BotCommand("cancel",   "Abort in-progress pipeline & clear state"),
            BotCommand("stats",    "Weekly application stats"),
            BotCommand("insights", "What's converting: outcome analysis"),
            BotCommand("demand",   "Most in-demand skills across JDs (weekly)"),
            BotCommand("coach",    "Skill-gap learning roadmap"),
            BotCommand("history",  "Last 10 applications with status"),
            BotCommand("export",   "Download full history as CSV"),
            BotCommand("help",     "Show all commands & usage"),
        ])
        logger.info("Bot command suggestions registered.")
    except Exception as e:
        logger.error("Failed to register bot commands: %s", str(e))

    # Store bot app in FastAPI state for webhook endpoint access
    app.state.ptb_app = ptb_app

    # Start the bot application (begins processing updates)
    await ptb_app.start()
    logger.info("Telegram bot application started.")

    # ── Startup: clear stale pipeline state ─────────────────
    # If the server crashed mid-pipeline, Redis still holds PROCESSING_PIPELINE.
    # Any new message would be incorrectly queued instead of dispatched.
    try:
        from src.controllers.bot.states import (
            STATE_KEY_PREFIX, CHECKPOINT_KEY_PREFIX, QUEUE_KEY_PREFIX
        )
        r = await get_redis()
        chat_id = settings.TELEGRAM_CHAT_ID
        state_key = f"{STATE_KEY_PREFIX}{chat_id}"
        current = await r.get(state_key)
        if current == "PROCESSING_PIPELINE":
            await r.delete(
                state_key,
                f"{CHECKPOINT_KEY_PREFIX}{chat_id}",
                f"{QUEUE_KEY_PREFIX}{chat_id}",
            )
            logger.info("Cleared stale PROCESSING_PIPELINE state for chat_id=%s", chat_id)
    except Exception as e:
        logger.warning("Startup state cleanup failed (non-fatal): %s", str(e))

    # ── Background Tasks ─────────────────────────────────────
    followup_task = asyncio.create_task(_daily_followup_checker(ptb_app.bot))
    digest_task = asyncio.create_task(_weekly_digest_sender(ptb_app.bot))
    bg_tasks = [followup_task, digest_task]

    if settings.JOB_DISCOVERY_ENABLED:
        bg_tasks.append(asyncio.create_task(_job_discovery(ptb_app.bot)))
    if settings.DAILY_BRIEFING_ENABLED:
        bg_tasks.append(asyncio.create_task(_daily_briefing(ptb_app.bot)))
    logger.info(
        "Background tasks started: follow-up, digest%s%s.",
        ", job discovery" if settings.JOB_DISCOVERY_ENABLED else "",
        ", daily briefing" if settings.DAILY_BRIEFING_ENABLED else "",
    )

    yield

    for t in bg_tasks:
        t.cancel()

    # ── Shutdown ─────────────────────────────────────────────
    logger.info("JobForge shutting down...")

    # Stop and shutdown the bot application
    await ptb_app.stop()
    await ptb_app.shutdown()
    logger.info("  Telegram bot stopped.")

    # Close Redis connections
    await close_redis()
    logger.info("  Redis connections closed.")

    # Dispose database engine
    await dispose_engine()
    logger.info("  Database engine disposed.")


async def _daily_followup_checker(bot) -> None:
    """
    Runs every 6 hours. Finds applications that have been sitting in 'applied'
    status without an email open for FOLLOWUP_DAYS days, then sends a HITL
    reminder to Telegram so the user can approve or dismiss a follow-up email.
    """
    import json
    from src.controllers.bot.keyboards import build_followup_keyboard
    from src.database.session import async_session_factory
    from src.database.redis import get_redis
    from src.services.repository.application_repo import find_applications_needing_followup
    from src.services.communication.mailer import draft_followup_email
    from src.controllers.bot.states import FOLLOWUP_PENDING_PREFIX, FOLLOWUP_TTL

    while True:
        await asyncio.sleep(6 * 3600)
        logger.info("Follow-up checker: scanning for stale applications...")
        try:
            async with async_session_factory() as db:
                apps = await find_applications_needing_followup(db, days=settings.FOLLOWUP_DAYS)

            if not apps:
                logger.info("Follow-up checker: nothing to remind.")
                continue

            r = await get_redis()
            for app in apps:
                key = f"{FOLLOWUP_PENDING_PREFIX}{app.app_id}"
                if await r.exists(key):
                    continue  # Already pending user action

                subject, body = await draft_followup_email(
                    job_title=app.job_title or "the role",
                    company_name=app.company_name or "the company",
                    original_subject=app.email_subject or "",
                )
                payload = {
                    "app_id": str(app.app_id),
                    "hr_email": app.hr_email,
                    "subject": subject,
                    "body": body,
                    "pdf_path": app.pdf_path,
                }
                await r.set(key, json.dumps(payload), ex=FOLLOWUP_TTL)

                import html as _html
                await bot.send_message(
                    chat_id=int(settings.TELEGRAM_CHAT_ID),
                    text=(
                        "🔔 <b>Follow-up Reminder</b>\n\n"
                        f"🏢 <b>{_html.escape(app.company_name or 'Unknown')}</b> — "
                        f"{_html.escape(app.job_title or 'Unknown Role')}\n"
                        f"📅 Applied: {app.created_at.strftime('%d %b %Y')}\n"
                        f"📧 {_html.escape(app.hr_email or '')}\n\n"
                        f"No response in {settings.FOLLOWUP_DAYS}+ days. Send a follow-up?"
                    ),
                    parse_mode="HTML",
                    reply_markup=build_followup_keyboard(str(app.app_id)),
                )
                logger.info("Sent follow-up reminder for app_id=%s", app.app_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Follow-up checker error: %s", str(e), exc_info=True)


async def _weekly_digest_sender(bot) -> None:
    """
    Wakes every hour. On the configured weekday + hour, sends a full stats
    digest to Telegram. Uses a Redis key to ensure it fires exactly once
    per calendar week.
    """
    from datetime import datetime, timezone
    from src.database.redis import get_redis
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import get_full_stats
    import html as _html

    while True:
        await asyncio.sleep(3600)
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() != settings.WEEKLY_DIGEST_DAY or now.hour != settings.WEEKLY_DIGEST_HOUR:
                continue

            r = await get_redis()
            digest_key = f"digest:sent:{now.strftime('%Y-%W')}"
            if await r.exists(digest_key):
                continue

            async with async_session_factory() as db:
                s = await get_full_stats(db)

            lines = [
                "📊 <b>JobForge Weekly Digest</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                f"  📨  Sent:        {s['sent']}",
                f"  👀  Opened:      {s['opened']}  ({s['open_rate']}%)",
                f"  🎯  Interviews:  {s['interviews']}  ({s['interview_rate']}%)",
                f"  💰  Offers:      {s['offers']}",
                f"  👻  Ghosted:     {s['ghosted']}",
                f"  ❌  Rejected:    {s['rejections']}",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                f"<b>All-time:</b> {s['total_all_time']} application(s)",
            ]

            if s["top_missing_skills"]:
                lines.append("\n<b>Top 5 missing skills this week:</b>")
                for skill, count in s["top_missing_skills"][:5]:
                    lines.append(f"  • {_html.escape(skill)}  ×{count}")

            await bot.send_message(
                chat_id=int(settings.TELEGRAM_CHAT_ID),
                text="\n".join(lines),
                parse_mode="HTML",
            )

            # Weekly market-demand report — the deterministic "most in-demand
            # skills across all JDs this week, per persona" analysis.
            try:
                from src.services.ai.insights import generate_demand_report
                async with async_session_factory() as db:
                    demand_text = await generate_demand_report(db, window_days=7)
                await bot.send_message(
                    chat_id=int(settings.TELEGRAM_CHAT_ID),
                    text=demand_text,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Weekly demand report failed: %s", str(e), exc_info=True)

            await r.set(digest_key, "1", ex=8 * 24 * 3600)
            logger.info("Weekly digest + demand report sent.")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Weekly digest error: %s", str(e), exc_info=True)


async def _daily_briefing(bot) -> None:
    """
    Wakes hourly; at DAILY_BRIEFING_HOUR (UTC) sends the morning briefing —
    active discovery leads, follow-ups due, interviews in play, week funnel.
    Skips the message entirely when there's nothing worth saying. Redis key
    guarantees at most one briefing per calendar day.
    """
    from datetime import datetime, timezone as _tz
    from src.database.redis import get_redis
    from src.database.session import async_session_factory
    from src.services.ai.insights import compose_daily_briefing

    while True:
        try:
            await asyncio.sleep(3600)
            now = datetime.now(_tz.utc)
            if now.hour != settings.DAILY_BRIEFING_HOUR:
                continue

            r = await get_redis()
            key = f"briefing:sent:{now.strftime('%Y-%m-%d')}"
            if await r.exists(key):
                continue

            async with async_session_factory() as db:
                text = await compose_daily_briefing(db, r)

            if text:
                await bot.send_message(
                    chat_id=int(settings.TELEGRAM_CHAT_ID),
                    text=text,
                    parse_mode="HTML",
                )
                logger.info("Daily briefing sent.")
            else:
                logger.info("Daily briefing skipped — nothing to report.")
            await r.set(key, "1", ex=2 * 24 * 3600)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Daily briefing error: %s", str(e), exc_info=True)


async def _job_discovery(bot) -> None:
    """
    Periodically scans configured public RSS/Atom job feeds, ATS-pre-screens
    new roles against the candidate's resume, and surfaces high-fit matches to
    Telegram with a one-tap Apply button. Never touches email.
    """
    import html as _html
    from src.controllers.bot.keyboards import build_discovery_keyboard
    from src.database.session import async_session_factory
    from src.database.redis import get_redis
    from src.services.ai.discovery import discover_jobs

    PERSONA_LABEL = {"ai_ml": "AI / ML", "swe": "Software Eng", "game_dev": "Game Dev"}
    interval = max(3600, settings.JOB_DISCOVERY_INTERVAL_HOURS * 3600)

    # Let startup settle before the first scan.
    try:
        await asyncio.sleep(120)
    except asyncio.CancelledError:
        return

    while True:
        logger.info("Job discovery: scanning feeds...")
        try:
            redis = await get_redis()
            async with async_session_factory() as db:
                leads = await discover_jobs(db, redis)

            for lead in leads:
                persona_label = PERSONA_LABEL.get(lead["persona"], lead["persona"])
                text = (
                    "🔎 <b>New matching job found!</b>\n\n"
                    f"🏢 <b>{_html.escape(lead['company'])}</b> — {_html.escape(lead['title'])}\n"
                    f"🎯 Persona: {persona_label}\n"
                    f"📊 ATS match: ~{lead['score']}%\n\n"
                    f"🔗 <a href=\"{_html.escape(lead['url'])}\">View posting</a>"
                )
                try:
                    await bot.send_message(
                        chat_id=int(settings.TELEGRAM_CHAT_ID),
                        text=text,
                        parse_mode="HTML",
                        reply_markup=build_discovery_keyboard(lead["lead_id"]),
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.warning("Discovery: failed to send lead %s: %s", lead["lead_id"], str(e))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Job discovery error: %s", str(e), exc_info=True)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


app = FastAPI(
    title="JobForge",
    description="Automated Job Application Pipeline",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Include API Routers ──────────────────────────────────────
from src.routes.api.webhook import router as webhook_router  # noqa: E402
from src.routes.api.tracker import router as tracker_router    # noqa: E402

app.include_router(webhook_router)
app.include_router(tracker_router)


# ── Health Check ──────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health_check():
    """Simple health check endpoint for Docker / load balancer probes."""
    return {"status": "healthy", "service": "jobforge"}
