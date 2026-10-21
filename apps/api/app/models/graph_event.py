"""GraphEvent model — the V3 Phase 1 append-only provenance record.

One row per observed fact, written only by ``services.graph.events.emit``.
There is no update path: the table is the evidence, and the Phase 2 projection
is a rebuildable view over it (design §4.4/§5.5).

``seq`` is the only total order readers may use. SQLite's ``AUTOINCREMENT``
guarantee (and PostgreSQL's sequence) means a ``seq`` is never reused after a
delete, so a Phase 2 cursor can never skip or replay evidence.
"""

from typing import Any, Optional

from sqlalchemy import Float, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GraphEvent(Base):
    """One provenance event. Append-only by construction."""

    __tablename__ = "graph_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    occurred_at: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False)

    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="service")

    # Isolation key: denormalized from tasks.user_id, never from a request.
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=False)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    step_node_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tool_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    target_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_class: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_graph_events_user_seq", "user_id", "seq"),
        Index("ix_graph_events_task_seq", "task_id", "seq"),
        Index("ix_graph_events_run_seq", "run_id", "seq"),
        Index("ix_graph_events_action", "action_id"),
        Index("ix_graph_events_type", "event_type"),
        {"sqlite_autoincrement": True},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<GraphEvent(seq={self.seq}, type={self.event_type}, "
            f"task_id={self.task_id}, outcome={self.outcome})>"
        )
