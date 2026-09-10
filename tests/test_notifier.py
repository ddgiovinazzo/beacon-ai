"""Unit tests for outbound transactional email notifier module."""

from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from src.config import Settings
from src.notifier import (
    build_notification_html,
    extract_mailto_from_outreach,
    send_match_notification,
)
from src.schemas import EvaluationResult, EvaluationStatus, JobPosting


@pytest.fixture
def sample_job() -> JobPosting:
    return JobPosting(
        title="Full Charge Bookkeeper",
        link="https://hudsonvalley.craigslist.org/acc/1234567.html",
        raw_text="<untrusted_job_posting>Full charge bookkeeper needed. QuickBooks experience required.</untrusted_job_posting>",
        source="craigslist",
    )


@pytest.fixture
def sample_result() -> EvaluationResult:
    return EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=92,
        estimated_compensation="$28 - $32/hr",
        tier_evaluated=2,
        match_highlights=[
            "Direct experience with QuickBooks Online",
            "Compensation exceeds minimum wage floor ($25/hr)",
        ],
    )


def test_extract_mailto_from_outreach(tmp_path: Path):
    """Verify regex extraction of mailto: URIs from generated outreach drafts."""
    outreach_file = tmp_path / "outreach.txt"
    outreach_file.write_text(
        """ONE-CLICK MAILTO LINK:
mailto:?subject=Application%20for%20Bookkeeper&body=Dear%20Team
===============================================================
""",
        encoding="utf-8",
    )

    mailto_link = extract_mailto_from_outreach(outreach_file)
    assert mailto_link == "mailto:?subject=Application%20for%20Bookkeeper&body=Dear%20Team"


def test_extract_mailto_nonexistent_file(tmp_path: Path):
    """Verify graceful handling when outreach draft file is missing."""
    assert extract_mailto_from_outreach(tmp_path / "nonexistent.txt") is None


def test_build_notification_html(sample_job: JobPosting, sample_result: EvaluationResult):
    """Verify HTML template structure and content escaping."""
    html_out = build_notification_html(
        sample_job,
        sample_result,
        mailto_url="mailto:?subject=Test",
    )
    assert "Full Charge Bookkeeper" in html_out
    assert "92/100 MATCH" in html_out
    assert "$28 - $32/hr" in html_out
    assert "QuickBooks Online" in html_out
    assert "mailto:?subject=Test" in html_out


def test_send_match_notification_missing_credentials(
    sample_job: JobPosting,
    sample_result: EvaluationResult,
    tmp_path: Path,
):
    """Verify notification gracefully aborts and returns False when credentials are missing."""
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    outreach_path = tmp_path / "outreach.txt"
    outreach_path.write_text("mailto:?test", encoding="utf-8")

    # Missing API key
    cfg1 = Settings(resend_api_key=None, notification_email_to="test@example.com")
    assert not send_match_notification(sample_job, sample_result, pdf_path, outreach_path, config=cfg1)

    # Missing destination email
    cfg2 = Settings(resend_api_key="re_123", notification_email_to=None)
    assert not send_match_notification(sample_job, sample_result, pdf_path, outreach_path, config=cfg2)


@patch("resend.Emails.send")
def test_send_match_notification_success(
    mock_resend_send: MagicMock,
    sample_job: JobPosting,
    sample_result: EvaluationResult,
    tmp_path: Path,
):
    """Verify successful email payload compilation and attachment encoding."""
    mock_resend_send.return_value = {"id": "msg_123456789"}

    pdf_path = tmp_path / "bookkeeper_resume.pdf"
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    pdf_path.write_bytes(pdf_bytes)

    outreach_path = tmp_path / "bookkeeper_outreach.txt"
    outreach_path.write_text(
        "ONE-CLICK MAILTO LINK: mailto:?subject=Job%20Application",
        encoding="utf-8",
    )

    config = Settings(
        resend_api_key="re_test_key_abc123",
        notification_email_to="candidate@ddgiovinazzo.com",
        notification_email_from="BeaconAI <alerts@ddgiovinazzo.com>",
    )

    success = send_match_notification(
        sample_job,
        sample_result,
        pdf_path,
        outreach_path,
        config=config,
    )

    assert success is True
    assert mock_resend_send.called
    params = mock_resend_send.call_args[0][0]

    assert params["from"] == "BeaconAI <alerts@ddgiovinazzo.com>"
    assert params["to"] == ["candidate@ddgiovinazzo.com"]
    assert "92/100" in params["subject"]
    assert "Full Charge Bookkeeper" in params["subject"]
    assert len(params["attachments"]) == 1
    assert params["attachments"][0]["filename"] == "bookkeeper_resume.pdf"
    assert params["attachments"][0]["content"] == list(pdf_bytes)


@patch("resend.Emails.send", side_effect=RuntimeError("API Gateway Timeout"))
def test_send_match_notification_api_error_handling(
    mock_resend_send: MagicMock,
    sample_job: JobPosting,
    sample_result: EvaluationResult,
    tmp_path: Path,
):
    """Verify unhandled API exceptions return False and don't halt application."""
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    outreach_path = tmp_path / "outreach.txt"
    outreach_path.write_text("mailto:?test", encoding="utf-8")

    config = Settings(
        resend_api_key="re_valid_key",
        notification_email_to="test@example.com",
    )

    success = send_match_notification(
        sample_job,
        sample_result,
        pdf_path,
        outreach_path,
        config=config,
    )
    assert success is False
