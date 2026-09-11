"""Unit tests for deterministic filtering, compensation extraction, and multi-tier evaluation."""

import json
from pathlib import Path
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
    # Gemini
    s_gemini = Settings(llm_model="gemini/gemini-2.5-flash", gemini_api_key="key123")
    assert s_gemini.has_llm_credentials() is True

    # Anthropic
    s_claude = Settings(llm_model="claude-3-5-sonnet-20241022", anthropic_api_key="sk-ant-123")
    assert s_claude.has_llm_credentials() is True

    # OpenAI
    s_openai = Settings(llm_model="gpt-4o-mini", openai_api_key="sk-proj-123")
    assert s_openai.has_llm_credentials() is True

    # Ollama (local runtime, needs no API key)
    s_ollama = Settings(llm_model="ollama/llama3.2")
    assert s_ollama.has_llm_credentials() is True

    # Unset credentials
    s_empty = Settings(llm_model="claude-3-5-sonnet-20241022", anthropic_api_key=None)
    assert s_empty.has_llm_credentials() is False


def test_evaluate_tier2_llm_model_agnostic_routing(monkeypatch, test_profile):
    """Verify LiteLLM completion receives the configured model name and returns structured output."""
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
        anthropic_api_key="sk-ant-test",
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

    # Verify that litellm was called with the exact Anthropic model string
    mock_client.chat.completions.create.assert_called_once()
    called_model = mock_client.chat.completions.create.call_args[1]["model"]
    assert called_model == "anthropic/claude-3-5-sonnet-20241022"


def test_generate_tailored_resume_data_model_agnostic(monkeypatch, test_profile):
    """Verify resume tailoring routes to configured LiteLLM model."""
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
        openai_api_key="sk-openai-test",
    )

    job = JobPosting(
        title="Senior Bookkeeper",
        link="https://example.com/gpt-job",
        raw_text="QuickBooks needed.",
        source="example.com",
    )

    resume_out = generate_tailored_resume_data(job, test_profile, settings, dry_run=False)
    assert resume_out.target_headline == "Targeted Senior Bookkeeper"
    called_model = mock_client.chat.completions.create.call_args[1]["model"]
    assert called_model == "gpt-4o-mini"


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


