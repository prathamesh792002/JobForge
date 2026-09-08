"""
Bot Package

Exports the setup_bot_app function for creating and configuring
the python-telegram-bot Application with all handlers.
"""

import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.controllers.bot.commands import start_command, help_command, unknown_command
from src.controllers.bot.application_flow import apply_command, cancel_command, route_incoming, handle_callback
from src.controllers.bot.dashboard import (
    stats_command, history_command, export_command,
    insights_command, coach_command, demand_command,
)
from src.config import settings

logger = logging.getLogger(__name__)


def setup_bot_app() -> Application:
    """
    Create and configure the python-telegram-bot Application.

    Registers all command handlers, message handlers, and callback
    query handlers. The Application is NOT started here — it is
    initialized and used only for update processing via webhook.

    Returns:
        Configured telegram.ext.Application instance.
    """
    logger.info("Setting up Telegram bot application...")

    builder = Application.builder().token(settings.TELEGRAM_BOT_TOKEN)

    # We manage the event loop ourselves (FastAPI), so disable
    # python-telegram-bot's built-in updater
    builder.updater(None)

    app = builder.build()

    # ── Register Command Handlers ────────────────────────────
    # Order matters: more specific handlers first
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("apply", apply_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("insights", insights_command))
    app.add_handler(CommandHandler("coach", coach_command))
    app.add_handler(CommandHandler("demand", demand_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("export", export_command))

    # Fallback for any OTHER slash-command (e.g. /foo). Registered AFTER the
    # specific commands above so they always take precedence; catches the rest
    # instead of leaving the user with no response.
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # ── Register Callback Query Handler ──────────────────────
    # Handles all inline button presses (send_, discard_, etc.)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # ── Register Message Handler (Passive Mode) ──────────────
    # Catches all non-command messages (URLs, images, etc.)
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            route_incoming,
        )
    )

    logger.info(
        "Bot application configured with %d handler group(s)",
        len(app.handlers),
    )

    return app
