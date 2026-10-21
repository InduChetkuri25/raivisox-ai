"""Trusted identity for V3 events (spec §6).

Identity never comes from tool arguments, request bodies or adapter scratch
data. It is resolved once per execution run from authoritative rows:

- ``user_id`` is ``tasks.user_id`` (the ownership key the API already enforces);
- ``run_id`` must be an ``execution_runs`` row belonging to that same task.

Any mismatch is an integrity fault, not a runtime condition: it raises
``ProvenanceError`` before any event is written, so an unprovable run never
gets to look provable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text


DEFAULT_ACTOR_ID = "pipeline-service"


class ProvenanceError(RuntimeError):
    """Raised when a subject identity cannot be tied to authoritative rows."""


@dataclass(frozen=True)
class Provenance:
    """Verified subject of one execution run."""

    task_id: str
    user_id: str
    run_id: str
    actor_id: str
    actor_type: str = "service"
    step_node_id: Optional[str] = None

    def fields(self) -> dict[str, Any]:
        """Keyword arguments for kernel/audit linkage."""
        return {
            "user_id": self.user_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "step_node_id": self.step_node_id,
        }


async def verify_subject(
    session,
    *,
    task_id: str,
    user_id: str,
    run_id: str | None = None,
) -> bool:
    """Check that ``task_id`` exists, owns ``user_id`` and (if given) ``run_id``.

    One query. Returns False on any mismatch — never guesses.
    """
    if not task_id or not user_id:
        return False
    if run_id:
        row = (
            await session.execute(
                text(
                    "SELECT t.user_id FROM tasks t "
                    "JOIN execution_runs r ON r.task_id = t.id "
                    "WHERE t.id = :task AND r.id = :run"
                ),
                {"task": task_id, "run": run_id},
            )
        ).first()
    else:
        row = (
            await session.execute(
                text("SELECT user_id FROM tasks WHERE id = :task"),
                {"task": task_id},
            )
        ).first()
    if row is None:
        return False
    return str(row[0]) == str(user_id)


async def resolve_run_provenance(
    db,
    task_id: str,
    run_id: str,
    actor_id: str = DEFAULT_ACTOR_ID,
) -> Provenance:
    """Resolve and verify the identity of one execution run.

    Raises ``ProvenanceError`` when the task does not exist or the run does not
    belong to it. Called once per run, never per event.
    """
    if not task_id or not run_id:
        raise ProvenanceError("provenance requires both a task and a run id")
    row = (
        await db.execute(
            text(
                "SELECT t.user_id, r.task_id FROM tasks t "
                "JOIN execution_runs r ON r.task_id = t.id "
                "WHERE t.id = :task AND r.id = :run"
            ),
            {"task": task_id, "run": run_id},
        )
    ).first()
    if row is None:
        raise ProvenanceError(
            "execution run does not belong to the given task; refusing to emit events"
        )
    user_id, owner_task_id = str(row[0]), str(row[1])
    if owner_task_id != str(task_id) or not user_id:
        raise ProvenanceError(
            "execution run does not belong to the given task; refusing to emit events"
        )
    return Provenance(
        task_id=str(task_id),
        user_id=user_id,
        run_id=str(run_id),
        actor_id=actor_id,
    )
