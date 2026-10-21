"""V3 Phase 1 acceptance tests — event writer (spec §9).

Each test owns its database (see ``v3_phase1_helpers``), so assertions are made
against real rows, not mocks of the writer.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from integrations.adapters import AdapterRegistry, FileReadAdapter, TextTransformAdapter
from integrations.adapters.base import AdapterResult, ToolAdapter
from services.graph import actions, events, schema
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


@pytest.fixture(autouse=True)
def _flag_default(monkeypatch):
    # Every test in this module needs the flag unless it explicitly clears it.
    monkeypatch.setenv("V3_EVENTS_ENABLED", "1")


# ---------------------------------------------------------------------------
# T1 / T1b / T2 / T3 — lifecycle around the unchanged kernel gate
# ---------------------------------------------------------------------------


async def test_tool_lifecycle_records_attempt_allow_execute(stack):
    write_scope_file(stack, "note.txt", "hello")
    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.READ_FILES)
    registry = AdapterRegistry(kernel)
    registry.register(FileReadAdapter(stack.workspace))

    result = await registry.run("fs_read", {"path": "note.txt"}, ctx_for(stack))
    assert result.success, result.error

    rows = await fetch_rows(stack)
    tool_rows = [r for r in rows if r["event_type"].startswith("tool.")]
    assert [r["event_type"] for r in tool_rows] == [
        "tool.attempted",
        "tool.allowed",
        "tool.executed",
    ]
    # The adapter's own resource event shares the invocation's action id.
    assert any(r["event_type"] == "resource.read.observed" for r in rows)
    assert len({r["action_id"] for r in rows}) == 1
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    assert {r["evidence_class"] for r in rows} == {"observed"}
    # The attempt precedes any decision, so it has no correlation; the
    # decision and the outcome cite the same audit request id.
    assert tool_rows[0]["correlation_id"] is None
    assert tool_rows[1]["correlation_id"]
    assert tool_rows[2]["correlation_id"] == tool_rows[1]["correlation_id"]


async def test_permissionless_adapter_has_no_allowed_event(stack):
    registry = AdapterRegistry(SecurityKernel())
    registry.register(TextTransformAdapter())

    result = await registry.run(
        "text_transform", {"text": "one two", "op": "count"}, ctx_for(stack)
    )
    assert result.success, result.error

    rows = await fetch_rows(stack)
    assert [r["event_type"] for r in rows] == ["tool.attempted", "tool.executed"]
    assert not any(r["event_type"] == "tool.allowed" for r in rows)


async def test_denied_tool_has_no_executed_sibling(stack):
    class _Spy(ToolAdapter):
        id = "spy"
        kernel_permissions = ["execute_code"]

        def __init__(self):
            self.called = False

        async def run(self, params, ctx):  # pragma: no cover - must not run
            self.called = True
            return AdapterResult(success=True, tool_id=self.id)

    spy = _Spy()
    registry = AdapterRegistry(SecurityKernel())  # no grant
    registry.register(spy)

    result = await registry.run("spy", {}, ctx_for(stack))
    assert result.permission_denied is True
    assert spy.called is False

    rows = await fetch_rows(stack)
    assert [r["event_type"] for r in rows] == ["tool.attempted", "tool.denied"]
    assert not any(r["event_type"] in {"tool.executed", "tool.failed"} for r in rows)


async def test_adapter_failure_records_failed(stack):
    class _Boom(ToolAdapter):
        id = "boom"
        kernel_permissions = []

        async def run(self, params, ctx):
            raise RuntimeError("adapter blew up")

    registry = AdapterRegistry(SecurityKernel())
    registry.register(_Boom())

    with pytest.raises(RuntimeError):
        await registry.run("boom", {}, ctx_for(stack))

    rows = await fetch_rows(stack)
    assert [r["event_type"] for r in rows] == ["tool.attempted", "tool.failed"]
    assert rows[-1]["payload"]["error_kind"] == "exception"


# ---------------------------------------------------------------------------
# T4 / T5 / T6 — payload allowlist, credential refusal, size cap
# ---------------------------------------------------------------------------


async def test_payload_allowlist_drops_unknown_keys(stack):
    ok = await emit_event(
        stack,
        event_type=schema.RESOURCE_READ_OBSERVED,
        outcome=schema.EXECUTED,
        payload={"content": "SECRET", "size": 1},
    )
    assert ok is True

    rows = await fetch_rows(stack)
    assert rows[0]["payload"] == {"size": 1}
    assert "SECRET" not in str(rows[0]["payload"])


async def test_secret_like_value_rejects_event(stack):
    ok = await emit_event(
        stack,
        event_type=schema.TOOL_DENIED,
        outcome=schema.DENIED,
        payload={"reason": "sk-" + "A1b2C3d4" * 5},
    )
    assert ok is False
    assert await count_rows(stack) == 0
    assert events.counters()["invalid"] == 1


async def test_oversized_payload_rejected(stack):
    ok = await emit_event(
        stack,
        event_type=schema.RESOURCE_READ_DENIED,
        outcome=schema.DENIED,
        payload={"reason": "x" * 9000},
    )
    assert ok is False
    assert await count_rows(stack) == 0
    assert events.counters()["invalid"] == 1


# ---------------------------------------------------------------------------
# T7 / T8 / T8b — idempotency, ordering, cursor/replay safety
# ---------------------------------------------------------------------------


async def test_duplicate_idempotency_key_is_a_no_op(stack):
    action_id = live_action()
    first = await emit_event(stack, action_id=action_id)
    second = await emit_event(stack, action_id=action_id)

    assert first is True and second is True
    assert await count_rows(stack) == 1
    assert events.counters()["duplicates"] == 1


async def test_seq_gap_is_preserved_and_ordering_uses_seq(stack):
    from sqlalchemy import text

    await emit_event(stack)  # seq 1
    action_id = live_action()
    await emit_event(
        stack,
        event_type=schema.TOOL_EXECUTED,
        outcome=schema.EXECUTED,
        action_id=action_id,
        payload={"duration_ms": 1},
    )  # seq 2
    async with stack.factory() as session:
        await session.execute(text("DELETE FROM graph_events WHERE seq = 2"))
        await session.commit()
    await emit_event(stack)  # seq 3

    rows = await fetch_rows(stack)
    assert [r["seq"] for r in rows] == [1, 3]


async def test_seq_is_never_reused_after_delete(stack):
    from sqlalchemy import text

    await emit_event(stack)
    await emit_event(stack)
    async with stack.factory() as session:
        await session.execute(text("DELETE FROM graph_events WHERE seq = 2"))
        await session.commit()
    await emit_event(stack)

    rows = await fetch_rows(stack)
    assert max(r["seq"] for r in rows) >= 3
    assert [r["seq"] for r in rows] == [1, 3]


# ---------------------------------------------------------------------------
# T9 / T10 / T10b / T10c / T11 / T12 — durability, flag, failures, parallelism
# ---------------------------------------------------------------------------


async def test_events_survive_restart(stack, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    assert await emit_event(stack) is True
    await stack.engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase1.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    events.set_session_factory(factory)
    try:
        async with factory() as session:
            rows, refused = await events.read_events(session)
        assert [r["event_type"] for r in rows] == ["tool.attempted"]
        assert refused == 0
    finally:
        await engine.dispose()


async def test_flag_off_writes_nothing(stack, monkeypatch):
    monkeypatch.delenv("V3_EVENTS_ENABLED", raising=False)
    registry = AdapterRegistry(SecurityKernel())
    registry.register(TextTransformAdapter())

    result = await registry.run(
        "text_transform", {"text": "a b", "op": "count"}, ctx_for(stack)
    )
    assert result.success, result.error
    assert await count_rows(stack) == 0


async def test_missing_table_is_non_fatal(stack):
    from sqlalchemy import text

    async with stack.factory() as session:
        await session.execute(text("DROP TABLE graph_events"))
        await session.commit()

    ok = await emit_event(stack)
    assert ok is False
    assert events.counters()["failures"] >= 1
    # Recording disables itself for the process instead of retrying forever.
    assert await emit_event(stack) is False


async def test_write_error_does_not_raise_into_pipeline(stack):
    from sqlalchemy.exc import OperationalError

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    class _FailingCommit:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            await self._session.close()
            return False

        async def execute(self, *args, **kwargs):
            return await self._session.execute(*args, **kwargs)

        def add(self, obj):
            self._session.add(obj)

        async def commit(self):
            raise OperationalError(
                "INSERT INTO graph_events", {}, Exception("connection is closed")
            )

        async def rollback(self):
            await self._session.rollback()

    real_factory = stack.factory
    failing = async_sessionmaker(stack.engine, expire_on_commit=False)

    def _factory():
        return _FailingCommit(failing())

    ok = await emit_event(stack, session_factory=_factory)
    assert ok is False
    assert events.counters()["failures"] == 1
    # The real factory is untouched and the API still works afterwards.
    assert await emit_event(stack, session_factory=real_factory) is True


async def test_concurrent_emits_do_not_lose_rows(stack):
    results = await asyncio.gather(*[emit_event(stack) for _ in range(4)])
    assert all(results)
    rows = await fetch_rows(stack)
    assert len(rows) == 4
    assert len({r["seq"] for r in rows}) == 4


async def test_unknown_schema_version_is_refused_by_reader(stack):
    from sqlalchemy import text

    await emit_event(stack)
    async with stack.factory() as session:
        await session.execute(
            text(
                "INSERT INTO graph_events (event_id, schema_version, event_type, "
                "occurred_at, recorded_at, actor_id, actor_type, user_id, task_id, "
                "outcome, evidence_class, source, idempotency_key, payload_hash) "
                "VALUES (:eid, 2, 'tool.attempted', 1.0, 1.0, 'pipeline-service', "
                "'service', :uid, :tid, 'attempted', 'observed', 'x', :key, :hash)"
            ),
            {
                "eid": str(uuid.uuid4()),
                "uid": stack.user_id,
                "tid": stack.task_id,
                "key": f"future:{uuid.uuid4()}",
                "hash": "0" * 64,
            },
        )
        await session.commit()

    async with stack.factory() as session:
        rows, refused = await events.read_events(session)
    assert refused == 1
    assert all(r["schema_version"] == 1 for r in rows)


# ---------------------------------------------------------------------------
# T15b — append-only by construction
# ---------------------------------------------------------------------------


def test_writer_only_inserts():
    import inspect

    from app.models.graph_event import GraphEvent

    assert "updated_at" not in GraphEvent.__table__.columns
    source = inspect.getsource(events)
    assert "session.delete" not in source
    assert ".update(" not in source
    assert not any(name.startswith(("update_", "delete_")) for name in dir(events))


# ---------------------------------------------------------------------------
# Non-functional — the hot path has a sanity bound (not a perf claim)
# ---------------------------------------------------------------------------


async def test_emit_cost_is_bounded(stack):
    import time

    start = time.perf_counter()
    for _ in range(100):
        assert await emit_event(stack) is True
    elapsed = time.perf_counter() - start

    # Spec bound: 1000 emits under 5 s on SQLite. Scaled to 100 for the suite;
    # a per-emit cost above 50 ms would fail here on any normal machine.
    assert elapsed < 5.0, f"100 emits took {elapsed:.2f}s"
    assert await count_rows(stack) == 100
