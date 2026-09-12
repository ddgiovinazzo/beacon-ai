"""Unit tests for SQLite database persistence, deduplication, and cache clearing."""

import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from main import app
from src.db import (
    TIMEFRAME_PRESETS,
    clear_cache,
    clear_match_artifacts,
    get_cache_counts,
    get_recent_matches,
    get_stats,
    init_db,
    is_job_seen,
    parse_timeframe,
    record_job,
)
from src.schemas import EvaluationResult, EvaluationStatus, JobPosting


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    yield db_path
    if db_path.exists():
        os.unlink(db_path)


def test_parse_timeframe():
    """Verify natural language and short code parsing for Google-style timeframes."""
    # 1 hour
    for s in ["1h", "1 h", "1 hour", "1hour", "hour", "last hour"]:
        key, delta = parse_timeframe(s)
        assert key == "1h"
        assert delta == timedelta(hours=1)

    # 6 hours
    for s in ["6h", "6 hours", "last 6 hours"]:
        key, delta = parse_timeframe(s)
        assert key == "6h"
        assert delta == timedelta(hours=6)

    # 1 day
    for s in ["1d", "1 day", "24h", "last 24 hours"]:
        key, delta = parse_timeframe(s)
        assert key == "1d"
        assert delta == timedelta(days=1)

    # 1 week
    for s in ["1w", "1 week", "7d", "last week"]:
        key, delta = parse_timeframe(s)
        assert key == "1w"
        assert delta == timedelta(weeks=1)

    # 1 month
    for s in ["1m", "1 month", "30d", "last month"]:
        key, delta = parse_timeframe(s)
        assert key == "1m"
        assert delta == timedelta(days=30)

    # 6 months
    for s in ["6m", "6 months", "last 6 months"]:
        key, delta = parse_timeframe(s)
        assert key == "6m"
        assert delta == timedelta(days=182)

    # 1 year
    for s in ["1y", "1 year", "365d", "last year"]:
        key, delta = parse_timeframe(s)
        assert key == "1y"
        assert delta == timedelta(days=365)

    # All time
    for s in ["all", "all time", "all-time", "alltime", "everything"]:
        key, delta = parse_timeframe(s)
        assert key == "all"
        assert delta is None

    # Invalid
    with pytest.raises(ValueError, match="Unknown timeframe"):
        parse_timeframe("2 centuries")


def test_get_cache_counts_and_clear(temp_db):
    """Test populating postings at various time deltas, inspecting counts, and clearing."""
    now = datetime.now(timezone.utc)

    # Insert test rows manually with controlled timestamps
    jobs_data = [
        ("https://ex.com/1", "Job 1", "MATCH", now - timedelta(minutes=15)),    # < 1h
        ("https://ex.com/2", "Job 2", "REJECT", now - timedelta(hours=3)),      # < 6h
        ("https://ex.com/3", "Job 3", "MATCH", now - timedelta(hours=12)),     # < 1d
        ("https://ex.com/4", "Job 4", "REJECT", now - timedelta(days=3)),       # < 1w
        ("https://ex.com/5", "Job 5", "MATCH", now - timedelta(days=15)),      # < 1m
        ("https://ex.com/6", "Job 6", "REJECT", now - timedelta(days=60)),      # < 6m
        ("https://ex.com/7", "Job 7", "MATCH", now - timedelta(days=250)),     # < 1y
        ("https://ex.com/8", "Job 8", "REJECT", now - timedelta(days=500)),     # > 1y (all time only)
    ]

    with sqlite3.connect(temp_db) as conn:
        for url, title, status, ts in jobs_data:
            conn.execute(
                "INSERT INTO jobs (url, title, source, status, fit_score, processed_at) VALUES (?, ?, 'test', ?, 80, ?)",
                (url, title, status, ts.isoformat()),
            )

    counts = get_cache_counts(temp_db)
    assert counts["1h"] == 1
    assert counts["6h"] == 2
    assert counts["1d"] == 3
    assert counts["1w"] == 4
    assert counts["1m"] == 5
    assert counts["6m"] == 6
    assert counts["1y"] == 7
    assert counts["all"] == 8

    # Filter by status: only REJECT
    reject_counts = get_cache_counts(temp_db, status="REJECT")
    assert reject_counts["1h"] == 0
    assert reject_counts["6h"] == 1
    assert reject_counts["all"] == 4

    # Clear last 1 hour
    deleted = clear_cache("1h", db_path=temp_db)
    assert deleted == 1
    assert not is_job_seen("https://ex.com/1", db_path=temp_db)
    assert is_job_seen("https://ex.com/2", db_path=temp_db)

    # Clear last 1 week
    deleted = clear_cache("1w", db_path=temp_db)
    assert deleted == 3  # Job 2, 3, 4 (Job 1 already cleared)
    assert not is_job_seen("https://ex.com/2", db_path=temp_db)
    assert is_job_seen("https://ex.com/5", db_path=temp_db)

    # Clear all time
    deleted = clear_cache("all", db_path=temp_db)
    assert deleted == 4  # Job 5, 6, 7, 8
    all_counts = get_cache_counts(temp_db)
    assert all_counts["all"] == 0


def test_clear_cache_with_status_filter(temp_db):
    """Test that clearing with status='REJECT' leaves MATCH jobs intact."""
    now = datetime.now(timezone.utc)
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO jobs (url, title, source, status, fit_score, processed_at) VALUES (?, ?, 'test', 'MATCH', 90, ?)",
            ("https://ex.com/match", "Match Job", now.isoformat()),
        )
        conn.execute(
            "INSERT INTO jobs (url, title, source, status, fit_score, processed_at) VALUES (?, ?, 'test', 'REJECT', 30, ?)",
            ("https://ex.com/reject", "Reject Job", now.isoformat()),
        )

    deleted = clear_cache("1h", status="REJECT", db_path=temp_db)
    assert deleted == 1
    assert is_job_seen("https://ex.com/match", db_path=temp_db)
    assert not is_job_seen("https://ex.com/reject", db_path=temp_db)


def test_clear_match_artifacts(tmp_path):
    """Verify purging of generated resume/outreach files according to timeframe."""
    matches_dir = tmp_path / "matches"
    matches_dir.mkdir()

    f1 = matches_dir / "recent_resume.md"
    f1.write_text("recent")

    # Set older mtime on f2 (2 days ago)
    f2 = matches_dir / "older_resume.pdf"
    f2.write_text("older")
    two_days_ago = time.time() - (2 * 86400)
    os.utime(f2, (two_days_ago, two_days_ago))

    # Dotfile should not be touched
    dotfile = matches_dir / ".gitkeep"
    dotfile.write_text("keep")

    # Purge last 1 hour
    deleted = clear_match_artifacts("1h", matches_dir=matches_dir)
    assert deleted == 1
    assert not f1.exists()
    assert f2.exists()
    assert dotfile.exists()

    # Purge all
    deleted_all = clear_match_artifacts("all", matches_dir=matches_dir)
    assert deleted_all == 1
    assert not f2.exists()
    assert dotfile.exists()


def test_clear_cache_cli(temp_db, monkeypatch):
    """Test CLI invocation of clear-cache command with flags."""
    runner = CliRunner()
    import main
    # Mock settings to use temp_db
    class MockSettings:
        db_path = temp_db
        matches_dir = Path("artifacts/matches")
    monkeypatch.setattr(main, "get_settings", lambda: MockSettings())

    # Insert a job
    now = datetime.now(timezone.utc)
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO jobs (url, title, source, status, fit_score, processed_at) VALUES (?, ?, 'test', 'MATCH', 95, ?)",
            ("https://ex.com/cli-test", "CLI Job", now.isoformat()),
        )

    # Run CLI with --timeframe 1h --yes
    result = runner.invoke(app, ["clear-cache", "--timeframe", "1h", "--yes"])
    assert result.exit_code == 0
    assert "Cache cleared successfully!" in result.stdout
    assert "1" in result.stdout

    # Test interactive prompt: insert another job, choose option 8 (All time), and confirm 'y'
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO jobs (url, title, source, status, fit_score, processed_at) VALUES (?, ?, 'test', 'MATCH', 95, ?)",
            ("https://ex.com/cli-test-2", "CLI Job 2", now.isoformat()),
        )
    result_interactive = runner.invoke(app, ["clear-cache"], input="8\ny\n")
    assert result_interactive.exit_code == 0
    assert "Cache cleared successfully!" in result_interactive.stdout
    assert "All time" in result_interactive.stdout

    # Run alias with invalid timeframe
    result_invalid = runner.invoke(app, ["cache-clear", "--timeframe", "bad-timeframe", "--yes"])
    assert result_invalid.exit_code == 1
    assert "Unknown timeframe" in result_invalid.stdout
