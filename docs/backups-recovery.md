# RAIVISOX AI — Backups & Recovery

Three durable state stores exist; back up all three to fully recover a
production deployment.

| Store | Location (compose) | Contents |
|---|---|---|
| PostgreSQL | `postgres_data` volume | Users, projects, tasks, steps, events, execution runs, artifact registry, audit trail |
| Artifact files | `${RAIVISOX_WORKSPACE_DIR:-./data/workspaces}` (host dir, `/app/workspaces`) | Bytes of every registered artifact |
| Environment config | `.env` on the host | Secrets and provider configuration |

The artifact **registry** (DB) and artifact **files** (volume) must stay
consistent: a row without its file is reported honestly as `MISSING.txt`
in bundles, and files without rows are invisible to the API — back them
up together.

## 1. Database backup (PostgreSQL)

```bash
# Hot backup (safe while running — pg_dump takes a consistent snapshot)
docker exec raivisox-postgres pg_dump -U raivisox -Fc raivisox \
  > raivisox_$(date +%Y%m%d_%H%M%S).dump
```

## 2. Database restore

```bash
# Recreate and restore. STOP the API first (restoring under a live
# writer can interleave with in-flight transactions):
docker compose -f docker-compose.prod.yml stop api
docker exec -i raivisox-postgres pg_restore -U raivisox -d raivisox \
  --clean --if-exists < raivisox_YYYYMMDD_HHMMSS.dump
docker compose -f docker-compose.prod.yml start api
```

Verify: `GET /health/ready` → `200`, then log in and check task/run
history.

## 3. Artifact storage backup

The workspace is a **host directory**, not a named volume: the sandbox
container is started through the `sandbox-worker` runtime socket, so the
bind source must be the same absolute path on the host, in the worker and
in the API (docs/deployment.md §10.7).

```bash
# Backup (files + nothing else lives here)
tar czf workspaces_$(date +%Y%m%d).tar.gz -C "${RAIVISOX_WORKSPACE_DIR:-./data/workspaces}" .

# Restore (API and worker stopped — nothing may write during the copy)
docker compose -f docker-compose.prod.yml stop api sandbox-worker
rm -rf "${RAIVISOX_WORKSPACE_DIR:-./data/workspaces}"/*
tar xzf workspaces_YYYYMMDD.tar.gz -C "${RAIVISOX_WORKSPACE_DIR:-./data/workspaces}"
docker compose -f docker-compose.prod.yml start sandbox-worker api
```

**Consistency rule:** restore the database dump and the workspace
archive from the same backup window when possible. Mismatched pairs
degrade gracefully (missing files → `MISSING.txt`; orphaned files →
invisible) but are confusing to operate.

## 4. Environment configuration recovery

`.env` contains `SECRET_KEY`, database credentials and provider keys.
Losing or rotating it has specific consequences:

- **Lost `.env`:** recreate from `.env.example`; generate a new
  `SECRET_KEY` (`openssl rand -hex 32`). All existing JWTs become
  invalid — every user must log in again (no data loss).
- **Rotated `SECRET_KEY`:** same effect, by design. Rotate immediately
  if a leak is suspected; nothing else stores the key.
- **Rotated `POSTGRES_PASSWORD`:** update the PostgreSQL container
  (`ALTER USER raivisox WITH PASSWORD '…'` inside psql) **and** `.env`
  `DATABASE_URL`, then restart the API.
- Keep an encrypted off-site copy of `.env`; it is the only file that
  cannot be derived from the others.

## 5. Full-disaster recovery order

1. `docker compose -f docker-compose.prod.yml up -d postgres`
2. Restore the database dump (§2).
3. Restore the workspace volume (§3).
4. Restore `.env` (§4).
5. `docker compose -f docker-compose.prod.yml up -d --build`
6. Verify: `/health/live` → 200, `/health/ready` → 200, login, open a
   task, download an artifact, open project export.

On first start after recovery the startup sweep closes any execution_run
that was left `running` by the disaster (they are marked failed with
their interruption stage) — recovery never fabricates a resumed run.

## 6. Local development databases

Dev/test SQLite files (`raivisox*.db` in the repo root) are disposable;
delete freely — the next start recreates them via `create_all`.

## 7. Artifact retention & cleanup operations

Retention (`ARTIFACT_RETENTION_DAYS`) interacts with backups in one
direction only: **cleanup never touches registry metadata** — rows,
stable artifact IDs, hashes and ExecutionRun history are permanent, so
the database backup is unaffected by retention. Only superseded history
bytes older than the window (and orphan workspace files) are deleted.

**Backup implications:**

- With retention `0` (default): workspace volume grows without bound —
  schedule storage monitoring; every backup contains every byte ever
  registered.
- With retention `N > 0`: superseded bytes vanish after ~N days, so the
  workspace volume self-prunes — but a backup only retains history that
  still exists at backup time. If long-term byte retention matters,
  back up the volume MORE often than the retention window (e.g. daily
  backups with a 7-day window keep every byte that ever existed, until
  the backup itself is pruned). Registry rows in the DB backup remain
  queryable regardless; restored rows whose bytes were reaped download
  as honest `404 missing` — never fabricated.

**Manual cleanup procedure (ops):**

```bash
# One retention pass, admin-authenticated (same policy as the worker):
curl -X POST "https://<api-host>/api/v1/admin/artifacts/cleanup" \
  -H "Authorization: Bearer <admin-token>"
# -> {"stats": {"deleted": …, "bytes_freed": …, "orphans_deleted": …,
#               "errors": …, "retention_disabled": …, …counts only…}}
```

Works with retention enabled (forces a pass between intervals) and at
retention 0 (honest no-op: `"retention_disabled": true`). Non-admin
callers get 403 (audit-logged); anonymous 401.

**Production scheduling recommendation:** the built-in worker (one per
API process, `ARTIFACT_CLEANUP_INTERVAL_SECONDS`, default 6 h) is the
intended mechanism and is safe with multiple replicas — passes are
idempotent and race-safe against registration. For stronger "run at
02:00" semantics, run replicas with retention 0 and drive the admin
endpoint from your scheduler (cron / k8s CronJob) instead. Do not run
cleanup while a database restore is in progress (same rule as §2).

**Failure/recovery behavior:** a failed pass logs counts and retries at
the next interval; the API is never crashed or blocked by cleanup. If a
pass repeatedly fails (e.g. a permission problem on the volume), the
only consequence is that bytes accumulate until fixed — metadata and
the API stay correct. The pass log line contains counters only; grep
`artifact cleanup pass` in the API logs to observe it.
