"""Skill model for plugin system."""

from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, JSON, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class Skill(TimestampMixin, UUIDMixin, Base):
    """Skill model for plugin system."""

    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    required_tools: Mapped[str] = mapped_column(JSON, nullable=False, default=[])
    required_models: Mapped[str] = mapped_column(JSON, nullable=False, default=[])
    permissions: Mapped[str] = mapped_column(JSON, nullable=False, default=[])
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default={})
    output_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default={})
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    category: Mapped[str] = mapped_column(String(100), nullable=True)
    skill_metadata: Mapped[dict] = mapped_column("metadata", JSON, nullable=True, default={})

    def __repr__(self) -> str:
        return f"<Skill(id={self.id}, name={self.name}, category={self.category})>"
