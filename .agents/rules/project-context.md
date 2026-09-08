# JobForge — Project Context

> This file is the single source of truth for all architectural decisions,
> schemas, prompts, and business rules. Read it completely before writing
> any code. See the master task directive for full contents.

## Quick Reference

- **8-Component Pipeline**: Telegram → Ingestion → Extraction → Classification → Tailoring → Compilation → HITL → Outreach → CRM
- **3 Personas**: AI_ML, SWE, DATA
- **Sparse JD Gate**: Skip tailoring if word_count < 40
- **Security**: Reject all chat_ids ≠ TELEGRAM_CHAT_ID
- **PDF Tool**: RenderCV ONLY — no alternatives
- **Email**: Gmail OAuth2 ONLY — no raw SMTP

## Build Phases

1. Infrastructure Foundation
2. Telegram Gateway
3. Extraction + Classification
4. Tailoring + Compilation
5. HITL + Email + CRM + Tracking

## Business Rules (Never Break)

1. LLM cannot modify: company names, job titles, dates, institutions, degrees
2. JD word count < 40 → skip tailoring
3. Email send requires explicit inline button tap
4. No hr_email → pipeline ends after PDF delivery
5. PDFs sent as Telegram documents (not photos)
6. Bot is private: reject all unauthorized chat_ids
7. Pixel tracking only fires first open alert
8. Discard → delete Redis payload + temp files, no CRM log
9. All three persona YAMLs follow identical schema
10. OAuth2 token refresh must happen automatically
