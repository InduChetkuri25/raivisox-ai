"""TaskEvent model for execution timeline events."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import String, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.task import Task
from app.models.task_step import TaskStep


class EventType(str, Enum):
    """Event type enumeration."""

    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    VERIFICATION = "verification"
    RECOVERY = "recovery"
    ERROR = "error"
    COMPLETED = "completed"
    WARNING = "warning"
    INFO = "info"
    # Real pipeline step transitions (emitted at the actual node state
    # changes inside ParallelExecutor — never fabricated progress).
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_SKIPPED = "step_skipped"
    # Recovery phase markers (the RECOVERY event itself carries the plan).
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    # Artifact Registry: emitted when registering a real artifact row
    # failed. The pipeline outcome is unaffected — honesty over polish.
    ARTIFACT_REGISTRATION_FAILED = "artifact_registration_failed"


class TaskEvent(TimestampMixin, UUIDMixin, Base):
    """TaskEvent model for execution timeline events."""

    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("task_steps.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Which execution run produced this event. Nullable: events emitted
    # before this column existed stay valid and unassociated — no run ids
    # are ever fabricated for them.
    execution_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )

    # Relationships
    task = relationship("Task", back_populates="events")
    step = relationship("TaskStep", back_populates="events")

    def __repr__(self) -> str:
        return f"<TaskEvent(id={self.id}, type={self.event_type}, task_id={self.task_id})>"
