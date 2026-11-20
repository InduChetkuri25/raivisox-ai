#!/usr/bin/env python3
"""Production isolation gate — end-to-end evidence for the execution boundary.

Run on a Docker-capable Linux host (the CI ``production-isolation-gate`` job):

    python tests/integration/production_isolation_gate.py

It is deliberately NOT a pytest module (the filename does not match
``test_*.py``): it needs a real container runtime plus the built images, and it
must fail *loudly* rather than skip. Nothing here is simulated — no mocked
runtime, no faked outputs.

Host mode:
  1. builds the production API image and the sandbox-worker image from a clean
     checkout (repo-root build context),
  2. proves the API image imports ``services.*`` / ``integrations.*`` / ``app``
     and that production startup validation works inside it,
  3. proves the API image carries no container-runtime client,
  4. starts the dedicated worker (the only service with a runtime socket),
  5. runs the client program *inside the API image* over the real network hop,
  6. asserts no sandbox container outlives the run.

``--in-container`` mode is that client program: it exercises
``build_default_sandbox()`` (the API's real doorway) and the signed HTTP
protocol, including the negative cases.

Isolation verified (each asserted, not assumed): non-root uid, no rootfs
writes, network denial, no host filesystem exposure, no runtime socket in the
API, bounded stdout, memory/timeout enforcement and cleanup, cancellation,
authentication refusals, replay refusal, duplicate-execution idempotency.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Host mode runs from tests/integration/; container mode mounts this file at
# /gate.py, where there is no repository — /. Only host mode uses REPO.
_SELF = Path(__file__).resolve()
REPO = _SELF.parents[2] if len(_SELF.parents) > 2 else Path("/app")
API_IMAGE = "raivisox-gate-api:local"
WORKER_IMAGE = "raivisox-gate-worker:local"
NETWORK = "raivisox-gate-net"
WORKER_NAME = "raivisox-gate-worker"
TOKEN = "gate-" + "a1b2c3d4" * 8          # 48+ chars, obviously not a real secret
WORKER_PORT = 8100
WORK_ROOT = "/app/workspaces/tasks"
SANDBOX_DIGEST = (
    "python:3.12-slim@sha256:"
    "a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
)

_FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        _FAILURES.append(f"{name}: {detail}")


def run(argv: list[str], *, check_rc: bool = True, timeout: int = 900,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    print("+ " + " ".join(argv), flush=True)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
        print(tail, flush=True)
        if check_rc:
            raise SystemExit(f"command failed ({proc.returncode}): {' '.join(argv)}")
    return proc


def _startup_cmd(extra_env: list[str]) -> list[str]:
    """The production import probe inside the API image (no DB connection)."""
    return [
        "docker", "run", "--rm", "--entrypoint", "python",
        "-e", "ENVIRONMENT=production",
        "-e", "SECRET_KEY=" + "ab12" * 12,
        "-e", "DATABASE_URL=postgresql+asyncpg://u:p@postgres:5432/raivisox",
        "-e", "CORS_ORIGINS=https://app.example.test",
        "-e", "ALLOWED_HOSTS=api.example.test",
        "-e", "RAIVISOX_LLM_BASE_URL=http://127.0.0.1:11434/v1",
        *extra_env,
        API_IMAGE, "-c", "import app.main;print('BOOTED')",
    ]


# ---------------------------------------------------------------------------
# In-container client program
# ---------------------------------------------------------------------------

async def _client_main() -> int:
    import asyncio

    import httpx

    from services.execution.remote_sandbox import WorkerSandbox
    from services.execution.sandbox import build_default_sandbox
    from services.execution.worker_protocol import (
        AUTH_HEADER,
        NONCE_HEADER,
        PROTOCOL_VERSION,
        TIMESTAMP_HEADER,
        ExecutionRequest,
        sign_request,
    )

    url = os.environ["SANDBOX_WORKER_URL"]
    token = os.environ["SANDBOX_WORKER_TOKEN"]
    from services.execution.worker_protocol import WorkerLimits

    limits = WorkerLimits(workspace_root=WORK_ROOT, max_timeout_s=120.0,
                          max_memory_bytes=512 * 1024 * 1024)
    task_id, run_id = "task-gate", "run-gate"
    sb = build_default_sandbox()
    check("API doorway selects the worker backend (no process fallback)",
          type(sb).__name__ == "WorkerSandbox", type(sb).__name__)

    seq = {"n": 0}

    def spec(argv: list[str], *, timeout_s: float = 30, sub: str = "",
             task: str | None = None) -> object:
        from services.execution.sandbox import SandboxSpec

        seq["n"] += 1
        scope = f"{WORK_ROOT}/{task or task_id}/{sub or f'run-{seq['n']}'}"
        os.makedirs(scope, exist_ok=True)
        return SandboxSpec(
            argv=argv, cwd=scope,
            env={"RAIVISOX_TASK_ID": task or task_id,
                 "RAIVISOX_RUN_ID": run_id,
                 "RAIVISOX_STEP_ID": f"step-{seq['n']}"},
            timeout_s=timeout_s,
            output_limit_bytes=1_048_576,
        )

    # -- 1. the workspace the sandbox sees is the one the API sees ----------
    # A canary written on the API side must be readable inside the container:
    # this is what proves the bind source maps to the same bytes (a wrong
    # translation binds an empty host directory and the run silently sees
    # nothing, which is exactly the failure this check exists to catch).
    first = spec(["true"])
    Path(first.cwd, "canary.txt").write_text("canary", encoding="utf-8")
    res = await sb.run(spec([
        "python", "-c", "print(open('canary.txt').read());print(2+2)",
    ], sub=Path(first.cwd).name))
    check("the run sees the API's workspace (bind source maps correctly)",
          res.exit_code == 0 and "canary" in res.stdout,
          f"exit={res.exit_code} out={res.stdout!r} err={res.stderr[-300:]!r}")
    check("harmless run executes and returns bounded stdout",
          res.exit_code == 0 and res.stdout.strip().endswith("4"),
          f"exit={res.exit_code} out={res.stdout!r} err={res.stderr[-300:]!r}")
    check("worker reports enforced isolation flags",
          {"network_deny", "cap_drop_all", "no_new_privileges",
           "fs_confinement"} <= set(res.enforced),
          ",".join(sorted(res.enforced)))

    # -- 2. artifacts ------------------------------------------------------
    run_dir = f"{WORK_ROOT}/{task_id}/artifacts-{uuid.uuid4().hex[:6]}"
    os.makedirs(run_dir, exist_ok=True)
    res = await sb.run(spec(["python", "-c",
                             "open('out.txt','w').write('artifact-body')"],
                            sub=Path(run_dir).name))
    paths = {a.get("path") for a in sb.last_artifacts}
    check("run artifacts are returned as a contained manifest",
          res.exit_code == 0 and "out.txt" in paths,
          f"exit={res.exit_code} paths={sorted(paths)} notes={res.limits_hit}")
    check("artifact manifest carries a digest and no bytes",
          all(len(a.get("sha256", "")) == 64 for a in sb.last_artifacts))

    # -- 3. non-root -------------------------------------------------------
    res = await sb.run(spec(["id", "-u"]))
    check("sandbox container runs non-root", res.stdout.strip() == "1000",
          res.stdout.strip())

    # -- 4. network deny ---------------------------------------------------
    res = await sb.run(spec([
        "python", "-c",
        "import socket;socket.create_connection(('1.1.1.1',53),timeout=3);print('CONNECTED')",
    ]))
    check("outbound network is denied", res.exit_code != 0
          and "CONNECTED" not in res.stdout,
          f"exit={res.exit_code} err={res.stderr[-200:]!r}")

    # -- 5. no host filesystem exposure -----------------------------------
    res = await sb.run(spec([
        "python", "-c",
        "import os,sys;"
        "bad=[p for p in ('/app/services','/var/run/docker.sock','/app/app') if os.path.exists(p)];"
        "print('LEAK:'+','.join(bad) if bad else 'CLEAN')",
    ]))
    check("host and worker filesystems are not visible",
          "CLEAN" in res.stdout, res.stdout.strip())

    # -- 6. read-only rootfs ----------------------------------------------
    res = await sb.run(spec([
        "python", "-c",
        "open('/rootfs-escape.txt','w').write('x');print('WROTE')",
    ]))
    check("root filesystem is read-only", res.exit_code != 0
          and "WROTE" not in res.stdout,
          f"exit={res.exit_code} err={res.stderr[-200:]!r}")

    # -- 7. workspace scope ------------------------------------------------
    sibling = f"{WORK_ROOT}/{task_id}/sibling-secret"
    os.makedirs(sibling, exist_ok=True)
    Path(sibling, "secret.txt").write_text("sibling", encoding="utf-8")
    res = await sb.run(spec([
        "python", "-c",
        "import os;print(sorted(os.listdir('/raivisox/workspace')))",
    ]))
    check("only the run's own subtree is mounted",
          "sibling-secret" not in res.stdout and "secret.txt" not in res.stdout,
          res.stdout.strip())

    # -- 8. memory limit ---------------------------------------------------
    res = await sb.run(spec([
        "python", "-c", "x=bytearray(700*1024*1024);print('ALLOCATED')",
    ]))
    check("memory ceiling terminates the run",
          res.exit_code != 0 and "ALLOCATED" not in res.stdout,
          f"exit={res.exit_code} killed={res.killed_reason}")

    # -- 9. timeout --------------------------------------------------------
    res = await sb.run(spec(["python", "-c", "import time;time.sleep(120)"],
                            timeout_s=2))
    check("wall-clock timeout kills the run",
          res.killed_reason == "timeout" and res.exit_code is None,
          f"killed={res.killed_reason} exit={res.exit_code}")

    # -- 10. output bounding ----------------------------------------------
    res = await sb.run(spec([
        "python", "-c", "print('x'*2_000_000)",
    ]))
    check("stdout capture is bounded",
          len(res.stdout) <= 1_048_576 + 100,
          f"len={len(res.stdout)}")

    # -- 11. authentication negatives --------------------------------------
    def post(path: str, body: dict, *, headers: dict | None = None) -> httpx.Response:
        raw = json.dumps(body).encode()
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        base = {
            TIMESTAMP_HEADER: ts, NONCE_HEADER: nonce,
            AUTH_HEADER: sign_request(token, ts, nonce, raw),
            "Content-Type": "application/json",
        }
        if headers is not None:
            base = headers
        return httpx.post(f"{url}{path}", content=raw, headers=base, timeout=30)

    request = ExecutionRequest(
        request_id=str(uuid.uuid4()), task_id=task_id, run_id=run_id,
        step_id="auth", argv=["true"], cwd=f"{WORK_ROOT}/{task_id}/auth",
        env={}, timeout_s=5,
    ).to_payload()

    resp = post("/v1/execute", request, headers={"Content-Type": "application/json"})
    check("unauthenticated request is refused", resp.status_code == 401,
          str(resp.status_code))

    raw = json.dumps(request).encode()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    resp = post("/v1/execute", request, headers={
        TIMESTAMP_HEADER: ts, NONCE_HEADER: nonce,
        AUTH_HEADER: sign_request(token, ts, nonce, raw),
        "Content-Type": "application/json",
    })
    # Same headers, same body: the nonce was consumed → replay refused.
    resp2 = post("/v1/execute", request, headers={
        TIMESTAMP_HEADER: ts, NONCE_HEADER: nonce,
        AUTH_HEADER: sign_request(token, ts, nonce, raw),
        "Content-Type": "application/json",
    })
    check("replayed request is refused (first accepted, second 401)",
          resp.status_code == 200 and resp2.status_code == 401,
          f"{resp.status_code}/{resp2.status_code}")

    resp = post("/v1/execute", request, headers={
        TIMESTAMP_HEADER: str(int(time.time()) - 4000),
        NONCE_HEADER: uuid.uuid4().hex,
        AUTH_HEADER: "0" * 64,
        "Content-Type": "application/json",
    })
    check("stale/forged signature is refused", resp.status_code == 401,
          str(resp.status_code))

    # -- 12. idempotency ---------------------------------------------------
    idem_cwd = f"{WORK_ROOT}/{task_id}/idem"
    os.makedirs(idem_cwd, exist_ok=True)
    req = ExecutionRequest(
        request_id=str(uuid.uuid4()), task_id=task_id, run_id=run_id,
        step_id="idem", argv=["python", "-c",
                              "open('marker.txt','a').write('x')"],
        cwd=idem_cwd, env={}, timeout_s=10,
    ).to_payload()
    first = post("/v1/execute", req)
    second = post("/v1/execute", req)
    marker = Path(idem_cwd, "marker.txt")
    lines = marker.read_text(encoding="utf-8").count("x") if marker.exists() else 0
    check("duplicate request_id replays instead of executing twice",
          first.status_code == 200 and second.status_code == 200
          and second.json().get("replayed") is True and lines == 1,
          f"{first.status_code}/{second.status_code} replayed="
          f"{second.json().get('replayed')} executions={lines}")

    # -- 13. cancellation --------------------------------------------------
    cancel_cwd = f"{WORK_ROOT}/{task_id}/cancel"
    os.makedirs(cancel_cwd, exist_ok=True)
    cancel_req = ExecutionRequest(
        request_id=str(uuid.uuid4()), task_id=task_id, run_id=run_id,
        step_id="cancel", argv=["python", "-c", "import time;time.sleep(120)"],
        cwd=cancel_cwd, env={}, timeout_s=110,
    ).to_payload()
    cancel_id = cancel_req["request_id"]

    async with httpx.AsyncClient(timeout=120) as client:
        raw = json.dumps(cancel_req).encode()
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        headers = {TIMESTAMP_HEADER: ts, NONCE_HEADER: nonce,
                   AUTH_HEADER: sign_request(token, ts, nonce, raw),
                   "Content-Type": "application/json"}
        pending = asyncio.create_task(
            client.post(f"{url}/v1/execute", content=raw, headers=headers))
        await asyncio.sleep(2.0)
        c = await client.post(f"{url}/v1/execute/{cancel_id}/cancel")
        body = await pending
    state = (body.json() or {}).get("state") if body.status_code == 200 else None
    check("cancellation stops the run and is reported",
          c.status_code == 200 and state == "cancelled",
          f"cancel={c.status_code} state={state} body={body.status_code}")

    # -- 14. the API process has no runtime access -------------------------
    check("API container has no runtime socket",
          not os.path.exists("/var/run/docker.sock"))
    check("API container cannot run docker",
          shutil.which("docker") is None)

    # -- 15. never a process fallback --------------------------------------
    dead = WorkerSandbox("http://127.0.0.1:1", token)
    res = await dead.run(spec(["python", "-c", "print('nope')"]))
    check("unreachable worker fails closed (no ProcessSandbox fallback)",
          res.exit_code is None and res.killed_reason == "supervisor"
          and "unreachable" in (res.error or ""),
          (res.error or "")[:160])

    return 1 if _FAILURES else 0


def in_container() -> int:
    import asyncio

    rc = asyncio.run(_client_main())
    if _FAILURES:
        print("FAILURES:", flush=True)
        for f in _FAILURES:
            print(" - " + f, flush=True)
    return rc


# ---------------------------------------------------------------------------
# Host orchestration
# ---------------------------------------------------------------------------

def _cleanup() -> None:
    subprocess.run(["docker", "rm", "-f", WORKER_NAME], capture_output=True)
    subprocess.run(["docker", "network", "rm", NETWORK], capture_output=True)
    leftovers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=raivisox-sbx-", "--format", "{{.Names}}"],
        capture_output=True, text=True)
    for name in (leftovers.stdout or "").split():
        check(f"sandbox container removed: {name}", False, "still present")
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def host_main() -> int:
    if shutil.which("docker") is None:
        raise SystemExit("docker is required for the isolation gate")
    run(["docker", "info"], timeout=120)

    # 1) Build both production images from the repository root.
    run(["docker", "build", "-f", "apps/api/Dockerfile", "-t", API_IMAGE, "."],
        timeout=1200)
    check("production API image builds from a clean checkout", True)
    run(["docker", "build", "-f", "apps/worker/Dockerfile", "-t", WORKER_IMAGE, "."],
        timeout=1200)
    check("sandbox-worker image builds from a clean checkout", True)

    # 2) Imports resolve inside the image; no dev material copied in.
    probe = (
        "import app.main, services.execution.sandbox, integrations.adapters.builtin,"
        " services.dod.dod_checks;"
        "import os;"
        "print('IMPORTS_OK');"
        "print('TESTS_PRESENT' if os.path.isdir('/app/tests') else 'NO_TESTS');"
        "print('ENV_PRESENT' if os.path.exists('/app/.env') else 'NO_ENV')"
    )
    proc = run(["docker", "run", "--rm", "--entrypoint", "python", API_IMAGE,
                "-c", probe], timeout=300)
    check("image imports app + services + integrations",
          "IMPORTS_OK" in proc.stdout, proc.stdout.strip()[-200:])
    check("image contains no tests/docs", "NO_TESTS" in proc.stdout)
    check("image contains no .env secret file", "NO_ENV" in proc.stdout)

    # 3) Startup validation inside the image (fail closed, then valid).
    proc = run(_startup_cmd([]), check_rc=False, timeout=300)
    check("production startup refuses an undeclared sandbox backend",
          proc.returncode != 0 and "SANDBOX_BACKEND" in (proc.stdout + proc.stderr),
          f"rc={proc.returncode}")

    proc = run(_startup_cmd([
        "-e", "SANDBOX_BACKEND=worker",
        "-e", f"SANDBOX_WORKER_URL=http://{WORKER_NAME}:{WORKER_PORT}",
        "-e", f"SANDBOX_WORKER_TOKEN={TOKEN}",
    ]), check_rc=False, timeout=300)
    check("production startup accepts the complete worker configuration",
          proc.returncode == 0 and "BOOTED" in proc.stdout,
          f"rc={proc.returncode} {(proc.stderr or '')[-300:]}")

    proc = run(_startup_cmd([
        "-e", "SANDBOX_BACKEND=worker",
        "-e", f"SANDBOX_WORKER_URL=http://{WORKER_NAME}:{WORKER_PORT}",
        "-e", "SANDBOX_WORKER_TOKEN=short",
    ]), check_rc=False, timeout=300)
    check("production startup refuses a weak worker key",
          proc.returncode != 0 and "SANDBOX_WORKER_TOKEN" in (proc.stdout + proc.stderr),
          f"rc={proc.returncode}")

    # 4) Start the worker — the only service with a runtime socket.
    workspace = REPO / ".gate-workspaces"
    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "tasks" / "task-gate").mkdir(parents=True)
    for sub in ("run-1", "auth", "idem", "cancel"):
        d = workspace / "tasks" / "task-gate" / sub
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace, 0o777)
    for dirpath, dirnames, _files in os.walk(workspace):
        os.chmod(dirpath, 0o777)
        for d in dirnames:
            os.chmod(os.path.join(dirpath, d), 0o777)

    _cleanup()
    run(["docker", "network", "create", NETWORK], check_rc=False)
    run([
        "docker", "run", "-d", "--name", WORKER_NAME, "--network", NETWORK,
        "-v", f"{workspace}:/app/workspaces",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-e", f"SANDBOX_WORKER_TOKEN={TOKEN}",
        "-e", f"SANDBOX_WORKER_WORKSPACE_ROOT={WORK_ROOT}",
        # The daemon resolves bind sources on the HOST, so the worker gets the
        # host location of exactly what it sees at SANDBOX_WORKER_WORKSPACE_ROOT.
        "-e", f"SANDBOX_HOST_WORKSPACE_ROOT={workspace}/tasks",
        "-e", "SANDBOX_CONTAINER_USER=1000:1000",
        "-e", f"SANDBOX_CONTAINER_IMAGE={SANDBOX_DIGEST}",
        "-e", "SANDBOX_WORKER_MAX_MEMORY_BYTES=536870912",
        "-e", "SANDBOX_WORKER_MAX_TIMEOUT_S=120",
        WORKER_IMAGE,
    ], timeout=300)
    check("worker container started", True)

    ready = False
    for _ in range(60):
        st = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", WORKER_NAME],
            capture_output=True, text=True)
        if (st.stdout or "").strip() == "healthy":
            ready = True
            break
        time.sleep(2)
    check("worker is healthy and has a reachable runtime", ready)

    # 5) The real API→worker path, executed from inside the API image.
    client_cmd = [
        "docker", "run", "--rm", "--network", NETWORK,
        "-v", f"{workspace}:/app/workspaces",
        "-v", f"{REPO / 'tests/integration/production_isolation_gate.py'}:/gate.py:ro",
        "-e", f"SANDBOX_WORKER_URL=http://{WORKER_NAME}:{WORKER_PORT}",
        "-e", f"SANDBOX_WORKER_TOKEN={TOKEN}",
        "-e", "SANDBOX_BACKEND=worker",
        "-e", "ENVIRONMENT=production",
        "-e", "PYTHONPATH=/app",
        API_IMAGE, "python", "/gate.py", "--in-container",
    ]
    proc = run(client_cmd, check_rc=False, timeout=900)
    check("API→worker end-to-end client scenarios", proc.returncode == 0,
          f"rc={proc.returncode}")

    # 6) No sandbox container outlives the runs.
    time.sleep(3)
    leftovers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=raivisox-sbx-", "--format", "{{.Names}}"],
        capture_output=True, text=True)
    names = (leftovers.stdout or "").split()
    check("no sandbox container survives success/timeout/cancel",
          not names, ",".join(names))

    return 1 if _FAILURES else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-container", action="store_true")
    args = parser.parse_args()
    try:
        return in_container() if args.in_container else host_main()
    finally:
        if not args.in_container:
            _cleanup()
            if _FAILURES:
                print("\n=== ISOLATION GATE FAILED ===", flush=True)
                for failure in _FAILURES:
                    print(" - " + failure, flush=True)
            else:
                print("\n=== ISOLATION GATE PASSED ===", flush=True)


if __name__ == "__main__":
    sys.exit(main())
