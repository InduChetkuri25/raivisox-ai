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
import re
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
# 8. HTTP — allowlisted GET, honest verdicts, no redirect escape
# ---------------------------------------------------------------------------
#
# The policy/response tests run offline against a controlled transport. That is
# the closest a test can get to a real request here: production requires https
# on the scheme's default port and refuses every non-global resolved address,
# so a loopback server is unreachable by design (a local fixture would have to
# weaken exactly the guard under test). Only the transport is substituted; the
# allowlist, the DNS/SSRF consult, redirect re-validation, status/content-type
# handling and body parsing are the production code paths.
#
# The single live test keeps the real socket and DNS path, and asserts only the
# property this repository owns: an allowlisted host gets through the whole
# policy stack and the adapter reports the far end's own verdict. A third
# party's 5xx is not a regression here and must not turn the suite red.


def _net_ok() -> bool:
    """Reachability probe — deliberately not an HTTP-status check.

    Probing for a 200 conflated "the internet works" with "example.com is
    healthy right now": a 503 does not raise, so the old probe returned True
    and the caller then failed on the status assertion.
    """
    import socket

    try:
        with socket.create_connection(("example.com", 443), timeout=5):
            return True
    except OSError:
        return False


def _stub_public_dns(monkeypatch, allowed: bool = True):
    """Answer the SSRF/DNS consult; return the hosts it was asked about.

    Resolution is the one production step a controlled fixture cannot own, so
    it is substituted explicitly — and still asserted to be consulted for
    every hop, which is what keeps an allowlisted name from skipping the guard.
    """
    import integrations.adapters.http_get as http_get

    calls: list[str] = []

    def _all_ips_global(host: str):
        calls.append(host)
        if allowed:
            return True, ""
        return False, f"Refusing non-public resolved address for host '{host}' (SSRF guard)"

    monkeypatch.setattr(http_get, "_all_ips_global", _all_ips_global)
    return calls


def _http_transport(monkeypatch, respond):
    """Point the adapter's real httpx client at ``respond``; return requested URLs."""
    import integrations.adapters.http_get as http_get

    seen: list[str] = []
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return respond(request)

    transport = httpx.MockTransport(_handler)

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(http_get.httpx, "AsyncClient", _client)
    return seen


def test_http_allowlisted_host_is_real(tmp_path):
    """A live allowlisted GET completes and its verdict is the server's own."""
    from integrations.adapters.http_get import HttpGetAdapter

    if not _net_ok():
        pytest.skip("no outbound network connectivity")
    h = HttpGetAdapter()

    # The real allowlist + SSRF guard decide admission before any byte is sent.
    admitted, reason = h._check_url("https://example.com/")
    assert admitted, f"allowlisted host refused by policy: {reason}"

    r = _run(h, {"url": "https://example.com/"})
    if r.success:
        out = r.output or {}
        assert out.get("status") == 200
        assert out.get("operation") == "web_read"
        assert "example" in str(out.get("title", "")).lower() or (
            len(str(out.get("content", ""))) > 50
        )
        return

    # A real response arrived but was not a success: the adapter must surface
    # the far end's status verbatim. Fabricating success, or failing without
    # an HTTP verdict, is what this assertion catches.
    error = r.error or ""
    match = re.fullmatch(r"HTTP (\d{3}) from https://example\.com/", error)
    assert match, f"live GET produced no HTTP verdict: {error!r}"
    status = int(match.group(1))
    assert 400 <= status <= 599, f"unexpected non-200 success-path status {status}"
    assert r.output is None, "a non-200 response must not yield output"
    # The request left the process and the far end answered with an error. The
    # happy path is pinned by the fixture tests below, so a third party's 5xx is
    # reported here rather than treated as this repository's regression.


def test_http_read_parses_a_real_200_response(monkeypatch):
    """Admission, status, title and body come from the actual response bytes."""
    from integrations.adapters.http_get import HttpGetAdapter

    dns = _stub_public_dns(monkeypatch)
    body = b"".join(
        [
            b"<html><head><title>Example Domain</title></head><body>",
            b"<h1>Example Domain</h1><p>",
            b"This domain is for use in documentation examples. " * 3,
            b"</p></body></html>",
        ]
    )
    seen = _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=body
        ),
    )

    r = _run(HttpGetAdapter(), {"url": "https://example.com/"})
    assert r.success, r.error
    out = r.output or {}
    assert out["status"] == 200
    assert out["operation"] == "web_read"
    assert out["title"] == "Example Domain"
    assert out["content_type"].startswith("text/html")
    assert "documentation examples" in out["content"]
    assert out["bytes"] == len(out["content"])
    # The allowlist did not short-circuit the guard: DNS is consulted on
    # admission and again for every hop before the request is issued.
    assert dns and set(dns) == {"example.com"}, dns
    assert seen == ["https://example.com/"]


def test_http_read_refuses_a_host_whose_resolution_is_not_public(monkeypatch):
    """An allowlisted name does not bypass the SSRF guard's verdict."""
    from integrations.adapters.http_get import HttpGetAdapter

    _stub_public_dns(monkeypatch, allowed=False)
    seen = _http_transport(
        monkeypatch, lambda request: httpx.Response(200, text="must not be sent")
    )

    r = _run(HttpGetAdapter(), {"url": "https://example.com/"})
    assert not r.success
    assert "SSRF" in (r.error or "")
    assert seen == [], "no request may be sent for a non-public resolution"


def test_http_read_non_200_is_a_failure_not_a_success(monkeypatch):
    """An arbitrary HTTP error is never converted into success."""
    from integrations.adapters.http_get import HttpGetAdapter

    _stub_public_dns(monkeypatch)
    _http_transport(
        monkeypatch, lambda request: httpx.Response(503, text="service unavailable")
    )

    r = _run(HttpGetAdapter(), {"url": "https://example.com/"})
    assert not r.success
    assert r.output is None
    assert "HTTP 503" in (r.error or "")


def test_http_read_refuses_non_text_content_type(monkeypatch):
    """A 200 is not enough: non-text payloads are refused."""
    from integrations.adapters.http_get import HttpGetAdapter

    _stub_public_dns(monkeypatch)
    _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"\x00" * 64
        ),
    )

    r = _run(HttpGetAdapter(), {"url": "https://example.com/"})
    assert not r.success
    assert "non-text" in (r.error or "").lower()


def test_http_read_does_not_follow_a_redirect_out_of_the_allowlist(monkeypatch):
    """An allowlisted origin cannot redirect the adapter to another host."""
    from integrations.adapters.http_get import HttpGetAdapter

    _stub_public_dns(monkeypatch)
    seen = _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://evil.example.org/steal"}
        ),
    )

    r = _run(HttpGetAdapter(), {"url": "https://example.com/"})
    assert not r.success
    assert "allowlist" in (r.error or "").lower()
    assert seen == ["https://example.com/"], (
        "the unapproved redirect target must never be fetched"
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
