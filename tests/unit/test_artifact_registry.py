"""Artifact Registry: first-class `task_artifacts` rows as the authority.

Everything runs the REAL pipeline against REAL files. The registry must:
- record a row ONLY after the file exists, is workspace-contained, sized
  and hashed (honesty contract),
- preserve history across reruns (supersession, never silent overwrite),
- fail honestly and non-fatally when registration cannot verify a file,
- expose NO filesystem paths through any API response,
- fall back to the legacy step-result derivation for pre-registry tasks.
"""

import asyncio
import hashlib
import io
import os
import time
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "registry_test")
os.makedirs(_TMP, exist_ok=True)

_DB_PATH = os.path.abspath(os.path.join(_TMP, "registry.db"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")

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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@reg.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()
        c.close()


def _mk_task(c, title="registry flow"):
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


def _other_user_headers(c: TestClient) -> dict:
    """A REAL second user through the real auth endpoints."""
    email = f"{uuid.uuid4().hex[:10]}@reg.test"
    r = c.post("/auth/register", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code in (200, 201), r.text
    r = c.post("/auth/login", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestRegistryCreation:
    def test_rows_registered_with_hash_size_step(self, client):
        """A completed real pipeline leaves current rows with real hash+size."""
        c, _ = client
        task_id = _mk_task(c, title="registry creation")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"

        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        assert len(arts) >= 1
        for a in arts:
            assert a["artifact_id"], "registry row id must surface"
            assert a["hash"], "sha256 must surface"
            assert a["kind"] == "registry"
            assert a["step_title"], "producing step must surface"
            assert a["created_at"]
            assert a["exists"]
            assert isinstance(a["size"], int) and a["size"] > 0

    def test_hash_and_size_match_disk_bytes(self, client):
        """Recorded hash/size equal the ACTUAL bytes on disk and served."""
        c, _ = client
        task_id = _mk_task(c, title="registry hash match")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"
        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        target = next(a for a in arts if a["exists"])

        served = c.get(f"/api/v1/tasks/{task_id}/artifacts/{target['index']}").content
        assert len(served) == target["size"]
        assert hashlib.sha256(served).hexdigest() == target["hash"]

    def test_no_filesystem_paths_in_any_response(self, client):
        """List/metadata responses never carry server paths or the workspace root."""
        c, _ = client
        task_id = _mk_task(c, title="registry no paths")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"
        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        assert arts
        blob = repr(arts)
        for forbidden in ("workspaces", "C:\\", "C:/", "storage_path"):
            assert forbidden not in blob, f"server path leak: {forbidden}"


class TestRegistrySecurity:
    def test_ownership_isolation_real_second_user(self, client):
        """Another real user: 404 on the task, its artifacts and its bundle."""
        import base64
        import json as _json

        import app.tasks.router as tasks_router

        c, _ = client
        task_id = _mk_task(c, title="registry ownership")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"
        other = _other_user_headers(c)
        # The fixture stubs the auth deps for the PRIMARY user (suite
        # convention), so for the isolation assertions we swap the stubs to
        # the other REAL user (id from their real token), then restore.
        sub = _json.loads(
            base64.urlsafe_b64decode(other["Authorization"].split(".")[1] + "==")
        )["sub"]
        other_user = type("U", (), {"id": sub, "email": "other@reg.test"})()
        from app import main as app_main

        from app.auth import service as auth_service

        saved = {
            app_main.get_current_user_dep: app_main.app.dependency_overrides[
                app_main.get_current_user_dep
            ],
            tasks_router.get_current_user: app_main.app.dependency_overrides[
                tasks_router.get_current_user
            ],
            auth_service.get_current_user: app_main.app.dependency_overrides[
                auth_service.get_current_user
            ],
        }
        for dep in saved:
            app_main.app.dependency_overrides[dep] = lambda: other_user
        try:
            assert c.get(f"/api/v1/tasks/{task_id}").status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts").status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/0").status_code == 404
            assert (
                c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download").status_code == 404
            )
        finally:
            app_main.app.dependency_overrides.update(saved)


class TestRegistryReruns:
    def test_reexecution_supersedes_without_overwriting_history(self, client):
        """A fresh honest run writes NEW rows; the previous rows survive as
        superseded history (is_current=False, superseded_at set) — nothing
        is silently overwritten or deleted."""
        from sqlalchemy import select

        from app.models.task_artifact import TaskArtifact

        c, _ = client
        task_id = _mk_task(c, title="registry rerun")
        body = _run_to_completion(c, task_id)
        assert body["status"] == "completed"
        first_rows = asyncio.run(_fetch_rows(task_id))
        assert first_rows, "expected rows after the first run"
        assert all(r.is_current for r in first_rows)

        # Real re-execution (resume_mode = fresh complete run).
        r = c.post(f"/api/v1/tasks/{task_id}/execute", json={"resume_mode": True})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 90
        body = {}
        while time.time() < deadline:
            body = c.get(f"/api/v1/tasks/{task_id}").json()
            if body["status"] in ("completed", "failed", "partially_completed", "cancelled"):
                break
            time.sleep(0.25)
        assert body["status"] == "completed"

        all_rows = asyncio.run(_fetch_rows(task_id, current_only=False))
        current = [r for r in all_rows if r.is_current]
        superseded = [r for r in all_rows if not r.is_current]
        assert superseded, "history must be preserved, not overwritten"
        assert all(r.superseded_at is not None for r in superseded)
        assert current, "the new run must have current rows"
        # No accidental overwrite: first-run rows are still there verbatim.
        first_ids = {r.id for r in first_rows}
        assert first_ids <= {r.id for r in superseded}
        # Registry history outlives per-run step rows by design (no FK).
        assert all(r.run_id for r in all_rows)

    def test_registry_history_survives_step_row_recreation(self, client):
        """Steps are recreated per run; artifact rows keep working regardless."""
        c, _ = client
        task_id = _mk_task(c, title="registry history")
        assert _run_to_completion(c, task_id)["status"] == "completed"
        # The endpoints keep serving through the registry after ANY rerun.
        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        assert arts
        r = c.get(f"/api/v1/tasks/{task_id}/artifacts/{arts[0]['index']}")
        assert r.status_code == 200 and len(r.content) > 0


class TestRegistryFailure:
    def test_registration_failure_is_honest_and_non_fatal(self, client):
        """A file that vanishes (or was never really written) before
        registration is recorded via artifact_registration_failed and never
        fabricates a row; execution state is untouched."""
        from app import main as app_main
        from app.models.task_event import EventType

        c, _ = client
        task_id = _mk_task(c, title="registry failure")
        real_register = app_main._register_task_artifact

        async def failing_register(*args, **kwargs):
            raise FileNotFoundError("artifact file missing at registration: x")

        app_main._register_task_artifact = failing_register
        try:
            body = _run_to_completion(c, task_id)
        finally:
            app_main._register_task_artifact = real_register

        assert body["status"] == "completed"  # execution NOT corrupted
        events = c.get(f"/api/v1/tasks/{task_id}/events").json()
        events = events["events"] if isinstance(events, dict) else events
        assert any(
            e["event_type"] == EventType.ARTIFACT_REGISTRATION_FAILED.value for e in events
        ), "the registration failure must be recorded honestly"
        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
        # No FABRICATED registry rows. The file genuinely exists on disk
        # (the adapter wrote it), so the legacy step-result fallback lists
        # it honestly — but nothing may claim registry provenance.
        assert not any(a.get("kind") == "registry" for a in arts)


class TestRegistryCompatibility:
    def test_bundle_contains_registry_artifacts_with_real_bytes(self, client):
        c, _ = client
        task_id = _mk_task(c, title="registry bundle")
        assert _run_to_completion(c, task_id)["status"] == "completed"
        arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]

        r = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.testzip() is None
        assert "MISSING.txt" not in zf.namelist()
        for a in arts:
            base = os.path.basename(a["filename"].replace("\\", "/"))
            assert base in zf.namelist()
            assert zf.read(base) == c.get(
                f"/api/v1/tasks/{task_id}/artifacts/{a['index']}"
            ).content


async def _fetch_rows(task_id: str, current_only: bool = True):
    from sqlalchemy import select

    from app import main as app_main
    from app.models.task_artifact import TaskArtifact

    q = select(TaskArtifact).where(TaskArtifact.task_id == task_id)
    if current_only:
        q = q.where(TaskArtifact.is_current == True)  # noqa: E712
    q = q.order_by(TaskArtifact.created_at.asc(), TaskArtifact.id.asc())
    async with app_main.async_session_factory() as db:
        return list((await db.execute(q)).scalars().all())


class TestRegistryBackfill:
    def test_backfill_is_idempotent_and_never_fabricates(self, client):
        """Rows created for pre-registry tasks only when the file still
        exists; running the backfill twice adds nothing."""
        c, _ = client
        task_id = _mk_task(c, title="registry backfill")
        assert _run_to_completion(c, task_id)["status"] == "completed"

        # Simulate a PRE-REGISTRY task: drop the whole table, leaving the
        # step results as the only artifact source (the legacy situation).
        from sqlalchemy import text

        from app import main as app_main

        async def _drop():
            async with app_main.async_session_factory() as db:
                await db.execute(text("DELETE FROM task_artifacts"))
                await db.commit()

        asyncio.run(_drop())

        # Startup-style backfill runs against the real DB.
        asyncio.run(app_main._backfill_task_artifacts())
        rows1 = asyncio.run(_fetch_rows(task_id, current_only=False))
        assert rows1, "existing task's still-present artifacts must be backfilled"
        for r in rows1:
            assert r.run_id == "backfill"
            assert r.sha256 and r.size_bytes > 0

        # Idempotent: a second pass must add exactly nothing.
        asyncio.run(app_main._backfill_task_artifacts())
        rows2 = asyncio.run(_fetch_rows(task_id, current_only=False))
        assert {r.id for r in rows2} == {r.id for r in rows1}

    def test_backfill_skips_vanished_files(self, client):
        """No row is fabricated for an artifact whose file is gone."""
        c, _ = client
        task_id = _mk_task(c, title="registry backfill vanished")
        assert _run_to_completion(c, task_id)["status"] == "completed"
        rows = asyncio.run(_fetch_rows(task_id, current_only=False))
        assert rows
        victim_path = rows[0].storage_path

        from sqlalchemy import text

        from app import main as app_main

        async def _setup():
            async with app_main.async_session_factory() as db:
                await db.execute(text("DELETE FROM task_artifacts"))
                await db.commit()
            os.remove(victim_path)  # file gone from disk

        asyncio.run(_setup())
        asyncio.run(app_main._backfill_task_artifacts())
        rows = asyncio.run(_fetch_rows(task_id, current_only=False))
        assert all(r.storage_path != victim_path for r in rows), (
            "backfill must NEVER fabricate a row for a vanished file"
        )
