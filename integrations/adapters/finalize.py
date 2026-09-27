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
from services.security.audit_trail import AuditTrail
from services.security.path_containment import contained_path


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

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)
        self._writer = FileWriterTool(allowed_directories=[self.workspace])

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
            )
            return AdapterResult(
                success=False,
                tool_id=self.id,
                error=f"Access denied: '{path}' is outside the workspace",
            )

        result = await self._writer.execute({
            "path": clamped,
            "content": document,
            "overwrite": True,  # retries legitimately rewrite the deliverable
            "create_directories": True,
        })
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
