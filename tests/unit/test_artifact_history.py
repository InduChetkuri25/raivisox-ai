"""Run-Scoped Artifact History: ?run= filtering, history grouping, supersession.

Everything runs the REAL pipeline against REAL files, then asserts the
persisted registry view:
- `?run=<run_id>` returns exactly that run's rows; unknown runs — including
  a run_id belonging to ANOTHER task — are an honest 404,
- /artifacts/history groups rows by run with factual persisted status,
- supersession chains keep history (never overwritten, exactly one current
  per physical path),
- downloads by stable artifact ID revalidate ownership/containment and
  404 honestly for deleted files,
- no storage_path or server paths ever appear in any response.
"""

import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "artifact_history_test")
os.makedirs(_TMP, exist_ok=True)

_DB_PATH = os.path.abspath(os.path.join(_TMP, "history.db"))
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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@hist.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()
        c.close()


def _mk_task(c, title="history flow"):
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
    email = f"{uuid.uuid4().hex[:10]}@hist.test"
    r = c.post("/auth/register", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code in (200, 201), r.text
    r = c.post("/auth/login", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _run_ids_of(c, task_id):
    h = c.get(f"/api/v1/tasks/{task_id}/artifacts/history").json()
    return [r["run_id"] for r in h["runs"]], h


class TestRunFiltering:
    def test_no_run_parameter_returns_current_view(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        body = c.get(f"/api/v1/tasks/{tid}/artifacts").json()
        assert body["task_id"] == tid
        assert body["artifacts"], "current view must list the run's current rows"
        assert all(a.get("run_id") is None for a in body["artifacts"])

    def test_valid_run_returns_only_that_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        runs, _ = _run_ids_of(c, tid)
        run_id = runs[0]
        body = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": run_id})
        assert body.status_code == 200
        arts = body.json()["artifacts"]
        assert arts, "the run that produced artifacts must list them"
        assert all(a["run_id"] == run_id for a in arts)
        # Within-run supersession (recovery rewrites) is allowed and honest:
        # superseded rows carry superseded_at; the final row per path is current.
        for a in arts:
            if a["is_current"] is False:
                assert a["superseded_at"]

    def test_unknown_run_is_honest_404(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        r = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": "no-such-run"})
        assert r.status_code == 404
        assert "Unknown run" in r.json()["detail"]

    def test_run_from_another_task_never_leaks(self, client):
        c, _ = client
        tid_a = _mk_task(c, "task A")
        tid_b = _mk_task(c, "task B")
        assert _run_to_completion(c, tid_a)["status"] == "completed"
        assert _run_to_completion(c, tid_b)["status"] == "completed"
        runs_a, _ = _run_ids_of(c, tid_a)
        r = c.get(f"/api/v1/tasks/{tid_b}/artifacts", params={"run": runs_a[0]})
        assert r.status_code == 404  # scoped by (task, run) — never task B rows

    def test_run_from_another_user_never_leaks(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        runs, _ = _run_ids_of(c, tid)
        # Dep-override stubs shadow real tokens (suite convention): assert the
        # other-user view by overriding with the OTHER user's stub.
        from app import main as app_main
        from app.auth import service as auth_service
        import app.tasks.router as tasks_router

        other = type("U", (), {"id": f"u-other-{uuid.uuid4().hex[:8]}", "email": "other@hist.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: other
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: other
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: other
        try:
            # The other user cannot see the task at all…
            assert c.get(f"/api/v1/tasks/{tid}").status_code == 404
            # …so the run filter also 404s (task-not-found dominates).
            r = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": runs[0]})
            assert r.status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()
            c.close()


class TestHistory:
    def test_multiple_runs_grouped_deterministically(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        assert len(h["runs"]) == 2
        started = [r["started_at"] for r in h["runs"]]
        assert started == sorted(started)  # deterministic run order

    def test_supersession_chain_exactly_one_current_per_path(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        from collections import defaultdict

        by_hash_or_name = defaultdict(list)
        for run in h["runs"]:
            for a in run["artifacts"]:
                by_hash_or_name[a["filename"]].append(a)
        for filename, rows in by_hash_or_name.items():
            current = [a for a in rows if a["is_current"]]
            assert len(current) == 1, f"{filename}: exactly one current row"
            # Historical rows exist and are marked superseded.
            superseded = [a for a in rows if not a["is_current"]]
            assert all(a["superseded_at"] for a in superseded) or not superseded

    def test_history_contains_only_factual_fields(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        raw = c.get(f"/api/v1/tasks/{tid}/artifacts/history").text
        assert "storage_path" not in raw
        assert "C:\\" not in raw and "workspaces" not in raw
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        run = h["runs"][0]
        assert run["artifact_count"] == len(run["artifacts"]) > 0
        a = run["artifacts"][0]
        for field in ("artifact_id", "filename", "size", "hash", "step_title", "created_at", "is_current"):
            assert field in a
        # First-Class Execution Runs: run context comes from the DURABLE
        # execution_runs row (authoritative), not timestamp inference.
        assert run["status"] in ("completed", "failed", "partially_completed", "cancelled", None)
        assert run["started_at"] is not None
        assert "is_legacy_run" in run
        assert run["is_legacy_run"] is False


class TestHistoricalDownloads:
    def test_download_by_artifact_id_returns_real_bytes(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        a = next(x for x in h["runs"][0]["artifacts"] if x["is_current"] and x["exists"])
        r = c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{a['artifact_id']}")
        assert r.status_code == 200
        assert len(r.content) == a["size"]
        import hashlib

        assert hashlib.sha256(r.content).hexdigest() == a["hash"]

    def test_download_unknown_artifact_id_404(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        r = c.get(f"/api/v1/tasks/{tid}/artifacts/rows/not-a-real-id")
        assert r.status_code == 404

    def test_superseded_row_never_serves_replaced_bytes(self, client):
        """A superseded row whose path now holds DIFFERENT bytes must not
        present those current bytes as its own — honest 404 with a clear
        reason (when the file actually changed)."""
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        from app.models.task_artifact import TaskArtifact

        async def _superseded_row():
            from app.database import async_session_factory
            from sqlalchemy import select

            async with async_session_factory() as db:
                rows = (
                    await db.execute(
                        select(TaskArtifact)
                        .where(TaskArtifact.task_id == tid, TaskArtifact.is_current == False)  # noqa: E712
                        .order_by(TaskArtifact.created_at.asc())
                    )
                ).scalars().all()
                for row in rows:
                    import os as _os

                    resolved = _os.path.realpath(row.storage_path)
                    if _os.path.isfile(resolved):
                        import hashlib as _h

                        with open(resolved, "rb") as fh:
                            cur = _h.sha256(fh.read()).hexdigest()
                        return row.id, row.sha256, cur
                return None, None, None

        aid, recorded, current = asyncio.run(_superseded_row())
        if aid is None:
            pytest.skip("no on-disk superseded row in this pipeline")
        r = c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{aid}")
        if recorded != current:
            assert r.status_code == 404
            assert "replaced" in r.json()["detail"].lower()
        else:
            # Path still holds the recorded bytes — download must succeed.
            assert r.status_code == 200

    def test_download_foreign_task_artifact_id_404(self, client):
        c, _ = client
        tid_a = _mk_task(c, "owner task")
        tid_b = _mk_task(c, "other task")
        assert _run_to_completion(c, tid_a)["status"] == "completed"
        assert _run_to_completion(c, tid_b)["status"] == "completed"
        ha = c.get(f"/api/v1/tasks/{tid_a}/artifacts/history").json()
        aid = ha["runs"][0]["artifacts"][0]["artifact_id"]
        r = c.get(f"/api/v1/tasks/{tid_b}/artifacts/rows/{aid}")
        assert r.status_code == 404  # row scoped to its own task

    def test_deleted_physical_file_metadata_stays_download_404s(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        a = next(x for x in h["runs"][0]["artifacts"] if x["exists"])
        from app.models.task_artifact import TaskArtifact

        async def _delete_file():
            from app.database import async_session_factory
            from sqlalchemy import select

            async with async_session_factory() as db:
                row = (await db.execute(select(TaskArtifact).where(TaskArtifact.id == a["artifact_id"]))).scalars().first()
                path = row.storage_path
            if os.path.isfile(path):
                os.remove(path)
            return path

        path = asyncio.run(_delete_file())
        assert path and not os.path.isfile(path)
        # Metadata survives with exists=false…
        h2 = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        a2 = next(x for run in h2["runs"] for x in run["artifacts"] if x["artifact_id"] == a["artifact_id"])
        assert a2["exists"] is False
        assert a2["hash"] == a["hash"] and a2["size"] == a["size"]
        # …and the download is an honest 404, never fabricated bytes.
        r = c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{a['artifact_id']}")
        assert r.status_code == 404
        assert "missing" in r.json()["detail"].lower()


class TestSecurity:
    def test_other_user_blocked_from_history_and_rows(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        aid = h["runs"][0]["artifacts"][0]["artifact_id"]
        # Stub-override convention: act as ANOTHER user (real JWTs are
        # shadowed by the owner stub registered in the fixture).
        from app import main as app_main
        from app.auth import service as auth_service
        import app.tasks.router as tasks_router

        other = type("U", (), {"id": f"u-other-{uuid.uuid4().hex[:8]}", "email": "other2@hist.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: other
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: other
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: other
        try:
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/history").status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{aid}").status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()
            c.close()

    def test_no_admin_bypass_on_history_or_rows(self, client):
        """Admin consistency: artifact authorization has never had an admin
        bypass; history/rows endpoints must not add one."""
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        aid = h["runs"][0]["artifacts"][0]["artifact_id"]

        from app import main as app_main
        from app.auth import service as auth_service
        import app.tasks.router as tasks_router

        admin = type("U", (), {"id": "admin-1", "email": "admin@hist.test"})()
        try:
            app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: admin
            app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: admin
            app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: admin
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/history").status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{aid}").status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()
            c.close()


class TestRegression:
    def test_existing_endpoints_still_work(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        # list (current view)
        lst = c.get(f"/api/v1/tasks/{tid}/artifacts").json()
        assert lst["artifacts"]
        # individual download by index (current rows)
        idx = next(i for i, a in enumerate(lst["artifacts"]) if a["exists"])
        r = c.get(f"/api/v1/tasks/{tid}/artifacts/{idx}")
        assert r.status_code == 200 and r.content
        # task ZIP
        z = c.get(f"/api/v1/tasks/{tid}/artifacts/bundle/download")
        assert z.status_code == 200 and z.content[:2] == b"PK"
