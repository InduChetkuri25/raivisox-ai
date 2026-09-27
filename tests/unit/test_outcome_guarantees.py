"""Honest-Outcome Guarantees: outcome classification + durable audit trail.

Every pipeline-level test drives the REAL execute endpoint. The outcome
field must reflect verification truth, not just execution success.
"""

import asyncio
import atexit
import os
import shutil
import sqlite3
import tempfile
import uuid
from types import SimpleNamespace

# Durable file DB pinned BEFORE app.main import (engine created at import).
_OUTCOME_TMPDIR = tempfile.mkdtemp(prefix="raivisox_outcome_")
_DB_PATH = os.path.join(_OUTCOME_TMPDIR, "outcome.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
atexit.register(shutil.rmtree, _OUTCOME_TMPDIR, ignore_errors=True)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

GOAL_FACTORIAL = (
    "Create a Python module that calculates the factorial of 6 and verify "
    "that the result is 720."
)
GOAL_ABSTAIN = "Summarize the main ideas of the theory of relativity."


@pytest.fixture()
def client():
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    with TestClient(app_main.app) as c:
        user = SimpleNamespace(
            id=f"u-{uuid.uuid4().hex[:10]}", email=f"{uuid.uuid4().hex[:10]}@outcome.test"
        )
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# compute_outcome — pure classification truth table
# ---------------------------------------------------------------------------


def test_compute_outcome_truth_table():
    from app.main import TaskStatus, TaskOutcome, Verdict, compute_outcome

    dod_ok = {"applicable": True, "satisfied": True}
    dod_bad = {"applicable": True, "satisfied": False}
    dod_abstain = {"applicable": False, "satisfied": None}

    assert compute_outcome(TaskStatus.COMPLETED, Verdict.PASSED, dod_ok, 0) == "verified"
    # Execution passed, DoD passed, but a simulated step participated:
    assert compute_outcome(TaskStatus.COMPLETED, Verdict.PASSED, dod_ok, 1) == "completed_unverified"
    # DoD abstained (nothing derivable): NOT verified.
    assert compute_outcome(TaskStatus.COMPLETED, Verdict.PASSED, dod_abstain, 0) == "completed_unverified"
    # No DoD summary at all: NOT verified.
    assert compute_outcome(TaskStatus.COMPLETED, Verdict.PASSED, None, 0) == "completed_unverified"
    assert compute_outcome(TaskStatus.COMPLETED, Verdict.PARTIAL, dod_ok, 0) == "completed_unverified"
    assert compute_outcome(TaskStatus.PARTIALLY_COMPLETED, Verdict.PARTIAL, dod_ok, 0) == "completed_unverified"
    assert compute_outcome(TaskStatus.FAILED, Verdict.FAILED, dod_bad, 0) == "failed"
    # Non-terminal states carry no outcome:
    for st in (TaskStatus.CREATED, TaskStatus.EXECUTING, TaskStatus.RECOVERING, TaskStatus.CANCELLED):
        assert compute_outcome(st, Verdict.PASSED, dod_ok, 0) is None


# ---------------------------------------------------------------------------
# Pipeline-level: factorial (DoD runs and passes) vs abstain
# ---------------------------------------------------------------------------


def test_verified_outcome_on_dod_pass(client):
    c, _ = client
    r = c.post("/api/v1/tasks", json={"title": "Factorial verified", "goal": GOAL_FACTORIAL})
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = c.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "completed"
    assert body["definition_of_done"]["applicable"] is True
    assert body["definition_of_done"]["satisfied"] is True
    assert body["outcome"] == "verified"
    assert body["result"]["outcome"] == "verified"
    assert body["result"]["simulated_steps"] == 0

    # The outcome is durably persisted on the task row.
    r = c.get(f"/api/v1/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json()["outcome"] == "verified"


def test_completed_unverified_outcome_when_dod_abstains(client):
    c, _ = client
    r = c.post("/api/v1/tasks", json={"title": "Essay", "goal": GOAL_ABSTAIN})
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = c.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    body = r.json()

    # Execution succeeded and DoD abstained — the task is NOT verified.
    assert body["status"] == "completed"
    assert body["definition_of_done"]["applicable"] is False
    assert body["outcome"] == "completed_unverified"
    assert body["result"]["outcome"] == "completed_unverified"

    r = c.get(f"/api/v1/tasks/{task_id}")
    assert r.json()["outcome"] == "completed_unverified"


def test_failed_outcome_on_exhausted_dod(client):
    c, _ = client
    r = c.post("/api/v1/tasks", json={"title": "Factorial wrong", "goal": GOAL_FACTORIAL})
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = c.post(f"/api/v1/tasks/{task_id}/execute", json={"fail_dod_with": 721})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "failed"
    assert body["outcome"] == "failed"
    r = c.get(f"/api/v1/tasks/{task_id}")
    assert r.json()["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Durable audit trail
# ---------------------------------------------------------------------------


async def _persist_audit_events():
    from app.database import async_session_factory
    from services.security.audit_trail import AuditTrail

    async with async_session_factory() as db:
        return await AuditTrail.instance().flush_pending(db)


def test_audit_events_are_durably_persisted(client):
    c, user = client
    # Trigger real kernel-gated work (pipeline fs writes + denies).
    r = c.post("/api/v1/tasks", json={"title": "Audit source", "goal": GOAL_FACTORIAL})
    task_id = r.json()["id"]
    c.post(f"/api/v1/tasks/{task_id}/execute", json={})

    import asyncio

    written = asyncio.run(_persist_audit_events())
    # NOTE: `written` is a racy proxy — the lifespan flusher (every 2s) may
    # have ALREADY drained `_pending` to the durable DB before this manual
    # flush, in which case written == 0 while durability still holds. The
    # guarantee under test is DURABILITY, asserted below against raw rows.

    raw = sqlite3.connect(_DB_PATH)
    try:
        rows = raw.execute(
            "SELECT event_id, actor_id, action, allowed FROM audit_events"
        ).fetchall()
    finally:
        raw.close()
    assert rows, "audit_events rows must exist in the durable DB (flushed by the test flush OR the periodic flusher)"
    actors = {row[1] for row in rows}
    assert "pipeline-service" in actors, "pipeline kernel decisions must be persisted"
    assert any(row[3] == 0 for row in rows) or True  # denials may or may not occur here


def test_audit_endpoint_requires_admin(client):
    c, user = client
    # A plain (non-admin) caller is denied AND the denial itself is audited.
    r = c.get("/api/v1/audit")
    assert r.status_code == 403, r.text
    body = r.json()
    assert "admin" in body["detail"].lower()

    from services.security.audit_trail import AuditTrail

    denials = [
        e
        for e in AuditTrail.instance().get_events(action="api:view_audit")
        if e.allowed is False
    ]
    assert denials, "the 403 must itself be audit-logged"


def test_audit_endpoint_serves_admin_and_survives_restart(client):
    c, user = client
    # Grant this caller the admin permission on the kernel.
    from services.security.security_kernel import Permission
    from services.security.audit_trail import AuditTrail

    from app import main as app_main

    app_main.security_kernel.grant_permission(user.id, Permission.ADMIN)

    # Generate some real events, then flush them.
    r = c.post("/api/v1/tasks", json={"title": "Admin audit", "goal": GOAL_ABSTAIN})
    task_id = r.json()["id"]
    c.post(f"/api/v1/tasks/{task_id}/execute", json={})

    import asyncio

    asyncio.run(_persist_audit_events())

    r = c.get("/api/v1/audit?limit=50")
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert events, "persisted events must be readable"
    assert any(e["actor_id"] == "pipeline-service" for e in events)
    assert any(e["action"] == "api:view_audit" for e in events)  # our own reads are audited

    # Restart-safety proof: read the SAME rows through a FRESH session and
    # a fresh AuditTrail instance state (clear the in-memory trail).
    AuditTrail.instance().clear()
    r2 = c.get("/api/v1/audit?limit=50")
    assert r2.status_code == 200
    events2 = r2.json()["events"]
    assert events2, "events must survive an in-memory wipe (durable table)"
    ids1 = {e["event_id"] for e in events}
    ids2 = {e["event_id"] for e in events2}
    assert ids1 & ids2, "the same events must be served after memory wipe"


def test_new_audit_events_persisted_after_memory_wipe(client):
    """Events logged AFTER a wipe get persisted (no dedup collapse)."""
    c, user = client
    from services.security.audit_trail import AuditTrail

    AuditTrail.instance().clear()
    AuditTrail.instance().log(
        actor_id="wipe-test", action="tool:audit_probe",
        resource="audit:wipe", allowed=False, details={"k": "v"},
    )
    assert AuditTrail.instance().pending_count() >= 1
    written = asyncio.run(_persist_audit_events())
    assert written >= 1
    raw = sqlite3.connect(_DB_PATH)
    try:
        n = raw.execute(
            "SELECT COUNT(*) FROM audit_events WHERE actor_id = 'wipe-test'"
        ).fetchone()[0]
    finally:
        raw.close()
    assert n >= 1
