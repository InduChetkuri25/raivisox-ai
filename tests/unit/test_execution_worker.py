"""Dedicated execution worker — protocol, boundary and packaging regression tests.

What this file proves (and what it does NOT):

- The API→worker hop is authenticated: a missing, stale, forged or replayed
  request is refused, and a valid one is accepted.
- The worker clamps every caller-supplied limit and owns network denial,
  pid/memory/cpu/fsize ceilings and the workspace scope — a caller can ask for
  less, never more.
- Provenance comes from the API's execution context; a tool parameter cannot
  supply or forge it.
- Idempotency is explicit: the same request id replays the stored answer and
  never executes twice.
- The worker endpoint fails closed: no URL/key (or a short key) is a
  configuration error, not a local fallback.
- The production packaging is coherent: both images build from the repo root,
  the API image copies the repo-level packages it imports, only the worker
  service has a runtime socket, and the API declares none.

These are deterministic unit/configuration checks. They start no container and
measure no isolation: the live evidence is the ``production-isolation-gate``
CI job, which builds the images and exercises the real path.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from services.execution import sandbox_policy as policy
from services.execution.remote_sandbox import WorkerSandbox
from services.execution.sandbox import SandboxConfigError, SandboxSpec
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
    sign_request,
    verify_request,
)

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TOKEN = "a" * 64
_URL = "http://sandbox-worker:8100"
_LIMITS = WorkerLimits()


def _payload(**overrides):
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "3f2b8c1e-0000-4000-8000-000000000001",
        "provenance": {"task_id": "task-1", "run_id": "run-1", "step_id": "step-2"},
        "spec": {
            "argv": ["python", "-c", "print('hi')"],
            "cwd": "/app/workspaces/tasks/task-1/run-1",
            "env": {},
            "timeout_s": 30,
        },
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Authentication (HTTP layer, no container)
# ---------------------------------------------------------------------------

def test_signature_roundtrip_and_refusals():
    body = b'{"a":1}'
    ts, nonce = "1700000000", "b" * 32
    sig = sign_request(_TOKEN, ts, nonce, body)
    verify_request(_TOKEN, ts, nonce, sig, body, now=1700000000,
                   replay=AuthReplayCache())

    with pytest.raises(SignatureError):
        verify_request(_TOKEN, ts, nonce, "0" * 64, body, now=1700000000)
    with pytest.raises(SignatureError):
        # Body altered after signing — the digest binds the exact bytes.
        verify_request(_TOKEN, ts, nonce, sig, b'{"a":2}', now=1700000000)
    with pytest.raises(SignatureError):
        verify_request(_TOKEN, ts, nonce, sig, body, now=1700000000 + 301)
    with pytest.raises(SignatureError):
        verify_request("", ts, nonce, sig, body, now=1700000000)
    with pytest.raises(SignatureError):
        verify_request(_TOKEN, ts, "", sig, body, now=1700000000)


def test_replay_is_refused_after_the_first_accept():
    body = b"{}"
    ts, nonce = "1700000000", "c" * 32
    sig = sign_request(_TOKEN, ts, nonce, body)
    cache = AuthReplayCache()
    verify_request(_TOKEN, ts, nonce, sig, body, now=1700000000, replay=cache)
    with pytest.raises(SignatureError):
        verify_request(_TOKEN, ts, nonce, sig, body, now=1700000000, replay=cache)


def test_unauthenticated_caller_cannot_burn_a_nonce():
    """The nonce is consumed only after the signature matched."""
    cache = AuthReplayCache()
    body = b"{}"
    with pytest.raises(SignatureError):
        verify_request(_TOKEN, "1700000000", "d" * 32, "0" * 64, body,
                       now=1700000000, replay=cache)
    sig = sign_request(_TOKEN, "1700000000", "d" * 32, body)
    verify_request(_TOKEN, "1700000000", "d" * 32, sig, body,
                   now=1700000000, replay=cache)


# ---------------------------------------------------------------------------
# 2. Worker request validation: caller may lower, never raise
# ---------------------------------------------------------------------------

def test_request_rejects_forged_identity_shapes():
    with pytest.raises(RequestValidationError):
        ExecutionRequest.from_payload(_payload(provenance={"run_id": "run-1"}),
                                      _LIMITS)
    with pytest.raises(RequestValidationError):
        ExecutionRequest.from_payload(_payload(request_id="not-a-uuid"), _LIMITS)
    with pytest.raises(RequestValidationError):
        ExecutionRequest.from_payload(
            _payload(provenance={"task_id": "../../etc", "run_id": "run-1"}),
            _LIMITS)
    with pytest.raises(RequestValidationError):
        ExecutionRequest.from_payload(_payload(protocol_version="0"), _LIMITS)


def test_request_limits_are_clamped_to_worker_ceilings():
    req = ExecutionRequest.from_payload(
        _payload(spec={
            "argv": ["python", "-c", "pass"],
            "cwd": "/app/workspaces/tasks/task-1/run-1",
            "timeout_s": 10_000,
            "memory_bytes": 10 ** 12,
            "max_pids": 10 ** 6,
            "cpu_seconds": 10 ** 6,
            "fsize_bytes": 10 ** 12,
            "output_limit_bytes": 10 ** 9,
        }),
        _LIMITS,
    )
    assert req.timeout_s == _LIMITS.max_timeout_s
    assert req.memory_bytes == _LIMITS.max_memory_bytes
    assert req.max_pids == _LIMITS.max_pids
    assert req.cpu_seconds == _LIMITS.max_cpu_seconds
    assert req.fsize_bytes == _LIMITS.max_fsize_bytes
    assert req.output_limit_bytes == _LIMITS.max_output_bytes


def test_request_never_lets_the_caller_relax_network_or_grant_privileges():
    """Network denial and limits are not request fields at all."""
    payload = _payload()
    payload["spec"]["network"] = "allow"
    payload["spec"]["privileged"] = True
    req = ExecutionRequest.from_payload(payload, _LIMITS)
    wire = json.dumps(req.to_payload())
    assert "network" not in wire
    assert "privileged" not in wire
    # Positive intervals are always present, so a run is never unlimited.
    assert req.memory_bytes > 0 and req.max_pids > 0 and req.cpu_seconds > 0
    assert req.fsize_bytes > 0


def test_worker_limits_reject_nonsense_configuration():
    with pytest.raises(RequestValidationError):
        WorkerLimits.from_env({"SANDBOX_WORKER_MAX_PIDS": "lots"})
    with pytest.raises(RequestValidationError):
        WorkerLimits.from_env({"SANDBOX_WORKER_MAX_MEMORY_BYTES": "0"})


# ---------------------------------------------------------------------------
# 3. API-side client: provenance and fail-closed behaviour
# ---------------------------------------------------------------------------

def _spec(**kwargs) -> SandboxSpec:
    base = dict(
        argv=["python", "-c", "print('ok')"],
        cwd="/app/workspaces/tasks/task-1/run-1",
        env={"RAIVISOX_TASK_ID": "task-1", "RAIVISOX_RUN_ID": "run-1",
             "RAIVISOX_STEP_ID": "step-2"},
        timeout_s=5,
    )
    base.update(kwargs)
    return SandboxSpec(**base)


@pytest.mark.asyncio
async def test_worker_sandbox_requires_context_provenance():
    """A spec without server-built provenance is refused, never anonymised."""
    sb = WorkerSandbox(_URL, _TOKEN)
    spec = _spec(env={})
    result = await sb.run(spec)
    assert result.error is not None and "provenance" in result.error
    assert result.killed_reason == "supervisor"
    assert sb.last_artifacts == []


@pytest.mark.asyncio
async def test_worker_sandbox_signs_the_request_and_reports_worker_truth():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        ts = request.headers[TIMESTAMP_HEADER]
        nonce = request.headers[NONCE_HEADER]
        # The worker verifies exactly this construction.
        verify_request(_TOKEN, ts, nonce, request.headers[AUTH_HEADER],
                       request.content, replay=AuthReplayCache())
        payload = json.loads(json.dumps(body))
        assert payload["provenance"]["task_id"] == "task-1"
        return httpx.Response(200, json={
            "protocol_version": PROTOCOL_VERSION,
            "request_id": payload["request_id"],
            "provenance": payload["provenance"],
            "state": "ok",
            "replayed": False,
            "result": {"exit_code": 0, "stdout": "ok\n", "stderr": "",
                       "duration_ms": 12, "killed_reason": None,
                       "limits_hit": [], "enforced": ["network_deny"], "error": None},
            "artifacts": [{"path": "out.txt", "size": 2, "sha256": "f" * 64}],
            "artifact_notes": [],
            "capabilities": {"network_deny": True, "runtime_available": True},
        })

    sb = WorkerSandbox(_URL, _TOKEN, transport=httpx.MockTransport(handler))
    result = await sb.run(_spec())
    assert result.exit_code == 0 and result.stdout == "ok\n"
    assert "network_deny" in result.enforced
    assert sb.last_artifacts[0]["path"] == "out.txt"
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/execute"


@pytest.mark.asyncio
async def test_worker_sandbox_fails_closed_when_the_worker_is_down():
    sb = WorkerSandbox(_URL, _TOKEN, timeout_s=1)

    def boom(request):  # noqa: ANN001
        raise httpx.ConnectError("refused", request=request)

    sb._transport = httpx.MockTransport(boom)  # noqa: SLF001 - test seam
    result = await sb.run(_spec())
    assert result.exit_code is None
    assert result.killed_reason == "supervisor"
    assert "unreachable" in (result.error or "")
    assert result.enforced == []  # nothing was enforced: no fake claims


@pytest.mark.asyncio
async def test_worker_sandbox_reports_a_401_as_a_refusal():
    def handler(request):  # noqa: ANN001
        return httpx.Response(401, json={"detail": "authentication refused"})

    sb = WorkerSandbox(_URL, _TOKEN, transport=httpx.MockTransport(handler))
    result = await sb.run(_spec())
    assert result.killed_reason == "supervisor"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_worker_sandbox_retries_a_transport_failure_with_the_same_request_id():
    ids: list[str] = []
    calls = {"n": 0}

    def handler(request):  # noqa: ANN001
        body = json.loads(request.content)
        ids.append(body["request_id"])
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={
            "request_id": body["request_id"],
            "result": {"exit_code": 0, "stdout": "", "stderr": "",
                       "duration_ms": 1, "killed_reason": None,
                       "limits_hit": [], "enforced": [], "error": None},
            "artifacts": [],
        })

    sb = WorkerSandbox(_URL, _TOKEN, transport=httpx.MockTransport(handler))
    result = await sb.run(_spec())
    assert result.exit_code == 0
    assert len(ids) == 2 and ids[0] == ids[1], "retry must reuse the request id"


def test_bind_source_is_translated_to_the_host_path():
    """The daemon resolves bind sources on the host, not in the worker."""
    from services.execution.container_sandbox import (
        CONTAINER_WORKSPACE,
        ContainerSandbox,
        build_run_argv,
    )

    sb = ContainerSandbox(bind_prefix_map=("/app/workspaces/tasks",
                                           "/srv/raivisox/workspaces/tasks"))
    assert sb.host_bind_path("/app/workspaces/tasks/t1/run-1") == \
        "/srv/raivisox/workspaces/tasks/t1/run-1"
    # Exact prefix and unrelated paths are left alone.
    assert sb.host_bind_path("/app/workspaces/tasks") == \
        "/srv/raivisox/workspaces/tasks"
    assert sb.host_bind_path("/tmp/x") == "/tmp/x"
    assert ContainerSandbox().host_bind_path("/app/w") == "/app/w"

    argv = build_run_argv(
        _spec(), runtime_exe="docker", image="img@sha256:" + "a" * 64,
        user="1000:1000", python_bin="/usr/local/bin/python3",
        container_cwd=CONTAINER_WORKSPACE, name="raivisox-sbx-test",
        host_writable="/host/run-1",
    )
    assert f"/host/run-1:{CONTAINER_WORKSPACE}:rw" in argv
    # Still exactly one mount, never the runtime socket.
    assert sum(1 for a in argv if a == "-v") == 1
    assert not any("docker.sock" in a for a in argv)


def test_worker_endpoint_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "worker")
    monkeypatch.delenv(policy.WORKER_URL_ENV, raising=False)
    monkeypatch.delenv(policy.WORKER_TOKEN_ENV, raising=False)
    with pytest.raises(SandboxConfigError) as ei:
        policy.resolve_backend_name()
    assert policy.WORKER_URL_ENV in str(ei.value)

    with pytest.raises(SandboxConfigError):
        policy.validate_worker_endpoint(_URL, "short")
    with pytest.raises(SandboxConfigError):
        policy.validate_worker_endpoint("ftp://worker", _TOKEN)
    with pytest.raises(SandboxConfigError):
        policy.validate_worker_endpoint("http://user:pw@worker:8100", _TOKEN)
    policy.validate_worker_endpoint(_URL, _TOKEN)


def test_production_worker_backend_needs_url_and_token(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "worker")
    monkeypatch.setenv(policy.WORKER_URL_ENV, _URL)
    monkeypatch.delenv(policy.WORKER_TOKEN_ENV, raising=False)
    with pytest.raises(SandboxConfigError) as ei:
        policy.resolve_backend_name()
    assert policy.WORKER_TOKEN_ENV in str(ei.value)

    monkeypatch.setenv(policy.WORKER_TOKEN_ENV, _TOKEN)
    assert policy.resolve_backend_name() == "worker"


def test_startup_refuses_a_half_configured_worker_boundary(monkeypatch):
    from app.config import Settings, validate_settings

    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    settings = Settings(
        _env_file=None,
        environment="production",
        secret_key="ab12" * 12,
        database_url="postgresql+asyncpg://u:p@db.internal:5432/raivisox",
        cors_origins="https://app.example.test",
        allowed_hosts="api.example.test",
        sandbox_backend="worker",
    )
    with pytest.raises(RuntimeError) as ei:
        validate_settings(settings)
    assert "SANDBOX_WORKER_URL" in str(ei.value)

    settings.sandbox_worker_url = _URL
    settings.sandbox_worker_token = _TOKEN
    validate_settings(settings)  # complete boundary boots


# ---------------------------------------------------------------------------
# 4. Artifact manifest: bounded, contained, no bytes
# ---------------------------------------------------------------------------

def test_artifact_manifest_is_contained_bounded_and_symlink_safe(tmp_path):
    root = tmp_path / "run"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "b.bin").write_bytes(b"\x00" * 8)
    outside = tmp_path / "secret.txt"
    outside.write_text("host secret", encoding="utf-8")
    try:
        os.symlink(outside, root / "link.txt")
    except (OSError, NotImplementedError):  # pragma: no cover - Windows w/o privilege
        pass

    entries, notes = collect_artifacts(str(root), ArtifactLimits(max_files=10,
                                                                max_file_bytes=1024,
                                                                max_total_bytes=4096))
    paths = {e.path for e in entries}
    assert paths == {"a.txt", "sub/b.bin"}
    assert all(not p.startswith("/") and ".." not in p for p in paths)
    assert all(len(e.sha256) == 64 for e in entries)
    assert isinstance(notes, list)


def test_artifact_manifest_honours_caps(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}.txt").write_text("x" * 10, encoding="utf-8")
    entries, notes = collect_artifacts(str(root), ArtifactLimits(max_files=2,
                                                                max_file_bytes=100,
                                                                max_total_bytes=1000))
    assert len(entries) == 2
    assert any("cap" in note for note in notes)


# ---------------------------------------------------------------------------
# 5. Packaging: the blocker and the boundary, asserted in files
# ---------------------------------------------------------------------------

def _read(*parts: str) -> str:
    with open(os.path.join(_REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def _compose_services(text: str) -> dict[str, str]:
    """Split the compose file into top-level service blocks (comments stripped)."""
    lines = [line.split("#", 1)[0].rstrip() for line in text.splitlines()]
    services: dict[str, str] = {}
    current: str | None = None
    in_services = False
    for line in lines:
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_services = line.strip() == "services:"
            current = None
            continue
        if not in_services:
            continue
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
            services[current] = ""
            continue
        if current:
            services[current] += line + "\n"
    return services


def test_api_image_builds_from_the_repo_root_context():
    """The blocker: ./apps/api cannot contain services.* / integrations.*."""
    compose = _read("docker-compose.prod.yml")
    services = _compose_services(compose)
    assert "context: ." in services["api"]
    assert "dockerfile: apps/api/Dockerfile" in services["api"]
    assert "dockerfile: apps/worker/Dockerfile" in services["sandbox-worker"]


def test_api_dockerfile_copies_the_repo_level_packages_it_imports():
    dockerfile = _read("apps", "api", "Dockerfile")
    assert "COPY services/ /app/services/" in dockerfile
    assert "COPY integrations/ /app/integrations/" in dockerfile
    assert "COPY apps/api/app /app/app" in dockerfile
    # No blanket copy of the build context: no secrets/tests/docs by accident.
    assert "COPY . " not in dockerfile
    assert "USER appuser" in dockerfile


def test_worker_dockerfile_is_the_only_image_with_a_runtime_client():
    worker = _read("apps", "worker", "Dockerfile")
    api = _read("apps", "api", "Dockerfile")
    assert "COPY --from=dockercli /usr/local/bin/docker" in worker
    # The API image carries no runtime client of any kind.
    assert "COPY --from=dockercli" not in api
    assert "/usr/local/bin/docker" not in api
    assert "docker.sock" not in api
    assert "COPY services/ /app/services/" in worker


def test_only_the_worker_service_declares_a_runtime_socket():
    services = _compose_services(_read("docker-compose.prod.yml"))
    assert "docker.sock" in services["sandbox-worker"]
    api = services["api"]
    assert "docker.sock" not in api
    assert "privileged" not in api.lower()
    assert "network_mode: host" not in api
    assert ":/app/workspaces" in api
    assert ":/app/workspaces" in services["sandbox-worker"]
    # The workspace is a host directory both services must mount, and the
    # worker is told its HOST location because the daemon resolves bind
    # sources there (not in the worker container).
    assert "SANDBOX_HOST_WORKSPACE_ROOT=${RAIVISOX_WORKSPACE_DIR" in (
        services["sandbox-worker"])
    assert "RAIVISOX_WORKSPACE_DIR:?" in api
    # The worker gets the shared workspace and nothing else from the API's env.
    worker = services["sandbox-worker"]
    assert "DATABASE_URL" not in worker
    assert "SECRET_KEY" not in worker
    assert "OPENAI_API_KEY" not in worker


def test_docker_build_context_excludes_secrets_and_dev_material():
    ignore = _read(".dockerignore")
    for pattern in (".env", "tests", "docs", "**/__pycache__", "*.db"):
        assert pattern in ignore
    # …and must never exclude the packages the images copy in.
    assert "services" not in ignore.replace("**/__pycache__", "")
