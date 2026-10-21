"""Shared helpers for the V3 Phase 1 acceptance tests.

Each test builds its OWN temporary SQLite database and injects its session
factory into ``services.graph.events``, so no test depends on import order or
on the app's default database URL. Nothing here is production code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class Stack:
    """A minimal, real subject: one user, one task, one execution run."""

    engine: Any
    factory: Any
    user_id: str
    task_id: str
    run_id: str
    workspace: str


async def build_stack(tmp_path) -> Stack:
    """Create a fresh database with all model tables and one seeded subject."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401 — populate Base.metadata
    from app.database import Base
    from app.models.execution_run import ExecutionRun
    from app.models.task import Task
    from app.models.user import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase1.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    user_id, task_id, run_id = (str(uuid.uuid4()) for _ in range(3))
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)

    async with factory() as session:
        session.add(
            User(
                id=user_id,
                email=f"{user_id[:8]}@example.test",
                hashed_password="not-used",
            )
        )
        await session.flush()
        session.add(
            Task(
                id=task_id,
                user_id=user_id,
                title="phase1 test task",
                goal="phase1 test goal",
                status="created",
            )
        )
        await session.flush()
        session.add(ExecutionRun(id=run_id, task_id=task_id, status="running"))
        await session.commit()

    return Stack(engine, factory, user_id, task_id, run_id, str(workspace))


@pytest.fixture
async def phase1_stack(tmp_path, monkeypatch):
    """A fresh DB + real subject with V3 event collection enabled.

    Imported into a test module's namespace to activate it (helpers is not a
    conftest module). Each test gets its own database file.
    """
    from services.graph import actions, events

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


def ctx_for(stack: Stack, **overrides):
    """AdapterContext carrying verified provenance for this stack."""
    from integrations.adapters.base import AdapterContext

    values = {
        "actor_id": "pipeline-service",
        "task_id": stack.task_id,
        "step_id": "step-1",
        "resource": f"task:{stack.task_id}/step:step-1",
        "workspace": stack.workspace,
        "user_id": stack.user_id,
        "execution_run_id": stack.run_id,
    }
    values.update(overrides)
    return AdapterContext(**values)


def live_action() -> str:
    """Register and return an action id the registry could have issued."""
    from services.graph import actions

    action_id = str(uuid.uuid4())
    actions.register(action_id)
    return action_id


async def emit_event(stack: Stack, **overrides):
    """Emit with valid defaults; tests override only what they are testing."""
    from services.graph import events, schema

    values: dict[str, Any] = {
        "event_type": schema.TOOL_ATTEMPTED,
        "outcome": schema.ATTEMPTED,
        "actor_id": "pipeline-service",
        "user_id": stack.user_id,
        "task_id": stack.task_id,
        "run_id": stack.run_id,
        "step_node_id": "step-1",
        "tool_id": "fs_read",
        "action_id": live_action(),
        "payload": {"resource_ref": "task:x/step:y"},
    }
    values.update(overrides)
    return await events.emit(**values)


async def fetch_rows(stack: Stack, *, task_id: str | None = None) -> list[dict]:
    from services.graph import events

    async with stack.factory() as session:
        rows, _refused = await events.read_events(
            session, task_id=task_id or stack.task_id, limit=1000
        )
    return rows


async def count_rows(stack: Stack, table: str = "graph_events") -> int:
    from sqlalchemy import text

    async with stack.factory() as session:
        return int(
            (
                await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            ).scalar()
            or 0
        )


def write_scope_file(stack: Stack, relpath: str, content: str) -> str:
    """Create a real file inside the task scope and return its absolute path."""
    import os

    scope = os.path.join(stack.workspace, stack.task_id)
    os.makedirs(scope, exist_ok=True)
    path = os.path.join(scope, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path
