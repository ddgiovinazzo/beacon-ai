"""Unit tests for inbound email ingestion and IMAP alert processing."""

import email
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from main import app
from src.config import Settings, get_settings
from src.ingestion import (
    decode_email_header,
    fetch_imap_emails,
    parse_craigslist_alert_email,
    parse_email_message,
    parse_generic_job_alert_email,
)


def test_decode_email_header():
    """Verify decoding of plain and RFC 2047 MIME encoded headers."""
    assert decode_email_header("Simple Subject") == "Simple Subject"
    assert decode_email_header(None) == ""
    # Encoded word: "Craigslist Alert" in UTF-8 base64
    encoded = "=?utf-8?B?Q3JhaWdzbGlzdCBBbGVydA==?="
    assert decode_email_header(encoded) == "Craigslist Alert"


def test_parse_craigslist_alert_email_html():
    """Verify parsing multiple job listings from realistic Craigslist alert HTML."""
    sample_html = """
    <html>
    <body>
        <h2>craigslist alert: "admin" in "hudson valley"</h2>
        <table>
            <tr>
                <td>
                    <a href="https://hudsonvalley.craigslist.org/ofc/d/white-plains-administrative-assistant/7812345678.html">
                        Administrative Assistant
                    </a>
                </td>
                <td>$22/hr</td>
                <td>(White Plains)</td>
            </tr>
            <tr>
                <td>
                    <a href="https://hudsonvalley.craigslist.org/ofc/d/kingston-remote-dispatcher/7812345679.html">
                        Remote Dispatcher
                    </a>
                </td>
                <td>$700/wk</td>
                <td>(Kingston)</td>
            </tr>
            <tr>
                <td>
                    <!-- Management and help links should be discarded -->
                    <a href="https://www.craigslist.org/about/help">Help</a>
                    <a href="https://accounts.craigslist.org/sub/manage">Manage Alerts</a>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    postings = parse_craigslist_alert_email(sample_html, "", date_str="Fri, 11 Sep 2026 08:00:00 -0400")

    assert len(postings) == 2
    p1 = postings[0]
    assert p1.title == "Administrative Assistant"
    assert p1.link == "https://hudsonvalley.craigslist.org/ofc/d/white-plains-administrative-assistant/7812345678.html"
    assert p1.source == "email:craigslist"
    assert p1.published == "Fri, 11 Sep 2026 08:00:00 -0400"
    assert "<untrusted_job_posting>" in p1.raw_text
    assert "White Plains" in p1.raw_text

    p2 = postings[1]
    assert p2.title == "Remote Dispatcher"
    assert p2.link == "https://hudsonvalley.craigslist.org/ofc/d/kingston-remote-dispatcher/7812345679.html"
    assert "Kingston" in p2.raw_text


def test_parse_craigslist_alert_email_plain_text_fallback():
    """Verify fallback extraction from plain-text email bodies."""
    plain_text = """
    New matches for your search "ofc":

    Office Assistant - $20/hr
    https://hudsonvalley.craigslist.org/ofc/d/middletown-office-assistant/7812345680.html

    Front Desk Clerk - Part Time
    https://newyork.craigslist.org/mnh/ofc/d/new-york-front-desk/7812345681.html

    Manage your alerts:
    https://accounts.craigslist.org/sub/manage
    """

    postings = parse_craigslist_alert_email("", plain_text, date_str="Fri, 11 Sep 2026 09:00:00 -0400")
    assert len(postings) == 2
    assert "7812345680.html" in postings[0].link
    assert "7812345681.html" in postings[1].link
    assert postings[0].source == "email:craigslist"


def test_parse_generic_job_alert_email():
    """Verify parsing of generic job board alert emails (e.g. LinkedIn or Indeed)."""
    sample_html = """
    <html>
    <body>
        <div class="job-card">
            <a href="https://www.linkedin.com/jobs/view/data-entry-clerk-123456">Data Entry Specialist</a>
            <p>Acme Logistics - Remote - Full-time</p>
        </div>
        <div class="job-card">
            <a href="https://www.linkedin.com/jobs/view/accounts-payable-clerk-789012">Accounts Payable Coordinator</a>
            <p>Global Services - New York, NY</p>
        </div>
        <a href="https://www.linkedin.com/help/privacy">Privacy Policy</a>
    </body>
    </html>
    """

    postings = parse_generic_job_alert_email(
        sample_html,
        "",
        subject="Your Daily Job Alert: Data Entry & Accounts",
        sender="jobalerts-noreply@linkedin.com",
    )

    assert len(postings) == 2
    assert postings[0].title == "Data Entry Specialist"
    assert "linkedin.com/jobs/view/data-entry" in postings[0].link
    assert postings[0].source == "email:linkedin.com"
    assert postings[1].title == "Accounts Payable Coordinator"


def test_parse_email_message_multipart_routing():
    """Verify MIME multipart decoding and provider routing based on sender."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "craigslist alert: admin jobs"
    msg["From"] = "robot@craigslist.org"
    msg["Date"] = "Fri, 11 Sep 2026 10:00:00 -0400"

    html = """
    <html><body>
        <a href="https://hudsonvalley.craigslist.org/ofc/d/clerk/7812345699.html">Office Clerk</a>
    </body></html>
    """
    msg.attach(MIMEText("Office Clerk: https://hudsonvalley.craigslist.org/ofc/d/clerk/7812345699.html", "plain"))
    msg.attach(MIMEText(html, "html"))

    postings = parse_email_message(msg)
    assert len(postings) == 1
    assert postings[0].title == "Office Clerk"
    assert postings[0].source == "email:craigslist"


def test_fetch_imap_emails_unconfigured():
    """Verify fetch_imap_emails returns empty list when IMAP is not configured."""
    settings = Settings(imap_server=None, imap_username=None, imap_password=None)
    assert not settings.is_imap_configured
    assert fetch_imap_emails(settings) == []


def test_fetch_imap_emails_mock_success():
    """Verify fetch_imap_emails connects, fetches, marks seen, and returns postings."""
    settings = Settings(
        imap_server="imap.example.com",
        imap_port=993,
        imap_username="user@example.com",
        imap_password="app_password",
        imap_mailbox="INBOX",
        imap_search_criteria="UNSEEN",
        imap_mark_seen=True,
    )
    assert settings.is_imap_configured

    sample_rfc822 = (
        b"From: robot@craigslist.org\r\n"
        b"Subject: craigslist alert: office\r\n"
        b"Date: Fri, 11 Sep 2026 10:00:00 -0400\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><a href=\"https://hudsonvalley.craigslist.org/ofc/d/aide/7899999.html\">Office Aide</a></body></html>"
    )

    mock_imap = MagicMock()
    mock_imap.select.return_value = ("OK", [b"1"])
    mock_imap.search.return_value = ("OK", [b"101 102"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {100}", sample_rfc822), b")"])

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        postings = fetch_imap_emails(settings)

    mock_imap.login.assert_called_once_with("user@example.com", "app_password")
    mock_imap.select.assert_called_once_with("INBOX")
    mock_imap.search.assert_called_once_with(None, "UNSEEN")
    # Verified that 2 message IDs were processed
    assert mock_imap.fetch.call_count == 2
    # Verified that messages were marked seen
    assert mock_imap.store.call_count == 2
    mock_imap.store.assert_called_with(b"102", "+FLAGS", "\\Seen")
    mock_imap.logout.assert_called_once()

    assert len(postings) == 2
    assert postings[0].title == "Office Aide"
    assert postings[0].link == "https://hudsonvalley.craigslist.org/ofc/d/aide/7899999.html"


def test_fetch_imap_emails_handles_auth_error():
    """Verify graceful handling when IMAP authentication fails."""
    import imaplib

    settings = Settings(
        imap_server="imap.example.com",
        imap_username="user@example.com",
        imap_password="bad_password",
    )

    mock_imap = MagicMock()
    mock_imap.login.side_effect = imaplib.IMAP4.error("Authentication failed")

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        postings = fetch_imap_emails(settings)

    assert postings == []
    mock_imap.logout.assert_called_once()


def test_cli_scan_with_email_integration(tmp_path: Path, monkeypatch):
    """Verify CLI scan integrates IMAP email source alongside feeds."""
    test_db = tmp_path / "email_scan_test.db"
    monkeypatch.setenv("DB_PATH", str(test_db))
    monkeypatch.setenv("IMAP_SERVER", "imap.example.com")
    monkeypatch.setenv("IMAP_USERNAME", "test@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "secret")
    get_settings.cache_clear()

    sample_rfc822 = (
        b"From: robot@craigslist.org\r\n"
        b"Subject: craigslist alert: office\r\n"
        b"Date: Fri, 11 Sep 2026 10:00:00 -0400\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><a href=\"https://hudsonvalley.craigslist.org/ofc/d/aide/7899999.html\">Office Aide</a></body></html>"
    )

    mock_imap = MagicMock()
    mock_imap.select.return_value = ("OK", [b"1"])
    mock_imap.search.return_value = ("OK", [b"101"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {100}", sample_rfc822), b")"])

    runner = CliRunner()
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        result = runner.invoke(
            app,
            [
                "scan",
                "--profile",
                "profiles/bookkeeper.json.example",
                "--feed",
                "tests/fixtures/sample_jobs.xml",
                "--dry-run",
                "--check-email",
            ],
        )

    assert result.exit_code == 0
    assert "Scanning Inbound Email (IMAP)" in result.output
    assert "Office Aide" in result.output
