"""Artifact generator: Markdown resumes, outreach drafts, and daily digests."""

import json
import logging
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import Settings
from src.schemas import (
    EvaluationResult,
    JobPosting,
    TailoredResumeData,
    UserProfile,
    WorkRole,
)

logger = logging.getLogger("beacon.generator")


def slugify(text: str) -> str:
    """Generate a filesystem-safe slug from a job title."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] if slug else "job_posting"


def get_jinja_env(template_dir: Path = Path("templates")) -> Environment:
    """Initialize and return Jinja2 environment."""
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def create_deterministic_tailored_data(
    posting: JobPosting,
    profile: UserProfile,
) -> TailoredResumeData:
    """Generate deterministic tailored resume data when running dry-run or offline."""
    headline = f"Experienced {posting.title.strip()} | Professional Specialist"

    summary = (
        f"Accomplished professional with proven track record in organizational efficiency, "
        f"financial accuracy, and systematic data administration. Expert in applying high-impact industry "
        f"tools and workflows to streamline operations. Dedicated candidate targeting the {posting.title} role "
        f"at {posting.source} with immediate readiness and verified competencies."
    )

    # Categorize skills
    skills = profile.master_experience.tools_and_technologies
    half = max(len(skills) // 2, 1)
    categorized_skills: Dict[str, List[str]] = {
        "Core Technical & ERP Systems": skills[:half],
        "Workflows, Compliance & Data Administration": skills[half:],
    }

    # Use master roles
    tailored_roles = profile.master_experience.roles

    return TailoredResumeData(
        target_headline=headline,
        tailored_summary=summary,
        categorized_skills=categorized_skills,
        tailored_experience=tailored_roles,
    )


def generate_tailored_resume_data(
    posting: JobPosting,
    profile: UserProfile,
    config: Settings,
    dry_run: bool = False,
) -> TailoredResumeData:
    """Generate TailoredResumeData using Gemini or fallback to deterministic synthesizer."""
    if dry_run or not config.gemini_api_key:
        return create_deterministic_tailored_data(posting, profile)

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=config.gemini_api_key)

        prompt = f"""
You are an executive resume writer. Tailor the candidate's master profile to highlight maximum relevance for this job.

TARGET JOB TITLE: {posting.title}
TARGET JOB CONTENT:
{posting.raw_text}

CANDIDATE MASTER ROLES & ACCOMPLISHMENTS:
{json.dumps([r.model_dump() for r in profile.master_experience.roles])}

CANDIDATE MASTER SKILLS:
{json.dumps(profile.master_experience.tools_and_technologies)}

INSTRUCTIONS:
1. Generate an impactful target_headline aligned with "{posting.title}".
2. Write a concise 3-4 sentence tailored_summary showcasing candidate's strengths for this role.
3. Group the candidate's actual skills into logical categorized_skills dictionaries.
4. Return tailored_experience keeping factual accomplishments from candidate's past roles, ordering bullets to highlight relevance to the target job.
Do not invent new companies or fake credentials.
"""
        response = client.models.generate_content(
            model=config.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TailoredResumeData,
                temperature=0.2,
            ),
        )

        data = TailoredResumeData.model_validate(json.loads(response.text))
        return data

    except Exception as e:
        logger.warning(f"Failed to generate LLM resume data ({e}). Using deterministic fallback.")
        return create_deterministic_tailored_data(posting, profile)


def generate_tailored_resume(
    posting: JobPosting,
    profile: UserProfile,
    result: EvaluationResult,
    config: Settings,
    dry_run: bool = False,
) -> Path:
    """Render and save a tailored Markdown resume for a matched job."""
    config.ensure_directories()
    slug = slugify(posting.title)
    output_path = config.matches_dir / f"{slug}_resume.md"

    resume_data = generate_tailored_resume_data(posting, profile, config, dry_run=dry_run)

    env = get_jinja_env()
    template = env.get_template("resume_template.md.j2")

    content = template.render(
        profile=profile,
        resume_data=resume_data,
        job=posting,
        result=result,
    )

    output_path.write_text(content, encoding="utf-8")
    logger.info(f"Saved tailored resume: {output_path}")
    return output_path


def generate_outreach_draft(
    posting: JobPosting,
    profile: UserProfile,
    result: EvaluationResult,
    config: Settings,
) -> Path:
    """Generate a human-in-the-loop plain-text outreach draft with a mailto: link."""
    config.ensure_directories()
    slug = slugify(posting.title)
    output_path = config.matches_dir / f"{slug}_outreach.txt"

    subject = f"Application for {posting.title} - {profile.name}"

    # Extract highlights or summary
    highlights_text = "\n".join(f"- {h}" for h in result.match_highlights) if result.match_highlights else "- Direct experience matching core requirements\n- Verifiable record of accomplishments"

    email_body = f"""Dear Hiring Team,

I am writing to express my strong interest in the {posting.title} role posted via {posting.source}.

With extensive hands-on experience in this domain, I offer immediate value in driving operational excellence:
{highlights_text}

My full resume is attached for your review. I welcome the opportunity to discuss how my background directly aligns with your goals.

Best regards,

{profile.name}
{profile.phone}
{profile.email}
{profile.location}
Job Link: {posting.link}
"""

    # Build mailto URL
    params = {
        "subject": subject,
        "body": email_body,
    }
    encoded_params = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    mailto_link = f"mailto:?{encoded_params}"

    content = f"""================================================================================
OUTREACH DRAFT (HUMAN-IN-THE-LOOP)
Target Role: {posting.title}
Source: {posting.source}
Posting URL: {posting.link}
Fit Score: {result.fit_score}/100
================================================================================

SUBJECT:
{subject}

BODY:
{email_body}
--------------------------------------------------------------------------------
ONE-CLICK MAILTO LINK (Copy and paste in browser or terminal):
{mailto_link}
================================================================================
"""

    output_path.write_text(content, encoding="utf-8")
    logger.info(f"Saved outreach draft: {output_path}")
    return output_path


def append_daily_digest(
    matches: List[tuple[JobPosting, EvaluationResult, Path, Path]],
    config: Settings,
) -> Path:
    """Append match summaries to artifacts/daily_digest_YYYY-MM-DD.md."""
    config.ensure_directories()
    today_str = datetime.now().strftime("%Y-%m-%d")
    digest_path = config.artifacts_dir / f"daily_digest_{today_str}.md"

    is_new = not digest_path.exists()
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: List[str] = []
    if is_new:
        lines.append(f"# BeaconAI Daily Digest — {today_str}\n")
        lines.append(f"*Generated by BeaconAI Job Intelligence Engine*\n")
        lines.append("## Qualified Matches\n")
        lines.append("| Fit | Job Title | Source | Compensation | Resume | Outreach |")
        lines.append("|:---:|:---|:---|:---|:---|:---|")

    with digest_path.open("a", encoding="utf-8") as f:
        if is_new:
            f.write("\n".join(lines) + "\n")

        for job, result, resume_path, outreach_path in matches:
            comp = result.estimated_compensation or "Not Stated"
            resume_rel = resume_path.relative_to(config.artifacts_dir.parent) if resume_path.is_relative_to(config.artifacts_dir.parent) else resume_path.name
            outreach_rel = outreach_path.relative_to(config.artifacts_dir.parent) if outreach_path.is_relative_to(config.artifacts_dir.parent) else outreach_path.name

            row = f"| **{result.fit_score}** | [{job.title}]({job.link}) | {job.source} | {comp} | [Resume]({resume_rel}) | [Outreach]({outreach_rel}) |"
            f.write(row + "\n")

    logger.info(f"Updated daily digest: {digest_path}")
    return digest_path
