"""Strict Pydantic V2 schemas for BeaconAI data contracts."""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, HttpUrl


class EvaluationStatus(str, Enum):
    """Evaluation decision status."""
    MATCH = "MATCH"
    REJECT = "REJECT"
    DEFERRED = "DEFERRED"



class UserConstraints(BaseModel):
    """Declarative constraints and non-negotiables for job filtering."""
    min_hourly_rate: float = Field(
        ...,
        description="Minimum acceptable gross hourly rate in USD.",
        ge=0.0,
    )
    min_annual_salary: Optional[float] = Field(
        default=None,
        description="Minimum acceptable annual salary in USD.",
        ge=0.0,
    )
    max_commute_miles: int = Field(
        default=25,
        description="Maximum acceptable one-way commute distance in miles.",
        ge=0,
    )
    physical_restrictions: List[str] = Field(
        default_factory=list,
        description="Disqualifying physical demands (e.g., 'lift 50 lbs', 'ladder', 'prolonged standing').",
    )
    schedule_boundaries: List[str] = Field(
        default_factory=list,
        description="Disqualifying schedule requirements (e.g., 'weekend', 'overnight', 'graveyard', 'mandatory overtime').",
    )


class ExperienceBullet(BaseModel):
    """A quantified work accomplishment bullet point."""
    bullet: str = Field(..., description="Action verb + quantifiable achievement + outcome.")
    tags: List[str] = Field(default_factory=list, description="Associated skills or domain areas.")


class WorkRole(BaseModel):
    """Historical professional role entry."""
    title: str = Field(..., description="Job title held.")
    organization: str = Field(..., description="Company, agency, or institution name.")
    location: str = Field(..., description="City, State or Remote.")
    start_date: str = Field(..., description="Start date (e.g., 'Jan 2021').")
    end_date: str = Field(..., description="End date (e.g., 'Present' or 'Dec 2023').")
    bullets: List[str] = Field(
        default_factory=list,
        description="Quantified accomplishment bullet points.",
    )


class MasterExperience(BaseModel):
    """Comprehensive portfolio of past experience, tools, and education."""
    target_titles: List[str] = Field(
        default_factory=list,
        description="Desired job titles to match against.",
    )
    roles: List[WorkRole] = Field(
        default_factory=list,
        description="Chronological work history.",
    )
    tools_and_technologies: List[str] = Field(
        default_factory=list,
        description="Known software, tools, and technical competencies.",
    )
    education: List[str] = Field(
        default_factory=list,
        description="Degrees, certifications, and educational credentials.",
    )


class UserProfile(BaseModel):
    """Declarative user profile containing contact info, constraints, and master experience."""
    name: str = Field(..., description="Candidate full name.")
    email: str = Field(..., description="Contact email address.")
    phone: str = Field(..., description="Contact telephone number.")
    location: str = Field(..., description="Candidate home city and state.")
    linkedin_url: Optional[str] = Field(default=None, description="LinkedIn profile URL.")
    constraints: UserConstraints = Field(..., description="Deterministic filtering constraints.")
    master_experience: MasterExperience = Field(..., description="Candidate's comprehensive work background.")


class JobPosting(BaseModel):
    """Sanitized and structured job posting from an RSS source."""
    title: str = Field(..., description="Extracted job posting title.")
    link: str = Field(..., description="Direct posting URL.")
    published: Optional[str] = Field(default=None, description="Publication timestamp string.")
    raw_text: str = Field(..., description="Plain-text sanitized body enclosed in XML guard boundaries.")
    source: str = Field(..., description="Source feed identifier or domain.")


class EvaluationResult(BaseModel):
    """Verdict and quantitative rationale for a job posting evaluation."""
    status: EvaluationStatus = Field(
        ...,
        description="Overall decision: MATCH if passing all constraints and relevant, else REJECT.",
    )
    rejection_reason: Optional[str] = Field(
        default=None,
        description="Specific deterministic or semantic reason for rejection, if rejected.",
    )
    fit_score: int = Field(
        ...,
        description="Relevance and qualification score from 0 to 100.",
        ge=0,
        le=100,
    )
    estimated_compensation: Optional[str] = Field(
        default=None,
        description="Extracted or estimated pay range from posting text, if available.",
    )
    match_highlights: List[str] = Field(
        default_factory=list,
        description="Key alignment points between candidate profile and posting.",
    )
    tier_evaluated: int = Field(
        default=1,
        description="Tier at which decision was reached: 1 (deterministic) or 2 (LLM).",
    )


class TailoredResumeData(BaseModel):
    """Structured tailored resume content aligned with a specific matched job."""
    target_headline: str = Field(..., description="Customized professional headline for this role.")
    tailored_summary: str = Field(..., description="3-4 sentence professional summary emphasizing alignment.")
    categorized_skills: Dict[str, List[str]] = Field(
        ...,
        description="Categorized skills (e.g. Core Accounting, Software & ERP, Compliance).",
    )
    tailored_experience: List[WorkRole] = Field(
        ...,
        description="Roles with bullets prioritized and tailored to highlight target job requirements.",
    )
