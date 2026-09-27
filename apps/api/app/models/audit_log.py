"""AuditLog model for security audit trail."""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import String, Text, JSON, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class AuditLog(TimestampMixin, UUIDMixin, Base):
    """AuditLog model for security audit trail."""

    __tablename__ = "audit_logs"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    permissions_required: Mapped[str] = mapped_column(JSON, nullable=True, default=[])
    risk_level: Mapped[str] = mapped_column(String(20), nullable=True, default="low")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, action={self.action}, resource={self.resource_type}:{self.resource_id})>"
