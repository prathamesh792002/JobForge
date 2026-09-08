"""
Intelligence Layer — Learning Loop, Career Coach, Strategy Advisor, Daily Briefing

Turns the CRM's outcome data (opens, interviews, rejections, missing skills)
back into actionable guidance:

  - generate_insights_text : deterministic outcome analysis (/insights command
    + weekly digest). What's converting, what isn't, with honest sample sizes.
  - generate_coach_plan    : aggregates missing skills across ALL applications
    into a Gemini-drafted learning roadmap (/coach command).
  - advise_strategy        : one-line per-application strategy note shown at the
    HITL gate (the "agent reasoning" layer).
  - compose_daily_briefing : deterministic morning briefing (background task).

Deterministic wherever possible — LLM calls only where language generation
actually adds value (coach plan, strategy note).
"""

import html as _html
import json as _json
import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings

logger = logging.getLogger(__name__)

_PERSONA_LABEL = {"ai_ml": "AI/ML", "swe": "SWE", "game_dev": "Game Dev"}


# ══════════════════════════════════════════════════════════════
#  Learning loop (deterministic)
# ══════════════════════════════════════════════════════════════

async def compute_outcome_stats(db) -> Dict[str, Any]:
    """Aggregate per-persona and per-ATS-band conversion stats from the CRM."""
    from sqlalchemy import select
    from src.models.application import Application

    result = await db.execute(select(Application))
    apps: List[Application] = list(result.scalars().all())

    sent = [a for a in apps if a.status not in ("pending", "failed")]

    def _rate(n: int, d: int) -> int:
        return round(n / d * 100) if d else 0

    # Per-persona funnel
    personas: Dict[str, Dict[str, int]] = {}
    for a in sent:
        p = a.persona_used or "unknown"
        d = personas.setdefault(p, {"sent": 0, "opened": 0, "interviews": 0, "rejected": 0})
        d["sent"] += 1
        if a.opened_at:
            d["opened"] += 1
        if a.status in ("interview", "offer_received", "offer_accepted"):
            d["interviews"] += 1
        if a.status == "rejected":
            d["rejected"] += 1

    # ATS-band → open rate (does a better-tailored resume get opened more?)
    bands: Dict[str, Dict[str, int]] = {}
    for a in sent:
        if a.final_ats_score is None:
            continue
        band = "80-100%" if a.final_ats_score >= 80 else ("60-79%" if a.final_ats_score >= 60 else "under 60%")
        d = bands.setdefault(band, {"sent": 0, "opened": 0})
        d["sent"] += 1
        if a.opened_at:
            d["opened"] += 1

    # Missing-skill analysis — deduped per application (a skill counts once per
    # role, not once per mention), split by persona and weighted by outcome.
    skill_roles: Counter = Counter()          # apps that REQUIRED (were missing) this skill
    skill_cost: Counter = Counter()           # of those, how many rejected/ghosted (non-converting)
    skill_win: Counter = Counter()            # of those, how many reached interview+
    missing_by_persona: Dict[str, Counter] = {}

    for a in apps:
        if not a.missing_skills:
            continue
        try:
            skills = _json.loads(a.missing_skills)
        except Exception:
            continue
        if not isinstance(skills, list):
            continue
        normed = {s.lower().strip() for s in skills if isinstance(s, str) and s.strip()}
        if not normed:
            continue
        p = a.persona_used or "unknown"
        mp = missing_by_persona.setdefault(p, Counter())
        non_converting = a.status in ("rejected", "ghosted")
        converting = a.status in ("interview", "offer_received", "offer_accepted")
        for sk in normed:
            skill_roles[sk] += 1
            mp[sk] += 1
            if non_converting:
                skill_cost[sk] += 1
            elif converting:
                skill_win[sk] += 1

    # Leverage: skills ranked by how many of your target roles they'd unlock,
    # annotated with how many of those roles rejected/ghosted you.
    skill_leverage = [
        {"skill": sk, "roles": cnt, "cost": skill_cost.get(sk, 0), "win": skill_win.get(sk, 0)}
        for sk, cnt in skill_roles.most_common(20)
    ]

    return {
        "total": len(apps),
        "sent": len(sent),
        "opened": sum(1 for a in sent if a.opened_at),
        "interviews": sum(1 for a in sent if a.status in ("interview", "offer_received", "offer_accepted")),
        "personas": personas,
        "ats_bands": bands,
        "missing_skill_counts": skill_roles.most_common(15),
        "missing_by_persona": {p: c.most_common(8) for p, c in missing_by_persona.items()},
        "skill_leverage": skill_leverage,
        "rate": _rate,
    }


def _existing_skills_by_persona() -> Dict[str, str]:
    """Flatten each persona resume's technical_skills so the coach knows what the
    candidate ALREADY has (keeps advice realistic and adjacency-based)."""
    import yaml as _yaml
    from src.services.ai.classifier import get_yaml_by_persona

    out: Dict[str, str] = {}
    for p in ("ai_ml", "swe", "game_dev"):
        try:
            data = _yaml.safe_load(get_yaml_by_persona(p)) or {}
            skills = data.get("cv", {}).get("sections", {}).get("technical_skills", []) or []
            flat = [
                str(item.get("details", "")).strip()
                for item in skills
                if isinstance(item, dict) and item.get("details")
            ]
            out[p] = "; ".join(f for f in flat if f)
        except Exception:
            out[p] = ""
    return out


# ══════════════════════════════════════════════════════════════
#  Market-demand engine (100% deterministic — no LLM in the counting)
# ══════════════════════════════════════════════════════════════

# Canonical aliases so variants collapse to one skill (accuracy of the counts).
_SKILL_ALIASES = {
    "js": "javascript", "ts": "typescript", "reactjs": "react", "react.js": "react",
    "nodejs": "node.js", "node": "node.js", "k8s": "kubernetes",
    "postgres": "postgresql", "psql": "postgresql", "gcp": "google cloud",
    "aws cloud": "aws", "amazon web services": "aws", "tf": "tensorflow",
    "sklearn": "scikit-learn", "ml": "machine learning", "dl": "deep learning",
    "nlp": "natural language processing", "genai": "generative ai",
    "llms": "llm", "large language models": "llm", "restful api": "rest api",
    "restful apis": "rest api", "rest apis": "rest api", "ci cd": "ci/cd",
    "golang": "go", "c sharp": "c#", "dsa": "data structures and algorithms",
}
_TIER_LABEL = {
    "tier_1_languages": "Languages",
    "tier_2_frameworks_infra": "Frameworks / Infra",
    "tier_3_methodologies": "Tools / Methods",
}


def _norm_skill(s: str) -> str:
    s = (s or "").strip().lower()
    return _SKILL_ALIASES.get(s, s)


def _resume_skill_set(persona: str) -> set:
    """Normalized set of skill tokens the candidate's persona resume already lists."""
    import yaml as _yaml
    from src.services.ai.classifier import get_yaml_by_persona

    toks: set = set()
    try:
        data = _yaml.safe_load(get_yaml_by_persona(persona)) or {}
        skills = data.get("cv", {}).get("sections", {}).get("technical_skills", []) or []
        for item in skills:
            if not isinstance(item, dict):
                continue
            for piece in str(item.get("details", "")).split(","):
                n = _norm_skill(piece)
                if n:
                    toks.add(n)
            lab = _norm_skill(str(item.get("label", "")))
            if lab:
                toks.add(lab)
    except Exception:
        pass
    return toks


def _candidate_has(skill: str, resume_set: set) -> bool:
    """Robust match: exact normalized, or substring either way (len>=3 guard)."""
    n = _norm_skill(skill)
    if n in resume_set:
        return True
    for rt in resume_set:
        if len(n) >= 3 and len(rt) >= 3 and (n in rt or rt in n):
            return True
    return False


async def compute_demand(db, window_days: Optional[int] = 7) -> Dict[str, Any]:
    """
    Deterministic market-demand aggregation from stored JD skill sets.
    Per persona: how many JDs demanded each skill (deduped per JD), the JD count,
    each skill's tier, and whether the candidate already has it.
    NO LLM is involved — the counts are exact and reproducible.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from src.models.application import Application

    result = await db.execute(
        select(Application).where(Application.demanded_skills.isnot(None))
    )
    apps = list(result.scalars().all())

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _in_window(a) -> bool:
        if window_days is None:
            return True
        return bool(a.created_at) and a.created_at >= (now - timedelta(days=window_days))

    windowed = [a for a in apps if _in_window(a)]

    per_persona: Dict[str, Dict[str, Any]] = {}
    for a in windowed:
        try:
            tiers = _json.loads(a.demanded_skills)
        except Exception:
            continue
        if not isinstance(tiers, dict):
            continue
        p = a.persona_used or "unknown"
        entry = per_persona.setdefault(p, {"jd_count": 0, "demand": Counter(), "tier": {}})
        entry["jd_count"] += 1
        seen = set()
        for tier_name in ("tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies"):
            for sk in (tiers.get(tier_name) or []):
                n = _norm_skill(sk)
                if not n or n in seen:
                    continue
                seen.add(n)
                entry["demand"][n] += 1
                entry["tier"].setdefault(n, tier_name)

    return {
        "window_days": window_days,
        "total_jds": len(windowed),
        "have_data": len(apps) > 0,
        "per_persona": per_persona,
    }


async def generate_demand_report(db, window_days: Optional[int] = 7) -> str:
    """
    Weekly market-demand report (HTML). Deterministic counts, cross-referenced
    against the candidate's resume to surface precise upskilling priorities.
    """
    d = await compute_demand(db, window_days=window_days)

    label = f"last {window_days} days" if window_days else "all-time"
    if not d["have_data"]:
        return (
            f"📈 <b>Market-Demand Report — {label}</b>\n\n"
            "No JD skill data captured yet. This report fills in as you send jobs "
            "through the bot (each posting's full skill demand is now recorded)."
        )
    if d["total_jds"] == 0:
        return (
            f"📈 <b>Market-Demand Report — {label}</b>\n\n"
            f"No applications in the {label} window. Try /demand for the all-time view."
        )

    lines = [
        f"📈 <b>Market-Demand Report — {label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<i>Based on {d['total_jds']} job description(s). Counts are exact "
        "(deterministic — no AI in the numbers).</i>",
    ]

    # Persona order: most JDs first
    for persona, e in sorted(d["per_persona"].items(), key=lambda kv: -kv[1]["jd_count"]):
        jd_count = e["jd_count"]
        if jd_count == 0:
            continue
        resume_set = _resume_skill_set(persona)
        ranked = e["demand"].most_common(12)

        lines.append(f"\n<b>{_PERSONA_LABEL.get(persona, persona)}</b> — {jd_count} JD(s)")
        lines.append("<u>Most in-demand</u>:")
        priorities: List[str] = []
        for skill, cnt in ranked[:8]:
            pct = round(cnt / jd_count * 100)
            has = _candidate_has(skill, resume_set)
            mark = "✅" if has else "❌ <b>gap</b>"
            lines.append(f"  • {_html.escape(skill)} — {cnt}/{jd_count} ({pct}%) {mark}")
            if not has:
                priorities.append(skill)

        if priorities:
            lines.append(
                "  🎯 <b>Learn next</b> (high-demand & missing): "
                + ", ".join(_html.escape(s) for s in priorities[:4])
            )
        else:
            lines.append("  🎉 You already cover the top demanded skills for this persona.")

    if window_days and d["total_jds"] < 4:
        lines.append(
            f"\n<i>⚠️ Only {d['total_jds']} JD(s) this window — demand is noisy at this "
            "size. Use /demand all for the all-time picture.</i>"
        )
    return "\n".join(lines)


async def generate_insights_text(db) -> str:
    """Deterministic outcome-analysis report (HTML for Telegram)."""
    s = await compute_outcome_stats(db)
    rate = s["rate"]

    lines = [
        "🧠 <b>JobForge Insights — what's working</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📨 {s['sent']} sent • 👀 {s['opened']} opened ({rate(s['opened'], s['sent'])}%) "
        f"• 🎯 {s['interviews']} interview(s) ({rate(s['interviews'], s['sent'])}%)",
    ]

    if s["personas"]:
        lines.append("\n<b>By persona</b>  <i>(interview rate is the honest signal)</i>")
        ranked = sorted(
            s["personas"].items(),
            key=lambda kv: (rate(kv[1]["interviews"], kv[1]["sent"]), rate(kv[1]["opened"], kv[1]["sent"])),
            reverse=True,
        )
        for p, d in ranked:
            miss = s["missing_by_persona"].get(p, [])
            gap = ("  ·  gaps: " + ", ".join(_html.escape(k) for k, _ in miss[:3])) if miss else ""
            lines.append(
                f"  • <b>{_PERSONA_LABEL.get(p, p)}</b>: {d['sent']} sent → "
                f"{rate(d['interviews'], d['sent'])}% interview, {rate(d['opened'], d['sent'])}% opened{gap}"
            )

        # Honest focus recommendation: needs a real sample AND a real difference.
        best, bd = ranked[0]
        best_iv = rate(bd["interviews"], bd["sent"])
        if len(ranked) > 1 and bd["sent"] >= 5 and best_iv > 0:
            lines.append(
                f"\n💡 <b>Focus signal:</b> {_PERSONA_LABEL.get(best, best)} is your strongest "
                f"({best_iv}% interview over {bd['sent']} apps). Weight your search there while it holds."
            )
        elif s["sent"] >= 5:
            lines.append(
                "\n💡 <b>Focus signal:</b> not enough interview outcomes yet to crown a persona — "
                "the gaps above are the clearer lever for now."
            )

    if s["ats_bands"]:
        lines.append("\n<b>Open rate by final ATS score</b>")
        for band in ("80-100%", "60-79%", "under 60%"):
            if band in s["ats_bands"]:
                d = s["ats_bands"][band]
                lines.append(f"  • ATS {band}: {rate(d['opened'], d['sent'])}% opened ({d['sent']} sent)")

    # Skill leverage — factual "unlocks N of your roles", flagged when it's also costing you.
    if s["skill_leverage"]:
        lines.append("\n<b>Highest-leverage skill gaps</b>  <i>(by roles they'd unlock)</i>")
        for item in s["skill_leverage"][:5]:
            cost = f" — {item['cost']} of those rejected/ghosted you" if item["cost"] else ""
            lines.append(
                f"  • <b>{_html.escape(item['skill'])}</b>: required by {item['roles']} of "
                f"your {s['sent']} roles{cost}"
            )
        lines.append("Run /coach for a persona-specific learning plan.")

    if s["sent"] < 10:
        lines.append(
            f"\n<i>⚠️ Small sample ({s['sent']} sent) — these are directional signals, not "
            "conclusions. Correlation ≠ causation; tap the status buttons when you hear back so "
            "the data sharpens.</i>"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  Career coach (LLM)
# ══════════════════════════════════════════════════════════════

_COACH_PROMPT = """You are a pragmatic, honest, and constructive career coach for Prathamesh
Shirole (AI/ML engineer, AI Intern at Infosys). Flagship project: JobForge (agentic AI job
pipeline). Be direct and never give false confidence or sugarcoat weak data, but stay
professional, respectful, and encouraging — critique the STRATEGY and the DATA, never the
person. No insults, no profanity.

You have REAL data from his application tracker — use it, do not invent.

SKILLS HE ALREADY HAS, per target-role type:
{existing_skills}

SKILL GAPS BY ROLE TYPE (skills the JD required that his resume lacked; "xN" = number of
that persona's applications where it was missing):
{gaps_by_persona}

OUTCOME-WEIGHTED LEVERAGE (across all applications) — "roles" = how many of his target
roles required the skill; "cost" = how many of those rejected or ghosted him:
{leverage}

MARKET DEMAND (deterministic count — skills the market ASKED FOR that he currently LACKS,
per role type, with how many JDs demanded each). This is the strongest upskilling signal:
{market_demand}

Track record: {sent} applications sent, {interviews} reached interview.

Write an honest, persona-segmented skill-gap action plan:
1. State which role type (AI/ML vs SWE vs Game Dev) his data suggests he should focus on,
   and say plainly if the sample is too small to be sure (be honest, no false confidence).
2. Pick the 2-3 highest-priority gaps, driven FIRST by MARKET DEMAND (how many JDs asked
   for it), then weighed by leverage/cost and adjacency to skills he ALREADY has. A quick,
   honest win (high-demand skill adjacent to his stack, learnable in 1-2 weeks) beats a rare
   or prestige skill. Give a concrete build-this project for each — not "take a course".
3. Clearly separate QUICK WINS (learnable in weeks, adjacent to his stack) from LONG-HORIZON
   gaps (need real experience — be honest that these won't be faked on a resume).
4. One line on what NOT to chase yet, and why.

Rules: only recommend skills he can genuinely and honestly claim after learning — never
advise padding the resume with skills he lacks. Plain text, no markdown symbols or emojis,
under 280 words."""


async def generate_coach_plan(db) -> str:
    """Persona-aware, outcome-weighted learning roadmap grounded in real data."""
    s = await compute_outcome_stats(db)
    if not s["skill_leverage"]:
        return (
            "🎉 No recurring missing skills in your tracked applications — "
            "your resume is covering what your target roles ask for. "
            "Keep applying and tap the status buttons so I can spot patterns as they emerge."
        )

    existing = _existing_skills_by_persona()
    existing_str = "\n".join(
        f"- {_PERSONA_LABEL.get(p, p)}: {sk or '(none on file)'}" for p, sk in existing.items()
    )
    gaps_str = "\n".join(
        f"- {_PERSONA_LABEL.get(p, p)}: " + ", ".join(f"{k} x{v}" for k, v in miss)
        for p, miss in s["missing_by_persona"].items() if miss
    ) or "(no per-persona gaps recorded yet)"
    leverage_str = "\n".join(
        f"- {it['skill']}: roles={it['roles']}, cost={it['cost']}" for it in s["skill_leverage"][:10]
    )

    # Market demand (all-time) — high-demand skills he LACKS, per persona.
    demand = await compute_demand(db, window_days=None)
    demand_lines = []
    for persona, e in sorted(demand["per_persona"].items(), key=lambda kv: -kv[1]["jd_count"]):
        rs = _resume_skill_set(persona)
        gaps = [(sk, cnt) for sk, cnt in e["demand"].most_common(30) if not _candidate_has(sk, rs)]
        if gaps:
            demand_lines.append(
                f"- {_PERSONA_LABEL.get(persona, persona)} ({e['jd_count']} JDs): "
                + ", ".join(f"{sk} in {cnt}/{e['jd_count']} JDs" for sk, cnt in gaps[:6])
            )
    demand_str = "\n".join(demand_lines) or "(no market-demand data captured yet)"

    prompt = _COACH_PROMPT.format(
        existing_skills=existing_str,
        gaps_by_persona=gaps_str,
        leverage=leverage_str,
        market_demand=demand_str,
        sent=s["sent"], interviews=s["interviews"],
    )

    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            resp = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.4, max_output_tokens=900,
                    # Disable thinking: on 2.5 models thinking tokens count against
                    # max_output_tokens and can truncate the visible answer.
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text = (resp.text or "").strip()
            if text:
                return "🎓 Skill-Gap Roadmap\n━━━━━━━━━━━━━━━━━━━━\n\n" + text
        except Exception as e:
            logger.warning("coach plan key #%d failed: %s", i + 1, str(e)[:120])

    # Deterministic fallback
    top = "\n".join(f"  • {k} — missing in {v} application(s)" for k, v in s["missing_skill_counts"][:5])
    return (
        "🎓 Skill-Gap Roadmap (AI drafting unavailable — raw data)\n\n"
        f"Most-demanded skills you're missing:\n{top}\n\n"
        "Start with whichever is closest to your current stack."
    )


# ══════════════════════════════════════════════════════════════
#  Strategy advisor (LLM, per-application, at the HITL gate)
# ══════════════════════════════════════════════════════════════

_STRATEGY_PROMPT = """You are advising a job candidate on ONE application, in ONE sentence.

Role: {job_title} at {company_name}
Keyword ATS: started {initial}%, tailored to {final}%
Skills still missing after tailoring: {missing}
Candidate: AI/ML engineer (AI Intern at Infosys; GenAI, RAG, Python, SQL). Flagship
project: JobForge — an agentic AI job-application pipeline he built end-to-end (Telegram
bot, Gemini self-healing resume loop, FastAPI/Redis/MySQL). When advising what to lead
with, prefer JobForge.

Give ONE practical sentence of strategy for THIS application — e.g. is it a strong
match to prioritize, a stretch worth a tailored note, or should the email lead with a
specific project? Be specific to the data. No emojis, no preamble, max 30 words."""


async def advise_strategy(
    job_title: str, company_name: str, initial: int, final: int, missing: List[str]
) -> Optional[str]:
    """One-line per-application strategy note. Returns None on failure (caller skips it)."""
    prompt = _STRATEGY_PROMPT.format(
        job_title=job_title or "the role",
        company_name=company_name or "the company",
        initial=initial, final=final,
        missing=", ".join(missing[:8]) or "none",
    )
    for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            resp = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.3, max_output_tokens=80,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text = (resp.text or "").strip().strip('"')
            if text:
                return text
        except Exception as e:
            logger.warning("advise_strategy key #%d failed: %s", i + 1, str(e)[:120])
    return None


# ══════════════════════════════════════════════════════════════
#  Daily briefing (deterministic)
# ══════════════════════════════════════════════════════════════

async def compose_daily_briefing(db, redis) -> Optional[str]:
    """
    Morning briefing: active discovery leads, follow-ups due, live interviews,
    week-so-far funnel. Returns None when there is genuinely nothing to say
    (so the task can skip the message instead of spamming).
    """
    from sqlalchemy import select
    from src.models.application import Application
    from src.services.repository.application_repo import (
        find_applications_needing_followup, get_full_stats,
    )

    followups = await find_applications_needing_followup(db, days=settings.FOLLOWUP_DAYS)

    result = await db.execute(
        select(Application).where(Application.status == "interview")
    )
    interviews = list(result.scalars().all())

    # Active (unactioned) discovery leads
    lead_count = 0
    try:
        from src.services.ai.discovery import LEAD_PREFIX
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"{LEAD_PREFIX}*", count=100)
            lead_count += len(keys)
            if cursor == 0:
                break
    except Exception as e:
        logger.debug("Briefing lead scan failed: %s", str(e))

    stats = await get_full_stats(db)

    if not followups and not interviews and lead_count == 0 and stats["sent"] == 0:
        return None  # nothing worth a message today

    lines = ["☀️ <b>Good morning — your JobForge briefing</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    if lead_count:
        lines.append(f"🔎 {lead_count} discovered role(s) awaiting your decision")
    if followups:
        names = ", ".join(_html.escape(a.company_name or "Unknown") for a in followups[:3])
        more = f" +{len(followups) - 3} more" if len(followups) > 3 else ""
        lines.append(f"📤 {len(followups)} follow-up(s) due: {names}{more}")
    if interviews:
        names = ", ".join(_html.escape(a.company_name or "Unknown") for a in interviews[:3])
        lines.append(f"🎯 Interview stage: {names}")
    lines.append(
        f"📊 Last 7 days: {stats['sent']} sent • {stats['opened']} opened "
        f"({stats['open_rate']}%) • {stats['interviews']} interview(s)"
    )
    lines.append("\n<i>/insights for analysis • /coach for your skill roadmap</i>")
    return "\n".join(lines)
