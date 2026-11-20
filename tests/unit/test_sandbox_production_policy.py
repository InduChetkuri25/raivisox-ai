"""H1 — production sandbox-backend policy regression tests.

What these tests prove (and what they deliberately do NOT prove):

- The production deployment can no longer fall through to the unconfined
  ``ProcessSandbox`` by default: an undeclared backend, or an explicit
  ``process`` without the documented acknowledgement, is refused at startup
  ``validate_settings`` time AND at the single sandbox doorway every adapter
  uses (``build_default_sandbox``).
- Development/test keep their convenient process default (no friction).
- An unknown/empty backend value is still a loud configuration error.
- The refusal is justified by evidence, not taste: the process backend itself
  reports ``network_deny: False`` and ``fs_confinement: False`` — it cannot
  satisfy the isolation a production deployment needs.
- The shipped production compose file requires the declaration and mounts no
  container-runtime socket.

These are CONFIGURATION and CONSTRUCTION checks. They do not start a
container, do not measure isolation, and are not a substitute for the live
``container-gate`` CI job. Runtime isolation remains unverified on any host
without a container runtime, and H1 is not closed by this file.
"""

import os

import pytest

from services.execution import sandbox_policy as policy
from services.execution.container_sandbox import ContainerSandbox
from services.execution.sandbox import (
    ProcessSandbox,
    SandboxConfigError,
    build_default_sandbox,
)

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ACK = policy.PROCESS_ACK_ENV


def _clear(monkeypatch):
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    monkeypatch.delenv(_ACK, raising=False)


# ---------------------------------------------------------------------------
# 1. Backend selection policy
# ---------------------------------------------------------------------------

def test_development_default_remains_the_process_backend(monkeypatch):
    """Local convenience is unchanged: no declaration needed outside prod."""
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert isinstance(build_default_sandbox(), ProcessSandbox)


def test_test_environment_default_remains_the_process_backend(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert isinstance(build_default_sandbox(), ProcessSandbox)


def test_production_without_declaration_is_refused(monkeypatch):
    """The shipped-default hole (H1): production must never default to process."""
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(SandboxConfigError) as ei:
        build_default_sandbox()
    msg = str(ei.value)
    assert "SANDBOX_BACKEND" in msg
    assert "production" in msg.lower()
    # The refusal points at both documented exits, never at a silent default.
    assert "container" in msg
    assert _ACK in msg


def test_production_process_is_refused_without_acknowledgement(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "process")
    with pytest.raises(SandboxConfigError) as ei:
        build_default_sandbox()
    msg = str(ei.value)
    assert "SANDBOX_BACKEND=process" in msg
    assert "network" in msg and "filesystem" in msg
    assert _ACK in msg


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_production_process_acknowledgement_must_be_truthy(monkeypatch, value):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "process")
    monkeypatch.setenv(_ACK, value)
    with pytest.raises(SandboxConfigError):
        build_default_sandbox()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_production_process_accepted_only_with_explicit_acknowledgement(
    monkeypatch, value
):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "process")
    monkeypatch.setenv(_ACK, value)
    assert isinstance(build_default_sandbox(), ProcessSandbox)


def test_acknowledged_production_posture_reports_itself(monkeypatch):
    """The weakening is never invisible: it produces an explicit notice."""
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "process")
    monkeypatch.setenv(_ACK, "1")
    notice = policy.process_posture_notice()
    assert notice is not None
    assert "UNCONFINED" in notice
    assert _ACK in notice


def test_confined_and_development_postures_emit_no_notice(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "container")
    assert policy.process_posture_notice() is None
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SANDBOX_BACKEND", "process")
    assert policy.process_posture_notice() is None


def test_production_container_backend_is_selected(monkeypatch):
    """Selection never probes or substitutes; the backend decides per run."""
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "container")
    assert isinstance(build_default_sandbox(), ContainerSandbox)


@pytest.mark.parametrize("environment", ["development", "test", "production"])
@pytest.mark.parametrize("value", ["containerd", "podman", "Containers"])
def test_unknown_backend_is_refused_everywhere(monkeypatch, environment, value):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SANDBOX_BACKEND", value)
    with pytest.raises(SandboxConfigError) as ei:
        build_default_sandbox()
    assert "invalid SANDBOX_BACKEND" in str(ei.value)


def test_mixed_case_container_is_still_container(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "  Container ")
    assert isinstance(build_default_sandbox(), ContainerSandbox)


@pytest.mark.parametrize("environment", ["development", "production"])
def test_explicitly_empty_backend_stays_a_configuration_error(
    monkeypatch, environment
):
    """Absent ≠ empty: an explicitly empty value was always refused."""
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SANDBOX_BACKEND", "")
    with pytest.raises(SandboxConfigError):
        build_default_sandbox()


def test_policy_error_is_value_error_compatible():
    """Existing callers catch ValueError for bad sandbox configuration."""
    assert issubclass(SandboxConfigError, ValueError)
    with pytest.raises(ValueError):
        policy.resolve_backend_name({"ENVIRONMENT": "production"})


def test_policy_resolves_from_an_explicit_mapping():
    """Pure resolution — no process environment involved."""
    assert policy.resolve_backend_name({"ENVIRONMENT": "development"}) == "process"
    assert (
        policy.resolve_backend_name(
            {"ENVIRONMENT": "production", "SANDBOX_BACKEND": "container"}
        )
        == "container"
    )
    assert policy.is_production({"ENVIRONMENT": " Production "})
    assert not policy.is_production({"ENVIRONMENT": "test"})


# ---------------------------------------------------------------------------
# 2. Why the refusal exists — the backend's own honest evidence
# ---------------------------------------------------------------------------

def test_process_backend_admits_it_cannot_confine_network_or_filesystem():
    """The policy refuses process in production because that is the truth."""
    caps = ProcessSandbox().capabilities()
    assert caps["network_deny"] is False
    assert caps["fs_confinement"] is False
    assert caps["pids_limit"] is False


# ---------------------------------------------------------------------------
# 3. Every adapter doorway fails closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "factory",
    [
        lambda ws: __import__(
            "integrations.adapters.builtin", fromlist=["PythonExecAdapter"]
        ).PythonExecAdapter(ws),
        lambda ws: __import__(
            "integrations.adapters.builtin", fromlist=["ShellExecAdapter"]
        ).ShellExecAdapter(ws),
        lambda ws: __import__(
            "integrations.adapters.codegen", fromlist=["CodegenAdapter"]
        ).CodegenAdapter(ws),
    ],
    ids=["python_exec", "shell_exec", "codegen"],
)
def test_adapters_refuse_to_construct_an_unconfined_production_sandbox(
    monkeypatch, tmp_path, factory
):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(SandboxConfigError):
        factory(str(tmp_path))


@pytest.mark.parametrize(
    "factory",
    [
        lambda ws: __import__(
            "integrations.adapters.builtin", fromlist=["PythonExecAdapter"]
        ).PythonExecAdapter(ws),
        lambda ws: __import__(
            "integrations.adapters.builtin", fromlist=["ShellExecAdapter"]
        ).ShellExecAdapter(ws),
        lambda ws: __import__(
            "integrations.adapters.codegen", fromlist=["CodegenAdapter"]
        ).CodegenAdapter(ws),
    ],
    ids=["python_exec", "shell_exec", "codegen"],
)
def test_adapters_construct_a_container_sandbox_when_production_declares_it(
    monkeypatch, tmp_path, factory
):
    _clear(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SANDBOX_BACKEND", "container")
    # Construction only: no runtime is probed here, and no fallback exists.
    factory(str(tmp_path))


# ---------------------------------------------------------------------------
# 4. Startup validation (the API refuses to boot on the unconfined default)
# ---------------------------------------------------------------------------

def _prod_settings(**overrides):
    from app.config import Settings

    base = dict(
        _env_file=None,
        environment="production",
        secret_key="ab12" * 12,
        database_url="postgresql+asyncpg://u:p@db.internal:5432/raivisox",
        cors_origins="https://app.example.test",
        allowed_hosts="api.example.test",
        debug=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_startup_refuses_production_without_a_backend(monkeypatch):
    from app.config import validate_settings

    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    with pytest.raises(RuntimeError) as ei:
        validate_settings(_prod_settings(sandbox_backend=""))
    assert "SANDBOX_BACKEND" in str(ei.value)
    assert "sandbox isolation" in str(ei.value)


def test_startup_refuses_production_process_without_acknowledgement(monkeypatch):
    from app.config import validate_settings

    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    with pytest.raises(RuntimeError) as ei:
        validate_settings(_prod_settings(sandbox_backend="process"))
    assert "SANDBOX_BACKEND=process" in str(ei.value)


def test_startup_accepts_container_backend_and_acknowledged_process(monkeypatch):
    from app.config import validate_settings

    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    validate_settings(_prod_settings(sandbox_backend="container"))
    validate_settings(
        _prod_settings(
            sandbox_backend="process", sandbox_allow_process_in_production="1"
        )
    )


def test_development_startup_is_unchanged():
    from app.config import Settings, validate_settings

    validate_settings(Settings(_env_file=None))  # no sandbox declaration needed


# ---------------------------------------------------------------------------
# 5. Deployment configuration regression (no host-wide runtime control)
# ---------------------------------------------------------------------------

def _prod_compose_text() -> str:
    with open(
        os.path.join(_REPO, "docker-compose.prod.yml"), encoding="utf-8"
    ) as fh:
        return fh.read()


def test_prod_compose_requires_an_explicit_sandbox_backend():
    text = _prod_compose_text()
    assert "SANDBOX_BACKEND=${SANDBOX_BACKEND:?" in text


def _compose_directives() -> str:
    """The compose file without comments — what it actually configures."""
    lines = [line.split("#", 1)[0].rstrip() for line in _prod_compose_text().splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _api_service_block() -> str:
    """The API service's own directives (not the rest of the stack)."""
    block: list[str] = []
    inside = False
    for line in _compose_directives().splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            name = line.strip().rstrip(":")
            if inside and name != "api":
                break
            inside = name == "api"
            continue
        if inside:
            block.append(line)
    return "\n".join(block)


def test_prod_compose_never_mounts_a_runtime_socket_into_the_api():
    """A runtime socket in the API container is host-wide control (rejected).

    Since the dedicated worker exists (§10.7), the socket lives in that one
    confined service instead — the API must still declare none, and the worker
    must carry no API secret or database credential.
    """
    api = _api_service_block()
    assert api, "api service block not found"
    assert "sock" not in api
    assert "privileged" not in api.lower()
    assert "network_mode: host" not in api
    # The only bind the API service declares is artifact storage. It is a
    # host path (not a named volume) so the worker's sandbox containers can
    # bind the same absolute path through the runtime socket.
    assert "/app/workspaces" in api
    assert "workspace_data" not in _compose_directives()

    directives = _compose_directives()
    assert "privileged" not in directives.lower()
    assert "network_mode: host" not in directives
