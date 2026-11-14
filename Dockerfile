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

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts

# pwuser is the image's built-in non-root user.
RUN mkdir -p /app/data /app/backups && chown -R pwuser:pwuser /app/data /app/backups
USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Migrations run with Alembic on start; the app never calls create_all.
# FORWARDED_ALLOW_IPS (set in docker-compose.yml) limits trusted proxy headers to Caddy only.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --no-access-log"]
