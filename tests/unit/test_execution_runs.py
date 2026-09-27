"""First-Class Execution Runs: durable execution_runs rows as the authoritative
run boundary for artifact history and event timelines.

Everything runs the REAL pipeline against REAL files. Asserted here:
- exactly ONE execution_run row per execute invocation (success, failure,
  completed_unverified; async mode included; reruns create their own rows),
- terminal fields (finished_at, status, outcome) are written atomically and
  exactly once — never duplicated, never left running,
- artifacts and NEW events reference the real run id (legacy rows untouched),
- ?run= validates (task_id, run_id) — foreign task/user runs are honest 404s,
- history is deterministic, legacy rows keep honest legacy representation,
- restart (orphan sweep) closes interrupted runs, and
- no storage_path or server paths ever leak.
"""

import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "execution_runs_test")
os.makedirs(_TMP, exist_ok=True)

_DB_PATH = os.path.abspath(os.path.join(_TMP, "runs.db"))
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
        user = type("U", (), {"id": user_id, "email": f"{user_id}@runs.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()
        c.close()


def _mk_task(c, title="execution runs flow", goal=GOAL_FACTORIAL):
    r = c.post("/api/v1/tasks", json={"title": title, "goal": goal})
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


def _run_rows(task_id):
    """Raw ExecutionRun rows for a task (durability asserted at DB level)."""
    from sqlalchemy import select

    from app.models.execution_run import ExecutionRun

    async def _q():
        from app.database import async_session_factory

        async with async_session_factory() as db:
            return list(
                (
                    await db.execute(
                        select(ExecutionRun)
                        .where(ExecutionRun.task_id == task_id)
                        .order_by(ExecutionRun.started_at.asc())
                    )
                ).scalars().all()
            )

    return asyncio.run(_q())


def _event_rows(task_id):
    from sqlalchemy import select

    from app.models.task_event import TaskEvent

    async def _q():
        from app.database import async_session_factory

        async with async_session_factory() as db:
            return list(
                (
                    await db.execute(
                        select(TaskEvent)
                        .where(TaskEvent.task_id == task_id)
                        .order_by(TaskEvent.created_at.asc())
                    )
                ).scalars().all()
            )

    return asyncio.run(_q())


def _artifact_rows(task_id):
    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    async def _q():
        from app.database import async_session_factory

        async with async_session_factory() as db:
            return list(
                (
                    await db.execute(
                        select(TaskArtifact)
                        .where(TaskArtifact.task_id == task_id)
                        .order_by(TaskArtifact.created_at.asc())
                    )
                ).scalars().all()
            )

    return asyncio.run(_q())


def _assert_one_terminal_run(task_id, *, status, outcome=None):
    rows = _run_rows(task_id)
    assert len(rows) == 1, f"exactly one run per execution, got {len(rows)}"
    run = rows[0]
    assert run.finished_at is not None, "terminal run must be finished"
    assert run.status == status
    if outcome is not None:
        assert run.outcome == outcome
    assert run.started_at is not None
    return run


class TestOneRunPerExecution:
    def test_successful_execution_creates_exactly_one_completed_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        run = _assert_one_terminal_run(tid, status="completed")
        assert run.outcome, "completed run must record its outcome"
        # Artifact registry rows reference the REAL run id (not a hex stub).
        arts = _artifact_rows(tid)
        assert arts, "factorial pipeline must register artifacts"
        assert all(a.run_id == run.id for a in arts)

    def test_rerun_creates_its_own_run_row(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        rows = _run_rows(tid)
        assert len(rows) == 2, "each execute invocation gets its own run row"
        assert rows[0].id != rows[1].id
        assert all(r.finished_at is not None and r.status == "completed" for r in rows)

    def test_failed_execution_creates_exactly_one_failed_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        # A permanently failing step: fail it on EVERY attempt so the
        # recovery loop exhausts honestly → final FAILED.
        r = c.post(
            f"/api/v1/tasks/{tid}/execute",
            json={"fail_step_index": 0, "fail_times": 99},
        )
        assert r.status_code == 200, r.text
        deadline = time.time() + 150
        body = c.get(f"/api/v1/tasks/{tid}").json()
        while time.time() < deadline and body["status"] not in (
            "completed", "failed", "partially_completed", "cancelled",
        ):
            time.sleep(0.25)
            body = c.get(f"/api/v1/tasks/{tid}").json()
        assert body["status"] == "failed"
        rows = _run_rows(tid)
        assert len(rows) == 1, "a whole pipeline (with all its recovery passes) is ONE run"
        assert rows[0].status == "failed" and rows[0].finished_at is not None
        assert rows[0].outcome == "failed"

    def test_completed_unverified_run_records_honest_outcome(self, client):
        c, _ = client
        # DoD-abstaining goal: execution completes cleanly, but no DoD
        # checks apply → the honest outcome is completed_unverified.
        tid = _mk_task(
            c,
            "Summarize relativity",
            goal="Summarize the main ideas of the theory of relativity.",
        )
        body = _run_to_completion(c, tid)
        assert body["status"] == "completed"
        assert body["outcome"] == "completed_unverified"
        rows = _run_rows(tid)
        assert len(rows) == 1
        assert rows[0].status == "completed"
        assert rows[0].outcome == "completed_unverified"
        assert rows[0].finished_at is not None

    def test_async_mode_also_persists_one_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        r = c.post(f"/api/v1/tasks/{tid}/execute", json={"async_mode": True})
        assert r.status_code == 202
        deadline = time.time() + 120
        body = c.get(f"/api/v1/tasks/{tid}").json()
        while time.time() < deadline and body["status"] not in (
            "completed", "failed", "partially_completed", "cancelled",
        ):
            time.sleep(0.25)
            body = c.get(f"/api/v1/tasks/{tid}").json()
        assert body["status"] == "completed"
        _assert_one_terminal_run(tid, status="completed")
        # The 202 duplicate guard must never have started a second pipeline:
        rows = _run_rows(tid)
        assert len(rows) == 1


class TestRunRelations:
    def test_new_events_carry_execution_run_id(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        rows = _run_rows(tid)
        run_id = rows[0].id
        events = _event_rows(tid)
        # The task-creation "created" event predates any run and must stay
        # unassociated; every pipeline event AFTER the run row must carry it.
        pipeline_events = [e for e in events if e.event_type != "created"]
        assert pipeline_events, "pipeline must emit events"
        unassociated = [e for e in pipeline_events if e.execution_run_id is None]
        assert not unassociated, (
            f"new pipeline events must reference their run: "
            f"{[(e.event_type, e.payload) for e in unassociated[:3]]}"
        )
        assert all(e.execution_run_id == run_id for e in pipeline_events)

    def test_created_event_is_never_fabricated_onto_a_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        created = [e for e in _event_rows(tid) if e.event_type == "created"]
        assert created and all(e.execution_run_id is None for e in created)

    def test_artifact_run_ids_reference_real_runs(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        run_ids = {r.id for r in _run_rows(tid)}
        for a in _artifact_rows(tid):
            assert a.run_id in run_ids, "artifact rows must reference the real run row"

    def test_supersession_scoped_across_runs_with_exactly_one_current(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        from collections import defaultdict

        by_path = defaultdict(list)
        for a in _artifact_rows(tid):
            by_path[a.storage_path].append(a)
        for path, rows in by_path.items():
            current = [r for r in rows if r.is_current]
            assert len(current) == 1, f"{path}: exactly one current row"
            for r in rows:
                if not r.is_current:
                    assert r.superseded_at is not None


class TestHistoryOnExecutionRuns:
    def test_history_reports_durable_run_fields(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        assert len(h["runs"]) == 1
        run = h["runs"][0]
        db_rows = _run_rows(tid)
        assert run["run_id"] == db_rows[0].id
        assert run["status"] == "completed"
        assert run["outcome"] == db_rows[0].outcome
        assert run["started_at"] is not None and run["finished_at"] is not None
        assert run["is_legacy_run"] is False
        assert run["artifact_count"] == len(run["artifacts"]) > 0
        assert run["current_count"] >= 1
        started = [r["started_at"] for r in h["runs"]]
        assert started == sorted(started)

    def test_history_ordering_multiple_runs(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        assert _run_to_completion(c, tid)["status"] in ("completed", "partially_completed")
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        assert len(h["runs"]) == 2
        ids = [r["run_id"] for r in h["runs"]]
        db_ids = [r.id for r in _run_rows(tid)]
        assert ids == db_ids, "history order must match durable run order"

    def test_legacy_artifact_group_is_represented_not_fabricated(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        # Simulate a pre-Execution-Runs registry row: hex run_id with no
        # matching execution_runs row. It must appear as an honest legacy
        # group — never folded into a fabricated run row.
        from datetime import datetime

        from app.models.task_artifact import TaskArtifact

        async def _insert():
            from app.database import async_session_factory

            async with async_session_factory() as db:
                db.add(
                    TaskArtifact(
                        task_id=tid,
                        run_id=_hex_id(),
                        filename="legacy_output.md",
                        storage_path=os.path.abspath(
                            os.path.join("workspaces", "tasks", "legacy_missing.md")
                        ),
                        size_bytes=3,
                        content_type="text/markdown",
                        sha256="a" * 64,
                        is_current=True,
                        created_at=datetime.utcnow(),
                    )
                )
                await db.commit()

        def _hex_id():
            return uuid.uuid4().hex[:16]

        asyncio.run(_insert())
        h = c.get(f"/api/v1/tasks/{tid}/artifacts/history").json()
        legacy = [r for r in h["runs"] if r["run_id"] not in {x.id for x in _run_rows(tid)}]
        assert len(legacy) == 1
        lr = legacy[0]
        assert lr["is_legacy_run"] is True
        assert lr["status"] is None and lr["outcome"] is None
        assert lr["finished_at"] is None
        assert lr["artifact_count"] == 1
        real = [r for r in h["runs"] if r["is_legacy_run"] is False]
        assert real and all(r["is_legacy_run"] is False for r in real)

    def test_history_never_leaks_paths(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        raw = c.get(f"/api/v1/tasks/{tid}/artifacts/history").text
        assert "storage_path" not in raw
        assert "workspaces" not in raw and "C:\\" not in raw


class TestRunFilterValidation:
    def test_valid_run_scoped_by_task_and_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        run_id = _run_rows(tid)[0].id
        body = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": run_id})
        assert body.status_code == 200
        arts = body.json()["artifacts"]
        assert arts and all(a["run_id"] == run_id for a in arts)

    def test_unknown_run_404(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        r = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": "no-such-run"})
        assert r.status_code == 404

    def test_foreign_task_run_404(self, client):
        c, _ = client
        tid_a = _mk_task(c, "run task A")
        tid_b = _mk_task(c, "run task B")
        assert _run_to_completion(c, tid_a)["status"] == "completed"
        assert _run_to_completion(c, tid_b)["status"] == "completed"
        foreign = _run_rows(tid_a)[0].id
        r = c.get(f"/api/v1/tasks/{tid_b}/artifacts", params={"run": foreign})
        assert r.status_code == 404

    def test_foreign_user_run_404(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        run_id = _run_rows(tid)[0].id
        from app import main as app_main
        from app.auth import service as auth_service
        import app.tasks.router as tasks_router

        other = type(
            "U", (), {"id": f"u-other-{uuid.uuid4().hex[:8]}", "email": "o@runs.test"}
        )()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: other
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: other
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: other
        try:
            # The other user cannot even see the task…
            assert c.get(f"/api/v1/tasks/{tid}").status_code == 404
            # …so the run filter is an honest 404 too.
            r = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": run_id})
            assert r.status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/history").status_code == 404
        finally:
            app_main.app.dependency_overrides.clear()
            c.close()


class TestConcurrency:
    def test_parallel_executions_on_distinct_tasks_get_distinct_runs(self, client):
        c, _ = client
        tids = [_mk_task(c, f"parallel run {i}") for i in range(3)]

        def _drive(tid):
            return _run_to_completion(c, tid, timeout=150)

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(_drive, tid) for tid in tids]
            results = [f.result() for f in futures]
        assert all(r["status"] == "completed" for r in results)
        all_run_ids = []
        for tid in tids:
            rows = _run_rows(tid)
            assert len(rows) == 1, f"{tid}: one run per execution under concurrency"
            assert rows[0].finished_at is not None
            all_run_ids.append(rows[0].id)
        assert len(set(all_run_ids)) == 3, "concurrent executions must not share a run"


class TestRestartBehavior:
    def test_startup_sweep_closes_orphaned_running_run(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        # Simulate a crash: a run row left running + task stuck EXECUTING.
        from datetime import datetime

        from app.models.execution_run import ExecutionRun

        async def _orphan():
            from app.database import async_session_factory

            async with async_session_factory() as db:
                db.add(
                    ExecutionRun(
                        task_id=tid,
                        started_at=datetime.utcnow(),
                        status="running",
                    )
                )
                await db.commit()

        asyncio.run(_orphan())
        from app.main import _reset_orphaned_running_tasks

        from sqlalchemy import update

        from app.models.task import Task

        async def _stick():
            from app.database import async_session_factory

            async with async_session_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == tid)
                    .values(status="executing", completed_at=None)
                )
                await db.commit()

        asyncio.run(_stick())
        asyncio.run(_reset_orphaned_running_tasks())
        rows = _run_rows(tid)
        running = [r for r in rows if r.status == "running"]
        assert not running, "no execution run may survive the sweep as running"
        swept = [r for r in rows if r.error and "interrupted" in (r.error or "")]
        assert swept, "the orphaned run must be closed as failed by the sweep"
        assert all(
            r.finished_at is not None and r.status == "failed" for r in swept
        )


class TestNoPathLeakage:
    def test_run_filter_response_never_exposes_paths(self, client):
        c, _ = client
        tid = _mk_task(c)
        assert _run_to_completion(c, tid)["status"] == "completed"
        run_id = _run_rows(tid)[0].id
        raw = c.get(f"/api/v1/tasks/{tid}/artifacts", params={"run": run_id}).text
        assert "storage_path" not in raw and "workspaces" not in raw
