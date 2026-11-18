# Deploy

Target: the GoDaddy Docker VPS, using Docker Compose with Caddy for HTTPS.

## First deployment: checklist

Follow these in order on the GoDaddy VPS. Each step says how to check it. Commands run from the
project folder on the server (for example `/opt/toran`). The server has no Python virtualenv:
app commands run inside the container (`make server-...` wraps `docker compose exec app ...`).

**Server**

- [ ] 1. SSH keys only (`PasswordAuthentication no`), and a firewall that allows only 22, 80 and 443:
  `sudo ufw allow 22,80,443/tcp && sudo ufw allow 443/udp && sudo ufw enable`
- [ ] 2. Docker and the Compose plugin are installed: `docker compose version`
- [ ] 3. Check the CPU architecture: `uname -m` (expect `x86_64`). The image installs the matching
  pinned gddy build; step 11 checks it runs.
- [ ] 4. Clone the repository and `cd` into it.

**DNS** (gddy CLI on your own machine, or the GoDaddy DNS page)

- [ ] 5. Point the domain at the server, then check from your machine with `dig +short <name>`:
  - `A` record `@` → the VPS IP (`gettoran.com`)
  - `A` record `www` → the VPS IP
  - `A` record `*` → the VPS IP (every business subdomain, for example `asha-home-bakery.gettoran.com`)

**Settings** (`.env` on the server only, never in git)

- [ ] 6. `cp .env.example .env && chmod 600 .env`, then set:
  - `APP_ENV=production`
  - `BASE_DOMAIN=gettoran.com`
  - `ACME_EMAIL=` the team address (Let's Encrypt contact)
  - `SECRET_KEY=` a new random value: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
  - `CLAUDE_MOCK=0`, `GDDY_MOCK=0`, `STT_PROVIDER=sarvam`
  - Secrets: `ANTHROPIC_API_KEY`, `STT_API_KEY` (Sarvam) and `GDDY_PAT` (GoDaddy personal access token)
  - `CLAUDE_DAILY_SPEND_CAP_USD=4` (see "When the daily Claude cap is reached" below)
  - `STT_DAILY_MINUTES_CAP` as planned for the hackathon
  - Email: `SMTP_HOST`, `SMTP_PORT=465`, `SMTP_USER` (the full mailbox address), `SMTP_PASSWORD`,
    `CONTACT_TO_EMAIL`
  - Check nothing was left empty: `grep -E '^[A-Z_]+=$' .env` should print nothing you need.
- [ ] 7. Create the backup folder for the app's non-root user (uid 1001), or Docker creates it as
  root and backups fail: `mkdir -p backups && sudo chown 1001:1001 backups`

**Start**

- [ ] 8. `make up` (builds the image, installs the pinned gddy with its SHA-256 check, and starts
  the app and Caddy). Migrations run on start.
- [ ] 9. Both containers healthy: `docker compose ps` shows `(healthy)` for `app` and `caddy`.
- [ ] 10. Logs show the migrations and no errors: `docker compose logs app | grep -E "Running upgrade|ERROR"`
- [ ] 11. gddy on this architecture: `docker compose exec app gddy --version` prints
  `gddy version 0.2.37`, and `curl -s https://gettoran.com/health` shows `"gddy": "installed"`.

**HTTPS**

- [ ] 12. Main domain: `curl -sI https://gettoran.com/ | head -1` is `HTTP/2 200`, and
  `curl -svo /dev/null https://gettoran.com 2>&1 | grep issuer` names Let's Encrypt.
- [ ] 13. `http://gettoran.com` redirects to HTTPS: `curl -sI http://gettoran.com | head -3`

**Content**

- [ ] 14. `make server-seed` (the six demo businesses and their sites).
- [ ] 15. A business subdomain over HTTPS: `curl -sI https://asha-home-bakery.gettoran.com/ | head -1`
  is `HTTP/2 200` (its certificate is made on the first request; allow a few seconds). An unknown
  subdomain gets no certificate: `curl -sI https://nobody.gettoran.com/` fails at the TLS step.
- [ ] 16. `make server-agent slug=savitri-gruh-udyog` once, with real Claude (about $0.25). It
  prints each round, the score before and after, and the cost. The home page's "Before and
  after" appears only after a real run that raised the score; if it stops without improving,
  run it again later.

**Check it like a judge**

- [ ] 17. On a phone: open https://gettoran.com, tap a "Try this voice note" card, reach the
  confirmation cards, confirm, note the link and PIN, sign in, and see the draft score.
- [ ] 18. Send the contact form on the home page and check the `CONTACT_TO_EMAIL` inbox.
- [ ] 19. Backups: `ls -l backups/` shows today's `app-*.db` owned by uid 1001 (the app makes one
  on start, then once a day).
- [ ] 20. Write down the image tag that works, for rollback: `docker image ls toran`

## When the daily Claude cap is reached

`CLAUDE_DAILY_SPEND_CAP_USD=4` stops new Claude calls for the rest of the day (Asia/Kolkata), so
judges cannot use up the hackathon credit. 30% of the cap is kept for businesses that already
exist (their agent runs, AI checks and web addresses). When the cap is reached:

- Judges see a friendly message where a new Claude call was needed: "Our helper is resting for
  today. Your site stays live. Please try again tomorrow." Nothing crashes.
- The three one-tap sample voice notes still work: their saved extraction is used instead of a
  new Claude call, so the confirmation cards appear as usual. Their draft is still built and
  tested in the browser; only the AI-assessed score items wait, with a friendly note.
- The home page, the demo businesses' sites, the gallery, the before and after, and every live
  site keep working, because they need no Claude call.
- Spending today: `make server-agent` prints its own cost; for the day's total, check the
  `claude_call` and `claude_spend_cap_reached` lines in `make logs`.

## Giving an owner a new link and PIN

The owner's link and PIN are shown once, when the owner confirms. If the owner lost them:

1. Confirm it is the owner: call or WhatsApp the phone number on the owner's live site (never a number given in
   the request). If the owner's site has no number, use the email thread from the owner's contact form message.
2. `make server-access slug=<web address>` asks you to type `confirmed`, then prints a new
   link and PIN once. The old link stops working at once, the owner is signed out everywhere, and
   remembered devices are revoked. The log records only that access was reissued.
3. Give them to the owner directly. Do not store them anywhere.

## Release with a tagged image

```bash
IMAGE_TAG=v0.1.0 docker compose build app
IMAGE_TAG=v0.1.0 docker compose up -d
```

## Roll back

```bash
IMAGE_TAG=<previous tag> docker compose up -d app
```

## Backups

- The app makes a backup by itself once a day into `./backups` (the cleanup loop, production only) and keeps 7 days. A failed backup is logged as an error, which emails an alert.
- Back up now: `docker compose exec app python -m scripts.backup --dir /app/backups`
- Restore: `docker compose stop app`, then `docker compose run --rm app python -m scripts.backup --restore /app/backups/<file>.db`, then `docker compose start app`
- Restoring brings back everything in that backup, including data deleted after it was taken: a business deleted by its owner, enquiries past 90 days, or expired intakes, up to 7 days old. After a restore, have owners who deleted their site in that window delete it again (or delete it for them), and say so if anyone asks. The privacy page states that backups are kept for 7 days.

## Email

- Set `SMTP_HOST`, `SMTP_PORT` (465), `SMTP_USER` (the full mailbox address), `SMTP_PASSWORD` and `CONTACT_TO_EMAIL` in `.env`. If `smtp.titan.email` rejects the login, use `smtpout.secureserver.net`.
- Check it after a deploy: send a message from the contact form on the home page, then look in the `CONTACT_TO_EMAIL` inbox. A rejected login shows as `mail_failed` in `make logs`.
- Messages in the mailbox fall under the same 90-day retention as enquiries; delete older ones from the mailbox by hand.

## Logs and retention

- The app writes JSON logs to `/app/data/logs/app.log`, one file per day (`app.log.YYYY-MM-DD`). Follow them with `make logs`.
- A cleanup runs when the app starts and then every hour. It deletes enquiries older than 90 days, log files older than 30 days, leftover voice notes and price-list photos older than a day (those the owner agreed to keep after 30 days), intakes never confirmed after 7 days, sample businesses made from the sample voice notes after 7 days (with all their data), and businesses their owner deleted while a test of their site was still running. It also makes the daily backup. Run it by hand with `docker compose exec app python -m scripts.purge`.
- Docker keeps only a small rolling buffer (2 x 1 MB) of each container's console output for crash diagnosis.

## Security notes

- `/internal/*` returns 404 through Caddy. Only Caddy's on-demand TLS `ask` call reaches it, directly on the internal network.
- Uvicorn trusts `X-Forwarded-For` only from the Caddy container (`FORWARDED_ALLOW_IPS`).
- Request bodies over 70 MiB are refused by Caddy (`request_body`) and again by the app.
- Chromium runs without its own sandbox, which is Playwright's default (`chromiumSandbox: false`). This is acceptable here because it only opens our own generated business sites (the draft on the internal address and the live subdomains), never pages from users or the open web, and it runs inside the non-root app container. If the tests ever open outside pages, run Chromium with its sandbox and Docker's seccomp profile for Chrome.
- The app container uses `init: true` (reaps Chromium's child processes) and `shm_size: 1gb` (Chromium crashes with Docker's 64 MB default).
