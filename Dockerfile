# Runtime image is the official Playwright Python image (Chromium included, needed in Block 4).
# Keep the tag in sync with the playwright pin in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
# Every package, including indirect ones, is pinned with hashes in the lock file.
RUN pip install --break-system-packages --require-hashes --no-deps -r requirements.txt

# GoDaddy CLI for web address lookups: a pinned release, refused unless its SHA-256 matches.
COPY scripts/install_gddy.py /tmp/install_gddy.py
RUN python3 /tmp/install_gddy.py /usr/local/bin && rm /tmp/install_gddy.py && gddy --version

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts
# Sample business profiles, used by scripts.seed_samples.
COPY tests/fixtures/profiles ./tests/fixtures/profiles
# Saved Claude and GoDaddy answers for mock mode (CLAUDE_MOCK=1, GDDY_MOCK=1): fake demo data,
# no secrets. Production runs with both off; these let the stack run without credits.
COPY tests/fixtures/claude ./tests/fixtures/claude
COPY tests/fixtures/gddy ./tests/fixtures/gddy
# Customer checklists are read by the AI checks.
COPY docs/checklists.md ./docs/checklists.md

# pwuser is the image's built-in non-root user.
RUN mkdir -p /app/data /app/backups && chown -R pwuser:pwuser /app/data /app/backups
USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Migrations run with Alembic on start; the app never calls create_all.
# FORWARDED_ALLOW_IPS (set in docker-compose.yml) limits trusted proxy headers to Caddy only.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --no-access-log"]
