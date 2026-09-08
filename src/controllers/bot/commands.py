import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.controllers.bot.application_flow import is_authorized

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /start command.
    Sends welcome message with instructions.
    """
    chat_id = str(update.effective_chat.id)

    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        logger.warning("Unauthorized /start from chat_id=%s", chat_id)
        return

    logger.info("Received /start from authorized user chat_id=%s", chat_id)

    welcome_text = (
        "🔥 *Welcome to JobForge!*\n\n"
        "I automate your job applications end-to-end:\n\n"
        "🔗 *Send a job posting URL* — I'll scrape it\n"
        "📸 *Send a screenshot* — I'll extract the JD\n"
        "📋 *Paste multiple URLs* — I'll queue and process them all\n\n"
        "Then I'll:\n"
        "• Detect if you've already applied (duplicate guard)\n"
        "• Classify the role (AI/ML, SWE, or Game Dev)\n"
        "• Tailor your resume with ATS keywords\n"
        "• Compile a polished PDF\n"
        "• Draft a cold email (if HR contact found)\n"
        "• Open it as a Gmail draft, or message the recruiter on WhatsApp\n"
        "• Track when your email is opened\n"
        "• Remind you to follow up after 7 days\n\n"
        "*Commands:*\n"
        "/apply — Start guided application flow\n"
        "/cancel — Abort in-progress pipeline & clear state\n"
        "/stats — Full stats (funnel, ATS by persona, missing skills)\n"
        "/history — Paginated application list\n"
        "/export — Download full history as CSV\n"
        "/help — Show all commands & usage\n\n"
        "_Just paste a link or send a screenshot to get started!_"
    )

    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /help command.
    Shows supported commands and usage examples.
    """
    chat_id = str(update.effective_chat.id)

    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Received /help from chat_id=%s", chat_id)

    help_text = (
        "📖 *JobForge — Help*\n\n"
        "*Supported Commands:*\n"
        "/start — Welcome message & quick intro\n"
        "/apply — Guided flow: bot prompts you for a link or screenshot\n"
        "/cancel — Abort current pipeline & clear all pending state\n"
        "/stats — Full analytics: funnel, ATS by persona, top missing skills\n"
        "/history — Paginated application list with status badges\n"
        "/export — Download complete history as a CSV file\n"
        "/help — This message\n\n"
        "*Passive Mode (no command needed):*\n"
        "• Send any URL — auto-detected and scraped\n"
        "• Send multiple URLs (one per line) — batch queued\n"
        "• Send any image — auto-detected and OCR'd via AI\n\n"
        "*Pipeline Features:*\n"
        "• Duplicate guard — warns before re-applying to the same role\n"
        "• Queue — multiple jobs processed serially, never in parallel\n"
        "• Draft in Gmail — open the cold email as a Gmail draft to review & send\n"
        "• WhatsApp outreach — message the recruiter directly if a phone is found\n"
        "• Edit Resume — tweak the PDF via natural language\n"
        "• Follow-up reminders — bot nudges you after 7 days of silence\n\n"
        "*Supported Job Boards:*\n"
        "LinkedIn, Indeed, Naukri, Glassdoor, Lever, Greenhouse, "
        "and most career pages.\n\n"
        "*Tracking:*\n"
        "Every email includes a tracking pixel. You'll get a "
        "Telegram alert when HR opens your email."
    )

    await update.message.reply_text(help_text, parse_mode="Markdown")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fallback for unrecognized slash-commands (e.g. /foo). Without this, PTB's
    command filter drops them and the user gets no response at all.
    """
    if update.message is None:
        return
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    logger.info("Unknown command from chat_id=%s: %s", chat_id, update.message.text)
    await update.message.reply_text(
        "❓ Unrecognized command.\n\n"
        "Try /help to see everything I can do — or just send a job link "
        "or screenshot to get started."
    )
