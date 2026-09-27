"""Pydantic schemas for the Intent Engine."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, ConfigDict

from services.intent.engine import (
    AmbiguityLevel,
    DefinitionOfDone,
    Intent,
    Requirement,
    RiskLevel,
    TaskType,
)


# Request/Response schemas

class IntentRequest(BaseModel):
    """Request to parse a user's intent."""

    user_input: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="The natural language request from the user",
    )
    context: Optional[dict[str, Any]] = Field(
        None,
        description="Optional additional context (project, preferences, etc.)",
    )


class RequirementSchema(BaseModel):
    """A single requirement."""

    model_config = ConfigDict(from_attributes=True)

    text: str
    category: str
    priority: str = "required"


class DefinitionOfDoneSchema(BaseModel):
    """Definition of done for a task."""

    model_config = ConfigDict(from_attributes=True)

    criteria: list[str] = []
    verification_methods: list[str] = []
    acceptance_threshold: str = "All required criteria met"


class IntentResponse(BaseModel):
    """Structured intent produced by the Intent Engine."""

    model_config = ConfigDict(from_attributes=True)

    goal: str
    task_type: str
    requirements: list[RequirementSchema]
    constraints: list[str]
    required_capabilities: list[str]
    risk_level: str
    ambiguity: str
    clarification_needed: list[str]
    definition_of_done: DefinitionOfDoneSchema
    analyzed_at: str


class ClarifyRequest(BaseModel):
    """Request for clarification on an ambiguous intent."""

    intent_id: str
    ambiguities: list[str]


class ClarifyResponse(BaseModel):
    """Response with clarification questions."""

    questions: list[str]
    urgency: str = "normal"


# Task Graph schemas

class TaskNode(BaseModel):
    """A single node in the task graph."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str = ""
    status: str = "pending"
    dependencies: list[str] = []
    can_start_parallel: bool = False
    estimated_effort: str = "unknown"  # trivial, small, medium, large


class TaskGraph(BaseModel):
    """A directed acyclic graph of task nodes."""

    model_config = ConfigDict(from_attributes=True)

    root_task_id: str
    nodes: list[TaskNode]
    parallel_groups: list[list[str]] = []  # groups of task IDs that can run in parallel


class TaskGraphResponse(BaseModel):
    """Response containing a task graph and metadata."""

    task_id: str
    intent_goal: str
    graph: TaskGraph
    created_at: str


# Strategy schemas

class StrategyOption(BaseModel):
    """A possible execution strategy for a task."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    approach: str  # sequential, parallel, batched, prepared, etc.
    estimated_speed: str  # fast, medium, slow
    estimated_reliability: str  # high, medium, low
    estimated_cost: str  # low, medium, high
    required_capabilities: list[str] = []
    confidence: float = 0.5


class StrategyDecision(BaseModel):
    """The chosen strategy and its rationale."""

    model_config = ConfigDict(from_attributes=True)

    selected_strategy_id: str
    selected_strategy: StrategyOption
    alternatives_considered: list[StrategyOption] = []
    rationale: str
    decision_factors: list[str] = []


# Planning schemas

class PlanStep(BaseModel):
    """A single step in the execution plan."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    order: int
    title: str
    description: str
    expected_output: str = ""
    capability_required: str = ""
    can_run_parallel_with: list[str] = []
    status: str = "pending"


class ExecutionPlan(BaseModel):
    """Full execution plan for a task."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    strategy: StrategyDecision
    steps: list[PlanStep]
    total_estimated_steps: int
    parallelizable_steps: int = 0
