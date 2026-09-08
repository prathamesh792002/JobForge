import asyncio
from src.database.session import engine, Base
import src.models.application  # import models so they are registered

async def init_models():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("MySQL tables created successfully!")

if __name__ == "__main__":
    asyncio.run(init_models())
