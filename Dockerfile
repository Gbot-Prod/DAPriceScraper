# One image, two roles:
#   API (default CMD):  uvicorn on $PORT
#   Scheduled sync:     override the start command with `da-dpi run-daily`
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[postgres]"

# Run as an unprivileged user; data/ holds downloaded PDFs and the manifest
# (scratch space only -- Postgres is the source of truth).
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown app:app /app/data
USER app

EXPOSE 8000

# Railway (and most PaaS hosts) inject PORT; default to 8000 elsewhere.
CMD ["sh", "-c", "exec uvicorn da_price_index.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
