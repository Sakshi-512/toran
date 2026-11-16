# All Python commands use the project virtual environment.
VENV := .venv
PY := $(VENV)/bin/python
BIN := $(VENV)/bin

.PHONY: setup lock dev test lint format typecheck audit migrate migration seed purge logs score agent shots eval accuracy up down backup

setup: ## Create the venv, install dependencies and git hooks
	test -d $(VENV) || python3.12 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install --require-hashes -r requirements-dev.txt
	$(BIN)/playwright install chromium
	$(BIN)/pre-commit install

lock: ## Re-pin every dependency (with hashes) after editing requirements*.in
	$(BIN)/pip-compile --generate-hashes --allow-unsafe --strip-extras --pip-args "--timeout 120 --retries 8" -o requirements.txt requirements.in
	$(BIN)/pip-compile --generate-hashes --allow-unsafe --strip-extras --pip-args "--timeout 120 --retries 8" -o requirements-dev.txt requirements-dev.in

dev: ## Run the app locally with reload (http, so cookies are not Secure)
	APP_ENV=development $(BIN)/alembic upgrade head
	APP_ENV=development $(BIN)/uvicorn --factory app.main:create_app --reload --port 8000

test: ## Run tests (mock mode, no Claude credits)
	CLAUDE_MOCK=1 $(BIN)/pytest

# Runs exactly the commit hooks on every tracked and new file, so a passing lint means the commit
# hooks pass. Hooks that fix files (format, whitespace) fail the run; run make lint again after.
lint: ## Run the commit hooks on all files
	git ls-files -z --cached --others --exclude-standard | xargs -0 $(BIN)/pre-commit run --files

format: ## Auto-fix lint issues and format
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy

audit: ## Check dependencies for known vulnerabilities
	$(BIN)/pip-audit --require-hashes --disable-pip -r requirements.txt

migrate: ## Apply database migrations
	$(BIN)/alembic upgrade head

migration: ## Create a migration: make migration msg="add x"
	$(BIN)/alembic revision --autogenerate -m "$(msg)"

seed: ## Create or update the sample businesses and build their sites
	APP_ENV=development $(PY) -m scripts.seed_samples

purge: ## Delete enquiries older than 90 days and log files older than 30 days
	$(PY) -m scripts.purge

eval: ## Rerun extraction on reviewed eval cases (evals/cases and evals/private) and report accuracy
	$(BIN)/alembic upgrade head
	$(PY) -m scripts.run_evals

accuracy: ## Accuracy from real use: first guesses vs confirmed profiles
	$(PY) -m scripts.accuracy_report

score: ## Run customer tests and score a site: make score slug=asha-home-bakery
	APP_ENV=development $(PY) -m scripts.score_site $(slug)

agent: ## Run the agent fix loop on a draft: make agent slug=<slug> [url=<draft url>]
	APP_ENV=development $(PY) -m scripts.agent_run $(slug) $(url)

shots: ## Remake the home page's phone screenshot from a real sample site
	$(PY) -m scripts.home_shots

logs: ## Follow the production app log file
	docker compose exec app tail -f /app/data/logs/app.log

up: ## Build and start the production stack
	docker compose up -d --build

down:
	docker compose down

backup: ## Back up the SQLite database (keeps 7 days)
	$(PY) -m scripts.backup
