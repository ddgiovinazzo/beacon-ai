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
    """Sanitize company name to prevent source URLs or feed filenames from leaking into resume text.

    If company_name is None, empty, ends in an extension/TLD (e.g. .xml, .com, .org, .net),
    or matches a URL scheme, returns None.
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


def create_deterministic_tailored_data(
    posting: JobPosting,
    profile: UserProfile,
) -> TailoredResumeData:
    """Generate deterministic tailored resume data when running dry-run or offline.
    
    Dynamically filters roles, projects, and education based on metadata tags and geographic context.
    """
    job_text = f"{posting.title}\n{posting.raw_text}\n{posting.source}".lower()

    # 1. Headline & Summary
    company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)
    target_role_title = clean_role_title(posting.title, company)

    tech_keywords = {"software", "engineer", "developer", "backend", "frontend", "fullstack", "python", "devops", "cloud", "data engineer"}
    is_tech_job = any(kw in job_text for kw in tech_keywords)

    headline = target_role_title
    impact_target = f"at {company}" if company else "in this role"
    if is_tech_job:
        summary = (
            f"Accomplished technical professional targeting the {target_role_title} role with hands-on "
            f"experience in scalable software systems, technical problem-solving, and operational excellence. "
            f"Prepared to deliver immediate value {impact_target}."
        )
    else:
        summary = (
            f"Accomplished professional targeting the {target_role_title} role with verified domain experience. "
            f"Proven track record delivering operational rigor, high accuracy, "
            f"and mission alignment. Prepared to make an immediate impact {impact_target}."
        )

    # 2. Dynamic Role Selection based on tag matching
    scored_roles = []
    for role in profile.master_experience.roles:
        score = 0
        # Tag matches
        for tag in role.tags:
            tag_clean = tag.lower().replace("_", " ")
            if tag_clean in job_text:
                score += 3
        # Title token matches
        for token in role.title.lower().split():
            if len(token) > 3 and token in job_text:
                score += 1
        # Bullet keyword overlap
        for bullet in role.bullets:
            for word in bullet.lower().split():
                if len(word) > 4 and word in job_text:
                    score += 0.1
        scored_roles.append((score, role))

    # Sort descending by relevance score
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
    
    # Check if job mentions candidate's local identifiers
    loc_tokens = [tok.strip().lower() for tok in profile.location.replace(",", " ").split() if len(tok.strip()) > 2]
    is_local_posting = any(tok in job_text for tok in loc_tokens) or bool(
        re.search(r"\b(?:local|county|district|municipal|town\s+of|city\s+of|civil\s+service)\b", job_text)
    )

    tailored_education = []
    for edu in profile.master_experience.education:
        edu_tags = [t.lower() for t in edu.tags]
        if is_local_posting and not is_remote:
            # Local posting: prioritize local tags and universal
            if "local" in edu_tags or "universal" in edu_tags or not edu_tags:
                tailored_education.append(edu)
        else:
            # Remote or non-local tech posting: include tech/universal and omit strictly local
            if "local" in edu_tags and "tech" not in edu_tags and "universal" not in edu_tags:
                continue
            tailored_education.append(edu)

    if not tailored_education:
        tailored_education = list(profile.master_experience.education)

    # 5. Dynamic Skills Categorization (Filtered for domain relevance)
    domain_skills = filter_skills_for_target_domain(profile.master_experience.tools_and_technologies, job_text)
    matched_skills = [s for s in domain_skills if s.lower() in job_text]
    unmatched_skills = [s for s in domain_skills if s.lower() not in job_text]
    ordered_skills = matched_skills + unmatched_skills

    half = max(len(ordered_skills) // 2, 1)
    categorized_skills: Dict[str, List[str]] = {
        "Core Technical & Domain Systems": ordered_skills[:half],
        "Workflows, Tools & Methodologies": ordered_skills[half:],
    }

    return TailoredResumeData(
        target_headline=headline,
        tailored_summary=summary,
        categorized_skills=categorized_skills,
        tailored_experience=selected_roles,
        tailored_projects=tailored_projects,
        tailored_education=tailored_education,
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

    active_model = config.llm_model or LLM_MODEL
    if not active_model:
        logger.error("No LLM model specified! Set LLM_MODEL environment variable or configure Settings.llm_model.")
        return create_deterministic_tailored_data(posting, profile)

    if not config.has_llm_credentials(active_model):
        return create_deterministic_tailored_data(posting, profile)

    try:
        import instructor
        import litellm

        config.sync_litellm_env()
        client = instructor.from_litellm(litellm.completion)

        target_company = extract_company_from_title(posting.title) or sanitize_target_company(posting.source)
        clean_title = clean_role_title(posting.title, target_company)
        domain_skills = filter_skills_for_target_domain(
            profile.master_experience.tools_and_technologies,
            f"{posting.title}\n{posting.raw_text}"
        )
        system_instruction = (
            "System Prompt: Strategic Resume Tailoring Agent\n\n"
            "Role & Objective:\n"
            "You are an expert technical recruiter and resume writer. Your objective is to analyze a provided Job Description (JD) "
            "and a preceding 'Context Prompt' (which contains the candidate's exact work history, accomplishments, and skills). "
            "You must first scan the JD for potential culture fit red flags. If it passes, you must adapt the base resume to "
            "perfectly align with the JD by acting as a top-down matching engine. You must strictly adhere to the candidate "
            "context, tone guardrails, and hard quantification rules.\n\n"
            "Candidate Context & Tone Guardrails:\n"
            "Rely strictly and exclusively on the candidate's verified work history, accomplishments, and narrative positioning provided in the Context Prompt.\n"
            "*CRITICAL TONE GUARDRAIL:* Your goal is to perfectly MATCH the Job Description's requested seniority level and domain. "
            "Never oversell the candidate at a tier higher than what the JD is asking for. Do not use high-level or senior-leaning action verbs "
            "(e.g., 'Architected', 'Spearheaded', 'Directed') UNLESS the JD explicitly uses those terms or specifically "
            "asks for that level of ownership. Otherwise, default to grounded, practical, domain-appropriate verbs (e.g., 'Built', 'Developed', 'Designed', 'Coordinated', 'Implemented', 'Collaborated').\n"
            "*CRITICAL FORMATTING GUARDRAIL:* Right before final output, perform a final parse and remove citation markers, source references, "
            "or brackets (e.g., [cite: 1], [source: 1]) that might be added from AI architecture.\n\n"
            "Step 1: Context Verification (STOP & CHECK):\n"
            "Verify that you have received the Context Prompt containing the candidate's work history and accomplishments before generating the tailored resume.\n\n"
            "Step 2: Culture Fit & Red Flag Scanner (STOP & WARN):\n"
            "Scan the JD for toxic workplace indicators ('work hard, play hard', 'we are a family', 'wear many hats', 'ninja', high-volume legacy staffing agency contracts). "
            "If detected, maintain strict professional boundaries and focus on sustainable, grounded, production-grade competencies.\n\n"
            "Step 3: JD Keyword Extraction (The Top-Down Scan):\n"
            "Silently parse the JD to extract:\n"
            "- Target seniority tier and requested years of experience.\n"
            "- Primary technical stack, domain tools, platforms, or systems requested.\n"
            "- Core responsibilities and pain points (e.g., cross-functional collaboration, data accuracy, process optimization, workflow execution, domain-specific tooling).\n"
            "- Specific vocabulary or action verbs the JD favors.\n\n"
            "Step 4: The Keyword-First Matching Algorithm (STRICT CONTEXT RELIANCE & HARD QUANTIFICATION):\n"
            "1. Search Context: For every core responsibility or keyword extracted from the JD, search the provided candidate accomplishments for the specific story that best demonstrates that competency.\n"
            "2. Draft the Bullet: Reframe that specific story using the JD's preferred vocabulary. You MUST rely COMPLETELY and EXCLUSIVELY on the Context Prompt for underlying facts. Do NOT invent, hallucinate, or add any skills or experiences not explicitly stated in the Context Prompt.\n"
            "3. Bullet Heading Format: You MUST format EVERY bullet in tailored_experience starting with a bold dynamic heading reflecting the JD competency, e.g.:\n"
            "   '**[Dynamic Competency Heading based on JD]:** [Tailored bullet point ending in a quantifiable result, based EXCLUSIVELY on Context Prompt]'\n"
            "   (Examples: '**Application Development:** Built...', '**Process Optimization:** Streamlined...', '**Data Accuracy:** Reconciled...', '**Cross-Functional Collaboration:** Collaborated with...')\n"
            "4. HARD QUANTIFICATION RULE (CRITICAL): You must ruthlessly edit the end of every single bullet point so it concludes with a concrete, measurable impact or definitive operational/technical resolution. NEVER end a bullet with vague filler like 'improving workflows', 'ensuring reliability', or 'optimizing operations'.\n"
            "   - Use Exact Numbers: Extract exact metrics from Context Prompt wherever possible (e.g., 'eliminating a 10-hour communication blocker', 'scaling across a 10-person team', 'processing 500+ records daily').\n"
            "   - Use Definitive Concrete Outcomes: If hard numbers are missing, end on the absolute functional, business, or technical result (e.g., 'enabling 100% offline functionality in zero-connectivity environments', 'eliminating manual data reconciliation bottlenecks without disrupting daily operations').\n\n"
            "Step 5: Tactical Experience Framing:\n"
            "- tailored_summary: Tweak the Professional Summary to directly mirror the JD's requested seniority tier and core requirements. "
            "Be highly strategic with stated years of experience: do NOT rigidly state an exact number of years if it might trigger over-qualification or mismatch the JD's requested tier (use phrasing like 'Proven experience' or 'Solid foundation' for lower tiers, and explicitly state years only when directly aligned with the target tier). Conclude with 'Prepared to make an immediate impact in this role' (or at verified company).\n"
            "- target_headline: Set to clean role title matching the JD (without employer name).\n"
            "- categorized_skills: Group candidate's actual matching skills into logical categories relevant to the role (e.g. 'Core Competencies', 'Tools & Technologies', 'Methodologies', or domain-specific groupings).\n"
            "- tailored_experience: Select 2-3 most relevant roles with 3-4 bullets each following the bold dynamic heading and hard quantification rules.\n"
            "- tailored_projects: If candidate has relevant portfolio or engineering projects, select up to 2 with bullets ending in quantifiable/concrete results; otherwise leave empty."
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
POSITIONING DIRECTIVE: {profile.master_experience.narrative_context or 'Aligned professional contributor'}

CANDIDATE MASTER ROLES & ACCOMPLISHMENTS:
{json.dumps([r.model_dump() for r in profile.master_experience.roles])}

CANDIDATE ENGINEERING PROJECTS:
{json.dumps([p.model_dump() for p in profile.master_experience.engineering_projects])}

CANDIDATE MASTER SKILLS (PRE-FILTERED FOR DOMAIN RELEVANCE):
{json.dumps(domain_skills)}

CANDIDATE EDUCATION BANK:
{json.dumps([e.model_dump() for e in profile.master_experience.education])}

INSTRUCTIONS:
1. target_headline: Set to "{clean_title}". Do NOT include the employer name.
2. tailored_summary: Write a dynamic 3-4 sentence summary mirroring the JD's requested seniority tier and core requirements. Apply tactical experience framing for years of experience.
3. categorized_skills: Group candidate's actual skills into logical domain categories relevant to this role.
4. tailored_experience: 2-3 most relevant roles. Format every bullet with bold heading '**[Competency Heading]:** [Grounded verb] ... [Quantifiable result / concrete outcome]'.
5. tailored_projects: Select relevant projects with quantifiable outcomes if applicable, else empty list.
6. tailored_education: Education credentials aligned with context.
"""

        active_model = config.llm_model or LLM_MODEL
        if not active_model:
            logger.error("No LLM model specified! Set LLM_MODEL environment variable or configure Settings.llm_model.")
            return create_deterministic_tailored_data(posting, profile)
        # Gemini 3+ models mandate temperature >= 1.0 to prevent degraded reasoning and infinite loops
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
        for role in resume_data.tailored_experience:
            if role.organization and role.location:
                role.organization = role.organization.strip(" |")
                role.location = role.location.strip(" |")
                if role.organization.endswith(f" {role.location}"):
                    role.organization = role.organization[:-len(role.location)-1].strip(" |")
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


