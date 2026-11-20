"""WorkerSandbox — the API-side half of the dedicated execution boundary.

With ``SANDBOX_BACKEND=worker`` the API never touches a container runtime. It
signs an :class:`~services.execution.worker_protocol.ExecutionRequest` and POSTs
it to the isolated sandbox worker, which owns the runtime and runs exactly one
throwaway container per request.

Why this exists: mounting a container-runtime socket into the API container
would hand host-wide control (root-equivalent on most hosts) to the process
that also serves untrusted HTTP and holds the database credentials. §10.7 of
docs/deployment.md compares the options; this backend is the one that removes
the socket from the API entirely.

Honest reporting rules (same as every other backend):

- ``capabilities()`` returns only what the *worker* reported, cached; before a
  successful probe every key is ``False`` — including ``worker_available``.
- ``run()`` fails closed with ``killed_reason="supervisor"`` when the worker is
  unreachable, refuses the request, or answers with an unexpected shape. There
  is no code path from here to a local process spawn.
- ``enforced`` comes from the worker's own per-run report, so a weakened run is
  visible in the result instead of hidden behind a configuration value.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

import httpx

from services.execution.sandbox import (
    ExecutionSandbox,
    SandboxResult,
    SandboxSpec,
)
from services.execution.worker_protocol import (
    AUTH_HEADER,
    NONCE_HEADER,
    PROTOCOL_VERSION,
    TIMESTAMP_HEADER,
    ExecutionRequest,
    sign_request,
)
from services.execution.sandbox_policy import (
    WORKER_TOKEN_ENV,
    WORKER_URL_ENV,
    SandboxConfigError,
    validate_worker_endpoint,
)

__all__ = ["WorkerSandbox"]

_CAPABILITY_KEYS = (
    "argv_execution",
    "minimal_env",
    "working_dir",
    "bounded_output",
    "timeout_kill",
    "cpu_limit",
    "memory_limit",
    "fsize_limit",
    "pids_limit",
    "network_deny",
    "fs_confinement",
    "non_root",
    "cap_drop_all",
    "no_new_privileges",
    "image_pinned",
    "read_only_rootfs",
    "workspace_quota",
    "runtime_available",
)

_PROBE_TTL_S = 30.0
_MAX_TRANSPORT_ATTEMPTS = 2  # one retry, same request_id — never a new run


class WorkerSandbox(ExecutionSandbox):
    """Client for the dedicated sandbox worker. Fail-closed by construction."""

    def __init__(self, url: str, token: str, *,
                 timeout_s: float | None = None,
                 client: httpx.AsyncClient | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._url = (url or "").strip().rstrip("/")
        self._token = token or ""
        self._timeout_s = float(
            timeout_s if timeout_s is not None
            else (os.environ.get("SANDBOX_WORKER_HTTP_TIMEOUT_S", "") or 320)
        )
        if self._timeout_s <= 0:
            raise SandboxConfigError(
                "SANDBOX_WORKER_HTTP_TIMEOUT_S must be positive"
            )
        self._client = client
        self._transport = transport
        self._caps: dict[str, bool] = {k: False for k in _CAPABILITY_KEYS}
        self._caps_at = 0.0
        #: Manifest of the most recent run (paths relative to its own scope).
        self.last_artifacts: list[dict[str, Any]] = []
        self.last_request_id: str = ""

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "WorkerSandbox":
        """Build from the environment; never guesses a default endpoint."""
        env = os.environ if environ is None else environ
        url = (env.get(WORKER_URL_ENV) or "").strip()
        token = (env.get(WORKER_TOKEN_ENV) or "").strip()
        validate_worker_endpoint(url, token)
        return cls(url, token)

    # -- capabilities -------------------------------------------------------

    def capabilities(self) -> dict[str, bool]:
        """Last worker-reported report; all-False until a probe succeeds."""
        now = time.monotonic()
        if now - self._caps_at < _PROBE_TTL_S:
            return dict(self._caps)
        self._caps = self._probe()
        self._caps_at = now
        return dict(self._caps)

    def _probe(self) -> dict[str, bool]:
        try:
            with httpx.Client(timeout=3.0, transport=self._transport) as client:
                resp = client.get(f"{self._url}/v1/capabilities")
            if resp.status_code != 200:
                return {k: False for k in _CAPABILITY_KEYS}
            payload = resp.json()
        except Exception:
            return {k: False for k in _CAPABILITY_KEYS}
        reported = payload.get("capabilities") or {}
        caps = {k: bool(reported.get(k, False)) for k in _CAPABILITY_KEYS}
        if payload.get("max_timeout_s") is not None:
            try:
                self._timeout_s = min(self._timeout_s,
                                      float(payload["max_timeout_s"]) + 20.0)
            except (TypeError, ValueError):
                pass
        return caps

    # -- execution ----------------------------------------------------------

    def _client_or_new(self) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=self._timeout_s,
                                 transport=self._transport), True

    def _build_request(self, spec: SandboxSpec, request_id: str) -> ExecutionRequest:
        """Translate the adapter's spec into the signed wire request.

        Provenance is taken from the spec's *server-built* environment keys
        (``RAIVISOX_TASK_ID`` / ``RAIVISOX_RUN_ID`` / ``RAIVISOX_STEP_ID``) —
        the adapter derives those from the execution context, never from a
        tool parameter. A caller that cannot supply them gets a refused run,
        not an anonymous one.
        """
        task_id = spec.env.get("RAIVISOX_TASK_ID", "").strip()
        run_id = spec.env.get("RAIVISOX_RUN_ID", "").strip()
        step_id = spec.env.get("RAIVISOX_STEP_ID", "").strip()
        if not task_id or not run_id:
            raise SandboxConfigError(
                "worker backend requires RAIVISOX_TASK_ID and RAIVISOX_RUN_ID in "
                "the sandbox spec environment (provenance must come from the "
                "authenticated execution context, never from tool parameters)"
            )
        return ExecutionRequest(
            request_id=request_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            argv=tuple(spec.argv),
            cwd=spec.cwd,
            env=dict(spec.env),
            timeout_s=spec.timeout_s,
            cpu_seconds=spec.cpu_seconds or 0,
            memory_bytes=spec.memory_bytes or 0,
            max_pids=spec.max_pids or 0,
            fsize_bytes=spec.fsize_bytes or 0,
            output_limit_bytes=spec.output_limit_bytes,
        )

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        request_id = str(uuid.uuid4())
        self.last_request_id = request_id
        self.last_artifacts = []
        try:
            request = self._build_request(spec, request_id)
        except SandboxConfigError as exc:
            return SandboxResult(error=str(exc), killed_reason="supervisor",
                                 enforced=["spec_validation"])
        body = json.dumps(request.to_payload(), separators=(",", ":"),
                          sort_keys=True).encode()

        client, owned = self._client_or_new()
        try:
            last_error = ""
            for attempt in range(_MAX_TRANSPORT_ATTEMPTS):
                timestamp = str(int(time.time()))
                nonce = uuid.uuid4().hex
                headers = {
                    TIMESTAMP_HEADER: timestamp,
                    NONCE_HEADER: nonce,
                    AUTH_HEADER: sign_request(self._token, timestamp, nonce, body),
                    "Content-Type": "application/json",
                }
                try:
                    resp = await client.post(f"{self._url}/v1/execute",
                                             content=body, headers=headers)
                except httpx.TransportError as exc:
                    # Retried ONLY on transport failure, and with the SAME
                    # request_id: the worker's idempotency store turns a
                    # duplicate into a replay instead of a second execution.
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt + 1 < _MAX_TRANSPORT_ATTEMPTS:
                        await asyncio.sleep(0.25)
                        continue
                    return SandboxResult(
                        error=f"execution worker unreachable: {last_error}",
                        killed_reason="supervisor",
                        enforced=[],
                    )
                return self._to_result(resp, request_id)
            return SandboxResult(  # pragma: no cover - loop always returns
                error=f"execution worker unreachable: {last_error}",
                killed_reason="supervisor", enforced=[])
        finally:
            if owned:
                await client.aclose()

    def _to_result(self, resp: httpx.Response, request_id: str) -> SandboxResult:
        try:
            payload = resp.json()
        except ValueError:
            return SandboxResult(
                error=(f"execution worker returned a non-JSON response "
                       f"(HTTP {resp.status_code})"),
                killed_reason="supervisor", enforced=[])

        if resp.status_code != 200:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            return SandboxResult(
                error=(f"execution worker refused the request (HTTP "
                       f"{resp.status_code}): {detail or 'no detail'}"),
                killed_reason="supervisor",
                enforced=["spec_validation"])

        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            return SandboxResult(
                error="execution worker response did not match the request id",
                killed_reason="supervisor", enforced=["spec_validation"])

        result = payload.get("result") or {}
        artifacts = payload.get("artifacts") or []
        self.last_artifacts = [a for a in artifacts if isinstance(a, dict)]

        caps = payload.get("capabilities")
        if isinstance(caps, dict):
            self._caps = {k: bool(caps.get(k, False)) for k in _CAPABILITY_KEYS}
            self._caps_at = time.monotonic()

        return SandboxResult(
            exit_code=result.get("exit_code"),
            stdout=result.get("stdout", "") or "",
            stderr=result.get("stderr", "") or "",
            duration_ms=int(result.get("duration_ms") or 0),
            killed_reason=result.get("killed_reason"),
            limits_hit=list(result.get("limits_hit") or []),
            enforced=list(result.get("enforced") or []),
            error=result.get("error"),
        )


def protocol_version() -> str:
    """Exposed for the readiness probe / diagnostics endpoint."""
    return PROTOCOL_VERSION
