"""Cross-restart behavior for async execution.

Covers the startup sweep's interruption-stage derivation (from persisted
events only — never guessed), honest FAILED outcomes that preserve the
pre-restart event history, and the explicit opt-in resume re-run
(`{"resume_mode": true}`), which recomputes the pipeline honestly and can
never fake success.
"""

import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "cross_restart_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "cross_restart.db"))
# setdefault: other test modules own the effective DATABASE_URL (their raw
# sqlite reads depend on it). The sweep under test reads through the app's
# session factory, so it follows whichever DB the chain selected.
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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@restart.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()


def _mk_task(c, title="restart flow", goal=GOAL_FACTORIAL):
    r = c.post("/api/v1/tasks", json={"title": title, "goal": goal})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _db_file() -> str:
    """Filesystem path of the DB the app is actually using (env-resolved)."""
    url = os.environ.get("DATABASE_URL", "")
    prefix = "sqlite+aiosqlite:///"
    return url[len(prefix):] if url.startswith(prefix) else url


def _wait_terminal(c, task_id, timeout=90):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = c.get(f"/api/v1/tasks/{task_id}").json()
        if body["status"] in ("completed", "failed", "partially_completed", "cancelled"):
            return body
        time.sleep(0.25)
    return body


async def _force_status(task_id, status: str):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.task import Task

    async with async_session_factory() as db:
        task = (await db.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.status = status  # raw persisted value, e.g. "executing"
        await db.commit()


async def _get_row(task_id):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.task import Task

    async with async_session_factory() as db:
        task = (await db.execute(select(Task).where(Task.id == task_id))).scalars().one()
        await db.refresh(task)
        return task.status, task.outcome, (task.error or {}).get("message", "")


class TestInterruptionStage:
    def test_stage_names_in_flight_step_from_events(self, client):
        """step-1 completed, step-2 started but never finished → the sweep
        names the exact in-flight step, derived from persisted events."""
        c, _ = client
        from app import main as app_main

        task_id = _mk_task(c, title="stage in-flight")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
        body = _wait_terminal(c, task_id)
        assert body["status"] == "completed"

        # Simulate a crash mid-step-2 of a subsequent run: force EXECUTING
        # and seed a persisted step_started-without-completion event via
        # raw sqlite (the DB is the only cross-process evidence).
        import json as _json
        import sqlite3

        con = sqlite3.connect(_db_file())
        try:
            con.execute(
                "INSERT INTO task_events (id, task_id, event_type, payload, created_at, updated_at) "
                # +1s: SQLite's datetime('now') has second granularity while
                # the pipeline's own events carry microseconds, so a plain
                # 'now' can sort BEFORE the completions it must follow and the
                # sweep then reports "between steps" (a timing flake).
                "VALUES (?, ?, ?, ?, datetime('now', '+1 second'), datetime('now', '+1 second'))",
                (
                    str(uuid.uuid4()),
                    task_id,
                    "step_started",
                    _json.dumps({"step_id": "step-2", "title": "mid-run step", "status": "running"}),
                ),
            )
            con.commit()
        finally:
            con.close()
        asyncio.run(_force_status(task_id, "executing"))

        asyncio.run(app_main._reset_orphaned_running_tasks())
        status, outcome, message = asyncio.run(_get_row(task_id))
        assert status == "failed"
        assert outcome == "failed"
        assert "step-2" in message
        assert "server restart" in message

    def test_stage_recovery_and_verification(self, client):
        c, _ = client
        from app import main as app_main

        task_id = _mk_task(c, title="stage recovering")
        asyncio.run(_force_status(task_id, "recovering"))
        asyncio.run(app_main._reset_orphaned_running_tasks())
        _, _, message = asyncio.run(_get_row(task_id))
        assert "recovery" in message

        task_id2 = _mk_task(c, title="stage verifying")
        asyncio.run(_force_status(task_id2, "verifying"))
        asyncio.run(app_main._reset_orphaned_running_tasks())
        _, _, message2 = asyncio.run(_get_row(task_id2))
        assert "verification" in message2

    def test_stage_pipeline_start_when_no_step_events(self, client):
        c, _ = client
        from app import main as app_main

        task_id = _mk_task(c, title="stage start")
        asyncio.run(_force_status(task_id, "executing"))
        asyncio.run(app_main._reset_orphaned_running_tasks())
        status, outcome, message = asyncio.run(_get_row(task_id))
        assert status == "failed"
        assert outcome == "failed"
        assert "pipeline start" in message

    def test_sweep_preserves_pre_restart_event_history(self, client):
        """The honest failure ADDS to the timeline; it never rewrites it."""
        c, _ = client
        from app import main as app_main

        task_id = _mk_task(c, title="history preserved")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
        _wait_terminal(c, task_id)
        asyncio.run(_force_status(task_id, "executing"))
        before = c.get(f"/api/v1/tasks/{task_id}/events").json()
        asyncio.run(app_main._reset_orphaned_running_tasks())
        after = c.get(f"/api/v1/tasks/{task_id}/events").json()
        assert len(after) >= len(before)
        # All pre-restart events are still present (by id).
        before_ids = {e["id"] for e in before}
        after_ids = {e["id"] for e in after}
        assert before_ids <= after_ids
        # And a sweep error event was appended.
        assert any(
            e["event_type"] == "error" and (e["payload"] or {}).get("startup_sweep")
            for e in after
        )


class TestResumeMode:
    def test_resume_mode_reruns_pipeline_honestly(self, client):
        """A terminal task re-run with resume_mode executes a fresh honest
        pipeline (new step events) and reaches a real terminal outcome."""
        c, _ = client
        task_id = _mk_task(c, title="resume rerun")

        r1 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
        assert r1.status_code == 202
        first = _wait_terminal(c, task_id)
        assert first["status"] == "completed"
        first_verified = first["outcome"] == "verified"

        r2 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"resume_mode": True})
        assert r2.status_code == 202
        assert "resuming" in r2.json()["detail"]
        second = _wait_terminal(c, task_id)
        assert second["status"] in ("completed", "failed", "partially_completed")
        assert second["status"] == "completed"
        # Honest semantics: if the first run was verified, the rerun (same
        # deterministic pipeline) is verified again — never downgraded by
        # the resume path, never marked successful without real execution.
        assert second["outcome"] == ("verified" if first_verified else second["outcome"])

        # A second wave of REAL step transitions occurred: the resume run
        # emitted a fresh full pass of step_started events.
        events = c.get(f"/api/v1/tasks/{task_id}/events").json()
        starts = [e for e in events if e["event_type"] == "step_started"]
        per_pass = len(second["steps"]) // 2  # steps are re-persisted per run
        assert len(starts) >= 2 * per_pass  # two full passes

    def test_resume_mode_duplicate_guard(self, client):
        """A resume while a run is in flight returns 202, never a 2nd pipeline."""
        c, _ = client
        task_id = _mk_task(c, title="resume guard")
        r1 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
        assert r1.status_code == 202
        r2 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"resume_mode": True})
        assert r2.status_code == 202
        assert r2.json()["detail"] == "execution already in progress"
        _wait_terminal(c, task_id)

    def test_resume_after_swept_orphan_reaches_verified(self, client):
        """Full restart story: orphan → sweep fails it honestly → explicit
        resume re-runs the pipeline → genuinely verified outcome."""
        c, _ = client
        from app import main as app_main

        task_id = _mk_task(c, title="resume after restart")
        asyncio.run(_force_status(task_id, "executing"))
        asyncio.run(app_main._reset_orphaned_running_tasks())
        status, outcome, _ = asyncio.run(_get_row(task_id))
        assert status == "failed" and outcome == "failed"

        r = c.post(f"/api/v1/tasks/{task_id}/execute", json={"resume_mode": True})
        assert r.status_code == 202
        final = _wait_terminal(c, task_id)
        assert final["status"] == "completed"
        assert final["outcome"] == "verified"
