"""
Mailer Service

Handles SMTP integration for sending cold emails and uses Claude 3.5 Sonnet
to synthesize personalized, high-converting cold email copy.
Injects a 1x1 tracking pixel into the HTML body.

Pipeline position: C5 — called after HITL approval.
"""

import base64
import json
import logging
import os
import smtplib
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from uuid import UUID

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings

logger = logging.getLogger(__name__)

SYNTHESIZER_PROMPT = """
You are an expert career writer composing a concise, compelling, professional cold outreach email on behalf of Prathamesh Shirole for a specific job. It must read like a sharp, motivated engineer wrote it — confident, specific, and tailored — never generic, robotic, or filler-heavy.

### CANDIDATE BASELINE DATA (use only these facts — never invent employers, titles, dates, or metrics)
- Candidate Name: Prathamesh Shirole
- Education: B.Tech in Computer Engineering, Dr. Babasaheb Ambedkar Technological University (CGPA: 7.56)
- Current Position: Artificial Intelligence Intern at Infosys Ltd
- Past Position: Data Science Intern at Siliconmount Tech Services
- Core Competencies: Generative AI, Retrieval-Augmented Generation (RAG), Agentic Workflows, Python, SQL/MySQL, Data Structures & Algorithms
- Featured Projects:
  * JobForge (FLAGSHIP — always prefer this when featuring a project): an agentic AI
    job-application pipeline he architected end-to-end — a Telegram-bot-driven system that
    scrapes job postings, scores ATS fit with a 3-tier skill matrix, tailors resumes through
    a Gemini-powered self-healing optimization loop, compiles PDFs, and automates recruiter
    outreach (FastAPI, Redis, MySQL, Gmail API, RAG/agentic workflows).
  * AI Real Estate Evaluation Application: built with modern AI frameworks on Kaggle datasets.
- Leadership: President, Association of Computer Engineering Students (2022-2024)

### TARGET
Company: {company_name}
Position: {job_title}
Job Description:
{jd_text}

### RECENT COMPANY CONTEXT (from live research; may be empty)
{company_hook}
If context is provided above, weave ONE specific, natural reference to it into
paragraph 1 to show informed interest (never fabricate context; if empty, skip this).

### WRITING RULES
1. Output ONLY the email. The FIRST line must be "Subject: ..." with no blank line before it. No preamble, no explanations, no markdown, no code fences.
2. ABSOLUTELY NO EMOJIS anywhere.
3. Tone: professional, warm, and confident. Specifically tailored to {company_name} and the job description. Avoid clichés like "I am writing to apply", "I am excited by the prospect", and generic filler.
4. Subject line: specific and compelling — include the role and 1-2 of the candidate's strongest matching strengths for it.
5. Body: three short paragraphs, roughly 150-180 words total.
   - Paragraph 1: a strong, specific opening that names the role, shows genuine and informed interest in {company_name}, and states who the candidate is in one line.
   - Paragraph 2: connect 2-3 of the candidate's most relevant skills, projects, or experience DIRECTLY to the key requirements in the job description. Be concrete and reference real projects/experience from the baseline data.
   - Paragraph 3: a confident close that notes the attached resume and invites a conversation.
6. End with EXACTLY this signature block, reproducing the bracketed tokens verbatim (they are replaced later):

Best regards,
Prathamesh Shirole
Phone: [INSERT_PHONE]
Email: [INSERT_EMAIL]
LinkedIn: [INSERT_LINKEDIN]
GitHub: [INSERT_GITHUB]
"""



async def research_company_hook(company_name: str) -> str:
    """
    Live-research 1-2 recent, verifiable facts about the company (funding, launch,
    recognition, tech direction) via Google-Search-grounded Gemini, for use as a
    personalization hook in the cold email. Returns "" on any failure — the email
    prompt degrades gracefully to no hook.
    """
    if not company_name or company_name.lower() in ("unknown", "not detected"):
        return ""

    prompt = (
        f"Using web search, state 1-2 recent, specific, verifiable facts about the company "
        f'"{company_name}" that a job applicant could naturally reference in a cold email '
        "(recent funding, product launch, award, notable growth, or tech-stack direction). "
        "2 short sentences max, plain factual text, no advice, no markdown. "
        "If you cannot find anything reliable about this specific company, output exactly: NONE"
    )
    grounding = genai_types.Tool(google_search=genai_types.GoogleSearch())

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            resp = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.2, tools=[grounding]),
            )
            text = (resp.text or "").strip()
            if not text or text.upper().startswith("NONE"):
                return ""
            logger.info("Company hook researched for %s (key #%d)", company_name, i + 1)
            return text[:400]
        except Exception as e:
            logger.warning("research_company_hook key #%d failed: %s", i + 1, str(e)[:120])
    return ""


async def draft_email(
    candidate_name: str,
    job_title: str,
    company_name: str,
    pdf_path: str,
    jd_text: str = "",
    company_hook: str = "",
) -> tuple[str, str]:
    """
    Draft the cold email subject and body using Claude 3.5 Sonnet based on the
    Job Description and the candidate's tailored resume.

    Returns:
        Tuple of (subject, html_body).
    """
    logger.info("Synthesizing cold email for %s at %s...", job_title, company_name)

    try:
        prompt = SYNTHESIZER_PROMPT.format(
            job_title=job_title,
            company_name=company_name,
            jd_text=jd_text,
            company_hook=company_hook or "(none)",
        )

        response_text = None
        last_error = None
        for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
            try:
                client = genai.Client(api_key=api_key)
                response = await client.aio.models.generate_content(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(temperature=0.3),
                )
                response_text = response.text.strip()
                logger.info("Email drafted with Gemini key #%d", i + 1)
                break
            except Exception as e:
                last_error = str(e)
                logger.warning("draft_email key #%d failed: %s", i + 1, last_error)

        if not response_text:
            raise Exception(f"All Gemini keys failed for email draft: {last_error}")
        
        # Post-processing the AI output — contact details come from .env
        clean_email = response_text.replace("[INSERT_PHONE]", settings.CANDIDATE_PHONE) \
                                   .replace("[INSERT_EMAIL]", settings.CANDIDATE_EMAIL) \
                                   .replace("[INSERT_LINKEDIN]", settings.CANDIDATE_LINKEDIN) \
                                   .replace("[INSERT_GITHUB]", settings.CANDIDATE_GITHUB)
        
        # Parse subject and body
        lines = clean_email.split("\n")
        subject = f"Application for {job_title}"
        if lines[0].lower().startswith("subject:"):
            subject = lines[0][8:].strip()
            body_text = "\n".join(lines[1:]).strip()
        else:
            body_text = clean_email
            
        # Convert markdown/text to HTML
        html_body = body_text.replace("\n", "<br>")

        # Ensure we wrap the body in a clean HTML structure if Claude didn't
        if "<html>" not in html_body.lower():
            html_body = f"""
            <html>
                <body style="font-family: sans-serif; line-height: 1.5; color: #333;">
                    {html_body}
                </body>
            </html>
            """
            
        return subject, html_body

    except Exception as e:
        logger.error("Failed to synthesize email: %s", str(e))
        # Fallback to generic template
        subject = f"{candidate_name} - Application for {job_title}"
        html_body = f"""
        <html>
            <body style="font-family: sans-serif; line-height: 1.5; color: #333;">
                <p>Hi {company_name} Team,</p>
                <p>I am writing to express my interest in the <strong>{job_title}</strong> position.</p>
                <p>I have attached my resume for your review. Let me know if my background aligns with what you're looking for.</p>
                <p>Best regards,<br>{candidate_name}</p>
            </body>
        </html>
        """
        return subject, html_body


_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _get_gmail_service():
    """
    Return an authenticated Gmail API service object.
    Raises RuntimeError with a helpful message if token.json is missing or
    lacks the gmail.compose scope (user needs to run setup_oauth.py).
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = Path("token.json")
    if not token_path.exists():
        raise RuntimeError(
            "token.json not found.\nRun:  python scripts/setup_oauth.py"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), _GMAIL_SCOPES)

    has_compose = (
        creds.scopes
        and "https://www.googleapis.com/auth/gmail.compose" in creds.scopes
    )
    if not has_compose:
        token_path.unlink(missing_ok=True)
        raise RuntimeError(
            "Gmail token is missing the compose scope.\n"
            "Run:  python scripts/setup_oauth.py  to re-authorize."
        )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _inject_tracking_pixel(html_body: str, pixel_id: UUID) -> str:
    """Return html_body with the 1x1 tracking pixel injected before </body>.

    Deliberately does NOT use display:none / visibility:hidden — several mail
    clients (incl. Gmail in some cases) skip fetching hidden images, which would
    silently defeat open-tracking. A 1x1 image with opacity:0 stays invisible
    while still being fetched by the client's image loader.
    """
    tracking_url = f"{settings.tracker_base_url}/pixel/{pixel_id}.gif"
    pixel_img = (
        f'\n<img src="{tracking_url}" width="1" height="1" alt="" '
        f'style="border:0;width:1px;height:1px;max-width:1px;max-height:1px;opacity:0;overflow:hidden;" />'
    )
    if "</body>" in html_body:
        return html_body.replace("</body>", f"{pixel_img}\n</body>")
    return html_body + pixel_img


def _build_raw_message(to_email: str, subject: str, html_body: str, pdf_path: str) -> str:
    """Build a base64url-encoded MIME message (HTML + optional PDF) for the Gmail API."""
    from_addr = settings.EMAIL_HOST_USER or settings.SMTP_EMAIL
    msg = MIMEMultipart("mixed")
    msg["To"] = to_email
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_data)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(pdf_path)}"',
        )
        msg.attach(part)

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


async def send_email_gmail_api(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_path: str,
    pixel_id: UUID,
) -> bool:
    """
    Send the outreach email via the Gmail API (OAuth2) with the tracking pixel
    injected and the PDF attached. Raises RuntimeError if the Gmail service is
    unavailable (no/invalid token) so the caller can fall back to SMTP.
    """
    import asyncio

    body_html = _inject_tracking_pixel(html_body, pixel_id)

    def _send() -> bool:
        raw = _build_raw_message(to_email, subject, body_html, pdf_path)
        service = _get_gmail_service()  # raises RuntimeError if token missing/invalid
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True

    result = await asyncio.to_thread(_send)
    logger.info("Email sent via Gmail API to %s (pixel_id=%s)", to_email, pixel_id)
    return result


async def send_outreach_email(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_path: str,
    pixel_id: UUID,
) -> bool:
    """
    Send the application email, preferring the Gmail API (better deliverability,
    consistent with the OAuth setup) and falling back to raw SMTP only if the
    Gmail service is unavailable.
    """
    try:
        return await send_email_gmail_api(to_email, subject, html_body, pdf_path, pixel_id)
    except RuntimeError as e:
        logger.warning("Gmail API unavailable (%s) — falling back to SMTP.", str(e))
    except Exception as e:
        logger.warning("Gmail API send failed (%s) — falling back to SMTP.", str(e))
    return await send_email(to_email, subject, html_body, pdf_path, pixel_id)


async def create_gmail_draft(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_path: str,
    pixel_id: UUID,
) -> str:
    """
    Create a Gmail draft with the cold email and PDF resume attached.
    Injects the tracking pixel into the body before creating the draft.

    Returns:
        The Gmail Drafts folder URL so the user can open and send it.
    """
    import asyncio

    body_html = _inject_tracking_pixel(html_body, pixel_id)

    def _build_and_create() -> str:
        raw = _build_raw_message(to_email, subject, body_html, pdf_path)
        service = _get_gmail_service()
        result = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()

        # Use the message ID (not draft resource ID) — this is what Gmail's
        # URL fragment router uses and what the mobile app deep-links to.
        message_id = result.get("message", {}).get("id", "")
        if message_id:
            return f"https://mail.google.com/mail/u/0/#drafts/{message_id}"
        return "https://mail.google.com/mail/u/0/#drafts"

    url = await asyncio.to_thread(_build_and_create)
    logger.info("Gmail draft created for %s (pixel_id=%s)", to_email, pixel_id)
    return url


async def redraft_email(
    current_subject: str,
    current_body: str,
    instruction: str,
    jd_text: str = "",
) -> tuple[str, str]:
    """Revise an existing cold email draft based on a natural-language instruction."""
    import re as _re

    prompt = (
        "You are editing a cold job application email. Here is the current version:\n\n"
        f"Subject: {current_subject}\n\n"
        f"Body:\n{current_body}\n\n"
        f"User instruction: {instruction}\n\n"
        "Revise the email according to the instruction. "
        'Output ONLY a JSON object with keys "subject" (string) and "body" (HTML string). '
        "No extra text or markdown wrappers."
    )

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.4),
            )
            text = response.text.strip()
            json_match = _re.search(r"\{.*\}", text, _re.DOTALL)
            if json_match:
                import json as _json
                data = _json.loads(json_match.group())
                new_subject = data.get("subject", current_subject)
                new_body = data.get("body", current_body)
                logger.info("Email redrafted with Gemini key #%d", i + 1)
                return new_subject, new_body
        except Exception as e:
            logger.warning("redraft_email key #%d failed: %s", i + 1, str(e))

    logger.warning("All Gemini keys failed for redraft_email — returning original")
    return current_subject, current_body


async def draft_followup_email(
    job_title: str,
    company_name: str,
    original_subject: str,
) -> tuple[str, str]:
    """Draft a short, polite follow-up email for a non-responsive HR contact."""
    prompt = (
        "Write a short, polite follow-up email for a job application that received no response.\n\n"
        f"Original role: {job_title} at {company_name}\n"
        f"Original subject line: {original_subject}\n\n"
        "Candidate: Prathamesh Shirole (AI/ML engineer, current AI Intern at Infosys).\n\n"
        "Requirements:\n"
        "- Under 100 words in the body\n"
        "- Polite and professional, not pushy\n"
        "- Reference the original application\n"
        "- End with contact details placeholders [INSERT_PHONE], [INSERT_EMAIL], [INSERT_LINKEDIN]\n"
        'Output ONLY a JSON object with keys "subject" and "body" (HTML). No extra text.'
    )

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            import re as _re, json as _json
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.3),
            )
            text = response.text.strip()
            json_match = _re.search(r"\{.*\}", text, _re.DOTALL)
            if json_match:
                data = _json.loads(json_match.group())
                subject = data.get("subject", f"Re: {original_subject}")
                body = data.get("body", "")
                body = (
                    body.replace("[INSERT_PHONE]", settings.CANDIDATE_PHONE)
                        .replace("[INSERT_EMAIL]", settings.CANDIDATE_EMAIL)
                        .replace("[INSERT_LINKEDIN]", settings.CANDIDATE_LINKEDIN)
                )
                logger.info("Follow-up email drafted with Gemini key #%d", i + 1)
                return subject, body
        except Exception as e:
            logger.warning("draft_followup_email key #%d failed: %s", i + 1, str(e))

    # Fallback
    subject = f"Following up — {job_title} Application"
    body = (
        f"<p>Dear Hiring Team,</p>"
        f"<p>I wanted to follow up on my application for the <strong>{job_title}</strong> role at "
        f"{company_name}. I remain very interested and would love to discuss the opportunity.</p>"
        f"<p>Best regards,<br>Prathamesh Shirole<br>"
        f"{settings.CANDIDATE_PHONE}<br>{settings.CANDIDATE_EMAIL}<br>{settings.CANDIDATE_LINKEDIN}</p>"
    )
    return subject, body


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_path: str,
    pixel_id: UUID,
    require_pdf: bool = True,
) -> bool:
    """
    Send an email via SMTP with an attached PDF resume and tracking pixel.

    Args:
        to_email: HR contact email address.
        subject: Email subject.
        html_body: Email HTML body.
        pdf_path: Path to the PDF resume to attach.
        pixel_id: UUID for the tracking pixel.
        require_pdf: When True (default, main outreach path), a missing PDF is a
            hard error. When False (follow-up path), send text-only if the PDF
            is gone — e.g. the temp file didn't survive a reboot.

    Returns:
        True if sent successfully, False otherwise.
    """
    logger.info("Preparing to send email to %s (pixel_id=%s)", to_email, pixel_id)

    smtp_email = settings.EMAIL_HOST_USER or settings.SMTP_EMAIL
    smtp_password = settings.EMAIL_HOST_PASSWORD or settings.SMTP_PASSWORD

    if not smtp_email or not smtp_password:
        logger.error("SMTP credentials not configured in .env (need EMAIL_HOST_USER and EMAIL_HOST_PASSWORD)")
        return False

    try:
        # 1. Inject tracking pixel into the HTML body
        html_body = _inject_tracking_pixel(html_body, pixel_id)

        # 2. Construct the EmailMessage
        message = EmailMessage()
        message["To"] = to_email
        message["From"] = smtp_email
        message["Subject"] = subject

        # Set content as HTML
        message.set_content("Please enable HTML to view this email.")
        message.add_alternative(html_body, subtype="html")

        # 3. Attach the PDF (if available)
        has_pdf = bool(pdf_path) and os.path.exists(pdf_path)
        if not has_pdf:
            if require_pdf:
                raise FileNotFoundError(f"PDF resume not found at {pdf_path}")
            logger.warning(
                "Sending email to %s WITHOUT a PDF attachment (require_pdf=False); path=%s",
                to_email, pdf_path,
            )

        if has_pdf:
            with open(pdf_path, "rb") as f:
                pdf_data = f.read()

            pdf_filename = os.path.basename(pdf_path)
            message.add_attachment(
                pdf_data,
                maintype="application",
                subtype="pdf",
                filename=pdf_filename,
            )

        # 4. Send via SMTP (Offloaded to a thread pool to prevent blocking the async event loop)
        logger.info("Connecting to SMTP server...")
        
        def _do_send():
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.login(smtp_email, smtp_password)
                server.send_message(message)

        import asyncio
        await asyncio.to_thread(_do_send)

        logger.info("Email sent successfully via SMTP!")
        return True

    except Exception as e:
        logger.error("Failed to send email via SMTP: %s", str(e), exc_info=True)
        return False
