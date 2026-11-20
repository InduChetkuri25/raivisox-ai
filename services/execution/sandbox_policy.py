"""Production sandbox-backend policy — explicit, fail closed, never a default.

H1 of the V3 Phase 1 acceptance audit: the shipped production stack declared
no ``SANDBOX_BACKEND``, so :func:`services.execution.sandbox.build_default_sandbox`
fell through to ``ProcessSandbox``. That backend confines the working
directory, the child environment, output size and — on POSIX — CPU,
address-space and file-size rlimits. It does **not** deny network access and
does **not** confine the filesystem, because no host gave Phase 1 a portable
mechanism for either.

Running task-generated code that way inside the API container means the child
shares the API's uid, filesystem view and network namespace. This module turns
that posture into a *decision* instead of an accident:

* development / test: unset → ``process`` (local convenience preserved);
* production: ``SANDBOX_BACKEND`` MUST be explicit — a missing value is a
  configuration error and is never defaulted to an unconfined backend;
* production: ``process`` is refused unless the operator explicitly accepts
  the documented, non-isolated posture via
  ``SANDBOX_ALLOW_PROCESS_IN_PRODUCTION`` (truthy);
* every environment: an unknown backend name is refused; this policy never
  picks another backend on the caller's behalf.

Only the backend NAME is decided here. What a backend actually enforces is
reported by its own ``capabilities()`` / ``SandboxResult.enforced`` — a policy
decision is never evidence of isolation.
"""

from __future__ import annotations

import os
from typing import Mapping

__all__ = [
    "BACKENDS",
    "ISOLATED_BACKENDS",
    "MIN_WORKER_TOKEN_LEN",
    "PROCESS_ACK_ENV",
    "PRODUCTION_ENVIRONMENTS",
    "WORKER_TOKEN_ENV",
    "WORKER_URL_ENV",
    "SandboxConfigError",
    "is_production",
    "process_posture_notice",
    "resolve_backend_name",
    "validate_worker_endpoint",
]

#: ``worker`` is the dedicated execution-boundary backend (docs §10.7): the
#: API never receives runtime access; it sends a signed request to the
#: sandbox worker, which owns the runtime.
BACKENDS = ("process", "container", "worker")
#: Backends that put a real kernel/namespace boundary between the API and
#: task-generated code. ``container`` still requires a runtime in the API
#: container; ``worker`` moves that runtime to a separate service.
ISOLATED_BACKENDS = ("container", "worker")
PRODUCTION_ENVIRONMENTS = ("production",)

WORKER_URL_ENV = "SANDBOX_WORKER_URL"
WORKER_TOKEN_ENV = "SANDBOX_WORKER_TOKEN"
#: Minimum HMAC secret length; a short shared key is not a boundary.
MIN_WORKER_TOKEN_LEN = 32

#: Explicit acknowledgement of the documented non-isolated production posture.
#: Never a default; see docs/deployment.md §10.
PROCESS_ACK_ENV = "SANDBOX_ALLOW_PROCESS_IN_PRODUCTION"

_TRUTHY = ("1", "true", "yes", "on")


class SandboxConfigError(ValueError):
    """A sandbox backend selection the deployment policy forbids.

    Subclasses ``ValueError``: an invalid configuration value, the same
    family as the pre-existing ``invalid SANDBOX_BACKEND`` error, so callers
    that already handle a configuration error keep working.
    """


def validate_worker_endpoint(url: str, token: str) -> None:
    """Validate the worker backend's endpoint and shared key — fail closed.

    Called by the API-side client AND by the worker's own startup, from the
    same module, so the two can never disagree about what a usable boundary
    is. Raises :class:`SandboxConfigError` naming variables, never values.
    """
    from urllib.parse import urlsplit

    text = (url or "").strip()
    if not text:
        raise SandboxConfigError(
            f"{WORKER_URL_ENV} is required when SANDBOX_BACKEND=worker "
            "(e.g. http://sandbox-worker:8100) — there is no default "
            "execution endpoint"
        )
    try:
        parts = urlsplit(text)
    except ValueError:
        raise SandboxConfigError(
            f"{WORKER_URL_ENV} is not a valid URL"
        ) from None
    if parts.scheme not in ("http", "https"):
        raise SandboxConfigError(
            f"{WORKER_URL_ENV} must use http:// or https://"
        )
    if not parts.hostname:
        raise SandboxConfigError(f"{WORKER_URL_ENV} must name a host")
    if parts.username or parts.password:
        raise SandboxConfigError(
            f"{WORKER_URL_ENV} must not embed credentials; the shared key is "
            f"sent as an HMAC signature ({WORKER_TOKEN_ENV})"
        )
    if not (token or "").strip():
        raise SandboxConfigError(
            f"{WORKER_TOKEN_ENV} is required when SANDBOX_BACKEND=worker: the "
            "API-to-worker hop must be authenticated"
        )
    if len((token or "").strip()) < MIN_WORKER_TOKEN_LEN:
        raise SandboxConfigError(
            f"{WORKER_TOKEN_ENV} must be at least {MIN_WORKER_TOKEN_LEN} "
            "characters (generate with: openssl rand -hex 32)"
        )

def _lookup(environ: Mapping[str, str] | None, name: str) -> tuple[bool, str]:
    """``(present, stripped-value)`` for ``name`` — absent is not empty.

    An explicitly empty value stays a configuration error (as before); only a
    genuinely absent variable may take the development default.
    """
    if environ is None:
        if name not in os.environ:
            return False, ""
        return True, (os.environ.get(name) or "").strip()
    if name not in environ:
        return False, ""
    return True, (environ.get(name) or "").strip()


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def is_production(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``ENVIRONMENT`` names a production deployment."""
    _present, value = _lookup(environ, "ENVIRONMENT")
    return value.lower() in PRODUCTION_ENVIRONMENTS


def resolve_backend_name(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured backend name (``process`` | ``container``).

    Raises :class:`SandboxConfigError` — a ``ValueError`` — for an invalid
    value, for a production deployment that declares no backend, and for
    ``process`` in production without the explicit acknowledgement.
    """
    present, backend = _lookup(environ, "SANDBOX_BACKEND")
    production = is_production(environ)

    if not present:
        if production:
            raise SandboxConfigError(
                "SANDBOX_BACKEND must be set explicitly when "
                "ENVIRONMENT=production: task-generated code is never run "
                "unconfined by default. Set SANDBOX_BACKEND=worker for the "
                "dedicated execution service (recommended; the API never "
                "touches a runtime), or SANDBOX_BACKEND=container to run "
                "untrusted steps in an isolated container from the API "
                "process (a reachable runtime is required), or acknowledge "
                "the documented non-isolated posture with "
                f"{PROCESS_ACK_ENV}=1 (see docs/deployment.md §10)"
            )
        return "process"

    backend = backend.lower()
    if backend not in BACKENDS:
        raise SandboxConfigError(
            f"invalid SANDBOX_BACKEND {backend!r}: expected 'process', "
            "'container' or 'worker'"
        )

    if backend == "worker":
        # The isolated boundary cannot be half-configured: an endpoint or key
        # that is missing is a configuration error, never a local fallback.
        _u_present, url = _lookup(environ, WORKER_URL_ENV)
        _t_present, token = _lookup(environ, WORKER_TOKEN_ENV)
        validate_worker_endpoint(url, token)

    if production and backend == "process":
        _ack_present, ack = _lookup(environ, PROCESS_ACK_ENV)
        if not _is_truthy(ack):
            raise SandboxConfigError(
                "SANDBOX_BACKEND=process is refused when ENVIRONMENT=production: "
                "the process backend denies neither network access nor "
                "filesystem access for task-generated code. Use "
                "SANDBOX_BACKEND=worker (dedicated execution service) or "
                "SANDBOX_BACKEND=container with an isolated runtime, or "
                "acknowledge the documented non-isolated posture with "
                f"{PROCESS_ACK_ENV}=1 (see docs/deployment.md §10)"
            )
    return backend


def process_posture_notice(environ: Mapping[str, str] | None = None) -> str | None:
    """Honest one-line notice, or ``None`` when the posture is confined.

    Returned (not logged) so the caller owns its logging and tests can assert
    the wording without capturing log records.
    """
    present, backend = _lookup(environ, "SANDBOX_BACKEND")
    if present and backend.lower() == "process" and is_production(environ):
        return (
            "SANDBOX: production is running the UNCONFINED process backend "
            f"({PROCESS_ACK_ENV} acknowledged): task-generated code shares the "
            "API uid, filesystem and network namespace; see "
            "docs/deployment.md §10"
        )
    return None
