# Toran

Toran is a voice-first agent that takes a small business owner from idea to launch. She describes her business by voice (Marathi, Hindi or English) or sends a photo of her price list. Claude builds a confirmed business profile and publishes a mobile-first website. A real browser then tests the site the way her customers would.

> Status: intake, site builder, customer tests, Readiness Score, publishing with undo and the agent fix loop work. GoDaddy domain suggestions come next.

## Who it's for

Women running businesses from home in India: home bakers, beauty parlours, tailors and tutors. It works for any small business.

## What it does

See `CLAUDE.md` for the full flow. Built so far:

- **Website builder** (`app/services/sitegen.py`): a confirmed `BusinessProfile` plus one of three themes (warm, elegant, simple) becomes a static, mobile-first site with separate English, Hindi and Marathi pages (`/`, `/hi/`, `/mr/`). Business text comes only from the profile. Prices are formatted in code, never translated. Anything that looks like a home address is left off and returned as a warning for the owner.
- **Business sites** are served at `https://<slug>.BASE_DOMAIN` (or `http://<slug>.localhost:8000` in development). They load no JavaScript and no external files, use self-hosted Mukta and Rozha One fonts, and stay under 500 KB per page.
- **Enquiry form** on every site posts to `/api/enquiry/<slug>` and is saved as an `Enquiry`. It is protected by an exact Origin check, a spam trap and a rate limit. Email delivery comes later.
- **Privacy page** at `/privacy` in all three languages.
- **Intake** (`/start`, `app/services/intake.py`, `app/services/extract.py`): she records a voice note (up to 2 minutes), uploads one, sends a photo of her price list, or types. Speech-to-text and extraction run as a background job. Claude Sonnet turns the transcript or photo into a draft profile; code then removes any price or phone number she never said (Devanagari digits and Indian comma formats are normalised first), clears anything that looks like a home address, and turns both into questions. Photo prices are kept but marked "please check" on the cards. She edits one card per detail, picks a readable web address (Devanagari names are transliterated, for example `sunita-beauty-parlour`), adds photos if she likes, and confirms. That creates the business (draft), profile version 1 and her consent record, and shows her private link and 4-digit PIN once (both stored only as hashes; 5 wrong PINs lock the link for 15 minutes).
- **Draft, score, publish** (`app/services/publish.py`, owner page): confirming builds a draft site (warm style; she can compare all three styles with her own details), which only she can preview. The draft is tested and scored in the background; her page shows her place in the queue, then the Readiness Score report in her language. "Make my site live" leads to a confirmation screen; only her yes publishes. Every publish is saved as a version: "Undo last publish" restores the previous one (undoing the first returns to draft), and she can take the site offline. The live site is tested too, and her page keeps the score history. The time from voice note to draft scored (system time) and to live site (including her decisions) is stored per intake and reported by `make accuracy`. Businesses made from the sample voice notes are marked Sample and deleted with all their data after 7 days.
- **Accuracy**: each intake stores Claude's first guess, the transcript and every correction. `make accuracy` reports field and price accuracy and the correction rate across real businesses (samples excluded). `make eval` reruns extraction on reviewed cases in `evals/` and checks prices and numbers exactly in code; see `evals/README.md`.
- **Speech-to-text** (`app/services/stt.py`): Sarvam AI through its official `sarvamai` SDK, using Saaras v4 in codemix mode (native script, English words kept in English, numbers as digits), with optional key terms such as product names. Short notes use the REST endpoint; notes it turns away as too long (voice notes are usually 1 to 2 minutes) go to Sarvam's batch API. Audio is never logged, and is only written to a temporary folder for the batch upload, then deleted. Duration and an **estimated** cost (₹0.49 per minute) are stored per business. `STT_PROVIDER=mock` uses saved transcripts; `none` turns voice off and owners type. Try it at `/dev/stt` (development only).
- **Email** (`app/services/mailer.py`): our GoDaddy mailbox over SMTP (SSL on port 465). The home page contact form (CSRF, spam trap, 5 per minute and 20 per hour) and every business enquiry are saved first, then emailed to `CONTACT_TO_EMAIL` by a background job. A contact form message with an email address sets it as Reply-To. Errors send at most one alert email per error type per hour. Without SMTP settings, mail goes to an in-memory outbox, so tests never send real email.
- **Customer tests and Readiness Score** (`app/services/customer_tests.py`, `app/services/score.py`): real Chromium visits a site as a customer would: a phone at 360 px, a laptop at 1280 px, and a slow network (1.5 Mbps, 300 ms latency). Each check runs three times and the middle result counts. The score follows `docs/score.md`. Every item records whether it was measured or AI-assessed, and AI-assessed items show as pending until the AI checks exist, so the report reads "X of Y possible so far". Runs happen as background jobs (at most 2 Chromium instances, a 180 s timeout, one retry), and each run is stored with the profile version and a hash of the exact site files tested.
- **Agent fix loop and "how it thinks"** (`app/services/agent.py`, `agent_tools.py`, `site_extras.py`; details in `docs/agent.md`): her "Let Toran improve my draft" button starts a background job. Each round, Claude Sonnet reads the latest score items and calls tools: `add_translation` (her confirmed product names, notes and size labels into English, Hindi or Marathi; numbers must match exactly, no prices, currency, phone numbers or links), `edit_layout` (main button first, photo after text, larger buttons, section order), `compress_images`, `ask_owner` (policy, notice needed, delivery areas, photos), `rebuild_draft` and `run_tests`. Every call is checked in code, and anything that would change a fact is rejected. Translations and layout are stored apart from her profile. The draft is rebuilt and re-tested after each round. The loop stops at 90, when a round brings no improvement (that round is rolled back), when only she can help, or after 5 rounds. Her page shows the score before and after and the main fixes, with "Approve these changes" leading to the publish confirmation and "Undo these changes". The panel (`/owner/agent/<id>`) shows each round in her language, with a detailed view for judges (Claude's reason, exact tool input, rejected calls, tokens, cost). Her answers to its questions are shown back to her and saved to her profile only after she confirms, then the draft is re-tested.
- **Home page and /start** (`app/services/showcase.py`): the home page explains the product in one line, shows a real sample site in a phone frame (made by `make shots` from the bakery sample, with its Sample banner), and offers one-tap sample voice notes in three languages. Its numbers (AI cost of one full check, voice note to tested draft, accuracy on real voice notes with the sample size, from 2 real businesses) come only from real, non-mock data and are hidden until measured. "Before and after" shows the latest real agent run on a demo business, linking to a read-only public panel at `/demo/agent/<id>` (demo businesses only), and stays hidden until such a run exists. The gallery shows real businesses only when they are live and the owner said yes on her page (off by default); demo businesses are listed separately and labelled. `/start` puts the samples first, then four cards (record, upload a voice note, photo of a price list, type) where only the chosen one opens, with no new JavaScript.
- **Web address suggestions** (`app/services/godaddy.py`): on her page, Claude Haiku suggests 6 short Latin-letter names from her profile, and each is checked for .in and .com with `gddy domain available <domain> --check-type full --json` (GDDY_PAT on the server, the existing login locally). Only available names are shown, with the first-year price in USD, about ₹ at a fixed rate, labelled "price before tax". Lookups run in the background, are cached for an hour per business, and every Claude and GoDaddy call is in the action log and her "How Toran found these" view. Nothing is bought in the app: "I want this one" records a request and emails the team, who buy it on godaddy.com and point DNS at her site. If the CLI is missing or fails, the section shows a friendly note.
- **Delete my site and data**: a confirmation screen with a permanent-deletion warning first, the full list of what goes, her PIN again, and a final button. It uses `delete_business_data`, takes the site offline at once, signs her out, shows a goodbye page and emails the team a note with no personal details.
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

Start an intake at `http://localhost:8000/start` (the sample voice notes in English, Hindi and Marathi work without a microphone or credits), or open `http://asha-home-bakery.localhost:8000/` (also `/hi/` and `/mr/`), the theme gallery at `http://localhost:8000/dev/themes`, or run the customer tests at `http://localhost:8000/dev/score` (development only; `make score slug=asha-home-bakery` does the same from the command line).

To see the agent fix loop: finish an intake (or sign in to a business), then press "Let Toran improve my draft" on the owner page and open "See how Toran thinks". From the command line, `make agent slug=<slug>` runs one loop and prints each round. In mock mode (`CLAUDE_MOCK=1`) the tool calls are saved fixtures; the `agent-demo-foods` fixtures take the home-foods sample from about 72 to about 87, then ask for her ordering policy.

## Measured vs AI-assessed

Measured today (median of three Chromium runs): time to main content on a slow network, page weight, order button visible on a phone, tap targets of at least 44 px, no sideways scrolling, nothing overflowing on a laptop, phone number shown, delivery area shown with no address, HTTPS (live site only). AI-assessed by one Claude call per test run (claude-sonnet-5-5 with the phone screenshot): language clarity on each language page, real photos (not applicable for samples), ordering or refund policy, and customer questions answered from `docs/checklists.md`. The report shows which model answered and the estimated cost. If the AI check cannot run, those items stay pending with a friendly reason. Checks that do not apply (the phone number on sample sites, HTTPS locally) give their points to the other checks in the same part.

Every score item records whether it is `measured` (by the browser) or `ai_assessed` (by Claude). See `docs/score.md`.

## Cost per business

Each Claude call is logged with input, output and cache tokens and an **estimated** USD cost per business (table `claudeusage`). Prices per million tokens, from the hackathon deck: Haiku $0.10 in / $0.50 out, Sonnet $2 / $10, Opus $4 / $20. Reports come later.

Each agent fix loop stores its total estimated cost (Claude decisions plus the AI checks of every re-test) on the run, logs it as `agent_run_done`, and shows it in the panel's detailed view. Its system prompt and tools are cached. One business can start at most 3 loops a day, and the daily spend cap stops a loop early while keeping the best draft so far.

Each speech-to-text call is logged with its audio duration and an **estimated** cost of ₹0.49 per minute (Sarvam's published pay-as-you-go rate), per business (table `sttusage`).

## Privacy of real data

The repository is public. Real voice notes, transcripts, photos and answer keys live only in git-ignored folders (`data/`, `voice_notes/`, `evals/private/`, `evals/reports/`), and a pre-commit hook refuses audio files, images outside the asset and sample folders, and anything under those folders. Voice notes and price-list photos are deleted after processing unless the owner agreed to keep them (leftovers are removed after a day). Logs never contain audio, transcripts or extracted details.

## Known limitations

- Rate limits are kept in memory, which suits a single app process.
- Buying a web address and pointing its DNS is done by the team on godaddy.com after her request; the app never buys anything. The rupee figure uses a fixed rate and is only a rough guide.
- Business text appears exactly as the owner gave it, unless the agent added a translation for a page's language; then her original is shown under it. Translation checks catch changed numbers, prices, currency, phone numbers, links, the wrong script and addresses, but not a reworded claim (for example a translation that adds "eggless"). She sees every translation in the change list before approving.
- In mock mode the AI checks after an agent round come from saved variants (`ai_checks_<slug>+translated+policy`), so mock scores follow the agent's changes only for fixtures that have them.
- Business sites are served by the app (Caddy compresses them); serving them straight from Caddy is a later step.
- Retention cleanup (enquiries 90 days, log files 30 days) runs inside the app once a day; Docker's own console buffer is size-capped (2 x 1 MB), not time-capped.
