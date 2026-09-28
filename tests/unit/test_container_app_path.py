"""V2 CI container gate — REAL RAIVISOX paths through ContainerSandbox.

No mocks and no fake sandbox objects: when a container runtime AND the
configured image are present, these tests drive the real application path
end to end with ``SANDBOX_BACKEND=container``:

    registry.run (kernel authorize_and_log + audit)
      → ContainerSandbox.run(SandboxSpec)
      → throwaway container
      → SandboxResult
      → adapter result / smoke dict / DoD result
      → output bytes in the task workspace (artifact evidence)
      → honest success/failure fields

Every test is gated on the same runtime+image probe as the ``test_live_*``
suite and SKIPs (never passes) when there is no runtime. Their purpose is
the CI container gate: proving the real RAIVISOX execution path — not just
the backend in isolation — inside actual containers. What is deliberately
NOT exercised here is the main.py HTTP pipeline (artifact registry rows and
task outcomes at the API layer); the adapter/result/outcome semantics these
tests assert are the same objects that pipeline consumes unchanged.
"""

import os
import subprocess

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.builtin import PythonExecAdapter, ShellExecAdapter
from integrations.adapters.codegen import CodegenAdapter
from integrations.adapters.registry import AdapterRegistry
from services.dod.dod_checks import run_module_checks_async
from services.execution.container_sandbox import ContainerSandbox, probe_runtime
from services.execution.sandbox import build_default_sandbox
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel

_IMAGE = os.environ.get("SANDBOX_CONTAINER_IMAGE", "").strip() or "python:3.12-slim"


def _live_state() -> tuple[bool, str]:
    exe, _ = probe_runtime(force=True)
    if not exe:
        return False, "no container runtime (docker/podman) on this host"
    try:
        p = subprocess.run(  # noqa: S603 - fixed argv, trusted binary
            [exe, "image", "inspect", _IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False, "container runtime image inspect failed"
    if p.returncode != 0:
        return False, f"image {_IMAGE} not present locally (tests never pull)"
    return True, ""


_LIVE, _LIVE_REASON = _live_state()


def live(fn=None):
    """Mark a test as live-container verification and gate it honestly.

    Adds the ``live`` marker (so ``pytest -m live`` selects the set in the
    CI container gate) and skips with an explicit reason when no runtime
    AND image exist on the host. Skipped tests are never passes.
    """
    marks = [pytest.mark.live]
    if not _LIVE:
        marks.append(pytest.mark.skip(
            reason=f"live container test skipped: {_LIVE_REASON}"))

    def _deco(f):
        for m in reversed(marks):
            f = m(f)
        return f

    return _deco(fn) if fn is not None else _deco


def _ctx(task: str) -> AdapterContext:
    return AdapterContext(actor_id="tester", task_id=task, step_id="s1",
                          resource=f"task:{task}")


@pytest.fixture
def container_backend(monkeypatch):
    """Select the container backend the same way production config does."""
    monkeypatch.setenv("SANDBOX_BACKEND", "container")
    sb = build_default_sandbox()
    assert isinstance(sb, ContainerSandbox)  # selection never silently swaps
    return sb


def _gated_registry(adapter) -> AdapterRegistry:
    """Real SecurityKernel + registry: authorization precedes the sandbox."""
    kernel = SecurityKernel()
    kernel.grant_permission("tester", Permission.EXECUTE_CODE)
    kernel.grant_permission("tester", Permission.EXECUTE_TERMINAL)
    registry = AdapterRegistry(kernel)
    registry.register(adapter)
    return registry


# ---------------------------------------------------------------------------
# python_exec — kernel authorization → container → SandboxResult → output
# ---------------------------------------------------------------------------

@live
async def test_python_exec_real_container_path(tmp_path, container_backend):
    ws = tmp_path / "ws"
    ws.mkdir()
    adapter = PythonExecAdapter(str(ws), sandbox=container_backend)
    registry = _gated_registry(adapter)
    AuditTrail.instance().clear()
    r = await registry.run(
        "python_exec",
        {"code": "open('artifact.txt', 'w').write('from-container')\nprint(6*7)"},
        _ctx("task-c1"))
    assert r.success, r.error
    assert r.output["exit_code"] == 0
    assert "42" in r.output["stdout"]
    # Output bytes landed in THIS task's scope inside the mounted workspace.
    assert (ws / "task-c1" / "artifact.txt").read_text() == "from-container"
    # The kernel decision for this real run was audited (allowed=True).
    events = [e for e in AuditTrail.instance().get_events(actor_id="tester")
              if e.action == "tool:python_exec"]
    assert events and events[-1].allowed is True
    # Honest enforcement reporting from the container boundary itself.
    for key in ("argv_execution", "minimal_env", "fs_confinement",
                "network_deny", "timeout_kill", "bounded_output"):
        assert key in r.output["sandbox_enforced"], r.output["sandbox_enforced"]


# ---------------------------------------------------------------------------
# shell_exec — legacy contract through the container backend
# ---------------------------------------------------------------------------

@live
async def test_shell_exec_real_container_path(tmp_path, container_backend):
    ws = tmp_path / "ws"
    ws.mkdir()
    adapter = ShellExecAdapter(str(ws), exec_sandbox=container_backend)
    registry = _gated_registry(adapter)
    r = await registry.run("shell_exec", {"command": "echo sandbox-alive"},
                           _ctx("task-c2"))
    assert r.success, r.error
    assert "sandbox-alive" in r.output["stdout"]
    assert r.output["exit_code"] == 0


# ---------------------------------------------------------------------------
# codegen smoke + DoD probe — generate/verify loop inside containers
# ---------------------------------------------------------------------------

@live
async def test_codegen_and_dod_real_container_path(tmp_path, container_backend):
    ws = tmp_path / "ws"
    ws.mkdir()
    adapter = CodegenAdapter(str(ws), sandbox=container_backend)
    r = await adapter.run(
        {"goal": "factorial of 6", "code": "def solve():\n    return 720\n"},
        _ctx("task-c3"))
    assert r.success, r.error
    sm = r.output["smoke_test"]
    assert sm["ran"] is True and sm["exit_code"] == 0
    assert sm["solve_present"] is True and "720" in sm["stdout"]
    for key in ("argv_execution", "minimal_env", "fs_confinement"):
        assert key in sm["sandbox_enforced"], sm["sandbox_enforced"]

    # DoD verification of a module inside the same container workspace.
    module = ws / "task-c3" / "factorial_of_6.py"
    module.write_text("def solve():\n    return 720\n")
    dod = await run_module_checks_async(
        "factorial of 6 and verify that the result is 720.", str(module),
        sandbox=container_backend, workspace_root=str(ws))
    assert dod.executed, dod.execution_error
    assert dod.execution_output == 720
    assert dod.satisfied


# ---------------------------------------------------------------------------
# The kernel still gates BEFORE any container exists
# ---------------------------------------------------------------------------

@live
async def test_kernel_denial_prevents_container_execution(tmp_path,
                                                          container_backend):
    ws = tmp_path / "ws"
    ws.mkdir()
    adapter = PythonExecAdapter(str(ws), sandbox=container_backend)
    registry = AdapterRegistry(SecurityKernel())  # no grants for anyone
    registry.register(adapter)
    r = await registry.run("python_exec", {"code": "print('never')"},
                           _ctx("task-c4"))
    assert not r.success
    assert r.permission_denied
    # The sandbox was never reached: no scope, no output.
    assert not (ws / "task-c4").exists()
