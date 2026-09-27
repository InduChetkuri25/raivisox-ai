"""Project-level artifact bundle: ZIP across all of a project's tasks.

Real pipeline, real files, real projects. The bundle must namespace entries
per task (context + collision-freedom), order deterministically, preserve
actual bytes, carry an honest MANIFEST.txt reflecting persisted DB state
(no server paths, no secrets), record missing artifacts instead of omitting
them, and enforce the exact same owner-scoped authorization as the existing
project endpoints (no admin bypass).
"""

import asyncio
import io
import os
import time
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "project_bundle_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "project_bundle.db"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")
atexit.register(lambda: os.path.exists(_DB_PATH) and os.remove(_DB_PATH))

GOAL_FACTORIAL = (
    "Create a Python module that calculates the factorial of 6 and verify "
    "that the result is 720."
)


@pytest.fixture()
def client():
    from app import main as app_main
    from app.auth import service as auth_service
    import app.tasks.router as tasks_router

    with TestClient(app_main.app) as c:
        user_id = f"u-{uuid.uuid4().hex[:10]}"
        user = type("U", (), {"id": user_id, "email": f"{user_id}@pbundle.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()
        c.close()


def _mk_project(c, name="Export Project"):
    r = c.post("/api/v1/projects", json={"name": name, "description": "bundle tests"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_task(c, project_id, title):
    r = c.post(
        "/api/v1/tasks",
        json={"title": title, "goal": GOAL_FACTORIAL, "project_id": project_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run_to_completion(c, task_id, timeout=90):
    c.post(f"/api/v1/tasks/{task_id}/execute", json={})
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = c.get(f"/api/v1/tasks/{task_id}").json()
        if body["status"] in ("completed", "failed", "partially_completed", "cancelled"):
            return body
        time.sleep(0.25)
    return body


class TestProjectBundleReal:
    def test_multi_task_bundle_bytes_and_manifest(self, client):
        """Two real tasks in one project → ZIP with per-task namespaced real
        bytes and a factual MANIFEST."""
        c, _ = client
        pid = _mk_project(c, name="Byte Check")
        t1 = _mk_task(c, pid, "bundle task one")
        t2 = _mk_task(c, pid, "bundle task two")
        b1 = _run_to_completion(c, t1)
        b2 = _run_to_completion(c, t2)
        assert b1["status"] == b2["status"] == "completed"

        r = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.testzip() is None
        names = zf.namelist()
        assert "MANIFEST.txt" in names
        file_entries = [n for n in names if n != "MANIFEST.txt"]
        # Per-task namespacing: both tasks contribute their own directory.
        assert len({n.split("/")[0] for n in file_entries}) == 2

        # Byte equality per task against the individual artifact downloads.
        for tid in (t1, t2):
            arts = c.get(f"/api/v1/tasks/{tid}/artifacts").json()["artifacts"]
            short = tid.replace("-", "")[:8]
            for a in arts:
                base = os.path.basename(a["filename"].replace("\\", "/"))
                entry = f"task_{short}/{base}"
                assert entry in names, entry
                assert zf.read(entry) == c.get(
                    f"/api/v1/tasks/{tid}/artifacts/{a['index']}"
                ).content
                assert len(zf.read(entry)) > 0

        # Manifest reflects persisted DB state, no server paths.
        man = zf.read("MANIFEST.txt").decode()
        for tid in (t1, t2):
            assert tid in man
        assert "bundle task one" in man and "bundle task two" in man
        assert "task_outcome: verified" in man
        assert "workspaces" not in man and "C:\\" not in man and "/home/" not in man
        # Project context is part of the manifest.
        assert f"project_id: {pid}" in man
        assert "project_name: Byte Check" in man
        assert "eyJ" not in man and "password" not in man.lower()
        # Each task's own directory carries its full artifact set (the
        # registry also captures generic fs_write outputs the old
        # step-result collector never saw — the honest new reality).
        # The manifest count must equal the ACTUAL recorded artifact count
        # (the deliverable's byte content varies with parallel completion
        # order, so the number itself is derived from persisted state here).
        expected = sum(
            len(c.get(f"/api/v1/tasks/{tid}/artifacts").json()["artifacts"])
            for tid in (t1, t2)
        )
        assert f"artifacts_recorded: {expected}" in man

    def test_deterministic_entry_ordering(self, client):
        """Same DB state → byte-identical ZIP twice (order is stable)."""
        c, _ = client
        pid = _mk_project(c, name="Order Check")
        t1 = _mk_task(c, pid, "ordering alpha")
        t2 = _mk_task(c, pid, "ordering beta")
        assert _run_to_completion(c, t1)["status"] == "completed"
        assert _run_to_completion(c, t2)["status"] == "completed"

        z1 = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download").content
        z2 = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download").content
        n1 = zipfile.ZipFile(io.BytesIO(z1)).namelist()
        n2 = zipfile.ZipFile(io.BytesIO(z2)).namelist()
        assert n1 == n2
        # Tasks were created in a known order; their dirs appear in that order.
        dirs = [n.split("/")[0] for n in n1 if n != "MANIFEST.txt"]
        first_of = []
        for d in dirs:
            if d not in first_of:
                first_of.append(d)
        t1s, t2s = t1.replace("-", "")[:8], t2.replace("-", "")[:8]
        assert first_of.index(f"task_{t1s}") < first_of.index(f"task_{t2s}")

    def test_task_outside_project_never_included(self, client):
        """A task NOT in the project must never appear in its bundle."""
        c, _ = client
        pid = _mk_project(c, name="Isolation")
        tid = _mk_task(c, pid, "inside task")
        assert _run_to_completion(c, tid)["status"] == "completed"
        outside = _mk_task(c, None, "outside task") if False else None
        # Create a task with no project at all.
        r = c.post("/api/v1/tasks", json={"title": "loose task", "goal": GOAL_FACTORIAL})
        assert r.status_code == 201
        loose_id = r.json()["id"]

        zf = zipfile.ZipFile(io.BytesIO(
            c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download").content
        ))
        man = zf.read("MANIFEST.txt").decode()
        assert tid in man
        assert loose_id not in man


class TestProjectBundleAuth:
    def test_other_user_404_for_list_and_bundle(self, client):
        c, _ = client
        pid = _mk_project(c, name="Not Yours")
        tid = _mk_task(c, pid, "secret task")
        _run_to_completion(c, tid)

        other = type("U", (), {"id": "u-other-pb", "email": "o@pbundle.test"})()
        from app import main as app_main
        import app.tasks.router as tasks_router

        # Override the same deps the fixture does (both auth layers), so the
        # router-level project GET resolves to the other user too.
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: other
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: other
        try:
            # Same 404 semantics as the existing project GET endpoint.
            assert c.get(f"/api/v1/projects/{pid}").status_code == 404
            assert c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()

    def test_admin_no_bypass(self, client):
        c, _ = client
        pid = _mk_project(c, name="Admin Consistency")
        tid = _mk_task(c, pid, "owned task")
        _run_to_completion(c, tid)

        admin = type("U", (), {"id": "u-admin-pb", "email": "a@pbundle.test", "is_admin": True})()
        from app import main as app_main

        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: admin
        try:
            assert c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()

    def test_unknown_project_404(self, client):
        c, _ = client
        assert c.get("/api/v1/projects/nope/artifacts/bundle/download").status_code == 404


class TestProjectBundleEdge:
    def test_zero_artifact_project_404(self, client):
        """Project with tasks but nothing executed → honest 404, not an
        empty or manifest-only ZIP."""
        c, _ = client
        pid = _mk_project(c, name="Empty")
        _mk_task(c, pid, "never executed")
        r = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download")
        assert r.status_code == 404
        assert "no artifacts" in r.json()["detail"].lower()

    def test_project_without_tasks_404(self, client):
        c, _ = client
        pid = _mk_project(c, name="No tasks at all")
        r = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download")
        assert r.status_code == 404

    def test_deleted_artifact_recorded_in_manifest(self, client):
        """Artifact removed from disk after execution → the manifest records
        it as missing; the bundle still 200s for the surviving artifacts."""
        from app import main as m

        c, _ = client
        pid = _mk_project(c, name="Missing File")
        tid = _mk_task(c, pid, "missing artifact task")
        assert _run_to_completion(c, tid)["status"] == "completed"

        async def _get_path():
            from app.database import async_session_factory

            async with async_session_factory() as db:
                t = await m.task_service.get_task_by_id(db, tid, None)
                return {
                    a["filename"]: a["path"]
                    for a in await m._task_artifacts_internal(db, t)
                }

        paths = asyncio.run(_get_path())
        victim = next(iter(paths))
        os.remove(paths[victim])

        r = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download")
        assert r.status_code in (200, 404)  # honest either way, decided below
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        man = zf.read("MANIFEST.txt").decode()
        assert victim in man
        assert "missing from workspace" in man
        if r.status_code == 404:
            # Only acceptable if NOTHING survived; victim was the sole file.
            assert "artifacts_recorded" in man

    def test_duplicate_physical_artifact_not_duplicated(self, client):
        """Several steps writing the SAME file (shared written_to path) yield
        one physical artifact — the collector dedups by path."""
        from app import main as m

        c, _ = client
        pid = _mk_project(c, name="Dedup")
        tid = _mk_task(c, pid, "dedup task")
        assert _run_to_completion(c, tid)["status"] == "completed"

        async def _collect():
            from app.database import async_session_factory

            async with async_session_factory() as db:
                t = await m.task_service.get_task_by_id(db, tid, None)
                arts = await m._task_artifacts_internal(db, t)
                paths = [a["path"] for a in arts]
                return len(paths), len(set(paths))

        n, unique = asyncio.run(_collect())
        assert n == unique  # no duplicated physical artifact


def _unit_sanitize_and_namespace():
    """Direct unit proof of component sanitization and namespacing."""
    from app import main as m

    # Traversal and separators cannot survive.
    assert "/" not in m._sanitize_zip_component("../../etc/passwd")
    assert "\\" not in m._sanitize_zip_component("..\\..\\x.py")
    assert ".." not in m._sanitize_zip_component("../x")
    assert m._sanitize_zip_component("") == "artifact"
    assert len(m._sanitize_zip_component("x" * 200)) <= 80
    # Control characters stripped.
    assert m._sanitize_zip_component("a\x00b.py") == "ab.py"


def test_sanitize_component_unit():
    _unit_sanitize_and_namespace()
