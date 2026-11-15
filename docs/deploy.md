# Deploy

Target: the GoDaddy Docker VPS, using Docker Compose with Caddy for HTTPS.

## First time

1. Server: SSH keys only, and a firewall that allows only ports 22, 80 and 443.
2. DNS (gddy CLI): an `A` record for `@`, `www` and `*` pointing at the VPS IP.
3. Copy `.env.example` to `.env` on the server and fill it in (`APP_ENV=production`).
4. `make up`
5. Seed the sample businesses once: `docker compose exec app python -m scripts.seed_samples`

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

- Back up now: `docker compose exec app python -m scripts.backup --dir /app/backups` (keeps 7 days)
- Daily cron on the host: `0 2 * * * cd /srv/toran && docker compose exec -T app python -m scripts.backup --dir /app/backups`
- Restore: `docker compose stop app`, then `docker compose run --rm app python -m scripts.backup --restore /app/backups/<file>.db`, then `docker compose start app`

## Email

- Set `SMTP_HOST`, `SMTP_PORT` (465), `SMTP_USER` (the full mailbox address), `SMTP_PASSWORD` and `CONTACT_TO_EMAIL` in `.env`. If `smtp.titan.email` rejects the login, use `smtpout.secureserver.net`.
- Check it after a deploy: send a message from the contact form on the home page, then look in the `CONTACT_TO_EMAIL` inbox. A rejected login shows as `mail_failed` in `make logs`.
- Messages in the mailbox fall under the same 90-day retention as enquiries; delete older ones from the mailbox by hand.

## Logs and retention

- The app writes JSON logs to `/app/data/logs/app.log`, one file per day (`app.log.YYYY-MM-DD`). Follow them with `make logs`.
- A cleanup runs when the app starts and then every 24 hours. It deletes enquiries older than 90 days and log files older than 30 days. Run it by hand with `docker compose exec app python -m scripts.purge`.
- Docker keeps only a small rolling buffer (2 x 1 MB) of each container's console output for crash diagnosis.

## Security notes

- `/internal/*` returns 404 through Caddy. Only Caddy's on-demand TLS `ask` call reaches it, directly on the internal network.
- Uvicorn trusts `X-Forwarded-For` only from the Caddy container (`FORWARDED_ALLOW_IPS`).
