"""Action-id lifecycle tests (V3 Phase 1 acceptance-audit finding M2).

Liveness must follow the invocation, not a wall-clock deadline: an id stays
live while its invocation is still producing evidence, and it is removed in
the invocation's ``finally`` whatever the outcome. ``IDLE_TTL_SECONDS`` is only
a leak backstop for an entry whose owner never reached cleanup.

The clock is injected (``services.graph.actions.set_clock``), so every test is
deterministic and nothing sleeps for 300 s.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from integrations.adapters import AdapterRegistry
from integrations.adapters.base import AdapterContext, AdapterResult, ToolAdapter
from services.graph import actions
from services.graph import schema as graph_schema
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import (  # noqa: F401
    ctx_for,
    emit_event,
    phase1_stack,
)


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _fresh_state():
    actions.clear()
    yield
    actions.clear()


@pytest.fixture
def clock():
    c = _Clock()
    actions.set_clock(c)
    yield c
    actions.set_clock(None)


@pytest.fixture
async def stack(phase1_stack):  # noqa: F811
    return phase1_stack


class _StubAdapter(ToolAdapter):
    """Real registry lifecycle, no real work: advances the clock or blocks."""

    id = "stub"
    description = "lifecycle stub"
    kernel_permissions: list[str] = []

    def __init__(self, *, clock=None, delay: float = 0.0, fail: bool = False,
                 block: asyncio.Event | None = None, started: asyncio.Event | None = None,
                 permissions=None):
        self._clock = clock
        self._delay = delay
        self._fail = fail
        self._block = block
        self._started = started
        if permissions is not None:
            self.kernel_permissions = list(permissions)

    async def run(self, params, ctx):
        if self._started is not None:
            self._started.set()
        if self._block is not None:
            await self._block.wait()
        if self._clock is not None:
            self._clock.advance(self._delay)
        if self._fail:
            raise RuntimeError("stub failure")
        return AdapterResult(success=True, tool_id=self.id, output={})


async def _events_for(stack, action_id: str) -> list[dict]:
    async with stack.factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, tool_id, step_node_id, user_id, task_id, run_id "
                    "FROM graph_events WHERE action_id = :aid ORDER BY seq"
                ),
                {"aid": action_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def _bind(action_id: str, stack, *, tool_id: str = "fs_read", step: str = "step-1") -> None:
    actions.register(
        action_id,
        actor_id="pipeline-service",
        user_id=stack.user_id,
        task_id=stack.task_id,
        run_id=stack.run_id,
        step_node_id=step,
        tool_id=tool_id,
    )


# ---------------------------------------------------------------------------
# 1–2. Liveness follows use, not the old 300-second deadline
# ---------------------------------------------------------------------------


async def test_action_stays_live_past_old_deadline_and_terminal_is_accepted(stack, clock):
    action_id = str(uuid.uuid4())
    _bind(action_id, stack)

    clock.advance(301)  # beyond the retired fixed TTL
    assert actions.is_live(action_id) is True

    clock.advance(120)  # 421 s after issue, still the same invocation
    assert await emit_event(
        stack,
        event_type=graph_schema.TOOL_EXECUTED,
        outcome=graph_schema.EXECUTED,
        action_id=action_id,
    ) is True

    rows = await _events_for(stack, action_id)
    assert [row["event_type"] for row in rows] == ["tool.executed"]


async def test_registry_invocation_longer_than_old_ttl_keeps_its_terminal_event(stack, clock):
    kernel = SecurityKernel()
    registry = AdapterRegistry(kernel)
    registry.register(_StubAdapter(clock=clock, delay=600))

    ctx = ctx_for(stack)
    result = await registry.run("stub", {}, ctx)
    assert result.success, result.error

    rows = await _events_for(stack, ctx.action_id)
    assert {row["event_type"] for row in rows} == {"tool.attempted", "tool.executed"}
    for row in rows:
        assert row["tool_id"] == "stub"
        assert row["step_node_id"] == "step-1"
        assert row["user_id"] == stack.user_id
        assert row["task_id"] == stack.task_id
    # Cleanup ran in the invocation's finally.
    assert actions.live_count() == 0


async def test_accepted_use_refreshes_the_backstop(stack, clock):
    action_id = str(uuid.uuid4())
    _bind(action_id, stack)

    clock.advance(actions.IDLE_TTL_SECONDS - 10)
    assert actions.is_live(action_id) is True  # refresh
    clock.advance(actions.IDLE_TTL_SECONDS - 10)
    # Past one idle window since issue, but only 1790 s since accepted use.
    assert actions.is_live(action_id) is True


# ---------------------------------------------------------------------------
# 3–4. Deterministic cleanup and the abandoned-entry backstop
# ---------------------------------------------------------------------------


async def test_cleanup_removes_the_action_on_success(stack):
    registry = AdapterRegistry(SecurityKernel())
    registry.register(_StubAdapter())
    ctx = ctx_for(stack)
    result = await registry.run("stub", {}, ctx)
    assert result.success
    assert actions.is_live(ctx.action_id) is False
    assert actions.live_count() == 0


async def test_cleanup_removes_the_action_on_kernel_denial(stack):
    kernel = SecurityKernel()  # no grants
    registry = AdapterRegistry(kernel)
    registry.register(_StubAdapter(permissions=[Permission.WRITE_FILES]))
    ctx = ctx_for(stack)
    result = await registry.run("stub", {}, ctx)
    assert result.permission_denied
    assert actions.live_count() == 0


async def test_cleanup_removes_the_action_when_the_adapter_raises(stack):
    registry = AdapterRegistry(SecurityKernel())
    registry.register(_StubAdapter(fail=True))
    ctx = ctx_for(stack)
    with pytest.raises(RuntimeError):
        await registry.run("stub", {}, ctx)
    assert actions.live_count() == 0


async def test_cleanup_removes_the_action_on_cancellation(stack):
    registry = AdapterRegistry(SecurityKernel())
    release, started = asyncio.Event(), asyncio.Event()
    registry.register(_StubAdapter(block=release, started=started))
    ctx = ctx_for(stack)

    task = asyncio.create_task(registry.run("stub", {}, ctx))
    # The adapter only starts after the registry has published the id.
    await asyncio.wait_for(started.wait(), timeout=10)
    action_id = ctx.action_id
    assert action_id, "the registry publishes the id on the context"
    assert actions.live_count() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert actions.is_live(action_id) is False
    assert actions.live_count() == 0


async def test_abandoned_action_is_reaped_after_the_idle_window(stack, clock):
    action_id = str(uuid.uuid4())
    _bind(action_id, stack)
    assert actions.live_count() == 1

    clock.advance(actions.IDLE_TTL_SECONDS + 1)
    assert actions.is_live(action_id) is False
    assert actions.live_count() == 0
    # A reaped id is not a usable credential either.
    assert await emit_event(
        stack,
        event_type=graph_schema.TOOL_EXECUTED,
        outcome=graph_schema.EXECUTED,
        action_id=action_id,
    ) is False


async def test_unknown_action_id_is_still_rejected(stack):
    assert await emit_event(stack, action_id=str(uuid.uuid4())) is False
    async with stack.factory() as session:
        count = (
            await session.execute(text("SELECT COUNT(*) FROM graph_events"))
        ).scalar()
    assert int(count) == 0


# ---------------------------------------------------------------------------
# 5–6. Issued ids are not transferable between invocations
# ---------------------------------------------------------------------------


async def test_borrowed_action_id_with_foreign_identity_is_rejected(stack):
    action_id = str(uuid.uuid4())
    _bind(action_id, stack, tool_id="fs_read", step="step-1")

    # Another tool, another step, another run: all refused before any write.
    assert await emit_event(stack, action_id=action_id, tool_id="shell_exec") is False
    assert await emit_event(stack, action_id=action_id, step_node_id="step-2") is False
    assert await emit_event(
        stack, action_id=action_id, run_id=str(uuid.uuid4())
    ) is False

    # The genuine invocation still works with the same id.
    assert await emit_event(stack, action_id=action_id) is True

    rows = await _events_for(stack, action_id)
    assert len(rows) == 1
    assert rows[0]["tool_id"] == "fs_read"


async def test_two_invocations_same_tool_cannot_share_an_id(stack):
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    _bind(first, stack, step="step-1")
    _bind(second, stack, step="step-2")

    # Using the second invocation's id with the first invocation's step.
    assert await emit_event(stack, action_id=second, step_node_id="step-1") is False
    assert await emit_event(stack, action_id=first, step_node_id="step-2") is False
    assert await emit_event(stack, action_id=first) is True
    assert await emit_event(stack, action_id=second, step_node_id="step-2") is True


# ---------------------------------------------------------------------------
# 7–8. Concurrency: independent registrations, thread-safe registry
# ---------------------------------------------------------------------------


async def test_concurrent_invocations_do_not_interfere(stack):
    registry = AdapterRegistry(SecurityKernel())
    registry.register(_StubAdapter())
    ctx_a = ctx_for(stack)
    ctx_b = ctx_for(stack, step_id="step-2", resource="task:x/step:step-2")

    results = await asyncio.gather(
        registry.run("stub", {}, ctx_a),
        registry.run("stub", {}, ctx_b),
    )
    assert all(r.success for r in results)
    assert ctx_a.action_id and ctx_b.action_id
    assert ctx_a.action_id != ctx_b.action_id
    assert actions.live_count() == 0

    for ctx, step in ((ctx_a, "step-1"), (ctx_b, "step-2")):
        rows = await _events_for(stack, ctx.action_id)
        assert {row["event_type"] for row in rows} == {
            "tool.attempted",
            "tool.executed",
        }
        assert {row["step_node_id"] for row in rows} == {step}
        assert {row["tool_id"] for row in rows} == {"stub"}


def test_registry_is_thread_safe_under_concurrent_use():
    ids = [str(uuid.uuid4()) for _ in range(200)]

    def worker(action_id: str) -> bool:
        actions.register(action_id, tool_id="fs_read")
        live = actions.is_live(action_id)
        actions.discard(action_id)
        return live

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(worker, ids))

    assert actions.live_count() == 0
