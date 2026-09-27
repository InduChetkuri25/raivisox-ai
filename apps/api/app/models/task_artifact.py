"""TaskArtifact model — first-class registry of files the pipeline wrote.

Replaces derivation from step results as the authoritative artifact source
(step results remain a fallback/backfill source, never the authority).

Honesty contract:
- A row exists ONLY after the file was actually written to disk, resolved
  and verified inside the task workspace, hashed, and sized.
- `is_current` marks the row whose recorded bytes match the file at
  `storage_path` as far as the registry knows; when a later write lands on
  the same path, older rows keep their history with `superseded_at` set —
  history is never silently overwritten or deleted.
- `storage_path` is internal (never serialized through the API).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class TaskArtifact(TimestampMixin, UUIDMixin, Base):
    """One real file a pipeline run produced for a task."""

    __tablename__ = "task_artifacts"

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # No FK: step rows are recreated per run and artifact history should
    # survive them. Plain references, like TaskStep.parent_id.
    step_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    node_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    step_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Which execute invocation produced this row (uuid per run; "backfill"
    # for rows derived from pre-registry step results).
    run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # Display name (basename); never contains server directory components.
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    # INTERNAL: absolute path used only by the server to read bytes.
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_task_artifacts_task_current", "task_id", "is_current"),
        Index("ix_task_artifacts_task_path", "task_id", "storage_path"),
    )
