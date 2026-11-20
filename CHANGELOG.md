# Changelog

All notable changes to RAIVISOX AI are documented here.
Versions follow the repository's existing metadata (`apps/api/pyproject.toml`,
`apps/web/package.json`, API `APP_VERSION`) — there is no separate tagging
convention in this repository.

## [0.1.0] — V1.0.0 release candidate (current)

**Release status: PASSED.** V1 Release Acceptance Gate passed (full test
matrix, production-config live verification, browser end-to-end journey,
release-blocker review: none found) and the PostgreSQL Live Gate passed
12/12 against a real PostgreSQL 16.9 server.

### Added

- **Honest outcome guarantees** — every task ends in a truthful terminal
  state (`verified`, `completed_unverified`, `failed`, `cancelled`);
  no false success is ever reported. DoD (definition-of-done) validation
  with real verification evidence and confidence.
- **Asynchronous execution** — `POST /tasks/{id}/execute` returns 202 and
  runs the 11-step pipeline in the background; duplicate execution is
  prevented by an atomic claim; graceful shutdown cancels in-flight
  pipelines without ever leaving a run stuck `running`.
- **First-class execution runs** — durable `execution_runs` rows per
  execute invocation; run-scoped artifact history filtered by `?run=`;
  crash/orphan recovery marks interrupted tasks honestly `failed` with
  their interruption stage (no true mid-pipeline resume exists or is
  claimed).
- **Artifact registry** — `task_artifacts` rows with SHA-256, size,
  content type, stable artifact IDs, current/superseded semantics on
  same-path rewrites, ownership-scoped downloads, per-run listing, and
  legacy-result backfill at startup.
- **Task & project ZIP exports** — basename-flattened, traversal-safe
  bundles with deduplication, size/entry-count limits (HTTP 413), and
  honest `MISSING.txt` entries for registry rows whose bytes are absent.
- **Artifact retention & cleanup** — `ARTIFACT_RETENTION_DAYS` (0 = keep
  bytes forever) with a scheduled one-per-process cleanup worker:
  expired superseded bytes and unregistered orphan files are deleted;
  registry metadata, stable IDs and run history are never deleted;
  current artifacts are never deleted; containment + current-path +
  mtime guards; admin manual trigger `POST /api/v1/admin/artifacts/cleanup`
  (401/403-and-audit boundaries); safe counts-only logging.
- **Security hardening** — per-task workspace isolation (no cross-task
  file access), production fail-fast configuration (explicit
  `SECRET_KEY`/`DATABASE_URL`/`CORS_ORIGINS`/`ALLOWED_HOSTS`/provider
  config; the default secret is refused in production), unauthenticated
  endpoints removed, strict CORS, email validation, input bounds,
  IDOR/traversal/leak test coverage (23-test dedicated suite).
- **Production hardening** — `/health`, `/health/live`, `/health/ready`
  (DB + storage gates, LLM advisory, no secrets/paths), structured
  operational logging (task_id / execution_run_id / status / duration /
  outcome), stack traces hidden unless `DEBUG`, internal paths scrubbed
  from persisted payloads, dialect-safe Alembic migrations with a
  zero-drift regression test.
- **Frontend integration** — Next.js 15 app with register/login/session
  restore, dashboard, live step progress polling (interval cleaned up on
  unmount), task detail with verification/DoD/artifact hashes, artifact
  + ZIP downloads, project pages, artifact history, logout; production
  build verified clean of secrets and internal paths.

### Fixed

- **`docker-compose.prod.yml` could not be parsed at all** — the
  `${SECRET_KEY:?…}` required-message contains `": "`, so YAML read the
  `- SECRET_KEY=…` sequence item as a mapping and every compose command
  (including the documented `up -d --build`) died with
  `services.api.environment.[4]: unexpected type map[string]interface {}`.
  The item is now quoted; the startup-fails-fast behaviour is unchanged.
- **Worker-backed sandbox execution always failed** — the execution
  provenance (`task_id`/`run_id`/`step_id`) was passed through
  `SandboxSpec.env`, which the adapters set to `{}` and the worker's
  `_ALLOWED_ENV_KEYS` allow-list strips, so every `python_exec`,
  `shell_exec`, codegen probe and DoD run under `SANDBOX_BACKEND=worker`
  died with "worker backend requires ... provenance". Provenance is now a
  first-class `SandboxSpec.provenance` field filled from the kernel-issued
  `AdapterContext`, and the worker enforces it as before. Found and fixed
  by the full-stack acceptance gate; the container/process backends and the
  isolation gate are unaffected.
- **PostgreSQL artifact registration** — the same-path supersession
  write-lock used the SQLite boolean literal `is_current = 1`, which
  PostgreSQL rejects (`operator does not exist: boolean = integer`);
  registration on PostgreSQL now uses `TRUE` (dialect flag in
  `app/database.py`). Found by the PostgreSQL Live Gate; SQLite behavior
  unchanged; full regression suite green after the fix.
- Alembic `001` foreign keys declared inline (SQLite `ALTER`-based
  post-hoc FKs previously failed); `002` adds `execution_runs`,
  `task_artifacts`, `audit_events`.
- Comma-separated list parsing for `CORS_ORIGINS`/`ALLOWED_HOSTS` (the
  documented `.env` form previously crashed config import).
- README quickstart corrected to the real app module
  (`uvicorn app.main:app`).

### Verified (this release)

- Test matrix: normal suite **361 passed / 6 skipped** (×3 during
  acceptance, again after the PostgreSQL fix); `pytest -m "not live_llm"`
  **361 passed / 2 skipped / 4 deselected**; `pytest -m live_llm`
  **4 passed** (real Ollama); `tsc --noEmit` clean; `next build` success.
- Production-config API smoke **15/15** (health/readiness, docs disabled,
  CORS, auth, project/task, async execution + duplicate guard, verified
  outcome, artifacts + download, task ZIP, run history, project ZIP,
  cross-user 404s, crash-restart orphan sweep, graceful shutdown, log
  hygiene) and retention live smoke **15/15** (real scheduled worker,
  current-byte protection, honest missing-byte downloads, MISSING.txt,
  admin gates, counts-only logs).
- PostgreSQL Live Gate **12/12** on real PostgreSQL 16.9: migrations
  `001 → 002` from empty DB, 16 FKs + all model-declared indexes present,
  full application journey with `DATABASE_URL=postgresql+asyncpg://…`
  (auth, projects, tasks, async execution, execution_runs, artifact
  registry with hash/size verification against DB rows, ZIPs, run
  history, ownership isolation, audit gates, retention worker, graceful
  shutdown); migrated schema matches models at attribute level.
- Browser end-to-end journey: register → execute real 11-step pipeline
  (0 simulated steps) → verified/100%/DoD-satisfied → artifact hashes
  shown → real ZIP download via UI → logout → re-login with persisted
  state.

### Security — production execution boundary (V3 Phase 1 audit, H1)

- **Explicit sandbox decision.** Production refuses to boot without
  `SANDBOX_BACKEND`; the unconfined `process` backend is refused unless the
  operator acknowledges it (`SANDBOX_ALLOW_PROCESS_IN_PRODUCTION=1`, logged as
  an explicit UNCONFINED warning). See `docs/deployment.md` §10.
- **Dedicated sandbox execution service (option C).** New `sandbox-worker`
  service (`apps/worker/Dockerfile`, `services/execution/worker_app.py`) is the
  only process with container-runtime access. The API sends HMAC-signed
  requests (`timestamp + nonce + body digest`, ±300 s window, replay refused)
  with server-derived `task_id`/`run_id`/`step_id`; the worker clamps limits and
  always runs one throwaway container with `--network none`, `--cap-drop ALL`,
  `no-new-privileges`, a read-only rootfs, a non-root uid, pid/memory/cpu/fsize
  caps, a single rw bind of the run's own subtree and a digest-pinned image.
  `request_id` idempotency means a retried request never executes twice.
- **Production image packaging fixed.** Both images now build from the
  repository root (`docker build -f apps/api/Dockerfile .`), so the API image
  contains the `services.*` and `integrations.*` packages it imports; a
  `.dockerignore` keeps tests, docs, `.env` and databases out.
- **CI gate:** `production-isolation-gate` builds the images and runs the real
  API-image → worker → container path, including network denial, read-only
  rootfs, workspace-scope, memory/timeout termination, cancellation, 401s for
  forged/stale/replayed requests and no `ProcessSandbox` fallback.

### Not claimed / known limitations

- **The full compose stack is not started as one unit in the release
  environment** (no Docker there). Both images are built and the API→worker→
  container path is exercised on Linux CI; the complete five-service stack has
  been reviewed, not booted end to end.
- **The worker holds the host runtime socket.** That is inherent to starting
  containers: a compromised worker is root-equivalent on the host. It holds no
  database, application or provider secret, and the API holds no runtime
  access at all (`docs/deployment.md` §10.8).
- No true mid-pipeline resume (interrupted executions are honestly
  `failed`; re-execution starts fresh).
- Retention reaps superseded/orphan bytes only; current deliverable
  bytes are kept forever by policy.
- Admin permission is granted via the in-process security kernel (no
  env-based admin bootstrap); rate limiting and OS-level sandboxing are
  out of scope for V1.

[0.1.0]: https://example.invalid/raivisox — repository has no remote/tag convention; see repository metadata.
