"""Memory model for user-controlled memory."""

from datetime import datetime
from enum import Enum

from sqlalchemy import String, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.user import User


class MemoryType(str, Enum):
    """Memory type enumeration."""

    USER_PREFERENCE = "user_preference"
    PROJECT_MEMORY = "project_memory"
    TASK_HISTORY = "task_history"
    WORKFLOW_MEMORY = "workflow_memory"
    EXECUTION_METRIC = "execution_metric"
    CUSTOM = "custom"


class Memory(TimestampMixin, UUIDMixin, Base):
    """Memory model for user-controlled memory."""

    __tablename__ = "memory"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)

    # Relationships
    owner = relationship("User", back_populates="memories")

    __table_args__ = (
        # Unique constraint on user_id + type + key
    )

    def __repr__(self) -> str:
        return f"<Memory(id={self.id}, type={self.type}, key={self.key})>"
