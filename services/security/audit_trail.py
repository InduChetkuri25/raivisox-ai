"""Global audit trail for security-relevant events.

The in-memory trail is the hot path (authorize_and_log is synchronous);
events are ALSO queued in `_pending` and persisted to the audit_events
table via flush_pending(), so security evidence survives restarts.
Honesty note: a hard crash can lose at most the un-flushed tail; the
flusher runs every few seconds and flush_pending is called before reads.

V3 Phase 1 adds **provenance linkage** without touching the forensic table:
when an event carries `user_id`/`task_id`/`run_id`/`step_node_id`/`action_id`,
flush_pending writes an `audit_links` row in the SAME transaction as the
audit row. Legacy rows (and rows with no task subject) simply have no link
row; nothing is backfilled and no identity is invented.
"""

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AuditEvent:
    """A single audit event."""

    event_id: str
    actor_id: str
    action: str
    resource: str
    allowed: bool
    request_id: str = ""
    details: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    # V3 Phase 1 provenance (None = unknown / legacy; never fabricated).
    user_id: Optional[str] = None
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    step_node_id: Optional[str] = None
    action_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "actor_id": self.actor_id,
            "action": self.action,
            "resource": self.resource,
            "allowed": self.allowed,
            "request_id": self.request_id,
            "details": self.details,
            "timestamp": self.timestamp,
            "user_id": self.user_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "step_node_id": self.step_node_id,
            "action_id": self.action_id,
        }

    def has_linkage(self) -> bool:
        return any(
            (
                self.user_id,
                self.task_id,
                self.run_id,
                self.step_node_id,
                self.action_id,
            )
        )


class AuditTrail:
    """Global audit trail — singleton collecting all security events."""

    _instance: "AuditTrail | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._events: list[AuditEvent] = []
            cls._instance._pending: deque[AuditEvent] = deque()
        return cls._instance

    @classmethod
    def instance(cls) -> "AuditTrail":
        """Return the shared AuditTrail instance."""
        return cls()

    def log(
        self,
        actor_id: str,
        action: str,
        resource: str,
        allowed: bool,
        request_id: str = "",
        details: dict | None = None,
        timestamp: float | None = None,
        *,
        user_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        step_node_id: str | None = None,
        action_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            actor_id=actor_id,
            action=action,
            resource=resource,
            allowed=allowed,
            request_id=request_id,
            details=details or {},
            timestamp=timestamp or time.time(),
            user_id=user_id or None,
            task_id=task_id or None,
            run_id=run_id or None,
            step_node_id=step_node_id or None,
            action_id=action_id or None,
        )
        self._events.append(event)
        self._pending.append(event)
        return event

    def pending_count(self) -> int:
        """Events awaiting durable persistence."""
        return len(self._pending)

    async def flush_pending(self, db) -> int:
        """Persist queued events to the audit_events table (+ their links).

        Uses the app's AsyncSession. Idempotent per event_id (unique).
        On failure the events stay queued — never silently dropped.

        The audit row and its `audit_links` row are committed together: a
        failure rolls back both, so an audit row can never be persisted
        without the link that was meant to accompany it, and a link can never
        point at a missing audit row.
        Returns the number of rows written.
        """
        from sqlalchemy import select

        from app.models.audit_event import AuditEventModel
        from app.models.audit_link import AuditLink

        if not self._pending:
            return 0
        batch = list(self._pending)
        ids = [e.event_id for e in batch]
        existing = {
            row[0]
            for row in (
                await db.execute(
                    select(AuditEventModel.event_id).where(
                        AuditEventModel.event_id.in_(ids)
                    )
                )
            ).all()
        }
        written = 0
        # Dequeue ONLY after the commit succeeds. Removing inside the loop
        # would drop earlier events from the queue when a later insert (e.g.
        # a link row) raises and the transaction rolls back — the event would
        # then exist in neither the table nor the queue.
        committed: list[AuditEvent] = []
        try:
            for e in batch:
                if e.event_id in existing:
                    committed.append(e)
                    continue
                db.add(
                    AuditEventModel(
                        event_id=e.event_id,
                        actor_id=e.actor_id,
                        action=e.action,
                        resource=e.resource[:500],
                        allowed=e.allowed,
                        request_id=e.request_id,
                        details=e.details,
                        timestamp=e.timestamp,
                    )
                )
                if e.has_linkage():
                    db.add(
                        AuditLink(
                            event_id=e.event_id,
                            user_id=e.user_id,
                            task_id=e.task_id,
                            run_id=e.run_id,
                            step_node_id=e.step_node_id,
                            action_id=e.action_id,
                        )
                    )
                committed.append(e)
                written += 1
            await db.commit()
        except Exception:
            await db.rollback()
            raise  # events remain in _pending; the flusher retries
        else:
            for e in committed:
                try:
                    self._pending.remove(e)
                except ValueError:  # pragma: no cover - concurrent flusher
                    pass
        return written

    async def read_from_db(
        self,
        db,
        actor_id: str | None = None,
        action: str | None = None,
        allowed: bool | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Read persisted events (restart-safe), including provenance linkage.

        Linkage comes from a LEFT JOIN on `audit_links`, so legacy rows and
        rows with no task subject read as `None` instead of being guessed.
        """
        from sqlalchemy import select

        from app.models.audit_event import AuditEventModel
        from app.models.audit_link import AuditLink

        q = (
            select(AuditEventModel, AuditLink)
            .outerjoin(AuditLink, AuditLink.event_id == AuditEventModel.event_id)
            .order_by(AuditEventModel.timestamp.desc())
            .limit(limit)
        )
        if actor_id:
            q = q.where(AuditEventModel.actor_id == actor_id)
        if action:
            q = q.where(AuditEventModel.action == action)
        if allowed is not None:
            q = q.where(AuditEventModel.allowed.is_(allowed))
        rows = (await db.execute(q)).all()
        out: list[dict] = []
        for row in rows:
            r = row[0]
            link = row[1]
            out.append(
                {
                    "event_id": r.event_id,
                    "actor_id": r.actor_id,
                    "action": r.action,
                    "resource": r.resource,
                    "allowed": r.allowed,
                    "request_id": r.request_id,
                    "details": r.details,
                    "timestamp": r.timestamp,
                    "user_id": getattr(link, "user_id", None),
                    "task_id": getattr(link, "task_id", None),
                    "run_id": getattr(link, "run_id", None),
                    "step_node_id": getattr(link, "step_node_id", None),
                    "action_id": getattr(link, "action_id", None),
                }
            )
        return out

    def get_events(
        self,
        actor_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        events = self._events
        if actor_id:
            events = [e for e in events if e.actor_id == actor_id]
        if action:
            events = [e for e in events if e.action == action]
        return events[-limit:]

    def recent(self, count: int = 50) -> list[AuditEvent]:
        return self._events[-count:]

    def clear(self) -> None:
        self._events.clear()
        self._pending.clear()

    def total_events(self) -> int:
        return len(self._events)
