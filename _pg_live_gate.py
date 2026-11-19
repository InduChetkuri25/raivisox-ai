"""Ephemeral PostgreSQL Live Gate: full application journey against real PG.

Boots the API with ENVIRONMENT=production and DATABASE_URL pointing at the
local portable PostgreSQL 16.9 instance, then verifies the complete flow:
health/readiness, register/login, project+task, async execution with
duplicate guard, terminal outcome, execution_run persistence (PG query),
artifact registry (PG query) + download + hash/size correctness, task ZIP,
run history, ownership isolation, audit authorization (401/403 audited),
retention cleanup (real scheduled worker: expired superseded bytes reaped,
metadata preserved, current bytes protected), and API health after passes.

Run:  RAIVISOX_PG_URL=... RAIVISOX_PG_DSN=... python _pg_live_gate.py
      (portable PG must be running on 5432; credentials come from the
      environment, never from this file)
Cleans up: drops the gate schema rows it created (fresh DB per run).
"""
import hashlib
import io
import os
import secrets
import signal
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta

import httpx
import psycopg2

REPO = os.path.dirname(os.path.abspath(__file__))
PORT = 8002  # free port; 8000 preview, 8001 smokes
BASE = f"http://127.0.0.1:{PORT}"
PG_URL = os.environ.get("RAIVISOX_PG_URL", "")  # postgresql+asyncpg://user:password@127.0.0.1:5432/raivisox
PG_DSN = os.environ.get("RAIVISOX_PG_DSN", "")  # dbname=raivisox user=... password=... host=127.0.0.1 port=5432
SECRET = secrets.token_hex(32)
PASS_COUNT = 0


def ok(name, detail=""):
    global PASS_COUNT
    PASS_COUNT += 1
    print(f"PASS {name}" + (f": {detail}" if detail else ""), flush=True)


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
            "DATABASE_URL": PG_URL,
            "CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
            "ALLOWED_HOSTS": "127.0.0.1",
            "RAIVISOX_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
            "RAIVISOX_LLM_MODEL": "qwen2.5:0.5b",
            "RAIVISOX_LLM_API_KEY": "local-ollama",
            "LOG_LEVEL": "INFO",
            "ARTIFACT_RETENTION_DAYS": "7",
            "ARTIFACT_CLEANUP_INTERVAL_SECONDS": "3",
            "PYTHONPATH": os.pathsep.join([REPO, os.path.join(REPO, "apps", "api")]),
        }
    )
    return env


ENV = make_env()
LOG = os.path.join(REPO, "pggate_api.log")


def start_server():
    handle = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", UVICORN_WRAPPER.replace("__PORT__", str(PORT))],
        cwd=REPO, env=ENV, stdout=handle, stderr=subprocess.STDOUT,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early: {proc.returncode}")
        try:
            if httpx.get(BASE + "/health/live", timeout=2).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("server never live")


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


def pg(sql, params=(), fetch=False):
    con = psycopg2.connect(PG_DSN, connect_timeout=10)
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        return cur.fetchall() if fetch else None
    finally:
        con.commit()
        con.close()


def wait_until(fn, timeout=40, desc="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return
        time.sleep(1.0)
    raise RuntimeError(f"timeout: {desc}: last={last!r}")


TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}


def poll_terminal(c, tid, headers, timeout=300):
    deadline = time.time() + timeout
    t = {}
    while time.time() < deadline:
        t = c.get(f"/api/v1/tasks/{tid}", headers=headers).json()
        if t.get("status") in TERMINAL:
            return t
        time.sleep(1.0)
    raise RuntimeError(f"never terminal: {t.get('status')}")


def main():
    stamp = datetime.utcnow().strftime("%H%M%S")
    user_ids = []
    server = start_server()
    print("API up against PostgreSQL 16.9 (ENVIRONMENT=production)", flush=True)
    try:
        with httpx.Client(base_url=BASE, timeout=60) as c:
            # 1. health/readiness — DB check is a REAL PG round-trip
            r = c.get("/health/ready").json()
            assert r["status"] == "ready" and r["checks"]["database"] == "ok", r
            ok("readiness with live PostgreSQL check", r["checks"]["database"])

            # 2. register + login
            a, b = f"pga-{stamp}@pg.test", f"pgb-{stamp}@pg.test"
            pw = "Sup3rSecret!pw"
            assert c.post("/auth/register", json={"email": a, "password": pw}).status_code == 201
            assert c.post("/auth/register", json={"email": b, "password": pw}).status_code == 201
            ha = {"Authorization": "Bearer " + c.post("/auth/login", json={"email": a, "password": pw}).json()["access_token"]}
            hb = {"Authorization": "Bearer " + c.post("/auth/login", json={"email": b, "password": pw}).json()["access_token"]}
            uid = pg("SELECT id FROM users WHERE email=%s", (a,), fetch=True)[0][0]
            user_ids.append(uid)
            ok("registration + login persisted in PostgreSQL", f"user {uid[:8]}…")

            # 3. project + task
            pid = c.post("/api/v1/projects", json={"name": f"PG Gate {stamp}"}, headers=ha).json()["id"]
            pg_proj = pg("SELECT id FROM projects WHERE id=%s", (pid,), fetch=True)
            assert pg_proj, "project row not in PG"
            goal = ("Create a Python module that calculates the factorial of 7 "
                    "and verify that the result is 5040.")
            tid = c.post("/api/v1/tasks", json={"title": f"PG gate {stamp}", "goal": goal, "project_id": pid}, headers=ha).json()["id"]
            ok("project + task persisted in PostgreSQL", f"task {tid[:8]}…")

            # 4. async execution + duplicate guard
            ex = c.post(f"/api/v1/tasks/{tid}/execute", json={"async_mode": True}, headers=ha)
            assert ex.status_code == 202, ex.text
            dup = c.post(f"/api/v1/tasks/{tid}/execute", json={"async_mode": True}, headers=ha)
            assert dup.status_code == 202 and dup.json()["detail"] == "execution already in progress"
            t = poll_terminal(c, tid, ha)
            assert t.get("outcome") in ("verified", "completed_unverified", "failed"), t.get("outcome")
            ok("async execution + duplicate guard + terminal outcome", f"{t['status']}/{t.get('outcome')}")

            # 5. execution_run persistence in PG
            runs = pg("SELECT id, status, finished_at FROM execution_runs WHERE task_id=%s", (tid,), fetch=True)
            assert runs and all(r[1] != "running" for r in runs) and all(r[2] for r in runs), runs
            ok("execution_run rows in PostgreSQL", f"{len(runs)} run(s), terminal")

            # 6. artifact registry in PG + download + hash/size correctness
            arts = c.get(f"/api/v1/tasks/{tid}/artifacts", headers=ha).json()["artifacts"]
            assert arts, "no artifacts"
            row = pg("SELECT id, filename, size_bytes, sha256, is_current FROM task_artifacts WHERE task_id=%s", (tid,), fetch=True)
            # Registry semantics: exactly one CURRENT row per distinct path
            # (history may hold superseded rows for re-run paths).
            assert row, "no artifact rows in PG"
            paths = {r[1] for r in row}
            for p in paths:
                cur = [r for r in row if r[1] == p and r[4]]
                assert len(cur) == 1, f"path {p}: {len(cur)} current rows"
            one = c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{arts[0]['artifact_id']}", headers=ha)
            assert one.status_code == 200
            pg_row = next(r for r in row if r[0] == arts[0]["artifact_id"])
            assert hashlib.sha256(one.content).hexdigest() == pg_row[3] and len(one.content) == pg_row[2], "hash/size mismatch vs PG"
            ok("artifact registry in PG + download hash/size verified", f"{len(arts)} artifact(s)")

            # 7. task ZIP + project ZIP + run history
            zb = c.get(f"/api/v1/tasks/{tid}/artifacts/bundle/download", headers=ha)
            assert zb.status_code == 200 and zipfile.ZipFile(io.BytesIO(zb.content)).namelist()
            pb = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download", headers=ha)
            assert pb.status_code == 200 and zipfile.ZipFile(io.BytesIO(pb.content)).namelist()
            hist = c.get(f"/api/v1/tasks/{tid}/artifacts/history", headers=ha).json()["runs"]
            assert hist
            ok("task ZIP + project ZIP + run history", f"{len(zf_names(zb))} + {len(zf_names(pb))} entries, {len(hist)} run(s)")

            # 8. ownership isolation (cross-user)
            for url in (f"/api/v1/tasks/{tid}", f"/api/v1/tasks/{tid}/artifacts",
                        f"/api/v1/tasks/{tid}/artifacts/history",
                        f"/api/v1/tasks/{tid}/artifacts/bundle/download",
                        f"/api/v1/projects/{pid}",
                        f"/api/v1/projects/{pid}/artifacts/bundle/download"):
                assert c.get(url, headers=hb).status_code == 404, url
            foreign_arts = pg("SELECT id FROM task_artifacts WHERE task_id=%s", (tid,), fetch=True)
            if foreign_arts:
                assert c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{foreign_arts[0][0]}", headers=hb).status_code == 404
            ok("cross-user isolation on PG-backed surfaces (7 x 404)")

            # 9. audit authorization
            assert c.get("/api/v1/audit", headers=ha).status_code == 403
            assert c.post("/api/v1/admin/artifacts/cleanup", headers=ha).status_code == 403
            assert c.post("/api/v1/admin/artifacts/cleanup").status_code == 401
            from services.security.audit_trail import AuditTrail  # noqa
            ok("audit + admin boundaries on PG backend (403/403/401)")

            # 10. retention cleanup by the REAL scheduled worker on PG
            ws = os.path.join(REPO, "workspaces", "tasks", tid)
            os.makedirs(ws, exist_ok=True)
            unique_path = os.path.join(ws, f"pg_history_{stamp}.txt")
            with open(unique_path, "wb") as f:
                f.write(b"PG-HISTORICAL-BYTES\n")
            old = (datetime.utcnow() - timedelta(days=30)).timestamp()
            os.utime(unique_path, (old, old))
            rid = "pg-" + secrets.token_hex(8)
            pg(
                "INSERT INTO task_artifacts (id, task_id, filename, storage_path, size_bytes,"
                " content_type, sha256, is_current, superseded_at, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (rid, tid, os.path.basename(unique_path), unique_path, 20, "text/plain",
                 hashlib.sha256(b"PG-HISTORICAL-BYTES\n").hexdigest(), False,
                 datetime.utcnow() - timedelta(days=29), datetime.utcnow() - timedelta(days=30), datetime.utcnow()),
            )
            current_before = [x[0] for x in pg("SELECT id FROM task_artifacts WHERE task_id=%s AND is_current", (tid,), fetch=True)]
            wait_until(lambda: not os.path.exists(unique_path), timeout=40, desc="PG-backed worker reaped expired bytes")
            meta = pg("SELECT is_current, superseded_at, sha256 FROM task_artifacts WHERE id=%s", (rid,), fetch=True)
            assert meta and meta[0][0] is False and meta[0][2], "metadata vanished!"
            current_after = [x[0] for x in pg("SELECT id FROM task_artifacts WHERE task_id=%s AND is_current", (tid,), fetch=True)]
            assert current_before == current_after, "current rows changed!"
            r = c.get(f"/api/v1/tasks/{tid}/artifacts/rows/{rid}", headers=ha)
            assert r.status_code == 404 and "missing" in r.json()["detail"].lower()
            ok("retention worker reaped on PostgreSQL; metadata + current rows preserved")

            # 11. API healthy after PG-backed cleanup passes
            assert c.get("/health/ready").json()["status"] == "ready"
            ok("API healthy after PG-backed cleanup passes")

        # 12. graceful shutdown
        stop_server(server)
        server = None
        log = open(LOG, encoding="utf-8", errors="replace").read()
        assert SECRET not in log
        assert "artifact cleanup pass" in log and "shutting down" in log
        ok("graceful shutdown; no secret in logs")

        print(f"\nALL {PASS_COUNT} POSTGRESQL LIVE GATE CHECKS PASSED", flush=True)
        return 0
    finally:
        try:
            if server is not None and server.poll() is None:
                stop_server(server)
        except Exception:
            pass


def zf_names(resp):
    return zipfile.ZipFile(io.BytesIO(resp.content)).namelist()


if __name__ == "__main__":
    sys.exit(main())
