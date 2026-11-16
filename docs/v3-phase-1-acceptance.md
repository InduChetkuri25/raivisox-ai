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
| PostgreSQL migration, events, audit links, retries (T34, T34b–T34y) | **PASS** — real PostgreSQL 16.2/16.9; see §7 |
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
  once; only *concurrency conflicts* are retried — PostgreSQL SQLSTATE class
  `40` (`40P01` deadlock, `40001` serialization failure) and `55P03`, or
  SQLite's lock message — with at most three attempts (50/200 ms backoff),
  each in a fresh transaction; any other write error returns `False` on the
  first attempt and increments `failures`. No event write can fail a task.
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
| T34j | `test_postgres_phase1.py::test_postgres_deadlock_is_classified_transient` | PASS — the server aborts a real deadlock with `40P01`; the classifier returns transient |
| T34k | `test_postgres_phase1.py::test_postgres_serialization_failure_is_classified_transient` | PASS — a real SERIALIZABLE conflict surfaces as `40001` and is transient |
| T34l | `test_postgres_phase1.py::test_postgres_writer_retries_a_real_deadlock` | PASS — server-raised `40P01` on attempt 1, one row and one retry; re-emit is a duplicate |
| T34m | `test_postgres_phase1.py::test_postgres_writer_gives_up_after_bounded_retries` | PASS — 3 attempts, 2 retries, 1 failure, 0 rows; recording stays enabled |
| T34n | `test_postgres_phase1.py::test_postgres_writer_does_not_retry_permanent_error` | PASS — a real `22001` is not retried (0 retries) and writes nothing |
| T34o | `test_postgres_phase1.py::test_postgres_audit_flush_retry_cannot_duplicate` | PASS — a real `40P01` at commit persists nothing and keeps the queue; the retry writes 2 audit rows + 1 link once; a third flush writes 0 |
| T34p | `test_postgres_phase1.py::test_postgres_lock_timeout_is_classified_transient` | PASS — a `lock_timeout` expiring against a real `ACCESS EXCLUSIVE` lock returns SQLSTATE `55P03`, classified transient and not treated as a missing table |
| T34q | `test_postgres_phase1.py::test_postgres_writer_gives_up_after_real_lock_timeouts` | PASS — the writer meets a real lock on `graph_events` three times: 2 retries, 1 failure, 0 rows; after release the same action writes exactly once and re-emits as a duplicate |
| T34r | `test_postgres_phase1.py::test_postgres_writer_retry_commits_once_when_the_lock_clears` | PASS — one real `55P03`, one fresh transaction, one row, no failure, no duplicate |
| T34s | `test_postgres_phase1.py::test_postgres_audit_flush_survives_a_real_lock_timeout` | PASS — a real `55P03` on the link insert persists neither the audit row nor its link and keeps both events queued; the retry writes 2 audit rows + 1 link once; a third flush writes 0 |
| T34t | `test_postgres_phase1.py::test_postgres_in_failed_transaction_is_not_transient` | PASS — a real `23505` aborts the transaction and the next statement returns `25P02`; both are non-transient, and the abort effect is not read as a missing table |
| T34u | `test_postgres_phase1.py::test_postgres_writer_does_not_retry_in_failed_transaction` | PASS — the real `25P02` at commit gives 0 retries, 1 failure, 0 rows; recording stays enabled and the next emit succeeds |
| T34v | `test_postgres_phase1.py::test_postgres_aborted_session_is_not_reused` | PASS — a real `22012` leaves the session's transaction dead (`25P02` on the next statement); a fresh session from the same factory is clean and emits one row with 0 retries |
| T34w | `test_postgres_phase1.py::test_postgres_audit_flush_keeps_queue_after_in_failed_transaction` | PASS — the real `25P02` at the flush commit persists nothing and keeps both events queued; a clean flush writes 2 audit rows + 1 link once; a third flush writes 0 |
| T34x | `test_postgres_phase1.py::test_postgres_serialization_chain_keeps_the_cause_retryable` | PASS — a real `40001` aborts the transaction and the next statement returns `25P02`; the cause is transient, the effect is not |
| T34y | `test_postgres_phase1.py::test_postgres_writer_retries_chain_cause_in_a_fresh_transaction` | PASS — the real `40001` from that chain is retried once in a fresh transaction: 1 row, 0 failures, and a repeat emit is a duplicate |
| T35 | `test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema` (existing) | PASS |
| T36 | Existing suites (`tests/unit/**`) | PASS — see §6 |
| T37 | `test_transient_retry.py` (34 tests) | PASS — SQLSTATE classification table, SQLite lock text, bounded retries, final-error propagation, cancellation, dedupe after retry, missing-table disable |
| — | `test_graph_events.py::test_emit_cost_is_bounded` (non-functional) | PASS |
| — | `test_audit_linkage.py::test_kernel_call_sites_still_work_without_new_kwargs` | PASS |
| — | `test_audit_linkage.py::test_links_never_contain_content_or_secret` | PASS |

**78 passed, 1 skipped, 0 failed** across the Phase 1 acceptance files plus
`test_transient_retry.py` without a PostgreSQL URL. With
`RAIVISOX_TEST_POSTGRES_URL` set the same selection plus
`test_postgres_phase1.py` is **103 passed, 0 skipped, 0 failed** — the
twenty-five PostgreSQL-gated tests (T34, T34b–T34y) run instead of skipping,
and T34j–T34y add the concurrency evidence: real deadlocks (`40P01`), real
serialization failures (`40001`), a real lock timeout (`55P03`) and a real
aborted-transaction error (`25P02`) that is refused rather than retried —
including one real `40001 → 25P02` chain where only the cause is retried.

## 6. Verification runs

Exact commands and counts on the development host (Windows, Python 3.12.2,
pytest 9.1.1):

| Run | Command | Result |
|---|---|---|
| Phase 1 focused | `python -m pytest tests/unit/test_graph_events.py tests/unit/test_transient_retry.py tests/unit/test_provenance.py tests/unit/test_audit_linkage.py tests/unit/test_fs_read_instrumentation.py tests/unit/test_endpoint_instrumentation.py tests/unit/test_workspace_quota_integration.py tests/unit/test_artifact_events.py tests/unit/test_migration_003.py -q` | **78 passed, 1 skipped, 0 failed** |
| Focused transient-retry tests | `python -m pytest tests/unit/test_transient_retry.py -q` | **34 passed, 0 failed** |
| Phase 1 focused, with PostgreSQL | same selection plus `tests/unit/test_postgres_phase1.py`, `RAIVISOX_TEST_POSTGRES_URL` set | **103 passed, 0 skipped, 0 failed** |
| PostgreSQL migration + PG gate | `RAIVISOX_TEST_POSTGRES_URL=... python -m pytest tests/unit/test_migration_003.py tests/unit/test_postgres_phase1.py -q` | **27 passed, 0 skipped** |
| Full backend | `python -m pytest tests/unit -q` | **653 passed, 72 skipped, 0 failed** (725 collected; see §7 for the PG run) |
| Full backend, every test directory, with PostgreSQL | `RAIVISOX_TEST_POSTGRES_URL=... python -m pytest tests -q` | **678 passed, 51 skipped, 0 failed** (729 collected, 860 s) |
| Full backend, every test directory | `python -m pytest tests -q` | **653 passed, 76 skipped, 0 failed** (729 collected, 434 s) |
| Container gate, non-live half | `python -m pytest tests/unit/test_sandbox_phase4.py tests/unit/test_container_app_path.py tests/unit/test_sandbox_phase5.py -m "not live" -q` | 62 passed, 5 skipped, 19 deselected |
| Frontend typecheck | `cd apps/web && npx tsc --noEmit` | exit 0, no diagnostics |
| Frontend production build | `cd apps/web && npm run build` (`next build`, Next 15.1.6) | exit 0, 6 routes generated |
| Full backend (baseline, pre-change, for comparison) | same suite at `014fcd6` | 618 passed, 49 skipped |

Skips are pre-existing environment skips (Windows-only variants, no Ollama, no
Docker) plus the twenty-five PostgreSQL-gated tests (T34, T34b–T34y) when no PG
URL is supplied. The PostgreSQL run that executes them is in §7.

One pre-existing flake was seen and is reported rather than hidden:
`test_llm_dod.py::test_recovery_regenerates_wrong_llm_module` failed once
(`assert 2 == 1` on `recovery_attempts`) in the first `pytest tests` run of this
session and passed on the immediate rerun in the same tree (653 passed, 70
skipped); the file passes 3/3 standalone runs and passes in every `pytest
tests/unit` run. The test exercises the LLM repair loop, which this change does
not touch (the diff is confined to PostgreSQL retry tests and documentation);
it is recorded here as an intermittent test, not as a pass.

Linux CI (`ubuntu-latest`) for the same commit, from the run's logs:

| Job / step | Result |
|---|---|
| `backend-tests` — full suite (`python -m pytest -q`) | **672 passed, 57 skipped, 0 failed** (729 collected, 132 s) |
| `backend-tests` — explicit `-m "not live_llm"` selection | 672 passed, 53 skipped, 4 deselected |
| `postgres-gate` — PostgreSQL-gated tests against a `postgres:16-alpine` service | **27 passed, 0 skipped** (the job fails on any skip) |
| `container-gate` — docker runtime proof | Docker `server=28.0.4 client=28.0.4`; digest-pinned image ran |
| `container-gate` — live container tests (`-m live`) | **19 passed, 0 skipped** (a skip fails the gate) |
| `container-gate` — non-live half | 64 passed, 3 skipped, 19 deselected |
| `frontend-validation` — `tsc --noEmit` + `npm run build` | passed, build compiled successfully |

CI and the Windows host now collect the same 729 items (`pytest -q` in CI,
`pytest tests -q` locally). CI skips 57 and the host 76, because several
sandbox/container tests are POSIX-only; no test that passes locally fails there.
The SQLite backend job skips the twenty-five PostgreSQL-gated tests by design; the
separate `postgres-gate` job runs them.

## 7. Container gate and PostgreSQL

- **Live Linux container gate (CI job `container-gate`).** The dev host has no
  Docker CLI, no podman and no WSL, so the gate cannot run locally; the non-live
  half it runs was executed locally (§6). The real gate ran in GitHub Actions on
  push and **passed**: run `39554959798`, job `container-gate`, Docker
  `28.0.4`, 19 live container tests passed and **zero skipped** (the job fails
  on any skip), 64 passed / 3 skipped in the non-live half. This is the
  authoritative Linux container evidence.
- **PostgreSQL (verified).** The PostgreSQL path was verified against a real
  server. The lock-contention, aborted-transaction and error-chain evidence
  below (T34p–T34y) was produced on PostgreSQL **16.2**; the earlier Phase 1
  run (T34b–T34o) used **16.9**, and the whole gate — including T34x–T34y — was
  then repeated on a second, independently provisioned **16.9** server (16.9
  parity below). Both are the same major version as the `postgres:16-alpine`
  service the compose files ship. Each instance was a throwaway cluster reachable only on
  `127.0.0.1` with trust auth scoped to that loopback address; no credential was
  written to the repository, the URL was exported only into the test process
  environment, and the cluster was stopped and deleted after the run.

  | Run | Result |
  |---|---|
  | `test_migration_003.py` (T32–T34) with the PG URL | **3 passed, 0 skipped** |
  | `test_postgres_phase1.py` (T34b–T34y) | **24 passed, 0 skipped** on 16.2, and again **24 passed, 0 skipped** on 16.9 (parity below) |
  | Phase 1 focused suite with the PG URL (§5) | **103 passed, 0 skipped** |
  | Full backend suite without the PG URL (§6) | **653 passed, 72 skipped, 0 failed** |
  | Full backend suite with the PG URL (§6) | **678 passed, 51 skipped, 0 failed** |
  | CI `postgres-gate` (§11) | reported in §11 for the current commit |
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

  The same run now also pins the retry policy against the server itself
  (T34j–T34y): a deliberately provoked deadlock arrives as SQLSTATE `40P01` and
  a deliberately provoked SERIALIZABLE conflict as `40001`, both classified
  transient; a server-raised `40P01` on the first write attempt is retried and
  commits exactly one row; a persistently failing write stops after three
  attempts with one counted failure and no row; a real `22001`
  (string too long) is **not** retried; and a `40P01` at the audit flush
  persists nothing, keeps both events queued, and the retry writes 2 audit rows
  + 1 link exactly once with a third flush writing nothing.

  `55P03` (`lock_not_available`) is no longer an untested branch of the
  classifier. A second session holds `ACCESS EXCLUSIVE` on the probe table, the
  waiter sets `lock_timeout` inside its own transaction, and PostgreSQL itself
  answers with `asyncpg` `LockNotAvailableError`, `sqlstate = 55P03`,
  `message = canceling statement due to lock timeout` — no message or code is
  synthesised by the test. That error is then produced against the writer in the
  shape a production lock would take: an `ACCESS EXCLUSIVE` lock on
  `graph_events` while each writer connection carries `lock_timeout`. The
  writer exhausts its three attempts (`2` retries, `1` failure, `0` rows),
  recording stays enabled, and the same action commits exactly once after the
  lock is released; when the lock clears between attempts, one real `55P03` is
  followed by one fresh transaction that writes exactly one row with no
  duplicate. The audit path is covered the same way with the lock on
  `audit_links`, so the blocked statement is the link insert: the real `55P03`
  leaves 0 audit rows, 0 links and both events still queued, and the retry
  writes 2 audit rows + 1 link exactly once.

  The two SQLSTATEs also occur as one real chain, which is the case the policy
  exists for. Two SERIALIZABLE transactions read the same row and the second to
  write is refused by the server with `asyncpg.exceptions.SerializationError`,
  `sqlstate = 40001`, `message = could not serialize access due to concurrent
  update`; the next statement in that aborted transaction is answered with
  `InFailedSQLTransactionError`, `sqlstate = 25P02` (T34x). The classifier
  returns `True` for the cause and `False` for the effect, so a retry retries
  the conflict once and never the abort: driving the chain's real `40001`
  through the writer gives 1 retry, 1 row, 0 failures, and a repeat emit of the
  same action is a duplicate rather than a second fact (T34y).

  `25P02` is the mirror case: a SQLSTATE that *is* real but must never be
  retried, because it reports an earlier failure instead of causing one. A
  genuine unique-constraint violation (`23505`) aborts a real transaction, and
  the next statement in it is answered by the server with
  `asyncpg.exceptions.InFailedSQLTransactionError`, `sqlstate = 25P02`,
  `message = current transaction is aborted, commands ignored until end of
  transaction block`. The classifier returns false for both the cause and the
  effect (T34t), so a retry can neither mask the constraint that failed nor
  spin on a dead transaction. Driving that real `25P02` through the writer gives
  0 retries, 1 counted failure and 0 rows, with recording still enabled (T34u).
  T34v shows the mechanism the writer relies on: a session whose transaction a
  real `22012` aborted answers `25P02` if it is reused, while a fresh session
  from the same factory is clean and emits normally — the writer opens a session
  per attempt, so it never sends a second statement into an aborted
  transaction. At the audit flush the same error persists nothing, keeps both
  events queued, and a clean flush writes 2 audit rows + 1 link exactly once
  (T34w).

  **16.9 parity.** The 16.2 cluster above forced a deliberate check: is any of
  that evidence a property of  the patch release rather than of the major version? A second server was
  provisioned from the EnterpriseDB `postgresql-16.9-1-windows-x64` binaries (not the 16.2 package already present), initialised with `initdb -A
  trust` into a fresh data directory and started on `127.0.0.1:55433`. The
  version was read from the running server, never from the package name:
  `SHOW server_version` returned `16.9` and `SELECT version()` returned
  `PostgreSQL 16.9, compiled by Visual C++ build 1943, 64-bit`, matching the
  startup log line `starting PostgreSQL 16.9`.

  Against that server, the chain is byte-for-byte the same shape as on 16.2:
  the SERIALIZABLE conflict arrives as `asyncpg.exceptions.SerializationError`
  with `sqlstate = 40001` and `message = could not serialize access due to
  concurrent update`, `_is_transient_lock` is `True` and `_is_missing_table` is
  `False`; the next statement in the same transaction arrives as
  `asyncpg.exceptions.InFailedSQLTransactionError` with `sqlstate = 25P02` and
  `message = current transaction is aborted, commands ignored until end of
  transaction block`, `_is_transient_lock` is `False` and `_is_missing_table` is
  `False`. T34k, T34x and T34y pass unchanged — 1 retry, 1 row, 0 failures, a
  repeat emit counted as a duplicate, and the abort effect never retried — so
  neither the classification nor the fresh-session mechanism depends on the
  patch release.

  | Run against the 16.9 server | Result |
  |---|---|
  | `test_migration_003.py` + `test_postgres_phase1.py` (the CI gate selection) | **27 passed, 0 skipped** |
  | Chain selection only (T34k, T34x, T34y) | **3 passed** |
  | Retry/event/audit/link selection (the gate plus `test_transient_retry.py`, `test_graph_events.py`, `test_audit_linkage.py`, `test_fs_read_instrumentation.py`) | **90 passed, 0 skipped** |

  Duplicate suppression, the three-attempt retry bound, the queued-event
  survival at a failed audit flush and the atomic audit/link write are all
  inside that 90-test selection, so they are re-proven on 16.9 rather than
  inherited from the 16.2 run.

  Reproduce (PostgreSQL 13+, role needs `CREATEDB`: the tests create and drop
  their own throwaway databases; T34 additionally migrates the database named in
  the URL):

  ```bash
  createdb -h 127.0.0.1 -U "$PGUSER" raivisox_v3_test
  RAIVISOX_TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/raivisox_v3_test \
    python -m pytest tests/unit/test_migration_003.py tests/unit/test_postgres_phase1.py -q
  ```

  Without the variable, all twenty-five PostgreSQL-gated tests skip with an
  explicit reason and the report says PostgreSQL is unverified — a skip is never
  a pass. The CI workflow runs this same gate twice — against the floating
  `postgres:16-alpine` service in the `postgres-gate` job and against the
  pinned `postgres:16.9` image in the `postgres-gate-16-9` job, which asserts
  the *running* server reports 16.9 before running a single test (results in
  §11). Both jobs fail if any of the 27 gate tests skips, so a future PG
  regression cannot pass CI silently and the version the evidence names cannot
  drift (R11). Nothing in the suite claims a PG guarantee from SQLite evidence.
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
| Transaction boundaries | Audit row + link commit together, dequeue after commit (T18b, T34o, T34s, T34w); each event is its own short transaction; no partial event exists (all fields validated before the insert); a retry always restarts the transaction rather than continuing an aborted one (T34j–T34m, T34q–T34r), and a session whose transaction the server aborted is never reused (T34v). |
| Retry surface | Only concurrency conflicts are retried (`40P01`/`40001`/`55P03`, SQLite lock text), each proven against a real server (T34j, T34k, T34p). Constraint, authorization, missing-table, connection and configuration errors are not retried (T34n, T37), and neither is the server's own `25P02` abort effect (T34t–T34u), so a retry cannot mask a permanent failure or replay a rejected write. When `40001` and `25P02` arrive as one real chain, only the cause is retried and the retry commits once in a fresh transaction (T34x–T34y). |
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

- PostgreSQL is verified on 16.2 (the `55P03` and `25P02` evidence) and on
  16.9 (the whole gate, including the `40001 → 25P02` chain, re-run on a second
  server — 16.9 parity above) by the opt-in runs above and by the CI
  `postgres-gate` and
  `postgres-gate-16-9` jobs (§11), but only on that major version: no
  multi-major-version matrix was run, and the
  concurrency evidence (`40P01`, `40001`, `55P03`) comes from deliberately
  provoked single deadlock / serialization / lock-timeout scenarios, not from a
  sustained contention benchmark. `25P02` ("current transaction is aborted") is
  deliberately *not* classified as transient — the server produces it for real
  in T34t, and T34u–T34w show it is refused rather than retried: it is the
  effect of an earlier abort, and the classifier is only ever handed the
  original error because each retry opens a new transaction. `55P03` is provoked through `lock_timeout`; a
  plain unbounded wait would hang rather than error, which is why the tests set
  the timeout the way a deployment would. SQLite remains the dialect every
  other test uses.
- Child-process I/O, header/body contents, resolved IPs, clock skew: out of
  scope by design and not claimed.
- Rollback is `alembic downgrade 002`, which drops the two new tables and
  leaves `audit_events` and every V2 path untouched. Disabling the flag stops
  all new writes immediately; existing rows simply remain.
- Phase 2 (projection, graph store, blast radius, policy, approval, UI) is not
  started.

## 11. CI and delivery

**The last exact LLM-call total in the DoD recovery tests, fixed at the
cause (commit `1a12bad`).** `test_llm_dod.py::test_recovery_regenerates_wrong_llm_module`
asserted `len(calls) == 6` (three codegen nodes x two passes: the wrong
generation, then the DoD-driven regeneration). Six is not an invariant — and
unlike the `03bc703` case it was not merely a brittle assertion: the same
call-order-keyed stub also made the test's own subject disappear under load.
Measured against the production pipeline with only the LLM transport stubbed,
a single transient solve-probe failure — injected exactly as CPU load
produces it — gives **4 calls** when it lands in the initial pass and **7**
when it lands in the regeneration pass. In the first case the branch-only
retry re-runs one codegen node, and because the stub answered by call order
(`per_pass=3`) that retried node received the "correct" module, so the DoD
passed and the run finished successfully with `recovery_attempts == 1` — the
DoD regeneration the test exists to verify never ran at all. In the second
case `recovery_attempts` became 2. So `assert len(calls) == 6` failed in both
shapes, and `recovery_attempts == 1` failed in the second: a false red, and
in the first shape a false green for the test's actual subject.

The fix is in the fixture and the assertions, not in production. The stub is
now keyed to the pipeline's DoD re-arm (`_install_llm_stub_until_dod_recovery`,
plus an `on_dod_rearm` hook on the attempt recorder): it serves the wrong
module until `_reset_codegen_nodes` actually runs on the DoD-failure path, and
the corrected module afterwards. The initial pass is therefore wrong for all
three codegen nodes however the passes are scheduled, a branch-only retry
cannot accidentally satisfy the DoD, and the regeneration is always
exercised. The test now asserts, per execution: the run ends `completed` with
the DoD satisfied, `5050` from every module, a passed verdict and all steps
completed; the stub's `recovered` flag is set, so recovery was genuinely
required and this is never a spurious success;
`len(attempts) == recovery_attempts + 1`; the initial attempt ran all three
codegen nodes with three LLM consultations; **every DoD-driven re-arm
re-armed all three codegen nodes and the following attempt made exactly three
codegen invocations and three LLM calls** — never a silent subset, never a
reused module; every attempt consulted the LLM once per codegen execution
(nothing omitted, nothing double-counted); the two `fs_write` nodes still ran
exactly once, so completed independent nodes are never re-executed by
recovery; and the whole-run call count equals the kernel's `tool:codegen`
audit delta. No call total is pinned. Nothing was skipped, relaxed, retried or
marked non-blocking, and the outcome, DoD, verdict, step and `fs_write`
assertions are unchanged.

Evidence, local unless marked CI: `test_llm_dod.py` **9 passed** on **10 of 10**
consecutive runs, and **15 of 15** more with three pytest processes in
parallel for five rounds while CPU hogs saturated every core. Mutation checks
still bite: making DoD recovery re-arm only one codegen node fails 3 tests in
the file, and making the adapter reuse its first generation instead of
re-consulting the LLM (a silently omitted consultation) fails 3. The full
backend suite (`pytest tests -q -m "not live_llm"`) gave **668 passed,
72 skipped, 4 deselected, 0 failed** in 7:16. GitHub Actions run
**43888623506** for commit `1a12bad` — **success**, all five jobs green:
`Backend tests (pytest)` **687 passed / 57 skipped / 0 failed** and
**687 passed / 53 skipped / 4 deselected**; `PostgreSQL gate (real server)` and
`PostgreSQL gate (16.9, pinned)` **27 passed, 0 skipped** each, the pinned job
again printing `server_version = 16.9 (Debian 16.9-1.pgdg130+1)` read from the
running server; `Container sandbox gate (live Docker)` 19 live passed /
0 skipped plus 64 passed / 3 skipped; `Frontend (typecheck + build)` typecheck
clean and `Compiled successfully`.

**The load-sensitive LLM-call count in the DoD recovery test, fixed at the
cause (commit `03bc703`).** `test_llm_dod.py::test_exhausted_wrong_llm_module_fails`
occasionally asserted 10 LLM calls instead of 12 while `recovery_attempts == 3`
already held. The counter was right; the hard-coded total was wrong. The
pipeline's recovery budget is shared: if any step fails during a pass
(typically a code-generation solve probe timing out while the machine is
saturated by the rest of the suite), that pass ends on the
*verification-failure* path instead of the DoD path, and `_reset_for_retry`
re-arms only the failed node. The next pass therefore executes one codegen
node rather than three, one of the three recovery attempts is spent there, and
the run legitimately performs 10 consultations — same number of attempts, each
codegen execution still consulting the LLM exactly once. No retry or recovery
defect was involved, and no production code was changed.

The tests now assert the guarantee instead of the pass shape. A helper records
every attempt and, per attempt, the kernel's `tool:codegen` audit delta (what
was really executed) beside the stubbed transport's received calls (what the
LLM was really asked), then asserts: the task ends failed with
`recovery_attempts == 3` and DoD unsatisfied; attempts = recoveries + 1; every
attempt ran at least one codegen node and its LLM calls equal its codegen
executions (nothing silently omitted or double-counted); the first attempt
generated all three modules; and every DoD-driven regeneration re-armed all
three codegen nodes and re-consulted the LLM for each. A new deterministic
test (`test_incidental_step_failure_still_exhausts_recovery`) injects exactly
the load-shaped failure — one probe returning `ran: False` after the LLM was
consulted — and asserts those same guarantees plus that the failed node was
retried and some pass ran a single node, without pinning which composition
results (this machine produces `[3, 1, 3, 3]`; a full-suite run under real
load produced `[3, 1, 3, 1]`, both legitimate — that difference is why the
shape is not asserted). `test_llm_receives_goal_not_title` carried the same
brittleness (`assert len(calls) == 3`) and failed the same way in a full-suite
run (`assert 4 == 3`); it now requires at least three consultations, one per
codegen execution according to the audit count, and still checks the actual
subject — the goal, not the title, reaches every prompt. Neither test was
skipped, relaxed, retried or marked non-blocking.

Evidence, all local unless marked CI. `test_llm_dod.py` **9 passed** on **10 of
10** consecutive runs, and **15 of 15** more with three pytest processes in
parallel for five rounds while CPU hogs saturated every core. Mutation checks
confirm the assertions still bite: re-arming only one codegen node after a DoD
failure fails 3 tests in the file, and cutting the recovery budget from 3 to 1
fails 2. The full backend suite (`pytest tests -q -m "not live_llm"`) gave
**668 passed, 72 skipped, 4 deselected, 0 failed** in 6:52. Two earlier
full-suite runs on this same work surfaced the second failure shape
(`[3, 1, 3, 1]` and `assert 4 == 3`) — both fixed above rather than re-run away.

The same file then still pinned one exact total:
`test_recovery_regenerates_wrong_llm_module` asserted `len(calls) == 6`
(3 initial + 3 regenerated). It was left untouched at the time because no run
had failed on it and rewriting it without an observed failure would have been
an unrequested change to a neighbouring guarantee. It was subsequently
reviewed with injected load-shaped failures, which showed that six is not an
invariant (4 and 7 were measured) and that the call-order-keyed stub could
satisfy the DoD without running the regeneration at all — fixed in `1a12bad`,
see the entry at the top of this section.

GitHub Actions run **41283818265** for commit `03bc703` — **success**, all five
jobs green: `Backend tests (pytest)` **687 passed / 57 skipped / 0 failed** and
**687 passed / 53 skipped / 4 deselected** for the explicit `-m "not live_llm"`
selection; `PostgreSQL gate (real server)` and `PostgreSQL gate (16.9, pinned)`
**27 passed, 0 skipped** each, the pinned job again printing
`server_version = 16.9 (Debian 16.9-1.pgdg130+1)` read from the running server
before any test; `Container sandbox gate (live Docker)` 19 live passed /
0 skipped plus 64 passed / 3 skipped in the non-live half; `Frontend (typecheck
+ build)` typecheck clean and `Compiled successfully`. CI evidence is the green
run above; everything else in this section was run locally on Windows/Python
3.12.2.

GitHub Actions run **40182067467** for commit `ddb83da` (the pinned 16.9 gate)
— **success** on re-run, all five jobs green: `Backend tests (pytest)` 672
passed / 57 skipped / 0 failed (729 collected), and 672 passed / 53 skipped / 4
deselected for the explicit `-m "not live_llm"` selection; `PostgreSQL gate
(real server)` **27 passed, 0 skipped** against `postgres:16-alpine`; the new
`PostgreSQL gate (16.9, pinned)` **27 passed, 0 skipped** against
`postgres:16.9`, whose first step printed `server_version = 16.9 (Debian
16.9-1.pgdg130+1)` read from the running server before any test started;
`Container sandbox gate (live Docker)` 19 live passed / 0 skipped plus 64
passed / 3 skipped in the non-live half; `Frontend (typecheck + build)` passed.
The first attempt of this run failed only the `-m "not live_llm"` step, on
`test_capability_audit.py::test_http_allowlisted_host_is_real` with `AssertionError:
HTTP 503 from https://example.com` — a live third-party fetch, and the same test
passed in the full-suite step of that same attempt. The job was re-run
unchanged and passed; no test, assertion or workflow was relaxed to get there.
That flake was then fixed at the cause (commit `8568810`, below) rather than
re-run away.

**The flaky HTTP capability proof, fixed at the cause (commit `8568810`).** The
test measured a third party's availability instead of this repository's
behaviour: its skip probe counted *any* HTTP response as "online" (a 503 does
not raise), and the test then asserted `status == 200` from `example.com`. The
capability proof is now split by what each part can actually establish.

The live test keeps the real socket and DNS path: the allowlisted host must be
admitted by the real allowlist and SSRF guard, and the adapter's verdict must
then be a 200 (success, with title and body parsed from the real bytes) or the
far end's own non-200 status surfaced verbatim as a failure with no output. A
third party's 5xx is therefore reported, not attributed to this code, while a
fabricated success — or a failure carrying no HTTP verdict — still fails the
test. A dead endpoint now skips with an explicit connectivity reason instead of
claiming the capability is broken.

Five offline tests pin the response policy against a controlled transport, so
the happy path no longer depends on anyone's uptime: a 200 is parsed into
status/title/content/bytes with the DNS guard shown to be consulted on admission
and per hop; a 503 is a failure with no output; a `200` with a non-text content
type is refused; a non-public resolution makes the adapter send nothing at all;
and a redirect to a non-allowlisted host is refused with the target never
fetched. Only the transport is substituted — the allowlist, the DNS/SSRF
consult, per-hop redirect re-validation, the content-type and size handling and
the body parsing are the production code paths, and the SSRF verdict is proven
to be honoured rather than merely called.

What the fixture deliberately does **not** claim: it says nothing about public
DNS, TLS to a real host, or Internet reachability — production requires https on
the scheme's default port and refuses every non-global resolved address, so a
loopback server is unreachable by design, and a local fixture would have to
weaken the very guard under test. That residual concern stays with the live
test, which is where it was asserted on the CI runner in run `40462985806`.

GitHub Actions run **40462985806** for commit `8568810` — **success on the first
attempt**, all five jobs green: `Backend tests (pytest)` **677 passed / 57
skipped / 0 failed** (734 collected) for the full suite and **677 passed / 53
skipped / 4 deselected** for the explicit `-m "not live_llm"` selection (the
five new tests account for 672 → 677, and the skip count is unchanged at 57, so
the live HTTP test ran rather than skipping); `PostgreSQL gate (real server)`
**27 passed, 0 skipped**; `PostgreSQL gate (16.9, pinned)` **27 passed, 0
skipped** with `server_version` read from the running server as `16.9 (Debian
16.9-1.pgdg130+1)`; `Container sandbox gate (live Docker)` green; `Frontend
(typecheck + build)` green.

Locally, on the same commit without a PostgreSQL URL or a container runtime:
the full backend suite gave **658 passed, 76 skipped, 0 failed** in 11:04
(exit 0), and `test_capability_audit.py` + `test_http_adapter.py` together gave
**38 passed, 0 skipped**. The HTTP subset (`-k http`) passed **10 of 10**
consecutive runs, and the live test was driven twice by hand — once with a
real-shaped `HTTP 503` (passes, having asserted the honest verdict) and once
with a verdict-less transport failure (fails) — which is the exact distinction
the old assertion got wrong. The 76 skips are the pre-existing environment-gated
selections — 25 across the PostgreSQL suites without a URL, 10 live-container
tests without a runtime, and the rest of that kind; none of them is an HTTP or
capability test.

**The sibling live tests, hardened the same way (commit `04c9500`).** The same
defect remained in `test_http_adapter.py`, which is what the capability-audit
report flagged as the outstanding exposure: three `network`-marked adapter tests
asserted `status == 200` from `example.com` and Wikipedia, so a remote 5xx would
fail CI while the adapter had behaved correctly. A fourth,
`test_non_allowlisted_domain_rejected_before_any_request`, carried the marker
without making any request — it was mislabelled, and the rejection happens before
DNS, so it needs no network at all.

Response handling is now pinned offline through the registry (kernel gate
included), with only the transport and the DNS consult substituted: a 200 read
parsed into status/url/title/content/bytes with the guard shown to be consulted;
`max_chars` truncating the body while `bytes` still reports the full size; a 503
surfacing as a failure with no output; a `200` with a non-text content type
refused; a redirect to a non-allowlisted host refused with the target never
fetched; the non-allowlisted host proven to send nothing at all; and `web_search`
parsing a real API payload (titles, percent-encoded query, tag-stripped snippets,
`total_hits`), clamping an oversized `max_results` to 20, failing cleanly on
non-JSON, and reporting a remote `429`. Nine new tests take the file from 19 to
28 with no skips.

The three live tests remain, still marked `network`, and the execution policy is
explicit: CI does not deselect the marker, so they run on every push, and they
are the only evidence here about public DNS, TLS and Internet reachability. They
assert only the verdict this repository owns — a 200 with parsed content, or the
far end's own status surfaced verbatim — and skip with a stated reason when the
host is unreachable, so the suite is never red because example.com had a bad
minute. A fabricated success still fails, and so does a failure with no HTTP
verdict.

GitHub Actions run **40466842138** for commit `04c9500` — **success on the first
attempt**, all five jobs green: `Backend tests (pytest)` **686 passed / 57
skipped / 0 failed** (743 collected) and **686 passed / 53 skipped / 4 deselected**
(the nine new tests account for 677 → 686, and the skip count is unchanged at 57,
so the live HTTP tests ran rather than skipping); both PostgreSQL gates **27
passed, 0 skipped** (the pinned job again printing `server_version = 16.9`);
`Container sandbox gate (live Docker)` green; `Frontend (typecheck + build)`
green.

Locally on that commit: `test_http_adapter.py` **28 passed, 0 skipped** over
**10 of 10** consecutive runs, `test_http_adapter.py` + `test_capability_audit.py`
together **47 passed, 0 skipped**, and the `-m network` selection is exactly the
three live tests (3 passed). The live tests were also driven by hand through
three cases: a real-shaped 503 (passes, verdict asserted), a fabricated 200 with
the wrong body (fails), and no connectivity (skips with a reason).

The full local backend suite on that commit gave **667 passed, 76 skipped, 0
failed** in 9:28 (exit 0) — 658 plus the nine new tests. One run of the same
command earlier reported a single failure,
`test_llm_dod.py::test_exhausted_wrong_llm_module_fails` (`assert 10 == 12` on its LLM-call
counter, with `recovery_attempts == 3` already satisfied). It did not reproduce:
the identical command re-run was green, the test passed 3/3 in isolation and 3/3
with the new HTTP tests ahead of it in the same process, and CI — which runs it
in both backend steps — passed it. It is a CPU-load-sensitive count assertion in
a test this change does not touch (its own docstring already notes full-suite
load sensitivity), and the same file's `test_recovery_regenerates_wrong_llm_module`
is already recorded above as an intermittent test. It is disclosed here rather
than papered over or widened out of scope. It was subsequently root-caused and
fixed at the assertion — see the `03bc703` entry in §11.

GitHub Actions run **40172988124** for commit `704fb7c` (the `40001 → 25P02`
chain) — **success**, all four jobs green: `Backend tests (pytest)` 672 passed /
57 skipped / 0 failed (729 collected), and 672 passed / 53 skipped / 4
deselected for the explicit `-m "not live_llm"` selection; `PostgreSQL gate
(real server)` **27 passed, 0 skipped** against a `postgres:16-alpine` service
(the job fails on any skip), so `…_serialization_chain_keeps_the_cause_retryable`
and `…_writer_retries_chain_cause_in_a_fresh_transaction` produce the real chain
on the CI-pinned SQLAlchemy and driver too; `Container sandbox gate (live
Docker)` Docker `server=28.0.4 client=28.0.4`, 19 live passed / 0 skipped plus
64 passed / 3 skipped in the non-live half; `Frontend (typecheck + build)`
typecheck clean and `Compiled successfully`.

GitHub Actions run **39761493290** for commit `84cf4e6` (the `25P02` evidence) —
**success**, all four jobs green: `Backend tests (pytest)` 672 passed / 55
skipped / 0 failed (727 collected), and 672 passed / 51 skipped / 4 deselected
for the explicit `-m "not live_llm"` selection; `PostgreSQL gate (real server)`
**25 passed, 0 skipped** against a `postgres:16-alpine` service (the job fails
on any skip), so `test_postgres_in_failed_transaction_is_not_transient`,
`…_writer_does_not_retry_in_failed_transaction`,
`…_aborted_session_is_not_reused` and
`…_audit_flush_keeps_queue_after_in_failed_transaction` pass on the CI-pinned
SQLAlchemy and driver too; `Container sandbox gate (live Docker)` Docker
`server=28.0.4 client=28.0.4`, 19 live passed / 0 skipped plus 64 passed /
3 skipped in the non-live half; `Frontend (typecheck + build)` typecheck clean
and `Compiled successfully`.

GitHub Actions run **39742438261** for commit `1cbe7ab` (the `55P03` lock-timeout
evidence) — **success**, all four jobs green: `Backend tests (pytest)` 672
passed / 51 skipped / 0 failed (723 collected); `PostgreSQL gate (real server)`
**21 passed, 0 skipped** against a `postgres:16-alpine` service (the job fails
on any skip), so `test_postgres_lock_timeout_is_classified_transient`,
`…_gives_up_after_real_lock_timeouts`, `…_retry_commits_once_when_the_lock_clears`
and `…_audit_flush_survives_a_real_lock_timeout` all pass on the CI-pinned
SQLAlchemy and driver too; `Container sandbox gate (live Docker)` Docker
`server=28.0.4 client=28.0.4`, 19 live passed / 0 skipped plus 64 passed /
3 skipped in the non-live half; `Frontend (typecheck + build)` typecheck clean
and `Compiled successfully`.

GitHub Actions run **39590736377** for commit `9d1672b` (the retry hardening) —
**success**, all four jobs green: `Backend tests (pytest)` 672 passed / 47
skipped / 0 failed; `PostgreSQL gate (real server)` **17 passed, 0 skipped**
against a `postgres:16-alpine` service (the job fails on any skip), which also
proves the SQLSTATE classification against the CI-pinned SQLAlchemy; `Container
sandbox gate (live Docker)` 19 live passed / 0 skipped plus 64 passed in the
non-live half; `Frontend (typecheck + build)` passed.

GitHub Actions run **39579100724** for commit `5384608` — **success**, all four
jobs green, including the new `PostgreSQL gate (real server)`: 11 PostgreSQL
tests passed against a `postgres:16-alpine` service with **zero skips**, and the
SQLite `backend-tests` job reported 638 passed / 41 skipped / 0 failed. This is
the evidence that keeps the PostgreSQL gap closed on every push.

GitHub Actions run **39554959798** for commit `f30e6a4` — **success**, all three
jobs green: `Backend tests (pytest)` (638 passed, 33 skipped, 0 failed),
`Container sandbox gate (live Docker)` (19 live passed, 0 skipped; 64 passed in
the non-live half), `Frontend (typecheck + build)`.

- Implementation commits: `f30e6a4` (`feat(v3): record provenance-aware events
  and audit links behind a flag`), `5384608` (`test(v3): verify Phase 1
  against a real PostgreSQL server`), `9d1672b` (`fix(v3): retry PostgreSQL
  concurrency conflicts by SQLSTATE`), `1cbe7ab` (`test(v3): prove 55P03 lock
  timeouts against a real PostgreSQL server`), `84cf4e6` (`test(v3): prove
  25P02 is refused, not retried, against real PostgreSQL`) and `704fb7c`
  (`test(v3): prove one real 40001 → 25P02 chain retries only its cause`), plus
  `ddb83da` (`ci(v3): gate PostgreSQL 16.9 by version and record the parity
  run`), all pushed to `origin/main`, followed by `8568810`, `04c9500` and
  `03bc703` (`test(v3): assert the DoD recovery guarantee, not the pass
  composition`) and `1a12bad` (`test(v3): assert the DoD regeneration
  guarantee, not the call total`) after the release.
- `v1.0.0` untouched (`0fe1eca`); no release tag created, and Phase 2 not
  started.
- Working tree clean after the documentation commit that records this report.

## 12. Verdict

`V3 PHASE 1: PASSED` — every Phase 1 acceptance item is evidenced, including
the PostgreSQL path (T34, T34b–T34y on real PostgreSQL 16.2 and 16.9 servers —
the whole gate was re-run on 16.9 rather than extrapolated from 16.2 — and the
CI `postgres-gate` plus pinned `postgres-gate-16-9` jobs that keep it
verified). The retry policy is
dialect-aware: PostgreSQL deadlocks (`40P01`), serialization failures (`40001`)
and lock timeouts (`55P03`) are recognised from the driver's SQLSTATE and
retried in a fresh transaction, while constraint, authorization, missing-table,
connection and configuration errors are not retried at all — including the
server's own `25P02` abort effect (T34j–T34y, T37). Each transient SQLSTATE is
proven by the server itself, including `55P03` from a real `lock_timeout`, and
the two errors PostgreSQL emits as one chain: the `40001` cause is retried once
in a fresh transaction while the `25P02` effect it leaves behind is refused
(T34x–T34y). Phase 1 retry hardening is **closed** here: the classifier's one
rule — retry a concurrency-conflict *cause*, never an abort *effect* — is pinned
on every host and against a real server, and no further SQLSTATE enumeration is
planned without a concrete failure case. The three owner gates G1–G3 remain
open, and Phase 2 has not been started.
