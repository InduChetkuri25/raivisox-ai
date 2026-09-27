"""RecoveryAttempt model for failure recovery tracking."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import String, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.task import Task
from app.models.task_step import TaskStep


class FailureCategory(str, Enum):
    """Failure category enumeration."""

    MODEL_FAILURE = "model_failure"
    TOOL_FAILURE = "tool_failure"
    NETWORK_FAILURE = "network_failure"
    TIMEOUT = "timeout"
    UI_CHANGE = "ui_change"
    VALIDATION_FAILURE = "validation_failure"
    PERMISSION_FAILURE = "permission_failure"
    EXTERNAL_SERVICE_FAILURE = "external_service_failure"
    AMBIGUOUS_INSTRUCTION = "ambiguous_instruction"
    UNKNOWN = "unknown"


class RecoveryAttempt(TimestampMixin, UUIDMixin, Base):
    """RecoveryAttempt model for failure recovery tracking."""

    __tablename__ = "recovery_attempts"

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
    failure_category: Mapped[str] = mapped_column(String(100), nullable=False)
    failure_description: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recovery_strategy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alternatives_considered: Mapped[str] = mapped_column(JSON, nullable=True, default=[])
    success: Mapped[bool] = mapped_column(nullable=False, default=False)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    task = relationship("Task", back_populates="recovery_attempts")
    step = relationship("TaskStep", back_populates="recovery_attempts")

    def __repr__(self) -> str:
        return f"<RecoveryAttempt(id={self.id}, category={self.failure_category}, success={self.success})>"
