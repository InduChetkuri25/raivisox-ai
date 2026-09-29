"""Tests for the executable Definition-of-Done verification stage.

Every execution-path test runs the REAL subprocess probe against a REAL
module file on disk — nothing is mocked. Pipeline-level tests drive the
actual execute endpoint with the DoD stage wired in, using the
fail_dod_with negative-test hook for the "executes fine, goal not met"
scenario.

The DATABASE_URL pin below MUST run before app.main is imported: the
engine and settings are created at import time.
"""

import atexit
import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace

import pytest

# Pin SQLite (file-based, unique per run) BEFORE importing app.main.
_DOD_TMPDIR = tempfile.mkdtemp(prefix="raivisox_dod_tests_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(_DOD_TMPDIR, 'dod.db')}"
atexit.register(shutil.rmtree, _DOD_TMPDIR, ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402

from integrations.adapters import AdapterContext, CodegenAdapter  # noqa: E402
from services.dod.dod_checks import (  # noqa: E402
    derive_checks,
    run_module_checks,
    summarize,
)

GOAL = (
    "Create a Python module that calculates the factorial of 6 "
    "and verify that the result is 720."
)


# ---------------------------------------------------------------------------
# A. Check derivation from the goal
# ---------------------------------------------------------------------------


def test_factorial_goal_derives_checks():
    checks = derive_checks(GOAL)
    ids = {c["id"] for c in checks}
    assert "factorial_value" in ids
    assert "expected_result" in ids
    for c in checks:
        assert c["kind"] == "value_equals"
        assert c["expected"] == 720


def test_no_derivable_criteria_abstains():
    checks = derive_checks("Build a nice calculator module someday")
    assert checks == []
    res = run_module_checks("Build a nice calculator module someday", __file__)
    assert res.abstained is True
    assert res.abstain_reason
    summary = summarize("Build a nice calculator module someday", [res])
    assert summary["applicable"] is False
    assert summary["satisfied"] is None


# ---------------------------------------------------------------------------
# B/C. Real execution + acceptance evaluation (real subprocess, no mocks)
# ---------------------------------------------------------------------------


def _codegen_factorial_module(tmp_path):
    adapter = CodegenAdapter(str(tmp_path))
    result = _run(adapter)
    return result.output["written_to"]


def _run(adapter, params=None):
    import asyncio

    return asyncio.run(
        adapter.run(
            params or {"goal": GOAL},
            AdapterContext(actor_id="t", task_id="t1", step_id="s1", resource="t:t1/s:s1"),
        )
    )


def test_solve_returns_720_passes(tmp_path):
    path = _codegen_factorial_module(tmp_path)
    assert os.path.isfile(path)
    res = run_module_checks(GOAL, path)
    assert res.executed is True
    assert res.execution_output == 720  # the REAL value from solve()
    assert res.satisfied is True
    assert all(c["passed"] for c in res.checks)


def test_wrong_value_fails(tmp_path):
    mod = tmp_path / "wrong.py"
    mod.write_text("def solve():\n    return 721\n")
    res = run_module_checks(GOAL, str(mod))
    assert res.executed is True  # ran cleanly...
    assert res.execution_output == 721  # ...and produced the REAL wrong value
    assert res.satisfied is False
    failed = [c for c in res.checks if not c["passed"]]
    assert failed, "acceptance checks must fail for 721"


def test_execution_success_dod_fail_distinct(tmp_path):
    """Execution success and goal satisfaction are separate fields."""
    path = _codegen_factorial_module(tmp_path)
    res = run_module_checks(GOAL, path, override_return=721)
    assert res.executed is True
    assert res.execution_output == 721
    assert res.satisfied is False
    summary = summarize(GOAL, [res])
    assert summary["applicable"] is True
    assert summary["satisfied"] is False


def test_missing_artifact_reports_execution_failure(tmp_path):
    res = run_module_checks(GOAL, str(tmp_path / "nope.py"))
    assert res.executed is False
    assert res.execution_error
    assert res.satisfied is False
    assert any(not c["passed"] for c in res.checks)


def test_hanging_solve_times_out(tmp_path):
    mod = tmp_path / "hanging.py"
    mod.write_text("import time\ndef solve():\n    time.sleep(30)\n    return 1\n")
    res = run_module_checks(GOAL, str(mod), timeout=2)
    assert res.executed is False
    assert "timed out" in (res.execution_error or "")
    assert res.satisfied is False


def test_crashing_solve_reports_error(tmp_path):
    mod = tmp_path / "crash.py"
    mod.write_text("def solve():\n    raise RuntimeError('boom')\n")
    res = run_module_checks(GOAL, str(mod))
    assert res.executed is False
    assert "boom" in (res.execution_error or "")
    assert res.satisfied is False


def test_no_solve_reports_missing(tmp_path):
    mod = tmp_path / "nosolve.py"
    mod.write_text("X = 1\n")
    res = run_module_checks(GOAL, str(mod))
    assert res.executed is False
    assert "solve()" in (res.execution_error or "")
    assert res.satisfied is False


# ---------------------------------------------------------------------------
# D. Selective re-arm after a DoD-only failure
# ---------------------------------------------------------------------------


def test_reset_rearms_only_codegen_and_finalize():
    from app.main import _reset_codegen_nodes
    from services.execution.task_graph import ExecutionStatus, TaskGraph

    class _S:
        def __init__(self, caps):
            self.required_capabilities = caps
            self.title = "t"
            self.description = "d"

    decomposed = [
        _S(["planning"]),          # step-1
        _S(["code_generation"]),   # step-2
        _S(["filesystem_write"]),  # step-3
        _S([]),                    # step-4 (finalize)
    ]
    graph = TaskGraph()
    for i in range(4):
        graph.add_step(id=f"step-{i + 1}", title=f"t{i + 1}")
    for nid in ("step-1", "step-3", "step-4"):
        node = graph.nodes[nid]
        node.status = ExecutionStatus.COMPLETED
        node.result = {"tool": "finalize" if nid == "step-4" else "other"}
    graph.nodes["step-2"].status = ExecutionStatus.COMPLETED
    graph.nodes["step-2"].result = {"tool": "codegen"}
    graph.nodes["step-3"].status = ExecutionStatus.FAILED  # failed non-codegen

    rearmed = _reset_codegen_nodes(graph, decomposed)

    assert sorted(rearmed) == ["step-2", "step-4"]
    assert graph.nodes["step-2"].status == ExecutionStatus.PENDING
    assert graph.nodes["step-4"].status == ExecutionStatus.PENDING
    # Completed independent nodes are untouched.
    assert graph.nodes["step-1"].status == ExecutionStatus.COMPLETED
    # A failed NON-codegen node is not silently re-armed by the DoD path.
    assert graph.nodes["step-3"].status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# E/F. Pipeline-level: positive and negative through the REAL endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    with TestClient(app_main.app) as c:
        user = SimpleNamespace(
            id=f"u-{uuid.uuid4().hex[:10]}", email=f"{uuid.uuid4().hex[:10]}@dod.test"
        )
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c
        app_main.app.dependency_overrides.clear()


def _make_task(client, title, goal):
    r = client.post("/api/v1/tasks", json={"title": title, "goal": goal})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_pipeline_positive_factorial(client):
    task_id = _make_task(client, "Factorial DoD", GOAL)
    r = client.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    body = r.json()

    # Final task outcome is completed/passed — only because DoD passed.
    assert body["status"] == "completed"
    assert body["result"]["verification"]["verdict"] == "passed"

    dod = body["definition_of_done"]
    assert dod["applicable"] is True
    assert dod["satisfied"] is True
    modules = dod["modules"]
    assert modules, "codegen artifacts must have been DoD-checked"
    for m in modules:
        assert m["executed"] is True
        assert m["execution_output"] == 720  # independent probe value
        assert m["satisfied"] is True

    # The generated artifact exists on disk and solve() REALLY executes.
    import importlib.util

    # Per-task workspace scoping: the artifact lives under THIS task's own
    # directory (the flat “tasks/generated” path only existed before that
    # scoping, so a stale leftover made this pass on a dirty machine while
    # clean checkouts failed).
    from app.main import TASK_WORKSPACE

    ws_generated = os.path.join(TASK_WORKSPACE, task_id, "generated")
    for m in modules:
        mod_path = os.path.join(ws_generated, f"{m['module']}.py")
        assert os.path.isfile(mod_path), mod_path
        spec = importlib.util.spec_from_file_location(f"dodlive_{m['module']}", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(getattr(mod, "solve", None))
        assert mod.solve() == 720

    assert body["result"]["recovery_attempts"] == 0


def test_pipeline_negative_wrong_result_recovers_then_fails(client):
    task_id = _make_task(client, "Factorial DoD NEG", GOAL)
    r = client.post(
        f"/api/v1/tasks/{task_id}/execute", json={"fail_dod_with": 721}
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Execution itself succeeded every time...
    for m in body["definition_of_done"]["modules"]:
        assert m["executed"] is True
        assert m["execution_output"] == 721
        assert m["satisfied"] is False

    # ...but the goal is NOT satisfied, so the task is NOT successful.
    assert body["definition_of_done"]["satisfied"] is False
    assert body["result"]["definition_of_done"]["satisfied"] is False
    assert body["status"] == "failed"
    assert body["result"]["verification"]["verdict"] == "passed"

    # DoD recovery ran until exhausted (MAX_RECOVERY_ATTEMCTS = 3).
    assert body["result"]["recovery_attempts"] == 3
    assert body["recovery_attempts"] == 3
    assert body["recovery"] is not None
