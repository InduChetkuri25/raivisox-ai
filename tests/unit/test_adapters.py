"""Tests for the tool adapter layer and its pipeline integration.

Covers: registration, capability selection, real tool execution, tool
failure, permission rejection, retry-after-transient-failure of a REAL
tool, permanent failure with skip propagation, parallel independent
tools, and completed nodes not being re-executed.
"""

import os

import pytest

from integrations.adapters import (
    AdapterContext,
    AdapterRegistry,
    UnknownToolError,
    FileReadAdapter,
    FileWriteAdapter,
    PythonExecAdapter,
    ShellExecAdapter,
    TextTransformAdapter,
)
from integrations.adapters.base import CAPABILITY_TO_ADAPTER
from services.execution.task_graph import ExecutionStatus, ParallelExecutor
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class RecordingPythonAdapter(PythonExecAdapter):
    """PythonExecAdapter that counts real invocations."""

    def __init__(self, workspace, timeout=20):
        super().__init__(workspace, timeout)
        self.call_count = 0

    async def run(self, params, ctx):
        self.call_count += 1
        return await super().run(params, ctx)


class FlakyFileWriteAdapter(FileWriteAdapter):
    """FileWriteAdapter that fails its first N real invocations."""

    def __init__(self, workspace, fail_times=1):
        super().__init__(workspace)
        self.fail_times = fail_times
        self.real_calls = 0

    async def run(self, params, ctx):
        self.real_calls += 1
        if self.real_calls <= self.fail_times:
            from integrations.adapters.base import AdapterResult
            return AdapterResult(
                success=False, tool_id=self.id, error="simulated disk hiccup"
            )
        return await super().run(params, ctx)


def ctx_for(actor="tester", task="t1"):
    return AdapterContext(actor_id=actor, task_id=task, step_id="s1", resource=f"task:{task}")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


@pytest.fixture
def kernel():
    """Kernel granting the pipeline tester the permissions real tools need."""
    k = SecurityKernel()
    for perm in (Permission.READ_FILES, Permission.WRITE_FILES,
                 Permission.EXECUTE_TERMINAL, Permission.EXECUTE_CODE):
        k.grant_permission("tester", perm)
    return k


@pytest.fixture
def registry(workspace, kernel):
    reg = AdapterRegistry(kernel)
    reg.register(FileReadAdapter(workspace))
    reg.register(FileWriteAdapter(workspace))
    reg.register(ShellExecAdapter(workspace))
    reg.register(PythonExecAdapter(workspace))
    reg.register(TextTransformAdapter())
    return reg


@pytest.fixture(autouse=True)
def _clear_audit():
    AuditTrail.instance().clear()
    yield
    AuditTrail.instance().clear()


# ---------------------------------------------------------------------------
# Registration & selection
# ---------------------------------------------------------------------------

def test_registration_and_listing(registry):
    ids = [a["id"] for a in registry.list_adapters()]
    assert ids == ["fs_read", "fs_write", "shell_exec", "python_exec", "text_transform"]


async def test_unknown_tool_raises(registry):
    with pytest.raises(UnknownToolError):
        await registry.run("nope", {}, ctx_for())


def test_capability_mapping_is_real_only():
    assert CAPABILITY_TO_ADAPTER["filesystem_read"] == "fs_read"
    assert CAPABILITY_TO_ADAPTER["filesystem_write"] == "fs_write"
    assert CAPABILITY_TO_ADAPTER["code_execution"] == "python_exec"
    assert CAPABILITY_TO_ADAPTER["terminal_execute"] == "shell_exec"
    assert CAPABILITY_TO_ADAPTER["data_transformation"] == "text_transform"


def test_select_returns_none_for_unmapped_capability(registry):
    # code_generation / test_execution have NO real adapter — stay simulated
    assert registry.select(["code_generation", "test_execution"]) is None


def test_select_picks_registered_adapter(registry):
    assert registry.select(["data_transformation"]) == "text_transform"
    assert registry.select(["terminal_execute"]) == "shell_exec"
    assert registry.select(["filesystem_read"]) == "fs_read"


# ---------------------------------------------------------------------------
# Real tool execution
# ---------------------------------------------------------------------------

async def test_text_transform_real(registry):
    r = await registry.run(
        "text_transform", {"op": "count", "text": "one two three"}, ctx_for()
    )
    assert r.success
    assert r.output["words"] == 3
    assert r.output["characters"] == 13


async def test_fs_write_then_fs_read_real(registry, workspace):
    w = await registry.run(
        "fs_write", {"path": "out/hello.txt", "content": "hello adapter world"}, ctx_for()
    )
    assert w.success, w.error
    r = await registry.run("fs_read", {"path": "out/hello.txt"}, ctx_for())
    assert r.success, r.error
    assert "hello adapter world" in r.output["content"]
    # Writes are scoped into the task's own directory (per-task isolation).
    assert os.path.exists(os.path.join(workspace, "t1", "out", "hello.txt"))


async def test_python_exec_real_stdout(registry):
    r = await registry.run("python_exec", {"code": "print(21 * 2)"}, ctx_for())
    assert r.success, r.error
    assert r.output["exit_code"] == 0
    assert "42" in r.output["stdout"]


async def test_shell_exec_real_stdout(registry):
    r = await registry.run("shell_exec", {"command": "echo sandbox-alive"}, ctx_for())
    assert r.success, r.error
    assert "sandbox-alive" in r.output["stdout"]


async def test_shell_blocklist_rejects_dangerous_command(registry, workspace):
    r = await registry.run("shell_exec", {"command": "del important.txt"}, ctx_for())
    # 'del' is not in the shell allowlist — must be blocked, not executed
    assert not r.success
    assert not os.path.exists(os.path.join(workspace, "important.txt"))


async def test_tool_failure_is_failure(registry):
    r = await registry.run("fs_read", {"path": "does/not/exist.txt"}, ctx_for())
    assert not r.success
    assert r.error


async def test_python_exec_failure_propagates(registry):
    r = await registry.run("python_exec", {"code": "raise SystemExit(3)"}, ctx_for())
    assert not r.success
    assert r.output["exit_code"] == 3


# ---------------------------------------------------------------------------
# Permission / security boundaries
# ---------------------------------------------------------------------------

async def test_path_outside_workspace_denied(registry):
    target = "C:/Windows/raivisox_should_not_exist.txt"
    r = await registry.run(
        "fs_write", {"path": target, "content": "nope"}, ctx_for()
    )
    assert not r.success
    assert "outside the workspace" in (r.error or "")
    assert not os.path.exists(target)


async def test_kernel_permission_rejection(workspace):
    kernel = SecurityKernel()  # no grants at all
    reg = AdapterRegistry(kernel)
    reg.register(FileWriteAdapter(workspace))
    r = await reg.run(
        "fs_write", {"path": "denied.txt", "content": "x"},
        ctx_for(actor="unprivileged-actor"),
    )
    assert not r.success
    assert r.permission_denied
    assert "Permission denied by security kernel" in (r.error or "")
    denials = [
        e for e in AuditTrail.instance().get_events(actor_id="unprivileged-actor")
        if not e.allowed
    ]
    assert denials, "expected a denied audit event"


async def test_kernel_granted_permission_allows_run(workspace):
    kernel = SecurityKernel()
    kernel.grant_permission("actor-ok", Permission.WRITE_FILES)
    reg = AdapterRegistry(kernel)
    reg.register(FileWriteAdapter(workspace))
    r = await reg.run(
        "fs_write", {"path": "allowed.txt", "content": "x"}, ctx_for(actor="actor-ok")
    )
    assert r.success, r.error
    assert not r.permission_denied


# ---------------------------------------------------------------------------
# Pipeline integration: parallel, retry, no-re-execution
# ---------------------------------------------------------------------------

def _tt_params(node_id):
    return {"op": "count", "text": f"payload for {node_id}"}


async def test_parallel_independent_real_tools(registry):
    exec_ = ParallelExecutor(max_concurrent=4)

    def make_worker(nid):
        async def _worker():
            r = await registry.run("text_transform", _tt_params(nid), ctx_for(task=nid))
            if not r.success:
                raise RuntimeError(r.error)
            return {"tool": "text_transform", "mode": "real"}
        return _worker

    for nid in ("t1", "t2", "t3"):
        exec_.add_step(nid, nid, fn=make_worker(nid), depends_on=[])
    await exec_.execute()
    assert exec_.graph.all_completed
    assert all(n.result["mode"] == "real" for n in exec_.graph.nodes.values())


async def test_retry_after_transient_real_tool_failure(workspace, kernel):
    reg = AdapterRegistry(kernel)
    flaky = FlakyFileWriteAdapter(workspace, fail_times=1)
    reg.register(flaky)

    exec_ = ParallelExecutor(max_concurrent=2)

    async def _invoke():
        r = await reg.run(
            "fs_write", {"path": "retry.txt", "content": "attempt"}, ctx_for()
        )
        if not r.success:
            raise RuntimeError(r.error)
        return {"tool": "fs_write"}

    exec_.add_step("w1", "write", fn=_invoke, depends_on=[])

    # Attempt 1: real tool fails transiently -> node FAILED
    await exec_.execute()
    assert exec_.graph.nodes["w1"].status == ExecutionStatus.FAILED
    assert flaky.real_calls == 1

    # Recovery: re-arm only the failed node and re-execute the REAL tool
    node = exec_.graph.nodes["w1"]
    node.status = ExecutionStatus.PENDING
    node.error = ""
    await exec_.execute()

    assert exec_.graph.nodes["w1"].status == ExecutionStatus.COMPLETED
    assert flaky.real_calls == 2, "the real tool must be re-executed on retry"
    # Scoped into the task's own directory (ctx_for() → task "t1").
    assert os.path.exists(os.path.join(workspace, "t1", "retry.txt"))


async def test_completed_nodes_not_reexecuted(workspace, kernel):
    rec = RecordingPythonAdapter(workspace)
    reg = AdapterRegistry(kernel)
    reg.register(rec)

    exec_ = ParallelExecutor(max_concurrent=2)

    async def _py():
        r = await reg.run("python_exec", {"code": "print('x')"}, ctx_for())
        if not r.success:
            raise RuntimeError(r.error)
        return {}

    async def _fail():
        raise RuntimeError("permanent failure")

    exec_.add_step("p1", "run", fn=_py, depends_on=[])
    await exec_.execute()
    assert exec_.graph.nodes["p1"].status == ExecutionStatus.COMPLETED
    first_count = rec.call_count

    # Retry cycle: add a failing sibling; completed p1 must NOT re-run
    exec_.add_step("p2", "fail", fn=_fail, depends_on=[])
    await exec_.execute()
    assert exec_.graph.nodes["p2"].status == ExecutionStatus.FAILED
    assert rec.call_count == first_count, "completed node must not re-execute"


async def test_permanent_failure_skips_dependents(registry):
    exec_ = ParallelExecutor(max_concurrent=2)

    async def fail():
        raise RuntimeError("boom")

    async def ok():
        r = await registry.run("text_transform", _tt_params("child"), ctx_for())
        if not r.success:
            raise RuntimeError(r.error)
        return {}

    exec_.add_step("root", "root", fn=fail, depends_on=[])
    exec_.add_step("child", "child", fn=ok, depends_on=["root"])
    exec_.add_step("grandchild", "gc", fn=ok, depends_on=["child"])
    await exec_.execute()

    statuses = {nid: n.status for nid, n in exec_.graph.nodes.items()}
    assert statuses["root"] == ExecutionStatus.FAILED
    assert statuses["child"] == ExecutionStatus.SKIPPED
    assert statuses["grandchild"] == ExecutionStatus.SKIPPED
