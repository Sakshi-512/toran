# Deploy

Target: the GoDaddy Docker VPS, using Docker Compose with Caddy for HTTPS.

## First time

1. Server: SSH keys only, and a firewall that allows only ports 22, 80 and 443.
2. DNS (gddy CLI): an `A` record for `@`, `www` and `*` pointing at the VPS IP.
3. Copy `.env.example` to `.env` on the server and fill it in (`APP_ENV=production`).
4. Create the backup folder for the app's non-root user (uid 1001 in the Playwright image), or Docker creates it owned by root and backups fail: `mkdir -p backups && sudo chown 1001:1001 backups`
5. `make up`. The image installs the pinned GoDaddy CLI (gddy 0.2.37, SHA-256 checked during the build); `curl -s https://<BASE_DOMAIN>/health` should show `"gddy": "installed"`.
6. Seed the sample businesses once: `docker compose exec app python -m scripts.seed_samples`
7. Run the agent fix loop once on the home-foods demo business, with real Claude (about $0.25):
   `docker compose exec app python -m scripts.agent_run savitri-gruh-udyog` (the same as `make agent slug=savitri-gruh-udyog` outside Docker). It prints each round, the final score and the cost. The home page's "Before and after" section stays hidden until such a real run on a demo business has raised the score; mock runs are never shown there. If a run stops without improving, run it again later.

Migrations run automatically on start (`alembic upgrade head`). The app never calls `create_all`.

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
