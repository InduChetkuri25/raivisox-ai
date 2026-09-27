"""TaskStep model for individual steps within tasks."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import String, Text, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.task import Task


class StepStatus(str, Enum):
    """Step status enumeration."""

    PENDING = "pending"
    CREATED = "created"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    PREPARING = "preparing"
    WAITING_FOR_USER = "waiting_for_user"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStep(TimestampMixin, UUIDMixin, Base):
    """TaskStep model for individual steps within tasks."""

    __tablename__ = "task_steps"

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    status: Mapped[str] = mapped_column(String(50), default=StepStatus.PENDING.value, nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dependencies: Mapped[str] = mapped_column(String(500), nullable=True, default="")
    model_used: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tools_used: Mapped[str] = mapped_column(String(500), nullable=True, default="")
    timeout: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    task = relationship("Task", back_populates="steps")
    events = relationship("TaskEvent", back_populates="step", cascade="all, delete-orphan")
    verifications = relationship("VerificationResult", back_populates="step", cascade="all, delete-orphan")
    recovery_attempts = relationship("RecoveryAttempt", back_populates="step", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<TaskStep(id={self.id}, title={self.title}, status={self.status})>"
