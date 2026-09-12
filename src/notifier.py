"""Outbound transactional email notification module using Resend."""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Optional
import urllib.parse

from jinja2 import Environment, FileSystemLoader, select_autoescape
import resend

from src.config import Settings, get_settings
from src.schemas import EvaluationResult, JobPosting

logger = logging.getLogger("beacon.notifier")


def sanitize_link(url: str) -> str:
    """Ensure URL strictly adheres to HTTP or HTTPS protocols to prevent scheme hijacking."""
    url = url.strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return "#"


def extract_target_email(job: JobPosting) -> Optional[str]:
    """
    Extract contact email from job metadata or via regex in description/raw_text.
    Checks job.contact_email, job.description, and job.raw_text.
    """
    contact = getattr(job, "contact_email", None)
    if contact and isinstance(contact, str) and "@" in contact:
        clean = contact.strip()
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean):
            return clean

    text = getattr(job, "description", None) or getattr(job, "raw_text", "")
    if text:
        matches = re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text)
        for m in matches:
            if not m.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
                return m

    return None


def extract_mailto_from_outreach(outreach_path: Path) -> Optional[str]:
    """Extract one-click mailto: link from generated outreach text file if present."""
    if not outreach_path.exists():
        return None
    try:
        content = outreach_path.read_text(encoding="utf-8")
        match = re.search(r"(mailto:\S+)", content)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"Could not extract mailto from {outreach_path}: {e}")
    return None


def extract_draft_body_from_outreach(outreach_path: Path) -> Optional[str]:
    """Extract the email draft body from the outreach text file if present."""
    if not outreach_path.exists():
        return None
    try:
        content = outreach_path.read_text(encoding="utf-8")
        match = re.search(r"BODY:\s*\n(.*?)(?:\n-{3,}|\n={3,}|\nONE-CLICK|\Z)", content, re.DOTALL)
        if match:
            return match.group(1).strip()
        mailto_match = re.search(r"mailto:[^?\s]*\?([^#\s]*)", content)
        if mailto_match:
            query = urllib.parse.parse_qs(mailto_match.group(1))
            if "body" in query and query["body"]:
                return query["body"][0].strip()
    except Exception as e:
        logger.debug(f"Could not extract draft body from {outreach_path}: {e}")
    return None


def get_email_jinja_env(template_dir: Path = Path("templates")) -> Environment:
    """Initialize and return Jinja2 environment for email alerts."""
    search_paths = [str(template_dir)]
    base_dir = Path(__file__).resolve().parent.parent / "templates"
    if str(base_dir) not in search_paths:
        search_paths.append(str(base_dir))
    return Environment(
        loader=FileSystemLoader(search_paths),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_notification_html(
    job: JobPosting,
    result: EvaluationResult,
    mailto_url: Optional[str] = None,
    target_email: Optional[str] = None,
    email_draft: Optional[str] = None,
    template_dir: Path = Path("templates"),
) -> str:
    """
    Render responsive, ATS-tailored HTML email template for candidate match notification.
    Passes target_email, posting_url, job_title, match_score, email_draft, and match_highlights
    to the template renderer.
    """
    clean_title = re.sub(r"[\r\n\t]+", " ", job.title).strip()
    raw_source = re.sub(r"[\r\n\t]+", " ", job.source).strip()

    # Human-friendly source formatting
    if "craigslist" in raw_source.lower():
        display_source = "Craigslist"
    elif "linkedin" in raw_source.lower():
        display_source = "LinkedIn"
    elif "indeed" in raw_source.lower():
        display_source = "Indeed"
    elif raw_source.startswith("email:"):
        display_source = raw_source.replace("email:", "").split("@")[-1].strip()
    else:
        display_source = raw_source

    safe_posting_url = sanitize_link(job.link)
    comp_text = result.estimated_compensation or "Not Stated"
    verdict = result.status.value

    # Determine target email if not explicitly provided
    if not target_email:
        target_email = extract_target_email(job)

    # If mailto_url was passed and target_email is still None, attempt extraction from mailto_url
    if not target_email and mailto_url:
        mailto_match = re.match(r"^mailto:([^?]+)", mailto_url)
        if mailto_match and mailto_match.group(1).strip():
            target_email = mailto_match.group(1).strip()

    # Determine email draft if not explicitly provided
    if not email_draft and mailto_url:
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(mailto_url).query)
        if "body" in parsed and parsed["body"]:
            email_draft = parsed["body"][0].strip()

    # Generate one-click Gmail compose web link
    gmail_compose_url = None
    if email_draft:
        subject = f"Application for {clean_title}"
        if "Application for " in email_draft:
            m = re.search(r"Application for ([^\n\r]+)", email_draft)
            if m:
                subject = f"Application for {m.group(1).strip()}"
        gmail_params = {
            "view": "cm",
            "fs": "1",
            "su": subject,
            "body": email_draft,
        }
        if target_email:
            gmail_params["to"] = target_email
        gmail_compose_url = f"https://mail.google.com/mail/?{urllib.parse.urlencode(gmail_params, quote_via=urllib.parse.quote)}"

    # Determine company name if detected
    company_name = getattr(result, "detected_company", None) or getattr(job, "detected_company", None)
    if not company_name:
        try:
            from src.evaluator import extract_company_from_posting
            company_name = extract_company_from_posting(job)
        except Exception:
            company_name = None

    block_company_url = "https://github.com/ddgiovinazzo/beacon-ai/actions/workflows/block_company.yml"

    try:
        env = get_email_jinja_env(template_dir)
        template = env.get_template("email_alert.html.j2")
        return template.render(
            target_email=target_email,
            posting_url=safe_posting_url,
            job_title=clean_title,
            job_source=display_source,
            display_source=display_source,
            company_name=company_name,
            block_company_url=block_company_url,
            match_score=result.fit_score,
            email_draft=email_draft,
            gmail_compose_url=gmail_compose_url,
            match_highlights=result.match_highlights,
            comp_text=comp_text,
            verdict=verdict,
            job=job,
            result=result,
        )
    except Exception as e:
        logger.error(f"Template rendering failed for email alert: {e}", exc_info=True)
        raise


def send_match_notification(
    job: JobPosting,
    result: EvaluationResult,
    resume_pdf_path: Path,
    outreach_txt_path: Path,
    config: Optional[Settings] = None,
    cover_letter_pdf_path: Optional[Path] = None,
) -> bool:
    """
    Dispatch a transactional email notification with attached PDF resume (and optional cover letter) via Resend.
    
    Returns True if sent successfully, False if skipped due to missing config or error.
    """
    if config is None:
        config = get_settings()

    # Graceful degradation if Resend credentials or destination address missing
    if not config.resend_api_key or not config.notification_email_to:
        logger.warning(
            "Resend notification skipped: RESEND_API_KEY or NOTIFICATION_EMAIL_TO is not configured."
        )
        return False

    resend.api_key = config.resend_api_key

    mailto_url = extract_mailto_from_outreach(outreach_txt_path)
    email_draft = extract_draft_body_from_outreach(outreach_txt_path)
    target_email = extract_target_email(job)

    html_content = build_notification_html(
        job=job,
        result=result,
        mailto_url=mailto_url,
        target_email=target_email,
        email_draft=email_draft,
    )

    clean_title = re.sub(r"[\r\n\t]+", " ", job.title).strip()
    clean_source = re.sub(r"[\r\n\t]+", " ", job.source).strip()
    subject = f"🎯 Job Match ({result.fit_score}/100): {clean_title} [{clean_source}]"

    # Prepare PDF attachments (Resume + Cover Letter)
    attachments = []
    if resume_pdf_path.exists():
        try:
            pdf_bytes = resume_pdf_path.read_bytes()
            attachments.append(
                {
                    "filename": resume_pdf_path.name,
                    "content": list(pdf_bytes),
                }
            )
        except Exception as e:
            logger.error(f"Failed reading Resume PDF attachment at {resume_pdf_path}: {e}")

    if cover_letter_pdf_path and cover_letter_pdf_path.exists():
        try:
            cl_bytes = cover_letter_pdf_path.read_bytes()
            attachments.append(
                {
                    "filename": cover_letter_pdf_path.name,
                    "content": list(cl_bytes),
                }
            )
        except Exception as e:
            logger.error(f"Failed reading Cover Letter PDF attachment at {cover_letter_pdf_path}: {e}")

    params: resend.Emails.SendParams = {
        "from": config.notification_email_from,
        "to": [config.notification_email_to],
        "subject": subject,
        "html": html_content,
    }

    if attachments:
        params["attachments"] = attachments

    try:
        response = resend.Emails.send(params)
        logger.info(f"Notification email successfully dispatched via Resend. Response: {response}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email notification via Resend: {e}")
        return False
