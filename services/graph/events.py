"""V3 Phase 1 event writer (spec §3–§8).

Properties this module guarantees:

- **Observation never changes execution.** Every failure here is counted and
  logged; nothing is raised into a pipeline except ``ProvenanceError``, which
  means the subject itself is unprovable (an integrity fault, never a runtime
  condition).
- **No invented identity.** Missing provenance skips the event and increments a
  counter; a mismatched user/task/run raises instead of writing a wrong row.
- **Idempotent.** ``idempotency_key`` is deterministic; a retried write is a
  no-op, a second execution is a distinct action.
- **Append-only.** No update/delete helper exists.
- **Default off.** ``V3_EVENTS_ENABLED`` gates everything (design §13).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from services.graph import actions, schema
from services.graph.provenance import ProvenanceError, verify_subject

logger = logging.getLogger("raivisox.graph.events")

TRUTHY = frozenset({"1", "true", "yes", "on"})

_COUNTER_KEYS = (
    "written",
    "duplicates",
    "failures",
    "invalid",
    "invalid_target",
    "skipped_no_provenance",
    "retries",
)

_counters: dict[str, int] = {key: 0 for key in _COUNTER_KEYS}
_disabled = False
_session_factory: Optional[Callable[[], Any]] = None


def reset_state() -> None:
    """Reset counters, the disabled flag and any injected session factory (tests)."""
    global _disabled, _session_factory
    for key in _COUNTER_KEYS:
        _counters[key] = 0
    _disabled = False
    _session_factory = None


def events_enabled() -> bool:
    return os.environ.get("V3_EVENTS_ENABLED", "").strip().lower() in TRUTHY


def set_session_factory(factory: Optional[Callable[[], Any]]) -> None:
    """Inject a session factory (tests own the database they assert on)."""
    global _session_factory
    _session_factory = factory


def _factory() -> Callable[[], Any]:
    if _session_factory is not None:
        return _session_factory
    from app.database import async_session_factory  # local import: app layer

    return async_session_factory


def counters() -> dict[str, int]:
    return dict(_counters)


def _inc(name: str) -> None:
    _counters[name] = _counters.get(name, 0) + 1


async def emit(
    *,
    event_type: str,
    outcome: str,
    actor_id: str,
    user_id: str = "",
    task_id: str = "",
    actor_type: str = "service",
    run_id: Optional[str] = None,
    step_node_id: Optional[str] = None,
    tool_id: Optional[str] = None,
    action_id: Optional[str] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    evidence_class: str = schema.OBSERVED,
    source: Optional[str] = None,
    correlation_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    occurred_at: Optional[float] = None,
    session_factory: Optional[Callable[[], Any]] = None,
) -> bool:
    """Append one event. Returns True when the row exists (written or duplicate).

    Keyword-only and explicit: there is no ``**kwargs`` path through which an
    untrusted caller could smuggle an identity field.
    """
    if not events_enabled() or _disabled:
        return False

    if event_type not in schema.EVENT_TYPES:
        _inc("invalid")
        return False
    if not schema.valid_outcome(event_type, outcome):
        _inc("invalid")
        return False
    if evidence_class not in schema.EVIDENCE_CLASSES:
        _inc("invalid")
        return False
    if event_type in schema.VERIFIED_ONLY_TYPES and evidence_class != schema.VERIFIED:
        _inc("invalid")
        return False
    if actor_type not in schema.ACTOR_TYPES:
        _inc("invalid")
        return False
    if not isinstance(actor_id, str) or not schema.is_action_id(actor_id):
        _inc("invalid")
        return False
    # No identity ⇒ no event. An unattributable action is not assigned to
    # anyone (design §4.4: user_id NOT NULL; §11.1: missing ⇒ unknown). This
    # is checked BEFORE format validation so a provenance gap is counted as a
    # gap, not mislabelled as a malformed identifier.
    if not user_id or not task_id:
        _inc("skipped_no_provenance")
        return False
    if not schema.is_uuid(task_id) or not schema.is_uuid(user_id):
        _inc("invalid")
        return False
    if run_id is not None and not schema.is_uuid(run_id):
        _inc("invalid")
        return False
    if step_node_id is not None and not schema.is_step_node_id(step_node_id):
        _inc("invalid")
        return False
    if tool_id is not None and not schema.is_tool_id(tool_id):
        _inc("invalid")
        return False

    artifact_id = None
    if event_type == schema.ARTIFACT_REGISTERED:
        artifact_id = (payload or {}).get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            _inc("invalid")
            return False
    else:
        if not action_id or not schema.is_uuid(action_id):
            _inc("invalid")
            return False
        # An adapter may only speak about an invocation the registry issued.
        if not actions.is_live(action_id):
            _inc("invalid")
            return False

    if source is None:
        source = schema.SOURCE_BY_EVENT[event_type]
    elif source != schema.SOURCE_BY_EVENT[event_type]:
        _inc("invalid")
        return False

    try:
        clean_payload = schema.filter_payload(event_type, payload)
        clean_payload, digest = schema.canonical_payload(clean_payload)
    except schema.SchemaError:
        _inc("invalid")
        return False

    clean_target = schema.validate_target(target_id)
    if target_id is not None and clean_target is None:
        _inc("invalid_target")

    try:
        key = schema.idempotency_key(
            event_type, action_id=action_id, artifact_id=artifact_id
        )
    except schema.SchemaError:
        _inc("invalid")
        return False

    now = time.time()
    occurred = float(occurred_at) if occurred_at is not None else now
    if occurred <= 0:
        occurred = now
    recorded = max(occurred, now)

    factory = session_factory or _factory()

    # --- ownership verification (one query, authoritative rows) -----------
    try:
        async with factory() as session:
            owned = await verify_subject(
                session, task_id=task_id, user_id=user_id, run_id=run_id
            )
    except OperationalError as exc:
        _inc("failures")
        _maybe_disable(exc)
        _safe_warn("ownership check failed", event_type, exc)
        return False
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        _inc("failures")
        _safe_warn("ownership check failed", event_type, exc)
        return False
    if not owned:
        raise ProvenanceError(
            "user/task/run do not match authoritative records; refusing to emit"
        )

    from app.models.graph_event import GraphEvent  # local import: app layer

    def _row() -> "GraphEvent":
        # Built per attempt: a retry starts a fresh session, so it must not
        # reuse an instance that a failed transaction may have touched.
        return GraphEvent(
            event_id=str(uuid.uuid4()),
            schema_version=schema.SCHEMA_VERSION,
            event_type=event_type,
            occurred_at=occurred,
            recorded_at=recorded,
            actor_id=actor_id,
            actor_type=actor_type,
            user_id=user_id,
            task_id=task_id,
            run_id=run_id,
            step_node_id=step_node_id,
            tool_id=tool_id,
            action_id=action_id,
            target_kind=target_kind,
            target_id=clean_target,
            outcome=outcome,
            evidence_class=evidence_class,
            source=source,
            correlation_id=correlation_id,
            idempotency_key=key,
            payload=clean_payload,
            payload_hash=digest,
        )

    for attempt in range(3):
        try:
            async with factory() as session:
                session.add(_row())
                await session.commit()
            _inc("written")
            return True
        except IntegrityError:
            # Deterministic key already stored: the fact is recorded once.
            _inc("duplicates")
            return True
        except OperationalError as exc:
            if _is_missing_table(exc):
                _inc("failures")
                _maybe_disable(exc)
                _safe_warn("event table missing", event_type, exc)
                return False
            if _is_transient_lock(exc) and attempt < 2:
                _inc("retries")
                await asyncio.sleep(0.05 if attempt == 0 else 0.2)
                continue
            _inc("failures")
            _safe_warn("event write failed", event_type, exc)
            return False
        except SQLAlchemyError as exc:  # pragma: no cover - defensive
            _inc("failures")
            _safe_warn("event write failed", event_type, exc)
            return False
    _inc("failures")
    return False


async def read_events(
    session,
    *,
    after_seq: int = 0,
    task_id: Optional[str] = None,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    """Read events in ``seq`` order, refusing schema versions we do not know.

    Ordering is ``seq`` only — never a timestamp. Returns
    ``(rows, refused_unknown_version_count)``.
    """
    from sqlalchemy import select

    from app.models.graph_event import GraphEvent

    query = (
        select(GraphEvent)
        .where(GraphEvent.seq > int(after_seq))
        .order_by(GraphEvent.seq.asc())
        .limit(max(1, min(int(limit), 1000)))
    )
    if task_id:
        query = query.where(GraphEvent.task_id == task_id)
    rows = (await session.execute(query)).scalars().all()
    out: list[dict[str, Any]] = []
    refused = 0
    for row in rows:
        if row.schema_version != schema.SCHEMA_VERSION:
            refused += 1
            continue
        out.append(
            {
                "seq": row.seq,
                "event_id": row.event_id,
                "schema_version": row.schema_version,
                "event_type": row.event_type,
                "occurred_at": row.occurred_at,
                "recorded_at": row.recorded_at,
                "actor_id": row.actor_id,
                "actor_type": row.actor_type,
                "user_id": row.user_id,
                "task_id": row.task_id,
                "run_id": row.run_id,
                "step_node_id": row.step_node_id,
                "tool_id": row.tool_id,
                "action_id": row.action_id,
                "target_kind": row.target_kind,
                "target_id": row.target_id,
                "outcome": row.outcome,
                "evidence_class": row.evidence_class,
                "source": row.source,
                "correlation_id": row.correlation_id,
                "idempotency_key": row.idempotency_key,
                "payload": row.payload,
                "payload_hash": row.payload_hash,
            }
        )
    return out, refused


# --- failure classification ------------------------------------------------


def _is_missing_table(exc: OperationalError) -> bool:
    text = str(exc).lower()
    return "no such table" in text or "does not exist" in text


def _is_transient_lock(exc: OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _maybe_disable(exc: OperationalError) -> None:
    """A missing event table disables recording for this process (spec §8.1)."""
    global _disabled
    if _is_missing_table(exc) and not _disabled:
        _disabled = True
        logger.warning(
            "graph event recording disabled for this process: %s",
            type(exc).__name__,
        )


def _safe_warn(message: str, event_type: str, exc: BaseException) -> None:
    # Never log payloads, paths or ids beyond the event type and error class.
    logger.warning(
        "graph event %s event_type=%s error=%s", message, event_type, type(exc).__name__
    )
