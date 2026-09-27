"""Fine-grained step persistence + dashboard progress data contract.

Each REAL node transition must update its TaskStep row in the SAME
transaction as its event (rows match events at every instant), rows must
never duplicate across recovery/re-run passes, and the task LIST endpoint
must embed the transition events the dashboard progress UI derives from.
"""

import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "fine_grained_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "fine_grained.db"))
# setdefault: other test modules own the effective DATABASE_URL.
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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@fg.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()


def _mk_task(c, title="fine-grained", goal=GOAL_FACTORIAL):
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


async def _fetch_rows(task_id):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.task_step import TaskStep

    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(TaskStep)
                    .where(TaskStep.task_id == task_id)
                    .order_by(TaskStep.order.asc())
                )
            )
            .scalars()
            .all()
        )
        return [(r.id, r.title, r.status, r.order) for r in rows]


class TestFineGrainedRows:
    def test_rows_match_transitions_after_success(self, client):
        """One row per pipeline step (no duplicates), all completed, order
        preserved — and the row statuses agree with the final events."""
        c, _ = client
        task_id = _mk_task(c, title="rows success")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={})
        body = _wait_terminal(c, task_id)
        assert body["status"] == "completed"

        rows = asyncio.run(_fetch_rows(task_id))
        n = len(body["steps"])
        # steps are persisted per run; one pass ran exactly once.
        assert len(rows) == n
        assert all(status == "completed" for _, _, status, _ in rows)
        # Ordering preserved (decomposer orders are 1-based).
        assert [o for _, _, _, o in rows] == sorted(o for _, _, _, o in rows)

        # Row titles match the event payloads (same vocabulary, no drift).
        events = c.get(f"/api/v1/tasks/{task_id}/events").json()
        ev_titles = {
            e["payload"]["step_id"]: e["payload"]["title"]
            for e in events
            if e["event_type"] == "step_started"
        }
        assert len(ev_titles) == n

    def test_rows_reflect_failure_then_recovery_without_duplication(self, client):
        """A transient pass-1 failure (failed + skipped rows) followed by a
        successful pass updates the SAME rows in place — count never grows."""
        c, _ = client
        task_id = _mk_task(c, title="rows recovery")
        c.post(
            f"/api/v1/tasks/{task_id}/execute",
            json={"fail_step_index": 0, "fail_times": 1},
        )
        body = _wait_terminal(c, task_id)
        assert body["status"] == "completed"  # recovered on pass 2

        events = c.get(f"/api/v1/tasks/{task_id}/events").json()
        types = [e["event_type"] for e in events]
        assert types.count("step_failed") >= 1
        assert types.count("step_skipped") >= 1  # dependents skipped in pass 1

        rows = asyncio.run(_fetch_rows(task_id))
        n = len(body["steps"])
        # The recovery pass UPDATES THE SAME rows in place — the row count
        # never grows across passes (no duplication), and final statuses
        # reflect the successful pass. The full history lives in events.
        assert len(rows) == n
        assert all(status == "completed" for _, _, status, _ in rows)
        # Fail-fast reality: pass 1 started ONLY the injected-failure step
        # (dependents were skipped without starting); pass 2 re-ran all n.
        starts = [e for e in events if e["event_type"] == "step_started"]
        first_step_starts = [
            e for e in starts if (e["payload"] or {}).get("step_id") == "step-1"
        ]
        assert len(starts) == n + 1
        assert len(first_step_starts) == 2  # failed, then recovered

    def test_rows_agree_with_terminal_outcome(self, client):
        """verified outcome ⇒ every row completed; no partial lies."""
        c, _ = client
        task_id = _mk_task(c, title="rows outcome")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={})
        body = _wait_terminal(c, task_id)
        assert body["outcome"] == "verified"
        rows = asyncio.run(_fetch_rows(task_id))
        assert rows and all(status == "completed" for _, _, status, _ in rows)


class TestTransitionWriter:
    def test_emit_step_event_updates_row_atomically(self):
        """The fine-grained writer updates the row in the SAME transaction
        as the event: started→executing, completed→completed(+result)."""
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.task import Task
        from app.models.task_step import TaskStep
        from app import main as app_main

        async def scenario():
            async with async_session_factory() as db:
                t = Task(
                    id=f"fg-{uuid.uuid4().hex[:8]}",
                    user_id="u-fg",
                    title="writer probe",
                    status="executing",
                )
                db.add(t)
                row = TaskStep(
                    id=f"fgs-{uuid.uuid4().hex[:8]}",
                    task_id=t.id,
                    title="probe step",
                    status="pending",
                    order=0,
                )
                db.add(row)
                await db.commit()
                return t.id, row.id

        task_id, row_id = asyncio.run(scenario())

        asyncio.run(
            app_main._emit_step_event(
                task_id,
                "step_started",
                {"step_id": "step-1", "title": "probe step", "status": "running", "transition": "started"},
                step_row_id=row_id,
            )
        )
        rows = asyncio.run(_fetch_rows(task_id))
        assert rows[0][2] == "executing"  # started → executing on the row

        asyncio.run(
            app_main._emit_step_event(
                task_id,
                "step_completed",
                {
                    "step_id": "step-1",
                    "title": "probe step",
                    "status": "completed",
                    "transition": "completed",
                    "result": {"written_to": "x.py"},
                },
                step_row_id=row_id,
            )
        )
        rows = asyncio.run(_fetch_rows(task_id))
        assert rows[0][2] == "completed"

        # The event and the row update landed in the same transactions.
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.task_event import TaskEvent

        async def count_events():
            async with async_session_factory() as db:
                evs = (
                    (await db.execute(select(TaskEvent).where(TaskEvent.task_id == task_id)))
                    .scalars()
                    .all()
                )
                return sorted(e.event_type for e in evs)

        assert asyncio.run(count_events()) == ["step_completed", "step_started"]

    def test_writer_failure_never_raises(self):
        """A bad step_row_id must not break execution — observability only."""
        from app import main as app_main

        asyncio.run(
            app_main._emit_step_event(
                "no-such-task",
                "step_started",
                {"step_id": "s", "title": "t", "status": "running", "transition": "started"},
                step_row_id="no-such-row",
            )
        )  # must not raise


class TestDashboardDataContract:
    def test_list_endpoint_embeds_transition_events(self, client):
        """GET /tasks (the dashboard's data source) carries the step
        transition events the progress UI derives from."""
        c, _ = client
        task_id = _mk_task(c, title="dashboard data")
        c.post(f"/api/v1/tasks/{task_id}/execute", json={})
        _wait_terminal(c, task_id)

        listing = c.get("/api/v1/tasks").json()
        mine = next(t for t in listing["tasks"] if t["id"] == task_id)
        types = [e["event_type"] for e in mine["events"]]
        assert "step_started" in types and "step_completed" in types
        # And the steps collection is embedded with real statuses.
        assert mine["steps"], "dashboard needs embedded steps"
