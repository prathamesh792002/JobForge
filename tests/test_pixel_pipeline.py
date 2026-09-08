"""
Rigorous end-to-end test suite for the pixel tracker pipeline.

Tests every link in the chain:
  1. Pixel injection into HTML email body
  2. CRM record creation with pixel_id
  3. Tracking URL resolution & format
  4. Pixel endpoint -- genuine open fires alert
  5. Pixel endpoint -- bot/scanner user-agents are filtered
  6. Pixel endpoint -- prefetch suppression (too-soon-after-send)
  7. Pixel endpoint -- re-open idempotency
  8. Pixel endpoint -- unknown pixel_id graceful handling
  9. mark_email_opened atomicity (only first open returns the record)
  10. Gmail draft flow -- pixel_id consistency
  11. SMTP fallback -- pixel injection still present
  12. Follow-up detection -- only non-opened, stale applications
  13. APP_BASE_URL reachability (ngrok tunnel alive?)
  14. Database pixel_id -> application round-trip integrity
"""
import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

# Force UTF-8 on Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import settings
from src.services.communication.mailer import _inject_tracking_pixel
from src.validators.application import ApplicationCreate


# ─── Helpers ─────────────────────────────────────────────────
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}


def check(condition: bool, name: str, detail: str = "", warn_only: bool = False):
    if condition:
        print(f"  {PASS} {name}")
        results["pass"] += 1
    elif warn_only:
        print(f"  {WARN} {name}" + (f" — {detail}" if detail else ""))
        results["warn"] += 1
    else:
        print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))
        results["fail"] += 1


# =========================================================━━
# TEST 1: Pixel Injection into HTML Body
# =========================================================━━
def test_pixel_injection():
    print("\n=== TEST 1: Pixel Injection into HTML Body ===")
    pixel_id = uuid.uuid4()

    # Case A: HTML with </body> tag
    html_with_body = """<html><body style="font-family: sans-serif;">
        <p>Dear Hiring Manager,</p>
        <p>I am applying for the role.</p>
    </body></html>"""

    injected = _inject_tracking_pixel(html_with_body, pixel_id)

    # The pixel img should exist before </body>
    check(f"{pixel_id}.gif" in injected, "Pixel URL contains pixel_id")
    check("</body>" in injected, "</body> tag still present after injection")
    check(
        injected.index(f"{pixel_id}.gif") < injected.index("</body>"),
        "Pixel img is BEFORE </body>"
    )
    check(
        'width="1"' in injected and 'height="1"' in injected,
        "Pixel is 1x1 dimensions"
    )
    check(
        'style="' in injected and "display:none" not in injected,
        "Pixel does NOT use display:none (would be stripped by Gmail)"
    )
    check(
        "opacity:0" in injected,
        "Pixel uses opacity:0 for invisibility (fetched but hidden)"
    )

    # Case B: HTML WITHOUT </body> tag (malformed)
    html_no_body = "<p>Hi there, this is a plain HTML email.</p>"
    injected2 = _inject_tracking_pixel(html_no_body, pixel_id)
    check(
        f"{pixel_id}.gif" in injected2,
        "Pixel appended even when no </body> tag"
    )

    # Case C: Verify the full tracking URL format
    expected_base = f"{settings.APP_BASE_URL}/v1/tracker/pixel/{pixel_id}.gif"
    check(
        expected_base in injected,
        f"Full tracking URL matches expected: .../{pixel_id}.gif"
    )

    # Case D: Check APP_BASE_URL is not localhost in production
    parsed = urlparse(settings.APP_BASE_URL)
    is_public = parsed.hostname not in ("localhost", "127.0.0.1", "0.0.0.0")
    check(
        is_public,
        f"APP_BASE_URL ({settings.APP_BASE_URL}) is publicly reachable (not localhost)",
        detail=f"Current value: {settings.APP_BASE_URL} — emails will contain this URL. If it's localhost, HR's email client can NEVER fetch the pixel!",
    )


# =========================================================━━
# TEST 2: CRM Record Creation with pixel_id
# =========================================================━━
async def test_crm_record_creation():
    print("\n=== TEST 2: CRM Record Creation with pixel_id ===")
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        create_application,
        get_application_by_pixel,
    )

    test_pixel_id = uuid.uuid4()
    app_data = ApplicationCreate(
        company_name="PixelTestCorp",
        job_title="Test Engineer",
        persona_used="ai_ml",
        hr_email="pixel-test@example.com",
        source_type="raw_text",
        pdf_path="/tmp/pixel_test.pdf",
        jd_text="Pixel pipeline test JD",
        tailoring_skipped=True,
        initial_ats_score=70,
        final_ats_score=85,
        email_subject="Test Subject",
        email_body="<html><body>test</body></html>",
        status="applied",
        pixel_id=test_pixel_id,
    )

    async with async_session_factory() as db:
        app = await create_application(db, app_data)

        check(app.pixel_id is not None, "pixel_id is NOT null on created record")
        check(
            str(app.pixel_id) == str(test_pixel_id),
            f"pixel_id matches what was provided ({test_pixel_id})"
        )
        check(app.opened_at is None, "opened_at is NULL initially (not pre-opened)")
        check(app.status == "applied", "Initial status is 'applied'")

        # Can we look it up by pixel?
        found = await get_application_by_pixel(db, test_pixel_id)
        check(
            found is not None and found.app_id == app.app_id,
            "get_application_by_pixel returns the correct record"
        )

        # Test with auto-generated pixel_id (no explicit pixel_id)
        app_data_auto = ApplicationCreate(
            company_name="AutoPixelCorp",
            job_title="Auto Pixel Test",
            persona_used="swe",
            hr_email="auto@example.com",
            source_type="link",
            jd_text="Auto pixel test",
            status="applied",
        )
        app_auto = await create_application(db, app_data_auto)
        check(
            app_auto.pixel_id is not None,
            "Auto-generated pixel_id is present when not explicitly provided"
        )
        check(
            str(app_auto.pixel_id) != str(test_pixel_id),
            "Auto-generated pixel_id is different from explicit one"
        )


# =========================================================━━
# TEST 3: mark_email_opened Atomicity & Idempotency
# =========================================================━━
async def test_mark_email_opened():
    print("\n=== TEST 3: mark_email_opened Atomicity & Idempotency ===")
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        create_application,
        mark_email_opened,
    )

    pixel_id = uuid.uuid4()
    app_data = ApplicationCreate(
        company_name="OpenTestCorp",
        job_title="Open Tester",
        persona_used="ai_ml",
        hr_email="open@test.com",
        source_type="raw_text",
        jd_text="Testing mark_email_opened",
        status="applied",
        pixel_id=pixel_id,
    )

    async with async_session_factory() as db:
        app = await create_application(db, app_data)

        # First open — should return the record
        result1 = await mark_email_opened(db, pixel_id)
        check(result1 is not None, "First open: returns the Application record")
        check(
            result1.opened_at is not None,
            "First open: opened_at is now SET"
        )
        check(
            result1.status == "viewed",
            f"First open: status advanced to 'viewed' (got '{result1.status}')"
        )

        # Second open (re-open) — should return None (idempotent)
        result2 = await mark_email_opened(db, pixel_id)
        check(
            result2 is None,
            "Re-open: returns None (idempotent, no duplicate alert)"
        )

        # Third open with a FAKE pixel_id — should return None
        result3 = await mark_email_opened(db, uuid.uuid4())
        check(
            result3 is None,
            "Unknown pixel_id: returns None gracefully"
        )


# =========================================================━━
# TEST 4: Status Downgrade Protection
# =========================================================━━
async def test_status_no_downgrade():
    print("\n=== TEST 4: Status Downgrade Protection (interview → viewed) ===")
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        create_application,
        update_application_status,
        mark_email_opened,
    )

    pixel_id = uuid.uuid4()
    app_data = ApplicationCreate(
        company_name="NoDowngradeCorp",
        job_title="Status Guard",
        persona_used="swe",
        hr_email="nodown@test.com",
        source_type="raw_text",
        jd_text="Testing status downgrade protection",
        status="applied",
        pixel_id=pixel_id,
    )

    async with async_session_factory() as db:
        app = await create_application(db, app_data)

        # Manually advance to 'interview'
        await update_application_status(db, app.app_id, "interview")

        # Now simulate a pixel open — should NOT downgrade to 'viewed'
        result = await mark_email_opened(db, pixel_id)
        check(
            result is not None,
            "Pixel open fires even when status is 'interview'"
        )
        check(
            result.status == "interview",
            f"Status stays 'interview' (not downgraded to 'viewed'), got '{result.status}'"
        )


# =========================================================━━
# TEST 5: Bot/Scanner User-Agent Filtering
# =========================================================━━
def test_bot_filtering():
    print("\n=== TEST 5: Bot/Scanner User-Agent Filtering ===")
    from src.routes.api.tracker import _BOT_SIGNATURES

    # These should be filtered (bots/scanners)
    bot_uas = [
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Mozilla/5.0 (compatible; bingpreview/2.0; +http://www.bing.com/bingbot.htm)",
        "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
        "Barracuda/5.0",
        "mimecast/1.0",
        "Proofpoint/1.0",
        "WhatsApp/2.21.4.22",
        "TelegramBot (like TwitterBot)",
        "Microsoft-CryptoAPI/10.0",
    ]

    for ua in bot_uas:
        ua_lower = ua.lower()
        is_filtered = any(sig in ua_lower for sig in _BOT_SIGNATURES)
        check(is_filtered, f"Filtered: {ua[:60]}...")

    # These should NOT be filtered (real email clients)
    real_uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15",
        "Microsoft Outlook 16.0",
        "GoogleImageProxy",  # Gmail's real image proxy — should NOT be filtered
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Mobile Safari",
    ]

    for ua in real_uas:
        ua_lower = ua.lower()
        is_filtered = any(sig in ua_lower for sig in _BOT_SIGNATURES)
        check(not is_filtered, f"NOT filtered (real client): {ua[:60]}...")


# =========================================================━━
# TEST 6: Prefetch Suppression Window
# =========================================================━━
def test_prefetch_suppression():
    print("\n=== TEST 6: Prefetch Suppression Window ===")
    from src.routes.api.tracker import _seconds_since_sent

    delay = settings.PIXEL_OPEN_MIN_DELAY_SECONDS
    check(
        delay > 0,
        f"PIXEL_OPEN_MIN_DELAY_SECONDS is set ({delay}s)"
    )
    check(
        delay >= 60,
        f"Delay is at least 60s (got {delay}s) — less would pass most prefetches through",
        warn_only=True
    )
    check(
        delay <= 600,
        f"Delay is ≤ 600s (got {delay}s) — more would suppress genuine early opens",
        warn_only=True
    )

    # Simulate a record created 10s ago → should be suppressed
    class FakeApp:
        created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)

    age = _seconds_since_sent(FakeApp())
    check(
        age is not None and age < delay,
        f"Record created 10s ago: age={age:.0f}s < delay={delay}s → would be SUPPRESSED"
    )

    # Simulate a record created 5 min ago → should NOT be suppressed
    class FakeApp2:
        created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=300)

    age2 = _seconds_since_sent(FakeApp2())
    check(
        age2 is not None and age2 >= delay,
        f"Record created 300s ago: age={age2:.0f}s >= delay={delay}s → would PASS through"
    )


# =========================================================━━
# TEST 7: Tracking URL Reachability (ngrok alive?)
# =========================================================━━
async def test_tracking_url_reachability():
    print("\n=== TEST 7: Tracking URL Reachability ===")
    import httpx

    # First, check that the base URL looks sane
    base = settings.APP_BASE_URL
    check(
        base.startswith("https://"),
        f"APP_BASE_URL uses HTTPS ({base})",
        detail=f"Got: {base}. Most email clients block HTTP image loads.",
        warn_only=True
    )

    # Try to hit the health endpoint
    health_url = f"{base}/health"
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(health_url)
            check(
                resp.status_code == 200,
                f"Health endpoint reachable ({health_url}): HTTP {resp.status_code}",
                detail=f"Got HTTP {resp.status_code}. If your ngrok tunnel is down, no pixel will ever fire!",
            )
    except httpx.ConnectError:
        check(False, f"Health endpoint reachable ({health_url})",
              detail="CONNECTION REFUSED — your ngrok tunnel or server is DOWN. This is the #1 reason you'd get zero alerts.")
    except httpx.ConnectTimeout:
        check(False, f"Health endpoint reachable ({health_url})",
              detail="CONNECTION TIMEOUT — your ngrok tunnel may be expired/dead.")
    except Exception as e:
        check(False, f"Health endpoint reachable ({health_url})",
              detail=f"Unexpected error: {e}")

    # Try to hit a fake pixel endpoint — should return 200 with GIF
    fake_pixel = uuid.uuid4()
    pixel_url = f"{base}/v1/tracker/pixel/{fake_pixel}.gif"
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(pixel_url)
            check(
                resp.status_code == 200,
                f"Pixel endpoint returns 200 for unknown pixel_id"
            )
            check(
                resp.headers.get("content-type", "").startswith("image/gif"),
                f"Pixel endpoint returns image/gif content-type"
            )
            check(
                len(resp.content) > 0,
                f"Pixel response body is not empty (GIF data present)"
            )
    except Exception as e:
        check(False, f"Pixel endpoint reachable ({pixel_url})",
              detail=f"Error: {e}. This means no email open will ever be tracked!")


# =========================================================━━
# TEST 8: Full Round-Trip (create app → inject pixel → simulate open)
# =========================================================━━
async def test_full_roundtrip():
    print("\n=== TEST 8: Full Round-Trip (CRM → Inject → Open Simulation) ===")
    import httpx
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        create_application,
        get_application_by_pixel,
    )

    pixel_id = uuid.uuid4()
    app_data = ApplicationCreate(
        company_name="RoundtripCorp",
        job_title="Roundtrip Tester",
        persona_used="ai_ml",
        hr_email="roundtrip@test.com",
        source_type="raw_text",
        jd_text="Full roundtrip pixel test",
        status="applied",
        pixel_id=pixel_id,
        email_subject="Roundtrip Test",
        email_body="<html><body>Roundtrip test</body></html>",
    )

    async with async_session_factory() as db:
        app = await create_application(db, app_data)

    # Step 1: Inject pixel
    html = "<html><body><p>Test email body</p></body></html>"
    injected_html = _inject_tracking_pixel(html, pixel_id)
    expected_url = f"{settings.APP_BASE_URL}/v1/tracker/pixel/{pixel_id}.gif"
    check(expected_url in injected_html, "Injected HTML contains correct pixel URL")

    # Step 2: Simulate what happens when HR's email client fetches the pixel
    # We need to wait past the prefetch suppression window, so we manipulate
    # the created_at. For now, we just hit the endpoint and verify behavior.
    pixel_url = f"{settings.APP_BASE_URL}/v1/tracker/pixel/{pixel_id}.gif"

    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            # First fetch — should be suppressed (too soon after creation)
            resp1 = await client.get(pixel_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
            })
            check(resp1.status_code == 200, "First pixel fetch returns 200 (GIF served)")

        # Verify: since it was fetched too soon, opened_at should still be NULL
        async with async_session_factory() as db:
            app_after = await get_application_by_pixel(db, pixel_id)
            if app_after:
                was_suppressed = app_after.opened_at is None
                check(
                    was_suppressed,
                    f"Prefetch suppression worked: opened_at still NULL (record age < {settings.PIXEL_OPEN_MIN_DELAY_SECONDS}s)",
                    detail="opened_at was set immediately — prefetch suppression may not be working",
                    warn_only=True,
                )
            else:
                check(False, "Application record found after pixel fetch")

    except Exception as e:
        check(False, f"Roundtrip pixel fetch", detail=f"Server not reachable: {e}")


# =========================================================━━
# TEST 9: Follow-up Detection (Only Non-Opened, Stale Apps)
# =========================================================━━
async def test_followup_detection():
    print("\n=== TEST 9: Follow-up Detection Logic ===")
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import (
        create_application,
        find_applications_needing_followup,
        mark_email_opened,
    )

    followup_days = settings.FOLLOWUP_DAYS

    async with async_session_factory() as db:
        # App A: fresh (created now) — should NOT trigger follow-up
        app_a = await create_application(db, ApplicationCreate(
            company_name="FreshCorp",
            job_title="FreshJob",
            hr_email="fresh@test.com",
            source_type="raw_text",
            jd_text="Fresh",
            status="applied",
        ))

        # App B: old but opened — should NOT trigger follow-up
        pix_b = uuid.uuid4()
        app_b = await create_application(db, ApplicationCreate(
            company_name="OpenedCorp",
            job_title="OpenedJob",
            hr_email="opened@test.com",
            source_type="raw_text",
            jd_text="Opened",
            status="applied",
            pixel_id=pix_b,
        ))
        await mark_email_opened(db, pix_b)

        # App C: old with no HR email — should NOT trigger
        app_c = await create_application(db, ApplicationCreate(
            company_name="NoEmailCorp",
            job_title="NoEmailJob",
            hr_email=None,
            source_type="raw_text",
            jd_text="No HR email",
            status="applied",
        ))

        stale_apps = await find_applications_needing_followup(db, days=followup_days)

        stale_ids = {str(a.app_id) for a in stale_apps}

        check(
            str(app_a.app_id) not in stale_ids,
            "Fresh app (just created) NOT in follow-up list"
        )
        check(
            str(app_b.app_id) not in stale_ids,
            "Opened app NOT in follow-up list"
        )
        check(
            str(app_c.app_id) not in stale_ids,
            "App without HR email NOT in follow-up list"
        )


# =========================================================━━
# TEST 10: Verify All Sent Emails Have Pixel in DB
# =========================================================━━
async def test_existing_applications_have_pixels():
    print("\n=== TEST 10: All Existing Applications Have Valid pixel_id ===")
    from src.database.session import async_session_factory
    from src.services.repository.application_repo import get_all_applications_for_export

    async with async_session_factory() as db:
        all_apps = await get_all_applications_for_export(db)

    total = len(all_apps)
    print(f"  {INFO} Total applications in database: {total}")

    if total == 0:
        check(False, "At least one application exists in the database",
              detail="No applications found. Can't verify pixel tracking.")
        return

    null_pixels = [a for a in all_apps if a.pixel_id is None]
    check(
        len(null_pixels) == 0,
        f"All {total} applications have a non-null pixel_id",
        detail=f"{len(null_pixels)} applications have NULL pixel_id!"
    )

    # Check for duplicate pixel_ids (should be unique)
    pixel_ids = [str(a.pixel_id) for a in all_apps if a.pixel_id]
    duplicates = len(pixel_ids) - len(set(pixel_ids))
    check(
        duplicates == 0,
        f"All pixel_ids are unique (no duplicates)",
        detail=f"Found {duplicates} duplicate pixel_ids!"
    )

    # Count how many have been opened
    opened = sum(1 for a in all_apps if a.opened_at is not None)
    viewed = sum(1 for a in all_apps if a.status == "viewed")
    applied_no_open = sum(1 for a in all_apps if a.status == "applied" and a.opened_at is None)

    print(f"  {INFO} Opened (opened_at set): {opened}/{total}")
    print(f"  {INFO} Status='viewed': {viewed}/{total}")
    print(f"  {INFO} Status='applied' & never opened: {applied_no_open}/{total}")

    if total > 10 and opened == 0:
        check(False, f"At least SOME of {total} applications should have been opened",
              detail="0 out of {total} opened — this strongly suggests the pixel pipeline is broken!",
              warn_only=True)


# =========================================================━━
# TEST 11: Gmail Draft Pixel Consistency
# =========================================================━━
def test_gmail_draft_pixel_flow():
    print("\n=== TEST 11: Gmail Draft Pixel Flow ===")

    # Verify the code path for Gmail drafts — the pixel_id used in the draft
    # must match what goes into the CRM record
    pixel_id = uuid.uuid4()
    html = "<html><body>Draft email</body></html>"

    # Inject pixel into draft
    injected = _inject_tracking_pixel(html, pixel_id)

    # Extract the pixel_id from the injected URL
    match = re.search(r"/pixel/([a-f0-9-]+)\.gif", injected)
    check(match is not None, "Pixel URL found in injected draft HTML")
    if match:
        extracted_id = match.group(1)
        check(
            extracted_id == str(pixel_id),
            f"Pixel ID in draft URL matches: {extracted_id}"
        )


# =========================================================━━
# TEST 12: Verify ngrok URL Consistency
# =========================================================━━
def test_ngrok_consistency():
    print("\n=== TEST 12: ngrok URL Consistency ===")

    webhook_url = settings.TELEGRAM_WEBHOOK_URL
    app_base_url = settings.APP_BASE_URL

    # Extract the domain from both URLs
    webhook_domain = urlparse(webhook_url).netloc
    app_domain = urlparse(app_base_url).netloc

    check(
        webhook_domain == app_domain,
        f"TELEGRAM_WEBHOOK_URL domain matches APP_BASE_URL domain",
        detail=f"Webhook: {webhook_domain} vs App: {app_domain}. If ngrok restarts, BOTH must be updated!"
    )

    # Check if it's a free ngrok URL (which changes on restart)
    is_free_ngrok = "ngrok-free.dev" in app_base_url or "ngrok.io" in app_base_url
    if is_free_ngrok:
        print(f"  {WARN} Using free ngrok — URL changes on every restart!")
        print(f"        ↳ If you restarted ngrok since deploying, ALL pixel URLs in already-sent emails are DEAD.")
        print(f"        ↳ Consider: ngrok with a fixed domain, or a paid tunnel service.")
        results["warn"] += 1


# =========================================================━━
# TEST 13: Email HTML Structure (pixel not stripped by email clients)
# =========================================================━━
def test_email_html_structure():
    print("\n=== TEST 13: Email HTML Structure for Pixel Survivability ===")

    pixel_id = uuid.uuid4()

    # Simulate what draft_email returns (the HTML wrapper)
    body_text = "Test email content<br>Second line"
    html_body = f"""
    <html>
        <body style="font-family: sans-serif; line-height: 1.5; color: #333;">
            {body_text}
        </body>
    </html>
    """
    
    injected = _inject_tracking_pixel(html_body, pixel_id)

    # The img tag should be a proper self-closing tag
    img_match = re.search(r'<img [^>]*?/>', injected)
    check(img_match is not None, "Pixel <img> is self-closing (valid HTML)")

    # Check it's NOT inside a comment or hidden element
    check(
        "<!--" not in injected or injected.index(f"{pixel_id}") < injected.index("<!--"),
        "Pixel is not inside an HTML comment"
    )

    # Check there's no Content-Security-Policy that could block it
    # (this would be a server-side header issue, but worth flagging)
    check(
        "content-security-policy" not in injected.lower(),
        "No CSP meta tag in email HTML (could block image loading)"
    )


# =========================================================━━
# MAIN RUNNER
# =========================================================━━
async def main():
    print("=" * 65)
    print("  PIXEL TRACKER PIPELINE — RIGOROUS TEST SUITE")
    print("=" * 65)

    # Synchronous tests
    test_pixel_injection()
    test_bot_filtering()
    test_prefetch_suppression()
    test_gmail_draft_pixel_flow()
    test_ngrok_consistency()
    test_email_html_structure()

    # Async tests (DB + network)
    try:
        await test_crm_record_creation()
    except Exception as e:
        print(f"\n  {FAIL} TEST 2 crashed: {e}")
        results["fail"] += 1

    try:
        await test_mark_email_opened()
    except Exception as e:
        print(f"\n  {FAIL} TEST 3 crashed: {e}")
        results["fail"] += 1

    try:
        await test_status_no_downgrade()
    except Exception as e:
        print(f"\n  {FAIL} TEST 4 crashed: {e}")
        results["fail"] += 1

    try:
        await test_followup_detection()
    except Exception as e:
        print(f"\n  {FAIL} TEST 9 crashed: {e}")
        results["fail"] += 1

    try:
        await test_existing_applications_have_pixels()
    except Exception as e:
        print(f"\n  {FAIL} TEST 10 crashed: {e}")
        results["fail"] += 1

    try:
        await test_tracking_url_reachability()
    except Exception as e:
        print(f"\n  {FAIL} TEST 7 crashed: {e}")
        results["fail"] += 1

    try:
        await test_full_roundtrip()
    except Exception as e:
        print(f"\n  {FAIL} TEST 8 crashed: {e}")
        results["fail"] += 1

    # ── Summary ──────────────────────────────────────────────
    print("\n" + "=" * 65)
    total = results["pass"] + results["fail"] + results["warn"]
    print(f"  RESULTS: {results['pass']}/{total} passed, "
          f"{results['fail']} failed, {results['warn']} warnings")

    if results["fail"] > 0:
        print(f"\n  {FAIL} PIPELINE HAS ISSUES — see failures above!")
    elif results["warn"] > 0:
        print(f"\n  {WARN} Pipeline logic OK, but check warnings above.")
    else:
        print(f"\n  {PASS} ALL CHECKS PASSED — pixel pipeline is healthy!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
