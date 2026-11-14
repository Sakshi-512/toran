# Voice Site Agent

A voice-first agent that takes a small business owner from idea to launch. She describes her business by voice (Marathi, Hindi or English) or sends a photo of her price list. Claude builds a confirmed business profile and publishes a mobile-first website. A real browser then tests the site the way her customers would.

> Status: foundation only (Block 2). Intake, site builder, customer tests and the agent loop come next.

## Who it's for

Women running businesses from home in India: home bakers, beauty parlours, tailors and tutors. It works for any small business.

## What it does

_To be completed as features land._ See `CLAUDE.md` for the full flow.

## Dependencies

Direct dependencies are listed in `requirements.in` and `requirements-dev.in`. `make lock` compiles them with pip-tools into `requirements.txt` and `requirements-dev.txt`, which pin every package (including indirect ones) with hashes. Installs use `--require-hashes`.

## Architecture

- FastAPI with thin routes in `app/routes/`; logic lives in `app/services/`.
- SQLModel on SQLite (WAL mode), with Alembic migrations in `migrations/`.
- All Claude calls go through `app/services/claude_client.py`, which handles mock mode, retries, prompt caching, cost logging and the daily spend cap (Asia/Kolkata days).
- Owner-facing text is in `app/i18n/{en,hi,mr}.json`.
- Security: signed sessions, CSRF tokens, rate limits by real client IP, security headers, upload checks that strip EXIF and GPS.
- Logs are JSON lines with request IDs. Secrets are redacted and phone numbers show only the last 4 digits.
- Deployment: Docker Compose (the app on the Playwright image, running as non-root) plus Caddy with on-demand TLS for business subdomains.

## Setup on a new machine

```bash
make setup                 # venv, hashed dependencies, pre-commit hooks
cp .env.example .env       # fill in values; APP_ENV=development locally
make migrate
make dev                   # http://localhost:8000
make lint && make test
```

## How judges can test it

_To be added with the intake flow._

## Measured vs AI-assessed

Every score item records whether it is `measured` (by the browser) or `ai_assessed` (by Claude). See `docs/score.md`.

## Cost per business

Each Claude call is logged with input, output and cache tokens and an **estimated** USD cost per business (table `claudeusage`). Prices per million tokens, from the hackathon deck: Haiku $0.10 in / $0.50 out, Sonnet $2 / $10, Opus $4 / $20. Reports come later.

## Known limitations

- Rate limits are kept in memory, which suits a single app process.
- Not built yet: intake, site builder, customer tests, agent loop, mail, GoDaddy integration, privacy page.
