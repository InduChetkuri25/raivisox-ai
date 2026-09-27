"""ExecutionRun model — first-class record of one task execution.

Replaces timestamp-inferred run boundaries with a durable row:

- Exactly one row per execute invocation (sync or async mode), created at
  pipeline start.
- Terminal state (`finished_at`, `status`, `outcome`) is written exactly
  once, atomically, at the same commit that moves the task to its final
  status (or, for crash paths, at the orphan-sweep / _drive_pipeline
  failure handler).
- `task_artifacts.run_id` and (new) `task_events.execution_run_id`
  reference this id, making artifact history and event timelines
  authoritatively run-scoped.
- Legacy rows (artifacts/events written before this table existed) keep
  their old hex run_ids; they are represented as legacy/unscoped and no
  synthetic run rows are ever fabricated for them.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class ExecutionRunStatus(str):
    """Status values for an ExecutionRun lifecycle."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPLETED_UNVERIFIED = "completed_unverified"
    PARTIALLY_COMPLETED = "partially_completed"
    CANCELLED = "cancelled"


class ExecutionRun(TimestampMixin, UUIDMixin, Base):
    """One durable execution of a task."""

    __tablename__ = "execution_runs"

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="running")
    outcome: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)

    __table_args__ = (
        Index("ix_execution_runs_task_started", "task_id", "started_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ExecutionRun(id={self.id}, task_id={self.task_id}, "
            f"status={self.status}, outcome={self.outcome})>"
        )
