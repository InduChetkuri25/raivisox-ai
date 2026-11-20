# RAIVISOX sandbox worker — the dedicated execution boundary (docs §10.7)
#
# BUILD CONTEXT IS THE REPOSITORY ROOT:
#
#   docker build -f apps/worker/Dockerfile -t raivisox-sandbox-worker .
#
# This image is the ONLY one that carries a container-runtime client and the
# only service that mounts the runtime socket. It runs a narrow, HMAC-
# authenticated protocol and holds no database URL, application secret or LLM
# key. The API image (apps/api/Dockerfile) has no runtime at all.

# The CLI is copied from the official image rather than installed from a
# third-party tarball; the worker never runs the daemon, only the client.
FROM docker:27-cli AS dockercli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# The worker executes `docker run` against the mounted socket; it needs no
# TTY tooling beyond the client binary itself.
RUN docker --version

WORKDIR /app

COPY apps/worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Only the execution boundary and the modules it imports — not the API app,
# not the adapters, not the database layer.
COPY services/ /app/services/

# The runtime socket is root-owned, so the worker's own process runs as root
# inside its container. That is exactly why it carries no other secret: the
# socket already grants host-wide control to this container and nothing more
# is handed to it (docs/deployment.md §10.7). The containers it *starts* are
# non-root (SANDBOX_CONTAINER_USER).
EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "services.execution.worker_app:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers"]
