"""
JobForge Application Settings

All configuration values are loaded from environment variables via pydantic-settings.
Never hardcode API keys, URLs, or secrets — always use settings.<FIELD>.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration for the entire JobForge application.
    All values are read from .env (or OS environment variables).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram ─────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_WEBHOOK_URL: str
    TELEGRAM_CHAT_ID: str

    # ── Database (MySQL) ─────────────────────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "jobforge"
    DB_PASSWORD: str = "password"
    DB_NAME: str = "jobforge_db"

    # ── AI Settings ──────────────────────────────────────────
    MINIMUM_FIT_THRESHOLD: int = 40
    MAX_OPTIMIZATION_PASSES: int = 2
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ── Redis ────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"

    # ── LLM APIs ─────────────────────────────────────────────
    GOOGLE_API_KEY: str

    # ── Web Scraping ─────────────────────────────────────────
    FIRECRAWL_API_KEY: str = ""  # Optional — falls back to Playwright if not set

    # ── SMTP Mailer ──────────────────────────────────────────
    SMTP_EMAIL: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_HOST_USER: str = ""
    EMAIL_HOST_PASSWORD: str = ""
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 465

    # ── Candidate Contact Details (used in email outreach) ───
    CANDIDATE_PHONE: str = ""
    CANDIDATE_EMAIL: str = ""
    CANDIDATE_LINKEDIN: str = ""
    CANDIDATE_GITHUB: str = ""

    # ── App ──────────────────────────────────────────────────
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_BASE_URL: str = "http://localhost:8000"
    LOG_LEVEL: str = "INFO"

    # ── Intelligence Features ────────────────────────────────
    SEMANTIC_MATCH_ENABLED: bool = True      # Embedding-based semantic fit scoring
    SEMANTIC_RESCUE_THRESHOLD: int = 65      # Semantic fit (cosine*100) that rescues a low keyword score
    EMBEDDING_MODEL: str = "gemini-embedding-001"
    STRATEGY_ADVISOR_ENABLED: bool = True    # Per-application strategy note at the HITL gate
    COMPANY_RESEARCH_ENABLED: bool = True    # Grounded company hook woven into cold emails
    DAILY_BRIEFING_ENABLED: bool = True      # Morning agent briefing on Telegram
    DAILY_BRIEFING_HOUR: int = 3             # UTC hour (3 UTC ≈ 8:30 IST)

    # ── Open Tracking (pixel) ────────────────────────────────
    # Mail providers / security scanners pre-fetch the tracking pixel when the
    # email is *delivered* (before any human opens it). Pixel hits that arrive
    # within this many seconds of sending are treated as prefetch and ignored,
    # so the "Email Opened" alert only fires on a genuine later open.
    PIXEL_OPEN_MIN_DELAY_SECONDS: int = 120

    # ── WhatsApp Outreach ────────────────────────────────────
    WHATSAPP_ENABLED: bool = True                # Offer WhatsApp outreach when a phone is detected
    WHATSAPP_DEFAULT_COUNTRY_CODE: str = "91"    # Prepended to numbers without a country code (India)

    # ── Follow-up & Digest ───────────────────────────────────
    FOLLOWUP_DAYS: int = 7           # Days before a non-opened email triggers a follow-up prompt
    WEEKLY_DIGEST_DAY: int = 6       # 0=Monday … 6=Sunday
    WEEKLY_DIGEST_HOUR: int = 9      # Hour (local server time) to fire the Sunday digest

    # ── Proactive Job Discovery ──────────────────────────────
    JOB_DISCOVERY_ENABLED: bool = False          # Master switch (opt-in)
    # Comma-separated RSS/Atom job-feed URLs. Examples (remote dev roles):
    #   https://weworkremotely.com/categories/remote-programming-jobs.rss
    #   https://remoteok.com/remote-jobs.rss
    JOB_DISCOVERY_SOURCES: str = ""
    JOB_DISCOVERY_KEYWORDS: str = ""             # Comma-separated; matched in title/summary. Empty = all
    JOB_DISCOVERY_INTERVAL_HOURS: int = 12       # How often to scan feeds
    JOB_DISCOVERY_MIN_ATS: int = 55              # Only surface roles scoring >= this
    JOB_DISCOVERY_MAX_PER_RUN: int = 5           # Max roles surfaced per scan (anti-spam)
    JOB_DISCOVERY_MAX_SCREEN: int = 10           # Max roles ATS-pre-screened per scan (cost cap)

    @property
    def DISCOVERY_SOURCE_LIST(self) -> list[str]:
        return [s.strip() for s in self.JOB_DISCOVERY_SOURCES.split(",") if s.strip()]

    @property
    def DISCOVERY_KEYWORD_LIST(self) -> list[str]:
        return [k.strip().lower() for k in self.JOB_DISCOVERY_KEYWORDS.split(",") if k.strip()]

    @property
    def ALL_GEMINI_KEYS(self) -> list[str]:
        import logging
        from pathlib import Path
        from dotenv import dotenv_values
        keys = []
        if self.GOOGLE_API_KEY:
            keys.append(self.GOOGLE_API_KEY)

        env_path = Path(".env")
        if not env_path.exists():
            logging.getLogger(__name__).warning(
                ".env file not found — only GOOGLE_API_KEY from environment will be used for key rotation"
            )
            return keys

        env_dict = dotenv_values(str(env_path))
        for key, val in env_dict.items():
            if key.upper().startswith("GEMINI_API_KEY_") and val:
                if val not in keys:
                    keys.append(val)
        return keys

    # ── Derived Properties ───────────────────────────────────

    @property
    def DATABASE_URL(self) -> str:
        return f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def database_url_sync(self) -> str:
        """Synchronous DB URL for Alembic CLI (uses PyMySQL instead of aiomysql)."""
        return self.DATABASE_URL.replace("mysql+aiomysql://", "mysql+pymysql://")

    @property
    def REDIS_BROKER_URL(self) -> str:
        return self.CELERY_BROKER_URL

    @property
    def tracker_base_url(self) -> str:
        """Base URL for tracking pixel endpoints."""
        return f"{self.APP_BASE_URL}/v1/tracker"


@lru_cache()
def get_settings() -> Settings:
    """
    Cached singleton for application settings.
    Call this function everywhere you need config values.
    """
    return Settings()


# Convenience alias used throughout the codebase
settings = get_settings()
