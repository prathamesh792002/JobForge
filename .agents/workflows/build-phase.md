# JobForge — Build Phase Workflow

Execute build phases in order. Each phase must pass verification before proceeding.

## Usage

Run `/build-phase <N>` where N is 1–5.

## Phases

1. **Infrastructure Foundation** — Docker, DB, config, Alembic
2. **Telegram Gateway** — Bot handlers, webhook, FSM
3. **Extraction + Classification** — Firecrawl, Playwright, Gemini
4. **Tailoring + Compilation** — Claude, RenderCV, PDF
5. **HITL + Email + CRM + Tracking** — Gmail, pixel, CRM, approval flow

## Rules

- Read `project-context.md` fully before coding any phase
- Create every file specified for the phase
- Run verification after creating all files
- Fix issues before reporting completion
- Do not modify previous phase files unless fixing a bug
