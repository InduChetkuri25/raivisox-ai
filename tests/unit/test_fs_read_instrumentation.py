"""V3 Phase 1 acceptance tests — filesystem-read instrumentation (T21–T24).

The read boundary is the application-level fact: what the adapter observed,
not what a sandboxed child process did. Nothing here claims visibility the
application does not have.
"""

from __future__ import annotations

import json

import pytest

from integrations.adapters import AdapterRegistry, FileReadAdapter
from services.graph import schema
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import (
    fetch_rows,
    phase1_stack,  # noqa: F401 — fixture
    ctx_for,
    write_scope_file,
)


@pytest.fixture
async def stack(phase1_stack):  # noqa: F811
    return phase1_stack


def _registry() -> AdapterRegistry:
    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.READ_FILES)
    registry = AdapterRegistry(kernel)
    return registry


# ---------------------------------------------------------------------------
# T21 — a successful read is observed and audited
# ---------------------------------------------------------------------------


async def test_successful_read_emits_observed_and_audits(stack):
    write_scope_file(stack, "src/data.txt", "0123456789")
    registry = _registry()
    registry.register(FileReadAdapter(stack.workspace))

    result = await registry.run("fs_read", {"path": "src/data.txt"}, ctx_for(stack))
    assert result.success, result.error
    assert result.output["content"] == "0123456789"

    rows = [r for r in await fetch_rows(stack) if r["event_type"] == schema.RESOURCE_READ_OBSERVED]
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == schema.EXECUTED
    assert row["target_id"] == f"res:{stack.task_id}/src/data.txt"
    assert row["target_kind"] == "resource"
    assert row["payload"]["size"] == 10
    assert row["payload"]["relpath"] == "src/data.txt"
    assert row["evidence_class"] == schema.OBSERVED

    from services.security.audit_trail import AuditTrail

    audits = [
        e
        for e in AuditTrail.instance().get_events(action="tool:fs_read")
        if e.allowed is True
    ]
    assert audits, "a successful read must be audited"
    assert audits[-1].details["size"] == 10
    assert audits[-1].task_id == stack.task_id


# ---------------------------------------------------------------------------
# T22 — a refusal is visible without leaking a path
# ---------------------------------------------------------------------------


async def test_denied_read_emits_denied_and_no_content(stack):
    registry = _registry()
    registry.register(FileReadAdapter(stack.workspace))

    result = await registry.run("fs_read", {"path": "../x"}, ctx_for(stack))
    assert result.success is False

    rows = [r for r in await fetch_rows(stack) if r["event_type"] == schema.RESOURCE_READ_DENIED]
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == schema.DENIED
    assert row["target_id"] is None
    assert row["payload"] == {"reason": "outside_workspace", "requested_basename": "x"}

    dumped = json.dumps(row, default=str)
    assert ".." not in dumped
    assert stack.workspace.replace("\\", "/") not in dumped

    from services.security.audit_trail import AuditTrail

    denials = [
        e
        for e in AuditTrail.instance().get_events(action="tool:fs_read")
        if e.allowed is False
    ]
    assert denials, "the refusal must also be audited"


# ---------------------------------------------------------------------------
# T23 — a tool error is not a policy denial
# ---------------------------------------------------------------------------


async def test_missing_file_is_failed_not_denied(stack):
    registry = _registry()
    registry.register(FileReadAdapter(stack.workspace))

    result = await registry.run("fs_read", {"path": "nope.txt"}, ctx_for(stack))
    assert result.success is False

    rows = await fetch_rows(stack)
    observed = [r for r in rows if r["event_type"] == schema.RESOURCE_READ_OBSERVED]
    assert len(observed) == 1
    assert observed[0]["outcome"] == schema.FAILED
    assert observed[0]["payload"]["error_kind"] == "not_found"
    assert observed[0]["target_id"] == f"res:{stack.task_id}/nope.txt"
    assert not any(r["event_type"] == schema.RESOURCE_READ_DENIED for r in rows)


# ---------------------------------------------------------------------------
# T24 — content can never enter telemetry
# ---------------------------------------------------------------------------


async def test_read_payload_never_contains_file_content(stack):
    sentinel = "SENTINEL-CONTENT-DO-NOT-RECORD-7f3a"
    write_scope_file(stack, "secret.txt", sentinel)
    registry = _registry()
    registry.register(FileReadAdapter(stack.workspace))

    result = await registry.run("fs_read", {"path": "secret.txt"}, ctx_for(stack))
    assert result.success, result.error

    from services.security.audit_trail import AuditTrail

    for row in await fetch_rows(stack):
        assert sentinel not in json.dumps(row, default=str)
        assert sentinel not in (row["target_id"] or "")

    for event in AuditTrail.instance().get_events():
        assert sentinel not in json.dumps(event.to_dict(), default=str)
