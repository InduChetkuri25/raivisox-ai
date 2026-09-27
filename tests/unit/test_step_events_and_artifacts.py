"""Live step progress + artifact endpoints.

Step events must correspond to REAL executor transitions: for a task with N
steps the persisted timeline must contain exactly N step_started and (on
success) N step_completed events, plus phase markers for verification and
recovery. Artifact endpoints must derive from real persisted step results,
be owner-scoped, and never leak server paths.
"""

import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "step_events_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "step_events.db"))
# setdefault: other test modules own the effective DATABASE_URL (their raw
# sqlite reads depend on it). This module is API-driven, so it safely runs
# against whichever DB the import chain already selected.
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")
atexit.register(lambda: os.path.exists(_DB_PATH) and os.remove(_DB_PATH))

GOAL_FACTORIAL = (
    "Create a Python module that calculates the factorial of 6 and verify "
    "that the result is 720."
)


@pytest.fixture()
def client():
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    with TestClient(app_main.app) as c:
        user_id = f"u-{uuid.uuid4().hex[:10]}"
        user = type("U", (), {"id": user_id, "email": f"{user_id}@events.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()


def _mk_task(c, title="events flow", goal=GOAL_FACTORIAL):
    r = c.post("/api/v1/tasks", json={"title": title, "goal": goal})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_terminal(c, task_id, timeout=90):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = c.get(f"/api/v1/tasks/{task_id}").json()
        if body["status"] in ("completed", "failed", "partially_completed", "cancelled"):
            return body
        time.sleep(0.25)
    return body


class TestStepEvents:
    def test_step_transitions_are_persisted_for_every_node(self, client):
        """Every step gets a real step_started + step_completed event pair."""
        c, _ = client
        task_id = _mk_task(c, title="step events happy path")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={})
        body = _wait_terminal(c, task_id)
        assert body["status"] == "completed", body.get("result", {}).get("message")

        events = c.get(f"/api/v1/tasks/{task_id}/events").json()
        types = [e["event_type"] for e in events]
        n_steps = len(body["steps"])

        assert types.count("step_started") == n_steps
        assert types.count("step_completed") == n_steps
        assert types.count("verification") >= 2  # started phase + result(s)

        # started/completed counts must match per step — no phantom progress
        started_ids = {
            e["payload"]["step_id"] for e in events if e["event_type"] == "step_started"
        }
        completed_ids = {
            e["payload"]["step_id"] for e in events if e["event_type"] == "step_completed"
        }
        assert started_ids == completed_ids == {f"step-{i+1}" for i in range(n_steps)}

        # Each started event carries the real node title and status.
        for e in events:
            if e["event_type"] in ("step_started", "step_completed"):
                assert e["payload"]["title"]
                assert e["payload"]["status"] in ("running", "completed", "failed", "pending")

    def test_step_failed_and_skipped_events_on_injected_failure(self, client):
        """A transient step failure yields step_failed, recovery markers, and
        skipped events for dependents — matching what really executed."""
        c, _ = client
        task_id = _mk_task(c, title="step events failure path")
        r = c.post(
            f"/api/v1/tasks/{task_id}/execute",
            json={"fail_step_index": 0, "fail_times": 1},
        )
        assert r.status_code == 200, r.text
        body = _wait_terminal(c, task_id)

        events = c.get(f"/api/v1/tasks/{task_id}/events").json()
        types = [e["event_type"] for e in events]

        assert types.count("step_failed") >= 1
        assert "recovery_started" in types
        assert "recovery_completed" in types
        # After the successful retry the task is honestly completed.
        assert body["status"] == "completed"

        # The failed event carries the real injected error message.
        failed = [e for e in events if e["event_type"] == "step_failed"]
        assert any("Injected transient failure" in (e["payload"].get("error") or "") for e in failed)


class TestArtifacts:
    def test_list_and_download_after_real_pipeline(self, client):
        """A real pipeline execution produces a real, downloadable artifact."""
        c, _ = client
        task_id = _mk_task(c, title="artifacts happy path")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={})
        body = _wait_terminal(c, task_id)
        assert body["status"] == "completed"

        r = c.get(f"/api/v1/tasks/{task_id}/artifacts")
        assert r.status_code == 200, r.text
        arts = r.json()["artifacts"]
        assert arts, "expected at least one artifact from a completed pipeline"
        # Generated .py modules and/or the finalize deliverable must be listed.
        assert any(a["exists"] and a["size"] and a["size"] > 0 for a in arts)
        # No server paths may leak in the listing.
        for a in arts:
            assert "path" not in a
            assert "workspaces" not in (a.get("filename") or "")

        # Download index 0 — must be real bytes with a filename header.
        d = c.get(f"/api/v1/tasks/{task_id}/artifacts/0")
        assert d.status_code == 200
        assert len(d.content) > 0
        assert "filename" in d.headers.get("content-disposition", "")

    def test_empty_artifact_list_for_task_without_execution(self, client):
        c, _ = client
        task_id = _mk_task(c, title="artifacts empty")
        r = c.get(f"/api/v1/tasks/{task_id}/artifacts")
        assert r.status_code == 200
        assert r.json() == {"task_id": task_id, "artifacts": []}

    def test_download_out_of_range_index_404(self, client):
        c, _ = client
        task_id = _mk_task(c, title="artifacts bad index")
        assert c.get(f"/api/v1/tasks/{task_id}/artifacts/99").status_code == 404
        # Negative indexes are integers — they resolve through the bounds
        # check to an honest 404 (never wraps around to a real artifact).
        assert c.get(f"/api/v1/tasks/{task_id}/artifacts/-1").status_code == 404
        assert c.get(f"/api/v1/tasks/{task_id}/artifacts/notanumber").status_code == 422

    def test_artifacts_are_owner_scoped(self, client):
        """Another user must get 404 for someone else's task artifacts."""
        c, _ = client
        task_id = _mk_task(c, title="artifacts ownership")
        other = type("U", (), {"id": "u-other", "email": "other@events.test"})()
        c.get(f"/api/v1/tasks/{task_id}/artifacts")
        # Simulate the ownership check by overriding the resolved user.
        from app import main as app_main

        token_user = other
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: token_user
        try:
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts")
            assert r.status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/0").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()
            c.close()
