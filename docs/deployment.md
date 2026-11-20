# RAIVISOX AI — Production Deployment

This is the documented production path. It uses Docker Compose with five
services (PostgreSQL, API, sandbox worker, frontend, optional Ollama) and no
other infrastructure.

> **Verification status:** the application and its PostgreSQL path are
> live-verified with real PostgreSQL 16.9 under `ENVIRONMENT=production`
> (see §5 and `CHANGELOG.md`). Both production images are built from a clean
> checkout on Linux CI, and the `production-isolation-gate` job starts the API
> image and the sandbox worker and runs the real execution path through them
> (§10.7). The **full compose stack has not been started as one unit** in the
> release environment (no Docker there); the gates above exercise the same
> images, network hop and sandbox flags individually.

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
| `SANDBOX_BACKEND` | `container`, or `process` + `SANDBOX_ALLOW_PROCESS_IN_PRODUCTION=1` | Backend for task-generated code. Production refuses to start without an explicit choice and refuses the unconfined process backend without an acknowledgement — see §10. |

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
§2. Storage lives in the host directory `${RAIVISOX_WORKSPACE_DIR:-./data/workspaces}`,
mounted at `/app/workspaces` (§10.7 explains why it must be a host path).
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
  `DEBUG=true`, no LLM provider, non-positive artifact limits, or an
  undeclared / unconfined `SANDBOX_BACKEND` in production (§10) —
  naming variables only, never values.
- Graceful shutdown cancels and awaits in-flight background pipelines
  and the artifact cleanup worker; any execution_run left open is closed
  by the startup sweep on next boot (runs can never stay `running`).

## 9. TLS / reverse proxy

Terminate TLS at your proxy and forward to `api:8000` (uvicorn runs with
`--proxy-headers`) and `web:3000`. Set `CORS_ORIGINS` to the exact
browser-facing origins and `ALLOWED_HOSTS` to the public API hostname.

## 10. Sandbox isolation for task-generated code (audit finding H1)

### 10.1 What actually runs

Every untrusted execution — `python_exec`, `shell_exec`, the codegen
smoke-run and the DoD checks — goes through `build_default_sandbox()`
(`services/execution/sandbox.py`), the single doorway. Which backend it
returns is decided by `SANDBOX_BACKEND` through
`services/execution/sandbox_policy.py`.

`ProcessSandbox` (the development/test backend) enforces exactly this —
`capabilities()` and `SandboxResult.enforced` are the per-run witness:

| Guarantee | Process backend |
|---|---|
| argv execution (never a shell string) | yes |
| minimal explicit child environment | yes |
| working directory = the task workspace | yes |
| bounded output, wall-clock timeout + tree kill | yes |
| CPU / address-space / file-size rlimits | POSIX hosts only |
| network deny | **no** (`network_deny: False`) |
| filesystem confinement beyond cwd semantics | **no** (`fs_confinement: False`) |
| process-count cap | **no** (`pids_limit: False`) |

A working directory is not a boundary: the child keeps the API process's
uid, so absolute paths (`/app/...`) stay reachable, and nothing blocks
outbound sockets.

### 10.2 Why that is not a production posture (H1)

The production compose file previously declared no `SANDBOX_BACKEND`, so the
factory defaulted to `ProcessSandbox` inside the API container. That means:

- **Cross-tenant artifact access.** `/app/workspaces` is one tree for every
  user; a child running as the API user can read and rewrite other tasks' and
  users' files. App-layer path clamps do not apply to raw process execution.
- **Application tampering.** `/app` is owned by `appuser` — the same uid the
  child runs as — so task code can modify the running application.
- **Unrestricted egress.** No network namespace is created: task code reaches
  the internet and the compose network (PostgreSQL, the web tier, the model
  server), enabling exfiltration and internal probing.
- **Container-wide denial of service.** No PID or cumulative memory ceiling.
- **Possible secret disclosure** where the host allows same-user
  `/proc/<pid>/environ` reads (`ptrace_scope=0`). Not assumed, not excluded.

Rlimits and timeouts bound a run; they do not confine it.

### 10.3 What is enforced now (configuration, fail closed)

| Environment | `SANDBOX_BACKEND` | Result |
|---|---|---|
| development / test | absent | `process` (unchanged) |
| development / test | `process` / `container` | that backend |
| **production** | **absent** | **startup refusal** — the API does not boot |
| **production** | `process` | **refused** unless `SANDBOX_ALLOW_PROCESS_IN_PRODUCTION=1` |
| **production** | `process` + acknowledgement | allowed; logs an explicit UNCONFINED warning |
| production | `container` | allowed; each run fails honestly when no runtime is reachable |
| **production** | `worker` | **recommended**: signed requests to the dedicated `sandbox-worker` (§10.7) |
| production | `worker` without URL or key | **startup refusal** — the boundary cannot be half-configured |
| any | unknown or explicitly empty | configuration error — never a substitute backend |

These are deterministic configuration checks (no runtime required): they
prove the selection, **not** isolation. `docker-compose.prod.yml` uses
`${SANDBOX_BACKEND:?…}`, so compose fails fast on the same decision.

### 10.4 Options considered

| Option | Isolation | Ops complexity | Portability | Failure behavior | Verdict |
|---|---|---|---|---|---|
| A. Process backend in the API container | none (§10.1) | lowest | any | falls open to shared uid/net | rejected as default; acknowledged posture only |
| B. Container backend with the runtime socket mounted into the API | per-container flags, plus a root-equivalent socket in the API | medium | Docker/Podman hosts | runtime outage stops executions (honest) | **rejected** — one API compromise would mean host-wide control |
| B′. Container backend via a remote/rootless runtime (podman-remote over SSH, or a runtime-only sidecar with a narrow API) | same as B without a local socket | medium-high | Linux | honest fail-closed | acceptable interim on one host |
| C. Dedicated sandbox execution service (API sends a signed request → isolated worker runs one throwaway container per run) | strongest: the API never touches a runtime | high (worker identity, storage handoff, image supply chain) | any | worker outage fails closed, retryable | **implemented — §10.7** |

Mounting `/var/run/docker.sock` lets the containerised API start any
container, mount the host filesystem and reach other containers — effective
root on the host. Since task code runs in that same container, "compromise
the API" and "control the host" would become one event. The shipped compose
file mounts no runtime socket.

### 10.5 Recommendation and status

- **Implemented:** the fail-closed configuration above, option C as the
  dedicated `sandbox-worker` service (§10.7), and the production image
  packaging fix (§10.3a).
- **Recommended deployment:** `SANDBOX_BACKEND=worker`. The API container has
  no runtime client and no runtime socket; the worker is the only service that
  can start containers.
- **Supported alternatives:** `SANDBOX_BACKEND=container` (isolated, but the
  runtime must be reachable from the API container — on a single Linux host
  that usually means the socket back in the API, which §10.4 rejects), or the
  acknowledged `process` posture whose limits are listed above.

H1 is **mitigated for the supported architecture**: the silent insecure
fallback is gone, and the deployment that ships now carries a real
namespace-level boundary whose behaviour is measured by CI (not asserted from
configuration). It is **not closed** for every host: see the limitations in
§10.8 (in particular a worker that is itself compromised still holds the host
runtime).

### 10.3a Production image packaging (fixed)

The API imports the repository-level `services.*` and `integrations.*`
packages, but the shipped build context was `./apps/api`, which contains
neither — so the produced image could not import its own application. Both
images now build from the **repository root**:

```
docker build -f apps/api/Dockerfile    -t raivisox-api .
docker build -f apps/worker/Dockerfile -t raivisox-sandbox-worker .
```

`apps/api/Dockerfile` copies `services/`, `integrations/`, `apps/api/app`,
`apps/api/alembic` and `apps/api/alembic.ini` — no blanket context copy, so
`.dockerignore` keeps tests, docs, `.env`, databases and local workspaces out
of the image. The `production-isolation-gate` CI job builds both images from a
clean checkout and asserts imports, production startup validation and the
absence of development material inside them.

### 10.6 Remaining limitations

- This change enforces a policy decision and ships the option-C boundary; what
  the boundary actually enforces is reported per run (`SandboxResult.enforced`)
  and measured by the gate — never inferred from configuration.
- Container guarantees are verified only where a runtime exists (Linux CI). No
  isolation is claimed for a runtime-less or Windows host.

### 10.7 The dedicated execution service (option C)

**Trust boundary.** Two processes, one shared secret, no shared database:

```
  API container                        sandbox-worker container
  ─────────────                        ────────────────────────
  DB credentials, SECRET_KEY,          SANDBOX_WORKER_TOKEN only
  provider keys, HTTP surface    ──▶   no DB URL, no app secret,
  no runtime client,             HMAC   no provider key
  no runtime socket                    /var/run/docker.sock (only here)
                                       │
                                       └─▶ throwaway sandbox container
                                           (non-root, --network none,
                                            read-only rootfs, cap-drop ALL,
                                            no-new-privileges, pinned digest)
```

**Flow.** `python_exec` / `shell_exec` / codegen / DoD → `build_default_sandbox()`
→ `WorkerSandbox.run(spec)` → signed `POST /v1/execute` → the worker validates
and clamps, runs one container through `ContainerSandbox`, returns bounded
stdout/stderr, exit status, timeout/kill reason, the flags actually enforced,
and a bounded artifact manifest (path, size, sha256 — no bytes; the volume is
shared) → the API reads artifacts through its own containment check.

**Authentication.** HMAC-SHA256 over `timestamp\n nonce\n sha256(body)` with a
≥32-character shared key, a ±300 s clock window and a bounded nonce cache: a
captured request cannot be replayed, and an unsigned call gets 401 with no
detail. The key is the only thing the two services share.

**Provenance.** `task_id` / `run_id` / `step_id` are taken from the
server-built sandbox environment (the execution context), never from a tool
parameter; the client refuses to send a request without them, and the worker
validates their shape and echoes them back. Artifact ownership follows the
run's own scope, which the worker contains against its workspace root.

**What a caller cannot do.** The request has no network, privilege or
capability fields at all. The worker always passes `--network none`,
`--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, a
non-root `--user`, `--pids-limit`, `--memory`/`--memory-swap`, `--ulimit
cpu=`/`fsize=`, and a single rw bind of the run's own subtree. Timeout, CPU,
memory, pid, fsize and output caps are clamped to the worker's ceilings — a
caller may ask for less, never more.

**Idempotency.** A `request_id` executes at most once. A duplicate replays the
stored response (`replayed: true`); one already running answers 409; the store
is bounded (512 entries, 1 h TTL). The API retries only on a transport error,
and reuses the same request id for that retry.

**Cancellation and cleanup.** `POST /v1/execute/{id}/cancel` cancels the run;
`ContainerSandbox` force-removes the container on cancellation, timeout and
failure, and the gate asserts no `raivisox-sbx-*` container survives.

**Workspace requirement.** The runtime daemon resolves bind sources on the
**host**, so a path inside the worker container is not usable as a bind source.
`RAIVISOX_WORKSPACE_DIR` is therefore a required **absolute host directory**
(not a named volume), mounted at `/app/workspaces` in the API and the worker,
and the worker translates that prefix back to the host path through
`SANDBOX_HOST_WORKSPACE_ROOT` — the host path of **exactly**
`SANDBOX_WORKER_WORKSPACE_ROOT` (i.e. `${RAIVISOX_WORKSPACE_DIR}/tasks`, not
its parent) — via `ContainerSandbox.host_bind_path`. Without the translation
the daemon silently creates an empty host directory and the run sees an empty
workspace; the gate asserts the opposite (a canary written by the API is read
inside the container).

**Configuration.**

| Variable | Service | Meaning |
|---|---|---|
| `SANDBOX_BACKEND=worker` | API | selects the worker client |
| `SANDBOX_WORKER_URL` | API | e.g. `http://sandbox-worker:8100` (no default) |
| `SANDBOX_WORKER_TOKEN` | API + worker | shared HMAC key, ≥32 chars, same value |
| `SANDBOX_WORKER_WORKSPACE_ROOT` | worker | `/app/workspaces/tasks` |
| `SANDBOX_HOST_WORKSPACE_ROOT` | worker | host path of `SANDBOX_WORKER_WORKSPACE_ROOT` (bind-source translation) |
| `SANDBOX_CONTAINER_IMAGE` | worker | digest-pinned sandbox image |
| `SANDBOX_CONTAINER_USER` | worker | `1000:1000` — matches the API uid |
| `SANDBOX_WORKER_MAX_*` | worker | timeout / memory / pids / concurrency ceilings |
| `RAIVISOX_WORKSPACE_DIR` | compose | host path for task workspaces |

**Verified by CI (`production-isolation-gate`).** Built images from a clean
checkout; imports and production startup validation inside the image; a real
API-image → worker → container execution; non-root uid; network denial;
read-only rootfs; no host/worker filesystem visibility; only the run's own
subtree mounted; memory and timeout termination; bounded output; artifact
manifest; 401 for unsigned/stale/replayed requests; replay refusal; duplicate
request id executed once; cancellation; no leftover containers; no runtime
socket or client in the API container; and no `ProcessSandbox` fallback when
the worker is unreachable.

### 10.8 Open limitations after this change

- **The worker holds the host runtime.** Any design that starts containers
  does. The socket is now confined to a service with no database, application
  or provider secrets and a narrow validated protocol, but a compromised
  worker is still root-equivalent on the host. Use a rootless runtime, a
  remote runtime (`DOCKER_HOST` to a dedicated VM) or a dedicated host to
  reduce this further; the compose file mounts the socket only in that service.
- **The worker runs as root inside its own container** because the runtime
  socket is root-owned. The containers it starts are non-root (uid 1000).
- **Artifact bytes cross no boundary** — the API and worker share a host
  directory, so the manifest is a statement about paths the API re-validates.
  The worker never transmits file contents.
- **Idempotency state is in memory.** A worker restart forgets completed
  request ids; a retry after a restart could execute again. The API's retry is
  limited to transport failures within one call, so the window is small but
  non-zero.
- **No queue.** Concurrency is capped by a semaphore and excess work answers
  429. Sustained load needs an external queue, which is out of scope here.
- **Base images are tag-pinned, not digest-pinned** (`python:3.12-slim`,
  `docker:27-cli`); the *sandbox* image is digest-pinned and verified per run
  by `services/execution/image_pins.py`. Pin the build bases too if your
  supply-chain policy requires it.
- **The LLM-driven task pipeline is not exercised in CI** (no model there):
  the gate runs the same sandbox doorway the adapters use, not a generated
  program.
- **Verified on Linux only**, with a real Docker daemon; no isolation is
  claimed for a runtime-less or Windows host.

## 11. The whole stack, end to end (acceptance gate)

`tests/integration/production_stack_gate.py` boots the **shipped** production
compose file and proves the system works as one stack — not as isolated
images. It is the same script the `production-stack-gate` CI job runs:

```bash
# Linux host with Docker + Compose v2, from the repository root:
pip install "httpx>=0.27.0"
python tests/integration/production_stack_gate.py
```

The gate writes its own `.stack-gate.env` (gitignored, mode 600, test-only
values), so it never touches your `.env`. Every check prints `PASS`/`FAIL` and
any failure exits non-zero; nothing is skipped.

### 11.1 What it does, in order

1. `docker compose build` for the API, the worker and the web frontend from
   this checkout.
2. Start `postgres`, wait for **healthy**; start `sandbox-worker`, wait for
   **healthy**; start `api` and `web`. The API must answer
   `GET /health/ready` with `status: ready` under `ENVIRONMENT=production`
   (`DEBUG=false`), and the frontend must answer on `:3000`.
3. **Migrations** through the image's own entrypoint (`alembic upgrade head`
   in `apps/api/Dockerfile`): `alembic current` must equal `alembic heads`,
   the required tables must exist, and `select version()` must be PostgreSQL.
4. **Stack shape**: the API container has no runtime socket, no docker CLI
   and no `DOCKER_HOST`; the worker has the socket, the shared token and
   nothing else (no `DATABASE_URL`, `SECRET_KEY` or provider key).
5. **API surface**: readiness reports database + storage ok and the LLM as
   not reachable (there is no model in CI — see §11.4); `/docs`, `/redoc` and
   `/openapi.json` are 404; CORS echoes only the configured origin;
   unauthenticated task listing is refused.
6. **Real execution workflow** through the authenticated HTTP API:
   - a full task reaches `completed` / `verified` with a satisfied
     definition-of-done, one closed `execution_runs` row, registered
     artifacts whose SHA-256 matches the downloaded bytes, and a flat,
     traversal-safe ZIP bundle;
   - `python_exec` and `shell_exec` run through the kernel-gated registry
     inside throwaway containers (real, non-simulated steps);
   - a step that exceeds its timeout fails the task honestly (no false
     success) and leaves no run `running`.
7. **Provenance and audit** in PostgreSQL: `audit_links` rows for the task and
   the run carry an `action_id`; `tool:*` audit events are linked; with
   `V3_EVENTS_ENABLED=1` the `graph_events` rows cover `tool.attempted` →
   `tool.executed` for the run; the worker's log attributes the execution to
   the real `task_id` and `run_id`.
8. **Restarts**: worker restart (execution still works), API restart (state
   survives, no run left `running`, no task stuck active), PostgreSQL restart
   (data survives and readiness recovers).
9. **Failures**: no worker → the task fails with an unreachable-worker error
   and **no** process fallback; a wrong worker key is refused with 401; three
   concurrent runs finish serialized 2-at-a-time (the concurrency ceiling
   holds); no `raivisox-sbx-*` container survives.
10. **Shared-workspace security**: per-run directories; a symlink pointing at
    a sibling run is unreadable inside the sandbox; a write into a sibling run
    is refused; no world-writable path exists under the workspace root; a
    foreign user gets 404 for another user's artifacts.

### 11.2 Workspace ownership (required)

`RAIVISOX_WORKSPACE_DIR` must be an **absolute host directory owned by uid
1000** (the API user and `SANDBOX_CONTAINER_USER`). A directory owned by any
other uid is not writable by the API, and every run fails at its first file
write. Provision it as root before the first start:

```bash
install -d -o 1000 -g 1000 -m 0755 /srv/raivisox/workspaces
```

The gate does the same chown and asserts it.

### 11.3 Start, health, shutdown

```bash
cp .env.example .env      # fill every ${VAR:?}; see §2
docker compose -f docker-compose.prod.yml up -d --build   # migrates, then serves
docker compose -f docker-compose.prod.yml ps              # every service healthy
curl -fsS http://127.0.0.1:8000/health/ready             # {"status":"ready",...}
curl -fsS http://127.0.0.1:8000/health/live              # liveness only
docker compose -f docker-compose.prod.yml logs -f api sandbox-worker
docker compose -f docker-compose.prod.yml down            # graceful shutdown
```

`SIGTERM` drains the API: in-flight pipelines are cancelled and every open
`execution_runs` row is closed, so a restart never leaves a task stuck active
(the gate asserts this). Compose restarts services with `restart:
unless-stopped`; there is no durable queue, so a task interrupted by a hard
kill is marked `failed` with its interruption stage at next start rather than
resumed.

### 11.4 What the gate does **not** claim

- **No LLM is exercised.** `RAIVISOX_LLM_BASE_URL` points at a closed port, so
  the model-backed planning/generation path is not verified by CI; the
  rule-based planner and the codegen adapter's deterministic engine run the
  pipeline instead. Model quality and LLM-driven execution remain unverified
  in CI and must be checked against a real endpoint.
- **Event emission is on for the gate only.** `V3_EVENTS_ENABLED=1` is a CI
  input, not the production default (the V3 Phase 1 decision keeps graph
  events off outside dev/test/CI). Switching it on in production requires that
  decision to be revisited.
- **Idempotency is per-process memory.** A worker restart forgets completed
  request ids, so a retry after a restart can execute again; the API only
  retries on transport errors within one call. Exactly-once execution is not
  claimed (§10.8).
