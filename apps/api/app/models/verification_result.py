"""VerificationResult model for outcome validation."""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import String, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.task import Task
from app.models.task_step import TaskStep


class VerificationResult(TimestampMixin, UUIDMixin, Base):
    """VerificationResult model for outcome validation."""

    __tablename__ = "verification_results"

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
    verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.0)
    evidence: Mapped[str] = mapped_column(JSON, nullable=False, default=[])
    failed_checks: Mapped[str] = mapped_column(JSON, nullable=False, default=[])
    verification_type: Mapped[str] = mapped_column(String(100), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    task = relationship("Task", back_populates="verifications")
    step = relationship("TaskStep", back_populates="verifications")

    def __repr__(self) -> str:
        return f"<VerificationResult(id={self.id}, verified={self.verified}, confidence={self.confidence})>"
