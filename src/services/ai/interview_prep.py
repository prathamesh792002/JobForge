"""
Interview Prep Service

When an application's status flips to 'interview', this generates a tailored
prep brief: a company snapshot (grounded with live Google Search), the most
likely interview questions derived from the JD, and STAR-format answer starters
mapped to the candidate's real experience.

Uses Gemini with Google Search grounding for up-to-date company facts, with a
graceful fallback to ungrounded generation if the grounding tool is unavailable.

Pipeline position: triggered by the 'Interview' status button (not part of the
main application pipeline).
"""

import logging

from google import genai
from google.genai import types as genai_types

from src.config.settings import settings

logger = logging.getLogger(__name__)

PREP_PROMPT = """You are an interview coach preparing a candidate for a specific interview.

CANDIDATE: Prathamesh Shirole — AI/ML engineer, currently an AI Intern at Infosys Ltd, past Data Science Intern at Siliconmount. Skilled in Generative AI, RAG, agentic workflows, Python, SQL, DSA. Flagship project: JobForge — an agentic AI job-application pipeline he architected end-to-end (Telegram bot, Gemini-powered self-healing resume tailoring, FastAPI, Redis, MySQL). Prefer JobForge when grounding STAR answers in a project.

TARGET ROLE: {job_title}
TARGET COMPANY: {company_name}

JOB DESCRIPTION (for question targeting):
{jd_text}

CANDIDATE RESUME (for STAR answer grounding):
{resume_text}

Using live web search where useful, produce a concise, practical interview prep brief.

Format the output as PLAIN TEXT (no markdown asterisks, no HTML tags). Use these exact emoji section headers and "- " for bullets:

🏢 COMPANY SNAPSHOT
- 3-5 bullets: what {company_name} does, recent news or funding, their tech stack, and culture signals. Use current, factual info.

❓ LIKELY QUESTIONS
- 6-8 questions this specific role is likely to ask, derived from the JD (mix technical + behavioral).

⭐ STAR ANSWER STARTERS
- For 3 of the most important questions, give a 1-2 line STAR starter that maps the candidate's ACTUAL experience/projects to the question. Do not invent experience.

💡 TIPS
- 2-3 tailored tips for this company/role.

Keep the whole brief under 3000 characters. Be specific, not generic."""


async def generate_interview_prep(
    job_title: str,
    company_name: str,
    jd_text: str,
    resume_text: str,
) -> str:
    """
    Generate a tailored interview prep brief as plain text.

    Tries Gemini with Google Search grounding first (for current company facts),
    then falls back to ungrounded generation, then to a minimal static brief.
    """
    prompt = PREP_PROMPT.format(
        job_title=job_title or "the role",
        company_name=company_name or "the company",
        jd_text=(jd_text or "")[:3000],
        resume_text=(resume_text or "")[:2500],
    )

    grounding_tool = genai_types.Tool(google_search=genai_types.GoogleSearch())

    # Two config variants: grounded (preferred) then plain.
    configs = [
        genai_types.GenerateContentConfig(temperature=0.4, tools=[grounding_tool]),
        genai_types.GenerateContentConfig(temperature=0.4),
    ]

    for cfg_idx, cfg in enumerate(configs):
        grounded = cfg_idx == 0
        for i, api_key in enumerate(settings.ALL_GEMINI_KEYS):
            try:
                client = genai.Client(api_key=api_key)
                response = await client.aio.models.generate_content(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config=cfg,
                )
                text = (response.text or "").strip()
                if text:
                    logger.info(
                        "Interview prep generated (key #%d, grounded=%s).", i + 1, grounded
                    )
                    return text
            except Exception as e:
                logger.warning(
                    "Interview prep key #%d (grounded=%s) failed: %s",
                    i + 1, grounded, str(e),
                )

    logger.error("All attempts to generate interview prep failed — returning fallback.")
    return (
        f"🏢 COMPANY SNAPSHOT\n"
        f"- Research {company_name or 'the company'} manually (site, LinkedIn, recent news).\n\n"
        f"❓ LIKELY QUESTIONS\n"
        f"- Walk me through your most relevant project for this role.\n"
        f"- How would you approach the core responsibilities in this JD?\n"
        f"- Tell me about a time you overcame a technical challenge.\n\n"
        f"⭐ STAR ANSWER STARTERS\n"
        f"- Use the Situation-Task-Action-Result structure with your Infosys / project work.\n\n"
        f"💡 TIPS\n"
        f"- Prep 2-3 concrete stories and questions to ask them.\n\n"
        f"(AI prep generation was unavailable — this is a generic checklist.)"
    )
