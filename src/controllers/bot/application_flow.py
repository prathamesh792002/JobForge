"""
Application Pipeline Controller

Drives the 8-stage pipeline triggered by Telegram messages:
  Stage 1: Extraction   (URL / image / raw text + LLM field enrichment)
  Stage 2: Classification (persona detection via Gemini)
  Stage 3: Tailoring   (ATS self-healing optimization loop)
  Stage 4: Compilation  (RenderCV → PDF)
  Stage 5: HITL Gate   (inline keyboard: Send / Edit Email / Edit Resume / Discard)
  Stage 6: Outreach     (SMTP email + tracking pixel)
  Stage 7: CRM          (SQLAlchemy write)
  Stage 8: Tracking     (pixel open → Telegram alert + status update)

New in this version:
  - Pipeline queue: multiple jobs serialized via Redis list
  - Stage checkpointing: current stage persisted in Redis for diagnostics
  - Extraction confidence gate: abort on login-wall / empty scrapes
  - Duplicate detection: URL-level check before dispatch; company+title warning mid-pipeline
  - Batch URL input: newline-separated URLs queued automatically
  - Editable email: new HITL button lets user revise the email body via LLM
  - Extended status tracking: offer_received, offer_accepted, ghosted
  - Follow-up HITL: background checker routes reminders through inline buttons
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.config.settings import settings
from src.controllers.bot.keyboards import (
    build_approval_keyboard,
    build_duplicate_warning_keyboard,
    build_extended_status_keyboard,
    build_followup_keyboard,
    build_gmail_confirm_keyboard,
    build_outreach_progress_keyboard,
)
from src.controllers.bot.states import (
    AWAITING_APPROVAL,
    AWAITING_EDIT_INSTRUCTION,
    AWAITING_JOB_DATA,
    CHECKPOINT_KEY_PREFIX,
    CHECKPOINT_TTL,
    DUPE_PAYLOAD_PREFIX,
    DUPE_TTL,
    FOLLOWUP_PENDING_PREFIX,
    PENDING_KEY_PREFIX,
    PROCESSING_PIPELINE,
    QUEUE_KEY_PREFIX,
    QUEUE_TTL,
    STATE_KEY_PREFIX,
    STATE_TTL,
)
from src.database.redis import get_redis
from src.services.ai.classifier import classify_jd, get_yaml_by_persona
from src.services.ai.extractor import extract_from_url, extract_structured_fields_via_llm
from src.services.ai.tailor import PoorFitError, _save_yaml_to_temp, tailor_resume
from src.services.ai.vision import download_telegram_photo, extract_from_image
from src.validators.application import ApplicationCreate, ExtractionResult

logger = logging.getLogger(__name__)

# Tracks the currently-running pipeline task per chat so /cancel can actually
# stop an in-flight run (not just clear Redis state).
_active_pipeline_tasks: dict[str, "asyncio.Task"] = {}


def _spawn_pipeline(context, chat_id: str, source_type: str, payload: str):
    """Dispatch the pipeline as a tracked background task so it can be cancelled."""
    task = context.application.create_task(
        dispatch_pipeline(chat_id=chat_id, source_type=source_type, payload=payload, context=context)
    )
    _active_pipeline_tasks[chat_id] = task

    def _clear(t: "asyncio.Task") -> None:
        # Only remove if this is still the registered task for the chat.
        if _active_pipeline_tasks.get(chat_id) is t:
            _active_pipeline_tasks.pop(chat_id, None)

    task.add_done_callback(_clear)
    return task


def is_authorized(chat_id: str) -> bool:
    return str(chat_id) == str(settings.TELEGRAM_CHAT_ID)


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _extraction_confidence_low(extraction: ExtractionResult) -> tuple[bool, str]:
    """
    Return (True, reason) if the extraction result looks like a login wall,
    bot block, interstitial, or otherwise non-job page. Only applied to
    link-sourced content.
    """
    if extraction.source_type != "link":
        return False, ""

    if extraction.word_count < 15:
        return True, "Almost no content was extracted — the page may be login-gated or bot-blocked."

    text_lower = extraction.raw_jd_text.lower()
    gate_phrases = [
        # login / bot walls
        "please sign in", "log in to continue", "sign in to continue",
        "access denied", "403 forbidden", "enable javascript",
        "sign up to continue", "create an account to continue",
        "verify you are human", "are you a robot", "captcha",
        # LinkedIn / redirect interstitials
        "you're about to leave", "you are about to leave", "the link you clicked",
        "join linkedin", "sign in to linkedin", "sign in to see",
    ]
    if any(p in text_lower for p in gate_phrases):
        return True, (
            "The page looks like a login wall or interstitial, not a job posting. "
            "Sites like LinkedIn/Indeed usually block scraping — send a screenshot "
            "or paste the job description text instead."
        )

    # No job signal at all: no title, no company, and thin content → likely a
    # redirect landing page, error page, or non-job page rather than a posting.
    if (not extraction.job_title
            and not extraction.company_name
            and extraction.word_count < 120):
        return True, (
            "No job details could be detected — the link may be a redirect, "
            "login wall, or non-job page. Try a direct posting link, a screenshot, "
            "or paste the job description text."
        )

    return False, ""


# ── Bot Commands ─────────────────────────────────────────────


async def apply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return

    r = await get_redis()
    await r.set(f"{STATE_KEY_PREFIX}{chat_id}", AWAITING_JOB_DATA, ex=STATE_TTL)
    await update.message.reply_text(
        "✅ *Ready!* Send a job posting link or screenshot.\n\n"
        "_I'll start processing as soon as I receive it._",
        parse_mode="Markdown",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    state_key = f"{STATE_KEY_PREFIX}{chat_id}"
    queue_key = f"{QUEUE_KEY_PREFIX}{chat_id}"

    # Snapshot state before we tear it down (for temp-file cleanup).
    pending_data = await r.get(pending_key)
    queue_len = await r.llen(queue_key)

    # 1. Clear all Redis state FIRST so a task finishing right now can't pop the
    #    next queued job or leave stale PROCESSING_PIPELINE state.
    await r.delete(state_key, pending_key, queue_key, f"{CHECKPOINT_KEY_PREFIX}{chat_id}")

    # 2. Hard-cancel any pipeline coroutine actively running for this chat.
    #    Deleting Redis keys alone does NOT stop an in-flight background task.
    task = _active_pipeline_tasks.pop(chat_id, None)
    cancelled_running = task is not None and not task.done()
    if cancelled_running:
        task.cancel()
        logger.info("Cancelled in-flight pipeline task for chat_id=%s", chat_id)

    # 3. Clean up temp files from a pending (HITL-stage) payload, if any.
    if pending_data:
        try:
            from src.services.document.compiler import cleanup_temp_dir
            pdf_path = json.loads(pending_data).get("pdf_path")
            if pdf_path:
                cleanup_temp_dir(pdf_path)
        except Exception as e:
            logger.warning("Failed to clean up temp files in cancel_command: %s", str(e))

    running_note = " Stopped the job in progress." if cancelled_running else ""
    queue_note = f" ({queue_len} queued job(s) also cleared.)" if queue_len > 0 else ""
    await update.message.reply_text(
        f"🛑 *Cancelled.*\n\nAll pending state has been cleared.{running_note}{queue_note} "
        "Send a new job link whenever you're ready.",
        parse_mode="Markdown",
    )


# ── Queue Helpers ─────────────────────────────────────────────


async def _enqueue_or_dispatch(
    chat_id: str,
    source_type: str,
    payload: str,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    r,
    processing_msg: str = "🔍 Processing... hang tight.",
) -> None:
    """Push to Redis queue if pipeline is busy; otherwise dispatch immediately."""
    queue_key = f"{QUEUE_KEY_PREFIX}{chat_id}"
    current_state = await r.get(f"{STATE_KEY_PREFIX}{chat_id}")

    if current_state == PROCESSING_PIPELINE:
        queue_len = await r.rpush(queue_key, json.dumps({"source_type": source_type, "payload": payload}))
        await r.expire(queue_key, QUEUE_TTL)
        await message.reply_text(
            f"⏳ <b>Pipeline is busy.</b> Your job has been queued at position #{queue_len}.\n"
            "I'll process it as soon as the current one finishes.",
            parse_mode="HTML",
        )
    else:
        await r.set(f"{STATE_KEY_PREFIX}{chat_id}", PROCESSING_PIPELINE, ex=3600)
        await message.reply_text(processing_msg)
        _spawn_pipeline(context, chat_id, source_type, payload)


async def _process_queue_next(chat_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    After a pipeline completes (HITL resolved, early exit, or error),
    pop the next queued job and dispatch it — or clear the processing state.
    """
    r = await get_redis()
    queue_key = f"{QUEUE_KEY_PREFIX}{chat_id}"
    next_raw = await r.lpop(queue_key)

    if next_raw:
        item = json.loads(next_raw)
        remaining = await r.llen(queue_key)
        suffix = f" ({remaining} more in queue)" if remaining > 0 else ""
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"⏭️ <b>Starting next queued job{suffix}...</b>",
            parse_mode="HTML",
        )
        await r.set(f"{STATE_KEY_PREFIX}{chat_id}", PROCESSING_PIPELINE, ex=3600)
        _spawn_pipeline(context, chat_id, item["source_type"], item["payload"])
    else:
        await r.delete(f"{STATE_KEY_PREFIX}{chat_id}")


async def _handle_batch_urls(
    chat_id: str,
    urls: list[str],
    message,
    context: ContextTypes.DEFAULT_TYPE,
    r,
) -> None:
    """Validate and queue multiple URLs from a single message."""
    valid_urls = [u for u in urls if re.match(r"https?://\S{5,}", u)]
    if not valid_urls:
        await message.reply_text("⚠️ No valid URLs found in your message.")
        return

    from src.database.session import async_session_factory
    from src.services.repository.application_repo import check_duplicate_by_url

    new_urls = []
    skipped = 0
    async with async_session_factory() as db:
        for url in valid_urls:
            if await check_duplicate_by_url(db, url):
                skipped += 1
            else:
                new_urls.append(url)

    if not new_urls:
        await message.reply_text(
            f"⚠️ All {len(valid_urls)} URL(s) are duplicates — you've already applied to these. Nothing to queue."
        )
        return

    dup_note = f"\n⚠️ {skipped} duplicate(s) skipped." if skipped else ""
    current_state = await r.get(f"{STATE_KEY_PREFIX}{chat_id}")
    queue_key = f"{QUEUE_KEY_PREFIX}{chat_id}"

    if current_state == PROCESSING_PIPELINE:
        for url in new_urls:
            await r.rpush(queue_key, json.dumps({"source_type": "link", "payload": url}))
        await r.expire(queue_key, QUEUE_TTL)
        await message.reply_text(
            f"📥 <b>Batch queued!</b> {len(new_urls)} URL(s) added to the pipeline queue.{dup_note}",
            parse_mode="HTML",
        )
    else:
        first_url, *rest_urls = new_urls
        for url in rest_urls:
            await r.rpush(queue_key, json.dumps({"source_type": "link", "payload": url}))
        if rest_urls:
            await r.expire(queue_key, QUEUE_TTL)

        queue_note = f" ({len(rest_urls)} more queued after)" if rest_urls else ""
        await message.reply_text(
            f"📥 <b>Batch mode — {len(new_urls)} URL(s){queue_note}</b>. Starting the first one...{dup_note}",
            parse_mode="HTML",
        )
        await r.set(f"{STATE_KEY_PREFIX}{chat_id}", PROCESSING_PIPELINE, ex=3600)
        _spawn_pipeline(context, chat_id, "link", first_url)


# ── Message Router ───────────────────────────────────────────


async def route_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    chat_id = str(message.chat_id)
    if not is_authorized(chat_id):
        await message.reply_text("⛔ Unauthorized.")
        return

    r = await get_redis()
    state_key = f"{STATE_KEY_PREFIX}{chat_id}"
    current_state = await r.get(state_key)

    # ── Priority: FSM edit states ────────────────────────────
    if current_state == AWAITING_EDIT_INSTRUCTION and message.text:
        await handle_manual_edit(chat_id, message.text.strip(), context)
        return

    # ── Photo ────────────────────────────────────────────────
    if message.photo:
        if current_state == AWAITING_APPROVAL:
            old_payload = await r.get(f"{PENDING_KEY_PREFIX}{chat_id}")
            if old_payload:
                try:
                    from src.services.document.compiler import cleanup_temp_dir
                    cleanup_temp_dir(json.loads(old_payload).get("pdf_path", ""))
                except Exception as e:
                    logger.debug("Temp cleanup on approval-override (photo) failed: %s", str(e))
            await r.delete(f"{PENDING_KEY_PREFIX}{chat_id}", state_key)
        await _enqueue_or_dispatch(
            chat_id, "image", message.photo[-1].file_id, message, context, r,
            processing_msg="🔍 Processing image... hang tight.",
        )
        return

    # ── Text ─────────────────────────────────────────────────
    if message.text:
        text_content = message.text.strip()

        # Catch unrecognized slash-commands before touching any state
        if text_content.startswith("/"):
            await message.reply_text(
                "❓ Unrecognized command.\n\n"
                "Available commands:\n"
                "/apply — Start a new job application\n"
                "/cancel — Abort current pipeline\n"
                "/start — Welcome message\n"
                "/stats — View your application stats\n"
                "/history — Recent applications\n"
                "/export — Download history as CSV"
            )
            return

        if current_state == AWAITING_APPROVAL:
            old_payload = await r.get(f"{PENDING_KEY_PREFIX}{chat_id}")
            if old_payload:
                try:
                    from src.services.document.compiler import cleanup_temp_dir
                    cleanup_temp_dir(json.loads(old_payload).get("pdf_path", ""))
                except Exception as e:
                    logger.debug("Temp cleanup on approval-override (text) failed: %s", str(e))
            await r.delete(f"{PENDING_KEY_PREFIX}{chat_id}", state_key)

        # ── Batch URL detection ──────────────────────────────
        url_lines = [ln.strip() for ln in text_content.splitlines() if ln.strip().startswith("http")]
        if len(url_lines) > 1:
            await _handle_batch_urls(chat_id, url_lines, message, context, r)
            return

        if text_content.startswith("http"):
            first_token = text_content.split()[0]
            if not re.match(r"https?://\S{5,}", first_token):
                await message.reply_text(
                    "⚠️ That doesn't look like a valid URL.\n\n"
                    "Please send a complete job posting link, e.g.:\n"
                    "<code>https://jobs.example.com/role/12345</code>",
                    parse_mode="HTML",
                )
                return

            # Duplicate URL check
            from src.database.session import async_session_factory
            from src.services.repository.application_repo import check_duplicate_by_url
            async with async_session_factory() as db:
                existing = await check_duplicate_by_url(db, first_token)
            if existing:
                url_hash = _url_hash(first_token)
                await r.set(
                    f"{DUPE_PAYLOAD_PREFIX}{url_hash}",
                    json.dumps({"source_type": "link", "payload": first_token}),
                    ex=DUPE_TTL,
                )
                await message.reply_text(
                    "⚠️ <b>Already Applied!</b>\n\n"
                    f"🏢 <b>{html.escape(existing.company_name or 'Unknown')}</b> — "
                    f"{html.escape(existing.job_title or 'Unknown Role')}\n"
                    f"📅 Applied: {existing.created_at.strftime('%d %b %Y')}  •  "
                    f"{existing.status.replace('_', ' ').capitalize()}\n\n"
                    "Do you want to apply again anyway?",
                    parse_mode="HTML",
                    reply_markup=build_duplicate_warning_keyboard(url_hash),
                )
                return

            await _enqueue_or_dispatch(
                chat_id, "link", first_token, message, context, r,
                processing_msg="🔍 Scraping job posting... hang tight.",
            )

        else:
            words = text_content.split()
            lower = text_content.lower()

            # Detect a natural-language question/query (not a JD) so we don't run
            # the pipeline on it. A real job posting never starts with a question
            # word or ends with "?" — and is longer than a passing question.
            first_word = words[0].lower().strip("?,.'\"") if words else ""
            _QUERY_WORDS = {
                "what", "what's", "whats", "how", "why", "when", "who", "whom",
                "which", "where", "can", "could", "would", "should", "do", "does",
                "did", "is", "are", "will", "tell", "show", "give", "list",
                "explain", "help", "hi", "hey", "hello", "thanks", "thank",
            }
            looks_conversational = text_content.endswith("?") or first_word in _QUERY_WORDS

            if looks_conversational and len(words) < 40:
                # Point them at the most relevant command when we can guess intent.
                hint = ""
                if any(k in lower for k in ("demand", "in-demand", "in demand", "skill")):
                    hint = "\n\n👉 You likely want <b>/demand</b> — most in-demand skills per persona."
                elif any(k in lower for k in ("convert", "open rate", "interview rate", "funnel", "stat")):
                    hint = "\n\n👉 Try <b>/stats</b> or <b>/insights</b>."
                elif any(k in lower for k in ("learn", "coach", "roadmap", "upskill", "gap")):
                    hint = "\n\n👉 Try <b>/coach</b> for your learning plan."

                await message.reply_text(
                    "🤔 That looks like a <b>question</b>, not a job posting — and I work by "
                    "commands, not free chat.\n\n"
                    "<b>Analysis commands:</b>\n"
                    "• /demand — most in-demand skills across your JDs (per persona)\n"
                    "• /insights — what's converting for you\n"
                    "• /coach — your skill-gap learning plan\n"
                    "• /stats — application funnel\n"
                    "• /help — everything I can do\n\n"
                    "<i>To apply to a job, send a link, a screenshot, or the full JD (40+ words).</i>"
                    + hint,
                    parse_mode="HTML",
                )
                return

            if len(words) < 5:
                await message.reply_text(
                    "⚠️ That doesn't look like a job description.\n\n"
                    "Please send one of:\n"
                    "• A job posting URL\n"
                    "• A screenshot of the job posting\n"
                    "• The full job description text (at least a few sentences)"
                )
                return
            await _enqueue_or_dispatch(
                chat_id, "raw_text", text_content, message, context, r,
                processing_msg="🔍 Processing raw job description... hang tight.",
            )
        return

    if current_state == AWAITING_APPROVAL:
        await message.reply_text(
            "⚠️ You have a pending resume. Please approve/edit it, "
            "or send a new job link to restart."
        )
        return

    await message.reply_text(
        "👋 Send me a job posting link or screenshot to get started.\n"
        "Or type /apply for the guided flow."
    )


# ── Pipeline ─────────────────────────────────────────────────


async def dispatch_pipeline(
    chat_id: str,
    source_type: str,
    payload: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Run the full 8-stage job application pipeline as a background task."""
    logger.info("Pipeline dispatch: chat_id=%s, source_type=%s", chat_id, source_type)

    r = await get_redis()
    # Ensure PROCESSING_PIPELINE state is set (refresh TTL)
    await r.set(f"{STATE_KEY_PREFIX}{chat_id}", PROCESSING_PIPELINE, ex=3600)

    async def _checkpoint(stage: str) -> None:
        await r.set(f"{CHECKPOINT_KEY_PREFIX}{chat_id}", stage, ex=CHECKPOINT_TTL)

    async def _early_exit(msg: str = "") -> None:
        """Clean up state and advance the queue after a non-HITL pipeline exit."""
        await r.delete(f"{CHECKPOINT_KEY_PREFIX}{chat_id}")
        if msg:
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="HTML")
            except Exception:
                pass
        await _process_queue_next(chat_id, context)

    async def _safe_edit(msg_obj, text: str) -> None:
        """Edit a Telegram message, silently ignoring rate-limit / not-modified errors."""
        try:
            await msg_obj.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    try:
        await _dispatch_pipeline_inner(
            chat_id, source_type, payload, context,
            r, _checkpoint, _early_exit, _safe_edit,
        )
    except asyncio.CancelledError:
        # /cancel hard-cancelled this run. State/queue were already cleared by
        # cancel_command — just stop. Do NOT advance the queue or post an error.
        logger.info("Pipeline task cancelled for chat_id=%s — stopping.", chat_id)
        try:
            await r.delete(f"{CHECKPOINT_KEY_PREFIX}{chat_id}")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error("Unhandled exception in dispatch_pipeline: %s", str(e), exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=(
                    "❌ <b>Pipeline crashed unexpectedly.</b>\n\n"
                    f"<code>{html.escape(str(e)[:200])}</code>\n\n"
                    "State has been cleared. Send a new job link to try again."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        await r.delete(f"{CHECKPOINT_KEY_PREFIX}{chat_id}")
        await _process_queue_next(chat_id, context)


async def _dispatch_pipeline_inner(
    chat_id: str,
    source_type: str,
    payload: str,
    context: ContextTypes.DEFAULT_TYPE,
    r,
    _checkpoint,
    _early_exit,
    _safe_edit,
) -> None:
    """Inner pipeline body — all stages. Wrapped by dispatch_pipeline for safety."""

    # ── Stage 1: Extraction ─────────────────────────────────
    await _checkpoint("extraction")
    try:
        if source_type == "link":
            extraction = await extract_from_url(payload)

        elif source_type == "image":
            image_bytes = await download_telegram_photo(payload, context.bot)
            extraction = await extract_from_image(image_bytes, file_id=payload)

        elif source_type == "raw_text":
            extraction = ExtractionResult(
                raw_jd_text=payload,
                job_title=None,
                company_name=None,
                hr_email=None,
                word_count=len(payload.split()),
                source_type="raw_text",
            )
            llm_fields = await extract_structured_fields_via_llm(payload)
            if llm_fields:
                extraction.job_title = llm_fields.get("job_title")
                extraction.company_name = llm_fields.get("company_name")
                extraction.hr_email = llm_fields.get("hr_email")
                extraction.required_skills = llm_fields.get("required_skills", [])

        else:
            await _early_exit("⚠️ Unknown source type.")
            return

    except Exception as e:
        logger.error("Extraction failed: %s", str(e), exc_info=True)
        await _early_exit(
            "❌ <b>Extraction failed.</b>\n\n"
            f"Error: <code>{html.escape(str(e)[:200])}</code>\n\n"
            "Please try a different link or paste the JD text directly."
        )
        return

    # Extraction confidence gate (link sources only)
    low_confidence, confidence_reason = _extraction_confidence_low(extraction)
    if low_confidence:
        await _early_exit(
            f"❌ <b>Extraction failed: no job content found.</b>\n\n"
            f"{html.escape(confidence_reason)}\n\n"
            "Try a different link, or paste the job description text directly."
        )
        return

    # ── Stage 2: Classification ─────────────────────────────
    await _checkpoint("classification")
    try:
        persona_tag = await classify_jd(extraction.raw_jd_text)
        base_yaml = get_yaml_by_persona(persona_tag)
    except Exception as e:
        logger.error("Classification failed: %s", str(e), exc_info=True)
        persona_tag = "swe"
        base_yaml = get_yaml_by_persona(persona_tag)

    # Soft duplicate warning for image / raw_text (we can only check after extraction)
    if source_type in ("image", "raw_text") and extraction.company_name and extraction.job_title:
        try:
            from src.database.session import async_session_factory
            from src.services.repository.application_repo import check_duplicate_by_company_title
            async with async_session_factory() as db:
                existing = await check_duplicate_by_company_title(
                    db, extraction.company_name, extraction.job_title
                )
            if existing:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"⚠️ <b>Possible duplicate:</b> you may have already applied to "
                        f"<b>{html.escape(extraction.company_name)}</b> — "
                        f"{html.escape(extraction.job_title)} "
                        f"({existing.created_at.strftime('%d %b %Y')}). "
                        "Proceeding anyway..."
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    status_msg = await context.bot.send_message(
        chat_id=int(chat_id),
        text=(
            "✅ <b>Extraction &amp; Classification Complete</b>\n\n"
            f"💼 <b>Title:</b> {html.escape(extraction.job_title or 'Not detected')}\n"
            f"🏢 <b>Company:</b> {html.escape(extraction.company_name or 'Not detected')}\n"
            f"📧 <b>HR Email:</b> {html.escape(extraction.hr_email or 'Not found')}\n"
            f"📝 <b>Word count:</b> {extraction.word_count}\n"
            f"🎯 <b>Persona:</b> {html.escape(persona_tag)}\n\n"
            "🔧 <b>Analyzing skills &amp; optimizing resume...</b>"
        ),
        parse_mode="HTML",
    )

    # ── Stage 3: Tailoring ──────────────────────────────────
    await _checkpoint("tailoring")
    missing_skills: list = []
    initial_score = 0
    final_score = 0
    skipped = False
    tailored_yaml_content = base_yaml

    async def update_status(msg: str) -> None:
        try:
            await status_msg.edit_text(msg, parse_mode="HTML")
        except Exception:
            pass

    try:
        tailored_yaml_content, yaml_path, skipped, missing_skills, initial_score, final_score = (
            await tailor_resume(extraction, base_yaml, chat_id, progress_callback=update_status)
        )
    except PoorFitError as e:
        missing_text = html.escape(", ".join(e.missing_skills[:15]) if e.missing_skills else "None")
        await _safe_edit(
            status_msg,
            f"❌ <b>Application Rejected: Poor Fit</b>\n\n"
            f"Initial ATS score: <b>{e.score}%</b> (minimum: {settings.MINIMUM_FIT_THRESHOLD}%)\n\n"
            f"<b>Critical missing skills:</b> {missing_text}\n\n"
            f"<i>Tailoring aborted to prevent hallucinated qualifications.</i>",
        )
        await r.delete(f"{CHECKPOINT_KEY_PREFIX}{chat_id}")
        await _process_queue_next(chat_id, context)
        return
    except Exception as e:
        logger.error("Tailoring failed: %s", str(e), exc_info=True)
        await _safe_edit(
            status_msg,
            f"❌ <b>Tailoring failed:</b> <code>{html.escape(str(e)[:100])}</code>"
            f"\n<i>Proceeding with base resume.</i>",
        )
        yaml_path = _save_yaml_to_temp(base_yaml, chat_id)

    # Build result summary
    missing_skills_text = html.escape(", ".join(missing_skills[:15]) if missing_skills else "None")
    result_text = (
        "✅ <b>Extraction &amp; Optimization Complete</b>\n\n"
        f"🏢 <b>Company:</b> {html.escape(extraction.company_name or 'Not detected')}\n"
        f"💼 <b>Title:</b> {html.escape(extraction.job_title or 'Not detected')}\n"
        f"📧 <b>HR Email:</b> {html.escape(extraction.hr_email or 'Not found')}\n"
        f"📝 <b>Word count:</b> {extraction.word_count}\n"
        f"🎯 <b>Persona:</b> {html.escape(persona_tag)}\n\n"
    )
    if not skipped:
        result_text += (
            f"📊 <b>ATS Match Score:</b> {final_score}% (optimized from {initial_score}%)\n"
            f"🔧 <b>Missing skills/keywords:</b> {missing_skills_text}\n\n"
        )
    if extraction.word_count < 40:
        result_text += "⚠️ <i>JD is sparse (&lt;40 words). Tailoring was skipped.</i>\n\n"

    await _safe_edit(status_msg, result_text + "⏳ <b>Compiling final PDF...</b>")

    # ── Stage 4: Compilation ────────────────────────────────
    await _checkpoint("compilation")
    pdf_path = None
    try:
        from src.services.document.compiler import compile_pdf
        pdf_path = await compile_pdf(yaml_path, use_custom_theme=True)
    except Exception as e:
        logger.error("PDF compilation failed: %s", str(e), exc_info=True)
        await _safe_edit(
            status_msg,
            result_text + "❌ <b>PDF Compilation failed.</b>\n\nSending optimized YAML instead:",
        )
        import io
        yaml_bytes = io.BytesIO(tailored_yaml_content.encode("utf-8"))
        await context.bot.send_document(
            chat_id=int(chat_id),
            document=yaml_bytes,
            filename="resume_fallback.yaml",
            caption="Compilation failed — resume YAML saved here.",
        )
        from src.services.document.compiler import cleanup_temp_dir
        cleanup_temp_dir(yaml_path)
        await r.delete(f"{CHECKPOINT_KEY_PREFIX}{chat_id}")
        await _process_queue_next(chat_id, context)
        return

    await _safe_edit(status_msg, result_text + "✅ <b>PDF compiled!</b>")
    with open(pdf_path, "rb") as pdf_file:
        await context.bot.send_document(
            chat_id=int(chat_id),
            document=pdf_file,
            filename=f"Resume_{extraction.company_name or 'Application'}.pdf",
            caption="Here is your tailored, ATS-optimized resume.",
        )

    # ── Stage 5: HITL Gate ──────────────────────────────────
    await _checkpoint("hitl")

    missing_skills_json = json.dumps(missing_skills) if missing_skills else None

    # Persist the FULL JD skill demand (not just what's missing) for the weekly
    # market-demand report. Cached by JD-text hash from the tailoring step, so
    # this is a Redis cache hit — no extra LLM call on the normal path.
    demanded_skills_json = None
    if extraction.word_count >= 40:
        try:
            from src.services.ai.classifier import extract_categorized_skills
            jd_sk = await extract_categorized_skills(extraction.raw_jd_text, cache_key_prefix="jd")
            if any(jd_sk.get(t) for t in ("tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies")):
                demanded_skills_json = json.dumps(jd_sk)
        except Exception as e:
            logger.warning("Demanded-skills capture failed (non-fatal): %s", str(e))

    app_data = ApplicationCreate(
        company_name=extraction.company_name,
        job_title=extraction.job_title,
        persona_used=persona_tag,
        hr_email=extraction.hr_email,
        status="pending",
        source_type=extraction.source_type,
        source_url=extraction.application_link,
        pdf_path=pdf_path,
        jd_text=extraction.raw_jd_text,
        tailoring_skipped=skipped,
        initial_ats_score=initial_score,
        final_ats_score=final_score,
        missing_skills=missing_skills_json,
        demanded_skills=demanded_skills_json,
    )

    if extraction.hr_email:
        from src.services.communication.mailer import draft_email, research_company_hook

        # Live company research → personalization hook in the cold email
        company_hook = ""
        if settings.COMPANY_RESEARCH_ENABLED and extraction.company_name:
            try:
                await _safe_edit(status_msg, result_text + "🔎 <b>Researching the company...</b>")
                company_hook = await research_company_hook(extraction.company_name)
            except Exception as e:
                logger.warning("Company research failed (non-fatal): %s", str(e))

        subject, body = await draft_email(
            candidate_name="Candidate",
            job_title=extraction.job_title or "Open Role",
            company_name=extraction.company_name or "Company",
            pdf_path=pdf_path,
            jd_text=extraction.raw_jd_text,
            company_hook=company_hook,
        )
        app_data.email_subject = subject
        app_data.email_body = body

    # Detect a recruiter phone number for optional WhatsApp outreach
    if settings.WHATSAPP_ENABLED:
        try:
            from src.services.communication.whatsapp import (
                extract_contact_phone, draft_whatsapp_message,
            )
            # Prefer a phone the extractor captured directly (vision), else scan the JD text.
            phone = extraction.contact_phone or await extract_contact_phone(extraction.raw_jd_text)
            if phone:
                app_data.contact_phone = phone
                app_data.whatsapp_message = await draft_whatsapp_message(
                    job_title=extraction.job_title or "the role",
                    company_name=extraction.company_name or "your company",
                    jd_text=extraction.raw_jd_text,
                )
                logger.info("WhatsApp outreach available for this application.")
        except Exception as e:
            logger.warning("WhatsApp phone detection failed (non-fatal): %s", str(e))

    temp_id = str(uuid.uuid4())
    await r.set(
        f"{PENDING_KEY_PREFIX}{chat_id}",
        json.dumps(app_data.model_dump(mode="json")),
        ex=STATE_TTL,
    )

    # Transition to AWAITING_APPROVAL so the queue knows to wait for HITL resolution
    await r.set(f"{STATE_KEY_PREFIX}{chat_id}", AWAITING_APPROVAL, ex=STATE_TTL)
    await r.delete(f"{CHECKPOINT_KEY_PREFIX}{chat_id}")

    keyboard = build_approval_keyboard(
        temp_id,
        has_email=bool(extraction.hr_email),
        has_phone=bool(app_data.contact_phone),
    )

    wa_note = ""
    if app_data.contact_phone:
        wa_note = (
            f"\n📲 <b>Recruiter phone found:</b> <code>+{html.escape(app_data.contact_phone)}</code>"
            " — tap <b>Message on WhatsApp</b> to reach out there.\n"
        )

    if extraction.hr_email:
        body_preview = re.sub(r"<[^>]+>", " ", app_data.email_body or "").strip()
        body_preview = " ".join(body_preview.split())[:200]
        if len(app_data.email_body or "") > 200:
            body_preview += "..."

        hitl_text = (
            f"📧 <b>HR Email Found:</b> <code>{html.escape(extraction.hr_email)}</code>\n\n"
            f"<b>Subject:</b> {html.escape(app_data.email_subject or '')}\n\n"
            f"<b>Email preview:</b>\n{html.escape(body_preview)}\n"
            f"{wa_note}\n"
            "Approve to send, edit, or discard:"
        )
    else:
        hitl_text = (
            "⚠️ <b>No HR Email Found.</b>\n"
            f"{wa_note}\n"
            "Save this application to your CRM or edit the resume?"
        )

    # Strategy advisor: one line of agent reasoning about THIS application
    if settings.STRATEGY_ADVISOR_ENABLED and not skipped:
        try:
            from src.services.ai.insights import advise_strategy
            note = await advise_strategy(
                extraction.job_title or "", extraction.company_name or "",
                initial_score, final_score, missing_skills,
            )
            if note:
                hitl_text += f"\n\n🧭 <b>Strategy:</b> <i>{html.escape(note)}</i>"
        except Exception as e:
            logger.warning("Strategy advisor failed (non-fatal): %s", str(e))

    await context.bot.send_message(
        chat_id=int(chat_id),
        text=hitl_text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    # Queue advances only when user resolves this HITL (send / discard)


# ── Callback Router ──────────────────────────────────────────


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    chat_id = str(query.message.chat_id)
    if not is_authorized(chat_id):
        await query.edit_message_text("⛔ Unauthorized.")
        return

    data = query.data
    logger.info("Callback: chat_id=%s data=%s", chat_id, data)

    if data.startswith("send_"):
        await handle_send_approval(chat_id, data[len("send_"):], query, context)
    elif data.startswith("open_gmail_"):
        await handle_open_gmail(chat_id, data[len("open_gmail_"):], query, context)
    elif data.startswith("gmail_sent_"):
        await handle_gmail_sent(chat_id, data[len("gmail_sent_"):], query, context)
    elif data.startswith("open_whatsapp_"):
        await handle_open_whatsapp(chat_id, data[len("open_whatsapp_"):], query, context)
    elif data.startswith("whatsapp_sent_"):
        await handle_whatsapp_sent(chat_id, data[len("whatsapp_sent_"):], query, context)
    elif data.startswith("finish_"):
        await handle_finish(chat_id, data[len("finish_"):], query, context)
    elif data.startswith("edit_"):
        await handle_edit_resume(chat_id, data[len("edit_"):], query, context)
    elif data.startswith("discard_"):
        await handle_discard(chat_id, data[len("discard_"):], query, context)
    elif data.startswith("proceed_dupe_"):
        await handle_proceed_duplicate(chat_id, data[len("proceed_dupe_"):], query, context)
    elif data.startswith("abort_dupe_"):
        await query.edit_message_text("✅ Aborted. No new application was created.")
    elif data.startswith("status_interview_"):
        await handle_extended_status(chat_id, data[len("status_interview_"):], "interview", query, context)
    elif data.startswith("status_rejected_"):
        await handle_extended_status(chat_id, data[len("status_rejected_"):], "rejected", query, context)
    elif data.startswith("status_offer_"):
        await handle_extended_status(chat_id, data[len("status_offer_"):], "offer_received", query, context)
    elif data.startswith("status_accepted_"):
        await handle_extended_status(chat_id, data[len("status_accepted_"):], "offer_accepted", query, context)
    elif data.startswith("status_ghosted_"):
        await handle_extended_status(chat_id, data[len("status_ghosted_"):], "ghosted", query, context)
    elif data.startswith("send_followup_"):
        await handle_send_followup(chat_id, data[len("send_followup_"):], query, context)
    elif data.startswith("dismiss_followup_"):
        await handle_dismiss_followup(chat_id, data[len("dismiss_followup_"):], query)
    elif data.startswith("history_page_"):
        page = int(data[len("history_page_"):])
        await handle_history_page(chat_id, page, query)
    elif data.startswith("discover_apply_"):
        await handle_discovery_apply(chat_id, data[len("discover_apply_"):], query, context)
    elif data.startswith("discover_dismiss_"):
        await handle_discovery_dismiss(chat_id, data[len("discover_dismiss_"):], query)
    else:
        await query.edit_message_text("Unknown action.")


# ── HITL Action Handlers ─────────────────────────────────────


async def handle_extended_status(
    chat_id: str, app_id_str: str, new_status: str, query,
    context: ContextTypes.DEFAULT_TYPE = None,
) -> None:
    import uuid as _uuid
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import update_application_status

    try:
        app_id = _uuid.UUID(app_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid application ID.")
        return

    STATUS_EMOJI = {
        "interview": "🎯",
        "offer_received": "💰",
        "offer_accepted": "✅",
        "ghosted": "👻",
        "rejected": "❌",
    }

    async with async_session_factory() as db:
        updated = await update_application_status(db, app_id, new_status)
        # Capture plain values while the session is open (object detaches after).
        if updated:
            job_title = updated.job_title or ""
            company_name = updated.company_name or ""
            jd_text = updated.jd_text or ""
            persona_used = updated.persona_used or ""

    if not updated:
        await query.edit_message_text("⚠️ Application not found.")
        return

    emoji = STATUS_EMOJI.get(new_status, "•")
    label = new_status.replace("_", " ").capitalize()
    await query.edit_message_text(
        f"{emoji} <b>Status updated:</b> {label}\n\n"
        f"🏢 {html.escape(company_name or 'Unknown')} — "
        f"{html.escape(job_title or 'Unknown Role')}",
        parse_mode="HTML",
    )

    # ── Auto-generate an interview prep brief when moving to 'interview' ──
    if new_status == "interview" and context is not None:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="🧠 <b>Generating your interview prep brief...</b>\n<i>Researching the company — this takes ~15-30s.</i>",
            parse_mode="HTML",
        )
        context.application.create_task(
            _generate_and_send_prep(
                chat_id, job_title, company_name, jd_text, persona_used, context
            )
        )


async def _send_long_text(chat_id: str, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send text to Telegram in <=4000-char chunks (plain text, no parse mode)."""
    MAX = 4000
    if len(text) <= MAX:
        await context.bot.send_message(chat_id=int(chat_id), text=text, disable_web_page_preview=True)
        return
    # Split on paragraph boundaries where possible
    remaining = text
    while remaining:
        if len(remaining) <= MAX:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n\n", 0, MAX)
            if split_at == -1:
                split_at = remaining.rfind("\n", 0, MAX)
            if split_at == -1:
                split_at = MAX
            chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip("\n")
        await context.bot.send_message(chat_id=int(chat_id), text=chunk, disable_web_page_preview=True)


async def _generate_and_send_prep(
    chat_id: str, job_title: str, company_name: str, jd_text: str,
    persona_used: str, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Background task: build and deliver the interview prep brief."""
    try:
        from src.services.ai.interview_prep import generate_interview_prep
        from src.services.ai.classifier import get_yaml_by_persona, _extract_resume_text

        resume_text = ""
        if persona_used:
            try:
                resume_yaml = get_yaml_by_persona(persona_used)
                resume_text = _extract_resume_text(resume_yaml)
            except Exception as e:
                logger.warning("Could not load persona resume for prep: %s", str(e))

        brief = await generate_interview_prep(
            job_title=job_title,
            company_name=company_name,
            jd_text=jd_text,
            resume_text=resume_text,
        )

        header = f"🎯 Interview Prep — {company_name or 'the company'}\n{'━' * 20}\n\n"
        await _send_long_text(chat_id, header + brief, context)

    except Exception as e:
        logger.error("Interview prep generation failed: %s", str(e), exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="⚠️ Couldn't generate the interview prep brief. You can retry by re-tapping the Interview button.",
            )
        except Exception:
            pass


# Control/transport keys that are NOT columns on the Application model.
_OUTREACH_CONTROL_KEYS = (
    "crm_app_id", "crm_pixel_id", "email_done", "whatsapp_done",
    "draft_pixel_id", "draft_url", "contact_phone", "whatsapp_message",
)


async def _ensure_crm_record(db, payload: dict):
    """
    Create the CRM record once and reuse it across outreach channels so a job
    reached by BOTH email and WhatsApp produces a single record.
    Returns (app_id: UUID, pixel_id: UUID).
    """
    import uuid as _uuid
    from src.services.repository.application_repo import create_application

    if payload.get("crm_app_id"):
        pix = payload.get("crm_pixel_id")
        return _uuid.UUID(payload["crm_app_id"]), (_uuid.UUID(pix) if pix else None)

    clean = {k: v for k, v in payload.items() if k not in _OUTREACH_CONTROL_KEYS}
    db_app = await create_application(db, ApplicationCreate(**clean))
    payload["crm_app_id"] = str(db_app.app_id)
    payload["crm_pixel_id"] = str(db_app.pixel_id)
    return db_app.app_id, db_app.pixel_id


async def _finalize_and_cleanup(chat_id: str, payload: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clean temp files, clear pending state, offer status tracking, advance the queue."""
    r = await get_redis()
    from src.services.document.compiler import cleanup_temp_dir

    pdf_path = payload.get("pdf_path")
    if pdf_path:
        cleanup_temp_dir(pdf_path)
    await r.delete(f"{PENDING_KEY_PREFIX}{chat_id}")

    crm_app_id = payload.get("crm_app_id")
    if crm_app_id:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="📌 <b>Update status when you hear back:</b>",
            parse_mode="HTML",
            reply_markup=build_extended_status_keyboard(crm_app_id),
        )
    await _process_queue_next(chat_id, context)


async def _progress_or_finalize(
    chat_id: str, temp_id: str, payload: dict,
    context: ContextTypes.DEFAULT_TYPE, query, done_text: str,
) -> None:
    """
    After one channel is used: if another channel is still available, persist
    state and show it (+ Done). Otherwise finalize.
    """
    r = await get_redis()
    has_email = bool(payload.get("hr_email"))
    has_phone = bool(payload.get("contact_phone"))
    email_done = payload.get("email_done", False)
    whatsapp_done = payload.get("whatsapp_done", False)
    remaining = (has_email and not email_done) or (has_phone and not whatsapp_done)

    if remaining:
        await r.set(f"{PENDING_KEY_PREFIX}{chat_id}", json.dumps(payload), ex=STATE_TTL)
        await query.edit_message_text(
            done_text + "\n\n<b>Reach out via another channel, or tap Done:</b>",
            parse_mode="HTML",
            reply_markup=build_outreach_progress_keyboard(
                temp_id, has_email, email_done, has_phone, whatsapp_done
            ),
        )
    else:
        await query.edit_message_text(done_text, parse_mode="HTML")
        await _finalize_and_cleanup(chat_id, payload, context)


async def handle_send_approval(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.info("Approval: chat_id=%s, app_id=%s", chat_id, app_id)

    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if not pending_data:
        await query.edit_message_text("⚠️ This application has expired. Please start a new one.")
        await _process_queue_next(chat_id, context)
        return

    try:
        payload = json.loads(pending_data)
    except json.JSONDecodeError:
        await query.edit_message_text("⚠️ Error reading application data.")
        await _process_queue_next(chat_id, context)
        return

    # Idempotency: if email was already sent/attempted, don't send again.
    if payload.get("email_done"):
        await _progress_or_finalize(
            chat_id, app_id, payload, context, query, "✅ <b>Email already handled.</b>"
        )
        return

    from src.database.session import async_session_factory
    from src.services.communication.mailer import send_outreach_email
    from src.services.repository.application_repo import update_application_status

    has_email = bool(payload.get("hr_email"))

    async with async_session_factory() as db:
        crm_app_id, crm_pixel_id = await _ensure_crm_record(db, payload)

        if has_email:
            success = await send_outreach_email(
                to_email=payload.get("hr_email"),
                subject=payload.get("email_subject"),
                html_body=payload.get("email_body"),
                pdf_path=payload.get("pdf_path"),
                pixel_id=crm_pixel_id,
            )
            payload["email_done"] = True  # attempted — don't loop
            if success:
                await update_application_status(db, crm_app_id, "applied")
                done_text = (
                    f"🚀 <b>Application Sent!</b>\n\nApp ID: <code>{crm_app_id}</code>\n"
                    "Tracking pixel active."
                )
            else:
                await update_application_status(db, crm_app_id, "failed")
                done_text = f"❌ <b>Email failed to send.</b>\n\nApp ID: <code>{crm_app_id}</code>"
        else:
            payload["email_done"] = True
            done_text = (
                f"✅ <b>Saved to CRM!</b>\n\nApp ID: <code>{crm_app_id}</code>\nNo HR email found."
            )

    await _progress_or_finalize(chat_id, app_id, payload, context, query, done_text)


async def handle_finish(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Finalize a multi-channel outreach: clean up, log status keyboard, advance queue."""
    r = await get_redis()
    pending_data = await r.get(f"{PENDING_KEY_PREFIX}{chat_id}")
    if not pending_data:
        await query.edit_message_text("✅ <b>Done.</b>", parse_mode="HTML")
        await _process_queue_next(chat_id, context)
        return

    payload = json.loads(pending_data)
    await query.edit_message_text("✅ <b>Finished.</b>", parse_mode="HTML")
    await _finalize_and_cleanup(chat_id, payload, context)


async def handle_edit_resume(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    r = await get_redis()
    await r.set(f"{STATE_KEY_PREFIX}{chat_id}", AWAITING_EDIT_INSTRUCTION, ex=STATE_TTL)
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=int(chat_id),
        text="What would you like to change in the resume? Reply below.\n"
             "(e.g. 'Change the Python bullet to say C++')",
    )


async def handle_open_gmail(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Create a Gmail draft pre-filled with the cold email + PDF attachment,
    then send the user a direct link to open and send it from Gmail.
    """
    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if not pending_data:
        await query.edit_message_text("⚠️ This application has expired. Please start a new one.")
        await _process_queue_next(chat_id, context)
        return

    payload = json.loads(pending_data)
    hr_email = payload.get("hr_email", "")
    subject = payload.get("email_subject", "")
    body = payload.get("email_body", "")
    pdf_path = payload.get("pdf_path", "")

    await query.edit_message_text("📧 <b>Creating Gmail draft...</b>", parse_mode="HTML")

    from src.services.communication.mailer import create_gmail_draft
    import uuid as _uuid

    # Reuse the existing pixel_id if possible; fall back to a new one
    # (pixel fires regardless of how the email is actually sent)
    try:
        pixel_id = _uuid.uuid4()
        draft_url = await create_gmail_draft(
            to_email=hr_email,
            subject=subject,
            html_body=body,
            pdf_path=pdf_path,
            pixel_id=pixel_id,
        )
        # Store pixel_id so CRM can record it on "Mark as Sent"
        payload["draft_pixel_id"] = str(pixel_id)
        await r.set(pending_key, json.dumps(payload), ex=STATE_TTL)

        temp_id = str(uuid.uuid4())
        # NOTE: We deliberately do NOT pass a https://mail.google.com draft URL
        # to the keyboard. On mobile, Telegram opens https links in its in-app
        # browser (a stripped Gmail web view that often shows a blank compose),
        # not the Gmail app. The keyboard uses the googlegmail:// scheme which
        # the OS routes straight to the installed Gmail app. The draft is already
        # fully populated (recipient, subject, body, PDF) via the API, so the
        # user just opens the app → Drafts → review → send.
        draft_text = (
            f"✅ <b>Draft saved to your Gmail!</b>\n\n"
            f"📧 <b>To:</b> <code>{html.escape(hr_email)}</code>\n"
            f"📌 <b>Subject:</b> {html.escape(subject)}\n"
            f"📎 Resume PDF attached\n\n"
            "1️⃣ Tap <b>Open Gmail App</b> below\n"
            "2️⃣ Go to your <b>Drafts</b> folder\n"
            "3️⃣ Open this draft, review it, and hit Send\n\n"
            "Then come back and tap <b>Mark as Sent</b> to log it to your CRM."
        )
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=draft_text,
                parse_mode="HTML",
                reply_markup=build_gmail_confirm_keyboard(temp_id),
                disable_web_page_preview=True,
            )
        except BadRequest as be:
            # Some Telegram clients/API versions reject custom-scheme button URLs
            # (googlegmail://) with BUTTON_URL_INVALID. Fall back to a keyboard
            # without the open button so the flow still works — the draft is in
            # Gmail regardless; the user opens the app manually.
            logger.warning("googlegmail:// button rejected by Telegram (%s); sending without it.", str(be))
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            fallback_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Mark as Sent", callback_data=f"gmail_sent_{temp_id}"),
                InlineKeyboardButton("❌ Discard", callback_data=f"discard_{temp_id}"),
            ]])
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=draft_text + "\n\n<i>(Open the Gmail app on your phone and go to Drafts.)</i>",
                parse_mode="HTML",
                reply_markup=fallback_kb,
                disable_web_page_preview=True,
            )

    except RuntimeError as e:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"❌ <b>Gmail draft failed:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


async def handle_gmail_sent(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    User confirmed they sent the email from Gmail.
    Log to CRM as 'applied' and advance the queue.
    """
    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if not pending_data:
        await query.edit_message_text("⚠️ Application data expired. Nothing was logged.")
        await _process_queue_next(chat_id, context)
        return

    payload = json.loads(pending_data)
    draft_pixel_id = payload.pop("draft_pixel_id", None)
    app_data = ApplicationCreate(**{k: v for k, v in payload.items() if k not in ("draft_pixel_id",)})
    app_data.status = "applied"
    # Carry the pixel_id from the draft so the tracking pixel injected into the
    # sent email maps back to this CRM record when HR opens it.
    if draft_pixel_id:
        import uuid as _uuid2
        app_data.pixel_id = _uuid2.UUID(draft_pixel_id)

    from src.database.session import async_session_factory
    from src.services.document.compiler import cleanup_temp_dir
    from src.services.repository.application_repo import create_application

    async with async_session_factory() as db:
        db_app = await create_application(db, app_data)

    await r.delete(pending_key)
    if app_data.pdf_path:
        cleanup_temp_dir(app_data.pdf_path)

    await query.edit_message_text(
        f"✅ <b>Logged to CRM!</b>\n\nApp ID: <code>{db_app.app_id}</code>\n"
        "Tracking pixel active — you'll get an alert when HR opens the email.",
        parse_mode="HTML",
    )

    await context.bot.send_message(
        chat_id=int(chat_id),
        text="📌 <b>Update status when you hear back:</b>",
        parse_mode="HTML",
        reply_markup=build_extended_status_keyboard(str(db_app.app_id)),
    )

    await _process_queue_next(chat_id, context)


async def handle_open_whatsapp(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Prepare a WhatsApp click-to-chat link for the detected recruiter number.
    The user reviews the pre-filled message in WhatsApp and sends it themselves.
    """
    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if not pending_data:
        await query.edit_message_text("⚠️ This application has expired. Please start a new one.")
        await _process_queue_next(chat_id, context)
        return

    payload = json.loads(pending_data)
    phone = payload.get("contact_phone")
    message = payload.get("whatsapp_message") or ""
    pdf_path = payload.get("pdf_path")

    if not phone:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="⚠️ No recruiter phone number is available for this application.",
        )
        return

    from src.services.communication.whatsapp import build_wa_link, draft_whatsapp_message
    from src.controllers.bot.keyboards import build_whatsapp_confirm_keyboard

    # Draft on demand if it wasn't pre-drafted (e.g. older payload)
    if not message:
        message = await draft_whatsapp_message(
            job_title=payload.get("job_title") or "the role",
            company_name=payload.get("company_name") or "your company",
        )
        payload["whatsapp_message"] = message
        await r.set(pending_key, json.dumps(payload), ex=STATE_TTL)

    wa_link = build_wa_link(phone, message)
    temp_id = str(uuid.uuid4())

    text = (
        "📲 <b>WhatsApp message ready!</b>\n\n"
        f"📱 <b>To:</b> <code>+{html.escape(phone)}</code>\n\n"
        f"<b>Message:</b>\n<i>{html.escape(message)}</i>\n\n"
        "Tap <b>Open WhatsApp</b> → the chat opens with this message pre-filled → review and send.\n"
        "📎 <b>Remember to attach your resume PDF</b> (sent above) in the chat.\n\n"
        "Then tap <b>Mark as Sent</b> to log it to your CRM."
    )
    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            parse_mode="HTML",
            reply_markup=build_whatsapp_confirm_keyboard(temp_id, wa_link),
            disable_web_page_preview=True,
        )
    except BadRequest as be:
        logger.warning("wa.me button rejected by Telegram (%s); sending link inline.", str(be))
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=text + f"\n\n🔗 {html.escape(wa_link)}",
            parse_mode="HTML",
            reply_markup=build_whatsapp_confirm_keyboard(temp_id, "https://wa.me/"),
            disable_web_page_preview=True,
        )

    # Re-send the PDF so it's easy to forward into the WhatsApp chat
    if pdf_path and os.path.exists(pdf_path):
        try:
            with open(pdf_path, "rb") as pdf_file:
                await context.bot.send_document(
                    chat_id=int(chat_id),
                    document=pdf_file,
                    filename=os.path.basename(pdf_path),
                    caption="📎 Your resume — forward this into the WhatsApp chat.",
                )
        except Exception as e:
            logger.warning("Failed to re-send PDF for WhatsApp: %s", str(e))


async def handle_whatsapp_sent(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """User confirmed they sent the WhatsApp message. Log to the shared CRM record."""
    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if not pending_data:
        await query.edit_message_text("⚠️ Application data expired. Nothing was logged.")
        await _process_queue_next(chat_id, context)
        return

    payload = json.loads(pending_data)

    # Idempotency: don't re-log WhatsApp if already done.
    if payload.get("whatsapp_done"):
        await _progress_or_finalize(
            chat_id, app_id, payload, context, query, "✅ <b>WhatsApp already logged.</b>"
        )
        return

    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        update_application_status, update_application_notes,
    )

    async with async_session_factory() as db:
        crm_app_id, _pix = await _ensure_crm_record(db, payload)
        await update_application_status(db, crm_app_id, "applied")
        channel_note = f"Outreach via WhatsApp (+{payload.get('contact_phone', '')})."
        await update_application_notes(db, crm_app_id, notes=channel_note)
        payload["whatsapp_done"] = True

    done_text = (
        f"✅ <b>WhatsApp outreach logged!</b>\n\nApp ID: <code>{crm_app_id}</code>\n"
        "Outreach channel: WhatsApp 📲"
    )
    await _progress_or_finalize(chat_id, app_id, payload, context, query, done_text)


async def handle_manual_edit(
    chat_id: str, instruction: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if not pending_data:
        await context.bot.send_message(chat_id=int(chat_id), text="⚠️ No pending application found to edit.")
        await _process_queue_next(chat_id, context)
        return

    payload = json.loads(pending_data)
    pdf_path = payload.get("pdf_path")

    if not pdf_path:
        await context.bot.send_message(chat_id=int(chat_id), text="❌ Cannot find original PDF path.")
        await _process_queue_next(chat_id, context)
        return

    yaml_path = str(Path(pdf_path).parent.parent / "resume.yaml")
    if not os.path.exists(yaml_path):
        await context.bot.send_message(chat_id=int(chat_id), text="❌ Cannot find original YAML to edit.")
        await _process_queue_next(chat_id, context)
        return

    current_yaml = await asyncio.to_thread(
        lambda: Path(yaml_path).read_text(encoding="utf-8")
    )

    status_msg = await context.bot.send_message(
        chat_id=int(chat_id), text="✏️ <b>Applying manual edit...</b>", parse_mode="HTML"
    )

    from src.services.document.editor import apply_manual_edit
    updated_yaml = await apply_manual_edit(current_yaml, instruction)

    if not updated_yaml:
        await status_msg.edit_text("❌ Edit failed. Please try again or approve the current version.")
        await r.set(f"{STATE_KEY_PREFIX}{chat_id}", AWAITING_APPROVAL, ex=STATE_TTL)
        return

    await asyncio.to_thread(
        lambda: Path(yaml_path).write_text(updated_yaml, encoding="utf-8")
    )

    await status_msg.edit_text("⏳ <b>Recompiling PDF...</b>", parse_mode="HTML")
    from src.services.document.compiler import compile_pdf
    try:
        new_pdf_path = await compile_pdf(yaml_path, use_custom_theme=True)
    except Exception as e:
        await status_msg.edit_text(f"❌ PDF compilation failed after edit: {html.escape(str(e)[:150])}")
        await r.set(f"{STATE_KEY_PREFIX}{chat_id}", AWAITING_APPROVAL, ex=STATE_TTL)
        return

    await status_msg.edit_text("✅ <b>Edit applied successfully!</b>", parse_mode="HTML")
    with open(new_pdf_path, "rb") as pdf_file:
        await context.bot.send_document(
            chat_id=int(chat_id),
            document=pdf_file,
            filename="Resume_Edited.pdf",
            caption="Here is your manually edited resume.",
        )

    new_temp_id = str(uuid.uuid4())
    payload["pdf_path"] = new_pdf_path
    await r.set(pending_key, json.dumps(payload), ex=STATE_TTL)
    await r.set(f"{STATE_KEY_PREFIX}{chat_id}", AWAITING_APPROVAL, ex=STATE_TTL)

    await context.bot.send_message(
        chat_id=int(chat_id),
        text="Do you want to send this updated version?",
        reply_markup=build_approval_keyboard(new_temp_id, has_email=bool(payload.get("hr_email"))),
    )


async def handle_discard(
    chat_id: str, app_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.info("Discard: chat_id=%s, app_id=%s", chat_id, app_id)

    r = await get_redis()
    pending_key = f"{PENDING_KEY_PREFIX}{chat_id}"
    pending_data = await r.get(pending_key)

    if pending_data:
        try:
            from src.services.document.compiler import cleanup_temp_dir
            payload = json.loads(pending_data)
            pdf_path = payload.get("pdf_path")
            if pdf_path:
                cleanup_temp_dir(pdf_path)
        except Exception as e:
            logger.error("Failed to clean up discarded temp files: %s", str(e))

    await r.delete(pending_key)
    await query.edit_message_text(
        "🗑️ <b>Discarded.</b>\n\nApplication removed. No CRM record was created.",
        parse_mode="HTML",
    )

    await _process_queue_next(chat_id, context)


# ── Duplicate Handling ───────────────────────────────────────


async def handle_proceed_duplicate(
    chat_id: str, url_hash: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """User confirmed they want to apply again despite a detected duplicate."""
    r = await get_redis()
    raw = await r.get(f"{DUPE_PAYLOAD_PREFIX}{url_hash}")
    if not raw:
        await query.edit_message_text("⚠️ This confirmation has expired. Please paste the URL again.")
        return

    item = json.loads(raw)
    await r.delete(f"{DUPE_PAYLOAD_PREFIX}{url_hash}")
    await query.edit_message_text("✅ Proceeding with duplicate application...")

    await _enqueue_or_dispatch(
        chat_id, item["source_type"], item["payload"], query.message, context, r,
        processing_msg="🔍 Scraping job posting... hang tight.",
    )


# ── Follow-up HITL ───────────────────────────────────────────


async def handle_send_followup(
    chat_id: str, app_id_str: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    import uuid as _uuid
    r = await get_redis()

    from src.services.communication.mailer import send_email, draft_followup_email
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        mark_followup_sent,
        get_application_by_id,
    )

    try:
        app_id = _uuid.UUID(app_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid application ID.")
        return

    raw = await r.get(f"{FOLLOWUP_PENDING_PREFIX}{app_id_str}")
    if raw:
        fu_payload = json.loads(raw)
        hr_email = fu_payload.get("hr_email")
        subject = fu_payload.get("subject", "")
        body = fu_payload.get("body", "")
        pdf_path = fu_payload.get("pdf_path")
    else:
        # Redis payload gone (TTL lapsed or restart) — reconstruct from the
        # durable CRM record instead of failing. The application always exists
        # in MySQL by the time a follow-up reminder fires.
        async with async_session_factory() as db:
            app = await get_application_by_id(db, app_id)
        if not app or not app.hr_email:
            await query.edit_message_text(
                "⚠️ Couldn't find this application to follow up on. It may have been removed."
            )
            return
        await query.edit_message_text("📤 <b>Re-drafting & sending follow-up...</b>", parse_mode="HTML")
        subject, body = await draft_followup_email(
            job_title=app.job_title or "the role",
            company_name=app.company_name or "the company",
            original_subject=app.email_subject or "",
        )
        hr_email = app.hr_email
        pdf_path = app.pdf_path

    if raw:
        await query.edit_message_text("📤 <b>Sending follow-up email...</b>", parse_mode="HTML")

    # Follow-ups don't use tracking pixels (no new pixel_id generated).
    # Send text-only if the original resume PDF no longer exists on disk.
    pdf_ok = bool(pdf_path) and os.path.exists(pdf_path)
    success = False
    if hr_email:
        dummy_pixel = _uuid.uuid4()
        success = await send_email(
            to_email=hr_email,
            subject=subject,
            html_body=body,
            pdf_path=pdf_path if pdf_ok else "",
            pixel_id=dummy_pixel,
            require_pdf=False,
        )

    async with async_session_factory() as db:
        await mark_followup_sent(db, app_id)

    await r.delete(f"{FOLLOWUP_PENDING_PREFIX}{app_id_str}")

    if success:
        note = "" if pdf_ok else "\n<i>(sent without resume — original PDF no longer on disk)</i>"
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"✅ <b>Follow-up sent</b> to <code>{html.escape(hr_email or '')}</code>.{note}",
            parse_mode="HTML",
        )
    else:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="⚠️ Follow-up could not be sent (SMTP error or no HR email). Marked as sent to avoid repeat prompts.",
            parse_mode="HTML",
        )


async def handle_dismiss_followup(chat_id: str, app_id_str: str, query) -> None:
    """Dismiss a follow-up reminder and mark followup_sent_at so it won't appear again."""
    import uuid as _uuid
    r = await get_redis()
    await r.delete(f"{FOLLOWUP_PENDING_PREFIX}{app_id_str}")

    try:
        app_id = _uuid.UUID(app_id_str)
        from src.database.session import async_session_factory
        from src.services.repository.application_repo import mark_followup_sent
        async with async_session_factory() as db:
            await mark_followup_sent(db, app_id)
    except Exception as e:
        logger.warning("Failed to mark followup dismissed for app_id=%s: %s", app_id_str, str(e))

    await query.edit_message_text("🚫 <b>Follow-up dismissed.</b> Won't remind you again for this application.", parse_mode="HTML")


# ── History Pagination ───────────────────────────────────────

PAGE_SIZE = 5

async def handle_history_page(chat_id: str, page: int, query) -> None:
    """Edit the history message in-place to show a different page."""
    import html as _html
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import get_applications_page
    from src.controllers.bot.keyboards import build_history_pagination_keyboard

    STATUS_EMOJI = {
        "applied": "📨", "viewed": "👀", "interview": "🎯",
        "offer_received": "💰", "offer_accepted": "✅",
        "ghosted": "👻", "rejected": "❌", "pending": "⏳", "failed": "⚠️",
    }

    async with async_session_factory() as db:
        apps, total = await get_applications_page(db, page=page, page_size=PAGE_SIZE)

    import math
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    start = page * PAGE_SIZE + 1

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

    if not apps:
        lines.append("No applications on this page.")

    lines.append(f"\n💡 <i>Total: {total} application(s). Use /export for CSV.</i>")

    markup = build_history_pagination_keyboard(page, total_pages)
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=markup,
    )


# ── Job Discovery Handlers ───────────────────────────────────


async def handle_discovery_apply(
    chat_id: str, lead_id: str, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Apply to a discovered job: feed its URL into the normal pipeline."""
    from src.services.ai.discovery import LEAD_PREFIX
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import check_duplicate_by_url

    r = await get_redis()
    url = await r.get(f"{LEAD_PREFIX}{lead_id}")
    if not url:
        await query.edit_message_text("⚠️ This job lead has expired. It may have been removed from the board.")
        return

    await query.edit_message_reply_markup(reply_markup=None)

    # Guard: already applied since it was surfaced
    async with async_session_factory() as db:
        if await check_duplicate_by_url(db, url):
            await query.edit_message_text("✅ You've already applied to this role.")
            return

    state_key = f"{STATE_KEY_PREFIX}{chat_id}"
    queue_key = f"{QUEUE_KEY_PREFIX}{chat_id}"
    current_state = await r.get(state_key)

    if current_state == PROCESSING_PIPELINE:
        pos = await r.rpush(queue_key, json.dumps({"source_type": "link", "payload": url}))
        await r.expire(queue_key, QUEUE_TTL)
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"⏳ <b>Pipeline busy.</b> Queued this job at position #{pos}.",
            parse_mode="HTML",
        )
    else:
        await r.set(state_key, PROCESSING_PIPELINE, ex=3600)
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="🚀 <b>Starting application from your discovered job...</b>",
            parse_mode="HTML",
        )
        _spawn_pipeline(context, chat_id, "link", url)


async def handle_discovery_dismiss(chat_id: str, lead_id: str, query) -> None:
    """Dismiss a discovered job. It's already marked seen, so it won't resurface."""
    from src.services.ai.discovery import LEAD_PREFIX
    r = await get_redis()
    await r.delete(f"{LEAD_PREFIX}{lead_id}")
    await query.edit_message_text("🚫 Dismissed. I won't surface this role again.")
