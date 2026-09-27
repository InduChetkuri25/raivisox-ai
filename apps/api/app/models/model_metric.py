"""ModelMetric model for AI model performance tracking."""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Float, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class ModelMetric(TimestampMixin, UUIDMixin, Base):
    """ModelMetric model for AI model performance tracking."""

    __tablename__ = "model_metrics"

    provider: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    task_type: Mapped[str] = mapped_column(String(100), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=True, default=0.0)
    tokens_input: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(nullable=False, default=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    def __repr__(self) -> str:
        return f"<ModelMetric(provider={self.provider}, model={self.model_name}, latency={self.latency_ms}ms)>"
