"""Outbound transactional email notification module using Resend."""

import html
import logging
import re
from pathlib import Path
from typing import Optional

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


def build_notification_html(
    job: JobPosting,
    result: EvaluationResult,
    mailto_url: Optional[str] = None,
) -> str:
    """Render responsive, ATS-tailored HTML email template for candidate match notification."""
    safe_title = html.escape(re.sub(r"[\r\n\t]+", " ", job.title).strip())
    safe_source = html.escape(re.sub(r"[\r\n\t]+", " ", job.source).strip())
    safe_link = html.escape(sanitize_link(job.link))
    comp_text = html.escape(result.estimated_compensation or "Not Stated")

    highlights_html = ""
    if result.match_highlights:
        items = "".join(f"<li style='margin-bottom: 6px;'>{html.escape(h)}</li>" for h in result.match_highlights)
        highlights_html = f"""
        <div style="margin: 16px 0;">
            <h3 style="font-size: 14px; text-transform: uppercase; color: #475569; margin-bottom: 8px;">Key Match Highlights</h3>
            <ul style="padding-left: 20px; color: #1e293b; margin: 0;">
                {items}
            </ul>
        </div>
        """

    mailto_cta_html = ""
    if mailto_url:
        safe_mailto = html.escape(mailto_url)
        mailto_cta_html = f"""
        <div style="margin: 24px 0 16px 0; text-align: center;">
            <a href="{safe_mailto}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; font-weight: 600; border-radius: 6px; display: inline-block;">
                ✉️ Open Pre-Filled Application Outreach
            </a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px; }}
  .card {{ background-color: #ffffff; max-width: 600px; margin: 0 auto; border-radius: 8px; border: 1px solid #e2e8f0; overflow: hidden; }}
  .header {{ background-color: #0f172a; color: #ffffff; padding: 20px 24px; }}
  .badge {{ background-color: #10b981; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-weight: bold; font-size: 12px; display: inline-block; }}
  .body {{ padding: 24px; color: #334155; line-height: 1.5; }}
  .meta-table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  .meta-table td {{ padding: 8px 12px; border: 1px solid #f1f5f9; font-size: 14px; }}
  .meta-label {{ background-color: #f8fafc; font-weight: 600; width: 35%; color: #475569; }}
  .footer {{ background-color: #f1f5f9; padding: 16px 24px; text-align: center; font-size: 12px; color: #64748b; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <span style="font-size: 12px; letter-spacing: 1px; text-transform: uppercase; color: #94a3b8;">BeaconAI Job Alert</span>
      <span class="badge">{result.fit_score}/100 MATCH</span>
    </div>
    <h1 style="margin: 12px 0 4px 0; font-size: 20px; color: #ffffff;">{safe_title}</h1>
    <div style="font-size: 14px; color: #cbd5e1;">Source: {safe_source}</div>
  </div>
  <div class="body">
    <table class="meta-table">
      <tr>
        <td class="meta-label">Direct Posting</td>
        <td><a href="{safe_link}" style="color: #2563eb; text-decoration: none; word-break: break-all;">{safe_link}</a></td>
      </tr>
      <tr>
        <td class="meta-label">Est. Compensation</td>
        <td><strong>{comp_text}</strong></td>
      </tr>
      <tr>
        <td class="meta-label">Evaluation Verdict</td>
        <td><span style="color: #10b981; font-weight: 600;">{result.status.value}</span></td>
      </tr>
    </table>

    {highlights_html}

    <p style="font-size: 14px; color: #64748b; margin-top: 16px;">
      📎 <strong>Attached:</strong> Tailored, single-page ATS-compliant PDF resume generated specifically for this role.
    </p>

    {mailto_cta_html}
  </div>
  <div class="footer">
    Sent autonomously by <strong>BeaconAI</strong> Job Intelligence Engine.<br>
    Deterministic Zero-Trust Architecture • All rights reserved.
  </div>
</div>
</body>
</html>
"""


def send_match_notification(
    job: JobPosting,
    result: EvaluationResult,
    resume_pdf_path: Path,
    outreach_txt_path: Path,
    config: Optional[Settings] = None,
) -> bool:
    """
    Dispatch a transactional email notification with attached PDF resume via Resend.
    
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
    html_content = build_notification_html(job, result, mailto_url=mailto_url)

    clean_title = re.sub(r"[\r\n\t]+", " ", job.title).strip()
    clean_source = re.sub(r"[\r\n\t]+", " ", job.source).strip()
    subject = f"🎯 Job Match ({result.fit_score}/100): {clean_title} [{clean_source}]"

    # Prepare PDF attachment
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
            logger.error(f"Failed reading PDF attachment at {resume_pdf_path}: {e}")

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
