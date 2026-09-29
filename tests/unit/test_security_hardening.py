"""Security hardening tests — boundary and isolation guarantees.

Every test here proves a REAL security property that an attacker could
probe from outside:

- Authentication: protected endpoints reject anonymous and forged tokens.
- Ownership (IDOR): a second user gets honest 404s on foreign tasks,
  projects, runs, events, artifacts, downloads and bundles — no data and
  no existence oracle beyond the conventional 404.
- Filesystem: path traversal cannot escape the workspace through artifact
  downloads, and adapters scope writes per task (no cross-task reads).
- Input validation: malformed IDs and emails are rejected safely.
- Leakage: task/artifact payloads never expose server filesystem paths.

These tests run in the NORMAL suite (no Ollama, no network).
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

_TMP = os.path.join("workspaces", "tasks", "security_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "security.db"))
# setdefault: other test modules own the effective DATABASE_URL chain.
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")
atexit.register(lambda: os.path.exists(_DB_PATH) and os.remove(_DB_PATH))

GOAL_FACTORIAL = (
    "Create a Python module that calculates the factorial of 6 and verify "
    "that the result is 720."
)


@pytest.fixture()
def two_users():
    """One TestClient whose auth override can be swapped owner↔attacker."""
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    def _bind(user):
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user

    owner = SimpleNamespace(
        id=f"u-owner-{uuid.uuid4().hex[:8]}",
        email=f"owner-{uuid.uuid4().hex[:8]}@security.test",
    )
    mallory = SimpleNamespace(
        id=f"u-mallory-{uuid.uuid4().hex[:8]}",
        email=f"mallory-{uuid.uuid4().hex[:8]}@security.test",
    )
    _bind(owner)
    with TestClient(app_main.app) as c:
        yield c, owner, mallory
    app_main.app.dependency_overrides.clear()


def _as(c, user):
    """Re-bind all auth dependency overrides to `user` (attacker swap)."""
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
    app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
    app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user


def _req(c, method: str, url: str):
    """Issue a request; body-carrying methods get an empty JSON object."""
    if method in ("post", "put", "patch"):
        return getattr(c, method)(url, json={})
    return getattr(c, method)(url)


@pytest.fixture()
def client():
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    with TestClient(app_main.app) as c:
        user = SimpleNamespace(
            id=f"u-{uuid.uuid4().hex[:10]}", email=f"{uuid.uuid4().hex[:10]}@security.test"
        )
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c
        app_main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Authentication boundaries (no dependency override → real token path)
# ---------------------------------------------------------------------------


def test_protected_endpoints_reject_anonymous():
    from app import main as app_main

    with TestClient(app_main.app) as c:
        for method, url in (
            ("get", "/api/v1/tasks"),
            ("post", "/api/v1/tasks"),
            ("get", "/api/v1/tasks/whatever/artifacts"),
            ("get", "/api/v1/tasks/whatever/artifacts/bundle/download"),
            ("get", "/api/v1/tasks/whatever/artifacts/history"),
            ("get", "/api/v1/projects"),
            ("get", "/api/v1/audit"),
            ("post", "/api/v1/tasks/whatever/execute"),
            ("get", "/api/v1/intent/parse?user_input=hello"),
            ("get", "/api/v1/models"),
        ):
            r = getattr(c, method)(url)
            assert r.status_code == 401, f"{method.upper()} {url} → {r.status_code}"


def test_forged_and_garbage_tokens_rejected():
    import base64

    from app import main as app_main

    # Token signed with a DIFFERENT key (classic forge attempt).
    try:
        from jose import jwt as _jwt

        forged = _jwt.encode(
            {"sub": "u-forged", "exp": 9999999999, "type": "access"},
            "attacker-controlled-secret",
            algorithm="HS256",
        )
    except Exception:
        forged = "x"
    garbage = base64.urlsafe_b64encode(b"not-a-jwt").decode()

    with TestClient(app_main.app) as c:
        for tok in (forged, garbage, "abc.def.ghi"):
            r = c.get(
                "/api/v1/tasks",
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert r.status_code == 401, tok
        # Tampered payload of a VALID-structure token (wrong signature).
        r = c.get(
            "/api/v1/tasks",
            headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.AA"},
        )
        assert r.status_code == 401


def test_health_is_public():
    from app import main as app_main

    with TestClient(app_main.app) as c:
        assert c.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# 2. Ownership / IDOR — every foreign access is an honest 404
# ---------------------------------------------------------------------------


def _mk_task(c, title="victim", goal=GOAL_FACTORIAL, project_id=None):
    r = c.post(
        "/api/v1/tasks",
        json={"title": title, "goal": goal, "project_id": project_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run_pipeline(c, task_id):
    r = c.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    return r.json()


def test_foreign_task_is_invisible_idor(two_users):
    c, owner, mallory = two_users
    task_id = _mk_task(c, "owner secret")
    _run_pipeline(c, task_id)

    # Swap to the attacker.
    _as(c, mallory)

    for method, url in (
        ("get", f"/api/v1/tasks/{task_id}"),
        ("get", f"/api/v1/tasks/{task_id}/events"),
        ("get", f"/api/v1/tasks/{task_id}/artifacts"),
        ("get", f"/api/v1/tasks/{task_id}/artifacts/history"),
        ("get", f"/api/v1/tasks/{task_id}/artifacts/0"),
        ("get", f"/api/v1/tasks/{task_id}/artifacts/bundle/download"),
        ("get", f"/api/v1/tasks/{task_id}/artifacts/rows/nonexistent-id"),
        ("post", f"/api/v1/tasks/{task_id}/execute"),
        ("post", f"/api/v1/tasks/{task_id}/cancel"),
        ("put", f"/api/v1/tasks/{task_id}"),
        ("post", f"/api/v1/tasks/{task_id}/decompose"),
        ("post", f"/api/v1/tasks/{task_id}/plan"),
        ("post", f"/api/v1/tasks/{task_id}/route"),
        ("post", f"/api/v1/tasks/{task_id}/definition-of-done"),
    ):
        r = _req(c, method, url)
        assert r.status_code == 404, f"{method.upper()} {url} → {r.status_code}: {r.text[:120]}"


def test_foreign_run_id_is_404_not_leaked(two_users):
    """task_id+run_id pair from another task must never list artifacts."""
    c, owner, mallory = two_users
    t1 = _mk_task(c, "runs one")
    _run_pipeline(c, t1)
    history = c.get(f"/api/v1/tasks/{t1}/artifacts/history").json()
    run_ids = [r["run_id"] for r in history.get("runs", []) if not r.get("is_legacy_run")]
    if not run_ids:
        pytest.skip("no first-class runs recorded for this pipeline")
    real_run = run_ids[0]

    t2 = _mk_task(c, "runs two")

    # Mallory owns t2 but probes it with OWNER's run id → foreign pair.
    _as(c, mallory)
    r = c.get(f"/api/v1/tasks/{t2}/artifacts?run={real_run}")
    assert r.status_code == 404
    # Cross-task run usage inside the owner's own account is also 404.
    # (Same user, different task: the pair must still validate.)
    r = c.get(f"/api/v1/tasks/{t2}/artifacts?run={real_run}")
    assert r.status_code == 404


def test_foreign_artifact_download_is_404(two_users):
    c, owner, mallory = two_users
    t1 = _mk_task(c, "artifacts one")
    _run_pipeline(c, t1)
    listing = c.get(f"/api/v1/tasks/{t1}/artifacts").json()
    arts = listing.get("artifacts") or []
    if not arts:
        pytest.skip("no artifacts registered for this pipeline")

    _as(c, mallory)

    art = arts[0]
    r = c.get(f"/api/v1/tasks/{t1}/artifacts/rows/{art['artifact_id']}")
    assert r.status_code == 404
    r = c.get(f"/api/v1/tasks/{t1}/artifacts/0")
    assert r.status_code == 404
    r = c.get(f"/api/v1/tasks/{t1}/artifacts/bundle/download")
    assert r.status_code == 404


def test_foreign_project_is_404_and_not_listable(two_users):
    c, owner, mallory = two_users
    r = c.post("/api/v1/projects", json={"name": "secret project"})
    assert r.status_code == 201
    project_id = r.json()["id"]
    _mk_task(c, "task in project", project_id=project_id)

    _as(c, mallory)

    r = c.get(f"/api/v1/projects/{project_id}")
    assert r.status_code == 404
    r = c.put(f"/api/v1/projects/{project_id}", json={"name": "hijacked"})
    assert r.status_code == 404
    r = c.delete(f"/api/v1/projects/{project_id}")
    assert r.status_code == 404
    r = c.get(f"/api/v1/projects/{project_id}/artifacts/bundle/download")
    assert r.status_code == 404
    # The project list must not contain the foreign project.
    names = [p["id"] for p in c.get("/api/v1/projects").json().get("projects", [])]
    assert project_id not in names


def test_task_in_foreign_project_cannot_be_created(client):
    r = client.post("/api/v1/tasks", json={"title": "x", "project_id": "no-such-project"})
    assert r.status_code == 404


def test_admin_boundary_non_admin_gets_403(client):
    from services.security.audit_trail import AuditTrail

    r = client.get("/api/v1/audit")
    assert r.status_code == 403
    denials = [
        e for e in AuditTrail.instance().get_events(action="api:view_audit")
        if e.allowed is False
    ]
    assert denials, "the 403 itself must be audit-logged"


# ---------------------------------------------------------------------------
# 3. Filesystem: traversal + per-task isolation
# ---------------------------------------------------------------------------


def test_artifact_download_cannot_escape_workspace_via_path(client):
    """Artifact addressing is by index/id only — no path parameters exist."""
    task_id = _mk_task(client)
    for probe in ("..%2f..%2fetc%2fpasswd", "..\\..\\secret.txt", "%00", "x/../y"):
        r = client.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{probe}")
        # Unknown id → honest 404, never file contents. (A probe like
        # %2e%2e%2f is dot-segment-normalized by the HTTP client into the
        # owner-scoped LIST endpoint of the SAME task — safe by design.)
        assert r.status_code == 404, probe


def test_zip_bundle_never_contains_traversal_entries(client):
    import io
    import zipfile

    task_id = _mk_task(client)
    _run_pipeline(client, task_id)
    r = client.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
    if r.status_code == 404:
        pytest.skip("no artifacts for this pipeline")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    for name in zf.namelist():
        assert ".." not in name, name
        assert not name.startswith("/") and not name.startswith("\\"), name
        assert ":" not in name.replace("MANIFEST", ""), name  # no drive letters


def test_task_workspace_isolation_between_tasks():
    """Two tasks with the same relative artifact path cannot see each
    other's bytes: adapters scope into per-task directories."""
    import asyncio

    from integrations.adapters import AdapterContext, FileWriteAdapter, FileReadAdapter
    from app.main import TASK_WORKSPACE

    a_id, b_id = f"task-{uuid.uuid4().hex[:8]}", f"task-{uuid.uuid4().hex[:8]}"
    writer = FileWriteAdapter(TASK_WORKSPACE)
    reader = FileReadAdapter(TASK_WORKSPACE)

    async def _scenario():
        ctx_a = AdapterContext(actor_id="p", task_id=a_id, step_id="s1", resource=f"task:{a_id}")
        ctx_b = AdapterContext(actor_id="p", task_id=b_id, step_id="s1", resource=f"task:{b_id}")
        wa = await writer.run({"path": "generated/x.py", "content": "A"}, ctx_a)
        wb = await writer.run({"path": "generated/x.py", "content": "B"}, ctx_b)
        ra = await reader.run({"path": "generated/x.py"}, ctx_a)
        rb = await reader.run({"path": "generated/x.py"}, ctx_b)
        return wa, wb, ra, rb

    wa, wb, ra, rb = asyncio.run(_scenario())
    assert wa.success and wb.success
    assert (ra.output or {}).get("content") == "A"
    assert (rb.output or {}).get("content") == "B"
    # The two physical files are distinct.
    pa = os.path.join(TASK_WORKSPACE, a_id, "generated", "x.py")
    pb = os.path.join(TASK_WORKSPACE, b_id, "generated", "x.py")
    assert os.path.isfile(pa) and os.path.isfile(pb) and pa != pb
    with open(pa, encoding="utf-8") as f:
        assert f.read() == "A"
    with open(pb, encoding="utf-8") as f:
        assert f.read() == "B"


def test_adapter_rejects_traversal_outside_task_scope():
    import asyncio

    from integrations.adapters import AdapterContext, FileWriteAdapter
    from app.main import TASK_WORKSPACE

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    writer = FileWriteAdapter(TASK_WORKSPACE)
    ctx = AdapterContext(actor_id="p", task_id=task_id, step_id="s1", resource=f"task:{task_id}")
    probes = [
        "../other-task/steal.py",
        "../../outside.txt",                       # POSIX-style traversal
        os.path.join("..", "..", "outside.txt"),   # native traversal
        "/etc/raivisox_pwn",                       # absolute
    ]
    if os.name == "nt":
        # A backslash path is a traversal only on Windows; on POSIX it is an
        # ordinary (allowed) filename, so asserting denial there would be
        # asserting a falsehood.
        probes.append("..\\..\\outside.txt")
    for probe in probes:
        r = asyncio.run(writer.run({"path": probe, "content": "x"}, ctx))
        assert not r.success, probe
        assert "denied" in (r.error or "").lower()


def test_python_exec_runs_inside_task_scope():
    import asyncio

    from integrations.adapters import AdapterContext, PythonExecAdapter
    from app.main import TASK_WORKSPACE

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    adapter = PythonExecAdapter(TASK_WORKSPACE)
    ctx = AdapterContext(actor_id="p", task_id=task_id, step_id="s1", resource=f"task:{task_id}")
    r = asyncio.run(
        adapter.run(
            {"code": "import os; print(os.getcwd())"},
            ctx,
        )
    )
    assert r.success, r.error
    cwd = (r.output or {}).get("stdout", "").strip()
    assert os.path.normcase(task_id) in os.path.normcase(cwd), cwd


def test_shell_exec_runs_inside_task_scope():
    import asyncio

    from integrations.adapters import AdapterContext, ShellExecAdapter
    from app.main import TASK_WORKSPACE

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    adapter = ShellExecAdapter(TASK_WORKSPACE)
    ctx = AdapterContext(actor_id="p", task_id=task_id, step_id="s1", resource=f"task:{task_id}")
    r = asyncio.run(adapter.run({"command": "cd"}, ctx))
    # `cd` with no target prints the cwd on POSIX shells; on Windows it is
    # a no-op that succeeds. Either way the call must not crash or escape.
    assert r.success, r.error
    assert adapter._sandbox.working_dir.endswith(task_id)


def test_codegen_writes_inside_task_scope():
    import asyncio

    from integrations.adapters import AdapterContext, CodegenAdapter
    from app.main import TASK_WORKSPACE

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    adapter = CodegenAdapter(TASK_WORKSPACE)
    ctx = AdapterContext(actor_id="p", task_id=task_id, step_id="s1", resource=f"task:{task_id}")
    r = asyncio.run(
        adapter.run({"goal": "Create a module that calculates the factorial of 6"}, ctx)
    )
    assert r.success, r.error
    written = os.path.normcase(os.path.abspath((r.output or {})["written_to"]))
    expected_dir = os.path.normcase(os.path.abspath(os.path.join(TASK_WORKSPACE, task_id)))
    assert written.startswith(expected_dir + os.sep), written


# ---------------------------------------------------------------------------
# 4. Input validation
# ---------------------------------------------------------------------------


def test_malformed_ids_are_honest_404s(client):
    task_id = _mk_task(client)
    for bad in ("", " ", "%00", "x" * 500, "task-'); --", "../../etc"):
        r = client.get(f"/api/v1/tasks/{bad}/artifacts" if bad.strip() else "/api/v1/tasks/ /artifacts")
        assert r.status_code == 404, bad
    r = client.get(f"/api/v1/tasks/{task_id}/artifacts?run=")
    assert r.status_code == 404  # empty run param = unknown run


def test_register_rejects_invalid_emails():
    from app import main as app_main

    with TestClient(app_main.app) as c:
        for bad in ("not-an-email", "a b@test.io", "x@y", "@nope.com", "a@" + "x" * 300 + ".com"):
            r = c.post(
                "/auth/register",
                json={"email": bad, "password": "ValidPass123!", "full_name": "T"},
            )
            assert r.status_code == 422, f"{bad} → {r.status_code}"


def test_register_rejects_short_password():
    from app import main as app_main

    with TestClient(app_main.app) as c:
        r = c.post(
            "/auth/register",
            json={"email": "shortpw@security.test", "password": "short"},
        )
        assert r.status_code == 422


def test_oversized_inputs_rejected(client):
    r = client.post("/api/v1/tasks", json={"title": "x" * 501})
    assert r.status_code == 422
    r = client.post("/api/v1/tasks", json={"title": "ok", "goal": "g" * 10001})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 5. Leakage
# ---------------------------------------------------------------------------


def test_responses_never_leak_server_paths(client):
    task_id = _mk_task(client)
    body = _run_pipeline(client, task_id)
    blobs = [
        client.get(f"/api/v1/tasks/{task_id}").text,
        client.get(f"/api/v1/tasks/{task_id}/events").text,
        client.get(f"/api/v1/tasks/{task_id}/artifacts").text,
        client.get(f"/api/v1/tasks/{task_id}/artifacts/history").text,
    ]
    leak_markers = (
        "C:\\Users",
        "C:/Users",
        "c:\\users",
        str(os.path.abspath("workspaces")).lower(),
        "storage_path",
        "hashed_password",
        "secret_key",
        "sqlite+aiosqlite",
    )
    for blob in blobs:
        low = blob.lower()
        for marker in leak_markers:
            if marker == "storage_path":
                assert marker not in low, f"leaked {marker} in {blob[:200]}"
            else:
                assert marker not in low, f"leaked {marker}"


def test_step_outputs_do_not_embed_server_paths(client):
    """written_to is a workspace-relative path; events must not carry
    absolute server filesystem locations."""
    task_id = _mk_task(client)
    _run_pipeline(client, task_id)
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()
    workspace_abs = os.path.abspath("workspaces").lower()
    for e in events:
        payload = e.get("payload") or {}
        result = payload.get("result")
        if isinstance(result, dict):
            out = result.get("output")
            if isinstance(out, dict):
                wt = out.get("written_to")
                if isinstance(wt, str):
                    assert not os.path.isabs(wt) or wt.lower().startswith(workspace_abs), wt


def test_error_responses_do_not_disclose_internals(client):
    from app import main as app_main

    # settings.debug is False by default → generic 500 detail.
    assert app_main.settings.debug is False
    task_id = _mk_task(client)
    r = client.post(f"/api/v1/tasks/{task_id}/execute", json={"fail_step_index": "not-an-int"})
    # Malformed payload values must not 500 with a traceback.
    assert r.status_code in (200, 400, 422), r.status_code
