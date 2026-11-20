# Toran

**Speak your business. Get your website.** A home baker, tailor or beauty parlour owner describes the business by voice in Marathi, Hindi or English, and Toran builds a mobile-first website, tests it like a customer would, and improves it before the owner says yes.

**Try it, no sign-up:** https://gettoran.com → tap one of the three sample voice notes.

**Headline:** a voice note becomes a tested draft website in minutes, and Toran's agent then improves the draft on its own: it keeps a change only if the Readiness Score rises, and rolls it back otherwise. Real-voice test: results in the project brief.

---

## What it is and who it's for

Toran is a voice-first agent that takes a small business owner from idea to launch. It is for small business owners in India who find websites and technology a barrier, starting with home-based businesses: home bakers, beauty parlours, tailors and tutors. If you can describe your business in Marathi, Hindi or English, Toran can put it online. Our first real users are women running businesses from home in Pune.

1. The owner describes the business by voice (or sends a photo of the price list, or types).
2. Claude turns it into a business profile. The owner checks each detail on simple cards; anything unclear becomes a question, never a guess.
3. Toran builds a fast, three-language website (English, Hindi, Marathi) on `<business-name>.gettoran.com`.
4. A real browser tests the site like a customer on a slow phone and gives a **Customer Readiness Score** (0 to 100).
5. An agent improves a draft copy, re-tests it, and shows the owner the score before and after. Nothing goes live until the owner approves.
6. Toran can suggest a web address with real GoDaddy prices; the team buys it only after the owner asks.
7. Later, the owner edits the menu and prices from the owner page ("Edit my menu and prices"). The changes go to a draft the owner can preview and go live only after the owner approves. "Restore the previous menu" undoes an edit, and big price changes need the PIN again. Phone, area and ordering policy are in "My details".

## What to try (about 5 minutes)

1. **Tap a sample voice note** on https://gettoran.com ("Try it in 3 languages"). No microphone or sign-up needed.
2. **Confirm the cards**: check the details Toran heard, then press "Confirm my details". Save the private link and PIN it shows once.
3. **Open the owner page** with that link and PIN, and look at the **draft score** and "See the full report" (each item says how to fix it).
4. **Press "Let Toran improve my draft"**, wait for it to finish, then open **"See how Toran thinks"**: before and after first, then every round, tool call and score change, with "Details for judges".
5. **Tap "Edit my menu and prices"**, change a price, and save: the draft is rebuilt and tested again, and the live site changes only when you publish. Try "Restore the previous menu" too.
6. **Open a live demo site in another language**, for example https://asha-home-bakery.gettoran.com/mr/ or https://savitri-gruh-udyog.gettoran.com/hi/.

## How it maps to the judging criteria

| Criterion | Evidence | Where |
| --- | --- | --- |
| **Impact (35%)** | Built for small business owners in India who find technology a barrier, starting with home-based businesses (first real users: women running businesses from home in Pune); voice in Marathi, Hindi and English; sites readable in all three; real vs sample businesses tracked; accuracy measured on real voice notes; cost per business measured | `app/services/accuracy.py`, `scripts/accuracy_report.py`, `app/services/showcase.py` ("Measured on the live system"), real-world results below |
| **Demo (35%)** | One-tap samples, no sign-up; the score improves visibly; "How Toran thinks" panel; friendly messages instead of errors (spend cap, Claude down, stuck jobs); works at 360 px to 1280 px | `app/templates/app/home.html`, `app/templates/app/owner_agent.html`, `tests/test_layout.py` |
| **Tech Usage (15%)** | Claude Sonnet for profile extraction (with vision), AI-assessed checks and an agent loop with six guarded tools; Claude Haiku for web address ideas; Sarvam speech-to-text; prompt caching, cost logging and daily caps. GoDaddy: the Docker VPS, the domain with wildcard DNS (set once at GoDaddy), the mailbox, and the gddy CLI inside the product for live domain availability and prices | "How Toran uses Claude and GoDaddy" below |
| **Execution (15%)** | Layered FastAPI app (thin routes, services, swappable providers), Alembic migrations, over 800 automated tests including real-Chromium layout and end-to-end checks, ruff, mypy, gitleaks, pip-audit, CI on every push, an independent code review with fixes | `docs/review.md`, `.github/workflows/ci.yml`, `tests/` |

### How Toran uses Claude and GoDaddy

Every Claude call goes through `ClaudeClient` (`app/services/claude_client.py`): `max_tokens`, a timeout, retries with backoff, prompt caching of the system prompt (which also covers the tools sent before it), token and cost logging per business, and a daily spend cap (`check_spend_cap`, with 30% reserved for existing businesses, `RESERVED_SHARE`). Mock mode (`CLAUDE_MOCK=1`) answers from `tests/fixtures/claude/`. Text from voice notes, photos, websites and forms is passed to Claude as data inside tagged blocks (angle brackets neutralised), never as instructions.

| Use | What it does | Model or service | Main files | Covered by |
| --- | --- | --- | --- | --- |
| Speech-to-text | Turns a Marathi, Hindi or English voice note into text: the REST endpoint for short notes, the batch API for longer ones; audio is never logged | Sarvam Saaras v4 (`saaras:v4`, `sarvamai` SDK); providers `none`, `mock`, `sarvam` | `app/services/stt.py` | `tests/test_stt.py` |
| Profile extraction | A transcript, typed text or a price-list photo (vision) becomes a business profile as Pydantic-validated JSON; owner text is data, nothing is invented | Claude Sonnet | `app/services/extract.py`, `app/schemas.py` | `tests/test_extract.py`, `tests/test_schemas.py`; `make eval` (`scripts/run_evals.py`, `evals/cases/`, latest totals in `evals/latest.json`); `make accuracy` (`scripts/accuracy_report.py`) |
| Eval name judge | Decides whether two product names mean the same item when scoring evals | Claude Haiku | `app/services/evals.py` | `tests/test_evals_owner.py` |
| Readiness checks: measured | A real browser opens the site like a customer: speed, page weight, order button above the fold, tap targets, sideways scroll, laptop overflow, phone shown, area shown, HTTPS (9 items, 52 points); at most 2 browsers at once | Playwright with Chromium | `app/services/customer_tests.py`, `app/services/score.py`, `docs/score.md` | `tests/test_customer_tests.py`, `tests/test_score.py`; `make score` |
| Readiness checks: AI-assessed | Language clarity, real photos, ordering policy, customer questions answered, judged from the page text in three languages and a phone screenshot; each item records `ai_assessed` (4 items, 48 points) | Claude Sonnet (vision) | `app/services/ai_checks.py`, `app/services/checklists.py`, `docs/checklists.md` | `tests/test_ai_checks.py`, `tests/test_score.py` |
| Agent fix loop | Improves the draft with tool use: reads the score items, calls tools, re-tests | Claude Sonnet | `app/services/agent.py` (`AgentLoop`), `app/services/agent_report.py` | `tests/test_agent.py`; `make agent` |
| Agent tools and guards | Exactly six tools (`run_tests`, `compress_images`, `add_translation`, `edit_layout`, `ask_owner`, `rebuild_draft`). Strict inputs (a `price` or `phone` field is rejected); translations keep the same numbers, with no prices, phone numbers or links; layout choices come from a fixed list. At most 5 rounds and 3 calls per round; a round that does not raise the score is rolled back; one run at a time and 3 runs per business per day. It cannot change facts, publish or spend money | Code checks around Claude | `app/services/agent_tools.py`, `app/services/agent.py` | `tests/test_agent.py` |
| Domain name ideas | Suggests 6 short names from the confirmed profile | Claude Haiku | `suggest_names` in `app/services/godaddy.py` | `tests/test_domains_and_delete.py` (saved answers in `tests/fixtures/gddy/`) |
| Domain availability and prices | Each name with `.in` and `.com`, checked with `gddy domain available <domain> --check-type full --json` and shown as tool calls in the owner's view. Nothing is bought from the app: "I want this one" records the request and emails the team, who buy it on godaddy.com | GoDaddy gddy CLI (pinned, SHA-256 checked, authenticates with `GDDY_PAT`) | `run_lookup`, `request_domain` in `app/services/godaddy.py`, `scripts/install_gddy.py`, `Dockerfile` | `tests/test_domains_and_delete.py` |
| VPS, Docker and Caddy | The app (non-root container) and Caddy for HTTPS, with restart policies, health checks on both and tagged images for rollback | GoDaddy Docker VPS, Docker Compose, Caddy | `docker-compose.yml`, `Dockerfile`, `Caddyfile`, `docs/deploy.md`, `/health` in `app/routes/health.py` | `tests/test_health_and_home.py`, `tests/test_docker_files.py`; the server checklist in `docs/deploy.md` |
| Wildcard DNS and per-site HTTPS | `A` records for `@`, `www` and `*` were set once at GoDaddy and point at the VPS. Each business subdomain gets its own certificate on demand, only after `/internal/tls-check` confirms it belongs to a real business. The app never changes DNS | GoDaddy DNS, Caddy on-demand TLS | `Caddyfile`, `tls_check` in `app/routes/health.py`, `docs/deploy.md` | `tests/test_health_and_home.py`, `tests/test_publish.py` |
| Mailbox and email | Contact form to our mailbox, enquiry notifications, domain requests to the team, and error alerts (at most one per error type per hour), over SMTP as background jobs with retry | GoDaddy mailbox (SMTP) | `app/services/mailer.py`, `app/services/mail_jobs.py`, `app/services/contact.py`, `app/services/alerts.py` | `tests/test_mail.py`, `tests/test_enquiry.py` |
| Spend caps | Claude: a daily dollar cap (Asia/Kolkata days, 30% kept for existing businesses). Speech-to-text: a daily minutes cap, after which owners are asked to type. Agent: 3 runs per business per day. Tokens and estimated cost are logged per business | | `check_spend_cap` in `app/services/claude_client.py`, `app/services/stt.py`, `app/services/agent.py` | `tests/test_claude_client.py`, `tests/test_spend_cap_samples.py`, `tests/test_review_fixes.py` (speech-to-text cap), `tests/test_agent.py` |

## Architecture

```mermaid
flowchart LR
  phone["Owner's phone<br/>(and their customers)"] -->|HTTPS| caddy["Caddy<br/>HTTPS, on-demand TLS<br/>for business subdomains"]
  caddy --> app["FastAPI app<br/>owner pages, intake,<br/>business sites"]
  app --> db[("SQLite (WAL)<br/>profiles, versions,<br/>scores, action log")]
  app --> files[("Site files<br/>draft / live / versions")]
  app --> jobs["Background job workers<br/>intake · scoring · agent ·<br/>web addresses · email"]
  jobs --> stt["Sarvam<br/>speech-to-text"]
  jobs --> claude["Claude API"]
  claude --> extract["Profile extraction<br/>(text and photo)"]
  claude --> checks["AI-assessed checks<br/>(with screenshot)"]
  claude --> agent["Agent loop: run_tests, compress_images,<br/>add_translation, edit_layout,<br/>ask_owner, rebuild_draft"]
  jobs --> pw["Playwright + Chromium<br/>customer tests"]
  pw -->|tests the draft or live site| app
  jobs --> gddy["gddy CLI"]
  gddy --> godaddy["GoDaddy<br/>domain availability and prices"]
  app -->|SMTP| mail["GoDaddy mailbox<br/>contact form, enquiries,<br/>team notes"]
  subgraph GoDaddy services
    godaddy
    mail
    vps["Docker VPS<br/>(runs Caddy and the app)"]
    dns["Domain and DNS<br/>gettoran.com, *.gettoran.com"]
  end
```

- Routes stay thin (`app/routes/`); logic lives in `app/services/`; providers (Claude, speech-to-text, GoDaddy CLI, SMTP) sit behind interfaces with mock versions.
- Browser tests and the agent run as background jobs (`app/services/jobs.py`); pages refresh themselves while they wait.
- Business sites are static files built from the confirmed profile (`app/services/sitegen.py`) and served on their own subdomain.

## Safety and privacy

- **Nothing goes live without the owner's approval.** The agent works on a draft only; publishing is a separate confirmation screen. A real business can't go live without a phone number unless the owner explicitly chooses no order button.
- **Facts only from the owner's confirmed details.** Prices and numbers are inserted from the profile, never written or translated by Claude; agent tools that try to change a fact are rejected and logged.
- **No home address.** Anything that looks like an address is left off the site and turned into a question.
- **Owner access:** an unguessable private link plus a hashed 4-digit PIN, lockout after 5 wrong tries, the PIN again to change the phone number or delete the site, remembered devices revocable, and a team-only reissue after checking the owner's identity (`make server-access`).
- **Retention:** voice notes and price-list photos deleted right after processing (30 days if the owner chose to keep them); unconfirmed starts after 7 days; sample businesses after 7 days; enquiries after 90 days; logs after 30 days; backups kept 7 days. See `/privacy` in all three languages.
- **One-action deletion** of the owner's site and all data, with a permanent-deletion warning and the PIN.
- **Security patterns:** CSRF on every form (Origin check for the cross-site enquiry form), rate limits, upload checks (re-encoded images, location data stripped), security headers, non-root container, secrets only in `.env` (never in the repo or its history; gitleaks in pre-commit).

## Measured vs AI-assessed

Each score item records whether it is `measured` or `ai_assessed` (`app/services/score.py`, `docs/score.md`).

- **Measured** by real Chromium (median of three runs): time to main content on slow internet, page weight, order button visible on a phone, tap targets at least 44 px, no sideways scrolling, nothing overflowing on a laptop, phone number shown, delivery area shown with no address, HTTPS (live site).
- **AI-assessed** by one Claude call per test run: language clarity on each language page, real photos, ordering policy, customer questions answered (`docs/checklists.md`). Points come from fixed verdicts (yes, partly, no), so repeated runs stay close.

## Cost per business (measured)

- **One full check** (customer tests plus the AI checks): about **$0.04** in Claude cost, the median of real checks; see the live figures on the home page ("Measured on the live system"), which the app computes from real runs.
- **One agent run**: see the agent report on an owner page ("See how Toran thinks"), which shows the tokens and cost of every round.
- Every call's tokens and estimated cost are stored per business (`claudeusage` table); speech-to-text minutes and estimated cost in `sttusage`. The home page shows measured medians with their sample size and date.

## Real-world results

<!-- TODO: fill in the real-world numbers below. -->

| Measure | Result |
| --- | --- |
| Real businesses onboarded | 1 (a beauty parlour in Pune, built from one Marathi voice note, with the owner’s written consent) |
| Field accuracy on real voice notes | 90% of fields needed no edit (1 note, 1 owner). The edited fields include the business name, a misheard offer (“free” for “pre”), and a phone number that was not in the note. |
| Price accuracy on real voice notes | 0 prices or products invented, 0 products missed. The owner gave only price ranges, so the app kept them as ranges and asked, rather than guess. (Report shows 0% exact: no exact price existed in the note.) |
| Median time from voice note to a tested draft | 3.6 min for the one real business, including the owner’s own checking and editing time (4.7 min to live). The home page figure is for sample runs, which skip the owner’s review. |
| Pune audit (small, non-random sample) | 10 businesses: 5 had no website, 3 only a free builder page, 2 their own domain. 9 of 10 listed a phone number. |

`make accuracy` prints the accuracy and timing report from real (non-sample) businesses.

## Setup on a new machine

```bash
make setup                 # virtualenv, pinned and hashed dependencies, pre-commit hooks
cp .env.example .env       # fill in values; APP_ENV=development locally
make migrate
make seed                  # six demo businesses, marked Sample
make dev                   # http://localhost:8000
```

Everything works in mock mode (`CLAUDE_MOCK=1`, `GDDY_MOCK=1`, `STT_PROVIDER=mock`) without spending credits.

## Tests and checks

```bash
make lint      # ruff, formatting, mypy, gitleaks, private-files hook, whitespace (pre-commit)
make test      # over 800 automated tests, mock mode, including real-Chromium end-to-end and layout checks
make eval      # extraction evals (evals/), checked in code
```

- **Layout check** (`tests/test_layout.py`): every owner page in every state, the home page, `/start` and live business sites, at 360, 768, 1024 and 1280 px; fails if any element sticks out of its card or is wider than the screen. `LAYOUT_SHOTS=<folder>` saves screenshots.
- **CI** (`.github/workflows/ci.yml`) runs the hooks, the tests and `pip-audit` on every push.
- **Translations:** `make i18n-sheet` writes `docs/translation-review.md` for native speakers; `make i18n-apply file=<filled copy>` applies their fixes and refuses any that change a placeholder or number.

## Production deployment

Docker Compose on the GoDaddy VPS: the app (non-root, `init`, 1 GB shared memory for Chromium) and Caddy (HTTPS, on-demand certificates for business subdomains, allowed only for real businesses). Migrations run on start, the app makes a daily backup, and tagged images allow a one-command rollback. The step-by-step server checklist, the spend-cap behaviour and how to give an owner a new link and PIN are in [docs/deploy.md](docs/deploy.md).

## Known limitations

- **Prices and menu can be edited only by typing, not by voice.** Owners edit them on the owner page; updating them by voice is a next step.
- **Owners are not notified by WhatsApp or email when a customer's form message arrives.** It appears in the enquiries inbox on the owner page and is emailed to the Toran team.
- **One agent run per draft at a time.**
- **The Pune audit is a small, non-random sample**, so its numbers show direction, not a population estimate.
- **Speech-to-text can mishear**, especially names and numbers, which is why the owner confirms every detail before anything is built.
- **AI-assessed score items can vary by a point or two** between runs of the same site.
- **Hindi and Marathi wording was written by AI** and is being reviewed by native speakers (`docs/translation-review.md`).
- Rate limits are kept in memory (one app process), and the rupee figure for domain prices uses a fixed rate.

## Next steps

- Price and menu updates by voice, using the same draft, restore and PIN rules as the menu editor.
- Notify owners on WhatsApp when a customer sends an enquiry.
- Finish the native-speaker review of Hindi and Marathi, then add more Indian languages.
- Automate buying and DNS setup for a requested web address, still only after the owner's approval.
- A larger, random sample of real businesses for the accuracy audit.

## Repository

- `app/`: FastAPI app (`routes/`, `services/`, `templates/`, `i18n/`, `static/`)
- `migrations/`: Alembic migrations · `scripts/`: operations scripts · `tests/`: test suite
- `docs/`: score rules, checklists, deployment, review, translation review
- `evals/cases/`: fake sample cases only; real cases live in git-ignored `evals/private/`
