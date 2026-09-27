# RAIVISOX API — production image
# Python 3.12 (pinned): every pinned dependency ships wheels for it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# gcc: fallback for any sdist build; postgresql-client: ops/backup use
# inside the container (see docs/backups-recovery.md).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root runtime user; the task workspace must be writable by it and is
# usually mounted as a volume (artifact storage — docs/backups-recovery.md).
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/workspaces \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Migrate first (works on PostgreSQL AND SQLite — see alembic/env.py),
# then serve. init_db() at startup additionally runs create_all, which is
# idempotent.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
