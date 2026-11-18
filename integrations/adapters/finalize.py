"""Deliverable finalize adapter — the sensible default for pipeline steps.

Every pipeline step resolves to a REAL adapter. Steps that declare no
required capabilities (typically the final "deliver" step of a plan) get
this adapter: it composes the task's deliverable document from the
parameters it is given and WRITES A REAL FILE into the task workspace via
the allowlist-enforced FileWriterTool.

Honesty contract: the adapter performs exactly two real operations —
document composition from its inputs and a real, clamped file write. It
does not pretend to "understand" or "deliver" anything beyond that, and
its output records the artifact path and size so callers can verify the
file exists on disk. The document CONTENT is built by the pipeline from
real dependency outputs; this adapter never invents content.
"""

import os
import time
from typing import Any

from integrations.adapters.base import AdapterContext, AdapterResult, ToolAdapter
from integrations.tools.file_writer import FileWriterTool
from services.execution.workspace_quota import (
    WorkspaceQuota,
    WorkspaceQuotaCache,
    WorkspaceQuotaExceeded,
)
from services.security.audit_trail import AuditTrail
from services.security.path_containment import contained_path


def _linkage_kwargs(ctx: AdapterContext) -> dict[str, Any]:
    """Audit provenance from the trusted context (None when absent).

    Same contract as `integrations.adapters.builtin._linkage_kwargs`:
    linkage is evidence only, never an input to any decision, and a missing
    field is passed as None rather than invented.
    """
    return {
        "user_id": getattr(ctx, "user_id", "") or None,
        "task_id": getattr(ctx, "task_id", "") or None,
        "run_id": getattr(ctx, "execution_run_id", "") or None,
        "step_node_id": getattr(ctx, "step_id", "") or None,
        "action_id": getattr(ctx, "action_id", "") or None,
    }


class FinalizeAdapter(ToolAdapter):
    """Write the task deliverable to a real file in the workspace.

    Params:
      document (required, non-empty str): the deliverable content, built by
        the pipeline from real dependency outputs.
      path (optional): workspace-relative path for the artifact
        (default: deliverables/result.md). Clamped to the workspace.
      title (optional): human-readable artifact title recorded in output.
    """

    id = "finalize"
    description = (
        "Deliverable finalization: composes the pipeline's deliverable document "
        "and writes it to a real file in the task workspace "
        "(allowlist-enforced). Default adapter for steps with no capabilities."
    )
    # It performs a real file write, so it requires the same permission as
    # fs_write — granted, not inherited.
    kernel_permissions: list[str] = ["write_files"]

    def __init__(self, workspace: str, quota: WorkspaceQuota | None = None):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)
        self._writer = FileWriterTool(allowed_directories=[self.workspace])
        # V2 closeout (T8): the deliverable write is quota-gated before any
        # bytes land, like every other application-layer workspace write.
        self._enforcers = WorkspaceQuotaCache(
            quota if quota is not None else WorkspaceQuota.from_env())

    def _clamp(self, path: str, scope: str | None = None) -> str | None:
        """Resolve a request path against the workspace; None if outside.

        SECURITY (Phase 0): symlink-resolving containment. When the write
        targets a task scope, containment is enforced against THAT scope
        (matching builtin._clamp and the file tools) — so an absolute
        client-supplied path into another task's directory is rejected,
        not merely clamped to the shared root.
        """
        return contained_path(path, scope if scope else self.workspace)

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        started = time.perf_counter()

        document = params.get("document")
        if not isinstance(document, str) or not document.strip():
            return AdapterResult(
                success=False,
                tool_id=self.id,
                error="finalize requires a non-empty 'document' parameter",
            )

        path = str(params.get("path", "")).strip() or "deliverables/result.md"
        title = str(params.get("title", "")).strip() or "Task Deliverable"

        # SECURITY: scope the write into THIS task's directory (kernel
        # context is trusted; client params never choose the scope) so
        # concurrent tasks cannot write into each other's workspace.
        task_id = getattr(ctx, "task_id", None)
        scope: str | None = None
        if task_id:
            safe = "".join(c for c in str(task_id) if c.isalnum() or c == "-")
            if safe:
                scope = os.path.join(self.workspace, safe)
                os.makedirs(scope, exist_ok=True)
                path = os.path.join(scope, path)

        clamped = self._clamp(path, scope)
        if clamped is None:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:finalize", resource=path,
                allowed=False, details={"reason": "path outside workspace"},
                **_linkage_kwargs(ctx),
            )
            return AdapterResult(
                success=False,
                tool_id=self.id,
                error=f"Access denied: '{path}' is outside the workspace",
            )

        enforcer = self._enforcers.for_scope(scope if scope else self.workspace)
        try:
            with enforcer.reserve(clamped, len(document.encode("utf-8"))):
                result = await self._writer.execute({
                    "path": clamped,
                    "content": document,
                    "overwrite": True,  # retries legitimately rewrite the deliverable
                    "create_directories": True,
                })
        except WorkspaceQuotaExceeded as exc:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:finalize", resource=path,
                allowed=False,
                details={"reason": "workspace quota", **exc.status()},
                **_linkage_kwargs(ctx),
            )
            return AdapterResult(
                success=False, tool_id=self.id,
                error=f"Workspace write refused: {exc}",
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not result.success:
            return AdapterResult(
                success=False,
                tool_id=self.id,
                error=result.error,
                execution_time_ms=result.execution_time_ms,
            )

        output = dict(result.output or {})
        output.update({
            "artifact_title": title,
            "characters": len(document),
        })
        return AdapterResult(
            success=True,
            tool_id=self.id,
            output=output,
            execution_time_ms=elapsed_ms,
        )
