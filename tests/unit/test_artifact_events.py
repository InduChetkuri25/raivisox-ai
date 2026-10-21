"""V3 Phase 1 acceptance tests — verified artifact events (T30–T31).

`artifact.registered` is the only `verified` event in Phase 1: it is written
after the artifact row committed and the bytes were hashed on disk.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from services.graph import schema
from tests.unit.v3_phase1_helpers import count_rows, phase1_stack  # noqa: F401


@pytest.fixture
async def stack(phase1_stack):  # noqa: F811
    return phase1_stack


@pytest.fixture
async def app_main(stack, monkeypatch):
    """Point the artifact registrar at this test's database and workspace."""
    from app import main as module

    monkeypatch.setattr(module, "async_session_factory", stack.factory)
    monkeypatch.setattr(module, "TASK_WORKSPACE", stack.workspace)
    return module


def _write_deliverable(stack, name: str = "solution.py") -> str:
    scope = os.path.join(stack.workspace, stack.task_id)
    os.makedirs(scope, exist_ok=True)
    path = os.path.join(scope, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("print('RAIVISOX')\n")
    return path


async def _rows(stack) -> list[dict]:
    import json

    from sqlalchemy import text

    async with stack.factory() as session:
        rows = [
            dict(row)
            for row in (
                await session.execute(
                    text("SELECT * FROM graph_events WHERE event_type = :t"),
                    {"t": schema.ARTIFACT_REGISTERED},
                )
            ).mappings()
        ]
    for row in rows:
        if isinstance(row.get("payload"), str):
            row["payload"] = json.loads(row["payload"])
    return rows


# ---------------------------------------------------------------------------
# T30 — verified requires the corroborating row and the real hash
# ---------------------------------------------------------------------------


async def test_artifact_registered_is_verified_with_hash(stack, app_main):
    path = _write_deliverable(stack)
    expected = hashlib.sha256(open(path, "rb").read()).hexdigest()

    row = await app_main._register_task_artifact(
        stack.task_id,
        path=path,
        node_id="step-1",
        step_db_id=None,
        step_title="deliverable",
        run_id=stack.run_id,
    )
    assert row.id

    rows = await _rows(stack)
    assert len(rows) == 1
    event = rows[0]
    assert event["evidence_class"] == schema.VERIFIED
    assert event["payload"]["sha256"] == expected
    assert event["payload"]["artifact_id"] == row.id
    assert event["payload"]["size_bytes"] == os.path.getsize(path)
    assert event["target_id"] == f"res:{stack.task_id}/solution.py"
    assert event["run_id"] == stack.run_id
    assert event["user_id"] == stack.user_id

    # The referenced row really exists (corroboration, not a claim).
    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    async with stack.factory() as session:
        stored = (
            await session.execute(select(TaskArtifact).where(TaskArtifact.id == row.id))
        ).scalars().one()
    assert stored.sha256 == expected


# ---------------------------------------------------------------------------
# T31 — no verified claim when registration fails
# ---------------------------------------------------------------------------


async def test_artifact_event_absent_when_registration_fails(stack, app_main):
    outside = os.path.join(os.path.dirname(stack.workspace), "outside.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("nope\n")

    with pytest.raises(ValueError):
        await app_main._register_task_artifact(
            stack.task_id,
            path=outside,
            node_id="step-1",
            step_db_id=None,
            step_title="deliverable",
            run_id=stack.run_id,
        )

    assert await _rows(stack) == []
    assert await count_rows(stack, "task_artifacts") == 0
