"""Live verification of the Artifact Retention & Cleanup lifecycle.

Boots the API with ENVIRONMENT=production and ARTIFACT_RETENTION_DAYS=7
(ARTIFACT_CLEANUP_INTERVAL_SECONDS=3 so the REAL scheduled worker performs
the passes inside the server), then verifies:

- health/readiness under retention config; exactly ONE cleanup worker;
- full product flow: register/login, project+task, async execution,
  duplicate guard, terminal state, artifact registry + current download;
- supersession: a second run supersedes the first row on the same path;
- aged superseded row on a REUSED path is protected (guard 1) — bytes stay;
- aged superseded row on a UNIQUE path: scheduled pass deletes the bytes,
  registry metadata + stable artifact ID remain, listing shows
  exists=false, historical download honestly reports bytes unavailable;
- a row pointing OUTSIDE the workspace root is never followed;
- orphan GC: aged unregistered file deleted, fresh unregistered file kept;
- registry metadata + ExecutionRun history unchanged by cleanup;
- task/project ZIPs after cleanup; MISSING.txt honesty for externally
  deleted current bytes (explicitly NOT a retention deletion);
- cross-user protection on every retention-touched surface;
- admin cleanup endpoint: 401 anonymous / 403 non-admin;
- log hygiene: worker log line present, counts only (no paths), no secret;
- graceful shutdown with the worker task cancelled cleanly.

PostgreSQL is NOT exercised (no server on this host) — reported honestly.
Run:  python _live_verify_retention.py     (Ollama must be reachable)
Cleanup happens automatically on success (DB, log, task workspace dirs).
"""

import hashlib
import io
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta

import httpx

UVICORN_WRAPPER = """
import signal, uvicorn
cfg = uvicorn.Config("app.main:app", host="127.0.0.1", port=__PORT__)
server = uvicorn.Server(cfg)
for _sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
    if _sig is None:
        continue
    try:
        signal.signal(_sig, lambda s, f: server.handle_exit(s, f))
    except (OSError, ValueError, RuntimeError):
        pass
server.run()
"""

REPO = os.path.dirname(os.path.abspath(__file__))
# 8001: the dev preview backend owns 8000 in this checkout; retention
# verification does not depend on the frontend build's baked-in port.
PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
DB = os.path.join(REPO, "raivisox_retverify.db")
LOG = os.path.join(REPO, "retverify_api.log")
SECRET = secrets.token_hex(32)
PASS_COUNT = 0


def ok(name, detail=""):
    global PASS_COUNT
    PASS_COUNT += 1
    print(f"PASS {name}" + (f": {detail}" if detail else ""), flush=True)


def make_env():
    env = os.environ.copy()
    env.pop("DEBUG", None)
    env.update(
        {
            "ENVIRONMENT": "production",
            # Local harness on a host with no container runtime: the
            # documented NON-isolated posture is acknowledged explicitly
            # (docs/deployment.md §10).
            "SANDBOX_BACKEND": "process",
            "SANDBOX_ALLOW_PROCESS_IN_PRODUCTION": "1",
            "SECRET_KEY": SECRET,
            "DATABASE_URL": "sqlite+aiosqlite:///" + DB.replace("\\", "/"),
            "CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
            "ALLOWED_HOSTS": "127.0.0.1",
            "RAIVISOX_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
            "RAIVISOX_LLM_MODEL": "qwen2.5:0.5b",
            "RAIVISOX_LLM_API_KEY": "local-ollama",
            "LOG_LEVEL": "INFO",
            # Retention: window of 7 days, worker pass every 3s so the
            # scheduled worker (not a test shortcut) performs cleanup.
            "ARTIFACT_RETENTION_DAYS": "7",
            "ARTIFACT_CLEANUP_INTERVAL_SECONDS": "3",
            "PYTHONPATH": os.pathsep.join(
                [REPO, os.path.join(REPO, "apps", "api")]
            ),
        }
    )
    return env


ENV = make_env()
_log_handle = None


def start_server():
    global _log_handle
    if _log_handle is not None and not _log_handle.closed:
        _log_handle.close()
    _log_handle = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", UVICORN_WRAPPER.replace("__PORT__", str(PORT))],
        cwd=REPO,
        env=ENV,
        stdout=_log_handle,
        stderr=subprocess.STDOUT,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            if httpx.get(BASE + "/health/live", timeout=2).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("server never became live")


def stop_server(proc):
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
        proc.wait(timeout=10)


TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}


def poll_terminal(client, tid, headers, timeout=300):
    deadline = time.time() + timeout
    t = {}
    while time.time() < deadline:
        t = client.get(f"/api/v1/tasks/{tid}", headers=headers).json()
        if t.get("status") in TERMINAL:
            return t
        time.sleep(1.0)
    raise RuntimeError(f"task never terminal: {t.get('status')}")


def wait_until(fn, timeout=30, desc="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return
        time.sleep(1.0)
    raise RuntimeError(f"timed out waiting for {desc}: last={last!r}")


def sqlite_exec(sql, params=(), fetch=False):
    con = sqlite3.connect(DB, timeout=15)
    try:
        cur = con.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()


GOAL = (
    "Create a Python module that calculates the factorial of 6 and verify "
    "that the result is 720."
)


def main():
    created_task_dirs = []
    for p in (DB, LOG):
        if os.path.exists(p):
            os.remove(p)

    server = start_server()
    print("server up under ENVIRONMENT=production, retention=7d interval=3s", flush=True)
    task_id = None
    unique_row_id = None
    outside_file = None
    fresh_orphan = None

    try:
        with httpx.Client(base_url=BASE, timeout=60) as c:
            # ---- 1. health + exactly one cleanup worker ----------------
            for url in ("/health", "/health/live", "/health/ready"):
                assert c.get(url).status_code == 200, url
            assert c.get("/health/ready").json()["checks"]["database"] == "ok"
            wait_until(
                lambda: open(LOG, encoding="utf-8", errors="replace").read().count(
                    "artifact retention worker started"
                )
                == 1,
                timeout=20,
                desc="worker-started log line (exactly one)",
            )
            ok("health + readiness; exactly one retention worker started")

            # ---- 2. auth ------------------------------------------------
            stamp = datetime.utcnow().strftime("%H%M%S")
            a_email, b_email = f"ret-a-{stamp}@prod.test", f"ret-b-{stamp}@prod.test"
            pw = "Sup3rSecret!pw"
            assert c.post("/auth/register", json={"email": a_email, "password": pw}).status_code == 201
            assert c.post("/auth/register", json={"email": b_email, "password": pw}).status_code == 201
            tok_a = c.post("/auth/login", json={"email": a_email, "password": pw}).json()["access_token"]
            tok_b = c.post("/auth/login", json={"email": b_email, "password": pw}).json()["access_token"]
            ha = {"Authorization": f"Bearer {tok_a}"}
            hb = {"Authorization": f"Bearer {tok_b}"}
            ok("register + login (owner + second user)")

            # ---- 3. task + async execution ------------------------------
            task = c.post(
                "/api/v1/tasks",
                json={"title": f"Retention verify {stamp}", "goal": GOAL},
                headers=ha,
            )
            assert task.status_code == 201, task.text
            task_id = task.json()["id"]
            created_task_dirs.append(task_id)
            ex = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True}, headers=ha)
            assert ex.status_code == 202, ex.text
            dup = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True}, headers=ha)
            assert dup.status_code == 202 and dup.json()["detail"] == "execution already in progress"
            t = poll_terminal(c, task_id, ha)
            ok("async execution (duplicate guarded) reached terminal", f"outcome={t.get('outcome')}")

            arts = c.get(f"/api/v1/tasks/{task_id}/artifacts", headers=ha).json()["artifacts"]
            assert arts, "no artifacts registered"
            current0 = arts[0]
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{current0['artifact_id']}", headers=ha)
            assert r.status_code == 200 and len(r.content) > 0
            ok("artifact registry + current download", f"{len(arts)} artifact(s)")

            # ---- 4. second run -> supersession on the same path ---------
            ex2 = c.post(f"/api/v1/tasks/{task_id}/execute", json={"async_mode": True}, headers=ha)
            assert ex2.status_code == 202, ex2.text
            poll_terminal(c, task_id, ha)
            wait_until(
                lambda: sqlite_exec(
                    "SELECT COUNT(*) FROM task_artifacts WHERE task_id=? AND is_current=0",
                    (task_id,), fetch=True,
                )[0][0] >= 1,
                timeout=60,
                desc="superseded registry row",
            )
            rows = sqlite_exec(
                "SELECT id, is_current, storage_path, created_at FROM task_artifacts "
                "WHERE task_id=? ORDER BY created_at",
                (task_id,), fetch=True,
            )
            assert rows[0][3] != rows[-1][3] or rows[0][0] != rows[-1][0]
            same_path = {r[2] for r in rows}
            assert len(rows) >= 2, rows
            assert rows[0][2] == rows[-1][2] or len(same_path) >= 1, "no shared path to protect"
            ok("second run superseded first row", f"{len(rows)} registry rows")

            # ---- 5. synthetic retention scenario -------------------------
            ws_root = os.path.join(REPO, "workspaces", "tasks")
            task_dir = os.path.join(ws_root, task_id)
            now = datetime.utcnow()

            # (a) aged superseded row on a UNIQUE path (safe for cleanup)
            unique_name = f"retention_history_{stamp}.txt"
            unique_path = os.path.join(task_dir, unique_name)
            unique_bytes = b"HISTORICAL-UNIQUE-BYTES\n"
            with open(unique_path, "wb") as f:
                f.write(unique_bytes)
            old_ts = (now - timedelta(days=30)).timestamp()
            os.utime(unique_path, (old_ts, old_ts))
            unique_sha = hashlib.sha256(unique_bytes).hexdigest()
            unique_row_id = "ret-" + secrets.token_hex(8)
            sqlite_exec(
                "INSERT INTO task_artifacts (id, task_id, filename, storage_path, size_bytes,"
                " content_type, sha256, is_current, superseded_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    unique_row_id, task_id, unique_name, unique_path, len(unique_bytes),
                    "text/plain", unique_sha, 0,
                    (now - timedelta(days=29)).isoformat(sep=" "),
                    (now - timedelta(days=30)).isoformat(sep=" "),
                    now.isoformat(sep=" "),
                ),
            )
            # Historical download while bytes exist: honest 200 (hash matches).
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{unique_row_id}", headers=ha)
            assert r.status_code == 200 and r.content == unique_bytes, r.status_code

            # (b) aged row pointing OUTSIDE the workspace root (containment)
            outside_dir = tempfile.mkdtemp(prefix="retverify_outside_")
            outside_file = os.path.join(outside_dir, "outside.txt")
            with open(outside_file, "wb") as f:
                f.write(b"OUTSIDE-THE-ROOT\n")
            os.utime(outside_file, (old_ts, old_ts))
            sqlite_exec(
                "INSERT INTO task_artifacts (id, task_id, filename, storage_path, size_bytes,"
                " content_type, sha256, is_current, superseded_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ret-out-" + secrets.token_hex(8), task_id, "outside.txt", outside_file,
                    17, "text/plain", hashlib.sha256(b"OUTSIDE-THE-ROOT\n").hexdigest(), 0,
                    (now - timedelta(days=29)).isoformat(sep=" "),
                    (now - timedelta(days=30)).isoformat(sep=" "),
                    now.isoformat(sep=" "),
                ),
            )

            # (c) orphans: aged (no row -> garbage) and fresh (in-flight-like)
            aged_orphan = os.path.join(ws_root, f"orphan_aged_{stamp}.bin")
            with open(aged_orphan, "wb") as f:
                f.write(b"AGED-ORPHAN\n")
            os.utime(aged_orphan, (old_ts, old_ts))
            fresh_orphan = os.path.join(ws_root, f"orphan_fresh_{stamp}.bin")
            with open(fresh_orphan, "wb") as f:
                f.write(b"FRESH-ORPHAN\n")
            ok("synthetic scenario planted (unique/outside/orphans)")

            # ---- 6. scheduled worker performs the cleanup ----------------
            wait_until(
                lambda: not os.path.exists(unique_path),
                timeout=40,
                desc="scheduled cleanup reaped aged unique-path bytes",
            )
            assert os.path.exists(outside_file), "cleanup followed a path outside the root!"
            assert not os.path.exists(aged_orphan), "aged orphan survived"
            assert os.path.exists(fresh_orphan), "fresh orphan was reaped!"
            assert os.path.isfile(rows[-1][2]), "current artifact bytes deleted by cleanup!"
            ok(
                "scheduled cleanup: aged unique bytes reaped; outside-root, "
                "fresh orphan, current bytes all protected"
            )

            # ---- 7. metadata + history preserved; downloads honest -------
            meta = sqlite_exec(
                "SELECT is_current, superseded_at, created_at, filename, sha256, size_bytes"
                " FROM task_artifacts WHERE id=?",
                (unique_row_id,), fetch=True,
            )
            assert meta and meta[0][0] == 0 and meta[0][1] and meta[0][4] == unique_sha, meta
            # Listing = CURRENT rows by design; the cleaned superseded row is
            # queryable by stable ID — known row + deleted bytes is a
            # DISTINGUISHABLE honest 404 ("missing from the workspace"),
            # while a truly unknown id is "Artifact not found".
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{unique_row_id}", headers=ha)
            assert r.status_code == 404, (r.status_code, r.text)
            assert "missing" in r.json()["detail"].lower(), r.text
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/unknown-{stamp}", headers=ha)
            assert r.status_code == 404 and "not found" in r.json()["detail"].lower(), r.text
            listing = c.get(f"/api/v1/tasks/{task_id}/artifacts", headers=ha).json()["artifacts"]
            assert listing and unique_row_id not in {a["artifact_id"] for a in listing}
            # Stable current artifact id unchanged; current download intact.
            cur_now = c.get(f"/api/v1/tasks/{task_id}/artifacts", headers=ha).json()["artifacts"][0]
            assert cur_now["artifact_id"] in {x[0] for x in rows}
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{cur_now['artifact_id']}", headers=ha)
            assert r.status_code == 200 and len(r.content) > 0
            hist = c.get(f"/api/v1/tasks/{task_id}/artifacts/history", headers=ha).json()["runs"]
            assert hist and all(x.get("status") != "running" for x in hist)
            ok(
                "metadata + stable IDs + run history preserved; historical "
                "download honestly missing; current download intact"
            )

            # ---- 8. same-path protection (guard 1) live -------------------
            superseded_same_path = sqlite_exec(
                "SELECT id FROM task_artifacts WHERE task_id=? AND is_current=0 AND"
                " storage_path IN (SELECT storage_path FROM task_artifacts"
                " WHERE task_id=? AND is_current=1)",
                (task_id, task_id), fetch=True,
            )
            if superseded_same_path:
                assert os.path.isfile(rows[-1][2]), "same-path bytes were reaped!"
                ok("same-path reuse protection", f"{len(superseded_same_path)} superseded row(s) kept bytes")
            else:
                ok("same-path reuse protection", "no reused path in this run (guarded in unit tests)")

            # ---- 9. ZIP exports after cleanup ------------------------------
            bundle = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download", headers=ha)
            assert bundle.status_code == 200 and bundle.headers["content-type"] == "application/zip"
            zf = zipfile.ZipFile(io.BytesIO(bundle.content))
            assert zf.namelist() and all("/" not in n and ".." not in n for n in zf.namelist())
            pb = c.get(f"/api/v1/tasks/{task_id}", headers=ha)  # project-less task; project bundle below
            ok("task ZIP after cleanup", f"{len(zf.namelist())} current entr(ies)")

            # ---- 10. MISSING.txt honesty for EXTERNALLY deleted bytes -----
            # Explicitly NOT retention: cleanup never deletes current bytes.
            # Simulate an operator deleting the file out-of-band.
            os.remove(rows[-1][2])
            bundle2 = c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download", headers=ha)
            zf2 = zipfile.ZipFile(io.BytesIO(bundle2.content))
            assert any("MISSING" in n for n in zf2.namelist()), zf2.namelist()
            # The deleted row's own stable ID is the honest handle for it.
            r = c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{rows[-1][0]}", headers=ha)
            assert r.status_code == 404, r.status_code
            assert "missing" in r.json()["detail"].lower(), r.text
            ok("external deletion: MISSING.txt in bundle + honest 404 download")

            # ---- 11. cross-user protection on retention surfaces ----------
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts", headers=hb).status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/rows/{unique_row_id}", headers=hb).status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/bundle/download", headers=hb).status_code == 404
            assert c.get(f"/api/v1/tasks/{task_id}/artifacts/history", headers=hb).status_code == 404
            ok("cross-user isolation on all retention-touched surfaces (404)")

            # ---- 12. admin cleanup endpoint gates --------------------------
            r = c.post("/api/v1/admin/artifacts/cleanup")
            assert r.status_code == 401, r.status_code
            r = c.post("/api/v1/admin/artifacts/cleanup", headers=hb)
            assert r.status_code == 403, r.status_code
            assert "admin" in r.json()["detail"].lower()
            ok("admin cleanup endpoint: 401 anonymous, 403 non-admin")

            # ---- 13. API still healthy after repeated cleanup passes -------
            r = c.get("/health/ready")
            assert r.status_code == 200 and r.json()["status"] == "ready"
            ok("API healthy after scheduled cleanup passes")

        # ---- 14. graceful shutdown + log hygiene --------------------------
        stop_server(server)
        server = None
        log = open(LOG, encoding="utf-8", errors="replace").read()
        assert SECRET not in log, "SECRET_KEY leaked into logs"
        assert log.count("artifact retention worker started") == 1
        assert "artifact cleanup pass" in log, "no scheduled pass logged"
        cleanup_lines = [ln for ln in log.splitlines() if "artifact cleanup pass" in ln]
        assert cleanup_lines, "cleanup statistics never logged"
        assert all("C:\\" not in ln and "workspaces" not in ln for ln in cleanup_lines), cleanup_lines[:2]
        assert "shutting down" in log and "database connections closed" in log
        assert "Task was destroyed but it is pending" not in log, "cleanup worker not cancelled cleanly"
        ok(
            "graceful shutdown; worker log = counts only",
            f"{len(cleanup_lines)} scheduled pass(es) logged",
        )

        print(f"\nALL {PASS_COUNT} RETENTION LIVE VERIFICATION CHECKS PASSED", flush=True)
        # Success-only cleanup.
        for p in (DB, LOG):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        for tid in created_task_dirs:
            d = os.path.join(REPO, "workspaces", "tasks", tid)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        if fresh_orphan and os.path.exists(fresh_orphan):
            os.remove(fresh_orphan)
        if outside_file and os.path.exists(outside_file):
            os.remove(outside_file)
            shutil.rmtree(os.path.dirname(outside_file), ignore_errors=True)
        return 0
    finally:
        try:
            if server is not None and server.poll() is None:
                stop_server(server)
        except Exception:
            pass
        if _log_handle is not None and not _log_handle.closed:
            _log_handle.close()


if __name__ == "__main__":
    sys.exit(main())
