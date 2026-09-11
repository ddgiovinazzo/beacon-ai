"""SQLite persistence, schema management, and deduplication logic."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Union

from src.schemas import EvaluationResult, JobPosting


def get_connection(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Create and return an optimized SQLite connection."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for high concurrency and performance
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path: Union[str, Path] = "matches.db") -> None:
    """Initialize the SQLite schema if not already present."""
    schema = """
    CREATE TABLE IF NOT EXISTS jobs (
        url TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        fit_score INTEGER DEFAULT 0,
        rejection_reason TEXT,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_processed_at ON jobs(processed_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_url_status ON jobs(url, status);
    """
    with get_connection(db_path) as conn:
        conn.executescript(schema)
        conn.commit()


def is_job_seen(url: str, db_path: Union[str, Path] = "matches.db") -> bool:
    """Check if a posting URL has already been processed and finalized (MATCH or REJECT).
    
    DEFERRED jobs return False so they remain eligible for evaluation on future runs.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM jobs WHERE url = ? AND status IN ('MATCH', 'REJECT') LIMIT 1;",
            (url,),
        )
        row = cursor.fetchone()
        return row is not None


def record_job(
    job: JobPosting,
    result: EvaluationResult,
    db_path: Union[str, Path] = "matches.db",
) -> None:
    """Persist an evaluated job posting and its verdict to SQLite."""
    sql = """
    INSERT OR REPLACE INTO jobs (url, title, source, status, fit_score, rejection_reason, processed_at)
    VALUES (?, ?, ?, ?, ?, ?, ?);
    """
    now = datetime.now(timezone.utc).isoformat()

    with get_connection(db_path) as conn:
        conn.execute(
            sql,
            (
                job.link,
                job.title,
                job.source,
                result.status.value,
                result.fit_score,
                result.rejection_reason,
                now,
            ),
        )
        conn.commit()


def get_stats(db_path: Union[str, Path] = "matches.db") -> Dict[str, Any]:
    """Retrieve operational statistics on processed postings and rejection causes."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Overall counts
        cursor.execute("SELECT COUNT(*) FROM jobs;")
        total_seen = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'MATCH';")
        matches = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'REJECT';")
        rejects = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'DEFERRED';")
        deferred = cursor.fetchone()[0]

        # Top rejection reasons
        cursor.execute(
            """
            SELECT rejection_reason, COUNT(*) as count 
            FROM jobs 
            WHERE status = 'REJECT' AND rejection_reason IS NOT NULL
            GROUP BY rejection_reason 
            ORDER BY count DESC 
            LIMIT 10;
            """
        )
        rejection_breakdown = [
            {"reason": row["rejection_reason"], "count": row["count"]}
            for row in cursor.fetchall()
        ]

        # Average fit score of matches
        cursor.execute("SELECT AVG(fit_score) FROM jobs WHERE status = 'MATCH';")
        avg_score_row = cursor.fetchone()[0]
        avg_match_score = round(avg_score_row, 1) if avg_score_row is not None else 0.0

        return {
            "total_seen": total_seen,
            "matches": matches,
            "rejects": rejects,
            "deferred": deferred,
            "avg_match_score": avg_match_score,
            "rejection_breakdown": rejection_breakdown,
        }



def get_recent_matches(
    limit: int = 20,
    db_path: Union[str, Path] = "matches.db",
) -> List[Dict[str, Any]]:
    """Retrieve list of recently cleared MATCH postings."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT url, title, source, fit_score, rejection_reason, processed_at
            FROM jobs
            WHERE status = 'MATCH'
            ORDER BY processed_at DESC
            LIMIT ?;
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
