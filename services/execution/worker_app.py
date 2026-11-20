"""Dedicated sandbox execution worker — the container runtime boundary.

Deployed as its own service (docker-compose.prod.yml ``sandbox-worker``) with:

- the container runtime socket (the ONLY process that has one),
- a workspace volume shared with the API (artifact bytes stay on disk),
- a shared HMAC key (``SANDBOX_WORKER_TOKEN``),
- **no** database URL, application secret, LLM key or API credential.

The API reaches it through ``WorkerSandbox`` (``SANDBOX_BACKEND=worker``). The
protocol is in ``worker_protocol.py``; both ends import it, so they cannot
drift.

Per request the worker:

1. verifies the HMAC signature, timestamp window and nonce (replay refused),
2. parses and *clamps* the request against its own limits (a caller may ask
   for less, never more; network denial, pid/memory/cpu/fsize ceilings and a
   non-root, read-only-rootfs, cap-dropped container are not negotiable),
3. refuses a ``cwd`` that is not inside its own workspace root,
4. executes one throwaway container through :class:`ContainerSandbox`,
5. returns bounded stdout/stderr, exit status, timeout/kill reason, the flags
   actually enforced, and a bounded artifact manifest (no bytes),
6. removes the container on success, failure, timeout and cancellation.

Idempotency: the same ``request_id`` is executed at most once. A completed
request replays its stored response (``replayed: true``); one that is still
running answers 409. The store is in-memory and bounded (see
``docs/deployment.md §10.7``) — a worker restart forgets it, which is why the
API only retries on a *transport* failure and reuses the request id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.execution.container_sandbox import ContainerSandbox
from services.execution.sandbox import SandboxResult, SandboxSpec
from services.execution.sandbox_policy import validate_worker_endpoint
from services.execution.worker_protocol import (
    AUTH_HEADER,
    NONCE_HEADER,
    PROTOCOL_VERSION,
    TIMESTAMP_HEADER,
    ArtifactLimits,
    AuthReplayCache,
    ExecutionRequest,
    RequestValidationError,
    SignatureError,
    WorkerLimits,
    collect_artifacts,
    verify_request,
)
from services.security.path_containment import is_contained

__all__ = ["app", "create_app", "IDEMPOTENCY_TTL_S"]

_log = logging.getLogger("raivisox.sandbox_worker")
if not _log.handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

TOKEN_ENV = "SANDBOX_WORKER_TOKEN"
IDEMPOTENCY_TTL_S = 3600.0
IDEMPOTENCY_CAPACITY = 512


class _Idempotency:
    """Bounded TTL map: request_id → (state, response payload)."""

    def __init__(self, capacity: int = IDEMPOTENCY_CAPACITY,
                 ttl_s: float = IDEMPOTENCY_TTL_S) -> None:
        self._capacity = capacity
        self._ttl = ttl_s
        self._items: dict[str, tuple[float, str, dict[str, Any] | None]] = {}
        self._lock = asyncio.Lock()

    async def get(self, request_id: str) -> tuple[str, dict[str, Any] | None] | None:
        async with self._lock:
            self._evict()
            item = self._items.get(request_id)
            if item is None:
                return None
            return item[1], item[2]

    async def begin(self, request_id: str) -> bool:
        """Reserve ``request_id``; False when it already exists."""
        async with self._lock:
            self._evict()
            if request_id in self._items:
                return False
            if len(self._items) >= self._capacity:
                # Fail closed: refuse new work rather than lose dedup state.
                return False
            self._items[request_id] = (time.monotonic(), "running", None)
            return True

    async def finish(self, request_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._items[request_id] = (time.monotonic(), "done", payload)

    async def release(self, request_id: str) -> None:
        async with self._lock:
            self._items.pop(request_id, None)

    def _evict(self) -> None:
        now = time.monotonic()
        for key in [k for k, (ts, _s, _p) in self._items.items()
                    if now - ts > self._ttl]:
            del self._items[key]


class WorkerState:
    """Everything the worker owns — no API secrets live here."""

    def __init__(self) -> None:
        self.token = (os.environ.get(TOKEN_ENV) or "").strip()
        self.limits = WorkerLimits.from_env()
        self.workspace_root = os.path.abspath(self.limits.workspace_root)
        self.replay = AuthReplayCache()
        self.idempotency = _Idempotency()
        self.running: dict[str, asyncio.Task[Any]] = {}
        self.concurrency = asyncio.Semaphore(
            int(os.environ.get("SANDBOX_WORKER_MAX_CONCURRENCY", "2") or 2)
        )
        # The runtime daemon resolves bind sources on the HOST. This service is
        # itself a container, so its own workspace path is not a host path:
        # translate the prefix when the operator tells us where the directory
        # actually lives (`SANDBOX_HOST_WORKSPACE_ROOT` = the host path OF
        # SANDBOX_WORKER_WORKSPACE_ROOT, not of its parent). Without it the
        # paths are assumed identical (worker running directly on the host).
        host_root = (os.environ.get("SANDBOX_HOST_WORKSPACE_ROOT") or "").strip()
        bind_map = (self.workspace_root, host_root) if host_root else None
        if host_root and not os.path.isabs(host_root):
            raise RuntimeError(
                "SANDBOX_HOST_WORKSPACE_ROOT must be an absolute HOST path "
                "(the runtime daemon resolves bind sources on the host)"
            )
        self.sandbox = ContainerSandbox(bind_prefix_map=bind_map)

    def config_error(self) -> str | None:
        try:
            validate_worker_endpoint("http://worker.invalid", self.token)
        except Exception as exc:
            return str(exc)
        if not os.path.isdir(self.workspace_root):
            return (f"workspace root {self.workspace_root!r} does not exist; mount "
                    "the shared workspace volume before starting the worker")
        return None


def _result_payload(request: ExecutionRequest, result: SandboxResult,
                    artifacts: list[dict[str, Any]], notes: list[str],
                    capabilities: dict[str, bool], *, replayed: bool = False,
                    state: str = "ok") -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.request_id,
        "provenance": request.provenance,
        "state": state,
        "replayed": replayed,
        "result": {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
            "killed_reason": result.killed_reason,
            "limits_hit": list(result.limits_hit),
            "enforced": list(result.enforced),
            "error": result.error,
        },
        "artifacts": artifacts,
        "artifact_notes": notes,
        "capabilities": capabilities,
    }


def _spec_from_request(state: WorkerState, request: ExecutionRequest) -> SandboxSpec:
    """Build the sandbox spec from a validated request — nothing is optional.

    The worker owns every security-relevant field: network denial, pid/memory/
    cpu/fsize ceilings and the writable scope. Only argv, cwd, timeout and the
    allowlisted env come from the caller, and the cwd must be inside the
    worker's workspace root *after* symlink resolution.
    """
    root = state.workspace_root
    if not is_contained(request.cwd, root):
        raise RequestValidationError(
            f"spec.cwd {request.cwd!r} is outside the worker workspace root"
        )
    return SandboxSpec(
        argv=list(request.argv),
        cwd=request.cwd,
        env=dict(request.env),
        timeout_s=request.timeout_s,
        cpu_seconds=request.cpu_seconds,
        memory_bytes=request.memory_bytes,
        max_pids=request.max_pids,
        fsize_bytes=request.fsize_bytes,
        output_limit_bytes=request.output_limit_bytes,
        network="deny",          # not negotiable
        workspace_writable=request.cwd,
        workspace_readable=(request.cwd,),
    )


async def _execute(state: WorkerState, request: ExecutionRequest) -> dict[str, Any]:
    spec = _spec_from_request(state, request)
    try:
        result = await state.sandbox.run(spec)
    except asyncio.CancelledError:
        # ContainerSandbox removes the container on cancellation before
        # re-raising; report the cancellation instead of losing the request.
        return _result_payload(
            request,
            SandboxResult(error="execution cancelled by request",
                          killed_reason="supervisor",
                          limits_hit=["cancelled"], enforced=["cancellation"]),
            [], ["cancellation requested; container removed"],
            state.sandbox.capabilities(), state="cancelled",
        )
    artifacts, notes = collect_artifacts(
        spec.cwd, ArtifactLimits.from_limits(state.limits)
    )
    return _result_payload(
        request, result, [a.to_payload() for a in artifacts], notes,
        state.sandbox.capabilities(),
    )


def create_app() -> FastAPI:
    app = FastAPI(title="RAIVISOX sandbox worker", version=PROTOCOL_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    state = WorkerState()
    app.state.worker = state

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - exercised in CI/docker
        problem = state.config_error()
        if problem:
            # Fail closed: a worker without a token or workspace must not serve.
            raise RuntimeError(f"sandbox worker misconfigured: {problem}")
        _log.info("sandbox worker ready (workspace=%s protocol=%s)",
                  state.workspace_root, PROTOCOL_VERSION)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "protocol_version": PROTOCOL_VERSION})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        problem = state.config_error()
        if problem:
            return JSONResponse({"status": "not-ready", "reason": problem},
                                status_code=503)
        caps = await asyncio.to_thread(state.sandbox.capabilities)
        ready = bool(caps.get("runtime_available"))
        body = {"status": "ready" if ready else "not-ready",
                "runtime_available": ready, "protocol_version": PROTOCOL_VERSION}
        if not ready:
            body["reason"] = "container runtime not reachable from the worker"
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/v1/capabilities")
    async def capabilities() -> JSONResponse:
        caps = await asyncio.to_thread(state.sandbox.capabilities)
        return JSONResponse({
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": caps,
            "max_timeout_s": state.limits.max_timeout_s,
            "max_memory_bytes": state.limits.max_memory_bytes,
            "max_pids": state.limits.max_pids,
            "max_output_bytes": state.limits.max_output_bytes,
        })

    @app.post("/v1/execute")
    async def execute(http: Request) -> JSONResponse:
        body = await http.body()
        if len(body) > 256 * 1024:
            return JSONResponse({"detail": "request body too large"},
                                status_code=413)
        try:
            verify_request(
                state.token,
                http.headers.get(TIMESTAMP_HEADER),
                http.headers.get(NONCE_HEADER),
                http.headers.get(AUTH_HEADER),
                body,
                replay=state.replay,
            )
        except SignatureError as exc:
            # 401 for every authentication failure: no oracle for an attacker.
            _log.warning("sandbox worker auth refused: %s", exc)
            return JSONResponse({"detail": "authentication refused"},
                                status_code=401)
        try:
            payload = await http.json()
            request = ExecutionRequest.from_payload(payload, state.limits)
        except RequestValidationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

        known = await state.idempotency.get(request.request_id)
        if known is not None:
            known_state, cached = known
            if known_state == "done" and cached is not None:
                replay = dict(cached)
                replay["replayed"] = True
                return JSONResponse(replay)
            return JSONResponse(
                {"detail": "request_id is already being executed",
                 "request_id": request.request_id},
                status_code=409,
            )

        if not await state.idempotency.begin(request.request_id):
            return JSONResponse(
                {"detail": "worker is at capacity for idempotent execution; retry",
                 "request_id": request.request_id},
                status_code=429,
            )

        try:
            async with state.concurrency:
                task = asyncio.create_task(_execute(state, request))
                state.running[request.request_id] = task
                try:
                    result_payload = await task
                finally:
                    state.running.pop(request.request_id, None)
        except asyncio.CancelledError:
            await state.idempotency.release(request.request_id)
            raise
        except RequestValidationError as exc:
            await state.idempotency.release(request.request_id)
            return JSONResponse({"detail": str(exc)}, status_code=422)

        await state.idempotency.finish(request.request_id, result_payload)
        result = result_payload["result"]
        _log.info(
            "executed request=%s task=%s run=%s exit=%s killed=%s artifacts=%d",
            request.request_id, request.task_id, request.run_id,
            result["exit_code"], result["killed_reason"],
            len(result_payload["artifacts"]),
        )
        return JSONResponse(result_payload)

    @app.post("/v1/execute/{request_id}/cancel")
    async def cancel(request_id: str) -> JSONResponse:
        task = state.running.get(request_id)
        if task is None:
            return JSONResponse({"detail": "no such running request",
                                 "request_id": request_id}, status_code=404)
        task.cancel()
        return JSONResponse({"status": "cancelling", "request_id": request_id})

    @app.get("/v1/runs/{request_id}")
    async def run_status(request_id: str) -> JSONResponse:
        known = await state.idempotency.get(request_id)
        if known is None:
            return JSONResponse({"detail": "unknown request_id"}, status_code=404)
        known_state, cached = known
        if known_state == "done" and cached is not None:
            return JSONResponse(cached)
        return JSONResponse({"request_id": request_id, "state": "running"})

    return app


app = create_app()
