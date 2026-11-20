# RAIVISOX API — production image
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not ./apps/api. The API imports the
# repo-level `services.*` (execution sandbox, DoD, security) and
# `integrations.*` (tool adapters) packages, so a context limited to
# ./apps/api produced an image that could not import its own code — the
# packaging blocker recorded in docs/deployment.md §10.3.
#
#   docker build -f apps/api/Dockerfile -t raivisox-api .
#
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

COPY apps/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Repo-level packages the API imports. /app is on sys.path (WORKDIR), so
# `services.execution.sandbox` and `integrations.adapters.*` resolve exactly
# as they do in the test suite.
COPY services/ /app/services/
COPY integrations/ /app/integrations/

# The API application itself.
COPY apps/api/app /app/app
COPY apps/api/alembic /app/alembic
COPY apps/api/alembic.ini /app/alembic.ini

# Non-root runtime user; the task workspace must be writable by it and is
# usually mounted as a volume (artifact storage — docs/backups-recovery.md).
# uid 1000 is also the uid the sandboxed container runs as
# (SANDBOX_CONTAINER_USER), so files it writes stay readable here.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/workspaces \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Migrate first (works on PostgreSQL AND SQLite — see alembic/env.py),
# then serve. init_db() at startup additionally runs create_all, which is
# idempotent.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
