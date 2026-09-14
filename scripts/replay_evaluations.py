#!/usr/bin/env python3
"""Replay and re-evaluate historical postings in matches.db under the Tripartite Courtroom Protocol."""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings
from src.evaluator import EvaluationEngine
from src.schemas import JobPosting, UserProfile, EvaluationStatus

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("beacon.replay")


def replay_evaluations(
    db_path: str = "matches.db",
    profile_path: str = "profiles/daniel_giovinazzo.json",
    reason_filter: str = "staff",
    dry_run: bool = True,
):
    """Replay jobs matching a rejection reason through the EvaluationEngine."""
    logger.info(f"Loading profile from {profile_path}...")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = UserProfile.model_validate_json(f.read())

    config = Settings(dry_run=dry_run)
    engine = EvaluationEngine(config, dry_run=dry_run)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    query = "SELECT url, title, source, rejection_reason FROM jobs WHERE status = 'REJECT'"
    params = []
    if reason_filter:
        query += " AND rejection_reason LIKE ?"
        params.append(f"%{reason_filter}%")

    cursor.execute(query, params)
    rows = cursor.fetchall()
    logger.info(f"Found {len(rows)} jobs matching filter '{reason_filter}'")

    rescued_count = 0
    for url, title, source, old_reason in rows:
        posting = JobPosting(
            title=title,
            link=url,
            raw_text=f"<untrusted_job_posting>\n{title}\nSource: {source}\n</untrusted_job_posting>",
            source=source,
        )

        result = engine.evaluate(posting, profile)
        if result.status == EvaluationStatus.MATCH:
            rescued_count += 1
            logger.info(f"RESCUED: '{title}' -> Score: {result.fit_score}/100")
            if result.findings_of_fact:
                logger.info(f"  Findings: {result.findings_of_fact}")
        else:
            logger.info(f"Still rejected: '{title}' -> {result.rejection_reason}")

    logger.info(f"Replay complete. Rescued {rescued_count}/{len(rows)} postings.")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay evaluations from matches.db")
    parser.add_argument("--db", default="matches.db", help="Path to matches.db")
    parser.add_argument("--profile", default="profiles/daniel_giovinazzo.json", help="Profile path")
    parser.add_argument("--filter", default="staff", help="Rejection reason filter")
    parser.add_argument("--live", action="store_true", help="Run with live LLM (default is dry-run heuristic)")
    args = parser.parse_args()

    replay_evaluations(
        db_path=args.db,
        profile_path=args.profile,
        reason_filter=args.filter,
        dry_run=not args.live,
    )
