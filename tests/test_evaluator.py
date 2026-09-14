"""Unit tests for deterministic filtering, compensation extraction, and multi-tier evaluation."""

import pytest

from src.config import Settings
from src.evaluator import (
    EvaluationEngine,
    evaluate_tier1_deterministic,
    evaluate_tier2_heuristic,
    extract_company_from_posting,
    extract_compensation,
)
from src.schemas import (
    EvaluationResult,
    EvaluationStatus,
    JobPosting,
    MasterExperience,
    UserConstraints,
    UserProfile,
    WorkRole,
)



@pytest.fixture
def test_profile() -> UserProfile:
    """Fixture providing a standard test profile for a bookkeeper."""
    return UserProfile(
        name="Sarah Jenkins",
        email="sarah@example.com",
        phone="555-123-4567",
        location="Oakland, CA",
        constraints=UserConstraints(
            min_hourly_rate=28.0,
            min_annual_salary=58000.0,
            max_commute_miles=20,
            physical_restrictions=[
                "lift 50 lbs",
                "lift 40 lbs",
                "ladder",
                "warehouse labor",
                "heavy lifting",
            ],
            schedule_boundaries=[
                "overnight",
                "graveyard shift",
                "mandatory weekends",
            ],
        ),
        master_experience=MasterExperience(
            target_titles=["Bookkeeper", "Senior Bookkeeper", "Accounting Specialist"],
            roles=[
                WorkRole(
                    title="Full Charge Bookkeeper",
                    organization="Apex Advisory",
                    location="Berkeley, CA",
                    start_date="2021",
                    end_date="Present",
                    bullets=["Managed accounts payable and monthly GL reconciliations in QuickBooks."],
                )
            ],
            tools_and_technologies=["QuickBooks Online", "Excel", "Gusto"],
            education=["A.S. Accounting"],
        ),
    )


def test_extract_compensation_hourly():
    """Verify regex extraction of hourly wages and ranges."""
    max_h, max_a, raw = extract_compensation("Compensation is $25.50 - $32.00 per hour.")
    assert max_h == 32.00
    assert max_a is None
    assert "$25.50 - $32.00 per hour" in raw

    max_h2, _, _ = extract_compensation("Rate: $20/hr.")
    assert max_h2 == 20.0


def test_extract_compensation_annual():
    """Verify regex extraction of annual salaries."""
    _, max_a, raw = extract_compensation("Salary range: $55,000 - $65,000 / year.")
    assert max_a == 65000.0
    assert "$55,000 - $65,000 / year" in raw

    _, max_a2, _ = extract_compensation("Base pay is $60k annually.")
    assert max_a2 == 60000.0


def test_tier1_defers_lifting_to_tier2(test_profile):
    """Tier 1 minimalist gatekeeper defers lifting requirements to Tier 2 courtroom protocol."""
    job = JobPosting(
        title="Stock Clerk",
        link="https://example.com/job1",
        raw_text="Must be able to lift 60 lbs unassisted on a regular basis.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Cleared Tier 1 for Tier 2 evaluation


def test_tier1_defers_physical_restrictions_to_tier2(test_profile):
    """Tier 1 minimalist gatekeeper defers physical restrictions to Tier 2 to prevent false rejects on idioms."""
    job = JobPosting(
        title="Facility Maintenance",
        link="https://example.com/job2",
        raw_text="General facility work, ladder work, and fixture replacements.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Cleared Tier 1 for Tier 2


def test_tier1_rejects_hourly_pay_floor_violation(test_profile):
    """Tier 1 must reject explicit hourly rates below minimum floor."""
    job = JobPosting(
        title="Junior Bookkeeper",
        link="https://example.com/job3",
        raw_text="Light accounting tasks. Pay: $22.00/hr to start.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert result.tier_evaluated == 1
    assert "$22.00/hr < $28.00/hr" in result.rejection_reason


def test_tier1_defers_schedule_conflict_to_tier2(test_profile):
    """Tier 1 minimalist gatekeeper defers schedule evaluation to Tier 2 courtroom protocol."""
    job = JobPosting(
        title="Audit Assistant",
        link="https://example.com/job4",
        raw_text="Reconcile daily night registers. Hours: Graveyard shift 11 PM to 7 AM.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Cleared Tier 1 for Tier 2


def test_tier1_passes_qualified_job(test_profile):
    """Tier 1 must return None when all deterministic constraints are satisfied."""
    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/job5",
        raw_text="Manage monthly financials in QuickBooks Online. Pay: $34.00/hr. M-F 9am-5pm.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Cleared Tier 1 for Tier 2


def test_tier2_heuristic_matches_aligned_role(test_profile):
    """Tier 2 heuristic should award MATCH and high fit score for target role."""
    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/job5",
        raw_text="Experienced Bookkeeper needed. Proficient in QuickBooks Online, Excel reconciliations, and Gusto payroll.",
        source="example.com",
    )
    result = evaluate_tier2_heuristic(job, test_profile)
    assert result.status == EvaluationStatus.MATCH
    assert result.fit_score >= 75
    assert result.tier_evaluated == 2


def test_circuit_breaker_caps_evaluations(test_profile):
    """Circuit breaker must enforce MAX_LLM_EVALS_PER_RUN cap."""
    settings = Settings(max_llm_evals_per_run=2)
    engine = EvaluationEngine(settings, dry_run=True)

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/job",
        raw_text="QuickBooks Online, Excel reconciliations, and Gusto payroll accounting work. $35/hr.",
        source="example.com",
    )

    # First two evaluations pass through
    res1 = engine.evaluate(job, test_profile)
    assert res1.status == EvaluationStatus.MATCH
    res2 = engine.evaluate(job, test_profile)
    assert res2.status == EvaluationStatus.MATCH

    # Third evaluation must hit circuit breaker and be marked DEFERRED
    res3 = engine.evaluate(job, test_profile)
    assert res3.status == EvaluationStatus.DEFERRED
    assert "Circuit breaker limit reached" in res3.rejection_reason


def test_extract_compensation_single_dollar_range():
    """Verify compensation extraction when second number omits dollar sign."""
    max_h, _, raw = extract_compensation("Compensation: $20 - 25/hr.")
    assert max_h == 25.0
    assert "$20 - 25/hr" in raw

    max_h2, _, raw2 = extract_compensation("Rate is $22 to 28 per hour.")
    assert max_h2 == 28.0


def test_extract_compensation_salary_shorthand_and_ranges():
    """Verify annual salary extraction with k-shorthand and omitted second dollar symbol."""
    _, max_a, raw = extract_compensation("Estimated pay: $50k - $70k annually.")
    assert max_a == 70000.0

    _, max_a2, _ = extract_compensation("Salary range: $55,000 - 65,000/yr.")
    assert max_a2 == 65000.0


def test_idiomatic_ladder_not_rejected(test_profile):
    """Corporate and career ladder idioms must NOT trigger physical ladder restrictions."""
    job1 = JobPosting(
        title="Staff Accountant",
        link="https://example.com/job-ladder1",
        raw_text="Opportunity to climb the corporate ladder in a high-growth firm.",
        source="example.com",
    )
    result1 = evaluate_tier1_deterministic(job1, test_profile)
    assert result1 is None  # Must pass Tier 1

    job2 = JobPosting(
        title="Junior Analyst",
        link="https://example.com/job-ladder2",
        raw_text="Clear career ladder with rapid promotional opportunities.",
        source="example.com",
    )
    result2 = evaluate_tier1_deterministic(job2, test_profile)
    assert result2 is None  # Must pass Tier 1

    job3 = JobPosting(
        title="Culture Ambassador",
        link="https://example.com/job-ladder3",
        raw_text="Our mission is to lift spirits and inspire teamwork across the team.",
        source="example.com",
    )
    result3 = evaluate_tier1_deterministic(job3, test_profile)
    assert result3 is None  # Must pass Tier 1


def test_physical_ladder_rejected(test_profile):
    """Actual physical ladder demands are deferred to Tier 2 courtroom context."""
    job = JobPosting(
        title="Maintenance Assistant",
        link="https://example.com/job-phys-ladder",
        raw_text="Must climb 10-foot ladders for facility maintenance and lighting.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Defers to Tier 2


def test_circuit_breaker_sets_deferred_and_eligible_for_rescan(tmp_path, test_profile):
    """Deferred jobs must NOT be marked as permanently seen in SQLite."""
    from src.db import init_db, is_job_seen, record_job

    db_file = tmp_path / "test_deferred.db"
    init_db(db_file)

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/deferred-job",
        raw_text="QuickBooks Online, Excel reconciliations, and Gusto payroll. $35/hr.",
        source="example.com",
    )

    settings = Settings(db_path=db_file, max_llm_evals_per_run=1)
    engine = EvaluationEngine(settings, dry_run=True)

    # First evaluation consumes cap
    res1 = engine.evaluate(job, test_profile)
    assert res1.status == EvaluationStatus.MATCH

    # Second evaluation triggers circuit breaker
    res2 = engine.evaluate(job, test_profile)
    assert res2.status == EvaluationStatus.DEFERRED

    # Record the deferred job
    record_job(job, res2, db_file)

    # Must NOT be considered seen (finalized), so it can be re-evaluated on subsequent runs!
    assert is_job_seen(job.link, db_file) is False


def test_has_llm_credentials_multi_provider():
    """Verify credential detection across different foundation model providers."""
    s_key = Settings(llm_api_key="unified-key-123")
    assert s_key.has_llm_credentials() is True

    # Ollama (local runtime, needs no API key)
    s_ollama = Settings(llm_api_key=None)
    assert s_ollama.has_llm_credentials("ollama/llama3.2") is True
    assert s_ollama.has_llm_credentials("local/mistral") is True

    # Unset credentials without local model
    s_empty = Settings(llm_api_key=None)
    assert s_empty.has_llm_credentials("some-cloud-model") is False
    assert s_empty.has_llm_credentials() is False


def test_evaluate_tier2_llm_model_agnostic_routing(monkeypatch, test_profile):
    """Verify LiteLLM completion receives the configured model name from Settings/variable and unified API key."""
    from unittest.mock import MagicMock
    import instructor
    from src.evaluator import evaluate_tier2_llm

    mock_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        rejection_reason=None,
        fit_score=95,
        estimated_compensation="$35/hr",
        match_highlights=["Strong bookkeeping background", "QuickBooks expert"],
        tier_evaluated=2,
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_result
    monkeypatch.setattr(instructor, "from_litellm", lambda *args, **kwargs: mock_client)

    settings = Settings(
        llm_model="anthropic/claude-3-5-sonnet-20241022",
        llm_api_key="sk-ant-test",
    )

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/claude-job",
        raw_text="Experienced Bookkeeper needed. QuickBooks Online.",
        source="example.com",
    )

    res = evaluate_tier2_llm(job, test_profile, settings, dry_run=False)

    assert res.status == EvaluationStatus.MATCH
    assert res.fit_score == 95
    assert res.tier_evaluated == 2

    # Verify that litellm was called with the model from Settings and unified API key
    mock_client.chat.completions.create.assert_called_once()
    kwargs = mock_client.chat.completions.create.call_args[1]
    assert kwargs["model"] == "anthropic/claude-3-5-sonnet-20241022"
    assert kwargs["api_key"] == "sk-ant-test"


def test_generate_tailored_resume_data_model_agnostic(monkeypatch, test_profile):
    """Verify resume tailoring routes to configured LiteLLM model from Settings/variable."""
    from unittest.mock import MagicMock
    import instructor
    from src.generator import generate_tailored_resume_data
    from src.schemas import TailoredResumeData, WorkRole

    mock_resume = TailoredResumeData(
        target_headline="Targeted Senior Bookkeeper",
        tailored_summary="Proven accounting professional.",
        categorized_skills={"Accounting": ["QuickBooks", "Excel"]},
        tailored_experience=[
            WorkRole(
                title="Bookkeeper",
                organization="Apex",
                location="Oakland, CA",
                start_date="2021",
                end_date="Present",
                bullets=["Reconciled financials."],
            )
        ],
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resume
    monkeypatch.setattr(instructor, "from_litellm", lambda *args, **kwargs: mock_client)

    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key="sk-openai-test",
    )

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/gpt-job",
        raw_text="QuickBooks needed.",
        source="example.com",
    )

    resume_out = generate_tailored_resume_data(job, test_profile, settings, dry_run=False)
    assert resume_out.target_headline == "Targeted Senior Bookkeeper"
    kwargs = mock_client.chat.completions.create.call_args[1]
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["api_key"] == "sk-openai-test"


def test_llm_model_pulled_from_variable_not_profile(monkeypatch, test_profile):
    """Verify that model resolution pulls from configuration/environment and NOT from the profile schema."""
    from unittest.mock import MagicMock
    import instructor
    from src.evaluator import evaluate_tier2_llm

    # Confirm candidate UserProfile schema has no llm_model field
    assert "llm_model" not in UserProfile.model_fields
    assert not hasattr(test_profile, "llm_model")

    mock_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=90,
        tier_evaluated=2,
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_result
    monkeypatch.setattr(instructor, "from_litellm", lambda *args, **kwargs: mock_client)

    # 1. Config variable sets model
    settings = Settings(llm_model="model-from-settings-variable", llm_api_key="test-key")
    job = JobPosting(
        title="Accountant",
        link="https://example.com/job1",
        raw_text="Job details.",
        source="example.com",
    )
    evaluate_tier2_llm(job, test_profile, settings, dry_run=False)
    kwargs = mock_client.chat.completions.create.call_args[1]
    assert kwargs["model"] == "model-from-settings-variable"

    # 2. Environment variable sets model
    monkeypatch.setenv("LLM_MODEL", "model-from-env-var")
    env_settings = Settings(llm_api_key="test-key")
    evaluate_tier2_llm(job, test_profile, env_settings, dry_run=False)
    kwargs2 = mock_client.chat.completions.create.call_args[1]
    assert kwargs2["model"] == "model-from-env-var"


def test_evaluator_logs_error_when_no_llm_model_specified(monkeypatch, test_profile, caplog):
    """Verify that evaluate_tier2_llm logs an error when no LLM model variable is provided."""
    import logging
    from src.evaluator import evaluate_tier2_llm

    monkeypatch.delenv("LLM_MODEL", raising=False)
    settings = Settings(llm_model=None, llm_api_key="test-key")
    job = JobPosting(
        title="Bookkeeper",
        link="https://example.com/job-no-model",
        raw_text="Job details.",
        source="example.com",
    )

    with caplog.at_level(logging.ERROR):
        result = evaluate_tier2_llm(job, test_profile, settings, dry_run=False)

    assert "No LLM model specified" in caplog.text
    assert "LLM_MODEL is unset" in (result.rejection_reason or "")


def test_tier1_rejects_dynamic_physical_restriction(test_profile):
    """Tier 1 must dynamically reject postings matching any arbitrary physical restriction in profile."""
    test_profile.constraints.physical_restrictions = ["prolonged standing", "pallet jack operation"]
    job = JobPosting(
        title="Inventory Clerk",
        link="https://example.com/job-inv",
        raw_text="Position requires prolonged standing across 8-hour shifts.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Defers to Tier 2


def test_tier1_rejects_commute_distance_exceeding_max(test_profile):
    """Tier 1 must reject postings where physical commute exceeds profile max_commute_miles."""
    test_profile.constraints.max_commute_miles = 15
    job = JobPosting(
        title="On-Site Operations Lead",
        link="https://example.com/job-commute",
        raw_text="Requires a 50-mile commute to our rural operations hub.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert "Commute distance exceeds limit: 50 miles" in result.rejection_reason


def test_tier1_allows_remote_job_regardless_of_distance(test_profile):
    """Tier 1 must not disqualify remote roles even if distant coordinates or miles are mentioned."""
    test_profile.constraints.max_commute_miles = 15
    job = JobPosting(
        title="Remote Accounting Specialist",
        link="https://example.com/job-remote",
        raw_text="100% remote telecommute position. Headquarters is 60 miles from nearest airport.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None


def test_tier2_heuristic_scores_dynamic_tags(test_profile):
    """Tier 2 heuristic must dynamically match role and project tags against posting text."""
    test_profile.master_experience.roles[0].tags = ["payroll_compliance", "general_ledger"]
    job = JobPosting(
        title="Accounting Specialist",
        link="https://example.com/job-tag",
        raw_text="Seeking candidate strong in general ledger, payroll compliance, and QuickBooks Online.",
        source="example.com",
    )
    result = evaluate_tier2_heuristic(job, test_profile)
    assert result.status == EvaluationStatus.MATCH
    assert any("Domain alignment tags" in h for h in result.match_highlights)


def test_llm_completion_retries_on_rate_limit():
    """Verify execute_llm_completion retries with backoff on RateLimitError (429)."""
    from unittest.mock import MagicMock
    import litellm
    from src.evaluator import execute_llm_completion

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        litellm.RateLimitError("429 ResourceExhausted: rate limit exceeded", model="gemini", llm_provider="gemini"),
        "success",
    ]

    res = execute_llm_completion(mock_client, model="gemini")
    assert res == "success"
    assert mock_client.chat.completions.create.call_count == 2


def test_llm_completion_retries_on_service_unavailable():
    """Verify execute_llm_completion retries on ServiceUnavailableError (503)."""
    from unittest.mock import MagicMock
    import litellm
    from src.evaluator import execute_llm_completion

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        litellm.ServiceUnavailableError("503 Service Unavailable", model="gemini", llm_provider="gemini"),
        "success",
    ]

    res = execute_llm_completion(mock_client, model="gemini")
    assert res == "success"
    assert mock_client.chat.completions.create.call_count == 2


def test_resolve_profile_track_all_six_tracks():
    """Verify resolve_profile_track accurately routes across all 6 tracks using daniel_giovinazzo.json."""
    from src.evaluator import resolve_profile_track
    from src.schemas import JobPosting, UserProfile
    from pathlib import Path

    profile_path = Path("profiles/daniel_giovinazzo.json")
    profile = UserProfile.model_validate_json(profile_path.read_text())

    # 1. Clerical & Data Entry
    job_clerical = JobPosting(
        title="Data Entry Specialist",
        link="https://example.com/data-entry",
        raw_text="Looking for a high-accuracy data entry specialist with 10-key touch and spreadsheet skills.",
        source="craigslist.org",
    )
    track_clerical = resolve_profile_track(job_clerical, profile)
    assert track_clerical is not None
    assert track_clerical.track_id == "clerical_data_entry"

    # 2. Office & Administrative
    job_admin = JobPosting(
        title="Administrative Assistant",
        link="https://example.com/admin",
        raw_text="Seeking an administrative assistant for office coordination, calendar management, and filing.",
        source="craigslist.org",
    )
    track_admin = resolve_profile_track(job_admin, profile)
    assert track_admin is not None
    assert track_admin.track_id == "office_administrative"

    # 3. Accounting & Bookkeeping
    job_acct = JobPosting(
        title="Accounts Payable Clerk",
        link="https://example.com/ap-clerk",
        raw_text="Responsible for invoice processing, accounts payable, ledger reconciliation, and vendor payments.",
        source="craigslist.org",
    )
    track_acct = resolve_profile_track(job_acct, profile)
    assert track_acct is not None
    assert track_acct.track_id == "accounting_bookkeeping"

    # 4. Technical Support & QA
    job_qa = JobPosting(
        title="Application Support Analyst",
        link="https://example.com/app-support",
        raw_text="Provide Tier 1 and Tier 2 technical troubleshooting, incident triage, and software QA regression testing.",
        source="craigslist.org",
    )
    track_qa = resolve_profile_track(job_qa, profile)
    assert track_qa is not None
    assert track_qa.track_id == "technical_support_qa"

    # 5. Data Analysis & Reporting
    job_data = JobPosting(
        title="Data Analyst",
        link="https://example.com/data-analyst",
        raw_text="Extract insights using SQL queries, Python data analysis, and build reporting dashboards in Excel.",
        source="craigslist.org",
    )
    track_data = resolve_profile_track(job_data, profile)
    assert track_data is not None
    assert track_data.track_id == "data_analysis_reporting"

    # 6. Software Engineering
    job_swe = JobPosting(
        title="Full Stack Software Engineer",
        link="https://example.com/swe",
        raw_text="Build scalable web applications using Python, FastAPI, React, and AWS microservices.",
        source="craigslist.org",
    )
    track_swe = resolve_profile_track(job_swe, profile)
    assert track_swe is not None
    assert track_swe.track_id == "software_engineering"

    # Out of domain job -> None (Precision over recall)
    job_unrelated = JobPosting(
        title="Commercial Truck Driver",
        link="https://example.com/driver",
        raw_text="CDL Class A required. Haul freight nationwide.",
        source="craigslist.org",
    )
    assert resolve_profile_track(job_unrelated, profile) is None


def test_evaluate_tier2_rejects_immediately_when_no_track_matched():
    """Verify evaluate_tier2_heuristic rejects early without evaluating master context when no track matches."""
    from src.evaluator import evaluate_tier2_heuristic
    from src.schemas import EvaluationStatus, JobPosting, UserProfile
    from pathlib import Path

    profile_path = Path("profiles/daniel_giovinazzo.json")
    profile = UserProfile.model_validate_json(profile_path.read_text())

    job_unrelated = JobPosting(
        title="Forklift Operator",
        link="https://example.com/forklift",
        raw_text="Warehouse forklift operator needed.",
        source="craigslist.org",
    )
    result = evaluate_tier2_heuristic(job_unrelated, profile)
    assert result.status == EvaluationStatus.REJECT
    assert "Insufficient alignment score" in (result.rejection_reason or "")
    assert result.fit_score < 75


def test_tier1_defers_heavy_lifting_to_tier2(test_profile):
    """Verify that heavy lifting is deferred from Tier 1 to Tier 2 courtroom context."""
    from src.evaluator import evaluate_tier1_deterministic
    from src.schemas import JobPosting

    test_profile.constraints.physical_restrictions = ["heavy lifting", "prolonged standing"]

    job = JobPosting(
        title="Office Assistant",
        link="https://example.com/office",
        raw_text="Must be able to lift 35 lbs boxes of files regularly.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is None  # Defers to Tier 2


def test_tier1_defers_toxic_culture_disqualifiers_to_tier2(test_profile):
    """Verify that culture buzzwords are deferred from Tier 1 to Tier 2 courtroom."""
    from src.evaluator import evaluate_tier1_deterministic
    from src.schemas import JobPosting

    test_profile.constraints.culture_disqualifiers = [
        "work hard, play hard", "we are a family", "wear many hats", "thrives in chaos"
    ]

    job_family = JobPosting(
        title="Frontend Developer",
        link="https://example.com/startup-1",
        raw_text="Join our tight-knit team! We are a family here and love what we do.",
        source="example.com",
    )
    res_family = evaluate_tier1_deterministic(job_family, test_profile)
    assert res_family is None  # Defers to Tier 2

    job_hats = JobPosting(
        title="Junior Developer",
        link="https://example.com/startup-2",
        raw_text="Fast-moving startup where you will wear many hats and work directly with founders.",
        source="example.com",
    )
    res_hats = evaluate_tier1_deterministic(job_hats, test_profile)
    assert res_hats is None  # Defers to Tier 2


def test_extract_company_from_posting():
    """Verify regex extraction of company/employer names from postings."""
    p1 = JobPosting(
        title="Web Developer (Remote) - Coalition Technologies",
        link="https://craigslist.org/1",
        raw_text="We are hiring a full time developer.",
        source="craigslist.org",
    )
    assert extract_company_from_posting(p1) == "Coalition Technologies"

    p2 = JobPosting(
        title="Data Specialist",
        link="https://craigslist.org/2",
        raw_text="Company: Acme Corp Inc.\nLocation: New York, NY\nApply today.",
        source="craigslist.org",
    )
    assert extract_company_from_posting(p2) == "Acme Corp Inc"

    p3 = JobPosting(
        title="Software Engineer at Stripe",
        link="https://example.com/3",
        raw_text="Join our infrastructure team.",
        source="example.com",
    )
    assert extract_company_from_posting(p3) == "Stripe"


def test_tier1_rejects_excluded_companies_from_profile(test_profile):
    """Verify that candidate profile excluded_companies are rejected at Tier 1."""
    test_profile.constraints.excluded_companies = ["Coalition Technologies", "Apex Staffing"]

    job = JobPosting(
        title="Python Engineer - Coalition Technologies",
        link="https://craigslist.org/coalition-1",
        raw_text="Coalition Technologies is looking for a backend developer.",
        source="craigslist.org",
    )
    res = evaluate_tier1_deterministic(job, test_profile)
    assert res is not None
    assert res.status == EvaluationStatus.REJECT
    assert "Blocked company/employer matched" in res.rejection_reason
    assert res.detected_company == "Coalition Technologies"


def test_tier1_rejects_blocked_companies_from_db(test_profile, tmp_path):
    """Verify that SQLite blocked_companies table entries are rejected at Tier 1."""
    from src.db import block_company
    db_file = tmp_path / "test_blocked.db"
    block_company("Revature", reason="Predatory training contract", db_path=db_file)

    config = Settings(db_path=db_file)
    job = JobPosting(
        title="Associate Software Engineer",
        link="https://example.com/revature-1",
        raw_text="Revature is currently seeking entry level engineers for our client placements.",
        source="example.com",
    )
    res = evaluate_tier1_deterministic(job, test_profile, config=config)
    assert res is not None
    assert res.status == EvaluationStatus.REJECT
    assert "Blocked company/employer matched: 'Revature'" in res.rejection_reason


def test_tier1_rejects_test_gate_patterns(test_profile, tmp_path):
    """Verify that upfront unpaid assessment gates are rejected at Tier 1 without auto-banning company."""
    from src.db import is_company_blocked
    db_file = tmp_path / "test_testgate.db"
    config = Settings(db_path=db_file)

    job = JobPosting(
        title="Copywriter - TestMill Agency",
        link="https://example.com/testmill-1",
        raw_text="To be considered for this role, a 45-minute skills assessment is required before any interviews.",
        source="example.com",
    )
    res = evaluate_tier1_deterministic(job, test_profile, config=config)
    assert res is None  # Defers to Tier 2 courtroom context


def test_layer1_allows_seniority_titles_for_tier2_context(test_profile):
    """Layer 1: Verify minimalist Tier 1 passes Senior/Lead/Staff titles to Tier 2 for contextual courtroom evaluation."""
    test_profile.constraints.seniority_disqualifiers = [
        "senior", "sr.", "sr ", "lead", "principal", "staff", "architect", "director", "manager", "controller", "head of", "vp"
    ]

    titles_to_test = [
        "Senior Software Engineer",
        "Sr. Python Developer",
        "Lead Full Stack Engineer",
        "Principal Systems Architect",
        "Staff Software Engineer",
        "Director of Software Engineering",
        "Accounting Controller",
        "VP of Technology",
        "Software Engineer",
        "Full Stack Developer",
        "Junior Web Developer",
        "Bookkeeper",
        "Data Analyst",
    ]
    for title in titles_to_test:
        job = JobPosting(
            title=title,
            link="https://example.com/job",
            raw_text="Full time role building web apps. Seated desk work.",
            source="example.com",
        )
        res = evaluate_tier1_deterministic(job, test_profile)
        assert res is None, f"Expected {title} to pass Tier 1 for Tier 2 evaluation"


def test_layer1_defers_excessive_experience_years_to_tier2(test_profile):
    """Layer 1: Verify Tier 1 defers experience requirements to Tier 2 to prevent false rejects on company heritage."""
    test_profile.constraints.max_experience_years = 4

    excessive_job = JobPosting(
        title="Python Engineer",
        link="https://example.com/job",
        raw_text="Requirements: 7+ years of experience in backend development required.",
        source="example.com",
    )
    res = evaluate_tier1_deterministic(excessive_job, test_profile)
    assert res is None  # Cleared Tier 1 for Tier 2

    # Moderate experience passes
    moderate_job = JobPosting(
        title="Python Engineer",
        link="https://example.com/job",
        raw_text="Requirements: 2-3 years of experience in backend development. Seated office work.",
        source="example.com",
    )
    res_mod = evaluate_tier1_deterministic(moderate_job, test_profile)
    assert res_mod is None


def test_track_forbidden_keywords_clears_tier1_and_vetoed_in_engine(test_profile):
    """Track-forbidden keywords clear Tier 1 and are vetoed in Engine post-evaluation."""
    from src.schemas import ProfileTrack
    test_profile.master_experience.tracks = {
        "software": ProfileTrack(
            track_id="software",
            display_name="Software",
            target_titles=["Software Engineer"],
            trigger_keywords=["software"],
            forbidden_keywords=["c++", "java", "c#", ".net", "golang"],
        )
    }

    job_cpp = JobPosting(
        title="C++ Software Engineer",
        link="https://example.com/job",
        raw_text="Build low-latency trading systems in C++.",
        source="example.com",
    )
    res_tier1 = evaluate_tier1_deterministic(job_cpp, test_profile)
    assert res_tier1 is None  # Clears Tier 1

    engine = EvaluationEngine(config=Settings(), dry_run=True)
    res_engine = engine.evaluate(job_cpp, test_profile)
    assert res_engine.status == EvaluationStatus.REJECT


def test_layer3_python_veto_unmet_core_stack(test_profile):
    """Layer 3: Verify Python Veto overrides an eager LLM MATCH if candidate_meets_core_stack is False."""
    from unittest.mock import patch
    config = Settings()
    engine = EvaluationEngine(config=config, dry_run=False)

    fake_llm_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=88,
        candidate_meets_core_stack=False,
        unmet_mandatory_requirements=["C++", "Qt Framework"],
        primary_required_languages=["C++"],
        tier_evaluated=2,
    )

    job = JobPosting(
        title="Software Engineer",
        link="https://example.com/job",
        raw_text="Desktop application development in C++.",
        source="example.com",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=fake_llm_result):
        final_res = engine.evaluate(job, test_profile)

    assert final_res.status == EvaluationStatus.REJECT
    assert "Deterministic Veto: Role requires core technologies" in final_res.rejection_reason


def test_layer3_python_veto_forbidden_track_keyword(test_profile):
    """Layer 3: Verify Python Veto overrides LLM MATCH if posting body contains track forbidden keyword."""
    from unittest.mock import patch
    from src.schemas import ProfileTrack
    test_profile.master_experience.tracks = {
        "software": ProfileTrack(
            track_id="software",
            display_name="Software",
            target_titles=["Software Engineer", "Full Stack Engineer"],
            trigger_keywords=["software", "engineer"],
            forbidden_keywords=["c++", "golang"],
        )
    }
    config = Settings()
    engine = EvaluationEngine(config=config, dry_run=False)

    fake_llm_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=85,
        candidate_meets_core_stack=True,
        unmet_mandatory_requirements=[],
        tier_evaluated=2,
        matched_track_id="software",
    )

    job = JobPosting(
        title="Full Stack Engineer",
        link="https://example.com/job",
        raw_text="We build web apps, but all background microservices are strictly implemented in Golang.",
        source="example.com",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=fake_llm_result):
        final_res = engine.evaluate(job, test_profile)

    assert final_res.status == EvaluationStatus.REJECT
    assert "Deterministic Veto: Posting requires incompatible track keyword: 'golang'" in final_res.rejection_reason


def test_layer4_quality_threshold_rejection(test_profile):
    """Layer 4: Verify that fit scores below 75 are converted to REJECT."""
    from unittest.mock import patch
    config = Settings()
    engine = EvaluationEngine(config=config, dry_run=False)

    # Lukewarm score of 70
    lukewarm_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=70,
        candidate_meets_core_stack=True,
        unmet_mandatory_requirements=[],
        tier_evaluated=2,
    )

    job = JobPosting(
        title="Software Engineer",
        link="https://example.com/job",
        raw_text="General web development role.",
        source="example.com",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=lukewarm_result):
        res = engine.evaluate(job, test_profile)

    assert res.status == EvaluationStatus.REJECT
    assert "Score below quality threshold (70/100, minimum 75 required)" in res.rejection_reason

    # Strong score of 82 passes
    strong_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=82,
        candidate_meets_core_stack=True,
        unmet_mandatory_requirements=[],
        tier_evaluated=2,
    )
    with patch("src.evaluator.evaluate_tier2_llm", return_value=strong_result):
        res_strong = engine.evaluate(job, test_profile)

    assert res_strong.status == EvaluationStatus.MATCH
    assert res_strong.fit_score == 82


def test_tier1_defers_offshore_broker_to_tier2_axioms(test_profile):
    """Tier 1 defers talent broker evaluation to Tier 2 Axiom 2 / Courtroom Prosecution."""
    job = JobPosting(
        title="Full Stack Developer (Python, React)",
        link="https://example.com/job-latam",
        raw_text="GoFasti is a platform connecting top talent from LATAM with high-growth US tech companies. Looking for nearshore developers.",
        source="example.com",
    )
    res = evaluate_tier1_deterministic(job, test_profile)
    assert res is None  # Defers to Tier 2


def test_engine_veto_illegitimate_employment(test_profile):
    """Engine must veto any posting flagged as non-viable or illegitimate employment (Axiom 2)."""
    from unittest.mock import patch
    config = Settings()
    engine = EvaluationEngine(config=config, dry_run=False)

    fake_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=88,
        candidate_meets_core_stack=True,
        is_legitimate_employment=False,
        is_verifiable_entity=True,
        unmet_mandatory_requirements=[],
        tier_evaluated=2,
    )

    job = JobPosting(
        title="Software Engineer",
        link="https://example.com/job",
        raw_text="Looking for a software engineer to join our development team.",
        source="example.com",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=fake_result):
        res = engine.evaluate(job, test_profile)

    assert res.status == EvaluationStatus.REJECT
    assert "flagged as offshore talent broker, international contractor pool, or non-viable employment structure" in res.rejection_reason


def test_engine_veto_unverifiable_entity(test_profile):
    """Engine must veto any ghost posting or unverifiable scraper template (Axiom 3)."""
    from unittest.mock import patch
    config = Settings()
    engine = EvaluationEngine(config=config, dry_run=False)

    fake_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=85,
        candidate_meets_core_stack=True,
        is_legitimate_employment=True,
        is_verifiable_entity=False,
        unmet_mandatory_requirements=[],
        tier_evaluated=2,
    )

    job = JobPosting(
        title="Customer Support Specialist",
        link="https://example.com/ghost-job",
        raw_text="Confidential company seeks remote agent. Submit your resume to our general pool.",
        source="example.com",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=fake_result):
        res = engine.evaluate(job, test_profile)

    assert res.status == EvaluationStatus.REJECT
    assert "Posting lacks identifiable organizational identity or appears to be a ghost lead-generation template" in res.rejection_reason


def test_engine_allows_good_match_cleanly(test_profile):
    """Legitimate direct employment meeting all criteria and score >= 75 must pass without veto."""
    from unittest.mock import patch
    config = Settings()
    engine = EvaluationEngine(config=config, dry_run=False)

    fake_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=90,
        candidate_meets_core_stack=True,
        is_legitimate_employment=True,
        is_verifiable_entity=True,
        unmet_mandatory_requirements=[],
        tier_evaluated=2,
        match_highlights=["Excellent QuickBooks reconciliation overlap", "Matches full-charge experience"],
    )

    job = JobPosting(
        title="Full Charge Bookkeeper",
        link="https://example.com/legit-job",
        raw_text="Established local accounting firm seeking Full Charge Bookkeeper for QuickBooks Online GL reconciliations.",
        source="example.com",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=fake_result):
        res = engine.evaluate(job, test_profile)

    assert res.status == EvaluationStatus.MATCH
    assert res.fit_score == 90
    assert len(res.match_highlights) == 2


def test_tier1_allows_family_owned_business(test_profile):
    """Tier 1 must not falsely reject a legitimate family-owned small business due to 'we are a family' rule."""
    test_profile.constraints.culture_disqualifiers = ["we are a family", "family atmosphere"]
    job = JobPosting(
        title="Bookkeeper",
        link="https://example.com/family-biz",
        raw_text="We are a family-owned and operated plumbing contractor seeking an honest bookkeeper for QuickBooks billing.",
        source="example.com",
    )
    res = evaluate_tier1_deterministic(job, test_profile)
    # Must NOT be rejected at Tier 1 for culture disqualifier
    assert res is None


def test_courtroom_protocol_prosecution_fatal_barrier_veto(test_profile):
    """Courtroom Protocol: Python veto overrides MATCH to REJECT if Prosecutor establishes fatal barriers."""
    from unittest.mock import patch
    from src.schemas import ProsecutionCase, DefenseCase
    engine = EvaluationEngine(Settings(dry_run=False))
    job = JobPosting(
        title="Accountant",
        link="https://example.com/job",
        raw_text="Full-time accounting role.",
        source="example.com",
    )

    fake_result = EvaluationResult(
        status=EvaluationStatus.MATCH,
        fit_score=85,
        match_highlights=["Strong bookkeeping skills"],
        tier_evaluated=2,
        prosecution=ProsecutionCase(
            fatal_barriers=["Mandatory active CPA license required by New York State law"],
            unverified_competencies=[],
            deception_or_exploitation_flags=[],
            argument="Candidate lacks state CPA license.",
        ),
        defense=DefenseCase(
            practical_task_overlap=["Ledger reconciliations"],
            transferable_strengths=["QuickBooks"],
            context_defense="Strong operational alignment.",
            advocate_score=85,
        ),
        findings_of_fact="Candidate meets general accounting workflows but lacks mandatory state CPA license.",
    )

    with patch("src.evaluator.evaluate_tier2_llm", return_value=fake_result):
        res = engine.evaluate(job, test_profile)

    assert res.status == EvaluationStatus.REJECT
    assert "Courtroom Veto: Fatal licensing or prerequisite barriers" in res.rejection_reason
    assert "CPA license" in res.rejection_reason


def test_tier1_allows_staff_accountant_and_engineer_to_reach_tier2(test_profile):
    """Tier 1: Minimalist shield passes both Staff Accountant and Staff Engineer to Tier 2 for deliberation."""
    test_profile.constraints.seniority_disqualifiers = ["staff"]

    accountant_job = JobPosting(
        title="Staff Accountant",
        link="https://example.com/staff-acct",
        raw_text="Responsible for journal entries and bank reconciliations.",
        source="example.com",
    )
    res_acct = evaluate_tier1_deterministic(accountant_job, test_profile)
    assert res_acct is None, "Staff Accountant should not be blocked at Tier 1"

    bookkeeper_job = JobPosting(
        title="Full Charge Bookkeeper / Staff Bookkeeper",
        link="https://example.com/staff-bk",
        raw_text="Manage office accounts and payroll.",
        source="example.com",
    )
    res_bk = evaluate_tier1_deterministic(bookkeeper_job, test_profile)
    assert res_bk is None, "Staff Bookkeeper should not be blocked at Tier 1"

    engineer_job = JobPosting(
        title="Staff Software Engineer",
        link="https://example.com/staff-eng",
        raw_text="Lead distributed systems architecture.",
        source="example.com",
    )
    res_eng = evaluate_tier1_deterministic(engineer_job, test_profile)
    assert res_eng is None, "Staff Engineer should pass Tier 1 to allow Tier 2 Courtroom evaluation"


def test_tier1_does_not_reject_company_longevity_experience(test_profile):
    """Tier 1: '25 years of experience' describing company heritage must not reject candidate with 4-yr max."""
    test_profile.constraints.max_experience_years = 4

    longevity_job = JobPosting(
        title="Office Assistant",
        link="https://example.com/heritage",
        raw_text="Family-owned plumbing contractor. Our business brings over 25 years of experience serving Rockland County. Seeking office assistant.",
        source="example.com",
    )
    res = evaluate_tier1_deterministic(longevity_job, test_profile)
    assert res is None, "Company longevity boast should not trip candidate experience ceiling"








