# Toran

Toran is a voice-first agent that takes a small business owner from idea to launch. She describes her business by voice (Marathi, Hindi or English) or sends a photo of her price list. Claude builds a confirmed business profile and publishes a mobile-first website. A real browser then tests the site the way her customers would.

> Status: foundation only (Block 2). Intake, site builder, customer tests and the agent loop come next.

## Who it's for

Women running businesses from home in India: home bakers, beauty parlours, tailors and tutors. It works for any small business.

## What it does

See `CLAUDE.md` for the full flow. Built so far:

- **Website builder** (`app/services/sitegen.py`): a confirmed `BusinessProfile` plus one of three themes (warm, elegant, simple) becomes a static, mobile-first site with separate English, Hindi and Marathi pages (`/`, `/hi/`, `/mr/`). Business text comes only from the profile. Prices are formatted in code, never translated. Anything that looks like a home address is left off and returned as a warning for the owner.
- **Business sites** are served at `https://<slug>.BASE_DOMAIN` (or `http://<slug>.localhost:8000` in development). They load no JavaScript and no external files, use self-hosted Mukta and Rozha One fonts, and stay under 500 KB per page.
- **Enquiry form** on every site posts to `/api/enquiry/<slug>` and is saved as an `Enquiry`. It is protected by an exact Origin check, a spam trap and a rate limit. Email delivery comes later.
- **Privacy page** at `/privacy` in all three languages.
- **Email** (`app/services/mailer.py`): our GoDaddy mailbox over SMTP (SSL on port 465). The home page contact form (CSRF, spam trap, 5 per minute and 20 per hour) and every business enquiry are saved first, then emailed to `CONTACT_TO_EMAIL` by a background job. A contact form message with an email address sets it as Reply-To. Errors send at most one alert email per error type per hour. Without SMTP settings, mail goes to an in-memory outbox, so tests never send real email.
- **Customer tests and Readiness Score** (`app/services/customer_tests.py`, `app/services/score.py`): real Chromium visits a site as a customer would: a phone at 360 px, a laptop at 1280 px, and a slow network (1.5 Mbps, 300 ms latency). Each check runs three times and the middle result counts. The score follows `docs/score.md`. Every item records whether it was measured or AI-assessed, and AI-assessed items show as pending until the AI checks exist, so the report reads "X of Y possible so far". Runs happen as background jobs (at most 2 Chromium instances, a 180 s timeout, one retry), and each run is stored with the profile version and a hash of the exact site files tested.
- **Sample businesses** use free Unsplash stock photos, always labelled "Sample photo" (credits in `docs/photo-credits.md`). Screenshots of each theme are in `docs/screenshots/`.

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

_The full judge flow comes with intake._ For now:

```bash
make migrate && make seed   # 5 sample businesses (marked Sample)
make dev
```

Then open `http://asha-home-bakery.localhost:8000/` (also `/hi/` and `/mr/`), the theme gallery at `http://localhost:8000/dev/themes`, or run the customer tests at `http://localhost:8000/dev/score` (development only; `make score slug=asha-home-bakery` does the same from the command line).

## Measured vs AI-assessed

Measured today (median of three Chromium runs): time to main content on a slow network, page weight, order button visible on a phone, tap targets of at least 44 px, no sideways scrolling, nothing overflowing on a laptop, phone number shown, delivery area shown with no address, HTTPS (live site only). AI-assessed by one Claude call per test run (claude-sonnet-5-5 with the phone screenshot): language clarity on each language page, real photos (not applicable for samples), ordering or refund policy, and customer questions answered from `docs/checklists.md`. The report shows which model answered and the estimated cost. If the AI check cannot run, those items stay pending with a friendly reason. Checks that do not apply (the phone number on sample sites, HTTPS locally) give their points to the other checks in the same part.

Every score item records whether it is `measured` (by the browser) or `ai_assessed` (by Claude). See `docs/score.md`.

## Cost per business

Each Claude call is logged with input, output and cache tokens and an **estimated** USD cost per business (table `claudeusage`). Prices per million tokens, from the hackathon deck: Haiku $0.10 in / $0.50 out, Sonnet $2 / $10, Opus $4 / $20. Reports come later.

## Known limitations

- Rate limits are kept in memory, which suits a single app process.
- Not built yet: intake, customer tests, agent loop, mail, GoDaddy integration, draft and live versions, the owner's one-click delete.
- Business text (product names, notes) appears exactly as the owner gave it on every language page; only interface labels are translated.
- Business sites are served by the app (Caddy compresses them); serving them straight from Caddy is a later step.
- Retention cleanup (enquiries 90 days, log files 30 days) runs inside the app once a day; Docker's own console buffer is size-capped (2 x 1 MB), not time-capped.
