"""ToolUsage model for tool execution tracking."""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class ToolUsage(TimestampMixin, UUIDMixin, Base):
    """ToolUsage model for tool execution tracking."""

    __tablename__ = "tool_usage"

    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    step_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    tool_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<ToolUsage(id={self.id}, tool={self.tool_name}, duration={self.duration_ms}ms)>"
