"""Unit tests for artifact and sandboxed PDF generation."""

from pathlib import Path
import pytest

from src.generator import blocked_url_fetcher, export_markdown_to_pdf
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


def test_generate_clean_resume_filename():
    """Verify recruiter-friendly filename generation with company and fallback conventions."""
    from src.generator import generate_clean_resume_filename

    # Format with company
    fn1 = generate_clean_resume_filename("Daniel Giovinazzo", "Aker Systems", "Principal Software Engineer")
    assert fn1 == "Daniel_Giovinazzo_AkerSystems_Resume.pdf"

    # Fallback to job title when company is None or whitespace
    fn2 = generate_clean_resume_filename("Daniel Giovinazzo", None, "Principal Software Engineer")
    assert fn2 == "Daniel_Giovinazzo_PrincipalSoftwareEngineer_Resume.pdf"

    # Special characters and punctuation in candidate name sanitized
    fn3 = generate_clean_resume_filename("Mary-Jane O'Connor", "Acme Corp!", "Lead Developer")
    assert fn3 == "MaryJane_OConnor_AcmeCorp_Resume.pdf"


def test_extract_company_from_title():
    """Verify extraction of company from delimited job posting titles."""
    from src.generator import extract_company_from_title

    assert extract_company_from_title("Google: Staff Software Engineer") == "Google"
    assert extract_company_from_title("Staff Software Engineer at Meta") == "Meta"
    assert extract_company_from_title("Stripe - Infrastructure Engineer") == "Stripe"
    assert extract_company_from_title("Senior Product Manager") is None


def test_resume_template_removes_watermark_and_formats_education(tmp_path: Path, monkeypatch):
    """Verify generated resume has no AI watermark and formats multiple education entries on distinct lines."""
    from src.config import Settings
    from src.generator import export_markdown_to_pdf, generate_tailored_resume
    from src.schemas import (
        EducationEntry,
        EvaluationResult,
        EvaluationStatus,
        ExperienceRole,
        JobPosting,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    matches_dir = tmp_path / "matches"
    test_settings = Settings(matches_dir=matches_dir, artifacts_dir=tmp_path)

    profile = UserProfile(
        name="Alex Morgan",
        email="alex@example.com",
        phone="555-0188",
        location="New York, NY",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["Software Engineer"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Software Engineer",
                    organization="TechCorp",
                    location="New York, NY",
                    start_date="2021",
                    end_date="Present",
                    tags=["software"],
                    bullets=["Built API services."],
                )
            ],
            education=[
                EducationEntry(
                    id="e1",
                    institution="State University of New York",
                    degree="B.S. in Computer Science",
                    end_date="2020",
                    tags=["universal", "tech"],
                ),
                EducationEntry(
                    id="e2",
                    institution="Empire State Polytechnic",
                    degree="Certificate in Cloud Architecture",
                    end_date="2021",
                    tags=["universal", "tech"],
                ),
            ],
        ),
    )

    job = JobPosting(
        title="Apex Labs: Cloud Software Engineer",
        link="https://example.com/jobs/apex-cloud",
        raw_text="Seeking a cloud software engineer with Python experience.",
        source="example.com",
    )

    eval_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=95,
        match_highlights=["Cloud API experience", "Python expertise"],
        tier_evaluated=1,
    )

    # Generate tailored resume
    resume_path = generate_tailored_resume(job, profile, eval_result, test_settings, dry_run=True)
    assert resume_path.exists()
    assert resume_path.name == "Alex_Morgan_ApexLabs_Resume.md"

    md_content = resume_path.read_text(encoding="utf-8")

    # 1. Verify complete elimination of AI watermark
    assert "Generated by BeaconAI" not in md_content
    assert "Match Fit Score" not in md_content

    # 2. Verify structured, non-squashed multi-school education formatting
    assert "### State University of New York\nB.S. in Computer Science | 2020" in md_content
    assert "### Empire State Polytechnic\nCertificate in Cloud Architecture | 2021" in md_content

    # 3. Verify PDF compilation with clean name
    pdf_path = export_markdown_to_pdf(resume_path)
    assert pdf_path.exists()
    assert pdf_path.name == "Alex_Morgan_ApexLabs_Resume.pdf"
    assert pdf_path.read_bytes()[:5] == b"%PDF-"


def test_narrative_context_does_not_bleed_into_tech_job():
    """Verify raw narrative directives do not bleed into headline or executive summary for tech postings."""
    from src.generator import create_deterministic_tailored_data
    from src.schemas import (
        ExperienceRole,
        JobPosting,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    profile = UserProfile(
        name="Jordan Tech",
        email="jordan@example.com",
        phone="555-0199",
        location="Austin, TX",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["Systems Engineer"],
            narrative_context="Seeking a 1-year seated administrative role with zero travel.",
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Systems Engineer",
                    organization="Dev Solutions",
                    location="Austin, TX",
                    start_date="2020",
                    end_date="Present",
                    tags=["software", "backend"],
                    bullets=["Maintained backend services."],
                )
            ],
            tools_and_technologies=["Python", "Docker"],
        ),
    )

    tech_job = JobPosting(
        title="Backend Software Engineer",
        link="https://example.com/backend-eng",
        raw_text="Looking for a Python backend software engineer to scale our platform.",
        source="techjobs.com",
    )

    data = create_deterministic_tailored_data(tech_job, profile)
    assert data.target_headline == "Backend Software Engineer"
    assert "seated administrative" not in data.target_headline.lower()
    assert "seated administrative" not in data.tailored_summary.lower()
    assert "Accomplished technical professional" in data.tailored_summary


def test_generate_clean_resume_filename_40_chars_word_boundary():
    """Verify clean filename allows up to 40 chars and avoids slicing words mid-syllable."""
    from src.generator import generate_clean_resume_filename

    # Long job title exceeding 40 chars - should cleanly break before 'Engineer'
    long_title = "Senior Cloud Infrastructure Platform Systems Engineer"
    fn = generate_clean_resume_filename("Alex Morgan", None, long_title)
    # Senior(6) + Cloud(5) + Infrastructure(14) + Platform(8) + Systems(7) = 40 chars
    assert fn == "Alex_Morgan_SeniorCloudInfrastructurePlatformSystems_Resume.pdf"
    assert "Eng" not in fn  # Did not slice 'Engineer' mid-word

    # Long company name exceeding 40 chars
    long_company = "Apex Advisory Services International Corporate Organization"
    fn_co = generate_clean_resume_filename("Jane Doe", long_company, "Developer")
    # Apex(4) + Advisory(8) + Services(8) + International(13) = 33 chars (< 40, next word 'Corporate' is 9 chars -> 42 > 40)
    assert fn_co == "Jane_Doe_ApexAdvisoryServicesInternational_Resume.pdf"


def test_executive_summary_sanitizes_feed_and_urls():
    """Verify source URLs, domain TLDs (.com, .org), and XML filenames do not leak into summary."""
    from src.generator import create_deterministic_tailored_data, sanitize_target_company
    from src.schemas import (
        ExperienceRole,
        JobPosting,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    # Sanitize helper checks
    assert sanitize_target_company("sample_jobs.xml") is None
    assert sanitize_target_company("weworkremotely.com") is None
    assert sanitize_target_company("craigslist.org") is None
    assert sanitize_target_company("https://remoteok.com/feed") is None
    assert sanitize_target_company("Stripe") == "Stripe"

    profile = UserProfile(
        name="Alex Morgan",
        email="alex@example.com",
        phone="555-0100",
        location="Metropolis, NY",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["Bookkeeper"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Staff Bookkeeper",
                    organization="Finance Corp",
                    location="Metropolis, NY",
                    start_date="2020",
                    end_date="Present",
                    tags=["accounting"],
                    bullets=["Reconciled ledgers."],
                )
            ],
            tools_and_technologies=["QuickBooks"],
        ),
    )

    # 1. Job with XML source and no company in title
    xml_job = JobPosting(
        title="Full Charge Bookkeeper",
        link="https://feed.example.com/job/101",
        raw_text="Seeking a full charge bookkeeper for records.",
        source="sample_jobs.xml",
    )
    data_xml = create_deterministic_tailored_data(xml_job, profile)
    assert "sample_jobs.xml" not in data_xml.tailored_summary
    assert data_xml.tailored_summary.endswith("Prepared to make an immediate impact in this role.")

    # 2. Job with .com source
    web_job = JobPosting(
        title="Accountant Specialist",
        link="https://weworkremotely.com/job/202",
        raw_text="Seeking an accountant.",
        source="weworkremotely.com",
    )
    data_web = create_deterministic_tailored_data(web_job, profile)
    assert "weworkremotely.com" not in data_web.tailored_summary
    assert data_web.tailored_summary.endswith("Prepared to make an immediate impact in this role.")

    # 3. Job with clean company name
    corp_job = JobPosting(
        title="Apex Advisory: Bookkeeper",
        link="https://example.com/job/303",
        raw_text="Seeking an accountant.",
        source="example.com",
    )
    data_corp = create_deterministic_tailored_data(corp_job, profile)
    assert data_corp.tailored_summary.endswith("Prepared to make an immediate impact at Apex Advisory.")


def test_resume_template_certifications_bullet_and_separation(tmp_path: Path):
    """Verify certifications are formatted as distinct bullets and separated with newlines from education."""
    from src.config import Settings
    from src.generator import generate_tailored_resume
    from src.schemas import (
        CertificationEntry,
        EducationEntry,
        EvaluationResult,
        EvaluationStatus,
        ExperienceRole,
        JobPosting,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    matches_dir = tmp_path / "matches"
    test_settings = Settings(matches_dir=matches_dir, artifacts_dir=tmp_path)

    profile = UserProfile(
        name="Sam Rivera",
        email="sam@example.com",
        phone="555-0155",
        location="Boston, MA",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["DevOps Engineer"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="DevOps Engineer",
                    organization="CloudOps",
                    location="Boston, MA",
                    start_date="2021",
                    end_date="Present",
                    tags=["devops", "cloud"],
                    bullets=["Maintained Kubernetes clusters."],
                )
            ],
            education=[
                EducationEntry(
                    id="e1",
                    institution="Boston University",
                    degree="B.S. Information Systems",
                    end_date="2019",
                    tags=["universal"],
                )
            ],
            certifications=[
                CertificationEntry(name="AWS Certified Solutions Architect", status="Active"),
                CertificationEntry(name="Certified Kubernetes Administrator", status="Active"),
            ],
            tools_and_technologies=["Kubernetes", "AWS"],
        ),
    )

    job = JobPosting(
        title="DevOps Engineer",
        link="https://example.com/jobs/devops",
        raw_text="Seeking a DevOps Engineer.",
        source="example.com",
    )

    eval_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=90,
        tier_evaluated=1,
    )

    resume_path = generate_tailored_resume(job, profile, eval_result, test_settings, dry_run=True)
    content = resume_path.read_text(encoding="utf-8")

    # Verify certifications section and format
    assert "## CERTIFICATIONS" in content
    assert "### AWS Certified Solutions Architect\nActive" in content
    assert "### Certified Kubernetes Administrator\nActive" in content

    # Verify clean education formatting and separation
    assert "### Boston University\nB.S. Information Systems | 2019" in content


def test_technical_skills_all_lines_start_with_bullet(tmp_path: Path):
    """Verify that every skill category line under Technical Skills starts with a bullet '* **'."""
    from src.config import Settings
    from src.generator import generate_tailored_resume
    from src.schemas import (
        EvaluationResult,
        EvaluationStatus,
        ExperienceRole,
        JobPosting,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    matches_dir = tmp_path / "matches"
    test_settings = Settings(matches_dir=matches_dir, artifacts_dir=tmp_path)

    profile = UserProfile(
        name="Morgan Skills",
        email="morgan@example.com",
        phone="555-0188",
        location="Seattle, WA",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["Full Stack Engineer"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Full Stack Engineer",
                    organization="Web Dynamics",
                    location="Seattle, WA",
                    start_date="2020",
                    end_date="Present",
                    tags=["fullstack"],
                    bullets=["Built web applications."],
                )
            ],
            tools_and_technologies=[
                "Python", "TypeScript", "React", "Docker", "PostgreSQL", "GraphQL"
            ],
        ),
    )

    job = JobPosting(
        title="Full Stack Engineer",
        link="https://example.com/jobs/fullstack",
        raw_text="Looking for full stack developer with Python and TypeScript experience.",
        source="example.com",
    )

    eval_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=95,
        tier_evaluated=1,
    )

    resume_path = generate_tailored_resume(job, profile, eval_result, test_settings, dry_run=True)
    content = resume_path.read_text(encoding="utf-8")

    # Extract TECHNICAL SKILLS section
    assert "## TECHNICAL SKILLS" in content
    skills_part = content.split("## TECHNICAL SKILLS")[1].split("## EDUCATION")[0]
    skill_lines = [line.strip() for line in skills_part.splitlines() if line.strip()]

    assert len(skill_lines) >= 2
    for line in skill_lines:
        assert line.startswith("* **"), f"Skill line does not start with '* **': {line}"


def test_experience_headers_render_pipe_delimiter_with_location(tmp_path: Path):
    """Verify experience section reliably outputs pipe delimiters when location and dates are present."""
    from src.config import Settings
    from src.generator import generate_tailored_resume
    from src.schemas import (
        EvaluationResult,
        EvaluationStatus,
        ExperienceRole,
        JobPosting,
        MasterExperience,
        UserConstraints,
        UserProfile,
    )

    matches_dir = tmp_path / "matches"
    test_settings = Settings(matches_dir=matches_dir, artifacts_dir=tmp_path)

    profile = UserProfile(
        name="Casey Jordan",
        email="casey@example.com",
        phone="555-0177",
        location="Chicago, IL",
        constraints=UserConstraints(),
        master_experience=MasterExperience(
            target_titles=["Backend Engineer"],
            roles=[
                ExperienceRole(
                    id="r1",
                    title="Backend Engineer",
                    organization="Tech Systems",
                    location="Chicago, IL",
                    start_date="2021",
                    end_date="Present",
                    tags=["backend"],
                    bullets=["Developed APIs."],
                ),
                ExperienceRole(
                    id="r2",
                    title="Software Consultant",
                    organization="Solo Practice",
                    location="",
                    start_date="2019",
                    end_date="2021",
                    tags=["backend"],
                    bullets=["Consulting services."],
                ),
            ],
            tools_and_technologies=["Go", "SQL"],
        ),
    )

    job = JobPosting(
        title="Backend Engineer",
        link="https://example.com/jobs/backend",
        raw_text="Seeking a Backend Engineer.",
        source="example.com",
    )

    eval_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=92,
        tier_evaluated=1,
    )

    resume_path = generate_tailored_resume(job, profile, eval_result, test_settings, dry_run=True)
    content = resume_path.read_text(encoding="utf-8")

    # Header with location has pipe delimiter
    assert "### Tech Systems | Chicago, IL" in content
    assert "#### **Backend Engineer | 2021 to Present**" in content

    # Header without location does not have trailing pipe
    assert "### Solo Practice\n" in content
    assert "### Solo Practice |" not in content


def test_export_markdown_to_pdf_uses_extra_and_sane_lists(tmp_path: Path, monkeypatch):
    """Verify that export_markdown_to_pdf parses markdown using only extra and sane_lists extensions."""
    import markdown
    from src.generator import export_markdown_to_pdf

    captured_extensions = []
    original_markdown = markdown.markdown

    def spy_markdown(text, extensions=None, **kwargs):
        if extensions:
            captured_extensions.extend(extensions)
        return original_markdown(text, extensions=extensions, **kwargs)

    monkeypatch.setattr("markdown.markdown", spy_markdown)

    md_file = tmp_path / "test_extensions.md"
    md_file.write_text("# Test Resume\n\n## Section\nLine 1\nLine 2\n", encoding="utf-8")
    export_markdown_to_pdf(md_file)

    assert captured_extensions == ["extra", "sane_lists"]
    assert "nl2br" not in captured_extensions


def test_generator_logs_error_when_no_llm_model_specified(monkeypatch, caplog):
    """Verify that generate_tailored_resume_data logs an error when no LLM model is configured."""
    import logging
    from src.config import Settings
    from src.generator import generate_tailored_resume_data
    from src.schemas import JobPosting, UserConstraints, UserProfile, MasterExperience

    monkeypatch.delenv("LLM_MODEL", raising=False)
    settings = Settings(llm_model=None)
    profile = UserProfile(
        name="Test Candidate",
        email="test@example.com",
        phone="555-0000",
        location="Chicago, IL",
        constraints=UserConstraints(),
        master_experience=MasterExperience(roles=[]),
    )
    job = JobPosting(
        title="Software Engineer",
        link="https://example.com/job-no-model",
        raw_text="Job description.",
        source="example.com",
    )

    with caplog.at_level(logging.ERROR):
        result = generate_tailored_resume_data(job, profile, settings, dry_run=False)

    assert "No LLM model specified" in caplog.text
    assert result is not None







