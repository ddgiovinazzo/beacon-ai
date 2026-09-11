"""Unit tests for deterministic filtering, compensation extraction, and multi-tier evaluation."""

import pytest

from src.config import Settings
from src.evaluator import (
    EvaluationEngine,
    evaluate_tier1_deterministic,
    evaluate_tier2_heuristic,
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


def test_tier1_rejects_lifting_violation(test_profile):
    """Tier 1 must reject postings requiring lifting exceeding physical threshold."""
    job = JobPosting(
        title="Stock Clerk",
        link="https://example.com/job1",
        raw_text="Must be able to lift 60 lbs unassisted on a regular basis.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert result.tier_evaluated == 1
    assert "60 lbs" in result.rejection_reason


def test_tier1_rejects_keyword_physical_restriction(test_profile):
    """Tier 1 must reject postings matching explicit physical restriction keywords."""
    job = JobPosting(
        title="Facility Maintenance",
        link="https://example.com/job2",
        raw_text="General facility work, ladder work, and fixture replacements.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert result.tier_evaluated == 1
    assert "ladder" in result.rejection_reason.lower()


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


def test_tier1_rejects_schedule_conflict(test_profile):
    """Tier 1 must reject postings requiring restricted schedules."""
    job = JobPosting(
        title="Audit Assistant",
        link="https://example.com/job4",
        raw_text="Reconcile daily night registers. Hours: Graveyard shift 11 PM to 7 AM.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert result.tier_evaluated == 1
    assert "graveyard shift" in result.rejection_reason.lower()


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
        raw_text="Experienced Bookkeeper needed. Proficient in QuickBooks Online and Excel reconciliations.",
        source="example.com",
    )
    result = evaluate_tier2_heuristic(job, test_profile)
    assert result.status == EvaluationStatus.MATCH
    assert result.fit_score >= 70
    assert result.tier_evaluated == 2


def test_circuit_breaker_caps_evaluations(test_profile):
    """Circuit breaker must enforce MAX_LLM_EVALS_PER_RUN cap."""
    settings = Settings(max_llm_evals_per_run=2)
    engine = EvaluationEngine(settings, dry_run=True)

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/job",
        raw_text="QuickBooks and Excel accounting work. $35/hr.",
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
    """Actual physical ladder demands must trigger Tier 1 rejection."""
    job = JobPosting(
        title="Maintenance Assistant",
        link="https://example.com/job-phys-ladder",
        raw_text="Must climb 10-foot ladders for facility maintenance and lighting.",
        source="example.com",
    )
    result = evaluate_tier1_deterministic(job, test_profile)
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert "ladder" in result.rejection_reason.lower()


def test_circuit_breaker_sets_deferred_and_eligible_for_rescan(tmp_path, test_profile):
    """Deferred jobs must NOT be marked as permanently seen in SQLite."""
    from src.db import init_db, is_job_seen, record_job

    db_file = tmp_path / "test_deferred.db"
    init_db(db_file)

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/deferred-job",
        raw_text="QuickBooks and reconciliations. $35/hr.",
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
    assert result is not None
    assert result.status == EvaluationStatus.REJECT
    assert "prolonged standing" in result.rejection_reason


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
        raw_text="Seeking candidate strong in general ledger and payroll compliance.",
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
