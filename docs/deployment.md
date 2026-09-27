# RAIVISOX AI — Production Deployment

This is the documented production path. It uses Docker Compose with four
services (PostgreSQL, API, frontend, optional Ollama) and no other
infrastructure.

> **Verification status:** the application and its PostgreSQL path are
> live-verified with real PostgreSQL 16.9 under `ENVIRONMENT=production`
> (see §5 and `CHANGELOG.md`). The **Docker container stack itself has
> not been started in the release environment** (Docker unavailable
> there); the compose files and Dockerfiles are reviewed and consistent
> with the verified configuration.

## 1. Requirements

- Docker + Docker Compose v2
- A public domain with TLS termination in front of the stack (reverse
  proxy such as Caddy/nginx — not bundled, any conforming proxy works)
- 2+ CPU, 4 GB RAM (plus model size if you run the bundled Ollama)

## 2. Configure

```bash
cp .env.example .env
openssl rand -hex 32   # → SECRET_KEY
```

Edit `.env` — the following are **required**; the stack refuses to start
without them (fail-fast by design):

| Variable | Example | Purpose |
|---|---|---|
| `SECRET_KEY` | 64 hex chars | JWT signing key. The known development default is **refused** in production. Never logged or echoed in errors. |
| `POSTGRES_PASSWORD` | strong | Bundled PostgreSQL password. |
| `CORS_ORIGINS` | `https://app.example.com` | Exact frontend origin(s). Comma-separated or JSON. |
| `ALLOWED_HOSTS` | `api.example.com` | Host-header allow-list (TrustedHost middleware). |
| `NEXT_PUBLIC_API_URL` | `https://api.example.com` | API base URL **baked into the frontend bundle at build time**. |
| `RAIVISOX_LLM_BASE_URL` | `http://ollama:11434/v1` or `https://api.openai.com/v1` | OpenAI-compatible provider endpoint. |
| `DATABASE_URL` | set automatically inside compose | Explicit in non-compose deployments. Must be `postgresql+asyncpg://…` in production. |

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `RAIVISOX_LLM_MODEL` | `qwen2.5:0.5b` | Model name sent to the provider. |
| `RAIVISOX_LLM_API_KEY` | empty | Non-empty marks any endpoint usable. For the bundled Ollama (non-localhost from the API container) set a sentinel, e.g. `local-ollama` — Ollama ignores auth. For hosted providers use the real key. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | unset | Hosted provider keys. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `MAX_ARTIFACT_SIZE_BYTES` | 52428800 (50 MiB) | Per-artifact ceiling. |
| `MAX_ARTIFACTS_PER_TASK` | 1000 | Registry rows (incl. history) per task. |
| `MAX_ZIP_SIZE_BYTES` | 209715200 (200 MiB) | Per-bundle uncompressed byte ceiling. |
| `MAX_ZIP_FILES` | 10000 | Entries per bundle. |
| `ARTIFACT_RETENTION_DAYS` | 0 (keep forever) | 0 disables retention entirely: no cleanup worker is started and artifact bytes are kept forever. `N > 0` enables the scheduled cleanup worker — superseded artifact bytes older than N days are deleted, registry metadata is NEVER deleted (see §6a). |
| `ARTIFACT_CLEANUP_INTERVAL_SECONDS` | 21600 (6 h) | Seconds between scheduled cleanup passes. Only used when `ARTIFACT_RETENTION_DAYS > 0`; must be positive when retention is enabled (startup refuses otherwise). |
| `DEBUG` | `false` | Must be `false` in production (enforced); debug returns internal error details to clients. |

Frontend values prefixed `NEXT_PUBLIC_` are inlined into the JavaScript
bundle at **build** time — changing them requires a rebuild
(`docker compose build web`).

## 3. Start

```bash
# API + frontend + PostgreSQL:
docker compose -f docker-compose.prod.yml up -d --build

# With the bundled local provider (Ollama):
docker compose -f docker-compose.prod.yml --profile ollama up -d --build
docker exec raivisox-ollama ollama pull qwen2.5:0.5b
```

On start the API: runs `alembic upgrade head` (works on PostgreSQL and
SQLite), then an idempotent `create_all` in lifespan, resets orphaned
running tasks from a previous crash (they become honestly `failed` with
their interruption stage — no mid-pipeline resume exists or is implied),
and backfills legacy artifact registry rows.

## 4. Health / readiness

| Endpoint | Meaning |
|---|---|
| `GET /health` | Legacy liveness (kept for compatibility). |
| `GET /health/live` | Liveness — process is up. No dependency checks; a restart-on-failure probe. |
| `GET /health/ready` | Readiness — `200` only when the **database** round-trips and the **artifact storage** directory is usable; returns `503` otherwise. Reports `llm.configured` / `llm.reachable` as **advisory** (the API can serve traffic without an LLM). |

Responses contain service name, version and check statuses only — no
secrets, URLs or filesystem paths.

## 5. Database requirements

- **PostgreSQL 16** in production (compose ships `postgres:16-alpine`).
  SQLite remains fully supported for development and tests. **Live-verified
  against a real PostgreSQL 16.9 server**: Alembic `001 → 002` applies
  cleanly, the full application journey (auth, projects, tasks, async
  execution, execution_runs, artifact registry + downloads, ZIPs, run
  history, ownership isolation, audit gates, retention worker, graceful
  shutdown) passes with `DATABASE_URL=postgresql+asyncpg://…`, and the
  migrated schema matches the models at attribute level (columns + every
  model-declared index; `alembic.autogenerate` reports no object drift).
- Schema is managed by Alembic (`apps/api/alembic`); revisions `001` and
  `002` reproduce the model metadata exactly (a regression test enforces
  zero drift on SQLite; PostgreSQL verified live as above). Foreign keys
  are declared inline so migrations apply to both PostgreSQL and SQLite.
- Engine: pool_size 10 / max_overflow 20 with `pool_pre_ping` (stale
  connections recycled) on PostgreSQL; SQLite uses
  `check_same_thread=False` as before.
- Transaction boundaries: one session per request; the pipeline holds
  its own session; artifact registration and step-event persistence use
  independent sessions so a failure never corrupts execution state.
- Concurrency: duplicate async executes are prevented by an atomic
  conditional UPDATE claim (exactly one winner); artifact same-path
  registration serializes via a write-lock UPDATE before its SELECT.

## 6. Artifact storage limits

Enforced at registration (size, per-task count — a violation fails the
registration honestly, never fabricates or hides) and at bundle download
(entry count, byte total — HTTP 413 with a stable message). Defaults in
§2. Storage lives in the `workspace_data` volume (`/app/workspaces`).
These limits are independent of retention: retention governs how long
**superseded history bytes** survive; the limits govern what may be
registered and bundled at all.

## 6a. Artifact retention lifecycle

When `ARTIFACT_RETENTION_DAYS > 0`, the API starts exactly one cleanup
worker per process (`ARTIFACT_CLEANUP_INTERVAL_SECONDS`, default 6 h).
Each pass deletes, deterministically and idempotently:

1. **Expired superseded history bytes** — a registry row qualifies only
   when it is `is_current = false`, older than the retention window, its
   path is NOT referenced by any current row (same-path reuse: the bytes
   belong to the newer registration), the path is inside the workspace
   root (realpath containment — outside paths are never followed), and
   the file's mtime is not newer than the row's `superseded_at`/`created_at`
   (the bytes were not rewritten after supersession).
2. **Orphan workspace files** — files older than the window that NO
   registry row references (never-registered leftovers, deleted tasks).
   Fresh mtimes are always protected, so in-flight execution writes are
   untouchable. Symlinks are never followed; directories are never
   removed.

**Never touched:** current artifact bytes (regardless of age), registry
metadata (rows, stable artifact IDs, hashes, run grouping), ExecutionRun
history, anything outside the workspace root. After cleanup a historical
row remains queryable (artifact listing with `?run=`, row-by-ID) and a
download of it returns an honest `404 Artifact file is missing from the
workspace` — it never fabricates content. Task/project ZIP bundles export
current rows only, so retention cannot introduce new `MISSING.txt`
entries; missing current bytes (out-of-band deletion, not retention)
keep the existing honest `MISSING.txt` behavior.

**Manual trigger:** `POST /api/v1/admin/artifacts/cleanup` — same policy
as the worker, admin permission required (anonymous 401, non-admin 403
+ audit event), no path input accepted, returns counts only. At
retention 0 it answers `"retention_disabled": true` as an honest no-op.

**Failure behavior:** a failed pass is logged (counts only — no paths,
no secrets) and retried at the next interval; it never crashes the API
and never blocks request handling (the worker runs on its own task and
DB session). A deleted file that a later pass re-encounters is counted
as `already_missing` — passes are safe to overlap.

**Multiple API replicas:** passes are idempotent and race-safe against
registration (current-row and mtime guards), so the built-in worker is
safe to leave enabled on every replica; alternatively disable it (keep
retention 0 on replicas) and schedule an external job that calls the
admin endpoint — see `docs/backups-recovery.md` §7.

## 7. Logging & errors

- Operational logs are single-line `key=value` records:
  `pipeline terminal task_id=… execution_run_id=… status=… outcome=… duration_s=…`,
  plus startup sweep counts and step-event persistence failures.
- No secrets, tokens, passwords or request bodies are logged (audited).
- 500 responses return a fixed message (stack traces only when
  `DEBUG=true`, which production forbids). Persisted user-visible error
  text (task errors, step events, crash messages) has absolute
  filesystem paths scrubbed to `<path>`.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled
  when `ENVIRONMENT=production`.

## 8. Backend behavior in production

- Startup **refuses** to run on a missing/default `SECRET_KEY`,
  missing `DATABASE_URL`, empty `CORS_ORIGINS`, empty `ALLOWED_HOSTS`,
  `DEBUG=true`, no LLM provider, or non-positive artifact limits —
  naming variables only, never values.
- Graceful shutdown cancels and awaits in-flight background pipelines
  and the artifact cleanup worker; any execution_run left open is closed
  by the startup sweep on next boot (runs can never stay `running`).

## 9. TLS / reverse proxy

Terminate TLS at your proxy and forward to `api:8000` (uvicorn runs with
`--proxy-headers`) and `web:3000`. Set `CORS_ORIGINS` to the exact
browser-facing origins and `ALLOWED_HOSTS` to the public API hostname.
