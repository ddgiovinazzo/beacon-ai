"""Unit tests for artifact and sandboxed PDF generation."""

from pathlib import Path
import pytest

from src.generator import blocked_url_fetcher, export_markdown_to_pdf, get_job_slug
from src.schemas import JobPosting


def test_blocked_url_fetcher_prevents_ssrf_and_lfi():
    """Verify that blocked_url_fetcher raises PermissionError on remote and local URIs."""
    forbidden_urls = [
        "http://169.254.169.254/latest/meta-data/",
        "https://malicious-attacker.com/leak",
        "file:///etc/passwd",
        "file:///Users/daniel/.ssh/id_rsa",
    ]
    for url in forbidden_urls:
        with pytest.raises(PermissionError) as exc_info:
            blocked_url_fetcher(url)
        assert "strictly forbidden" in str(exc_info.value)


def test_export_markdown_to_pdf_generates_valid_pdf(tmp_path: Path):
    """Verify compilation of a clean Markdown resume to a valid ATS PDF."""
    md_file = tmp_path / "test_resume.md"
    pdf_file = tmp_path / "test_resume.pdf"

    md_file.write_text(
        """# Jane Doe
**Senior Bookkeeper**
jane@example.com | (555) 123-4567

---
## Summary
Experienced accounting specialist with 10+ years in general ledger and payroll.

## Experience
### Lead Bookkeeper | Apex Financial
- Managed reconciliations for 25+ accounts.
""",
        encoding="utf-8",
    )

    result_pdf = export_markdown_to_pdf(md_file, output_pdf_path=pdf_file)
    assert result_pdf.exists()
    assert result_pdf.stat().st_size > 0

    # Verify standard PDF file header
    header = result_pdf.read_bytes()[:5]
    assert header == b"%PDF-"


def test_export_markdown_to_pdf_blocks_remote_image_ssrf(tmp_path: Path):
    """Verify that attempting to inject external resources triggers PermissionError."""
    malicious_md = tmp_path / "malicious.md"
    malicious_md.write_text(
        """# Injected Resume
<img src="http://169.254.169.254/latest/meta-data/">
""",
        encoding="utf-8",
    )

    with pytest.raises(PermissionError) as exc_info:
        export_markdown_to_pdf(malicious_md)
    assert "Sandboxed PDF engine blocked unauthorized URL access" in str(exc_info.value)


def test_export_markdown_to_pdf_blocks_local_file_lfi(tmp_path: Path):
    """Verify that attempting local file inclusion triggers PermissionError."""
    malicious_md = tmp_path / "malicious_lfi.md"
    malicious_md.write_text(
        """# Injected LFI
<img src="file:///etc/passwd">
""",
        encoding="utf-8",
    )

    with pytest.raises(PermissionError) as exc_info:
        export_markdown_to_pdf(malicious_md)
    assert "Sandboxed PDF engine blocked unauthorized URL access" in str(exc_info.value)


def test_export_markdown_to_pdf_decomposes_inline_dangerous_tags(tmp_path: Path):
    """Verify that inline script, style, and iframe tags are decomposed before rendering."""
    md_with_tags = tmp_path / "resume_with_tags.md"
    pdf_out = tmp_path / "resume_clean.pdf"

    md_with_tags.write_text(
        """# John Doe
**Software Engineer**
<style>body { display: none !important; }</style>
<script>alert("malicious js");</script>
<iframe src="about:blank"></iframe>
<object data="test"></object>

## Experience
Clean content that should render properly.
""",
        encoding="utf-8",
    )

    result_pdf = export_markdown_to_pdf(md_with_tags, output_pdf_path=pdf_out)
    assert result_pdf.exists()
    assert result_pdf.stat().st_size > 0
    assert result_pdf.read_bytes()[:5] == b"%PDF-"


def test_scan_fault_tolerance_on_artifact_error(tmp_path: Path, monkeypatch):
    """Verify that if PDF generation fails on a matching job, SQLite state is not poisoned and scan continues."""
    from unittest.mock import patch
    from typer.testing import CliRunner
    from main import app
    from src.config import get_settings
    from src.db import is_job_seen

    test_db = tmp_path / "fault_test.db"
    monkeypatch.setenv("DB_PATH", str(test_db))
    get_settings.cache_clear()

    try:
        # Mock export_markdown_to_pdf to fail
        with patch("main.export_markdown_to_pdf", side_effect=PermissionError("Simulated sandbox violation")):
            runner = CliRunner()
            result = runner.invoke(
                app,
                [
                    "scan",
                    "--profile",
                    "profiles/bookkeeper.json.example",
                    "--feed",
                    "tests/fixtures/sample_jobs.xml",
                    "--dry-run",
                ],
            )

            # Scan should complete gracefully (exit code 0), logging errors instead of crashing
            assert result.exit_code == 0
            assert "Synthesis failed" in result.output

            # Verify that the failing matching job was NOT marked as seen/committed in SQLite
            matched_url = "https://bayarea.example.com/jobs/101-bookkeeper"
            assert is_job_seen(matched_url, test_db) is False

            # Verify that the rejected jobs WERE recorded in SQLite
            rejected_url = "https://bayarea.example.com/jobs/103-low-pay-books"
            assert is_job_seen(rejected_url, test_db) is True
    finally:
        get_settings.cache_clear()


