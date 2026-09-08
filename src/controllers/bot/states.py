"""
Telegram Bot Conversation State Constants

Redis-backed FSM state constants for the bot's conversation flow.
"""

# ── Conversation States ──────────────────────────────────────
IDLE = "IDLE"
AWAITING_JOB_DATA = "AWAITING_JOB_DATA"
PROCESSING_PIPELINE = "PROCESSING_PIPELINE"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
AWAITING_EDIT_INSTRUCTION = "AWAITING_EDIT_INSTRUCTION"
AWAITING_EMAIL_EDIT = "AWAITING_EMAIL_EDIT"

# ── Redis Key Prefixes ───────────────────────────────────────
STATE_KEY_PREFIX = "bot:state:"           # bot:state:{chat_id} → state string
PENDING_KEY_PREFIX = "app:pending:"       # app:pending:{chat_id} → JSON payload
JD_CACHE_PREFIX = "app:jd_cache:"         # app:jd_cache:{hash} → JSON payload
QUEUE_KEY_PREFIX = "bot:queue:"           # bot:queue:{chat_id} → Redis list of queued jobs
CHECKPOINT_KEY_PREFIX = "pipeline:chk:"  # pipeline:chk:{chat_id} → current stage name
FOLLOWUP_PENDING_PREFIX = "followup:pnd:" # followup:pnd:{app_id} → follow-up payload JSON
DUPE_PAYLOAD_PREFIX = "dupe:payload:"     # dupe:payload:{url_hash} → original job payload

# ── TTL Values (seconds) ─────────────────────────────────────
STATE_TTL = 86400       # 24 hours — HITL window for state + pending approval payloads.
                        # The pending "Send Email" payload lives ONLY in Redis until
                        # the user taps a button, so this is how long they have to act
                        # before the button dies with "application has expired".
PENDING_TTL = 86400     # 24 hours for pending approval payloads (kept in sync with STATE_TTL)
JD_CACHE_TTL = 259200   # 72 hours for JD skills cache
QUEUE_TTL = 7200        # 2 hours for queued jobs
CHECKPOINT_TTL = 3600   # 1 hour for pipeline checkpoints
DUPE_TTL = 600          # 10 minutes for duplicate-warning payloads
FOLLOWUP_TTL = 86400    # 24 hours for follow-up HITL payloads
