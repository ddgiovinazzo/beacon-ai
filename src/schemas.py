"""Strict Pydantic V2 schemas for BeaconAI data contracts."""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class EvaluationStatus(str, Enum):
    """Evaluation decision status."""
    MATCH = "MATCH"
    REJECT = "REJECT"
    DEFERRED = "DEFERRED"



class ExperienceRole(BaseModel):
    """Historical professional role entry with metadata tags for dynamic role selection."""
    id: str = Field(default="", description="Unique role identifier.")
    title: str = Field(..., description="Job title held.")
    organization: str = Field(..., description="Company, agency, or institution name.")
    location: str = Field(..., description="City, State or Remote.")
    start_date: str = Field(..., description="Start date (e.g., 'Jan 2021').")
    end_date: str = Field(..., description="End date (e.g., 'Present' or 'Dec 2023').")
    tags: List[str] = Field(
        default_factory=list,
        description="Domain tags for dynamic selection (e.g., 'data_entry', 'engineering', 'accounting').",
    )
    bullets: List[str] = Field(
        default_factory=list,
        description="Quantified accomplishment bullet points.",
    )


# Backward-compatibility alias
WorkRole = ExperienceRole


class EngineeringProject(BaseModel):
    """Independent technical or engineering project entry."""
    id: str = Field(default="", description="Unique project identifier.")
    name: str = Field(..., description="Project name or system title.")
    tags: List[str] = Field(
        default_factory=list,
        description="Domain or technology tags (e.g., 'rag', 'llm', 'backend', 'fullstack').",
    )
    bullets: List[str] = Field(
        default_factory=list,
        description="Key implementation details and quantifiable results.",
    )


class EducationEntry(BaseModel):
    """Educational credential with institutional and geographic tags."""
    id: str = Field(default="", description="Unique education entry identifier.")
    institution: str = Field(..., description="Educational institution or program name.")
    degree: str = Field(..., description="Degree, diploma, or certification earned.")
    start_date: Optional[str] = Field(default=None, description="Start date string.")
    end_date: Optional[str] = Field(default=None, description="End date or completion year.")
    tags: List[str] = Field(
        default_factory=list,
        description="Geographic and category tags (e.g., 'local', 'tech', 'universal').",
    )


class CertificationEntry(BaseModel):
    """Professional or industry certification."""
    name: str = Field(..., description="Certification name.")
    status: str = Field(default="Active", description="Certification status (e.g., 'Active', 'In Progress').")


class UserConstraints(BaseModel):
    """Declarative constraints and non-negotiables for job filtering."""
    min_hourly_rate: Optional[float] = Field(
        default=None,
        description="Minimum acceptable gross hourly rate in USD.",
        ge=0.0,
    )
    min_weekly_earnings: Optional[float] = Field(
        default=None,
        description="Minimum acceptable weekly earnings in USD.",
        ge=0.0,
    )
    min_annual_salary: Optional[float] = Field(
        default=None,
        description="Minimum acceptable annual salary in USD.",
        ge=0.0,
    )
    max_commute_miles: Optional[int] = Field(
        default=25,
        description="Maximum acceptable one-way commute distance in miles.",
        ge=0,
    )
    physical_restrictions: List[str] = Field(
        default_factory=list,
        description="Disqualifying physical demands (e.g., 'heavy lifting', 'ladder climbing', 'prolonged standing').",
    )
    schedule_boundaries: List[str] = Field(
        default_factory=list,
        description="Disqualifying schedule requirements (e.g., 'graveyard', 'unannounced overtime', 'mandatory weekend').",
    )


class ProfileTrack(BaseModel):
    """Discrete, isolated resource track for targeted resume compilation (The Selector Pattern)."""
    track_id: str = Field(..., description="Unique track identifier (e.g. 'clerical_data_entry', 'software_engineering').")
    display_name: str = Field(..., description="Human-readable track name.")
    skills_header: str = Field(default="TECHNICAL SKILLS", description="Custom markdown header for skills section.")
    target_titles: List[str] = Field(default_factory=list, description="Target job titles that trigger this track.")
    trigger_keywords: List[str] = Field(default_factory=list, description="Keywords in posting title/body that align with this track.")
    approved_summary_traits: List[str] = Field(
        default_factory=list,
        description="Curated bank of approved professional traits for Sentence 1 of the summary.",
    )
    approved_summary_outcomes: List[str] = Field(
        default_factory=list,
        description="Curated bank of approved outcome/impact traits for Sentence 2 of the summary.",
    )
    categorized_skills: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Deterministic, human-curated skills matrix isolated for this track.",
    )
    roles: List[ExperienceRole] = Field(
        default_factory=list,
        description="Pre-tailored roles and verified bullet pools specifically framed for this track.",
    )
    projects: List[EngineeringProject] = Field(
        default_factory=list,
        description="Relevant engineering or operational projects for this track (empty list hides Projects section).",
    )
    education: List[EducationEntry] = Field(
        default_factory=list,
        description="Relevant education credentials for this track.",
    )
    certifications: List[CertificationEntry] = Field(
        default_factory=list,
        description="Relevant certifications for this track.",
    )
    include_portfolio: bool = Field(
        default=False,
        description="Whether to include portfolio website in contact line.",
    )
    include_github: bool = Field(
        default=False,
        description="Whether to include raw GitHub link in contact line.",
    )
    narrative_context: Optional[str] = Field(
        default=None,
        description="Optional positioning guidance for this track.",
    )


class MasterExperience(BaseModel):
    """The ground-truth master repository of candidate history, roles, tools, and credentials."""
    target_titles: List[str] = Field(
        default_factory=list,
        description="Comprehensive list of job titles the candidate is targeting.",
    )
    narrative_context: Optional[str] = Field(
        default=None,
        description="Synthesized career narrative and framing guidelines for LLM tailoring.",
    )
    roles: List[ExperienceRole] = Field(
        default_factory=list,
        description="Detailed chronological employment and volunteer history with verified accomplishment bullets.",
    )
    tools_and_technologies: List[str] = Field(
        default_factory=list,
        description="Full, canonical bank of all skills, frameworks, and domain competencies.",
    )
    engineering_projects: List[EngineeringProject] = Field(
        default_factory=list,
        description="Independent or professional engineering, system architecture, or open-source projects.",
    )
    education: List[EducationEntry] = Field(
        default_factory=list,
        description="Formal academic degrees, certifications, or intensive training programs.",
    )
    certifications: List[CertificationEntry] = Field(
        default_factory=list,
        description="Professional and industry certifications.",
    )
    tracks: Dict[str, ProfileTrack] = Field(
        default_factory=dict,
        description="Optional discrete persona pools for multi-track deterministic routing.",
    )

    @field_validator("education", mode="before")
    @classmethod
    def coerce_education(cls, v):
        """Coerce legacy string education entries into EducationEntry objects."""
        if isinstance(v, list):
            coerced = []
            for idx, item in enumerate(v):
                if isinstance(item, str):
                    coerced.append({
                        "id": f"edu-{idx+1}",
                        "institution": item,
                        "degree": item,
                        "tags": ["universal"],
                    })
                else:
                    coerced.append(item)
            return coerced
        return v

    @field_validator("roles", mode="before")
    @classmethod
    def coerce_roles(cls, v):
        """Ensure roles have IDs if missing."""
        if isinstance(v, list):
            for idx, r in enumerate(v):
                if isinstance(r, dict) and not r.get("id"):
                    r["id"] = f"role-{idx+1}"
        return v

    @field_validator("engineering_projects", mode="before")
    @classmethod
    def coerce_projects(cls, v):
        """Ensure engineering projects have IDs if missing."""
        if isinstance(v, list):
            for idx, p in enumerate(v):
                if isinstance(p, dict) and not p.get("id"):
                    p["id"] = f"proj-{idx+1}"
        return v



class UserProfile(BaseModel):
    """Declarative user profile containing contact info, constraints, and master experience."""
    name: str = Field(..., description="Candidate full name.")
    email: str = Field(..., description="Contact email address.")
    phone: str = Field(..., description="Contact telephone number.")
    location: str = Field(..., description="Candidate home city and state.")
    linkedin_url: Optional[str] = Field(default=None, description="LinkedIn profile URL.")
    portfolio_url: Optional[str] = Field(default=None, description="Portfolio website URL.")
    github_url: Optional[str] = Field(default=None, description="GitHub profile URL.")
    constraints: UserConstraints = Field(default_factory=UserConstraints, description="Deterministic filtering constraints.")
    master_experience: MasterExperience = Field(..., description="Candidate's comprehensive work background.")

    @property
    def tracks(self) -> Dict[str, ProfileTrack]:
        return self.master_experience.tracks


class JobPosting(BaseModel):
    """Sanitized and structured job posting from an RSS source."""
    title: str = Field(..., description="Extracted job posting title.")
    link: str = Field(..., description="Direct posting URL.")
    published: Optional[str] = Field(default=None, description="Publication timestamp string.")
    raw_text: str = Field(..., description="Plain-text sanitized body enclosed in XML guard boundaries.")
    source: str = Field(..., description="Source feed identifier or domain.")
    contact_email: Optional[str] = Field(default=None, description="Optional direct employer contact email.")
    description: Optional[str] = Field(default=None, description="Optional job description body.")
    email_msg_id: Optional[str] = Field(default=None, description="IMAP message ID for non-destructive seen tracking.")


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
    matched_track_id: Optional[str] = Field(
        default=None,
        description="ID of the resolved ProfileTrack if multi-track routing matched.",
    )


class TailoredResumeData(BaseModel):
    """Structured tailored resume content aligned with a specific matched job."""
    target_headline: str = Field(..., description="Customized professional headline for this role.")
    tailored_summary: str = Field(..., description="3-4 sentence professional summary emphasizing alignment.")
    categorized_skills: Dict[str, List[str]] = Field(
        ...,
        description="Categorized skills (e.g. Core Technical, Domain Workflows, Tools).",
    )
    tailored_experience: List[ExperienceRole] = Field(
        ...,
        description="Roles with bullets prioritized and tailored to highlight target job requirements.",
    )
    tailored_projects: List[EngineeringProject] = Field(
        default_factory=list,
        description="Relevant engineering or technical projects tailored to the role.",
    )
    tailored_education: List[EducationEntry] = Field(
        default_factory=list,
        description="Selected education credentials aligned with geographic and technical context.",
    )
    include_portfolio_link: bool = Field(
        default=False,
        description="Set True ONLY if the target role is primarily software, web, cloud/DevOps, or AI/data engineering where a developer portfolio is standard. Set False for administrative, clerical, operational, accounting, or bookkeeping roles.",
    )
    include_github_link: bool = Field(
        default=False,
        description="Set True ONLY if the target role specifically evaluates code repositories. Set False for non-developer or general analytical roles.",
    )
    skills_header: Optional[str] = Field(
        default=None,
        description="Custom header label for the skills section (e.g. 'CORE COMPETENCIES & SKILLS').",
    )

    @property
    def headline(self) -> str:
        return self.target_headline

    @property
    def summary(self) -> str:
        return self.tailored_summary

    @property
    def experience(self) -> List[ExperienceRole]:
        return self.tailored_experience

    @property
    def projects(self) -> List[EngineeringProject]:
        return self.tailored_projects

    @property
    def education(self) -> List[EducationEntry]:
        return self.tailored_education

    @property
    def skill_categories(self) -> List[Dict[str, Any]]:
        if isinstance(self.categorized_skills, dict):
            categories = []
            for cat, sk in self.categorized_skills.items():
                cat_name = str(cat).strip()
                if not cat_name:
                    continue
                if isinstance(sk, list):
                    clean_skills = [str(item).strip() for item in sk if str(item).strip()]
                elif isinstance(sk, str):
                    clean_skills = [s.strip() for s in sk.split(",") if s.strip()]
                else:
                    clean_skills = []
                if clean_skills:
                    categories.append({"name": cat_name, "skills": clean_skills})
            return categories
        return []

    @property
    def skills(self) -> List[str]:
        all_skills = []
        for cat in self.skill_categories:
            all_skills.extend(cat["skills"])
        return all_skills

