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

    # Third evaluation must hit circuit breaker
    res3 = engine.evaluate(job, test_profile)
    assert res3.status == EvaluationStatus.REJECT
    assert "Circuit breaker limit reached" in res3.rejection_reason
