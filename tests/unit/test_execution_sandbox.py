"""V2 Phase 1 — ExecutionSandbox interface and process backend tests.

Focused tests for the sandbox boundary introduced in
docs/sandbox-design.md §7 phase 1/2:

- argv execution (never shell-string)
- minimal explicit environment + secret isolation
- working-directory containment
- timeout with kill
- bounded output
- failure semantics (spawn failure → honest failed result, no fallback)
- capability reporting honesty
- adapter integration: PythonExecAdapter through the sandbox
- symlink containment regression (Phase 0 boundary shared by the sandbox)

Platform discipline: POSIX-rlimit-backed guarantees (cpu/memory/fsize) are
reported by ``capabilities()`` and asserted against THAT report — a test
never passes merely because a limit was not enforced on this host.
"""

import asyncio
import os
import sys

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.builtin import PythonExecAdapter
from services.execution.sandbox import (
    ExecutionSandbox,
    ProcessSandbox,
    SandboxSpec,
    SandboxResult,
    build_default_sandbox,
)
from services.security.path_containment import contained_path

SECRET_KEY_NAME = "RAIVISOX_PHASE1_SECRET"
SECRET_VALUE = "sup3r-secret-do-not-leak-42"


def _spec(cwd, code, **kw) -> SandboxSpec:
    return SandboxSpec(argv=[sys.executable, "-c", code], cwd=str(cwd), env={}, **kw)


def _ctx(task="t1") -> AdapterContext:
    return AdapterContext(actor_id="tester", task_id=task, step_id="s1",
                          resource=f"task:{task}")


@pytest.fixture(autouse=True)
def _secret_env():
    os.environ[SECRET_KEY_NAME] = SECRET_VALUE
    yield
    os.environ.pop(SECRET_KEY_NAME, None)


@pytest.fixture
def sandbox():
    return ProcessSandbox()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# 1. argv execution
# ---------------------------------------------------------------------------

async def test_argv_execution_basic(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "print(6*7)"))
    assert r.success, r.error
    assert r.exit_code == 0
    assert "42" in r.stdout
    assert r.killed_reason is None
    assert r.duration_ms >= 0


async def test_nonzero_exit_is_failure_not_error(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "import sys; sys.exit(3)"))
    assert not r.success
    assert r.exit_code == 3
    assert r.killed_reason is None  # ran to completion; not killed
    assert r.error is None


async def test_stderr_is_captured(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "import sys; print('boom', file=sys.stderr)"))
    assert "boom" in r.stderr


# ---------------------------------------------------------------------------
# 2. Minimal environment + secret isolation
# ---------------------------------------------------------------------------

async def test_child_env_is_minimal_and_allowlisted(sandbox, workspace):
    os.environ["RAIVISOX_RANDOM_UNLISTED"] = "nope"
    code = "import os, json; print(json.dumps(sorted(os.environ.keys())))"
    r = await sandbox.run(_spec(workspace, code))
    assert r.success, r.error
    keys = eval(r.stdout.strip())  # noqa: S307 (test-only, sandbox output)
    assert "RAIVISOX_RANDOM_UNLISTED" not in keys
    assert SECRET_KEY_NAME not in keys
    # Only allowlisted keys may appear at all
    assert all(k in (
        "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR",
        "TEMP", "TMP", "PATH", "PYTHONIOENCODING", "PYTHONUTF8",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA") for k in keys)


async def test_secret_isolation_value_never_leaks(sandbox, workspace):
    code = "import os; print(os.environ.get('RAIVISOX_PHASE1_SECRET', 'ABSENT'))"
    r = await sandbox.run(_spec(workspace, code))
    assert r.success, r.error
    assert r.stdout.strip() == "ABSENT"
    assert SECRET_VALUE not in r.stdout
    assert SECRET_VALUE not in r.stderr


async def test_spec_env_cannot_smuggle_host_secret(sandbox, workspace):
    # Even a malicious spec cannot inject keys outside the allowlist.
    r = await sandbox.run(SandboxSpec(
        argv=[sys.executable, "-c",
              "import os; print(os.environ.get('RAIVISOX_PHASE1_SECRET', 'ABSENT'))"],
        cwd=str(workspace),
        env={SECRET_KEY_NAME: SECRET_VALUE},  # not allowlisted → dropped
    ))
    assert r.success, r.error
    assert r.stdout.strip() == "ABSENT"


# ---------------------------------------------------------------------------
# 3. Working-directory containment
# ---------------------------------------------------------------------------

async def test_cwd_is_task_scope(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "import os; print(os.getcwd())"))
    assert r.success, r.error
    assert contained_path(r.stdout.strip(), str(workspace))


async def test_relative_writes_land_in_scope(sandbox, workspace):
    r = await sandbox.run(_spec(
        workspace,
        "open('out.txt', 'w').write('hi'); "
        "import os; print(os.path.abspath('out.txt'))"))
    assert r.success, r.error
    assert (workspace / "out.txt").read_text() == "hi"
    assert contained_path(r.stdout.strip(), str(workspace))


async def test_spec_rejected_when_cwd_outside_writable_scope(sandbox, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = tmp_path / "scope"
    scope.mkdir()
    r = await sandbox.run(_spec(scope, "print('hi')",
                                workspace_writable=str(outside)))
    assert not r.success
    assert "outside writable workspace" in r.error
    assert r.killed_reason == "supervisor"
    assert r.exit_code is None  # never spawned


# ---------------------------------------------------------------------------
# 4. Timeout + kill
# ---------------------------------------------------------------------------

async def test_timeout_kills_and_reports(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "import time; time.sleep(30)",
                                timeout_s=2))
    assert not r.success
    assert r.killed_reason == "timeout"
    assert r.exit_code is None
    assert "timeout" in r.error


async def test_timeout_duration_honored(sandbox, workspace):
    import time as _t
    start = _t.monotonic()
    await sandbox.run(_spec(workspace, "import time; time.sleep(30)",
                            timeout_s=1))
    elapsed = _t.monotonic() - start
    assert elapsed < 15  # killed near 1s, not after 30s of sleep


# ---------------------------------------------------------------------------
# 5. Bounded output
# ---------------------------------------------------------------------------

async def test_output_is_bounded(sandbox, workspace):
    r = await sandbox.run(_spec(
        workspace, "print('x' * 5_000_00)",  # ~500KB < 1MB default
        output_limit_bytes=100))
    assert r.success, r.error
    assert len(r.stdout) <= 200
    assert "output_limit:stdout" in r.limits_hit


async def test_default_output_bound(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "print('y' * 3_000_000)"))
    assert len(r.stdout) < 2_000_000  # default 1MB bound held


# ---------------------------------------------------------------------------
# 6. Failure semantics — spawn failure, NO unsandboxed fallback
# ---------------------------------------------------------------------------

class BrokenSandbox(ExecutionSandbox):
    """Sandbox that can never start a process (e.g. backend unavailable)."""

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        return SandboxResult(
            error="execution environment unavailable: sandbox backend failed to start",
            killed_reason="supervisor",
            enforced=[],
        )

    def capabilities(self) -> dict[str, bool]:
        return {"argv_execution": False}


async def test_sandbox_unavailable_fails_honestly(sandbox, workspace):
    broken = BrokenSandbox()
    adapter = PythonExecAdapter(str(workspace), sandbox=broken)
    r = await adapter.run({"code": "print(1)"}, _ctx())
    assert not r.success
    assert "unavailable" in r.error


async def test_no_unsandboxed_fallback_code_never_ran(sandbox, workspace):
    """If the sandbox fails, the code MUST NOT have executed anywhere."""
    canary = workspace / "canary.txt"
    broken = BrokenSandbox()
    adapter = PythonExecAdapter(str(workspace), sandbox=broken)
    r = await adapter.run({"code": f"open({str(canary)!r}, 'w').write('escaped')"}, _ctx())
    assert not r.success
    assert not canary.exists()


async def test_spawn_failure_returns_error_result(sandbox, workspace):
    # argv[0] that cannot exist → spawn raises → honest error result
    r = await sandbox.run(SandboxSpec(
        argv=["definitely-not-a-real-binary-xyz"],
        cwd=str(workspace), env={}))
    assert not r.success
    assert r.error is not None
    assert "spawn failed" in r.error


# ---------------------------------------------------------------------------
# 7. Capability reporting — honest, per-host
# ---------------------------------------------------------------------------

def test_capabilities_report_shape(sandbox):
    caps = sandbox.capabilities()
    # Always-true core guarantees
    for key in ("argv_execution", "minimal_env", "working_dir",
                "bounded_output", "timeout_kill"):
        assert caps[key] is True
    # Phase 1 honest gaps
    assert caps["pids_limit"] is False
    assert caps["network_deny"] is False
    assert caps["fs_confinement"] is False


def test_posix_rlimit_caps_match_host(sandbox):
    caps = sandbox.capabilities()
    on_posix = os.name == "posix"
    assert caps["cpu_limit"] is on_posix
    assert caps["memory_limit"] is on_posix
    assert caps["fsize_limit"] is on_posix


async def test_enforced_records_only_real_enforcement(sandbox, workspace):
    r = await sandbox.run(_spec(workspace, "print(1)", memory_bytes=512 * 1024 * 1024))
    if sandbox.capabilities()["memory_limit"]:
        assert "memory_limit" in r.enforced
    else:
        # On this host the limit cannot be enforced — the result must NOT
        # claim it (test would fail if enforcement were faked).
        assert "memory_limit" not in r.enforced


# ---------------------------------------------------------------------------
# 8. Adapter integration — python_exec through the sandbox
# ---------------------------------------------------------------------------

async def test_adapter_runs_through_sandbox(workspace):
    adapter = PythonExecAdapter(str(workspace))
    r = await adapter.run({"code": "print(6*7)"}, _ctx())
    assert r.success, r.error
    out = r.output or {}
    assert out.get("exit_code") == 0
    assert "42" in str(out.get("stdout", ""))
    # Honesty fields present and consistent with this host's capabilities
    assert isinstance(out.get("sandbox_enforced"), list)
    assert "argv_execution" in out["sandbox_enforced"]


async def test_adapter_missing_code_rejected(workspace):
    adapter = PythonExecAdapter(str(workspace))
    r = await adapter.run({}, _ctx())
    assert not r.success
    assert "code" in r.error


async def test_adapter_timeout_wording_preserved(workspace):
    adapter = PythonExecAdapter(str(workspace), timeout=2)
    r = await adapter.run({"code": "import time; time.sleep(60)"}, _ctx())
    assert not r.success
    assert "python_exec timed out after 2s" in r.error


async def test_adapter_scope_is_per_task(workspace):
    a1 = PythonExecAdapter(str(workspace))
    a2 = PythonExecAdapter(str(workspace))
    r1 = await a1.run({"code": "import os; print(os.getcwd())"}, _ctx("task-a"))
    r2 = await a2.run({"code": "import os; print(os.getcwd())"}, _ctx("task-b"))
    assert r1.success and r2.success
    cwd_a = r1.output["stdout"].strip()
    cwd_b = r2.output["stdout"].strip()
    assert cwd_a != cwd_b
    assert contained_path(cwd_a, str(workspace / "task-a"))
    assert contained_path(cwd_b, str(workspace / "task-b"))


async def test_adapter_env_isolation_through_kernel_path(workspace):
    adapter = PythonExecAdapter(str(workspace))
    r = await adapter.run(
        {"code": f"import os; print(os.environ.get('{SECRET_KEY_NAME}', 'ABSENT'))"},
        _ctx())
    assert r.success, r.error
    assert r.output["stdout"].strip() == "ABSENT"


# ---------------------------------------------------------------------------
# 9. Symlink containment regression (Phase 0 boundary reused)
# ---------------------------------------------------------------------------

async def test_spec_cwd_symlink_outside_rejected(sandbox, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    link = ws / "link"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges on this host")
    if not contained_path(str(link), str(ws)):
        pytest.skip("host does not resolve symlink (Windows unprivileged)")
    r = await sandbox.run(_spec(link, "print(os.getcwd())"))
    assert not r.success
    assert "outside writable workspace" in r.error


def test_containment_helper_shared(tmp_path):
    """The sandbox uses the same Phase 0 boundary as the clamps."""
    outside = tmp_path / "outside"
    outside.mkdir()
    assert contained_path(str(outside), str(tmp_path / "ws")) is None
    inside = tmp_path / "ws" / "sub"
    inside.mkdir(parents=True)
    assert contained_path(str(inside), str(tmp_path / "ws")) == str(inside)
