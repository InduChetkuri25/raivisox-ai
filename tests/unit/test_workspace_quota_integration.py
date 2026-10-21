"""V3 Phase 1 acceptance tests — write/quota instrumentation (T28–T29).

A quota refusal becomes attributable evidence; a successful write is recorded
as *observed*, never promoted to verified.
"""

from __future__ import annotations

import os

import pytest

from integrations.adapters import AdapterRegistry, FileWriteAdapter
from services.execution.workspace_quota import WorkspaceQuota
from services.graph import schema
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import fetch_rows, phase1_stack  # noqa: F401
from tests.unit.v3_phase1_helpers import ctx_for


@pytest.fixture
async def stack(phase1_stack):  # noqa: F811
    return phase1_stack


def _registry(adapter: FileWriteAdapter) -> AdapterRegistry:
    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.WRITE_FILES)
    registry = AdapterRegistry(kernel)
    registry.register(adapter)
    return registry


# ---------------------------------------------------------------------------
# T28 — a quota refusal is recorded and writes nothing
# ---------------------------------------------------------------------------


async def test_write_denial_emits_resource_write_denied(stack):
    quota = WorkspaceQuota(
        max_total_bytes=None,
        max_file_bytes=4,
        max_entries=None,
        max_depth=None,
    )
    registry = _registry(FileWriteAdapter(stack.workspace, quota=quota))

    result = await registry.run(
        "fs_write", {"path": "big.txt", "content": "x" * 100}, ctx_for(stack)
    )
    assert result.success is False

    rows = await fetch_rows(stack)
    denied = [r for r in rows if r["event_type"] == schema.RESOURCE_WRITE_DENIED]
    assert len(denied) == 1
    row = denied[0]
    assert row["outcome"] == schema.DENIED
    assert row["payload"]["reason"] == "quota"
    assert row["payload"]["bytes"] == 100
    assert row["payload"]["quota_kind"] == "workspace_quota"
    assert not any(r["event_type"] == schema.RESOURCE_WRITE_COMMITTED for r in rows)

    on_disk = os.path.join(stack.workspace, stack.task_id, "big.txt")
    assert not os.path.exists(on_disk)


# ---------------------------------------------------------------------------
# T29 — a committed write is observed, never verified
# ---------------------------------------------------------------------------


async def test_write_commit_is_observed_not_verified(stack):
    registry = _registry(FileWriteAdapter(stack.workspace))

    result = await registry.run(
        "fs_write", {"path": "notes/item.txt", "content": "hello"}, ctx_for(stack)
    )
    assert result.success, result.error

    rows = await fetch_rows(stack)
    committed = [r for r in rows if r["event_type"] == schema.RESOURCE_WRITE_COMMITTED]
    assert len(committed) == 1
    row = committed[0]
    assert row["evidence_class"] == schema.OBSERVED
    assert row["outcome"] == schema.EXECUTED
    assert row["target_id"] == f"res:{stack.task_id}/notes/item.txt"
    assert row["payload"]["bytes"] == 5
    assert row["payload"]["created"] is True
    assert row["payload"]["relpath"] == "notes/item.txt"

    on_disk = os.path.join(stack.workspace, stack.task_id, "notes", "item.txt")
    assert os.path.isfile(on_disk)
