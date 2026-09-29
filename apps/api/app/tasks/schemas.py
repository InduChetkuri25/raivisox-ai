"""Task and project schemas."""

import os
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _relativize_workspace_paths(value: Any) -> Any:
    """Recursively rewrite absolute paths INSIDE the task workspace to
    workspace-relative strings for outbound responses.

    Persisted step results legitimately record absolute host paths (the
    executor needs them), but the server's filesystem layout must never
    reach a client. Doing this at the response boundary is what makes the
    guarantee independent of the serializer: JSON escaping hides backslash
    paths, so a naive string check silently misses Windows leaks.
    """
    if isinstance(value, str):
        if not os.path.isabs(value):
            return value
        try:
            # Lazy import: app.main builds the workspace root and imports
            # this module, so a module-level import would be circular.
            from app.main import TASK_WORKSPACE

            root = os.path.realpath(TASK_WORKSPACE)
            resolved = os.path.realpath(value)
            if resolved.startswith(root + os.sep):
                return os.path.relpath(resolved, root)
        except Exception:  # noqa: BLE001 — display-only transformation
            return value
        return value
    if isinstance(value, dict):
        return {k: _relativize_workspace_paths(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_relativize_workspace_paths(v) for v in value]
    return value

# Import enums from models to avoid duplication
from app.models.task import TaskStatus, ExecutionMode, RiskLevel
from app.models.task_step import StepStatus
from app.models.task_event import EventType


# Task Schemas

class TaskCreate(BaseModel):
    """Schema for creating a task."""

    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    goal: Optional[str] = Field(None, max_length=10000)
    project_id: Optional[str] = None
    mode: str = ExecutionMode.BALANCED
    definition_of_done: Optional[dict[str, Any]] = None


class TaskUpdate(BaseModel):
    """Schema for updating a task."""

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    goal: Optional[str] = Field(None, max_length=10000)
    mode: Optional[str] = None
    definition_of_done: Optional[dict[str, Any]] = None
    status: Optional[str] = None


class TaskStepCreate(BaseModel):
    """Schema for creating a task step."""

    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    order: int = 0
    dependencies: Optional[str] = None
    timeout: Optional[int] = None


class TaskStepUpdate(BaseModel):
    """Schema for updating a task step."""

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None


class TaskStepResponse(BaseModel):
    """Schema for task step response."""

    id: str
    task_id: str
    parent_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    status: str
    order: int
    dependencies: Optional[str] = None
    model_used: Optional[str] = None
    tools_used: Optional[str] = None
    timeout: Optional[int] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @field_validator("result", "error", mode="before")
    @classmethod
    def _scrub_paths(cls, value: Any) -> Any:
        return _relativize_workspace_paths(value)


class TaskEventResponse(BaseModel):
    """Schema for task event response."""

    id: str
    task_id: str
    step_id: Optional[str] = None
    event_type: str
    payload: Optional[dict[str, Any]] = None
    execution_run_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

    @field_validator("payload", mode="before")
    @classmethod
    def _scrub_paths(cls, value: Any) -> Any:
        return _relativize_workspace_paths(value)


class TaskResponse(BaseModel):
    """Schema for task response."""

    id: str
    user_id: str
    project_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    goal: Optional[str] = None
    status: str
    mode: str
    risk_level: str
    outcome: Optional[str] = None
    definition_of_done: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    steps: list[TaskStepResponse] = []
    events: list[TaskEventResponse] = []

    class Config:
        from_attributes = True

    @field_validator("definition_of_done", "result", "error", mode="before")
    @classmethod
    def _scrub_paths(cls, value: Any) -> Any:
        return _relativize_workspace_paths(value)


class TaskListResponse(BaseModel):
    """Schema for task list response."""

    tasks: list[TaskResponse]
    total: int
    page: int
    page_size: int


# Project Schemas

class ProjectCreate(BaseModel):
    """Schema for creating a project."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = None


class ProjectResponse(BaseModel):
    """Schema for project response."""

    id: str
    user_id: str
    name: str
    description: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectListResponse(BaseModel):
    """Schema for project list response."""

    projects: list[ProjectResponse]
    total: int
    page: int
    page_size: int


# Verification Schemas

class VerificationRequest(BaseModel):
    """Schema for verification request."""

    task_id: str
    step_id: Optional[str] = None
    verification_type: Optional[str] = None
    criteria: Optional[dict[str, Any]] = None


class VerificationResultResponse(BaseModel):
    """Schema for verification result response."""

    id: str
    task_id: str
    step_id: Optional[str] = None
    verified: bool
    confidence: float
    evidence: list[dict[str, Any]]
    failed_checks: list[dict[str, Any]]
    verification_type: Optional[str] = None
    details: Optional[dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True
