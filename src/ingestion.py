import email
from email.header import decode_header
import imaplib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union
from urllib.parse import urlparse

import feedparser
import requests
import warnings
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

from src.config import HTTP_USER_AGENT
from src.schemas import JobPosting

if TYPE_CHECKING:
    from src.config import Settings

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

    if "<" not in raw_html:
        text = ZERO_WIDTH_PATTERN.sub(" ", raw_html)
        text = re.sub(r"[ \t]+", " ", text)
        return re.sub(r"\n\s*\n+", "\n\n", text).strip()

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


# Maximum allowed RSS feed response size in bytes (10 MB)
MAX_FEED_BYTES = 10 * 1024 * 1024


def wrap_untrusted_content(clean_text: str) -> str:
    """Encapsulate untrusted posting content in XML guard boundaries.
    
    Protects downstream LLMs against prompt injection instructions
    embedded in job postings using case-insensitive and whitespace-tolerant tag neutralization.
    """
    safe_text = re.sub(
        r"<\s*/\s*untrusted_job_posting\s*>",
        "[escaped_tag]",
        clean_text,
        flags=re.IGNORECASE,
    )
    return f"<untrusted_job_posting>\n{safe_text}\n</untrusted_job_posting>"


DEFAULT_BROWSER_USER_AGENT = HTTP_USER_AGENT


def fetch_feed(
    source: Union[str, Path],
    user_agent: str = HTTP_USER_AGENT,
    timeout_seconds: int = 15,
) -> List[JobPosting]:
    """Ingest and parse an RSS feed from a remote URL or local XML file.
    
    Enforces a 10MB response ceiling and returns a list of sanitized JobPosting instances.
    """
    source_str = str(source).strip()
    feed_content: Union[str, bytes] = ""
    source_id = source_str

    is_url = urlparse(source_str).scheme in ("http", "https")

    if is_url:
        logger.info(f"Fetching RSS feed from remote URL: {source_str}")
        try:
            headers = {
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            }
            with requests.get(source_str, headers=headers, timeout=timeout_seconds, stream=True) as response:
                response.raise_for_status()
                chunks = []
                total_bytes = 0
                for chunk in response.iter_content(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FEED_BYTES:
                        logger.error(f"Feed payload exceeded maximum size ({MAX_FEED_BYTES} bytes). Aborting.")
                        return []
                    chunks.append(chunk)
                feed_content = b"".join(chunks)
            source_id = urlparse(source_str).netloc
        except requests.exceptions.HTTPError as e:
            status_code = getattr(e.response, "status_code", "Error")
            logger.warning(
                f"HTTP {status_code} encountered while fetching feed from {source_str}: {e}. Skipping feed."
            )
            return []
        except Exception as e:
            logger.error(f"Failed to fetch RSS feed from {source_str}: {e}. Skipping feed.")
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

        published = None
        if "published" in entry:
            published = entry["published"]
        elif "updated" in entry:
            published = entry["updated"]

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


def decode_email_header(header_val: Optional[str]) -> str:
    """Safely decode RFC 2047 MIME encoded-word email headers."""
    if not header_val:
        return ""
    decoded_fragments = []
    for fragment, charset in decode_header(header_val):
        if isinstance(fragment, bytes):
            charset_name = charset or "utf-8"
            try:
                decoded_fragments.append(fragment.decode(charset_name, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_fragments.append(fragment.decode("utf-8", errors="replace"))
        else:
            decoded_fragments.append(str(fragment))
    return " ".join(decoded_fragments).strip()


def parse_craigslist_alert_email(
    html_body: str,
    plain_body: str,
    date_str: Optional[str] = None,
) -> List[JobPosting]:
    """Extract individual job listings from a Craigslist saved search alert email."""
    postings: List[JobPosting] = []
    seen_links = set()

    # 1. Parse HTML structure if available
    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            # Discard non-craigslist or management/help/terms links
            if not href.startswith(("http://", "https://")):
                continue
            parsed_url = urlparse(href)
            netloc = parsed_url.netloc.lower()
            if not (netloc == "craigslist.org" or netloc.endswith(".craigslist.org")):
                continue
            path = parsed_url.path.lower()
            if any(ign in path for ign in ["/about/", "/help/", "/terms", "/sub/", "accounts.", "/feedback", "unsubscribe"]):
                continue
            # Ensure it targets an actual posting (usually ends in .html or contains /d/)
            if not (".html" in path or "/d/" in path or "/o/" in path):
                continue

            if href in seen_links:
                continue

            title = a_tag.get_text(separator=" ", strip=True)
            if not title or len(title) < 2 or title.lower() in ["view", "details", "click here", "link"]:
                parent_text = a_tag.parent.get_text(separator=" ", strip=True) if a_tag.parent else ""
                if len(parent_text) > len(title):
                    title = parent_text

            if not title or len(title) < 2:
                continue

            # Extract descriptive context from parent container
            container = a_tag.find_parent(["tr", "li", "p", "div"])
            snippet = container.get_text(separator=" ", strip=True) if container else title

            clean_snippet = sanitize_html(snippet)
            guarded_snippet = wrap_untrusted_content(clean_snippet)

            seen_links.add(href)
            postings.append(
                JobPosting(
                    title=title,
                    link=href,
                    published=date_str,
                    raw_text=guarded_snippet,
                    source="email:craigslist",
                )
            )

    # 2. Fallback to plain text URLs if HTML produced nothing
    if not postings and plain_body:
        for match in re.finditer(r"(https?://[a-zA-Z0-9.-]*craigslist\.org/[^\s<>'\"]+\.html)", plain_body):
            url = match.group(1).strip()
            if url in seen_links:
                continue
            parsed_url = urlparse(url)
            if any(ign in parsed_url.path.lower() for ign in ["/about/", "/help/", "/terms", "accounts."]):
                continue

            lines = [l.strip() for l in plain_body.splitlines() if url in l]
            context_line = lines[0] if lines else url
            clean_title = context_line.replace(url, "").strip(" -:|\t") or "Craigslist Job Posting"

            seen_links.add(url)
            clean_snippet = sanitize_html(context_line)
            postings.append(
                JobPosting(
                    title=clean_title,
                    link=url,
                    published=date_str,
                    raw_text=wrap_untrusted_content(clean_snippet),
                    source="email:craigslist",
                )
            )

    logger.info(f"Extracted {len(postings)} job postings from Craigslist alert email")
    return postings


def parse_generic_job_alert_email(
    html_body: str,
    plain_body: str,
    subject: str,
    sender: str,
    date_str: Optional[str] = None,
) -> List[JobPosting]:
    """Parse generic job alert emails (e.g., LinkedIn, Indeed, ZipRecruiter, Google Alerts)."""
    postings: List[JobPosting] = []
    seen_links = set()

    sender_domain = "email"
    email_match = re.search(r"@([\w.-]+)", sender)
    if email_match:
        sender_domain = f"email:{email_match.group(1).lower()}"

    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href.startswith(("http://", "https://")):
                continue
            href_lower = href.lower()
            if any(k in href_lower for k in ["/job/", "/jobs/", "/viewjob", "/posting/", "/apply", "/careers/"]):
                if any(ign in href_lower for ign in ["unsubscribe", "preferences", "privacy", "help", "terms", "settings"]):
                    continue
                if href in seen_links:
                    continue

                title = a_tag.get_text(separator=" ", strip=True)
                if not title or len(title) < 3 or title.lower() in ["apply", "view", "apply now", "view job", "learn more"]:
                    container = a_tag.find_parent(["tr", "li", "div", "p"])
                    if container:
                        title = container.get_text(separator=" ", strip=True)[:100]

                if not title or len(title) < 3:
                    continue

                container = a_tag.find_parent(["tr", "li", "div", "p"])
                snippet = container.get_text(separator=" ", strip=True) if container else title

                seen_links.add(href)
                postings.append(
                    JobPosting(
                        title=title,
                        link=href,
                        published=date_str,
                        raw_text=wrap_untrusted_content(sanitize_html(snippet)),
                        source=sender_domain,
                    )
                )

    # If no discrete multiple job links were extracted, treat the whole email as a single job if descriptive
    if not postings:
        body_text = plain_body if plain_body else (sanitize_html(html_body) if html_body else "")
        if body_text and len(body_text.strip()) > 30 and len(subject.strip()) > 3:
            link_match = re.search(r"https?://[^\s<>'\"]+", body_text)
            direct_link = link_match.group(0).strip() if link_match else f"email:{hash(subject + (date_str or ''))}"
            clean_subject = re.sub(r"^(?:fwd?|re):\s*", "", subject, flags=re.IGNORECASE).strip()

            postings.append(
                JobPosting(
                    title=clean_subject,
                    link=direct_link,
                    published=date_str,
                    raw_text=wrap_untrusted_content(sanitize_html(body_text)),
                    source=sender_domain,
                )
            )

    logger.info(f"Extracted {len(postings)} job postings from generic alert email ({sender_domain})")
    return postings


def parse_email_message(msg: email.message.Message) -> List[JobPosting]:
    """Decode and extract job postings from an RFC 822 email message."""
    subject = decode_email_header(msg.get("Subject", ""))
    sender = decode_email_header(msg.get("From", ""))
    date_str = decode_email_header(msg.get("Date", ""))

    html_parts: List[str] = []
    plain_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")

            if content_type == "text/html":
                html_parts.append(text)
            elif content_type == "text/plain":
                plain_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    combined_html = "\n".join(html_parts)
    combined_plain = "\n".join(plain_parts)

    sender_lower = sender.lower()
    subject_lower = subject.lower()

    if "craigslist" in sender_lower or "craigslist" in subject_lower or "craigslist.org" in combined_html:
        return parse_craigslist_alert_email(combined_html, combined_plain, date_str=date_str)
    else:
        return parse_generic_job_alert_email(
            combined_html,
            combined_plain,
            subject=subject,
            sender=sender,
            date_str=date_str,
        )


def fetch_imap_emails(settings: "Settings") -> List[JobPosting]:
    """Connect to an IMAP mailbox, search for unread job alert emails, and parse job postings."""
    if not settings.is_imap_configured:
        logger.debug("IMAP credentials not configured; skipping email ingestion.")
        return []

    logger.info(f"Connecting to IMAP server {settings.imap_server}:{settings.imap_port} (mailbox: {settings.imap_mailbox})...")
    postings: List[JobPosting] = []
    client = None

    try:
        client = imaplib.IMAP4_SSL(
            settings.imap_server,
            settings.imap_port,
        )
        client.login(settings.imap_username, settings.imap_password)
        select_status, _ = client.select(settings.imap_mailbox)
        if select_status != "OK":
            logger.error(f"Failed to select IMAP mailbox '{settings.imap_mailbox}'")
            return []

        search_criteria = settings.imap_search_criteria or "UNSEEN"
        status, message_numbers = client.search(None, search_criteria)
        if status != "OK" or not message_numbers or not message_numbers[0]:
            logger.info(f"No matching emails found under criteria '{search_criteria}'")
            return []

        msg_ids = message_numbers[0].split()
        logger.info(f"Found {len(msg_ids)} matching email(s) in {settings.imap_mailbox}")

        allowed = settings.allowed_senders_list
        if allowed:
            logger.info(f"Filtering emails by allowed senders/domains: {', '.join(allowed)}")

        for msg_id in msg_ids:
            try:
                res, data = client.fetch(msg_id, "(RFC822)")
                if res != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                    continue
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Sender whitelist filtering
                sender_val = decode_email_header(msg.get("From", "")).lower()
                if allowed and not any(a in sender_val for a in allowed):
                    logger.debug(f"Skipping email {msg_id.decode() if isinstance(msg_id, bytes) else msg_id} from '{sender_val}': sender not in allowed list.")
                    continue

                extracted = parse_email_message(msg)
                postings.extend(extracted)

                if settings.imap_mark_seen:
                    client.store(msg_id, "+FLAGS", "\\Seen")
            except Exception as e:
                logger.warning(f"Error parsing email ID {msg_id}: {e}")

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP protocol/authentication error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during IMAP email ingestion: {e}")
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass

    logger.info(f"Total postings ingested via IMAP email: {len(postings)}")
    return postings
