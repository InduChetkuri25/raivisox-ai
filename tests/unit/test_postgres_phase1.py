"""V3 Phase 1 acceptance tests — the PostgreSQL path (T34b–T34h).

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

import datetime
import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
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
