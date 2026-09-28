"""ContainerSandbox — V2 Phase 4: the container-backed ExecutionSandbox.

docs/sandbox-design.md §7 phase 5 ("Container backend (opt-in,
config-gated)"): the same ``ExecutionSandbox`` interface, with confinement
delegated to a container runtime (Docker or Podman). Selected with
``SANDBOX_BACKEND=container``; the Phase 1 ``ProcessSandbox`` remains the
default and is NEVER substituted when the container runtime is missing —
``run()`` fails honestly instead (an unconfined run is worse than no run).

Confinement model (one throwaway container per run):

- argv-only execution: the SandboxSpec argv is appended to ``docker/podman
  run`` as separate CLI elements. No shell string exists anywhere in this
  module; metacharacters are inert data inside a single argv element.
- filesystem: the ONLY host path mounted is the spec's writable workspace
  root, read-write at the fixed in-container path ``/raivisox/workspace``;
  the container rootfs is ``--read-only`` with a small ``noexec`` tmpfs at
  /tmp. Host root, sibling tasks, app data, and the container runtime
  socket are never mounted. A symlink inside the workspace targeting a host
  path resolves inside the container namespace, where that path does not
  exist — escapes cannot cross the mount boundary.
- environment: built from scratch (HOME/TMPDIR defaults plus allowlisted
  ``spec.env`` keys only). ``os.environ`` is never consulted — the API
  process environment cannot leak; the image's own PATH is the image's,
  not the host's.
- network: ``--network none`` for the default ``network="deny"`` policy;
  ``network="allow"`` gets the runtime's default bridge. ``--network host``
  is never passed.
- privileges: ``--cap-drop ALL``, ``--security-opt no-new-privileges``,
  ``--read-only``, and a non-root ``--user`` (host uid/gid on POSIX,
  unprivileged fallback elsewhere, ``SANDBOX_CONTAINER_USER`` override).
- limits: ``--memory``, ``--pids-limit``, ``--ulimit cpu=|fsize=`` map the
  SandboxSpec fields onto runtime flags when the caller sets them.
- supervision: each container gets a unique ``raivisox-sbx-*`` name; on
  timeout it is force-removed (killing the CLI client alone would NOT stop
  it), the client is killed and reaped, output is bounded while draining
  (never accumulated unboundedly), and a best-effort removal runs in
  ``finally`` whenever the client did not exit normally — no container
  outlives a run.

Honesty rules (same as ProcessSandbox):

- ``capabilities()`` reports True only when a runtime was actually probed
  AND the guarantee maps to a real flag boundary; with no runtime every
  key is False, including ``runtime_available``.
- ``SandboxResult.enforced`` records the flags actually passed to THIS run.
- Runtime unavailable → ``execution environment unavailable: ...`` with
  ``killed_reason="supervisor"`` and ``exit_code=None``. There is no code
  path from this backend to a host-process execution.

Compatibility note: ``argv[0]`` is interpreted INSIDE the image. The host
interpreter path (``sys.executable``) is mapped to
``SANDBOX_CONTAINER_PYTHON`` (default ``/usr/local/bin/python3``) so the
python_exec/codegen/DoD probes run; any other host-resolved executable
path runs only if the image provides the same path (typical for FHS paths
on Linux) and otherwise fails honestly with the runtime's exit 126/127 —
never by falling back to the host.

Verification status: the unavailable/fail-closed path and every command
construction rule are unit-tested on this host (no runtime present); the
``test_live_*`` tests run only where a runtime AND the image exist and are
skipped otherwise — a live container result is never faked.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import uuid

from services.execution.sandbox import (
    ExecutionSandbox,
    SandboxResult,
    SandboxSpec,
    _ALLOWED_ENV_KEYS,
)
from services.security.path_containment import contained_path

__all__ = [
    "CONTAINER_WORKSPACE",
    "DEFAULT_IMAGE",
    "ContainerSandbox",
    "probe_runtime",
    "build_container_env",
    "map_container_cwd",
    "build_run_argv",
    "read_bounded",
    "decode_bounded",
]

# Fixed in-container mount point for the single host bind (the writable
# workspace root). Never derived from host paths.
CONTAINER_WORKSPACE = "/raivisox/workspace"
DEFAULT_IMAGE = "python:3.12-slim"
_DEFAULT_CONTAINER_PYTHON = "/usr/local/bin/python3"
_PROBE_RETRY_S = 30.0  # failed probes retry after this; successes are cached

_CAP_KEYS = (
    "argv_execution",
    "minimal_env",
    "working_dir",
    "bounded_output",
    "timeout_kill",
    "cpu_limit",
    "memory_limit",
    "fsize_limit",
    "pids_limit",
    "network_deny",
    "fs_confinement",
    "non_root",
    "cap_drop_all",
    "no_new_privileges",
    "runtime_available",
)

# Probe cache: (monotonic ts, executable-or-None, reason-if-None)
_probe_cache: tuple[float, str | None, str] | None = None


# ---------------------------------------------------------------------------
# Runtime availability probe (trusted infrastructure — the runtime client)
# ---------------------------------------------------------------------------

def _probe_uncached() -> str | None:
    """Return the runtime client path if ``<runtime> info`` succeeds."""
    for name in ("docker", "podman"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
                [exe, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return exe
    return None


def probe_runtime(force: bool = False) -> tuple[str | None, str]:
    """``(client-path, "")`` when a runtime is reachable, else ``(None, reason)``.

    A successful probe is cached for the process (the daemon rarely
    disappears mid-run); a failed probe is retried after ``_PROBE_RETRY_S``
    so a runtime that comes up later becomes usable without a restart.
    Synchronous — callers inside the event loop use ``asyncio.to_thread``.
    """
    global _probe_cache
    now = time.monotonic()
    if _probe_cache is not None and not force:
        ts, exe, reason = _probe_cache
        if exe is not None or (now - ts) < _PROBE_RETRY_S:
            return exe, reason
    exe = _probe_uncached()
    reason = "" if exe else "no reachable container runtime (docker/podman)"
    _probe_cache = (now, exe, reason)
    return exe, reason


# ---------------------------------------------------------------------------
# Pure construction helpers (unit-testable without a runtime)
# ---------------------------------------------------------------------------

def build_container_env(spec: SandboxSpec) -> dict[str, str]:
    """Explicit container environment — built from scratch.

    Only HOME/TMPDIR defaults plus allowlisted ``spec.env`` keys.
    ``os.environ`` is NEVER read: API-process secrets cannot reach the
    container even transitively.
    """
    env: dict[str, str] = {"HOME": "/tmp", "TMPDIR": "/tmp"}
    for key, val in spec.env.items():
        if key in _ALLOWED_ENV_KEYS and isinstance(val, str):
            env[key] = val
    return env


def map_container_cwd(spec: SandboxSpec) -> tuple[str | None, SandboxResult | None]:
    """``(container cwd, None)`` when the spec maps cleanly, else ``(None, error)``.

    Performs the same Phase 0 containment validation as ``ProcessSandbox``
    (identical error wording, so callers see one boundary) and derives the
    in-container cwd from the host cwd's path relative to the single
    mounted workspace root. A relative result that would climb above the
    mount (``..``) is rejected outright — the mapping never leaves the
    mounted subtree.
    """
    writable = spec.workspace_writable or spec.cwd
    if not contained_path(spec.cwd, writable):
        return None, SandboxResult(
            error="sandbox rejected spec: cwd outside writable workspace",
            killed_reason="supervisor",
            enforced=["spec_validation"],
        )
    for r in (spec.workspace_readable or (spec.cwd,)):
        if not contained_path(r, writable):
            return None, SandboxResult(
                error="sandbox rejected spec: readable scope outside writable workspace",
                killed_reason="supervisor",
                enforced=["spec_validation"],
            )
    writable_abs = os.path.abspath(writable)
    try:
        rel = os.path.relpath(os.path.abspath(spec.cwd), writable_abs)
    except ValueError:  # different drives on Windows — cannot map
        rel = ".."
    if rel == os.curdir:
        return CONTAINER_WORKSPACE, None
    if rel.startswith("..") or os.path.isabs(rel):
        # Real containment already passed above; this only fires for
        # degenerate inputs. Reject — never climb out of the mount.
        return None, SandboxResult(
            error="sandbox rejected spec: cwd outside writable workspace",
            killed_reason="supervisor",
            enforced=["spec_validation"],
        )
    return f"{CONTAINER_WORKSPACE}/{rel.replace(os.sep, '/')}", None


def build_run_argv(
    spec: SandboxSpec,
    *,
    runtime_exe: str,
    image: str,
    user: str,
    python_bin: str,
    container_cwd: str,
    name: str,
) -> list[str]:
    """The exact ``docker/podman run`` command — pure and unit-testable.

    Security-relevant properties (asserted by tests): exactly one mount —
    the writable workspace, read-write, at the fixed path; never the
    runtime socket; never ``--privileged``; never ``--network host``;
    cap-drop ALL + no-new-privileges + read-only rootfs + non-root user;
    environment entries only from :func:`build_container_env`; and the spec
    argv appended as separate elements, never joined into a shell string.
    """
    env = build_container_env(spec)
    writable = os.path.abspath(spec.workspace_writable or spec.cwd)
    cmd: list[str] = [
        runtime_exe, "run",
        "--rm",
        "--name", name,
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=67108864",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", user,
        "-w", container_cwd,
    ]
    if spec.network == "deny":
        cmd += ["--network", "none"]
    if spec.memory_bytes is not None:
        cmd += ["--memory", str(spec.memory_bytes),
                "--memory-swap", str(spec.memory_bytes)]
    if spec.max_pids is not None:
        cmd += ["--pids-limit", str(spec.max_pids)]
    if spec.cpu_seconds is not None:
        cmd += ["--ulimit", f"cpu={spec.cpu_seconds}:{spec.cpu_seconds}"]
    if spec.fsize_bytes is not None:
        cmd += ["--ulimit", f"fsize={spec.fsize_bytes}:{spec.fsize_bytes}"]
    cmd += ["-v", f"{writable}:{CONTAINER_WORKSPACE}:rw"]
    for key in sorted(env):
        cmd += ["-e", f"{key}={env[key]}"]
    cmd.append(image)
    argv = list(spec.argv)
    # The host interpreter path does not exist inside the image; map it to
    # the in-container interpreter. Deterministic, exact-match only —
    # any other argv[0] is passed through and fails honestly (126/127)
    # when the image does not provide that path.
    if python_bin and argv[0] == sys.executable:
        argv[0] = python_bin
    cmd += argv
    return cmd


def _default_user() -> str:
    """Non-root ``uid:gid`` for ``--user`` — never root, even under a root host."""
    override = os.environ.get("SANDBOX_CONTAINER_USER", "").strip()
    if override:
        return override
    if os.name == "posix" and hasattr(os, "getuid"):
        uid, gid = os.getuid(), os.getgid()
        if uid != 0:
            return f"{uid}:{gid}"
        return "65534:65534"  # host root → still unprivileged inside
    return "65534:65534"


def _is_non_root(user: str) -> bool:
    """Honest non-root verdict: numeric non-zero → True; unverifiable → False."""
    try:
        return int(user.split(":", 1)[0]) != 0
    except ValueError:
        return False  # named user (e.g. "root") — cannot verify without the image


async def read_bounded(stream: asyncio.StreamReader | None,
                       limit: int) -> tuple[bytes, bool]:
    """Drain a stream, storing at most ``limit + 1`` bytes. Returns (buf, overflow).

    The drain CONTINUES after the cap so a fast writer can never block on a
    full pipe or balloon API-process memory; only the stored prefix matters
    (the process-backend contract: bound after read).
    """
    if stream is None:
        return b"", False
    buf = bytearray()
    overflow = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if overflow:
            continue  # keep draining, store nothing
        room = limit + 1 - len(buf)
        if room <= 0:
            overflow = True
            continue
        buf += chunk[:room]
        if len(chunk) > room:
            overflow = True
    return bytes(buf), overflow


def decode_bounded(buf: bytes, overflow: bool, limit: int) -> tuple[str, bool]:
    """Decode a bounded buffer; append the standard truncation marker when cut."""
    s = buf.decode(errors="replace")
    if overflow or len(s) > limit:
        if len(s) > limit:
            s = s[:limit]
        return s + f"\n...[output truncated at {limit} chars]", True
    return s, False


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

class ContainerSandbox:
    """Container backend (Phase 4): fail-closed, honest, no host substitution.

    See the module docstring for the confinement model. Selection is
    config-level (``SANDBOX_BACKEND=container`` in
    ``build_default_sandbox``); availability is decided per run via a
    cached probe and NEVER by silently selecting another backend.
    """

    def __init__(self, image: str | None = None, python_bin: str | None = None,
                 user: str | None = None) -> None:
        self._image = (image or os.environ.get("SANDBOX_CONTAINER_IMAGE",
                                               "").strip() or DEFAULT_IMAGE)
        self._python_bin = (python_bin if python_bin is not None
                            else (os.environ.get("SANDBOX_CONTAINER_PYTHON",
                                                 "").strip()
                                  or _DEFAULT_CONTAINER_PYTHON))
        self._user = user or _default_user()

    # -- capability reporting ------------------------------------------------

    def capabilities(self) -> dict[str, bool]:
        """Per-host honest report. No runtime → every key False."""
        exe, _ = probe_runtime()
        if exe is None:
            return {k: False for k in _CAP_KEYS}
        return {
            "argv_execution": True,
            "minimal_env": True,
            "working_dir": True,
            "bounded_output": True,
            "timeout_kill": True,
            # Applied only when the spec requests them (see build_run_argv):
            "cpu_limit": True,      # --ulimit cpu=
            "memory_limit": True,   # --memory / --memory-swap
            "fsize_limit": True,    # --ulimit fsize=
            "pids_limit": True,     # --pids-limit
            # Always-on flag boundaries of every run:
            "network_deny": True,   # --network none for network="deny"
            "fs_confinement": True,  # single bind mount + read-only rootfs
            "non_root": _is_non_root(self._user),
            "cap_drop_all": True,
            "no_new_privileges": True,
            "runtime_available": True,
        }

    def _enforced_for(self, spec: SandboxSpec) -> list[str]:
        """What THIS run will actually pass to the runtime."""
        enforced = [
            "argv_execution", "minimal_env", "working_dir", "bounded_output",
            "timeout_kill", "fs_confinement", "cap_drop_all",
            "no_new_privileges",
        ]
        if spec.network == "deny":
            enforced.append("network_deny")
        if _is_non_root(self._user):
            enforced.append("non_root")
        if spec.cpu_seconds is not None:
            enforced.append("cpu_limit")
        if spec.memory_bytes is not None:
            enforced.append("memory_limit")
        if spec.fsize_bytes is not None:
            enforced.append("fsize_limit")
        if spec.max_pids is not None:
            enforced.append("pids_limit")
        return enforced

    # -- execution -----------------------------------------------------------

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        # 1) Spec scope validation — identical boundary and wording to
        #    ProcessSandbox, before any runtime interaction.
        container_cwd, err = map_container_cwd(spec)
        if err is not None:
            return err

        # 2) Runtime availability — fail-closed. No ProcessSandbox
        #    substitution exists anywhere in this path.
        exe, reason = await asyncio.to_thread(probe_runtime)
        if exe is None:
            return SandboxResult(
                error=f"execution environment unavailable: {reason}",
                killed_reason="supervisor",
                enforced=[],
            )

        name = f"raivisox-sbx-{uuid.uuid4().hex[:12]}"
        cmd = build_run_argv(
            spec, runtime_exe=exe, image=self._image, user=self._user,
            python_bin=self._python_bin, container_cwd=container_cwd, name=name,
        )
        enforced = self._enforced_for(spec)

        start = time.monotonic()
        proc: asyncio.subprocess.Process | None = None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,  # argv-only: never a shell string
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as e:  # spawn failure — honest failure
                return SandboxResult(
                    error=f"spawn failed: {type(e).__name__}",
                    killed_reason="supervisor",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    enforced=enforced,
                )
            deadline = time.monotonic() + spec.timeout_s
            try:
                out_b, err_b, out_cut, err_cut, rc = await self._await_bounded(
                    proc, spec, deadline)
            except TimeoutError:
                # The container must be force-removed: killing the CLI
                # client alone would leave it running.
                await self._force_remove(exe, name)
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except (TimeoutError, ProcessLookupError):
                        pass
                duration_ms = int((time.monotonic() - start) * 1000)
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

            duration_ms = int((time.monotonic() - start) * 1000)
            if rc == 125:
                # docker/podman itself failed (daemon gone, image pull
                # refused, bad flag): the run never produced a container
                # execution. Honest unavailable-class failure.
                out, _ = decode_bounded(out_b, out_cut, spec.output_limit_bytes)
                er, _ = decode_bounded(err_b, err_cut, spec.output_limit_bytes)
                return SandboxResult(
                    exit_code=None,
                    stdout=out,
                    stderr=er,
                    duration_ms=duration_ms,
                    killed_reason="supervisor",
                    enforced=enforced,
                    error="execution environment unavailable: "
                          "container runtime failed to start the run",
                )
            out, out_trunc = decode_bounded(out_b, out_cut, spec.output_limit_bytes)
            er, err_trunc = decode_bounded(err_b, err_cut, spec.output_limit_bytes)
            limits_hit: list[str] = []
            if out_trunc:
                limits_hit.append("output_limit:stdout")
            if err_trunc:
                limits_hit.append("output_limit:stderr")
            return SandboxResult(
                exit_code=rc,
                stdout=out,
                stderr=er,
                duration_ms=duration_ms,
                killed_reason=None,
                limits_hit=limits_hit,
                enforced=enforced,
            )
        except Exception as e:  # unexpected — honest failure, still cleaned up
            return SandboxResult(
                error=f"container run failed: {type(e).__name__}",
                killed_reason="supervisor",
                duration_ms=int((time.monotonic() - start) * 1000),
                enforced=enforced,
            )
        finally:
            # The client exiting normally means --rm already reclaimed the
            # container (or it never started). Anything else — timeout kill,
            # exception, caller cancellation — gets a best-effort removal so
            # no container outlives the run.
            if proc is not None and proc.returncode is None:
                await self._force_remove(exe, name)

    async def _await_bounded(
        self,
        proc: asyncio.subprocess.Process,
        spec: SandboxSpec,
        deadline: float,
    ) -> tuple[bytes, bytes, bool, bool, int]:
        """Bounded drain of both streams + exit code, within the deadline.

        Raises ``TimeoutError`` on deadline; reader/waiter tasks are
        cancelled so nothing accumulates after the kill.
        """
        limit = spec.output_limit_bytes
        out_task = asyncio.create_task(read_bounded(proc.stdout, limit))
        err_task = asyncio.create_task(read_bounded(proc.stderr, limit))
        wait_task = asyncio.create_task(proc.wait())
        remaining = max(0.0, deadline - time.monotonic())
        _done, pending = await asyncio.wait(
            {out_task, err_task, wait_task},
            timeout=remaining,
            return_when=asyncio.ALL_COMPLETED,
        )
        if pending:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise TimeoutError
        out_b, out_cut = out_task.result()
        err_b, err_cut = err_task.result()
        rc = wait_task.result()
        return out_b, err_b, out_cut, err_cut, rc

    async def _force_remove(self, exe: str, name: str) -> None:
        """Best-effort container teardown — never raises, bounded in time."""
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "rm", "-f", name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass
