.PHONY: init test run-dry stats clean lint

VENV = .venv
PYTHON = $(VENV)/bin/python
PIP = $(VENV)/bin/pip
PYTEST = $(VENV)/bin/pytest

init:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env from .env.example"; fi
	$(PYTHON) main.py init-db

test:
	$(PYTEST) -v tests/

run-dry:
	$(PYTHON) main.py scan --profile profiles/bookkeeper.json.example --feed tests/fixtures/sample_jobs.xml --dry-run

stats:
	$(PYTHON) main.py stats

clean:
	rm -rf __pycache__ tests/__pycache__ src/__pycache__ .pytest_cache
	rm -f matches.db
	rm -rf artifacts/matches/*.md artifacts/matches/*.txt
