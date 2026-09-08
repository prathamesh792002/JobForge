"""Add missing ATS score columns to applications table."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from src.database.session import async_session_factory

async def main():
    async with async_session_factory() as s:
        r = await s.execute(text("DESCRIBE applications"))
        cols = [row[0] for row in r]
        print("Existing columns:", cols)

        if "initial_ats_score" not in cols:
            await s.execute(text("ALTER TABLE applications ADD COLUMN initial_ats_score INT NULL"))
            print("Added: initial_ats_score")
        else:
            print("Already exists: initial_ats_score")

        if "final_ats_score" not in cols:
            await s.execute(text("ALTER TABLE applications ADD COLUMN final_ats_score INT NULL"))
            print("Added: final_ats_score")
        else:
            print("Already exists: final_ats_score")

        await s.commit()
        print("Schema updated.")

asyncio.run(main())
