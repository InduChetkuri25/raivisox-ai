"""Model and Tool Router — selects the right models and tools for a task.

Evaluates available models and tools against task requirements
and selects the most appropriate combination.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ModelCategory(str, Enum):
    """Categories of AI models."""

    GENERAL = "general"
    CODE = "code"
    REASONING = "reasoning"
    FAST = "fast"
    QUALITY = "quality"


class ToolCategory(str, Enum):
    """Categories of tools."""

    FILESYSTEM = "filesystem"
    TERMINAL = "terminal"
    NETWORK = "network"
    CODE = "code"
    DATA = "data"
    MEDIA = "media"
    UTILITY = "utility"


@dataclass
class ModelInfo:
    """Information about an available AI model."""

    id: str
    name: str
    provider: str
    category: ModelCategory
    context_window: int = 0
    supports_streaming: bool = False
    supports_vision: bool = False
    quality_rating: float = 0.5
    latency_estimate_ms: int = 0
    cost_per_1k_tokens: float = 0.0
    available: bool = True
    description: str = ""

    def score_for_task(
        self, task_type: str, required_capabilities: list[str], risk_level: str,
    ) -> float:
        """Score this model for a specific task."""
        score = 0.0

        # Category match
        if task_type == "software_build" and self.category == ModelCategory.CODE:
            score += 3
        elif task_type == "debugging" and self.category == ModelCategory.CODE:
            score += 3
        elif task_type in ("research", "file_operation") and self.category == ModelCategory.GENERAL:
            score += 2

        # Quality vs speed tradeoff
        if risk_level == "high":
            score += self.quality_rating * 2
        else:
            score += (1.0 - self.cost_per_1k_tokens) * 1.5

        return score


@dataclass
class ToolInfo:
    """Information about an available tool."""

    id: str
    name: str
    description: str
    category: ToolCategory
    permissions: list[str] = field(default_factory=list)
    risk_level: str = "low"
    available: bool = True
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    def matches_requirement(self, capability: str) -> bool:
        """Check if this tool satisfies a capability requirement."""
        capability_map = {
            "code_generation": ["code", "write"],
            "code_read": ["code", "read"],
            "filesystem_write": ["filesystem"],
            "filesystem_read": ["filesystem"],
            "terminal_execute": ["terminal"],
            "test_execution": ["terminal", "code"],
            "web_search": ["network"],
            "web_read": ["network"],
            "intent_parsing": ["general"],
            "planning": ["general"],
            "architecture_design": ["code"],
            "error_analysis": ["code", "terminal"],
            "verification": ["filesystem", "terminal"],
            "data_parse": ["data"],
            "document_generation": ["write"],
        }

        keywords = capability_map.get(capability, [capability])
        tool_cat = self.category.value

        return any(kw in tool_cat or kw in self.id or kw in self.name.lower()
                   for kw in keywords)


@dataclass
class RoutingDecision:
    """The routing decision for a task."""

    task_id: str
    selected_model: Optional[ModelInfo] = None
    selected_tools: list[ToolInfo] = field(default_factory=list)
    rationale: str = ""
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


class Router:
    """Routes tasks to appropriate models and tools.

    Evaluates the task requirements against available models and tools
    and selects the best combination for execution.
    """

    def __init__(self):
        self._models: list[ModelInfo] = []
        self._tools: list[ToolInfo] = []
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register default model and tool entries.

        In production, these would be populated from actual provider APIs.
        Here we register placeholder entries that represent the interface.
        """
        # Default model placeholders
        self._models = [
            ModelInfo(
                id="model-general",
                name="General Purpose Model",
                provider="adapter",
                category=ModelCategory.GENERAL,
                context_window=128000,
                supports_streaming=True,
                supports_vision=False,
                quality_rating=0.7,
                latency_estimate_ms=2000,
                cost_per_1k_tokens=0.001,
                available=True,
                description="A general-purpose model for common tasks.",
            ),
            ModelInfo(
                id="model-code",
                name="Code Specialist Model",
                provider="adapter",
                category=ModelCategory.CODE,
                context_window=128000,
                supports_streaming=True,
                supports_vision=False,
                quality_rating=0.85,
                latency_estimate_ms=3000,
                cost_per_1k_tokens=0.003,
                available=True,
                description="A code-specialist model for software development tasks.",
            ),
            ModelInfo(
                id="model-fast",
                name="Fast Model",
                provider="adapter",
                category=ModelCategory.FAST,
                context_window=32000,
                supports_streaming=True,
                supports_vision=False,
                quality_rating=0.5,
                latency_estimate_ms=500,
                cost_per_1k_tokens=0.0005,
                available=True,
                description="A fast, lightweight model for simple tasks.",
            ),
        ]

        # Default tool placeholders
        self._tools = [
            ToolInfo(
                id="filesystem",
                name="Filesystem Tool",
                description="Read and write files on the local filesystem.",
                category=ToolCategory.FILESYSTEM,
                permissions=["FILES_READ", "FILES_WRITE"],
                risk_level="medium",
                available=True,
            ),
            ToolInfo(
                id="terminal",
                name="Terminal Tool",
                description="Execute commands in a sandboxed terminal.",
                category=ToolCategory.TERMINAL,
                permissions=["TERMINAL_EXECUTE"],
                risk_level="high",
                available=True,
            ),
            ToolInfo(
                id="code_editor",
                name="Code Editor Tool",
                description="Read, write, and edit code files with syntax awareness.",
                category=ToolCategory.CODE,
                permissions=["FILES_READ", "FILES_WRITE"],
                risk_level="medium",
                available=True,
            ),
            ToolInfo(
                id="web_client",
                name="Web Client Tool",
                description="Make HTTP requests and read web pages.",
                category=ToolCategory.NETWORK,
                permissions=["EXTERNAL_API", "BROWSER_READ"],
                risk_level="medium",
                available=True,
            ),
            ToolInfo(
                id="calculator",
                name="Calculator Tool",
                description="Perform mathematical calculations safely.",
                category=ToolCategory.UTILITY,
                permissions=[],
                risk_level="low",
                available=True,
            ),
            ToolInfo(
                id="test_runner",
                name="Test Runner Tool",
                description="Run tests and report results.",
                category=ToolCategory.TERMINAL,
                permissions=["TERMINAL_EXECUTE"],
                risk_level="medium",
                available=True,
            ),
        ]

    def register_model(self, model: ModelInfo) -> None:
        """Register a model."""
        self._models.append(model)

    def register_tool(self, tool: ToolInfo) -> None:
        """Register a tool."""
        self._tools.append(tool)

    def route(
        self, task_id: str, task_type: str, required_capabilities: list[str],
        risk_level: str = "low",
    ) -> RoutingDecision:
        """Route a task to the best model and tools.

        Args:
            task_id: The task identifier.
            task_type: The type of task.
            required_capabilities: Capabilities needed.
            risk_level: The risk level of the task.

        Returns:
            A RoutingDecision with the selected model and tools.
        """
        # Select the best model
        selected_model = self._select_model(task_type, required_capabilities, risk_level)

        # Select tools that match required capabilities
        selected_tools = self._select_tools(required_capabilities)

        # Generate rationale
        rationale = self._generate_rationale(
            selected_model, selected_tools, task_type, required_capabilities, risk_level
        )

        # Generate alternatives
        alternatives = self._generate_alternatives(
            selected_model, selected_tools, task_type, required_capabilities
        )

        return RoutingDecision(
            task_id=task_id,
            selected_model=selected_model,
            selected_tools=selected_tools,
            rationale=rationale,
            alternatives=alternatives,
        )

    def _select_model(
        self, task_type: str, required_capabilities: list[str], risk_level: str,
    ) -> Optional[ModelInfo]:
        """Select the best model for the task."""
        if not self._models:
            return None

        # Score each model
        scored = [
            (m.score_for_task(task_type, required_capabilities, risk_level), m)
            for m in self._models if m.available
        ]

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def _select_tools(self, required_capabilities: list[str]) -> list[ToolInfo]:
        """Select tools that match the required capabilities."""
        selected: list[ToolInfo] = []
        seen_ids: set[str] = set()

        for capability in required_capabilities:
            for tool in self._tools:
                if tool.available and tool.id not in seen_ids and tool.matches_requirement(capability):
                    selected.append(tool)
                    seen_ids.add(tool.id)
                    break

        return selected

    def _generate_rationale(
        self, model: Optional[ModelInfo], tools: list[ToolInfo],
        task_type: str, capabilities: list[str], risk_level: str,
    ) -> str:
        """Generate a human-readable rationale for the routing decision."""
        parts = []

        if model:
            parts.append(f"Selected model '{model.name}' ({model.provider}) for its {model.category.value} capabilities")
        else:
            parts.append("No model available for routing")

        if tools:
            tool_names = [t.name for t in tools]
            parts.append(f"Selected {len(tools)} tool(s): {', '.join(tool_names)}")
        else:
            parts.append("No tools required for this task")

        if risk_level == "high":
            parts.append("Risk level is high, so model selection prioritized quality over speed")

        return ". ".join(parts) + "."

    def _generate_alternatives(
        self, selected_model: Optional[ModelInfo], selected_tools: list[ToolInfo],
        task_type: str, capabilities: list[str],
    ) -> list[dict[str, Any]]:
        """Generate alternative routing options."""
        alternatives = []

        for model in self._models:
            if model.available and model.id != (selected_model.id if selected_model else None):
                alternatives.append({
                    "model_id": model.id,
                    "model_name": model.name,
                    "reason": f"Alternative model with {model.category.value} focus",
                })

        return alternatives

    def get_available_models(self) -> list[dict[str, Any]]:
        """Get all available models."""
        return [
            {
                "id": m.id,
                "name": m.name,
                "provider": m.provider,
                "category": m.category.value,
                "context_window": m.context_window,
                "available": m.available,
                "quality_rating": m.quality_rating,
                "cost_per_1k_tokens": m.cost_per_1k_tokens,
            }
            for m in self._models
        ]

    def get_available_tools(self) -> list[dict[str, Any]]:
        """Get all available tools."""
        return [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "category": t.category.value,
                "permissions": t.permissions,
                "risk_level": t.risk_level,
                "available": t.available,
            }
            for t in self._tools
        ]


_engine: Optional[Router] = None


def get_router() -> Router:
    """Get the shared Router instance."""
    global _engine
    if _engine is None:
        _engine = Router()
    return _engine
