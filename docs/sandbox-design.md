# RAIVISOX Execution Sandbox — V2 Design

**Status:** design only — nothing in this document has been implemented.
Production code, Docker files, the capability kernel, and the database schema
are intentionally untouched.

**V1 context.** RAIVISOX executes real code (Python, shell) inside its own
process's privilege level. The application layer provides command
allow/blocklists, per-task working directories, timeouts, and process-group
kills — but no OS-enforced boundary exists. This document designs the
OS-level execution sandbox that V2 should investigate before expanding agent
capabilities.

---

## 1. Current execution audit

The full path from task request to audit, as it exists today (file and line
references verified against the V1.0.0 baseline, `74e2c1d`):

```
POST /api/v1/tasks/{task_id}/execute            main.py:2305
  └─ ownership check (task owned by current_user) → 404 otherwise
  └─ async_mode: atomic status claim (no duplicate runs) → background drive
  └─ run_task_pipeline()                         main.py:2464
       ├─ decompose intent into capability-tagged steps (services/decomposer)
       ├─ build TaskGraph (services/execution/task_graph.py), bounded parallelism
       └─ per node: _build_worker()._run()        main.py:2171
            ├─ registry.select(step.required_capabilities)
            │     capability → adapter id (integrations/adapters/base.py:59)
            ├─ registry.run(adapter_id, params, AdapterContext)
            │     integrations/adapters/registry.py:69
            │   ├─ kernel.authorize_and_log(...)  registry.py:83
            │   │     actor permissions vs adapter.kernel_permissions
            │   │     decision audited even when denied
            │   └─ adapter.run(params, ctx)       ← unprivileged-until-here
            ├─ pipeline wraps result: PermissionError/RuntimeError → recovery
            └─ step events emitted per state change (task_events)

Untrusted execution happens inside adapters (integrations/adapters/builtin.py):
  ShellExecAdapter (shell_exec)   → TerminalSandbox.execute
                                     services/execution/terminal_sandbox.py:40
                                     asyncio.create_subprocess_shell, cwd=task
                                     scope, allow/blocklist, timeout, kill
  PythonExecAdapter (python_exec) → asyncio.create_subprocess_exec(
                                     sys.executable, "-c", code, cwd=scope)
                                     DEVNULL stdin, timeout, kill
  CodeGenAdapter (codegen)        → writes module, compile(), smoke-runs it
  DoD verification                → services/dod/dod_checks.py:257
                                     subprocess.run([sys.executable, module],
                                     cwd=module_dir, timeout=20)

Workspace (TASK_WORKSPACE = workspaces/tasks, main.py:510):
  every task gets scope dir workspaces/tasks/<task_id>
  (_task_scope, builtin.py:31); path clamping via normcase+abspath prefix
  check (builtin.py:_clamp); FileReaderTool/FileWriterTool enforce their own
  allowlist of the workspace root.

Artifacts:
  _register_task_artifact (main.py:1902) — containment re-check, SHA-256,
  size, stable IDs, current/superseded semantics, execution_runs linkage.

Outcome (honest): steps → graph results → VerificationEngine → executable
DoD checks → recovery loop (max attempts) → terminal outcome
verified / completed_unverified / failed; every transition audited.
```

**Where the sandbox boundary goes:** between `registry.run` (authorization,
audited) and the adapter's process spawn. Concretely: the four spawn sites —
`TerminalSandbox.execute`, `PythonExecAdapter.run`, the codegen smoke-run, and
the DoD `subprocess.run` — become calls into a single `ExecutionSandbox`
interface. Authorization, workspace clamping, artifact registration, and
outcome recording stay exactly where they are; only *how the child process is
confined* changes.

### Known gaps in the current boundary (V1-acknowledged, untrusted-code-real)

These are the honest weaknesses the sandbox must close; they are documented
in code comments where relevant:

| Gap | Location today | Consequence |
|---|---|---|
| Child env inherits the API process environment | `ShellExecAdapter`/`TerminalSandbox` (no `env=` rebuild); `python_exec` inherits `os.environ` | Untrusted code can read provider keys/config of the host process |
| No CPU / memory / process-count limits | all four spawn sites | runaway CPU, RSS growth, fork bombs survive past the timeout window |
| Shell string execution, not argv | `create_subprocess_shell` | metacharacter blocklist is bypassable by construction |
| Blocklist blocks first token only; allowlist is substring | `TerminalSandbox.is_allowed`; `ShellExecAdapter.DEFAULT_ALLOW` contains `python` | `python -c <anything>` is on the allowed path |
| No resource budget shared across retries | recovery can re-run steps | a task's cumulative usage is unbounded |
| `TASK_WORKSPACE` prefix checks use `realpath` at registration but clamps at write time use `normcase(abspath)` | `builtin.py:_clamp` vs `main.py:1926` | a **symlink planted inside the task scope** pointing outside resolves away from the clamp — filesystem escape via symlink is possible today |
| Windows host in development cannot enforce POSIX limits | `preexec_fn`-style tuning unusable; Job Objects unused | dev machine runs with a weaker boundary than prod |

---

## 2. Threat model

Assets: API process secrets (env, config DB), other tasks' workspaces and
artifact bytes, host filesystem, host network position (cloud metadata,
internal services), API availability, audit integrity.

Attacker: the author of *task-supplied* code/commands (LLM-generated or
user-supplied step params) — possibly rational, possibly prompt-injected;
plus a hostile co-tenant task on the same host.

| # | Threat | Example | Solved at | Mechanism |
|---|--------|---------|-----------|-----------|
| T1 | Filesystem escape (read/write outside scope) | `open("/etc/passwd").read()` | OS | container/filesystem namespace; writable path = task scope only; host root mounted read-only or not at all |
| T2 | Cross-task workspace access | read sibling task's artifacts | app + OS | task scoping already partitions paths; OS mount of the *task's own* scope removes even the shared-root read |
| T3 | Application secret theft | print env / config | app + OS | **app:** scrubbed, minimal env handed to child (never inherit); **OS:** no host mounts, secrets never enter sandbox image |
| T4 | Arbitrary process execution beyond the step's declared need | spawn compilers, installers | OS | image contains only the toolchain the adapter needs; no package managers in PATH |
| T5 | Network access from untrusted code | exfiltrate artifacts, hit 169.254.169.254 | OS | default-deny network namespace (`--network none` / unshare -n); explicit allow via proxy per policy |
| T6 | Excessive CPU | crypto-mining in a step | OS | CPU quota per execution (cgroup/Job Object); wall-clock timeout already exists and stays |
| T7 | Excessive memory | allocate until host OOMs | OS | hard memory cap (cgroup/Job Object); container OOM, not host OOM |
| T8 | Disk exhaustion | write infinitely in scope | app + OS | app: per-task workspace quota enforced post-hoc + pre-execution check; OS: container filesystem size limit where supported |
| T9 | Fork/process bombs | `:(){ :\|:& };:` | OS | PID/process-count cap (pids cgroup / Job Object); app-level blocklist remains as a fast pre-filter only |
| T10 | Symlink / path traversal | plant symlink in scope → read host file | app + OS | app: resolve symlinks in the clamp (`realpath` semantics at *use* time, matching artifact registration); OS: rootless mount of the scope makes outside unreachable regardless |
| T11 | Long-running processes outliving the request | daemon survives pipeline | app | process-group kill on timeout/cancel already exists; sandbox supervisor reaps the whole container/process tree on any terminal state; graceful-shutdown hook extended |
| T12 | Host/environment inspection (finger-printing, listing tasks, reading app logs) | enumerate env, scan fs | app + OS | minimal env (app) + no host visibility (OS); outputs are scrubbed before persistence (existing `_scrub_internal_paths` extended to sandbox fs identifiers) |
| T13 | Audit/outcome tampering | task writes to app DB | OS | app DB/API creds never present in child env; app data not mounted into sandbox |

**Layer split.** Application layer (keep/strengthen, cheap, cross-platform):
task-scope clamping with symlink resolution, minimal child env, output
scrubbing, workspace quotas, wall-clock timeouts, command policy pre-filter,
existing kernel authorization and audit. OS layer (new, required for T1/T5—T9
guarantees): namespace/cgroup or Job Object isolation, filesystem confinement
of the scope, network default-deny, hard resource caps. Anything the app
layer "blocks" by string inspection must be treated as advisory, never a
security boundary.

---

## 3. Options comparison

| Option | Isolation strength | Win-dev story | Prod story | Ops cost | Latency | Verdict |
|---|---|---|---|---|---|---|
| A. Status quo (blocklists + timeouts) | none against determined code | trivial | trivial | none | ~0 | rejected as boundary; retained as advisory pre-filter |
| B. Docker containers per execution | strong (namespaces, cgroups, Seccomp default profile, no network) | **poor** — dev hosts (this project's) run Windows w/o Docker; Docker Desktop licensing/WSL2 overhead | excellent; images cached | moderate (image build/pull, socket security) | +1–3 s warm, +10–60 s cold | strongest prod candidate; unacceptable as the *only* mode |
| C. Rootless containers (podman rootless / rootless docker / nerdctl) | strong; no root daemon | same Windows gap; WSL2 dependency | good; smaller blast radius, no privileged socket | moderate–high | as B | best prod posture where available; same dev gap as B |
| D. OS-level process sandbox (POSIX: `unshare`/cgroups v2/rlimit/`LANDLOCK`; Windows: Job Objects + restricted tokens) | good-not-perfect: fs via landlock/mount ns, net via unshare -n, cpu/mem/pids via cgroups; seccomp filter profile | **works on Linux CI and Windows dev (Job Objects)**, no images | good; no daemon, no images | low | +10–50 ms | **recommended default**, with honest per-platform capability reporting |
| E. Windows-specific (AppContainer/Job Objects alone) | Job Objects: cpu/mem/pids/kill — yes; filesystem/net: weak (no AppContainer for arbitrary child toolchains) | native on dev | irrelevant if prod is Linux | low | ~0 | folded into D as the Windows *profile*; never claimed stronger than it is |
| F. seccomp/AppArmor/SELinux profiles standalone | syscall/mac hardening | N/A on Windows | good defense-in-depth | profile maintenance | ~0 | folded into D/B as hardening layer, not the boundary itself |
| G. In-process interpreters (RestrictedPython etc.) | weak: bypass history is long | trivial | trivial | low | ~0 | rejected: false sense of safety for real tool execution |

**Decision (recommended):** layered — **Option D as the portable default
boundary**, applied identically on Linux CI/prod and Windows dev, with
per-platform *declared capability* (the sandbox reports which guarantees are
enforced vs unavailable on the current host, and this lands in the step
result and health endpoint). **Option B/C (rootless containers) as the
opt-in production profile** for deployments that have Docker, enabled by
config (`SANDBOX_BACKEND=process|container`), sharing the same
`ExecutionSandbox` interface. This respects the environment reality (V1's
Docker was never verifiable on this host) without making prod weaker than it
could be. Convenience never selects the mechanism: the platform decides what
is enforceable, the config decides which backend is used, the result records
what actually held.

---

## 4. Recommended architecture

### Principle: one doorway, same locks

Every untrusted spawn goes through one interface; the kernel keeps gating
*before* it; artifacts, runs, outcomes, and audit stay *after* it. Nothing
else about the pipeline changes.

```
registry.run (kernel authorize + audit)          ← unchanged
      │
      ▼
ExecutionSandbox.run(spec)                       ← NEW boundary
      spec = {argv|shell=False, cwd=task_scope, env=minimal, timeout_s,
              cpu_seconds, memory_bytes, max_pids, fsize_bytes, network=deny}
      ├─ backend = container | process (config; per-OS capability check)
      ├─ filesystem: task scope mounted/rooted; host root invisible
      ├─ process limits: rlimits/cgroups/Job Object
      ├─ network: default-deny namespace; optional egress proxy
      ├─ supervision: wait, kill process tree on terminal state, reap
      └─ returns SandboxResult {exit_code, stdout, stderr, killed_reason,
                                limits_hit, enforced_guarantees, duration_ms}
      │
      ▼
adapter maps SandboxResult → AdapterResult      ← success/error mapping only
      │
      ▼
pipeline: unchanged outcomes, artifacts, events, audit
```

### Interface (design sketch, not implemented)

```python
# services/execution/sandbox.py (planned)
@dataclass(frozen=True)
class SandboxSpec:
    argv: list[str]                 # exec-style only; shell joined by the backend, never str
    cwd: str                        # absolute, inside TASK_WORKSPACE/<task_id>
    env: dict[str, str]             # explicit allowlist build; never os.environ.copy()
    timeout_s: float
    cpu_seconds: int | None
    memory_bytes: int | None
    max_pids: int | None
    fsize_bytes: int | None         # per-process file-size rlimit
    network: Literal["deny", "allow"] = "deny"
    workspace_writable: str         # = cwd; only writable path
    workspace_readable: list[str]   # default [cwd]; never host root

@dataclass
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    killed_reason: Literal["timeout", "memory", "cpu", "pids", "fsize",
                           "network", "supervisor", None]
    limits_hit: list[str]
    enforced: list[str]   # which guarantees the backend actually applied

class ExecutionSandbox(Protocol):
    async def run(self, spec: SandboxSpec) -> SandboxResult: ...
    def capabilities(self) -> dict[str, bool]  # per-host honest report
```

* `ShellExecAdapter` compiles its (allowlisted) command into argv via
  `shlex.split` and loses `create_subprocess_shell` entirely; the shell, if
  needed, is an explicit `["/bin/sh", "-c", ...]` inside the confined
  environment where it can only reach the mounted scope.
* `PythonExecAdapter`, codegen smoke-run, and DoD execution pass their
  current argv/cwd unchanged, plus limits; env is built by the adapter, not
  inherited.
* AdapterContext gains nothing; the scope derivation (`_task_scope`) is
  reused so the sandbox and the clamps can never disagree about the task's
  directory.

### What stays intact (explicitly)

| Invariant | How it is preserved |
|---|---|
| Capability kernel authorization | `registry.run` gates before the sandbox exists in the call path; the sandbox is unreachable without a kernel decision |
| Task workspace isolation | scope derivation shared by clamps and sandbox; sandbox makes it physical, not just conventional |
| Execution runs / artifact registry / audit events | downstream of adapters; untouched — same schema, same registration path, same audit logging |
| Honest outcomes | sandbox failures map to step failures; mapping defined in §5 — no new outcome values, no silent success |
| Cleanup & retention | scope dirs remain ordinary workspace files; existing retention/cleanup semantics apply unchanged; sandbox adds no external state |
| Frontend progress | step events/status polling unchanged; only `output.limits_hit`/`enforced` fields are new (additive, displayed as-is or ignored) |

---

## 5. Failure semantics

Every sandbox failure is a step failure with an honest, scrubbed error; the
outcome vocabulary never grows. Mapping (SandboxResult → AdapterResult →
pipeline):

| Condition | SandboxResult | Step result | Task outcome if final |
|---|---|---|---|
| Sandbox cannot start (backend missing/misconfigured) | spawn error, `killed_reason="supervisor"` | failed: `execution environment unavailable: <capability gap>` | **failed** — never silently falls back to unsandboxed execution |
| Wall-clock timeout | `killed_reason="timeout"` | failed: `timed out after Ns` (existing wording) | failed → recovery may retry (existing policy) |
| Memory cap exceeded (OOM kill) | `killed_reason="memory"` | failed: `memory limit exceeded` | failed |
| CPU quota exhausted | `killed_reason="cpu"` | failed: `cpu limit exceeded` | failed |
| Fork bomb / PID cap | `killed_reason="pids"` | failed: `process limit exceeded` | failed |
| File-size cap hit | `killed_reason="fsize"` | failed: `output size limit exceeded` | failed |
| Network denied (connection attempt) | `killed_reason="network"` or EPERM in stderr | failed: `network access denied by sandbox policy` | failed |
| Filesystem access denied (outside scope) | EACCES from confinement | failed: `access denied: path outside workspace` (same wording as clamps today) | failed |
| Child escapes and dies / kills its own group | supervisor reaps tree; nonzero exit | failed: `process terminated abnormally` | failed |
| Artifact collection fails after a *successful* run | n/a (outside sandbox) | step marked failed with `artifact registration failed`; no artifact row is born | **failed** — honesty beats salvaging a run whose evidence cannot be recorded |

Rules: (1) the error text is scrubbed of internal paths like any other; (2)
`limits_hit` is recorded in the step output for diagnosis, and `enforced` is
recorded so a *weaker dev boundary* is visible in results rather than
assumed; (3) recovery behaves exactly as today (retry/re-arm), except a
sandbox-availability failure is not retried (it will fail again); (4) no
failure mode can leave a run `running` — existing crash backstops apply.

---

## 6. Test matrix

New tests are *sandbox* tests; existing 361-test suite must stay green
untouched. Planned (unit unless noted):

| Area | Test | Assertion |
|---|---|---|
| Allowed execution | benign python/shell step through sandbox | exit 0, stdout captured, artifact produced, outcome verified |
| Workspace containment | code writes relative + absolute inside scope | all land in task scope; registered correctly |
| Path traversal (app layer) | `../`-heavy relative paths through clamps | denied with workspace wording; audited |
| Symlink escape | scope-internal symlink → host path, read+write attempts | denied at clamp (resolved) *and* at sandbox (fs confinement); regression test for the V1 gap |
| Secret isolation | code prints `os.environ`/attempts config read | env contains only the allowlist; no SECRET_KEY/DATABASE_URL tokens in stdout/stderr (grep) |
| Network policy | connect to loopback + external host with `network=deny` | fails fast; `killed_reason="network"`; no packets (CI-safe: assert error class, not packet capture) |
| Timeout | `while True: pass` | killed at timeout, `killed_reason="timeout"`, task failed, run row closed |
| Memory limit | allocate > cap | killed, `killed_reason="memory"` (Linux CI; skipped-if-unsupported on Windows with `enforced` reflecting reality) |
| CPU limit | busy-loop > quota | killed, `killed_reason="cpu"` (same platform skip discipline) |
| Process cleanup | spawn grandchildren then hang | entire tree reaped after kill; no orphans survive shutdown (shutdown hook test) |
| Artifact collection | successful run producing files + a run that dies mid-write | registry rows consistent; partial file either registered honestly with missing bytes semantics or absent — never a phantom hash match |
| Cross-task isolation | two concurrent tasks, each probing sibling scope path | both denied; no cross-reads |
| Restart/shutdown (integration) | background pipeline killed mid-execution, app restarted | orphan sweep marks run failed as today; no sandboxed process survives restart |

Platform discipline: tests that need cgroups/unshare are marked and skipped
with an explicit reason on hosts lacking them (CI = Linux → all enforced;
Windows dev = subset with honest `capabilities()` reporting). A test never
passes because a limit *wasn't enforced* — enforcement and expectation come
from `capabilities()`.

---

## 7. Migration plan

Phased so every step keeps the 361-test suite green and V1 tasks/rows
meaningful:

1. **Introduce `ExecutionSandbox` + process backend behind the interface**
   (new module only). No adapter switched yet. Unit tests on the backend.
2. **Switch `PythonExecAdapter`** to the sandbox (same argv/cwd semantics,
   env rebuilt minimally). Behavioral deltas: env no longer inherited
   (tests asserting env contents updated *only if* they encoded the old
   leak), limits enforced. Suite green.
3. **Switch DoD execution + codegen smoke-run** to the sandbox. DoD honesty
   semantics unchanged (fresh subprocess expectation preserved).
4. **Switch `ShellExecAdapter`** to argv + sandbox; keep the existing
   allow/block policy as the pre-filter. Biggest behavioral delta — keep the
   allowlist default conservative exactly as today.
5. **Container backend (opt-in, config-gated)** for Linux prod: same
   interface, image with the toolchain only; docs updated with
   verification-status language (never "verified" without a live run).
6. **Quotas + hardening leftovers:** per-task workspace quota, cumulative
   per-run budget, symlink-resolving clamp (this one can land as early as
   phase 1 as an independent security fix).

Compatibility guarantees across phases: task rows, execution_runs, artifact
registry, task_events, retention, frontend polling — no schema change and no
API change; steps carry two additive output fields (`limits_hit`,
`enforced`). In-flight tasks during a deploy finish on the old path (their
run rows were claimed under it); new executions use the sandbox. If any
phase regresses the suite or a live gate, that phase is reverted — the
sandbox is additive capability, never a forced rewrite.

---

## 8. Risks and unresolved questions

**Risks**

- *Windows parity ceiling:* Job Objects cannot give filesystem/network
  confinement; dev-host guarantees are weaker than prod's and must stay
  visibly so (`capabilities()`, docs). No UX should imply dev is fully
  sandboxed.
- *Allowlist drift on shell:* the current default allowlist contains
  `python` and substring matching; phase 4 must re-derive it, or the
  sandbox inherits a policy designed for a weaker boundary.
- *Container backend socket exposure:* mounting the Docker socket into the
  API widens host attack surface; if adopted, prefer rootless/podman and a
  separate supervisor process.
- *Latency and image lifecycle:* cold container starts and image updates
  become operational concerns (pinning, CVE refresh) that V1 never had.
- *Two backends to keep honest:* `enforced` reporting must be tested per
  backend, or the weak path will eventually be claimed as strong.

**Unresolved questions (need product/platform decisions)**

1. Network policy default for *research-class* capabilities (`web_read`
   already needs real HTTP): per-adapter egress proxy vs deny-by-default
   with an explicit allow profile per task class?
2. Is a per-task *cumulative* budget (across retries/recovery) desired, and
   what are the numbers (CPU-seconds, memory, wall clock, disk)?
3. Does the container backend become the *required* prod profile (fail
   closed) or an option (fail documented)? This decides how `SANDBOX_BACKEND`
   errors behave at startup.
4. Toolchain image contents: which interpreters/compilers are in scope for
   V2 codegen (Python only today; Node later?), and who owns the image
   supply chain?
5. Should `capabilities()` be surfaced in `/health/ready` (ops truth) or
   only in step results (evidence)? Both have audit value; both add
   disclosure surface.
6. Metrics/diagnostics for sandbox kills (counts by reason) — out of scope
   for V1's no-metrics stance; confirm V2 wants them before phase 5.

---

*Baseline this design was derived from: commit `74e2c1d6a99a06b064ef6486f337892818961fb6`
(tag `v1.0.0`), audit performed against the working tree at HEAD `7227aa4`
(CI commit). No code, schema, kernel, or Docker changes were made.*
