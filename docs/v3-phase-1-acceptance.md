# V3 Phase 1 — acceptance report

Specification: [v3-phase-1-spec.md](v3-phase-1-spec.md). Design:
[v3-agent-action-graph.md](v3-agent-action-graph.md).

Phase 1 is the **provenance-aware event layer only**: an append-only
`graph_events` table, one `audit_links` side table, a typed writer with trusted
identity resolution, and instrumentation at the five existing choke points.
No projection, graph store, blast-radius engine, policy engine, approval flow,
or graph UI exists in this change.

| Area | Result |
|---|---|
| Schema + migration `003` (SQLite) | **PASS** |
| Emission (tool lifecycle, fs_read, fs_write, http_get, artifact) | **PASS** |
| Trusted identity / anti-spoof | **PASS** |
| Audit linkage (gap 1) | **PASS** |
| Successful + denied `fs_read` auditing (gap 2) | **PASS** |
| Duplicates, ordering, `seq`/cursor replay protection | **PASS** |
| Failure semantics (flag-off, missing table, write error) | **PASS** |
| Retry / rollback / restart persistence | **PASS** |
| Legacy audit compatibility (`audit_events` untouched) | **PASS** |
| PostgreSQL migration, events, audit links (T34, T34b–T34i) | **PASS** — real PostgreSQL 16.9; see §7 |
| Live Linux container gate | **CI only** — no Docker/WSL on the dev host; see §7 |
| Flag default | `V3_EVENTS_ENABLED` **off**, unchanged |

## 1. What ships

New:

- `services/graph/schema.py` — closed event/outcome/evidence sets, per-type
  payload allowlists, normalizers, redaction, deterministic idempotency keys.
- `services/graph/actions.py` — bounded per-process registry of live
  `action_id`s (TTL 300 s, pruned on every touch).
- `services/graph/provenance.py` — `verify_subject` / `resolve_run_provenance`
  against `tasks` and `execution_runs`; `ProvenanceError` on mismatch.
- `services/graph/events.py` — `emit()` (append-only writer), `read_events()`
  (`seq`-ordered cursor read that refuses unknown schema versions), counters,
  failure classification, retry policy, process-level disable on missing table.
- `apps/api/app/models/graph_event.py`, `apps/api/app/models/audit_link.py`.
- `apps/api/alembic/versions/003_graph_events_audit_links.py`.
- `tests/unit/{test_graph_events,test_provenance,test_audit_linkage,test_fs_read_instrumentation,test_endpoint_instrumentation,test_workspace_quota_integration,test_artifact_events,test_migration_003,test_postgres_phase1}.py`
  plus `tests/unit/v3_phase1_helpers.py`.
- This report.

Modified (all additive, defaults preserve existing behaviour):

- `integrations/adapters/base.py` — typed `AdapterContext.user_id`,
  `execution_run_id`, `action_id` (not `ctx.extra`).
- `integrations/adapters/registry.py` — lifecycle events around the unchanged
  kernel gate; issues the per-invocation `action_id`.
- `integrations/adapters/builtin.py` — `fs_read` success/denied/failed +
  success audit entry; `fs_write` committed/denied + success audit entry.
- `integrations/adapters/http_get.py` — endpoint attempted/succeeded/denied.
- `services/security/security_kernel.py` — `SecurityDecision.request_id`
  exposed; `authorize_and_log` accepts provenance keywords as **evidence only**
  (they never reach `authorize()`).
- `services/security/audit_trail.py` — atomic audit row + link row, dequeue
  only after commit, `read_from_db` LEFT JOINs the link.
- `apps/api/app/main.py` — per-run provenance resolution, `AdapterContext`
  wiring, and the single `verified` event after artifact commit.
- `docs/v3-phase-1-spec.md`, `docs/v3-agent-action-graph.md` — implementation
  notes and status.

## 2. Schema and migration

Migration `003` creates two tables and alters nothing:

`graph_events` — `seq` INTEGER PRIMARY KEY AUTOINCREMENT (SQLite) /
bigserial-class sequence (PostgreSQL) with `sqlite_autoincrement=True`;
`event_id` unique; `schema_version`; `event_type`; `occurred_at` /
`recorded_at`; `actor_id` / `actor_type`; `user_id` (NOT NULL, isolation key);
`task_id` (NOT NULL, FK `tasks.id` ON DELETE CASCADE); nullable `run_id`,
`step_node_id`, `tool_id`, `action_id`; `target_kind` / `target_id`;
`outcome`; `evidence_class`; `source`; nullable `correlation_id`;
`idempotency_key` unique; `payload` JSON; `payload_hash`. Indexes:
`(user_id, seq)`, `(task_id, seq)`, `(run_id, seq)`, `(action_id)`,
`(event_type)`.

`audit_links` — `event_id` PK + FK `audit_events.event_id` ON DELETE CASCADE,
nullable `user_id`, `task_id`, `run_id`, `step_node_id`, `action_id`, each
indexed. Nullable by design: a legacy row has **no** link row at all, and an
audit action with no task subject stores `NULL` rather than a guess.

`audit_events` is byte-identical to V2: no columns added, no `ALTER` executed,
no backfill. `downgrade()` drops only the two new tables (and their indexes).

`AUTOINCREMENT` is what makes cursor/replay protection real: a deleted or
rolled-back `seq` is never reissued, so a consumer that persists
`after_seq = N` can never skip or double-read evidence (T8b).

## 3. Event writing and provenance

`emit()` is keyword-only and explicit — there is no `**kwargs` path through
which an untrusted caller could smuggle an identity. Order of checks: flag →
event type → outcome/evidence class/actor → identity presence → identifier
format → `action_id` liveness → source string → payload allowlist + redaction →
target normalization → idempotency key → **one** ownership query → insert.

- **Missing identity** (`user_id`/`task_id` empty) ⇒ no row,
  `skipped_no_provenance` counter. Nothing is attributed to "someone".
- **Mismatched/forged identity** (`user_id` not `tasks.user_id`, `run_id` not
  an `execution_runs` row of that task, unknown `action_id`) ⇒
  `ProvenanceError`, no row. This is the only exception `emit()` raises into a
  pipeline, and it is an integrity fault, not a runtime condition.
- **Duplicate delivery** ⇒ deterministic key `"{event_type}:{action_id}"`
  (artifacts: `"artifact.registered:{artifact_id}"`); the unique constraint
  makes a retry a no-op (`duplicates` counter), while a second execution has a
  new `action_id` and is correctly a distinct fact.
- **Ordering** is `seq` only — never a timestamp. `occurred_at` is clamped so
  `recorded_at >= occurred_at`.
- **Failure semantics**: missing table disables recording for the process
  once; transient lock retries 3× with 50/200 ms backoff; any other write error
  returns `False` and increments `failures`. No event write can fail a task.
- **No content**: payloads pass an allowlist per event type, secret-shaped
  values (`sk-…`, `ghp_…`, long opaque tokens) are refused rather than
  rewritten, payloads are capped at 8 KiB, and events store scope-relative
  paths only. Adapter-level events additionally require a registry-issued
  `action_id`, so a compromised adapter cannot invent an invocation.

`AdapterContext` carries the identity (typed fields, set by the pipeline from
`run_provenance`); `authorize_and_log` echoes it onto `SecurityDecision` and
`AuditEvent`, where it becomes evidence only. The kernel's decision path is
untouched — no V3 code reads events to decide anything.

## 4. Audit linkage and filesystem-read instrumentation

**Gap 1 (audit linkage).** `AuditTrail.log()` accepts optional
`user_id/task_id/run_id/step_node_id/action_id`. `flush_pending()` writes the
`audit_events` row and, when linkage exists, its `audit_links` row in the same
transaction; the queue is drained only after a successful commit, so a failure
rolls back both and leaves the events pending for the next flush. `read_from_db`
LEFT JOINs the link, so V2 rows return `NULL` for the five keys and every
existing key is unchanged.

**Gap 2 (fs_read).** A successful `fs_read` now writes an audit entry
(`allowed=True`) **and** `resource.read.observed` (`executed`, scope-relative
`relpath`, `size`, `target_id="res:<task>/<rel>"`). An out-of-workspace read
writes `resource.read.denied` with `reason=outside_workspace` and only the
basename — no absolute path, no content. A missing/unreadable file is
`resource.read.observed` / `failed` with `error_kind`, never `denied` (a tool
error is not a policy refusal). `fs_write` gets the symmetric treatment
(`resource.write.committed` observed, `resource.write.denied` for
outside-workspace and quota refusals, plus D11's success audit entry).

Per spec §3.1/T10 and D11's implementation note, the **new success** audit
entries are written only while `V3_EVENTS_ENABLED` is on, so the flag-off audit
stream stays byte-identical to V2 and existing suites that count
`tool:fs_read`/`tool:fs_write` rows are unaffected.

**Still unobservable (not papered over):** I/O performed by sandboxed child
processes (a `python_exec` reading a file directly), header/body contents,
resolved IPs, and multi-replica clock skew. Phase 1 records only what the
application boundary actually sees.

## 5. Acceptance matrix

Local run, `V3_EVENTS_ENABLED` set per test, file-backed SQLite. Full
per-file evidence in §6.

| ID | Test | Result |
|---|---|---|
| T1 | `test_graph_events.py::test_tool_lifecycle_records_attempt_allow_execute` | PASS |
| T1b | `test_graph_events.py::test_permissionless_adapter_has_no_allowed_event` | PASS |
| T2 | `test_graph_events.py::test_denied_tool_has_no_executed_sibling` | PASS |
| T3 | `test_graph_events.py::test_adapter_failure_records_failed` | PASS |
| T4 | `test_graph_events.py::test_payload_allowlist_drops_unknown_keys` | PASS |
| T5 | `test_graph_events.py::test_secret_like_value_rejects_event` | PASS |
| T6 | `test_graph_events.py::test_oversized_payload_rejected` | PASS |
| T7 | `test_graph_events.py::test_duplicate_idempotency_key_is_a_no_op` | PASS |
| T8 | `test_graph_events.py::test_seq_gap_is_preserved_and_ordering_uses_seq` | PASS |
| T8b | `test_graph_events.py::test_seq_is_never_reused_after_delete` | PASS |
| T9 | `test_graph_events.py::test_events_survive_restart` | PASS |
| T10 | `test_graph_events.py::test_flag_off_writes_nothing` | PASS |
| T10b | `test_graph_events.py::test_missing_table_is_non_fatal` | PASS |
| T10c | `test_graph_events.py::test_write_error_does_not_raise_into_pipeline` | PASS |
| T11 | `test_graph_events.py::test_concurrent_emits_do_not_lose_rows` | PASS |
| T12 | `test_graph_events.py::test_unknown_schema_version_is_refused_by_reader` | PASS |
| T13 | `test_provenance.py::test_forged_user_id_is_refused` | PASS |
| T13b | `test_provenance.py::test_task_id_must_match_provenance` | PASS |
| T14 | `test_provenance.py::test_run_belonging_to_another_task_is_refused` | PASS |
| T15 | `test_provenance.py::test_adapter_without_provenance_writes_no_events` | PASS |
| T15b | `test_graph_events.py::test_writer_only_inserts` | PASS |
| T16 | `test_provenance.py::test_unknown_action_id_is_rejected` | PASS |
| T17 | `test_provenance.py::test_identity_fields_are_not_read_from_params` | PASS |
| T18 | `test_audit_linkage.py::test_audit_row_carries_user_task_run_action` | PASS |
| T18b | `test_audit_linkage.py::test_link_and_audit_row_are_atomic` | PASS |
| T19 | `test_audit_linkage.py::test_legacy_audit_rows_remain_readable` | PASS |
| T19b | `test_audit_linkage.py::test_audit_events_schema_is_unchanged` | PASS |
| T20 | `test_audit_linkage.py::test_audit_api_shape_is_additive` | PASS |
| T21 | `test_fs_read_instrumentation.py::test_successful_read_emits_observed_and_audits` | PASS |
| T22 | `test_fs_read_instrumentation.py::test_denied_read_emits_denied_and_no_content` | PASS |
| T23 | `test_fs_read_instrumentation.py::test_missing_file_is_failed_not_denied` | PASS |
| T24 | `test_fs_read_instrumentation.py::test_read_payload_never_contains_file_content` | PASS |
| T25 | `test_endpoint_instrumentation.py::test_allowed_endpoint_records_host_and_path_only` | PASS |
| T26 | `test_endpoint_instrumentation.py::test_refused_endpoint_records_denied_before_any_byte` | PASS |
| T27 | `test_endpoint_instrumentation.py::test_query_string_is_not_stored` | PASS |
| T28 | `test_workspace_quota_integration.py::test_write_denial_emits_resource_write_denied` | PASS |
| T29 | `test_workspace_quota_integration.py::test_write_commit_is_observed_not_verified` | PASS |
| T30 | `test_artifact_events.py::test_artifact_registered_is_verified_with_hash` | PASS |
| T31 | `test_artifact_events.py::test_artifact_event_absent_when_registration_fails` | PASS |
| T32 | `test_migration_003.py::test_upgrade_downgrade_round_trip_sqlite` | PASS |
| T33 | `test_migration_003.py::test_existing_audit_rows_survive_upgrade` | PASS |
| T34 | `test_migration_003.py::test_postgres_migration_and_events` | **PASS** — PostgreSQL 16.9 |
| T34b | `test_postgres_phase1.py::test_postgres_migration_creates_v3_schema` | PASS — both tables, unique keys, all indexes, both `ON DELETE CASCADE` rules, `seq` default `nextval(...)` |
| T34c | `test_postgres_phase1.py::test_postgres_upgrade_downgrade_round_trip` | PASS — `upgrade → downgrade 002 → upgrade`; `audit_events` columns identical throughout |
| T34d | `test_postgres_phase1.py::test_postgres_emit_persists_and_deduplicates` | PASS — append, duplicate no-op, forged owner refused (`ProvenanceError`, no row) |
| T34e | `test_postgres_phase1.py::test_postgres_audit_link_is_atomic` | PASS — failed link insert leaves 0 audit rows, 0 links, queue intact; retry writes both |
| T34f | `test_postgres_phase1.py::test_postgres_foreign_keys_are_enforced` | PASS — orphan link refused; task delete cascades events; audit delete cascades links |
| T34g | `test_postgres_phase1.py::test_postgres_legacy_audit_rows_survive_upgrade` | PASS — pre-V3 row byte-identical after upgrade, no link row, reads as `NULL` provenance |
| T34h | `test_postgres_phase1.py::test_postgres_restart_preserves_events_and_seq` | PASS — rows survive a restart in `seq` order; a deleted `seq` is never reissued |
| T34i | `test_postgres_phase1.py::test_postgres_registry_read_records_event_and_link` | PASS — a real `fs_read` run produces the event, the audit row and its link |
| T35 | `test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema` (existing) | PASS |
| T36 | Existing suites (`tests/unit/**`) | PASS — see §6 |
| — | `test_graph_events.py::test_emit_cost_is_bounded` (non-functional) | PASS |
| — | `test_audit_linkage.py::test_kernel_call_sites_still_work_without_new_kwargs` | PASS |
| — | `test_audit_linkage.py::test_links_never_contain_content_or_secret` | PASS |

**44 passed, 1 skipped, 0 failed** across the eight Phase 1 files without a
PostgreSQL URL. With `RAIVISOX_TEST_POSTGRES_URL` set the same selection plus
`test_postgres_phase1.py` is **53 passed, 0 skipped, 0 failed** — the nine
PostgreSQL-gated tests run instead of skipping.

## 6. Verification runs

Exact commands and counts on the development host (Windows, Python 3.12.2,
pytest 9.1.1):

| Run | Command | Result |
|---|---|---|
| Phase 1 focused | `python -m pytest tests/unit/test_graph_events.py tests/unit/test_provenance.py tests/unit/test_audit_linkage.py tests/unit/test_fs_read_instrumentation.py tests/unit/test_endpoint_instrumentation.py tests/unit/test_workspace_quota_integration.py tests/unit/test_artifact_events.py tests/unit/test_migration_003.py -q` | 44 passed, 1 skipped |
| Phase 1 focused, with PostgreSQL | same selection plus `tests/unit/test_postgres_phase1.py`, `RAIVISOX_TEST_POSTGRES_URL` set | **53 passed, 0 skipped** |
| PostgreSQL migration + PG gate | `RAIVISOX_TEST_POSTGRES_URL=... python -m pytest tests/unit/test_migration_003.py tests/unit/test_postgres_phase1.py -q` | **11 passed, 0 skipped** |
| Full backend | `python -m pytest tests/unit -q` | **619 passed, 56 skipped, 0 failed** (675 collected, 727 s; see §7 for the PG run) |
| Container gate, non-live half | `python -m pytest tests/unit/test_sandbox_phase4.py tests/unit/test_container_app_path.py tests/unit/test_sandbox_phase5.py -m "not live" -q` | 62 passed, 5 skipped, 19 deselected |
| Frontend typecheck | `cd apps/web && npx tsc --noEmit` | exit 0, no diagnostics |
| Frontend production build | `cd apps/web && npm run build` (`next build`, Next 15.1.6) | exit 0, 6 routes generated |
| Full backend (baseline, pre-change, for comparison) | same suite at `014fcd6` | 618 passed, 49 skipped |

Skips are pre-existing environment skips (Windows-only variants, no Ollama, no
Docker) plus the nine PostgreSQL-gated tests (T34, T34b–T34i) when no PG URL is
supplied. The PostgreSQL run that executes them is in §7.

Linux CI (`ubuntu-latest`) for the same commit, from the run's logs:

| Job / step | Result |
|---|---|
| `backend-tests` — full suite (`python -m pytest -q`) | **638 passed, 33 skipped, 0 failed** (671 collected, 99 s) |
| `backend-tests` — explicit `-m "not live_llm"` selection | 638 passed, 29 skipped, 4 deselected |
| `container-gate` — docker runtime proof | Docker `server=28.0.4 client=28.0.4`; digest-pinned image ran |
| `container-gate` — live container tests (`-m live`) | **19 passed, 0 skipped** (a skip fails the gate) |
| `container-gate` — non-live half | 64 passed, 3 skipped, 19 deselected |
| `frontend-validation` — `tsc --noEmit` + `npm run build` | passed, build compiled successfully |

The Linux job collects 4 more items than the Windows host (671 vs 667) and skips
16 fewer, because several sandbox/container tests are POSIX-only; no test that
passes locally fails there.

## 7. Container gate and PostgreSQL

- **Live Linux container gate (CI job `container-gate`).** The dev host has no
  Docker CLI, no podman and no WSL, so the gate cannot run locally; the non-live
  half it runs was executed locally (§6). The real gate ran in GitHub Actions on
  push and **passed**: run `39554959798`, job `container-gate`, Docker
  `28.0.4`, 19 live container tests passed and **zero skipped** (the job fails
  on any skip), 64 passed / 3 skipped in the non-live half. This is the
  authoritative Linux container evidence.
- **PostgreSQL (verified).** The PostgreSQL path was verified against a real
  server: PostgreSQL **16.9**, the same major version as the `postgres:16-alpine`
  service the compose files ship. The instance was a throwaway cluster reachable
  only on `127.0.0.1`, with trust auth scoped to that loopback address; no
  credential was written to the repository, the URL was exported only into the
  test process environment, and the cluster was stopped and deleted after the
  run.

  | Run | Result |
  |---|---|
  | `test_migration_003.py` (T32–T34) with the PG URL | **3 passed, 0 skipped** |
  | `test_postgres_phase1.py` (T34b–T34i) | **8 passed, 0 skipped** |
  | Phase 1 focused suite with the PG URL (§5) | **53 passed, 0 skipped** |
  | Full backend suite without the PG URL (§6) | **619 passed, 56 skipped, 0 failed** |

  What the PostgreSQL run proves, in the server's own terms: migration `003`
  creates both tables with their unique keys, all nine indexes, both
  `ON DELETE CASCADE` rules and a `nextval(...)` default for `seq`;
  `upgrade → downgrade 002 → upgrade` round-trips and never alters
  `audit_events`; the writer appends, deduplicates by `idempotency_key` and
  refuses a forged owner with `ProvenanceError` and no row; a failed
  `audit_links` insert leaves neither the audit row nor the link and keeps the
  event queued for retry; PostgreSQL enforces the foreign keys (an orphan link is
  refused, deleting a task cascades its events, deleting an audit row cascades
  its link); a pre-V3 audit row is byte-identical after the upgrade and reads as
  `NULL` provenance; a restart keeps every row in `seq` order and a deleted
  `seq` is never reissued; and a real `fs_read` adapter run produces the event,
  the audit row and its link.

  Reproduce (PostgreSQL 13+, role needs `CREATEDB`: the tests create and drop
  their own throwaway databases; T34 additionally migrates the database named in
  the URL):

  ```bash
  createdb -h 127.0.0.1 -U "$PGUSER" raivisox_v3_test
  RAIVISOX_TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/raivisox_v3_test \
    python -m pytest tests/unit/test_migration_003.py tests/unit/test_postgres_phase1.py -q
  ```

  Without the variable, all nine PostgreSQL-gated tests skip with an explicit
  reason and the report says PostgreSQL is unverified — a skip is never a pass.
  The CI workflow now runs this same gate against a `postgres:16-alpine` service
  in the `postgres-gate` job (result in §11); that job fails if any of the nine
  tests skips, so a future PG regression cannot pass CI silently (R11). Nothing
  in the suite claims a PG guarantee from SQLite evidence.
- **Driver.** T34 previously inspected the migrated schema through
  `postgresql+psycopg2`, but `psycopg2` is not a declared dependency of this
  project (only `asyncpg` is). It now inspects with asyncpg — the assertions are
  unchanged — so the PG gate does not silently require an undeclared package.

## 8. Security review of the diff

| Question | Finding |
|---|---|
| Authorization regressions | None. The kernel gate's inputs, ordering and short-circuit are unchanged; V3 calls are additive and never gate a return path. T2 asserts a denial is never recorded as an execution. |
| Provenance spoofing | Closed. Identity comes from `tasks.user_id` + an `execution_runs` row of the same task, verified per event; `action_id` must be live in the process registry; params are ignored (T13–T17). |
| Cross-user leakage | Closed by the same checks: events and links cannot name another user's task or run; scope-relative paths only; `target_id` is `"res:<task>/<rel>"`. |
| Secrets/content in events | Payload allowlists, secret-shaped-value refusal, 8 KiB cap, no headers/query strings/body, no file content (T4, T5, T22, T24, T27). |
| Transaction boundaries | Audit row + link commit together, dequeue after commit (T18b); each event is its own short transaction; no partial event exists (all fields validated before the insert). |
| Migration compatibility | Additive only; `audit_events` untouched (T19b); upgrade → downgrade → upgrade round-trips (T32); pre-existing rows preserved (T33); zero-drift test passes (T35). |
| Unsafe execution paths | None added. No new shell usage, no unsandboxed fallback, no adapter body change beyond wrappers. |
| Sandbox/quota controls | Unchanged control flow; quota denial now also emits an event and is exercised against the real enforcer (T28, T29). |
| Observation can change execution | No: recording failures are swallowed and counted; only a provenance integrity fault propagates (T10b, T10c). |
| Authority added | No. The only consumers of `graph_events` are the writer and tests — nothing reads events to decide anything (DoD 12). |
| Flag default | Off. No production config, retention setting, or enablement was changed. |

## 9. Owner gates (unchanged, not approved here)

| ID | Gate | Status | Safe default implemented |
|---|---|---|---|
| G1 | Enabling `V3_EVENTS_ENABLED` in a real deployment | **OWNER INPUT REQUIRED** | stays off; recommended off until Phase 2 ships retention and a read path |
| G2 | Retention value vs `ARTIFACT_RETENTION_DAYS` | **OWNER INPUT REQUIRED** (Phase 2) | no retention implemented in Phase 1 |
| G3 | Whether real users may hold `ADMIN` | **OWNER INPUT REQUIRED** (Phase 5/6) | no admin surface and no new route added |

Nothing here is a human approval of those gates.

## 10. Limits and rollback

- PostgreSQL is verified on 16.9 by the opt-in run above and by the CI
  `postgres-gate` job (§11), but only on that major version: no PG
  concurrency/lock-contention or multi-version run was performed. SQLite
  remains the dialect every other test uses.
- Child-process I/O, header/body contents, resolved IPs, clock skew: out of
  scope by design and not claimed.
- Rollback is `alembic downgrade 002`, which drops the two new tables and
  leaves `audit_events` and every V2 path untouched. Disabling the flag stops
  all new writes immediately; existing rows simply remain.
- Phase 2 (projection, graph store, blast radius, policy, approval, UI) is not
  started.

## 11. CI and delivery

GitHub Actions run **39554959798** for commit `f30e6a4` — **success**, all three
jobs green: `Backend tests (pytest)` (638 passed, 33 skipped, 0 failed),
`Container sandbox gate (live Docker)` (19 live passed, 0 skipped; 64 passed in
the non-live half), `Frontend (typecheck + build)`.

- Implementation commit: `f30e6a4` (`feat(v3): record provenance-aware events
  and audit links behind a flag`), pushed to `origin/main`.
- `v1.0.0` untouched; no release tag created.
- Working tree clean after the documentation commit that records this report.

## 12. Verdict

`V3 PHASE 1: PASSED` — every Phase 1 acceptance item is now evidenced, including
the PostgreSQL path (T34, T34b–T34i on a real PostgreSQL 16.9 server, and the CI
`postgres-gate` job that keeps it verified). The three owner gates G1–G3 remain
open, and Phase 2 has not been started.
