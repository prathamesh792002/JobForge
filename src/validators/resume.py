"""
Resume Pydantic Schemas

Models for structured resume data used in the tailoring pipeline.
These map to the YAML structure expected by RenderCV.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class EducationEntry(BaseModel):
    """A single education entry in the resume."""

    institution: str = Field(..., description="Name of the educational institution")
    area: str = Field(..., description="Field of study / major")
    degree: str = Field(..., description="Degree type (e.g., Bachelor of Science)")
    start_date: str = Field(..., description="Start date (YYYY-MM format)")
    end_date: Optional[str] = Field(None, description="End date or 'present'")
    gpa: Optional[str] = Field(None, description="GPA if applicable")
    highlights: List[str] = Field(
        default_factory=list, description="Notable achievements or coursework"
    )


class ExperienceEntry(BaseModel):
    """A single work experience entry in the resume."""

    company: str = Field(..., description="Company name — NEVER modify this")
    position: str = Field(..., description="Job title — NEVER modify this")
    start_date: str = Field(..., description="Start date — NEVER modify this")
    end_date: Optional[str] = Field(None, description="End date — NEVER modify this")
    location: Optional[str] = Field(None, description="Work location")
    highlights: List[str] = Field(
        default_factory=list,
        description="Bullet points — ONLY these may be modified during tailoring",
    )


class ProjectEntry(BaseModel):
    """A single project entry in the resume."""

    name: str = Field(..., description="Project name")
    date: Optional[str] = Field(None, description="Project date or date range")
    url: Optional[str] = Field(None, description="Project URL if applicable")
    highlights: List[str] = Field(
        default_factory=list, description="Project description bullets"
    )


class SkillCategory(BaseModel):
    """A category of skills (e.g., 'Languages', 'Frameworks')."""

    label: str = Field(..., description="Category name")
    details: str = Field(..., description="Comma-separated list of skills")


class ResumeData(BaseModel):
    """
    Complete resume data model matching RenderCV YAML schema.
    Used to validate and transform resume YAML files during tailoring.
    """

    name: str = Field(..., description="Candidate full name")
    label: Optional[str] = Field(None, description="One-line professional summary")
    email: Optional[str] = Field(None, description="Contact email")
    phone: Optional[str] = Field(None, description="Contact phone")
    location: Optional[str] = Field(None, description="Location")
    website: Optional[str] = Field(None, description="Portfolio or LinkedIn URL")
    summary: Optional[str] = Field(None, description="Professional summary paragraph")

    education: List[EducationEntry] = Field(
        default_factory=list, description="Education entries"
    )
    experience: List[ExperienceEntry] = Field(
        default_factory=list, description="Work experience entries"
    )
    projects: List[ProjectEntry] = Field(
        default_factory=list, description="Project entries"
    )
    skills: List[SkillCategory] = Field(
        default_factory=list, description="Skill categories"
    )
