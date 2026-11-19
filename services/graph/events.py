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
- **Dialect-aware retries.** Only *concurrency conflicts* are retried, decided
  by the driver's SQLSTATE on PostgreSQL (class ``40``: ``40P01`` deadlock,
  ``40001`` serialization failure; plus ``55P03``) and by SQLite's lock
  message on SQLite. Each retry opens a fresh transaction; constraint,
  provenance, input and connection/configuration errors are never retried.
- **Default off.** ``V3_EVENTS_ENABLED`` gates everything (design §13).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from services.graph import actions, schema
from services.graph.provenance import ProvenanceError, verify_subject

logger = logging.getLogger("raivisox.graph.events")

TRUTHY = frozenset({"1", "true", "yes", "on"})

# Retry policy (spec §8.2): at most three attempts, 50 ms then 200 ms.
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.05, 0.2)

# Concurrency conflicts a retry can safely resolve. PostgreSQL aborts the
# *whole transaction* for these (class 40) and guarantees nothing in it was
# committed, so the only correct response is a fresh transaction; retrying a
# single statement inside the aborted one would just raise "current transaction
# is aborted". 55P03 (lock_not_available) is the same shape: nobody's work was
# lost, the lock was simply not obtainable this instant.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01", "55P03"})
_TRANSIENT_SQLSTATE_CLASSES = frozenset({"40"})  # transaction rollback
_MISSING_TABLE_SQLSTATES = frozenset({"42P01", "3F000"})  # undefined table/schema

# SQLite reports lock contention as text and never as a SQLSTATE.
_SQLITE_LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)

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
        # An adapter may only speak about an invocation the registry issued,
        # and only as that invocation: the trusted binding captured at
        # registration must agree with the identity this event carries.
        if not actions.validate(
            action_id,
            actor_id=actor_id,
            user_id=user_id,
            task_id=task_id,
            run_id=run_id,
            step_node_id=step_node_id,
            tool_id=tool_id,
        ):
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
    owned = await _check_ownership(
        factory, task_id=task_id, user_id=user_id, run_id=run_id, event_type=event_type
    )
    if owned is None:
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

    async def _insert() -> None:
        # A fresh session (and therefore a fresh transaction) per attempt:
        # after 40P01/40001 the old transaction is aborted server-side, so
        # continuing inside it is never an option.
        async with factory() as session:
            session.add(_row())
            await session.commit()

    try:
        await _retry_transient(_insert)
    except IntegrityError:
        # Deterministic key already stored: the fact is recorded once. This is
        # also what a retry lands on when a previous attempt did commit but
        # lost its connection before reporting success.
        _inc("duplicates")
        return True
    except DBAPIError as exc:
        if _is_missing_table(exc):
            _inc("failures")
            _maybe_disable(exc)
            _safe_warn("event table missing", event_type, exc)
            return False
        _inc("failures")
        _safe_warn("event write failed", event_type, exc)
        return False
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        _inc("failures")
        _safe_warn("event write failed", event_type, exc)
        return False
    _inc("written")
    return True


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


def _sqlstate(exc: BaseException) -> Optional[str]:
    """Return the SQLSTATE a driver attached to ``exc``, if it attached one.

    Drivers disagree on the attribute name (``sqlstate`` for asyncpg and
    psycopg3, ``pgcode`` for psycopg2) and on where the driver exception sits
    (``DBAPIError.orig``, or one of the ``__cause__``/``__context__`` links),
    so the whole chain is inspected rather than one guessed slot. Structured
    codes only: a message that merely *mentions* a state is not evidence.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        try:
            for attr in ("sqlstate", "pgcode"):
                value = getattr(current, attr, None)
                if isinstance(value, str) and len(value) == 5:
                    return value.upper()
            for attr in ("orig", "__cause__", "__context__", "asyncpg_error"):
                nested = getattr(current, attr, None)
                if isinstance(nested, BaseException):
                    stack.append(nested)
        except Exception:  # pragma: no cover - a hostile driver attribute
            continue
    return None


def _is_missing_table(exc: BaseException) -> bool:
    if _sqlstate(exc) in _MISSING_TABLE_SQLSTATES:
        return True
    text = str(exc).lower()
    return "no such table" in text or "does not exist" in text


def _is_transient_lock(exc: BaseException) -> bool:
    """True only for concurrency conflicts a bounded retry can resolve.

    PostgreSQL evidence comes from the driver's SQLSTATE: class 40 (deadlock
    detected ``40P01``, serialization failure ``40001``) and ``55P03``
    (``lock_not_available``). SQLite keeps its message-based rule. Anything
    else — constraint violations, authorization errors, bad configuration, a
    closed or unreachable server — is *not* transient and must not be retried:
    retrying those only delays an honest failure.
    """
    state = _sqlstate(exc)
    if state is not None:
        return state in _TRANSIENT_SQLSTATES or state[:2] in _TRANSIENT_SQLSTATE_CLASSES
    text = str(exc).lower()
    return any(marker in text for marker in _SQLITE_LOCK_MARKERS)


def _attempt_delay(attempt: int) -> float:
    """Backoff for ``attempt`` (0-based), bounded by the published schedule."""
    return _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]


async def _retry_transient(operation: Callable[[], Awaitable[Any]]) -> Any:
    """Await ``operation``, retrying only transient concurrency conflicts.

    ``operation`` must open its own session/transaction: a retry restarts the
    *whole* transaction, because PostgreSQL has already aborted the previous
    one (a single statement retried inside it would fail with 25P02).

    Bounded by ``_MAX_ATTEMPTS`` with the published backoff, so a persistent
    conflict surfaces instead of spinning. The final database error is
    propagated unchanged to the caller, as is ``CancelledError`` during the
    backoff (shutdown must not be delayed by retries). Non-transient errors
    are raised on the first attempt.
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await operation()
        except DBAPIError as exc:
            if not _is_transient_lock(exc) or attempt >= _MAX_ATTEMPTS - 1:
                raise
            _inc("retries")
            await asyncio.sleep(_attempt_delay(attempt))
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


def _maybe_disable(exc: BaseException) -> None:
    """A missing event table disables recording for this process (spec §8.1)."""
    global _disabled
    if _is_missing_table(exc) and not _disabled:
        _disabled = True
        logger.warning(
            "graph event recording disabled for this process: %s",
            type(exc).__name__,
        )


async def _check_ownership(
    factory: Callable[[], Any],
    *,
    task_id: str,
    user_id: str,
    run_id: Optional[str],
    event_type: str,
) -> Optional[bool]:
    """Verify the subject, retrying only transient concurrency errors.

    Returns the verdict (``True``/``False``), or ``None`` when the check itself
    could not run — already counted and logged here. Each attempt uses a fresh
    session, so a retried check never resumes an aborted transaction.
    """

    async def _check() -> bool:
        async with factory() as session:
            return await verify_subject(
                session, task_id=task_id, user_id=user_id, run_id=run_id
            )

    try:
        return await _retry_transient(_check)
    except DBAPIError as exc:
        _inc("failures")
        if _is_missing_table(exc):
            _maybe_disable(exc)
        _safe_warn("ownership check failed", event_type, exc)
        return None
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        _inc("failures")
        _safe_warn("ownership check failed", event_type, exc)
        return None


def _safe_warn(message: str, event_type: str, exc: BaseException) -> None:
    # Never log payloads, paths or ids beyond the event type and error class.
    logger.warning(
        "graph event %s event_type=%s error=%s", message, event_type, type(exc).__name__
    )
