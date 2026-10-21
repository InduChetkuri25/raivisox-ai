"""Retry policy for the V3 event writer (spec §8.2).

Two questions are kept apart on purpose:

1. **Classification** — is this database error a concurrency conflict that a
   retry can actually resolve? Decided from the driver's structured SQLSTATE
   on PostgreSQL (class 40: ``40P01`` deadlock, ``40001`` serialization
   failure; plus ``55P03``) and from SQLite's lock message on SQLite. Never
   "any OperationalError is a lock".
2. **Retry behaviour** — bounded attempts, a fresh transaction per attempt, no
   swallowed cancellation, and the final error propagated to the caller.

These tests need no server: they pin the policy. The real PostgreSQL server is
exercised in ``test_postgres_phase1.py`` (T34j–T34n), including genuine
deadlocks and serialization failures.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from services.graph import actions, events, schema
from tests.unit.v3_phase1_helpers import (
    build_stack,
    count_rows,
    emit_event,
    fetch_rows,
    live_action,
)


# ---------------------------------------------------------------------------
# Fakes that look like the drivers' own exception objects
# ---------------------------------------------------------------------------


class _DriverError(Exception):
    """A stand-in for a driver exception carrying a SQLSTATE.

    asyncpg exposes ``sqlstate``; psycopg2 exposes ``pgcode``. SQLAlchemy's
    asyncpg adapter copies the code onto both names, which is why the
    classifier accepts either spelling.
    """

    def __init__(self, message="driver error", *, sqlstate=None, pgcode=None):
        super().__init__(message)
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if pgcode is not None:
            self.pgcode = pgcode


def _dbapi_error(*, sqlstate=None, pgcode=None, message="driver error") -> DBAPIError:
    return DBAPIError(
        "INSERT INTO graph_events ...",
        {},
        _DriverError(message, sqlstate=sqlstate, pgcode=pgcode),
    )


def _deadlock() -> DBAPIError:
    return _dbapi_error(sqlstate="40P01", message="deadlock detected")


def _serialization() -> DBAPIError:
    return _dbapi_error(sqlstate="40001", message="could not serialize access")


@pytest.fixture
def fast_backoff(monkeypatch):
    """Record nothing, wait nothing: the schedule itself is tested separately."""

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(events, "_attempt_delay", lambda attempt: 0.0)
    monkeypatch.setattr(events.asyncio, "sleep", _no_sleep)


# ---------------------------------------------------------------------------
# Classification — SQLSTATE is the fact, message text is only the SQLite path
# ---------------------------------------------------------------------------


def test_asyncpg_and_psycopg_spellings_both_yield_sqlstate():
    assert events._sqlstate(_dbapi_error(sqlstate="40P01")) == "40P01"
    assert events._sqlstate(_dbapi_error(pgcode="40001")) == "40001"
    # SQLAlchemy's asyncpg adapter sets both attributes on the translated error.
    assert events._sqlstate(_dbapi_error(sqlstate="55P03", pgcode="55P03")) == "55P03"


def test_sqlstate_is_found_through_the_exception_chain():
    inner = _DriverError("deadlock detected", sqlstate="40P01")
    outer = RuntimeError("commit failed")
    outer.__cause__ = inner
    wrapped = DBAPIError("INSERT ...", {}, outer)
    assert events._sqlstate(wrapped) == "40P01"


def test_sqlstate_absent_is_reported_as_absent():
    assert events._sqlstate(OperationalError("COMMIT", {}, Exception("closed"))) is None
    # A SQLSTATE-shaped substring in the message is not a structured code.
    assert events._sqlstate(_dbapi_error(message="error 40P01 happened")) is None


@pytest.mark.parametrize(
    "sqlstate",
    ["40P01", "40001", "40000", "55P03"],  # deadlock, serialization, other class 40, lock
)
def test_concurrency_conflicts_are_transient(sqlstate):
    assert events._is_transient_lock(_dbapi_error(sqlstate=sqlstate)) is True


@pytest.mark.parametrize(
    "sqlstate",
    [
        "23505",  # unique_violation — the idempotency path, never a lock
        "23503",  # foreign_key_violation
        "42501",  # insufficient_privilege — authorization, not contention
        "42P01",  # undefined_table — missing migration, retrying changes nothing
        "25P02",  # in_failed_sql_transaction — the *effect*, not the cause
        "08006",  # connection_failure — permanent or handled elsewhere
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "28P01",  # invalid_password — configuration
        "57014",  # query_canceled — shutdown/cancellation
    ],
)
def test_non_concurrency_errors_are_not_transient(sqlstate):
    assert events._is_transient_lock(_dbapi_error(sqlstate=sqlstate)) is False


def test_sqlite_lock_messages_stay_transient():
    for message in (
        "database is locked",
        "database table is locked: graph_events",
        "database schema is locked",
        "(sqlite3.OperationalError) database is locked",
    ):
        exc = OperationalError("INSERT ...", {}, Exception(message))
        assert events._is_transient_lock(exc) is True, message


def test_non_lock_message_is_not_transient():
    exc = OperationalError("COMMIT", {}, Exception("connection is closed"))
    assert events._is_transient_lock(exc) is False
    assert events._is_transient_lock(RuntimeError("boom")) is False


def test_missing_table_is_recognised_by_sqlstate_and_text():
    assert events._is_missing_table(_dbapi_error(sqlstate="42P01")) is True
    assert events._is_missing_table(_dbapi_error(sqlstate="3F000")) is True
    assert events._is_missing_table(
        OperationalError("SELECT", {}, Exception("no such table: graph_events"))
    ) is True
    assert events._is_missing_table(_dbapi_error(sqlstate="40P01")) is False


def test_backoff_schedule_is_bounded():
    assert events._attempt_delay(0) == 0.05
    assert events._attempt_delay(1) == 0.2
    assert events._attempt_delay(2) == 0.2  # never grows past the last step
    assert events._MAX_ATTEMPTS == 3


# ---------------------------------------------------------------------------
# Retry primitive — attempts, backoff, propagation, cancellation
# ---------------------------------------------------------------------------


async def test_transient_retry_succeeds_within_budget(monkeypatch):
    events.reset_state()
    sleeps: list[float] = []

    async def _sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(events.asyncio, "sleep", _sleep)
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _deadlock()
        return "ok"

    assert await events._retry_transient(_op) == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.05, 0.2]
    assert events.counters()["retries"] == 2


async def test_transient_retry_is_bounded_and_propagates_the_last_error(fast_backoff):
    events.reset_state()
    calls = {"n": 0}
    errors = []

    async def _op():
        calls["n"] += 1
        exc = _deadlock()
        errors.append(exc)
        raise exc

    with pytest.raises(DBAPIError) as caught:
        await events._retry_transient(_op)

    assert calls["n"] == events._MAX_ATTEMPTS == 3
    assert events.counters()["retries"] == 2
    # The error the caller sees is the final attempt's, unchanged.
    assert caught.value is errors[-1]


async def test_permanent_error_is_not_retried(fast_backoff):
    events.reset_state()
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        raise _dbapi_error(sqlstate="42501", message="permission denied")

    with pytest.raises(DBAPIError):
        await events._retry_transient(_op)
    assert calls["n"] == 1
    assert events.counters()["retries"] == 0


async def test_cancellation_during_backoff_is_not_swallowed(monkeypatch):
    events.reset_state()
    monkeypatch.setattr(events, "_attempt_delay", lambda attempt: 0.0)

    async def _cancelled(_delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr(events.asyncio, "sleep", _cancelled)
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        raise _serialization()

    with pytest.raises(asyncio.CancelledError):
        await events._retry_transient(_op)
    # Shutdown stops the retry immediately instead of finishing the budget.
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Writer behaviour — one row, no duplicates, no escape into the pipeline
# ---------------------------------------------------------------------------


class _FailingSession:
    """Proxy for an AsyncSession that fails once, at ``fail_on``.

    The provenance check also opens a session from the same factory, so a
    ``commit`` failure only fires once a row has actually been added — that
    keeps the queue aimed at the write path instead of the read before it.
    """

    def __init__(self, session, queue, fail_on):
        self._session = session
        self._queue = queue
        self._fail_on = fail_on
        self._error = None

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._session.__aexit__(*exc_info)

    def add(self, obj):
        self._session.add(obj)
        # A write attempt claims the next queued failure; a read does not.
        if self._fail_on == "commit" and self._error is None and self._queue:
            self._error = self._queue.pop(0)

    async def execute(self, *args, **kwargs):
        if self._fail_on == "execute" and self._queue:
            self._error = self._queue.pop(0)
            raise self._error
        return await self._session.execute(*args, **kwargs)

    async def commit(self):
        if self._error is not None:
            raise self._error
        return await self._session.commit()

    async def rollback(self):
        return await self._session.rollback()


def _flaky_factory(real_factory, errors, *, fail_on="commit"):
    """A factory whose next ``len(errors)`` write attempts fail with ``errors[i]``."""
    queue = list(errors)

    def _factory():
        session = real_factory()
        if queue:
            return _FailingSession(session, queue, fail_on)
        return session

    return _factory


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("V3_EVENTS_ENABLED", "1")
    events.reset_state()
    actions.clear()
    built = await build_stack(tmp_path)
    events.set_session_factory(built.factory)
    try:
        yield built
    finally:
        events.reset_state()
        actions.clear()
        await built.engine.dispose()


async def test_writer_retries_transient_write_and_writes_once(stack, fast_backoff):
    factory = _flaky_factory(stack.factory, [_deadlock(), _serialization()])
    action_id = live_action()

    assert await emit_event(stack, action_id=action_id, session_factory=factory) is True
    assert events.counters()["retries"] == 2
    assert events.counters()["written"] == 1
    assert events.counters()["failures"] == 0
    assert events.counters()["duplicates"] == 0

    rows = await fetch_rows(stack)
    assert len(rows) == 1
    assert rows[0]["action_id"] == action_id

    # Idempotency survives the retries: the same fact is still one row.
    assert await emit_event(stack, action_id=action_id, session_factory=stack.factory) is True
    assert await count_rows(stack) == 1
    assert events.counters()["duplicates"] == 1


async def test_writer_never_inserts_twice_when_a_retry_hits_the_committed_row(
    stack, fast_backoff
):
    """A commit that succeeded but reported failure must still yield one row.

    The first attempt really commits, then raises as if the connection died.
    The retry collides on the idempotency key, which is a duplicate — not a
    second event, and not a failure.
    """
    real = stack.factory
    errors = [_deadlock()]

    class _CommittedThenLost(_FailingSession):
        async def commit(self):
            await self._session.commit()
            raise self._error

    def _factory():
        session = real()
        if errors:
            return _CommittedThenLost(session, errors, "commit")
        return session

    assert await emit_event(stack, session_factory=_factory) is True
    assert await count_rows(stack) == 1
    assert events.counters()["duplicates"] == 1
    assert events.counters()["failures"] == 0


async def test_writer_gives_up_after_bounded_retries(stack, fast_backoff):
    factory = _flaky_factory(stack.factory, [_deadlock() for _ in range(10)])
    assert await emit_event(stack, session_factory=factory) is False

    assert events.counters()["retries"] == 2  # two retries, three attempts
    assert events.counters()["failures"] == 1
    assert events.counters()["written"] == 0
    assert await count_rows(stack) == 0
    # The real factory still works: a bounded failure does not disable writing.
    assert await emit_event(stack, session_factory=stack.factory) is True


async def test_writer_does_not_retry_permanent_write_errors(stack, fast_backoff):
    factory = _flaky_factory(
        stack.factory,
        [_dbapi_error(sqlstate="42501", message="permission denied")],
    )
    assert await emit_event(stack, session_factory=factory) is False
    assert events.counters()["retries"] == 0
    assert events.counters()["failures"] == 1
    assert await count_rows(stack) == 0


async def test_writer_retries_transient_ownership_check(stack, fast_backoff):
    """The provenance read restarts its transaction too, and writes once."""
    factory = _flaky_factory(stack.factory, [_serialization()], fail_on="execute")
    assert await emit_event(stack, session_factory=factory) is True
    assert events.counters()["retries"] == 1
    assert await count_rows(stack) == 1


async def test_writer_does_not_retry_permanent_ownership_error(stack, fast_backoff):
    factory = _flaky_factory(
        stack.factory,
        [_dbapi_error(sqlstate="42501", message="permission denied")],
        fail_on="execute",
    )
    assert await emit_event(stack, session_factory=factory) is False
    assert events.counters()["retries"] == 0
    assert events.counters()["failures"] == 1
    assert await count_rows(stack) == 0


async def test_writer_propagates_cancellation_instead_of_counting_it(
    stack, monkeypatch
):
    monkeypatch.setattr(events, "_attempt_delay", lambda attempt: 0.0)

    async def _cancelled(_delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr(events.asyncio, "sleep", _cancelled)
    factory = _flaky_factory(stack.factory, [_deadlock() for _ in range(5)])

    with pytest.raises(asyncio.CancelledError):
        await emit_event(stack, session_factory=factory)
    # Cancellation is shutdown, not a failed observation: nothing is counted
    # as a write failure and no partial row exists.
    assert events.counters()["failures"] == 0
    assert await count_rows(stack) == 0


async def test_duplicate_key_is_still_a_duplicate_not_a_retry(stack, fast_backoff):
    """IntegrityError keeps its meaning: recorded once, no retry, no failure."""
    action_id = live_action()
    assert await emit_event(stack, action_id=action_id) is True

    duplicate = IntegrityError(
        "INSERT ...", {}, _DriverError("duplicate key", sqlstate="23505")
    )
    factory = _flaky_factory(stack.factory, [duplicate])
    assert await emit_event(stack, action_id=action_id, session_factory=factory) is True
    assert events.counters()["duplicates"] == 1
    assert events.counters()["retries"] == 0
    assert await count_rows(stack) == 1


async def test_writer_still_disables_on_missing_table(stack, fast_backoff):
    from sqlalchemy import text

    async with stack.factory() as session:
        await session.execute(text("DROP TABLE graph_events"))
        await session.commit()

    assert await emit_event(stack) is False
    assert events.counters()["failures"] == 1
    assert events.counters()["retries"] == 0
    # Disabled for the process: no further attempts, no retry storm.
    assert await emit_event(stack) is False
    # The table is gone, so the only honest check is that nothing was written
    # anywhere: a direct read must fail rather than return rows.
    with pytest.raises(OperationalError):
        await count_rows(stack)


async def test_event_ids_are_distinct_across_retries(stack, fast_backoff):
    """Each attempt builds a fresh row; a retry cannot resurrect a used instance."""
    factory = _flaky_factory(stack.factory, [_deadlock()])
    assert await emit_event(stack, session_factory=factory) is True
    rows = await fetch_rows(stack)
    assert len(rows) == 1
    assert str(uuid.UUID(rows[0]["event_id"])) == rows[0]["event_id"]
    assert rows[0]["schema_version"] == schema.SCHEMA_VERSION
