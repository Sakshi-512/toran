# All Python commands use the project virtual environment.
VENV := .venv
PY := $(VENV)/bin/python
BIN := $(VENV)/bin

.PHONY: setup lock dev test lint format typecheck audit migrate migration up down backup

setup: ## Create the venv, install dependencies and git hooks
	test -d $(VENV) || python3.12 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install --require-hashes -r requirements-dev.txt
	$(BIN)/pre-commit install

lock: ## Re-pin every dependency (with hashes) after editing requirements*.in
	$(BIN)/pip-compile --generate-hashes --allow-unsafe --strip-extras --pip-args "--timeout 120 --retries 8" -o requirements.txt requirements.in
	$(BIN)/pip-compile --generate-hashes --allow-unsafe --strip-extras --pip-args "--timeout 120 --retries 8" -o requirements-dev.txt requirements-dev.in

dev: ## Run the app locally with reload (http, so cookies are not Secure)
	APP_ENV=development $(BIN)/alembic upgrade head
	APP_ENV=development $(BIN)/uvicorn --factory app.main:create_app --reload --port 8000

test: ## Run tests (mock mode, no Claude credits)
	CLAUDE_MOCK=1 $(BIN)/pytest

lint: ## Lint, format check and type check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

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

up: ## Build and start the production stack
	docker compose up -d --build

down:
	docker compose down

backup: ## Back up the SQLite database (keeps 7 days)
	$(PY) -m scripts.backup
