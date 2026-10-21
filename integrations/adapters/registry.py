"""Adapter registry: registration, capability selection, kernel-gated runs."""

import uuid
from typing import Any, Optional

from integrations.adapters.base import (
    CAPABILITY_TO_ADAPTER,
    DEFAULT_ADAPTER_ID,
    AdapterContext,
    AdapterResult,
    ToolAdapter,
)
from services.graph import actions as graph_actions
from services.graph import events as graph_events
from services.graph import schema as graph_schema
from services.security.security_kernel import SecurityKernel


class UnknownToolError(KeyError):
    """Raised when a requested adapter id is not registered."""


def _linkage(ctx: AdapterContext) -> dict[str, Optional[str]]:
    """Provenance fields from the trusted context (None when absent)."""
    return {
        "user_id": getattr(ctx, "user_id", "") or None,
        "task_id": getattr(ctx, "task_id", "") or None,
        "run_id": getattr(ctx, "execution_run_id", "") or None,
        "step_node_id": getattr(ctx, "step_id", "") or None,
    }


async def _emit(
    ctx: AdapterContext,
    event_type: str,
    outcome: str,
    *,
    action_id: str,
    tool_id: str,
    payload: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    evidence_class: str = graph_schema.OBSERVED,
) -> None:
    """Record one lifecycle fact. Never raises for recording failures.

    ``emit`` itself counts/reports failures; only a provenance integrity fault
    (a ctx whose identity does not match the database) raises, and that must
    surface — an unprovable invocation is a bug, not a transient error.
    """
    await graph_events.emit(
        event_type=event_type,
        outcome=outcome,
        actor_id=ctx.actor_id,
        actor_type="service",
        user_id=getattr(ctx, "user_id", "") or "",
        task_id=getattr(ctx, "task_id", "") or "",
        run_id=getattr(ctx, "execution_run_id", "") or None,
        step_node_id=getattr(ctx, "step_id", "") or None,
        tool_id=tool_id,
        action_id=action_id,
        evidence_class=evidence_class,
        correlation_id=correlation_id,
        payload=payload,
    )


class AdapterRegistry:
    """Registry and gatekeeper for tool adapters."""

    def __init__(self, kernel: SecurityKernel):
        self._kernel = kernel
        self._adapters: dict[str, ToolAdapter] = {}

    # -- registration -------------------------------------------------

    def register(self, adapter: ToolAdapter) -> None:
        if not adapter.id:
            raise ValueError("Adapter must have a non-empty id")
        self._adapters[adapter.id] = adapter

    def get(self, adapter_id: str) -> ToolAdapter:
        if adapter_id not in self._adapters:
            raise UnknownToolError(adapter_id)
        return self._adapters[adapter_id]

    def has(self, adapter_id: str) -> bool:
        return adapter_id in self._adapters

    def list_adapters(self) -> list[dict[str, Any]]:
        return [a.describe() for a in self._adapters.values()]

    # -- selection ----------------------------------------------------

    def select(self, required_capabilities: list[str]) -> str | None:
        """Pick a real adapter for a step.

        Resolution order:
          1. The first capability that maps to a registered adapter.
          2. The default adapter ("finalize") when the step declares NO
             capabilities at all — it performs the real deliverable-write
             operation, so the pipeline contains no simulated steps.
          3. None when the step HAS capabilities but none maps to a real
             adapter — the caller keeps such steps clearly marked as
             simulated (never claim a tool we cannot truly perform).
        """
        caps = required_capabilities or []
        for cap in caps:
            adapter_id = CAPABILITY_TO_ADAPTER.get(cap)
            if adapter_id and self.has(adapter_id):
                return adapter_id
        if not caps and self.has(DEFAULT_ADAPTER_ID):
            return DEFAULT_ADAPTER_ID
        return None

    # -- gated execution ----------------------------------------------

    async def run(
        self,
        adapter_id: str,
        params: dict[str, Any],
        ctx: AdapterContext,
    ) -> AdapterResult:
        """Run an adapter after SecurityKernel authorization + audit log.

        Permission rejection is a first-class outcome (permission_denied),
        so the pipeline can treat it like any other step failure.

        V3 Phase 1 records the invocation around the (unchanged) kernel gate:
        attempted → allowed/denied → executed/failed. Recording never changes
        the returned result and never grants authority.
        """
        adapter = self.get(adapter_id)
        action_id = str(uuid.uuid4())
        graph_actions.register(action_id)

        await _guard(
            _emit(
                ctx,
                graph_schema.TOOL_ATTEMPTED,
                graph_schema.ATTEMPTED,
                action_id=action_id,
                tool_id=adapter.id,
                payload={"resource_ref": (ctx.resource or "")[:200] or None},
            )
        )
        ctx.action_id = action_id

        correlation: Optional[str] = None
        try:
            if adapter.kernel_permissions:
                decision = self._kernel.authorize_and_log(
                    actor_id=ctx.actor_id,
                    action=f"tool:{adapter.id}",
                    resource=ctx.resource,
                    permissions=list(adapter.kernel_permissions),
                    actor_type="service",
                    action_id=action_id,
                    **_linkage(ctx),
                )
                correlation = decision.request_id or None
                await _guard(
                    _emit(
                        ctx,
                        graph_schema.TOOL_ALLOWED
                        if decision.allowed
                        else graph_schema.TOOL_DENIED,
                        graph_schema.ALLOWED if decision.allowed else graph_schema.DENIED,
                        action_id=action_id,
                        tool_id=adapter.id,
                        correlation_id=correlation,
                        payload=(
                            {"granted_permissions": list(decision.granted_permissions)}
                            if decision.allowed
                            else {
                                "denied_permissions": list(decision.denied_permissions),
                                "reason": (decision.reason or "")[:200],
                            }
                        ),
                    )
                )
                if not decision.allowed:
                    return AdapterResult(
                        success=False,
                        tool_id=adapter.id,
                        error=f"Permission denied by security kernel: {decision.reason}",
                        permission_denied=True,
                    )

            try:
                result = await adapter.run(params, ctx)
            except Exception:
                await _guard(
                    _emit(
                        ctx,
                        graph_schema.TOOL_FAILED,
                        graph_schema.FAILED,
                        action_id=action_id,
                        tool_id=adapter.id,
                        correlation_id=correlation,
                        payload={"error_kind": "exception"},
                    )
                )
                raise

            if result.success:
                terminal = (
                    graph_schema.TOOL_EXECUTED,
                    graph_schema.EXECUTED,
                    {"duration_ms": int(result.execution_time_ms or 0)},
                )
            elif result.permission_denied:
                terminal = (
                    graph_schema.TOOL_DENIED,
                    graph_schema.DENIED,
                    {"reason": "permission_denied", "denied_permissions": []},
                )
            else:
                terminal = (
                    graph_schema.TOOL_FAILED,
                    graph_schema.FAILED,
                    {
                        "duration_ms": int(result.execution_time_ms or 0),
                        "error_kind": "adapter_failure",
                    },
                )
            await _guard(
                _emit(
                    ctx,
                    terminal[0],
                    terminal[1],
                    action_id=action_id,
                    tool_id=adapter.id,
                    correlation_id=correlation,
                    payload=terminal[2],
                )
            )
            return result
        finally:
            graph_actions.discard(action_id)


async def _guard(coro) -> None:
    """Await an emission, containing any failure that is not an integrity fault."""
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - recording must not fail execution
        from services.graph.provenance import ProvenanceError

        if isinstance(exc, ProvenanceError):
            raise
        # emit already counts failures; this is the last line of defence.
        return
