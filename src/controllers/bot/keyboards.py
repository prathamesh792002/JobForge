"""
Telegram Inline Keyboard Builders

Factory functions for all InlineKeyboardMarkup instances used by the bot.
Each button's callback_data encodes the action and the application ID.
"""

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def build_approval_keyboard(
    app_id: str, has_email: bool = True, has_phone: bool = False
) -> InlineKeyboardMarkup:
    """
    HITL keyboard presented after resume compilation.

    Buttons (when HR email found):
        🚀 Approve & Send Email   → send_{app_id}
        ✏️ Edit Email             → edit_email_{app_id}
        📝 Edit Resume            → edit_{app_id}
        ❌ Discard                → discard_{app_id}

    Buttons (no HR email):
        💾 Approve & Save to CRM  → send_{app_id}
        📝 Edit Resume            → edit_{app_id}
        ❌ Discard                → discard_{app_id}
    """
    approve_label = "🚀 Approve & Send Email" if has_email else "💾 Approve & Save to CRM"
    rows = [[InlineKeyboardButton(approve_label, callback_data=f"send_{app_id}")]]

    if has_email:
        rows.append([InlineKeyboardButton("📧 Draft in Gmail", callback_data=f"open_gmail_{app_id}")])

    if has_phone:
        rows.append([InlineKeyboardButton("📲 Message on WhatsApp", callback_data=f"open_whatsapp_{app_id}")])

    rows.append([InlineKeyboardButton("📝 Edit Resume", callback_data=f"edit_{app_id}")])
    rows.append([InlineKeyboardButton("❌ Discard", callback_data=f"discard_{app_id}")])

    logger.debug(
        "Built approval keyboard for app_id=%s, has_email=%s, has_phone=%s",
        app_id, has_email, has_phone,
    )
    return InlineKeyboardMarkup(rows)


def build_whatsapp_confirm_keyboard(app_id: str, wa_link: str) -> InlineKeyboardMarkup:
    """
    Shown after a WhatsApp draft is prepared. The 'Open WhatsApp' button is a
    wa.me click-to-chat link (pre-filled message); the user reviews and sends it
    themselves, then taps 'Mark as Sent' to log it to the CRM.
    """
    keyboard = [
        [InlineKeyboardButton("📲 Open WhatsApp", url=wa_link)],
        [
            InlineKeyboardButton("✅ Mark as Sent", callback_data=f"whatsapp_sent_{app_id}"),
            InlineKeyboardButton("✅ Done", callback_data=f"finish_{app_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_outreach_progress_keyboard(
    app_id: str,
    has_email: bool,
    email_done: bool,
    has_phone: bool,
    whatsapp_done: bool,
) -> InlineKeyboardMarkup:
    """
    Shown after one outreach channel is used, offering any remaining channel(s)
    plus 'Done'. Lets the user reach a recruiter by BOTH email and WhatsApp for
    the same application without re-posting the job.
    """
    rows = []
    if has_email and not email_done:
        rows.append([InlineKeyboardButton("🚀 Send Email", callback_data=f"send_{app_id}")])
    if has_phone and not whatsapp_done:
        rows.append([InlineKeyboardButton("📲 Message on WhatsApp", callback_data=f"open_whatsapp_{app_id}")])
    rows.append([InlineKeyboardButton("✅ Done", callback_data=f"finish_{app_id}")])
    return InlineKeyboardMarkup(rows)


def build_extended_status_keyboard(app_id: str) -> InlineKeyboardMarkup:
    """
    Full status update keyboard shown after an application is saved to CRM.

    Statuses: interview, offer_received, offer_accepted, ghosted, rejected
    """
    keyboard = [
        [
            InlineKeyboardButton("🎯 Interview", callback_data=f"status_interview_{app_id}"),
            InlineKeyboardButton("💰 Offer Received", callback_data=f"status_offer_{app_id}"),
        ],
        [
            InlineKeyboardButton("✅ Offer Accepted", callback_data=f"status_accepted_{app_id}"),
            InlineKeyboardButton("👻 Ghosted", callback_data=f"status_ghosted_{app_id}"),
        ],
        [
            InlineKeyboardButton("❌ Rejected", callback_data=f"status_rejected_{app_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_status_update_keyboard(app_id: str) -> InlineKeyboardMarkup:
    """Legacy 2-button keyboard (interview / rejected). Kept for tracker.py compat."""
    keyboard = [
        [
            InlineKeyboardButton("🎯 Interview", callback_data=f"status_interview_{app_id}"),
            InlineKeyboardButton("❌ Rejected", callback_data=f"status_rejected_{app_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_duplicate_warning_keyboard(url_hash: str) -> InlineKeyboardMarkup:
    """Shown when a URL is recognised as a duplicate application."""
    keyboard = [
        [
            InlineKeyboardButton("⚠️ Apply Anyway", callback_data=f"proceed_dupe_{url_hash}"),
            InlineKeyboardButton("❌ Abort", callback_data=f"abort_dupe_{url_hash}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_history_pagination_keyboard(
    page: int, total_pages: int
) -> Optional[InlineKeyboardMarkup]:
    """Prev / Next navigation for /history pages. Returns None when only one page."""
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"history_page_{page - 1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"history_page_{page + 1}"))

    if not row:
        return None
    return InlineKeyboardMarkup([row])


def build_gmail_confirm_keyboard(
    app_id: str,
    draft_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """
    Shown after a Gmail draft is created.

    Row 1 (when draft_url is provided): 'Open Draft' deep-links directly to
    the newly created draft on both desktop (HTTPS) and iOS/Android (the Gmail
    app intercepts mail.google.com URLs and opens the draft in-app).
    Row 1 (fallback): generic 'Open Gmail App' via the googlegmail:// scheme.
    Row 2: 'Mark as Sent' + 'Discard' action buttons.
    """
    open_url = draft_url if draft_url else "googlegmail://"
    open_label = "📩 Open Draft in Gmail" if draft_url else "📱 Open Gmail App"
    keyboard = [
        [InlineKeyboardButton(open_label, url=open_url)],
        [
            InlineKeyboardButton("✅ Mark as Sent", callback_data=f"gmail_sent_{app_id}"),
            InlineKeyboardButton("❌ Discard", callback_data=f"discard_{app_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_followup_keyboard(app_id: str) -> InlineKeyboardMarkup:
    """Shown when the daily checker finds an application needing a follow-up."""
    keyboard = [
        [
            InlineKeyboardButton("📤 Send Follow-up", callback_data=f"send_followup_{app_id}"),
            InlineKeyboardButton("🚫 Dismiss", callback_data=f"dismiss_followup_{app_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_discovery_keyboard(lead_id: str) -> InlineKeyboardMarkup:
    """
    Shown when the discovery scanner surfaces a high-fit job.
    'Apply Now' feeds the job URL into the normal application pipeline.
    """
    keyboard = [
        [
            InlineKeyboardButton("🚀 Apply Now", callback_data=f"discover_apply_{lead_id}"),
            InlineKeyboardButton("🚫 Dismiss", callback_data=f"discover_dismiss_{lead_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
