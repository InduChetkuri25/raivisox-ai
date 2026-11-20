"""Worker HTTP endpoint tests — the boundary's own refusal behaviour.

These run the real FastAPI app in-process with a *fake* sandbox: no container
is started, so they prove the endpoint contract (authentication, validation,
idempotency, provenance enforcement) and nothing about isolation. The live
guarantees are measured by ``tests/integration/production_isolation_gate.py``.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from services.execution import worker_app as worker_module
from services.execution.sandbox import SandboxResult
from services.execution.worker_protocol import (
    AUTH_HEADER,
    NONCE_HEADER,
    PROTOCOL_VERSION,
    TIMESTAMP_HEADER,
    WorkerLimits,
    sign_request,
)

_TOKEN = "t" * 48


class _FakeSandbox:
    """Records the spec the worker built; never starts anything."""

    def __init__(self) -> None:
        self.specs: list = []

    def capabilities(self) -> dict[str, bool]:
        return {"network_deny": True, "runtime_available": True}

    async def run(self, spec):  # noqa: ANN001
        self.specs.append(spec)
        return SandboxResult(exit_code=0, stdout="ok\n", stderr="",
                             duration_ms=1, enforced=["network_deny"])


@pytest.fixture()
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKER_TOKEN", _TOKEN)
    monkeypatch.setenv("SANDBOX_WORKER_WORKSPACE_ROOT", str(tmp_path))
    app = worker_module.create_app()
    state = app.state.worker
    state.sandbox = _FakeSandbox()
    state.limits = WorkerLimits(workspace_root=str(tmp_path), max_timeout_s=60.0)
    (tmp_path / "task-1" / "run-1").mkdir(parents=True)
    with TestClient(app) as client:
        yield client, state, tmp_path


def _signed(body: bytes, *, token: str = _TOKEN, nonce: str | None = None,
            ts: str | None = None) -> dict[str, str]:
    timestamp = ts or str(int(time.time()))
    n = nonce or uuid.uuid4().hex
    return {
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: n,
        AUTH_HEADER: sign_request(token, timestamp, n, body),
        "Content-Type": "application/json",
    }


def _payload(root, **overrides):
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "provenance": {"task_id": "task-1", "run_id": "run-1",
                       "step_id": "step-1"},
        "spec": {"argv": ["true"], "cwd": f"{root}/task-1/run-1",
                 "env": {}, "timeout_s": 5},
    }
    body.update(overrides)
    return json.dumps(body).encode()


def test_unsigned_execution_is_refused(worker):
    client, state, root = worker
    resp = client.post("/v1/execute", content=_payload(root),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert state.sandbox.specs == []


def test_foreign_key_is_refused(worker):
    client, state, root = worker
    body = _payload(root)
    resp = client.post("/v1/execute", content=body, headers=_signed(body, token="x" * 48))
    assert resp.status_code == 401
    assert state.sandbox.specs == []


def test_signed_request_executes_once_and_replays(worker):
    client, state, root = worker
    body = _payload(root)
    first = client.post("/v1/execute", content=body, headers=_signed(body))
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    assert len(state.sandbox.specs) == 1

    second = client.post("/v1/execute", content=body, headers=_signed(body))
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert len(state.sandbox.specs) == 1, "duplicate must not execute again"


def test_reused_nonce_is_refused(worker):
    client, state, root = worker
    body = _payload(root)
    headers = _signed(body)
    assert client.post("/v1/execute", content=body, headers=headers).status_code == 200
    body2 = _payload(root)
    assert client.post("/v1/execute", content=body2, headers=headers).status_code == 401


def test_worker_forces_network_deny_and_its_own_limits(worker):
    client, state, root = worker
    body = _payload(root, spec={
        "argv": ["true"], "cwd": f"{root}/task-1/run-1", "env": {},
        "timeout_s": 10_000, "memory_bytes": 10 ** 12, "network": "allow",
        "max_pids": 10 ** 6,
    })
    resp = client.post("/v1/execute", content=body, headers=_signed(body))
    assert resp.status_code == 200
    spec = state.sandbox.specs[0]
    assert spec.network == "deny"
    assert spec.timeout_s == state.limits.max_timeout_s
    assert spec.memory_bytes == state.limits.max_memory_bytes
    assert spec.max_pids == state.limits.max_pids
    assert spec.workspace_writable == spec.cwd


def test_cwd_outside_the_worker_workspace_is_refused(worker):
    client, state, root = worker
    body = _payload(root, spec={"argv": ["true"], "cwd": "/etc", "env": {},
                                "timeout_s": 5})
    resp = client.post("/v1/execute", content=body, headers=_signed(body))
    assert resp.status_code == 422
    assert state.sandbox.specs == []


def test_cwd_traversal_is_refused(worker):
    client, state, root = worker
    body = _payload(root, spec={
        "argv": ["true"], "cwd": f"{root}/task-1/run-1/../../..", "env": {},
        "timeout_s": 5,
    })
    resp = client.post("/v1/execute", content=body, headers=_signed(body))
    assert resp.status_code == 422


def test_forged_provenance_shape_is_refused(worker):
    client, state, root = worker
    body = _payload(root, provenance={"run_id": "run-1"})
    resp = client.post("/v1/execute", content=body, headers=_signed(body))
    assert resp.status_code == 422
    assert state.sandbox.specs == []


def test_health_and_capabilities_are_unauthenticated_and_bounded(worker):
    client, state, root = worker
    assert client.get("/healthz").json()["status"] == "ok"
    caps = client.get("/v1/capabilities").json()
    assert caps["capabilities"]["network_deny"] is True
    assert caps["max_timeout_s"] == state.limits.max_timeout_s


def test_cancel_of_an_unknown_request_is_404(worker):
    client, _state, _root = worker
    assert client.post("/v1/execute/does-not-exist/cancel").status_code == 404
