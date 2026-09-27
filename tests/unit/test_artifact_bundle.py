"""Deliverable Export UX: per-task ZIP bundle endpoint.

Everything runs against the REAL pipeline + REAL files on disk. The bundle
must contain the actual artifact bytes, sanitize archive entry names
(basename-only — traversal inside the ZIP is impossible), honestly report
missing files via MISSING.txt instead of silently omitting them, and enforce
exactly the same ownership as the individual artifact endpoints (no admin
bypass).
"""

import asyncio
import io
import os
import time
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "bundle_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "bundle.db"))
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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@bundle.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()
        c.close()


def _register_and_login(c: TestClient) -> dict:
    """A REAL second user through the real auth endpoints."""
    email = f"{uuid.uuid4().hex[:10]}@bundle.test"
    r = c.post("/auth/register", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code in (200, 201), r.text
    r = c.post("/auth/login", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mk_task(c, title="bundle flow"):
    r = c.post("/api/v1/tasks", json={"title": title, "goal": GOAL_FACTORIAL})
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


class TestBundleRealPipeline:
    def test_bundle_contains_real_bytes(self, client):
        """A completed real pipeline → ZIP with the actual bytes of every artifact."""
        c, _ = client
        task_id = _mk_task(c, title="bundle multi artifact")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"

        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        assert len(arts) >= 1, "expected at least one artifact from a completed pipeline"

        r = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert r.headers["content-disposition"].endswith('.zip"')

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.testzip() is None  # valid archive
        names = zf.namelist()
        # Every existing artifact is present by its (sanitized) filename;
        # no server paths, no traversal, no MISSING.txt when all is well.
        assert "MISSING.txt" not in names
        for a in arts:
            assert a["exists"]
            base = os.path.basename(a["filename"].replace("\\", "/"))
            assert base in names
            # Byte-level: ZIP entry == individual download == file on disk.
            entry = zf.read(base)
            individual = c.get(f"/api/v1/tasks/{task_id}/artifacts/{a['index']}").content
            assert entry == individual and len(entry) > 0

    def test_bundle_single_artifact_matches_individual_download(self, client):
        c, _ = client
        task_id = _mk_task(c, title="bundle single artifact")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"
        arts = [a for a in c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"] if a["exists"]]
        assert len(arts) >= 1

        r = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        base = os.path.basename(arts[0]["filename"].replace("\\", "/"))
        assert base in zf.namelist()
        assert zf.read(base) == c.get(f"/api/v1/tasks/{task_id}/artifacts/{arts[0]['index']}").content


class TestBundleHonesty:
    def test_bundle_no_artifacts_404(self, client):
        """Honest 404 for a task that produced nothing — not an empty ZIP."""
        c, _ = client
        task_id = _mk_task(c, title="bundle zero artifacts")
        r = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
        assert r.status_code == 404
        assert "no artifacts" in r.json()["detail"].lower()

    def test_bundle_unknown_task_404(self, client):
        c, _ = client
        r = c.get("/api/v1/tasks/does-not-exist/artifacts/bundle/download")
        assert r.status_code == 404

    def test_bundle_missing_artifact_recorded_not_omitted(self, client):
        """A recorded artifact deleted from disk → MISSING.txt names it; the
        other artifacts are still in the ZIP. Never a silent omission."""
        from app import main as m

        c, _ = client
        task_id = _mk_task(c, title="bundle missing file")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"

        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        victim = next(a for a in arts if a["exists"])

        # Resolve the true on-disk path server-side (the list endpoint
        # deliberately never exposes server paths to clients).
        async def _get_path():
            from app.database import async_session_factory

            async with async_session_factory() as db:
                t = await m.task_service.get_task_by_id(db, task_id, None)
                return {
                    a["filename"]: a["path"]
                    for a in await m._task_artifacts_internal(db, t)
                }

        paths = asyncio.run(_get_path())
        victim_path = paths[victim["filename"]]
        assert os.path.exists(victim_path)
        os.remove(victim_path)

        r = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "MISSING.txt" in zf.namelist()
        assert victim["filename"] in zf.read("MISSING.txt").decode()
        # Surviving artifacts are still present with real bytes.
        for a in arts:
            if a["filename"] == victim["filename"]:
                continue
            base = os.path.basename(a["filename"].replace("\\", "/"))
            if a["exists"] and os.path.exists(paths.get(a["filename"], "")):
                assert base in zf.namelist()


class TestBundleAuthorization:
    def test_bundle_is_owner_scoped(self, client):
        """Another user gets 404 for list, individual AND bundle — the same
        dependency-overridden user-swap the existing artifact tests use."""
        c, _ = client
        task_id = _mk_task(c, title="bundle ownership")
        other = type("U", (), {"id": "u-other-bundle", "email": "other@bundle.test"})()

        from app import main as app_main

        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: other
        try:
            for url in (
                f"/api/v1/tasks/{task_id}/artifacts",
                f"/api/v1/tasks/{task_id}/artifacts/0",
                f"/api/v1/tasks/{task_id}/artifacts/bundle/download",
            ):
                r = c.get(url)
                assert r.status_code == 404, (url, r.status_code)
        finally:
            app_main.app.dependency_overrides.clear()

    def test_admin_gets_no_artifact_bypass(self, client):
        """Admin authorization is CONSISTENT with the individual endpoint:
        no admin bypass — another user's artifacts are 404 for admins too,
        on both endpoints."""
        c, _ = client
        task_id = _mk_task(c, title="bundle admin consistency")
        admin = type("U", (), {"id": "u-admin", "email": "admin@bundle.test", "is_admin": True})()

        from app import main as app_main

        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: admin
        try:
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download").status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/0").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()


class TestBundleSanitization:
    def test_entry_names_are_basename_only_and_deduped(self):
        """Direct unit proof: traversal-style and duplicate names can never
        survive into the archive."""
        from app import main as m

        f1 = tmp_file("y.py", b"FIRST")
        f2 = tmp_file("y.py", b"SECOND")
        arts = [
            {"filename": "..\\..\\evil.py", "path": f1, "exists": True, "size": 5},
            {"filename": "sub/dir/y.py", "path": f2, "exists": True, "size": 6},
            {"filename": "y.py", "path": f2, "exists": True, "size": 6},
            {"filename": "gone.py", "path": "Z:/definitely/missing", "exists": False, "size": None},
        ]
        zf = zipfile.ZipFile(io.BytesIO(m._bundle_zip(arts)))
        names = zf.namelist()
        for n in names:
            assert "/" not in n and "\\" not in n and ".." not in n
        # gone.py was missing, so it is RECORDED in MISSING.txt — never
        # fabricated as an (empty) entry. Multi-entry: dedup + flatten.
        assert sorted(names) == ["MISSING.txt", "evil.py", "y.py", "y_2.py"]
        # 'evil.py' → f1 (FIRST). The FIRST 'y.py' entry in order is
        # 'sub/dir/y.py' (f2); the duplicate flattens to 'y_2.py' (also f2).
        assert zf.read("evil.py") == b"FIRST"
        assert zf.read("y.py") == b"SECOND"
        assert zf.read("y_2.py") == b"SECOND"
        assert zf.read("MISSING.txt").decode().count("gone.py") == 1


def tmp_file(name: str, data: bytes) -> str:
    import tempfile

    d = tempfile.mkdtemp(prefix="bundle_t_")
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    atexit.register(lambda: os.path.exists(p) and os.remove(p))
    return p
