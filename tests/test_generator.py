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
        "file:///home/user/.ssh/id_rsa",
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


def test_dynamic_role_and_project_selection_by_tags():
    """Verify that roles and projects are dynamically filtered based on tag alignment with job posting."""
    from src.generator import create_deterministic_tailored_data
    from src.schemas import (
        EducationEntry,
        EngineeringProject,
        ExperienceRole,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    profile = UserProfile(
        name="Taylor Jordan",
        email="taylor@example.com",
        phone="555-0199",
        location="Metropolis, NY",
        constraints=UserConstraints(min_hourly_rate=20.0),
        master_experience=MasterExperience(
            target_titles=["Records Clerk", "Software Engineer"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Records Clerk",
                    organization="City Services Agency",
                    location="Metropolis, NY",
                    start_date="2020",
                    end_date="Present",
                    tags=["clerical", "data_entry", "records"],
                    bullets=["Maintained municipal indexing system at 99.9% accuracy."],
                ),
                ExperienceRole(
                    id="r2",
                    title="Backend Software Contributor",
                    organization="Cloud Labs Inc",
                    location="Metropolis, NY",
                    start_date="2018",
                    end_date="2020",
                    tags=["software", "backend", "cloud"],
                    bullets=["Implemented REST APIs in Python."],
                ),
            ],
            engineering_projects=[
                EngineeringProject(
                    id="p1",
                    name="Distributed Task Engine",
                    tags=["software", "backend", "concurrency"],
                    bullets=["Asynchronous message worker in Python."],
                )
            ],
            tools_and_technologies=["Microsoft Excel", "Python", "SQL"],
        ),
    )

    clerical_job = JobPosting(
        title="Data Processing Clerk",
        link="https://example.com/clerk-job",
        raw_text="Seeking an accurate data entry and clerical specialist for records management.",
        source="example.com",
    )

    resume_data = create_deterministic_tailored_data(clerical_job, profile)
    selected_role_titles = [r.title for r in resume_data.tailored_experience]

    assert "Records Clerk" in selected_role_titles
    assert resume_data.tailored_projects == []


def test_dynamic_geographic_education_heuristics():
    """Verify that local education is prioritized for local jobs and omitted for remote tech jobs."""
    from src.generator import create_deterministic_tailored_data
    from src.schemas import (
        EducationEntry,
        ExperienceRole,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    profile = UserProfile(
        name="Jordan Lee",
        email="jordan@example.com",
        phone="555-0144",
        location="Metropolis, NY",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["Analyst"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Operations Analyst",
                    organization="City Agency",
                    location="Metropolis, NY",
                    start_date="2021",
                    end_date="Present",
                    tags=["analytics", "operations"],
                    bullets=["Analyzed reports."],
                )
            ],
            education=[
                EducationEntry(
                    id="e1",
                    institution="Metropolis Community College",
                    degree="Studies in Business",
                    tags=["local"],
                ),
                EducationEntry(
                    id="e2",
                    institution="Global Tech Institute",
                    degree="Software Engineering Certificate",
                    tags=["tech", "universal"],
                ),
            ],
        ),
    )

    # 1. Local municipal posting matching candidate location
    local_job = JobPosting(
        title="Operations Analyst",
        link="https://example.com/local-job",
        raw_text="Local municipal opening in Metropolis, NY for an operations analyst.",
        source="example.com",
    )
    local_data = create_deterministic_tailored_data(local_job, profile)
    local_institutions = [e.institution for e in local_data.tailored_education]
    assert "Metropolis Community College" in local_institutions

    # 2. 100% Remote technology posting
    remote_job = JobPosting(
        title="Remote Systems Analyst",
        link="https://example.com/remote-job",
        raw_text="100% remote telecommute opportunity for a cloud systems analyst.",
        source="example.com",
    )
    remote_data = create_deterministic_tailored_data(remote_job, profile)

    remote_institutions = [e.institution for e in remote_data.tailored_education]
    assert "Metropolis Community College" not in remote_institutions
    assert "Global Tech Institute" in remote_institutions


def test_scan_multi_feed_cli_and_target_feed_urls(tmp_path: Path, monkeypatch):
    """Verify that scan processes multiple --feed CLI arguments and falls back to TARGET_FEED_URLS."""
    from typer.testing import CliRunner
    from main import app
    from src.config import get_settings

    test_db = tmp_path / "multi_feed_test.db"
    monkeypatch.setenv("DB_PATH", str(test_db))
    get_settings.cache_clear()

    runner = CliRunner()

    # 1. Multiple --feed options on CLI
    result = runner.invoke(
        app,
        [
            "scan",
            "--profile",
            "profiles/bookkeeper.json.example",
            "--feed",
            "tests/fixtures/sample_jobs.xml",
            "--feed",
            "tests/fixtures/sample_jobs.xml",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "Target Feeds: 2 configured" in result.output

    # 2. Fallback to TARGET_FEED_URLS environment variable
    monkeypatch.setenv("TARGET_FEED_URLS", "tests/fixtures/sample_jobs.xml\ntests/fixtures/sample_jobs.xml")
    get_settings.cache_clear()

    result_env = runner.invoke(
        app,
        [
            "scan",
            "--profile",
            "profiles/bookkeeper.json.example",
            "--dry-run",
        ],
    )
    assert result_env.exit_code == 0
    assert "Target Feeds: 2 configured" in result_env.output



