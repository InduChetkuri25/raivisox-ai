"""V3 Phase 1 event vocabulary: closed sets, allowlists, normalizers.

This module is deliberately pure — no database, no app imports — so the
contract can be unit-tested and reviewed without a running API.

Everything here mirrors docs/v3-phase-1-spec.md §3. Adding an event type is a
reviewed schema change (design §5.2), not a configuration value.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_TARGET_LENGTH = 512

# --- event types (Phase 1 closed set, spec §3.3) ---------------------------

TOOL_ATTEMPTED = "tool.attempted"
TOOL_ALLOWED = "tool.allowed"
TOOL_DENIED = "tool.denied"
TOOL_EXECUTED = "tool.executed"
TOOL_FAILED = "tool.failed"
RESOURCE_READ_OBSERVED = "resource.read.observed"
RESOURCE_READ_DENIED = "resource.read.denied"
RESOURCE_WRITE_COMMITTED = "resource.write.committed"
RESOURCE_WRITE_DENIED = "resource.write.denied"
ENDPOINT_ATTEMPTED = "endpoint.connect.attempted"
ENDPOINT_SUCCEEDED = "endpoint.connect.succeeded"
ENDPOINT_DENIED = "endpoint.connect.denied"
ARTIFACT_REGISTERED = "artifact.registered"

EVENT_TYPES = frozenset(
    {
        TOOL_ATTEMPTED,
        TOOL_ALLOWED,
        TOOL_DENIED,
        TOOL_EXECUTED,
        TOOL_FAILED,
        RESOURCE_READ_OBSERVED,
        RESOURCE_READ_DENIED,
        RESOURCE_WRITE_COMMITTED,
        RESOURCE_WRITE_DENIED,
        ENDPOINT_ATTEMPTED,
        ENDPOINT_SUCCEEDED,
        ENDPOINT_DENIED,
        ARTIFACT_REGISTERED,
    }
)

# --- outcome vocabulary (spec §3.4) ----------------------------------------

ATTEMPTED = "attempted"
ALLOWED = "allowed"
DENIED = "denied"
EXECUTED = "executed"
FAILED = "failed"
INTERRUPTED = "interrupted"
CANCELLED = "cancelled"
EXPIRED = "expired"

OUTCOMES = frozenset(
    {ATTEMPTED, ALLOWED, DENIED, EXECUTED, FAILED, INTERRUPTED, CANCELLED, EXPIRED}
)

OUTCOMES_BY_EVENT: dict[str, frozenset[str]] = {
    TOOL_ATTEMPTED: frozenset({ATTEMPTED}),
    TOOL_ALLOWED: frozenset({ALLOWED}),
    TOOL_DENIED: frozenset({DENIED}),
    TOOL_EXECUTED: frozenset({EXECUTED}),
    TOOL_FAILED: frozenset({FAILED}),
    RESOURCE_READ_OBSERVED: frozenset({EXECUTED, FAILED}),
    RESOURCE_READ_DENIED: frozenset({DENIED}),
    RESOURCE_WRITE_COMMITTED: frozenset({EXECUTED}),
    RESOURCE_WRITE_DENIED: frozenset({DENIED}),
    ENDPOINT_ATTEMPTED: frozenset({ATTEMPTED}),
    ENDPOINT_SUCCEEDED: frozenset({EXECUTED}),
    ENDPOINT_DENIED: frozenset({DENIED}),
    ARTIFACT_REGISTERED: frozenset({EXECUTED}),
}

# --- evidence / actor vocabulary -------------------------------------------

OBSERVED = "observed"
VERIFIED = "verified"
EVIDENCE_CLASSES = frozenset({OBSERVED, VERIFIED})

ACTOR_TYPES = frozenset({"service", "user", "system"})

# Types that must always be "verified": they are emitted only after an
# independent row corroborates the claim (spec §5.7).
VERIFIED_ONLY_TYPES = frozenset({ARTIFACT_REGISTERED})

# --- fixed source strings (module:function, never user data) ---------------

SOURCE_BY_EVENT: dict[str, str] = {
    TOOL_ATTEMPTED: "integrations.adapters.registry:AdapterRegistry.run",
    TOOL_ALLOWED: "integrations.adapters.registry:AdapterRegistry.run",
    TOOL_DENIED: "integrations.adapters.registry:AdapterRegistry.run",
    TOOL_EXECUTED: "integrations.adapters.registry:AdapterRegistry.run",
    TOOL_FAILED: "integrations.adapters.registry:AdapterRegistry.run",
    RESOURCE_READ_OBSERVED: "integrations.adapters.builtin:FileReadAdapter.run",
    RESOURCE_READ_DENIED: "integrations.adapters.builtin:FileReadAdapter.run",
    RESOURCE_WRITE_COMMITTED: "integrations.adapters.builtin:FileWriteAdapter.run",
    RESOURCE_WRITE_DENIED: "integrations.adapters.builtin:FileWriteAdapter.run",
    ENDPOINT_ATTEMPTED: "integrations.adapters.http_get:HttpGetAdapter.run",
    ENDPOINT_SUCCEEDED: "integrations.adapters.http_get:HttpGetAdapter.run",
    ENDPOINT_DENIED: "integrations.adapters.http_get:HttpGetAdapter.run",
    ARTIFACT_REGISTERED: "apps.api.app.main:_register_task_artifact",
}

# --- payload allowlist (spec §3.6) ----------------------------------------

PAYLOAD_ALLOWLIST: dict[str, frozenset[str]] = {
    TOOL_ATTEMPTED: frozenset({"resource_ref"}),
    TOOL_ALLOWED: frozenset({"granted_permissions"}),
    TOOL_DENIED: frozenset({"denied_permissions", "reason"}),
    TOOL_EXECUTED: frozenset({"duration_ms"}),
    TOOL_FAILED: frozenset({"duration_ms", "error_kind", "denial_kind"}),
    RESOURCE_READ_OBSERVED: frozenset({"size", "encoding", "error_kind", "relpath"}),
    RESOURCE_READ_DENIED: frozenset({"reason", "requested_basename"}),
    RESOURCE_WRITE_COMMITTED: frozenset({"bytes", "created", "overwritten", "relpath"}),
    RESOURCE_WRITE_DENIED: frozenset({"reason", "bytes", "quota_kind"}),
    ENDPOINT_ATTEMPTED: frozenset({"host", "path_template"}),
    ENDPOINT_SUCCEEDED: frozenset({"host", "path_template", "status", "bytes"}),
    ENDPOINT_DENIED: frozenset({"host", "path_template", "reason", "denial_layer"}),
    ARTIFACT_REGISTERED: frozenset({"artifact_id", "sha256", "size_bytes", "filename"}),
}

# --- closed value sets used inside payloads --------------------------------

ERROR_KINDS = frozenset(
    {"not_found", "too_large", "unreadable", "exception", "adapter_failure", "unknown"}
)
DENIAL_KINDS = frozenset(
    {"outside_workspace", "quota", "unsupported", "permission", "unknown"}
)
DENIAL_LAYERS = frozenset({"adapter_policy", "kernel"})

# --- validation helpers ----------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9:_-]{1,100}$")
_NODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_REL_RE = re.compile(r"^[^\\:*?\"<>|\x00]{1,440}$")
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
_NET_PATH_RE = re.compile(r"^[A-Za-z0-9._~%/-]{0,300}$")

# Values that look like credentials are refused outright (never rewritten).
_SECRET_VALUE_RE = re.compile(r"^(sk-|sk_|ghp_|gho_|xox[baprs]-|AKIA[0-9A-Z]{16})")
_LONG_OPAQUE_RE = re.compile(r"^[A-Za-z0-9+/_=-]{40,}$")


class SchemaError(ValueError):
    """Raised when an event cannot be represented honestly."""


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def is_action_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ACTION_RE.match(value))


def is_step_node_id(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(_NODE_RE.match(value)))


def is_tool_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOOL_RE.match(value))


def valid_outcome(event_type: str, outcome: str) -> bool:
    allowed = OUTCOMES_BY_EVENT.get(event_type)
    return allowed is not None and outcome in allowed


def idempotency_key(event_type: str, *, action_id: str | None = None,
                    artifact_id: str | None = None) -> str:
    """Deterministic natural key (spec §3.5).

    A write retry reuses the same key; a second *execution* of the same step
    has a different ``action_id`` and is therefore a distinct action.
    """
    if event_type not in EVENT_TYPES:
        raise SchemaError(f"unknown event type {event_type!r}")
    if event_type == ARTIFACT_REGISTERED:
        if not artifact_id:
            raise SchemaError("artifact.registered requires an artifact id")
        return f"{event_type}:{artifact_id}"
    if not action_id:
        raise SchemaError(f"{event_type} requires an action id")
    return f"{event_type}:{action_id}"


# Keys whose values are structurally long-but-public (a content hash, a
# relative path, a host). The opaque-token heuristic must not fire on them or
# every verified artifact event would be rejected as a "secret".
_SAFE_VALUE_KEYS = frozenset(
    {
        "sha256",
        "artifact_id",
        "filename",
        "relpath",
        "requested_basename",
        "host",
        "path_template",
        "encoding",
    }
)


def _looks_secret(value: str, key: str = "") -> bool:
    if _SECRET_VALUE_RE.match(value):
        return True
    if key in _SAFE_VALUE_KEYS:
        return False
    # A long opaque token with no separators is indistinguishable from a
    # credential; refusing it is safer than storing it.
    return bool(_LONG_OPAQUE_RE.match(value)) and any(
        c.isdigit() for c in value
    ) and any(c.isalpha() for c in value)


def _scalar_ok(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return True
    return False


def filter_payload(event_type: str, payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop keys outside the allowlist; refuse ambiguous or credential-shaped data.

    Unknown keys are *dropped* (spec §3.6). A retained value that looks like a
    credential raises ``SchemaError`` so the caller can reject the whole event
    instead of storing a redacted guess.
    """
    if not payload:
        return None
    allowed = PAYLOAD_ALLOWLIST.get(event_type)
    if allowed is None:
        raise SchemaError(f"no payload allowlist for {event_type!r}")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) > 32 or not all(_scalar_ok(v) for v in items):
                raise SchemaError(f"payload key {key!r} is not a bounded scalar list")
            if any(isinstance(v, str) and _looks_secret(v, key) for v in items):
                raise SchemaError(f"payload key {key!r} contains a credential-shaped value")
            out[key] = items
        elif _scalar_ok(value):
            if isinstance(value, str) and _looks_secret(value, key):
                raise SchemaError(f"payload key {key!r} contains a credential-shaped value")
            out[key] = value
        else:
            raise SchemaError(f"payload key {key!r} is not a scalar")
    return out or None


def canonical_payload(payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """Return the canonical payload and its sha256 (stable across processes)."""
    canonical = json.dumps(
        payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if len(canonical.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise SchemaError("payload exceeds the 8 KiB cap")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (payload if payload else None), digest


def resource_target(task_id: str, relpath: str) -> str | None:
    """Build ``res:<task>/<scope-relative path>`` or return None if unsafe.

    Rejects rather than sanitizes: an absolute path, a parent traversal, a
    drive letter, a control character or an oversized path yields None, and
    the caller records the event without a target (spec §4).
    """
    if not is_uuid(task_id) or not isinstance(relpath, str):
        return None
    rel = relpath.replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    if re.match(r"^[A-Za-z]:", rel):
        return None
    if not _REL_RE.match(rel):
        return None
    target = f"res:{task_id}/{rel}"
    return target if len(target) <= MAX_TARGET_LENGTH else None


def endpoint_target(host: str, path: str) -> str | None:
    """Build ``net:<host>/<path>`` with no query string, port, or userinfo."""
    if not isinstance(host, str) or not host:
        return None
    host = host.lower()
    if not _HOST_RE.match(host):
        return None
    if not isinstance(path, str):
        return None
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if not _NET_PATH_RE.match(path):
        return None
    if len(path) > 300:
        return None
    target = f"net:{host}{path}"
    return target if len(target) <= MAX_TARGET_LENGTH else None


def validate_target(target: str | None) -> str | None:
    """Validate an already-built target; return None when it is not admissible."""
    if target is None:
        return None
    if not isinstance(target, str) or len(target) > MAX_TARGET_LENGTH:
        return None
    if target.startswith("res:"):
        rest = target[4:]
        task_id, _, rel = rest.partition("/")
        rebuilt = resource_target(task_id, rel)
        return rebuilt if rebuilt == target else None
    if target.startswith("net:"):
        rest = target[4:]
        host, _, path = rest.partition("/")
        rebuilt = endpoint_target(host, "/" + path)
        return rebuilt if rebuilt == target else None
    return None
