# ---------------------------------------------------------------------------
# Single-stage build: Playwright Noble image already has Chromium + Python.
# We just install our Python dependencies on top of it.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000 \
    EGX_BROWSER_HEADLESS=true \
    EGX_DATABASE_PATH=/app/data/egx.sqlite3

WORKDIR /app

# Install Python dependencies as root (before switching user)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip cache purge 2>/dev/null || true

# The Playwright Noble image ships with 'ubuntu' (UID 1000); reuse it.
RUN mkdir -p /app/data /app/logs \
    && chown -R ubuntu:ubuntu /app

# Copy only the application source (no .venv, no tests, no secrets)
COPY --chown=ubuntu:ubuntu app/ ./app/
COPY --chown=ubuntu:ubuntu scripts/ ./scripts/
COPY --chown=ubuntu:ubuntu docs/ ./docs/

USER ubuntu

EXPOSE 8000

# Healthcheck: hits the public /health endpoint (no API key required)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()" || exit 1

STOPSIGNAL SIGTERM

# Single uvicorn worker intentional: background poller runs in-process and
# uses a DB lease to prevent double-collection if ever scaled to replicas.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
