# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start the dev server
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Start infrastructure (MySQL + Redis)
docker-compose up -d

# Run database migrations
alembic upgrade head

# Run individual test modules
python -m tests.test_extractor
python -m tests.test_classifier
python -m tests.test_tailor
python -m tests.test_compiler

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (required for scraping fallback)
playwright install chromium
```

## Architecture

JobForge is a **private, single-user automated job application pipeline** driven by a Telegram bot. It exposes a FastAPI server that receives Telegram webhook updates and runs a multi-stage pipeline to scrape, classify, tailor, compile, and send job applications. On top of that core pipeline sits an intelligence/automation layer (proactive discovery, follow-ups, interview prep, outcome analytics) that reuses the same extraction/classification/scoring building blocks. See `README.md` for the full user-facing feature writeup — this file focuses on where things live and what not to break.

### Pipeline Stages (in order)

1. **Ingestion** (`src/controllers/bot/application_flow.py` → `route_incoming`) — Accepts a job URL, screenshot, or raw JD text from Telegram. All messages from unauthorized `chat_id`s are rejected immediately.

2. **Extraction** (`src/services/ai/extractor.py`) — Scrapes the job posting using Firecrawl (primary) with Playwright as fallback. Extracts job title, company, HR email, required skills, and word count via regex/keyword matching.

3. **Classification** (`src/services/ai/classifier.py`) — Gemini classifies the JD into one of three personas: `ai_ml`, `swe`, `game_dev`. The matching base resume YAML is loaded from `src/services/personas/`.

4. **ATS Scoring** (`src/services/ai/classifier.py`) — 3-tier weighted skill matching: Tier 1 Languages (50%), Tier 2 Frameworks/Infra (35%), Tier 3 Methodologies (15%). Applies a 78% honesty cap when any Tier 1 language is missing. An optional embedding-based semantic rescue (`SEMANTIC_MATCH_ENABLED`, `gemini-embedding-001`) can lift a weak keyword score when the JD is a strong conceptual fit. JD skills are cached in Redis (72h TTL) keyed by content hash.

5. **Tailoring** (`src/services/ai/tailor.py`) — Self-healing optimization loop (max 2 passes) using Gemini 2.5 Flash. The `design` block is always stripped before sending to the LLM and force-merged back after each pass to prevent theme loss. Returns early if score ≥ 80% (but still rewrites the summary). Raises `PoorFitError` if initial score < `MINIMUM_FIT_THRESHOLD`.

6. **PDF Compilation** (`src/services/document/compiler.py`) — Runs RenderCV CLI as an async subprocess. Copies the custom LaTeX theme from `src/resources/templates/mycustomtheme/` into the temp dir before compiling. Falls back to sending the raw YAML on compilation failure.

7. **HITL Gate** (`src/controllers/bot/application_flow.py`, keyboards in `src/controllers/bot/keyboards.py`) — Sends the PDF to Telegram with inline buttons: Send Email / Draft in Gmail / Message on WhatsApp (only if a phone number was detected) / Edit Resume / Discard. Pending state is stored in Redis with a TTL.

8. **Outreach + CRM** (`src/services/communication/mailer.py`, `src/services/repository/application_repo.py`) — Gmail OAuth2/SMTP sends the email with a 1x1 tracking pixel embedded. Application is written to the DB; pixel open alerts fire back through the Telegram bot via `src/routes/api/tracker.py`.

### Intelligence & Automation Layer (built on top of the pipeline, not part of it)

- **Proactive job discovery** (`src/services/ai/discovery.py`, background task in `main.py`) — opt-in (`JOB_DISCOVERY_ENABLED`). Polls configured RSS/Atom feeds, dedupes vs CRM + Redis seen-set, ATS-pre-screens new roles by reusing the extractor/classifier/scorer, surfaces high-fit matches to Telegram with an Apply button. Never touches email.
- **Follow-up reminders** (`_daily_followup_checker` in `main.py`) — every 6h, prompts once per application that's sat in `applied` with no email-open for `FOLLOWUP_DAYS` (default 7). Single-threshold reminder, not a multi-step day-3/7/14 cadence. The "Send Follow-up" handler (`handle_send_followup`) is resilient to a lost Redis payload: on a cache miss it reconstructs from the CRM via `get_application_by_id` + `draft_followup_email`, and sends text-only (`send_email(..., require_pdf=False)`) if the original resume PDF is gone from disk.
- **Weekly digest** (`_weekly_digest_sender` in `main.py`) — once a week (`WEEKLY_DIGEST_DAY`/`HOUR`), sends the stats funnel plus a market-demand report (`src/services/ai/insights.py::generate_demand_report`).
- **Daily briefing** (`_daily_briefing` in `main.py`, opt-in via `DAILY_BRIEFING_ENABLED`) — morning summary of discovery leads, due follow-ups, interviews in play; silent if nothing to report.
- **Interview prep** (`src/services/ai/interview_prep.py`) — triggered when an application's status flips to `interview`. Gemini + Google Search grounding produces a company snapshot, likely questions, and STAR answer starters.
- **WhatsApp outreach** (`src/services/communication/whatsapp.py`, opt-in via `WHATSAPP_ENABLED`) — detects a recruiter phone number, drafts an intro message, hands the user a `wa.me` click-to-chat link. Never sent automatically — no WhatsApp API is used (ToS/ban risk).
- **Insights / learning loop / coach** (`src/services/ai/insights.py`) — deterministic outcome analysis (`/insights`), Gemini-drafted skill-gap roadmap aggregated across all applications (`/coach`), one-line per-application strategy note at the HITL gate (`advise_strategy`), grounded company-research hook in cold emails (`COMPANY_RESEARCH_ENABLED`).

### Key Architectural Constraints (never violate)

- **LLM cannot modify**: company names, job titles, dates, institutions, degrees, GPA values — *enforced* in `tailor.py` via `_restore_protected_fields_inplace` (folded into `_force_merge_design`), not just prompted
- **Sparse JD gate**: `word_count < 40` → skip tailoring entirely
- **Poor fit gate**: initial ATS score < `MINIMUM_FIT_THRESHOLD` (default 40%) → raise `PoorFitError`, abort pipeline
- **HITL gate**: email requires explicit inline button tap, never sent automatically. The pending payload lives only in Redis (keyed `app:pending:{chat_id}`) with a 24h TTL (`STATE_TTL` in `states.py` — the pending payload is set with `STATE_TTL`, not `PENDING_TTL`, which is dead code). Before the user taps Send there is no CRM row yet (`_ensure_crm_record` runs at send time), so a main-gate payload cannot be reconstructed after it expires — only widened TTL + Redis persistence protect it. Redis runs with `--appendonly yes` (see `docker-compose.yml`) so a restart doesn't drop pending payloads.
- **No HR email**: pipeline ends after PDF delivery (no CRM log)
- **PDF tool**: RenderCV only — no alternatives
- **Email**: Gmail API (OAuth2) is primary for the direct-send path (`send_outreach_email`); raw SMTP is a fallback only when the Gmail service is unavailable
- **Discard action**: deletes Redis payload + temp files, writes nothing to CRM
- **WhatsApp**: click-to-chat only, never sent programmatically — no WhatsApp Business API integration
- **Job discovery**: read-only against job feeds; never emails or applies without the user tapping Apply first (which re-enters the normal HITL pipeline)
- **Inbound email reading is out of scope**: previously built and reverted by explicit user decision (privacy — no background inbox reading, no email body sent to Gemini). Do not rebuild without the user asking again. Gmail OAuth scope stays `gmail.send` + `gmail.compose` only.

### File Layout

```
src/
  main.py                     — FastAPI app, lifespan (bot init, webhook registration,
                                 background tasks: follow-up checker, weekly digest,
                                 daily briefing, job discovery)
  config/settings.py          — pydantic-settings, singleton via get_settings()
  controllers/bot/
    application_flow.py       — Pipeline dispatcher, HITL callbacks, Redis state machine
    commands.py                — /start, /help, unknown-command handling
    dashboard.py                — /stats, /insights, /demand, /coach, /history, /export
    keyboards.py                — Inline keyboard builders (approval, WhatsApp, status, discovery, follow-up)
    states.py                   — FSM state constants and Redis key prefixes
  services/
    ai/
      extractor.py              — Firecrawl + Playwright scraping
      classifier.py             — Gemini persona classification + ATS scoring matrix
      tailor.py                 — Self-healing optimization loop (Gemini)
      vision.py                 — Telegram photo download + Gemini Vision extraction
      discovery.py               — RSS/Atom feed scanning, dedupe, ATS pre-screening
      interview_prep.py          — Grounded interview-prep brief generation
      insights.py                 — Outcome analysis, coach plan, strategy advisor, briefing, demand report
    document/
      compiler.py                — RenderCV PDF compilation
      editor.py                   — Manual YAML editing via LLM
    communication/
      mailer.py                   — Email drafting (Gemini) + sending + tracking pixel injection
      whatsapp.py                  — Click-to-chat WhatsApp outreach drafting
    repository/
      application_repo.py         — SQLAlchemy CRUD + stats aggregation for applications table
  models/application.py         — SQLAlchemy ORM model (UUID PKs, pixel tracking)
  validators/
    application.py               — Pydantic schemas: ExtractionResult, ApplicationCreate
    resume.py                    — Resume YAML validation
  routes/api/
    webhook.py                   — POST /v1/telegram/webhook
    tracker.py                    — GET /v1/tracker/pixel/{pixel_id}.gif (1x1 GIF + Telegram alert)
  database/
    session.py                    — Async engine + session factory + get_db dependency
    redis.py                      — Redis connection management (get_redis / close_redis)
    migrations/                   — Alembic migration scripts (applications, ATS scores, enhancements, demanded skills)
  resources/templates/mycustomtheme/ — Custom RenderCV LaTeX theme

tests/
  conftest.py                 — pytest fixtures (async DB session)
  test_extractor.py / test_classifier.py / test_tailor.py / test_compiler.py
```

### Telegram Bot Commands

`/apply` (or just send a link/screenshot/text), `/cancel`, `/stats`, `/insights`, `/demand`, `/coach`, `/history`, `/export`, `/help`. Registered in `main.py` lifespan via `set_my_commands`; handlers split across `commands.py` (start/help/unknown) and `dashboard.py` (stats/insights/demand/coach/history/export).

### Settings

All config is loaded from `.env` via `src/config/settings.py`. The `DATABASE_URL` property assembles a MySQL+aiomysql URL from `DB_HOST/PORT/USER/PASSWORD/NAME`. The `ALL_GEMINI_KEYS` property auto-discovers additional keys named `GEMINI_API_KEY_*` in `.env` for key rotation.

Feature flags worth knowing (all default as shown unless overridden in `.env`): `SEMANTIC_MATCH_ENABLED=true`, `STRATEGY_ADVISOR_ENABLED=true`, `COMPANY_RESEARCH_ENABLED=true`, `DAILY_BRIEFING_ENABLED=true`, `WHATSAPP_ENABLED=true`, `JOB_DISCOVERY_ENABLED=false` (opt-in, needs `JOB_DISCOVERY_SOURCES` set). `FOLLOWUP_DAYS` and `WEEKLY_DIGEST_DAY`/`WEEKLY_DIGEST_HOUR` control the two `main.py` background reminder tasks.

### Windows-Specific Quirks

- `asyncio.WindowsProactorEventLoopPolicy` is set in `src/main.py` to fix Playwright on Windows.
- Playwright scraping runs in a thread (`asyncio.to_thread`) using `sync_playwright` to avoid `SelectorEventLoop` issues.
- Temp dir short-path (8.3) resolution is applied in `_save_yaml_to_temp` before passing paths to RenderCV/pdflatex, which crashes on tilde-paths (e.g., `PRATHA~1`).
- `RENDERCV_BIN` in `compiler.py` points to the absolute venv path to avoid PATH lookup failures.
