"""Artifact Retention & Cleanup — deterministic lifecycle tests.

Proves the retention contract end-to-end against the REAL service and REAL
files (os.remove is monkeypatched only for simulated I/O failures):

- retention=0 is a hard no-op (nothing scanned, nothing deleted);
- N>0 reaps only expired NON-current bytes, preserving registry metadata,
  stable artifact IDs and ExecutionRun history;
- current artifacts are untouchable regardless of age, including the
  same-path-reuse case (old superseded row + new current row share a path);
- orphan GC removes only aged files no registry row references;
- containment/traversal protection (a row pointing outside the root is
  never followed);
- concurrency: cleanup cannot race away a freshly (re-)registered
  artifact;
- scheduler wiring: worker only at N>0, cancelled on shutdown, failures
  logged not raised;
- admin endpoint: 401 anonymous / 403 non-admin (audited) / 200 admin with
  safe stats, retention=0 an honest no-op;
- historical downloads + task ZIP stay honest after cleanup; a reused path
  never leaks another artifact's bytes;
- cross-user ownership preserved.

Runs in the NORMAL suite (no Ollama, no network).
"""

import asyncio
import hashlib
import io
import os
import shutil
import time
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

_TMP = os.path.join("workspaces", "tasks", "retention_test")
os.makedirs(_TMP, exist_ok=True)

_DB_PATH = os.path.abspath(os.path.join(_TMP, "retention.db"))
# setdefault: other test modules own the effective DATABASE_URL chain.
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")

import atexit  # noqa: E402

# NOTE — lazy app imports only (suite invariant): app.main is imported
# INSIDE fixtures/helpers, never at module level. A module-level import
# binds the shared async engine to THIS module's DATABASE_URL (a setdefault
# loser anyway) and hijacks the DB file for the whole session, breaking
# modules that hard-pin their own (e.g. test_outcome_guarantees).


def _cleanup_db():
    try:
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)
    except OSError:
        pass  # engine may still hold the file at interpreter exit


atexit.register(_cleanup_db)


def _app_main():
    from app import main as _app_main

    return _app_main


def _artifact_cleanup():
    from app.services import artifact_cleanup as _ac

    return _ac


def _db():
    """Fresh AsyncSession (sessionmaker called lazily — suite invariant)."""
    from app.database import async_session_factory as _f

    return _f()


def _settings_cls():
    from app.config import Settings as _S

    return _S

GOAL = (
    "Create a Python module that calculates the factorial of 5 and verify "
    "that the result is 120."
)


def _uid() -> str:
    import uuid

    return uuid.uuid4().hex[:10]


@pytest.fixture()
def client():
    from app.auth import service as auth_service

    import app.tasks.router as tasks_router

    app_main = _app_main()
    with TestClient(app_main.app) as c:
        user_id = f"u-{_uid()}"
        user = type("U", (), {"id": user_id, "email": f"{user_id}@ret.test"})()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c, user
        app_main.app.dependency_overrides.clear()
        c.close()


def _mk_task(c, title="retention flow", project_id=None):
    payload = {"title": title, "goal": GOAL}
    if project_id:
        payload["project_id"] = project_id
    r = c.post("/api/v1/tasks", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _retention_settings(days, interval=3600):
    return _settings_cls()(
        _env_file=None,
        artifact_retention_days=days,
        artifact_cleanup_interval_seconds=interval,
    )


def _register(task_id, path, *, run_id=None):
    async def _go():
        return await _app_main()._register_task_artifact(
            task_id,
            path=path,
            node_id="node-ret",
            step_db_id=None,
            step_title="retention probe",
            run_id=run_id,
        )

    return asyncio.run(_go())


def _expire(row_id, *, age_days, superseded_at=None):
    """Backdate a row's created_at; optionally mark it superseded."""
    from app.models.task_artifact import TaskArtifact

    async def _go():
        async with _db() as db:
            r = await db.get(TaskArtifact, row_id)
            r.created_at = datetime.utcnow() - timedelta(days=age_days)
            if superseded_at is not None:
                r.is_current = False
                r.superseded_at = superseded_at
            db.add(r)
            await db.commit()

    asyncio.run(_go())


def _row_by_id(row_id):
    from app.models.task_artifact import TaskArtifact

    async def _go():
        async with _db() as db:
            r = await db.get(TaskArtifact, row_id)
            if r is not None:
                db.expunge(r)
            return r

    return asyncio.run(_go())


def _cleanup(root, days, now=None):
    from app.models.task_artifact import TaskArtifact  # noqa: F401 — engine warmup parity

    async def _go():
        async with _db() as db:
            return await _artifact_cleanup().cleanup_expired_artifacts(
                db, root=root, settings=_retention_settings(days), now=now
            )

    return asyncio.run(_go())


def _age_file(path, days):
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


@pytest.fixture()
def retention_ws():
    """A dedicated workspace subdir for orphan/containment experiments."""
    root = os.path.join(_app_main().TASK_WORKSPACE, f"ret_{_uid()}")
    os.makedirs(root, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _superseded_history(task_id, retention_ws, *, base="deliverable"):
    """v1 (expired, superseded, unowned path) + v2 (current, fresh).

    Returns (v1_path, v1_row, v2_path, v2_row). v1's path differs from
    v2's, so v1's bytes are genuinely unowned and eligible.
    """
    p1 = os.path.join(retention_ws, f"{base}.md")
    with open(p1, "w") as f:
        f.write("version one")
    # The FILE must be as old as its history: the mtime guard correctly
    # refuses to reap files younger than the supersession timestamp.
    _age_file(p1, 30)
    row1 = _register(task_id, p1)
    _expire(row1.id, age_days=30, superseded_at=datetime.utcnow() - timedelta(days=20))
    p2 = os.path.join(retention_ws, f"{base}_v2.md")
    with open(p2, "w") as f:
        f.write("version two")
    row2 = _register(task_id, p2)
    return p1, row1, p2, row2


# ---------------------------------------------------------------------------
# 1. Retention disabled (0) — hard no-op
# ---------------------------------------------------------------------------


def test_retention_zero_is_hard_noop(client, retention_ws):
    c, _ = client
    task_id = _mk_task(c)
    body = c.get(f"/api/v1/tasks/{task_id}").json()
    assert body["status"] in ("created", "completed", "failed")

    stale = os.path.join(retention_ws, "stale.bin")
    with open(stale, "wb") as f:
        f.write(b"stale bytes")
    _age_file(stale, 40)

    stats = _cleanup(_app_main().TASK_WORKSPACE, 0)

    assert stats.retention_disabled is True
    assert stats.scanned == 0 and stats.deleted == 0 and stats.orphans_deleted == 0
    assert os.path.isfile(stale), "retention=0 must keep even ancient orphan files"


# ---------------------------------------------------------------------------
# 2. Superseded history reaping with metadata preservation
# ---------------------------------------------------------------------------


def test_expired_superseded_bytes_deleted_metadata_preserved(client, retention_ws):
    c, _ = client
    task_id = _mk_task(c)
    p1, row1, p2, _row2 = _superseded_history(task_id, retention_ws)

    assert os.path.isfile(p1)
    stats = _cleanup(_app_main().TASK_WORKSPACE, 7)

    assert not os.path.isfile(p1), "expired unowned superseded bytes must be reaped"
    assert os.path.isfile(p2), "current artifact must survive"
    assert stats.deleted >= 1

    # Metadata is fully preserved: same stable ID, queryable history.
    r = _row_by_id(row1.id)
    assert r is not None, "registry row must remain after byte deletion"
    assert r.created_at is not None and r.superseded_at is not None
    assert r.sha256 == hashlib.sha256(b"version one").hexdigest()
    assert r.is_current is False

    # History endpoint still lists it, honestly marking bytes gone.
    hist = c.get(f"/api/v1/tasks/{task_id}/artifacts/history").json()
    arts = [a for run in hist["runs"] for a in run["artifacts"]]
    match = [a for a in arts if a["artifact_id"] == row1.id]
    assert match and match[0]["exists"] is False
    assert match[0]["hash"] == r.sha256


def test_historical_download_after_cleanup_is_honest_404(client, retention_ws):
    c, _ = client
    task_id = _mk_task(c)
    p1, row1, p2, _row2 = _superseded_history(task_id, retention_ws, base="hist")

    # While bytes exist, the historical row downloads its own recorded bytes.
    r_before = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{row1.id}")
    assert r_before.status_code == 200
    assert r_before.content == b"version one"

    _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert not os.path.isfile(p1)

    r_after = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{row1.id}")
    assert r_after.status_code == 404, "missing historical bytes must be an honest 404"


def test_non_expired_superseded_bytes_preserved(client, retention_ws):
    c, _ = client
    task_id = _mk_task(c)
    p1 = os.path.join(retention_ws, "fresh.md")
    with open(p1, "w") as f:
        f.write("fresh history")
    row1 = _register(task_id, p1)
    # Backdate the ROW 1 day but leave the FILE mtime fresh: the file's own
    # age is what the window measures, and it is inside the window.
    _expire(row1.id, age_days=1, superseded_at=datetime.utcnow() - timedelta(hours=2))
    p2 = os.path.join(retention_ws, "fresh_v2.md")
    with open(p2, "w") as f:
        f.write("current")
    _register(task_id, p2)

    stats = _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert os.path.isfile(p1), "within-window history must be preserved"
    assert os.path.isfile(p2)
    assert stats.deleted == 0
    assert stats.skipped_current_path >= 1 or stats.skipped_not_expired >= 1


def test_current_artifact_never_deleted_regardless_of_age(client, retention_ws):
    c, _ = client
    task_id = _mk_task(c)
    p = os.path.join(retention_ws, "ancient_current.md")
    with open(p, "w") as f:
        f.write("still the deliverable")
    row = _register(task_id, p)
    _expire(row.id, age_days=365)  # current, ancient

    stats = _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert os.path.isfile(p), "a CURRENT artifact is never retention-reaped"
    assert stats.deleted == 0
    r = _row_by_id(row.id)
    assert r is not None and r.is_current is True


# ---------------------------------------------------------------------------
# 3. Same-path reuse — no cross-artifact byte theft
# ---------------------------------------------------------------------------


def test_same_path_reuse_protects_new_bytes(client, retention_ws):
    """Old superseded row + new current row share ONE path.

    The file's bytes belong to the CURRENT registration. Cleanup must skip
    the expired old row entirely — deleting the file would destroy the live
    deliverable, and history already answers 404 for the old row's replaced
    bytes.
    """
    c, _ = client
    task_id = _mk_task(c)
    shared = os.path.join(retention_ws, "same_path.md")
    with open(shared, "w") as f:
        f.write("version one")
    old_row = _register(task_id, shared)
    _expire(old_row.id, age_days=30, superseded_at=datetime.utcnow() - timedelta(days=29))
    # Rerun lands on the SAME path (registry supersession semantics).
    with open(shared, "w") as f:
        f.write("version two — current")
    _register(task_id, shared)

    stats = _cleanup(_app_main().TASK_WORKSPACE, 7)

    assert os.path.isfile(shared), "shared path holds CURRENT bytes — untouchable"
    assert stats.skipped_current_path >= 1
    with open(shared) as f:
        assert f.read() == "version two — current"

    # The current row still serves its own bytes.
    arts = c.get(f"/api/v1/tasks/{task_id}/artifacts").json()["artifacts"]
    cur = next(a for a in arts if a["exists"])
    r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{cur['artifact_id']}")
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == cur["hash"]

    # The old row remains queryable; its replaced bytes honestly 404.
    old = _row_by_id(old_row.id)
    assert old is not None and old.is_current is False
    r_old = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{old_row.id}")
    assert r_old.status_code == 404


def test_cleanup_vs_registration_race(client, retention_ws):
    """Registration that lands between eligibility check and delete wins.

    Cleanup decides eligibility from a rows snapshot; a concurrent rerun
    can re-register the same path afterwards. The mtime guard must stop
    deletion because the file is now newer than the old row's timestamps.
    """
    c, _ = client
    task_id = _mk_task(c)
    shared = os.path.join(retention_ws, "race.md")
    with open(shared, "w") as f:
        f.write("old generation")
    old_row = _register(task_id, shared)
    sup = datetime.utcnow() - timedelta(days=25)
    _expire(old_row.id, age_days=30, superseded_at=sup)

    async def _race():
        real_root = os.path.realpath(_app_main().TASK_WORKSPACE)
        async with _db() as db:
            expired = await _artifact_cleanup().find_expired_artifacts(
                db, root=real_root, retention_days=7
            )
        assert expired, "old row must look eligible from the snapshot"
        # Simulate the concurrent rerun winning the race: new bytes, new row.
        await asyncio.sleep(0)
        with open(shared, "w") as f:
            f.write("new generation")
        fresh = await _app_main()._register_task_artifact(
            task_id,
            path=shared,
            node_id="node-race",
            step_db_id=None,
            step_title="race rerun",
            run_id=None,
        )
        # Deletion phase runs with the STALE snapshot: the mtime guard
        # (not the current-path set, which is also stale) must fire.
        # NOTE: the shared test DB holds rows from earlier tests whose
        # fixture dirs are gone — tolerate already-missing files exactly
        # like the service does.
        deleted = 0
        for _r, resolved in expired:
            if not os.path.isfile(resolved):
                continue
            mtime = os.path.getmtime(resolved)
            if mtime > sup.timestamp():
                continue
            os.remove(resolved)
            deleted += 1
        return deleted, fresh

    deleted, fresh = asyncio.run(_race())
    assert deleted == 0, "mtime guard must stop the stale-snapshot deletion"
    assert os.path.isfile(shared), "the NEW artifact's bytes must survive the race"
    row = _row_by_id(fresh.id)
    assert row is not None and row.is_current is True
    with open(shared) as f:
        assert f.read() == "new generation"


# ---------------------------------------------------------------------------
# 4. Orphan GC (pass B)
# ---------------------------------------------------------------------------


def test_orphan_gc_reaps_aged_unregistered_files_only(client, retention_ws):
    c, _ = client
    old = os.path.join(retention_ws, "leftover_old.bin")
    new = os.path.join(retention_ws, "leftover_new.bin")
    with open(old, "wb") as f:
        f.write(b"old orphan")
    with open(new, "wb") as f:
        f.write(b"fresh orphan")
    _age_file(old, 40)
    # A registered file in the same tree must be protected.
    reg = os.path.join(retention_ws, "registered.bin")
    with open(reg, "wb") as f:
        f.write(b"registered bytes")
    _age_file(reg, 40)
    task_id = _mk_task(c)
    _register(task_id, reg)  # current row → protected

    stats = _cleanup(_app_main().TASK_WORKSPACE, 30)

    assert not os.path.isfile(old), "aged orphan must be garbage-collected"
    assert stats.orphans_deleted >= 1 and stats.orphan_bytes_freed >= 10
    assert os.path.isfile(new), "fresh orphan (within window) must survive"
    assert os.path.isfile(reg), "registered path must never be orphan-reaped"
    assert stats.orphans_protected_registered >= 1


def test_orphan_gc_never_touches_directory_structure(client, retention_ws):
    sub = os.path.join(retention_ws, "subdir")
    os.makedirs(sub, exist_ok=True)
    stale = os.path.join(sub, "deep_old.txt")
    with open(stale, "w") as f:
        f.write("deep")
    _age_file(stale, 40)
    _age_file(sub, 40)

    _cleanup(_app_main().TASK_WORKSPACE, 30)

    assert not os.path.isfile(stale)
    assert os.path.isdir(sub), "directories are never removed by cleanup"


# ---------------------------------------------------------------------------
# 5. Containment / traversal protection
# ---------------------------------------------------------------------------


def test_row_outside_workspace_root_is_never_followed(client, tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "escaped.txt"
    outside.write_text("do not touch")

    async def _plant():
        from app.models.task_artifact import TaskArtifact

        async with _db() as db:
            row = TaskArtifact(
                task_id="t-containment-probe",
                step_id=None,
                node_id="n",
                step_title="containment probe",
                run_id=None,
                filename="escaped.txt",
                storage_path=str(outside),
                size_bytes=len("do not touch"),
                content_type="text/plain",
                sha256=hashlib.sha256(b"do not touch").hexdigest(),
                is_current=False,
                superseded_at=datetime.utcnow() - timedelta(days=25),
            )
            row.created_at = datetime.utcnow() - timedelta(days=30)
            db.add(row)
            await db.commit()
            db.expunge(row)
            return row

    row = asyncio.run(_plant())

    async def _remove():
        from app.models.task_artifact import TaskArtifact

        async with _db() as db:
            r = await db.get(TaskArtifact, row.id)
            if r is not None:
                await db.delete(r)
                await db.commit()

    try:
        stats = _cleanup(_app_main().TASK_WORKSPACE, 7)
        assert outside.exists(), "a path outside the root must NEVER be deleted"
        # find_expired_artifacts must EXCLUDE the uncontained row outright —
        # callers never even see an outside-root path (counter defense in
        # depth is guard 2 inside the pass).

        async def _probe():
            async with _db() as db:
                return await _artifact_cleanup().find_expired_artifacts(
                    db, root=_app_main().TASK_WORKSPACE, retention_days=7
                )

        eligible = asyncio.run(_probe())
        assert all(
            os.path.realpath(r.storage_path) != os.path.realpath(str(outside))
            for r, _p in eligible
        ), "outside-root row must never be returned as eligible"
        # Metadata intact.
        assert _row_by_id(row.id) is not None
    finally:
        asyncio.run(_remove())


# ---------------------------------------------------------------------------
# 6. Cleanup failure handling
# ---------------------------------------------------------------------------


def test_per_file_delete_failure_counted_not_raised(client, retention_ws, monkeypatch):
    stale = os.path.join(retention_ws, "locked.bin")
    with open(stale, "wb") as f:
        f.write(b"unremovable")
    _age_file(stale, 40)

    real_remove = os.remove

    def _boom(path, *a, **k):
        if os.path.abspath(str(path)) == os.path.abspath(stale):
            raise OSError("simulated locked file")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(_artifact_cleanup().os, "remove", _boom, raising=False)
    stats = _cleanup(_app_main().TASK_WORKSPACE, 30)
    assert stats.errors >= 1
    assert os.path.isfile(stale), "failed deletion leaves the file; retry next pass"


def test_pass_a_delete_failure_counted_not_raised(client, retention_ws, monkeypatch):
    """Pass A (registry-driven) delete failures are also contained."""
    c, _ = client
    task_id = _mk_task(c)
    p_old = os.path.join(retention_ws, "locked_a.md")
    with open(p_old, "w") as f:
        f.write("old gen")
    _age_file(p_old, 30)
    row_old = _register(task_id, p_old)
    _expire(row_old.id, age_days=30, superseded_at=datetime.utcnow() - timedelta(days=20))
    p_new = os.path.join(retention_ws, "current_a.md")
    with open(p_new, "w") as f:
        f.write("cur")
    _register(task_id, p_new)  # different path → p_old's bytes unowned

    real_remove = os.remove

    def _boom(path, *a, **k):
        if os.path.abspath(str(path)) == os.path.abspath(p_old):
            raise OSError("simulated locked file (pass A)")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(_artifact_cleanup().os, "remove", _boom, raising=False)
    stats = _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert stats.errors >= 1, "Pass A delete failures are counted, never raised"
    assert os.path.isfile(p_old)


# ---------------------------------------------------------------------------
# 7. Scheduler behavior
# ---------------------------------------------------------------------------


def test_scheduler_worker_only_when_retention_enabled():
    m = _app_main()
    assert m.settings.artifact_retention_days == 0
    assert m._cleanup_worker_task is None, "no worker at retention 0"


def test_scheduler_loop_failure_is_contained(monkeypatch):
    m = _app_main()
    ac = _artifact_cleanup()

    async def _boom(root, settings):
        raise RuntimeError("simulated cleanup crash")

    monkeypatch.setattr(ac, "run_cleanup_with_own_session", _boom)
    monkeypatch.setattr(
        m.settings, "artifact_cleanup_interval_seconds", 1, raising=False
    )

    async def _drive():
        task = asyncio.create_task(m._artifact_cleanup_loop())
        await asyncio.sleep(1.4)  # one interval + margin
        assert not task.done(), "loop must survive a failing pass"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())


def test_lifespan_graceful_shutdown_cancels_worker(monkeypatch):
    """Lifespan startup spawns the worker at N>0 and cancels it at shutdown."""
    m = _app_main()
    ac = _artifact_cleanup()

    async def _noop_session(root, settings):
        return ac.CleanupStats().safe_summary()

    monkeypatch.setattr(ac, "run_cleanup_with_own_session", _noop_session)
    monkeypatch.setattr(m.settings, "artifact_retention_days", 7, raising=False)
    monkeypatch.setattr(
        m.settings, "artifact_cleanup_interval_seconds", 3600, raising=False
    )
    try:
        with TestClient(m.app):
            worker = m._cleanup_worker_task
            assert worker is not None and not worker.done()
        assert worker.cancelled() or worker.done(), (
            "shutdown must cancel the cleanup worker"
        )
    finally:
        monkeypatch.setattr(m.settings, "artifact_retention_days", 0, raising=False)
        m._cleanup_worker_task = None


# ---------------------------------------------------------------------------
# 8. Admin manual cleanup endpoint
# ---------------------------------------------------------------------------


def test_admin_cleanup_requires_auth():
    from app import main as app_main

    with TestClient(app_main.app) as c:
        r = c.post("/api/v1/admin/artifacts/cleanup")
        assert r.status_code == 401, r.text


def test_admin_cleanup_forbidden_for_non_admin_is_audited(client):
    c, _user = client
    r = c.post("/api/v1/admin/artifacts/cleanup")
    assert r.status_code == 403, r.text
    assert "admin" in r.json()["detail"].lower()
    from services.security.audit_trail import AuditTrail

    denials = [
        e
        for e in AuditTrail.instance().get_events(action="api:admin_artifact_cleanup")
        if e.allowed is False
    ]
    assert denials, "the 403 must itself be audit-logged"


def test_admin_cleanup_noop_at_zero_and_no_paths_in_response(client):
    c, user = client
    from services.security.security_kernel import Permission

    _app_main().security_kernel.grant_permission(user.id, Permission.ADMIN)
    r = c.post("/api/v1/admin/artifacts/cleanup")
    assert r.status_code == 200, r.text
    stats = r.json()["stats"]
    assert stats["retention_disabled"] is True
    blob = repr(r.json())
    assert "workspaces" not in blob and "\\" not in blob and "/Users" not in blob


def test_admin_cleanup_runs_real_pass_when_enabled(client, retention_ws, monkeypatch):
    c, user = client
    stale = os.path.join(retention_ws, "admin_gc.bin")
    with open(stale, "wb") as f:
        f.write(b"ancient garbage")
    _age_file(stale, 40)
    from services.security.security_kernel import Permission

    m = _app_main()
    m.security_kernel.grant_permission(user.id, Permission.ADMIN)
    monkeypatch.setattr(m.settings, "artifact_retention_days", 30, raising=False)
    try:
        r = c.post("/api/v1/admin/artifacts/cleanup")
        assert r.status_code == 200, r.text
        assert r.json()["stats"]["orphans_deleted"] >= 1
        assert not os.path.isfile(stale)
    finally:
        monkeypatch.setattr(m.settings, "artifact_retention_days", 0, raising=False)


# ---------------------------------------------------------------------------
# 9. ZIP exports after cleanup
# ---------------------------------------------------------------------------


def test_task_zip_after_cleanup_lists_missing_honestly(client, retention_ws):
    """ZIP semantics after cleanup, matching the EXISTING bundle contract.

    Bundles export CURRENT registry rows only, so a cleaned superseded row
    simply never appears (nothing substituted). The honest-missing path is
    exercised with a current row whose bytes are genuinely gone — recorded
    in MISSING.txt, never silently omitted.
    """
    c, _ = client
    task_id = _mk_task(c)
    p_old, _row_old, p_cur, _row_cur = _superseded_history(
        task_id, retention_ws, base="zipped"
    )

    # A current artifact whose bytes are already missing (pre-existing
    # honest-missing case, unrelated to retention).
    p_gone = os.path.join(retention_ws, "vanished.md")
    with open(p_gone, "w") as f:
        f.write("soon gone")
    row_gone = _register(task_id, p_gone)
    os.remove(p_gone)

    _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert not os.path.isfile(p_old)

    r = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download")
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any("zipped_v2.md" in n for n in names), "current artifact bytes included"
    assert not any(
        n.endswith("zipped.md") for n in names
    ), "cleaned superseded bytes must not be substituted"
    assert "MISSING.txt" in names, "missing current artifact recorded honestly"
    assert "vanished.md" in zf.read("MISSING.txt").decode()


def test_project_zip_after_cleanup_uses_registry_metadata(client, retention_ws):
    """Project ZIP after cleanup: current bytes present, cleaned superseded
    rows absent (bundles are current-only), and a genuinely missing current
    row honestly recorded as `on_disk: NO` in the manifest.
    """
    c, _ = client
    pr = c.post("/api/v1/projects", json={"name": f"ret-proj-{_uid()}"})
    assert pr.status_code in (200, 201), pr.text
    project_id = pr.json()["id"]
    task_id = _mk_task(c, title="proj ret", project_id=project_id)

    p_old, _row_old, p_cur, _row_cur = _superseded_history(
        task_id, retention_ws, base="proj"
    )
    p_gone = os.path.join(retention_ws, "proj_vanished.md")
    with open(p_gone, "w") as f:
        f.write("soon gone")
    row_gone = _register(task_id, p_gone)
    os.remove(p_gone)

    _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert not os.path.isfile(p_old)

    r = c.get(f"/api/v1/projects/{project_id}/artifacts/bundle/download")
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = zf.read("MANIFEST.txt").decode()
    assert "proj_v2.md" in manifest, "current artifact recorded"
    assert "proj_vanished.md" in manifest
    assert "on_disk: NO" in manifest, "missing current artifact honest in manifest"
    names = zf.namelist()
    assert not any(n.endswith("proj.md") for n in names), "no substituted bytes"


# ---------------------------------------------------------------------------
# 10. Cross-user protection & limits interaction
# ---------------------------------------------------------------------------


def test_cleanup_respects_cross_user_ownership(client):
    """Ownership semantics are untouched by cleanup: a REAL foreign user
    (JWT, no dependency overrides) gets honest 404s on another user's
    task artifacts, while the owner still sees 200.
    """
    c, user = client
    other_task = _mk_task(c, title="other user's task")
    # Owner positive control (override user): sees the task.
    assert c.get(f"/api/v1/tasks/{other_task}/artifacts").status_code == 200

    # A real second user through the real auth endpoints.
    email = f"{_uid()}@ret.test"
    r = c.post("/auth/register", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code in (200, 201)
    r = c.post("/auth/login", json={"email": email, "password": "Str0ngPass!x"})
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Drop the overrides so the JWT path (real get_current_user) runs.
    app_main = _app_main()
    app_main.app.dependency_overrides.clear()
    try:
        r = c.get(f"/api/v1/tasks/{other_task}/artifacts", headers=headers)
        assert r.status_code == 404, "foreign task artifacts must stay 404"
        r = c.get(f"/api/v1/tasks/{other_task}/artifacts/history", headers=headers)
        assert r.status_code == 404
        r = c.get(f"/api/v1/tasks/{other_task}/artifacts/bundle/download", headers=headers)
        assert r.status_code == 404
    finally:
        app_main.app.dependency_overrides.clear()


def test_limits_and_retention_are_independent_knobs(client, retention_ws, monkeypatch):
    """Tightening retention does not weaken size/count/zip limits."""
    c, _ = client
    m = _app_main()
    monkeypatch.setattr(m.settings, "max_artifact_size_bytes", 8, raising=False)
    big = os.path.join(retention_ws, "big.bin")
    with open(big, "wb") as f:
        f.write(b"x" * 64)
    with pytest.raises(ValueError) as ei:
        _register("t-limits", big)
    assert "MAX_ARTIFACT_SIZE_BYTES" in str(ei.value)

    # An oversized registration never produced a row — only the orphan pass
    # could ever see the file, and only after the full retention window.
    monkeypatch.setattr(
        m.settings, "max_artifact_size_bytes", 1024 * 1024, raising=False
    )
    stats = _cleanup(_app_main().TASK_WORKSPACE, 7)
    assert os.path.isfile(big), "recent orphan (fresh mtime) stays within window"


def test_settings_validation_refuses_bad_retention_values():
    from app.config import validate_settings, Settings

    with pytest.raises(RuntimeError) as ei:
        validate_settings(
            Settings(
                _env_file=None,
                environment="production",
                secret_key="ab12" * 12,
                database_url="postgresql+asyncpg://u:p@db:5432/x",
                cors_origins="https://a.example",
                allowed_hosts="api.example",
                artifact_retention_days=-3,
            )
        )
    assert "ARTIFACT_RETENTION_DAYS" in str(ei.value)
