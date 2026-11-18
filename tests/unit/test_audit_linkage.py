"""V3 Phase 1 acceptance tests — audit linkage (spec §5.1, T18–T20).

The forensic `audit_events` table is never modified: provenance lives in the
side table `audit_links`, written in the same transaction as the audit row.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from integrations.adapters import AdapterRegistry, FileWriteAdapter
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import ctx_for, live_action, phase1_stack  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_trail():
    AuditTrail.instance().clear()
    yield
    AuditTrail.instance().clear()


async def _flush(stack) -> int:
    async with stack.factory() as session:
        return await AuditTrail.instance().flush_pending(session)


async def _count(stack, table: str) -> int:
    async with stack.factory() as session:
        return int((await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0)


@pytest.fixture
async def stack(phase1_stack):  # noqa: F811
    return phase1_stack


# ---------------------------------------------------------------------------
# T18 — every new audit row carries its provenance
# ---------------------------------------------------------------------------


async def test_audit_row_carries_user_task_run_action(stack):
    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.WRITE_FILES)
    registry = AdapterRegistry(kernel)
    registry.register(FileWriteAdapter(stack.workspace))

    ok = await registry.run(
        "fs_write", {"path": "out.txt", "content": "hello"}, ctx_for(stack)
    )
    assert ok.success, ok.error
    denied = await registry.run(
        "fs_write", {"path": "../escape.txt", "content": "nope"}, ctx_for(stack)
    )
    assert denied.success is False

    written = await _flush(stack)
    assert written > 0

    async with stack.factory() as session:
        audit_rows = (
            await session.execute(
                text(
                    "SELECT event_id, actor_id, action, allowed FROM audit_events "
                    "ORDER BY timestamp"
                )
            )
        ).mappings().all()
        links = {
            row["event_id"]: dict(row)
            for row in (
                await session.execute(text("SELECT * FROM audit_links"))
            ).mappings()
        }

    assert audit_rows, "the run must have produced audit rows"
    assert {row["actor_id"] for row in audit_rows} == {"pipeline-service"}
    assert {row["action"] for row in audit_rows} == {"tool:fs_write"}
    # Both a success and a denial were recorded.
    assert {bool(row["allowed"]) for row in audit_rows} == {True, False}

    assert len(links) == len(audit_rows), "exactly one link row per audit row"
    for row in audit_rows:
        link = links[row["event_id"]]
        assert link["user_id"] == stack.user_id
        assert link["task_id"] == stack.task_id
        assert link["run_id"] == stack.run_id
        assert link["step_node_id"] == "step-1"
        assert link["action_id"]

    # The forensic table's shape is untouched (T19b asserts this directly).
    assert await _count(stack, "audit_links") == len(audit_rows)


# ---------------------------------------------------------------------------
# T18b — link + audit row are one transaction
# ---------------------------------------------------------------------------


async def test_link_and_audit_row_are_atomic(stack, monkeypatch):
    import app.models.audit_link as link_module

    # Two events in one batch: the first has no linkage, the second does. If
    # dequeuing happened per-event, the failure on the second insert would
    # lose the first event (no row, no queue entry) — the regression this
    # test exists to prevent.
    AuditTrail.instance().log(
        actor_id="legacy-actor", action="api:view_audit", resource="audit", allowed=True
    )
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="x",
        allowed=True,
        user_id=stack.user_id,
        task_id=stack.task_id,
        run_id=stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    pending_before = AuditTrail.instance().pending_count()
    assert pending_before == 2

    def _boom(*_args, **_kwargs):
        raise RuntimeError("link insert failed")

    monkeypatch.setattr(link_module, "AuditLink", _boom)
    with pytest.raises(RuntimeError):
        await _flush(stack)

    assert await _count(stack, "audit_events") == 0, "no orphan audit row"
    assert await _count(stack, "audit_links") == 0, "no orphan link"
    assert AuditTrail.instance().pending_count() == pending_before, "events stay queued"

    monkeypatch.undo()
    assert await _flush(stack) == 2
    assert await _count(stack, "audit_events") == 2
    assert await _count(stack, "audit_links") == 1
    assert AuditTrail.instance().pending_count() == 0


# ---------------------------------------------------------------------------
# T19 — legacy rows stay readable and honestly unlinked
# ---------------------------------------------------------------------------


async def test_legacy_audit_rows_remain_readable(stack):
    from app.models.audit_event import AuditEventModel

    legacy_id = str(uuid.uuid4())
    async with stack.factory() as session:
        session.add(
            AuditEventModel(
                event_id=legacy_id,
                actor_id="legacy-actor",
                action="api:view_audit",
                resource="audit",
                allowed=False,
                request_id=str(uuid.uuid4()),
                details={"reason": "pre-v3"},
                timestamp=1700000000.0,
            )
        )
        await session.commit()

    async with stack.factory() as session:
        rows = await AuditTrail.instance().read_from_db(session, limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == legacy_id
    assert row["actor_id"] == "legacy-actor"
    assert row["action"] == "api:view_audit"
    assert row["resource"] == "audit"
    assert row["allowed"] is False
    assert row["details"] == {"reason": "pre-v3"}
    assert row["timestamp"] == 1700000000.0
    for key in ("user_id", "task_id", "run_id", "step_node_id", "action_id"):
        assert row[key] is None, f"{key} must not be invented for legacy rows"

    # No link row was created for it.
    assert await _count(stack, "audit_links") == 0


# ---------------------------------------------------------------------------
# T19b — the forensic schema is unchanged
# ---------------------------------------------------------------------------


async def test_audit_events_schema_is_unchanged(stack):
    from app.models.audit_event import AuditEventModel

    model_columns = {c.name for c in AuditEventModel.__table__.columns}
    async with stack.factory() as session:
        migrated = {
            row[1]
            for row in (
                await session.execute(text("PRAGMA table_info(audit_events)"))
            ).all()
        }

    # The pre-V3 shape (migration 002), including the mixin columns. The
    # guarantee is that V3 added NOTHING to this table.
    expected = {
        "id",
        "event_id",
        "actor_id",
        "action",
        "resource",
        "allowed",
        "request_id",
        "details",
        "timestamp",
        "created_at",
        "updated_at",
    }
    assert model_columns == expected
    assert migrated == expected
    # No linkage column leaked onto the model or the table.
    assert await _count(stack, "audit_links") == 0  # table exists and is empty


# ---------------------------------------------------------------------------
# T20 — the audit API shape is additive
# ---------------------------------------------------------------------------


async def test_audit_api_shape_is_additive(stack, monkeypatch):
    from types import SimpleNamespace

    from app import main as app_main

    admin_id = f"admin-{uuid.uuid4().hex[:8]}"
    app_main.security_kernel.grant_permission(admin_id, Permission.ADMIN)
    try:
        async with stack.factory() as session:
            response = await app_main.get_audit_trail(
                db=session,
                current_user=SimpleNamespace(id=admin_id),
                limit=50,
                action=None,
                allowed=None,
            )
    finally:
        app_main.security_kernel.revoke_permission(admin_id, Permission.ADMIN)

    assert set(response) == {"events", "count"}
    assert response["count"] == len(response["events"]) >= 1
    for event in response["events"]:
        assert {
            "event_id",
            "actor_id",
            "action",
            "resource",
            "allowed",
            "request_id",
            "details",
            "timestamp",
        } <= set(event)
        assert {"user_id", "task_id", "run_id", "step_node_id", "action_id"} <= set(event)
        # The admin's own read has no task subject ⇒ nullable, not fabricated.
        if event["action"] == "api:view_audit":
            assert event["task_id"] is None


# ---------------------------------------------------------------------------
# Non-functional — legacy kernel call sites keep working
# ---------------------------------------------------------------------------


async def test_kernel_call_sites_still_work_without_new_kwargs(stack):
    kernel = SecurityKernel()
    kernel.grant_permission("legacy-actor", Permission.READ_FILES)

    decision = kernel.authorize_and_log(
        "legacy-actor", "tool:fs_read", "task:x", ["read_files"]
    )
    assert decision.allowed is True
    assert decision.request_id

    assert await _flush(stack) == 1
    assert await _count(stack, "audit_links") == 0, "no provenance ⇒ no link row"

    async with stack.factory() as session:
        row = (
            await session.execute(
                text("SELECT request_id FROM audit_events LIMIT 1")
            )
        ).mappings().one()
    assert row["request_id"] == decision.request_id


# ---------------------------------------------------------------------------
# No secrets or content reach the audit link rows
# ---------------------------------------------------------------------------


async def test_links_never_contain_content_or_secret(stack):
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="relative.txt",
        allowed=True,
        details={"reason": "write", "size": 5},
        user_id=stack.user_id,
        task_id=stack.task_id,
        run_id=stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    await _flush(stack)

    async with stack.factory() as session:
        dumped = json.dumps(
            [dict(r) for r in (await session.execute(text("SELECT * FROM audit_links"))).mappings()]
        )
    assert "SECRET" not in dumped
    assert "sk-" not in dumped


# ---------------------------------------------------------------------------
# Adapter refusal paths carry provenance (acceptance-audit finding M1)
# ---------------------------------------------------------------------------
# ShellExecAdapter (blocked command), FinalizeAdapter (clamp, quota) and
# CodegenAdapter (clamp) each write a forensic row for an application-level
# refusal. Before this fix those calls passed no linkage, so the row existed
# with no `audit_links` entry even though the trusted context carried the
# subject. Linkage is evidence only: these tests leave the refusal path, its
# decision and its details untouched.

_LINK_FIELDS = ("user_id", "task_id", "run_id", "step_node_id", "action_id")


async def _linked_rows(stack, action: str) -> list[dict]:
    """Audit rows for one action, LEFT JOINed with their link columns."""
    async with stack.factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT a.event_id, a.actor_id, a.action, a.allowed, a.details, "
                    "l.user_id, l.task_id, l.run_id, l.step_node_id, l.action_id "
                    "FROM audit_events a "
                    "LEFT JOIN audit_links l ON l.event_id = a.event_id "
                    "WHERE a.action = :action ORDER BY a.timestamp, a.id"
                ),
                {"action": action},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def _assert_linked_to(rows, stack, action_id: str) -> None:
    assert rows, "the refusal must be audited"
    for row in rows:
        assert row["user_id"] == stack.user_id
        assert row["task_id"] == stack.task_id
        assert row["run_id"] == stack.run_id
        assert row["step_node_id"] == "step-1"
        assert row["action_id"] == action_id


async def test_shell_exec_blocked_command_audit_row_is_linked(stack, tmp_path):
    from integrations.adapters.builtin import ShellExecAdapter

    adapter = ShellExecAdapter(str(tmp_path / "shellws"))
    action_id = live_action()
    result = await adapter.run(
        {"command": "definitely-not-an-allowed-command --flag"},
        ctx_for(stack, action_id=action_id),
    )
    # Policy refused it before any process was spawned.
    assert result.success is False

    assert await _flush(stack) == 1
    rows = await _linked_rows(stack, "tool:shell_exec")
    _assert_linked_to(rows, stack, action_id)
    assert not rows[0]["allowed"]
    assert "allow list" in json.dumps(rows[0]["details"]).lower()


async def test_finalize_clamped_write_audit_row_is_linked(stack, tmp_path):
    from integrations.adapters import FinalizeAdapter

    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.WRITE_FILES)
    registry = AdapterRegistry(kernel)
    registry.register(FinalizeAdapter(stack.workspace))

    ctx = ctx_for(stack)
    outside = str(tmp_path / "outside" / "deliverable.md")
    result = await registry.run("finalize", {"document": "x", "path": outside}, ctx)
    assert result.success is False
    assert ctx.action_id, "the registry issues the action id"

    # The kernel's allow and the adapter's clamp refusal, both linked.
    assert await _flush(stack) == 2
    rows = await _linked_rows(stack, "tool:finalize")
    _assert_linked_to(rows, stack, ctx.action_id)
    assert {bool(row["allowed"]) for row in rows} == {True, False}

    # Graph evidence for the same invocation is unchanged and consistent.
    async with stack.factory() as session:
        events = (
            await session.execute(
                text(
                    "SELECT event_type, user_id, task_id, run_id, step_node_id "
                    "FROM graph_events WHERE action_id = :aid ORDER BY seq"
                ),
                {"aid": ctx.action_id},
            )
        ).mappings().all()
    assert "tool.attempted" in {event["event_type"] for event in events}
    for event in events:
        assert event["user_id"] == stack.user_id
        assert event["task_id"] == stack.task_id
        assert event["run_id"] == stack.run_id
        assert event["step_node_id"] == "step-1"


async def test_finalize_quota_refusal_audit_row_is_linked(stack):
    from integrations.adapters import FinalizeAdapter
    from services.execution.workspace_quota import WorkspaceQuota

    adapter = FinalizeAdapter(
        stack.workspace,
        quota=WorkspaceQuota(max_total_bytes=16, max_file_bytes=16),
    )
    action_id = live_action()
    result = await adapter.run(
        {"document": "x" * 128}, ctx_for(stack, action_id=action_id)
    )
    assert result.success is False
    assert "quota" in (result.error or "").lower()

    assert await _flush(stack) == 1
    _assert_linked_to(await _linked_rows(stack, "tool:finalize"), stack, action_id)


async def test_codegen_rejected_path_audit_row_is_linked(stack, tmp_path, monkeypatch):
    from integrations.adapters import CodegenAdapter

    for var in ("RAIVISOX_LLM_API_KEY", "OPENAI_API_KEY", "RAIVISOX_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    adapter = CodegenAdapter(stack.workspace)
    assert adapter._llm_usable is False

    action_id = live_action()
    outside = str(tmp_path / "outside" / "module.py")
    result = await adapter.run(
        {"goal": "audit linkage probe", "path": outside},
        ctx_for(stack, action_id=action_id),
    )
    assert result.success is False
    assert "outside the workspace" in (result.error or "")

    assert await _flush(stack) == 1
    _assert_linked_to(await _linked_rows(stack, "tool:codegen"), stack, action_id)


async def test_refusal_without_trusted_provenance_creates_no_link(stack, tmp_path):
    from integrations.adapters import AdapterContext
    from integrations.adapters.finalize import FinalizeAdapter

    # A legacy caller: no user/task/run/action. Whatever is absent stays None;
    # nothing is invented to fill the link row.
    adapter = FinalizeAdapter(stack.workspace)
    ctx = AdapterContext(actor_id="pipeline-service")
    result = await adapter.run(
        {"document": "x", "path": str(tmp_path / "nope.md")}, ctx
    )
    assert result.success is False

    assert await _flush(stack) == 1
    rows = await _linked_rows(stack, "tool:finalize")
    assert len(rows) == 1
    for field in _LINK_FIELDS:
        assert rows[0][field] is None, f"{field} must not be fabricated"


async def test_link_failure_rolls_back_newly_linked_adapter_row(stack, tmp_path, monkeypatch):
    import app.models.audit_link as link_module
    from integrations.adapters.finalize import FinalizeAdapter

    adapter = FinalizeAdapter(stack.workspace)
    action_id = live_action()
    result = await adapter.run(
        {"document": "x", "path": str(tmp_path / "outside.md")},
        ctx_for(stack, action_id=action_id),
    )
    assert result.success is False
    assert AuditTrail.instance().pending_count() == 1

    def _boom(*_args, **_kwargs):
        raise RuntimeError("link insert failed")

    monkeypatch.setattr(link_module, "AuditLink", _boom)
    with pytest.raises(RuntimeError):
        await _flush(stack)

    assert await _count(stack, "audit_events") == 0, "no orphan audit row"
    assert await _count(stack, "audit_links") == 0, "no orphan link"
    assert AuditTrail.instance().pending_count() == 1, "event stays queued"

    monkeypatch.undo()
    assert await _flush(stack) == 1
    _assert_linked_to(await _linked_rows(stack, "tool:finalize"), stack, action_id)
    assert await _count(stack, "audit_events") == 1
    assert await _count(stack, "audit_links") == 1
