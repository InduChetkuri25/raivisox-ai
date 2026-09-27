"""Database connection and session management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


# Build engine kwargs — connection pooling options differ per backend.
# SQLite (aiosqlite) does not support pool_size/max_overflow; PostgreSQL does.
engine_kwargs: dict = {
    "echo": settings.debug,
    "pool_pre_ping": True,
}
if settings.database_url_async.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

# Create async engine
engine = create_async_engine(
    settings.database_url_async,
    **engine_kwargs,
)

# Dialect flag for the few places that must hand-write SQL: PostgreSQL is
# strict about boolean literals (is_current = TRUE), SQLite wants 1.
_is_postgres = not settings.database_url_async.startswith("sqlite")

# Create session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides a database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Guard for pre-existing databases: create_all does not ALTER
        # tables that already exist, so columns added after the initial
        # release need an explicit additive migration (Honest-Outcome
        # Guarantees: tasks.outcome).
        def _ensure_outcome_column(sync_conn):
            from sqlalchemy import inspect, text

            insp = inspect(sync_conn)
            if "tasks" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("tasks")}
                if "outcome" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE tasks ADD COLUMN outcome VARCHAR(40)")
                    )

        await conn.run_sync(_ensure_outcome_column)

        # Execution Runs: task_events.execution_run_id was added after the
        # initial release; create_all won't ALTER an existing table.
        def _ensure_task_event_run_column(sync_conn):
            from sqlalchemy import inspect, text

            insp = inspect(sync_conn)
            if "task_events" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("task_events")}
                if "execution_run_id" not in cols:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE task_events "
                            "ADD COLUMN execution_run_id VARCHAR(36)"
                        )
                    )

        await conn.run_sync(_ensure_task_event_run_column)


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
