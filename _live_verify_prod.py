"""Live verification with production-like configuration (temporary).

Boots the API with ENVIRONMENT=production and required explicit config,
then walks the whole product surface: health/readiness, docs disabled,
CORS, register/login, project + task creation, async execution, duplicate
guard, execution_run creation, DoD verification, artifact registration +
download + bundle, project export, run history, cross-user isolation,
crash/orphan handling across a hard restart, and log hygiene.

Run:  python _live_verify_prod.py     (Ollama must be reachable)
Cleanup happens automatically (DB, log, task workspace dirs).
"""

import io
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import zipfile

import httpx

# Uvicorn child: SIGINT/SIGTERM are handled by uvicorn itself, but on
# Windows an external stop arrives as CTRL_BREAK -> SIGBREAK, which uvicorn
# does not map. Registering it here makes graceful shutdown (and therefore
# lifespan shutdown: pipeline cancel, audit flush, DB close) observable
# on this platform — production Linux/Docker gets SIGTERM natively.
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
# 8001: a long-running dev preview backend may own 8000 in this checkout;
# this harness must always test ITS OWN production-configured server.
PORT = 8001  # not the frontend's baked-in 8000 — the API is independent here
BASE = f"http://127.0.0.1:{PORT}"
DB = os.path.join(REPO, "raivisox_prodverify.db")
LOG = os.path.join(REPO, "prodverify_api.log")
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
            # This local harness verifies the app journey on a host with no
            # container runtime, so it runs the documented NON-isolated
            # posture explicitly (production refuses the process backend
            # without this acknowledgement — docs/deployment.md §10).
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
    # Append across boots: boot 1 proves structured pipeline logs, boot 2
    # proves the orphan sweep — the final check needs both.
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
            r = httpx.get(BASE + "/health/live", timeout=2)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("server never became live")


def stop_server(proc, hard=False):
    if hard:
        proc.kill()
    elif os.name == "nt":
        # Graceful: CTRL_BREAK -> SIGBREAK -> uvicorn handle_exit ->
        # lifespan shutdown (cancel pipelines, flush audit, close DB).
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


def wait_live(client, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/health/live", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("server did not come back live")


TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}


def poll_terminal(client, tid, headers, timeout=300):
    deadline = time.time() + timeout
    t = {}
    while time.time() < deadline:
        t = client.get(f"/api/v1/tasks/{tid}", headers=headers).json()
        if t.get("status") in TERMINAL:
            return t
        time.sleep(1.0)
    raise RuntimeError(f"task {tid} never reached a terminal state: {t.get('status')} {t}")


def main():
    created_task_dirs = []
    for p in (DB, LOG):
        if os.path.exists(p):
            os.remove(p)

    server = start_server()
    print("server up under ENVIRONMENT=production", flush=True)

    try:
        with httpx.Client(base_url=BASE, timeout=60) as c:
            # ---- 1. health / readiness --------------------------------
            for url in ("/health", "/health/live", "/health/ready"):
                r = c.get(url)
                assert r.status_code == 200, (url, r.status_code, r.text)
                assert SECRET not in r.text and "secret_key" not in r.text, url
            ready = c.get("/health/ready").json()
            assert ready["status"] == "ready", ready
            assert ready["checks"]["database"] == "ok", ready
            assert ready["checks"]["storage"] == "ok", ready
            assert ready["checks"]["llm"]["configured"] is True, ready
            ok("health + readiness", f"llm.reachable={ready['checks']['llm']['reachable']}")

            # ---- 2. production-only surfaces -------------------------
            assert c.get("/docs").status_code == 404
            assert c.get("/redoc").status_code == 404
            assert c.get("/openapi.json").status_code == 404
            ok("interactive API docs disabled in production")

            # ---- 3. CORS ----------------------------------------------
            pre = c.options(
                "/api/v1/tasks",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert pre.headers.get("access-control-allow-origin") == "http://localhost:3000"
            pre_bad = c.options(
                "/api/v1/tasks",
                headers={
                    "Origin": "http://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "access-control-allow-origin" not in pre_bad.headers
            ok("CORS allows configured origin, denies others")

            # ---- 4. auth ----------------------------------------------
            stamp = uuid4()
            a_email, b_email = f"alice-{stamp}@prod.test", f"bob-{stamp}@prod.test"
            pw = "Sup3rSecret!pw"
            r = c.post("/auth/register", json={"email": a_email, "password": pw, "full_name": "Alice"})
            assert r.status_code == 201, r.text
            r = c.post("/auth/register", json={"email": b_email, "password": pw})
            assert r.status_code == 201, r.text
            tok_a = c.post("/auth/login", json={"email": a_email, "password": pw}).json()["access_token"]
            tok_b = c.post("/auth/login", json={"email": b_email, "password": pw}).json()["access_token"]
            ha = {"Authorization": f"Bearer {tok_a}"}
            hb = {"Authorization": f"Bearer {tok_b}"}
            me = c.get("/auth/me", headers=ha).json()
            assert me["email"] == a_email, me
            ok("register + login + /auth/me", "two users")
            assert c.get("/api/v1/tasks", headers=hb).status_code == 200

            # ---- 5. project + task ------------------------------------
            proj = c.post(
                "/api/v1/projects",
                json={"name": f"ProdVerify {stamp}", "description": "live verification"},
                headers=ha,
            )
            assert proj.status_code == 201, proj.text
            pid = proj.json()["id"]
            goal = (
                "Create a Python module that calculates the factorial of 6 "
                "and verify that the result is 720."
            )
            task = c.post(
                "/api/v1/tasks",
                json={
                    "title": "Prod verify factorial",
                    "description": "production-like live verification run",
                    "goal": goal,
                    "project_id": pid,
                },
                headers=ha,
            )
            assert task.status_code == 201, task.text
            tid = task.json()["id"]
            created_task_dirs.append(tid)
            ok("project + task created", tid)

            # ---- 6. async execution + duplicate guard -----------------
            ex = c.post(f"/api/v1/tasks/{tid}/execute", json={"async_mode": True}, headers=ha)
            assert ex.status_code == 202, (ex.status_code, ex.text)
            assert ex.json()["status"] == "executing", ex.json()
            dup = c.post(f"/api/v1/tasks/{tid}/execute", json={"async_mode": True}, headers=ha)
            assert dup.status_code == 202 and dup.json()["detail"] == "execution already in progress", dup.text
            ok("async execute accepted; duplicate guarded (202)")

            t = poll_terminal(c, tid, ha)
            ok("pipeline reached terminal state", f"status={t['status']} outcome={t.get('outcome')}")

            # ---- 7. DoD verification + honest outcome -----------------
            res = t.get("result") or {}
            assert res.get("verification"), "verification block missing"
            assert "definition_of_done" in res, "DoD block missing"
            assert t.get("outcome") in ("verified", "completed_unverified", "failed"), t.get("outcome")
            ok("DoD verification + honest outcome recorded", str(t.get("outcome")))

            # ---- 8. artifact registration + downloads ------------------
            arts = c.get(f"/api/v1/tasks/{tid}/artifacts", headers=ha).json()["artifacts"]
            assert arts, "no artifacts registered"
            one = c.get(f"/api/v1/tasks/{tid}/artifacts/0", headers=ha)
            assert one.status_code == 200 and len(one.content) > 0
            ok("artifact registry + single download", f"{len(arts)} artifact(s)")

            bundle = c.get(f"/api/v1/tasks/{tid}/artifacts/bundle/download", headers=ha)
            assert bundle.status_code == 200 and bundle.headers["content-type"] == "application/zip"
            zf = zipfile.ZipFile(io.BytesIO(bundle.content))
            names = zf.namelist()
            assert names and all("/" not in n and ".." not in n for n in names), names
            ok("task ZIP bundle", f"{len(names)} entr(ies): {', '.join(names[:4])}")

            # ---- 9. run history (execution_runs) ----------------------
            hist_raw = c.get(f"/api/v1/tasks/{tid}/artifacts/history", headers=ha)
            assert hist_raw.status_code == 200, hist_raw.text
            runs = hist_raw.json()["runs"]
            assert runs, "no execution_runs rows"
            done_runs = [r for r in runs if r.get("finished_at")]
            assert done_runs, runs
            assert all(r.get("status") != "running" for r in runs), "a run stayed running!"
            for leak in (SECRET, "raivisox_prodverify", "storage_path", "workspaces"):
                assert leak not in hist_raw.text, f"history leaked {leak}"
            ok("run history", f"{len(runs)} run(s), terminal, leak-free")

            # ---- 10. project export ------------------------------------
            pb = c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download", headers=ha)
            assert pb.status_code == 200 and pb.headers["content-type"] == "application/zip"
            pzf = zipfile.ZipFile(io.BytesIO(pb.content))
            assert pzf.namelist()
            ok("project export ZIP", f"{len(pzf.namelist())} entr(ies)")

            # ---- 11. cross-user isolation ------------------------------
            assert c.get(f"/api/v1/tasks/{tid}", headers=hb).status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts", headers=hb).status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/history", headers=hb).status_code == 404
            assert c.get(f"/api/v1/tasks/{tid}/artifacts/bundle/download", headers=hb).status_code == 404
            assert c.get(f"/api/v1/projects/{pid}", headers=hb).status_code == 404
            assert c.get(f"/api/v1/projects/{pid}/artifacts/bundle/download", headers=hb).status_code == 404
            ok("cross-user isolation (6 foreign surfaces -> 404)")

            # ---- 12. crash / orphan handling across hard restart -------
            orphan = c.post(
                "/api/v1/tasks",
                json={"title": "Orphan victim", "goal": goal},
                headers=ha,
            )
            tid3 = orphan.json()["id"]
            created_task_dirs.append(tid3)
            ex3 = c.post(f"/api/v1/tasks/{tid3}/execute", json={"async_mode": True}, headers=ha)
            assert ex3.status_code == 202, ex3.text
            time.sleep(0.3)
            stop_server(server, hard=True)  # simulate crash mid-execution
            print("hard-killed server mid-execution", flush=True)

            server = start_server()
            with httpx.Client(base_url=BASE, timeout=60) as c2:
                wait_live(c2)
                t3 = c2.get(f"/api/v1/tasks/{tid3}", headers=ha).json()
                assert t3["status"] == "failed", f"orphan not failed: {t3['status']}"
                msg = (t3.get("error") or {}).get("message", "")
                assert "interrupted by server restart" in msg, msg
                # No task anywhere may be stuck in an active status.
                listing = c2.get("/api/v1/tasks", headers=ha).json()["tasks"]
                stuck = [x["id"] for x in listing if x["status"] in ("executing", "verifying", "recovering")]
                assert not stuck, f"stuck active tasks after restart: {stuck}"
                h3 = c2.get(f"/api/v1/tasks/{tid3}/artifacts/history", headers=ha)
                if h3.status_code == 200:
                    assert all(r.get("status") != "running" for r in h3.json()["runs"])
                    assert all(r.get("finished_at") for r in h3.json()["runs"])
                # readiness after restart
                r = c2.get("/health/ready")
                assert r.status_code == 200 and r.json()["status"] == "ready"
            ok("crash + restart: orphan failed honestly, runs closed, no stuck state")

        # ---- 13. graceful shutdown + log hygiene ----------------------
        stop_server(server, hard=False)
        server = None
        log = open(LOG, encoding="utf-8", errors="replace").read()
        assert SECRET not in log, "SECRET_KEY leaked into logs"
        assert "SECURITY WARNING" not in log, "dev secret warning in production"
        assert "pipeline terminal task_id=" in log, "structured terminal log missing"
        assert "shutting down" in log and "database connections closed" in log
        assert "startup sweep interrupted_tasks=" in log, "orphan sweep did not run"
        ok("graceful shutdown + structured logs, no secret leakage")

        print(f"\nALL {PASS_COUNT} LIVE VERIFICATION CHECKS PASSED", flush=True)
        # Success-only cleanup — on failure the DB + log stay for diagnosis.
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
        return 0
    finally:
        try:
            if server is not None and server.poll() is None:
                stop_server(server, hard=True)
        except Exception:
            pass


def uuid4():
    import uuid

    return uuid.uuid4().hex[:8]


if __name__ == "__main__":
    sys.exit(main())
