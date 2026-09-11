"""Artifact generator: Markdown resumes, outreach drafts, and daily digests."""

import hashlib
import json
import logging
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import LLM_MODEL, Settings
from src.evaluator import execute_llm_completion
from src.schemas import (
    EvaluationResult,
    JobPosting,
    TailoredResumeData,
    UserProfile,
)

logger = logging.getLogger("beacon.generator")


def slugify(text: str) -> str:
    """Generate a filesystem-safe slug from a job title."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] if slug else "job_posting"


def get_job_slug(posting: JobPosting) -> str:
    """Generate collision-proof slug using sanitized title and SHA-256 link hash."""
    base_slug = slugify(posting.title)
    link_hash = hashlib.sha256(posting.link.encode("utf-8")).hexdigest()[:6]
    return f"{base_slug}_{link_hash}"


def generate_clean_resume_filename(candidate_name: str, company_name: Optional[str], job_title: str) -> str:
    """
    Generates a clean, recruiter-friendly filename.
    Format: FirstName_LastName_CompanyName_Resume.pdf
    Fallback: FirstName_LastName_JobTitle_Resume.pdf
    Allows up to 40 characters for the company/title, ensuring words are not sliced mid-syllable.
    """
    # Clean candidate name: "Jane Doe" -> "Jane_Doe"
    clean_candidate = "_".join(re.sub(r'[^a-zA-Z0-9\s]', '', candidate_name).split())
    
    # Clean target entity
    raw_target = company_name if company_name and company_name.strip() else job_title
    
    # Extract alphanumeric words and title-case them without slicing mid-word/mid-syllable
    words = re.findall(r'[a-zA-Z0-9]+', raw_target)
    selected_words = []
    current_len = 0
    for w in words:
        w_title = w.capitalize()
        # If the first word alone exceeds 40 characters, truncate it
        if not selected_words and len(w_title) > 40:
            selected_words.append(w_title[:40])
            break
        if current_len + len(w_title) <= 40:
            selected_words.append(w_title)
            current_len += len(w_title)
        else:
            break

    clean_target = "".join(selected_words) if selected_words else "Role"
    
    return f"{clean_candidate}_{clean_target}_Resume.pdf"


def sanitize_target_company(company_name: Optional[str]) -> Optional[str]:
    """Sanitize company name to prevent source URLs, feed filenames, or generic ad phrases from leaking into text.

    If company_name is None, empty, ends in an extension/TLD (e.g. .xml, .com, .org, .net),
    matches a URL scheme, or matches a generic recruitment announcement, returns None.
    """
    if not company_name:
        return None
    cleaned = company_name.strip()
    if re.search(r"^https?://", cleaned, flags=re.IGNORECASE):
        return None
    if re.search(r"\.(xml|com|org|net|io|co|us|gov|edu|rss|json|html|htm)$", cleaned, flags=re.IGNORECASE):
        return None
    if "/" in cleaned or "\\" in cleaned:
        return None
    # Filter out common job board aggregation platforms and protocols
    if re.search(r"^(?:craigslist|indeed|linkedin|ziprecruiter|glassdoor|monster|dice|careerbuilder|simplyhired|snagajob|upwork|fiverr|rss|feed|rss_feed|job_board|email|alert|alerts)$", cleaned, flags=re.IGNORECASE):
        return None
    if re.search(r"^(?:now\s+hiring|urgent(?:ly)?\s+(?:hiring|needed)|help\s+wanted|immediate\s+opening|job\s+opening|position\s+available|hiring\s+immediately|we\s+are\s+hiring|seeking|wanted|needed|full[- ]time|part[- ]time|remote|entry[- ]level)$", cleaned, flags=re.IGNORECASE):
        return None
    return cleaned


def extract_company_from_title(title: str) -> Optional[str]:
    """Attempt to extract company name from job title if formatted with common delimiters."""
    candidate = None
    if ":" in title:
        parts = title.split(":", 1)[0].strip()
        if 1 < len(parts) <= 30:
            candidate = parts
    elif " at " in title.lower():
        parts = re.split(r"\s+at\s+", title, flags=re.IGNORECASE)
        if len(parts) > 1 and 1 < len(parts[-1].strip()) <= 30:
            candidate = parts[-1].strip()
    elif " - " in title:
        parts = title.split(" - ", 1)[0].strip()
        if 1 < len(parts) <= 30:
            candidate = parts
    return sanitize_target_company(candidate)


def clean_role_title(title: str, company: Optional[str] = None) -> str:
    """Extract and clean the pure job role title, removing company prefixes, requisition IDs, or feed tags."""
    cleaned = title.strip()
    if company:
        escaped_co = re.escape(company)
        cleaned = re.sub(rf"^{escaped_co}\s*[:|\-–—]\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(rf"\s*[:|\-–—]\s*{escaped_co}$", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(rf"\s+at\s+{escaped_co}$", "", cleaned, flags=re.IGNORECASE).strip()

    # Generic delimiter check: "Company: Role" or "Location | Role"
    if ":" in cleaned:
        prefix, rest = cleaned.split(":", 1)
        if rest.strip() and len(prefix.strip()) <= 35:
            cleaned = rest.strip()
    elif " - " in cleaned:
        parts = cleaned.split(" - ")
        if len(parts) == 2 and len(parts[0].strip()) <= 30 and any(kw in parts[1].lower() for kw in ["engineer", "developer", "manager", "clerk", "analyst", "specialist", "bookkeeper", "lead", "architect"]):
            cleaned = parts[1].strip()

    # Strip leading recruitment ad prefixes: e.g. "Construction Company seeking Clerical/ Administrative Assistant"
    cleaned = re.sub(
        r"^(?:[\w\s&.,-]{1,35}?\s+(?:is\s+)?(?:seeking|looking\s+for|hiring(?:\s+for)?|in\s+need\s+of)\s+)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Strip announcement tags: e.g. "Now Hiring: ", "Immediate Opening: "
    cleaned = re.sub(
        r"^(?:Now\s+Hiring|Urgent(?:ly)?\s+(?:Hiring|Needed)|Immediate\s+Opening|Job\s+Opening)[:\s\-–—]+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Strip trailing "needed", "wanted"
    cleaned = re.sub(r"\s+(?:needed|wanted|urgently\s+needed)\s*$", "", cleaned, flags=re.IGNORECASE).strip()

    # Normalize slash spacing: e.g. "Clerical/ Administrative" -> "Clerical / Administrative"
    cleaned = re.sub(r"([a-zA-Z])\s*\/\s*([a-zA-Z])", r"\1 / \2", cleaned)

    # Strip trailing requisition tags or employment types: e.g., (Req #1234), [Full Time], (Remote)
    cleaned = re.sub(r"\s*[\(\[\{](?:req(?:uisition)?\s*#?[\w\d]+|full[- ]time|part[- ]time|contract|remote)[\)\]\}]", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or title.strip()


def filter_skills_for_target_domain(skills: List[str], job_text: str) -> List[str]:
    """Filter out irrelevant cross-domain skills (e.g. typing speed or bookkeeping on tech roles) in a user-agnostic manner."""
    tech_keywords = {"software", "engineer", "developer", "backend", "frontend", "fullstack", "python", "devops", "cloud", "data engineer", "systems"}
    is_tech = any(kw in job_text.lower() for kw in tech_keywords)

    clerical_keywords = [
        "typing", "wpm", "civil service", "data terminal", "ledger",
        "quickbooks", "accounts payable", "accounts receivable", "intuit payroll",
        "word, outlook, powerpoint", "office suite"
    ]

    filtered = []
    for s in skills:
        s_lower = s.lower()
        if is_tech:
            # Drop clerical/accounting/typing skills unless explicitly mentioned in the target job text
            if any(ck in s_lower for ck in clerical_keywords) and not any(ck in job_text.lower() for ck in [s_lower]):
                continue
        filtered.append(s)
    return filtered or skills


def get_jinja_env(template_dir: Path = Path("templates")) -> Environment:
    """Initialize and return Jinja2 environment."""
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def select_best_approved_title(approved_titles: List[str], job_title: str, job_text: str = "") -> str:
    """Deterministically select the best matching title from the approved titles bank."""
    if not approved_titles:
        return job_title
    combined = f"{job_title} {job_text}".lower()
    title_lower = job_title.lower()

    # 1. Exact or substring match against posting title
    for title in approved_titles:
        if title.lower() in title_lower:
            return title

    # 2. Exact or substring match against full posting text
    for title in approved_titles:
        if title.lower() in combined:
            return title

    # 3. Maximum token overlap
    best_title = approved_titles[0]
    best_overlap = -1
    for title in approved_titles:
        tokens = [tok for tok in title.lower().split() if len(tok) > 2]
        overlap = sum(1 for tok in tokens if tok in combined)
        if overlap > best_overlap:
            best_overlap = overlap
            best_title = title

    return best_title


def create_deterministic_tailored_data(
    posting: JobPosting,
    profile: UserProfile,
) -> TailoredResumeData:
    """Synthesize complete, ATS-compliant TailoredResumeData deterministically without an LLM.
    
    If the profile defines ProfileTracks, routes to the matching track's isolated resources.
    """
    from src.evaluator import resolve_profile_track

    company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)
    headline = clean_role_title(posting.title, company)
    job_text = f"{posting.title} {posting.raw_text}".lower()

    track = resolve_profile_track(posting, profile) if profile.tracks else None

    if track:
        # Multi-Track Persona Mode: Select from pre-approved titles
        approved_titles = track.approved_titles if track.approved_titles else track.target_titles[:3]
        if approved_titles:
            headline = select_best_approved_title(approved_titles, posting.title, posting.raw_text)

        trait = track.approved_summary_traits[0] if track.approved_summary_traits else "verification accuracy"
        outcome = track.approved_summary_outcomes[0] if track.approved_summary_outcomes else "100% data integrity"
        for t in track.approved_summary_traits:
            if any(w in job_text for w in t.lower().split() if len(w) > 4):
                trait = t
                break
        for o in track.approved_summary_outcomes:
            if any(w in job_text for w in o.lower().split() if len(w) > 4):
                outcome = o
                break

        all_skills = [s for cat in track.categorized_skills.values() for s in cat]
        matched_skills = [s for s in all_skills if s.lower() in job_text]
        sample_skills = matched_skills[:2] if len(matched_skills) >= 2 else all_skills[:2]
        systems_phrase = " and ".join(sample_skills) if sample_skills else "operational workflows"

        summary = (
            f"{headline} with proven experience in {systems_phrase}, specializing in {trait}. "
            f"Experienced in structured workflow execution and records management, delivering {outcome}."
        )

        selected_roles = list(track.roles) if track.roles else list(profile.master_experience.roles)
        tailored_projects = list(track.projects)
        tailored_education = list(track.education) if track.education else list(profile.master_experience.education)
        categorized_skills = dict(track.categorized_skills)

        return TailoredResumeData(
            target_headline=headline,
            tailored_summary=summary,
            categorized_skills=categorized_skills,
            tailored_experience=selected_roles,
            tailored_projects=tailored_projects,
            tailored_education=tailored_education,
            include_portfolio_link=track.include_portfolio,
            include_github_link=False,
            skills_header=track.skills_header,
        )

    # Legacy Fallback (when profile doesn't define tracks)
    # 1. Dynamic Summary Synthesis
    tech_keywords = {"software", "engineer", "developer", "backend", "frontend", "fullstack", "python", "devops", "cloud", "data engineer"}
    is_tech_job = any(kw in job_text for kw in tech_keywords)

    impact_target = f"at {company}" if company else "in this role"
    if is_tech_job:
        summary = (
            f"Accomplished technical professional targeting the {headline} role with hands-on "
            f"experience in scalable software systems, technical problem-solving, and operational excellence. "
            f"Prepared to deliver immediate value {impact_target}."
        )
    else:
        summary = (
            f"Accomplished professional targeting the {headline} role with verified domain experience. "
            f"Proven track record delivering operational rigor, high accuracy, "
            f"and dependable performance. Prepared to make an immediate impact {impact_target}."
        )

    # 2. Dynamic Role Selection based on keyword and tag scoring
    scored_roles = []
    for role in profile.master_experience.roles:
        score = 0
        for tag in role.tags:
            tag_clean = tag.lower().replace("_", " ")
            if tag_clean in job_text:
                score += 3
        for token in role.title.lower().split():
            if len(token) > 3 and token in job_text:
                score += 1
        for bullet in role.bullets:
            for word in bullet.lower().split():
                if len(word) > 4 and word in job_text:
                    score += 0.1
        scored_roles.append((score, role))

    scored_roles.sort(key=lambda x: x[0], reverse=True)
    if any(s > 0 for s, _ in scored_roles):
        selected_roles = [r for s, r in scored_roles if s > 0][:3]
    else:
        selected_roles = [r for _, r in scored_roles][:3]

    # 3. Dynamic Engineering Project Selection based on tag matching
    selected_projects = []
    for proj in profile.master_experience.engineering_projects:
        proj_score = 0
        for tag in proj.tags:
            tag_clean = tag.lower().replace("_", " ")
            if tag_clean in job_text:
                proj_score += 2
        for token in proj.name.lower().split():
            if len(token) > 3 and token in job_text:
                proj_score += 1
        if proj_score > 0:
            selected_projects.append((proj_score, proj))

    selected_projects.sort(key=lambda x: x[0], reverse=True)
    tailored_projects = [p for _, p in selected_projects][:2]

    # 4. Geographic & Institutional Education Heuristics
    is_remote = bool(re.search(r"\b(?:remote|telecommute|virtual|work\s+from\s+home|100%\s+remote)\b", job_text))
    loc_tokens = [tok.strip().lower() for tok in profile.location.replace(",", " ").split() if len(tok.strip()) > 2]
    is_local_posting = any(tok in job_text for tok in loc_tokens) or bool(
        re.search(r"\b(?:local|county|district|municipal|town\s+of|city\s+of|civil\s+service)\b", job_text)
    )

    tailored_education = []
    for edu in profile.master_experience.education:
        edu_tags = [t.lower() for t in edu.tags]
        if is_local_posting and not is_remote:
            if "local" in edu_tags or "universal" in edu_tags or not edu_tags:
                tailored_education.append(edu)
        else:
            if "local" in edu_tags and "tech" not in edu_tags and "universal" not in edu_tags:
                continue
            tailored_education.append(edu)

    if not tailored_education:
        tailored_education = list(profile.master_experience.education)

    domain_skills = filter_skills_for_target_domain(profile.master_experience.tools_and_technologies, job_text)
    matched_skills = [s for s in domain_skills if s.lower() in job_text]
    unmatched_skills = [s for s in domain_skills if s.lower() not in job_text]
    ordered_skills = matched_skills + unmatched_skills

    half = max(len(ordered_skills) // 2, 1)
    categorized_skills: Dict[str, List[str]] = {
        "Core Technical & Domain Systems": ordered_skills[:half],
        "Workflows, Tools & Methodologies": ordered_skills[half:],
    }

    tech_keywords = {"software", "engineer", "developer", "backend", "frontend", "fullstack", "python", "devops", "cloud", "data engineer", "systems"}
    is_tech = any(kw in job_text for kw in tech_keywords)

    return TailoredResumeData(
        target_headline=headline,
        tailored_summary=summary,
        categorized_skills=categorized_skills,
        tailored_experience=selected_roles,
        tailored_projects=tailored_projects,
        tailored_education=tailored_education,
        include_portfolio_link=is_tech,
        include_github_link=is_tech,
        skills_header="TECHNICAL SKILLS" if is_tech else "CORE COMPETENCIES & SKILLS",
    )


def generate_tailored_resume_data(
    posting: JobPosting,
    profile: UserProfile,
    config: Settings,
    dry_run: bool = False,
) -> TailoredResumeData:
    """Generate TailoredResumeData using model-agnostic LiteLLM/Instructor or fallback to deterministic synthesizer."""
    if dry_run:
        return create_deterministic_tailored_data(posting, profile)

    from src.evaluator import resolve_profile_track
    track = resolve_profile_track(posting, profile) if profile.tracks else None

    company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)
    clean_title = clean_role_title(posting.title, company)
    target_company = company

    if track:
        approved_titles = track.approved_titles if track.approved_titles else track.target_titles[:3]
        active_roles = track.roles if track.roles else profile.master_experience.roles
        active_projects = track.projects
        active_skills = track.categorized_skills
        active_education = track.education if track.education else profile.master_experience.education
        active_traits = track.approved_summary_traits
        active_outcomes = track.approved_summary_outcomes
        skills_header = track.skills_header
        include_portfolio = track.include_portfolio
        narrative = track.narrative_context or profile.master_experience.narrative_context
    else:
        approved_titles = [clean_title]
        active_roles = profile.master_experience.roles
        active_projects = profile.master_experience.engineering_projects
        combined_job_text = f"{posting.title} {posting.raw_text}"
        domain_skills = filter_skills_for_target_domain(profile.master_experience.tools_and_technologies, combined_job_text)
        half = max(len(domain_skills) // 2, 1)
        active_skills = {
            "Core Technical & Domain Systems": domain_skills[:half],
            "Workflows, Tools & Methodologies": domain_skills[half:],
        }
        active_education = profile.master_experience.education
        active_traits = ["verification accuracy", "system architecture"]
        active_outcomes = ["100% data integrity", "scalable performance"]
        skills_header = "TECHNICAL SKILLS"
        tech_keywords = {"software", "engineer", "developer", "backend", "frontend", "fullstack", "python", "devops", "cloud", "data engineer", "systems"}
        include_portfolio = any(kw in combined_job_text.lower() for kw in tech_keywords)
        narrative = profile.master_experience.narrative_context

    try:
        import instructor
        import litellm

        config.sync_litellm_env()
        client = instructor.from_litellm(litellm.completion)

        system_instruction = (
            "System Prompt: Strategic Resume Tailoring Agent\n\n"
            "Role & Objective:\n"
            "You are an expert technical recruiter and resume writer. Your objective is to analyze a provided Job Description (JD) "
            "and a preceding 'Context Prompt' (which contains the candidate's exact work history, accomplishments, and skills). "
            "You must adapt the base resume to perfectly align with the JD by acting as a top-down matching engine. "
            "You must strictly adhere to the candidate context, tone guardrails, and hard quantification rules.\n\n"
            "Candidate Context & Tone Guardrails:\n"
            "Rely strictly and exclusively on the candidate's verified work history, accomplishments, and narrative positioning provided in the Context Prompt.\n"
            "*CRITICAL TONE GUARDRAIL:* Match the JD's requested seniority level and domain. Default to grounded, practical, domain-appropriate verbs.\n"
            "*CRITICAL FORMATTING GUARDRAIL:* Remove all citation markers, source references, or brackets (e.g., [cite: 1], [source: 1]).\n\n"
            "Step 1: Bullet Heading Format: Format EVERY bullet in tailored_experience starting with a bold dynamic heading reflecting the JD competency, e.g.:\n"
            "   '**[Dynamic Competency Heading]:** [Tailored bullet point ending in a quantifiable result, based EXCLUSIVELY on Context Prompt]'\n\n"
            "Step 2: Summary Rules (STRICT ANTI-FLUFF 2-SENTENCE BLUEPRINT):\n"
            "Write exactly 2 sentences following this strict template:\n"
            "- Sentence 1: [Target Title] with proven experience in [1-2 systems from Candidate Skills], specializing in [Exact 1 trait chosen from the approved traits bank].\n"
            "- Sentence 2: Experienced in [1-2 workflows from Candidate Roles], delivering [Exact 1 outcome chosen from the approved outcomes bank].\n"
            "- STRICT ANTI-FLUFF: NO subjective filler adjectives ('Methodical', 'detail-oriented', 'adept at', 'proven expertise', 'quiet efficiency') and NO boilerplate endings ('Prepared to make an immediate impact')."
        )

        user_content = f"""JOB DESCRIPTION (JD):
<untrusted_job_posting>
ROLE: {clean_title}
EMPLOYER: {target_company or 'Prospective Organization'}
CONTENT:
{posting.raw_text}
</untrusted_job_posting>

CONTEXT PROMPT (CANDIDATE WORK HISTORY & ACCOMPLISHMENTS):
CANDIDATE NAME: {profile.name}
HOME LOCATION: {profile.location}
POSITIONING DIRECTIVE: {narrative or 'Aligned professional contributor'}

APPROVED RESUME HEADLINES (Choose exactly 1):
{json.dumps(approved_titles)}

CANDIDATE MASTER ROLES & ACCOMPLISHMENTS:
{json.dumps([r.model_dump() for r in active_roles])}

CANDIDATE PROJECTS:
{json.dumps([p.model_dump() for p in active_projects])}

CANDIDATE SKILLS MATRIX:
{json.dumps(active_skills)}

APPROVED SUMMARY TRAITS (Sentence 1 - choose 1):
{json.dumps(active_traits)}

APPROVED SUMMARY OUTCOMES (Sentence 2 - choose 1):
{json.dumps(active_outcomes)}

CANDIDATE EDUCATION BANK:
{json.dumps([e.model_dump() for e in active_education])}

INSTRUCTIONS:
1. target_headline: Set to the 1 title from APPROVED RESUME HEADLINES that best matches the job posting.
2. tailored_summary: Write exactly 2 sentences following the strict blueprint with 1 approved trait and 1 approved outcome.
3. categorized_skills: Output the exact candidate skills matrix provided above.
4. tailored_experience: Select 2-3 most relevant roles with bold headings '**[Heading]:** ...'.
5. tailored_projects: Select relevant projects from candidate projects above, or empty list if none provided.
6. tailored_education: Education credentials aligned with context.
7. include_portfolio_link: Set to {json.dumps(include_portfolio)}.
8. include_github_link: false.
9. skills_header: Set to "{skills_header}".
"""

        active_model = config.llm_model or LLM_MODEL
        if not active_model:
            logger.error("No LLM model specified! Set LLM_MODEL environment variable or configure Settings.llm_model.")
            return create_deterministic_tailored_data(posting, profile)

        gen_temp = 1.0 if "gemini-3" in active_model else 0.2
        call_kwargs = {
            "model": active_model,
            "response_model": TailoredResumeData,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "temperature": gen_temp,
        }
        if config.llm_api_key:
            call_kwargs["api_key"] = config.llm_api_key

        resume_data: TailoredResumeData = execute_llm_completion(client, **call_kwargs)

        # Enforce deterministic track constraints
        if track:
            if approved_titles and resume_data.target_headline not in approved_titles:
                resume_data.target_headline = select_best_approved_title(approved_titles, posting.title, posting.raw_text)
            resume_data.categorized_skills = track.categorized_skills
            resume_data.skills_header = track.skills_header
            resume_data.tailored_projects = track.projects
            resume_data.include_portfolio_link = track.include_portfolio
            resume_data.include_github_link = False

            # Ground experience roles strictly to track's pre-approved roles and bullets
            if track.roles:
                role_by_id = {r.id: r for r in track.roles}
                role_by_org = {r.organization.lower(): r for r in track.roles}
                guarded_roles = []
                for exp_role in (resume_data.tailored_experience or []):
                    matched_role = role_by_id.get(exp_role.id) or role_by_org.get(exp_role.organization.lower())
                    if matched_role and matched_role not in guarded_roles:
                        guarded_roles.append(matched_role)
                if guarded_roles:
                    resume_data.tailored_experience = guarded_roles
                else:
                    resume_data.tailored_experience = list(track.roles)

            # Sanitize summary and enforce strict 2-sentence formula
            clean_summary = re.sub(r"\[(?:cite|source|citation|ref)[:\s][^\]]+\]", "", resume_data.tailored_summary, flags=re.IGNORECASE).strip()
            sentences = [s.strip() for s in clean_summary.split(".") if s.strip()]
            if len(sentences) != 2 or not clean_summary.startswith(resume_data.target_headline):
                fallback_data = create_deterministic_tailored_data(posting, profile)
                resume_data.tailored_summary = fallback_data.tailored_summary
            else:
                resume_data.tailored_summary = clean_summary
        else:
            if not resume_data.skill_categories:
                fallback_data = create_deterministic_tailored_data(posting, profile)
                resume_data.categorized_skills = fallback_data.categorized_skills

        return resume_data

    except Exception as e:
        logger.warning(f"Failed to generate LLM resume data via {active_model} ({e}). Using deterministic fallback.")
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
    company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)
    clean_pdf_name = generate_clean_resume_filename(profile.name, company, posting.title)
    md_filename = Path(clean_pdf_name).with_suffix(".md").name
    output_path = config.matches_dir / md_filename

    resume_data = generate_tailored_resume_data(posting, profile, config, dry_run=dry_run)

    env = get_jinja_env()
    template = env.get_template("resume_template.md.j2")

    content = template.render(
        profile=profile,
        tailored_data=resume_data,
        resume_data=resume_data,
        job=posting,
        result=result,
    )

    # CRITICAL FORMATTING GUARDRAIL: Remove citation markers, source references, or brackets
    content = re.sub(r"\[(?:cite|source|citation|ref):\s*[^\]]+\]", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\[(?:cite|source|citation|ref)\s+[^\]]+\]", "", content, flags=re.IGNORECASE)

    output_path.write_text(content, encoding="utf-8")
    logger.info(f"Saved tailored resume: {output_path}")
    return output_path


def build_grounded_email_pitch(
    posting: JobPosting,
    profile: UserProfile,
    result: EvaluationResult,
) -> tuple[str, str]:
    """
    Build a clean, grounded subject and body for outreach.
    Enforces strict anti-fluff rules:
    - No robotic evaluator metadata ('Matches target job title...', 'Leverages candidate's skill set...')
    - No raw internal source identifiers ('posted via email:alerts.craigslist.org')
    - First-person active voice highlighting verified skills and systems
    - Clean contact and link formatting
    """
    company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)

    from src.evaluator import resolve_profile_track
    track = resolve_profile_track(posting, profile) if profile.tracks else None

    if track and (track.approved_titles or track.target_titles):
        approved_titles = track.approved_titles if track.approved_titles else track.target_titles[:3]
        clean_title = select_best_approved_title(approved_titles, posting.title, posting.raw_text)
    else:
        clean_title = clean_role_title(posting.title, company)

    subject = f"Application for {clean_title} - {profile.name}"

    if company:
        salutation = f"Dear {company} Hiring Team,"
    else:
        salutation = "Dear Hiring Team,"

    opening = f"Please accept my application for the {clean_title} position."

    combined_text = f"{posting.title} {posting.raw_text}".lower()

    if track:
        all_skills = [s for cat in track.categorized_skills.values() for s in cat]
        matched = [s for s in all_skills if s.lower() in combined_text][:3]
        if not matched:
            matched = all_skills[:3]
        is_tech = track.include_portfolio

        trait = track.approved_summary_traits[0] if track.approved_summary_traits else "structured execution"
        for t in track.approved_summary_traits:
            if any(w in combined_text for w in t.lower().split() if len(w) > 4):
                trait = t
                break

        outcome = track.approved_summary_outcomes[0] if track.approved_summary_outcomes else "dependable results"
        for o in track.approved_summary_outcomes:
            if any(w in combined_text for w in o.lower().split() if len(w) > 4):
                outcome = o
                break

        if len(matched) == 1:
            skills_phrase = matched[0]
        elif len(matched) == 2:
            skills_phrase = f"{matched[0]} and {matched[1]}"
        else:
            skills_phrase = f"{', '.join(matched[:-1])}, and {matched[-1]}"

        middle = (
            f"My background includes hands-on experience in {skills_phrase}, "
            f"specializing in {trait} to consistently deliver {outcome}."
        )
    else:
        domain_skills = filter_skills_for_target_domain(profile.master_experience.tools_and_technologies, combined_text)
        matched = [s for s in domain_skills if s.lower() in combined_text][:3]
        if not matched:
            matched = domain_skills[:3]
        tech_keywords = {"software", "engineer", "developer", "backend", "frontend", "fullstack", "python", "devops", "cloud", "data engineer", "systems"}
        is_tech = any(kw in combined_text for kw in tech_keywords)

        if matched:
            if len(matched) == 1:
                skills_phrase = matched[0]
            elif len(matched) == 2:
                skills_phrase = f"{matched[0]} and {matched[1]}"
            else:
                skills_phrase = f"{', '.join(matched[:-1])}, and {matched[-1]}"
            middle = (
                f"My background includes hands-on experience in {skills_phrase}, "
                "with a strong focus on data accuracy, structured workflows, and dependable execution."
            )
        else:
            middle = (
                "With a solid background in structured data management and operational workflows, "
                "I focus on delivering accurate results, clear communication, and dependable execution."
            )

    closing = (
        "My tailored resume is attached for your review. "
        "I welcome the opportunity to discuss how my skillset and background align with your team's goals."
    )

    contact_parts = [profile.name, f"{profile.phone} | {profile.email}"]
    online_links = []
    if is_tech and profile.portfolio_url:
        online_links.append(profile.portfolio_url.replace("https://", "").replace("http://", ""))
    if profile.linkedin_url:
        online_links.append(profile.linkedin_url.replace("https://", "").replace("http://", ""))
    if online_links:
        contact_parts.append(" | ".join(online_links))

    signature = "\n".join(contact_parts)

    email_body = f"""{salutation}

{opening}

{middle}

{closing}

Best regards,

{signature}"""

    return subject, email_body


def generate_outreach_draft(
    posting: JobPosting,
    profile: UserProfile,
    result: EvaluationResult,
    config: Settings,
) -> Path:
    """Generate a human-in-the-loop plain-text outreach draft with a mailto: link."""
    config.ensure_directories()
    company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)
    clean_base = generate_clean_resume_filename(profile.name, company, posting.title).replace("_Resume.pdf", "")
    output_path = config.matches_dir / f"{clean_base}_outreach.txt"

    subject, email_body = build_grounded_email_pitch(posting, profile, result)

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

            safe_title = job.title.replace("|", "-").replace("[", "\\[").replace("]", "\\]")
            safe_link = job.link.replace(" ", "%20")

            row = f"| **{result.fit_score}** | [{safe_title}]({safe_link}) | {job.source} | {comp} | [Resume]({resume_rel}) | [Outreach]({outreach_rel}) |"
            f.write(row + "\n")

    logger.info(f"Updated daily digest: {digest_path}")
    return digest_path


def blocked_url_fetcher(url: str, *args, **kwargs):
    """Strict URL fetcher that rejects all external and local URI resolution to prevent SSRF and LFI."""
    raise PermissionError(
        f"URL fetching is strictly forbidden in sandboxed PDF generation: {url}"
    )

blocked_url_fetcher._fail_on_errors = True


def export_markdown_to_pdf(
    markdown_path: Path,
    output_pdf_path: Optional[Path] = None,
) -> Path:
    """Compile a Markdown resume artifact into a sandboxed ATS-compliant PDF."""
    if not markdown_path.exists():
        raise FileNotFoundError(f"Markdown file not found: {markdown_path}")

    if output_pdf_path is None:
        output_pdf_path = markdown_path.with_suffix(".pdf")

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    md_content = markdown_path.read_text(encoding="utf-8")

    import markdown
    import weasyprint
    from bs4 import BeautifulSoup

    # Parse Markdown into HTML
    raw_html_body = markdown.markdown(
        md_content,
        extensions=["extra", "sane_lists"],
    )

    # Decompose dangerous or layout-disrupting inline tags
    soup = BeautifulSoup(raw_html_body, "html.parser")
    for tag in soup(["script", "style", "iframe", "object", "embed", "form", "applet"]):
        tag.decompose()
    html_body = str(soup)

    # Load ATS resume styles from single source of truth
    css_candidates = [
        Path("templates/resume_styles.css"),
        Path(__file__).resolve().parent.parent / "templates" / "resume_styles.css",
    ]
    css_path = next((p for p in css_candidates if p.exists()), None)
    if css_path:
        css_content = css_path.read_text(encoding="utf-8")
    else:
        logger.warning("templates/resume_styles.css not found; using minimal baseline styles.")
        css_content = "@page { size: letter portrait; margin: 0.5in; } body { font-family: sans-serif; font-size: 10pt; line-height: 1.4; color: #111; }"

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Resume</title>
<style>
{css_content}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""

    try:
        doc = weasyprint.HTML(string=full_html, url_fetcher=blocked_url_fetcher)
        doc.write_pdf(target=str(output_pdf_path))
    except BaseException as e:
        if isinstance(e, PermissionError) or (e.__cause__ and isinstance(e.__cause__, PermissionError)):
            raise PermissionError(f"Sandboxed PDF engine blocked unauthorized URL access: {e}") from e
        raise

    logger.info(f"Generated sandboxed ATS PDF: {output_pdf_path}")
    return output_pdf_path


