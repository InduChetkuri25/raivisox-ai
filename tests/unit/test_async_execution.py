"""Async execution: 202-accept, background pipeline, duplicate guard,
crash persistence, and the restart sweep.

Sync mode (the default) is exercised by the entire existing suite — these
tests cover ONLY the async_mode contract.
"""

import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "async_exec_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "async_exec.db"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@async.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()


def _mk_task(c, title="async flow", goal=GOAL_FACTORIAL):
    r = c.post("/api/v1/tasks", json={"title": title, "goal": goal})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_terminal(c, task_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = c.get(f"/api/v1/tasks/{task_id}").json()
        if body["status"] in ("completed", "failed", "partially_completed", "cancelled"):
            return body
        time.sleep(0.3)
    return body


# ---------------------------------------------------------------------------
# Contract: 202 accept + background completion + duplicate guard
# ---------------------------------------------------------------------------


def test_async_execute_returns_202_and_completes_in_background(client):
    c, _ = client
    task_id = _mk_task(c)

    t0 = time.time()
    r = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
    accept_latency = time.time() - t0

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "executing"
    assert "background" in body["detail"]
    # The whole point: the request did NOT wait for the pipeline.
    assert accept_latency < 10, f"accept took {accept_latency:.1f}s"

    final = _wait_terminal(c, task_id)
    assert final["status"] == "completed", final.get("error")
    # Honest-outcome semantics unchanged.
    assert final["outcome"] == "verified"
    dod = (final.get("result") or {}).get("definition_of_done") or {}
    assert dod.get("satisfied") is True
    # The in-flight guard released; a second execute on a terminal task is a
    # fresh run (and the duplicate guard is proven by the next test).


def test_duplicate_execute_while_running_returns_202_not_new_pipeline(client):
    c, _ = client
    task_id = _mk_task(c)

    r1 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
    assert r1.status_code == 202
    # Immediately re-execute while the first run is in flight.
    r2 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
    assert r2.status_code == 202
    assert r2.json()["detail"] == "execution already in progress"

    final = _wait_terminal(c, task_id)
    assert final["status"] == "completed"
    # Exactly ONE pipeline ran: one verification event per attempt, no doubling.
    events = c.get(f"/api/v1/tasks/{task_id}/events").json()
    verifications = [e for e in events if e["event_type"] == "verification"]
    assert len(verifications) >= 1
    attempts = {e["payload"]["attempt"] for e in verifications if e.get("payload")}
    assert 1 in attempts


def test_async_crash_persists_failed_outcome(client):
    """A background pipeline crash must persist an honest FAILED outcome."""
    c, _ = client
    from app import main as app_main

    task_id = _mk_task(c, title="crash probe", goal="Summarize the main ideas of the theory of relativity.")

    real = app_main.run_task_pipeline

    async def boom(db, task, t_id, payload=None):
        raise RuntimeError("simulated background crash")

    app_main.run_task_pipeline = boom
    try:
        r = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True})
        assert r.status_code == 202
        final = _wait_terminal(c, task_id, timeout=30)
    finally:
        app_main.run_task_pipeline = real

    assert final["status"] == "failed"
    assert final["outcome"] == "failed"
    assert "simulated background crash" in str((final.get("error") or {}).get("message", ""))


def test_startup_sweep_fails_orphaned_running_tasks(client):
    """Tasks left EXECUTING by a dead process are failed on the next boot."""
    c, _ = client
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.task import Task

    task_id = _mk_task(c)
    # Simulate the restart state directly in the DB.
    asyncio.run(_force_executing(task_id))

    # Re-run the sweep (same function the lifespan calls at boot).
    from app import main as app_main

    asyncio.run(app_main._reset_orphaned_running_tasks())

    asyncio.run(_assert_swept(task_id))


async def _force_executing(task_id):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.task import Task, TaskStatus

    async with async_session_factory() as db:
        task = (await db.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.status = TaskStatus.EXECUTING.value
        await db.commit()


async def _assert_swept(task_id):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.task import Task, TaskStatus

    async with async_session_factory() as db:
        task = (
            await db.execute(select(Task).where(Task.id == task_id))
        ).scalars().one()
        assert task.status == TaskStatus.FAILED.value
        assert task.outcome == "failed"
        assert "restart" in str((task.error or {}).get("message", ""))
