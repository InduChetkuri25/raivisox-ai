"""AuditLink model — provenance for an audit row, without touching the row.

The V2 forensic `audit_events` table is left exactly as it was (design §13:
Phase 1 is additive with "no changes to existing tables"). Provenance lives
beside it in this table, one row per attributed audit event, written in the
same transaction as the audit row it describes.

Nullable by design: an audit action can have a user but no task (e.g. an API
admin view), and legacy rows simply have no link row at all. No identity is
ever invented to fill a gap.
"""

from typing import Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLink(Base):
    """Provenance side-table row for one `audit_events` row."""

    __tablename__ = "audit_links"

    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("audit_events.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    # No FK to tasks on purpose: deleting a task must not erase audit history.
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    step_node_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AuditLink(event_id={self.event_id}, task_id={self.task_id})>"
