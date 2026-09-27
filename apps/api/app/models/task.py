"""Task model for execution units."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import ForeignKey, String, Text, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.project import Project
from app.models.user import User


class TaskOutcome(str, Enum):
    """Outcome classification — carries verification truth.

    Distinct from status: a task can be COMPLETED (execution finished)
    while its outcome is COMPLETED_UNVERIFIED (the Definition-of-Done
    stage could not derive any verifiable criterion, so success was
    NOT independently confirmed). The pipeline is only allowed to
    report VERIFIED when required DoD checks actually passed.
    """

    VERIFIED = "verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """Task status enumeration."""

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


class ExecutionMode(str, Enum):
    """Execution mode enumeration."""

    FAST = "fast"
    BALANCED = "balanced"
    QUALITY = "quality"
    ECONOMY = "economy"


class RiskLevel(str, Enum):
    """Risk level enumeration."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Task(TimestampMixin, UUIDMixin, Base):
    """Task model for execution units."""

    __tablename__ = "tasks"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    goal: Mapped[str] = mapped_column(Text, nullable=True, default="")
    status: Mapped[str] = mapped_column(String(50), default=TaskStatus.CREATED.value, nullable=False)
    mode: Mapped[str] = mapped_column(String(50), default=ExecutionMode.BALANCED.value, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), default=RiskLevel.LOW.value, nullable=False)
    definition_of_done: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Outcome classification (Honest-Outcome Guarantees): the
    # verification truth behind the status. Set by the execute pipeline.
    outcome: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    error: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # Relationships
    owner = relationship("User", back_populates="tasks")
    project = relationship("Project", back_populates="tasks")
    steps = relationship("TaskStep", back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.order")
    events = relationship("TaskEvent", back_populates="task", cascade="all, delete-orphan")
    verifications = relationship("VerificationResult", back_populates="task", cascade="all, delete-orphan")
    recovery_attempts = relationship("RecoveryAttempt", back_populates="task", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Task(id={self.id}, title={self.title}, status={self.status})>"
