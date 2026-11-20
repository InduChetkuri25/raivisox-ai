"""Execution-worker wire protocol — authentication, validation, idempotency.

The production isolation boundary (docs/deployment.md §10.7) puts the container
runtime on a dedicated, narrow service instead of on the API process:

    API  --(HMAC-signed HTTP)-->  sandbox-worker  --(docker/podman)-->  throwaway container

This module is the *shared* half: it is imported by both ends so the two can
never drift. It contains only pure logic (no HTTP server, no runtime access),
which is what makes the security rules unit-testable without Docker.

Trust model
-----------
- The **worker** is the only process with container-runtime access. It holds
  no database credentials, no application secret and no LLM keys: its whole
  environment is a shared HMAC key plus its own limits.
- The **API** is the only caller. It authenticates with a shared secret
  (``SANDBOX_WORKER_TOKEN``) via an HMAC-SHA256 signature over
  ``timestamp \\n nonce \\n sha256(body)``. Bearer-token-only auth would let a
  captured request be replayed; the signature binds the exact bytes, the
  nonce makes a replay detectable and the timestamp bounds the window.
- **Provenance is asserted by the API, derived from the authenticated task
  context — never from tool parameters.** The worker validates the shape and
  echoes it back; artifact ownership is decided by the workspace scope the
  worker itself enforces (the request can only name a subtree of the worker's
  workspace root, which the API minted from server-side state).

Nothing here is a substitute for the runtime guarantees: those are reported by
``ContainerSandbox.enforced`` and measured by the CI isolation gate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "AUTH_HEADER",
    "NONCE_HEADER",
    "PROTOCOL_VERSION",
    "TIMESTAMP_HEADER",
    "SignatureError",
    "AuthReplayCache",
    "RequestValidationError",
    "ExecutionRequest",
    "ArtifactLimits",
    "ArtifactEntry",
    "collect_artifacts",
    "MAX_CLOCK_SKEW_S",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_TIMEOUT_S",
    "DEFAULT_LIMITS",
    "sign_request",
    "verify_request",
    "WorkerLimits",
]

PROTOCOL_VERSION = "1"

AUTH_HEADER = "X-Raivisox-Signature"
TIMESTAMP_HEADER = "X-Raivisox-Timestamp"
NONCE_HEADER = "X-Raivisox-Nonce"

#: A signature older (or newer) than this is refused — a captured request
#: cannot be replayed later even if the nonce cache was lost on restart.
MAX_CLOCK_SKEW_S = 300

DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
DEFAULT_MAX_TIMEOUT_S = 300.0
_MAX_ARGV = 64
_MAX_ARG_LEN = 4096
_MAX_ENV_KEYS = 24

_IDENT = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")
_SAFE_REL = re.compile(r"^[A-Za-z0-9._/@+-]{1,1024}$")


class SignatureError(Exception):
    """The request is not provably from the API (or was replayed)."""


class RequestValidationError(ValueError):
    """The signed request is well-authenticated but not a valid execution."""


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical(timestamp: str, nonce: str, body: bytes) -> bytes:
    return f"{timestamp}\n{nonce}\n{_body_digest(body)}".encode()


def sign_request(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Hex HMAC-SHA256 over the canonical request form."""
    if not secret:
        raise SignatureError("no worker token configured")
    return hmac.new(secret.encode(), _canonical(timestamp, nonce, body),
                    hashlib.sha256).hexdigest()


def verify_request(secret: str, timestamp: str | None, nonce: str | None,
                   signature: str | None, body: bytes, *,
                   now: float | None = None,
                   replay: "AuthReplayCache | None" = None) -> None:
    """Raise :class:`SignatureError` unless the request is fresh and authentic.

    Checked in this order (each check is a refusal, never a warning):
    configuration, presence, timestamp shape and skew, nonce shape and
    freshness, then the constant-time signature comparison. The nonce is
    consumed only *after* the signature matched, so an unauthenticated caller
    cannot burn the API's nonces.
    """
    if not secret:
        raise SignatureError("worker token is not configured")
    if not timestamp or not nonce or not signature:
        raise SignatureError("missing authentication headers")
    if not _IDENT.match(nonce) or len(nonce) < 16:
        raise SignatureError("malformed nonce")
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("malformed timestamp") from None

    current = time.time() if now is None else now
    if abs(current - sent_at) > MAX_CLOCK_SKEW_S:
        raise SignatureError(
            f"request timestamp outside the ±{MAX_CLOCK_SKEW_S}s window"
        )

    expected = sign_request(secret, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise SignatureError("signature mismatch")

    if replay is not None and not replay.consume(nonce, now=current):
        raise SignatureError("nonce already used (replay refused)")


class AuthReplayCache:
    """Bounded, TTL'd set of accepted nonces.

    Bounded so an authenticated-but-hostile caller cannot grow it without
    limit; sized well above any realistic request rate inside the skew window.
    """

    def __init__(self, capacity: int = 8192,
                 ttl_s: float = MAX_CLOCK_SKEW_S * 2) -> None:
        self._capacity = capacity
        self._ttl_s = ttl_s
        self._seen: dict[str, float] = {}

    def consume(self, nonce: str, *, now: float | None = None) -> bool:
        """Record ``nonce``; ``False`` when it was already used."""
        current = time.monotonic() if now is None else now
        self._evict(current)
        if nonce in self._seen:
            return False
        if len(self._seen) >= self._capacity:
            # Fail closed under pressure: refuse rather than forget.
            return False
        self._seen[nonce] = current
        return True

    def _evict(self, now: float) -> None:
        expired = [n for n, ts in self._seen.items() if now - ts > self._ttl_s]
        for n in expired:
            del self._seen[n]


# ---------------------------------------------------------------------------
# Worker-side limits (the ceiling a caller may never raise)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerLimits:
    """Hard ceilings the worker applies to every run, regardless of request.

    A client may ask for *less*, never more. Values come from the worker's own
    environment so the boundary is decided by the operator deploying the
    worker, not by the caller of the API.
    """

    max_timeout_s: float = DEFAULT_MAX_TIMEOUT_S
    max_memory_bytes: int = 512 * 1024 * 1024
    default_memory_bytes: int = 256 * 1024 * 1024
    max_pids: int = 64
    max_cpu_seconds: int = 300
    max_fsize_bytes: int = 64 * 1024 * 1024
    default_fsize_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_artifacts: int = 200
    max_artifact_bytes: int = 4 * 1024 * 1024
    max_artifact_total_bytes: int = 16 * 1024 * 1024
    workspace_root: str = "/app/workspaces/tasks"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "WorkerLimits":
        env = os.environ if environ is None else environ

        def _num(name: str, default: float, cast=float):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = cast(raw)
            except ValueError:
                raise RequestValidationError(
                    f"{name} must be numeric, got a non-numeric value"
                ) from None
            if value <= 0:
                raise RequestValidationError(f"{name} must be positive")
            return value

        return cls(
            max_timeout_s=_num("SANDBOX_WORKER_MAX_TIMEOUT_S", DEFAULT_MAX_TIMEOUT_S),
            max_memory_bytes=int(_num("SANDBOX_WORKER_MAX_MEMORY_BYTES",
                                      512 * 1024 * 1024, int)),
            default_memory_bytes=int(_num("SANDBOX_WORKER_MEMORY_BYTES",
                                          256 * 1024 * 1024, int)),
            max_pids=int(_num("SANDBOX_WORKER_MAX_PIDS", 64, int)),
            max_cpu_seconds=int(_num("SANDBOX_WORKER_MAX_CPU_SECONDS", 300, int)),
            max_fsize_bytes=int(_num("SANDBOX_WORKER_MAX_FSIZE_BYTES",
                                     64 * 1024 * 1024, int)),
            default_fsize_bytes=int(_num("SANDBOX_WORKER_FSIZE_BYTES",
                                        16 * 1024 * 1024, int)),
            max_output_bytes=int(_num("SANDBOX_WORKER_MAX_OUTPUT_BYTES",
                                      DEFAULT_MAX_OUTPUT_BYTES, int)),
            max_artifacts=int(_num("SANDBOX_WORKER_MAX_ARTIFACTS", 200, int)),
            max_artifact_bytes=int(_num("SANDBOX_WORKER_MAX_ARTIFACT_BYTES",
                                        4 * 1024 * 1024, int)),
            max_artifact_total_bytes=int(
                _num("SANDBOX_WORKER_MAX_ARTIFACT_TOTAL_BYTES", 16 * 1024 * 1024, int)),
            workspace_root=(env.get("SANDBOX_WORKER_WORKSPACE_ROOT")
                            or "/app/workspaces/tasks").strip(),
        )


@dataclass(frozen=True)
class ArtifactLimits:
    """Bounded artifact manifest policy for ONE run."""

    max_files: int = 200
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_limits(cls, limits: WorkerLimits) -> "ArtifactLimits":
        return cls(max_files=limits.max_artifacts,
                   max_file_bytes=limits.max_artifact_bytes,
                   max_total_bytes=limits.max_artifact_total_bytes)


@dataclass(frozen=True)
class ArtifactEntry:
    """One file the run produced, inside the run's own workspace scope."""

    path: str          # POSIX-relative, never absolute, never escaping
    size: int
    sha256: str

    def to_payload(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionRequest:
    """A validated execution request — every field is server-checked.

    Constructed only through :meth:`from_payload`, which is the single place
    that decides what a caller is allowed to ask for.
    """

    request_id: str
    task_id: str
    run_id: str
    step_id: str
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    cpu_seconds: int = 0
    memory_bytes: int = 0
    max_pids: int = 0
    fsize_bytes: int = 0
    output_limit_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    # -- construction ------------------------------------------------------

    @classmethod
    def from_payload(cls, payload: Any, limits: WorkerLimits) -> "ExecutionRequest":
        if not isinstance(payload, dict):
            raise RequestValidationError("request body must be a JSON object")
        if str(payload.get("protocol_version", "")) != PROTOCOL_VERSION:
            raise RequestValidationError(
                f"unsupported protocol_version (expected {PROTOCOL_VERSION})"
            )

        request_id = _as_ident(payload.get("request_id"), "request_id")
        try:
            uuid.UUID(request_id)
        except ValueError:
            raise RequestValidationError("request_id must be a UUID") from None

        provenance = payload.get("provenance")
        if not isinstance(provenance, dict):
            raise RequestValidationError("provenance object is required")
        task_id = _as_ident(provenance.get("task_id"), "provenance.task_id")
        run_id = _as_ident(provenance.get("run_id"), "provenance.run_id")
        step_id = provenance.get("step_id") or ""
        if step_id:
            step_id = _as_ident(step_id, "provenance.step_id")

        spec = payload.get("spec")
        if not isinstance(spec, dict):
            raise RequestValidationError("spec object is required")

        argv_raw = spec.get("argv")
        if not isinstance(argv_raw, list) or not argv_raw:
            raise RequestValidationError("spec.argv must be a non-empty list")
        if len(argv_raw) > _MAX_ARGV:
            raise RequestValidationError(f"spec.argv exceeds {_MAX_ARGV} elements")
        argv: list[str] = []
        for item in argv_raw:
            if not isinstance(item, str) or not item:
                raise RequestValidationError("spec.argv entries must be non-empty strings")
            if "\x00" in item:
                raise RequestValidationError("spec.argv entries must not contain NUL")
            if len(item) > _MAX_ARG_LEN:
                raise RequestValidationError("spec.argv entry is too long")
            argv.append(item)

        cwd = spec.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise RequestValidationError("spec.cwd must be an absolute path")

        env_raw = spec.get("env") or {}
        if not isinstance(env_raw, dict) or len(env_raw) > _MAX_ENV_KEYS:
            raise RequestValidationError("spec.env must be a small object")
        env: dict[str, str] = {}
        for key, value in env_raw.items():
            if not isinstance(key, str) or not _IDENT.match(key):
                raise RequestValidationError("spec.env keys must be identifiers")
            if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
                raise RequestValidationError("spec.env values must be short strings")
            env[key] = value

        timeout_s = _clamp_float(spec.get("timeout_s", 30.0), "spec.timeout_s",
                                 0.1, limits.max_timeout_s)
        cpu_seconds = _clamp_int(spec.get("cpu_seconds", 0), "spec.cpu_seconds",
                                 0, limits.max_cpu_seconds)
        memory_bytes = _clamp_int(spec.get("memory_bytes", 0), "spec.memory_bytes",
                                  0, limits.max_memory_bytes)
        max_pids = _clamp_int(spec.get("max_pids", 0), "spec.max_pids",
                              0, limits.max_pids)
        fsize_bytes = _clamp_int(spec.get("fsize_bytes", 0), "spec.fsize_bytes",
                                 0, limits.max_fsize_bytes)
        output_limit_bytes = _clamp_int(
            spec.get("output_limit_bytes", DEFAULT_MAX_OUTPUT_BYTES),
            "spec.output_limit_bytes", 1, limits.max_output_bytes)

        return cls(
            request_id=request_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            argv=tuple(argv),
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            cpu_seconds=cpu_seconds or min(int(timeout_s) + 1,
                                           limits.max_cpu_seconds),
            memory_bytes=memory_bytes or limits.default_memory_bytes,
            max_pids=max_pids or limits.max_pids,
            fsize_bytes=fsize_bytes or limits.default_fsize_bytes,
            output_limit_bytes=output_limit_bytes,
        )

    # -- serialization -----------------------------------------------------

    @property
    def provenance(self) -> dict[str, str]:
        payload = {"task_id": self.task_id, "run_id": self.run_id}
        if self.step_id:
            payload["step_id"] = self.step_id
        return payload

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": self.request_id,
            "provenance": self.provenance,
            "spec": {
                "argv": list(self.argv),
                "cwd": self.cwd,
                "env": dict(self.env),
                "timeout_s": self.timeout_s,
                "cpu_seconds": self.cpu_seconds,
                "memory_bytes": self.memory_bytes,
                "max_pids": self.max_pids,
                "fsize_bytes": self.fsize_bytes,
                "output_limit_bytes": self.output_limit_bytes,
            },
        }


def _as_ident(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError(f"{name} is required")
    text = value.strip()
    if not _IDENT.match(text):
        raise RequestValidationError(
            f"{name} contains characters outside [A-Za-z0-9._:@+-]"
        )
    return text


def _clamp_float(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestValidationError(f"{name} must be a number")
    if value <= 0:
        raise RequestValidationError(f"{name} must be positive")
    return float(min(max(float(value), low), high))


def _clamp_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestValidationError(f"{name} must be an integer")
    if value < 0:
        raise RequestValidationError(f"{name} must not be negative")
    return int(min(max(value, low), high))


# ---------------------------------------------------------------------------
# Artifact manifest collection (worker side, after the container exits)
# ---------------------------------------------------------------------------

def collect_artifacts(root: str, limits: ArtifactLimits,
                      *, max_depth: int = 32) -> tuple[list[ArtifactEntry], list[str]]:
    """Walk ``root`` and return a bounded manifest plus honest notes.

    Only regular files whose real path stays inside ``root`` are listed:
    symlinks are skipped (a link could point at the worker's filesystem, and
    the container's namespace differs from the worker's), as are FIFOs and
    devices. Directories deeper than ``max_depth`` and files over the size cap
    are called out in ``notes`` — never silently included.

    The manifest carries no bytes: the API and the worker share the workspace
    volume, so the API reads the file through its own containment check. That
    keeps stdout/env small and makes the worker's output bounded.
    """
    from services.security.path_containment import is_contained, realpath_norm

    root_abs = os.path.abspath(root)
    entries: list[ArtifactEntry] = []
    notes: list[str] = []
    total = 0

    if not os.path.isdir(root_abs):
        return [], [f"workspace {root_abs!r} is not a directory"]

    for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in ("__pycache__", ".git"))
        rel_dir = os.path.relpath(dirpath, root_abs)
        depth = 0 if rel_dir == os.curdir else rel_dir.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            notes.append(f"depth limit {max_depth} reached at {rel_dir}")
            continue
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except OSError:
                notes.append(f"unreadable entry {rel}")
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            if not is_contained(full, root_abs):
                notes.append(f"entry outside the workspace scope refused: {rel}")
                continue
            if not _SAFE_REL.match(rel):
                notes.append(f"entry with an unsupported name refused: {rel}")
                continue
            if st.st_size > limits.max_file_bytes:
                notes.append(f"artifact over the per-file cap skipped: {rel}")
                continue
            if total + st.st_size > limits.max_total_bytes:
                notes.append("artifact total cap reached; remaining files skipped")
                return entries, notes
            if len(entries) >= limits.max_files:
                notes.append("artifact count cap reached; remaining files skipped")
                return entries, notes
            digest = hashlib.sha256()
            try:
                with open(full, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        digest.update(chunk)
            except OSError:
                notes.append(f"unreadable entry {rel}")
                continue
            entries.append(ArtifactEntry(path=rel, size=st.st_size,
                                         sha256=digest.hexdigest()))
            total += st.st_size

    # Sanity: the manifest names only paths that resolve inside the root.
    for entry in entries:
        if not is_contained(os.path.join(root_abs, entry.path), root_abs):
            raise RequestValidationError(
                f"artifact {entry.path!r} escaped the workspace scope"
            )
    assert realpath_norm(root_abs)  # never empty; documents the invariant
    return entries, notes


def json_dumps(payload: Any) -> bytes:
    """Canonical-ish body used for signing (stable key order, no whitespace)."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def iter_provenance(payload: Mapping[str, Any]) -> Iterable[str]:
    """Convenience for logging: the provenance identifiers, never the argv."""
    prov = payload.get("provenance") or {}
    for key in ("task_id", "run_id", "step_id"):
        value = prov.get(key)
        if value:
            yield f"{key}={value}"
