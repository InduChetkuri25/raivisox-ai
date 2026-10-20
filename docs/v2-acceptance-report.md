# V2 Final Acceptance Report — Execution Sandbox, Container Backend, Resource Quotas

Date: 2026-09-29
Scope: V2 phases 0–5 (symlink-resolving containment, process sandbox, codegen/DoD,
shell argv migration, container backend, resource quotas + cumulative budgets).
No V3 / Agent Action Graph work is included, started, or implied by this report.

## 1. Verdict summary

| Gate | Result | Evidence |
|---|---|---|
| Linux CI, all three jobs | **PASS** | run `36599092879` on `d88fd42` — every job `success` |
| Live container gate (19 tests, real Docker) | **PASS** | `19 passed, 67 deselected` with the skip→fail guard active |
| Live container quota kills | **PASS** | memory OOM → `memory`, CPU → `cpu`, fsize → `fsize`, pids → fork bomb blocked |
| Real application paths in containers | **PASS** | `python_exec`, `shell_exec`, codegen + DoD through `ContainerSandbox` |
| Local unit suite (Windows host) | **PASS** | `512 passed, 40 skipped` (Windows-only skips) |
| Local live LLM gate (real Ollama) | **PASS** | `4 passed` |
| Frontend typecheck + production build | **PASS** | local `tsc --noEmit` clean, `next build` ok; CI job same |
| `v1.0.0` tag untouched | **PASS** | `git rev-parse v1.0.0^{commit}` = `74e2c1d6a99a06b064ef6486f337892818961fb6` |
| T1–T13 threat model | **10 PASS, 2 PARTIAL, 0 NOT VERIFIED** | §5 |

Commits under acceptance (newest first): `d88fd42`, `5488686`, `199e49a`, `29919f6`,
`f1473fe`, `86070d8`, `3228e5f`, `6733eae`, `bacbd6f` (Phase 5), `5be77ec`,
`9907958` (Phase 4), `eb12496`, `31b6196` (Phase 3) … back to `9b7491b` (Phase 0).

## 2. Live CI container gate (authoritative Linux evidence)

Workflow: `.github/workflows/ci.yml`, repository `InduChetkuri25/raivisox-ai`,
run [`36599092879`](https://github.com/InduChetkuri25/raivisox-ai/actions/runs/36599092879),
commit `d88fd42`, Ubuntu runner, `SANDBOX_BACKEND=container`,
`SANDBOX_CONTAINER_IMAGE=python:3.12-slim`.

All three jobs: `success`.

### 2.1 Runtime proof before any live result is accepted

The job first proves the runtime actually works, so an unavailable runtime can never be
mistaken for a pass (`docker run --rm "${SANDBOX_CONTAINER_IMAGE}" true`), then pulls the
image, then runs the live half. If the live half reports any skip, the job fails:

```sh
if grep -qE 'skipped|SKIPPED' live_container_results.txt; then
  echo '::error::Live container tests skipped although the runtime was proven usable - treating as gate failure'
```

### 2.2 Live half — 19 tests, zero skips

```
collected 86 items / 67 deselected / 19 selected
====================== 19 passed, 67 deselected in 12.30s ======================
```

Those 19 tests are the ten Phase-4 isolation tests, the five Phase-5 quota tests, and the
four real-application-path tests:

- argv execution, workspace read/write, host filesystem and Docker socket invisible,
  symlink escape cannot cross the container boundary, secret/host-env isolation,
  timeout kill + reclamation, bounded output, network deny, non-root execution,
  no container survives a normal run;
- memory limit kill attributed `memory` (real OOM), PID limit blocks a fork bomb,
  CPU limit kill attributed `cpu`, fsize ulimit makes the write fail with `EFBIG`
  attributed `fsize`, cleanup after a quota kill;
- `python_exec`, `shell_exec`, codegen + DoD through the real app path in containers,
  and kernel denial preventing container execution.

### 2.3 Non-live half in the same job

```
collected 86 items / 19 deselected / 67 selected
================ 64 passed, 3 skipped, 19 deselected in 10.74s =================
```

The three skips are host-specific tests (Windows-only Job Object coverage) skipping on
Linux, not container coverage.

### 2.4 Backend and frontend jobs

```
========= 524 passed, 32 skipped, 17694 warnings in 110.18s (0:01:50) ==========
== 524 passed, 28 skipped, 4 deselected, 17694 warnings in 102.18s (0:01:42) ===
```

The second line is the run with the `live_llm` marker deselected (it needs a local
Ollama server, which CI does not provide).

Frontend: `npm ci` → `added 373 packages`, `npx tsc --noEmit` clean,
`✓ Compiled successfully`, route table emitted.

## 3. Local regression (Windows 11 development host)

CPython 3.12.2, no Docker, no Podman, no WSL — so **no container runtime exists on this
host**, and every live container test skips here with an explicit reason. Local skips are
never counted as passes; the authoritative live evidence is §2.

| Suite | Result |
|---|---|
| `tests/unit/*.py` (33 files, OneDrive-safe splits) | `512 passed, 40 skipped` |
| `tests/test_live_llm.py -m live_llm` (real Ollama) | `4 passed` |
| `apps/web`: `npx tsc --noEmit` | clean |
| `apps/web`: `npm run build` | success |
| `tests/integration`, `tests/e2e` | no test modules exist (0 collected) |

The 40 local skips are the POSIX-only paths (rlimit-based CPU/fsize attribution, the
process-group `/proc` cleanup assertion, Windows Job Object tests on non-Windows) plus the
container `live` tests. Each carries an explicit skip reason.

## 4. What the local suite cannot prove

Stated plainly, because the distinction is the point of this report:

- **No container execution on this host.** Command construction, backend selection, and
  the fail-closed unavailable path are verified locally; actual container isolation and
  quota kills are verified only on Linux CI (§2).
- **No Windows cpu/memory/pids enforcement.** The process backend reports those limits as
  *not enforced* rather than pretending; `capabilities()` never claims them on Windows.
- **No live LLM verification in CI.** `live_llm` runs against a real local Ollama only.

## 5. T1–T13 security checklist

Grades are `PASS`, `PARTIAL`, or `NOT VERIFIED` — nothing else.

| ID | Threat | Grade | Evidence |
|---|---|---|---|
| T1 | Filesystem escape (read/write outside scope) | **PASS** | live: host sentinel file unreadable, Docker socket absent inside the container, only the task workspace is writable; `--read-only` rootfs with a 64 MB `noexec,nosuid` `/tmp` tmpfs |
| T2 | Cross-task workspace access | **PASS** | exactly one bind mount — the execution's own workspace — asserted by the argv rule tests and proven live (nothing else is visible); app-layer scoping remains as defence in depth |
| T3 | Application secret theft (env/config) | **PASS** | live: `RAIVISOX_PHASE4_SECRET` reads `ABSENT` and the host `PATH` value never appears; the child env is an explicit allowlist built from the spec, never inherited |
| T4 | Arbitrary process execution beyond the step's need | **PARTIAL** | `--network none` + read-only rootfs + image with no compiler prevent fetching or installing toolchains live; but the image is the general-purpose `python:3.12-slim`, `pip` is present in `PATH`, and PATH is not pruned to a per-adapter toolchain |
| T5 | Network access from untrusted code | **PASS** | live: `--network none`, egress attempt blocked, `network_deny` reported in `enforced` |
| T6 | Excessive CPU | **PASS** | live: CPU limit kill, attributed `cpu`; process backend attributes POSIX `SIGXCPU`/`SIGKILL`-with-cpu-only as `cpu` and refuses when unenforceable in strict mode |
| T7 | Excessive memory | **PASS** | live: memory limit → real OOM kill attributed `memory` (cgroup, not host OOM); process backend reports memory as unenforced on Windows instead of faking it |
| T8 | Disk exhaustion | **PARTIAL** | app: `QuotaEnforcer.check_write` refuses an oversized write before bytes land (unit-tested), cumulative budget covers repeated runs; OS: per-file `fsize` ulimit live-verified (`EFBIG` → `fsize`). **No total-disk cap on the writable workspace mount** — many files each under the per-file limit can still grow the host disk until the wall-clock/cumulative budget stops the run |
| T9 | Fork/process bombs | **PASS** | live: PID limit blocks a fork bomb, the spawn fails cleanly and the container is reclaimed; app-level blocklist remains a fast pre-filter only |
| T10 | Symlink / path traversal | **PASS** | app: realpath-resolving containment at use time (`services/security/path_containment.py`, Phase 0); OS: live test proves a symlink pointing outside the workspace cannot be followed across the container boundary |
| T11 | Long-running processes outliving the request | **PASS** | live: timeout kills the container and it is reclaimed, no container survives a normal run; process backend kills the process group and the supervisor reaps the tree |
| T12 | Host/environment inspection | **PASS** | live: minimal explicit env, host filesystem and runtime socket invisible; outputs are scrubbed before persistence |
| T13 | Audit/outcome tampering (task writes to app DB) | **PASS** | app credentials never enter the child env (explicit env only) and no application data is mounted — the single mount is the workspace; proven live by the mount-invisibility test |

### 5.1 Residual risk behind the two PARTIALs

1. **T4** — images are generic language images pulled by tag, so the toolchain surface is
   whatever the image ships (`pip` present). What holds the line is the combination of
   `--network none` (cannot fetch), read-only rootfs (cannot install into system paths),
   non-root, `--cap-drop ALL`, and `no-new-privileges` — not a purpose-built minimal
   image. Per-adapter minimal images are the honest fix and are not built.
2. **T8** — the writable workspace is a host-mounted directory with no filesystem-size
   quota. Per-write and per-file bounds exist; a total quota (project quota, loopback
   image, or `--storage-opt size=`) does not.

## 6. Attribution limits that were fixed during this gate

Recorded because they changed behaviour, not just tests:

- CPython 3.12.14 (CI) rejects `create_subprocess_exec` combined with `preexec_fn`
  outright; resource limits now run through a trusted wrapper exec
  (`ProcessSandbox._RLIMIT_WRAPPER`) that sets rlimits and then execs the real child.
- Live CI showed a CPU kill arriving as exit `137` while memory was not enforced. The
  attribution is now a pure, unit-tested function (`quota_reason_from_exit`): `152`→cpu,
  `153`→fsize, `136`→supervisor, `137`→memory only when memory is enforced and cpu is
  not, `137`→cpu only when cpu is enforced and memory is not, `137` with both → supervisor
  (ambiguous, never guessed), `EFBIG`/`File too large` with fsize enforced → fsize.
- CPython ignores `SIGXFSZ`, so an fsize kill surfaces as a Python `OSError: File too
  large` and exit 1; the limit held, and attribution now reads the child's own error text
  when fsize was enforced.

## 7. Out of scope / explicitly not claimed

- Production Docker/deployment readiness. Dockerfiles elsewhere in the repo are not
  evidence of that.
- Windows container isolation (AppContainer/Job Objects) beyond what the process backend
  reports as enforced.
- Any V3 capability: no Agent Action Graph, no graph executor, no new release tag.

## 8. Verdict

Every gate in §1 is green with evidence that is reproducible from the run and commit
references above; the two PARTIALs are documented limitations rather than failures, and
neither hides an unenforced control behind a false claim.

**V2 FINAL ACCEPTANCE: PASSED**
