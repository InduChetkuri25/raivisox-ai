# RAIVISOX Execution Sandbox — V2 Design

**Status:** V2 phases 0–5 are implemented and committed (see §9 for the
exact per-phase verification status — including what the container backend
has and has NOT been verified to do). The capability kernel and the
database schema remain untouched; no claim in this document is stronger
than what its tests actually proved on the host that ran them.

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
| T8 | Disk exhaustion | write infinitely in scope | app + OS | app: workspace write quota (bytes/entries/depth/escapes) enforced **before** each write + pre-run gate + post-run measurement (§5.1); OS: container filesystem size limit where supported — **not implemented**, so a running child is bounded by its per-file fsize limit and wall clock, not by a total disk quota |
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

### 5.1 Workspace write quotas — and what they are NOT

`services/execution/workspace_quota.py` bounds what one task may write
inside its own scope: total bytes, per-file bytes, entry count (files +
directories, hidden entries included), path depth, and links that resolve
outside the scope. Two enforcement points:

- **Application writes** (`fs_write`, the deliverable `finalize` write):
  `WorkspaceEnforcer.reserve()` checks *before* any bytes land, crediting
  back the bytes a rewrite replaces and holding in-flight reservations so
  concurrent writes in the process cannot collectively overshoot. A
  refusal is an ordinary adapter failure (audited, nothing written) — the
  caller's content is never silently truncated.
- **Executions** (`ProcessSandbox`, `ContainerSandbox`): a pre-run gate
  refuses a run whose scope is already over quota, unmeasurable, or holds
  an escaping link; after the run the scope is measured again and a
  violation is reported in `limits_hit` (`disk`, `disk:<kind>`) and in the
  result's stderr — the child's exit code is never rewritten into a fake
  quota kill.

Measurement never follows a symlink, never opens a file (`lstat` +
`scandir` only), counts hardlinked inodes once, treats an unreadable
subtree as a violation (an unmeasurable scope could hide bytes), and
flags any link whose resolution leaves the root.

**This is not a filesystem-wide disk quota, and three limitations are
explicit rather than hidden:**

1. A *running* child can keep writing sub-limit files until its per-file
   `RLIMIT_FSIZE`/`--ulimit fsize`, its wall-clock timeout, or the post-run
   measurement stops further runs. Mid-write cumulative enforcement needs
   an OS mechanism (per-container storage quota, project/XFS quota, a
   size-bounded volume) that this repository does not implement.
2. A hardlink created inside the scope that points outside it bypasses
   "bytes live in the workspace" reasoning; hardlink creation cannot be
   intercepted at the app layer. Inodes are counted once, so it is never
   *double* counted, and the limitation is stated here.
3. The quota bounds *workspace* bytes. Host disk consumed by the runtime's
   image store, logs, or anything outside the scope is out of scope.

Defaults are generous (256 MiB total, 8 MiB per file, 10k entries, depth
32) and tunable via `SANDBOX_WORKSPACE_MAX_*`; `0`/`off` disables a
dimension explicitly. Both sandbox backends apply the quota by default
(`build_default_sandbox`), so no adapter can opt out silently.

### 5.2 Container image pinning (supply chain)

A tag is mutable: `python:3.12-slim` today is not the bytes it was last
month, so a green gate on a tag certifies "whatever was current". The
container backend therefore runs **immutable digests only**
(`services/execution/image_pins.py`):

- `APPROVED_IMAGES` maps an approved `repo:tag` to its digest. A
  configured digest that contradicts the approved entry is refused
  (typo or substitution — either way, fail).
- A tag with no approved entry is refused by default. The documented
  escape hatch `SANDBOX_ALLOW_UNPINNED_IMAGE=1` is reported as
  `image_pinned: false`, so the weakening is visible in the result.
- Before a container starts, `verify_image` pulls the
  **digest-qualified** reference and requires that exact digest to appear
  in the local image's `RepoDigests`. Pull failure, missing image,
  unreadable inspect output, or a digest mismatch is an honest
  fail-closed `sandbox image verification failed: …` — no container runs.
- The run itself uses `repo@sha256:…`, so a retag between verification and
  execution cannot change which bytes execute.

**Approved digest update procedure (reviewed, never automatic):**

1. Resolve the new manifest-list digest for the tag, e.g.
   `curl -s https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim`
   and take the top-level `digest` field (not the per-architecture ones).
2. Update `APPROVED_IMAGES` in `services/execution/image_pins.py` **and**
   `SANDBOX_CONTAINER_IMAGE` in `.github/workflows/ci.yml` in the same
   commit, with the resolution date in the comment.
3. Run the live container gate. `tests/unit/test_image_pins.py` asserts the
   workflow's image equals the approved entry, so a half-updated pin fails
   locally instead of producing a gate that certified a different image.

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
(CI commit).*

---

## 9. Implementation status (V2)

| V2 phase | Scope | Commit | Verification |
|---|---|---|---|
| 0 | symlink-resolving containment (`services/security/path_containment.py`) | `9b7491b` | unit tests |
| 1 | `ExecutionSandbox` + process backend; `python_exec` migrated | `f95ba8f` | unit tests, host-enforced guarantees |
| 2 | codegen smoke-run + DoD execution through the sandbox | `364ebe9` | unit tests, host-enforced guarantees |
| 3 | shell/`TerminalSandbox` argv migration (§7 phase 4); no `create_subprocess_shell` remains in untrusted execution | `31b6196` | unit tests, host-enforced guarantees |
| 4 | container backend (§7 phase 5) + `SANDBOX_BACKEND` selection | `9907958` | unit tests + **live** container gate green on Linux CI (see below) |
| closeout | workspace write quotas (T8, §5.1) + immutable image pinning (§5.2) | `42e6899` | 70 new unit tests; Linux gate green with a digest-pinned image (`19 passed` live, `594 passed` backend) |
| 5 | resource quotas + cumulative execution budgets (§5 vocabulary, §7.6) | `5be77ec` | quota model/budget/enforcer unit-tested on both hosts; **live** container quota kills verified in the Linux CI gate (`memory` OOM, CPU, fsize, pids) and the POSIX process-backend attributions (`cpu` SIGXCPU, `fsize` EFBIG) verified on Linux CI too — see the evidence below |

**Container backend verification status (never claim more):**

- **Verified on this host (no container runtime present):** backend
  selection (`SANDBOX_BACKEND=process` default; `container` selects the
  container backend; anything else raises a configuration error — never a
  silent fallback), the fail-closed unavailable path (honest
  `execution environment unavailable` result, canary-proven that no host
  execution occurred), spec-validation parity with the process backend,
  and every `docker/podman run` construction rule (exactly one mount — the
  writable workspace; never the runtime socket; never `--privileged`;
  never `--network host`; `--cap-drop ALL` + `no-new-privileges` +
  `--read-only` + non-root `--user`; explicit environment only; argv never
  joined into a shell string) — `tests/unit/test_sandbox_phase4.py`.
- **NOT verified here:** live container execution. The `test_live_*` tests
  and the real-application container-path tests
  (`tests/unit/test_container_app_path.py`, marked `live`) run only where a
  runtime **and** the configured image exist, and skip with an explicit
  reason otherwise. A live container result is never faked, and the
  existence of Dockerfiles elsewhere in this repo is not evidence of
  Docker production readiness.

**Local Windows vs Linux CI (authoritative split):**

- **LOCAL WINDOWS (development host):** no Docker, no Podman, and WSL is
  not installed, so no Windows container runtime can exist here. The local
  runs verify the fail-closed unavailable path and every command
  construction rule; the live tests **skip locally with an explicit
  reason** — a local skip is never represented as a live pass.
- **LINUX CI (`.github/workflows/ci.yml`, job `container-gate`):** the
  authoritative live-container verification. The job proves the runtime is
  installed and usable (version output, `docker info`, a real `docker run
  --rm … true`), pulls `SANDBOX_CONTAINER_IMAGE`, then runs
  `pytest … -m live -rs` over both live suites with `SANDBOX_BACKEND=container`:
  any skip or zero-passed outcome FAILS the gate (a runtime was already
  proven, so a skip can only mean the gate is broken), and the real
  RAIVISOX paths (python_exec, shell_exec, codegen/DoD through the
  kernel-gated registry → ContainerSandbox → SandboxResult → result/
  artifact evidence → honest success/failure) execute inside actual
  containers. The normal `backend-tests` job stays independent and keeps
  the process backend as the default (`SANDBOX_BACKEND=process` sanity
  step); the container gate never weakens security flags to pass.
**Live Linux gate result (authoritative, not a local skip):** CI run
`36594685532` on commit `5488686` (GitHub-hosted `ubuntu-24.04`, CPython
3.12.14) is **green in all three jobs**:

- `container-gate`: runtime proven (docker version + `docker info` + a
  real `docker run … true`), image pulled, then `pytest … -m live -q -rs`
  → **19 passed, 0 skipped** (any skip fails the gate) and the non-live
  half → **64 passed, 3 skipped**. This covers the live container quota
  kills with honest attribution (`--memory` OOM → `killed_reason=memory`,
  CPU ulimit → `cpu`, `--ulimit fsize` → `fsize`, `--pids-limit`), the
  hardening invariants (non-root user, `--cap-drop ALL` +
  `no-new-privileges` + `--read-only`, no network, filesystem confinement)
  and the real RAIVISOX paths (python_exec / shell_exec / codegen / DoD)
  executing inside actual containers.
- The container image is **digest-pinned** (§5.2): the job pulls
  `python@sha256:a6e34c…` and each run verifies that digest before
  executing it, so a retag cannot change which bytes the gate certified.
- `backend-tests`: **594 passed, 32 skipped** for the full suite and
  **594 passed, 28 skipped** for the explicit `-m "not live_llm"`
  selection, on a clean Linux checkout — including the POSIX-only
  process-backend quota attributions (SIGXCPU → `cpu`, CPython's ignored
  SIGXFSZ surfacing as EFBIG → `fsize`) and the process-group cleanup
  test that proves a sandbox child never shares the host's group.
- `frontend-validation`: `tsc --noEmit` clean and the production build
  succeeds from the committed lockfile (the Linux-targeted lockfile the
  job's `npm ci` requires).

Local (Windows) runs remain a development signal only: the POSIX-only
quota and `/proc` tests skip there with an explicit reason and are never
reported as passes.

The consolidated acceptance evidence — CI run and commit references, the
local regression counts, the T1–T13 checklist with its two documented
PARTIALs (image toolchain minimality, no total-disk quota on the
workspace mount) and the final verdict — lives in
[`v2-acceptance-report.md`](v2-acceptance-report.md).

- Unresolved question 3 (fail closed vs. fail documented) is answered as:
  an **invalid** `SANDBOX_BACKEND` value raises loudly at construction; a
  **valid but unavailable** backend fails each execution honestly (§5 row
  1) with no process-backend substitution.
  The *production default* is answered separately (V3 audit finding H1) by
  `services/execution/sandbox_policy.py`: `ENVIRONMENT=production` must
  declare `SANDBOX_BACKEND` explicitly, and the unconfined `process` backend
  is refused unless the operator sets
  `SANDBOX_ALLOW_PROCESS_IN_PRODUCTION=1` (the documented **fail documented**
  posture, logged as an explicit UNCONFINED warning). Development/test keep
  the `process` default. This is a selection policy only — it does not add
  isolation, and the deployment decision (dedicated sandbox executor vs. a
  remote rootless runtime) is recorded in `docs/deployment.md` §10.
