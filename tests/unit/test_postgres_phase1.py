"""V3 Phase 1 acceptance tests — the PostgreSQL path (T34b–T34y).

T34 (`test_migration_003.py`) proves that migration 003 applies on a
PostgreSQL URL. That is necessary but not sufficient: the writer, the audit
link transaction, the foreign keys and the restart behaviour all use SQL that
SQLite and PostgreSQL execute differently. These tests run the same
guarantees against a real PostgreSQL server, so the dialect-specific claims in
the acceptance report are evidence rather than design argument.

Every test gets a throwaway database created from `RAIVISOX_TEST_POSTGRES_URL`,
migrated with Alembic (never `create_all`, so migration drift cannot hide), and
dropped afterwards. The role in that URL therefore needs the CREATE DATABASE
privilege; nothing else is assumed.

Run:  RAIVISOX_TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@host:5432/db \\
      python -m pytest tests/unit/test_postgres_phase1.py -q
Skips (with a reason) when the variable is unset — PostgreSQL is then
unverified, never silently "passing".
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from integrations.adapters import AdapterRegistry, FileReadAdapter
from services.graph import actions, events, schema
from services.graph.provenance import ProvenanceError
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import (
    Stack,
    ctx_for,
    count_rows,
    emit_event,
    fetch_rows,
    live_action,
    write_scope_file,
)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_API = os.path.join(_REPO, "apps", "api")
_PG_URL = os.environ.get("RAIVISOX_TEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="RAIVISOX_TEST_POSTGRES_URL not set — PostgreSQL is unverified",
)

# The pre-V3 shape of audit_events (migration 002 + the two timestamp columns).
_LEGACY_AUDIT_COLUMNS = {
    "id",
    "event_id",
    "actor_id",
    "action",
    "resource",
    "allowed",
    "request_id",
    "details",
    "timestamp",
    "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# Database lifecycle helpers (asyncpg only: it is the declared driver)
# ---------------------------------------------------------------------------


def _url(database: str | None = None):
    """Normalise the test URL to the asyncpg driver, optionally for a database."""
    url = make_url(_PG_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        raise pytest.UsageError(
            "RAIVISOX_TEST_POSTGRES_URL must be a PostgreSQL URL "
            "(postgresql://... or postgresql+asyncpg://...)"
        )
    if database is not None:
        url = url.set(database=database)
    return url


def _as_string(url) -> str:
    return url.render_as_string(hide_password=False)


async def _admin_query(statement: str, **params) -> None:
    """Run one statement on the maintenance database (CREATE/DROP DATABASE)."""
    engine = create_async_engine(
        _url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _create_database(name: str) -> None:
    await _admin_query(f'CREATE DATABASE "{name}"')


async def _drop_database(name: str) -> None:
    # Terminate stragglers first: a pooled connection from a failed test must
    # not make the cleanup (or the next run) fail.
    await _admin_query(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = :name AND pid <> pg_backend_pid()",
        name=name,
    )
    await _admin_query(f'DROP DATABASE IF EXISTS "{name}"')


def _alembic(url: str, *args: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([_REPO, _API])
    env["DATABASE_URL"] = url
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_API,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} failed\n"
        + (proc.stdout or "")[-2000:]
        + (proc.stderr or "")[-2000:]
    )


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def _audit_columns(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return {
                column["name"]
                for column in await conn.run_sync(
                    lambda c: inspect(c).get_columns("audit_events")
                )
            }
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_trail():
    """The audit queue is process-global; never let a test inherit rows."""
    AuditTrail.instance().clear()
    yield
    AuditTrail.instance().clear()


@pytest.fixture
async def pg_url():
    """A throwaway, empty database reachable with the asyncpg driver."""
    name = f"raivisox_v3_{uuid.uuid4().hex[:12]}"
    await _create_database(name)
    url = _as_string(_url(name))
    try:
        yield url
    finally:
        await _drop_database(name)


@pytest.fixture
def migrated(pg_url):
    """The throwaway database upgraded to head with Alembic."""
    _alembic(pg_url, "upgrade", "head")
    return pg_url


async def _seed(factory) -> Stack:
    from app.models.execution_run import ExecutionRun
    from app.models.task import Task
    from app.models.user import User

    user_id, task_id, run_id = (str(uuid.uuid4()) for _ in range(3))
    async with factory() as session:
        session.add(
            User(
                id=user_id,
                email=f"{user_id[:8]}@example.test",
                hashed_password="not-used",
            )
        )
        await session.flush()
        session.add(
            Task(
                id=task_id,
                user_id=user_id,
                title="pg phase1 task",
                goal="pg phase1 goal",
                status="created",
            )
        )
        await session.flush()
        session.add(ExecutionRun(id=run_id, task_id=task_id, status="running"))
        await session.commit()
    return Stack(None, factory, user_id, task_id, run_id, "")


@pytest.fixture
async def pg_stack(migrated, tmp_path, monkeypatch):
    """A real subject in the migrated PostgreSQL database with recording on."""
    monkeypatch.setenv("V3_EVENTS_ENABLED", "1")
    events.reset_state()
    actions.clear()

    engine = create_async_engine(migrated, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    stack = await _seed(factory)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    stack.engine = engine
    stack.workspace = str(workspace)

    events.set_session_factory(factory)
    try:
        yield stack
    finally:
        events.reset_state()
        actions.clear()
        await engine.dispose()


# ---------------------------------------------------------------------------
# T34b — the migrated PostgreSQL schema carries the V3 constraints
# ---------------------------------------------------------------------------


async def test_postgres_migration_creates_v3_schema(migrated):
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
            audit_cols = {
                column["name"]
                for column in await conn.run_sync(
                    lambda c: inspect(c).get_columns("audit_events")
                )
            }
            graph = await conn.run_sync(
                lambda c: {
                    "pk": inspect(c).get_pk_constraint("graph_events"),
                    "uniques": {
                        u["name"] for u in inspect(c).get_unique_constraints("graph_events")
                    },
                    "fks": {
                        fk["name"]: fk for fk in inspect(c).get_foreign_keys("graph_events")
                    },
                    "indexes": {i["name"] for i in inspect(c).get_indexes("graph_events")},
                }
            )
            links = await conn.run_sync(
                lambda c: {
                    "pk": inspect(c).get_pk_constraint("audit_links"),
                    "fks": {fk["name"]: fk for fk in inspect(c).get_foreign_keys("audit_links")},
                    "indexes": {i["name"] for i in inspect(c).get_indexes("audit_links")},
                }
            )
            seq_default = (
                await conn.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name = 'graph_events' AND column_name = 'seq'"
                    )
                )
            ).scalar()
            fk_delete_rules = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT constraint_name, delete_rule "
                            "FROM information_schema.referential_constraints "
                            "WHERE constraint_name IN "
                            "('fk_graph_events_task_id', 'fk_audit_links_event_id')"
                        )
                    )
                ).all()
            )
            version = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
    finally:
        await engine.dispose()

    assert {"graph_events", "audit_links"} <= tables
    assert version == "003"
    # audit_events is untouched on PostgreSQL too (T19b is dialect-independent).
    assert audit_cols == _LEGACY_AUDIT_COLUMNS

    assert graph["pk"]["constrained_columns"] == ["seq"]
    assert {
        "uq_graph_events_event_id",
        "uq_graph_events_idempotency_key",
    } <= graph["uniques"]
    assert graph["fks"]["fk_graph_events_task_id"]["referred_table"] == "tasks"
    assert {
        "ix_graph_events_user_seq",
        "ix_graph_events_task_seq",
        "ix_graph_events_run_seq",
        "ix_graph_events_action",
        "ix_graph_events_type",
    } <= graph["indexes"]
    # Appending order comes from a sequence, not from timestamps.
    assert "nextval" in (seq_default or "")

    assert links["pk"]["constrained_columns"] == ["event_id"]
    assert links["fks"]["fk_audit_links_event_id"]["referred_table"] == "audit_events"
    assert {
        "ix_audit_links_user_id",
        "ix_audit_links_task_id",
        "ix_audit_links_run_id",
        "ix_audit_links_action_id",
    } <= links["indexes"]
    # PostgreSQL reports the cascade rule the migration declared.
    assert fk_delete_rules == {
        "fk_graph_events_task_id": "CASCADE",
        "fk_audit_links_event_id": "CASCADE",
    }


# ---------------------------------------------------------------------------
# T34c — upgrade → downgrade → upgrade round-trips on PostgreSQL
# ---------------------------------------------------------------------------


async def test_postgres_upgrade_downgrade_round_trip(pg_url):
    _alembic(pg_url, "upgrade", "head")
    assert {"graph_events", "audit_links"} <= await _tables(pg_url)
    audit_before = await _audit_columns(pg_url)

    _alembic(pg_url, "downgrade", "002")

    after_downgrade = await _tables(pg_url)
    assert "audit_events" in after_downgrade
    assert not ({"graph_events", "audit_links"} & after_downgrade)
    assert await _audit_columns(pg_url) == audit_before, "downgrade must not ALTER audit_events"

    _alembic(pg_url, "upgrade", "head")
    assert {"graph_events", "audit_links"} <= await _tables(pg_url)
    assert await _audit_columns(pg_url) == audit_before


# ---------------------------------------------------------------------------
# T34d — events persist, deduplicate and refuse forged identity
# ---------------------------------------------------------------------------


async def test_postgres_emit_persists_and_deduplicates(pg_stack):
    assert await emit_event(pg_stack) is True
    assert await count_rows(pg_stack) == 1

    rows = await fetch_rows(pg_stack)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == schema.TOOL_ATTEMPTED
    assert row["outcome"] == schema.ATTEMPTED
    assert row["user_id"] == pg_stack.user_id
    assert row["task_id"] == pg_stack.task_id
    assert row["run_id"] == pg_stack.run_id
    assert row["payload"] == {"resource_ref": "task:x/step:y"}

    # Re-emitting the same action is a no-op, not a second fact.
    first = await fetch_rows(pg_stack)
    action_id = first[0]["action_id"]
    assert (
        await events.emit(
            event_type=schema.TOOL_ATTEMPTED,
            outcome=schema.ATTEMPTED,
            actor_id="pipeline-service",
            user_id=pg_stack.user_id,
            task_id=pg_stack.task_id,
            run_id=pg_stack.run_id,
            step_node_id="step-1",
            tool_id="fs_read",
            action_id=action_id,
            payload={"resource_ref": "task:x/step:y"},
        )
    ) is True
    assert await count_rows(pg_stack) == 1

    # A forged owner is an integrity fault, and writes nothing.
    with pytest.raises(ProvenanceError):
        await emit_event(pg_stack, user_id=str(uuid.uuid4()), action_id=live_action())
    assert await count_rows(pg_stack) == 1


# ---------------------------------------------------------------------------
# T34e — the audit row and its link are one transaction on PostgreSQL
# ---------------------------------------------------------------------------


async def test_postgres_audit_link_is_atomic(pg_stack, monkeypatch):
    import app.models.audit_link as link_module

    AuditTrail.instance().log(
        actor_id="legacy-actor", action="api:view_audit", resource="audit", allowed=True
    )
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="x",
        allowed=True,
        user_id=pg_stack.user_id,
        task_id=pg_stack.task_id,
        run_id=pg_stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    assert AuditTrail.instance().pending_count() == 2

    def _boom(*_args, **_kwargs):
        raise RuntimeError("link insert failed")

    monkeypatch.setattr(link_module, "AuditLink", _boom)
    with pytest.raises(RuntimeError):
        async with pg_stack.factory() as session:
            await AuditTrail.instance().flush_pending(session)

    assert await count_rows(pg_stack, "audit_events") == 0, "no orphan audit row"
    assert await count_rows(pg_stack, "audit_links") == 0, "no orphan link"
    assert AuditTrail.instance().pending_count() == 2, "events stay queued"

    monkeypatch.undo()
    async with pg_stack.factory() as session:
        assert await AuditTrail.instance().flush_pending(session) == 2
    assert await count_rows(pg_stack, "audit_events") == 2
    assert await count_rows(pg_stack, "audit_links") == 1
    assert AuditTrail.instance().pending_count() == 0


# ---------------------------------------------------------------------------
# T34f — PostgreSQL enforces the foreign keys (SQLite needs a pragma for this)
# ---------------------------------------------------------------------------


async def test_postgres_foreign_keys_are_enforced(pg_stack):
    assert await emit_event(pg_stack) is True
    assert await count_rows(pg_stack) == 1

    async with pg_stack.factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("INSERT INTO audit_links (event_id) VALUES (:id)"),
                {"id": str(uuid.uuid4())},
            )
            await session.commit()
        await session.rollback()

    # An audit row with a link: deleting the audit row takes the link with it.
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="x",
        allowed=True,
        user_id=pg_stack.user_id,
        task_id=pg_stack.task_id,
        run_id=pg_stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    async with pg_stack.factory() as session:
        await AuditTrail.instance().flush_pending(session)
    assert await count_rows(pg_stack, "audit_links") == 1
    async with pg_stack.factory() as session:
        await session.execute(text("DELETE FROM audit_events"))
        await session.commit()
    assert await count_rows(pg_stack, "audit_links") == 0

    # Deleting the task takes its events with it (nothing to project later).
    async with pg_stack.factory() as session:
        await session.execute(
            text("DELETE FROM tasks WHERE id = :id"), {"id": pg_stack.task_id}
        )
        await session.commit()
    assert await count_rows(pg_stack) == 0


# ---------------------------------------------------------------------------
# T34g — a pre-V3 audit row survives the upgrade, honestly unlinked
# ---------------------------------------------------------------------------


async def test_postgres_legacy_audit_rows_survive_upgrade(pg_url):
    _alembic(pg_url, "upgrade", "002")
    columns_before = await _audit_columns(pg_url)
    assert columns_before == _LEGACY_AUDIT_COLUMNS

    legacy = {
        "id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "actor_id": "legacy-actor",
        "action": "api:view_audit",
        "resource": "audit",
        "allowed": False,
        "request_id": str(uuid.uuid4()),
        "details": {"reason": "pre-v3"},
        "timestamp": 1700000000.0,
    }
    engine = create_async_engine(pg_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, event_id, actor_id, action, resource, allowed, request_id, "
                    " details, timestamp, created_at, updated_at) VALUES "
                    "(:id, :event_id, :actor_id, :action, :resource, :allowed, "
                    " :request_id, :details, :timestamp, :created_at, :updated_at)"
                ),
                {
                    **legacy,
                    "details": '{"reason": "pre-v3"}',
                    "created_at": datetime.datetime(2023, 11, 14, 12, 0, 0),
                    "updated_at": datetime.datetime(2023, 11, 14, 12, 0, 0),
                },
            )
    finally:
        await engine.dispose()

    _alembic(pg_url, "upgrade", "head")
    assert await _audit_columns(pg_url) == columns_before, "upgrade must not ALTER audit_events"

    engine = create_async_engine(pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            stored = (
                await session.execute(
                    text(
                        "SELECT id, event_id, actor_id, action, resource, allowed, "
                        "request_id, details, timestamp FROM audit_events"
                    )
                )
            ).mappings().one()
            raw_details = stored["details"]
            details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
            assert details == legacy["details"]
            for key, value in legacy.items():
                assert stored[key] == value, f"{key} changed during upgrade"
            assert int(
                (await session.execute(text("SELECT COUNT(*) FROM audit_links"))).scalar()
            ) == 0, "legacy rows must not be backfilled with invented identity"

            rows = await AuditTrail.instance().read_from_db(session, limit=10)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == legacy["event_id"]
    assert row["actor_id"] == "legacy-actor"
    assert row["allowed"] is False
    assert row["details"] == {"reason": "pre-v3"}
    for key in ("user_id", "task_id", "run_id", "step_node_id", "action_id"):
        assert row[key] is None, f"{key} must not be invented for legacy rows"


# ---------------------------------------------------------------------------
# T34h — a restart keeps the rows and never reissues a seq
# ---------------------------------------------------------------------------


async def test_postgres_restart_preserves_events_and_seq(pg_stack, migrated):
    first = await emit_event(pg_stack)
    second = await emit_event(pg_stack)
    assert first is True and second is True
    async with pg_stack.factory() as session:
        before = await events.read_events(session, limit=1000)
    seqs_before = [row["seq"] for row in before[0]]
    assert len(seqs_before) == 2 and seqs_before == sorted(seqs_before)

    # Simulate a process restart: the old engine is gone, a new one connects.
    await pg_stack.engine.dispose()
    engine = create_async_engine(migrated, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    events.set_session_factory(factory)
    try:
        async with factory() as session:
            after, refused = await events.read_events(session, limit=1000)
            assert refused == 0
            assert [row["seq"] for row in after] == seqs_before
            assert {row["event_id"] for row in after} == {
                row["event_id"] for row in before[0]
            }

            # A rolled-back / deleted row does not free its seq on PostgreSQL
            # either: the cursor can never skip or replay.
            await session.execute(
                text("DELETE FROM graph_events WHERE seq = :seq"),
                {"seq": seqs_before[-1]},
            )
            await session.commit()

        new_action = live_action()
        assert (
            await events.emit(
                event_type=schema.TOOL_ATTEMPTED,
                outcome=schema.ATTEMPTED,
                actor_id="pipeline-service",
                user_id=pg_stack.user_id,
                task_id=pg_stack.task_id,
                run_id=pg_stack.run_id,
                step_node_id="step-1",
                tool_id="fs_read",
                action_id=new_action,
                payload={"resource_ref": "task:x/step:y"},
            )
        ) is True
        async with factory() as session:
            seq_after = (
                await session.execute(text("SELECT MAX(seq) FROM graph_events"))
            ).scalar()
    finally:
        await engine.dispose()

    assert seq_after > seqs_before[-1], "a deleted seq must never be reissued"


# ---------------------------------------------------------------------------
# T34i — a real adapter run records both the event and the audit link on PG
# ---------------------------------------------------------------------------


async def test_postgres_registry_read_records_event_and_link(pg_stack):
    write_scope_file(pg_stack, "src/data.txt", "0123456789")

    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.READ_FILES)
    registry = AdapterRegistry(kernel)
    registry.register(FileReadAdapter(pg_stack.workspace))

    result = await registry.run("fs_read", {"path": "src/data.txt"}, ctx_for(pg_stack))
    assert result.success, result.error
    assert result.output["content"] == "0123456789"

    async with pg_stack.factory() as session:
        assert await AuditTrail.instance().flush_pending(session) >= 1

    rows = [r for r in await fetch_rows(pg_stack) if r["event_type"] == schema.RESOURCE_READ_OBSERVED]
    assert len(rows) == 1
    assert rows[0]["outcome"] == schema.EXECUTED
    assert rows[0]["target_id"] == f"res:{pg_stack.task_id}/src/data.txt"
    assert rows[0]["payload"]["relpath"] == "src/data.txt"

    async with pg_stack.factory() as session:
        audits = (
            await session.execute(
                text(
                    "SELECT a.event_id FROM audit_events a "
                    "WHERE a.action = 'tool:fs_read' AND a.allowed IS TRUE"
                )
            )
        ).scalars().all()
        links = (
            await session.execute(text("SELECT * FROM audit_links"))
        ).mappings().all()

    assert audits, "a successful read must be audited"
    assert len(links) == len(audits)
    link = dict(links[0])
    assert {row["event_id"] for row in links} == set(audits)
    assert link["user_id"] == pg_stack.user_id
    assert link["task_id"] == pg_stack.task_id
    assert link["run_id"] == pg_stack.run_id
    assert link["action_id"]

# ---------------------------------------------------------------------------
# Transient-error evidence — genuine server-produced SQLSTATEs
# ---------------------------------------------------------------------------


async def _capture_deadlock(url: str) -> DBAPIError:
    """Provoke a real PostgreSQL deadlock and return the server's own error.

    Two transactions each hold one row and then reach for the other's;
    PostgreSQL's deadlock detector aborts one with SQLSTATE ``40P01``. Nothing
    is mocked: the exception handed back is what asyncpg → SQLAlchemy raised,
    including whatever SQLSTATE the driver attached to it.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    conn_a = conn_b = None
    results: list = []
    try:
        async with engine.begin() as setup:
            await setup.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS deadlock_probe "
                    "(id integer PRIMARY KEY, v integer)"
                )
            )
            await setup.execute(text("DELETE FROM deadlock_probe"))
            await setup.execute(
                text("INSERT INTO deadlock_probe (id, v) VALUES (1, 0), (2, 0)")
            )

        conn_a = await engine.connect()
        conn_b = await engine.connect()
        tx_a = await conn_a.begin()
        tx_b = await conn_b.begin()
        await conn_a.execute(text("UPDATE deadlock_probe SET v = v + 1 WHERE id = 1"))
        await conn_b.execute(text("UPDATE deadlock_probe SET v = v + 1 WHERE id = 2"))

        async def _cross_a():
            return await conn_a.execute(
                text("UPDATE deadlock_probe SET v = v + 1 WHERE id = 2")
            )

        async def _cross_b():
            # Let A block first so the cycle is unambiguous.
            await asyncio.sleep(0.3)
            return await conn_b.execute(
                text("UPDATE deadlock_probe SET v = v + 1 WHERE id = 1")
            )

        results = await asyncio.gather(_cross_a(), _cross_b(), return_exceptions=True)
        for tx in (tx_a, tx_b):
            try:
                await tx.rollback()
            except Exception:  # pragma: no cover - an already-aborted victim
                pass
    finally:
        for conn in (conn_a, conn_b):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # pragma: no cover - cleanup only
                    pass
        await engine.dispose()

    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1, f"expected exactly one deadlock victim, got {errors!r}"
    assert isinstance(errors[0], DBAPIError), type(errors[0])
    return errors[0]


async def _capture_serialization_failure(url: str) -> DBAPIError:
    """Provoke a real SERIALIZABLE conflict and return SQLSTATE ``40001``."""
    engine = create_async_engine(url, poolclass=NullPool)
    conn_a = conn_b = None
    error: DBAPIError | None = None
    try:
        async with engine.begin() as setup:
            await setup.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS serial_probe "
                    "(id integer PRIMARY KEY, v integer)"
                )
            )
            await setup.execute(text("DELETE FROM serial_probe"))
            await setup.execute(text("INSERT INTO serial_probe (id, v) VALUES (1, 0)"))

        conn_a = await engine.connect()
        conn_b = await engine.connect()
        await conn_a.execution_options(isolation_level="SERIALIZABLE")
        await conn_b.execution_options(isolation_level="SERIALIZABLE")
        tx_a = await conn_a.begin()
        tx_b = await conn_b.begin()
        # Both read the same snapshot, then both write the same row: the second
        # committer cannot be serialized and is aborted with 40001.
        await conn_a.execute(text("SELECT v FROM serial_probe WHERE id = 1"))
        await conn_b.execute(text("SELECT v FROM serial_probe WHERE id = 1"))
        await conn_a.execute(text("UPDATE serial_probe SET v = v + 1 WHERE id = 1"))
        await tx_a.commit()
        try:
            await conn_b.execute(text("UPDATE serial_probe SET v = v + 1 WHERE id = 1"))
            await tx_b.commit()
        except DBAPIError as exc:
            error = exc
            try:
                await tx_b.rollback()
            except Exception:  # pragma: no cover - already aborted
                pass
    finally:
        for conn in (conn_a, conn_b):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # pragma: no cover - cleanup only
                    pass
        await engine.dispose()

    assert error is not None, "SERIALIZABLE conflict did not surface as a DBAPIError"
    return error


@pytest.fixture
async def real_deadlock(migrated):
    """A real 40P01 produced by the PostgreSQL deadlock detector."""
    return await _capture_deadlock(migrated)


class _FailingSession:
    """Delegating session proxy that fails the next write attempt at commit.

    Read-only work (the provenance check) passes through untouched, so the
    injected error lands exactly on the transaction under test.
    """

    def __init__(self, session, queue):
        self._session = session
        self._queue = queue
        self._error: DBAPIError | None = None

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._session.__aexit__(*exc_info)

    def add(self, obj):
        self._session.add(obj)
        if self._error is None and self._queue:
            self._error = self._queue.pop(0)

    async def execute(self, *args, **kwargs):
        return await self._session.execute(*args, **kwargs)

    async def commit(self):
        if self._error is not None:
            raise self._error
        return await self._session.commit()

    async def rollback(self):
        return await self._session.rollback()


def _failing_factory(real_factory, errors):
    """Factory feeding ``errors`` to the next ``len(errors)`` write attempts."""
    queue = list(errors)

    def _factory():
        session = real_factory()
        if queue:
            return _FailingSession(session, queue)
        return session

    return _factory


class _CommitFails:
    """Session proxy used by ``flush_pending``: commit always raises."""

    def __init__(self, session, error):
        self._session = session
        self._error = error

    def add(self, obj):
        self._session.add(obj)

    async def execute(self, *args, **kwargs):
        return await self._session.execute(*args, **kwargs)

    async def commit(self):
        raise self._error

    async def rollback(self):
        return await self._session.rollback()

    async def close(self):
        return await self._session.close()


# ---------------------------------------------------------------------------
# T34j — a real deadlock is recognised as transient (SQLSTATE 40P01)
# ---------------------------------------------------------------------------


async def test_postgres_deadlock_is_classified_transient(migrated, real_deadlock):
    assert events._sqlstate(real_deadlock) == "40P01", (
        f"driver did not expose the deadlock SQLSTATE: {real_deadlock!r}"
    )
    assert events._is_transient_lock(real_deadlock) is True
    # A deadlock is not a missing table, and must not disable recording.
    assert events._is_missing_table(real_deadlock) is False


# ---------------------------------------------------------------------------
# T34k — a real serialization failure is recognised as transient (40001)
# ---------------------------------------------------------------------------


async def test_postgres_serialization_failure_is_classified_transient(migrated):
    exc = await _capture_serialization_failure(migrated)
    assert events._sqlstate(exc) == "40001", (
        f"driver did not expose the serialization SQLSTATE: {exc!r}"
    )
    assert events._is_transient_lock(exc) is True
    assert events._is_missing_table(exc) is False


# ---------------------------------------------------------------------------
# T34l — the writer retries a server-raised deadlock and commits exactly once
# ---------------------------------------------------------------------------


async def test_postgres_writer_retries_a_real_deadlock(pg_stack, real_deadlock):
    factory = _failing_factory(pg_stack.factory, [real_deadlock])
    action_id = live_action()

    assert await emit_event(pg_stack, action_id=action_id, session_factory=factory) is True
    assert events.counters()["retries"] == 1
    assert events.counters()["written"] == 1
    assert events.counters()["failures"] == 0

    rows = await fetch_rows(pg_stack)
    assert len(rows) == 1
    assert rows[0]["action_id"] == action_id

    # Idempotency survives the retry: the same fact is still one row.
    assert (
        await events.emit(
            event_type=schema.TOOL_ATTEMPTED,
            outcome=schema.ATTEMPTED,
            actor_id="pipeline-service",
            user_id=pg_stack.user_id,
            task_id=pg_stack.task_id,
            run_id=pg_stack.run_id,
            step_node_id="step-1",
            tool_id="fs_read",
            action_id=action_id,
            payload={"resource_ref": "task:x/step:y"},
        )
    ) is True
    assert await count_rows(pg_stack) == 1
    assert events.counters()["duplicates"] == 1


# ---------------------------------------------------------------------------
# T34m — bounded retries: three attempts, then an honest failure
# ---------------------------------------------------------------------------


async def test_postgres_writer_gives_up_after_bounded_retries(pg_stack, real_deadlock):
    factory = _failing_factory(pg_stack.factory, [real_deadlock for _ in range(5)])

    assert await emit_event(pg_stack, session_factory=factory) is False
    assert events.counters()["retries"] == 2, "two retries, three attempts in total"
    assert events.counters()["failures"] == 1
    assert await count_rows(pg_stack) == 0, "a failed transaction leaves no row"

    # Exhausting the budget is not a reason to disable recording.
    assert await emit_event(pg_stack, session_factory=pg_stack.factory) is True
    assert await count_rows(pg_stack) == 1


# ---------------------------------------------------------------------------
# T34n — a permanent server error is not retried
# ---------------------------------------------------------------------------


async def test_postgres_writer_does_not_retry_permanent_error(pg_stack):
    # A genuine, permanent server error: the stored payload hash no longer fits
    # its column, so PostgreSQL raises 22001 (string_data_right_truncation).
    async with pg_stack.factory() as session:
        await session.execute(
            text("ALTER TABLE graph_events ALTER COLUMN payload_hash TYPE varchar(8)")
        )
        await session.commit()

    assert await emit_event(pg_stack) is False
    assert events.counters()["retries"] == 0, "a permanent error must not be retried"
    assert events.counters()["failures"] == 1
    assert await count_rows(pg_stack) == 0


# ---------------------------------------------------------------------------
# T34o — a transient failure keeps the audit queue intact and cannot duplicate
# ---------------------------------------------------------------------------


async def test_postgres_audit_flush_retry_cannot_duplicate(pg_stack, real_deadlock):
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="x",
        allowed=True,
        user_id=pg_stack.user_id,
        task_id=pg_stack.task_id,
        run_id=pg_stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    AuditTrail.instance().log(
        actor_id="legacy-actor", action="api:view_audit", resource="audit", allowed=True
    )
    assert AuditTrail.instance().pending_count() == 2

    failing = _CommitFails(pg_stack.factory(), real_deadlock)
    try:
        with pytest.raises(DBAPIError):
            await AuditTrail.instance().flush_pending(failing)
    finally:
        await failing.close()

    # Nothing was acknowledged: the whole flush rolled back and stayed queued.
    assert AuditTrail.instance().pending_count() == 2
    assert await count_rows(pg_stack, "audit_events") == 0
    assert await count_rows(pg_stack, "audit_links") == 0

    # The retry writes each event exactly once, with its link.
    async with pg_stack.factory() as session:
        assert await AuditTrail.instance().flush_pending(session) == 2
    assert AuditTrail.instance().pending_count() == 0
    assert await count_rows(pg_stack, "audit_events") == 2
    assert await count_rows(pg_stack, "audit_links") == 1

    # A further flush is a no-op: no duplicate audit rows or links.
    async with pg_stack.factory() as session:
        assert await AuditTrail.instance().flush_pending(session) == 0
    assert await count_rows(pg_stack, "audit_events") == 2
    assert await count_rows(pg_stack, "audit_links") == 1


# ---------------------------------------------------------------------------
# Lock-contention evidence — genuine 55P03 (lock_not_available)
# ---------------------------------------------------------------------------


class _TableLock:
    """Hold an ACCESS EXCLUSIVE lock on ``table`` from an independent session.

    This is the ordinary way a writer meets ``55P03``: a concurrent
    DDL-shaped lock (``LOCK TABLE``, ``ALTER TABLE``, ``VACUUM FULL``) on the
    table being written. Nothing is mocked — PostgreSQL blocks the writer's
    own ``INSERT`` until that writer's ``lock_timeout`` cancels the statement
    with ``lock_not_available``.
    """

    def __init__(self, url: str, table: str) -> None:
        self._url = url
        self._table = table
        self._engine = None
        self._conn = None
        self._tx = None
        self._held = False

    async def __aenter__(self) -> "_TableLock":
        self._engine = create_async_engine(self._url, poolclass=NullPool)
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        await self._conn.execute(
            text(f"LOCK TABLE {self._table} IN ACCESS EXCLUSIVE MODE")
        )
        self._held = True
        return self

    async def release(self) -> None:
        """End the holder's transaction, which releases the lock. Idempotent."""
        if not self._held:
            return
        self._held = False
        try:
            await self._tx.rollback()
        except Exception:  # pragma: no cover - already gone
            pass

    async def __aexit__(self, *exc_info) -> None:
        await self.release()
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # pragma: no cover - cleanup only
                pass
        if self._engine is not None:
            await self._engine.dispose()


def _lock_timeout_factory(url: str, milliseconds: int = 150):
    """Session factory whose connections carry ``lock_timeout``.

    The setting travels in the asyncpg startup packet, so it is the writer's
    own session that asks PostgreSQL to give up after ``milliseconds`` instead
    of waiting forever — the way a deployment would bound a lock wait. The
    server, not the client, produces the resulting error.
    """
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": str(milliseconds)}},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _capture_lock_not_available(url: str) -> DBAPIError:
    """Provoke a real PostgreSQL lock timeout and return SQLSTATE ``55P03``.

    One session holds ``ACCESS EXCLUSIVE`` on a probe table; the waiter sets
    ``lock_timeout`` inside its own transaction and then asks for the same
    lock. PostgreSQL itself cancels the waiter with ``canceling statement due
    to lock timeout`` and SQLSTATE ``55P03``. The exception returned is the
    one asyncpg raised through SQLAlchemy: this file synthesises no message
    and attaches no SQLSTATE of its own.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    holder = waiter = None
    error: DBAPIError | None = None
    try:
        async with engine.begin() as setup:
            await setup.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS lock_probe "
                    "(id integer PRIMARY KEY, v integer)"
                )
            )
            await setup.execute(text("DELETE FROM lock_probe"))
            await setup.execute(text("INSERT INTO lock_probe (id, v) VALUES (1, 0)"))

        holder = await engine.connect()
        waiter = await engine.connect()
        tx_holder = await holder.begin()
        tx_waiter = await waiter.begin()
        await holder.execute(text("LOCK TABLE lock_probe IN ACCESS EXCLUSIVE MODE"))
        # Set on the waiter's session, inside its transaction: the timer that
        # fires is PostgreSQL's, so the cancellation is the server's own.
        await waiter.execute(text("SET lock_timeout = '100ms'"))
        try:
            await waiter.execute(text("UPDATE lock_probe SET v = v + 1 WHERE id = 1"))
        except DBAPIError as exc:
            error = exc
        for tx in (tx_waiter, tx_holder):
            try:
                await tx.rollback()
            except Exception:  # pragma: no cover - an already-aborted transaction
                pass
    finally:
        for conn in (waiter, holder):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # pragma: no cover - cleanup only
                    pass
        await engine.dispose()

    assert error is not None, "PostgreSQL did not raise a lock timeout for the waiter"
    return error


class _ReleaseOnError:
    """Session proxy that clears the blocker as the first failure leaves.

    The error the writer sees is the server's own ``55P03``; the proxy only
    models contention that *ends* between attempts, which is the case a
    bounded retry exists for. Without it the lock would outlive the writer's
    budget and the retry could never be observed succeeding.
    """

    def __init__(self, session, release) -> None:
        self._session = session
        self._release = release

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._session.__aexit__(*exc_info)

    def add(self, obj):
        self._session.add(obj)

    async def execute(self, *args, **kwargs):
        return await self._session.execute(*args, **kwargs)

    async def commit(self):
        try:
            return await self._session.commit()
        except DBAPIError:
            await self._release()
            raise

    async def rollback(self):
        return await self._session.rollback()


def _release_on_failure_factory(real_factory, release):
    def _factory():
        return _ReleaseOnError(real_factory(), release)

    return _factory


# ---------------------------------------------------------------------------
# T34p — a real lock timeout is recognised as transient (SQLSTATE 55P03)
# ---------------------------------------------------------------------------


async def test_postgres_lock_timeout_is_classified_transient(migrated):
    exc = await _capture_lock_not_available(migrated)
    assert events._sqlstate(exc) == "55P03", (
        f"driver did not expose the lock-not-available SQLSTATE: {exc!r}"
    )
    assert events._is_transient_lock(exc) is True
    # Lock contention is not a missing table and must not disable recording.
    assert events._is_missing_table(exc) is False


# ---------------------------------------------------------------------------
# T34q — persistent lock contention: bounded retries, then an honest failure
# ---------------------------------------------------------------------------


async def test_postgres_writer_gives_up_after_real_lock_timeouts(pg_stack, migrated):
    engine, factory = _lock_timeout_factory(migrated)
    action_id = live_action()
    try:
        async with _TableLock(migrated, "graph_events"):
            # Every attempt meets the real lock and PostgreSQL cancels all
            # three with 55P03: the budget is bounded and the result is an
            # honest failure, not a slow spin.
            assert (
                await emit_event(pg_stack, action_id=action_id, session_factory=factory)
                is False
            )
        assert events.counters()["retries"] == 2, "two retries, three attempts in total"
        assert events.counters()["failures"] == 1
        assert await count_rows(pg_stack) == 0, "an aborted attempt leaves no row"

        # The lock is gone: the identical fact commits exactly once, so the
        # failed attempts left nothing behind to duplicate.
        assert (
            await emit_event(pg_stack, action_id=action_id, session_factory=factory)
            is True
        )
        assert events.counters()["written"] == 1
        assert events.counters()["duplicates"] == 0
        assert await count_rows(pg_stack) == 1

        # And the fact is still idempotent after the contention cleared.
        assert (
            await emit_event(pg_stack, action_id=action_id, session_factory=factory)
            is True
        )
        assert events.counters()["duplicates"] == 1
        assert await count_rows(pg_stack) == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# T34r — the retry restarts the transaction and commits exactly once
# ---------------------------------------------------------------------------


async def test_postgres_writer_retry_commits_once_when_the_lock_clears(
    pg_stack, migrated
):
    engine, base_factory = _lock_timeout_factory(migrated)
    lock = _TableLock(migrated, "graph_events")
    action_id = live_action()
    try:
        async with lock:
            factory = _release_on_failure_factory(base_factory, lock.release)
            assert (
                await emit_event(pg_stack, action_id=action_id, session_factory=factory)
                is True
            )
        assert events.counters()["retries"] == 1, "one real 55P03, one fresh transaction"
        assert events.counters()["written"] == 1
        assert events.counters()["failures"] == 0
        assert events.counters()["duplicates"] == 0

        rows = await fetch_rows(pg_stack)
        assert len(rows) == 1
        assert rows[0]["action_id"] == action_id
        assert await count_rows(pg_stack) == 1, "a restart commits once, never twice"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# T34s — a real lock timeout cannot acknowledge or duplicate audit rows
# ---------------------------------------------------------------------------


async def test_postgres_audit_flush_survives_a_real_lock_timeout(pg_stack, migrated):
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="x",
        allowed=True,
        user_id=pg_stack.user_id,
        task_id=pg_stack.task_id,
        run_id=pg_stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    AuditTrail.instance().log(
        actor_id="legacy-actor", action="api:view_audit", resource="audit", allowed=True
    )
    assert AuditTrail.instance().pending_count() == 2

    engine, factory = _lock_timeout_factory(migrated)
    try:
        # The link table is what the flush needs last: the audit row is already
        # staged when the link INSERT blocks, so the timeout proves atomicity
        # rather than an early read failure.
        async with _TableLock(migrated, "audit_links"):
            async with factory() as session:
                with pytest.raises(DBAPIError) as raised:
                    await AuditTrail.instance().flush_pending(session)
        assert events._sqlstate(raised.value) == "55P03"
        assert events._is_transient_lock(raised.value) is True

        # Nothing was acknowledged and nothing was persisted: the audit row and
        # its link rolled back together, so no orphan link exists either.
        assert AuditTrail.instance().pending_count() == 2
        assert await count_rows(pg_stack, "audit_events") == 0
        assert await count_rows(pg_stack, "audit_links") == 0

        async with factory() as session:
            assert await AuditTrail.instance().flush_pending(session) == 2
        assert AuditTrail.instance().pending_count() == 0
        assert await count_rows(pg_stack, "audit_events") == 2
        assert await count_rows(pg_stack, "audit_links") == 1

        # A further flush is a no-op: no duplicate audit rows or links.
        async with factory() as session:
            assert await AuditTrail.instance().flush_pending(session) == 0
        assert await count_rows(pg_stack, "audit_events") == 2
        assert await count_rows(pg_stack, "audit_links") == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Aborted-transaction evidence — genuine 25P02 (in_failed_sql_transaction)
# ---------------------------------------------------------------------------


async def _capture_in_failed_transaction(url: str) -> tuple[DBAPIError, DBAPIError]:
    """Abort a real transaction and return its cause plus the server's 25P02.

    The cause is a genuine unique-constraint violation (``23505``) raised by
    PostgreSQL; every later statement in that same transaction is then answered
    by the server with ``25P02`` ("current transaction is aborted, commands
    ignored..."). The connection runs in AUTOCOMMIT isolation so each
    statement reaches the server as written — SQLAlchemy's client-side "pending
    rollback" guard is never substituted for the server's own error.

    Both exceptions are the ones asyncpg raised through SQLAlchemy: nothing here
    synthesises a message or infers a SQLSTATE from text.
    """
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    cause: DBAPIError | None = None
    followup: DBAPIError | None = None
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS abort_probe "
                    "(id integer PRIMARY KEY, v integer)"
                )
            )
            await conn.execute(text("DELETE FROM abort_probe"))
            await conn.execute(text("INSERT INTO abort_probe (id, v) VALUES (1, 0)"))
            await conn.execute(text("BEGIN"))
            try:
                await conn.execute(text("INSERT INTO abort_probe (id, v) VALUES (1, 1)"))
            except DBAPIError as exc:
                cause = exc
            try:
                await conn.execute(text("SELECT 1"))
            except DBAPIError as exc:
                followup = exc
            try:
                await conn.execute(text("ROLLBACK"))
            except DBAPIError:  # pragma: no cover - the transaction is already dead
                pass
    finally:
        await engine.dispose()

    assert cause is not None, "the duplicate insert did not raise"
    assert followup is not None, "PostgreSQL did not reject a statement in the aborted transaction"
    return cause, followup


class _FailedFirstSession:
    """Session proxy whose first write raises the real 25P02 at commit.

    Read-only work (the provenance check) passes through, so the genuine error
    lands exactly on the transaction under test.
    """

    def __init__(self, session, error) -> None:
        self._session = session
        self._error = error

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._session.__aexit__(*exc_info)

    def add(self, obj):
        self._session.add(obj)

    async def execute(self, *args, **kwargs):
        return await self._session.execute(*args, **kwargs)

    async def commit(self):
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return await self._session.commit()

    async def rollback(self):
        return await self._session.rollback()


# ---------------------------------------------------------------------------
# T34t — a real 25P02 is not a transient cause
# ---------------------------------------------------------------------------


async def test_postgres_in_failed_transaction_is_not_transient(migrated):
    cause, followup = await _capture_in_failed_transaction(migrated)

    # The cause is a permanent constraint violation, never retried.
    assert events._sqlstate(cause) == "23505", (
        f"driver did not expose the constraint SQLSTATE: {cause!r}"
    )
    assert events._is_transient_lock(cause) is False

    # The server's own 25P02 is the *effect* of that abort, not a cause: a retry
    # could not succeed and would hide the constraint that actually failed.
    assert events._sqlstate(followup) == "25P02", (
        f"driver did not expose the aborted-transaction SQLSTATE: {followup!r}"
    )
    assert events._is_transient_lock(followup) is False
    assert events._is_missing_table(followup) is False
    assert "current transaction is aborted" in str(followup)


# ---------------------------------------------------------------------------
# T34u — even if 25P02 reached the writer, it is not retried
# ---------------------------------------------------------------------------


async def test_postgres_writer_does_not_retry_in_failed_transaction(pg_stack, migrated):
    _cause, followup = await _capture_in_failed_transaction(migrated)
    factory = _failing_factory(pg_stack.factory, [followup])

    assert await emit_event(pg_stack, session_factory=factory) is False
    assert events.counters()["retries"] == 0, "an aborted-transaction error is never retried"
    assert events.counters()["failures"] == 1
    assert await count_rows(pg_stack) == 0, "a failed transaction leaves no row"

    # The refusal is honest, not fatal: recording is still enabled afterwards.
    assert await emit_event(pg_stack) is True
    assert events.counters()["written"] == 1
    assert await count_rows(pg_stack) == 1


# ---------------------------------------------------------------------------
# T34v — a failed session is never reused; each attempt gets a clean one
# ---------------------------------------------------------------------------


async def test_postgres_aborted_session_is_not_reused(pg_stack):
    from sqlalchemy.exc import PendingRollbackError

    # A genuine server error aborts the transaction behind this session.
    async with pg_stack.factory() as session:
        with pytest.raises(DBAPIError) as raised:
            await session.execute(text("SELECT 1 / 0"))
        assert events._sqlstate(raised.value) == "22012"  # division_by_zero
        assert events._is_transient_lock(raised.value) is False

        # A second statement on the dead transaction is either answered by the
        # server with 25P02 or refused client-side; both mean the transaction is
        # unusable, which is why the writer opens a session per attempt.
        try:
            await session.execute(text("SELECT 1"))
        except DBAPIError as exc:
            assert events._sqlstate(exc) == "25P02"
            assert events._is_transient_lock(exc) is False
        except PendingRollbackError:
            pass
        else:  # pragma: no cover - a server that silently accepts this would be broken
            pytest.fail("an aborted transaction accepted a further statement")
        await session.rollback()

    # The next attempt starts from a clean transaction, as the writer's
    # per-attempt session does, and sees no 25P02.
    async with pg_stack.factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1

    assert await emit_event(pg_stack) is True
    assert events.counters()["retries"] == 0
    assert await count_rows(pg_stack) == 1


# ---------------------------------------------------------------------------
# T34w — a real 25P02 at the flush keeps every queued event
# ---------------------------------------------------------------------------


async def test_postgres_audit_flush_keeps_queue_after_in_failed_transaction(
    pg_stack, migrated
):
    AuditTrail.instance().log(
        actor_id="pipeline-service",
        action="tool:fs_write",
        resource="x",
        allowed=True,
        user_id=pg_stack.user_id,
        task_id=pg_stack.task_id,
        run_id=pg_stack.run_id,
        step_node_id="step-1",
        action_id=str(uuid.uuid4()),
    )
    AuditTrail.instance().log(
        actor_id="legacy-actor", action="api:view_audit", resource="audit", allowed=True
    )
    assert AuditTrail.instance().pending_count() == 2

    _cause, followup = await _capture_in_failed_transaction(migrated)
    async with _FailedFirstSession(pg_stack.factory(), followup) as failing:
        with pytest.raises(DBAPIError) as raised:
            await AuditTrail.instance().flush_pending(failing)
        assert events._sqlstate(raised.value) == "25P02"

    # Nothing acknowledged, nothing persisted: both events stay queued.
    assert AuditTrail.instance().pending_count() == 2
    assert await count_rows(pg_stack, "audit_events") == 0
    assert await count_rows(pg_stack, "audit_links") == 0

    # A clean session writes each event once, with its link.
    async with pg_stack.factory() as session:
        assert await AuditTrail.instance().flush_pending(session) == 2
    assert AuditTrail.instance().pending_count() == 0
    assert await count_rows(pg_stack, "audit_events") == 2
    assert await count_rows(pg_stack, "audit_links") == 1

    async with pg_stack.factory() as session:
        assert await AuditTrail.instance().flush_pending(session) == 0
    assert await count_rows(pg_stack, "audit_events") == 2
    assert await count_rows(pg_stack, "audit_links") == 1


# ---------------------------------------------------------------------------
# A real 40001 → 25P02 chain: the cause is retryable, the effect is not
# ---------------------------------------------------------------------------


async def _capture_serialization_then_abort(url: str) -> tuple[DBAPIError, DBAPIError]:
    """Produce a real ``40001`` and the ``25P02`` that follows it in one transaction.

    Two SERIALIZABLE transactions read the same row; the first to write commits,
    the second is refused by PostgreSQL with ``40001`` ("could not serialize
    access due to concurrent update"). A further statement in that same, now
    aborted, transaction is answered with ``25P02``.

    AUTOCOMMIT isolation plus explicit ``BEGIN`` means each statement reaches the
    server as written, so both errors are the driver's own objects and neither
    SQLSTATE is synthesised here.
    """
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    cause: DBAPIError | None = None
    followup: DBAPIError | None = None
    try:
        async with engine.begin() as setup:
            await setup.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS serial_chain "
                    "(id integer PRIMARY KEY, v integer)"
                )
            )
            await setup.execute(text("DELETE FROM serial_chain"))
            await setup.execute(text("INSERT INTO serial_chain (id, v) VALUES (1, 0)"))

        async with engine.connect() as conn_a, engine.connect() as conn_b:
            await conn_a.execute(text("BEGIN ISOLATION LEVEL SERIALIZABLE"))
            await conn_b.execute(text("BEGIN ISOLATION LEVEL SERIALIZABLE"))
            # Both transactions take the same snapshot before either writes.
            await conn_a.execute(text("SELECT v FROM serial_chain WHERE id = 1"))
            await conn_b.execute(text("SELECT v FROM serial_chain WHERE id = 1"))
            await conn_a.execute(text("UPDATE serial_chain SET v = v + 1 WHERE id = 1"))
            await conn_a.execute(text("COMMIT"))
            try:
                await conn_b.execute(
                    text("UPDATE serial_chain SET v = v + 1 WHERE id = 1")
                )
            except DBAPIError as exc:
                cause = exc
            try:
                await conn_b.execute(text("SELECT 1"))
            except DBAPIError as exc:
                followup = exc
            try:
                await conn_b.execute(text("ROLLBACK"))
            except DBAPIError:  # pragma: no cover - the transaction is already dead
                pass
    finally:
        await engine.dispose()

    assert cause is not None, "PostgreSQL did not report the serialization failure"
    assert followup is not None, "the aborted transaction accepted a further statement"
    return cause, followup


# ---------------------------------------------------------------------------
# T34x — the chain is classified from its cause, not its abort effect
# ---------------------------------------------------------------------------


async def test_postgres_serialization_chain_keeps_the_cause_retryable(migrated):
    cause, followup = await _capture_serialization_then_abort(migrated)

    assert events._sqlstate(cause) == "40001", (
        f"driver did not expose the serialization SQLSTATE: {cause!r}"
    )
    assert events._is_transient_lock(cause) is True

    assert events._sqlstate(followup) == "25P02", (
        f"driver did not expose the aborted-transaction SQLSTATE: {followup!r}"
    )
    assert events._is_transient_lock(followup) is False
    assert events._is_missing_table(followup) is False


# ---------------------------------------------------------------------------
# T34y — the chain's cause is retried in a fresh transaction, exactly once
# ---------------------------------------------------------------------------


async def test_postgres_writer_retries_chain_cause_in_a_fresh_transaction(
    pg_stack, migrated
):
    cause, followup = await _capture_serialization_then_abort(migrated)
    factory = _failing_factory(pg_stack.factory, [cause])
    action_id = live_action()

    assert await emit_event(pg_stack, action_id=action_id, session_factory=factory) is True
    assert events.counters()["retries"] == 1, "one retry, so the second attempt was a new transaction"
    assert events.counters()["written"] == 1
    assert events.counters()["failures"] == 0

    rows = await fetch_rows(pg_stack)
    assert len(rows) == 1
    assert rows[0]["action_id"] == action_id

    # The abort effect is never what gets retried, and it stays non-retryable.
    assert events._is_transient_lock(followup) is False

    # Idempotency survives the retry: the same fact is still one row.
    assert await emit_event(pg_stack, action_id=action_id) is True
    assert events.counters()["duplicates"] == 1
    assert await count_rows(pg_stack) == 1
