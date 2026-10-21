"""Per-process registry of live action ids (V3 Phase 1 anti-spoof support).

The registry creates one ``action_id`` per adapter invocation and publishes it
on the trusted ``AdapterContext``. The writer accepts an adapter-originated
event only when its ``action_id`` is in this set, so an adapter cannot invent
an invocation (spec §6.4, T16).

The set is bounded: entries expire after ``TTL_SECONDS`` and are pruned on
every registration/lookup, so a long-lived process cannot accumulate them.
"""

from __future__ import annotations

import threading
import time

TTL_SECONDS = 300.0

_LIVE: dict[str, float] = {}
_LOCK = threading.Lock()


def _prune(now: float) -> None:
    expired = [key for key, seen in _LIVE.items() if now - seen > TTL_SECONDS]
    for key in expired:
        _LIVE.pop(key, None)


def register(action_id: str) -> None:
    """Mark an action id as issued by the registry for this invocation."""
    if not action_id:
        return
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _LIVE[action_id] = now


def discard(action_id: str) -> None:
    """Forget an action id once the invocation has finished."""
    if not action_id:
        return
    with _LOCK:
        _LIVE.pop(action_id, None)


def is_live(action_id: str | None) -> bool:
    """True only for an action id the registry issued and has not retired."""
    if not action_id:
        return False
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        return action_id in _LIVE


def live_count() -> int:
    with _LOCK:
        return len(_LIVE)


def clear() -> None:
    with _LOCK:
        _LIVE.clear()
