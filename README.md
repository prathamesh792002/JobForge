# 🚀 JobForge: Autonomous AI Job Application Pipeline

![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109.2-009688.svg)
![Gemini AI](https://img.shields.io/badge/AI-Google_Gemini_2.5-orange.svg)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**JobForge** is an autonomous, agentic job-application pipeline driven entirely through a Telegram bot. 

Designed as a private, single-user CRM, it automates the most tedious parts of the job hunt:
1. **Scraping** job descriptions from URLs or screenshots.
2. **Tailoring** your resume using Google Gemini AI to highlight matching skills.
3. **Compiling** a beautifully formatted PDF via RenderCV/LaTeX.
4. **Sending** the application via Gmail OAuth2 with embedded pixel tracking.
5. **Tracking** opens, scheduling follow-ups, and providing interview prep.

Everything is gated by a Human-In-The-Loop (HITL) Telegram interface — no emails are sent without your explicit tap. It is built for exactly one user (`TELEGRAM_CHAT_ID`) — every inbound message from any other chat is rejected outright.

## How it works — the core pipeline

```
Telegram message (URL / screenshot / raw JD text)
        │
        ▼
1. Ingestion        src/controllers/bot/application_flow.py (route_incoming)
        │              Rejects unauthorized chats. Routes by message type.
        ▼
2. Extraction        src/services/ai/extractor.py
        │              Firecrawl scrapes the page (primary). Falls back to a
        │              headless Playwright browser if Firecrawl fails or isn't
        │              configured. Pulls job title, company, HR email, required
        │              skills, and word count.
        ▼
3. Classification    src/services/ai/classifier.py
        │              Gemini reads the JD and picks one of three personas:
        │              ai_ml / swe / game_dev. The matching base resume YAML
        │              is loaded from src/services/personas/.
        ▼
4. ATS Scoring       src/services/ai/classifier.py
        │              3-tier weighted skill match against the JD:
        │                Tier 1 Languages       — 50%
        │                Tier 2 Frameworks/Infra — 35%
        │                Tier 3 Methodologies    — 15%
        │              A 78% honesty cap applies if any Tier 1 language is
        │              missing — the score can't lie about a core gap.
        │              Optional semantic (embedding-based) rescue can lift a
        │              weak keyword score if the JD is a strong conceptual fit.
        │              JD skills are cached in Redis for 72h, keyed by a hash
        │              of the JD text, so re-runs skip the LLM call.
        ▼
    ── gates ──
    word_count < 40           → skip tailoring, JD too sparse to work with
    ATS score < MINIMUM_FIT_THRESHOLD (40%) → PoorFitError, pipeline aborts
        ▼
5. Tailoring         src/services/ai/tailor.py
        │              A self-healing loop (max 2 passes) using Gemini 2.5
        │              Flash rewrites resume bullets/summary to better match
        │              the JD. The `design` block (fonts, colors, layout) is
        │              always stripped before the LLM sees the YAML and
        │              force-merged back afterwards, so the LLM can never
        │              damage the resume's visual theme.
        │              Protected fields — company names, job titles, dates,
        │              institutions, degrees, GPA — are restored in place
        │              after every pass; the LLM is not trusted to leave
        │              them alone even if instructed to.
        │              If the score is already ≥ 80%, it skips straight to a
        │              summary rewrite instead of a full pass.
        ▼
6. PDF Compilation   src/services/document/compiler.py
        │              Runs the RenderCV CLI as an async subprocess against
        │              the tailored YAML, using a custom LaTeX theme copied
        │              in from src/resources/templates/mycustomtheme/.
        │              If compilation fails, the raw YAML is sent to Telegram
        │              instead of silently failing.
        ▼
7. HITL Gate         src/controllers/bot/application_flow.py
        │              The compiled PDF is sent to Telegram with inline
        │              buttons: Send Email · Draft in Gmail · Message on
        │              WhatsApp (if a phone number was detected) · Edit
        │              Resume · Discard. Nothing past this point happens
        │              without an explicit tap. Pending state lives in
        │              Redis with a 24h TTL so a stale flow can't fire later.
        │              (Redis runs with append-only persistence, so a restart
        │              doesn't lose an un-actioned pending payload.)
        ▼
8. Outreach + CRM    src/services/communication/mailer.py
                       Gmail OAuth2 is the primary send path; raw SMTP is a
                       fallback if the Gmail service is unavailable. Every
                       email gets a 1×1 tracking-pixel GIF embedded
                       (src/routes/api/tracker.py); when it's requested, the
                       bot pings you on Telegram that the email was opened.
                       The application record is written to MySQL via
                       src/services/repository/application_repo.py.
```

**No HR email found** → the pipeline still delivers the PDF, but stops there — nothing is logged to the CRM, since there's nowhere to send it.
**Discard** → deletes the Redis payload and any temp files; nothing touches the database.

## Beyond the pipeline — the intelligence layer

These run on top of the same extraction/classification/scoring building blocks, but aren't part of the linear apply flow.

| Feature | File | What it does |
|---|---|---|
| **Follow-up reminders** | `main.py` (`_daily_followup_checker`) | Every 6h, finds applications sitting in `applied` with no email-open for `FOLLOWUP_DAYS` (default 7) and sends a Telegram prompt to approve/dismiss a drafted follow-up. The "Send Follow-up" button is resilient: if its Redis payload has expired, it re-drafts straight from the CRM record, and sends text-only if the original resume PDF is no longer on disk. |
| **Weekly digest** | `main.py` (`_weekly_digest_sender`) | Fires once a week (`WEEKLY_DIGEST_DAY`/`HOUR`) with a stats funnel (sent/opened/interviews/offers/ghosted/rejected) plus a market-demand report. |
| **Daily briefing** | `main.py` (`_daily_briefing`) → `src/services/ai/insights.py` | Optional (`DAILY_BRIEFING_ENABLED`) morning summary of active discovery leads, due follow-ups, and interviews in play. Silent if there's nothing to report. |
| **Proactive job discovery** | `main.py` (`_job_discovery`) → `src/services/ai/discovery.py` | Optional (`JOB_DISCOVERY_ENABLED`), opt-in. Polls public RSS/Atom job feeds (`JOB_DISCOVERY_SOURCES`), dedupes against Redis + the CRM, ATS-pre-screens new roles using the same extractor/classifier/scorer, and surfaces matches above `JOB_DISCOVERY_MIN_ATS` with a one-tap Apply button. Never touches email. |
| **Interview prep** | `src/services/ai/interview_prep.py` | Triggered by tapping "Interview" on an application. Uses Gemini with live Google Search grounding to produce a company snapshot, likely interview questions derived from the JD, and STAR-format answer starters grounded in your real experience. |
| **WhatsApp outreach** | `src/services/communication/whatsapp.py` | If a recruiter phone number is detected in the posting, drafts a short intro message and hands you a `wa.me` click-to-chat link. You review and send it yourself from WhatsApp — nothing is ever auto-sent (no API, no ToS/ban risk). |
| **Insights / learning loop** | `src/services/ai/insights.py` | Deterministic outcome analysis over the CRM: what's converting, what isn't, honest about small sample sizes (`/insights`). Also aggregates missing skills across every application into a Gemini-drafted learning roadmap (`/coach`), and generates the weekly market-demand report. |
| **Strategy advisor** | `src/services/ai/insights.py` (`advise_strategy`) | One-line per-application strategy note shown at the HITL gate — the "why this approach" reasoning layer. |
| **Company research** | gated by `COMPANY_RESEARCH_ENABLED` | Grounds cold-outreach emails with a real fact about the company, pulled via Gemini's search grounding. |
| **Manual resume editing** | `src/services/document/editor.py` | Lets you hand-edit the tailored YAML via natural-language instructions to the LLM before re-compiling, from the "Edit Resume" button. |

## Telegram bot commands

| Command | Purpose |
|---|---|
| `/apply` | Start a new application (or just send a link/screenshot/text directly) |
| `/cancel` | Abort an in-progress pipeline run and clear its Redis state |
| `/stats` | This week's funnel: sent, opened, interviews, offers, ghosted, rejected |
| `/insights` | Outcome analysis — what's actually converting |
| `/demand` | Most in-demand skills across recent JDs, per persona |
| `/coach` | Skill-gap learning roadmap based on everything you've applied to |
| `/history` | Paginated last-10 applications with status |
| `/export` | Full history as a downloadable CSV |
| `/help` | Full command reference |

## Architecture at a glance

```
src/
  main.py                     FastAPI app: lifespan (bot init, webhook registration,
                               background tasks for follow-ups/digest/briefing/discovery)
  config/settings.py          pydantic-settings config, singleton via get_settings()
  controllers/bot/
    application_flow.py       Pipeline dispatcher, HITL callback handlers, Redis state machine
    commands.py                /start, /help, unknown-command handling
    dashboard.py                /stats, /insights, /demand, /coach, /history, /export
    keyboards.py                All inline keyboard builders
    states.py                   FSM state constants and Redis key prefixes
  services/
    ai/
      extractor.py              Firecrawl + Playwright scraping
      classifier.py             Gemini persona classification + ATS scoring
      tailor.py                 Self-healing resume optimization loop
      vision.py                 Telegram photo → Gemini Vision extraction
      discovery.py               RSS/Atom feed scanning + pre-screening
      interview_prep.py          Grounded interview-prep brief generation
      insights.py                 Outcome analysis, coach plan, strategy advisor, briefing
    document/
      compiler.py                RenderCV PDF compilation
      editor.py                   Manual YAML editing via LLM
    communication/
      mailer.py                   Email drafting + sending + tracking-pixel injection
      whatsapp.py                  Click-to-chat WhatsApp outreach drafting
    repository/
      application_repo.py         SQLAlchemy CRUD + stats aggregation for applications
  models/application.py         SQLAlchemy ORM model (UUID PKs, pixel tracking)
  validators/                   Pydantic schemas for extraction results, resume YAML
  routes/api/
    webhook.py                   POST /v1/telegram/webhook
    tracker.py                   GET /v1/tracker/pixel/{id}.gif — 1×1 GIF + open alert
  database/
    session.py                   Async SQLAlchemy engine/session factory
    redis.py                     Redis connection management
    migrations/                  Alembic migrations
  resources/templates/mycustomtheme/  Custom RenderCV LaTeX theme

tests/
  test_extractor.py / test_classifier.py / test_tailor.py / test_compiler.py
```

## Setup

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium   # fallback scraper, needed even if Firecrawl is primary

# Start infrastructure
docker-compose up -d           # MySQL + Redis

# Run database migrations
alembic upgrade head

# Configure — copy .env.example to .env and fill in:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_URL, TELEGRAM_CHAT_ID
#   DB_HOST/PORT/USER/PASSWORD/NAME
#   GOOGLE_API_KEY (+ optional GEMINI_API_KEY_* for key rotation)
#   FIRECRAWL_API_KEY (optional — Playwright is the fallback)
#   SMTP_EMAIL/PASSWORD or Gmail OAuth2 credentials
#   CANDIDATE_PHONE/EMAIL/LINKEDIN/GITHUB

# Run the dev server
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

### Running tests

```bash
python -m tests.test_extractor
python -m tests.test_classifier
python -m tests.test_tailor
python -m tests.test_compiler
```

## Hard rules the pipeline never breaks

- **The LLM cannot rewrite** company names, job titles, dates, institutions, degrees, or GPA — enforced in code (`tailor.py`), not just prompted.
- **Sparse JDs are skipped** — under 40 words of content, tailoring never runs.
- **Poor-fit JDs abort the pipeline** — below `MINIMUM_FIT_THRESHOLD` (40% by default), it raises rather than sending a bad-fit resume.
- **Nothing is emailed automatically** — the HITL gate requires an explicit Telegram button tap, every time.
- **WhatsApp is never auto-sent** — click-to-chat only, you send it yourself.
- **RenderCV is the only PDF tool** — no alternative compiler paths.
- **Discard means discard** — Redis payload and temp files are deleted, nothing is written to the CRM.

## Windows-specific notes

This runs on Windows, which needs a few workarounds baked into the code:

- `asyncio.WindowsProactorEventLoopPolicy` is set in `main.py` — required for Playwright to work at all on Windows.
- Playwright scraping runs inside `asyncio.to_thread` using `sync_playwright`, avoiding `SelectorEventLoop` incompatibilities.
- Temp directories are resolved to their 8.3 short-path form before being handed to RenderCV/pdflatex, which otherwise crashes on paths containing a `~` (e.g. `PRATHA~1`).
- `RENDERCV_BIN` in `compiler.py` points at the absolute venv path rather than relying on `PATH` resolution.
