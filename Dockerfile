# Build from this folder:
#   docker build -t a11y-check .
# Slim image: Debian + Python + Chromium's headless shell only. Microsoft's Playwright image ships
# three browsers (~2 GB); a smaller image downloads faster when Azure starts the container cold.
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/KarinSzilagyi/a11y-check" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    AUDIT_CODE_DIR=/app/audit

WORKDIR /app
COPY requirements.txt .
# fonts-dejavu-core: the tab-order screenshots draw their numbers in DejaVu Sans Bold.
# --with-deps installs the system libraries and fonts Chromium needs; --only-shell skips full Chromium.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-liberation \
 && pip install -r requirements.txt \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache \
 && useradd --create-home --uid 1000 app \
 && chmod -R a+rX /ms-playwright

COPY audit/audit_accessibility.py /app/audit/
COPY app.py worker.py /app/
COPY static /app/static

USER app
EXPOSE 8000
# One uvicorn worker. RUN_MODE=inline (default) keeps audits in this process's memory.
# RUN_MODE=queue: the same image runs the checks as a job with `python worker.py`.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
