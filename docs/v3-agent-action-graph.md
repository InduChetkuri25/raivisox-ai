# RAIVISOX V3 — Agent Action Graph and Blast-Radius Intelligence

**Status:** design proposal, for review. **No V3 code exists.** This document is
Phase 0; nothing below is implemented, and no claim in it is a verification
result.

Baseline inspected for this design: commit `9776497` on `main`
(`docs: record the closeout controls and their verified numbers`), clean
working tree, only tag `v1.0.0` → `74e2c1d6a99a06b064ef6486f337892818961fb6`.
CI on that commit: run `39409267172`, all three jobs `success`
(backend `594 passed, 32 skipped`; container gate live `19 passed`; frontend
typecheck + build).

---

## 1. Current architecture and integration points

### 1.1 What exists (inspected, not inferred)

| Layer | Where | What it durably records today |
|---|---|---|
| API | `apps/api/app/main.py` (FastAPI, ~3k lines), `app/auth/*`, `app/tasks/*` | tasks, steps, task events, executions, artifacts |
| Auth | JWT bearer (`app/auth/service.py`), `get_current_user_dep` | user identity per request; `users` table has no roles |
| Security kernel | `services/security/security_kernel.py` | in-memory actor→permission map; no persistence |
| Audit | `services/security/audit_trail.py` + `audit_events` table | `event_id, actor_id, action, resource, allowed, request_id, details, timestamp` |
| Adapter gate | `integrations/adapters/registry.py::AdapterRegistry.run` | calls `authorize_and_log` for adapters with `kernel_permissions` |
| Pipeline | `run_task_pipeline` → `_build_worker` → `ParallelExecutor` | `execution_runs` (one row per execute), `task_events` (with `execution_run_id`), `task_steps` |
| Artifacts | `_register_task_artifact` on node completion | `task_artifacts` with `sha256`, `size_bytes`, `run_id`, `is_current`, `superseded_at` |
| Sandbox | `services/execution/{sandbox,container_sandbox,quotas,workspace_quota,image_pins}.py` | per-run `SandboxResult.enforced` / `limits_hit` returned to callers only |
| Crash backstops | `_close_open_execution_runs`, `_reset_orphaned_running_tasks`, `_interruption_stage` | close runs/tasks left `running` after restart |
| CI | `.github/workflows/ci.yml` | backend tests, live Docker container gate, frontend build |

### 1.2 The five integration points V3 can hook (and nothing else)

1. **`AdapterRegistry.run`** — the single choke point every tool invocation passes
   through (`registry.get(adapter_id)` → kernel decision → `adapter.run`). It knows
   `adapter_id`, `ctx.actor_id`, `ctx.task_id`, `ctx.step_id`, `ctx.resource`, and the
   kernel decision.
2. **`_build_worker` → `AdapterContext`** — carries the run's identity into adapters.
   Today `actor_id=PIPELINE_ACTOR` (`"pipeline-service"`) for every step.
3. **`_on_node_transition` (pipeline)** — the only place step completion and artifact
   registration meet; already emits `task_events` and registers artifacts.
4. **`AuditTrail.log` / `SecurityKernel.record_audit`** — every allow/deny decision,
   already durable via `flush_pending`.
5. **Adapter bodies** (`fs_read`, `fs_write`, `http_get`, `python_exec`, `shell_exec`,
   `codegen`, `finalize`) — the only places that know the actual file/URL touched.

### 1.3 Facts that constrain the design (all verified in code)

- **Audit events cannot be tied to a user, task, step, or run.** `AuditEvent` has
  `actor_id`, `action`, `resource`, `request_id`, `details` — and the pipeline always
  passes `actor_id=PIPELINE_ACTOR`. The end user is reachable only via
  `task.user_id` (a task lookup), never from the event row.
- **The kernel's permission map is in-memory only**, rebuilt deterministically at
  import time (`permission_system.assign_role(kernel, "system", ADMIN)` and
  `grant_permission(PIPELINE_ACTOR, …)`). There is no persisted grant history.
- **No per-tool-call event exists.** `EventType.TOOL_CALL` and `MODEL_CALL` are
  declared but never emitted; the tool that ran is only visible inside a
  `step_completed` payload (`result.tool`, `result.mode`).
- **`fs_read` audits denials only.** Successful reads emit nothing, so "the agent read
  file X" is not observable at the app layer today.
- **`http_get` records the URL only inside a successful result payload**; refused
  URLs (allowlist/SSRF) are returned as `AdapterResult.error` and never audited.
- **`tool_usage` is a dead table**: the model exists, nothing ever writes a row.
- **No approval mechanism exists.** `waiting_for_user` is declared in `TaskStatus`
  and `StepStatus` (and read by one cancel guard) but never set anywhere.
- **`/api/v1/audit` requires `admin`**, and no real user can hold it: only the
  `"system"` actor is ever assigned `Role.ADMIN`. The audit UI is closed to end
  users (fail-closed, not a bypass).
- **`TaskGraph` is an in-memory workflow DAG** (nodes = plan steps, discarded after the
  run). It is *not* the Agent Action Graph and must not be conflated with it.

---

## 2. V3 scope and non-goals

### 2.1 Questions V3 answers

1. **Capabilities** — what can this agent/execution reach? (declared, plus what the
   kernel actually granted at startup)
2. **Actions** — what did it attempt and what actually happened? (observed events)
3. **Blast radius** — if this agent or tool were compromised, what is exposed? (bounded,
   evidence-graded reachability over the observed + declared graph)
4. **Decision** — should a proposed action be allowed, denied, or require human
   approval? (restrictive-only policy evaluation at existing execution boundaries)

### 2.2 In scope

- A provenance-first event schema and durable event store inside the existing database.
- A graph projection rebuilt deterministically from those events.
- New instrumentation **only** at choke points that already exist (§1.2).
- A deterministic blast-radius analyzer returning paths with evidence, assumptions and
  uncertainty.
- A restrictive-only policy layer (allow / deny / require_approval / audit_only) and a
  durable approval workflow.
- A minimal UI: identity, observed actions, exposure findings, policy decisions,
  approvals, uncertainty.

### 2.3 Non-goals (explicit)

- **No change to isolation.** V3 does not make the sandbox stronger; T1, T5, T6, T7, T9
  outcomes are unchanged (see §3.4).
- **No new execution path.** Policy evaluation can only *subtract* authority. It never
  grants a permission, never bypasses the kernel, never bypasses the sandbox, and never
  introduces an unconfined fallback.
- **No OS-level instrumentation** (auditd/eBPF/ptrace) in V3. Sandboxed child process
  activity stays unobservable and is labeled as such (§6.5, §15).
- **No graph database.** No Neo4j/Typo/etc.; the graph is relational projection over the
  existing DB (§10).
- **No ML/embedding-based risk scoring.** Every number is a rule with a stated reason.
- **No cross-tenant/org model.** Isolation stays per-user (§9).
- **No policy DSL / OPA.** Structured policy rows only; a DSL is an open question (§15).
- **No change to the V2 execution controls.** Kernel authorization, sandbox flags and
  required-limit gates, workspace quotas, and image digest verification (including its
  fail-closed behaviour on hosts without registry access) are untouched. Graph policy is
  evaluated after them and cannot override, relax, or bypass any of them (§7.2).

---

## 3. Threat model and trust boundaries

### 3.1 Assets

Execution integrity; audit/grader evidence; user data in workspaces and artifacts;
credentials (LLM provider keys, DB URL, secret key); approval authority; the integrity of
the graph itself (a poisoned graph produces bad decisions).

### 3.2 Adversaries

| ID | Adversary | Capability |
|---|---|---|
| A1 | Untrusted task content (prompt injection into a plan/step) | chooses step text, and via `step_params` some adapter parameters; cannot choose the actor, scope, or kernel grants |
| A2 | Compromised tool/adapter | arbitrary behaviour inside the sandbox boundary; can try to write misleading events |
| A3 | Malicious authenticated user | owns their tasks; tries to read another user's graph, forge approvals, or self-approve |
| A4 | Operator error | misconfigured policy/staleness/retention that silently weakens enforcement |
| A5 | Storage/DB attacker or corruption | tamper with events/projection/approvals, or make the store unavailable |

### 3.3 Trust boundaries

```
[browser] --JWT--> [API] --in-process--> [kernel] --> [registry] --> [adapters]
                                                          |
                                                          +--> [sandbox] --> [child process]
                                                          |
                                    [audit trail] --flush--> [audit_events]
                                    [V3 events]   --insert--> [graph_events] --> [projection]
[policy evaluator] --reads projection--> decision --> boundary hook (registry/adapter/quota)
```

- The **API process is trusted**; the child process inside the sandbox is **not**.
- The **database is trusted for availability-independent integrity but not for
  unforgeability**: a DB attacker can rewrite rows. V3 therefore treats the graph as
  *evidence*, not as proof, and every finding cites its source events (§6.6).
- **Events written from inside an adapter describe the adapter's own action.** A
  compromised adapter can lie about what it did; this is why `verified` requires an
  independent corroboration (artifact row whose `sha256` matches bytes on disk, or a
  kernel decision row) rather than the adapter's word (§4.3).

### 3.4 Relationship to the V2 T1–T13 model (what V3 does *not* fix)

| V2 threat | V3 effect |
|---|---|
| T1 filesystem escape, T5 network, T6 CPU, T7 memory, T9 fork bombs | **none** — isolation unchanged; V3 only observes/decides |
| T3 secret theft | partial: V3 adds a *name-only* credential inventory and can flag credential exposure paths; it does not stop exfiltration |
| T10 symlink/traversal | none (existing containment untouched) |
| T12 host inspection | none |
| T13 audit tampering | **improves detection**: graph integrity checks compare projection against events and flag divergence; a child still cannot write the DB |
| New: A2 lying adapter | mitigated by evidence grades; a single-source "observed" claim is never promoted to "verified" |

---

## 4. Graph schema and provenance model

### 4.1 Epistemic vocabulary (used everywhere: schema, API, UI, docs)

| Term | Definition | Where it comes from |
|---|---|---|
| **Declared capability** | Something the system *says* it can do: adapter `kernel_permissions`, kernel grants, allowlists, workspace scope | config/code at startup |
| **Observed action** | A record emitted by an enforcing choke point at runtime | V3 event insert at §1.2 hooks |
| **Verified resource access** | An observed action corroborated by independent evidence (e.g. artifact row whose `sha256` matches the bytes on disk) | two sources agree |
| **Inferred reachability** | A path computed by the analyzer; never persisted as a fact edge | analyzer output only |
| **Potential impact** | Modeled consequence of a reachable resource class | rule table on findings |
| **Confirmed impact** | Impact with verified evidence that the effect occurred (e.g. a superseded artifact row proving an overwrite) | verified edges + rule |

**Invariant:** an edge carries `evidence_class ∈ {declared, observed, verified}` and is
only ever written from an event. Inferred relationships are returned by the analyzer with
their path and are **never** promoted into the fact graph. The API serializes
`evidence_class` on every edge and `classification` on every finding, so a caller cannot
mistake one for the other.

### 4.2 Nodes (only what has a reliable source)

| Node | Identity | Security purpose | Source | Available today |
|---|---|---|---|---|
| `Identity` | `user:<uuid>` or `service:pipeline-service` | who acted / who could act | auth + `PIPELINE_ACTOR` | yes (declared) |
| `ExecutionRun` | `run:<uuid>` | scope of one execute invocation | `execution_runs` | yes |
| `ExecutionStep` | `step:<task>/<node_id>` | finer scope; where deps live | `task_steps` + `task_events.payload.step_id` | yes |
| `Tool` | `tool:<adapter_id>` | the unit that holds power | `AdapterRegistry` | yes (declared) |
| `Capability` | `cap:<permission>` | the four-question "what can it access" axis | `Permission` enum + kernel grants | yes (declared) |
| `Resource` | `res:<task_id>/<scope-relative path>` | what can be read/written | artifact registry (writes); adapter params (reads) | writes yes; reads need P1 |
| `Endpoint` | `net:<host><path-template>` | external reachability | `http_get` | needs P1 |
| `CredentialRef` | `cred:env:<NAME>` (name only, no value) | blast-radius multiplier | new declared inventory (P1) | no — must be added |
| `PolicyDecision` | `decision:<uuid>` | why an action was allowed/denied | kernel + V3 policy | kernel: partial (no action id) |
| `Policy` | `policy:<id>` | standing rule | V3 `policies` table | no — P5 |
| `Approval` | `approval:<uuid>` | human gate state | V3 `approvals` table | no — P5 |

Not added, deliberately: free-form "owner", "contains", "related-to" edges; file-content
nodes; parameter/value nodes. None of them answer one of the four questions with reliable
evidence, and each would create a place to infer something the system cannot observe.

### 4.3 Edges (direction, evidence, and honest availability)

| Edge | From → To | Evidence required | Available today |
|---|---|---|---|
| `holds-capability` | Identity → Capability | declared kernel grant snapshot at startup | yes (config-derived) |
| `requires-capability` | Tool → Capability | declared `adapter.kernel_permissions` | yes |
| `can-invoke` | Identity → Tool | declared: registry has it **and** kernel grant covers its permissions | yes |
| `executed` | ExecutionStep → Tool | observed `registry.run` return (success/failure) + kernel decision id | P1 |
| `denied-by` | ExecutionStep → PolicyDecision | observed kernel denial (`allowed=false`) | P1 links it to an action id |
| `authorized-by` | ExecutionStep → PolicyDecision | observed kernel allow | P1 |
| `writes` | ExecutionStep → Resource | **verified**: `task_artifacts` row + `sha256` matches file on disk | yes (registry), P1 adds the event |
| `reads` | ExecutionStep → Resource | observed `fs_read` success event | **no** — needs P1 instrumentation; sandboxed child reads remain unobservable |
| `can-access` | ExecutionStep → Resource | declared: workspace scope containment (the child *may* reach it) | yes (derived from spec/scope) |
| `connects-to` | ExecutionStep → Endpoint | observed `http_get` attempt (success **or** refused) | **no** — needs P1 |
| `depends-on` | ExecutionStep → ExecutionStep | declared plan dependency (`task_steps.dependencies`) | yes |
| `part-of` | ExecutionStep → ExecutionRun | observed `task_events.execution_run_id` | yes |
| `performed-by` | ExecutionRun → Identity | observed `task.user_id` + service actor | partial (user known via task) |
| `uses-credential` | Tool → CredentialRef | declared inventory | P1 (declared only, never observed values) |
| `requires-approval` | Tool/Endpoint → Policy | declared policy row | P5 |
| `approved-by` / `denied-by` (approval) | Approval → Identity | observed decision | P5 |

Rules:

- An edge exists only if its `evidence` column is satisfiable today. Rows marked "needs
  P1" are *not* in the schema until the instrumentation lands.
- **A resource node with no inbound edge is `exists_only`** and is excluded from impact
  findings (see §6.4) — its mere existence proves nothing about reachability.
- Duplicate edges collapse on `(from, relation, to, evidence_class)`; the earliest and
  latest supporting events are retained as `first_seen` / `last_seen`.

### 4.4 Provenance event (the only write path into the graph)

Every graph fact originates from exactly one row in `graph_events`:

| Field | Meaning |
|---|---|
| `id` | uuid (event identity) |
| `seq` | DB-assigned monotonic integer — the total order used by all readers |
| `occurred_at` | source time (UTC, from the emitting site) |
| `recorded_at` | insert time |
| `schema_version` | integer; unknown versions are refused, not guessed |
| `event_type` | enumerated (§5.2) |
| `actor_id`, `actor_type` | `user:<id>` / `service:pipeline-service` / `system` |
| `user_id` | **NOT NULL** — owning user (isolation key, §9) |
| `task_id`, `run_id`, `step_id`, `tool_id`, `action_id` | subject links; nullable only where the event genuinely has no such subject |
| `target_kind`, `target_id` | node reference (`res:…`, `net:…`, `cap:…`, `decision:…`) |
| `outcome` | `attempted`, `allowed`, `denied`, `executed`, `failed`, `interrupted`, `cancelled`, `expired` |
| `evidence_class` | `declared`, `observed`, `verified` |
| `source` | `module:function` that emitted it (fixed strings, not user data) |
| `correlation_id` | kernel `request_id` / pipeline attempt id, to join related events |
| `idempotency_key` | deterministic natural key, **unique** |
| `payload` | redacted JSON, ≤ 8 KiB, allowlisted keys per `event_type` |
| `payload_hash` | sha256 of the canonical payload (tamper detection, §11.2) |

---

## 5. Event collection design

### 5.1 Emission points (each is an existing choke point)

| Point | Event(s) | Linking |
|---|---|---|
| Startup (config load) | `capability.granted` per actor/permission | actor, capability; `run_id=null` |
| `AdapterRegistry.run` entry | `tool.attempted` | `action_id = uuid`, task/run/step from ctx |
| Kernel decision | `tool.allowed` / `tool.denied` | same `action_id`, `correlation_id=request_id`, decision reason |
| `AdapterRegistry.run` return | `tool.executed` / `tool.failed` | same `action_id`, duration, outcome |
| `fs_read` success / denial | `resource.read.observed` / `resource.read.denied` | scope-relative path; **no content** |
| `fs_write` commit | `resource.write.committed` | path, bytes; verified later by artifact registration |
| `http_get` attempt (before request) and result | `endpoint.connect.attempted` / `succeeded` / `denied` | host + path template only |
| `_register_task_artifact` | `artifact.registered` | artifact id, sha256, size, run_id |
| `_on_node_transition` | `step.started/completed/failed/skipped` | existing `task_events` remain the timeline; V3 mirrors the security-relevant subset |
| `run_task_pipeline` start/end, crash closes | `run.started` / `run.completed` / `run.failed` / `run.interrupted` | `execution_runs` status |
| Policy evaluator | `policy.evaluated` | decision id, policy id, mode, reason |
| Approval API | `approval.requested` / `granted` / `denied` / `expired` | fingerprint only |
| Projection rebuild / integrity check | `graph.projection.rebuilt` / `graph.integrity.violation` | seq range |

### 5.2 Event types (closed set; adding one is a reviewed schema change)

`capability.granted`, `tool.attempted`, `tool.allowed`, `tool.denied`, `tool.executed`,
`tool.failed`, `resource.read.observed`, `resource.read.denied`,
`resource.write.committed`, `artifact.registered`, `artifact.registration_failed`,
`endpoint.connect.attempted`, `endpoint.connect.succeeded`, `endpoint.connect.denied`,
`step.started`, `step.completed`, `step.failed`, `step.skipped`, `run.started`,
`run.completed`, `run.failed`, `run.interrupted`, `policy.evaluated`,
`approval.requested`, `approval.granted`, `approval.denied`, `approval.expired`,
`graph.projection.rebuilt`, `graph.integrity.violation`.

### 5.3 Duplicates, ordering, and concurrency

- **Idempotency:** `idempotency_key` is deterministic, not random, e.g.
  `tool.executed:{run_id}:{step_id}:{attempt}:{adapter_id}`,
  `artifact.registered:{artifact_id}`, `policy.evaluated:{decision_id}`. Insert is
  conflict-ignore; a retry after a crash cannot double-count.
- **Distinct events for one action are expected** (`tool.attempted`, `tool.allowed`,
  `tool.executed`) and get distinct keys; they share `action_id`.
- **Ordering:** readers order by `seq`. `occurred_at` is display-only. Two events with the
  same `occurred_at` are still totally ordered.
- **Concurrency:** the pipeline runs steps in parallel; `seq` is assigned by the DB, so
  interleaving is well-defined. Per-entity ordering claims are always derived from `seq`,
  never from a wall clock.
- **Backpressure:** events are written on the same DB session as the action's own row
  where possible (kernel decision + event in one commit); otherwise a bounded queue with
  a synchronous flush before the action returns (§11.2).

### 5.4 Failed / denied / interrupted / successful are different

- `denied` is terminal for that action: a denied action **must not** have an
  `executed` event, and a unit test asserts the invariant per `action_id`.
- `failed` means the adapter ran and raised or returned failure.
- `interrupted` is produced by the restart sweep (`_reset_orphaned_running_tasks` /
  `_close_open_execution_runs`), never inferred from a missing event.
- `cancelled` comes from the cancel path; `expired` from approval timeout.
- A missing `executed` event for an `attempted` action is **unknown**, never treated as
  success or failure.

### 5.5 Restart survival

Events are rows, so they survive restarts. The projection is rebuildable:
`projection_state.last_seq` records how far the projection has consumed; on startup the
projector resumes from `last_seq + 1`. If `projection_state` is missing or the projection
disagrees with the events, it is rebuilt from `seq=0` (idempotent upserts), emitting
`graph.projection.rebuilt`. **The projection is disposable; the events are the record.**

### 5.6 Missing instrumentation (explicit, so the graph cannot pretend)

| Surface | Observable? | Consequence for the graph |
|---|---|---|
| App-layer `fs_read` success | no (today) | no `reads` edge; **P1 adds it** |
| Sandboxed child file reads/writes (python/shell/codegen/DoD) | **no, and not in V3** | only `can-access` (declared scope); never a `reads`/`writes` fact. Requires OS instrumentation (§15) |
| `http_get` refused URLs | no (today) | no `connects-to` for refused attempts; **P1 adds it** |
| LLM provider calls | no | credential use is declared only |
| Model/tool selection inside `TextIntelAdapter` | partial | not graphed in V3 |
| Deleted/expired artifacts | yes (registry rows) | `writes` history retained with `superseded_at` |

---

## 6. Blast-radius analysis

### 6.1 Inputs

A read-only snapshot: nodes/edges (fact graph) filtered to one `user_id` and one
`run_id` (or `task_id`), plus `projection_state` (current seq, lag) and a
`source_status` map (which instrumentation emitted events for this run).

### 6.2 Traversal (deterministic)

- **Start set:** the compromised subject — an `Identity`, `Tool`, or `ExecutionStep` —
  plus its `part-of` run.
- **Allowed relations per question:** capability reach → `holds-capability`,
  `requires-capability`, `can-invoke`; data reach → `writes`, `reads`, `can-access`;
  network reach → `connects-to`; propagation → `depends-on` (forward only);
  authority → `authorized-by`, `denied-by`, `approved-by`.
- **Bounds:** `max_depth=4`, `max_nodes=5000`, `max_paths_per_target=20`,
  `max_wall_ms=2000`. Hitting a bound sets `truncated: true` with the exact bound name;
  it never silently returns a partial answer as complete.
- **Cycles:** visited set keyed `(node_id, grade)`; a node is expanded again only at a
  strictly better grade. Path reconstruction refuses to revisit a node, so cycles
  terminate and are reported as `cycle_suppressed` rather than as new reach.
- **Determinism:** adjacency sorted by `(grade_rank, relation, node_id)`; ties broken by
  node id; output JSON is canonical (sorted keys) and contains no timestamps. Same store
  snapshot → byte-identical output (tested, §12).

### 6.3 Evidence grades per path

Path grade = **weakest edge** on the path, with rank `verified > observed > declared`.

| Path grade | Meaning | Example |
|---|---|---|
| `verified` | every hop corroborated | run wrote `deliverables/result.md`, artifact sha256 matches the file on disk |
| `observed` | every hop emitted by a choke point, one hop single-source | `http_get` attempted `en.wikipedia.org` (adapter-reported) |
| `declared` | at least one hop is configuration only | "the step's scope *permits* reading any file in the task workspace" |
| `unknown` | a required source emitted nothing for this run | child-process reads |

**A path is never upgraded** because a target is "important" or because the node exists.

### 6.4 Findings (never claim reachability from existence alone)

| Finding class | Requires | Wording in output |
|---|---|---|
| `confirmed_access` | all hops `verified` | "verified: this run wrote / overwrote …" |
| `observed_attempt` | all hops `observed` or better | "observed: the run attempted to reach …, outcome …" |
| `potential_access` | any hop `declared` | "potential: the scope *permits* …; no observed access" |
| `unknown` | a required source is missing/stale | "unknown: no evidence either way for …" |
| `exists_only` | node with no inbound edge | listed separately as "present, not reachable by any recorded action" |

### 6.5 Uncertainty surfacing

Every finding carries:

```
uncertainty: {
  missing_sources: ["sandbox.child.file_io", "llm.provider_calls"],
  projection_current: true,
  stale_seconds: 0.4,
  truncated: false,
  assumptions: ["scope containment implies read eligibility; no read was observed"]
}
```

If `projection_current` is false or `stale_seconds > max_staleness_s`, findings are
downgraded to `unknown` (never silently presented as confirmed) and policy evaluation
takes the stale branch (§7.4).

### 6.6 Finding schema (what a reviewer actually receives)

```json
{
  "finding_id": "f_01J...",
  "classification": "observed_attempt",
  "subject": {"kind": "step", "id": "step:task-1/step-3"},
  "target": {"kind": "endpoint", "id": "net:en.wikipedia.org/w/api.php"},
  "path": [
    {"from": "identity:service:pipeline-service", "relation": "can-invoke",
     "to": "tool:http_get", "evidence": "declared"},
    {"from": "step:task-1/step-3", "relation": "executed",
     "to": "tool:http_get", "evidence": "observed"},
    {"from": "step:task-1/step-3", "relation": "connects-to",
     "to": "net:en.wikipedia.org/w/api.php", "evidence": "observed"}
  ],
  "evidence_sources": ["registry.run", "http_get.attempt"],
  "assumptions": ["host is allowlisted by configuration at run start"],
  "uncertainty": {
    "missing_sources": ["sandbox.child.file_io", "llm.provider_calls"],
    "projection_current": true,
    "stale_seconds": 0.4,
    "truncated": false
  },
  "impact": {
    "resource_class": "endpoint_allowlisted",
    "crosses_privilege_boundary": false,
    "touches_shared_resource": false,
    "reason": "allowlisted outbound read; no credential node on this path"
  },
  "confirmation_required": ["an observed response event for this endpoint"]
}
```

A `confirmed_access` finding differs in exactly one way: every hop is `verified` and
`confirmation_required` is empty. Nothing else about the shape changes, so a reviewer
cannot tell two findings apart by formatting instead of evidence.

### 6.7 Impact model (rules, not scores)

`impact.resource_class`:

| Class | Examples | Weight |
|---|---|---|
| `workspace_file` | task artifact | low |
| `shared_resource` | artifact referenced by >1 task (by sha256) | medium |
| `endpoint_allowlisted` | host in `RAIVISOX_HTTP_ALLOWED_HOSTS` allowlist | medium |
| `endpoint_unlisted` | host not in allowlist (should be impossible; a hit means config drift) | high |
| `credential_reference` | `cred.env:OPENAI_API_KEY` | high |
| `authority` | policy/approval objects | high |

Multipliers are explicit booleans in the output (`crosses_privilege_boundary`,
`touches_shared_resource`, `uses_untrusted_tool` where "untrusted tool" = an adapter whose
`kernel_permissions` exceed the acting identity's grants, or an adapter flagged
`experimental` in a reviewed allowlist). No hidden weights, no float score that a UI can
round into a false "safe".

**Potential vs confirmed impact.** Potential impact is the class-and-reason statement above,
derived from a path. Confirmed impact requires additionally that the *effect* is verified:
today that means an artifact row proving the write (with `sha256` matching the bytes on
disk) or a superseded artifact row proving an overwrite. Nothing else in the current system
can confirm an effect, so nothing else is promoted — in particular, an adapter's own report
that it "wrote" a file is `observed`, not confirmed.

`explain()` renders the same structured finding into a fixed template used by both the
API and the UI: path → evidence per hop → assumptions → uncertainty → impact class →
"what would confirm or refute this".

---

## 7. Policy and approval design

### 7.1 Decision points (before the protected action)

| # | Boundary | Hook | What it can decide |
|---|---|---|---|
| 1 | HTTP request (API) | existing deps | unchanged (ownership/auth) |
| 2 | Tool invocation | `AdapterRegistry.run`, **after** the kernel decision, before `adapter.run` | allow / deny / require_approval / audit_only |
| 3 | File write | `fs_write` adapter before write (alongside the workspace quota gate) | allow / deny / require_approval |
| 4 | Outbound network | `http_get` before the first byte | allow / deny / require_approval |
| 5 | Sandbox spawn | existing spec validation/quota gate (unchanged) | not policed by V3 in V3.1 |

### 7.2 Evaluation order and the one invariant that matters

```
user auth → task ownership
  → SecurityKernel.authorize_and_log        (existing, unchanged)
  → sandbox spec validation + required limits + image verification + workspace quota
  → V3 graph policy                          (restrictive-only)
  → action
```

**Invariant:** `effective_allowed = existing_allowed AND graph_policy_allowed`.
The graph policy can return `deny` or `require_approval`; it can never return "allow
despite the kernel". A missing/empty/broken policy layer therefore degrades to today's
behaviour, never to something weaker than today's controls, and never bypasses the
sandbox.

### 7.3 Modes

| Mode | Behaviour | Records |
|---|---|---|
| `audit_only` (V3 default) | evaluates and records, never blocks | `policy.evaluated` with `would_be=deny/require_approval` |
| `enforce` | blocks per decision | decision + reason + policy id |

Promotion from `audit_only` to `enforce` is a reviewed config change per policy, and each
policy names the boundary it applies to.

### 7.4 Failure and uncertainty behaviour (explicit, per case)

| Situation | `audit_only` | `enforce`, sensitive class | `enforce`, other |
|---|---|---|---|
| Missing graph information for the action | proceed, flag `unknown` | **deny** | proceed, flag `unknown` |
| Capability not in the declared set | proceed, flag | **deny** (unknown power is not trusted) | proceed, flag |
| Policy engine raised | proceed, `policy_error` event | **deny** | proceed, `policy_error` |
| Graph store unavailable / cannot record decision | proceed, log locally | **deny** (no evidence, no authorization) | proceed, log (documented fail-open) |
| Projection stale beyond `max_staleness_s` | proceed, flag `stale` | **deny** | proceed, flag |
| Approval timed out | n/a | **deny** (`approval_timeout`), step fails | same |
| Conflicting policies | deny > require_approval > allow; then specificity (`resource` > `tool` > `task` > `global`); then newest policy version | same | same |

**Documented, permitted fail-open behaviour** (and why it is acceptable): `audit_only`
mode is non-blocking by design, and non-sensitive classes proceed when the graph store is
unavailable. In both cases the kernel, sandbox, quota and image controls still run, so the
fail-open is confined to *V3's additional restriction*, not to any existing control. Every
such decision is recorded (or logged locally when the store is down) with
`decision=proceed_uncertain`, so it is auditable after the fact.

**Sensitive classes** (fail-closed): writes that overwrite a previously verified artifact,
network access to any host, any action on a `credential_reference` node, and any action
whose blast radius contains an `authority` or `endpoint_unlisted` node.

### 7.5 Approval workflow

- Table `approvals`: `id, task_id, run_id, step_id, tool_id, action_fingerprint,
  requested_by (service actor), requested_at, expires_at, status, decided_by (user_id),
  decided_at, reason, policy_id`.
- `action_fingerprint = sha256(canonical(tool_id, normalized_params_hash, target ids,
  attempt))`. The raw params are **not** stored — only the hash and a redacted summary.
- Single-use: a granted approval is consumed by exactly one matching action; a different
  action (or a recovery retry with a different attempt) needs a new approval.
- **No self-approval:** the requester is always the service actor, and the approver must be
  the task owner (or an explicitly granted admin) — an agent cannot approve itself.
- While pending: step status `waiting_for_user`, run stays `running`, bounded by
  `approval_timeout_s` (default 900) and the run's own timeout; expiry → `denied` +
  step failure with `approval_timeout`. No implicit allow, ever.
- Denial/expiry is recorded as an event and appears in the run timeline.

---

## 8. User interface and investigation workflow

The UI exists to answer the four questions with **evidence visible on screen**. It is not a
graph toy: nothing is drawn that the API did not return with an evidence class, and there is
no client-side inference.

### 8.1 Surfaces (three screens, one drawer)

| Surface | Route (proposed) | Answers |
|---|---|---|
| **Run Review** | `/tasks/{id}/runs/{run_id}` | identity, run state, integrity banner, action timeline |
| **Exposure** (tab of Run Review) | same, `?tab=exposure` | capabilities, resources, endpoints, blast-radius findings |
| **Approvals inbox** | `/approvals` | pending approvals with the exact action and its blast radius |
| **Path drawer** | overlay | one finding's path, hop-by-hop evidence, uncertainty, what would confirm it |

### 8.2 What each element shows (and its API field)

**Run Review header**

- *Who*: `Identity` (user + `service:pipeline-service`), `run_id`, task, start/end, outcome.
- *Integrity banner*: `projection_current`, `stale_seconds`, `truncated`, and the
  `missing_sources` list. If any is degraded the banner is the first thing on the page —
  a reviewer must not read a partial graph as complete.
- *Counters with their class*: "3 verified writes · 1 observed endpoint · 2 declared
  capabilities · 4 unknown".

**Action timeline** — one row per `graph_events` entry for the run, ordered by `seq`
(never by timestamp):

| Column | Source |
|---|---|
| time | `occurred_at` (display only; label says "source time") |
| actor | `actor_id` |
| action | `event_type` in human words ("attempted", "denied", "executed") |
| tool / target | `tool_id`, `target_id` (scope-relative) |
| outcome | `outcome` badge |
| evidence | `evidence_class` badge |

Denied attempts and interrupted runs are visually distinct from successes; a missing
terminal event renders as **unknown**, with an inline explanation, never as a green row.

**Capabilities panel** (declared): each `holds-capability` / `requires-capability` edge,
labelled **DECLARED**, with the note "granted at process start; this is permission, not
usage". Adjacent, the *observed* tool invocations show what was actually used. The panel
never merges the two numbers into one "capabilities used" figure.

**Resources panel**: per `Resource`, the strongest inbound evidence —
`verified` (artifact hash matches disk), `observed` (adapter reported a read), `declared`
(scope permits access), or `unknown` (no evidence). A resource that exists but has no
inbound edge is listed under "present, not reachable by any recorded action" and is
excluded from findings.

**Findings list**: one row per blast-radius finding, showing `classification`
(`confirmed_access` / `observed_attempt` / `potential_access` / `unknown`), target, hops,
and impact class. Clicking opens the Path drawer. Findings are sorted by
`(classification rank, impact class rank, target id)` — deterministic, matching the API
order, so two reviewers see the same list.

**Path drawer**: the actual answer to "why does this matter?" —

- the path as an ordered list `step → tool → target`, each hop with its relation and
  evidence class;
- the structured `assumptions`, `uncertainty` (missing sources, staleness, truncation);
- the impact class and the *reason string*, not a score;
- "what would confirm or refute this" (e.g. "enable child-process I/O instrumentation",
  "an observed read event for this path").

**Approval card**: the exact action fingerprint summary (tool, redacted target, attempt),
the blast-radius findings that triggered the requirement, the policy id and reason, an
expiry countdown, and two actions — Approve / Deny. There is **no** "approve all", no bulk
action, and no default focus on Approve. Expiry is shown as "will be denied at …", because
timeout denies.

### 8.3 Honesty rules the UI enforces

1. **Every claim carries a badge** (VERIFIED / OBSERVED / DECLARED / INFERRED / UNKNOWN).
   A badge is never derived client-side; it comes from the API field.
2. **Absence of evidence is not evidence of absence.** Empty states read "No evidence was
   recorded for this run" plus the missing-sources list — never "No exposure" or a green
   check.
3. **Inferred relationships are labelled INFERRED and cannot be pinned, saved, or exported
   as facts.** Only events and verified edges are exportable evidence.
4. **Stale/truncated data disables approval actions** for findings that depend on it
   (the server also enforces this; the UI is not the control).
5. **No decorative graph canvas.** There is no force-directed "attack graph" view. Paths
   are rendered only for findings the analyzer actually returned; a whole-graph canvas
   invites reading structure as risk.
6. **No numeric risk score.** Impact classes + reasons only, so a reviewer cannot anchor on
   a number that hides its inputs.

### 8.4 API surface required by the UI

| Endpoint | Method | Auth | Returns |
|---|---|---|---|
| `/api/v1/tasks/{id}/runs/{run_id}/events` | GET | task owner | paged `graph_events` (redacted) |
| `/api/v1/tasks/{id}/runs/{run_id}/exposure` | GET | task owner | capabilities, resources, endpoints, findings, uncertainty |
| `/api/v1/tasks/{id}/runs/{run_id}/findings/{finding_id}` | GET | task owner | one path with per-hop evidence |
| `/api/v1/approvals` | GET | task owner | pending approvals for the caller's tasks |
| `/api/v1/tasks/{id}/approvals/{approval_id}/decide` | POST | task owner (never the requester actor) | decision result |

All responses are user-scoped (404 for another user's run), and all payloads pass the same
redaction as the store (§9).

### 8.5 States the UI must handle

| State | Rendering |
|---|---|
| V3 disabled / tables absent | "Exposure data is not enabled on this deployment" (no fake empty graph) |
| No events for the run | "No evidence recorded" + the `missing_sources` list |
| Projection rebuilding | banner with `projection_rebuilt_at`, findings marked stale |
| Truncated traversal | findings list header states the exact bound hit (depth/nodes/paths/time) |
| Policy engine unavailable | decisions shown as `policy_error`; approvals disabled |
| Cross-user or unknown run | 404 page (never an empty-but-valid page that implies "nothing found") |

---

## 9. Data isolation and privacy

- **Isolation key:** `user_id` on every `graph_events`, projection, policy and approval
  row, denormalized from `task.user_id` at write time (never taken from a request body).
  All graph reads filter on the authenticated user; a cross-user lookup returns **404**
  (matching the existing task API), not an empty 200.
- **No new public surface:** graph/analysis/approval endpoints require the same JWT as
  tasks; ingestion is in-process only (no ingestion HTTP endpoint).
- **Admin views** stay behind the kernel's `admin` permission. Note: no real user holds
  ADMIN today, so these views are closed to end users (§15, unresolved product decision).
- **Deletion:** `ON DELETE CASCADE` from `tasks` for events/projection/approvals, matching
  `task_events` semantics. Retention (default 90 days, configurable) drops whole runs,
  never single events, so a partially retained run cannot look complete.
- **Secrets and sensitive data:**
  - Payloads are built from an allowlist per `event_type`; unknown keys are dropped.
  - Value scrubber rejects key names matching `secret|token|key|password|authorization|
    cookie|api[_-]?key` and values that look like credentials (long base64/hex, `sk-…`).
  - Paths are stored **scope-relative** (`res:<task>/<relpath>`); absolute host paths and
    `storage_path` never enter the graph.
  - Endpoints store `host + path template` only — no query strings (they can carry tokens),
    no headers, no bodies; query presence is recorded as a boolean.
  - Credential nodes carry the **name** only (`cred:env:OPENAI_API_KEY`), never a value,
    prefix, suffix, or hash of the value.
  - Payload cap 8 KiB with a truncation marker; oversized payloads are rejected, not
    silently cut mid-secret.

---

## 10. Performance and storage considerations

- **Volume estimate:** ~6–12 V3 events per step (attempt/allowed/executed + resource or
  endpoint + step lifecycle). A 10-step task ≈ 60–120 events ≈ 30–80 KB JSON. At 1000
  tasks/day that is ~30–80 MB/day; 90-day retention ≈ 3–7 GB. Acceptable for the existing
  Postgres deployment; SQLite dev unaffected.
- **Write cost:** one insert per event in the action's own transaction where possible;
  three indexes (`user_id, seq`, `run_id, seq`, unique `idempotency_key`). No synchronous
  projection update on the hot path — projection is advanced by a small consumer (§5.5).
- **Read cost:** analyzer is bounded (§6.2). Target p95 < 250 ms for a 10k-event task;
  measured in P4 with a synthetic fixture, not assumed.
- **Projection rebuild:** O(events) single pass, no network; target < 2 s for 100k events.
- **Why not a graph database:** the data is small, the queries are shallow and bounded,
  and keeping evidence in the same transactional store as runs/artifacts avoids a second
  datastore with its own credentials, backups, and tenancy model. The schema deliberately
  avoids deep recursive queries (fixed depth ≤ 4), so SQL `WITH RECURSIVE` is enough.
  If scale ever demands it, the event log remains the portable source of truth.

---

## 11. Failure behavior

### 11.1 Principles

1. **A graph failure never fabricates evidence.** Missing events → `unknown`, never a
   default allow/success.
2. **Recording a graph event must not change execution *outcome*** in `audit_only` mode
   (mirroring the existing `_emit_step_event` rule), but **may block** when a policy in
   `enforce` mode depends on it (§7.4). The distinction is explicit and tested.
3. **No new crash path.** Every write is best-effort with bounded retries; failures are
   logged safely (counts, no payloads).

### 11.2 Failure matrix

| Failure | Behaviour |
|---|---|
| Event insert fails (transient) | retry ×2 with backoff inside the action; then `audit_only`: continue + count in `graph_write_failures` health metric; `enforce` + sensitive: action denied |
| Projection consumer crashes | restart resumes from `last_seq`; no data loss (events intact) |
| Projection corrupted | detected by integrity check (`payload_hash`/count mismatch) → rebuild from `seq=0`, event `graph.integrity.violation`, UI shows `projection_rebuilt_at` |
| Graph tables missing (pre-migration) | V3 features disabled with an explicit `v3_enabled=false` health flag; existing pipeline unaffected (backward compatibility, §13) |
| Policy evaluator raises | per §7.4 (fail-closed for sensitive in `enforce`) |
| Approval storage unavailable | `require_approval` actions fail closed (cannot record a pending approval); never auto-approve |
| Clock skew | ordering never depends on `occurred_at`; `stale_seconds` computed from `recorded_at` and guarded against negative values |
| DB is the bottleneck | events are the first thing to be sampled? **No** — sampling would break provenance. Instead: cap per-run event count (`max_events_per_run`, default 10 000) and record a `truncated` flag on the run, then require operator action. Never silently drop. |

---

## 12. Testing strategy

Only tests whose subject actually exists can pass; each phase lands its own tests, and the
V2 suites must stay green untouched.

| Area | Tests (unit unless noted) |
|---|---|
| Event schema | required fields; unknown `schema_version` refused; payload allowlist drops unknown keys; 8 KiB cap; outcome vocabulary closed |
| Idempotency | same key inserted twice → one row; distinct lifecycle events share `action_id` |
| Provenance | `denied` action has no `executed` event; `attempted` without outcome is `unknown`; `source` is a fixed string |
| Projection | rebuild from events is deterministic; `last_seq` resume; corrupted projection rebuilds and emits the violation event |
| Restart | kill/restart mid-run (mirroring `test_cross_restart.py`): events survive, run marked `interrupted`, projection current |
| Isolation | user B gets 404 for A's run/findings/approvals; no event leaks another user's `user_id` |
| Secrets | fuzz payloads with API keys/passwords/URLs with query tokens → nothing persisted; paths relativized; endpoint query stripped |
| Analyzer | golden fixtures: verified/observed/declared/unknown paths; existence-only excluded; cycles terminate; bounds set `truncated`; determinism (same snapshot twice → identical bytes); 10k-event perf budget |
| Policy | precedence table (deny > approval > allow; specificity; newest); every §7.4 row; restrictive-only invariant (policy cannot turn a kernel denial into an allow); audit_only never blocks |
| Approvals | fingerprint mismatch denied; replay denied (single-use); expiry denied; non-owner approver 403; agent self-approval impossible |
| Integration | kernel → event → projection → API roundtrip; `fs_write` denial path creates no resource edge; `http_get` refusal creates an `endpoint.connect.denied` event (P1) |
| Existing controls | the entire V2 suite, including `test_sandbox_phase*`, `test_workspace_quota`, `test_image_pins`, plus the live Linux container gate unchanged |
| Migration | upgrade from the current schema (revision `002`) creates V3 tables without altering existing ones; downgrade drops them; app runs with V3 tables absent |

Integration tests use the existing conventions (file-backed SQLite, `app.main` import
after `DATABASE_URL` is pinned, `httpx.ASGITransport`) exactly as
`tests/unit/test_capability_audit.py` and `test_cross_restart.py` do.

---

## 13. Phased implementation roadmap

Each phase is independently reviewable, shippable behind a flag, and leaves the system
strictly no weaker. "DoD" is the bar for merging.

### Phase 0 — Architecture, threat model, acceptance criteria *(this document)*

- **Files:** `docs/v3-agent-action-graph.md` only.
- **Depends on:** nothing.
- **Security risks:** designing against imagined telemetry (a graph that looks complete
  while missing a source). Mitigation: §1.3 lists what is *not* observable, and §5.6
  forbids edges without a source.
- **Tests:** none (documentation). Consistency checks only (§16).
- **Migration:** none.
- **Acceptance:** the four questions are answered by named artifacts; every edge has a
  source or is marked missing; V2 limitations classified (§15/§16).
- **DoD:** reviewed and approved; no code changed.

### Phase 1 — Provenance-aware event schema and emission

- **Files:** `services/security/security_kernel.py` (emit decision events),
  `services/security/audit_trail.py` (link `action_id`/`task_id`/`run_id` — additive),
  `integrations/adapters/registry.py` (attempted/allowed/denied/executed),
  `integrations/adapters/builtin.py` (`fs_read` success, `fs_write` commit),
  `integrations/adapters/http_get.py` (attempt/success/refusal),
  `apps/api/app/models/graph_event.py` (new), `alembic/versions/003_*.py`.
- **Depends on:** Phase 0.
- **Security risks:** accidentally logging secrets/paths; a failed event write blocking
  execution. Mitigation: allowlist + scrubber + the §11.2 retry policy, with tests.
- **Tests:** schema, idempotency, redaction, `denied`-without-`executed`, no-execution-
  change in `audit_only`.
- **Migration:** additive table + indexes; no changes to existing tables; feature flag
  `V3_EVENTS_ENABLED` (default off in CI until Phase 2).
- **Acceptance:** every kernel decision and adapter invocation produces exactly one
  `attempted` + one terminal event; the V2 suite stays green.
- **DoD:** events observable in a dev DB; flag off by default.

**Status (2026-10-21): IMPLEMENTED — Phase 1 only.** Implementation commit
`f30e6a4`; CI run `39554959798` green (backend 638 passed / 33 skipped, live Docker
container gate 19 passed / 0 skipped, frontend build). Specification:
`docs/v3-phase-1-spec.md`; results: `docs/v3-phase-1-acceptance.md`. What exists now:
`services/graph/{schema,actions,provenance,events}.py`, `app/models/{graph_event,audit_link}.py`,
`alembic/versions/003_graph_events_audit_links.py`, registry/adapter instrumentation, the
`audit_links` side table, and 53 Phase 1 acceptance tests (44 on a SQLite-only host; nine are
PostgreSQL-gated). `audit_events` is unchanged (no linkage columns); legacy rows read as
`NULL` provenance and are never backfilled. `V3_EVENTS_ENABLED` remains **off by default**;
production enablement is operational gate G1. Not implemented (Phase 2+): projection, graph
store, blast radius, policy engine, approval flow, graph UI. PostgreSQL 16.9 is verified
through the opt-in gate (`RAIVISOX_TEST_POSTGRES_URL`: T34, T34b–T34i), and the CI
`postgres-gate` job runs that gate against `postgres:16-alpine` on every push.

### Phase 2 — Durable store and graph projection

- **Files:** `services/graph/events.py` (append/query), `services/graph/projection.py`
  (rebuild/advance/integrity), `apps/api/app/models/graph_node.py`,
  `graph_edge.py`, `projection_state.py`, `alembic/versions/004_*.py`,
  `main.py` lifespan consumer start/stop.
- **Depends on:** Phase 1.
- **Security risks:** projection drift presented as fact. Mitigation: integrity check +
  `projection_current` flag surfaced in every API response.
- **Tests:** rebuild determinism, resume, corruption rebuild, restart survival, perf
  budget.
- **Migration:** additive; backfill is **not** attempted (pre-V3 activity stays
  `unobserved`, never synthesized).
- **Acceptance:** rebuild from 0 equals incremental projection; `projection_state`
  advances within 1 s of event insert in the test harness.
- **DoD:** flag-on in CI; existing tests green.

### Phase 3 — Additional instrumentation (read/endpoint/credential)

- **Files:** `integrations/adapters/builtin.py`, `http_get.py`, new
  `services/graph/credentials.py` (name-only inventory from config),
  `apps/api/app/config.py` (inventory source).
- **Depends on:** Phase 2.
- **Security risks:** credential inventory becoming a secret store. Mitigation: names
  only, validated against `^[A-Z][A-Z0-9_]{2,64}$`, no values read at all.
- **Tests:** read success/deny events; endpoint attempt/deny; inventory rejects anything
  that looks like a value.
- **Migration:** none (inventory is derived).
- **Acceptance:** `reads` and `connects-to` edges appear with `observed` evidence; a
  sandboxed child's activity produces **no** such edge (asserted).
- **DoD:** graph shows "unknown" for child I/O rather than pretending.

### Phase 4 — Reachability and blast-radius analysis

- **Files:** `services/graph/analysis.py` (traversal, grades, findings, explain),
  `services/graph/impact.py` (class table), `apps/api/app/main.py` (read-only endpoint
  `GET /api/v1/tasks/{id}/exposure`).
- **Depends on:** Phases 2–3.
- **Security risks:** overstating reach; unbounded traversal as DoS. Mitigation: §6.2
  bounds + §6.4 classes + determinism tests.
- **Tests:** golden paths per grade; existence-only excluded; cycles; bounds; determinism;
  p95 budget.
- **Migration:** none.
- **Acceptance:** every finding has path + evidence + assumptions + uncertainty; no
  finding is emitted from node existence alone.
- **DoD:** API returns byte-identical JSON for identical snapshots.

### Phase 5 — Policy evaluation and approvals

- **Files:** `services/graph/policy.py`, `services/graph/approvals.py`,
  `apps/api/app/models/{policy,approval}.py`, `alembic/versions/005_*.py`,
  `integrations/adapters/registry.py` + `builtin.py` + `http_get.py` (boundary hooks),
  `apps/api/app/tasks/router.py` (approval endpoints).
- **Depends on:** Phase 4.
- **Security risks:** the layer becoming a bypass; approval replay/self-approval. Mitigate
  with the §7.2 invariant (tested), fingerprints, single-use, owner-only decisions.
- **Tests:** the full §7.4 matrix, precedence, restrictive-only invariant, approval
  lifecycle, and that V2 controls still run *after* a V3 allow.
- **Migration:** additive; policies start empty ⇒ `audit_only` ⇒ behaviour identical to
  V2.
- **Acceptance:** with empty policies, no action's outcome changes; with `enforce`, the
  sensitive matrix denies.
- **DoD:** approvals work end-to-end in an integration test; a denied action performs no
  adapter call (asserted with a spy).

### Phase 6 — UI and investigation workflow

- **Files:** `apps/web/src/app/tasks/[id]/exposure/page.tsx` (new),
  `apps/web/src/lib/api.ts`, `types.ts`, approval inbox page.
- **Depends on:** Phases 4–5.
- **Security risks:** showing "no findings" as "safe"; leaking paths. Mitigation: the
  empty state must say "no evidence collected", and only scope-relative paths are
  rendered (server already strips internals).
- **Tests:** component-level render tests for each evidence badge, empty state, and
  approval actions; `tsc --noEmit` + production build in CI.
- **Migration:** none.
- **Acceptance:** a reviewer can answer the four questions from one screen without reading
  raw JSON, and every claim on screen carries its evidence class.
- **DoD:** UI ships behind the same auth as tasks; no decorative graph canvas.

### Phase 7 — Security testing and release acceptance

- **Files:** `tests/unit/test_graph_*.py`, `tests/unit/test_approvals.py`,
  `docs/v3-acceptance-report.md`, `docs/sandbox-design.md` §9 cross-link.
- **Depends on:** all previous.
- **Security risks:** self-assessment bias. Mitigation: adversarial tests (A2 forged
  events, A3 cross-user, A5 tampering) with explicit expected-deny assertions.
- **Tests:** the full table in §12, plus the unchanged V2 suites and the live container
  gate.
- **Migration:** none.
- **Acceptance:** §14 criteria all evidenced; the report states PASS/PARTIAL/NOT VERIFIED
  with run/commit references, no claim without a command.
- **DoD:** CI green on the final commit; tag decision made explicitly (V3 does not create
  a tag as part of this design).

---

## 14. Acceptance criteria (V3 release)

| ID | Criterion | Evidence required |
|---|---|---|
| AC1 | Every kernel allow/deny and every adapter invocation has a matching event with a shared `action_id` | count-equality test + a live run query |
| AC2 | No edge is written without an event; a node with no inbound edge never appears in a finding | analyzer test + code review of the projection writer |
| AC3 | Re-inserting the same event is a no-op | idempotency test |
| AC4 | Analysis is deterministic and bounded | byte-equality test; bounds test |
| AC5 | Cross-user access returns 404 for events, findings, approvals | integration test with two users |
| AC6 | No secret value, header, body, or absolute path is persisted | scrubber tests + a grep of a real dev DB fixture |
| AC7 | In `enforce`, every §7.4 failure case denies the sensitive action | per-row tests |
| AC8 | V2 isolation, quotas, and image pinning are unchanged | V2 suites + live container gate green |
| AC9 | Analyzer p95 < 250 ms on a 10k-event fixture; projection lag < 1 s | measured, reported with the command |
| AC10 | Approvals are fingerprint-bound, single-use, owner-decided, expiry-denied | lifecycle tests |
| AC11 | Restart loses no event and leaves no run `running` | restart test mirroring `test_cross_restart.py` |
| AC12 | UI shows evidence class and uncertainty on every claim; empty state is not "safe" | render tests + screenshot in the acceptance report |

---

## 15. Known limitations and unresolved decisions

### 15.1 V2 limitations carried into V3 (classified from evidence, not assumed)

| Limitation | Status | Evidence |
|---|---|---|
| No filesystem-wide disk quota; app-level workspace quota is not an OS total cap | **PARTIAL** — the workspace-write half is implemented and verified; the filesystem-wide half is **not implemented** | `services/execution/workspace_quota.py`; 70 tests; live gate run `39408218912`; sandbox-design §5.1 |
| Hardlink redirection and writes by running children can exceed the quota | **PARTIAL** — documented, inodes counted once, no interception | sandbox-design §5.1; `test_hardlinked_inode_is_counted_once` |
| Container image toolchain not minimal (`python:3.12-slim` ships `pip`) | **PARTIAL** — digest pinned, toolchain still general | `services/execution/image_pins.py`; `APPROVED_IMAGES`; live gate |
| Digest verification needs registry access; must fail closed offline | **PASS** for the documented behaviour — refusal, pull failure, and mismatch all fail closed; the explicit override is reported as `image_pinned: false` | `tests/unit/test_image_pins.py` (policy refusal, pull/inspect/mismatch, override flag) |
| V3 Phase 1 (event schema + emission + audit linkage) | **VERIFIED** — 53 acceptance tests (44 SQLite + 9 PostgreSQL-gated) passing locally including a real PostgreSQL 16.9 run, Linux CI green (backend suite + live container gate) | `docs/v3-phase-1-acceptance.md` §5–§7, §11 |
| V3 Phases 2–6 (projection, graph store, blast radius, policy, approval, UI) | **NOT IMPLEMENTED** — design only; no code, no tests | this document §13 |

No concrete security **regression** was found while inspecting for this design. Two
pre-existing **provenance** gaps were found and are recorded rather than fixed here:
audit events carry no user/task/run linkage (actor is always `pipeline-service`), and
`fs_read` successes are not audited. Both are V3 Phase 1 work, not regressions in a
claimed control.

### 15.2 Unresolved decisions (must be settled before or during the phase named)

1. **Credential inventory source of truth** (P3): `.env.example`-derived manifest vs
   config-declared list. Must never read values.
2. **Admin/operator role** (P5/6): audit and global graph views are unreachable by real
   users today. Granting `ADMIN` to users is a product/security decision that this design
   will not make silently.
3. **Approval across recovery retries** (P5): fingerprint includes the attempt, so a retry
   needs a new approval. Confirm this is the intended UX versus a time-boxed reusable
   grant.
4. **Policy authoring** (P5): structured rows only for V3. Whether to adopt OPA/Rego later
   is open; adopting it would add a runtime dependency and a second evaluation engine.
5. **Sandbox observability** (post-V3): child FS/network activity needs auditd/eBPF. Until
   then those edges stay `declared`/`unknown` by design.
6. **Retention defaults** (P2): 90 days proposed; must be reconciled with
   `ARTIFACT_RETENTION_DAYS` (default 0 = keep forever) so evidence and artifacts do not
   diverge.
7. **`tool_usage` table** (P1): dead today. Reuse it or drop it — but never repurpose it
   silently, because old deployments may have rows.
8. **Tenant model** (P2): per-user only; no `tenant_id` column is added speculatively, so
   a future org model needs a migration, not a reinterpretation.

### 15.3 Residual risk accepted by this design

- A compromised adapter can emit false `observed` events; only `verified` findings
  (artifact hash corroboration, kernel decision rows) resist that. Findings must therefore
  show *which* source spoke.
- The DB operator can rewrite events; the graph is evidence, not tamper-proof proof.
  Tamper *detection* (payload hashes, integrity events) is in scope; tamper *prevention*
  is not.
- Sampling is never used to shed load (§11.2): a truncated run is reported as truncated.

---

## 16. Documentation and repository consistency (design-phase checks)

This document was reviewed against the implementation at commit `9776497`; the claims in
§1.3 were each read from code, not from prior reports:

| Claim | File inspected |
|---|---|
| Kernel decisions are audited, actor is the pipeline actor | `services/security/security_kernel.py`, `apps/api/app/main.py` (`_build_worker` call site), `integrations/adapters/registry.py` |
| Audit rows have no task/run/step link | `apps/api/app/models/audit_event.py`, `services/security/audit_trail.py` |
| Runs are first-class and events carry `execution_run_id` | `apps/api/app/models/execution_run.py`, `task_event.py`, `main.py::run_task_pipeline` |
| Artifacts are verified rows with sha256 | `apps/api/app/models/task_artifact.py`, `main.py::_register_task_artifact` |
| `fs_read` logs denials only; `http_get` logs nothing | `integrations/adapters/builtin.py`, `http_get.py` |
| `TOOL_CALL`/`MODEL_CALL` never emitted; `tool_usage` never written | `models/task_event.py`, `models/tool_usage.py` (no writer found) |
| No approvals; `waiting_for_user` never set | `models/task.py`, `models/task_step.py`, `grep` across `apps/services/integrations` |
| Sandbox/quota/image controls as reported | `services/execution/*.py`, `tests/unit/test_sandbox_phase*.py`, `test_workspace_quota.py`, `test_image_pins.py` |
| CI structure and current state | `.github/workflows/ci.yml`; run `39409267172` (`success`) |

**Update (2026-10-20):** the paragraph above described the repository at design time and is
kept as the design record. Phase 1 has since been implemented (`services/graph/`, the
two new models, migration `003`, adapter instrumentation) and is reported in
`docs/v3-phase-1-acceptance.md`. Still absent: projection/graph store, blast-radius
engine, policy tables, approval flow, and any graph UI — i.e. Phases 2–6.
