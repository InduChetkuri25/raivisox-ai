"""Adapter registry: registration, capability selection, kernel-gated runs."""

from typing import Any

from integrations.adapters.base import (
    CAPABILITY_TO_ADAPTER,
    DEFAULT_ADAPTER_ID,
    AdapterContext,
    AdapterResult,
    ToolAdapter,
)
from services.security.security_kernel import SecurityKernel


class UnknownToolError(KeyError):
    """Raised when a requested adapter id is not registered."""


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
        """
        adapter = self.get(adapter_id)

        if adapter.kernel_permissions:
            decision = self._kernel.authorize_and_log(
                actor_id=ctx.actor_id,
                action=f"tool:{adapter.id}",
                resource=ctx.resource,
                permissions=list(adapter.kernel_permissions),
                actor_type="service",
            )
            if not decision.allowed:
                return AdapterResult(
                    success=False,
                    tool_id=adapter.id,
                    error=f"Permission denied by security kernel: {decision.reason}",
                    permission_denied=True,
                )

        return await adapter.run(params, ctx)
