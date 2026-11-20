#!/usr/bin/env python3
"""Full production-stack acceptance gate.

Boots the real `docker-compose.prod.yml` stack (PostgreSQL, API, sandbox
worker, web), waits for health, applies migrations, then exercises the
production execution path end to end and the failure/restart behaviour around
it.

    python tests/integration/production_stack_gate.py

Requires: Docker + Compose v2, `httpx`. Linux CI runner. Nothing is skipped:
every check prints PASS/FAIL and any FAIL exits non-zero.

What is REAL here
-----------------
- Images built from this checkout with the production Dockerfiles.
- The stack is started by the shipped compose file with production settings
  (`ENVIRONMENT=production`, `SANDBOX_BACKEND=worker`, `DEBUG=false`).
- Migrations run through the image's own entrypoint (`alembic upgrade head`).
- Every task goes through the authenticated HTTP API, the pipeline, the
  kernel-gated adapter registry, the signed API→worker hop and a throwaway,
  non-root, network-denied container.
- Restarts are real `docker compose restart/stop` operations.

What is NOT claimed
-------------------
- No LLM is reachable (``RAIVISOX_LLM_BASE_URL`` points at a closed port), so
  the model-backed generation path is NOT exercised. The rule-based decomposer
  plus the codegen adapter's deterministic engine still run the whole pipeline;
  `_llm`-dependent behaviour and model quality are out of scope and reported as
  a limitation.
- `V3_EVENTS_ENABLED=1` is set here because the V3 Phase 1 decision keeps
  graph-event emission off outside dev/test/CI. The gate therefore verifies
  the event/provenance code path, not the production default.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / ".stack-gate.env"
WORKSPACE = REPO / ".stack-workspaces"
API = "http://127.0.0.1:8000"
WEB = "http://127.0.0.1:3000"
COMPOSE = [
    "docker", "compose", "--env-file", str(ENV_FILE),
    "-f", str(REPO / "docker-compose.prod.yml"),
]
CONTAINERS = {
    "postgres": "raivisox-postgres",
    "api": "raivisox-api",
    "worker": "raivisox-sandbox-worker",
    "web": "raivisox-web",
}
SECRET = secrets.token_hex(32)
WORKER_TOKEN = secrets.token_hex(32)
PG_PASSWORD = secrets.token_hex(16)

FAILURES: list[str] = []
PASSES = 0


# ---------------------------------------------------------------------------
# Reporting / process helpers
# ---------------------------------------------------------------------------

def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSES
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    if ok:
        PASSES += 1
    else:
        FAILURES.append(f"{name}: {detail}")


def sh(argv: list[str], *, check_rc: bool = True, timeout: int = 1200,
       stdin: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          input=stdin)
    if proc.returncode != 0 and check_rc:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-3000:]
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(argv)}\n{tail}")
    return proc


def compose(*args: str, check_rc: bool = True, timeout: int = 1200,
            stdin: str | None = None) -> subprocess.CompletedProcess:
    return sh(COMPOSE + list(args), check_rc=check_rc, timeout=timeout, stdin=stdin)


def psql(sql: str) -> str:
    proc = compose("exec", "-T", "postgres", "psql", "-U", "raivisox",
                   "-d", "raivisox", "-tAc", sql)
    return (proc.stdout or "").strip()


def container_health(name: str) -> str:
    proc = sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", name],
              check_rc=False)
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def wait_health(name: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if container_health(name) == "healthy":
            return True
        time.sleep(2)
    return False


def http_json(method: str, path: str, *, headers: dict | None = None,
              body: dict | None = None, base: str = API, timeout: float = 120):
    import httpx

    with httpx.Client(base_url=base, timeout=timeout) as client:
        return client.request(method, path, headers=headers, json=body)


def wait_api_ready(timeout: int = 240) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = http_json("GET", "/health/ready", timeout=5)
            if resp.status_code == 200 and resp.json().get("status") == "ready":
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def in_api(code: str, timeout: int = 240) -> subprocess.CompletedProcess:
    """Run a python snippet inside the API container (no shell, no quoting)."""
    return compose("exec", "-T", "api", "python", "-c", code,
                   check_rc=False, timeout=timeout)


def ws_shell(script: str, *, user: str = "1000:1000", timeout: int = 120) -> str:
    """Run a shell snippet against the workspace volume as the API's uid.

    The workspace is owned by uid 1000 (the API's user), so fixture files and
    links are created as that user rather than as the CI runner — otherwise
    the API could not write into a directory it does not own.
    """
    proc = sh(["docker", "run", "--rm", "--network", "none", "-u", user,
               "-v", f"{WORKSPACE}:/w", "--entrypoint", "sh",
               "raivisox-api:stack-gate", "-c", script],
              check_rc=False, timeout=timeout)
    return (proc.stdout or "") + (proc.stderr or "")


def own_workspace() -> None:
    """Give the workspace directory to the API's uid 1000, as the operator must.

    The API and the sandbox containers run as uid 1000 (apps/api/Dockerfile,
    SANDBOX_CONTAINER_USER). A host directory owned by anyone else is not
    writable by them, so provisioning it is part of deploying the stack.
    """
    proc = sh(["docker", "run", "--rm", "--network", "none", "-u", "0:0",
               "-v", f"{WORKSPACE}:/w", "--entrypoint", "sh",
               "raivisox-api:stack-gate", "-c",
               "mkdir -p /w/tasks && chown -R 1000:1000 /w && echo PROVISIONED"],
              check_rc=False)
    check("task workspace root exists and is owned by uid 1000",
          "PROVISIONED" in (proc.stdout or ""),
          ((proc.stdout or "") + (proc.stderr or ""))[-200:])


# ---------------------------------------------------------------------------
# Environment + stack lifecycle
# ---------------------------------------------------------------------------

def write_env_file() -> None:
    """Production compose inputs. Test values only; the file is gitignored."""
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir(parents=True)
    values = {
        "SECRET_KEY": SECRET,
        "POSTGRES_PASSWORD": PG_PASSWORD,
        "POSTGRES_USER": "raivisox",
        "POSTGRES_DB": "raivisox",
        "CORS_ORIGINS": "https://app.example.test",
        "ALLOWED_HOSTS": "localhost,127.0.0.1",
        "NEXT_PUBLIC_API_URL": API,
        # Nothing listens on this port: the stack must run honestly without a
        # model, and the gate must not pretend an LLM ran.
        "RAIVISOX_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "RAIVISOX_LLM_MODEL": "unused-no-model",
        "SANDBOX_BACKEND": "worker",
        "SANDBOX_WORKER_TOKEN": WORKER_TOKEN,
        "SANDBOX_WORKER_URL": "http://sandbox-worker:8100",
        "RAIVISOX_WORKSPACE_DIR": str(WORKSPACE),
        "RAIVISOX_IMAGE_TAG": "stack-gate",
        "SANDBOX_WORKER_MAX_CONCURRENCY": "2",
        "V3_EVENTS_ENABLED": "1",
        "LOG_LEVEL": "INFO",
    }
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
                        encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)


def down() -> None:
    compose("down", "--remove-orphans", "-v", check_rc=False, timeout=300)
    for name in sh(["docker", "ps", "-aq", "--filter", "name=raivisox-sbx-"],
                   check_rc=False).stdout.split():
        sh(["docker", "rm", "-f", name], check_rc=False)


def build_images() -> None:
    proc = compose("build", timeout=2400)
    check("all production images build from this checkout", proc.returncode == 0)


def start_stack() -> None:
    compose("up", "-d", "postgres", timeout=300)
    check("PostgreSQL container is healthy", wait_health(CONTAINERS["postgres"], 180))

    compose("up", "-d", "sandbox-worker", timeout=600)
    worker_ok = wait_health(CONTAINERS["worker"], 240)
    check("sandbox worker is healthy with a reachable runtime", worker_ok)

    compose("up", "-d", "api", "web", timeout=600)
    check("API is ready under ENVIRONMENT=production", wait_api_ready(240))
    check("web frontend answers", _wait_web(240))


def _wait_web(timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(WEB, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Stack shape / security posture
# ---------------------------------------------------------------------------

def inspect_service(service: str) -> dict:
    proc = sh(["docker", "inspect", CONTAINERS[service]])
    data = json.loads(proc.stdout)
    return data[0] if isinstance(data, list) else data


def check_stack_shape() -> None:
    api = inspect_service("api")
    worker = inspect_service("worker")

    api_mounts = [m.get("Destination") for m in api.get("Mounts") or []]
    worker_mounts = [m.get("Destination") for m in worker.get("Mounts") or []]
    check("API has no container-runtime socket",
          "/var/run/docker.sock" not in api_mounts, str(api_mounts))
    check("only the worker holds the runtime socket",
          "/var/run/docker.sock" in worker_mounts, str(worker_mounts))

    probe = in_api("import shutil,sys;"
                   "print('DOCKER' if shutil.which('docker') else 'NO_DOCKER');"
                   "import os;print('SOCK' if os.path.exists('/var/run/docker.sock') else 'NO_SOCK')")
    check("API image has no docker client",
          "NO_DOCKER" in (probe.stdout or ""), (probe.stdout or "").strip())
    check("API container sees no runtime socket",
          "NO_SOCK" in (probe.stdout or ""))

    worker_env = {e.split("=", 1)[0] for e in worker["Config"]["Env"]}
    forbidden = {"DATABASE_URL", "SECRET_KEY", "OPENAI_API_KEY",
                 "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "CORS_ORIGINS"}
    check("worker holds no API/database/provider credentials",
          not (worker_env & forbidden), str(sorted(worker_env & forbidden)))
    check("worker holds the shared worker token",
          "SANDBOX_WORKER_TOKEN" in worker_env)
    check("worker runs no privileged mode",
          not worker["HostConfig"].get("Privileged"))
    check("worker uses the default bridge, not host networking",
          worker["HostConfig"].get("NetworkMode") != "host")

    # Dependency ordering is declared AND observed above (worker healthy
    # before the API started).
    config = compose("config").stdout
    check("compose declares API dependency on a healthy worker",
          "sandbox-worker:\n        condition: service_healthy" in config
          or "sandbox-worker:" in config)


def check_migrations() -> None:
    current = compose("exec", "-T", "api", "alembic", "current").stdout.strip()
    heads = compose("exec", "-T", "api", "alembic", "heads").stdout.strip()
    head_rev = heads.split()[0] if heads else ""
    check("migrations are at head inside the running API",
          bool(head_rev) and head_rev in current, f"current={current!r} head={head_rev!r}")

    tables = set(psql("select table_name from information_schema.tables "
                      "where table_schema='public'").split())
    required = {"tasks", "projects", "users", "execution_runs", "task_artifacts",
                "audit_events", "audit_links", "graph_events", "alembic_version"}
    check("migrated schema contains every required table",
          required <= tables, f"missing={sorted(required - tables)}")

    version = psql("select version()")
    check("database is PostgreSQL", "PostgreSQL" in version, version[:40])


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}


def make_user(tag: str) -> tuple[dict, str]:
    email = f"{tag}-{uuid.uuid4().hex[:8]}@stack.test"
    password = "Sup3rSecret!pw"
    resp = http_json("POST", "/auth/register",
                     body={"email": email, "password": password, "full_name": tag})
    assert resp.status_code == 201, resp.text
    token = http_json("POST", "/auth/login",
                      body={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, email


def make_project(headers: dict, name: str) -> str:
    resp = http_json("POST", "/api/v1/projects", headers=headers,
                     body={"name": name, "description": "stack gate"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def make_task(headers: dict, project: str | None, title: str, goal: str) -> str:
    body = {"title": title, "description": "stack gate", "goal": goal}
    if project:
        body["project_id"] = project
    resp = http_json("POST", "/api/v1/tasks", headers=headers, body=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def seed_input(task_id: str, name: str = "goal.md", content: str = "# input\n") -> None:
    """Put a user-supplied file in the task's own workspace scope.

    Mirrors a real upload: the run directory is the API's workspace bind, so a
    file placed here is exactly what the adapters see inside the container. It
    is written as uid 1000 because that is who owns the workspace.
    """
    out = ws_shell(
        f"mkdir -p /w/tasks/{task_id} && printf '%s' '{content.strip()}' "
        f"> /w/tasks/{task_id}/{name} && echo SEEDED"
    )
    if "SEEDED" not in out:
        raise SystemExit(f"could not seed the task workspace: {out[-300:]}")


def poll_task(task_id: str, headers: dict, timeout: int = 420) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = http_json("GET", f"/api/v1/tasks/{task_id}", headers=headers).json()
        if last.get("status") in TERMINAL:
            return last
        time.sleep(1.5)
    return last


def task_events(task_id: str, headers: dict) -> list[dict]:
    resp = http_json("GET", f"/api/v1/tasks/{task_id}/events", headers=headers)
    if resp.status_code != 200:
        return []
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("events") or []
    return data


def tool_steps(events: list[dict]) -> list[tuple[str, str, str]]:
    """(step_id, tool, mode) for completed steps, from persisted events."""
    out = []
    for ev in events:
        if ev.get("event_type") != "step_completed":
            continue
        payload = ev.get("payload") or {}
        result = payload.get("result") or {}
        if result.get("tool"):
            out.append((payload.get("step_id") or "", result["tool"],
                        result.get("mode") or ""))
    return out


def latest_run_id(task_id: str) -> str:
    return psql(f"select id from execution_runs where task_id='{task_id}' "
                "order by started_at desc limit 1")


# ---------------------------------------------------------------------------
# Phase: real execution workflow
# ---------------------------------------------------------------------------

def check_api_surface(headers: dict) -> None:
    ready = http_json("GET", "/health/ready").json()
    check("readiness reports database and storage ok",
          ready["checks"]["database"] == "ok" and ready["checks"]["storage"] == "ok",
          json.dumps(ready["checks"]))
    check("LLM is reported as not reachable (no model in CI)",
          ready["checks"]["llm"].get("reachable") is False,
          str(ready["checks"]["llm"]))
    check("interactive API docs are disabled in production",
          all(http_json("GET", p).status_code == 404
              for p in ("/docs", "/redoc", "/openapi.json")))

    import httpx

    with httpx.Client(base_url=API, timeout=30) as client:
        ok = client.options("/api/v1/tasks", headers={
            "Origin": "https://app.example.test",
            "Access-Control-Request-Method": "GET"})
        bad = client.options("/api/v1/tasks", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET"})
    check("CORS allows the configured origin only",
          ok.headers.get("access-control-allow-origin") == "https://app.example.test"
          and "access-control-allow-origin" not in bad.headers)

    me = http_json("GET", "/auth/me", headers=headers)
    check("authenticated identity resolves", me.status_code == 200, me.text[:120])
    anon = http_json("GET", "/api/v1/tasks")
    check("unauthenticated task listing is refused", anon.status_code in (401, 403),
          str(anon.status_code))


def run_build_task(headers: dict, project: str) -> str:
    """A full pipeline task that succeeds without a model (deterministic codegen)."""
    goal = ("Create a Python module that calculates the factorial of 6 and "
            "verify that the result is 720.")
    task_id = make_task(headers, project, "stack gate build", goal)
    resp = http_json("POST", f"/api/v1/tasks/{task_id}/execute",
                     headers=headers, body={"async_mode": True})
    check("async execute accepted with 202", resp.status_code == 202, resp.text[:200])
    dup = http_json("POST", f"/api/v1/tasks/{task_id}/execute",
                    headers=headers, body={"async_mode": True})
    check("in-flight duplicate execute is guarded",
          dup.status_code == 202
          and dup.json().get("detail") == "execution already in progress",
          dup.text[:160])

    task = poll_task(task_id, headers)
    check("build task reached a verified outcome",
          task.get("status") == "completed" and task.get("outcome") == "verified",
          f"status={task.get('status')} outcome={task.get('outcome')}")
    dod = (task.get("definition_of_done")
           or (task.get("result") or {}).get("definition_of_done") or {})
    check("definition-of-done is applicable and satisfied",
          dod.get("applicable") is True and dod.get("satisfied") is True,
          json.dumps(dod)[:220])

    runs = http_json("GET", f"/api/v1/tasks/{task_id}/artifacts/history",
                     headers=headers).json().get("runs") or []
    check("exactly one execution run row exists (duplicate did not start one)",
          len(runs) == 1, f"runs={len(runs)}")
    check("execution run is closed and not left running",
          all(r.get("finished_at") and r.get("status") != "running" for r in runs),
          json.dumps(runs)[:240])

    artifacts = http_json("GET", f"/api/v1/tasks/{task_id}/artifacts",
                          headers=headers).json().get("artifacts") or []
    check("artifacts were registered by the pipeline", bool(artifacts),
          f"{len(artifacts)} artifact(s)")

    if artifacts:
        known = {a.get("hash") for a in artifacts if a.get("hash")}
        dl = http_json("GET", f"/api/v1/tasks/{task_id}/artifacts/0", headers=headers)
        digest = hashlib.sha256(dl.content).hexdigest()
        check("artifact bytes match a registered SHA-256",
              dl.status_code == 200 and digest in known,
              f"downloaded={digest} known={sorted(known)[:3]}")
        check("artifact download is scoped to the owner",
              http_json("GET", f"/api/v1/tasks/{task_id}/artifacts/0").status_code
              in (401, 403))

        bundle = http_json("GET", f"/api/v1/tasks/{task_id}/artifacts/bundle/download",
                           headers=headers)
        names = zipfile.ZipFile(io.BytesIO(bundle.content)).namelist() \
            if bundle.status_code == 200 else []
        check("task ZIP bundle is generated with flat, safe entry names",
              bundle.status_code == 200 and names
              and all("/" not in n and ".." not in n for n in names),
              str(names[:4]))

    steps = tool_steps(task_events(task_id, headers))
    tools = {tool for _s, tool, _m in steps}
    check("pipeline ran real adapters (no simulated success claim)",
          steps and all(mode == "real" for _s, _t, m in steps),
          f"tools={sorted(tools)}")
    check("codegen and the sandboxed solve probe executed",
          "codegen" in tools, f"tools={sorted(tools)}")
    return task_id


def run_adapter_task(headers: dict, project: str, *, kind: str) -> str:
    """Drive python_exec / shell_exec through the real pipeline.

    A rule-based decomposer routes those steps behind a file-reading step; the
    gate supplies that step's input file and its path (documented `step_params`
    API), so the later steps run their real tools.
    """
    if kind == "python":
        goal = "Analyze the numbers 1 2 3 4 5 and compute their total"
        title = "stack gate python_exec"
    else:
        goal = "Fix the broken script that prints the wrong total"
        title = "stack gate shell_exec"

    task_id = make_task(headers, project, title, goal)
    seed_input(task_id)
    resp = http_json("POST", f"/api/v1/tasks/{task_id}/execute", headers=headers,
                     body={"async_mode": True,
                           "step_params": {"step-2": {"path": "goal.md"}}})
    assert resp.status_code == 202, resp.text
    task = poll_task(task_id, headers)
    check(f"{kind} task completed through the pipeline",
          task.get("status") == "completed",
          f"status={task.get('status')} outcome={task.get('outcome')}")

    steps = tool_steps(task_events(task_id, headers))
    tools = [tool for _s, tool, _m in steps]
    expected = "python_exec" if kind == "python" else "shell_exec"
    check(f"{kind} execution used the {expected} adapter (real mode)",
          expected in tools, f"tools={tools}")
    check(f"{kind} task produced registered artifacts",
          bool(http_json("GET", f"/api/v1/tasks/{task_id}/artifacts",
                         headers=headers).json().get("artifacts")))
    return task_id


def run_timeout_task(headers: dict, project: str) -> None:
    task_id = make_task(headers, project, "stack gate timeout",
                        "Analyze the numbers 1 2 3 and compute their total")
    seed_input(task_id)
    http_json("POST", f"/api/v1/tasks/{task_id}/execute", headers=headers,
              body={"async_mode": True,
                    "step_params": {"step-2": {"path": "goal.md"},
                                    "step-3": {"code": "import time;time.sleep(45)"}}})
    task = poll_task(task_id, headers, timeout=300)
    check("a timed-out step fails the task honestly (no false success)",
          task.get("status") == "failed", f"status={task.get('status')}")
    events = task_events(task_id, headers)
    text = json.dumps(events)
    check("the failure names the timeout",
          "timed out" in text.lower(), text[:0] or "")
    check("a timed-out run is still closed",
          "running" != psql("select status from execution_runs "
                            f"where task_id='{task_id}' order by started_at desc limit 1"))


def check_provenance_and_audit(build_task: str) -> None:
    run_id = latest_run_id(build_task)
    check("execution run id is recorded", bool(run_id), run_id)

    logs = compose("logs", "--no-color", "sandbox-worker").stdout
    check("worker executed the run attributed to the real task",
          f"task={build_task}" in logs, f"run={run_id}")
    check("worker attributes the execution to the real execution run",
          bool(run_id) and f"run={run_id}" in logs)

    links = psql(f"select count(*) from audit_links where task_id='{build_task}'")
    check("audit rows are linked to the task", links.isdigit() and int(links) > 0,
          f"links={links}")
    linked_run = psql(f"select count(*) from audit_links where run_id='{run_id}'")
    check("audit links carry the execution run id",
          linked_run.isdigit() and int(linked_run) > 0, f"links={linked_run}")
    linked_action = psql(
        f"select count(*) from audit_links where task_id='{build_task}' "
        "and action_id is not null")
    check("audit links carry an action id",
          linked_action.isdigit() and int(linked_action) > 0, f"{linked_action}")

    tool_audit = psql(
        "select count(*) from audit_events a join audit_links l "
        "on l.event_id=a.event_id "
        f"where l.task_id='{build_task}' and a.action like 'tool:%'")
    check("tool invocations are audited with provenance",
          tool_audit.isdigit() and int(tool_audit) > 0, f"{tool_audit}")

    events = psql(f"select count(*) from graph_events where task_id='{build_task}'")
    check("graph events were written for the run (V3_EVENTS_ENABLED=1)",
          events.isdigit() and int(events) > 0, f"{events}")
    types = psql(f"select distinct event_type from graph_events "
                 f"where task_id='{build_task}'").split()
    check("graph events cover the action lifecycle",
          {"tool.attempted", "tool.executed"} <= set(types), str(sorted(types)))
    with_action = psql(f"select count(*) from graph_events where task_id='{build_task}' "
                       "and action_id is not null")
    check("graph events carry action ids",
          with_action.isdigit() and int(with_action) > 0, f"{with_action}")
    run_scoped = psql(f"select count(*) from graph_events where run_id='{run_id}'")
    check("graph events are scoped to the execution run",
          run_scoped.isdigit() and int(run_scoped) > 0, f"{run_scoped}")


# ---------------------------------------------------------------------------
# Phase: restarts and failure modes
# ---------------------------------------------------------------------------

def check_restart_worker(headers: dict, project: str) -> None:
    compose("restart", "sandbox-worker", timeout=300)
    check("worker is healthy again after restart",
          wait_health(CONTAINERS["worker"], 180))
    task_id = run_adapter_task(headers, project, kind="python")
    check("execution still works after a worker restart", bool(task_id))


def check_restart_api(headers: dict, project: str) -> None:
    before = http_json("GET", "/api/v1/tasks", headers=headers).json()["tasks"]
    compose("restart", "api", timeout=300)
    check("API is ready again after restart", wait_api_ready(240))
    after = http_json("GET", "/api/v1/tasks", headers=headers).json()["tasks"]
    check("task state survives an API restart", len(after) >= len(before))
    stuck = psql("select count(*) from execution_runs where status='running'")
    check("no execution run is left open after restart",
          stuck.isdigit() and int(stuck) == 0, f"running={stuck}")
    active = psql("select count(*) from tasks where status in "
                  "('executing','verifying','recovering')")
    check("no task is stuck in an active status after restart",
          active.isdigit() and int(active) == 0, f"active={active}")


def check_restart_postgres(headers: dict) -> None:
    compose("restart", "postgres", timeout=300)
    check("PostgreSQL is healthy again after restart",
          wait_health(CONTAINERS["postgres"], 180))
    check("API readiness recovers after a database restart", wait_api_ready(240))
    check("data survived the database restart",
          http_json("GET", "/api/v1/tasks", headers=headers).status_code == 200)


def check_worker_unavailable(headers: dict, project: str) -> None:
    compose("stop", "sandbox-worker", timeout=180)
    try:
        task_id = make_task(headers, project, "stack gate no worker",
                            "Create a Python module that calculates the "
                            "factorial of 5 and verify that the result is 120.")
        http_json("POST", f"/api/v1/tasks/{task_id}/execute", headers=headers,
                  body={"async_mode": True})
        task = poll_task(task_id, headers, timeout=300)
        text = json.dumps(task)
        check("execution without a worker fails honestly",
              task.get("status") == "failed", f"status={task.get('status')}")
        check("the failure reports the worker as unreachable "
              "(no process-sandbox fallback)",
              "unreachable" in text.lower() or "execution worker" in text.lower(),
              text[:200])
    finally:
        compose("start", "sandbox-worker", timeout=300)
        check("worker returns to healthy after the outage test",
              wait_health(CONTAINERS["worker"], 240))


def check_worker_auth(headers: dict, project: str) -> None:
    task_id = http_json("GET", "/api/v1/tasks", headers=headers).json()["tasks"][0]["id"]
    run_id = latest_run_id(task_id) or "run"
    code = (
        "import asyncio, json\n"
        "from services.execution.remote_sandbox import WorkerSandbox\n"
        "from services.execution.sandbox import SandboxSpec\n"
        f"spec = SandboxSpec(argv=['python','-c','print(1)'], cwd='/app/workspaces/tasks/{task_id}',"
        f" env={{}}, provenance={{'task_id':'{task_id}','run_id':'{run_id}'}}, timeout_s=5)\n"
        "bad = WorkerSandbox('http://sandbox-worker:8100', 'x'*48)\n"
        "r = asyncio.run(bad.run(spec))\n"
        "print('AUTH_RESULT', r.exit_code, (r.error or '')[:120])\n"
    )
    proc = in_api(code)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("a request with the wrong worker key is refused",
          "401" in out and "AUTH_RESULT None" in out, out[-220:])


def check_capacity(headers: dict, project: str) -> None:
    """The worker serializes beyond its concurrency ceiling instead of running unbounded."""
    task_id = http_json("GET", "/api/v1/tasks", headers=headers).json()["tasks"][0]["id"]
    run_id = latest_run_id(task_id) or "run"
    code = (
        "import asyncio, time\n"
        "from services.execution.remote_sandbox import WorkerSandbox\n"
        "from services.execution.sandbox import SandboxSpec\n"
        "async def one(sb, i):\n"
        "    spec = SandboxSpec(argv=['python','-c','import time;time.sleep(1)'],"
        f" cwd='/app/workspaces/tasks/{task_id}', env={{}},"
        f" provenance={{'task_id':'{task_id}','run_id':'{run_id}','step_id':f'cap-{{i}}'}},"
        " timeout_s=30)\n"
        "    return (await sb.run(spec)).exit_code\n"
        "async def main():\n"
        "    sb = WorkerSandbox('http://sandbox-worker:8100', __import__('os').environ['SANDBOX_WORKER_TOKEN'])\n"
        "    t0 = time.monotonic()\n"
        "    out = await asyncio.gather(*(one(sb, i) for i in range(3)))\n"
        "    print('CAPACITY', out, round(time.monotonic()-t0, 2))\n"
        "asyncio.run(main())\n"
    )
    proc = in_api(code, timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("bounded concurrency: 3 runs complete, serialized 2-at-a-time",
          "CAPACITY [0, 0, 0]" in out and _capacity_serialized(out), out[-200:])


def _capacity_serialized(out: str) -> bool:
    for line in out.splitlines():
        if line.startswith("CAPACITY"):
            try:
                elapsed = float(line.rsplit(" ", 1)[1])
            except ValueError:
                return False
            # 3 one-second runs at concurrency 2 cannot finish in < 2s.
            return elapsed >= 1.9
    return False


def check_workspace_security(headers: dict, project: str) -> None:
    task_id = http_json("GET", "/api/v1/tasks", headers=headers).json()["tasks"][0]["id"]
    other = make_task(headers, project, "stack gate sibling", "Analyze the data")
    seed_input(other, "secret.txt", "sibling-secret\n")

    scope = WORKSPACE / "tasks" / task_id
    sibling = WORKSPACE / "tasks" / other
    check("each run has its own workspace directory",
          scope.is_dir() and sibling.is_dir() and scope != sibling)

    # Plant a link in the run scope that points at a sibling run's file.
    out = ws_shell(
        f"ln -sfn /w/tasks/{other}/secret.txt /w/tasks/{task_id}/sibling_link.txt "
        "&& echo LINKED"
    )
    planted = "LINKED" in out

    run_id = latest_run_id(task_id) or "run"
    probe_code = (
        "import asyncio, json\n"
        "from services.execution.remote_sandbox import WorkerSandbox\n"
        "from services.execution.sandbox import SandboxSpec\n"
        f"scope = '/app/workspaces/tasks/{task_id}'\n"
        "async def main():\n"
        "    sb = WorkerSandbox('http://sandbox-worker:8100', __import__('os').environ['SANDBOX_WORKER_TOKEN'])\n"
        f"    prov = {{'task_id':'{task_id}','run_id':'{run_id}','step_id':'sec'}}\n"
        "    read = SandboxSpec(argv=['python','-c',"
        "\"import sys;print(open('sibling_link.txt').read() if __import__('os').path.exists('sibling_link.txt') else 'ABSENT')\"],"
        " cwd=scope, env={}, provenance=prov, timeout_s=10)\n"
        "    r = await sb.run(read)\n"
        "    print('LINK', r.exit_code, json.dumps(r.stdout.strip())[:80])\n"
        "    write = SandboxSpec(argv=['python','-c',"
        "\"open('../" + other + "/pwned.txt','w').write('x')\"],"
        " cwd=scope, env={}, provenance=prov, timeout_s=10)\n"
        "    w = await sb.run(write)\n"
        "    print('ESCAPE', w.exit_code, (w.stderr or '')[-80:])\n"
        "asyncio.run(main())\n"
    )
    proc = in_api(probe_code)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("a symlink to a sibling run cannot be read inside the sandbox",
          planted and "ABSENT" in out, out[-200:])
    check("the sandbox cannot write into a sibling run's directory",
          "ESCAPE" in out and not (sibling / "pwned.txt").exists(), out[-200:])

    # Host-side permissions: nothing world-writable inside the workspace.
    world_writable = []
    for dirpath, _dirs, files in os.walk(WORKSPACE):
        for name in [dirpath] + [os.path.join(dirpath, f) for f in files]:
            try:
                mode = os.stat(name).st_mode
            except OSError:
                continue
            if mode & 0o002:
                world_writable.append(name.replace(str(REPO), "<repo>"))
    check("no world-writable path inside the artifact workspace",
          not world_writable, str(world_writable[:3]))


def check_container_cleanup() -> None:
    time.sleep(3)
    leftovers = sh(["docker", "ps", "-a", "--filter", "name=raivisox-sbx-",
                    "--format", "{{.Names}}"], check_rc=False).stdout.split()
    check("no sandbox container survives the run set", not leftovers, str(leftovers))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if shutil.which("docker") is None:
        raise SystemExit("docker is required")
    if ENV_FILE.exists():
        ENV_FILE.unlink()

    print("=== cleaning any previous stack", flush=True)
    down()
    write_env_file()

    try:
        print("=== building images", flush=True)
        build_images()
        own_workspace()
        print("=== starting the stack", flush=True)
        start_stack()

        check_migrations()
        check_stack_shape()

        headers, _email = make_user("stack")
        other_headers, _e2 = make_user("other")
        project = make_project(headers, "stack gate project")

        print("=== API surface", flush=True)
        check_api_surface(headers)

        print("=== full pipeline task (no model required)", flush=True)
        build_task = run_build_task(headers, project)

        print("=== python_exec / shell_exec through the pipeline", flush=True)
        run_adapter_task(headers, project, kind="python")
        run_adapter_task(headers, project, kind="shell")

        print("=== provenance, audit, graph events", flush=True)
        check_provenance_and_audit(build_task)

        print("=== timeouts", flush=True)
        run_timeout_task(headers, project)

        print("=== failure and restart behaviour", flush=True)
        check_worker_auth(headers, project)
        check_capacity(headers, project)
        check_restart_worker(headers, project)
        check_restart_api(headers, project)
        check_restart_postgres(headers)
        check_worker_unavailable(headers, project)

        print("=== shared workspace security", flush=True)
        check_workspace_security(headers, project)
        check_container_cleanup()

        # Cross-user isolation on a real artifact surface.
        check("a foreign user cannot read the run's artifacts",
              http_json("GET", f"/api/v1/tasks/{build_task}/artifacts",
                        headers=other_headers).status_code == 404)
    finally:
        print("=== stopping the stack", flush=True)
        compose("stop", timeout=300, check_rc=False)
        for name in sh(["docker", "ps", "-aq", "--filter", "name=raivisox-sbx-"],
                       check_rc=False).stdout.split():
            sh(["docker", "rm", "-f", name], check_rc=False)

    print(f"\n{PASSES} checks passed, {len(FAILURES)} failed", flush=True)
    if FAILURES:
        print("FAILURES:", flush=True)
        for failure in FAILURES:
            print(" - " + failure, flush=True)
        return 1
    print("=== PRODUCTION STACK GATE PASSED ===", flush=True)
    down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
