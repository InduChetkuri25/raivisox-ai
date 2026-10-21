# RAIVISOX V3 Phase 1 — Implementation Specification

**Status:** specification only. **No Phase 1 code exists.** Nothing in this document has
been implemented, migrated, or tested; the test matrix below is a *plan*, not results.

Implements the Phase 1 milestone of [v3-agent-action-graph.md](v3-agent-action-graph.md)
(§5, §13 Phase 1). Baseline inspected: commit `4a4a3fc` on `main`, clean working tree,
only tag `v1.0.0` → `74e2c1d6a99a06b064ef6486f337892818961fb6`.

**Decision status: all 12 owner decisions are resolved** — classified as
`RESOLVED — SECURE DEFAULT` or `RESOLVED — EXISTING CONTRACT`, each with its reason,
consequences, tests and compatibility impact in §11.2–§11.3. No decision blocks Phase 1.
Three gates still require explicit human approval, and none blocks implementation: enabling
events in a real deployment (§11.4 G1), the Phase 2 retention value (G2), and whether real
users may hold `ADMIN` (G3).

Inline tags `D1`–`D12` refer to the decision record; every tag in the body resolves to a row
in §11.2. A resolution here is an architect decision under the stated principles, not a
claim that a human signed it off.

---

## 1. Scope and non-goals

### 1.1 What Phase 1 delivers

A **provenance-aware event contract** plus the instrumentation needed to produce it, and
nothing that consumes it:

1. One new append-only table, `graph_events`, with a closed event-type set, deterministic
   idempotency keys, a payload allowlist, and a payload hash.
2. Additive **audit linkage** in a new side table, `audit_links` (`event_id`, `user_id`,
   `task_id`, `run_id`, `step_node_id`, `action_id`), populated for new rows only. No
   existing table is altered (D2).
3. Emission at five existing choke points: `AdapterRegistry.run` (tool lifecycle),
   `FileReadAdapter` (successful and denied reads), `FileWriteAdapter` (committed and
   denied writes), `HttpGetAdapter` (endpoint attempt/success/denial), and
   `_register_task_artifact` (verified write).
4. Trusted identity plumbing: one per-run provenance handle, validated against the
   database once per run, carried in `AdapterContext`.
5. Alembic revision `003` (two new tables, no `ALTER` on any existing table, no `init_db`
   shim) and the acceptance tests of §9.

### 1.2 Non-goals (explicit)

- **No graph**: no `graph_nodes`, `graph_edges`, projection, rebuild, analyzer, findings, or
  reachability. Phase 2.
- **No policy, approvals, or decision authority.** Phase 1 never allows or denies anything.
  It only records.
- **No API.** No new route, response model, or serialization. Deferred (see §7.8).
- **No UI.**
- **No sandbox instrumentation.** Child-process file/network activity remains unobservable;
  Phase 1 does not pretend otherwise (§5.9).
- **No change to any existing control**: kernel authorization, sandbox flags, required-limit
  gates, workspace quotas, digest verification, offline fail-closed behaviour, audit
  semantics, pipeline control flow.
- **No retention/cleanup job** for the new table — deferred to Phase 2 by design §15.2 item 6
  and D9; see §11.4 G2 for the retention gate.

### 1.3 The authority invariant (Phase 1 form)

Phase 1 has **no authority to subtract from or add to** the security kernel (that is Phase 5,
and even then it is restrictive-only). Concretely:

- The only code that can change an execution outcome is the existing kernel/sandbox path,
  unmodified in behaviour.
- Every Phase 1 write is best-effort observation: a failure is counted and logged, never
  raised into the pipeline (§8).
- No event, flag, or column introduced here is read by any authorization decision.

---

## 2. Actual repository baseline

### 2.1 Repository state (verified)

| Fact | Value |
|---|---|
| HEAD | `5e2ca57` (`docs: state that V3 leaves the V2 execution controls untouched`) |
| Branch / remote | `main` / `origin` = `https://github.com/InduChetkuri25/raivisox-ai.git` |
| Working tree | clean (`git status --porcelain` empty) |
| Tags | `v1.0.0` only → `74e2c1d6a99a06b064ef6486f337892818961fb6` |
| CI on HEAD | run `39413459595` — all three jobs `success` (backend `594 passed, 32 skipped`; live container gate `19 passed`; frontend typecheck + build) |
| Alembic head | revision `002` (`002_execution_runs_artifacts_audit_events.py`, down_revision `001`) |

### 2.2 Files inspected (each claim below is read from these)

| Area | File | What it establishes |
|---|---|---|
| Kernel | `services/security/security_kernel.py` | `authorize`/`record_audit`/`authorize_and_log(actor_id, action, resource, permissions, actor_type)`; in-memory grants; audit call has no task/run/user |
| Audit | `services/security/audit_trail.py` | `AuditEvent(event_id, actor_id, action, resource, allowed, request_id, details, timestamp)`; `log(...)`; deque + `flush_pending(db)` batching; `read_from_db` builds response dicts field-by-field |
| Audit model | `apps/api/app/models/audit_event.py` | columns: `event_id, actor_id, action, resource, allowed, request_id, details, timestamp`; no linkage columns |
| Gate | `integrations/adapters/registry.py` | `run()` gate: `authorize_and_log(actor_id=ctx.actor_id, action=f"tool:{id}", resource=ctx.resource, ...)`; `AdapterResult.permission_denied` |
| Adapter ctx | `integrations/adapters/base.py` | `AdapterContext(actor_id, task_id, step_id, resource, workspace, extra)` — **no user id, no run id** |
| Pipeline | `apps/api/app/main.py` | `run_task_pipeline` creates `ExecutionRun` before steps; `_build_worker(..., actor_id=PIPELINE_ACTOR, ...)` at the call site (line ~2629); `_on_node_transition` registers artifacts; `_register_task_artifact(task_id, *, path, node_id, step_db_id, step_title, run_id)` |
| Runs/events/artifacts | `apps/api/app/models/{execution_run,task_event,task_artifact,task}.py` | `execution_runs` durable row per execute; `task_events.execution_run_id`; artifacts carry `sha256/size_bytes/run_id/is_current/superseded_at`; `tasks.user_id` is the ownership key |
| Read tool | `integrations/tools/file_reader.py` | returns `content` in the tool output; failures are `ToolResult(success=False)` |
| Read adapter | `integrations/adapters/builtin.py` | audits denials only; success path emits nothing |
| Write adapter | `integrations/adapters/builtin.py` | denies on clamp failure and on `WorkspaceQuotaExceeded`; success returns the tool output |
| Network adapter | `integrations/adapters/http_get.py` | allowlist + SSRF validation before any byte; refusals returned as `AdapterResult.error`, not audited |
| Sandbox/quota | `services/execution/*.py` | per-run `enforced`/`limits_hit`; quotas and digest verification unchanged by this spec |
| DB init | `apps/api/app/database.py` | `init_db()` runs `Base.metadata.create_all` then explicit additive `ALTER TABLE` guards (`tasks.outcome`, `task_events.execution_run_id`) |
| Migrations | `apps/api/alembic/versions/*` | two revisions, `001` → `002` |
| Zero-drift test | `tests/unit/test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema` | alembic head must reproduce `Base.metadata` exactly (tables, columns, indexes) |
| Auth | `apps/api/app/auth/service.py`, `app/main.py::get_current_user_dep` | JWT → `decode_token` → `get_user_by_id`; task reads scoped by `current_user.id` |
| Test config | `pyproject.toml` | `asyncio_mode=auto`, `pythonpath=[".", "apps/api"]`, markers `network`, `live_llm`, `live` |
| CI | `.github/workflows/ci.yml` | three jobs; SQLite DBs; container gate pinned by digest |

### 2.3 Facts that constrain this spec

1. **The pipeline actor is the only identity adapters see.** `_build_worker` passes
   `actor_id=PIPELINE_ACTOR` for every step, so audit rows say `pipeline-service`.
2. **The user id exists in the pipeline**, on the loaded `Task` row (`task.user_id`), and the
   run id exists (`execution_run.id`) before any worker is built.
3. **`AdapterContext` has no user/run field**, and it is constructed in **23** places (1 in
   `main.py::_build_worker`, 22 in tests — `grep -rn "AdapterContext(" apps integrations tests`).
   New fields must default to empty so every existing call site keeps working.
4. **Audit persistence is batched and idempotent per `event_id`**; adding columns means
   extending the dataclass, the `log()` signature, the flush, and `read_from_db`.
5. **`init_db()` never ALTERs pre-existing tables** except through explicit guards; a new
   column without a guard would silently be missing on an existing deployment.
6. **A zero-drift test compares alembic head to metadata**, so a new model without a matching
   migration fails the suite (this is desirable).
7. **`TOOL_CALL` is never emitted**, so there is today no per-invocation record of which tool
   ran; `step_completed` carries `result.tool` after the fact.
8. **`tool_usage` has no writer**; this spec does not revive it.
9. **No API is needed** to make Phase 1 verifiable: tests can assert rows directly.

---

## 3. Proposed event schema

### 3.1 Table `graph_events` (append-only)

```sql
CREATE TABLE graph_events (
  seq             INTEGER      NOT NULL PRIMARY KEY AUTOINCREMENT,  -- PG: SERIAL
  event_id        VARCHAR(36)  NOT NULL UNIQUE,
  schema_version  INTEGER      NOT NULL DEFAULT 1,
  event_type      VARCHAR(48)  NOT NULL,
  occurred_at     FLOAT        NOT NULL,
  recorded_at     FLOAT        NOT NULL,
  actor_id        VARCHAR(100) NOT NULL,
  actor_type      VARCHAR(32)  NOT NULL,
  user_id         VARCHAR(36)  NOT NULL,
  task_id         VARCHAR(36)  NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id          VARCHAR(36)  NULL,
  step_node_id    VARCHAR(64)  NULL,
  tool_id         VARCHAR(64)  NULL,
  action_id       VARCHAR(36)  NULL,
  target_kind     VARCHAR(16)  NULL,
  target_id       VARCHAR(512) NULL,
  outcome         VARCHAR(24)  NOT NULL,
  evidence_class  VARCHAR(16)  NOT NULL,
  source          VARCHAR(64)  NOT NULL,
  correlation_id  VARCHAR(100) NULL,
  idempotency_key VARCHAR(200) NOT NULL UNIQUE,
  payload         JSON         NULL,
  payload_hash    VARCHAR(64)  NOT NULL
);
```

Indexes: `ix_graph_events_user_seq (user_id, seq)`, `ix_graph_events_task_seq
(task_id, seq)`, `ix_graph_events_run_seq (run_id, seq)`, `ix_graph_events_action
(action_id)`, `ix_graph_events_type (event_type)`. Unique constraints: `event_id`,
`idempotency_key`.

Why `seq` is the primary key: it is the only total order that survives concurrent step
execution and restarts, and it makes the projection cursor (§Phase 2) a single integer. Why
`Integer` rather than `BigInteger`: SQLite only auto-increments an `INTEGER PRIMARY KEY`;
`BigInteger` would silently stop auto-incrementing there while the zero-drift test still
passes. The 2³¹ ceiling (~2.1B events) is acceptable **at the design's own volume estimate**:
design §10 assumes 1000 tasks/day at 6–12 events/step over ~10 steps, i.e. ≤ 120k events/day,
so the ceiling is ≈ 49 years of continuous operation; R8 records the trigger for a future
migration. Carried as `RESOLVED — EXISTING CONTRACT` (D6): the design specifies a
"DB-assigned monotonic integer", and every integer column in this repo is `Integer`.

**`seq` values must never be reused.** A plain SQLite `INTEGER PRIMARY KEY` assigns
`max(rowid)+1`, so deleting the newest rows (a task delete cascades its events) would let the
next insert reuse a `seq` that a Phase 2 cursor may already have passed — silent skipped or
duplicated evidence. The model therefore sets
`__table_args__ = {"sqlite_autoincrement": True}`, which emits
`INTEGER PRIMARY KEY AUTOINCREMENT` and monotonic ids; PostgreSQL's `SERIAL` sequence
already never reuses. The Alembic `op.create_table(..., sqlite_autoincrement=True)` must
match. A test asserts monotonic `seq` across a delete-then-insert cycle (§9 T8b).

Immutability is enforced by construction: the module exposes no update/delete function and no
ORM mutators are used; a test asserts that the model has no `updated_at` and that the writer
only inserts (§9 T15b).

### 3.2 Column contract

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `seq` | integer PK | no | autoincrement | total order; gaps are legal (§8.4) |
| `event_id` | string(36) unique | no | uuid4 | event identity, independent of `seq` |
| `schema_version` | integer | no | 1 | reader refuses unknown versions |
| `event_type` | string(48) | no | — | closed set (§3.3) |
| `occurred_at` | float | no | — | source clock, epoch seconds (same unit as `audit_events.timestamp`) |
| `recorded_at` | float | no | `time.time()` | insert clock, `>= occurred_at` |
| `actor_id` | string(100) | no | — | `pipeline-service`, `user:<uuid>`, `system` |
| `actor_type` | string(32) | no | — | `service` \| `user` \| `system` |
| `user_id` | string(36) | no | — | owning user; **isolation key** |
| `task_id` | string(36) FK | no | — | `tasks.id`, cascade delete |
| `run_id` | string(36) | yes | — | `execution_runs.id` (no FK, matching `task_artifacts.run_id`) |
| `step_node_id` | string(64) | yes | — | plan node id (`step-3`), **not** `task_steps.id` |
| `tool_id` | string(64) | yes | — | adapter id (`fs_read`, `http_get`, …) |
| `action_id` | string(36) | yes | — | one tool invocation; correlates lifecycle events |
| `target_kind` | string(16) | yes | — | `resource` \| `endpoint` |
| `target_id` | string(512) | yes | — | `res:<task>/<relpath>` or `net:<host>/<path>` |
| `outcome` | string(24) | no | — | §3.4 |
| `evidence_class` | string(16) | no | — | `observed` \| `verified` (Phase 1 has no `declared` events) |
| `source` | string(64) | no | — | fixed `module:function` string from a constant table |
| `correlation_id` | string(100) | yes | — | kernel `request_id` where a decision exists |
| `idempotency_key` | string(200) unique | no | — | §3.5 |
| `payload` | JSON | yes | — | allowlisted, redacted, ≤ 8 KiB (§3.6) |
| `payload_hash` | string(64) | no | — | sha256 of canonical payload JSON |

### 3.3 Event types (Phase 1 closed set)

| # | `event_type` | Emitted at | Outcome(s) | Evidence | In design doc? |
|---|---|---|---|---|---|
| 1 | `tool.attempted` | `AdapterRegistry.run` before the kernel decision | `attempted` | observed | yes |
| 2 | `tool.allowed` | after `authorize_and_log` returns allowed | `allowed` | observed | yes |
| 3 | `tool.denied` | kernel denied, or adapter returned `permission_denied` | `denied` | observed | yes |
| 4 | `tool.executed` | adapter returned `success=True` | `executed` | observed | yes |
| 5 | `tool.failed` | adapter returned failure, or raised | `failed` | observed | yes |
| 6 | `resource.read.observed` | `FileReadAdapter` after the tool returns (success **or** tool-level error) | `executed` \| `failed` | observed | yes |
| 7 | `resource.read.denied` | `FileReadAdapter` when the workspace clamp refuses | `denied` | observed | yes |
| 8 | `resource.write.committed` | `FileWriteAdapter` after a successful tool write | `executed` | observed | yes |
| 9 | `resource.write.denied` | `FileWriteAdapter` on clamp or workspace-quota refusal | `denied` | observed | **reviewed addition — D3 resolved** |
| 10 | `endpoint.connect.attempted` | `HttpGetAdapter` after URL validation, before the request | `attempted` | observed | yes |
| 11 | `endpoint.connect.succeeded` | after a 2xx text response is accepted | `executed` | observed | yes |
| 12 | `endpoint.connect.denied` | validation/allowlist/SSRF/policy refusal before any byte | `denied` | observed | yes |
| 13 | `artifact.registered` | `_register_task_artifact` after the row commits | `executed` | **verified** | yes (§5.1) |

`capability.granted` is **deliberately deferred to Phase 2** — D4 resolved as
`EXISTING CONTRACT` (design §13 Phase 1 acceptance covers kernel decisions and adapter
invocations only; the type stays in the design's closed set for Phase 2): kernel grants are
global to service actors, have no user or task subject, and
forcing them into a table whose `user_id` is `NOT NULL` would either weaken the isolation
invariant or require synthetic users. Note the deliberate asymmetry with the per-event
"attempted / allowed / denied / executed / failed" split explained in §3.8: that is the
distinction between *decision* and *outcome*, not a decision field. Phase 2 introduces declared capability nodes, where a global,
non-user subject is the correct shape.

`resource.write.denied` does not exist in the design's closed set. It is proposed because a
write can be refused **after** the kernel allowed it (workspace clamp, quota), and without
this type a refusal is only a free-text `tool.failed` error. D3 = `RESOLVED — SECURE DEFAULT`:
the type is added (a reviewed addition under design §5.2), because a quota/clamp refusal is
not an adapter failure and must not be recorded as one.

### 3.4 Outcome vocabulary and invariants

`attempted`, `allowed`, `denied`, `executed`, `failed`, `interrupted`, `cancelled`, `expired`.

Phase 1 emits the first five only; `interrupted` / `cancelled` / `expired` belong to run and
approval lifecycles (Phase 2/5). Invariants asserted by tests:

- `tool.allowed` ⇒ there is a `tool.attempted` with the same `action_id` inserted earlier
  (`seq`).
- `tool.denied` ⇒ **no** `tool.executed` / `tool.failed` for that `action_id`.
- `*.denied` ⇒ `outcome = denied`; `*.committed`/`*.succeeded` ⇒ `executed`.
- `artifact.registered` ⇒ `evidence_class = verified` and references an artifact row that
  exists (the writer is called after commit).
- Every event with a non-null `action_id` has `tool_id` set.

### 3.5 Idempotency keys (deterministic, no randomness)

`action_id` is a uuid4 generated **once per adapter invocation** by the registry and reused
for that invocation's lifecycle events. Keys:

| Event | Key |
|---|---|
| `tool.attempted` / `tool.allowed` / `tool.denied` / `tool.executed` / `tool.failed` | `{event_type}:{action_id}` |
| `resource.read.*` / `resource.write.*` / `endpoint.connect.*` | `{event_type}:{action_id}` |
| `artifact.registered` | `artifact.registered:{artifact_id}` |

Properties: a **write retry** reuses the same in-memory key ⇒ deduped; a **second execution**
of the same step (recovery retry) gets a new `action_id` ⇒ a distinct row, which is correct
because it is a distinct action (the design's `attempt` distinction is carried by
`action_id`, so no `attempt` column is needed).

### 3.6 Payload allowlist and redaction (per event type)

| `event_type` | Allowed payload keys | Forbidden by construction |
|---|---|---|
| `tool.attempted` | `resource_ref` (string ≤ 200) | params, argv, content |
| `tool.allowed` | `granted_permissions` (list[str]) | — |
| `tool.denied` | `denied_permissions`, `reason` | — |
| `tool.executed` | `duration_ms` (int) | output, stdout, stderr |
| `tool.failed` | `duration_ms`, `error_kind` (enum), `denial_kind` (enum, optional) | error text with paths |
| `resource.read.observed` | `size` (int), `encoding`, `error_kind`, `relpath` | **content**, absolute path |
| `resource.read.denied` | `reason` (enum), `requested_basename` | absolute path |
| `resource.write.committed` | `bytes` (int), `created` (bool), `overwritten` (bool), `relpath` | content, absolute path |
| `resource.write.denied` | `reason` (enum), `bytes`, `quota_kind` | content, absolute path |
| `endpoint.connect.*` | `host`, `path_template`, `status` (int), `bytes` (int), `reason` (enum), `denial_layer` (enum) | query string, headers, body, userinfo |
| `artifact.registered` | `artifact_id`, `sha256`, `size_bytes`, `filename` | storage_path, absolute path |

`error_kind` / `reason` / `denial_layer` values are constants defined beside the schema
(`"outside_workspace" | "quota" | "not_found" | "too_large" | "permission" | "unknown"`
etc.), so payloads are countable, not free text.

Redaction is applied by the writer, not by callers: unknown keys are **dropped** (not
sanitized), and a value that matches the secret heuristics (`sk-…`, ≥ 40 chars of
base64/hex, `authorization`/`cookie`/`token` key names) causes the event to be **rejected**
and counted (`graph_events_invalid`), never silently rewritten.

### 3.7 Schema version

`schema_version` starts at `1`. The writer always writes `1`. Readers (Phase 2) must refuse
rows with a version they do not know and report the count; a Phase 1 test asserts that an
inserted version-`2` row is not silently interpreted (the reader helper rejects it).

### 3.8 Requested action, authorization decision, observed outcome (kept distinct)

Phase 1 represents the request/decision/outcome triple **as separate rows, not as fields on
one row**, because collapsing them is how a denial gets misread as an execution. The mapping
to the kernel's own vocabulary is exact:

| Concept | Kernel / pipeline vocabulary | Phase 1 representation |
|---|---|---|
| Requested action | `action=f"tool:{adapter.id}"` passed to `authorize_and_log` | derivable as `tool:<tool_id>`; the *target* of the request is `target_id`, the invocation is `action_id` |
| Requested permissions | `adapter.kernel_permissions` | `tool.attempted.payload.resource_ref` (no permission list — it is a static adapter property, so storing it per event would be noise) |
| Authorization decision | `SecurityDecision.allowed` / `denied_permissions` / `reason` | **its own row**: `tool.allowed` (payload `granted_permissions`) or `tool.denied` (payload `denied_permissions`, `reason`); `correlation_id` = the audit `request_id` |
| Observed outcome | `AdapterResult.success` / `error` | a **separate** row with the same `action_id`: `tool.executed` or `tool.failed` (`error_kind`; `denial_kind` only for adapter-level refusals) |
| No decision at all | `kernel_permissions == []` | no `allowed`/`denied` row exists; the absence is meaningful (§9 T1b) |

Consequences the tests enforce: a denied invocation has a `tool.denied` row and **no**
terminal row for that `action_id` (§9 T2); a `tool.allowed` row without its preceding
`tool.attempted` is a defect (§3.4); and an adapter-level refusal (`resource.*.denied`,
`tool.failed` with `denial_kind`) is never written as `tool.denied`, because the kernel
allowed it. There is deliberately **no** `decision` column and **no** `success` column: a
single boolean column would invite exactly the conflation the design forbids.

Two clocks only, both stated: `occurred_at` is the emitting process's wall clock at the
event site; `recorded_at` is the insert clock. Neither is ever used for ordering (§8.4).

---

## 4. Field-level provenance table

| Field | Type | Req | Authoritative source | Validation | May contain sensitive data? | If unavailable |
|---|---|---|---|---|---|---|
| `seq` | int | yes | database (autoincrement) | monotonic per insert; never supplied by caller | no | insert fails ⇒ event dropped + counted (§8) |
| `event_id` | uuid4 | yes | `uuid.uuid4()` in the writer | RFC-4122 v4 string | no | impossible (generated) |
| `schema_version` | int | yes | writer constant | must equal known version | no | refuse to write |
| `event_type` | enum | yes | emitting call site (constant) | closed set (§3.3) | no | refuse to write |
| `occurred_at` | float | yes | `time.time()` captured at the emission site | finite, > 0 | no | impossible |
| `recorded_at` | float | yes | DB default / writer | `max(occurred_at, time.time())` | no | impossible |
| `actor_id` | string | yes | `AdapterContext.actor_id` (pipeline constant `PIPELINE_ACTOR`) | `^[A-Za-z0-9:_-]{1,100}$`; never from `params` | no | omit event (no actor ⇒ no provenance) |
| `actor_type` | enum | yes | derived: `service` for pipeline actor, `user` for `<user>` actors | closed set | no | refuse |
| `user_id` | uuid | yes | **`tasks.user_id`** read from the database at run start | 36-char uuid; re-verified once per run against `tasks` | no (identifier only) | omit event + count `skipped_no_provenance` |
| `task_id` | uuid | yes | `AdapterContext.task_id` (pipeline), validated against `tasks` | FK + 36-char uuid | no | omit event |
| `run_id` | uuid | yes* | `execution_run.id` created by the pipeline | 36-char uuid; verified once to belong to `task_id` | no | omit event (*required for Phase 1 emissions; the column is nullable for future non-run events) |
| `step_node_id` | string | no | `AdapterContext.step_id` (pipeline-assigned node id) | `^[A-Za-z0-9_-]{1,64}$` | no | null (tool used outside a step) |
| `tool_id` | string | yes† | `AdapterRegistry` (`adapter.id`) | `^[a-z0-9_]{1,64}$` | no | omit event (†required for tool/resource/endpoint events) |
| `action_id` | uuid | yes† | registry, once per invocation | 36-char uuid | no | omit event |
| `target_kind` | enum | no | emitting adapter | `resource` \| `endpoint` | no | null |
| `target_id` | string | no | normalizer in the writer (from a scope-relative path or a parsed URL) | see below | **possibly** (user file names) | null + count `invalid_target` |
| `outcome` | enum | yes | emitting call site | closed set + §3.4 invariants | no | refuse |
| `evidence_class` | enum | yes | emitting call site (`observed`, or `verified` for artifacts) | closed set | no | refuse |
| `source` | string | yes | constant table keyed by `event_type` | exact match to the table | no | refuse |
| `correlation_id` | string | no | `SecurityDecision.request_id` (new field, from the kernel's `ActionContext`) | `^[A-Za-z0-9-]{1,100}$` | no | null (events with no kernel decision) |
| `idempotency_key` | string | yes | deterministic recipe (§3.5) | ≤ 200 chars, unique | no | refuse |
| `payload` | JSON | no | emitting call site, filtered by the writer | allowlist + ≤ 8 KiB canonical JSON + scalar values | **possibly** (basenames) | dropped to `null` if invalid, event still recorded |
| `payload_hash` | hex | yes | computed by the writer | `^[0-9a-f]{64}$` | no | refuse |

`target_id` normalization rules (reject, never "fix"):

- Resource: derived from the **already-clamped** path relative to the task scope:
  `res:<task_id>/<relpath with "/" separators>`; reject if the relpath is absolute, contains
  `..`, a drive letter, a NUL, or exceeds 500 chars; reject if the resolved path is outside
  the task scope (defence in depth — the adapter already clamped it).
- Endpoint: `net:<host>/<path>` from the parsed URL with **no** query string, fragment,
  userinfo, or port; reject otherwise.

---

## 5. Audit and filesystem-read instrumentation plan

### 5.1 Audit linkage (gap 1)

**Schema (D2 = `RESOLVED — EXISTING CONTRACT`): a new side table, not new columns.** Design
§13 Phase 1 mandates an additive migration with "no changes to existing tables", and
`audit_events` is forensic evidence that V2 tooling already reads. Adding five columns would
edit that table's shape; the side table leaves it byte-identical. The earlier column
recommendation is therefore withdrawn.

`audit_links`:

| Column | Type | Null | Notes |
|---|---|---|---|
| `event_id` | string(64) PK | no | FK → `audit_events.event_id` (unique), `ON DELETE CASCADE`; exactly one link row per audit row |
| `user_id` | string(36) | yes | owning user; NULL only for audit paths with no task subject |
| `task_id` | string(36) | yes | historical task id; **deliberately no FK** — deleting a task must not erase audit linkage |
| `run_id` | string(36) | yes | `execution_runs.id` |
| `step_node_id` | string(64) | yes | pipeline node id (`step-N`) |
| `action_id` | string(36) | yes | tool invocation |

Indexes (names fixed, because the zero-drift test compares them): `ix_audit_links_user_id`,
`ix_audit_links_task_id`, `ix_audit_links_run_id`, `ix_audit_links_action_id`. No link row is
written when every linkage field would be null (those rows are genuinely unattributed, §5.8).

**Code path (all additive, all defaulted).** The kernel builds its `ActionContext`
*internally* in `authorize_and_log`, so the linkage has to travel through it — this is the
exact shape the current code requires:

1. `AuditEvent` dataclass gains five optional fields (default `None`); `to_dict()` includes
   them (so in-memory reads and persisted reads agree).
2. `AuditTrail.log(...)` gains keyword-only `user_id=None, task_id=None, run_id=None,
   step_node_id=None, action_id=None` and passes them into the event.
3. `AuditTrail.flush_pending` inserts the `AuditEventModel` row **and**, when any linkage
   field is set, the `AuditLinkModel` row in the **same transaction** (existing per-`event_id`
   idempotency and `resource[:500]` truncation unchanged). A failure rolls back both and the
   events stay pending, so no audit row is ever persisted without the link that was meant to
   accompany it, and no link can reference a missing audit row. The existing crash window is
   safe: a crash after commit but before dequeue is caught by the `event_id`-exists check on
   the next flush, so a duplicate link is never attempted (its PK would otherwise raise).
4. `AuditTrail.read_from_db` left-joins `audit_links` and returns the same keys plus the five
   linkage keys (`None` when no link row exists).
5. `ActionContext` gains the same five optional fields; `SecurityKernel.authorize_and_log`
   gains the same keyword-only parameters, passes them into `ActionContext`, and
   `record_audit` forwards them to `AuditTrail.log`. `authorize()` itself is untouched —
   linkage is evidence, never an input to the decision.
6. `SecurityDecision` gains `request_id: str = ""`, populated by `authorize_and_log` from
   the `ActionContext` it created (D12 = `RESOLVED — EXISTING CONTRACT`: design §4.4/§5.1
   require `correlation_id = kernel request_id`, and today that id is written to the audit row
   then discarded, so the correlation could not be honoured without this field). Additive,
   defaulted, no behaviour change.
7. `AdapterRegistry.run` supplies the linkage from `ctx` (`user_id`, `task_id`,
   `execution_run_id` → `run_id`, `step_id` → `step_node_id`, plus the new `action_id`) and
   uses `decision.request_id` as `correlation_id` on the tool events.

**Backward compatibility:** existing `audit_events` rows are untouched and have no link row,
so the five keys read as `null` for them (never backfilled, never fabricated). The audit
table's shape is unchanged, so any existing forensic query or external reader keeps working.
`/api/v1/audit` returns a plain dict (no response model), so five added nullable keys are
non-breaking; the existing tests that read `event_id/actor_id/action/allowed` continue to
pass.

**FK honesty:** this repo does not enable `PRAGMA foreign_keys` on SQLite, so FKs are
enforced by PostgreSQL and merely *declared* on SQLite. The link/row invariant therefore rests
on the single-transaction write above, not on the FK — and the tests assert the transaction
behaviour, not the constraint.

### 5.2 Emission points (exactly five, all existing choke points)

| # | Point (file:function) | Events | When | Identity source |
|---|---|---|---|---|
| 1 | `integrations/adapters/registry.py::AdapterRegistry.run` | `tool.attempted`, `tool.allowed`, `tool.denied`, `tool.executed`, `tool.failed` | before the kernel decision; after the decision; after `adapter.run` | `ctx` (actor/task/run/step) + registry `action_id` |
| 2 | `integrations/adapters/builtin.py::FileReadAdapter.run` | `resource.read.observed` (`executed`/`failed`), `resource.read.denied` | after clamp; after the tool returns | `ctx` + `ctx.action_id` (typed field, D10) |
| 3 | `integrations/adapters/builtin.py::FileWriteAdapter.run` | `resource.write.committed`, `resource.write.denied` | after quota/clamp; after the tool returns | `ctx` + same `action_id` |
| 4 | `integrations/adapters/http_get.py::HttpGetAdapter.run` | `endpoint.connect.attempted`, `.succeeded`, `.denied` | after URL validation, before the request; after the response; on refusal | `ctx` + `action_id` |
| 5 | `apps/api/app/main.py::_register_task_artifact` | `artifact.registered` | after the artifact row commits | `task_id`, `user_id` resolved from the task row, plus the run id passed in |

How the adapter knows its `action_id` (D10 = `RESOLVED — SECURE DEFAULT`): a **typed,
defaulted `action_id` field on `AdapterContext`**, set by the registry immediately before
`adapter.run`. The alternative (`ctx.extra["v3_action_id"]`) is rejected because `extra` is an
untyped grab-bag with no production consumer today (`grep` shows zero non-test uses), so a
dict key would be the weakest possible carrier for provenance: no type, no default, no
discoverability, and every future caller free to write the same key. The typed field keeps
`run(params, ctx)` and `AdapterResult` unchanged, and adapters still emit nothing when it is
empty. The anti-spoof check remains a live-registry-set membership test (§6.4).

### 5.3 `fs_read` (gap 2)

```
FileReadAdapter.run:
  clamped = self._clamp(path, ctx)
  if clamped is None:
      AuditTrail.instance().log(..., allowed=False, resource=path,
                     details={"reason": "path outside workspace"}, + linkage from ctx)
      emit resource.read.denied   {reason: "outside_workspace", requested_basename: basename(path)}
      return AdapterResult(success=False, error=...)
  result = await self._tool.execute(...)
  if result.success:
      AuditTrail.instance().log(..., allowed=True, resource=clamped,
                     details={"reason": "read", "size": result.output["size"]} + linkage)
      emit resource.read.observed {outcome: executed, size, encoding, relpath}
  else:
      emit resource.read.observed {outcome: failed, error_kind: classify(result.error), relpath}
  return AdapterResult(...)          # unchanged success/error/structure
```

Notes:

- The **content** never enters an event or the audit details; only size/encoding/relpath.
- `classify(error)` maps the tool's existing messages (`File not found`, `File too large`,
  `Access denied`) to the enum; unknown text becomes `unknown` and the text is not stored.
- A **denial** (clamp refusal) is now both audited (as before) **and** emitted as
  `resource.read.denied`, closing the "successful reads are invisible" gap without changing
  the adapter's return value.

### 5.4 `fs_write`

- Success ⇒ `resource.write.committed` (`bytes`, `created`/`overwritten` derived from the
  tool output when present, `relpath`) + a **new** success audit entry
  (`action="tool:fs_write", allowed=True`), emitted **only while `V3_EVENTS_ENABLED` is on**
  — §3.1/T10 requires the flag-off audit stream to stay byte-identical, and existing suites
  count `tool:fs_write` rows (T36). The same rule applies to the read success entry in §5.3. D11 = `RESOLVED — SECURE DEFAULT`: a write
  changes state, so an auditor restricted to `audit_events` must be able to see that it
  happened, not only that some writes were refused; the asymmetry (denials recorded, successes
  not) is an evidence hole. The entry carries no content. Cost: one extra audit row per
  successful write, batched by the existing flusher; it does not replace the graph event.
- Clamp refusal / `WorkspaceQuotaExceeded` ⇒ `resource.write.denied` (`reason:
  outside_workspace | quota`, `bytes`, `quota_kind` when available). The existing audit
  denial entries are unchanged.
- `evidence_class` stays `observed`: an adapter's claim is not verification. The **verified**
  signal is `artifact.registered`, written only after `_register_task_artifact` hashed the
  bytes on disk.

### 5.5 `http_get`

- After `_check_url` passes and **before** the first request:
  `endpoint.connect.attempted` with `host`, `path_template` (path only).
- On an accepted 2xx text response: `endpoint.connect.succeeded` (`status`, `bytes`).
- On any validation refusal (scheme, allowlist, userinfo, port, IP-literal, SSRF) **or** a
  redirect hop refusal: `endpoint.connect.denied` with `reason` + `denial_layer:
  "adapter_policy"`. No connection was made in these cases, and the event says so.
- Redirect hops are **not** individually graphed in Phase 1 (only the initial URL and the
  final outcome). This is recorded as a coverage gap in the Phase 2 source-status map.

### 5.6 Tool lifecycle in the registry (pseudo-code, exact ordering)

```
adapter = self.get(adapter_id)
action_id = uuid4()
provenance = provenance_from(ctx)          # None when ctx lacks user/run
if provenance: emit tool.attempted(outcome=attempted, action_id, tool_id=adapter.id)
ctx.action_id = action_id                  # typed field; never trusted for identity
correlation = None
if adapter.kernel_permissions:
    decision = kernel.authorize_and_log(
        actor_id=ctx.actor_id,
        action=f"tool:{adapter.id}",
        resource=ctx.resource,
        permissions=list(adapter.kernel_permissions),
        actor_type="service",
        user_id=ctx.user_id, task_id=ctx.task_id, run_id=ctx.execution_run_id,
        step_node_id=ctx.step_id, action_id=action_id,
    )
    correlation = decision.request_id     # new field, added in S4
    if provenance:
        emit tool.allowed if decision.allowed else tool.denied
    if not decision.allowed:
        return AdapterResult(permission_denied=True, ...)   # unchanged
try:
    result = await adapter.run(params, ctx)
except Exception:
    if provenance: emit tool.failed(error_kind="exception", correlation_id=correlation)
    raise
if provenance: emit (tool.executed | tool.failed, correlation_id=correlation)
return result
```

The kernel call order and its return value are **unchanged**; the events are emitted around
it. `tool.denied` is emitted *after* the kernel decision and *before* returning the denial,
so a denied invocation has no `executed`/`failed` sibling (§3.4).

### 5.7 `artifact.registered`

`_register_task_artifact` already resolves and hashes the file. After its commit, emit
`artifact.registered` with `evidence_class="verified"`, `payload={artifact_id, sha256,
size_bytes, filename}`, `target_id="res:<task>/<relpath>"`. `user_id` is resolved from the
`tasks` row (one primary-key lookup, already inside the function's transaction scope) — never
from the caller. D7 = `RESOLVED — EXISTING CONTRACT` (design §4.3 requires this event for the
`verified` `writes` edge; it extends the design's Phase 1 file list by one function in
`main.py`).

### 5.8 Legacy and unprovenanced behaviour

| Situation | Behaviour |
|---|---|
| Existing `audit_events` rows | No `audit_links` row, so the five keys read `null`; the table itself is unchanged, and all existing reads/endpoints behave identically |
| Audit row written by a path with no task subject (e.g. an API admin action) | `user_id` may be set, `task_id`/`run_id` `null`; linkage is partial by fact, not by omission |
| Adapter invoked directly (tests, scripts) without a provenance-bearing `ctx` | **No** `graph_events` row; `graph_events_skipped_no_provenance` increments. D8 = `RESOLVED — EXISTING CONTRACT` (design §4.4: `user_id` is `NOT NULL`; design §11.1: missing evidence is `unknown`, never invented). Counting keeps the gap visible instead of silent |
| Pipeline run | Full linkage; every tool invocation produces at least `tool.attempted` + one terminal event |
| `V3_EVENTS_ENABLED` unset/false | No events at all; zero new DB writes; existing behaviour byte-for-byte. D5 = `RESOLVED — EXISTING CONTRACT`: design §13 Phase 1 fixes the flag's name and "default off"; the new tests set it explicitly, so default-off does not reduce coverage in CI |

### 5.9 What Phase 1 still cannot observe (stated, not papered over)

- **Sandboxed child process file reads/writes** (python/shell/codegen/DoD): no syscall
  instrumentation exists. Phase 1 emits **no** resource events for them; Phase 2 must
  represent them as `declared can-access` scope, never as observed access.
- **LLM provider calls**: no hook exists; no `endpoint.connect` events for them.
- **Redirect hops** inside `http_get`: initial URL + final outcome only.
- **Which file a `codegen`/`finalize` write touched before the artifact register runs**: the
  artifact event is the verified record; there is no separate adapter-level write event for
  codegen in Phase 1.

These gaps must appear in the Phase 2 `missing_sources` list; inventing events for them
would be a fabricated graph.

### 5.10 Interrupted operations (denied / failed / interrupted / successful)

| Case | What is written | What readers must conclude |
|---|---|---|
| Denied by the kernel | `tool.denied`, outcome `denied` | the action did not run |
| Denied by a clamp/quota after kernel approval | `resource.*.denied` (or `tool.failed`) | the action ran but touched nothing |
| Adapter returned a failure | `tool.failed` / `resource.read.observed(failed)` | attempted, no verified effect |
| Adapter returned success | `tool.executed` / `resource.write.committed` | **observed** effect only — not verified |
| Cancelled or timed out mid-action (step aborted, process killed) | **no** terminal event is fabricated | `unknown`: an `attempted` with no sibling proves nothing about the effect |
| Crash between the action and the event insert | same as above | `unknown`; never "no access" |

The rule is stated once for Phase 2/4: **absence of a terminal event is `unknown`, never
"did not happen"**. Phase 1 therefore has no "interrupted" writer path at all — a synthetic
`interrupted` event would claim a fact (that the action stopped at that point) that the
process cannot observe from inside a killed step.

---

## 6. Identity and ownership validation

### 6.1 Trusted sources (single source per field)

| Field | Comes from | Explicitly NOT from |
|---|---|---|
| `actor_id` | `PIPELINE_ACTOR` constant in `main.py` / kernel caller | request body, `step_params`, tool params |
| `user_id` | `tasks.user_id` read from the DB at run start | JWT claims at adapter level (adapters never see the request), `ctx.extra`, params |
| authenticated user | the task owner, established *before* execution by `get_task_by_id(db, task_id, current_user.id)` (the 404-on-mismatch ownership check); the graph event carries that owner id | a token decoded inside a step — adapters have no request scope, so re-deriving identity there would be both impossible and spoofable |
| `task_id` | the pipeline's `task_id` (already ownership-checked by `get_task_by_id(db, task_id, user.id)`) | `params`, `ctx.extra` |
| `run_id` | `execution_run.id` created by `run_task_pipeline` | params, events payloads |
| `step_node_id` | pipeline `node_id` (`step-N`) | params |
| `tool_id` | `adapter.id` (registry state) | `params["tool"]` |
| `action_id` | registry-generated uuid4, published on `AdapterContext` only after generation | any caller input, `ctx.extra`, `params` |

### 6.2 `AdapterContext` extension (D1 = `RESOLVED — SECURE DEFAULT`)

Add three defaulted fields:

```python
@dataclass
class AdapterContext:
    actor_id: str
    task_id: str = ""
    step_id: str = ""
    resource: str = ""
    workspace: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""            # NEW: owning user (trusted, set by the pipeline)
    execution_run_id: str = ""   # NEW: first-class run id (trusted, set by the pipeline)
    action_id: str = ""          # NEW: this invocation's id (set by the registry, D10)
```

Why this is the secure default rather than `ctx.extra`: the provenance invariant is that
identity and action ids come from the **trusted execution context**. A typed field is set in
one place, has a visible default, is covered by the dataclass' own validation surface, and
cannot be confused with adapter-specific scratch data; `extra` is untyped, currently unused
in production code, and would let any caller pre-populate a look-alike key. Typing does not
replace the anti-spoof check (§6.4) — it removes a class of confusion before that check runs.

All 23 existing construction sites keep working. `_build_worker` gains `user_id` and
`execution_run_id` parameters; its single pipeline call site (`main.py`, inside the step
loop, next to `actor_id=PIPELINE_ACTOR`) passes `task.user_id` and `run_id` — both already
in scope there. `_build_worker` already builds the `AdapterContext` at the `registry.run`
call, so the wiring is two keyword arguments in that literal.

### 6.3 Per-run provenance handle

New module `services/graph/provenance.py`:

```python
@dataclass(frozen=True)
class Provenance:
    task_id: str
    user_id: str
    run_id: str
    actor_id: str
    actor_type: str = "service"
    step_node_id: str | None = None
    def fields(self) -> dict: ...       # for kernel/audit kwargs

async def resolve_run_provenance(db, task_id: str, run_id: str) -> Provenance:
    """One query: task.user_id + verify execution_runs.id belongs to task_id."""
```

`run_task_pipeline` calls it **once**, immediately after the `ExecutionRun` row commits, and
re-uses the handle for every worker in that run. Failure (task missing, run missing, run
belongs to another task) raises `ProvenanceError` **before** any step executes — a
programming/integrity fault must not produce an unprovable run.

Cost: one extra SELECT per run (not per event). This is the only DB verification on the hot
path.

### 6.4 Anti-spoof rules (enforced by construction + tests)

1. The writer accepts **explicit keyword arguments only** — no `**kwargs`, no dict spread —
   so a caller cannot smuggle an identity field through an arbitrary dict.
2. Adapters never pass identity; they pass only `tool_id`, target, outcome and payload. The
   registry supplies `action_id`; the handle supplies identity.
3. `ctx.action_id` is written by the registry *after* it generates the id, and adapters
   treat it as untrusted input: the writer validates that the `action_id` it receives
   **matches the action_id the registry registered for this invocation** (a per-process set
   of live action ids) before accepting an adapter-level event. An adapter that overwrites
   the field with an id the registry never issued is refused (T16).
4. Nothing in the graph writer reads `params`, `request.body`, or environment variables.
5. `user_id` is never accepted from a request path: no Phase 1 API exists.

### 6.5 Cross-user invariants

- `user_id` is denormalized from the task row and validated against it at run start, so a
  row cannot claim a user the task does not belong to. `task_id` also has a DB foreign key;
  note that SQLite in this repo runs without `PRAGMA foreign_keys`, so the FK is enforced on
  PostgreSQL and declared-only on SQLite — the guarantee is the run-start validation plus the
  FK where it is enforced, never the FK alone.
- The writer refuses (`ProvenanceError`, no row) if a supplied `user_id` differs from
  `tasks.user_id` — exercised by a test that forges the user id.
- All future reads filter on `user_id`; Phase 1 adds no read API, so no read path can leak
  yet. The column is indexed accordingly (`user_id, seq`).

### 6.6 Legacy compatibility

Legacy audit rows stay exactly as they are: each keeps its place in `audit_events` and simply
has no `audit_links` row, so its linkage reads `null` and is never reclassified, guessed, or
backfilled. Legacy `task_events` are untouched. No pre-V3 graph events exist and **no
backfill** synthesizes them from audit rows or artifacts (that would fabricate provenance).

---

## 7. Database and migration design

### 7.1 Is a migration needed? Yes

Two **new tables** and no alterations to anything existing: `graph_events` (§3.1) and
`audit_links` (§5.1). This is exactly the shape design §13 Phase 1 requires:
"additive table + indexes; no changes to existing tables".

### 7.2 Revision `003` (`apps/api/alembic/versions/003_graph_events_audit_links.py`)

- `revision = "003"`, `down_revision = "002"`.
- `upgrade()`:
  1. `op.create_table("graph_events", ...)` with the columns/constraints of §3.1
     (`sqlite_autoincrement=True` on the table for the `seq` non-reuse guarantee).
  2. `op.create_index(...)` for the five `graph_events` indexes.
  3. `op.create_table("audit_links", ...)` with the columns/PK/FK of §5.1.
  4. `op.create_index(...)` for the four `audit_links` indexes.
- `downgrade()`: drop the four `audit_links` indexes, drop `audit_links`, drop the five
  `graph_events` indexes, drop `graph_events`.
- **No** `ALTER TABLE`, no data migration, no backfill, no type change on an existing table.
- Index names: all ≤ 63 characters (Postgres identifier limit), all lowercase.

### 7.3 `init_db()` — no compatibility guard is needed

Because no existing table is altered, the `_ensure_outcome_column`-style ALTER guard the
earlier draft required is **removed from the plan**: `Base.metadata.create_all` creates both
new tables on a fresh database and on a pre-existing one, and there is nothing for an ALTER
shim to add. The writers still treat a missing table as a non-fatal condition (§8), so a
deployment that runs new code against an un-migrated database loses events, not execution.

### 7.4 Model registration and the zero-drift test

- New models `apps/api/app/models/graph_event.py` (`GraphEvent`) and
  `apps/api/app/models/audit_link.py` (`AuditLink`), both imported in
  `apps/api/app/models/__init__.py` (and `__all__`), so `Base.metadata` contains them.
- `tests/unit/test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema`
  must keep passing: the migration must produce exactly the models' columns and indexes. The
  models' index names must therefore match §7.2 verbatim.
- `apps/api/app/models/audit_event.py` is **not modified**. That is now a testable property,
  not a promise: T19b asserts its column set is unchanged, so the "no changes to existing
  tables" constraint cannot be quietly broken later.

### 7.5 Transaction boundaries

- Each graph event is written in its own short transaction on a fresh session (`async with
  async_session_factory()` → `add` → `commit`), matching `create_task_event` and
  `_emit_step_event` conventions.
- No graph event write joins the pipeline's request transaction: a failed event must never
  roll back task state.
- The audit link is written in the **same** transaction as the audit row it describes
  (§5.1), so the pair is atomic; a failure leaves the audit event queued for retry.
- The artifact event is emitted **after** the artifact commit (never inside it), so a
  verified event cannot exist without the row it references.

### 7.6 SQLite and PostgreSQL compatibility

| Concern | SQLite | PostgreSQL | Action |
|---|---|---|---|
| Autoincrement PK | `INTEGER PRIMARY KEY AUTOINCREMENT` (never reuses) | `SERIAL` (sequence never reuses) | `sa.Integer, primary_key=True, autoincrement=True` + `sqlite_autoincrement=True` in both model and migration |
| JSON payload | stored as TEXT, `json` round-trip via SQLAlchemy | `JSON` | `sa.JSON` (already used by `audit_events.details`) |
| Schema change kind | `CREATE TABLE` only | `CREATE TABLE` only | no `batch_alter_table`, no `ALTER`, so neither dialect's column-alter limits apply |
| FK enforcement | **declared only** — no `PRAGMA foreign_keys` in this repo | enforced | the atomic write (§5.1) and the run-start ownership check are the guarantees; tests assert behaviour, not the constraint |
| Index size | n/a | 63-char identifier cap | names fixed, short |
| Concurrent writes | database-level write lock | MVCC, `40P01`/`40001` | writer retries only concurrency conflicts, classified by SQLSTATE on PG and by message on SQLite (§8.2) |

PostgreSQL behaviour is verified only where a real PG URL is supplied: `T34` plus the
`test_postgres_phase1.py` gate cover schema, round-trip, the atomic audit/link write, FK
cascades, legacy rows and restart. The `backend-tests` CI job uses SQLite; a dedicated
`postgres-gate` job runs the PG tests against `postgres:16-alpine`. The PG-specific tests are
marked and skipped with an explicit reason when `RAIVISOX_TEST_POSTGRES_URL` is absent — with
no URL **PG verification is not claimed** (§9, §11.1 R11). A recorded PostgreSQL 16.9 run is in
[v3-phase-1-acceptance.md](v3-phase-1-acceptance.md) §7.

### 7.7 Existing records

`audit_events` keeps every row, unchanged in place, with no link row (so its linkage reads
`null`). `graph_events` and `audit_links` start empty. No `task_events`, `execution_runs`, or
artifact rows are modified, and no existing table's DDL is touched.

### 7.8 API: deferred (explicit)

No endpoint is added in Phase 1. Verification is by direct DB assertions and by existing
audit endpoints continuing to work. The first read API arrives in Phase 2/4
(`/events`, `/exposure`). This keeps Phase 1's security surface at zero new public routes.

---

## 8. Failure semantics

### 8.1 Behaviour matrix

| Condition | Behaviour | Observable signal | Changes execution? |
|---|---|---|---|
| `V3_EVENTS_ENABLED` unset/false (the default, D5) | no writes, no lookups | none | no |
| `graph_events` table absent (migration not applied) | writer catches, disables itself for the process | `graph_events_write_failures` + one warning per process | no |
| `ctx` has no provenance (direct adapter use) | event skipped | `graph_events_skipped_no_provenance` | no |
| Duplicate `idempotency_key` (`IntegrityError`) | treated as success (already recorded) | `graph_events_duplicates` | no |
| Transient concurrency conflict — PG SQLSTATE class `40` (`40P01` deadlock, `40001` serialization failure) plus `55P03`, or SQLite "database is locked" | up to 3 attempts with 50 ms / 200 ms backoff, each in a **fresh session/transaction**; the provenance read follows the same rule | `graph_events_retries`; on final failure `graph_events_write_failures` | no |
| Non-transient DB error (constraint `23xxx`, authorization `42501`, undefined table `42P01`, connection/configuration `08xxx`/`28P01`, cancelled `57014`) | no retry; a missing table additionally disables recording | `graph_events_write_failures` + safe warning (event type, task id, error class — no payload) | no |
| Cancellation during a retry backoff | `CancelledError` propagates immediately; it is neither counted as a write failure nor swallowed | none | no (shutdown is not an observation failure) |
| Payload invalid (unknown keys / oversized / secret heuristic hit) | event dropped, not rewritten | `graph_events_invalid` + warning naming the field | no |
| `target_id` normalization fails | event kept with `target_id = NULL` | `graph_events_invalid_target` | no |
| Provenance mismatch at run start | `ProvenanceError` raised **before steps** | run fails with an explicit error | **yes, deliberately** — an unprovable run must not execute |
| Audit flush failure | unchanged existing behaviour (events stay queued, retried) | existing | no |
| Audit link insert fails | the whole flush transaction rolls back, so the audit row is not persisted either; events stay queued and retry | existing flush diagnostics | no |
| Process crash mid-run | events already committed survive; the un-flushed tail is lost (same honesty note as the audit trail) | none | no |

### 8.2 Retry policy (concrete)

**Classification comes from the driver, not from the exception class alone.** `emit()`
retries a `DBAPIError` only when it is a *concurrency conflict*:

- PostgreSQL: the driver's SQLSTATE, read from `sqlstate`/`pgcode` on the original driver
exception (the whole `orig`/`__cause__`/`__context__` chain is inspected, because drivers
disagree on the attribute name and on where they wrap the error). Transient = class `40`
(`40001` serialization failure, `40P01` deadlock detected) and `55P03`
(`lock_not_available`). A SQLSTATE-shaped substring inside a message is not accepted as
evidence.
- SQLite: the existing message markers ("database is locked", "database table is locked",
  "database schema is locked"), which is how that engine reports contention.

Everything else is raised on the first attempt: constraint violations (`23xxx`), insufficient
privilege (`42501`), undefined table (`42P01`, which instead disables recording for the
process), failed connections (`08xxx`), authentication (`28P01`), `25P02`
(`in_failed_sql_transaction` — the *effect* of an earlier abort, not a cause) and
`57014` (cancelled). Retrying those would only delay an honest failure.

**The transaction is always restarted.** Each attempt opens a new session, so a retried
write never continues inside a transaction PostgreSQL has already aborted (that would fail
with `25P02`). The number of attempts is bounded at three (two retries) with 50 ms and
200 ms backoff; the backoff is `await`ed, so a shutdown cancels it and `CancelledError`
propagates instead of being counted as a failure.

**What a retry cannot do:** it reuses the same `event_id`/`idempotency_key`, so it cannot
double-write; if the previous attempt did commit but lost its connection before reporting
success, the retry collides on the unique key and is counted as a `duplicate` (the fact is
recorded once). `IntegrityError` keeps that meaning. When the budget is exhausted the final
database error propagates out of the retry boundary and `emit()` converts it to `False` +
`graph_events_write_failures` — observation still never breaks execution (§8.1). The audit
flusher is different on purpose: `AuditTrail.flush_pending` re-raises its final error and
leaves the events queued for the next flush.

Tests: `tests/unit/test_transient_retry.py` (classification table, bounded retries,
propagation, cancellation, dedupe after retry) runs on every host; `T34j–T34o` prove the
same policy against a real PostgreSQL server, including a genuine deadlock (`40P01`) and a
genuine SERIALIZABLE conflict (`40001`) produced by the server itself.

### 8.3 No partial events

A graph event is one row, so there is no multi-row transaction to leave half-done. The one
place where two rows must agree — an audit row and its `audit_links` row — is committed in a
single transaction, so a link can never outlive its audit row or vice versa. If a lifecycle
event fails while its sibling succeeds, the gap is **honest**: readers see `attempted`
without `executed` and must treat it as `unknown` (Phase 4 rule), which is exactly what the
design requires.

### 8.4 Ordering and timestamps

- `seq` provides total order; readers must never order by `occurred_at`.
- **`seq` orders inserts; it does not assert causality or identity.** Two events' relationship
  comes from `action_id` (same invocation), `correlation_id` (same kernel decision) and
  `target_id` (same resource); a lower `seq` never by itself proves that one event caused
  another. A gap in `seq` is a gap in the write log, not a claim that something was omitted.
- `occurred_at` is the emitting process's wall clock (single API process in the current
  deployment; multi-replica skew would show as non-monotonic timestamps but never as wrong
  ordering).
- `recorded_at = max(occurred_at, time.time())` guards a backwards clock step at insert.
- Deleted/rolled-back inserts leave gaps in `seq`. Gaps are expected and never renumbered.

### 8.5 What Phase 1 deliberately does NOT do

- It does not block an action when recording fails (no enforcement exists yet). The design's
  fail-closed matrix applies to `enforce` mode in Phase 5; Phase 1 is observation only.
- It does not buffer events in memory for later flush. A lost event is counted, not queued,
  so a crash cannot resurrect stale evidence out of order.

---

## 9. Acceptance test matrix

Conventions: file-backed SQLite via `DATABASE_URL=sqlite+aiosqlite:///<tmp>` pinned before
importing `app.main` (as in `test_capability_audit.py`), `asyncio_mode=auto`, real
`AdapterRegistry`/kernel (no stubs for the gate), and direct SQL assertions on
`graph_events` / `audit_links` / `audit_events`. Each test sets `V3_EVENTS_ENABLED=1` via fixture unless the
test is about the flag. **Every test path below is a new file** except
`test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema` (existing,
marked *existing*).

| ID | Test (file::name) | Setup | Action | Expected | Security rationale |
|---|---|---|---|---|---|
| T1 | `test_graph_events.py::test_tool_lifecycle_records_attempt_allow_execute` | real kernel with `read_files` granted to `pipeline-service`, `AdapterContext` with user/task/run, real file in scope | `registry.run("fs_read", params, ctx)` | 3 rows sharing one `action_id`: `attempted`, `allowed`, `executed`; `evidence_class=observed`; `seq` strictly increasing | proves each invocation is provable end-to-end |
| T1b | `test_graph_events.py::test_permissionless_adapter_has_no_allowed_event` | `text_transform` (`kernel_permissions == []`) | `registry.run` | `attempted` + `executed` only, **no** `tool.allowed` (no kernel decision was made) | no event claims an authorization that never happened |
| T2 | `test_graph_events.py::test_denied_tool_has_no_executed_sibling` | kernel without the adapter's permission | `registry.run` | `tool.denied` present; **no** `executed`/`failed` for that `action_id`; adapter body not called (spy) | a denial must never be recorded as an execution |
| T3 | `test_graph_events.py::test_adapter_failure_records_failed` | adapter that raises | `registry.run` | `tool.failed` with `error_kind=exception`; exception still propagates | failure is visible without swallowing the error |
| T4 | `test_graph_events.py::test_payload_allowlist_drops_unknown_keys` | direct `emit()` with `payload={"content": "SECRET", "size": 1}` | emit | row exists, `payload` has only `size`; `content` absent | content can never enter the graph |
| T5 | `test_graph_events.py::test_secret_like_value_rejects_event` | `emit()` with `payload={"reason": "sk-" + 40 chars}` | emit | no row; `graph_events_invalid` incremented | a secret-shaped value is refused, not silently stored |
| T6 | `test_graph_events.py::test_oversized_payload_rejected` | payload of 9 KiB | emit | no row (or `payload=NULL` per §3.6 policy), counter incremented | bounded storage, no partial secrets |
| T7 | `test_graph_events.py::test_duplicate_idempotency_key_is_a_no_op` | emit the same event twice | two emits | exactly one row; duplicates counter = 1 | retries cannot double-count |
| T8 | `test_graph_events.py::test_seq_gap_is_preserved_and_ordering_uses_seq` | insert A, force a failed insert, insert B | read ordered | B after A; gap tolerated; no renumbering | ordering is honest, not timestamp-derived |
| T8b | `test_graph_events.py::test_seq_is_never_reused_after_delete` | insert rows, delete the newest (or delete a task, cascading), insert again | read `seq` values | new `seq` strictly greater than every previously issued value | a cursor can never skip or replay evidence |
| T9 | `test_graph_events.py::test_events_survive_restart` | emit, dispose engine, re-open | query | rows still present with same `seq` | evidence survives a restart |
| T10 | `test_graph_events.py::test_flag_off_writes_nothing` | `V3_EVENTS_ENABLED` unset | full adapter run | zero `graph_events` rows; existing behaviour unchanged | the feature is inert by default |
| T10b | `test_graph_events.py::test_missing_table_is_non_fatal` | flag on, `graph_events` table renamed away / DB at revision `002` | full adapter run | adapter result unchanged, run completes; `graph_events_write_failures` incremented once; no exception escapes | a missing migration must degrade observation, never execution |
| T10c | `test_graph_events.py::test_write_error_does_not_raise_into_pipeline` | session factory patched so `commit()` raises `OperationalError` (non-lock) | `emit()` from the registry path | `emit()` returns `False`, `graph_events_write_failures` incremented, the adapter result is returned unchanged, the exception does not propagate | observation failure can never fail a task |
| T13b | `test_provenance.py::test_task_id_must_match_provenance` | valid provenance for task A | `emit(task_id=task_B)` with task B's real user id | `ProvenanceError`; no row | a valid-looking but foreign task cannot be attached to an event |
| T11 | `test_graph_events.py::test_concurrent_emits_do_not_lose_rows` | 4 tasks emitting concurrently on SQLite | `asyncio.gather` | 4 rows, unique `seq`; no "database is locked" escapes | hot-path writes are safe under the pipeline's parallelism |
| T12 | `test_graph_events.py::test_unknown_schema_version_is_refused_by_reader` | insert `schema_version=2` directly | reader helper | reader refuses/ignores it and reports the count | forward compatibility cannot silently misread |
| T13 | `test_provenance.py::test_forged_user_id_is_refused` | valid task; `emit(user_id=<other user>)` | emit | `ProvenanceError`; no row | cross-user linkage is structurally blocked |
| T14 | `test_provenance.py::test_run_belonging_to_another_task_is_refused` | two tasks/runs | `resolve_run_provenance(task_a, run_b)` | `ProvenanceError`, nothing written, pipeline would not start | run/task confusion cannot launder evidence |
| T15 | `test_provenance.py::test_adapter_without_provenance_writes_no_events` | `AdapterContext` with empty `user_id`/`execution_run_id` | `registry.run` on `fs_read` | zero rows; `skipped_no_provenance` incremented; adapter result unchanged | unattributable actions are not recorded as belonging to someone |
| T15b | `test_graph_events.py::test_writer_only_inserts` | model introspection + one emit | inspect `GraphEvent` | no `updated_at` column; the writer module exposes no update/delete helper; the row is unchanged after a re-emit | evidence is append-only by construction |
| T16 | `test_provenance.py::test_unknown_action_id_is_rejected` | adapter emits with an `action_id` the registry never issued | emit | no row; counter incremented | a compromised adapter cannot invent an invocation |
| T17 | `test_provenance.py::test_identity_fields_are_not_read_from_params` | `params={"user_id": victim, "task_id": victim_task}` with a real ctx | full run | event `user_id`/`task_id` equal the **ctx/task** values, not the params | client-controlled params can never set identity |
| T18 | `test_audit_linkage.py::test_audit_row_carries_user_task_run_action` | pipeline run with `fs_write` (denial and success) | flush, read `audit_events` + `audit_links` | one link row per new audit row; `user_id/task_id/run_id/action_id` populated; `actor_id=pipeline-service` | closes audit gap 1 without touching the audit table |
| T18b | `test_audit_linkage.py::test_link_and_audit_row_are_atomic` | flush patched so the `audit_links` insert raises | `flush_pending` | nothing persisted (no orphan audit row, no orphan link); events remain pending; a later flush persists both | linkage can never be silently dropped |
| T19 | `test_audit_linkage.py::test_legacy_audit_rows_remain_readable` | insert an `audit_events` row directly, no link | `read_from_db` + `/api/v1/audit` | row returned; five linkage keys `None`; existing keys identical | no break for pre-V3 evidence |
| T19b | `test_audit_linkage.py::test_audit_events_schema_is_unchanged` | introspect `AuditEventModel` and the migrated table | compare column sets | identical to the pre-V3 shape (no linkage columns added) | enforces design §13's "no changes to existing tables" as a test, not a promise |
| T20 | `test_audit_linkage.py::test_audit_api_shape_is_additive` | system-admin token as in existing tests | GET `/api/v1/audit` | same status/fields; five new nullable keys present, sourced from the join | API compatibility |
| T21 | `test_fs_read_instrumentation.py::test_successful_read_emits_observed_and_audits` | real file inside task scope | `fs_read` via registry | `resource.read.observed` outcome `executed`, `target_id="res:<task>/<rel>"`, `size` correct; audit `allowed=true` | closes audit gap 2 |
| T22 | `test_fs_read_instrumentation.py::test_denied_read_emits_denied_and_no_content` | path outside scope (`../x`) | `fs_read` | `resource.read.denied`, `reason=outside_workspace`; `target_id` NULL; payload has basename only; **no** absolute path anywhere in the row | refusals are visible without leaking paths |
| T23 | `test_fs_read_instrumentation.py::test_missing_file_is_failed_not_denied` | non-existent path inside scope | `fs_read` | `resource.read.observed` outcome `failed`, `error_kind=not_found`; result still `success=False` | distinguishes a tool error from a policy denial |
| T24 | `test_fs_read_instrumentation.py::test_read_payload_never_contains_file_content` | file containing a unique sentinel | read | sentinel absent from `payload`, `target_id`, and `audit details` | content exfiltration via telemetry is impossible |
| T25 | `test_endpoint_instrumentation.py::test_allowed_endpoint_records_host_and_path_only` | mock transport (no network) | `http_get` read | `endpoint.connect.attempted` + `.succeeded`; `target_id` has host/path, no `?query`, no port, no userinfo | endpoint identity is normalized, not raw |
| T26 | `test_endpoint_instrumentation.py::test_refused_endpoint_records_denied_before_any_byte` | non-allowlisted host | `http_get` | `.denied` with `reason=host_not_allowlisted`, `denial_layer=adapter_policy`; transport never called (spy) | refusals are observable and provably pre-request |
| T27 | `test_endpoint_instrumentation.py::test_query_string_is_not_stored` | URL with `?token=SECRET` (allowlisted host) | validate + emit | `SECRET` absent from all columns | tokens in URLs cannot enter evidence |
| T28 | `test_workspace_quota_integration.py::test_write_denial_emits_resource_write_denied` | quota too small for the payload | `fs_write` | `resource.write.denied` with `reason=quota`; no `committed`; file absent | quota refusals are attributable |
| T29 | `test_workspace_quota_integration.py::test_write_commit_is_observed_not_verified` | normal write | `fs_write` | `resource.write.committed` `evidence_class=observed` | an adapter claim is never promoted |
| T30 | `test_artifact_events.py::test_artifact_registered_is_verified_with_hash` | real pipeline step writing a deliverable | run pipeline | `artifact.registered` `evidence_class=verified`; `sha256` equals file hash; references existing artifact row | verified evidence requires corroboration |
| T31 | `test_artifact_events.py::test_artifact_event_absent_when_registration_fails` | force registration failure | run | no `artifact.registered`; existing `artifact_registration_failed` task event still emitted | no verified claim without the row |
| T32 | `test_migration_003.py::test_upgrade_downgrade_round_trip_sqlite` | temp SQLite DB, env pinned | `alembic upgrade head` → `downgrade 002` → `upgrade head` | succeeds; `graph_events` + `audit_links` present/absent/present; `audit_events` DDL unchanged throughout | reversible, non-destructive migration |
| T33 | `test_migration_003.py::test_existing_audit_rows_survive_upgrade` | DB at `002` with audit rows | upgrade to head | row count and every value unchanged; no link rows created; no `alter` ran on `audit_events` | legacy evidence is preserved verbatim |
| T34 | `test_migration_003.py::test_postgres_migration_and_events` (marked, skipped without PG) | `RAIVISOX_TEST_POSTGRES_URL` set | upgrade + inspect | same schema on PG; `audit_events` unchanged | the PG schema is the migrated one, not `create_all` |
| T34b–T34i | `test_postgres_phase1.py` (marked, skipped without PG) | a PG role with `CREATEDB` | migrate, emit, flush, restart | constraints and cascade rules; round-trip; dedupe + forged-owner refusal; atomic audit/link; FK enforcement; legacy row preserved and unlinked; restart keeps rows and never reuses a `seq`; a real `fs_read` run links both | PG behaviour is evidenced, not argued |
| T34j | `test_postgres_phase1.py::test_postgres_deadlock_is_classified_transient` | two transactions locking rows in opposite order (real PG) | cross-update | server aborts one with SQLSTATE `40P01`; the writer's classifier returns `True` and does not mistake it for a missing table | the deadlock path is proven by the server, not simulated |
| T34k | `test_postgres_phase1.py::test_postgres_serialization_failure_is_classified_transient` | two SERIALIZABLE transactions reading, then writing, the same row | commit | one is aborted with `40001`; classifier returns `True` | serialization failures are retryable, and recognised |
| T34l | `test_postgres_phase1.py::test_postgres_writer_retries_a_real_deadlock` | a `40P01` raised by the server on the first write attempt | `emit()` | one row written, one retry counted, no failure; a second emit of the same action is a duplicate | a retry cannot double-write or lose the fact |
| T34m | `test_postgres_phase1.py::test_postgres_writer_gives_up_after_bounded_retries` | every attempt fails with `40P01` | `emit()` | `False`, 2 retries, 1 failure, 0 rows; recording is not disabled and a later emit succeeds | bounded, and a failure is not a reason to stop observing |
| T34n | `test_postgres_phase1.py::test_postgres_writer_does_not_retry_permanent_error` | `payload_hash` column narrowed so the insert fails with `22001` | `emit()` | `False`, **0 retries**, 1 failure, 0 rows | a permanent server error is not treated as contention |
| T34o | `test_postgres_phase1.py::test_postgres_audit_flush_retry_cannot_duplicate` | a real `40P01` at the flush commit | `flush_pending` twice | first attempt persists nothing and keeps both events queued; the retry writes 2 audit rows + 1 link exactly once; a third flush writes 0 | a transient failure cannot acknowledge or duplicate audit evidence |
| T37 | `tests/unit/test_transient_retry.py` (34 tests, no server needed) | fake driver errors carrying `sqlstate`/`pgcode`, and a flaky session factory over real SQLite | classify + `emit()` | `40P01`/`40001`/`40000`/`55P03` transient; `23505`/`23503`/`42501`/`42P01`/`25P02`/`08xxx`/`28P01`/`57014` not; SQLite lock text still transient; bounded attempts; final error propagated to the caller; `CancelledError` not swallowed; duplicate still a duplicate | the policy is pinned on every host, including the classification table PG cannot enumerate cheaply |
| T35 | `test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema` (existing) | unchanged | run | still passes (zero drift) | model and migration cannot diverge |
| T36 | Existing suites (`tests/unit/*`, live container gate) | unchanged | run | all previously passing tests still pass; counts unchanged | no regression to authorization, sandbox, quotas, digest pinning |

Additional non-functional assertions:

- `test_graph_events.py::test_emit_cost_is_bounded`: 1000 emits on SQLite complete within 5 s
  (sanity bound for the hot path, not a performance claim).
- `test_audit_linkage.py::test_kernel_call_sites_still_work_without_new_kwargs`: a legacy
  `authorize_and_log(actor_id, action, resource, permissions)` call (no new kwargs) still
  works and writes no `audit_links` row.

### 9.1 Explicitly not tested / not claimed

- PostgreSQL is **not verified** unless a PG URL is provided: the SQLite
  `backend-tests` job skips the PG-gated tests. A recorded PostgreSQL 16.9 run is in the
  acceptance report, and the CI `postgres-gate` job repeats it.
- Sandboxed child I/O remains unobservable; no test asserts events for it.
- Multi-replica clock skew is not exercised.

### 9.2 No weakening

No existing test is modified. No current test asserts an `audit_events` column set, so the new
test T19b *adds* that guarantee rather than retrofitting it; the drift test is expected to
pass unchanged. If a test fails, the implementation is wrong, not the test.

---

## 10. Implementation sequence

Each step is independently reviewable and leaves the system no weaker. No step touches
Phase 2 concerns.

| Step | Work | Files | Review checkpoint |
|---|---|---|---|
| S1 | Event schema constants: event types, outcome/enums, allowlist table, source table, key recipes, target normalizers (pure, no DB) | `services/graph/schema.py` (new) | unit tests for enums/normalizers/key recipes pass with no DB |
| S2 | Writer: `emit()`, retries, counters, validation, payload hash, enable flag read from `V3_EVENTS_ENABLED` via `os.environ` in the graph module (mirrors `WorkspaceQuota.from_env`; no `app.config` import, so `services/graph` stays framework-free — `Settings` is **not** extended) | `services/graph/events.py` (new) | T4–T12 green |
| S3 | Models + migration + `models/__init__` (two new tables, **no `init_db` guard**, no edit to `audit_event.py`) | `apps/api/app/models/graph_event.py`, `apps/api/app/models/audit_link.py`, `alembic/versions/003_*.py`, `app/models/__init__.py` | T19b, T32, T33, T35 green |
| S4 | Audit linkage plumbing (dataclass, `log`, flush with atomic link insert, join read, `ActionContext` fields, kernel kwargs, `SecurityDecision.request_id`) | `services/security/audit_trail.py`, `security_kernel.py` | T18–T20 green; legacy call-site test green |
| S5 | Provenance handle + `AdapterContext` fields + pipeline wiring | `services/graph/provenance.py` (new), `integrations/adapters/base.py`, `apps/api/app/main.py` (`_build_worker`, one call site, `run_task_pipeline`) | T13–T17 green |
| S6 | Registry lifecycle emission (+ `ctx.action_id`, live-action registry) | `integrations/adapters/registry.py` | T1–T3, T16 green |
| S7 | `fs_read` / `fs_write` instrumentation | `integrations/adapters/builtin.py` | T21–T24, T28, T29 green |
| S8 | `http_get` instrumentation | `integrations/adapters/http_get.py` | T25–T27 green |
| S9 | Artifact event | `apps/api/app/main.py::_register_task_artifact` | T30, T31 green |
| S10 | Full regression + docs cross-link | `docs/v3-agent-action-graph.md` §13 Phase 1 status line, `docs/v3-phase-1-acceptance.md` (new, results) | full suite + live gate green; report written |

Ordering rationale: the writer and schema land before anything calls them (so a partially
landed state emits nothing rather than something unvalidated); identity plumbing lands before
the registry so no event is ever written with an unverified `user_id`.

---

## 11. Risks and unresolved decisions

### 11.1 Risks

| ID | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Extra DB writes on the tool hot path (up to 5 rows per invocation) | latency, SQLite write contention | one short insert per event; retry-on-locked; T11 concurrency test; measure in S10 |
| R2 | `audit_links` adds one row per attributed audit event + four indexes | storage, write cost | row written only when linkage exists; batched with the audit insert; no backfill |
| R3 | Path/user-data leakage through `target_id`/payload | privacy | normalizer rejects; allowlist drops unknown keys; sentinel tests T22/T24/T27 |
| R4 | Events silently absent because the flag is off | false sense of coverage | counters + startup log now; `/health` visibility in Phase 2; CI enables it for the new tests; production enablement gated (G1) |
| R5 | Kernel signature creep | callers passing wrong kwargs | all new params default `None`; legacy-call test |
| R6 | SQLite "database is locked" under parallel steps | lost events | retry policy §8.2; T11 |
| R7 | Unbounded growth (no retention in Phase 1) | disk, and sensitive metadata at rest without expiry | flag off by default (D5); G1 gate before production enablement; retention in P2 (D9, G2) |
| R8 | `Integer` `seq` ceiling (~2.1B) | future table rebuild | ≈ 49 years at design §10 volume; monitor `max(seq)`; D6 |
| R9 | A compromised adapter lies in its `observed` events | misleading graph | `evidence_class` stays `observed`; only artifact registration is `verified`; Phase 4 forbids promotion |
| R10 | Provenance resolution adds one query per run | negligible | once per run, not per event |
| R11 | a PG-only DDL/behaviour defect can pass the SQLite suite | a PG regression reaches production unnoticed | T34 + `test_postgres_phase1.py` gated on `RAIVISOX_TEST_POSTGRES_URL`, verified on PostgreSQL 16.9 (acceptance report §7) and run on every push by the CI `postgres-gate` job, which fails on any skip |
| R12 | SQLite runs without `PRAGMA foreign_keys`, so FKs are declarative there | a reader might over-trust the constraint | the guarantees are the writer's single transaction (T18b) and the run-start ownership check (T13–T17); PG enforces the FKs |

### 11.2 Decision summary (all 12 classified)

Classification meanings, used verbatim throughout: **`RESOLVED — SECURE DEFAULT`** — the
requirements and the invariants above imply one choice, and it is the more restrictive or
better-evidenced one; **`RESOLVED — EXISTING CONTRACT`** — the V3 design or existing code
already fixes the answer, so deviating would be the change; **`OWNER INPUT REQUIRED`** —
depends on product or deployment intent that neither document can settle, so it is recorded as
a gate (§11.4), never silently decided. None of the twelve falls in the third class; the gates
that do are listed separately and none blocks implementation.

| ID | Question | Classification | Final choice | Tests |
|---|---|---|---|---|
| D1 | Does `AdapterContext` carry user/run identity? | `RESOLVED — SECURE DEFAULT` | typed, defaulted `user_id` + `execution_run_id` fields (plus `action_id`, D10) | T1, T15, T17 |
| D2 | Columns on `audit_events`, or a side table? | `RESOLVED — EXISTING CONTRACT` | new `audit_links` table; `audit_events` untouched | T18, T18b, T19, T19b, T20, T32, T33 |
| D3 | Is a quota/clamp write refusal a new event type? | `RESOLVED — SECURE DEFAULT` | add `resource.write.denied` (reviewed addition to design §5.2) | T28 |
| D4 | Include `capability.granted` in Phase 1? | `RESOLVED — EXISTING CONTRACT` | no — Phase 2, where declared capability nodes land | — (P2 test, deferred) |
| D5 | Flag name, default, and where it is read? | `RESOLVED — EXISTING CONTRACT` | `V3_EVENTS_ENABLED`, default **off**, read via `os.environ` inside `services/graph` | T10, T1–T3 |
| D6 | What type is `seq`? | `RESOLVED — EXISTING CONTRACT` | `Integer` + `sqlite_autoincrement` (no dialect variant) | T8b, T32 |
| D7 | Is `artifact.registered` in Phase 1? | `RESOLVED — EXISTING CONTRACT` | yes — it is the only `verified` event | T30, T31 |
| D8 | What happens to events with no provenance? | `RESOLVED — EXISTING CONTRACT` | skipped and counted; never stored unattributed | T15, T17 |
| D9 | Retention in Phase 1? | `RESOLVED — EXISTING CONTRACT` | none — Phase 2 (design §15.2 item 6); production enablement gated | — (P2 test, deferred) |
| D10 | How does an adapter learn its `action_id`? | `RESOLVED — SECURE DEFAULT` | typed `AdapterContext.action_id`, set by the registry | T1, T16 |
| D11 | Audit successful `fs_write`? | `RESOLVED — SECURE DEFAULT` | yes — state changes must be visible in the audit trail | T18 |
| D12 | Expose the kernel `request_id`? | `RESOLVED — EXISTING CONTRACT` | additive `SecurityDecision.request_id` | T1, T18 |

### 11.3 Decision record (reason, consequences, compatibility)

**D1 — `AdapterContext` as the identity carrier.** Options: (a) typed defaulted fields;
(b) `ctx.extra` keys; (c) extra parameters on `run(params, ctx, …)`.
(b) is untyped and currently unused in production, so a look-alike key is trivially
pre-populated and nothing documents it; (c) churns 23 construction sites plus every adapter
for no security gain. **`RESOLVED — SECURE DEFAULT` → (a).** Reason: design §4.4/§9 require
user/task/run to come from trusted context, never a request body; a typed field is set at one
pipeline site, has a visible default, and cannot be confused with adapter scratch data.
Consequences: 23 existing call sites unaffected; a directly-constructed context without
provenance produces no events (D8). Compatibility: purely additive defaults; no signature
change; no migration.

**D2 — audit linkage shape.** Options: (a) five nullable columns on `audit_events`;
(b) `audit_links` side table; (c) defer to Phase 2. (a) edits a forensic table and violates
design §13's "no changes to existing tables"; (c) leaves gap 1 open and contradicts
design §15.1, which classifies it as Phase 1 work. **`RESOLVED — EXISTING CONTRACT` → (b).** This
**supersedes the earlier column recommendation** in §5.1 of this document, which is
withdrawn. Reason: the design is authoritative on migration scope, and the audit row is
evidence whose shape existing tooling already depends on; linkage is optional metadata about
that row, not part of the audit fact. Consequences: flush writes two rows in one transaction
(atomic pair), `read_from_db` left-joins, legacy rows have no link and read `null`, the
`init_db` ALTER guard disappears from the plan, and `audit_event.py` is not edited. The design's
Phase 1 file list gains one new file, `apps/api/app/models/audit_link.py` (added, not changed).
Compatibility: `audit_events` byte-identical (asserted by T19b); `/api/v1/audit` gains five
nullable keys on a plain-dict response; revision `003` is `CREATE TABLE` only.

**D3 — write refusals get their own type.** Options: (a) add `resource.write.denied`;
(b) reuse `tool.failed` + `denial_kind`. (b) conflates "the adapter ran and failed" with "a
gate refused before any byte was written", forcing readers to parse a string to tell them
apart — exactly what design §5.4 forbids. **`RESOLVED — SECURE DEFAULT` → (a).** Reason: design
§5.2 permits additions as a reviewed schema change, and the closed set already has
`resource.read.denied`; a kernel *decision* is still never written as a resource denial
(§3.8). Consequences: 13 event types in Phase 1; the design's §5.2 list must record the
addition in the same implementation commit; the Phase 2 projection maps it to a denial, not
to a failed execution. Compatibility: no schema change beyond the enum's closed set; no
migration.

**D4 — `capability.granted` waits for Phase 2.** Options: (a) defer; (b) emit in Phase 1 with
a nullable `user_id`; (c) emit against a synthetic service user. (b) and (c) either weaken the
`user_id NOT NULL` isolation key (design §4.4/§9) or invent an identity, which the design
explicitly refuses to do. **`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design §13 Phase 1
acceptance is scoped to kernel decisions and adapter invocations; capability grants are
global to service actors, carry no user or task subject, and design §4.3 already marks
`holds-capability` as config-derived rather than run-scoped. Consequences: Phase 1 emits no
capability rows; Phase 2 owns declared capability nodes and their startup snapshot. No
decision in either document is blocked by the absence.

**D5 — flag name, default, and location.** Options: (a) `V3_EVENTS_ENABLED`, default off, read
via `os.environ` in `services/graph`; (b) default on; (c) a `Settings` field.
(b) starts writing user-attributable evidence on deploy with no consumer, no retention (D9),
and no read API — a privacy surface with no reader; (c) couples the service layer to
`app.config` for no functional gain. **`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design
§13 Phase 1 names the flag and says "default off"; service-level flags already read
`os.environ` (`SANDBOX_*`, `RAIVISOX_HTTP_ALLOWED_HOSTS`, `WorkspaceQuota.from_env`), so this
matches the codebase. Consequences: tests and CI set the variable explicitly to exercise the
enabled paths, so default-off costs no coverage; R4 (silent absence) is handled by counters
and a startup log, not by collecting by default. Compatibility: unset flag = byte-for-byte
today's behaviour.

**D6 — `seq` stays `Integer`.** Options: (a) `Integer` + `sqlite_autoincrement`;
(b) `BigInteger` with a SQLite variant; (c) plain `BigInteger`. (c) silently breaks SQLite
autoincrement while the column-name drift test still passes; (b) works but introduces the
repo's first dialect-specific type variant to solve a capacity problem the design's own
volume estimate does not reach. **`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design §4.4
specifies a "DB-assigned monotonic integer"; every integer column in `apps/api/app/models` is
`Integer`; and the ordering guarantee rests on non-reuse (`sqlite_autoincrement`, PG sequence),
not on width. At design §10's upper estimate of 120k events/day the 2³¹ ceiling is ≈ 49 years.
Consequences: R8 stays open with an explicit trigger (monitor `max(seq)` against 2³¹); a
future widening is a table rebuild, and the events themselves remain the portable record.

**D7 — `artifact.registered` is Phase 1.** Options: (a) include; (b) defer. (b) would leave
Phase 1 with only `observed` evidence and give the design's `writes` edge — specified as
`verified` — no source at all. **`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design §4.3
says the writes edge is verified and "P1 adds the event"; design §5.1 lists
`_register_task_artifact` as an emission point. Consequences: the `user_id` is resolved inside
that function from the task row and the event is emitted only after the artifact commit, so a
`verified` event cannot exist without the row it corroborates (T31). Compatibility: no schema
change; one hook in an existing function.

**D8 — unattributable events are skipped.** Options: (a) skip and count; (b) sentinel user id;
(c) nullable `user_id`. (b) assigns actions to someone who did not perform them; (c) removes
the column's isolation meaning. **`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design §4.4
makes `user_id` `NOT NULL` and design §11.1 requires missing evidence to read as `unknown`,
never as invented. Consequences: direct adapter use outside a run produces no graph rows; the
counter `graph_events_skipped_no_provenance` keeps that gap visible instead of silent.
Compatibility: none — no event exists for those calls, exactly as today.

**D9 — no retention in Phase 1.** Options: (a) none; (b) a 90-day pruning job now. (b) adds a
deletion path over security evidence before the projection or read API exists, and the design
assigns retention to Phase 2 precisely so it can be reconciled with
`ARTIFACT_RETENTION_DAYS` (default `0` = keep forever) rather than diverging from it.
**`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design §15.2 item 6 names P2; design §13
Phase 1 migration and DoD do not include retention. Consequences: if an operator enables the
flag, growth is unbounded until P2 — which is why production enablement is gated (G1). The
retention *value* remains an owner decision for P2 (G2).

**D10 — `action_id` is a typed field.** Options: (a) `AdapterContext.action_id`;
(b) `ctx.extra["v3_action_id"]`. (b) puts provenance in an untyped dict that any caller can
pre-populate and that has no production consumer today; it is indistinguishable from scratch
data. **`RESOLVED — SECURE DEFAULT` → (a).** Reason: provenance must travel on the trusted
context, and a typed field is the version that can be reviewed, defaulted and grepped.
Consequences: the registry assigns it immediately before `adapter.run`; the live-set
membership check (T16) still decides whether an adapter-originated id is accepted, so typing
is defence in depth, not the control. Compatibility: one more additive default; no signature
change.

**D11 — successful writes are audited.** Options: (a) add the success audit entry;
(b) graph event only. (b) leaves an audit trail that records refused writes but not completed
ones — an evidence hole for the operations that actually change state.
**`RESOLVED — SECURE DEFAULT` → (a).** Reason: reads were the named gap, but writes are the
higher-consequence class; symmetry is the minimum honest coverage, and the entry carries no
content. Consequences: one extra audit row (and its `audit_links` row) per successful write;
the existing batched flusher absorbs it; the graph event remains the analytical record.
Implementation note (§5.4): the entry is written only while `V3_EVENTS_ENABLED` is on, because
T10 requires flag-off behaviour to be unchanged and T36's existing suites count `tool:fs_write`
audit rows. Flag-on evidence is richer; flag-off is byte-identical to V2.

**D12 — the kernel exposes its `request_id`.** Options: (a) add
`SecurityDecision.request_id`; (b) leave `correlation_id` null. (b) abandons the design's
stated correlation between an event and the exact audit row that justified it, leaving readers
to guess from timestamps — which design §5.3 forbids as an ordering mechanism.
**`RESOLVED — EXISTING CONTRACT` → (a).** Reason: design §4.4 defines `correlation_id` as the
kernel `request_id` and §5.1 requires it on the decision events; the id is already generated
and written to the audit row, then discarded. Consequences: one additive defaulted field; no
behavioural change; `correlation_id` is null exactly where no decision was made, which is
itself meaningful (§3.8). Compatibility: no schema or API change.

### 11.4 Owner gates (none blocks Phase 1 implementation)

| ID | Gate | Status | Recommendation | Why it is not blocking |
|---|---|---|---|---|
| G1 | Enabling `V3_EVENTS_ENABLED` in a real deployment | **OWNER INPUT REQUIRED** | keep it off outside dev/test/CI until Phase 2 ships retention and a read path | Phase 1 implementation is testable with the flag enabled in tests; the default stays off |
| G2 | Retention value and its reconciliation with `ARTIFACT_RETENTION_DAYS` | **OWNER INPUT REQUIRED** (Phase 2) | 90 days (design §9), aligned so evidence and artifacts expire together | Phase 1 implements no retention (D9) |
| G3 | Whether real users may hold `ADMIN` (audit and global graph views) | **OWNER INPUT REQUIRED** (Phase 5/6) | decide with the operator model; V3 must not grant it silently | Phase 1 adds no admin surface and no new route |

These are product/deployment decisions, not technical unknowns: each has a stated safe default
that this document already implements, and none of them changes what Phase 1 code must do.
They are listed so that no reader mistakes an architect resolution for human sign-off.

### 11.5 Deferred to later phases (not gaps in Phase 1)

Projection/nodes/edges (P2), declared capability nodes and `capability.granted` (P2), retention
(P2), read API (P2/P4), analysis (P4), policy/approvals (P5), UI (P6), OS-level child-process
instrumentation (post-V3), LLM provider events (post-V3).

---

## 12. Definition of Done

Phase 1 is done only when **all** of the following are demonstrated with evidence, not
assertion:

1. **Schema**: `graph_events` exists with the §3.1 columns/constraints/indexes and
   `audit_links` with the §5.1 shape; `audit_events` still has exactly its pre-V3 columns
   (T19b); `alembic upgrade head` on a fresh SQLite DB reproduces `Base.metadata` exactly
   (the existing zero-drift test passes).
2. **Migration safety**: `upgrade → downgrade 002 → upgrade` round-trips; a pre-existing
   `audit_events` row survives byte-identically with no link row; no `ALTER` runs on any
   existing table (T32, T33).
3. **Emission**: when `V3_EVENTS_ENABLED=1`, a pipeline run produces `tool.attempted` plus a
   terminal event per invocation, `resource.read.*` for each `fs_read`, `resource.write.*`
   for each `fs_write`, `endpoint.connect.*` for `http_get`, and one
   `artifact.registered` per registered artifact — asserted by querying the run's rows and
   counting against the adapters actually invoked (T1, T21, T25, T30).
4. **Gap 1 closed**: every new audit row written by a pipeline tool gate has an
   `audit_links` row carrying `user_id`, `task_id`, `run_id`, `action_id`; the audit table's
   own shape is unchanged and legacy rows read as `null` linkage (T18, T18b, T19, T19b).
5. **Gap 2 closed**: a successful `fs_read` produces both an audit entry and a
   `resource.read.observed` event; a denied read produces `resource.read.denied`
   (T21, T22).
6. **Identity cannot be spoofed**: forged `user_id`, foreign `run_id`, unknown `action_id`,
   and identity-in-params are all refused with no row written (T13–T17).
7. **No secrets/content**: no file content, env value, query string, header, or absolute
   path appears in `graph_events`, `audit_links`, or audit details (T4, T5, T22, T24, T27).
8. **Duplicates and ordering**: re-emitting is a no-op; ordering is `seq`-based; gaps are
   preserved (T7, T8).
9. **Restart**: a restart preserves rows (T9).
10. **No regression**: the full local suite and the Linux CI (backend + **live container
    gate**) are green with counts recorded; the V2 sandbox/quota/image tests are unchanged;
    `v1.0.0` is untouched; no release tag is created.
11. **Flag-off inertness**: with `V3_EVENTS_ENABLED` unset, zero rows are written and
    existing behaviour is unchanged (T10).
12. **No authority added**: no Phase 1 code reads events to make a decision; a code-review
    checklist item confirms the only consumer of `graph_events` is the writer and tests.
13. **Docs**: [v3-agent-action-graph.md](v3-agent-action-graph.md) §13 Phase 1 status is
    updated with the commit and evidence; a results document states PASS/PARTIAL/NOT VERIFIED
    per acceptance item (PG marked NOT VERIFIED unless a PG URL was used).
14. **No Phase 2 work**: no nodes, edges, projection, analysis, policy, approval, API, or UI
    exists in the diff.

---

## 13. Traceability to the V3 design and current code

| Phase 1 element | Design reference | Code reference |
|---|---|---|
| Closed event set | design §5.2 | — (new) |
| Provenance fields | design §4.4 | `audit_event.py`, `execution_run.py`, `task.py` |
| Emission points | design §5.1 | `registry.py`, `builtin.py`, `http_get.py`, `main.py` |
| Audit linkage gap | design §1.3, design §15.1 | `audit_trail.py` (`flush_pending`, `read_from_db`), `security_kernel.py`; new `models/audit_link.py` |
| Migration scope | design §13 Phase 1 ("no changes to existing tables") | revision `003` = two `CREATE TABLE`s; no `ALTER`; no `init_db` guard |
| `fs_read` gap | design §5.6 | `builtin.py` (denial-only audit) |
| Verified writes | design §4.3 (`verified`) | `task_artifact.py`, `_register_task_artifact` |
| Authority invariant | design §7.2 | kernel/sandbox path unchanged |
| Missing sources | design §5.6 (missing instrumentation), design §6.5 (uncertainty) | §5.9 of this spec |
| Isolation | design §9 | `tasks.user_id`, existing 404 semantics |
| Migration/revision | design §13 Phase 1 | `alembic/versions/002…`, `database.py::init_db` |
| Zero-drift constraint | — | `test_production_hardening.py::test_alembic_upgrade_head_matches_model_schema` |
