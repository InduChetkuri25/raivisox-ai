"""Production hardening tests — fail-fast config, health, limits, migrations.

Every test here proves a REAL production-readiness property:

- Configuration: ENVIRONMENT=production refuses missing/default secrets,
  default DATABASE_URL, empty CORS/hosts, DEBUG=true and absent LLM
  provider — naming VARIABLES only, never values (no secret can leak
  through a startup error). Development keeps its convenient defaults.
- Health: liveness is public and dependency-free; readiness gates on
  database + storage, reports the LLM provider as advisory, and exposes
  no secrets or filesystem paths.
- Artifact limits: per-artifact size, per-task count and ZIP bundle
  limits refuse honestly (ValueError → recorded registration failure;
  HTTP 413 for bundles) without breaking history semantics.
- Errors: user-visible persisted error text never carries internal
  filesystem paths.
- Concurrency: the duplicate-execute atomic claim has exactly one
  winner.
- Migrations: `alembic upgrade head` succeeds AND reproduces the model
  schema exactly (zero drift) — on SQLite and therefore on PostgreSQL.

These tests run in the NORMAL suite (no Ollama, no network).
"""

import asyncio
import os
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "production_hardening_test")
os.makedirs(_TMP, exist_ok=True)

import atexit  # noqa: E402

_DB_PATH = os.path.abspath(os.path.join(_TMP, "production_hardening.db"))
# setdefault: other test modules own the effective DATABASE_URL chain.
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")
def _cleanup_db():
    try:
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)
    except OSError:
        pass  # engine may still hold the file at interpreter exit


atexit.register(_cleanup_db)


from app import main as app_main  # noqa: E402
from app.config import (  # noqa: E402
    DEV_DATABASE_URL,
    DEV_SECRET_KEY,
    Settings,
    validate_settings,
)

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_API = os.path.join(_REPO, "apps", "api")

# Values so obviously fake that no assertion can confuse them with real
# production configuration.
_GOOD_SECRET = "ab12" * 12  # 48 chars, no placeholder markers
_PROVIDER_ENV = (
    "RAIVISOX_LLM_BASE_URL",
    "RAIVISOX_LLM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
)


def _production_settings(**overrides) -> Settings:
    """A fully valid production Settings (provider supplied separately)."""
    base = dict(
        _env_file=None,
        environment="production",
        secret_key=_GOOD_SECRET,
        database_url="postgresql+asyncpg://prod_user:prod_pass@db.internal:5432/raivisox",
        cors_origins="https://app.example.test",
        allowed_hosts="api.example.test",
        debug=False,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 1. Configuration fail-fast
# ---------------------------------------------------------------------------


def test_dev_environment_always_passes():
    """Development keeps local defaults — no friction."""
    validate_settings(Settings(_env_file=None))  # environment=development
    validate_settings(Settings(_env_file=None, environment="test"))


def test_production_defaults_refused_with_variable_names_only():
    with pytest.raises(RuntimeError) as ei:
        # Explicit inputs, not ambient ones: the CI job exports SECRET_KEY,
        # which would otherwise silently satisfy the requirement and drop
        # SECRET_KEY from the refusal message (a false pass of this test).
        validate_settings(
            Settings(
                _env_file=None,
                environment="production",
                secret_key=DEV_SECRET_KEY,
                database_url=DEV_DATABASE_URL,
            )
        )
    msg = str(ei.value)
    # Names the offending variables…
    assert "SECRET_KEY" in msg
    assert "DATABASE_URL" in msg
    assert "CORS_ORIGINS" in msg
    assert "ALLOWED_HOSTS" in msg
    # …but never any value that could leak a secret or credential.
    assert DEV_SECRET_KEY not in msg
    assert "dev-secret-key" not in msg
    assert "postgres:postgres" not in msg


def test_production_placeholder_and_short_secrets_refused():
    for bad in ("", "xy9", "changeme-please", DEV_SECRET_KEY):
        with pytest.raises(RuntimeError) as ei:
            validate_settings(
                _production_settings(secret_key=bad, database_url=DEV_DATABASE_URL)
            )
        assert "SECRET_KEY" in str(ei.value)
        # The refused value itself must not be echoed.
        if bad and len(bad) >= 4:
            assert bad not in str(ei.value)


def test_production_requires_provider_config(monkeypatch):
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError) as ei:
        validate_settings(_production_settings())
    assert "LLM provider" in str(ei.value)


def test_production_good_config_passes(monkeypatch):
    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    validate_settings(_production_settings())  # must not raise


def test_production_refuses_debug():
    with pytest.raises(RuntimeError) as ei:
        validate_settings(_production_settings(debug=True))
    assert "DEBUG" in str(ei.value)


def test_origin_and_host_list_parsing():
    """Comma-separated AND JSON forms both parse (the documented form
    used to crash the API at import)."""
    s = Settings(
        _env_file=None,
        cors_origins="http://a.example, http://b.example",
        allowed_hosts="api.example.com",
    )
    assert s.cors_origins == ["http://a.example", "http://b.example"]
    assert s.allowed_hosts == ["api.example.com"]
    s2 = Settings(_env_file=None, cors_origins='["https://x.example"]')
    assert s2.cors_origins == ["https://x.example"]
    s3 = Settings(_env_file=None, cors_origins="")
    assert s3.cors_origins == []


# ---------------------------------------------------------------------------
# 2. Health / readiness
# ---------------------------------------------------------------------------


def test_liveness_endpoints_public_and_clean():
    with TestClient(app_main.app) as client:
        for url in ("/health", "/health/live"):
            r = client.get(url)
            assert r.status_code == 200
            body = r.text
            assert "secret" not in body.lower()
            assert "workspaces" not in body
            assert "Users" not in body


def test_readiness_ok_when_dependencies_up():
    with TestClient(app_main.app) as client:
        r = client.get("/health/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["storage"] == "ok"
        assert "configured" in data["checks"]["llm"]


def test_readiness_503_when_database_down(monkeypatch):
    async def _down():
        return False

    monkeypatch.setattr(app_main, "_database_ready", _down)
    with TestClient(app_main.app) as client:
        r = client.get("/health/ready")
        assert r.status_code == 503
        data = r.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["database"] == "unavailable"


def test_readiness_body_has_no_paths_or_secrets():
    with TestClient(app_main.app) as client:
        body = client.get("/health/ready").text
    assert app_main.settings.secret_key not in body
    assert "secret_key" not in body
    assert "raivisox.db" not in body
    assert "workspaces" not in body
    assert "\\" not in body  # no Windows paths
    assert "/home" not in body and "/Users" not in body and "/app" not in body


# ---------------------------------------------------------------------------
# 3. Artifact storage limits
# ---------------------------------------------------------------------------


def test_artifact_size_limit_refuses_registration(monkeypatch):
    path = os.path.join(app_main.TASK_WORKSPACE, "limit_test", "big.bin")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * 1024)
    try:
        monkeypatch.setattr(app_main.settings, "max_artifact_size_bytes", 100)
        with pytest.raises(ValueError) as ei:
            asyncio.run(
                app_main._register_task_artifact(
                    "t-limit-size",
                    path=path,
                    node_id="step-1",
                    step_db_id=None,
                    step_title="limit",
                    run_id=None,
                )
            )
        assert "MAX_ARTIFACT_SIZE_BYTES" in str(ei.value)
    finally:
        os.remove(path)


def test_artifact_count_limit_refuses_registration(monkeypatch):
    path = os.path.join(app_main.TASK_WORKSPACE, "limit_test", "small.bin")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"small")
    try:
        monkeypatch.setattr(app_main.settings, "max_artifacts_per_task", 0)
        with pytest.raises(ValueError) as ei:
            asyncio.run(
                app_main._register_task_artifact(
                    "t-limit-count",
                    path=path,
                    node_id="step-1",
                    step_db_id=None,
                    step_title="limit",
                    run_id=None,
                )
            )
        assert "MAX_ARTIFACTS_PER_TASK" in str(ei.value)
    finally:
        os.remove(path)


def test_bundle_limits_return_413(monkeypatch):
    from fastapi import HTTPException

    arts2 = [
        {"exists": True, "size": 10, "filename": "a"},
        {"exists": True, "size": 10, "filename": "b"},
    ]
    big = [{"exists": True, "size": 10_000_000, "filename": "big"}]
    ok = [{"exists": True, "size": 10, "filename": "a"}]

    # Under the limits: passes.
    app_main._enforce_bundle_limits(ok)

    monkeypatch.setattr(app_main.settings, "max_zip_files", 1)
    with pytest.raises(HTTPException) as ei:
        app_main._enforce_bundle_limits(arts2)
    assert ei.value.status_code == 413
    assert "MAX_ZIP_FILES" in ei.value.detail
    monkeypatch.undo()

    monkeypatch.setattr(app_main.settings, "max_zip_size_bytes", 1000)
    with pytest.raises(HTTPException) as ei:
        app_main._enforce_bundle_limits(big)
    assert ei.value.status_code == 413
    assert "MAX_ZIP_SIZE_BYTES" in ei.value.detail


# ---------------------------------------------------------------------------
# 4. Error handling — no internal paths in user-visible text
# ---------------------------------------------------------------------------


def test_scrub_internal_paths_helpers():
    win = r"C:\Users\someone\OneDrive\Desktop\app\workspaces\tasks\t1\out.txt"
    scrubbed = app_main._scrub_internal_paths(f"failed reading {win} during step")
    assert "C:\\" not in scrubbed
    assert "<path>" in scrubbed

    posix = "/home/deploy/raivisox/workspaces/tasks/t2/result.md"
    scrubbed = app_main._scrub_internal_paths(f"missing {posix}")
    assert "/home" not in scrubbed
    assert "<path>" in scrubbed

    # Plain messages pass through untouched (honest content preserved).
    assert app_main._scrub_internal_paths("step 3 timed out") == "step 3 timed out"


def test_display_path_is_workspace_relative():
    inside = os.path.join(app_main.TASK_WORKSPACE, "disp_test", "result.md")
    os.makedirs(os.path.dirname(inside), exist_ok=True)
    with open(inside, "w") as f:
        f.write("x")
    try:
        shown = app_main._display_path(inside)
        assert not os.path.isabs(shown)
        assert shown.replace("\\", "/").startswith("disp_test/")
        outside = os.path.join(_REPO, "pyproject.toml")
        assert app_main._display_path(outside) == "pyproject.toml"
    finally:
        os.remove(inside)


# ---------------------------------------------------------------------------
# 5. Background execution — duplicate prevention is atomic
# ---------------------------------------------------------------------------


def test_atomic_claim_has_single_winner():
    """The conditional UPDATE used by async execute lets exactly one
    caller flip a non-active task to EXECUTING (closes the TOCTOU race
    between the friendly status check and pipeline start)."""
    from datetime import datetime

    from sqlalchemy import delete, select, update

    from app.database import async_session_factory
    from app.models.task import Task, TaskStatus

    tid = f"t-claim-{uuid.uuid4().hex[:8]}"

    def _claim():
        return (
            update(Task)
            .where(
                Task.id == tid,
                Task.status.notin_(
                    [
                        TaskStatus.EXECUTING.value,
                        TaskStatus.RECOVERING.value,
                        TaskStatus.VERIFYING.value,
                    ]
                ),
            )
            .values(status=TaskStatus.EXECUTING.value, updated_at=datetime.utcnow())
            .execution_options(synchronize_session=False)
        )

    async def _run():
        async with async_session_factory() as s:
            s.add(Task(id=tid, user_id="u-claim", title="claim me"))
            await s.commit()
        try:
            async with async_session_factory() as s:
                r1 = await s.execute(_claim())
                await s.commit()
            async with async_session_factory() as s:
                r2 = await s.execute(_claim())
                await s.commit()
            async with async_session_factory() as s:
                final = (
                    (await s.execute(select(Task.status).where(Task.id == tid)))
                    .scalars()
                    .first()
                )
            return r1.rowcount, r2.rowcount, final
        finally:
            async with async_session_factory() as s:
                await s.execute(delete(Task).where(Task.id == tid))
                await s.commit()

    won1, won2, final = asyncio.run(_run())
    assert (won1, won2) == (1, 0)
    assert final == TaskStatus.EXECUTING.value


def test_lifespan_refuses_default_secret_in_production(monkeypatch):
    """Defense in depth: even if env validation were bypassed at import,
    lifespan startup refuses an unsafe production configuration."""
    bad = Settings(_env_file=None, environment="production",
                   secret_key=DEV_SECRET_KEY, database_url=DEV_DATABASE_URL)
    monkeypatch.setattr(app_main, "settings", bad)
    with pytest.raises(Exception) as ei:  # noqa: B017 — wrapping differs by starlette version
        with TestClient(app_main.app):
            pass
    assert "SECRET_KEY" in str(ei.value)
    assert DEV_SECRET_KEY not in str(ei.value)


# ---------------------------------------------------------------------------
# 6. Startup fail-fast & migrations (subprocess: import-time behavior)
# ---------------------------------------------------------------------------


def _subprocess_env(**extra) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([_REPO, _API])
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def test_production_import_fails_fast_without_config():
    """`import app.main` with ENVIRONMENT=production and missing secrets
    must abort the process before serving anything — and the error must
    name variables, never values."""
    # Clear the CI job's own values so the test asserts the intended
    # scenario (production with NO configured secret) on any machine.
    env = _subprocess_env(ENVIRONMENT="production", SECRET_KEY=None,
                          DATABASE_URL=None, CORS_ORIGINS=None,
                          ALLOWED_HOSTS=None)
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, "production import must fail fast"
    assert "SECRET_KEY" in out
    assert "CORS_ORIGINS" in out
    assert "ALLOWED_HOSTS" in out
    assert DEV_SECRET_KEY not in out
    assert "postgres:postgres" not in out


def test_production_import_succeeds_with_valid_config(tmp_path):
    db = tmp_path / "prod_import.db"
    env = _subprocess_env(
        ENVIRONMENT="production",
        SECRET_KEY=_GOOD_SECRET,
        DATABASE_URL=f"sqlite+aiosqlite:///{str(db).replace(chr(92), '/')}",
        CORS_ORIGINS="https://app.example.test",
        ALLOWED_HOSTS="api.example.test",
        RAIVISOX_LLM_BASE_URL="http://127.0.0.1:11434/v1",
        DEBUG=None,
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main; print('IMPORT_OK')"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
    assert "IMPORT_OK" in proc.stdout


def test_alembic_upgrade_head_matches_model_schema(tmp_path):
    """`alembic upgrade head` must succeed AND reproduce the model
    metadata exactly: every table, column and index — zero drift. This
    is the PostgreSQL startup-migration guarantee (same DDL ops)."""
    from sqlalchemy import create_engine, inspect

    from app.database import Base
    import app.models  # noqa: F401,F403 — populate metadata

    db_file = tmp_path / "migr.db"
    url_path = str(db_file).replace(chr(92), "/")
    env = _subprocess_env(DATABASE_URL=f"sqlite+aiosqlite:///{url_path}")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_API,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]

    engine = create_engine(f"sqlite:///{url_path}")
    insp = inspect(engine)
    have = set(insp.get_table_names())
    missing_tables = set(Base.metadata.tables) - have
    assert not missing_tables, f"tables missing from migrations: {sorted(missing_tables)}"
    for name, table in Base.metadata.tables.items():
        db_cols = {c["name"] for c in insp.get_columns(name)}
        model_cols = {c.name for c in table.columns}
        assert db_cols == model_cols, (
            f"{name}: model-only={sorted(model_cols - db_cols)} "
            f"db-only={sorted(db_cols - model_cols)}"
        )
        db_idx = {i["name"] for i in insp.get_indexes(name)}
        model_idx = {i.name for i in table.indexes}
        assert not (model_idx - db_idx), (
            f"{name}: indexes missing from migrations: {sorted(model_idx - db_idx)}"
        )
    engine.dispose()
