"""SQLite persistence, schema management, and deduplication logic."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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

    CREATE TABLE IF NOT EXISTS blocked_companies (
        company_name TEXT PRIMARY KEY,
        reason TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
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

    sanitized_reason = result.rejection_reason
    if sanitized_reason:
        # Strip specific geographic PII that the LLM might echo from candidate constraints
        sanitized_reason = re.sub(r"from\s+[A-Z][a-zA-Z\s,]+(?:\b\d{5}\b)?", "from candidate location", sanitized_reason)

    with get_connection(db_path) as conn:
        conn.execute(
            sql,
            (
                job.link,
                job.title,
                job.source,
                result.status.value,
                result.fit_score,
                sanitized_reason,
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
 
 
TIMEFRAME_PRESETS: Dict[str, Tuple[str, Optional[timedelta]]] = {
    "1h": ("Last 1 hour", timedelta(hours=1)),
    "6h": ("Last 6 hours", timedelta(hours=6)),
    "1d": ("Last 1 day (24 hours)", timedelta(days=1)),
    "1w": ("Last 1 week (7 days)", timedelta(weeks=1)),
    "1m": ("Last 1 month (30 days)", timedelta(days=30)),
    "6m": ("Last 6 months", timedelta(days=182)),
    "1y": ("Last 1 year", timedelta(days=365)),
    "all": ("All time", None),
}


def parse_timeframe(timeframe_str: str) -> Tuple[str, Optional[timedelta]]:
    """Parse and normalize timeframe string into preset key and timedelta.
    
    Supports: 1h, 6h, 1d, 1w, 1m, 6m, 1y, all (and natural language equivalents like
    '1 hour', '6 hours', '1 day', '1 week', '1 month', '6 months', '1 year', 'all time').
    """
    cleaned = timeframe_str.strip().lower().replace("_", " ").replace("-", " ")
    if cleaned in ("1h", "1 h", "1 hour", "1hour", "hour", "last hour"):
        return "1h", timedelta(hours=1)
    if cleaned in ("6h", "6 h", "6 hours", "6hours", "last 6 hours"):
        return "6h", timedelta(hours=6)
    if cleaned in ("1d", "1 d", "1 day", "1day", "day", "24h", "24 hours", "last day", "last 24 hours"):
        return "1d", timedelta(days=1)
    if cleaned in ("1w", "1 w", "1 week", "1week", "week", "7d", "7 days", "last week", "last 7 days"):
        return "1w", timedelta(weeks=1)
    if cleaned in ("1m", "1 m", "1 month", "1month", "month", "30d", "last month", "last 4 weeks"):
        return "1m", timedelta(days=30)
    if cleaned in ("6m", "6 m", "6 months", "6months", "last 6 months"):
        return "6m", timedelta(days=182)
    if cleaned in ("1y", "1 y", "1 year", "1year", "year", "365d", "last year", "last 1 year"):
        return "1y", timedelta(days=365)
    if cleaned in ("all", "all time", "alltime", "everything"):
        return "all", None
    raise ValueError(
        f"Unknown timeframe '{timeframe_str}'. Valid options: 1h, 6h, 1d, 1w, 1m, 6m, 1y, all (or '1 hour', '1 day', 'all time', etc.)"
    )


def get_cache_counts(
    db_path: Union[str, Path] = "matches.db",
    status: Optional[str] = None,
) -> Dict[str, int]:
    """Retrieve count of jobs eligible for clearing across each standard timeframe preset."""
    counts = {}
    now = datetime.now(timezone.utc)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for key, (_, delta) in TIMEFRAME_PRESETS.items():
            query = "SELECT COUNT(*) FROM jobs WHERE 1=1"
            params: List[Any] = []

            if delta is not None:
                cutoff = (now - delta).isoformat()
                query += " AND datetime(processed_at) >= datetime(?)"
                params.append(cutoff)

            if status and status.upper() != "ALL":
                query += " AND status = ?"
                params.append(status.upper())

            cursor.execute(query, params)
            counts[key] = cursor.fetchone()[0]

    return counts


def clear_cache(
    timeframe: str,
    status: Optional[str] = None,
    db_path: Union[str, Path] = "matches.db",
) -> int:
    """Clear cached jobs matching timeframe and optional status filter.

    Returns the number of deleted records.
    """
    key, delta = parse_timeframe(timeframe)
    now = datetime.now(timezone.utc)

    query = "DELETE FROM jobs WHERE 1=1"
    params: List[Any] = []

    if delta is not None:
        cutoff = (now - delta).isoformat()
        query += " AND datetime(processed_at) >= datetime(?)"
        params.append(cutoff)

    if status and status.upper() != "ALL":
        query += " AND status = ?"
        params.append(status.upper())

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        deleted_count = cursor.rowcount
        conn.commit()
        # Compact database to reclaim disk space
        conn.execute("VACUUM;")
        conn.commit()

    return deleted_count


def clear_match_artifacts(
    timeframe: str,
    matches_dir: Union[str, Path] = "artifacts/matches",
) -> int:
    """Purge generated resume/outreach match artifacts created within the given timeframe."""
    dir_path = Path(matches_dir)
    if not dir_path.exists():
        return 0

    key, delta = parse_timeframe(timeframe)
    now_ts = datetime.now(timezone.utc).timestamp()
    deleted_files = 0

    for file in dir_path.iterdir():
        if file.name.startswith(".") or file.is_dir():
            continue
        if delta is not None:
            cutoff_ts = now_ts - delta.total_seconds()
            if file.stat().st_mtime < cutoff_ts:
                continue
        try:
            file.unlink()
            deleted_files += 1
        except OSError:
            pass

    return deleted_files


def block_company(
    company_name: str,
    reason: str = "manual",
    db_path: Union[str, Path] = "matches.db",
) -> None:
    """Add a company to the persistent blocklist."""
    clean_name = company_name.strip()
    if not clean_name:
        return
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO blocked_companies (company_name, reason, added_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(company_name) DO UPDATE SET reason = excluded.reason;
            """,
            (clean_name, reason),
        )
        conn.commit()


def is_company_blocked(
    company_name: str,
    db_path: Union[str, Path] = "matches.db",
) -> bool:
    """Check if a company name matches any entry in the persistent blocklist."""
    clean_name = company_name.strip().lower()
    if not clean_name:
        return False
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT company_name FROM blocked_companies;")
        rows = cursor.fetchall()
        for row in rows:
            blocked_term = str(row[0]).strip().lower()
            if blocked_term and (blocked_term in clean_name or clean_name in blocked_term):
                return True
    return False


def get_blocked_companies(db_path: Union[str, Path] = "matches.db") -> List[str]:
    """Retrieve all blocked company names from SQLite."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT company_name FROM blocked_companies ORDER BY company_name ASC;")
        rows = cursor.fetchall()
        return [str(r[0]) for r in rows if r[0]]


def get_blocked_companies_details(db_path: Union[str, Path] = "matches.db") -> List[Dict[str, Any]]:
    """Retrieve all blocked company records with details from SQLite."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT company_name, reason, added_at FROM blocked_companies ORDER BY added_at DESC, company_name ASC;")
        rows = cursor.fetchall()
        return [
            {
                "company_name": str(r[0]),
                "reason": str(r[1]) if r[1] is not None else "",
                "added_at": str(r[2]) if r[2] is not None else "",
            }
            for r in rows
            if r[0]
        ]


