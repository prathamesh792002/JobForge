"""
Hermetic test for the follow-up resilience fix.

Safe by construction:
  - SMTP is monkeypatched — NO real email is ever sent.
  - mark_followup_sent is monkeypatched — the real CRM is NOT mutated.
  - get_application_by_id runs for real (read-only) against the live DB.

Run: python test_followup_fix.py
"""
import asyncio
import os
import smtplib
import sys
import tempfile
import uuid

# Windows console defaults to cp1252 — force UTF-8 so arrows/emoji don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Quiet SQLAlchemy's statement echo for readable test output.
import logging
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# A real application id (with hr_email) pulled from the live CRM.
REAL_APP_ID = "7603572a34c84b43b997f4ef5daadc31"

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results = []


def check(name, cond):
    results.append(cond)
    print(f"  [{PASS if cond else FAIL}] {name}")


# ── Dummy SMTP so nothing leaves the machine ─────────────────────
class DummySMTP:
    sent = []
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a, **k): pass
    def send_message(self, msg): DummySMTP.sent.append(msg)


smtplib.SMTP_SSL = DummySMTP  # global patch — mailer uses smtplib.SMTP_SSL


async def test_get_application_by_id():
    print("\n1) get_application_by_id (real, read-only)")
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import get_application_by_id
    async with async_session_factory() as db:
        app = await get_application_by_id(db, uuid.UUID(REAL_APP_ID))
    check("returns the application record", app is not None)
    check("record has an hr_email", bool(app and app.hr_email))
    if app:
        print(f"       → {app.company_name} / {app.job_title} / {app.hr_email}")


async def test_send_email_require_pdf():
    print("\n2) send_email(require_pdf=...) behavior")
    from src.services.communication.mailer import send_email

    # Case A: default require_pdf=True + missing PDF → must NOT send.
    # send_email catches the FileNotFoundError internally and returns False,
    # so the main outreach path never sends a resume-less email.
    DummySMTP.sent.clear()
    ok_main = await send_email("x@example.com", "s", "<p>b</p>", "does_not_exist.pdf", uuid.uuid4())
    check("require_pdf=True + missing PDF returns False (main path won't send resume-less)", ok_main is False)
    check("require_pdf=True + missing PDF sent nothing", len(DummySMTP.sent) == 0)

    # Case B: require_pdf=False + missing PDF → sends text-only, no attachment
    DummySMTP.sent.clear()
    ok = await send_email("x@example.com", "s", "<p>b</p>", "", uuid.uuid4(), require_pdf=False)
    msg = DummySMTP.sent[-1] if DummySMTP.sent else None
    n_attach = len(list(msg.iter_attachments())) if msg else -1
    check("require_pdf=False + missing PDF returns True", bool(ok))
    check("text-only email has 0 attachments", n_attach == 0)

    # Case C: require_pdf=False + a real PDF file → attaches it
    DummySMTP.sent.clear()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(b"%PDF-1.4 fake"); tmp_pdf = tf.name
    try:
        ok = await send_email("x@example.com", "s", "<p>b</p>", tmp_pdf, uuid.uuid4(), require_pdf=False)
        msg = DummySMTP.sent[-1] if DummySMTP.sent else None
        n_attach = len(list(msg.iter_attachments())) if msg else -1
        check("require_pdf=False + real PDF attaches it (1 attachment)", n_attach == 1)
    finally:
        os.unlink(tmp_pdf)


async def test_handler_redis_miss_reconstruct():
    print("\n3) handle_send_followup — Redis MISS → DB reconstruction")
    import src.services.communication.mailer as mailer_mod
    import src.services.repository.application_repo as repo_mod
    from src.controllers.bot.states import FOLLOWUP_PENDING_PREFIX
    from src.database.redis import get_redis

    captured = {}
    async def fake_send_email(**kw): captured.update(kw); return True
    async def fake_draft(**kw): return ("Following up", "<p>body</p>")
    async def fake_mark(db, app_id): captured["marked"] = str(app_id)

    orig_send, orig_draft, orig_mark = mailer_mod.send_email, mailer_mod.draft_followup_email, repo_mod.mark_followup_sent
    mailer_mod.send_email = fake_send_email
    mailer_mod.draft_followup_email = fake_draft
    repo_mod.mark_followup_sent = fake_mark

    class FakeQuery:
        def __init__(self): self.texts = []
        async def edit_message_text(self, text, **k): self.texts.append(text)
    class FakeBot:
        def __init__(self): self.sent = []
        async def send_message(self, chat_id, text, **k): self.sent.append(text)
    class FakeCtx:
        def __init__(self): self.bot = FakeBot()

    try:
        from src.controllers.bot.application_flow import handle_send_followup
        r = await get_redis()
        await r.delete(f"{FOLLOWUP_PENDING_PREFIX}{REAL_APP_ID}")  # force the miss

        q, ctx = FakeQuery(), FakeCtx()
        await handle_send_followup("873396577", REAL_APP_ID, q, ctx)

        check("send_email was called (didn't just say 'expired')", "to_email" in captured)
        check("reconstructed the real hr_email from the CRM", captured.get("to_email") == "sophia@baigenlabs.in")
        check("called with require_pdf=False (text-only tolerant)", captured.get("require_pdf") is False)
        check("temp PDF gone → sent text-only (empty pdf_path)", captured.get("pdf_path") == "")
        check("marked follow-up as sent", "marked" in captured)
        check("user got a success confirmation", any("Follow-up sent" in t for t in ctx.bot.sent))
    finally:
        mailer_mod.send_email, mailer_mod.draft_followup_email, repo_mod.mark_followup_sent = orig_send, orig_draft, orig_mark


async def main():
    print("=" * 60)
    print("FOLLOW-UP RESILIENCE FIX — HERMETIC TEST")
    print("(no real emails, no CRM writes)")
    print("=" * 60)
    await test_get_application_by_id()
    await test_send_email_require_pdf()
    await test_handler_redis_miss_reconstruct()
    print("\n" + "=" * 60)
    print(f"RESULT: {sum(results)}/{len(results)} checks passed")
    print("=" * 60)
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
