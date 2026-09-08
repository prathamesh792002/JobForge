"""
Shared async Redis client for JobForge.

Single connection pool shared across all services (application_flow,
classifier, etc.) to avoid duplicate connections and inconsistent state.
"""

import logging

import redis.asyncio as aioredis

from src.config import settings

logger = logging.getLogger(__name__)

_redis_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Get or create the shared async Redis client singleton."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        logger.debug("Redis client initialized: %s", settings.REDIS_URL)
    return _redis_client


async def close_redis() -> None:
    """Close the shared Redis client. Called during application shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis client closed.")
