"""V3 Phase 1 acceptance tests — migration 003 (T32–T34).

Runs Alembic in a subprocess against a throwaway SQLite file, exactly like the
existing zero-drift test. PostgreSQL is exercised only when a test URL is
provided; otherwise the test skips and the limitation is reported.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_API = os.path.join(_REPO, "apps", "api")

_NEW_TABLES = {"graph_events", "audit_links"}


def _env(db_path: str) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([_REPO, _API])
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    return env


def _alembic(db_path: str, *args: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_API,
        env=_env(db_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} failed\n"
        + (proc.stdout or "")[-2000:]
        + (proc.stderr or "")[-2000:]
    )


def _tables(db_path: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()


def _ddl(db_path: str, table: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row[0] if row else ""
    finally:
        con.close()


def _insert_audit_row(db_path: str) -> dict:
    values = {
        "id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "actor_id": "legacy-actor",
        "action": "api:view_audit",
        "resource": "audit",
        "allowed": 0,
        "request_id": str(uuid.uuid4()),
        "details": '{"reason": "legacy"}',
        "timestamp": 1700000000.5,
        "created_at": "2026-01-01 00:00:00.000000",
        "updated_at": "2026-01-01 00:00:00.000000",
    }
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO audit_events (id, event_id, actor_id, action, resource, "
            "allowed, request_id, details, timestamp, created_at, updated_at) "
            "VALUES (:id, :event_id, :actor_id, :action, :resource, :allowed, "
            ":request_id, :details, :timestamp, :created_at, :updated_at)",
            values,
        )
        con.commit()
    finally:
        con.close()
    return values


def _fetch_audit_row(db_path: str, event_id: str) -> dict:
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM audit_events WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# T32 — reversible, non-destructive round trip
# ---------------------------------------------------------------------------


def test_upgrade_downgrade_round_trip_sqlite(tmp_path):
    db = str(tmp_path / "roundtrip.db")

    _alembic(db, "upgrade", "head")
    after_upgrade = _tables(db)
    assert _NEW_TABLES <= after_upgrade
    audit_ddl_after_upgrade = _ddl(db, "audit_events")
    graph_ddl = _ddl(db, "graph_events")
    assert "AUTOINCREMENT" in graph_ddl.upper(), (
        "seq must never be reused after a delete (SQLite AUTOINCREMENT)"
    )

    _alembic(db, "downgrade", "002")
    after_downgrade = _tables(db)
    assert not (_NEW_TABLES & after_downgrade)
    assert "audit_events" in after_downgrade
    assert _ddl(db, "audit_events") == audit_ddl_after_upgrade

    _alembic(db, "upgrade", "head")
    assert _NEW_TABLES <= _tables(db)
    assert _ddl(db, "audit_events") == audit_ddl_after_upgrade


# ---------------------------------------------------------------------------
# T33 — existing forensic rows survive verbatim
# ---------------------------------------------------------------------------


def test_existing_audit_rows_survive_upgrade(tmp_path):
    db = str(tmp_path / "legacy.db")

    _alembic(db, "upgrade", "002")
    assert not (_NEW_TABLES & _tables(db))
    inserted = _insert_audit_row(db)
    ddl_before = _ddl(db, "audit_events")

    _alembic(db, "upgrade", "head")

    assert _ddl(db, "audit_events") == ddl_before, "upgrade must not ALTER audit_events"
    row = _fetch_audit_row(db, inserted["event_id"])
    assert row, "the legacy row must still exist"
    for key, value in inserted.items():
        assert row[key] == value, f"{key} changed during upgrade"

    con = sqlite3.connect(db)
    try:
        link_count = con.execute("SELECT COUNT(*) FROM audit_links").fetchone()[0]
    finally:
        con.close()
    assert link_count == 0, "legacy rows must not be backfilled with invented identity"


# ---------------------------------------------------------------------------
# T34 — PostgreSQL (only when a URL is configured)
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get("RAIVISOX_TEST_POSTGRES_URL", "").strip()


@pytest.mark.skipif(
    not _PG_URL, reason="RAIVISOX_TEST_POSTGRES_URL not set — PostgreSQL is unverified"
)
def test_postgres_migration_and_events():
    from sqlalchemy import create_engine, inspect

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([_REPO, _API])
    env["DATABASE_URL"] = _PG_URL
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_API,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]

    sync_url = _PG_URL.replace("+asyncpg", "+psycopg2").replace(
        "+aiosqlite", ""
    )
    engine = create_engine(sync_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert _NEW_TABLES <= tables
        audit_cols = {c["name"] for c in inspect(engine).get_columns("audit_events")}
        assert not ({"user_id", "task_id", "run_id"} & audit_cols), (
            "audit_events must stay unchanged on PostgreSQL too"
        )
    finally:
        engine.dispose()
