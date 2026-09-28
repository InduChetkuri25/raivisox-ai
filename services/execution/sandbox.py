"""ExecutionSandbox — the single doorway for untrusted process execution.

V2 Phase 1 of the design in docs/sandbox-design.md: the interface plus a
platform-aware process backend. This module and its Phase 4 sibling
(services/execution/container_sandbox.py) are the ONLY places in the
codebase allowed to spawn an untrusted child process; every caller must go
through ``ExecutionSandbox.run`` with an explicit, argv-only ``SandboxSpec``.
``build_default_sandbox`` selects the backend via ``SANDBOX_BACKEND``
(process default; container opt-in; invalid values raise — never a silent
fallback to another backend).

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
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from services.security.path_containment import contained_path, realpath_norm

__all__ = [
    "KilledReason",
    "SandboxSpec",
    "SandboxResult",
    "ExecutionSandbox",
    "ProcessSandbox",
]

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

    What this backend enforces on POSIX (via preexec rlimits):
      - CPU seconds (RLIMIT_CPU), address space (RLIMIT_AS), file size
        (RLIMIT_FSIZE)

    What NO host gave Phase 1 a clean portable mechanism for is reported
    honestly as not enforced: filesystem confinement beyond cwd semantics,
    network deny, and process-count caps. On Windows, the POSIX rlimits are
    likewise reported unavailable rather than pretended. The caller sees the
    truth in ``capabilities()`` / ``SandboxResult.enforced`` and must decide
    policy (the integrated adapter fails the step when a *required*
    capability is missing — never silently runs unsandboxed).
    """

    def __init__(self) -> None:
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

    def _preexec(self, spec: SandboxSpec) -> None:  # pragma: no cover (runs in child)
        """Child-side rlimits (POSIX only). Runs between fork and exec."""
        import resource

        if spec.cpu_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (spec.cpu_seconds, spec.cpu_seconds))
        if spec.memory_bytes is not None:
            resource.setrlimit(
                resource.RLIMIT_AS, (spec.memory_bytes, spec.memory_bytes)
            )
        if spec.fsize_bytes is not None:
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (spec.fsize_bytes, spec.fsize_bytes)
            )

    async def run(self, spec: SandboxSpec) -> SandboxResult:
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

        env = self._build_env(spec)
        enforced = ["argv_execution", "minimal_env", "working_dir", "bounded_output"]
        caps = self.capabilities()
        if spec.cpu_seconds is not None and caps["cpu_limit"]:
            enforced.append("cpu_limit")
        if spec.memory_bytes is not None and caps["memory_limit"]:
            enforced.append("memory_limit")
        if spec.fsize_bytes is not None and caps["fsize_limit"]:
            enforced.append("fsize_limit")

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=env,
                preexec_fn=self._preexec if (self._posix and self._has_resource) else None,
            )
        except Exception as e:  # spawn failure — honest failure, no fallback
            return SandboxResult(
                error=f"spawn failed: {type(e).__name__}",
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

        duration_ms = int((time.monotonic() - start) * 1000)

        if timed_out:
            err = f"sandbox: process killed after timeout of {spec.timeout_s}s"
            return SandboxResult(
                exit_code=None,
                stdout="",
                stderr=err,
                duration_ms=duration_ms,
                killed_reason="timeout",
                limits_hit=["timeout"],
                enforced=enforced,
                error=err,
            )

        out, out_trunc = _truncate(stdout_b or b"", spec.output_limit_bytes)
        err, err_trunc = _truncate(stderr_b or b"", spec.output_limit_bytes)
        limits_hit: list[str] = []
        if out_trunc:
            limits_hit.append("output_limit:stdout")
        if err_trunc:
            limits_hit.append("output_limit:stderr")

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=out,
            stderr=err,
            duration_ms=duration_ms,
            killed_reason=None,
            limits_hit=limits_hit,
            enforced=enforced,
        )

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the child (and its group on POSIX) and reap it."""
        try:
            if self._posix:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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


def build_default_sandbox() -> ExecutionSandbox:
    """Factory used by adapters — backend selected by ``SANDBOX_BACKEND``.

    - ``process`` (default): the Phase 1 :class:`ProcessSandbox`.
    - ``container``: the Phase 4 ``ContainerSandbox`` (opt-in). It fails
      honestly at ``run()`` when no container runtime is reachable — the
      process backend is NEVER silently substituted.
    - anything else: ``ValueError`` — an invalid backend is a configuration
      error, not a license to pick another backend.
    """
    backend = os.environ.get("SANDBOX_BACKEND", "process").strip().lower()
    if backend == "process":
        return ProcessSandbox()
    if backend == "container":
        from services.execution.container_sandbox import ContainerSandbox

        return ContainerSandbox()
    raise ValueError(
        f"invalid SANDBOX_BACKEND {backend!r}: expected 'process' or 'container'"
    )
