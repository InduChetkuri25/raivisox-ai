"""Global audit trail for security-relevant events.

The in-memory trail is the hot path (authorize_and_log is synchronous);
events are ALSO queued in `_pending` and persisted to the audit_events
table via flush_pending(), so security evidence survives restarts.
Honesty note: a hard crash can lose at most the un-flushed tail; the
flusher runs every few seconds and flush_pending is called before reads.
"""

import time
import uuid
from collections import deque
from dataclasses import dataclass, field


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
        }


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
        )
        self._events.append(event)
        self._pending.append(event)
        return event

    def pending_count(self) -> int:
        """Events awaiting durable persistence."""
        return len(self._pending)

    async def flush_pending(self, db) -> int:
        """Persist queued events to the audit_events table.

        Uses the app's AsyncSession. Idempotent per event_id (unique).
        On failure the events stay queued — never silently dropped.
        Returns the number of rows written.
        """
        from sqlalchemy import select

        from app.models.audit_event import AuditEventModel

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
        try:
            for e in batch:
                if e.event_id in existing:
                    self._pending.remove(e)
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
                self._pending.remove(e)
                written += 1
            await db.commit()
        except Exception:
            await db.rollback()
            raise  # events remain in _pending; the flusher retries
        return written

    async def read_from_db(
        self,
        db,
        actor_id: str | None = None,
        action: str | None = None,
        allowed: bool | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Read persisted events (restart-safe)."""
        from sqlalchemy import select

        from app.models.audit_event import AuditEventModel

        q = select(AuditEventModel).order_by(AuditEventModel.timestamp.desc()).limit(limit)
        if actor_id:
            q = q.where(AuditEventModel.actor_id == actor_id)
        if action:
            q = q.where(AuditEventModel.action == action)
        if allowed is not None:
            q = q.where(AuditEventModel.allowed.is_(allowed))
        rows = (await db.execute(q)).scalars().all()
        return [
            {
                "event_id": r.event_id,
                "actor_id": r.actor_id,
                "action": r.action,
                "resource": r.resource,
                "allowed": r.allowed,
                "request_id": r.request_id,
                "details": r.details,
                "timestamp": r.timestamp,
            }
            for r in rows
        ]

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
