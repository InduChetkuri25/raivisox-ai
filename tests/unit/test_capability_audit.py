"""Capability-audit tests: fresh runtime evidence for the capability matrix.

These tests exist to PROVE (or disprove) that each audited capability
performs its claimed real operation at runtime. They deliberately use the
real adapters/tools/engines directly — no stubs — and independently verify
results (on-disk files, subprocess stdout, raw sqlite reads, audit records).

Network-dependent tests skip honestly when no outbound connectivity exists.
"""

import asyncio
import atexit
import os
import shutil
import sqlite3
import tempfile
import uuid
from types import SimpleNamespace

# Pin a file-based (durable) DB BEFORE app.main is imported.
_AUDIT_TMPDIR = tempfile.mkdtemp(prefix="raivisox_audit_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(_AUDIT_TMPDIR, 'audit.db')}"
atexit.register(shutil.rmtree, _AUDIT_TMPDIR, ignore_errors=True)

import httpx  # noqa: E402
import pytest  # noqa: E402

from integrations.adapters import (  # noqa: E402
    AdapterContext,
    AdapterRegistry,
    CodegenAdapter,
)
from integrations.adapters.builtin import (  # noqa: E402
    FileReadAdapter,
    FileWriteAdapter,
    PythonExecAdapter,
    ShellExecAdapter,
)
from services.dod.dod_checks import run_module_checks  # noqa: E402


def _ctx(actor: str = "audit") -> AdapterContext:
    return AdapterContext(
        actor_id=actor, task_id="audit", step_id="s1", resource="audit:s1",
        workspace=None,
    )


def _run(adapter, params, actor="audit"):
    return asyncio.run(adapter.run(params, _ctx(actor)))


# ---------------------------------------------------------------------------
# 5. Filesystem tools — REAL roundtrip through the allowlisted writers
# ---------------------------------------------------------------------------


def test_fs_roundtrip_is_real(tmp_path):
    ws = str(tmp_path)
    w = FileWriteAdapter(ws)
    r = _run(w, {"path": "audit/roundtrip.txt", "content": "RAIVISOX-42"})
    assert r.success, r.error
    # _ctx() uses task id "audit" → the write lands in the task's own
    # scope directory (per-task isolation).
    on_disk = os.path.join(ws, "audit", "audit", "roundtrip.txt")
    assert os.path.isfile(on_disk)
    with open(on_disk, encoding="utf-8") as f:
        assert f.read() == "RAIVISOX-42"

    rd = FileReadAdapter(ws)
    r2 = _run(rd, {"path": "audit/roundtrip.txt"})
    assert r2.success, r2.error
    content = (r2.output or {}).get("content", "")
    assert content == "RAIVISOX-42"


def test_fs_workspace_escape_denied_and_audited(tmp_path):
    from services.security.audit_trail import AuditTrail

    ws = str(tmp_path)
    w = FileWriteAdapter(ws)
    escape = os.path.join(ws, "..", "escaped.txt")
    r = _run(w, {"path": escape, "content": "nope"})
    # Denial is signaled via success=False (not the kernel permission flag):
    # the adapter clamps the path itself and refuses before any write.
    assert not r.success, "path traversal must be denied"
    assert "denied" in (r.error or "").lower()
    assert not os.path.isfile(os.path.abspath(os.path.join(ws, "..", "escaped.txt")))
    denials = [
        e for e in AuditTrail.instance().get_events(actor_id="audit")
        if e.allowed is False
    ]
    assert denials, "denial must be audit-logged"


# ---------------------------------------------------------------------------
# 6. Shell execution — REAL subprocess via the sandbox
# ---------------------------------------------------------------------------


def test_shell_exec_is_real(tmp_path):
    sh = ShellExecAdapter(str(tmp_path))
    r = _run(sh, {"command": 'python -c "print(21*2)"'})
    assert r.success, r.error
    out = r.output or {}
    assert "42" in str(out.get("stdout", ""))


def test_shell_unlisted_command_denied(tmp_path):
    sh = ShellExecAdapter(str(tmp_path))
    r = _run(sh, {"command": "del everything"})
    assert (not r.success) or r.permission_denied, (
        "commands outside the default allowlist must be rejected"
    )


# ---------------------------------------------------------------------------
# 7. Python execution — REAL subprocess, independently verified output
# ---------------------------------------------------------------------------


def test_python_exec_is_real(tmp_path):
    py = PythonExecAdapter(str(tmp_path))
    r = _run(py, {"code": "print(6*7)"})
    assert r.success, r.error
    out = r.output or {}
    assert out.get("exit_code") == 0
    assert "42" in str(out.get("stdout", ""))


def test_python_exec_hanging_code_killed(tmp_path):
    py = PythonExecAdapter(str(tmp_path), timeout=3)
    r = _run(py, {"code": "import time; time.sleep(60)"})
    assert not r.success
    assert "timed out" in (r.error or "")


# ---------------------------------------------------------------------------
# 8. HTTP — REAL allowlisted GET; IP-literal and unlisted hosts denied
# ---------------------------------------------------------------------------


def _net_ok() -> bool:
    try:
        httpx.get("https://example.com", timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def test_http_allowlisted_host_is_real(tmp_path):
    from integrations.adapters.http_get import HttpGetAdapter

    if not _net_ok():
        pytest.skip("no outbound network connectivity")
    h = HttpGetAdapter()
    r = _run(h, {"url": "https://example.com"})
    assert r.success, r.error
    out = r.output or {}
    assert out.get("status") == 200
    assert "example" in str(out.get("title", "")).lower() or (
        len(str(out.get("body", ""))) > 50
    )


def test_http_ip_literal_denied(tmp_path):
    from integrations.adapters.http_get import HttpGetAdapter

    h = HttpGetAdapter()
    r = _run(h, {"url": "https://169.254.169.254/latest/meta-data/"})
    assert not r.success, "SSRF IP-literal host must be rejected before send"
    assert r.permission_denied or "allow" in (r.error or "").lower()


def test_http_unlisted_host_denied(tmp_path):
    from integrations.adapters.http_get import HttpGetAdapter

    h = HttpGetAdapter()
    r = _run(h, {"url": "https://definitely-not-allowlisted.example.net/"})
    assert not r.success
    assert r.permission_denied or "allow" in (r.error or "").lower()


def test_http_plain_http_scheme_denied(tmp_path):
    from integrations.adapters.http_get import HttpGetAdapter

    h = HttpGetAdapter()
    r = _run(h, {"url": "http://example.com/"})
    assert not r.success, "plain http:// must be rejected outright"


# ---------------------------------------------------------------------------
# 22. Security kernel — deny without grant, audit the denial
# ---------------------------------------------------------------------------


def test_security_kernel_denies_ungranted_actor_and_audits():
    from services.security.audit_trail import AuditTrail
    from services.security.security_kernel import SecurityKernel

    kernel = SecurityKernel()
    # Uncapped window: the shared in-process trail legitimately grows past
    # get_events' default last-100 view as the suite runs more real
    # pipelines; the cap must not make this count comparison ambiguous.
    before = len(AuditTrail.instance().get_events(action="tool:shell_exec", limit=100000))
    decision = kernel.authorize_and_log(
        actor_id="unprivileged-actor",
        action="tool:shell_exec",
        resource="audit:denied",
        permissions=["execute_terminal"],
    )
    assert not decision.allowed, "actor without the grant must be denied"
    after = AuditTrail.instance().get_events(action="tool:shell_exec", limit=100000)
    assert len(after) == before + 1
    assert after[-1].allowed is False


# ---------------------------------------------------------------------------
# 24. Persistence — DURABLE (file-backed) database, verified raw
# ---------------------------------------------------------------------------


@pytest.fixture()
def file_client():
    from fastapi.testclient import TestClient

    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    with TestClient(app_main.app) as c:
        user = SimpleNamespace(
            id=f"u-{uuid.uuid4().hex[:10]}", email=f"{uuid.uuid4().hex[:10]}@audit.test"
        )
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c
        app_main.app.dependency_overrides.clear()


def test_persistence_is_durable_sqlite(file_client):
    email = f"durable.{uuid.uuid4().hex[:8]}@audit.test"
    r = file_client.post(
        "/auth/register",
        json={"email": email, "password": "AuditPass123!", "name": "Audit"},
    )
    assert r.status_code == 201, r.text
    r = file_client.post(
        "/auth/login", json={"email": email, "password": "AuditPass123!"}
    )
    token = r.json()["access_token"]
    r = file_client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Durable audit task", "goal": "verify persistence"},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    # Read the DB file RAW with the stdlib after the request completed —
    # proves rows are persisted, not merely held in request state.
    from app.database import engine

    db_path = str(engine.url).replace("sqlite+aiosqlite:///", "")
    con = sqlite3.connect(db_path)
    try:
        users = con.execute(
            "SELECT email FROM users WHERE email = ?", (email,)
        ).fetchall()
        tasks = con.execute(
            "SELECT id, title FROM tasks WHERE id = ?", (task_id,)
        ).fetchall()
    finally:
        con.close()
    assert users, "user row must be durably persisted"
    assert tasks and tasks[0][1] == "Durable audit task"


# ---------------------------------------------------------------------------
# Honesty: unmapped capabilities stay SIMULATED; default resolves to finalize
# ---------------------------------------------------------------------------


def test_unmapped_capabilities_are_honestly_simulated():
    from integrations.adapters.finalize import FinalizeAdapter
    from services.security.security_kernel import SecurityKernel

    reg = AdapterRegistry(SecurityKernel())
    reg.register(FinalizeAdapter(str(_AUDIT_TMPDIR)))
    assert reg.select(["test_execution"]) is None
    assert reg.select(["code_read"]) is None
    assert reg.select(["data_parse"]) is None
    assert reg.select(["browser_control"]) is None
    assert reg.select([]) == "finalize"


# ---------------------------------------------------------------------------
# 11. DoD — execution success must not equal goal satisfaction
# ---------------------------------------------------------------------------


def test_dod_rejects_wrong_result_module(tmp_path):
    goal = (
        "Create a Python module that computes the sum of all integers from 1 "
        "to 100 and verify that the result is 5050."
    )
    mod = tmp_path / "wrong.py"
    mod.write_text("def solve():\n    return 4950\n")  # executes cleanly, wrong
    res = run_module_checks(goal, str(mod))
    assert res.executed is True  # execution succeeded...
    assert res.execution_output == 4950
    assert res.satisfied is False  # ...but the goal was NOT met
    assert run_module_checks(goal, str(mod)).satisfied is False
