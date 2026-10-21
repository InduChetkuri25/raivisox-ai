"""V3 Phase 1 acceptance tests — identity, ownership and anti-spoof (spec §6).

Provenance must come from authoritative rows, never from tool arguments, and a
cross-user or cross-task relationship must be refused rather than recorded.
"""

from __future__ import annotations

import uuid

import pytest

from integrations.adapters import AdapterRegistry, FileReadAdapter, TextTransformAdapter
from services.graph import actions, events, schema
from services.graph.provenance import ProvenanceError, resolve_run_provenance
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import (
    build_stack,
    count_rows,
    ctx_for,
    emit_event,
    fetch_rows,
    live_action,
    write_scope_file,
)


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("V3_EVENTS_ENABLED", "1")
    events.reset_state()
    actions.clear()
    built = await build_stack(tmp_path)
    events.set_session_factory(built.factory)
    try:
        yield built
    finally:
        events.reset_state()
        actions.clear()
        await built.engine.dispose()


async def _add_second_subject(stack) -> dict[str, str]:
    """A second user with their own task and run, in the same database."""
    from app.models.execution_run import ExecutionRun
    from app.models.task import Task
    from app.models.user import User

    ids = {key: str(uuid.uuid4()) for key in ("user_id", "task_id", "run_id")}
    async with stack.factory() as session:
        session.add(
            User(
                id=ids["user_id"],
                email=f"{ids['user_id'][:8]}@example.test",
                hashed_password="not-used",
            )
        )
        await session.flush()
        session.add(
            Task(
                id=ids["task_id"],
                user_id=ids["user_id"],
                title="other task",
                goal="other goal",
                status="created",
            )
        )
        await session.flush()
        session.add(
            ExecutionRun(id=ids["run_id"], task_id=ids["task_id"], status="running")
        )
        await session.commit()
    return ids


async def _read_all(stack, task_id: str) -> list[dict]:
    from sqlalchemy import text

    async with stack.factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM graph_events WHERE task_id = :tid"),
                {"tid": task_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# T13 / T13b — forged user, mismatched task
# ---------------------------------------------------------------------------


async def test_forged_user_id_is_refused(stack):
    with pytest.raises(ProvenanceError):
        await emit_event(stack, user_id=str(uuid.uuid4()))

    assert await count_rows(stack) == 0


async def test_task_id_must_match_provenance(stack):
    """A real user for task A cannot be attached to task B's event."""
    other = await _add_second_subject(stack)

    with pytest.raises(ProvenanceError):
        await emit_event(
            stack,
            user_id=stack.user_id,  # belongs to task A
            task_id=other["task_id"],  # a different, real task
            run_id=other["run_id"],
        )

    assert await count_rows(stack) == 0


# ---------------------------------------------------------------------------
# T14 — a run that belongs to another task is refused
# ---------------------------------------------------------------------------


async def test_run_belonging_to_another_task_is_refused(stack):
    other = await _add_second_subject(stack)

    with pytest.raises(ProvenanceError):
        async with stack.factory() as session:
            await resolve_run_provenance(session, stack.task_id, other["run_id"])

    # The legitimate pairing still resolves.
    async with stack.factory() as session:
        provenance = await resolve_run_provenance(
            session, stack.task_id, stack.run_id
        )
    assert provenance.user_id == stack.user_id
    assert provenance.task_id == stack.task_id


# ---------------------------------------------------------------------------
# T15 — no provenance ⇒ no event, but the gap is counted
# ---------------------------------------------------------------------------


async def test_adapter_without_provenance_writes_no_events(stack):
    write_scope_file(stack, "note.txt", "hello")
    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.READ_FILES)
    registry = AdapterRegistry(kernel)
    registry.register(FileReadAdapter(stack.workspace))

    ctx = ctx_for(stack, user_id="", execution_run_id="")
    result = await registry.run("fs_read", {"path": "note.txt"}, ctx)

    assert result.success, result.error
    assert await count_rows(stack) == 0
    assert events.counters()["skipped_no_provenance"] >= 1


# ---------------------------------------------------------------------------
# T16 — an adapter cannot invent an invocation id
# ---------------------------------------------------------------------------


async def test_unknown_action_id_is_rejected(stack):
    ok = await events.emit(
        event_type=schema.TOOL_ATTEMPTED,
        outcome=schema.ATTEMPTED,
        actor_id="pipeline-service",
        user_id=stack.user_id,
        task_id=stack.task_id,
        run_id=stack.run_id,
        step_node_id="step-1",
        tool_id="fs_read",
        action_id=str(uuid.uuid4()),  # never issued by the registry
        payload={"resource_ref": "x"},
    )

    assert ok is False
    assert await count_rows(stack) == 0
    assert events.counters()["invalid"] == 1
    # A registered action id is still accepted.
    assert await emit_event(stack, action_id=live_action()) is True


# ---------------------------------------------------------------------------
# T17 — identity in params cannot override the context
# ---------------------------------------------------------------------------


async def test_identity_fields_are_not_read_from_params(stack):
    other = await _add_second_subject(stack)
    registry = AdapterRegistry(SecurityKernel())
    registry.register(TextTransformAdapter())

    result = await registry.run(
        "text_transform",
        {
            "text": "a b c",
            "op": "count",
            "user_id": other["user_id"],
            "task_id": other["task_id"],
            "run_id": other["run_id"],
        },
        ctx_for(stack),
    )
    assert result.success, result.error

    rows = await fetch_rows(stack)
    assert rows
    assert {r["user_id"] for r in rows} == {stack.user_id}
    assert {r["task_id"] for r in rows} == {stack.task_id}
    assert {r["run_id"] for r in rows} == {stack.run_id}
    assert await _read_all(stack, other["task_id"]) == []
