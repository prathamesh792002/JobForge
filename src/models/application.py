"""
Application ORM Model

Maps to the `applications` table in MySQL. Tracks every job application
through the pipeline: ingestion → tailoring → compilation → outreach → tracking.
UUIDs are stored as CHAR(36) by SQLAlchemy's generic Uuid type on MySQL.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.types import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Application(Base):
    """
    Represents a single job application processed through the JobForge pipeline.

    Lifecycle:
        1. Created after HITL approval (status='pending')
        2. Updated to 'applied' when email is sent successfully
        3. Updated to 'failed' if email send fails
        4. Updated to 'opened' when tracking pixel fires
        5. Manually updated to 'interview' or 'rejected' via bot inline buttons
    """

    __tablename__ = "applications"

    # ── Primary Key ──────────────────────────────────────────
    app_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Tracking Pixel ID ────────────────────────────────────
    pixel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid.uuid4,
    )

    # ── Timestamps ───────────────────────────────────────────
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ── Job Details ──────────────────────────────────────────
    company_name: Mapped[Optional[str]] = mapped_column(
        String(150), nullable=True
    )
    job_title: Mapped[Optional[str]] = mapped_column(
        String(150), nullable=True
    )
    persona_used: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, comment="AI_ML | SWE | DATA"
    )
    hr_email: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    # ── Application Status ───────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="applied",
        server_default="applied",
        comment="applied | viewed | interview | offer_received | offer_accepted | ghosted | rejected",
    )

    # ── Source Information ───────────────────────────────────
    source_type: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, comment="link | image"
    )
    source_url: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    # ── Pipeline Artifacts ───────────────────────────────────
    pdf_path: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    jd_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    tailoring_skipped: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )

    # ── Email Details ────────────────────────────────────────
    email_subject: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    email_body: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    # ── ATS Scores ───────────────────────────────────────────
    initial_ats_score: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="ATS score before tailoring (0-100)"
    )
    final_ats_score: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="ATS score after tailoring (0-100)"
    )

    # ── Tracking ─────────────────────────────────────────────
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Enhanced CRM Fields ───────────────────────────────────
    missing_skills: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="JSON array of skills missing at tailoring time"
    )
    demanded_skills: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON of the FULL tiered skill set the JD demanded (market-demand signal)",
    )
    salary_offered: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    followup_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Audit Timestamps ─────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Indexes ──────────────────────────────────────────────
    __table_args__ = (
        Index("idx_applications_status", "status"),
        Index("idx_applications_pixel_id", "pixel_id"),
        Index("idx_applications_timestamp", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<Application(app_id={self.app_id!r}, "
            f"company={self.company_name!r}, "
            f"status={self.status!r})>"
        )
