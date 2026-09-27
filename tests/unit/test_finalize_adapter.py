"""Tests for the finalize adapter (default adapter for capability-less steps).

The finalize adapter performs two real operations: it composes a
deliverable document from parameters derived from real dependency outputs,
and it writes a real file to the workspace via the allowlist-enforced
FileWriterTool. Tests assert REAL filesystem effects and honest rejection
of empty/invalid documents.
"""

import os

import pytest

from integrations.adapters import (
    AdapterContext,
    AdapterRegistry,
    FinalizeAdapter,
)
from integrations.adapters.base import DEFAULT_ADAPTER_ID
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel


def ctx_for(actor="tester", task="t1", step="s1"):
    return AdapterContext(
        actor_id=actor,
        task_id=task,
        step_id=step,
        resource=f"task:{task}/step:{step}",
    )


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


@pytest.fixture
def kernel():
    k = SecurityKernel()
    k.grant_permission("tester", Permission.WRITE_FILES)
    return k


@pytest.fixture
def registry(workspace, kernel):
    reg = AdapterRegistry(kernel)
    reg.register(FinalizeAdapter(workspace))
    return reg


@pytest.fixture(autouse=True)
def _clear_audit():
    AuditTrail.instance().clear()
    yield
    AuditTrail.instance().clear()


# ---------------------------------------------------------------------------
# Real execution
# ---------------------------------------------------------------------------


async def test_finalize_writes_real_file(registry, workspace):
    r = await registry.run(
        "finalize",
        {"document": "# Deliverable\n\nReal content.", "title": "My Deliverable"},
        ctx_for(),
    )
    assert r.success, r.error
    assert r.output["path"]
    artifact = r.output["path"]
    # FileWriterTool returns an absolute path in output["path"].
    assert os.path.exists(artifact), f"artifact {artifact} must exist on disk"
    with open(artifact, encoding="utf-8") as f:
        content = f.read()
    assert "Real content." in content
    assert content.startswith("# Deliverable")
    assert r.output["artifact_title"] == "My Deliverable"
    assert r.output["characters"] == len("# Deliverable\n\nReal content.")


async def test_finalize_creates_nested_directories(registry):
    r = await registry.run(
        "finalize",
        {"document": "deep content", "path": "a/b/c/result.md"},
        ctx_for(),
    )
    assert r.success, r.error
    assert os.path.exists(r.output["path"])
    assert r.output["created"] is True


async def test_finalize_overwrites_on_retry(registry, workspace):
    p1 = await registry.run("finalize", {"document": "attempt 1"}, ctx_for())
    assert p1.success, p1.error
    p2 = await registry.run("finalize", {"document": "attempt 2"}, ctx_for())
    assert p2.success, p2.error
    same = os.path.normcase(os.path.abspath(p1.output["path"])) == os.path.normcase(
        os.path.abspath(p2.output["path"])
    )
    assert same, "retries rewrite the same default deliverable path"
    with open(p2.output["path"], encoding="utf-8") as f:
        assert "attempt 2" in f.read()


async def test_finalize_rejects_empty_document(registry):
    r = await registry.run("finalize", {"document": "   "}, ctx_for())
    assert not r.success
    assert "non-empty 'document'" in (r.error or "")


async def test_finalize_rejects_missing_document(registry):
    r = await registry.run("finalize", {}, ctx_for())
    assert not r.success
    assert "document" in (r.error or "")


async def test_finalize_path_outside_workspace_denied(registry, workspace):
    target = os.path.join(str(workspace), "..", "escape_should_not_exist.md")
    r = await registry.run(
        "finalize", {"document": "x", "path": target}, ctx_for()
    )
    assert not r.success
    assert "outside the workspace" in (r.error or "")
    assert not os.path.exists(
        os.path.join(os.path.dirname(str(workspace)), "escape_should_not_exist.md")
    )


async def test_finalize_permission_denied_without_kernel_grant(workspace):
    kernel = SecurityKernel()  # no grants
    reg = AdapterRegistry(kernel)
    reg.register(FinalizeAdapter(workspace))
    r = await reg.run("finalize", {"document": "x"}, ctx_for(actor="unprivileged"))
    assert not r.success
    assert r.permission_denied
    denials = [
        e for e in AuditTrail.instance().get_events(actor_id="unprivileged")
        if not e.allowed
    ]
    assert denials, "denial must be audited"


async def test_finalize_default_path_is_deliverables_result_md(registry, workspace):
    r = await registry.run("finalize", {"document": "content"}, ctx_for())
    assert r.success, r.error
    normalized = os.path.normcase(os.path.abspath(r.output["path"]))
    # ctx_for() uses task id "t1" → the write is scoped into the task's own
    # directory under the workspace (per-task isolation).
    expected = os.path.normcase(
        os.path.abspath(
            os.path.join(str(workspace), "t1", "deliverables", "result.md")
        )
    )
    assert normalized == expected


# ---------------------------------------------------------------------------
# Selection semantics: default adapter fills the capability-less gap
# ---------------------------------------------------------------------------


def test_default_adapter_id_is_finalize():
    assert DEFAULT_ADAPTER_ID == "finalize"


def test_select_empty_capabilities_returns_finalize(workspace, kernel):
    reg = AdapterRegistry(kernel)
    reg.register(FinalizeAdapter(workspace))
    assert reg.select([]) == "finalize"
    assert reg.select(None) == "finalize"


def test_select_unmapped_capability_still_none(workspace, kernel):
    """Capabilities with no real adapter must stay simulated — no silent default."""
    reg = AdapterRegistry(kernel)
    reg.register(FinalizeAdapter(workspace))
    assert reg.select(["code_generation"]) is None
    assert reg.select(["test_execution"]) is None


def test_select_capability_beats_default(workspace, kernel):
    """A step with real capabilities must get its real adapter, not finalize."""
    reg = AdapterRegistry(kernel)
    reg.register(FinalizeAdapter(workspace))

    from integrations.adapters import TextTransformAdapter

    reg.register(TextTransformAdapter())
    assert reg.select(["data_transformation"]) == "text_transform"
    # Mixed: a real capability wins over the default even if others are unmapped.
    assert reg.select(["data_transformation", "code_generation"]) == "text_transform"
