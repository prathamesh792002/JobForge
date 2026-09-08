"""
Application Pydantic Schemas

Input/output models for application data and the extraction pipeline.
"""

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ExtractionResult(BaseModel):
    """
    Unified output from both URL scraping (extractor.py) and
    image processing (vision.py). Every extraction path must
    produce an instance of this model.
    """

    raw_jd_text: str = Field(
        ..., description="Complete job description text"
    )
    job_title: Optional[str] = Field(
        None, description="Extracted job title"
    )
    company_name: Optional[str] = Field(
        None, description="Extracted company name"
    )
    hr_email: Optional[str] = Field(
        None, description="HR contact email if found"
    )
    contact_phone: Optional[str] = Field(
        None, description="Recruiter contact phone (normalized, digits only) if found"
    )
    application_link: Optional[str] = Field(
        None, description="Direct application URL if found"
    )
    required_skills: List[str] = Field(
        default_factory=list, description="List of required skills"
    )
    source_type: Literal["link", "image", "raw_text"] = Field(
        ..., description="How the JD was submitted"
    )
    word_count: int = Field(
        ..., description="Word count of raw_jd_text for sparse JD gate"
    )


class ApplicationCreate(BaseModel):
    """Input schema for creating a new application record in the CRM."""

    company_name: Optional[str] = None
    job_title: Optional[str] = None
    persona_used: Optional[str] = None
    hr_email: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    pdf_path: Optional[str] = None
    jd_text: Optional[str] = None
    tailoring_skipped: bool = False
    initial_ats_score: Optional[int] = None
    final_ats_score: Optional[int] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    status: Optional[str] = "applied"
    missing_skills: Optional[str] = None
    demanded_skills: Optional[str] = None
    salary_offered: Optional[str] = None
    notes: Optional[str] = None
    # Transport-only (carried in the Redis pending payload, not written to DB):
    contact_phone: Optional[str] = None      # Normalized recruiter phone for WhatsApp outreach
    whatsapp_message: Optional[str] = None   # Pre-drafted WhatsApp intro text
    # When set, the ORM uses this pixel_id instead of generating a new one.
    # Required for the Gmail draft flow so the tracking pixel injected into
    # the draft email matches the CRM record.
    pixel_id: Optional[uuid.UUID] = None


class ApplicationRead(BaseModel):
    """Output schema for reading application records from the CRM."""

    model_config = {"from_attributes": True}

    app_id: uuid.UUID
    pixel_id: uuid.UUID
    timestamp: datetime
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    persona_used: Optional[str] = None
    hr_email: Optional[str] = None
    status: str
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    pdf_path: Optional[str] = None
    jd_text: Optional[str] = None
    tailoring_skipped: bool = False
    initial_ats_score: Optional[int] = None
    final_ats_score: Optional[int] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    opened_at: Optional[datetime] = None
    missing_skills: Optional[str] = None
    demanded_skills: Optional[str] = None
    salary_offered: Optional[str] = None
    notes: Optional[str] = None
    followup_sent_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
