"""Ingestion, feed parsing, and sanitization module."""

import logging
import re
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from src.schemas import JobPosting

logger = logging.getLogger("beacon.ingestion")

# Regex to detect zero-width characters and invisible unicode artifacts
ZERO_WIDTH_PATTERN = re.compile(r"[\u200B-\u200D\uFEFF\u00A0\u202A-\u202E]")


def sanitize_html(raw_html: str) -> str:
    """Sanitize raw HTML/XML job description text.
    
    Strips script tags, style blocks, hidden spans, zero-width characters,
    and returns normalized plain text.
    """
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    # 1. Strip script, style, noscript, svg, and iframe elements
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "head", "meta"]):
        tag.decompose()

    # 2. Decompose elements with hidden styles (display:none, visibility:hidden)
    for hidden_tag in soup.find_all(
        lambda tag: tag.has_attr("style")
        and any(
            rule in tag["style"].lower().replace(" ", "")
            for rule in ["display:none", "visibility:hidden"]
        )
    ):
        hidden_tag.decompose()

    # Also decompose elements with hidden attributes
    for hidden_tag in soup.find_all(attrs={"hidden": True}):
        hidden_tag.decompose()

    # 3. Extract text with clean spacing
    text = soup.get_text(separator=" ", strip=True)

    # 4. Strip zero-width and invisible unicode characters
    text = ZERO_WIDTH_PATTERN.sub(" ", text)

    # 5. Normalize multiple whitespace and newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)

    return text.strip()


def wrap_untrusted_content(clean_text: str) -> str:
    """Encapsulate untrusted posting content in XML guard boundaries.
    
    Protects downstream LLMs against prompt injection instructions
    embedded in job postings.
    """
    # Sanitize any malicious closing tags inside the untrusted text
    safe_text = clean_text.replace("</untrusted_job_posting>", "[escaped_tag]")
    return f"<untrusted_job_posting>\n{safe_text}\n</untrusted_job_posting>"


def fetch_feed(
    source: Union[str, Path],
    user_agent: str = "BeaconAI/1.0 (+https://github.com/beacon-ai; polite-job-crawler)",
    timeout_seconds: int = 15,
) -> List[JobPosting]:
    """Ingest and parse an RSS feed from a remote URL or local XML file.
    
    Returns a list of sanitized JobPosting instances.
    """
    source_str = str(source).strip()
    feed_content: Union[str, bytes] = ""
    source_id = source_str

    is_url = urlparse(source_str).scheme in ("http", "https")

    if is_url:
        logger.info(f"Fetching RSS feed from remote URL: {source_str}")
        try:
            headers = {"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
            response = requests.get(source_str, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            feed_content = response.content
            source_id = urlparse(source_str).netloc
        except Exception as e:
            logger.error(f"Failed to fetch RSS feed from {source_str}: {e}")
            return []
    else:
        # Local file path
        local_path = Path(source_str)
        if not local_path.exists():
            logger.error(f"Local feed file does not exist: {local_path}")
            return []
        logger.info(f"Reading RSS feed from local file: {local_path}")
        feed_content = local_path.read_bytes()
        source_id = local_path.name

    # Parse feed with feedparser
    parsed = feedparser.parse(feed_content)

    if parsed.bozo and not parsed.entries:
        logger.warning(f"Feed parsing error for {source_id}: {parsed.bozo_exception}")
        return []

    postings: List[JobPosting] = []

    for entry in parsed.entries:
        title = entry.get("title", "Untitled Posting").strip()
        link = entry.get("link", "").strip()

        # Fallback to entry ID if link is missing
        if not link and "id" in entry:
            link = str(entry["id"]).strip()

        if not link:
            # Skip invalid entries lacking unique identifier/URL
            continue

        published = entry.get("published", entry.get("updated", None))

        # Extract raw description or content
        raw_body = ""
        if "content" in entry and entry.content:
            raw_body = entry.content[0].get("value", "")
        elif "summary" in entry:
            raw_body = entry.get("summary", "")
        elif "description" in entry:
            raw_body = entry.get("description", "")

        # Sanitize HTML and wrap in untrusted guard boundaries
        clean_body = sanitize_html(raw_body)
        guarded_body = wrap_untrusted_content(clean_body)

        postings.append(
            JobPosting(
                title=title,
                link=link,
                published=published,
                raw_text=guarded_body,
                source=source_id,
            )
        )

    logger.info(f"Parsed {len(postings)} postings from {source_id}")
    return postings
