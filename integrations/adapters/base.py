"""Tool adapter abstraction.

An adapter is the unit the execution pipeline schedules. It wraps a real
tool (filesystem tools, the terminal sandbox, a Python subprocess runner,
or a pure transformation) and declares which SecurityKernel permissions
it requires. The AdapterRegistry gates every privileged run through
`SecurityKernel.authorize_and_log` before the tool touches anything.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterContext:
    """Per-execution context handed to every adapter run."""

    actor_id: str
    task_id: str = ""
    step_id: str = ""
    resource: str = ""
    workspace: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # V3 Phase 1 trusted provenance (set by the pipeline / registry; never
    # from tool params). Defaults keep every existing construction site valid.
    user_id: str = ""            # owning user (from tasks.user_id)
    execution_run_id: str = ""   # execution_runs.id for this run
    action_id: str = ""          # this invocation's id (registry-issued)


@dataclass
class AdapterResult:
    """Result of a single adapter run."""

    success: bool
    tool_id: str
    output: Any = None
    error: str | None = None
    execution_time_ms: int = 0
    permission_denied: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tool_id": self.tool_id,
            "output": self.output,
            "error": self.error,
            "execution_time_ms": self.execution_time_ms,
            "permission_denied": self.permission_denied,
        }


def sandbox_provenance(ctx: "AdapterContext | None") -> dict[str, str]:
    """Trusted identity for a sandboxed run, from the kernel AdapterContext.

    The worker backend needs to attribute a remote execution to a task, run and
    step. Those values are server-derived (the pipeline builds the context; a
    tool parameter can never set them), so a request cannot forge them. Absent
    values are omitted rather than invented: the worker refuses an anonymous
    request instead of accepting a guessed owner.
    """
    if ctx is None:
        return {}
    out: dict[str, str] = {}
    for key, value in (
        ("task_id", getattr(ctx, "task_id", "")),
        ("run_id", getattr(ctx, "execution_run_id", "")),
        ("step_id", getattr(ctx, "step_id", "")),
        ("user_id", getattr(ctx, "user_id", "")),
    ):
        text = str(value or "").strip()
        if text:
            out[key] = text
    return out


class ToolAdapter(ABC):
    """Base class for pipeline-executable tool adapters."""

    id: str = ""
    description: str = ""
    # SecurityKernel Permission values that must be granted to the acting
    # actor before `run` is invoked. Empty list = no privileged access.
    kernel_permissions: list[str] = []

    @abstractmethod
    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        """Execute the adapter's real operation."""

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "kernel_permissions": list(self.kernel_permissions),
        }


# Intent-engine capability -> adapter id. Only capabilities that have a
# REAL, safe implementation are mapped; anything unmapped stays simulated
# and is reported as such. Never map a capability we cannot truly perform.
CAPABILITY_TO_ADAPTER: dict[str, str] = {
    "filesystem_read": "fs_read",
    "filesystem_write": "fs_write",
    "code_execution": "python_exec",
    "terminal_execute": "shell_exec",
    "data_transformation": "text_transform",
    # Code generation -> the real codegen adapter (writes a runnable
    # module, compile-checks it, smoke-runs it in a subprocess).
    "code_generation": "codegen",
    # Research/network capabilities -> the real HTTP GET adapter.
    "web_read": "http_get",
    "web_search": "http_get",
    # Reasoning/text capabilities -> the real text-intelligence adapter
    # (deterministic local engine, or a configured LLM backend).
    "intent_parsing": "text_intel",
    "planning": "text_intel",
    "document_generation": "text_intel",
    "error_analysis": "text_intel",
    "architecture_design": "text_intel",
}

# Default adapter id for steps that declare NO required capabilities
# (e.g. the final "deliver" step of a plan). The adapter performs a real
# operation — composing and writing the task deliverable to the workspace.
DEFAULT_ADAPTER_ID = "finalize"
