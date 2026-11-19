"""ExecutionSandbox — the single doorway for untrusted process execution.

V2 Phase 1 of the design in docs/sandbox-design.md: the interface plus a
platform-aware process backend. This module and its Phase 4 sibling
(services/execution/container_sandbox.py) are the ONLY places in the
codebase allowed to spawn an untrusted child process; every caller must go
through ``ExecutionSandbox.run`` with an explicit, argv-only ``SandboxSpec``.
``build_default_sandbox`` selects the backend via ``SANDBOX_BACKEND``
(development/test default: process; production requires an explicit
selection and refuses the unconfined process backend unless the operator
acknowledges it; invalid values raise — never a silent fallback to another
backend). The policy itself lives in ``services.execution.sandbox_policy``.

Phase 1 scope (intentionally small):
- argv execution only — never a shell string
- explicit minimal environment — the child NEVER inherits the API process
  environment wholesale
- working directory = the task's workspace scope (cwd is the confinement
  root the app layer enforces; see services/security/path_containment.py)
- bounded stdout/stderr capture
- wall-clock timeout with tree kill
- best-effort platform limits where the host can honestly enforce them

Honesty rules (non-negotiable):
- ``capabilities()`` reports only what THIS host actually enforces.
- ``SandboxResult.enforced`` records exactly what was applied to THIS run.
- A sandbox that cannot start FAILS the execution. There is no fallback to
  unsandboxed execution — an unconfined run is worse than no run.
- String blocklists are advisory pre-filters; they are never the boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from services.execution.quotas import ExecutionBudget, SandboxQuota
from services.execution.sandbox_policy import (
    SandboxConfigError,
    process_posture_notice,
    resolve_backend_name,
)
from services.execution.workspace_quota import (
    WorkspaceEnforcer,
    WorkspaceQuota,
    WorkspaceQuotaExceeded,
)
from services.security.path_containment import contained_path, realpath_norm

__all__ = [
    "KilledReason",
    "SandboxSpec",
    "SandboxResult",
    "ExecutionSandbox",
    "ProcessSandbox",
    "SandboxQuota",
    "ExecutionBudget",
    "WorkspaceQuota",
    "process_quota_reason",
    "SandboxConfigError",
    "build_default_sandbox",
]

_log = logging.getLogger(__name__)

# POSIX-only signal numbers. They are absent on Windows, where subprocess
# return codes are never negative — so the signal branch of the process
# attribution below is simply unreachable there (guarded, not assumed).
_SIGXCPU = getattr(signal, "SIGXCPU", None)
_SIGXFSZ = getattr(signal, "SIGXFSZ", None)

KilledReason = Literal[
    "timeout",
    "memory",
    "cpu",
    "pids",
    "fsize",
    "network",
    "supervisor",
]

# Environment variables a child may receive. Deliberately tiny: identity of
# the run, nothing that could carry host/provider credentials.
_ALLOWED_ENV_KEYS = ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
                     "WINDIR", "TEMP", "TMP", "PATH", "PYTHONIOENCODING",
                     "PYTHONUTF8", "HOME", "USERPROFILE", "APPDATA",
                     "LOCALAPPDATA")


@dataclass(frozen=True)
class SandboxSpec:
    """What the child may do. Built by the adapter, never by the request.

    ``argv`` is exec-style only — backends that need a shell join it
    themselves inside the confined environment; the spec never carries a
    shell string.
    """

    argv: list[str]
    cwd: str                        # absolute; the task's workspace scope
    env: dict[str, str]             # explicit allowlist build — never os.environ
    timeout_s: float = 30.0
    cpu_seconds: int | None = None      # enforced on POSIX (rlimit) only
    memory_bytes: int | None = None     # enforced on POSIX (rlimit) only
    max_pids: int | None = None         # reported as unavailable in Phase 1
    fsize_bytes: int | None = None      # enforced on POSIX (rlimit) only
    output_limit_bytes: int = 1_048_576  # bounded stdout/stderr capture
    network: Literal["deny", "allow"] = "deny"
    workspace_writable: str = ""        # defaults to cwd when empty
    workspace_readable: tuple[str, ...] = ()  # defaults to (cwd,) when empty

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("SandboxSpec.argv must not be empty")
        if not os.path.isabs(self.cwd):
            raise ValueError("SandboxSpec.cwd must be absolute")
        if not isinstance(self.env, dict):
            raise ValueError("SandboxSpec.env must be a dict")
        if self.timeout_s <= 0:
            raise ValueError("SandboxSpec.timeout_s must be positive")


@dataclass
class SandboxResult:
    """What actually happened, including what was actually enforced."""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    killed_reason: KilledReason | None = None
    limits_hit: list[str] = field(default_factory=list)
    enforced: list[str] = field(default_factory=list)  # guarantees applied
    error: str | None = None  # scrubbed, human-readable failure

    @property
    def success(self) -> bool:
        return self.error is None and self.killed_reason is None and self.exit_code == 0


class ExecutionSandbox(Protocol):
    """Boundary protocol — one doorway for every untrusted spawn."""

    async def run(self, spec: SandboxSpec) -> SandboxResult: ...

    def capabilities(self) -> dict[str, bool]:
        """Per-host honest report: which guarantees can actually be enforced."""
        ...


def _post_run_workspace(enforcer: WorkspaceEnforcer | None
                        ) -> tuple[list[str], str]:
    """Measure the workspace after an execution; report a violation.

    Shared by both backends. Returns ``(extra_limits_hit, stderr_note)``;
    a clean workspace yields an empty pair. The child has already exited by
    the time this runs, so a violation is REPORTED (never converted into a
    fake kill reason or a rewritten exit code).
    """
    if enforcer is None:
        return [], ""
    violation = enforcer.record_after_run()
    if violation is None:
        return [], ""
    return (["disk", f"disk:{violation.kind}"],
            f"[sandbox: {violation}]")


def _truncate(text: bytes | str, limit: int) -> tuple[str, bool]:
    """Decode to text and bound it to ``limit`` characters."""
    s = text.decode(errors="replace") if isinstance(text, bytes) else str(text)
    if len(s) <= limit:
        return s, False
    return s[:limit] + f"\n...[output truncated at {limit} chars]", True


class ProcessSandbox:
    """Process backend: one honest, portable confinement step above spawn.

    What this backend enforces everywhere (reported by ``capabilities`` and
    recorded per-run in ``SandboxResult.enforced``):
      - argv execution (never shell-string)
      - minimal explicit environment (no wholesale inheritance)
      - working directory set to the task scope
      - bounded output capture
      - wall-clock timeout with process-tree kill

    What this backend enforces on POSIX (rlimits applied by a trusted
    wrapper-exec before the real program runs):
      - CPU seconds (RLIMIT_CPU), address space (RLIMIT_AS), file size
        (RLIMIT_FSIZE). Quota terminations are attributed from the kernel's
        own signals: SIGXCPU → ``cpu``, SIGXFSZ → ``fsize`` (V2 Phase 5);
        any other signal death is reported as an honest generic supervisor
        failure, never as an invented quota cause.

    Per-run CPU usage is accounted via ``RUSAGE_CHILDREN`` deltas on POSIX
    (``cpu_used:<s>`` in ``limits_hit``, ``cpu_time`` basis) and as wall
    duration elsewhere — the basis used is the one reported. The POSIX
    counter is process-global, so under concurrent sandboxes the delta can
    include sibling children: it overcounts, which is budget-safe and
    therefore kept.

    What NO host gave Phase 1 a clean portable mechanism for is reported
    honestly as not enforced: filesystem confinement beyond cwd semantics,
    network deny, and process-count caps. On Windows, the POSIX rlimits are
    likewise reported unavailable rather than pretended — there is no fake
    Windows CPU/memory/PID enforcement layer. The caller sees the truth in
    ``capabilities()`` / ``SandboxResult.enforced`` and must decide policy
    (the integrated adapter fails the step when a *required* capability is
    missing — never silently runs unsandboxed).
    """

    def __init__(self, quota: SandboxQuota | None = None,
                 budget: ExecutionBudget | None = None,
                 workspace_quota: "WorkspaceQuota | None" = None) -> None:
        # V2 Phase 5: optional spec-level quota (required-limit enforcement)
        # and cumulative per-context budget. Both are additive; None keeps
        # the Phase 1 behavior byte-for-byte.
        self._quota = quota
        self._budget = budget
        # V2 closeout: optional workspace write quota around each run (the
        # child writes through the host filesystem, so the gate refuses a
        # run that could only grow an exhausted scope and measures what the
        # child produced afterwards).
        self._workspace_quota = workspace_quota
        self._posix = os.name == "posix"
        self._has_resource = False
        if self._posix:
            try:
                import resource  # noqa: F401  (POSIX-only module)

                self._has_resource = True
            except ImportError:  # pragma: no cover
                self._has_resource = False

    # ------------------------------------------------------------------
    # Capability reporting
    # ------------------------------------------------------------------

    def capabilities(self) -> dict[str, bool]:
        return {
            "argv_execution": True,
            "minimal_env": True,
            "working_dir": True,
            "bounded_output": True,
            "timeout_kill": True,
            "cpu_limit": self._posix and self._has_resource,
            "memory_limit": self._posix and self._has_resource,
            "fsize_limit": self._posix and self._has_resource,
            "pids_limit": False,   # Phase 1: no portable mechanism yet
            "network_deny": False, # Phase 1: needs namespace/container backend
            "fs_confinement": False,  # beyond cwd semantics: Phase 1 reports no
            # App-layer (no OS support needed): a workspace write quota is
            # enforced around each run when one is configured.
            "workspace_quota": self._workspace_quota is not None,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _build_env(self, spec: SandboxSpec) -> dict[str, str]:
        """Explicit, minimal child environment. Never inherits wholesale."""
        env: dict[str, str] = {}
        for key in _ALLOWED_ENV_KEYS:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        # Caller-provided values win, but only through the same allowlist —
        # a spec cannot smuggle host secrets into the child.
        for key, val in spec.env.items():
            if key in _ALLOWED_ENV_KEYS and isinstance(val, str):
                env[key] = val
        return env

    def _preexec(self, spec: SandboxSpec) -> None:  # pragma: no cover
        """Deprecated child-side rlimits hook - superseded by the trusted
        wrapper-exec below (``_RLIMIT_WRAPPER``/``_wrap_argv_posix``).
        Retained name only for the capability-reporting history."""
        raise NotImplementedError("superseded by the rlimit wrapper-exec")

    # ------------------------------------------------------------------
    # V2 acceptance: preexec-free POSIX rlimit wrapper (trusted argv[0])
    # ------------------------------------------------------------------
    # asyncio.create_subprocess_exec(preexec_fn=...) fails to spawn on the
    # CI runner's CPython 3.12.14 (bare SubprocessError on every exec;
    # A/B-diagnosed: the same argv spawns fine WITHOUT preexec_fn). Beyond
    # that specific breakage, preexec_fn is documented fork-unsafe in
    # threaded processes. The boundary below is the standard alternative:
    # a generated wrapper re-applies the identical rlimits and os.execv's
    # the real argv. It is spawned WITHOUT preexec_fn; the executable is
    # the trusted interpreter's absolute path; the child code is the
    # embedded constant below; and a wrapper death (e.g. the soft limit
    # itself) fails the run honestly - there is no unconfined exec path.

    # Token grammar: NAME=SOFT[:HARD]; HARD defaults to SOFT (so "FOO=5"
    # means soft==hard==5, exactly as before) — see _wrap_argv_posix for why
    # the CPU limit deliberately uses a soft/hard pair. argv[2] carries the
    # intended environment KEYS (colon-separated) so the wrapper can hand the
    # child exactly that environment.
    _RLIMIT_WRAPPER = (
        "import os, resource, sys\n"
        "limits = []\n"
        "for token in sys.argv[1].split(','):\n"
        "    if token:\n"
        "        name, value = token.split('=')\n"
        "        soft, _, hard = value.partition(':')\n"
        "        limits.append((getattr(resource, name), int(soft), int(hard or soft)))\n"
        "for res, soft, hard in limits:\n"
        "    resource.setrlimit(res, (soft, hard))\n"
        "keep = set(sys.argv[2].split(':')) if sys.argv[2] else set()\n"
        "for key in list(os.environ):\n"
        "    if key not in keep:\n"
        "        del os.environ[key]\n"
        "os.execv(sys.argv[3], sys.argv[3:])\n"
    )

    def _wrap_argv_posix(self, spec: SandboxSpec,
                         env_keys: tuple[str, ...] = ()) -> list[str]:
        """POSIX argv with the rlimit wrapper as trusted argv[0].

        ``env_keys`` (the spec's built environment) is passed through so the
        wrapper can prune anything the interpreter added to its own environ:
        CPython's locale coercion (PEP 538) sets LC_CTYPE, and execv would
        otherwise hand that mutation to the child — an environment variable
        the spec never allowed.
        """
        limits: list[str] = []
        if spec.cpu_seconds is not None:
            # soft < hard: the kernel's soft-limit SIGXCPU is the
            # deterministic, attributable death. With soft==hard the kernel
            # races SIGXCPU against the hard-limit SIGKILL, and a SIGKILL
            # (reported as a generic supervisor kill) can win — the same
            # rule the container backend's ulimit uses.
            limits.append(
                f"RLIMIT_CPU={spec.cpu_seconds}:{spec.cpu_seconds + 1}")
        if spec.memory_bytes is not None:
            limits.append(f"RLIMIT_AS={spec.memory_bytes}")
        if spec.fsize_bytes is not None:
            limits.append(f"RLIMIT_FSIZE={spec.fsize_bytes}")
        if not limits:
            return list(spec.argv)
        return [
            sys.executable, "-c", self._RLIMIT_WRAPPER,
            ",".join(limits), ":".join(sorted(env_keys)),
            spec.argv[0], *spec.argv[1:],
        ]

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        # V2 Phase 5 — spec-level quota gate: a caller-required limit the
        # host cannot enforce fails the run honestly BEFORE any process
        # exists (never silently weaker, never a fake enforcement claim).
        # With strict_limits=False the caller accepted a best-effort run:
        # the result's ``enforced`` list stays the honest witness instead.
        if self._quota is not None and self._quota.strict_limits:
            missing = self._quota.missing_from(self.capabilities())
            if missing:
                return SandboxResult(
                    error=("sandbox quota not enforceable on this host: "
                           + ", ".join(sorted(missing))
                           + " (refused; no weaker unconfined run)"),
                    killed_reason="supervisor",
                    enforced=["spec_validation"],
                )
        # V2 Phase 5 — cumulative budget: a hard gate before the run (a
        # budget is a limit, not an advisory). Uses the budget's own
        # monotonic clock; no OS support required.
        budget = self._budget
        if budget is not None:
            try:
                budget.check_reserve(kind="run")
            except Exception as exc:
                # Honest mapping by dimension: wall-clock exhaustion IS a
                # timeout; CPU exhaustion is a cpu termination; any other
                # (e.g. run-count) stays a supervisor-class failure.
                kind = getattr(exc, "kind", "")
                reason = {"wall_clock": "timeout", "cpu": "cpu"}.get(
                    kind, "supervisor")
                return SandboxResult(
                    error=f"execution budget exhausted: {exc}",
                    killed_reason=reason,
                    limits_hit=["budget", kind or "budget"],
                    enforced=["budget"],
                )

        # The cwd MUST be contained in the declared writable workspace (the
        # task scope). This reuses the Phase 0 symlink-resolving boundary so
        # the sandbox and the clamps can never disagree.
        writable = spec.workspace_writable or spec.cwd
        if not contained_path(spec.cwd, writable):
            return SandboxResult(
                error="sandbox rejected spec: cwd outside writable workspace",
                killed_reason="supervisor",
                enforced=["spec_validation"],
            )
        readable = spec.workspace_readable or (spec.cwd,)
        for r in readable:
            if not contained_path(r, writable):
                return SandboxResult(
                    error="sandbox rejected spec: readable scope outside writable workspace",
                    killed_reason="supervisor",
                    enforced=["spec_validation"],
                )

        # V2 closeout — workspace write quota: refuse a run whose scope is
        # already over quota, unmeasurable, or holds a link that escapes.
        enforcer: WorkspaceEnforcer | None = None
        if self._workspace_quota is not None:
            enforcer = WorkspaceEnforcer(writable, self._workspace_quota,
                                         budget=budget)
            try:
                enforcer.check_before_run()
            except WorkspaceQuotaExceeded as exc:
                return SandboxResult(
                    error=f"workspace quota: {exc} (refused; the run could only "
                          "grow a scope that is already over its quota)",
                    killed_reason="supervisor",
                    limits_hit=["disk", f"disk:{exc.kind}"],
                    enforced=["workspace_quota"],
                )

        env = self._build_env(spec)
        enforced = ["argv_execution", "minimal_env", "working_dir", "bounded_output"]
        if enforcer is not None:
            enforced.append("workspace_quota")
        caps = self.capabilities()
        if spec.cpu_seconds is not None and caps["cpu_limit"]:
            enforced.append("cpu_limit")
        if spec.memory_bytes is not None and caps["memory_limit"]:
            enforced.append("memory_limit")
        if spec.fsize_bytes is not None and caps["fsize_limit"]:
            enforced.append("fsize_limit")

        start = time.monotonic()
        # POSIX: spawn the child in its OWN session/process group. The
        # timeout and cancellation cleanup kills the group
        # (os.killpg(getpgid(pid))) so descendants are reaped too - without
        # this, the child shares the parent's group and that kill signals
        # the host process itself (observed on CI: it SIGKILLed pytest and
        # the runner's worker, orphaning the job). start_new_session is the
        # documented, preexec-free way to do this (POSIX-only option).
        spawn_kwargs: dict[str, object] = {}
        if self._posix:
            spawn_kwargs["start_new_session"] = True
        try:
            spawn_argv = (self._wrap_argv_posix(spec, tuple(env))
                          if (self._posix and self._has_resource) else list(spec.argv))
            proc = await asyncio.create_subprocess_exec(
                *spawn_argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=env,
                **spawn_kwargs,
            )
        except Exception as e:  # spawn failure — honest failure, no fallback
            # Include the message: subprocess wraps the child-side exec
            # failure protocol (errno/strerror) in the exception text, and
            # without it a spawn failure is undiagnosable.
            return SandboxResult(
                error=f"spawn failed: {type(e).__name__}: {e}",
                killed_reason="supervisor",
                duration_ms=int((time.monotonic() - start) * 1000),
                enforced=enforced,
            )

        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=spec.timeout_s
            )
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill_tree(proc)
        except asyncio.CancelledError:
            # V2 Phase 5: cleanup after cancellation — the caller gave up,
            # but the child must never outlive the run.
            await self._kill_tree(proc)
            raise

        duration_ms = int((time.monotonic() - start) * 1000)

        if timed_out:
            disk_limits, disk_note = _post_run_workspace(enforcer)
            err = f"sandbox: process killed after timeout of {spec.timeout_s}s"
            if disk_note:
                err = f"{err}\n{disk_note}"
            return SandboxResult(
                exit_code=None,
                stdout="",
                stderr=err,
                duration_ms=duration_ms,
                killed_reason="timeout",
                limits_hit=["timeout", *disk_limits],
                enforced=enforced,
                error=err,
            )

        out, out_trunc, _ = self._decode_with_reason(
            stdout_b or b"", spec.output_limit_bytes)
        err, err_trunc, _ = self._decode_with_reason(
            stderr_b or b"", spec.output_limit_bytes)
        limits_hit: list[str] = []
        if out_trunc:
            limits_hit.extend(("output_limit", "output_limit:stdout"))
        if err_trunc:
            limits_hit.extend(("output_limit", "output_limit:stderr"))

        # Honest per-run CPU accounting: the child's ACTUAL cpu time where
        # the platform exposes it (POSIX RUSAGE_CHILDREN delta), the wall
        # duration otherwise — the basis used is the one reported.
        cpu_used = duration_ms / 1000.0
        cpu_basis = "wall_clock"
        if self._posix and self._has_resource:
            try:
                import resource

                ru = resource.getrusage(resource.RUSAGE_CHILDREN)
                child_cpu = ru.ru_utime + ru.ru_stime
                previous = getattr(self, "_last_children_cpu", 0.0)
                delta = child_cpu - previous
                self._last_children_cpu = child_cpu
                if delta > 0:
                    cpu_used = delta
                    cpu_basis = "cpu_time"
            except Exception:  # pragma: no cover - accounting is best-effort
                pass

        # Quota termination semantics (Phase 5): ordinary non-zero exits
        # are NOT quota terminations. Only kernel-delivered resource
        # signals map onto the quota vocabulary; anything else stays an
        # honest supervisor-class failure.
        killed_reason, quota_suffix = process_quota_reason(
            proc.returncode, enforced, err)
        if killed_reason is not None:
            limits_hit.append(killed_reason)
            if quota_suffix:
                err = (err + "\n" + quota_suffix).strip()

        limits_hit.append(f"cpu_used:{cpu_used:.3f}")

        # V2 Phase 5 — budget accounting after the run: actual measured CPU
        # (or the basis actually used) is consumed honestly.
        if budget is not None:
            budget.record_usage(cpu_seconds=cpu_used, cpu_basis=cpu_basis,
                                runs=1)

        # V2 closeout — workspace measurement after the child exited; a
        # violation is reported, never used to rewrite the exit code.
        disk_limits, disk_note = _post_run_workspace(enforcer)
        limits_hit.extend(disk_limits)
        if disk_note:
            err = (err + "\n" + disk_note).strip()

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=out,
            stderr=err,
            duration_ms=duration_ms,
            killed_reason=killed_reason,
            limits_hit=limits_hit,
            enforced=enforced,
            error=None,
        )

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the child (and its own group on POSIX) and reap it."""
        try:
            if self._posix:
                try:
                    pgid = os.getpgid(proc.pid)
                    # SAFETY: never signal the host's own process group. The
                    # child is spawned with start_new_session, so a distinct
                    # group is the expected case; if it is somehow shared,
                    # kill only this child rather than the whole session
                    # (which would take pytest and the runner down with it).
                    if pgid == os.getpgid(0):
                        proc.kill()
                    else:
                        os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # V2 Phase 5: quota termination semantics + honest CPU accounting
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_with_reason(buf: bytes, limit: int) -> tuple[str, bool, bool]:
        """Decode bounded output; ``killed_reason`` value for an output kill.

        Returns ``(text, truncated, killed)`` — ``killed`` is True when the
        truncation actually cut content (the design's honest
        ``killed_reason="output"``-class termination is folded into the
        ``supervisor``-reported resource limit and surfaced here for the
        result mapping).
        """
        text, truncated = _truncate(buf, limit)
        return text, truncated, truncated


# Signal-based attribution lives in process_quota_reason below (single
# source of truth): SIGXCPU → cpu, SIGXFSZ → fsize, anything else →
# supervisor. SIGKILL is deliberately NOT attributed to memory — the killer
# is unknowable (external kill vs OOM reclaim) — never invent a cause.


def process_quota_reason(returncode: int | None, enforced: list[str],
                         stderr: str) -> tuple[KilledReason | None, str | None]:
    """Honest quota attribution for the PROCESS backend's child exit.

    Mirrors the container backend's ``quota_reason_from_exit``: only
    kernel-attributed signals and provable text evidence are claimed.

    - SIGXCPU → ``cpu``; SIGXFSZ → ``fsize``; any other signal →
      ``supervisor`` (the killer is unknowable, never invented).
    - CPython ignores SIGXFSZ, so an RLIMIT_FSIZE violation normally
      surfaces as a Python ``OSError: EFBIG`` / "File too large" with a
      POSITIVE exit code rather than signal death. That text is evidence
      for ``fsize`` — but only when the fsize limit was enforced this run.
    - Ordinary exits → ``None``: never a quota claim.
    """
    if returncode is not None and returncode < 0:
        signum = -returncode
        if _SIGXCPU is not None and signum == _SIGXCPU:
            return "cpu", "[sandbox: child killed by CPU limit (SIGXCPU)]"
        if _SIGXFSZ is not None and signum == _SIGXFSZ:
            return ("fsize",
                    "[sandbox: child killed by file-size limit (SIGXFSZ)]")
        return "supervisor", f"[sandbox: child terminated by signal {signum}]"
    if (returncode not in (None, 0) and "fsize_limit" in enforced
            and ("EFBIG" in stderr or "File too large" in stderr)):
        return "fsize", "[sandbox: child blocked by the file-size limit]"
    return None, None


def build_default_sandbox() -> ExecutionSandbox:
    """Factory used by adapters — backend selected by ``SANDBOX_BACKEND``.

    Selection is policy-checked (``services.execution.sandbox_policy``):

    - ``process``: the Phase 1 :class:`ProcessSandbox` — the default in
      development/test only. In production it is refused unless the operator
      explicitly acknowledged the documented non-isolated posture.
    - ``container``: the Phase 4 ``ContainerSandbox``. It fails honestly at
      ``run()`` when no container runtime is reachable — the process backend
      is NEVER silently substituted.
    - unset in production, or anything else: ``SandboxConfigError`` (a
      ``ValueError``) — a configuration error, not a license to pick another
      backend.
    """
    backend = resolve_backend_name()
    # V2 closeout: workspace write quotas are on by default (generous
    # defaults; SANDBOX_WORKSPACE_* tunes them, 0/off disables explicitly).
    workspace_quota = WorkspaceQuota.from_env()
    if backend == "process":
        notice = process_posture_notice()
        if notice:
            _log.warning(notice)
        return ProcessSandbox(workspace_quota=workspace_quota)
    from services.execution.container_sandbox import ContainerSandbox

    return ContainerSandbox(workspace_quota=workspace_quota)
