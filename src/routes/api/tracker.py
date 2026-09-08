"""
Pixel Tracking Endpoint

Serves a 1x1 transparent GIF and sends a Telegram alert (with status update
buttons) the FIRST time an email is opened. Re-opens are silently ignored.
"""

import base64
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.database.session import get_db
from src.services.repository.application_repo import (
    get_application_by_pixel,
    mark_email_opened,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tracking"])

TRANSPARENT_1X1_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# Known crawler / link-preview user-agent signatures to ignore.
#
# NOTE: We intentionally do NOT filter "googleimageproxy" or "applewebkit":
#   - Gmail loads every image through GoogleImageProxy *when the user opens the
#     email*, so a proxy hit is a genuine open signal — filtering it would miss
#     every Gmail recipient.
#   - "AppleWebKit" appears in Safari/Chrome/iOS Mail and most real clients, so
#     filtering it would suppress the majority of legitimate human opens.
# We still filter actual crawlers and link-preview bots below.
_BOT_SIGNATURES = [
    "bot", "spider", "crawl", "preview",
    "slurp", "mediapartners", "yandex", "bingpreview",
    "facebookexternalhit", "slackbot", "whatsapp", "telegrambot",
    # Email security / anti-spam scanners that pre-fetch links & images.
    "barracuda", "mimecast", "proofpoint", "symantec", "mailcontrol",
    "microsoft-cryptoapi", "trendmicro", "forcepoint", "cisco", "ironport",
    "gmailimageproxy",  # NOTE: distinct from "GoogleImageProxy" (real opens)
]


def _seconds_since_sent(app_record) -> float | None:
    """Seconds elapsed since the email was sent (≈ record creation), or None."""
    created = getattr(app_record, "created_at", None)
    if created is None:
        return None
    now = datetime.now(timezone.utc)
    # created_at is stored naive-UTC (server now()); align both to naive-UTC.
    if created.tzinfo is not None:
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    return (now.replace(tzinfo=None) - created).total_seconds()


@router.get("/v1/tracker/pixel/{pixel_id}.gif")
async def tracking_pixel(
    pixel_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Serve a 1x1 tracking pixel and alert the user on first genuine human open.

    - mark_email_opened returns the Application only if opened_at was NULL
      (i.e. this is the first open). Returns None for re-opens.
    - Bot/proxy fetches are filtered and never trigger alerts.
    """
    user_agent = request.headers.get("user-agent", "")
    logger.info("Tracking pixel requested: pixel_id=%s UA=%r", pixel_id, user_agent[:200])

    try:
        ua_lower = user_agent.lower()
        is_bot = any(sig in ua_lower for sig in _BOT_SIGNATURES)

        if is_bot:
            logger.info("Ignoring bot/scanner fetch (UA: %s) for pixel_id=%s", user_agent[:120], pixel_id)
        else:
            # Peek at the record first so we can distinguish a genuine human open
            # from a delivery-time prefetch (scanner/proxy that fetches the image
            # seconds after the email is sent, before anyone opens it).
            existing = await get_application_by_pixel(db, pixel_id)

            if existing is None:
                logger.info("Pixel %s has no matching application; ignoring.", pixel_id)
                return Response(content=TRANSPARENT_1X1_GIF, media_type="image/gif")

            if existing.opened_at is not None:
                logger.debug("Pixel %s already marked opened; ignoring re-open.", pixel_id)
                return Response(content=TRANSPARENT_1X1_GIF, media_type="image/gif")

            age = _seconds_since_sent(existing)
            if age is not None and age < settings.PIXEL_OPEN_MIN_DELAY_SECONDS:
                # Too soon after send → almost certainly a prefetch, not a human.
                # Do NOT mark opened, so a genuine later open still fires the alert.
                logger.info(
                    "Suppressing prefetch open for pixel_id=%s (%.0fs after send < %ds window).",
                    pixel_id, age, settings.PIXEL_OPEN_MIN_DELAY_SECONDS,
                )
                return Response(content=TRANSPARENT_1X1_GIF, media_type="image/gif")

            # Genuine open: atomically mark (returns the Application only on first open)
            app_record = await mark_email_opened(db, pixel_id)

            if app_record:
                ptb_app = getattr(request.app.state, "ptb_app", None)
                if ptb_app is None:
                    logger.error("Bot app not initialized — cannot send pixel alert for pixel_id=%s", pixel_id)
                else:
                    from src.controllers.bot.keyboards import build_status_update_keyboard
                    import asyncio
                    import html as _html

                    alert_text = (
                        "\U0001f441 <b>Email Opened!</b>\n\n"
                        f"HR contact for <b>{_html.escape(app_record.job_title or 'Unknown Role')}</b> "
                        f"at <b>{_html.escape(app_record.company_name or 'Unknown Company')}</b> "
                        f"just opened your email.\n\n"
                        f"Update the application status when you hear back:"
                    )

                    task = asyncio.create_task(
                        ptb_app.bot.send_message(
                            chat_id=int(settings.TELEGRAM_CHAT_ID),
                            text=alert_text,
                            parse_mode="HTML",
                            reply_markup=build_status_update_keyboard(str(app_record.app_id)),
                        )
                    )
                    task.add_done_callback(
                        lambda t: logger.error("Failed to send pixel alert: %s", t.exception())
                        if not t.cancelled() and t.exception() else None
                    )

    except Exception as e:
        logger.error("Error processing tracking pixel for %s: %s", pixel_id, str(e), exc_info=True)

    return Response(content=TRANSPARENT_1X1_GIF, media_type="image/gif")
