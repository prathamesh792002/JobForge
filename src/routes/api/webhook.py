"""
Telegram Webhook Endpoint

POST /v1/telegram/webhook — receives Telegram updates via webhook
and passes them to the python-telegram-bot Application for processing.

Returns 200 immediately before processing so Telegram never sees a slow
or dropped response. Updates are processed in a background task managed
by PTB's Application.create_task() to prevent garbage collection.
"""

import asyncio
import logging

from fastapi import APIRouter, Request, Response
from telegram import Update

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Telegram"])

# Keeps references to in-flight process_update tasks so they aren't GC'd
# before they complete. PTB normally does this internally, but since we're
# bypassing the updater loop we maintain it ourselves.
_active_update_tasks: set[asyncio.Task] = set()


@router.post("/v1/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """
    Receive a Telegram webhook update.

    Returns 200 immediately so Telegram never retries due to a slow
    handler. The update is processed asynchronously in the background.
    """
    try:
        ptb_app = getattr(request.app.state, "ptb_app", None)
        if ptb_app is None:
            logger.error("Bot application not initialized — lifespan startup may have failed")
            return Response(status_code=503)

        data = await request.json()
        update = Update.de_json(data=data, bot=ptb_app.bot)

        logger.debug("Webhook received update_id=%s", update.update_id)

        # Schedule processing as a background task and return 200 immediately.
        # Storing the task in _active_update_tasks prevents premature GC.
        task = asyncio.create_task(ptb_app.process_update(update))
        _active_update_tasks.add(task)
        task.add_done_callback(_active_update_tasks.discard)

        return Response(status_code=200)

    except Exception as e:
        logger.error("Error in webhook endpoint: %s", str(e), exc_info=True)
        return Response(status_code=200)
