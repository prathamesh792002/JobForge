"""
One-time backfill for the `demanded_skills` column.

Applications created before the market-demand feature existed have no
demanded_skills. This re-extracts the full tiered skill set from each
application's stored jd_text and saves it, so /demand covers historical data.

Future applications populate demanded_skills automatically in the pipeline —
this script is only for the historical catch-up. Safe to re-run (it skips rows
that already have data).

Usage:
    python scripts/backfill_demanded_skills.py
"""

import asyncio
import json
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


async def main() -> None:
    from sqlalchemy import select
    from src.database.session import async_session_factory
    from src.models.application import Application
    from src.services.ai.classifier import extract_categorized_skills

    _TIERS = ("tier_1_languages", "tier_2_frameworks_infra", "tier_3_methodologies")

    async with async_session_factory() as db:
        rows = (await db.execute(
            select(Application)
            .where(Application.demanded_skills.is_(None))
            .where(Application.jd_text.isnot(None))
        )).scalars().all()

        total = len(rows)
        print(f"Found {total} application(s) needing backfill.")
        if not total:
            print("Nothing to do — every application already has demand data.")
            return

        filled, skipped = 0, 0
        for i, app in enumerate(rows, 1):
            jd = app.jd_text or ""
            if len(jd.split()) < 20:
                skipped += 1
                print(f"  [{i}/{total}] skip (JD too short): {app.company_name or app.app_id}")
                continue
            try:
                skills = await extract_categorized_skills(jd, cache_key_prefix="jd")
            except Exception as e:
                skipped += 1
                print(f"  [{i}/{total}] FAILED {app.company_name or app.app_id}: {str(e)[:80]}")
                continue

            if any(skills.get(t) for t in _TIERS):
                app.demanded_skills = json.dumps(skills)
                filled += 1
                n = sum(len(skills.get(t, [])) for t in _TIERS)
                print(f"  [{i}/{total}] {app.company_name or 'Unknown'} -> {n} skills")
            else:
                skipped += 1
                print(f"  [{i}/{total}] skip (no skills extracted): {app.company_name or app.app_id}")

            await db.commit()
            # Gentle pacing for the Gemini free-tier rate limit.
            await asyncio.sleep(3)

        print(f"\nDone. Backfilled {filled}, skipped {skipped}, of {total}.")


if __name__ == "__main__":
    asyncio.run(main())
