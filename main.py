"""BeaconAI: Lightweight, modular, deterministic CLI job intelligence engine."""

import json
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import get_settings
from src.db import get_recent_matches, get_stats, init_db, is_job_seen, record_job
from src.evaluator import EvaluationEngine, evaluate_tier1_deterministic
from src.generator import (
    append_daily_digest,
    export_markdown_to_pdf,
    generate_outreach_draft,
    generate_tailored_resume,
)
from src.ingestion import fetch_feed
from src.notifier import send_match_notification
from src.schemas import EvaluationStatus, JobPosting, UserProfile

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Silence verbose third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)

app = typer.Typer(
    name="beacon",
    help="BeaconAI: Deterministic CLI Job Intelligence Engine",
    add_completion=False,
)
console = Console()


def load_profile(profile_path: Path) -> UserProfile:
    """Load and validate declarative user profile JSON."""
    if not profile_path.exists():
        console.print(f"[bold red]Error:[/bold red] Profile file not found at: {profile_path}")
        raise typer.Exit(code=1)

    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        return UserProfile.model_validate(data)
    except Exception as e:
        console.print(f"[bold red]Error parsing profile JSON:[/bold red] {e}")
        raise typer.Exit(code=1)


@app.command()
def init():
    """Initialize SQLite database tables and local artifact directories."""
    settings = get_settings()
    init_db(settings.db_path)
    settings.ensure_directories()
    console.print(
        Panel(
            f"[bold green]✓ Database and directories initialized successfully![/bold green]\n"
            f"• SQLite DB: [cyan]{settings.db_path}[/cyan]\n"
            f"• Matches Dir: [cyan]{settings.matches_dir}[/cyan]\n"
            f"• Artifacts Dir: [cyan]{settings.artifacts_dir}[/cyan]",
            title="BeaconAI Initialization",
            border_style="green",
        )
    )


@app.command("init-db")
def init_db_cmd():
    """Alias for init."""
    init()


@app.command()
def scan(
    profile: Path = typer.Option(
        Path("profiles/bookkeeper.json.example"),
        "--profile",
        "-p",
        help="Path to user profile JSON file.",
    ),
    feed: Optional[List[str]] = typer.Option(
        None,
        "--feed",
        "-f",
        help="RSS feed URL or local XML file path (can be specified multiple times).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-d",
        help="Run without calling live LLM APIs (deterministic scoring).",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-l",
        help="Maximum number of postings to evaluate.",
    ),
    notify: bool = typer.Option(
        False,
        "--notify/--no-notify",
        help="Compile ATS PDF resume and dispatch email notifications for matched jobs via Resend.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Universal LiteLLM model override (defaults to LLM_MODEL env var or settings.llm_model).",
    ),
):
    """Ingest, deduplicate, filter, evaluate, and generate tailored application artifacts."""
    settings = get_settings()
    if model:
        settings.llm_model = model
    init_db(settings.db_path)
    settings.ensure_directories()

    user_profile = load_profile(profile)

    # Collect and normalize target feeds
    raw_feed_inputs = list(feed) if feed else []
    if not raw_feed_inputs and settings.target_feed_urls:
        raw_feed_inputs = [settings.target_feed_urls]

    target_feeds = []
    for raw in raw_feed_inputs:
        for line in str(raw).splitlines():
            for f in line.split(","):
                clean = f.strip()
                if not clean or clean.startswith("#"):
                    continue
                # Extract URL if wrapped in markdown link syntax [text](url)
                md_match = re.search(r"\[.*?\]\((https?://[^\s\)]+)\)", clean)
                if md_match:
                    clean = md_match.group(1).strip()
                target_feeds.append(clean)


    if not target_feeds:
        console.print("[bold red]Error:[/bold red] No target feeds provided via --feed or TARGET_FEED_URLS.")
        raise typer.Exit(code=1)

    active_llm = settings.llm_model or "Not Set"
    mode_label = "[bold yellow]DRY-RUN (Deterministic Scoring)[/bold yellow]" if dry_run else f"[bold cyan]LIVE (Tier 1 + {active_llm})[/bold cyan]"
    notify_label = "[bold green]ENABLED (Resend)[/bold green]" if notify else "[dim]DISABLED[/dim]"
    min_h = f"${user_profile.constraints.min_hourly_rate:.2f}/hr" if user_profile.constraints.min_hourly_rate is not None else "Not Set"
    min_a = f"${user_profile.constraints.min_annual_salary:,.0f}/yr" if user_profile.constraints.min_annual_salary is not None else "Not Set"

    console.print(
        Panel(
            f"Candidate: [bold]{user_profile.name}[/bold] ({user_profile.location})\n"
            f"Target Feeds: [blue]{len(target_feeds)} configured[/blue]\n"
            f"Evaluation Mode: {mode_label}\n"
            f"Notifications: {notify_label}\n"
            f"Active LLM: [magenta]{active_llm}[/magenta]\n"
            f"Min Pay Floor: [green]{min_h}[/green] | [green]{min_a}[/green]\n"
            f"Circuit Breaker Cap: [magenta]{settings.max_llm_evals_per_run} LLM evals/run[/magenta]",
            title="BeaconAI Scan Initiated",
            border_style="cyan",
        )
    )

    engine = EvaluationEngine(settings, dry_run=dry_run)
    total_skipped_count = 0
    total_match_count = 0
    total_reject_count = 0
    total_deferred_count = 0
    all_generated_matches = []

    for current_feed in target_feeds:
        console.print(f"\n[bold blue]==> Scanning Target Feed: {current_feed}[/bold blue]")

        postings = fetch_feed(
            current_feed,
            user_agent=settings.http_user_agent,
            timeout_seconds=settings.request_timeout_seconds,
        )

        if not postings:
            console.print(f"[yellow]No postings found or failed to parse feed: {current_feed}[/yellow]")
            continue

        if limit and limit > 0:
            postings = postings[:limit]

        console.print(f"[bold]Fetched {len(postings)} postings from feed.[/bold]")

        results_table = Table(title=f"Scan Results: {current_feed[:40]}", header_style="bold magenta")
        results_table.add_column("Status", justify="center", width=10)
        results_table.add_column("Tier", justify="center", width=6)
        results_table.add_column("Score", justify="right", width=7)
        results_table.add_column("Title", style="bold", width=34)
        results_table.add_column("Verdict / Details", width=42)

        for posting in postings:
            # Check SQLite deduplication
            if is_job_seen(posting.link, settings.db_path):
                total_skipped_count += 1
                results_table.add_row(
                    "[dim]SKIPPED[/dim]",
                    "-",
                    "-",
                    posting.title[:32],
                    "[dim]Already processed (seen in DB)[/dim]",
                )
                continue

            # Evaluate posting
            result = engine.evaluate(posting, user_profile)

            # Enforce sequential delay between live LLM evaluations to stay strictly within 5-15 RPM quotas
            if not dry_run and result.tier_evaluated == 2 and settings.llm_rate_limit_delay > 0:
                time.sleep(settings.llm_rate_limit_delay)

            if result.status == EvaluationStatus.MATCH:
                try:
                    resume_path = generate_tailored_resume(
                        posting, user_profile, result, settings, dry_run=dry_run
                    )
                    if not dry_run and settings.llm_rate_limit_delay > 0:
                        time.sleep(settings.llm_rate_limit_delay)
                    pdf_path = export_markdown_to_pdf(resume_path)
                    outreach_path = generate_outreach_draft(
                        posting, user_profile, result, settings
                    )
                    all_generated_matches.append((posting, result, resume_path, outreach_path))

                    if notify:
                        sent = send_match_notification(
                            posting, result, pdf_path, outreach_path, config=settings
                        )
                        if sent:
                            console.print(
                                f"  [bold blue]✉ Email alert dispatched to {settings.notification_email_to}[/bold blue]"
                            )
                        else:
                            console.print(
                                "  [dim yellow]⚠ Email alert skipped (check RESEND_API_KEY and NOTIFICATION_EMAIL_TO)[/dim yellow]"
                            )

                    record_job(posting, result, settings.db_path)
                    total_match_count += 1

                    results_table.add_row(
                        "[bold green]MATCH[/bold green]",
                        f"T{result.tier_evaluated}",
                        f"[bold green]{result.fit_score}[/bold green]",
                        posting.title[:32],
                        f"[green]Matched[/green] -> [underline]{pdf_path.name}[/underline]",
                    )
                except Exception as e:
                    logging.getLogger("beacon.main").error(
                        f"Artifact generation failed for '{posting.title}': {e}", exc_info=True
                    )
                    results_table.add_row(
                        "[bold red]ERROR[/bold red]",
                        f"T{result.tier_evaluated}",
                        f"[dim]{result.fit_score}[/dim]",
                        posting.title[:32],
                        f"[red]Synthesis failed: {str(e)[:25]}[/red]",
                    )
                    continue
            elif result.status == EvaluationStatus.DEFERRED:
                record_job(posting, result, settings.db_path)
                total_deferred_count += 1
                reason = result.rejection_reason or "Throttled"
                results_table.add_row(
                    "[bold yellow]DEFERRED[/bold yellow]",
                    f"T{result.tier_evaluated}",
                    "[dim]0[/dim]",
                    posting.title[:32],
                    f"[yellow]{reason[:40]}[/yellow]",
                )
            else:
                record_job(posting, result, settings.db_path)
                total_reject_count += 1
                reason = result.rejection_reason or "Disqualified"
                results_table.add_row(
                    "[bold red]REJECT[/bold red]",
                    f"T{result.tier_evaluated}",
                    f"[dim]{result.fit_score}[/dim]",
                    posting.title[:32],
                    f"[red]{reason[:40]}[/red]",
                )

        console.print(results_table)

    # Append to daily digest if matches occurred across all feeds
    if all_generated_matches:
        digest_path = append_daily_digest(all_generated_matches, settings)
        console.print(f"\n[bold green]✓ Daily digest updated:[/bold green] [cyan]{digest_path}[/cyan]")

    # Summary Panel
    console.print(
        Panel(
            f"• Target Feeds Scanned: [bold]{len(target_feeds)}[/bold]\n"
            f"• Total Evaluated: [bold]{total_match_count + total_reject_count + total_deferred_count}[/bold]\n"
            f"• Skipped (Deduplicated): [dim]{total_skipped_count}[/dim]\n"
            f"• Qualified Matches: [bold green]{total_match_count}[/bold green]\n"
            f"• Disqualified: [bold red]{total_reject_count}[/bold red]\n"
            f"• Deferred (Circuit Breaker): [bold yellow]{total_deferred_count}[/bold yellow]\n"
            f"• Tier-2 LLM / Heuristic Calls: [magenta]{engine.llm_eval_count}[/magenta]",
            title="Scan Summary",
            border_style="cyan",
        )
    )


@app.command()
def stats():
    """Display SQLite database persistence statistics and rejection breakdown."""
    settings = get_settings()
    init_db(settings.db_path)
    data = get_stats(settings.db_path)

    table = Table(title="BeaconAI Persistence Metrics", header_style="bold cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total Postings Tracked", str(data["total_seen"]))
    table.add_row("Matches (Cleared)", f"[green]{data['matches']}[/green]")
    table.add_row("Rejections (Disqualified)", f"[red]{data['rejects']}[/red]")
    table.add_row("Deferred (Throttled)", f"[yellow]{data.get('deferred', 0)}[/yellow]")
    table.add_row("Average Match Score", f"[yellow]{data['avg_match_score']}/100[/yellow]")

    console.print(table)


    if data["rejection_breakdown"]:
        reject_table = Table(title="Top Rejection Causes", header_style="bold red")
        reject_table.add_column("Rejection Cause", style="dim")
        reject_table.add_column("Count", justify="right")
        for item in data["rejection_breakdown"]:
            reject_table.add_row(item["reason"], str(item["count"]))
        console.print(reject_table)

    recent = get_recent_matches(limit=5, db_path=settings.db_path)
    if recent:
        matches_table = Table(title="Recent Qualified Matches", header_style="bold green")
        matches_table.add_column("Score", justify="right", width=6)
        matches_table.add_column("Title", style="bold", width=35)
        matches_table.add_column("Source", width=20)
        matches_table.add_column("Processed At", width=20)
        for row in recent:
            matches_table.add_row(
                str(row["fit_score"]),
                row["title"][:33],
                row["source"],
                row["processed_at"][:19],
            )
        console.print(matches_table)


@app.command("test-eval")
def test_eval(
    text: str = typer.Option(..., "--text", "-t", help="Raw job description or title text to evaluate."),
    profile: Path = typer.Option(
        Path("profiles/bookkeeper.json.example"),
        "--profile",
        "-p",
        help="Path to user profile JSON file.",
    ),
):
    """Test deterministic Tier-1 filtering logic instantly against arbitrary text."""
    user_profile = load_profile(profile)
    fake_posting = JobPosting(
        title="Test Job Posting",
        link="https://example.com/test",
        raw_text=text,
        source="manual-test",
    )

    result = evaluate_tier1_deterministic(fake_posting, user_profile)
    if result is None:
        console.print(
            Panel(
                "[bold green]CLEARED TIER 1[/bold green]\n"
                "The text passed all deterministic checks (physical, schedule, pay floor).\n"
                "Eligible for Tier 2 evaluation.",
                title="Tier 1 Verdict",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                f"[bold red]REJECTED AT TIER 1[/bold red]\n"
                f"Reason: [red]{result.rejection_reason}[/red]\n"
                f"Estimated Comp: {result.estimated_compensation or 'None detected'}",
                title="Tier 1 Verdict",
                border_style="red",
            )
        )


if __name__ == "__main__":
    app()
