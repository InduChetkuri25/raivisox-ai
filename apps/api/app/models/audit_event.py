"""AuditEvent model — durable, DB-backed security audit trail.

Persists the in-memory AuditTrail (services/security/audit_trail.py) so
security evidence survives process restarts. The in-memory trail remains
the hot path (authorize_and_log is synchronous); this table is filled by
the background flusher / explicit flush_pending calls.
"""

from typing import Optional

from sqlalchemy import JSON, Boolean, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class AuditEventModel(UUIDMixin, TimestampMixin, Base):
    """One persisted audit event, mirroring AuditTrail.AuditEvent."""

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    actor_id: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource: Mapped[str] = mapped_column(String(500), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    request_id: Mapped[str] = mapped_column(String(100), default="")
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Epoch seconds, mirroring the in-memory event for lossless round-trip.
    timestamp: Mapped[float] = mapped_column(Float, default=0.0)

    def __repr__(self) -> str:
        return f"<AuditEventModel(event_id={self.event_id}, action={self.action}, allowed={self.allowed})>"
