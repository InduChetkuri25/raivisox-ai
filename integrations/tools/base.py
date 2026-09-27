"""Base tool interface for RAIVISOX."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time


class ToolPermission(str, Enum):
    """Tool permission scopes."""

    FILES_READ = "files_read"
    FILES_WRITE = "files_write"
    FILES_DELETE = "files_delete"
    BROWSER_READ = "browser_read"
    BROWSER_INTERACT = "browser_interact"
    TERMINAL_EXECUTE = "terminal_execute"
    EXTERNAL_API = "external_api"
    PUBLISH = "publish"
    PAYMENT = "payment"
    ACCOUNT_CHANGE = "account_change"


class ToolRiskLevel(str, Enum):
    """Tool risk levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ToolResult:
    """Result from tool execution."""

    success: bool
    output: Any = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_time_ms: int = 0


@dataclass
class ToolDefinition:
    """Definition of a tool's interface."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    examples: list[dict[str, Any]] = field(default_factory=list)


class BaseTool(ABC):
    """Abstract base class for tools."""

    # Class-level attributes
    id: str = ""
    name: str = ""
    description: str = ""
    category: str = "general"

    # Instance attributes
    available: bool = True
    disabled_reason: Optional[str] = None

    # Schema
    input_schema: dict[str, Any] = {}
    output_schema: dict[str, Any] = {}
    definition: Optional[ToolDefinition] = None

    # Permissions and risk
    permissions: list[ToolPermission] = []
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW

    def __init__(self):
        """Initialize the tool."""
        if not self.id:
            self.id = self.__class__.__name__.lower().replace("tool", "")
        if not self.definition:
            self.definition = ToolDefinition(
                name=self.name,
                description=self.description,
                input_schema=self.input_schema,
                output_schema=self.output_schema,
            )

    @abstractmethod
    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    async def validate_parameters(self, parameters: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate input parameters."""
        # Default implementation - can be overridden
        return True, None

    def get_definition(self) -> ToolDefinition:
        """Get the tool definition."""
        return self.definition

    def check_permission(self, required_permission: ToolPermission) -> bool:
        """Check if the tool has a specific permission."""
        return required_permission in self.permissions

    def is_available(self) -> bool:
        """Check if the tool is available."""
        return self.available and self.disabled_reason is None

    def disable(self, reason: str):
        """Disable the tool."""
        self.available = False
        self.disabled_reason = reason

    def enable(self):
        """Enable the tool."""
        self.available = True
        self.disabled_reason = None


class ToolRegistry:
    """Registry for managing tools."""

    def __init__(self):
        """Initialize the registry."""
        self._tools: dict[str, BaseTool] = {}
        self._by_permission: dict[ToolPermission, list[str]] = {}
        self._by_category: dict[str, list[str]] = {}

    def register(self, tool: BaseTool):
        """Register a tool."""
        self._tools[tool.id] = tool

        # Index by permissions
        for perm in tool.permissions:
            if perm not in self._by_permission:
                self._by_permission[perm] = []
            self._by_permission[perm].append(tool.id)

        # Index by category
        if tool.category not in self._by_category:
            self._by_category[tool.category] = []
        self._by_category[tool.category].append(tool.id)

    def get(self, tool_id: str) -> Optional[BaseTool]:
        """Get a tool by ID."""
        return self._tools.get(tool_id)

    def get_all(self) -> list[BaseTool]:
        """Get all registered tools."""
        return list(self._tools.values())

    def get_by_permission(self, permission: ToolPermission) -> list[BaseTool]:
        """Get tools with a specific permission."""
        tool_ids = self._by_permission.get(permission, [])
        return [self._tools[tid] for tid in tool_ids if tid in self._tools]

    def get_by_category(self, category: str) -> list[BaseTool]:
        """Get tools in a specific category."""
        tool_ids = self._by_category.get(category, [])
        return [self._tools[tid] for tid in tool_ids if tid in self._tools]

    def remove(self, tool_id: str):
        """Remove a tool from the registry."""
        if tool_id in self._tools:
            tool = self._tools[tool_id]
            # Remove from indexes
            for perm in tool.permissions:
                if perm in self._by_permission:
                    self._by_permission[perm] = [
                        tid for tid in self._by_permission[perm] if tid != tool_id
                    ]
            if tool.category in self._by_category:
                self._by_category[tool.category] = [
                    tid for tid in self._by_category[tool.category] if tid != tool_id
                ]
            del self._tools[tool_id]

    def list_tools(self) -> list[dict[str, Any]]:
        """List all tools with their definitions."""
        return [
            {
                "id": tool.id,
                "name": tool.name,
                "description": tool.description,
                "category": tool.category,
                "permissions": [p.value for p in tool.permissions],
                "risk_level": tool.risk_level.value,
                "available": tool.is_available(),
                "disabled_reason": tool.disabled_reason,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in self._tools.values()
        ]
