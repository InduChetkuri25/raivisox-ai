"""Per-process registry of live action ids (V3 Phase 1 anti-spoof support).

The registry creates one ``action_id`` per adapter invocation and publishes it
on the trusted ``AdapterContext``. The writer accepts an adapter-originated
event only when that id is live **and** the identity it carries agrees with the
trusted binding captured when the id was issued (spec §6.4, T16).

Liveness follows the invocation, never a wall-clock execution deadline:

- ``register(...)`` opens the action and records its binding (actor, user,
  task, run, step, tool) as supplied by the registry. Later events can neither
  widen that binding nor replace it.
- Every accepted event refreshes the entry (``validate``), so an invocation
  that is still producing evidence stays live however long it takes.
- ``discard(...)`` closes it deterministically in the invocation's ``finally``
  — success, refusal, exception and cancellation all reach it.
- ``IDLE_TTL_SECONDS`` is only a leak backstop for an entry whose owner never
  reached cleanup (a bug, or a task killed mid-invocation). It is measured
  from the last accepted activity, so it can never expire an active
  invocation, and it sits well above the longest bounded silence of a Phase 1
  invocation (codegen: 120 s per LLM call × 3 attempts × 3 repair rounds
  ≈ 1080 s between its first and last event).

Memory stays bounded: entries exist only for invocations that are active or
abandoned, and an abandoned entry is reaped after one idle window.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# Leak backstop, not an execution deadline: see the module docstring.
IDLE_TTL_SECONDS = 1800.0

# Fields captured at registration from the trusted context. A validating
# caller may omit a field (None/empty); it may never contradict one.
_BINDING_FIELDS = (
    "actor_id",
    "user_id",
    "task_id",
    "run_id",
    "step_node_id",
    "tool_id",
)

# action_id -> (last accepted activity, binding captured at registration)
_LIVE: dict[str, tuple[float, dict[str, str]]] = {}
_LOCK = threading.Lock()
_clock: Callable[[], float] = time.monotonic


def set_clock(clock: Callable[[], float] | None = None) -> None:
    """Inject the monotonic clock used for expiry (tests); ``None`` restores it."""
    global _clock
    _clock = clock if clock is not None else time.monotonic


def _prune(now: float) -> None:
    expired = [
        key for key, (seen, _binding) in _LIVE.items()
        if now - seen > IDLE_TTL_SECONDS
    ]
    for key in expired:
        _LIVE.pop(key, None)


def register(
    action_id: str,
    *,
    actor_id: str = "",
    user_id: str = "",
    task_id: str = "",
    run_id: str = "",
    step_node_id: str = "",
    tool_id: str = "",
) -> None:
    """Publish an action id with the trusted binding of its invocation.

    The registry calls this once per invocation, before any lifecycle event.
    An empty field is simply not bound; a bound field is immutable for the
    lifetime of the entry.
    """
    if not action_id:
        return
    binding = {
        "actor_id": actor_id or "",
        "user_id": user_id or "",
        "task_id": task_id or "",
        "run_id": run_id or "",
        "step_node_id": step_node_id or "",
        "tool_id": tool_id or "",
    }
    now = _clock()
    with _LOCK:
        _prune(now)
        _LIVE[action_id] = (now, binding)


def discard(action_id: str | None) -> None:
    """Forget an action id once the invocation has finished."""
    if not action_id:
        return
    with _LOCK:
        _LIVE.pop(action_id, None)


def touch(action_id: str | None) -> bool:
    """Refresh a live entry (no binding check); True when it was live."""
    if not action_id:
        return False
    now = _clock()
    with _LOCK:
        _prune(now)
        entry = _LIVE.get(action_id)
        if entry is None:
            return False
        _LIVE[action_id] = (now, entry[1])
        return True


def validate(
    action_id: str | None,
    *,
    actor_id: str | None = None,
    user_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    step_node_id: str | None = None,
    tool_id: str | None = None,
) -> bool:
    """True only for a live id whose supplied identity matches its binding.

    A field the caller does not supply is ignored; a field the caller supplies
    that contradicts the registration is a refusal (no counter, no write). A
    successful validation refreshes the entry, so use *is* the liveness
    refresh — there is no heartbeat to schedule and no deadline to race.
    """
    if not action_id:
        return False
    supplied = {
        "actor_id": actor_id or "",
        "user_id": user_id or "",
        "task_id": task_id or "",
        "run_id": run_id or "",
        "step_node_id": step_node_id or "",
        "tool_id": tool_id or "",
    }
    now = _clock()
    with _LOCK:
        _prune(now)
        entry = _LIVE.get(action_id)
        if entry is None:
            return False
        binding = entry[1]
        for field in _BINDING_FIELDS:
            bound = binding.get(field, "")
            if bound and supplied[field] and supplied[field] != bound:
                return False
        _LIVE[action_id] = (now, binding)
        return True


def is_live(action_id: str | None) -> bool:
    """Membership test + refresh, without a binding comparison.

    Kept for callers that only have an id (and for the documented anti-spoof
    rule). The writer uses :func:`validate` so a borrowed id is refused.
    """
    return touch(action_id)


def live_count() -> int:
    with _LOCK:
        return len(_LIVE)


def clear() -> None:
    """Drop every entry and restore the default clock (test hook)."""
    global _clock
    with _LOCK:
        _LIVE.clear()
    _clock = time.monotonic
