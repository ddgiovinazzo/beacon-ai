# BeaconAI 🚨

**BeaconAI** is a lightweight, modular, and deterministic CLI job intelligence engine in Python.

It ingests unstructured RSS job feeds (e.g., Craigslist, regional municipal/school boards), deduplicates postings against a local SQLite database, filters them against declarative JSON user constraints (`profile.json`) using zero-cost deterministic logic first (**Tier 1 Cost Shield**), evaluates cleared candidates with Gemini using strict Pydantic structured schemas (**Tier 2**), and generates tailored Markdown resume/application artifacts alongside a local daily digest.

---

## Key Features

1. **Two-Tier Multi-Stage Evaluation**:
   - **Tier 1 (Deterministic Cost Shield, $0 LLM tokens)**: Instant regex and keyword checks for physical labor violations (e.g., lifting > max lbs, ladders, warehouse labor), pay floor enforcement (hourly and annual minimums), and schedule conflicts (e.g., overnight/graveyard shifts). Disqualified jobs are instantly committed to SQLite without spending any LLM tokens.
   - **Tier 2 (Structured LLM Scorer)**: Only cleared postings are evaluated with Gemini 2.5 Flash, enforcing strict Pydantic JSON contracts. Includes a configurable per-run circuit breaker (`MAX_LLM_EVALS_PER_RUN = 20`) to prevent bill shock.
2. **Instant SQLite Deduplication**: Fast indexed lookup on posting URL / ID skips already-seen jobs immediately.
3. **Prompt Injection Hardening**: Strips script tags, style attributes, hidden CSS spans, and zero-width unicode characters, encapsulating untrusted text in strict `<untrusted_job_posting>` XML boundaries.
4. **Artifact Generation (Human-in-the-Loop)**:
   - Pixel-consistent Markdown resumes rendered via Jinja2 (`artifacts/matches/<slug>_resume.md`).
   - Plain-text outreach cover messages (`artifacts/matches/<slug>_outreach.txt`) with pre-filled `mailto:` links for one-click manual sending (no unauthorized emailing).
   - Local daily digest markdown summary (`artifacts/daily_digest_YYYY-MM-DD.md`).
5. **Polite Feed Ingestion**: Custom User-Agent header, timeout handling, and graceful error recovery.

---

## Directory Structure

```
beacon-ai/
├── profiles/
│   ├── bookkeeper.json.example       # Example declarative user configuration
│   └── data_clerk.json.example       # Secondary test profile
├── templates/
│   └── resume_template.md.j2         # Standardized Jinja2 markdown template
├── artifacts/
│   └── matches/                      # Generated tailored resumes and outreach drafts
├── src/
│   ├── __init__.py
│   ├── config.py                     # Pydantic BaseSettings, API keys, paths, rate caps
│   ├── db.py                         # SQLite persistence, schema, and deduplication logic
│   ├── ingestion.py                  # Feedparser XML fetching + BeautifulSoup sanitization
│   ├── evaluator.py                  # Deterministic regex/keyword filters + LLM Pydantic scorer
│   ├── generator.py                  # Jinja2 template rendering for resumes and outreach
│   └── schemas.py                    # Strict Pydantic models for inputs, filters, and outputs
├── tests/
│   ├── __init__.py
│   ├── fixtures/
│   │   └── sample_jobs.xml           # Test fixture RSS feed
│   ├── test_evaluator.py             # Unit tests for deterministic rejection logic
│   └── test_ingestion.py             # Unit tests for feed parsing and sanitization
├── .env.example
├── .gitignore                        # Ignores .env, *.db, matches/, .venv, *.log
├── LICENSE                           # MIT License
├── Makefile                          # One-command init, test, and run targets
├── pyproject.toml / requirements.txt
└── main.py                           # Typer / Rich CLI runner
```

---

## Quick Start

### 1. Installation

```bash
# Clone and enter directory
cd beacon-ai

# Initialize virtualenv, install dependencies, and setup DB
make init
```

Or manually:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py init-db
```

### 2. Configuration (`.env`)

Copy `.env.example` to `.env` and configure your settings:
```bash
cp .env.example .env
```

```ini
# Gemini API Key (Required for live Tier 2 evaluation and resume tailoring)
GEMINI_API_KEY=your_gemini_api_key_here

# Model selection
GEMINI_MODEL=gemini-2.5-flash

# Database path
DB_PATH=matches.db

# Circuit Breaker: Max LLM evaluations per single run
MAX_LLM_EVALS_PER_RUN=20

# Polite crawler identity
USER_AGENT=BeaconAI/1.0 (+https://github.com/beacon-ai; polite-job-crawler)
REQUEST_TIMEOUT_SECONDS=15
```

---

## CLI Usage

### 1. Scan a Job Feed (Dry-Run / Local Fixture)
Test without making external LLM calls or spending API tokens:
```bash
python main.py scan --profile profiles/bookkeeper.json.example --feed tests/fixtures/sample_jobs.xml --dry-run
```

### 2. Live Scan with Gemini 2.5 Flash
```bash
python main.py scan --profile profiles/bookkeeper.json.example --feed "https://sfbay.craigslist.org/search/acc?format=rss"
```

### 3. Quick Rule Testing (`test-eval`)
Instantly test Tier 1 deterministic rules against arbitrary text:
```bash
python main.py test-eval --text "Requires lifting 65 lbs and standing all day" --profile profiles/bookkeeper.json.example
```

### 4. Database Persistence & Metrics (`stats`)
Display processed jobs, rejection reasons breakdown, and match history:
```bash
python main.py stats
```

---

## Running Tests

Run the full automated test suite (deterministic discard filters, regex extractors, sanitization, and RSS parsing):
```bash
make test
# or
pytest -v
```

---

## License
MIT License. See [LICENSE](file:///Users/daniel/code/beacon-ai/LICENSE) for details.
